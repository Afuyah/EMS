"""Attendance repositories: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import date

from sqlalchemy import case, func, select

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.features.attendance.constants import (
    DEFAULT_VERY_LATE_AFTER_MINUTES,
    AttendanceStatus,
)
from app.features.attendance.models import AttendanceDaily, AttendancePolicy


class AttendancePolicyRepository(BaseRepository[AttendancePolicy]):
    """Queries over ``attendance_policies``."""

    model = AttendancePolicy

    def global_policy(self) -> AttendancePolicy | None:
        """The default policy row (``department_id IS NULL``)."""
        stmt = (
            select(AttendancePolicy)
            .where(
                AttendancePolicy.department_id.is_(None),
                AttendancePolicy.is_active.is_(True),
            )
            .order_by(AttendancePolicy.id)
            .limit(1)
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def for_department(self, department_id: int | None) -> AttendancePolicy | None:
        if department_id is None:
            return None
        stmt = (
            select(AttendancePolicy)
            .where(
                AttendancePolicy.department_id == department_id,
                AttendancePolicy.is_active.is_(True),
            )
            .limit(1)
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def all_policies(self) -> list[AttendancePolicy]:
        stmt = select(AttendancePolicy).order_by(
            AttendancePolicy.department_id.nullsfirst()
        )
        return list(db.session.execute(stmt).scalars().all())

    def upsert(
        self,
        *,
        department_id: int | None,
        shift_start,
        shift_end,
        grace_minutes: int,
        very_late_after_minutes: int | None = None,
        auto_absent_after_minutes: int | None = None,
        is_active: bool = True,
    ) -> AttendancePolicy:
        """Create or update the policy for a scope (one row per department)."""
        existing = self.for_department(department_id) if department_id else self.global_policy()
        if existing is None and department_id is not None:
            existing = self.get_or_none(department_id=department_id)
        if existing is None and department_id is None:
            existing = self.get_or_none(department_id=None)

        if existing is not None:
            existing.shift_start = shift_start
            existing.shift_end = shift_end
            existing.grace_minutes = grace_minutes
            if very_late_after_minutes is not None:
                existing.very_late_after_minutes = very_late_after_minutes
            existing.auto_absent_after_minutes = auto_absent_after_minutes
            existing.is_active = is_active
            db.session.flush()
            return existing

        return self.create(
            department_id=department_id,
            shift_start=shift_start,
            shift_end=shift_end,
            grace_minutes=grace_minutes,
            very_late_after_minutes=(
                DEFAULT_VERY_LATE_AFTER_MINUTES
                if very_late_after_minutes is None
                else very_late_after_minutes
            ),
            auto_absent_after_minutes=auto_absent_after_minutes,
            is_active=is_active,
        )


class AttendanceRepository(BaseRepository[AttendanceDaily]):
    """Queries over ``attendance_daily``."""

    model = AttendanceDaily

    # ---- single rows -----------------------------------------------------
    def get_for_day(self, employee_id: int, work_date: date) -> AttendanceDaily | None:
        stmt = select(AttendanceDaily).where(
            AttendanceDaily.employee_id == employee_id,
            AttendanceDaily.work_date == work_date,
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def get_or_create_for_day(self, employee_id: int, work_date: date) -> AttendanceDaily:
        record = self.get_for_day(employee_id, work_date)
        if record is None:
            record = self.create(
                employee_id=employee_id,
                work_date=work_date,
                status=AttendanceStatus.ABSENT.value,
            )
        return record

    # ---- ranges ----------------------------------------------------------
    def list_for_employee(
        self,
        employee_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[AttendanceDaily]:
        stmt = select(AttendanceDaily).where(AttendanceDaily.employee_id == employee_id)
        if start is not None:
            stmt = stmt.where(AttendanceDaily.work_date >= start)
        if end is not None:
            stmt = stmt.where(AttendanceDaily.work_date <= end)
        stmt = stmt.order_by(AttendanceDaily.work_date.desc())
        if limit:
            stmt = stmt.limit(limit)
        return list(db.session.execute(stmt).scalars().all())

    def list_for_day(
        self, work_date: date, *, department_id: int | None = None
    ) -> list[AttendanceDaily]:
        stmt = select(AttendanceDaily).where(AttendanceDaily.work_date == work_date)
        if department_id is not None:
            from app.features.employees.models import Employee

            stmt = stmt.join(Employee, Employee.id == AttendanceDaily.employee_id).where(
                Employee.department_id == department_id
            )
        return list(db.session.execute(stmt).scalars().all())

    def days_in_range(
        self, *, start: date, end: date, employee_ids: list[int] | None = None
    ) -> list[AttendanceDaily]:
        stmt = select(AttendanceDaily).where(
            AttendanceDaily.work_date >= start,
            AttendanceDaily.work_date <= end,
        )
        if employee_ids is not None:
            stmt = stmt.where(AttendanceDaily.employee_id.in_(employee_ids))
        return list(db.session.execute(stmt).scalars().all())

    # ---- aggregates ------------------------------------------------------
    def employee_stats(
        self,
        employee_ids: list[int],
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[int, dict]:
        """Per-employee attendance summary in one query (no N+1).

        Returns ``{employee_id: {late_days, absent_days, on_leave_days,
        avg_worked_minutes, present_days}}``.
        """
        if not employee_ids:
            return {}

        stmt = (
            select(
                AttendanceDaily.employee_id.label("employee_id"),
                func.count(AttendanceDaily.id).label("recorded_days"),
                func.sum(
                    case(
                        (
                            AttendanceDaily.status.in_(
                                [
                                    AttendanceStatus.LATE.value,
                                    AttendanceStatus.VERY_LATE.value,
                                ]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("late_days"),
                func.sum(
                    case(
                        (AttendanceDaily.status == AttendanceStatus.ABSENT.value, 1),
                        else_=0,
                    )
                ).label("absent_days"),
                func.sum(
                    case(
                        (AttendanceDaily.status == AttendanceStatus.ON_LEAVE.value, 1),
                        else_=0,
                    )
                ).label("on_leave_days"),
                func.sum(
                    case(
                        (
                            AttendanceDaily.status.in_(
                                [
                                    AttendanceStatus.PRESENT.value,
                                    AttendanceStatus.LATE.value,
                                    AttendanceStatus.VERY_LATE.value,
                                    AttendanceStatus.HALF_DAY.value,
                                ]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("present_days"),
                func.coalesce(func.avg(AttendanceDaily.worked_minutes), 0).label(
                    "avg_worked_minutes"
                ),
                func.coalesce(func.sum(AttendanceDaily.worked_minutes), 0).label(
                    "total_worked_minutes"
                ),
                func.coalesce(func.sum(AttendanceDaily.late_minutes), 0).label(
                    "total_late_minutes"
                ),
            )
            .where(AttendanceDaily.employee_id.in_(employee_ids))
            .group_by(AttendanceDaily.employee_id)
        )
        if start is not None:
            stmt = stmt.where(AttendanceDaily.work_date >= start)
        if end is not None:
            stmt = stmt.where(AttendanceDaily.work_date <= end)

        summary: dict[int, dict] = {}
        for row in self.rows(stmt):
            summary[int(row["employee_id"])] = {
                "recorded_days": int(row["recorded_days"] or 0),
                "late_days": int(row["late_days"] or 0),
                "absent_days": int(row["absent_days"] or 0),
                "on_leave_days": int(row["on_leave_days"] or 0),
                "present_days": int(row["present_days"] or 0),
                "avg_worked_minutes": int(row["avg_worked_minutes"] or 0),
                "total_worked_minutes": int(row["total_worked_minutes"] or 0),
                "total_late_minutes": int(row["total_late_minutes"] or 0),
            }
        return summary

    def count_by_status(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
        employee_ids: list[int] | None = None,
    ) -> dict[str, int]:
        """Status -> count across a range (one query)."""
        stmt = (
            select(
                AttendanceDaily.status.label("status"),
                func.count(AttendanceDaily.id).label("total"),
            )
            .where(
                AttendanceDaily.work_date >= start,
                AttendanceDaily.work_date <= end,
            )
            .group_by(AttendanceDaily.status)
        )
        if department_id is not None:
            from app.features.employees.models import Employee

            stmt = stmt.join(Employee, Employee.id == AttendanceDaily.employee_id).where(
                Employee.department_id == department_id
            )
        if employee_ids is not None:
            stmt = stmt.where(AttendanceDaily.employee_id.in_(employee_ids))

        return {row["status"]: int(row["total"] or 0) for row in self.rows(stmt)}

    def daily_trend(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
        employee_ids: list[int] | None = None,
    ) -> list[dict]:
        """Per-day status counts for the trend charts (one query)."""
        stmt = (
            select(
                AttendanceDaily.work_date.label("work_date"),
                func.count(AttendanceDaily.id).label("recorded"),
                func.sum(
                    case(
                        (
                            AttendanceDaily.status.in_(
                                [
                                    AttendanceStatus.LATE.value,
                                    AttendanceStatus.VERY_LATE.value,
                                ]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("late"),
                func.sum(
                    case(
                        (AttendanceDaily.status == AttendanceStatus.ABSENT.value, 1),
                        else_=0,
                    )
                ).label("absent"),
                func.sum(
                    case(
                        (AttendanceDaily.status == AttendanceStatus.ON_LEAVE.value, 1),
                        else_=0,
                    )
                ).label("on_leave"),
                func.sum(
                    case(
                        (
                            AttendanceDaily.status.in_(
                                [
                                    AttendanceStatus.PRESENT.value,
                                    AttendanceStatus.LATE.value,
                                ]
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("present"),
                func.coalesce(func.avg(AttendanceDaily.worked_minutes), 0).label(
                    "avg_worked_minutes"
                ),
            )
            .where(
                AttendanceDaily.work_date >= start,
                AttendanceDaily.work_date <= end,
            )
            .group_by(AttendanceDaily.work_date)
            .order_by(AttendanceDaily.work_date)
        )
        if department_id is not None:
            from app.features.employees.models import Employee

            stmt = stmt.join(Employee, Employee.id == AttendanceDaily.employee_id).where(
                Employee.department_id == department_id
            )
        if employee_ids is not None:
            stmt = stmt.where(AttendanceDaily.employee_id.in_(employee_ids))

        return self.rows(stmt)

    def top_late(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Employees ranked by late days then total late minutes."""
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.name.label("department_name"),
                func.count(AttendanceDaily.id).label("late_days"),
                func.coalesce(func.sum(AttendanceDaily.late_minutes), 0).label(
                    "late_minutes"
                ),
            )
            .join(Employee, Employee.id == AttendanceDaily.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                AttendanceDaily.work_date >= start,
                AttendanceDaily.work_date <= end,
                AttendanceDaily.late_minutes > 0,
            )
            .group_by(
                Employee.id,
                Employee.employee_no,
                Employee.first_name,
                Employee.last_name,
                Department.name,
            )
            .order_by(
                func.count(AttendanceDaily.id).desc(),
                func.coalesce(func.sum(AttendanceDaily.late_minutes), 0).desc(),
            )
            .limit(limit)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return self.rows(stmt)

    def employees_on_leave_on(self, work_date: date) -> list[int]:
        stmt = select(AttendanceDaily.employee_id).where(
            AttendanceDaily.work_date == work_date,
            AttendanceDaily.status == AttendanceStatus.ON_LEAVE.value,
        )
        return [int(row[0]) for row in db.session.execute(stmt).all()]

    def late_streaks(self, employee_id: int, *, limit: int = 60) -> list[AttendanceDaily]:
        """Recent days for streak calculation, newest first."""
        stmt = (
            select(AttendanceDaily)
            .where(AttendanceDaily.employee_id == employee_id)
            .order_by(AttendanceDaily.work_date.desc())
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    def recent_punches(self, *, limit: int = 8) -> list[dict]:
        """Latest clock-ins across the company, for the live board."""
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                AttendanceDaily.id.label("attendance_id"),
                AttendanceDaily.work_date.label("work_date"),
                AttendanceDaily.clock_in.label("clock_in"),
                AttendanceDaily.clock_out.label("clock_out"),
                AttendanceDaily.status.label("status"),
                AttendanceDaily.late_minutes.label("late_minutes"),
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.name.label("department_name"),
            )
            .join(Employee, Employee.id == AttendanceDaily.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(AttendanceDaily.clock_in.is_not(None))
            .order_by(AttendanceDaily.clock_in.desc())
            .limit(limit)
        )
        return self.rows(stmt)

    def list_for_day_enriched(
        self,
        work_date: date,
        *,
        department_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> list[dict]:
        """Board rows for one day, with employee and department joined in."""
        from app.features.employees.models import Department, Employee, Position

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Employee.current_color.label("current_color"),
                Employee.tz.label("tz"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                Position.title.label("position_title"),
                AttendanceDaily.id.label("attendance_id"),
                AttendanceDaily.clock_in.label("clock_in"),
                AttendanceDaily.clock_out.label("clock_out"),
                AttendanceDaily.status.label("status"),
                AttendanceDaily.late_minutes.label("late_minutes"),
                AttendanceDaily.early_leave_minutes.label("early_leave_minutes"),
                AttendanceDaily.worked_minutes.label("worked_minutes"),
                AttendanceDaily.notes.label("notes"),
            )
            .select_from(Employee)
            .join(
                AttendanceDaily,
                (AttendanceDaily.employee_id == Employee.id)
                & (AttendanceDaily.work_date == work_date),
                isouter=True,
            )
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .join(Position, Position.id == Employee.position_id, isouter=True)
            .where(Employee.is_active.is_(True))
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        if status:
            if status == "absent":
                stmt = stmt.where(
                    (AttendanceDaily.status == AttendanceStatus.ABSENT.value)
                    | (AttendanceDaily.id.is_(None))
                )
            else:
                stmt = stmt.where(AttendanceDaily.status == status)
        if search:
            needle = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                func.lower(Employee.first_name).like(needle)
                | func.lower(Employee.last_name).like(needle)
                | func.lower(Employee.employee_no).like(needle)
            )
        stmt = stmt.order_by(Employee.last_name, Employee.first_name)
        return self.rows(stmt)

    def correctable_employees(
        self, work_date: date, *, department_id: int | None = None
    ) -> list[dict]:
        """Every active employee, with any existing row for ``work_date``.

        The LEFT JOIN is the point: an employee with no attendance row that day
        still appears, so HR can record a missed punch instead of being limited
        to editing rows that already exist.
        """
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Employee.tz.label("tz"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                AttendanceDaily.id.label("attendance_id"),
            )
            .select_from(Employee)
            .join(
                AttendanceDaily,
                (AttendanceDaily.employee_id == Employee.id)
                & (AttendanceDaily.work_date == work_date),
                isouter=True,
            )
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(Employee.is_active.is_(True))
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        stmt = stmt.order_by(Employee.last_name, Employee.first_name)
        return self.rows(stmt)
