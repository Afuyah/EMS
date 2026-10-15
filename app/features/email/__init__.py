"""Outbound email feature: queue, render, deliver, retry."""

from app.features.email.constants import (  # noqa: F401
    EmailBackend,
    EmailStatus,
)
from app.features.email.exceptions import (  # noqa: F401
    EmailBackendError,
    EmailDeliveryError,
    EmailError,
    MissingRecipientError,
)
from app.features.email.service import EmailService, render_email  # noqa: F401

__all__ = [
    "EmailBackend",
    "EmailStatus",
    "EmailError",
    "EmailDeliveryError",
    "EmailBackendError",
    "MissingRecipientError",
    "EmailService",
    "render_email",
]
