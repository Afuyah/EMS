"""Email retry / outbox drain job.

Runs alongside the notification dispatcher.  Any email left ``pending`` — because
SMTP was briefly unavailable, the process restarted mid-send, or the transport
timed out — is re-attempted until it succeeds or exhausts its attempts.
"""

from __future__ import annotations

from app.core.database import db
from app.core.logging import get_logger
from app.features.email.service import EmailService

logger = get_logger(__name__)


def email_dispatch_impl(*, limit: int = 50, min_age_seconds: int = 30) -> dict:
    """Drain the pending outbox.  Pure entry point, callable from the CLI."""
    service = EmailService()
    try:
        summary = service.retry_pending(limit=limit, min_age_seconds=min_age_seconds)
    except Exception:
        db.session.rollback()
        raise
    summary["backend"] = service.backend
    summary["enabled"] = service.enabled
    return summary


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.email_dispatch.retry_pending")
    def retry_pending(limit: int = 50, min_age_seconds: int = 30) -> dict:
        return email_dispatch_impl(limit=limit, min_age_seconds=min_age_seconds)


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
