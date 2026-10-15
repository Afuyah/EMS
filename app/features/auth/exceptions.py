"""Auth-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AuthenticationError, ConflictError, ValidationError


class AuthError(AuthenticationError):
    """Base class for auth feature failures."""

    error_code = "auth_error"
    message = "Authentication failed."


class InvalidCredentialsError(AuthError):
    """Email/password pair did not match."""

    error_code = "invalid_credentials"
    message = "Incorrect email or password."


class InactiveUserError(AuthError):
    """Account exists but has been deactivated."""

    status_code = 403
    error_code = "inactive_user"
    message = "This account has been deactivated."


class TokenRevokedError(AuthError):
    """Refresh token was already rotated, revoked, or is unknown."""

    error_code = "token_revoked"
    message = "This refresh token is no longer valid."


class TokenExpiredError(AuthError):
    """Refresh token passed its expiry."""

    error_code = "token_expired"
    message = "This refresh token has expired."


class EmailAlreadyRegisteredError(ConflictError):
    """Duplicate email on registration."""

    error_code = "email_already_registered"
    message = "That email address is already registered."


class WeakPasswordError(ValidationError):
    """Password policy failure."""

    error_code = "weak_password"
    message = "The password does not meet the minimum requirements."
