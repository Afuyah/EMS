"""JWT callbacks for the auth feature.

The refresh-token lookup has to consult ``refresh_tokens``, so these callbacks
live with the auth feature rather than in ``core`` (which must not import
feature models).  ``app.create_app`` calls :func:`register_jwt_callbacks` after
it has constructed the application's ``JWTManager``.
"""

from __future__ import annotations

from datetime import timedelta

from flask import current_app, jsonify
from flask_jwt_extended import (
    create_access_token,
    create_refresh_token,
    get_jwt,
)

from app.core.logging import get_logger
from app.core.security import hash_token
from app.core.time import utcnow
from app.features.auth.constants import (
    CLAIM_ACTIVE,
    CLAIM_DEPARTMENT_ID,
    CLAIM_EMAIL,
    CLAIM_EMPLOYEE_ID,
    CLAIM_ROLE,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Claim construction
# ---------------------------------------------------------------------------
def build_access_claims(user, employee=None, *, extra: dict | None = None) -> dict:
    """Additional claims embedded in access and refresh tokens."""
    claims = {
        CLAIM_ROLE: user.role,
        CLAIM_EMAIL: user.email,
        CLAIM_ACTIVE: bool(user.is_active),
        CLAIM_EMPLOYEE_ID: getattr(employee, "id", None),
        CLAIM_DEPARTMENT_ID: getattr(employee, "department_id", None),
    }
    if extra:
        claims.update(extra)
    return claims


def access_expires_seconds() -> int:
    expires = current_app.config.get("JWT_ACCESS_TOKEN_EXPIRES")
    if isinstance(expires, timedelta):
        return int(expires.total_seconds())
    return 900


def issue_tokens(user, employee=None, *, token_id: str | None = None) -> dict:
    """Mint an access/refresh pair.

    Both tokens share ``token_id`` (the refresh row's jti) so that revoking the
    refresh row also invalidates the access tokens minted with it.
    """
    identity = str(user.id)
    claims = build_access_claims(user, employee)
    if token_id is not None:
        claims["iid"] = token_id

    access_token = create_access_token(identity=identity, additional_claims=claims)
    refresh_token = create_refresh_token(identity=identity, additional_claims=claims)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": access_expires_seconds(),
    }


def attach_auth_cookies(response, tokens: dict):
    """Set both HTTP-only cookies on an outgoing response."""
    from flask_jwt_extended import set_access_cookies, set_refresh_cookies

    set_access_cookies(response, tokens["access_token"])
    set_refresh_cookies(response, tokens["refresh_token"])
    return response


def clear_auth_cookies(response):
    """Expire the auth cookies."""
    from flask_jwt_extended import unset_jwt_cookies

    unset_jwt_cookies(response)
    return response


# ---------------------------------------------------------------------------
# Flask-JWT-Extended hooks
# ---------------------------------------------------------------------------
def register_jwt_callbacks(app=None) -> None:
    """Wire user lookup, revocation and error shapes onto the app's JWTManager.

    ``app`` must be supplied when called during factory setup (where no
    application context exists yet).  The user-lookup callback deliberately
    re-reads the app from ``current_app`` at request time.
    """
    from flask import current_app

    target = app if app is not None else current_app
    jwt = target.extensions.get("ems_jwt_manager")
    if jwt is None:  # pragma: no cover - factory always installs it
        raise RuntimeError(
            "The JWT manager is missing; app._init_extensions must run first."
        )

    @jwt.user_lookup_loader
    def _load_user(_jwt_header, jwt_data):
        from app.features.auth.repository import UserRepository

        identity = jwt_data.get("sub")
        if identity is None:
            return None
        try:
            return UserRepository().get(int(identity))
        except (TypeError, ValueError):
            return None

    @jwt.token_in_blocklist_loader
    def _is_revoked(_jwt_header, jwt_data):
        """Reject any token whose session has been revoked.

        Access and refresh tokens carry the same ``iid`` (the refresh row's
        jti), so rotating or revoking a refresh row immediately invalidates the
        access tokens minted alongside it.
        """
        from app.features.auth.repository import RefreshTokenRepository

        session_id = jwt_data.get("iid")
        if session_id is None:
            # Tokens issued without a session id cannot be revoked.
            return False

        record = RefreshTokenRepository().get_by_session_id(str(session_id))
        if record is None:
            return True
        if record.revoked_at is not None:
            return True
        return record.expires_at <= utcnow()

    @jwt.revoked_token_loader
    def _revoked(_jwt_header, _jwt_data):
        return (
            jsonify(
                {
                    "error": "token_revoked",
                    "message": "This token is no longer valid.",
                }
            ),
            401,
        )

    @jwt.expired_token_loader
    def _expired(_jwt_header, _jwt_data):
        return (
            jsonify({"error": "token_expired", "message": "This token has expired."}),
            401,
        )

    @jwt.invalid_token_loader
    def _invalid(reason):
        return jsonify({"error": "invalid_token", "message": str(reason)}), 401

    @jwt.unauthorized_loader
    def _unauthorized(reason):
        return jsonify({"error": "authorization_required", "message": str(reason)}), 401

    logger.debug("JWT callbacks registered")
