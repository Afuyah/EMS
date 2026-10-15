"""Notifications-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, NotFoundError, ValidationError


class NotificationError(AppError):
    """Base class for notifications feature failures."""

    status_code = 400
    error_code = "notification_error"
    message = "The notification operation could not be completed."


class NotificationNotFoundError(NotFoundError):
    error_code = "notification_not_found"
    message = "Notification not found."


class SenderNotConfiguredError(ValidationError):
    error_code = "sender_not_configured"
    message = "No sender is configured for that channel."


class DeliveryFailedError(NotificationError):
    status_code = 502
    error_code = "delivery_failed"
    message = "The notification could not be delivered."
