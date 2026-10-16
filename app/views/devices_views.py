"""Device management views."""

from __future__ import annotations

from flask import current_app, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.auth.constants import Role
from app.features.biometrics.constants import ADAPTER_REGISTRY
from app.features.biometrics.service import BiometricsService
from app.features.employees.service import EmployeeService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_info,
    flash_success,
    form_int,
    form_str,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_devices = BiometricsService()
_employees = EmployeeService()


def _adms_connection_details() -> dict:
    """The three values to type into a terminal's server menu.

    A device menu accepts a bare host and a port, not a URL, so the configured
    public base URL is split for it. ``APP_BASE_URL`` is used when set, because
    the device must reach the public address rather than whatever host this
    request happened to arrive on.
    """
    from urllib.parse import urlparse

    raw = (current_app.config.get("APP_BASE_URL") or "").strip()
    if not raw:
        raw = request.url_root
    parsed = urlparse(raw if "//" in raw else f"https://{raw}")

    host = parsed.hostname or ""
    scheme = parsed.scheme or "https"
    port = parsed.port or (443 if scheme == "https" else 80)

    return {
        "adms_host": host,
        "adms_port": port,
        # Unauthenticated liveness probe, so reachability can be tested from a
        # browser before touching the device's configuration.
        "adms_probe_url": f"{scheme}://{host}:{port}/iclock/ping"
        if port not in (80, 443)
        else f"{scheme}://{host}/iclock/ping",
    }


@views_bp.get("/devices")
@view_login_required()
@view_roles_required(Role.ADMIN)
def devices():
    """Device inventory, mappings and raw punch log."""
    health = _devices.health()
    return render(
        "devices/list.html",
        active_nav="Devices",
        devices=_devices.list_devices(),
        health=health,
        mappings=_devices.list_mappings(),
        logs=_devices.recent_raw_logs(limit=40),
        employees=_employees.list_employees({"is_active": True}),
        vendors=sorted(ADAPTER_REGISTRY),
        new_secret=request.args.get("secret"),
        new_serial=request.args.get("serial"),
        **_adms_connection_details(),
    )


@views_bp.post("/devices")
@view_login_required()
@view_roles_required(Role.ADMIN)
def device_create():
    """Register a device; the secret is shown once via redirect."""
    try:
        device, secret = _devices.register_device(
            serial_no=form_str("serial_no", "") or "",
            location=form_str("location"),
            vendor=form_str("vendor", "zkteco"),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.devices"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("device registration failed")
        flash_error("The device could not be registered.")
        from flask import redirect

        return redirect(url_for("views.devices"))

    flash_success(f"Device {device.serial_no} registered.")
    from flask import redirect

    return redirect(
        url_for("views.devices", secret=secret, serial=device.serial_no)
    )


@views_bp.post("/devices/<int:device_id>/rotate")
@view_login_required()
@view_roles_required(Role.ADMIN)
def device_rotate(device_id: int):
    """Rotate a device's shared secret."""
    try:
        device, secret = _devices.rotate_secret(device_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.devices"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("device rotate failed")
        flash_error("The secret could not be rotated.")
        from flask import redirect

        return redirect(url_for("views.devices"))

    flash_info("Secret rotated. The device must be updated immediately.")
    from flask import redirect

    return redirect(url_for("views.devices", secret=secret, serial=device.serial_no))


@views_bp.post("/devices/<int:device_id>/set_active/<any(true,false):state>")
@view_login_required()
@view_roles_required(Role.ADMIN)
def device_set_active(device_id: int, state: str):
    """Activate or deactivate a device."""
    try:
        device = _devices.set_active(device_id, state == "true")
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.devices"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("device set_active failed")
        flash_error("The device could not be updated.")
        from flask import redirect

        return redirect(url_for("views.devices"))

    flash_success(
        f"{device.serial_no} {'activated' if device.is_active else 'deactivated'}."
    )
    from flask import redirect

    return redirect(url_for("views.devices"))


@views_bp.post("/devices/map")
@view_login_required()
@view_roles_required(Role.ADMIN)
def device_map():
    """Map a device-local user id onto an employee."""
    device_id = form_int("device_id")
    employee_id = form_int("employee_id")
    if device_id is None or employee_id is None:
        flash_error("Choose both a device and an employee.")
        from flask import redirect

        return redirect(url_for("views.devices"))

    try:
        _devices.map_employee(
            device_id=device_id,
            employee_id=employee_id,
            device_user_id=form_str("device_user_id", "") or "",
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.devices"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("device mapping failed")
        flash_error("The mapping could not be created.")
        from flask import redirect

        return redirect(url_for("views.devices"))

    flash_success("Device user mapped.")
    from flask import redirect

    return redirect(url_for("views.devices"))
