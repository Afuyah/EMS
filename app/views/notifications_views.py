"""Notification views: inbox and the polling bell fragment."""

from __future__ import annotations

from flask import redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.notifications.service import NotificationService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    principal,
    render,
    rollback_session,
    view_login_required,
    wants_partial,
)

logger = get_logger(__name__)

_notifications = NotificationService()


@views_bp.get("/notifications")
@view_login_required()
def notifications():
    """The caller's notification inbox."""
    user = principal()
    unread_only = request.args.get("unread") == "1"
    limit = min(request.args.get("limit", type=int) or 50, 200)

    records = _notifications.inbox(
        user_id=user.user_id, unread_only=unread_only, limit=limit
    )
    return render(
        "notifications/list.html",
        active_nav="Notifications",
        notifications=records,
        unread=_notifications.unread_count(user_id=user.user_id),
        unread_only=unread_only,
        status_counts=_notifications.status_counts(),
        channel_counts=_notifications.channel_counts(),
    )


@views_bp.get("/notifications/bell")
@view_login_required()
def notification_bell():
    """HTMX fragment polled by the nav bell."""
    user = principal()
    unread = _notifications.unread_count(user_id=user.user_id)
    recent = _notifications.recent(user_id=user.user_id, limit=6)
    return render(
        "notifications/_bell.html",
        unread=unread,
        recent=recent,
    )


@views_bp.post("/notifications/<int:notification_id>/read")
@view_login_required()
def notification_read(notification_id: int):
    """Mark one notification read."""
    user = principal()
    try:
        _notifications.mark_read(
            notification_id=notification_id, user_id=user.user_id
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("mark read failed")
        flash_error("The notification could not be updated.")

    if wants_partial():
        records = _notifications.inbox(user_id=user.user_id, limit=50)
        return render(
            "notifications/_list.html",
            notifications=records,
            unread=_notifications.unread_count(user_id=user.user_id),
        )
    return redirect(url_for("views.notifications"))


@views_bp.post("/notifications/read_all")
@view_login_required()
def notifications_read_all():
    """Mark every notification read."""
    user = principal()
    try:
        count = _notifications.mark_all_read(user_id=user.user_id)
        commit_session()
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("mark all read failed")
        flash_error("Notifications could not be updated.")
        return redirect(url_for("views.notifications"))

    flash_success(f"Marked {count} notification(s) as read.")
    return redirect(url_for("views.notifications"))
