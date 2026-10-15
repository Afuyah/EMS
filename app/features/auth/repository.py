"""Auth repositories: the only place with SQL for users and refresh tokens."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.core.time import utcnow
from app.features.auth.models import RefreshToken, User


class UserRepository(BaseRepository[User]):
    """Queries over ``users``."""

    model = User

    def get_by_email(self, email: str) -> User | None:
        """Case-insensitive lookup used by login."""
        if not email:
            return None
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return db.session.execute(stmt).scalar_one_or_none()

    def email_exists(self, email: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(User.id).where(func.lower(User.email) == email.strip().lower())
        if excluding_id is not None:
            stmt = stmt.where(User.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def list_by_role(self, *roles: str):
        stmt = select(User).where(User.role.in_(list(roles))).order_by(User.id)
        return db.session.execute(stmt).scalars().all()

    def active_recipients(self):
        """Active users, used when broadcasting a notification."""
        stmt = select(User).where(User.is_active.is_(True)).order_by(User.id)
        return db.session.execute(stmt).scalars().all()

    def count_active(self) -> int:
        stmt = select(func.count()).select_from(User).where(User.is_active.is_(True))
        return int(db.session.execute(stmt).scalar_one() or 0)

    def count_by_role(self) -> list[dict]:
        """Role -> count, for dashboards."""
        stmt = (
            select(User.role.label("role"), func.count(User.id).label("total"))
            .group_by(User.role)
            .order_by(User.role)
        )
        return self.rows(stmt)


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    """Queries over ``refresh_tokens``."""

    model = RefreshToken

    def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Look up the session row for a presented refresh token."""
        stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        return db.session.execute(stmt).scalar_one_or_none()

    def get_by_session_id(self, session_id: str) -> RefreshToken | None:
        """Look up a session by the jti shared with its access tokens."""
        from app.core.security import hash_token

        return self.get_by_hash(hash_token(session_id))

    def get_by_id(self, token_id: int) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.id == token_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def create_token(
        self,
        *,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
    ) -> RefreshToken:
        return self.create(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )

    def revoke(self, record: RefreshToken, *, when: datetime | None = None) -> RefreshToken:
        """Idempotently stamp ``revoked_at``."""
        if record.revoked_at is None:
            record.revoked_at = when or utcnow()
            db.session.flush()
        return record

    def revoke_all_for_user(self, user_id: int) -> int:
        """Revoke every still-active token; returns how many were affected."""
        stmt = (
            update(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        result = db.session.execute(stmt)
        db.session.flush()
        return int(result.rowcount or 0)

    def active_for_user(self, user_id: int):
        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > utcnow(),
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return db.session.execute(stmt).scalars().all()

    def purge_expired(self, *, before: datetime | None = None) -> int:
        """Delete tokens that expired before ``before`` (default: now)."""
        cutoff = before or utcnow()
        stmt = select(RefreshToken).where(RefreshToken.expires_at < cutoff)
        records = db.session.execute(stmt).scalars().all()
        for record in records:
            db.session.delete(record)
        db.session.flush()
        return len(records)
