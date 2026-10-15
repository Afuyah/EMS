"""Outbound email table.

One row per message attempt, so a delivery failure is auditable rather than
lost in a log.  Data container only — no business rules, no queries.
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
from app.features.email.constants import EmailStatus


class EmailLog(db.Model):
    """A single outbound email and its delivery state."""

    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    cc_email: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)

    # Optional links back to the domain object that caused the email.
    recipient_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    recipient_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), index=True
    )
    payload: Mapped[dict | None] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EmailStatus.PENDING.value, index=True
    )
    backend: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    recipient_user: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[recipient_user_id]
    )
    recipient_employee: Mapped["Employee | None"] = relationship(  # noqa: F821
        foreign_keys=[recipient_employee_id]
    )

    # ---- derived (no queries) -------------------------------------------
    @property
    def status_enum(self) -> EmailStatus:
        return EmailStatus.coerce(self.status)

    @property
    def is_sent(self) -> bool:
        return self.status == EmailStatus.SENT.value

    @property
    def is_failed(self) -> bool:
        return self.status == EmailStatus.FAILED.value

    @property
    def status_tailwind(self) -> str:
        return {
            EmailStatus.SENT.value: "bg-emerald-100 text-emerald-700 ring-emerald-600/20",
            EmailStatus.PENDING.value: "bg-amber-100 text-amber-700 ring-amber-600/20",
            EmailStatus.FAILED.value: "bg-rose-100 text-rose-700 ring-rose-600/20",
        }.get(self.status, "bg-slate-100 text-slate-600 ring-slate-500/20")

    @property
    def retryable(self) -> bool:
        return self.status == EmailStatus.PENDING.value

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<EmailLog {self.id} {self.event_type} to={self.to_email} "
            f"{self.status} attempts={self.attempts}>"
        )
