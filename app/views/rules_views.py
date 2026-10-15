"""Admin views for lateness and absenteeism rules.

One page, two rule families:

* the attendance-health colour ladder (drives employee badges)
* the disciplinary escalation ladder (drives HR actions)

Views are HTTP glue only: they validate form input, call
:class:`~app.features.rules.rules_admin_service.RuleAdminService`, and own the
transaction boundary — committing on success, rolling back on failure.
"""

from __future__ import annotations

from flask import redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.auth.constants import Role
from app.features.employees.constants import EmployeeColor
from app.features.employees.service import DepartmentService
from app.features.rules.constants import (
    DisciplinaryActionType,
    RulePeriod,
    TriggerType,
)
from app.features.rules.rules_admin_service import RuleAdminService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    form_bool,
    form_checkbox,
    form_int,
    form_str,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_rules = RuleAdminService()
_departments = DepartmentService()


def _options() -> dict:
    """Enum choices and departments shared by the page and its forms."""
    return {
        "colors": list(EmployeeColor),
        "triggers": list(TriggerType),
        "periods": list(RulePeriod),
        "actions": list(DisciplinaryActionType),
        "departments": _departments.list_departments(),
        "suspension_actions": [
            DisciplinaryActionType.SUSPENSION,
        ],
    }


def _optional_int(value: str | None) -> int | None:
    """Blank means "not supplied" rather than zero."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
@views_bp.get("/rules")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rules():
    """Both rule families, global first, with inline create and edit forms."""
    return render(
        "rules/index.html",
        active_nav="Attendance rules",
        color_rules=_rules.list_color_rules(),
        disciplinary_rules=_rules.list_disciplinary_rules(),
        summary=_rules.summary(),
        **_options(),
    )


@views_bp.get("/rules/colors/<int:rule_id>/edit")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_color_edit(rule_id: int):
    """Edit one colour rule."""
    try:
        rule = _rules.get_color_rule(rule_id)
    except AppError as error:
        flash_error(error.message)
        return redirect(url_for("views.rules"))

    return render(
        "rules/color_form.html",
        active_nav="Attendance rules",
        rule=rule,
        **_options(),
    )


@views_bp.get("/rules/disciplinary/<int:rule_id>/edit")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_disciplinary_edit(rule_id: int):
    """Edit one disciplinary rule."""
    try:
        rule = _rules.get_disciplinary_rule(rule_id)
    except AppError as error:
        flash_error(error.message)
        return redirect(url_for("views.rules"))

    return render(
        "rules/disciplinary_form.html",
        active_nav="Attendance rules",
        rule=rule,
        **_options(),
    )


# ---------------------------------------------------------------------------
# Colour rule writes
# ---------------------------------------------------------------------------
@views_bp.post("/rules/colors")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_color_create():
    """Add a rung to the colour ladder."""
    try:
        rule = _rules.create_color_rule(
            color=form_str("color", "") or "",
            department_id=form_int("department_id"),
            min_lates=form_int("min_lates", 0) or 0,
            min_absences=form_int("min_absences", 0) or 0,
            window_days=form_int("window_days", 30) or 30,
            priority=form_int("priority", 0) or 0,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rules"))
    except Exception:  # noqa: BLE001 - unexpected failures still roll back
        rollback_session()
        logger.exception("colour rule create failed")
        flash_error("The colour rule could not be created.")
        return redirect(url_for("views.rules"))

    flash_success(
        f"Colour rule added: {rule.color} at {rule.min_lates}+ late and "
        f"{rule.min_absences}+ absent days over {rule.window_days} days."
    )
    return redirect(url_for("views.rules"))


@views_bp.post("/rules/colors/<int:rule_id>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_color_update(rule_id: int):
    """Update a colour rule in place."""
    try:
        rule = _rules.update_color_rule(
            rule_id,
            color=form_str("color"),
            min_lates=form_int("min_lates"),
            min_absences=form_int("min_absences"),
            window_days=form_int("window_days"),
            priority=form_int("priority"),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rule_color_edit", rule_id=rule_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("colour rule update failed")
        flash_error("The colour rule could not be updated.")
        return redirect(url_for("views.rule_color_edit", rule_id=rule_id))

    flash_success(f"Colour rule '{rule.color}' updated.")
    return redirect(url_for("views.rules"))


@views_bp.post("/rules/colors/<int:rule_id>/delete")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_color_delete(rule_id: int):
    """Delete a colour rule."""
    try:
        _rules.delete_color_rule(rule_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rules"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("colour rule delete failed")
        flash_error("The colour rule could not be deleted.")
        return redirect(url_for("views.rules"))

    flash_success("Colour rule deleted.")
    return redirect(url_for("views.rules"))


# ---------------------------------------------------------------------------
# Disciplinary rule writes
# ---------------------------------------------------------------------------
@views_bp.post("/rules/disciplinary")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_disciplinary_create():
    """Add a disciplinary threshold."""
    try:
        rule = _rules.create_disciplinary_rule(
            trigger_type=form_str("trigger_type", "") or "",
            # No coercion default: an empty threshold must reach the service so
            # it is rejected rather than silently becoming a valid number.
            threshold=form_int("threshold"),
            period=form_str("period", "") or "",
            action=form_str("action", "") or "",
            department_id=form_int("department_id"),
            suspension_days=_optional_int(request.form.get("suspension_days")),
            auto_apply=form_checkbox("auto_apply"),
            is_active=form_checkbox("is_active"),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rules"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("disciplinary rule create failed")
        flash_error("The disciplinary rule could not be created.")
        return redirect(url_for("views.rules"))

    applied = "applied automatically" if rule.auto_apply else "proposed to HR"
    flash_success(
        f"Rule added: {rule.threshold}+ {rule.trigger_type} per {rule.period} "
        f"→ {rule.action.replace('_', ' ')} ({applied})."
    )
    return redirect(url_for("views.rules"))


@views_bp.post("/rules/disciplinary/<int:rule_id>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_disciplinary_update(rule_id: int):
    """Update a disciplinary rule in place."""
    try:
        rule = _rules.update_disciplinary_rule(
            rule_id,
            trigger_type=form_str("trigger_type"),
            threshold=form_int("threshold"),
            period=form_str("period"),
            action=form_str("action"),
            suspension_days=_optional_int(request.form.get("suspension_days")),
            auto_apply=form_checkbox("auto_apply"),
            is_active=form_checkbox("is_active"),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rule_disciplinary_edit", rule_id=rule_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("disciplinary rule update failed")
        flash_error("The disciplinary rule could not be updated.")
        return redirect(url_for("views.rule_disciplinary_edit", rule_id=rule_id))

    flash_success("Disciplinary rule updated.")
    return redirect(url_for("views.rules"))


@views_bp.post("/rules/disciplinary/<int:rule_id>/active/<any(true,false):state>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_disciplinary_set_active(rule_id: int, state: str):
    """Enable or disable a disciplinary rule."""
    is_active = state == "true"
    try:
        rule = _rules.set_disciplinary_rule_active(rule_id, is_active)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rules"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("disciplinary rule toggle failed")
        flash_error("The rule could not be updated.")
        return redirect(url_for("views.rules"))

    flash_success(
        f"Rule at {rule.threshold}+ {rule.trigger_type} "
        f"{'enabled' if is_active else 'disabled'}."
    )
    return redirect(url_for("views.rules"))


@views_bp.post("/rules/disciplinary/<int:rule_id>/delete")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def rule_disciplinary_delete(rule_id: int):
    """Delete a disciplinary rule that has produced no actions."""
    try:
        _rules.delete_disciplinary_rule(rule_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.rules"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("disciplinary rule delete failed")
        flash_error("The rule could not be deleted.")
        return redirect(url_for("views.rules"))

    flash_success("Disciplinary rule deleted.")
    return redirect(url_for("views.rules"))
