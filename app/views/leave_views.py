"""Leave views: my dashboard, the request/suggestion flow, and the HR queue."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from flask import request, url_for

from app.core.exceptions import AppError, AuthorizationError
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.auth.constants import Role
from app.features.employees.service import DepartmentService, EmployeeService
from app.features.exports.service import ExportsService
from app.features.leave.constants import LeaveStatus, LeaveType
from app.features.leave.rules_service import LeaveRulesService
from app.features.leave.service import LeaveService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_info,
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

_leave = LeaveService()
_employees = EmployeeService()
_departments = DepartmentService()
_rules = LeaveRulesService()
_exports = ExportsService()


def _current_employee():
    """The employee record behind the logged-in user, or ``None``."""
    user = principal()
    return _employees.get_employee_by_user(user.user_id)


# ---------------------------------------------------------------------------
# My leave
# ---------------------------------------------------------------------------
@views_bp.get("/leave")
@view_login_required()
def leave():
    """Employee leave dashboard: balances, pending actions, history."""
    employee = _current_employee()
    if employee is None:
        flash_error("Your account is not linked to an employee record.")
        from flask import redirect

        return redirect(url_for("views.dashboard"))

    today = today_local(employee.tz)
    year = today.year

    balances = _leave.balances_for_employee(employee.id, year=year)
    everything = _leave.my_requests(employee.id)
    pending = [r for r in everything if r.status == LeaveStatus.SUGGESTED.value]
    awaiting_hr = [r for r in everything if r.status == LeaveStatus.PENDING.value]
    upcoming = [
        r
        for r in everything
        if r.status in {"approved", "approved_forced"} and r.end_date >= today
    ]
    past = [
        r
        for r in everything
        if r.status in {"denied", "cancelled"}
        or (r.status in {"approved", "approved_forced"} and r.end_date < today)
    ]

    colleagues = (
        _leave.teammates_on_leave(
            department_id=employee.department_id,
            start=today,
            end=today + timedelta(days=30),
            exclude_employee_id=employee.id,
        )
        if employee.department_id
        else []
    )

    # The published rosters: this employee's own allocation, and who else in the
    # department is off for the rest of the year.  This is what answers "when do
    # I get my leave?" and "who is away in December?" without asking HR.
    from app.features.leave.planner_service import LeavePlanService

    planner = LeavePlanService()
    allocation = planner.published_item_for(employee_id=employee.id, year=year)
    department_roster = (
        [
            item
            for item in planner.items_for_department(
                department_id=employee.department_id,
                year=year,
                published_only=True,
            )
            if item.is_placed and item.employee_id != employee.id
        ]
        if employee.department_id
        else []
    )
    # Upcoming first, then anything already past within the year.
    department_roster.sort(key=lambda item: (item.start_date < today, item.start_date))

    return render(
        "leave/mine.html",
        active_nav="Leave",
        employee=employee,
        today=today,
        year=year,
        balances=balances,
        pending=pending,
        awaiting_hr=awaiting_hr,
        upcoming=upcoming,
        past=past[:15],
        colleagues=colleagues,
        allocation=allocation,
        department_roster=department_roster,
        leave_types=_employees.leave_eligibility(employee)["allowed"],
    )


@views_bp.get("/leave/request")
@view_login_required()
def leave_request_form():
    """The request form, including the HTMX suggestion panel."""
    employee = _current_employee()
    if employee is None:
        flash_error("Your account is not linked to an employee record.")
        return render("leave/request.html", active_nav="Leave", employee=None)

    today = today_local(employee.tz)
    # Maternity/paternity are filtered by the employee's recorded gender.
    eligibility = _employees.leave_eligibility(employee)
    return render(
        "leave/request.html",
        active_nav="Leave",
        employee=employee,
        today=today,
        year=today.year,
        leave_types=eligibility["allowed"],
        eligibility=eligibility,
        rules=_rules.list_rules(employee.department_id),
        balances=_leave.balances_for_employee(employee.id, year=today.year),
        special_types=[
            kind.value
            for kind in LeaveType
            if kind.skips_suggestion_engine and kind.value in eligibility["allowed"]
        ],
    )


@views_bp.post("/leave/suggest")
@view_login_required()
def leave_suggest():
    """HTMX endpoint: run the engine and return the suggestion modal."""
    employee = _current_employee()
    if employee is None:
        return render("leave/_suggest_error.html", message="No employee record for this login.")

    leave_type = form_str("leave_type", LeaveType.ANNUAL.value) or LeaveType.ANNUAL.value
    days = form_int("days")
    start_raw = form_str("start_date")
    end_raw = form_str("end_date")
    reason = form_str("reason")

    kind = LeaveType.coerce(leave_type)
    today = today_local(employee.tz)

    # Special leaves skip the engine entirely.
    if kind.skips_suggestion_engine:
        try:
            default_days = _rules.default_days(
                department_id=employee.department_id, leave_type=kind
            )
        except AppError as error:
            return render("leave/_suggest_error.html", message=error.message)
        return render(
            "leave/_suggest_modal.html",
            employee=employee,
            leave_type=kind.value,
            window_start=_parse_date(start_raw) or today,
            window_end=_parse_date(end_raw) or (today + timedelta(days=default_days - 1)),
            days=default_days,
            suggestions=[],
            special=True,
            reason=reason,
            error=None,
        )

    try:
        requested = days if days else _default_requested_days(employee, kind, today)
        scored = _leave.suggest(
            employee_id=employee.id,
            leave_type=kind,
            days=requested,
        )
    except AppError as error:
        logger.info("suggestion failed employee=%s: %s", employee.id, error.message)
        return render(
            "leave/_suggest_error.html",
            message=error.message,
            leave_type=kind.value,
            days=days,
        )

    if not scored:
        return render(
            "leave/_suggest_error.html",
            message=(
                "No window satisfies the current rules within the next 120 days. "
                "Try a shorter duration."
            ),
            leave_type=kind.value,
            days=days,
        )

    best = scored[0]
    return render(
        "leave/_suggest_modal.html",
        employee=employee,
        leave_type=kind.value,
        window_start=best.window.start_date,
        window_end=best.window.end_date,
        days=best.days,
        suggestions=[item.as_suggestion(rank=i + 1) for i, item in enumerate(scored)],
        special=False,
        reason=reason,
        error=None,
    )


def _default_requested_days(employee, kind: LeaveType, today: date) -> int:
    """Pick a sensible duration when the employee does not state one."""
    balance = _leave.get_balance(
        employee_id=employee.id, year=today.year, leave_type=kind
    )
    if balance is not None and balance.available > 0:
        return min(int(balance.available), 5)
    try:
        return max(1, int(_rules.default_days(
            department_id=employee.department_id, leave_type=kind
        )))
    except AppError:
        return 1


@views_bp.post("/leave/submit")
@view_login_required()
def leave_submit():
    """Create the leave request from the suggestion modal."""
    employee = _current_employee()
    if employee is None:
        flash_error("Your account is not linked to an employee record.")
        from flask import redirect

        return redirect(url_for("views.leave"))

    leave_type = form_str("leave_type", LeaveType.ANNUAL.value) or LeaveType.ANNUAL.value
    start = _parse_date(form_str("start_date"))
    end = _parse_date(form_str("end_date"))
    days = form_int("days")
    reason = form_str("reason")

    try:
        request_obj = _leave.request_leave(
            employee_id=employee.id,
            leave_type=leave_type,
            start_date=start,
            end_date=end,
            days=days,
            reason=reason,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.leave_request_form"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave submit failed")
        flash_error("The leave request could not be created.")
        from flask import redirect

        return redirect(url_for("views.leave_request_form"))

    if request_obj.status == LeaveStatus.SUGGESTED.value:
        flash_info(
            "A leave window has been suggested. Accept it, or decline once to "
            "be offered an alternative."
        )
    else:
        flash_success("Your leave request has been submitted for approval.")

    from flask import redirect

    return redirect(url_for("views.leave"))


@views_bp.post("/leave/<int:request_id>/respond")
@view_login_required()
def leave_respond(request_id: int):
    """Accept or decline the suggested window (HTMX)."""
    employee = _current_employee()
    action = form_str("action", "accept") or "accept"

    try:
        leave_request = _leave.get_request(request_id)
        if employee is None or leave_request.employee_id != employee.id:
            raise AuthorizationError("You may only respond to your own requests.")

        updated = _leave.respond(
            request_id=request_id,
            action=action,
            actor_user_id=principal().user_id,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        if wants_partial():
            return render("leave/_respond_result.html", request=None, error=error.message)
        from flask import redirect

        return redirect(url_for("views.leave"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave respond failed")
        if wants_partial():
            return render(
                "leave/_respond_result.html",
                request=None,
                error="The response could not be saved.",
            )
        from flask import redirect

        return redirect(url_for("views.leave"))

    if updated.status == LeaveStatus.APPROVED_FORCED.value:
        flash_info(
            "Your one decline has been used, so this window is now confirmed."
        )
    elif updated.status == LeaveStatus.DENIED.value:
        flash_info("Declined. A new window has been suggested.")
    elif updated.status == LeaveStatus.APPROVED.value:
        flash_success("Leave approved.")
    else:
        flash_info(f"Request is now {updated.status.replace('_', ' ')}.")

    if wants_partial():
        return render("leave/_respond_result.html", request=updated, error=None)
    from flask import redirect

    return redirect(url_for("views.leave"))


@views_bp.post("/leave/<int:request_id>/cancel")
@view_login_required()
def leave_cancel(request_id: int):
    """Cancel a request, releasing reserved days."""
    employee = _current_employee()
    try:
        leave_request = _leave.get_request(request_id)
        user = principal()
        if employee is None or (
            leave_request.employee_id != employee.id and not user.is_hr_or_admin
        ):
            raise AuthorizationError("You may only cancel your own requests.")

        _leave.cancel(request_id=request_id, actor_user_id=user.user_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave cancel failed")
        flash_error("The request could not be cancelled.")

    from flask import redirect

    target = "views.leave_admin" if principal().is_hr_or_admin else "views.leave"
    return redirect(url_for(target))


# ---------------------------------------------------------------------------
# HR queue
# ---------------------------------------------------------------------------
@views_bp.get("/leave/admin")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_admin():
    """HR queue with a month calendar and recently decided requests."""
    today = today_local(None)
    year = request.args.get("year", type=int) or today.year
    month = request.args.get("month", type=int) or today.month
    department_id = request.args.get("department_id", type=int)

    first_day, last_day = _month_bounds(year, month)
    departments = _departments.list_departments()

    # ``None`` means "every department".  Defaulting to the first department
    # silently hid every request outside it: the KPI cards counted globally
    # while the queue filtered to a department that might have no employees,
    # so a pending request appeared in the totals with no row to act on.
    selected_department = department_id

    pending = _leave.list_requests(
        statuses=[LeaveStatus.PENDING.value],
        department_id=selected_department,
    )
    suggested = _leave.list_requests(
        statuses=[LeaveStatus.SUGGESTED.value],
        department_id=selected_department,
    )
    recently_decided = [
        r
        for r in _leave.list_requests(department_id=selected_department)
        if r.status
        in {"approved", "approved_forced", "denied", "cancelled", "recalled"}
    ][:12]

    # Counts must use the *same* filter as the queue, or the cards and the list
    # disagree and the page looks broken.
    counts = _leave.counts_by_status(
        year=year, department_id=selected_department
    )

    calendar_entries = _leave.calendar(
        department_id=selected_department, start=first_day, end=last_day
    )

    return render(
        "leave/admin.html",
        active_nav="Leave queue",
        today=today,
        year=year,
        month=month,
        month_name=calendar.month_name[month],
        weeks=_build_calendar(year, month, calendar_entries),
        pending=pending,
        suggested=suggested,
        recently_decided=recently_decided,
        departments=departments,
        selected_department=selected_department,
        counts=counts,
        prev_month=_shift_month(year, month, -1),
        next_month=_shift_month(year, month, 1),
    )


@views_bp.post("/leave/<int:request_id>/decide")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_decide(request_id: int):
    """Approve or deny a request."""
    decision = form_str("decision", "approve") or "approve"
    note = form_str("note")

    try:
        updated = _leave.decide(
            request_id=request_id,
            decision=decision,
            actor_user_id=principal().user_id,
            note=note,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.leave_admin"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave decide failed")
        flash_error("The decision could not be saved.")
        from flask import redirect

        return redirect(url_for("views.leave_admin"))

    verb = "approved" if decision == "approve" else "denied"
    flash_success(
        f"{updated.employee.full_name if updated.employee else 'Request'} {verb}."
    )
    from flask import redirect

    return redirect(url_for("views.leave_admin"))


# ---------------------------------------------------------------------------
# Calendar helpers (pure)
# ---------------------------------------------------------------------------
def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


def _build_calendar(year: int, month: int, entries: list[dict]) -> list[list[dict]]:
    """Month grid where each cell lists who is on leave that day."""
    by_date: dict[date, list[dict]] = {}
    for entry in entries:
        span_start = entry["start_date"]
        span_end = entry["end_date"]
        current = max(span_start, date(year, month, 1))
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        while current <= min(span_end, month_end):
            by_date.setdefault(current, []).append(entry)
            current += timedelta(days=1)

    weeks: list[list[dict]] = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for day in week:
            row.append(
                {
                    "date": day,
                    "in_month": day.month == month,
                    "entries": by_date.get(day, []),
                }
            )
        weeks.append(row)
    return weeks


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
