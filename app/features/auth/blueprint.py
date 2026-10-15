"""Auth HTTP routes for the JSON API."""

from __future__ import annotations

from flask import Blueprint, request
from flask_jwt_extended import jwt_required

from app.core.permissions import require_roles
from app.features.auth.constants import Role
from app.features.auth.controller import AuthController

auth_api = Blueprint("auth_api", __name__, url_prefix="/auth")

_controller = AuthController()


def _limiter():
    """Resolve the shared limiter lazily to avoid an import cycle."""
    from app import limiter

    return limiter


@auth_api.post("/login")
@_limiter().limit("20 per minute")
def login():
    """Exchange credentials for an access/refresh token pair."""
    return _controller.login_response(request.get_json(silent=True) or {})


@auth_api.post("/refresh")
@jwt_required(refresh=True)
def refresh():
    """Rotate the refresh token and issue a new pair."""
    payload = request.get_json(silent=True) or {}
    return _controller.refresh(presented_token=payload.get("refresh_token"))


@auth_api.post("/logout")
@jwt_required(refresh=True)
def logout():
    """Revoke the presented refresh token."""
    return _controller.logout(all_sessions=False)


@auth_api.post("/logout_all")
@jwt_required(refresh=True)
def logout_all():
    """Revoke every session belonging to the caller."""
    return _controller.logout(all_sessions=True)


@auth_api.get("/me")
@jwt_required()
def me():
    """The authenticated caller's profile."""
    return _controller.me()


@auth_api.post("/users")
@jwt_required()
def create_user():
    """Create a login account (Admin only)."""
    require_roles(Role.ADMIN)
    return _controller.register(request.get_json(silent=True) or {})


@auth_api.post("/users/<int:user_id>/active/<any(true,false):state>")
@jwt_required()
def set_user_active(user_id: int, state: str):
    """Activate or deactivate a login (Admin only)."""
    require_roles(Role.ADMIN)
    return _controller.set_active(user_id, state == "true")


@auth_api.post("/users/<int:user_id>/password")
@jwt_required()
def reset_user_password(user_id: int):
    """Reset another user's password (Admin only)."""
    require_roles(Role.ADMIN)
    return _controller.reset_password(user_id, request.get_json(silent=True) or {})
