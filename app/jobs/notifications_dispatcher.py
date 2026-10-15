"""Notification dispatcher job.

Runs every minute.  Flushes the queue by attempting delivery of each pending
row; one failure never aborts the batch.
"""

from __future__ import annotations

from app.core.database import db
from app.core.logging import get_logger
from app.features.notifications.service import NotificationService

logger = get_logger(__name__)


def notifications_dispatcher_impl(*, limit: int = 100) -> dict:
    """Pure entry point: dispatch pending notifications and commit.

    Returns a summary dict so the CLI can print something useful.
    """
    service = NotificationService()
    try:
        summary = service.dispatch_pending(limit=limit)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return summary


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.notifications_dispatcher.dispatch_pending")
    def dispatch_pending(limit: int = 100) -> dict:
        return notifications_dispatcher_impl(limit=limit)


# Import-time registration for ``celery_worker``'s ``include`` list.
try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
