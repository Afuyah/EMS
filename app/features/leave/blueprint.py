"""Leave request/response/decision routes for the JSON API."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.core.database import db
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.core.permissions import require_roles
from app.core.responses import ok
from app.features.auth.constants import Role
from app.features.employees.service import EmployeeService
from app.features.leave.controller import LeaveController

leave_api = Blueprint("leave_api", __name__, url_prefix="/leave")

_controller = LeaveController()
_employees = EmployeeService()


def _current_user_id() -> int:
    identity = get_jwt_identity()
    if identity is None:
        raise AuthorizationError()
    return int(identity)


def _current_employee_id() -> int:
    """The employee record behind the caller's login."""
    employee = _employees.get_employee_by_user(_current_user_id())
    if employee is None:
        raise AuthorizationError(
            "Your account is not linked to an employee record."
        )
    return employee.id


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc


@leave_api.post("/request")
@jwt_required()
def request_leave():
    """Create a leave request, returning the suggested window(s)."""
    payload = request.get_json(silent=True) or {}

    # HR/Admin may request on behalf of someone else; staff may only self-serve.
    from app.core.permissions import current_principal

    principal = current_principal()
    if payload.get("employee_id") and not principal.is_hr_or_admin:
        raise AuthorizationError("You may only request leave for yourself.")

    actor_employee_id = None if payload.get("employee_id") else _current_employee_id()
    return _controller.request_leave(payload, actor_employee_id=actor_employee_id)


@leave_api.get("/requests")
@jwt_required()
def list_requests():
    """Leave queue, scoped to the caller's role."""
    from app.core.permissions import current_principal

    principal = current_principal()
    query = request.args.to_dict()

    if principal.is_hr_or_admin:
        return _controller.list_requests(query)

    employee = _employees.get_employee_by_user(_current_user_id())
    if employee is None:
        raise AuthorizationError("Your account is not linked to an employee record.")

    if principal.is_manager:
        # Managers see their department, but only their own requests in detail.
        department_id = request.args.get("department_id", type=int)
        if department_id is None:
            return _controller.my_requests(employee.id, query)

    return _controller.my_requests(employee.id, query)


@leave_api.get("/requests/<int:request_id>")
@jwt_required()
def get_request(request_id: int):
    """One leave request with its full transition history."""
    return _controller.get_request(request_id)


@leave_api.post("/requests/<int:request_id>/respond")
@jwt_required()
def respond(request_id: int):
    """Employee accepts or denies the suggested window."""
    return _controller.respond(
        request_id,
        request.get_json(silent=True) or {},
        actor_user_id=_current_user_id(),
    )


@leave_api.post("/requests/<int:request_id>/decide")
@jwt_required()
def decide(request_id: int):
    """HR/Admin approves or denies a request."""
    require_roles(Role.HR, Role.ADMIN)
    return _controller.decide(
        request_id,
        request.get_json(silent=True) or {},
        actor_user_id=_current_user_id(),
    )


@leave_api.post("/requests/<int:request_id>/cancel")
@jwt_required()
def cancel(request_id: int):
    """Cancel a request, releasing any reserved days."""
    return _controller.cancel(
        request_id,
        request.get_json(silent=True) or {},
        actor_user_id=_current_user_id(),
    )


@leave_api.get("/balances/<int:employee_id>")
@jwt_required()
def balances(employee_id: int):
    """Leave balances for one employee and year."""
    year = request.args.get("year", type=int) or date.today().year
    return _controller.my_balances(employee_id, year)


@leave_api.post("/balances/adjust")
@jwt_required()
def adjust_balance():
    """Manual balance correction (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    payload = request.get_json(silent=True) or {}
    if not payload.get("employee_id") or not payload.get("year") or not payload.get("leave_type"):
        raise ValidationError("employee_id, year and leave_type are required.")
    return _controller.adjust_balance(payload)


@leave_api.post("/balances/allocate")
@jwt_required()
def allocate_balances():
    """Allocate year-start entitlements (Admin)."""
    require_roles(Role.ADMIN)
    payload = request.get_json(silent=True) or {}
    year = int(payload.get("year") or date.today().year)
    return _controller.allocate_year(year, overwrite=bool(payload.get("overwrite")))


@leave_api.get("/rules")
@jwt_required()
def list_rules():
    """Configured leave rules (global plus per-department)."""
    from app.features.leave.rules_service import LeaveRulesService

    department_id = request.args.get("department_id", type=int)
    rules = LeaveRulesService().list_rules(department_id)
    return ok(
        [
            {
                "id": rule.id,
                "department_id": rule.department_id,
                "scope": "global" if rule.is_global else "department",
                "leave_type": rule.leave_type,
                "max_per_year": rule.max_per_year,
                "max_per_month": rule.max_per_month,
                "max_consecutive_days": rule.max_consecutive_days,
                "max_employees_off_per_dept": rule.max_employees_off_per_dept,
                "min_notice_days": rule.min_notice_days,
                "default_days": rule.default_days,
                "is_active": rule.is_active,
            }
            for rule in rules
        ]
    )
