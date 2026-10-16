"""HTML view blueprint for the web UI.

Routes live in one place, but each handler delegates to a feature **service**
and owns its own transaction boundary.  Views never import repositories or
models.
"""

from __future__ import annotations

from flask import Blueprint

views_bp = Blueprint(
    "views",
    __name__,
    url_prefix="/app",
    template_folder="../templates",
)


def _register_view_modules() -> None:
    """Import view modules so their routes attach to this blueprint."""
    from app.views import (  # noqa: F401
        attendance_policy_views,
        attendance_views,
        auth_views,
        dashboard_views,
        devices_views,
        employee_views,
        leave_plan_views,
        leave_views,
        notifications_views,
        profile_views,
        reports_views,
        rules_views,
    )


@views_bp.get("")
@views_bp.get("/")
def index():
    """Bare ``/app`` sends the visitor to the right place."""
    from flask import redirect, url_for

    from app.views.helpers import is_signed_in

    if is_signed_in():
        return redirect(url_for("views.dashboard"))
    return redirect(url_for("views.login"))


_register_view_modules()
