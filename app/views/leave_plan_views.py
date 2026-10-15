"""Annual leave plan views: the year-at-a-glance roster for HR, managers and staff.

Views own the transaction boundary: they commit on success and roll back on
failure.  All planning logic lives in :class:`LeavePlanService`.
"""

from __future__ import annotations

from datetime import date

from flask import redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from datetime import date
from app.features.auth.constants import Role
from app.features.employees.service import DepartmentService, EmployeeService
from app.features.leave.planner_service import LeavePlanService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    form_bool,
    form_int,
    principal,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_plans = LeavePlanService()
_departments = DepartmentService()
_employees = EmployeeService()


@views_bp.get("/leave/plan")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN, Role.MANAGER)
def leave_plan():
    """The annual roster: who is off, per department, month by month."""
    # GET filters come from the query string, not the form body — reading them
    # with form_int would silently ignore ?department_id=... entirely.
    year = request.args.get("year", type=int) or date.today().year
    department_id = request.args.get("department_id", type=int)
    departments = _departments.list_departments()

    plan = _plans.find_plan(department_id=department_id, year=year)
    months = _plans.monthly_breakdown(department_id=department_id, year=year)
    items = _plans.items_for_department(
        department_id=department_id, year=year, published_only=False
    )

    # ``items`` includes rows from any plan covering this department, so a draft
    # can exist for a scope the lookup missed.  Surface whichever plan actually
    # owns those rows, or the publish controls would never render.
    draft_plan = plan
    if draft_plan is None and items:
        draft_plan = _plans.get_plan(items[0].plan_id)

    total_off_days = sum(month["count"] for month in months)
    placed = sum(1 for item in items if item.is_placed)

    return render(
        "leave/plan.html",
        active_nav="Annual plan",
        year=year,
        prev_year=year - 1,
        next_year=year + 1,
        departments=departments,
        selected_department=department_id,
        plan=draft_plan,
        months=months,
        items=items,
        placed_count=placed,
        unplaced_count=len(items) - placed,
        total_off_days=total_off_days,
        is_hr=principal().is_hr_or_admin,
    )


@views_bp.post("/leave/plan/generate")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_plan_generate():
    """Draft a roster for one department and year.

    A department must be chosen.  The concurrency cap is *per department*, so
    planning "all departments" as one pool would apply a single cap across the
    whole company and serialise everyone.  Each department is planned on its own.
    """
    year = form_int("year") or date.today().year
    department_id = form_int("department_id")
    overwrite = form_bool("overwrite", False)

    if department_id is None:
        flash_error(
            "Choose a department to plan. The limit on how many people can be off "
            "applies per department, so each one is planned separately."
        )
        return redirect(url_for("views.leave_plan", year=year))

    try:
        plan, result = _plans.generate(
            department_id=department_id,
            year=year,
            actor_user_id=principal().user_id,
            overwrite=overwrite,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.leave_plan", year=year,
                                department_id=department_id))
    except Exception:  # noqa: BLE001 - unexpected failures still roll back
        rollback_session()
        logger.exception("leave plan generation failed")
        flash_error("The roster could not be generated.")
        return redirect(url_for("views.leave_plan", year=year,
                                department_id=department_id))

    flash_success(
        f"Draft roster created for {year}: {len(result.placed)} window(s) placed, "
        f"peak {result.peak_concurrent} off per day"
        + (
            f", {len(result.unplaced)} employee(s) not fully placed"
            if result.unplaced
            else ""
        )
        + "."
    )
    return redirect(url_for("views.leave_plan", year=year,
                            department_id=department_id))


@views_bp.post("/leave/plan/<int:plan_id>/publish")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_plan_publish(plan_id: int):
    """Publish a roster so employees can see their dates."""
    try:
        plan = _plans.publish(plan_id=plan_id, actor_user_id=principal().user_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.leave_plan"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave plan publish failed")
        flash_error("The roster could not be published.")
        return redirect(url_for("views.leave_plan"))

    note = (
        f" {plan.unplaced_count} employee(s) still have no dates."
        if plan.unplaced_count
        else ""
    )
    flash_success(
        f"Roster published for {plan.year}. Employees can now see their dates."
        + note
    )
    return redirect(url_for("views.leave_plan", year=plan.year,
                            department_id=plan.department_id or ""))


@views_bp.post("/leave/plan/<int:plan_id>/unpublish")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_plan_unpublish(plan_id: int):
    """Return a roster to draft, hiding the dates again."""
    try:
        plan = _plans.unpublish(plan_id=plan_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.leave_plan"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave plan unpublish failed")
        flash_error("The roster could not be returned to draft.")
        return redirect(url_for("views.leave_plan"))

    flash_success(
        f"Roster for {plan.year} returned to draft; employees no longer see those dates."
    )
    return redirect(url_for("views.leave_plan", year=plan.year))


@views_bp.post("/leave/plan/<int:plan_id>/materialise")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_plan_materialise(plan_id: int):
    """Turn published allocations into approved leave requests."""
    try:
        summary = _plans.create_requests_for_plan(
            plan_id=plan_id, actor_user_id=principal().user_id
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.leave_plan"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave plan materialise failed")
        flash_error("The allocations could not be turned into leave requests.")
        return redirect(url_for("views.leave_plan"))

    message = (
        f"{summary['created']} leave request(s) created and approved"
        f"{', ' + str(summary['skipped']) + ' skipped' if summary['skipped'] else ''}."
    )
    blocked = summary.get("blocked") or []
    if blocked:
        # Say why, rather than leaving HR to wonder about missing requests.
        first = blocked[0]
        flash_error(
            message
            + f" {len(blocked)} could not be created without breaking the "
            f"department limit — for example {first['employee_no']} "
            f"({first['start_date']} to {first['end_date']}). "
            f"Re-generate the roster, or raise the limit for that department."
        )
    elif summary["failures"]:
        flash_error(
            message
            + f" {len(summary['failures'])} could not be created (usually no "
            f"balance): {summary['failures'][0]['error']}"
        )
    else:
        flash_success(message)
    return redirect(url_for("views.leave_plan"))


@views_bp.post("/leave/recall/<int:request_id>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def leave_recall(request_id: int):
    """Recall an employee from approved leave and propose replacement dates."""
    from app.features.leave.service import LeaveService

    reason = (request.args.get("reason") or "").strip() or None
    propose = form_bool("propose_new_dates", True)

    try:
        result = LeaveService().recall(
            request_id=request_id,
            actor_user_id=principal().user_id,
            reason=reason,
            propose_new_dates=propose,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.leave_admin"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("leave recall failed")
        flash_error("The employee could not be recalled.")
        return redirect(url_for("views.leave_admin"))

    replacement = result["replacement"]
    if replacement is not None:
        flash_success(
            "Employee recalled and their days returned. A replacement window "
            f"({replacement.start_date.strftime('%d %b')} - "
            f"{replacement.end_date.strftime('%d %b')}) was proposed for them to confirm."
        )
    else:
        flash_success(
            "Employee recalled and their days returned. No replacement window "
            "fits right now, so they can request new dates themselves."
        )
    return redirect(url_for("views.leave_admin"))
