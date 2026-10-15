"""Report routes for the JSON API."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, request
from flask_jwt_extended import jwt_required

from app.core.exceptions import ValidationError
from app.core.permissions import require_roles
from app.core.responses import ok
from app.features.auth.constants import Role
from app.features.reports.service import ReportsService

reports_api = Blueprint("reports_api", __name__, url_prefix="/reports")

_service = ReportsService()


def _date_arg(name: str) -> date | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"Invalid {name} {raw!r}; expected YYYY-MM-DD.") from exc


def _year_arg() -> int:
    return request.args.get("year", type=int) or date.today().year


@reports_api.get("")
@jwt_required()
def overview():
    """Headline workforce counts."""
    return ok(_service.overview())


@reports_api.get("/attendance/colors")
@jwt_required()
def attendance_colors():
    """Attendance-health colour distribution."""
    return ok(_service.attendance_colors(department_id=request.args.get("department_id", type=int)))


@reports_api.get("/attendance/department/<int:department_id>")
@jwt_required()
def department_attendance(department_id: int):
    """Attendance summary for one department over a range."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    return ok(
        _service.department_attendance(
            department_id=department_id, start=start, end=end
        )
    )


@reports_api.get("/attendance/headcount")
@jwt_required()
def headcount():
    """Daily headcount, clock-ins and leave counts."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    return ok(_service.headcount(start=start, end=end))


@reports_api.get("/attendance/top_late")
@jwt_required()
def top_late():
    """Employees with the most late arrivals."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    limit = min(request.args.get("limit", type=int) or 10, 100)
    return ok(
        _service.top_late(
            start=start,
            end=end,
            department_id=request.args.get("department_id", type=int),
            limit=limit,
        )
    )


@reports_api.get("/leave/utilization")
@jwt_required()
def leave_utilisation():
    """Leave entitlement utilisation by type."""
    return ok(
        _service.leave_utilisation(
            year=_year_arg(), department_id=request.args.get("department_id", type=int)
        )
    )


@reports_api.get("/leave/calendar/<int:department_id>")
@jwt_required()
def leave_calendar(department_id: int):
    """Approved leave across a window for one department."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    return ok(_service.leave_calendar(department_id=department_id, start=start, end=end))


@reports_api.get("/leave/balances/<int:department_id>")
@jwt_required()
def balance_ledger(department_id: int):
    """Per-employee balance ledger for a department."""
    return ok(_service.balance_ledger(department_id=department_id, year=_year_arg()))


@reports_api.get("/leave/requests")
@jwt_required()
def leave_request_counts():
    """Leave request counts grouped by status."""
    from app.features.leave.service import LeaveService

    require_roles(Role.HR, Role.ADMIN)
    return ok(LeaveService().counts_by_status(year=_year_arg() or None))
