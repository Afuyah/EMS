"""Attendance policy admin views: shifts, grace, lateness tiers, absence cut-off.

Views are HTTP glue only.  They validate form input, call
:class:`~app.features.attendance.service.AttendancePolicyService`, and own the
transaction boundary — committing on success, rolling back on failure.
"""

from __future__ import annotations

from datetime import datetime, time

from flask import redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.attendance.service import AttendancePolicyService
from app.features.auth.constants import Role
from app.features.employees.service import DepartmentService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    form_bool,
    form_int,
    form_str,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_policies = AttendancePolicyService()
_departments = DepartmentService()


def _parse_clock(raw: str | None, fallback: time) -> time:
    """Parse ``HH:MM`` (also accepts ``HH:MM:SS``)."""
    if not raw:
        return fallback
    text = str(raw).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise AppError(f"'{text}' is not a valid time; use HH:MM.")


def _optional_minutes(raw: str | None) -> int | None:
    """Blank means "not configured" rather than zero."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


@views_bp.get("/attendance/policies")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policies():
    """Every shift policy, global first, with the resolved chain per department."""
    policies = _policies.list_policies()
    departments = _departments.list_departments()

    # Show what each department actually gets, so an override is never invisible.
    resolved = [
        {
            "department": department,
            **_policies.resolved_preview(department.id),
        }
        for department in departments
    ]

    return render(
        "attendance/policies.html",
        active_nav="Attendance rules",
        policies=policies,
        resolved=resolved,
        global_preview=_policies.resolved_preview(None),
        departments=departments,
    )


@views_bp.get("/attendance/policies/new")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_new():
    """Create a policy for a department (or the global default)."""
    return render(
        "attendance/policy_form.html",
        active_nav="Attendance rules",
        policy=None,
        departments=_departments.list_departments(),
        preview=_policies.resolved_preview(None),
        mode="create",
    )


@views_bp.get("/attendance/policies/<int:policy_id>/edit")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_edit(policy_id: int):
    """Edit one policy."""
    try:
        policy = _policies.get_policy_or_raise(policy_id)
    except AppError as error:
        flash_error(error.message)
        return redirect(url_for("views.attendance_policies"))

    return render(
        "attendance/policy_form.html",
        active_nav="Attendance rules",
        policy=policy,
        departments=_departments.list_departments(),
        preview=_policies.resolved_preview(policy.department_id),
        mode="edit",
    )


@views_bp.post("/attendance/policies")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_create():
    """Create or update the policy for the chosen scope."""
    department_id = form_int("department_id")
    shift_start = _parse_clock(form_str("shift_start"), time(9, 0))
    shift_end = _parse_clock(form_str("shift_end"), time(17, 0))

    try:
        policy = _policies.upsert(
            department_id=department_id,
            shift_start=shift_start,
            shift_end=shift_end,
            grace_minutes=form_int("grace_minutes", 0) or 0,
            very_late_after_minutes=form_int("very_late_after_minutes"),
            auto_absent_after_minutes=_optional_minutes(
                request.form.get("auto_absent_after_minutes")
            ),
            is_active=form_bool("is_active", True),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.attendance_policy_new"))
    except Exception:  # noqa: BLE001 - unexpected failures still roll back
        rollback_session()
        logger.exception("attendance policy create failed")
        flash_error("The attendance policy could not be saved.")
        return redirect(url_for("views.attendance_policy_new"))

    scope = "the global default" if policy.is_global else (
        policy.department.name if policy.department else "the department"
    )
    flash_success(
        f"Saved {scope}: {policy.shift_start.strftime('%H:%M')}\u2013"
        f"{policy.shift_end.strftime('%H:%M')}, {policy.grace_minutes} min grace, "
        f"very late after {policy.very_late_after_minutes} min"
        + (
            f", absent after {policy.auto_absent_label}"
            if policy.auto_absent_after_minutes is not None
            else ""
        )
        + "."
    )
    return redirect(url_for("views.attendance_policies"))


@views_bp.post("/attendance/policies/<int:policy_id>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_update(policy_id: int):
    """Update an existing policy (scope is immutable)."""
    try:
        policy = _policies.get_policy_or_raise(policy_id)
    except AppError as error:
        flash_error(error.message)
        return redirect(url_for("views.attendance_policies"))

    shift_start = _parse_clock(form_str("shift_start"), policy.shift_start)
    shift_end = _parse_clock(form_str("shift_end"), policy.shift_end)

    try:
        _policies.upsert(
            department_id=policy.department_id,
            shift_start=shift_start,
            shift_end=shift_end,
            grace_minutes=form_int("grace_minutes", policy.grace_minutes)
            or 0,
            very_late_after_minutes=form_int("very_late_after_minutes"),
            auto_absent_after_minutes=_optional_minutes(
                request.form.get("auto_absent_after_minutes")
            ),
            is_active=form_bool("is_active", True),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.attendance_policy_edit", policy_id=policy_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("attendance policy update failed")
        flash_error("The attendance policy could not be saved.")
        return redirect(url_for("views.attendance_policy_edit", policy_id=policy_id))

    flash_success("Attendance policy updated.")
    return redirect(url_for("views.attendance_policies"))


@views_bp.post("/attendance/policies/<int:policy_id>/active/<any(true,false):state>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_set_active(policy_id: int, state: str):
    """Enable or disable a policy."""
    try:
        policy = _policies.set_active(policy_id, state == "true")
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.attendance_policies"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("attendance policy toggle failed")
        flash_error("The policy could not be updated.")
        return redirect(url_for("views.attendance_policies"))

    flash_success(f"Policy {'enabled' if policy.is_active else 'disabled'}.")
    return redirect(url_for("views.attendance_policies"))


@views_bp.post("/attendance/policies/<int:policy_id>/delete")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def attendance_policy_delete(policy_id: int):
    """Delete a department override."""
    try:
        _policies.delete(policy_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.attendance_policies"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("attendance policy delete failed")
        flash_error("The policy could not be deleted.")
        return redirect(url_for("views.attendance_policies"))

    flash_success("Department policy deleted; it now follows the global default.")
    return redirect(url_for("views.attendance_policies"))
