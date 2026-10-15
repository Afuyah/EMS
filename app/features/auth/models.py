"""Auth tables: users and refresh_tokens.

Data containers only — no business rules, no queries.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampMixin, db
from app.features.auth.constants import Role


class User(TimestampMixin, db.Model):
    """An authenticated principal."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=Role.EMPLOYEE.value, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Set when the password was issued by someone else (HR provisioning or an
    #: emailed temporary password).  The user is pushed to change it before
    #: using the system, which is what makes emailing a password acceptable.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    employee: Mapped["Employee | None"] = relationship(  # noqa: F821
        back_populates="user",
        uselist=False,
    )

    # ---- convenience (no queries) ---------------------------------------
    @property
    def role_enum(self) -> Role:
        return Role.coerce(self.role)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    @property
    def is_hr(self) -> bool:
        return self.role == Role.HR.value

    @property
    def display_name(self) -> str:
        if self.employee is not None:
            return self.employee.full_name
        return self.email.split("@")[0]

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User {self.id} {self.email} {self.role}>"


class RefreshToken(db.Model):
    """A server-side refresh token record enabling rotation and revocation."""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        state = "revoked" if self.revoked_at else "active"
        return f"<RefreshToken {self.id} user={self.user_id} {state}>"
