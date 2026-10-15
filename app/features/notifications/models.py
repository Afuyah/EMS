"""Notification table.

Data container only — no business rules, no queries.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import db
from app.features.notifications.constants import (
    DEFAULT_CHANNEL,
    NotificationStatus,
)


class Notification(db.Model):
    """A queued or delivered message for one recipient."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recipient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    recipient_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_CHANNEL, index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NotificationStatus.PENDING.value, index=True
    )
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    recipient_user: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[recipient_user_id]
    )
    recipient_employee: Mapped["Employee | None"] = relationship(  # noqa: F821
        back_populates="notifications"
    )

    # ---- derived (no queries) -------------------------------------------
    @property
    def status_enum(self) -> NotificationStatus:
        return NotificationStatus.coerce(self.status)

    @property
    def is_unread(self) -> bool:
        return self.status != NotificationStatus.READ.value

    @property
    def is_delivered(self) -> bool:
        return self.status in {
            NotificationStatus.SENT.value,
            NotificationStatus.READ.value,
        }

    @property
    def icon(self) -> str:
        """Emoji-free icon slug the templates map to an inline SVG."""
        return {
            "leave_requested": "inbox",
            "leave_suggested": "sparkles",
            "leave_approved": "check",
            "leave_denied": "x",
            "leave_forced": "shield",
            "leave_cancelled": "slash",
            "attendance_absent": "user-x",
            "attendance_late": "clock",
            "color_escalated": "trending-up",
            "disciplinary_action": "gavel",
            "device_offline": "plug",
            "welcome": "sparkles",
        }.get(self.event_type, "bell")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<Notification {self.id} user={self.recipient_user_id} "
            f"{self.event_type}/{self.channel} {self.status}>"
        )
