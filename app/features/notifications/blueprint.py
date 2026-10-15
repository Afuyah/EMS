"""Notification routes for the JSON API."""

from __future__ import annotations

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app.core.database import db
from app.core.exceptions import AuthenticationError
from app.core.responses import ok
from app.features.notifications.repository import NotificationRepository
from app.features.notifications.schemas import (
    notification_create_schema,
    notifications_schema,
)
from app.features.notifications.service import NotificationService

notifications_api = Blueprint("notifications_api", __name__, url_prefix="/notifications")

_service = NotificationService(NotificationRepository())


def _current_user_id() -> int:
    identity = get_jwt_identity()
    if identity is None:
        raise AuthenticationError()
    return int(identity)


@notifications_api.get("")
@jwt_required()
def list_notifications():
    """The caller's inbox, newest first."""
    unread_only = request.args.get("unread_only", "").lower() in {"1", "true", "yes"}
    limit = min(request.args.get("limit", type=int) or 50, 200)
    offset = max(request.args.get("offset", type=int) or 0, 0)

    records = _service.inbox(
        user_id=_current_user_id(), unread_only=unread_only, limit=limit, offset=offset
    )
    return ok(
        notifications_schema.dump(records),
        meta={
            "unread_count": _service.unread_count(user_id=_current_user_id()),
            "limit": limit,
            "offset": offset,
        },
    )


@notifications_api.get("/unread_count")
@jwt_required()
def unread_count():
    """Badge count for the notification bell."""
    return ok({"unread": _service.unread_count(user_id=_current_user_id())})


@notifications_api.post("/<int:notification_id>/read")
@jwt_required()
def mark_read(notification_id: int):
    """Mark one notification read."""
    try:
        record = _service.mark_read(
            notification_id=notification_id, user_id=_current_user_id()
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    from app.features.notifications.schemas import notification_schema

    return ok(notification_schema.dump(record))


@notifications_api.post("/read_all")
@jwt_required()
def mark_all_read():
    """Mark every notification read for the caller."""
    try:
        count = _service.mark_all_read(user_id=_current_user_id())
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return ok({"marked_read": count})


@notifications_api.post("")
@jwt_required()
def create_notification():
    """Queue a notification directly (Admin/testing seam)."""
    from app.core.permissions import require_roles
    from app.features.auth.constants import Role
    from app.features.notifications.schemas import load_or_raise

    require_roles(Role.ADMIN)
    data = load_or_raise(notification_create_schema, request.get_json(silent=True) or {})
    try:
        record = _service.enqueue(**data)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    from app.core.responses import created
    from app.features.notifications.schemas import notification_schema

    return created(notification_schema.dump(record))
