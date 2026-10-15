"""Notification repositories: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.core.time import utcnow
from app.features.notifications.constants import NotificationStatus


class NotificationRepository(BaseRepository["Notification"]):  # noqa: F821
    """Queries over ``notifications``."""

    def __init__(self, model=None) -> None:
        if model is None:
            from app.features.notifications.models import Notification

            model = Notification
        super().__init__(model)

    # ---- reads -----------------------------------------------------------
    def inbox(
        self,
        *,
        recipient_user_id: int,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list:
        stmt = select(self.model).where(
            self.model.recipient_user_id == recipient_user_id
        )
        if unread_only:
            stmt = stmt.where(self.model.status != NotificationStatus.READ.value)
        stmt = (
            stmt.order_by(self.model.created_at.desc(), self.model.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(db.session.execute(stmt).scalars().all())

    def unread_count(self, *, recipient_user_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.recipient_user_id == recipient_user_id,
                self.model.status != NotificationStatus.READ.value,
            )
        )
        return int(db.session.execute(stmt).scalar_one() or 0)

    def recent_for_user(self, *, recipient_user_id: int, limit: int = 8) -> list:
        stmt = (
            select(self.model)
            .where(self.model.recipient_user_id == recipient_user_id)
            .order_by(self.model.created_at.desc(), self.model.id.desc())
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    # ---- dispatcher ------------------------------------------------------
    def pending_batch(self, *, limit: int, max_attempts: int) -> list:
        """Rows the dispatcher should try to deliver, oldest first."""
        stmt = (
            select(self.model)
            .where(
                self.model.status == NotificationStatus.PENDING.value,
                self.model.attempts < max_attempts,
            )
            .order_by(self.model.created_at)
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    def mark_sent(self, record, *, when: datetime | None = None):
        record.status = NotificationStatus.SENT.value
        record.sent_at = when or utcnow()
        record.last_error = None
        db.session.flush()
        return record

    def mark_read(self, record, *, when: datetime | None = None):
        """Reading an in-app notification also marks it sent."""
        record.status = NotificationStatus.READ.value
        record.read_at = when or utcnow()
        if record.sent_at is None:
            record.sent_at = record.read_at
        db.session.flush()
        return record

    def mark_failed(self, record, *, error: str):
        record.status = NotificationStatus.FAILED.value
        record.last_error = (error or "")[:2000]
        db.session.flush()
        return record

    def bump_attempt(self, record):
        record.attempts = (record.attempts or 0) + 1
        db.session.flush()
        return record

    def mark_all_read(self, *, recipient_user_id: int) -> int:
        stmt = (
            update(self.model)
            .where(
                self.model.recipient_user_id == recipient_user_id,
                self.model.status != NotificationStatus.READ.value,
            )
            .values(status=NotificationStatus.READ.value, read_at=utcnow())
        )
        result = db.session.execute(stmt)
        db.session.flush()
        return int(result.rowcount or 0)

    def count_by_status(self) -> list[dict]:
        stmt = select(
            self.model.status.label("status"),
            func.count(self.model.id).label("total"),
        ).group_by(self.model.status)
        return self.rows(stmt)

    def count_by_channel(self) -> list[dict]:
        stmt = select(
            self.model.channel.label("channel"),
            func.count(self.model.id).label("total"),
        ).group_by(self.model.channel)
        return self.rows(stmt)
