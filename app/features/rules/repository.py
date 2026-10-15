"""Rules repositories: the only place with SQL for this feature.

This module holds the repositories for colour rules, disciplinary rules and
leave rules, plus a read-only aggregation of attendance counts that the
evaluators consume.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, select

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.features.rules.models import (
    ColorRule,
    DisciplinaryAction,
    DisciplinaryRule,
    LeaveRule,
)


class ColorRuleRepository(BaseRepository[ColorRule]):
    """Queries over ``color_rules``."""

    model = ColorRule

    def _scope_filter(self, stmt: Select, department_id: int | None) -> Select:
        if department_id is None:
            return stmt
        return stmt.where(
            (ColorRule.department_id == department_id)
            | (ColorRule.department_id.is_(None))
        )

    def active_rules(self, department_id: int | None = None) -> list[ColorRule]:
        """Department rules override globals, ordered by priority descending."""
        stmt = self._scope_filter(select(ColorRule), department_id)
        stmt = stmt.order_by(ColorRule.priority.desc(), ColorRule.id)
        return list(db.session.execute(stmt).scalars().all())

    def list_all(self) -> list[ColorRule]:
        stmt = select(ColorRule).order_by(
            ColorRule.priority.desc(), ColorRule.department_id.nullsfirst()
        )
        return list(db.session.execute(stmt).scalars().all())

    def find(self, rule_id: int) -> ColorRule | None:
        """Fetch one colour rule by primary key."""
        stmt = select(ColorRule).where(ColorRule.id == rule_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def find_scoped(
        self,
        *,
        department_id: int | None,
        color: str,
        excluding_id: int | None = None,
    ) -> ColorRule | None:
        """Locate the rule that defines ``color`` within one scope.

        A scope may define each colour only once, so this doubles as the
        duplicate guard for creates and edits.
        """
        stmt = select(ColorRule).where(
            ColorRule.color == color,
            ColorRule.department_id.is_(None)
            if department_id is None
            else ColorRule.department_id == department_id,
        )
        if excluding_id is not None:
            stmt = stmt.where(ColorRule.id != excluding_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def for_department(self, department_id: int | None) -> list[ColorRule]:
        """Rules defined in exactly one scope (used for rule-set checks)."""
        stmt = select(ColorRule).where(
            ColorRule.department_id.is_(None)
            if department_id is None
            else ColorRule.department_id == department_id
        )
        return list(db.session.execute(stmt).scalars().all())

    def create_rule(
        self,
        *,
        color: str,
        department_id: int | None = None,
        min_lates: int = 0,
        min_absences: int = 0,
        window_days: int = 30,
        priority: int = 0,
    ) -> ColorRule:
        return self.create(
            color=color,
            department_id=department_id,
            min_lates=min_lates,
            min_absences=min_absences,
            window_days=window_days,
            priority=priority,
        )


class DisciplinaryRuleRepository(BaseRepository[DisciplinaryRule]):
    """Queries over ``disciplinary_rules``."""

    model = DisciplinaryRule

    def active_rules(self, department_id: int | None = None) -> list[DisciplinaryRule]:
        stmt = select(DisciplinaryRule).where(DisciplinaryRule.is_active.is_(True))
        if department_id is not None:
            stmt = stmt.where(
                (DisciplinaryRule.department_id == department_id)
                | (DisciplinaryRule.department_id.is_(None))
            )
        # Department-specific rules are more specific, so they come first.
        stmt = stmt.order_by(
            DisciplinaryRule.department_id.nullsfirst(),
            DisciplinaryRule.trigger_type,
            DisciplinaryRule.threshold,
        )
        return list(db.session.execute(stmt).scalars().all())

    def list_all(self) -> list[DisciplinaryRule]:
        stmt = select(DisciplinaryRule).order_by(
            DisciplinaryRule.department_id.nullsfirst(),
            DisciplinaryRule.trigger_type,
            DisciplinaryRule.threshold,
        )
        return list(db.session.execute(stmt).scalars().all())

    def find(self, rule_id: int) -> DisciplinaryRule | None:
        """Fetch one disciplinary rule by primary key."""
        stmt = select(DisciplinaryRule).where(DisciplinaryRule.id == rule_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def find_scoped(
        self,
        *,
        department_id: int | None,
        trigger_type: str,
        threshold: int,
        excluding_id: int | None = None,
    ) -> DisciplinaryRule | None:
        """Duplicate guard: same scope + trigger + threshold already defined."""
        stmt = select(DisciplinaryRule).where(
            DisciplinaryRule.trigger_type == trigger_type,
            DisciplinaryRule.threshold == threshold,
            DisciplinaryRule.department_id.is_(None)
            if department_id is None
            else DisciplinaryRule.department_id == department_id,
        )
        if excluding_id is not None:
            stmt = stmt.where(DisciplinaryRule.id != excluding_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def for_department(self, department_id: int | None) -> list[DisciplinaryRule]:
        """Rules defined in exactly one scope."""
        stmt = select(DisciplinaryRule).where(
            DisciplinaryRule.department_id.is_(None)
            if department_id is None
            else DisciplinaryRule.department_id == department_id
        )
        return list(db.session.execute(stmt).scalars().all())

    def create_rule(
        self,
        *,
        trigger_type: str,
        threshold: int,
        period: str,
        action: str,
        department_id: int | None = None,
        suspension_days: int = 0,
        auto_apply: bool = False,
        is_active: bool = True,
    ) -> DisciplinaryRule:
        return self.create(
            trigger_type=trigger_type,
            threshold=threshold,
            period=period,
            action=action,
            department_id=department_id,
            suspension_days=suspension_days,
            auto_apply=auto_apply,
            is_active=is_active,
        )


class DisciplinaryActionRepository(BaseRepository[DisciplinaryAction]):
    """Queries over ``disciplinary_actions``."""

    model = DisciplinaryAction

    def exists_for(
        self, *, employee_id: int, rule_id: int | None, triggered_on: date
    ) -> bool:
        """Honours ``unique(employee_id, rule_id, triggered_on)``."""
        stmt = select(DisciplinaryAction.id).where(
            DisciplinaryAction.employee_id == employee_id,
            DisciplinaryAction.triggered_on == triggered_on,
        )
        if rule_id is None:
            stmt = stmt.where(DisciplinaryAction.rule_id.is_(None))
        else:
            stmt = stmt.where(DisciplinaryAction.rule_id == rule_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def count_for_employee(self, employee_id: int) -> int:
        return self.count(employee_id=employee_id)

    def count_for_rule(self, rule_id: int) -> int:
        """How many recorded actions reference this rule.

        Used as a delete guard: historical actions must keep pointing at the
        rule that produced them.
        """
        return self.count(rule_id=rule_id)

    def counts_for_employees(self, employee_ids: list[int]) -> dict[int, int]:
        """employee_id -> action count, in one query."""
        if not employee_ids:
            return {}
        stmt = (
            select(
                DisciplinaryAction.employee_id.label("employee_id"),
                func.count(DisciplinaryAction.id).label("total"),
            )
            .where(DisciplinaryAction.employee_id.in_(employee_ids))
            .group_by(DisciplinaryAction.employee_id)
        )
        return {
            int(row["employee_id"]): int(row["total"] or 0) for row in self.rows(stmt)
        }

    def for_employee(self, employee_id: int, *, limit: int | None = None) -> list[DisciplinaryAction]:
        stmt = (
            select(DisciplinaryAction)
            .where(DisciplinaryAction.employee_id == employee_id)
            .order_by(DisciplinaryAction.triggered_on.desc(), DisciplinaryAction.id.desc())
        )
        if limit:
            stmt = stmt.limit(limit)
        return list(db.session.execute(stmt).scalars().all())

    def recent(self, *, limit: int = 10) -> list[dict]:
        """Recent actions with employee and rule context."""
        from app.features.employees.models import Employee

        stmt = (
            select(
                DisciplinaryAction.id.label("id"),
                DisciplinaryAction.action.label("action"),
                DisciplinaryAction.triggered_on.label("triggered_on"),
                DisciplinaryAction.details.label("details"),
                Employee.id.label("employee_id"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Employee.employee_no.label("employee_no"),
                DisciplinaryRule.trigger_type.label("trigger_type"),
                DisciplinaryRule.threshold.label("threshold"),
                DisciplinaryRule.period.label("period"),
            )
            .select_from(DisciplinaryAction)
            .join(Employee, Employee.id == DisciplinaryAction.employee_id)
            .join(DisciplinaryRule, DisciplinaryRule.id == DisciplinaryAction.rule_id, isouter=True)
            .order_by(DisciplinaryAction.triggered_on.desc(), DisciplinaryAction.id.desc())
            .limit(limit)
        )
        return self.rows(stmt)

    def for_employee_with_rules(self, employee_id: int) -> list[dict]:
        from app.features.employees.models import Employee

        stmt = (
            select(
                DisciplinaryAction.id.label("id"),
                DisciplinaryAction.action.label("action"),
                DisciplinaryAction.triggered_on.label("triggered_on"),
                DisciplinaryAction.details.label("details"),
                DisciplinaryAction.created_at.label("created_at"),
                DisciplinaryRule.trigger_type.label("trigger_type"),
                DisciplinaryRule.threshold.label("threshold"),
                DisciplinaryRule.period.label("period"),
                DisciplinaryRule.suspension_days.label("suspension_days"),
            )
            .select_from(DisciplinaryAction)
            .join(Employee, Employee.id == DisciplinaryAction.employee_id)
            .join(DisciplinaryRule, DisciplinaryRule.id == DisciplinaryAction.rule_id, isouter=True)
            .where(DisciplinaryAction.employee_id == employee_id)
            .order_by(DisciplinaryAction.triggered_on.desc(), DisciplinaryAction.id.desc())
        )
        return self.rows(stmt)


class LeaveRuleRepository(BaseRepository[LeaveRule]):
    """Queries over ``leave_rules``."""

    model = LeaveRule

    def find(self, *, department_id: int | None, leave_type: str) -> LeaveRule | None:
        """Department rule when present, otherwise the global rule."""
        if department_id is not None:
            stmt = select(LeaveRule).where(
                LeaveRule.department_id == department_id,
                LeaveRule.leave_type == leave_type,
                LeaveRule.is_active.is_(True),
            )
            found = db.session.execute(stmt).scalar_one_or_none()
            if found is not None:
                return found

        stmt = select(LeaveRule).where(
            LeaveRule.department_id.is_(None),
            LeaveRule.leave_type == leave_type,
            LeaveRule.is_active.is_(True),
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def list_rules(self, department_id: int | None = None):
        stmt = select(LeaveRule)
        if department_id is not None:
            stmt = stmt.where(
                (LeaveRule.department_id == department_id)
                | (LeaveRule.department_id.is_(None))
            )
        stmt = stmt.order_by(
            LeaveRule.department_id.nullsfirst(), LeaveRule.leave_type
        )
        return list(db.session.execute(stmt).scalars().all())


class RuleMetricsRepository(BaseRepository):
    """Read-only attendance counts consumed by the rule evaluators.

    Colour and disciplinary evaluation both need "how many lates / absences did
    this employee accumulate in a window", which is a single grouped query.
    """

    #: Composes across features; no single model owns it.
    model = None  # type: ignore[assignment]

    def __init__(self) -> None:
        # Deliberately not calling super().__init__: there is no single model.
        pass

    def window_counts(
        self,
        *,
        employee_ids: list[int],
        start: date,
        end: date,
    ) -> dict[int, dict]:
        """``{employee_id: {lates, absences, early_leaves}}`` in one query."""
        if not employee_ids:
            return {}

        from app.features.attendance.models import AttendanceDaily

        stmt = (
            select(
                AttendanceDaily.employee_id.label("employee_id"),
                func.sum(
                    func.cast(AttendanceDaily.late_minutes > 0, db.Integer)
                ).label("lates"),
                func.sum(
                    func.cast(AttendanceDaily.status == "absent", db.Integer)
                ).label("absences"),
                func.sum(
                    func.cast(AttendanceDaily.early_leave_minutes > 0, db.Integer)
                ).label("early_leaves"),
            )
            .where(
                AttendanceDaily.employee_id.in_(employee_ids),
                AttendanceDaily.work_date >= start,
                AttendanceDaily.work_date <= end,
            )
            .group_by(AttendanceDaily.employee_id)
        )
        return {
            int(row["employee_id"]): {
                "lates": int(row["lates"] or 0),
                "absences": int(row["absences"] or 0),
                "early_leaves": int(row["early_leaves"] or 0),
            }
            for row in self.rows(stmt)
        }
