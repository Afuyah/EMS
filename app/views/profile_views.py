"""Self-service profile views: the employee's own details and password.

An employee may edit their first name, last name, email and timezone.  The
employee number and phone number belong to HR and are shown read-only with an
explanation, rather than being quietly ignored.
"""

from __future__ import annotations

from flask import redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.auth.constants import Role
from app.features.employees.service import EmployeeService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_info,
    flash_success,
    form_str,
    principal,
    render,
    rollback_session,
    view_login_required,
)

logger = get_logger(__name__)

_employees = EmployeeService()


def _current_employee():
    """The employee record behind the logged-in user, or ``None``."""
    return _employees.get_employee_by_user(principal().user_id)


@views_bp.get("/profile")
@view_login_required()
def profile():
    """Show the employee's own profile and the fields they may change."""
    employee = _current_employee()
    if employee is None:
        flash_info(
            "Your login is not linked to an employee record, so there is no "
            "profile to edit. Ask HR to link it."
        )
        return render(
            "profile/index.html",
            active_nav="My profile",
            employee=None,
        )

    return render(
        "profile/index.html",
        active_nav="My profile",
        employee=employee,
        user=principal(),
        is_hr=principal().is_hr_or_admin,
    )


@views_bp.post("/profile")
@view_login_required()
def profile_update():
    """Update the editable profile fields."""
    employee = _current_employee()
    if employee is None:
        flash_error("Your login is not linked to an employee record.")
        return redirect(url_for("views.profile"))

    # Only forward fields that were actually submitted, so an untouched input
    # never blanks a column.
    changes: dict[str, str] = {}
    for field in ("first_name", "last_name", "email", "tz"):
        if field in request.form:
            value = form_str(field)
            if value is not None:
                changes[field] = value

    # Any attempt to edit an HR-owned field is refused loudly rather than
    # silently dropped.
    for locked in ("employee_no", "phone", "department_id", "position_id", "hired_at"):
        if locked in request.form and request.form.get(locked):
            submitted = form_str(locked)
            if locked == "employee_no" and submitted == employee.employee_no:
                continue  # unchanged value echoed back by the form
            if locked == "phone" and submitted == (employee.phone or ""):
                continue
            changes[locked] = submitted

    try:
        _employees.update_own_profile(employee.id, **changes)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.profile"))
    except Exception:  # noqa: BLE001 - unexpected failures still roll back
        rollback_session()
        logger.exception("profile update failed")
        flash_error("Your profile could not be saved.")
        return redirect(url_for("views.profile"))

    flash_success("Your profile has been updated.")
    return redirect(url_for("views.profile"))


@views_bp.post("/profile/password")
@view_login_required()
def profile_password():
    """Change the password, requiring the current one."""
    employee = _current_employee()
    if employee is None:
        flash_error("Your login is not linked to an employee record.")
        return redirect(url_for("views.profile"))

    current_password = form_str("current_password", "") or ""
    new_password = form_str("new_password", "") or ""
    confirm_password = form_str("confirm_password", "") or ""

    if new_password != confirm_password:
        flash_error("The new password and its confirmation do not match.")
        return redirect(url_for("views.profile"))

    try:
        _employees.change_own_password(
            employee.id,
            current_password=current_password,
            new_password=new_password,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.profile"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("password change failed")
        flash_error("Your password could not be changed.")
        return redirect(url_for("views.profile"))

    flash_success(
        "Your password has been changed. All other sessions were signed out."
    )
    return redirect(url_for("views.profile"))
