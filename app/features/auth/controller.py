"""Auth controller: use-case orchestration and the transaction boundary.

Controllers commit on success and roll back on failure.  They return
response-shaped dicts; blueprints turn those into HTTP.
"""

from __future__ import annotations

from datetime import timedelta

from flask_jwt_extended import create_access_token, create_refresh_token, get_jwt
from sqlalchemy.exc import IntegrityError

from app.core.database import db
from app.core.logging import get_logger
from app.core.security import hash_token
from app.core.time import utcnow
from app.features.auth.constants import Role
from app.features.auth.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    TokenRevokedError,
)
from app.features.auth.jwt_callbacks import attach_auth_cookies, issue_tokens, clear_auth_cookies
from app.features.auth.schemas import load_or_raise, login_schema
from app.features.auth.service import AuthService

logger = get_logger(__name__)


class AuthController:
    """Coordinates auth use-cases for both the JSON API and the HTML views."""

    def __init__(self, service: AuthService | None = None) -> None:
        self.service = service or AuthService()

    # ---- login -----------------------------------------------------------
    def login(self, payload: dict, *, set_cookies: bool = True):
        """Authenticate and open a session.

        Returns ``(body, tokens)``.  The caller attaches cookies, because only
        the blueprint layer knows whether it is answering JSON or HTML.
        """
        data = load_or_raise(login_schema, payload)
        try:
            user, tokens = self.service.login(
                email=data["email"], password=data["password"]
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        logger.info("login ok user=%s role=%s", user.id, user.role)
        body = {
            "user": self.service.load_profile(user.id),
            "tokens": tokens,
        }
        return body, tokens

    def login_response(self, payload: dict):
        """``POST /auth/login`` returning a ready-to-send ``(response, status)``."""
        from app.core.responses import ok

        body, tokens = self.login(payload)
        response, status = ok(body)
        attach_auth_cookies(response, tokens)
        return response, status

    # ---- refresh ---------------------------------------------------------
    def refresh(self, *, presented_token: str | None = None):
        """Rotate the refresh token, returning a new pair.

        The token may arrive in the JSON body, as a bearer header, or as the
        HTTP-only cookie, so both the browser and API clients work unchanged.
        """
        from flask_jwt_extended import get_jwt_identity

        from app.core.responses import ok

        token = presented_token or _bearer_refresh_token() or self._token_from_cookie()
        if not token:
            raise TokenRevokedError("No refresh token was supplied.")

        identity = get_jwt_identity()
        if identity is None:
            raise TokenRevokedError("No refresh token was supplied.")

        try:
            user, tokens = self.service.rotate(
                user_id=int(identity), presented_token=token
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        body = {"user": self.service.load_profile(user.id), "tokens": tokens}
        response, status = ok(body)
        attach_auth_cookies(response, tokens)
        return response, status

    @staticmethod
    def _token_from_cookie() -> str | None:
        from flask import current_app, request

        name = current_app.config.get("JWT_REFRESH_COOKIE_NAME", "refresh_token_cookie")
        return request.cookies.get(name)

    # ---- logout ----------------------------------------------------------
    def logout(self, *, all_sessions: bool = False):
        """Revoke this session, or every session for the user."""
        from app.core.responses import ok

        identity = get_jwt_identity_safe()
        token = self._token_from_cookie() or _bearer_refresh_token()

        try:
            if all_sessions and identity is not None:
                revoked = self.service.logout_all(int(identity))
            else:
                revoked = 1 if self.service.logout(presented_token=token) else 0
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        response, status = ok({"revoked": revoked})
        clear_auth_cookies(response)
        return response, status

    # ---- read ------------------------------------------------------------
    def me(self):
        """``GET /auth/me``."""
        from flask_jwt_extended import get_jwt_identity

        from app.core.exceptions import AuthenticationError
        from app.core.responses import ok

        identity = get_jwt_identity()
        if identity is None:
            raise AuthenticationError()
        profile = self.service.load_profile(int(identity))
        return ok(profile)

    # ---- user administration --------------------------------------------
    def register(self, payload: dict):
        """Create a login (Admin only, enforced at the blueprint)."""
        from app.core.responses import created
        from app.features.auth.schemas import register_schema

        data = load_or_raise(register_schema, payload)
        try:
            user = self.service.create_user(
                email=data["email"],
                password=data["password"],
                role=data["role"],
                is_active=data.get("is_active", True),
            )
            db.session.commit()
        except IntegrityError as exc:
            db.session.rollback()
            raise EmailAlreadyRegisteredError() from exc
        except Exception:
            db.session.rollback()
            raise

        return created(self.service.load_profile(user.id))

    def set_active(self, user_id: int, is_active: bool):
        """Activate/deactivate a login (Admin only)."""
        from app.core.exceptions import NotFoundError
        from app.core.responses import ok

        user = self.service.get_user(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} was not found.")
        try:
            self.service.set_active(user, is_active)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(self.service.load_profile(user_id))

    def reset_password(self, user_id: int, payload: dict):
        """Reset another user's password (Admin only)."""
        from app.core.exceptions import NotFoundError
        from app.core.responses import ok
        from app.features.auth.schemas import change_password_schema

        data = load_or_raise(change_password_schema, payload)
        user = self.service.get_user(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} was not found.")
        try:
            self.service.set_password(user, data["password"])
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok({"updated": True})


def get_jwt_identity_safe() -> int | None:
    """``get_jwt_identity`` that returns ``None`` instead of raising."""
    try:
        from flask_jwt_extended import get_jwt_identity

        identity = get_jwt_identity()
        return int(identity) if identity is not None else None
    except Exception:  # noqa: BLE001 - no token / no context
        return None


def _bearer_refresh_token() -> str | None:
    """Allow API clients to present the refresh token as a bearer token.

    The HTTP-only cookie is preferred for browsers; this covers the documented
    "header + cookie" delivery for JSON clients.
    """
    from flask import request

    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip() or None
