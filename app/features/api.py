"""The ``/api/v1`` JSON blueprint.

Feature routers are separate blueprints registered underneath the version
prefix, so every feature owns its own HTTP surface.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

#: Version prefix is applied by the factory so the docs blueprint can sit
#: alongside it at /api/docs.
api_v1 = Blueprint("api_v1", __name__)


def _register_feature_routers() -> None:
    """Import and attach each feature's API router."""
    from app.features.attendance.blueprint import attendance_api
    from app.features.auth.blueprint import auth_api
    from app.features.biometrics.blueprint import devices_api
    from app.features.employees.blueprint import employees_api
    from app.features.exports.blueprint import exports_api
    from app.features.leave.blueprint import leave_api
    from app.features.notifications.blueprint import notifications_api
    from app.features.reports.blueprint import reports_api

    for router in (
        auth_api,
        employees_api,
        attendance_api,
        leave_api,
        notifications_api,
        reports_api,
        exports_api,
        devices_api,
    ):
        api_v1.register_blueprint(router)


@api_v1.get("")
@api_v1.get("/")
def api_root():
    """Service descriptor for the API root."""
    return jsonify(
        {
            "data": {
                "name": "EMS JSON API",
                "version": "v1",
                "docs": "/api/docs/ui",
                "openapi": "/api/docs/openapi.json",
            }
        }
    )


_register_feature_routers()
