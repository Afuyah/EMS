"""Rules tables: colour ladder, disciplinary rules/actions, leave rules.

Data containers only — no business rules, no queries.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import db
from app.features.employees.constants import EmployeeColor
from app.features.rules.constants import (
    DisciplinaryActionType,
    RulePeriod,
    TriggerType,
)


class ColorRule(db.Model):
    """Maps late/absence counts onto an attendance-health colour."""

    __tablename__ = "color_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    color: Mapped[str] = mapped_column(String(10), nullable=False)
    min_lates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_absences: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)

    department: Mapped["Department | None"] = relationship()  # noqa: F821

    @property
    def color_enum(self) -> EmployeeColor:
        return EmployeeColor.coerce(self.color)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        scope = "global" if self.department_id is None else f"dept={self.department_id}"
        return (
            f"<ColorRule {scope} {self.color} lates>={self.min_lates} "
            f"absences>={self.min_absences} prio={self.priority}>"
        )


class DisciplinaryRule(db.Model):
    """Threshold that triggers a disciplinary action."""

    __tablename__ = "disciplinary_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    suspension_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_apply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    department: Mapped["Department | None"] = relationship()  # noqa: F821
    actions: Mapped[list["DisciplinaryAction"]] = relationship(
        back_populates="rule",
        passive_deletes=True,
    )

    @property
    def trigger_enum(self) -> TriggerType:
        return TriggerType.coerce(self.trigger_type)

    @property
    def period_enum(self) -> RulePeriod:
        return RulePeriod.coerce(self.period)

    @property
    def action_enum(self) -> DisciplinaryActionType:
        return DisciplinaryActionType.coerce(self.action)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        scope = "global" if self.department_id is None else f"dept={self.department_id}"
        return (
            f"<DisciplinaryRule {scope} {self.trigger_type}>={self.threshold}/"
            f"{self.period} -> {self.action}>"
        )


class DisciplinaryAction(db.Model):
    """A recorded disciplinary outcome (auto-applied or manual)."""

    __tablename__ = "disciplinary_actions"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "rule_id",
            "triggered_on",
            name="uq_disciplinary_employee_rule_date",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("disciplinary_rules.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    applied_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="disciplinary_actions")  # noqa: F821
    rule: Mapped[DisciplinaryRule | None] = relationship(back_populates="actions")
    applier: Mapped["User | None"] = relationship(foreign_keys=[applied_by])  # noqa: F821

    @property
    def action_enum(self) -> DisciplinaryActionType:
        return DisciplinaryActionType.coerce(self.action)

    @property
    def action_tailwind(self) -> str:
        return self.action_enum.tailwind

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<DisciplinaryAction employee={self.employee_id} "
            f"{self.action} on={self.triggered_on}>"
        )


class LeaveRule(db.Model):
    """Admin-configured leave constraints per department (or globally)."""

    __tablename__ = "leave_rules"
    __table_args__ = (
        UniqueConstraint(
            "department_id", "leave_type", name="uq_leave_rules_department_type"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    leave_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    max_per_year: Mapped[int | None] = mapped_column(Integer)
    max_per_month: Mapped[int | None] = mapped_column(Integer)
    max_consecutive_days: Mapped[int | None] = mapped_column(Integer)
    max_employees_off_per_dept: Mapped[int | None] = mapped_column(Integer)
    min_notice_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    default_days: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    department: Mapped["Department | None"] = relationship()  # noqa: F821

    @property
    def is_global(self) -> bool:
        return self.department_id is None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        scope = "global" if self.is_global else f"dept={self.department_id}"
        return f"<LeaveRule {scope} {self.leave_type} max/yr={self.max_per_year}>"
