"""JSON API routes for biometric devices and punch ingestion.

Device-facing routes live on ``hooks_bp`` (HMAC-authenticated by the
``before_request`` guard in this module); management routes live on
``devices_api`` under ``/api/v1``.
"""

from __future__ import annotations

from flask import Blueprint, current_app, g, jsonify, request
from flask_jwt_extended import jwt_required

from app.core.logging import get_logger
from app.core.permissions import require_roles
from app.core.responses import created, ok
from app.features.auth.constants import Role
from app.features.biometrics.constants import (
    HEADER_DEVICE_SN,
    HEADER_DEVICE_SECRET,
)
from app.features.biometrics.controller import BiometricsController
from app.features.biometrics.exceptions import DeviceAuthError

logger = get_logger(__name__)

#: Device-facing webhook blueprint (no JWT; authenticates by device headers).
hooks_bp = Blueprint("hooks", __name__, url_prefix="/hooks")

#: Management API blueprint.
devices_api = Blueprint("devices_api", __name__, url_prefix="/devices")

_controller = BiometricsController()


@hooks_bp.before_request
def authenticate_device():
    """Verify ``X-Device-SN`` / ``X-Device-Secret`` for every webhook call."""
    serial = request.headers.get(HEADER_DEVICE_SN)
    secret = request.headers.get(HEADER_DEVICE_SECRET)
    if not serial or not secret:
        logger.warning(
            "device auth rejected: missing headers path=%s ip=%s",
            request.path,
            request.remote_addr,
        )
        raise DeviceAuthError("Device credentials are required.")

    device = _controller.authenticate_device(serial, secret)
    # Stash on the request globals for the view to reuse (no re-query).
    g.biometric_device = device
    return None


@hooks_bp.post("/biometrics/punch")
def ingest_punch():
    """Accept one punch from a device.  Idempotent on retries."""
    payload = request.get_json(silent=True) or {}
    body, status = _controller.ingest_punch(
        device=g.biometric_device,
        payload=payload,
        raw_headers={
            key: value
            for key, value in request.headers.items()
            if key.lower().startswith("x-")
        },
    )
    return jsonify(body), status


@hooks_bp.post("/biometrics/heartbeat")
def heartbeat():
    """Let a device report that it is alive."""
    body, status = _controller.heartbeat(device=g.biometric_device)
    return jsonify(body), status


# ---------------------------------------------------------------------------
# Management API
# ---------------------------------------------------------------------------
@devices_api.get("")
@jwt_required()
def list_devices():
    """All registered devices (Admin)."""
    require_roles(Role.ADMIN)
    return ok(_controller.list_devices())


@devices_api.post("")
@jwt_required()
def create_device():
    """Register a device and return its one-time shared secret (Admin)."""
    require_roles(Role.ADMIN)
    body, secret = _controller.register_device(request.get_json(silent=True) or {})
    return created({**body, "secret": secret})


@devices_api.post("/<int:device_id>/rotate")
@jwt_required()
def rotate_secret(device_id: int):
    """Rotate a device's shared secret (Admin)."""
    require_roles(Role.ADMIN)
    body, secret = _controller.rotate_secret(device_id)
    return ok({**body, "secret": secret})


@devices_api.get("/map")
@jwt_required()
def list_mappings():
    """Device-to-employee mappings (Admin)."""
    require_roles(Role.ADMIN)
    serial = request.args.get("serial_no")
    return ok(_controller.list_mappings(serial_no=serial))


@devices_api.post("/map")
@jwt_required()
def create_mapping():
    """Map a device-local user id to an employee (Admin)."""
    require_roles(Role.ADMIN)
    return created(_controller.map_employee(request.get_json(silent=True) or {}))


@devices_api.get("/raw-logs")
@jwt_required()
def raw_logs():
    """Recent raw punch rows (Admin)."""
    require_roles(Role.ADMIN)
    return ok(
        _controller.recent_raw_logs(
            device_serial=request.args.get("serial_no"),
            limit=request.args.get("limit", type=int) or 100,
        )
    )
