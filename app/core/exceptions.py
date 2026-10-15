"""Domain exception hierarchy.

Handlers live in :mod:`app.core.error_handlers` and :mod:`app.core.responses`.
Never raise raw ``HTTPException`` from services or controllers.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every expected, handled failure."""

    status_code: int = 500
    error_code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Any = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message
        if error_code:
            self.error_code = error_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": self.error_code,
            "message": self.message,
        }
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ValidationError(AppError):
    """Request payload or parameters failed validation."""

    status_code = 400
    error_code = "validation_error"
    message = "The submitted data is invalid."


class AuthenticationError(AppError):
    """Credentials are missing, invalid or expired."""

    status_code = 401
    error_code = "authentication_error"
    message = "Authentication is required."


class AuthorizationError(AppError):
    """Authenticated, but the role/permission is insufficient."""

    status_code = 403
    error_code = "authorization_error"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    """Requested resource does not exist."""

    status_code = 404
    error_code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    """Uniqueness or state conflict (e.g. duplicate email)."""

    status_code = 409
    error_code = "conflict"
    message = "The resource conflicts with existing data."


class RateLimitError(AppError):
    """Too many requests."""

    status_code = 429
    error_code = "rate_limited"
    message = "Too many requests."


class DeviceAuthError(AuthenticationError):
    """Biometric device authentication failed (bad serial or secret)."""

    error_code = "device_auth_failed"
    message = "Device authentication failed."


class RuleViolationError(ConflictError):
    """A configured leave or disciplinary rule blocks the operation."""

    error_code = "rule_violation"
    message = "A configured rule blocks this operation."


class DenialLimitReached(RuleViolationError):
    """Employee already used their single denial for this request.

    Enforced in :mod:`app.features.leave.state_machine`, not in the database.
    """

    error_code = "denial_limit_reached"
    message = "Your one denial has been used; the current window was force-approved."
