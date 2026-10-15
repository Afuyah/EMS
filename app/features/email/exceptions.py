"""Email-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, ValidationError


class EmailError(AppError):
    """Base class for email feature failures."""

    status_code = 500
    error_code = "email_error"
    message = "The email could not be processed."


class EmailDeliveryError(EmailError):
    """The transport rejected the message."""

    status_code = 502
    error_code = "email_delivery_failed"
    message = "The email could not be delivered."


class EmailBackendError(EmailError, ValidationError):
    """The configured backend is unknown or unusable."""

    error_code = "email_backend_invalid"
    message = "The configured email backend is not usable."


class MissingRecipientError(EmailError, ValidationError):
    """Nothing to send to, or an address that cannot be used."""

    error_code = "email_recipient_missing"
    message = "No valid recipient address was supplied."
