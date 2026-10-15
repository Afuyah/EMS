"""Email repository: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.core.time import utcnow
from app.features.email.constants import EmailStatus
from app.features.email.models import EmailLog


class EmailRepository(BaseRepository[EmailLog]):
    """Queries over ``email_logs``."""

    model = EmailLog

    # ---- reads -----------------------------------------------------------
    def recent(self, *, limit: int = 100) -> list[EmailLog]:
        stmt = (
            select(EmailLog)
            .order_by(EmailLog.created_at.desc(), EmailLog.id.desc())
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    def for_recipient(self, email: str, *, limit: int = 50) -> list[EmailLog]:
        stmt = (
            select(EmailLog)
            .where(func.lower(EmailLog.to_email) == (email or "").strip().lower())
            .order_by(EmailLog.created_at.desc())
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    def pending(
        self, *, limit: int, max_attempts: int, older_than: datetime | None = None
    ) -> list[EmailLog]:
        """Rows still due for delivery, oldest first."""
        stmt = (
            select(EmailLog)
            .where(
                EmailLog.status == EmailStatus.PENDING.value,
                EmailLog.attempts < max_attempts,
            )
            .order_by(EmailLog.created_at)
            .limit(limit)
        )
        if older_than is not None:
            stmt = stmt.where(EmailLog.created_at <= older_than)
        return list(db.session.execute(stmt).scalars().all())

    def counts(self) -> dict[str, int]:
        """Status -> count."""
        stmt = select(
            EmailLog.status.label("status"),
            func.count(EmailLog.id).label("total"),
        ).group_by(EmailLog.status)
        return {row["status"]: int(row["total"] or 0) for row in self.rows(stmt)}

    def count_by_event(self, *, limit: int = 10) -> list[dict]:
        stmt = (
            select(
                EmailLog.event_type.label("event_type"),
                func.count(EmailLog.id).label("total"),
            )
            .group_by(EmailLog.event_type)
            .order_by(func.count(EmailLog.id).desc())
            .limit(limit)
        )
        return self.rows(stmt)

    # ---- writes ----------------------------------------------------------
    def mark_sent(self, record: EmailLog, *, backend: str) -> EmailLog:
        record.status = EmailStatus.SENT.value
        record.sent_at = utcnow()
        record.backend = backend
        record.last_error = None
        db.session.flush()
        return record

    def mark_failed(self, record: EmailLog, *, error: str, backend: str) -> EmailLog:
        record.status = EmailStatus.FAILED.value
        record.last_error = (error or "")[:2000]
        record.backend = backend
        db.session.flush()
        return record

    def bump_attempt(self, record: EmailLog) -> EmailLog:
        record.attempts = (record.attempts or 0) + 1
        db.session.flush()
        return record
