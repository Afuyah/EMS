"""Dashboard views: one endpoint dispatching three role-shaped dashboards."""

from __future__ import annotations

from datetime import date, timedelta

from app.core.time import today_local
from app.features.attendance.service import AttendanceService
from app.features.auth.constants import Role
from app.features.biometrics.service import BiometricsService
from app.features.employees.service import DepartmentService, EmployeeService
from app.features.leave.service import LeaveService
from app.features.notifications.service import NotificationService
from app.features.reports.service import ReportsService
from app.views.blueprint import views_bp
from app.views.helpers import principal, render, view_login_required

_employees = EmployeeService()
_departments = DepartmentService()
_attendance = AttendanceService()
_leave = LeaveService()
_reports = ReportsService()
_notifications = NotificationService()
_devices = BiometricsService()


@views_bp.get("/dashboard")
@view_login_required()
def dashboard():
    """Role-dispatched landing page."""
    user = principal()
    if user.is_hr_or_admin:
        return _admin_dashboard()
    if user.is_manager:
        return _manager_dashboard()
    return _employee_dashboard()


# ---------------------------------------------------------------------------
# HR / Admin
# ---------------------------------------------------------------------------
def _admin_dashboard():
    today = today_local(None)
    window_start = today - timedelta(days=13)

    overview = _reports.overview()
    colors = _reports.attendance_colors()
    top_late = _reports.top_late(start=today - timedelta(days=29), end=today, limit=5)
    trend = _attendance.trend(start=window_start, end=today)
    punches = _attendance.recent_punches(limit=8)
    department_split = _employees.count_by_department()

    in_queue = len(_leave.list_requests(statuses=["pending"]))
    awaiting_employee = len(_leave.list_requests(statuses=["suggested"]))
    device_health = _devices.health()

    return render(
        "dashboard/admin.html",
        active_nav="Dashboard",
        today=today,
        overview=overview,
        colors=colors,
        top_late=top_late,
        trend=_fill_trend(trend, window_start, today),
        punches=punches,
        # Raw device punches for the live stream — distinct from `punches`
        # above, which are resolved attendance rows.
        live_punches=_devices.recent_raw_logs(limit=10),
        department_split=department_split,
        in_queue=in_queue,
        awaiting_employee=awaiting_employee,
        device_health=device_health,
        notifications=_notifications.recent(user_id=principal().user_id, limit=5),
    )


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------
def _manager_dashboard():
    user = principal()
    employee = _employees.get_employee_by_user(user.user_id)
    if employee is None:
        return _employee_dashboard()

    today = today_local(employee.tz)
    window_start = today - timedelta(days=13)
    department_id = employee.department_id

    team = _employees.team_of(employee)
    team_ids = [member.id for member in team]

    stats = _attendance.employee_stats(team_ids, start=today, end=today)
    board = _attendance.board_rows(today, department_id=department_id)
    trend = _attendance.trend(start=window_start, end=today, department_id=department_id)

    pending = _leave.department_queue(department_id, statuses=["pending"])
    on_leave_today = _leave.calendar(
        department_id=department_id, start=today, end=today
    )

    present = sum(1 for row in board if row.get("clock_in") is not None)
    late = sum(1 for row in board if (row.get("late_minutes") or 0) > 0)
    absent = sum(
        1
        for row in board
        if row.get("attendance_id") is None or row.get("status") == "absent"
    )

    return render(
        "dashboard/manager.html",
        active_nav="Dashboard",
        today=today,
        employee=employee,
        department=employee.department,
        team=team,
        headcount=len(team),
        present=present,
        late=late,
        absent=absent,
        on_leave=len(on_leave_today),
        board=board,
        trend=_fill_trend(trend, window_start, today),
        pending=pending,
        colors=_reports.attendance_colors(department_id=department_id),
        top_late=_reports.top_late(
            start=today - timedelta(days=29), end=today, department_id=department_id, limit=5
        ),
    )


# ---------------------------------------------------------------------------
# Employee
# ---------------------------------------------------------------------------
def _employee_dashboard():
    user = principal()
    employee = _employees.get_employee_by_user(user.user_id)
    if employee is None:
        return render(
            "dashboard/employee.html",
            active_nav="Dashboard",
            employee=None,
            today=date.today(),
            notifications=_notifications.recent(user_id=user.user_id, limit=5),
            unread=_notifications.unread_count(user_id=user.user_id),
        )

    today = today_local(employee.tz)
    year = today.year
    window_start = today - timedelta(days=13)

    balances = _leave.balances_for_employee(employee.id, year=year)
    history = _attendance.history(employee.id, start=window_start, end=today)
    today_row = _attendance.get_day(employee.id, today)
    pending = _leave.my_pending(employee.id)
    upcoming = [
        request
        for request in _leave.my_active(employee.id, today=today)
        if request.start_date >= today
    ]
    streak = _attendance.longest_late_streak(employee.id)
    ytd_stats = _attendance.employee_stats(
        [employee.id], start=date(year, 1, 1), end=today
    )
    stats = ytd_stats.get(employee.id, {})

    colleagues = (
        _leave.teammates_on_leave(
            department_id=employee.department_id,
            start=today,
            end=today + timedelta(days=14),
            exclude_employee_id=employee.id,
        )
        if employee.department_id
        else []
    )

    # The published annual allocation: what the employee is told on login.
    from app.features.leave.planner_service import LeavePlanService

    allocation = LeavePlanService().published_item_for(
        employee_id=employee.id, year=year
    )

    return render(
        "dashboard/employee.html",
        active_nav="Dashboard",
        employee=employee,
        today=today,
        today_row=today_row,
        balances=balances,
        history=history,
        pending=pending,
        upcoming=upcoming,
        streak=streak,
        stats=stats,
        colleagues=colleagues,
        allocation=allocation,
        notifications=_notifications.recent(user_id=user.user_id, limit=5),
        unread=_notifications.unread_count(user_id=user.user_id),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fill_trend(rows: list[dict], start: date, end: date) -> list[dict]:
    """Zero-fill a daily trend so charts show gaps rather than collapsing."""
    by_date = {row["work_date"]: row for row in rows}
    filled: list[dict] = []
    current = start
    while current <= end:
        row = by_date.get(current)
        if row is None:
            filled.append(
                {
                    "work_date": current,
                    "present": 0,
                    "late": 0,
                    "absent": 0,
                    "on_leave": 0,
                    "recorded": 0,
                }
            )
        else:
            row = dict(row)
            for key in ("present", "late", "absent", "on_leave", "recorded"):
                row[key] = int(row.get(key) or 0)
            filled.append(row)
        current += timedelta(days=1)
    return filled
