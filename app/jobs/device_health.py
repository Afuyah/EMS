"""Device health job.

Runs every five minutes.  Notifies administrators about devices that have
stopped calling in, at most once per offline episode per device.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.biometrics.constants import (
    DEVICE_OFFLINE_AFTER_MINUTES,
    EVENT_DEVICE_OFFLINE,
)
from app.features.biometrics.repository import DeviceRepository
from app.features.notifications.constants import Channel
from app.features.notifications.service import NotificationService

logger = get_logger(__name__)


def device_health_impl(
    *,
    offline_after_minutes: int = DEVICE_OFFLINE_AFTER_MINUTES,
    notify: bool = True,
) -> dict:
    """Detect offline devices and alert admins once per episode.

    "Once per episode" is enforced by checking for an existing unread
    ``device_offline`` notification mentioning this serial, so a device that
    stays down for hours does not page anyone repeatedly.
    """
    cutoff = utcnow() - timedelta(minutes=offline_after_minutes)
    repository = DeviceRepository()
    offline = repository.offline_since(cutoff)

    alerted: list[str] = []
    seen: list[str] = []

    if notify and offline:
        service = NotificationService()
        admin_ids = _admin_user_ids()
        for device in offline:
            seen.append(device.serial_no)
            if _already_alerted(service, admin_ids, device.serial_no):
                continue
            last_seen = (
                device.last_seen_at.isoformat() if device.last_seen_at else "never"
            )
            for admin_id in admin_ids:
                service.enqueue(
                    event_type=EVENT_DEVICE_OFFLINE,
                    subject=f"Biometric device {device.serial_no} is offline",
                    body=(
                        f"Device {device.serial_no} at "
                        f"{device.location or 'an unspecified location'} has not "
                        f"called in for more than {offline_after_minutes} minutes. "
                        f"Last seen: {last_seen}."
                    ),
                    recipient_user_id=admin_id,
                    channel=Channel.IN_APP,
                    payload={
                        "device_id": device.id,
                        "serial_no": device.serial_no,
                        "last_seen_at": last_seen,
                    },
                )
            alerted.append(device.serial_no)
    else:
        seen = [device.serial_no for device in offline]

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    logger.info(
        "device_health offline=%s alerted=%s", len(seen), len(alerted)
    )
    return {
        "offline": seen,
        "offline_count": len(seen),
        "alerted": alerted,
        "alerted_count": len(alerted),
    }


def _admin_user_ids() -> list[int]:
    """Active ADMIN users, resolved through the auth repository."""
    from app.features.auth.constants import Role
    from app.features.auth.repository import UserRepository

    users = UserRepository().list_by_role(Role.ADMIN.value)
    return [user.id for user in users if user.is_active]


def _already_alerted(service: NotificationService, admin_ids: list[int], serial_no: str) -> bool:
    """Whether an unread alert for this device already exists for any admin."""
    for admin_id in admin_ids:
        for record in service.recent(user_id=admin_id, limit=50):
            if record.event_type != EVENT_DEVICE_OFFLINE:
                continue
            if not record.is_unread:
                continue
            payload = record.payload or {}
            if payload.get("serial_no") == serial_no:
                return True
    return False


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.device_health.check_devices")
    def check_devices() -> dict:
        return device_health_impl()


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
