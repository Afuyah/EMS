"""Reports repositories: the only place with SQL for this feature.

Every method issues a single statement — no per-row follow-up queries.  These
are the documented read-heavy exception where plain dicts are returned instead
of ORM models.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, select

from app.common.base_repository import BaseRepository
from app.core.database import db


class ReportsRepository(BaseRepository):
    """Aggregation queries backing the reports pages and CSV exports."""

    #: No single model owns this repository; it composes across features.
    model = None  # type: ignore[assignment]

    def __init__(self) -> None:
        # Deliberately not calling super().__init__: there is no single model.
        pass

    # ------------------------------------------------------------------
    # Attendance
    # ------------------------------------------------------------------
    def attendance_summary(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
    ) -> list[dict]:
        """Per-department attendance summary for a date range (one query)."""
        from app.features.attendance.models import AttendanceDaily
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Department.id.label("department_id"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                func.count(func.distinct(Employee.id)).label("employees"),
                func.count(AttendanceDaily.id).label("records"),
                func.sum(
                    func.cast(AttendanceDaily.late_minutes > 0, db.Integer)
                ).label("late_days"),
                func.sum(
                    func.cast(AttendanceDaily.status == "absent", db.Integer)
                ).label("absent_days"),
                func.sum(
                    func.cast(AttendanceDaily.status == "on_leave", db.Integer)
                ).label("on_leave_days"),
                func.coalesce(func.sum(AttendanceDaily.worked_minutes), 0).label(
                    "worked_minutes"
                ),
                func.coalesce(func.avg(AttendanceDaily.worked_minutes), 0).label(
                    "avg_worked_minutes"
                ),
            )
            .select_from(AttendanceDaily)
            .join(Employee, Employee.id == AttendanceDaily.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                AttendanceDaily.work_date >= start,
                AttendanceDaily.work_date <= end,
            )
            .group_by(Department.id, Department.name, Department.code)
            .order_by(Department.name)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)

        rows = self.rows(stmt)
        for row in rows:
            records = int(row["records"] or 0)
            row["employees"] = int(row["employees"] or 0)
            row["records"] = records
            row["late_days"] = int(row["late_days"] or 0)
            row["absent_days"] = int(row["absent_days"] or 0)
            row["on_leave_days"] = int(row["on_leave_days"] or 0)
            row["worked_minutes"] = int(row["worked_minutes"] or 0)
            row["avg_worked_minutes"] = int(row["avg_worked_minutes"] or 0)
            row["attendance_rate"] = (
                round(((records - row["absent_days"]) / records) * 100) if records else 0
            )
        return rows

    def headcount_by_day(self, *, start: date, end: date) -> list[dict]:
        """Daily headcount using a single ``generate_series`` statement."""
        from sqlalchemy import text

        stmt = text(
            """
            SELECT d::date AS work_date,
                   (SELECT count(*) FROM employees e
                     WHERE e.is_active = true
                       AND (e.hired_at IS NULL OR e.hired_at <= d::date)) AS headcount,
                   (SELECT count(*) FROM attendance_daily a
                     WHERE a.work_date = d::date AND a.clock_in IS NOT NULL) AS clocked_in,
                   (SELECT count(*) FROM attendance_daily a
                     WHERE a.work_date = d::date AND a.status = 'on_leave') AS on_leave
              FROM generate_series(:start_date, :end_date, interval '1 day') AS d
             ORDER BY d
            """
        )
        result = db.session.execute(stmt, {"start_date": start, "end_date": end})
        return [dict(row) for row in result.mappings().all()]

    def color_distribution(self, *, department_id: int | None = None) -> list[dict]:
        """Active headcount per colour (one query)."""
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Employee.current_color.label("color"),
                func.count(Employee.id).label("total"),
            )
            .where(Employee.is_active.is_(True))
            .group_by(Employee.current_color)
            .order_by(Employee.current_color)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return self.rows(stmt)

    def top_late_employees(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Employees ranked by late days then late minutes (one query)."""
        from app.features.attendance.models import AttendanceDaily
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                func.count(AttendanceDaily.id).label("late_days"),
                func.coalesce(func.sum(AttendanceDaily.late_minutes), 0).label(
                    "late_minutes"
                ),
                func.coalesce(func.avg(AttendanceDaily.late_minutes), 0).label(
                    "avg_late_minutes"
                ),
            )
            .select_from(AttendanceDaily)
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
                Department.code,
            )
            .order_by(
                func.count(AttendanceDaily.id).desc(),
                func.coalesce(func.sum(AttendanceDaily.late_minutes), 0).desc(),
            )
            .limit(limit)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)

        rows = self.rows(stmt)
        for row in rows:
            row["late_days"] = int(row["late_days"] or 0)
            row["late_minutes"] = int(row["late_minutes"] or 0)
            row["avg_late_minutes"] = int(row["avg_late_minutes"] or 0)
        return rows

    # ------------------------------------------------------------------
    # Leave
    # ------------------------------------------------------------------
    def leave_utilisation(
        self, *, year: int, department_id: int | None = None
    ) -> list[dict]:
        """Entitled vs used per leave type (one query)."""
        from app.features.employees.models import Employee
        from app.features.leave.models import LeaveBalance

        stmt = (
            select(
                LeaveBalance.leave_type.label("leave_type"),
                func.sum(LeaveBalance.entitled).label("entitled"),
                func.sum(LeaveBalance.used).label("used"),
                func.sum(LeaveBalance.pending).label("pending"),
                func.count(func.distinct(LeaveBalance.employee_id)).label("employees"),
            )
            .where(LeaveBalance.year == year)
            .group_by(LeaveBalance.leave_type)
            .order_by(LeaveBalance.leave_type)
        )
        if department_id is not None:
            stmt = stmt.join(
                Employee, Employee.id == LeaveBalance.employee_id
            ).where(Employee.department_id == department_id)

        rows = self.rows(stmt)
        for row in rows:
            entitled = int(row["entitled"] or 0)
            used = int(row["used"] or 0)
            pending = int(row["pending"] or 0)
            row.update(
                {
                    "entitled": entitled,
                    "used": used,
                    "pending": pending,
                    "employees": int(row["employees"] or 0),
                    "available": max(0, entitled - used - pending),
                    "utilisation_percent": round((used / entitled) * 100) if entitled else 0,
                }
            )
        return rows

    def department_leave_calendar(
        self, *, department_id: int, start: date, end: date
    ) -> list[dict]:
        """Approved leaves overlapping a window, with employee context."""
        return self._leave_calendar_rows(
            start=start, end=end, department_id=department_id
        )

    def all_leave_calendar(self, *, start: date, end: date) -> list[dict]:
        return self._leave_calendar_rows(start=start, end=end, department_id=None)

    def _leave_calendar_rows(
        self, *, start: date, end: date, department_id: int | None
    ) -> list[dict]:
        from app.features.employees.models import Department, Employee
        from app.features.leave.models import LeaveRequest

        stmt = (
            select(
                LeaveRequest.id.label("request_id"),
                LeaveRequest.leave_type.label("leave_type"),
                LeaveRequest.start_date.label("start_date"),
                LeaveRequest.end_date.label("end_date"),
                LeaveRequest.days.label("days"),
                LeaveRequest.status.label("status"),
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.id.label("department_id"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
            )
            .select_from(LeaveRequest)
            .join(Employee, Employee.id == LeaveRequest.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                LeaveRequest.status.in_(["approved", "approved_forced"]),
                LeaveRequest.end_date >= start,
                LeaveRequest.start_date <= end,
            )
            .order_by(LeaveRequest.start_date, Employee.last_name)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return self.rows(stmt)

    def balance_ledger(self, *, department_id: int, year: int) -> list[dict]:
        """Per-employee balance ledger for a department (one query)."""
        from app.features.employees.models import Department, Employee
        from app.features.leave.models import LeaveBalance

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.code.label("department_code"),
                LeaveBalance.leave_type.label("leave_type"),
                LeaveBalance.entitled.label("entitled"),
                LeaveBalance.used.label("used"),
                LeaveBalance.pending.label("pending"),
                (
                    LeaveBalance.entitled - LeaveBalance.used - LeaveBalance.pending
                ).label("available"),
            )
            .select_from(LeaveBalance)
            .join(Employee, Employee.id == LeaveBalance.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                Employee.department_id == department_id,
                LeaveBalance.year == year,
            )
            .order_by(Employee.last_name, Employee.first_name, LeaveBalance.leave_type)
        )
        return self.rows(stmt)

    # ------------------------------------------------------------------
    # Cross-cutting
    # ------------------------------------------------------------------
    def workforce_overview(self) -> dict:
        """Headline counts for the reports landing page (one query)."""
        from sqlalchemy import text

        stmt = text(
            """
            SELECT
              (SELECT count(*) FROM employees WHERE is_active = true) AS active_employees,
              (SELECT count(*) FROM employees) AS total_employees,
              (SELECT count(*) FROM departments) AS departments,
              (SELECT count(*) FROM biometric_devices WHERE is_active = true) AS active_devices,
              (SELECT count(*) FROM leave_requests WHERE status = 'pending') AS pending_leave,
              (SELECT count(*) FROM leave_requests WHERE status = 'suggested') AS awaiting_employee,
              (SELECT count(*) FROM disciplinary_actions) AS disciplinary_actions
            """
        )
        row = db.session.execute(stmt).mappings().one()
        return {key: int(value or 0) for key, value in row.items()}
