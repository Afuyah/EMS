"""Attendance views: company board and per-employee history."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from flask import redirect, request, url_for

from app.core.exceptions import AppError, ValidationError
from app.core.logging import get_logger
from app.core.time import today_local
from app.core.time import UTC
from app.features.attendance.service import AttendanceService
from app.features.auth.constants import Role
from app.features.biometrics.service import BiometricsService
from app.features.employees.service import DepartmentService, EmployeeService
from app.features.leave.service import LeaveService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    form_int,
    form_str,
    principal,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
    wants_partial,
)

logger = get_logger(__name__)

_attendance = AttendanceService()
_employees = EmployeeService()
_departments = DepartmentService()
_leave = LeaveService()
_devices = BiometricsService()


@views_bp.get("/attendance")
@view_login_required()
def attendance():
    """Rich attendance board for one day, HTMX-polled."""
    user = principal()
    employee = _employees.get_employee_by_user(user.user_id)
    default_tz = employee.tz if employee else None
    today = today_local(default_tz)

    work_date = _parse_date(request.args.get("date")) or today
    department_id = request.args.get("department_id", type=int)
    status = request.args.get("status") or None
    search = request.args.get("q") or None

    # Managers are scoped to their own department.
    if user.is_manager and employee is not None and department_id is None:
        department_id = employee.department_id

    rows = _attendance.board_rows(
        work_date, department_id=department_id, status=status, search=search
    )

    present = sum(1 for row in rows if row.get("clock_in") is not None)
    late = sum(1 for row in rows if (row.get("late_minutes") or 0) > 0)
    absent_rows = [
        row
        for row in rows
        if row.get("attendance_id") is None or row.get("status") == "absent"
    ]
    on_leave_rows = [row for row in rows if row.get("status") == "on_leave"]
    open_rows = [
        row
        for row in rows
        if row.get("clock_in") is not None and row.get("clock_out") is None
    ]

    worked = [row.get("worked_minutes") or 0 for row in rows if row.get("worked_minutes")]
    avg_worked = round(sum(worked) / len(worked)) if worked else 0

    context = dict(
        active_nav="Attendance",
        work_date=work_date,
        is_today=work_date == today,
        today=today,
        rows=rows,
        present=present,
        late=late,
        absent=len(absent_rows),
        on_leave=len(on_leave_rows),
        open_count=len(open_rows),
        avg_worked=avg_worked,
        headcount=len(rows),
        absent_rows=absent_rows[:8],
        late_rows=sorted(
            [row for row in rows if (row.get("late_minutes") or 0) > 0],
            key=lambda row: row["late_minutes"],
            reverse=True,
        )[:8],
        on_leave_rows=on_leave_rows[:8],
        departments=_departments.list_departments(),
        # Everyone who can be corrected today, including staff with no record
        # yet, so a missed punch can be recorded rather than only edited.
        correctable=_attendance.correctable_employees(
            work_date, department_id=department_id
        ),
        punches=_devices.recent_raw_logs(limit=10),
        filters={
            "date": work_date.isoformat(),
            "department_id": department_id,
            "status": status or "",
            "q": search or "",
        },
    )

    # HTMX polls just the board body.
    if wants_partial():
        return render("attendance/_board.html", **context)
    return render("attendance/board.html", **context)


@views_bp.get("/attendance/employees/<int:employee_id>")
@view_login_required()
def attendance_employee(employee_id: int):
    """Per-employee attendance KPIs and full history."""
    try:
        employee = _employees.get_employee(employee_id)
    except AppError as error:
        flash_error(error.message)
        return redirect(url_for("views.attendance"))

    user = principal()
    # An employee may only see their own record.
    own = _employees.get_employee_by_user(user.user_id)
    if not user.is_hr_or_admin and not user.is_manager:
        if own is None or own.id != employee_id:
            flash_error("You may only view your own attendance.")
            return redirect(url_for("views.attendance"))

    today = today_local(employee.tz)
    year_start = date(today.year, 1, 1)

    history = _attendance.history(employee.id, start=year_start, end=today)
    stats = _attendance.employee_stats(
        [employee.id], start=year_start, end=today
    ).get(employee.id, {})

    return render(
        "attendance/employee.html",
        active_nav="Attendance",
        employee=employee,
        today=today,
        history=history,
        stats=stats,
        avg_worked=_attendance.average_worked_minutes(
            employee.id, start=year_start, end=today
        ),
        on_time_percent=_attendance.on_time_percent(
            employee.id, start=year_start, end=today
        ),
        longest_streak=_attendance.longest_late_streak(employee.id),
    )


@views_bp.post("/attendance/correct")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_correct():
    """Record or amend one employee's attendance for a day.

    Addressed by employee and date, not by an existing row id, so a day with no
    punch at all can still be corrected.
    """
    employee_id = form_int("employee_id")
    work_date = _parse_date(form_str("work_date"))
    clock_in = _parse_datetime(form_str("clock_in"))
    clock_out = _parse_datetime(form_str("clock_out"))
    status = form_str("status")
    notes = form_str("notes")

    back = url_for("views.attendance", date=work_date.isoformat() if work_date else None)

    if employee_id is None or work_date is None:
        flash_error("Choose an employee and a date before saving.")
        return redirect(back)

    try:
        record = _attendance.correct_employee_day(
            employee_id=employee_id,
            work_date=work_date,
            clock_in=clock_in,
            clock_out=clock_out,
            status=status,
            notes=notes,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(back)
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("attendance correction failed")
        flash_error("The correction could not be saved.")
        return redirect(back)

    flash_success(
        f"Corrected {record.work_date.strftime('%d %b')} — "
        f"late {record.late_minutes}m, worked {record.worked_minutes}m."
    )
    return redirect(back)


@views_bp.get("/attendance/live")
@view_login_required()
def attendance_live():
    """Small HTMX fragment: the live punch stream."""
    return render(
        "attendance/_live.html",
        punches=_devices.recent_raw_logs(limit=10),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse a datetime-local input as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
