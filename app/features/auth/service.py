"""Auth service: login, refresh rotation, logout.

Stateless with respect to HTTP.  It flushes but never commits — controllers,
views and jobs own the transaction boundary.

Cross-feature note: this feature is imported *by* others (they need the current
user), so it deliberately does not import their services at module scope.
"""

from __future__ import annotations

from datetime import timedelta

from flask import current_app
from flask_jwt_extended import decode_token, get_jwt

from app.core.database import db
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.security import (
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)
from app.core.time import utcnow
from app.features.auth.constants import CLAIM_ROLE, Role
from app.features.auth.exceptions import (
    EmailAlreadyRegisteredError,
    InactiveUserError,
    InvalidCredentialsError,
    TokenExpiredError,
    TokenRevokedError,
    WeakPasswordError,
)
from app.features.auth.jwt_callbacks import build_access_claims
from app.features.auth.models import RefreshToken, User
from app.features.auth.repository import RefreshTokenRepository, UserRepository

logger = get_logger(__name__)

MIN_PASSWORD_LENGTH = 8


class AuthService:
    """Use-cases around identity and session lifecycle."""

    def __init__(
        self,
        users: UserRepository | None = None,
        tokens: RefreshTokenRepository | None = None,
    ) -> None:
        self.users = users or UserRepository()
        self.tokens = tokens or RefreshTokenRepository()

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _refresh_ttl() -> timedelta:
        configured = current_app.config.get("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=30))
        if isinstance(configured, timedelta):
            return configured
        return timedelta(days=30)

    @staticmethod
    def validate_password_strength(password: str) -> None:
        """Minimal policy: length plus a letter/digit mix."""
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise WeakPasswordError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
            )
        if password.isalpha() or password.isdigit():
            raise WeakPasswordError(
                "Password must contain both letters and numbers."
            )

    def _mint(self, user: User, employee=None) -> tuple[dict, str]:
        """Create a JWT pair plus the session id they share.

        Both tokens carry the same ``iid`` (a fresh session id) and the refresh
        row is keyed by that id, so revoking the row invalidates the access
        token minted alongside it.
        """
        import uuid

        from app.features.auth.jwt_callbacks import issue_tokens

        # ``user.id`` is only assigned after a flush, so make sure it exists
        # before it is embedded as the token subject.
        if user.id is None:
            db.session.flush()

        session_id = str(uuid.uuid4())
        tokens = issue_tokens(user, employee, token_id=session_id)
        return tokens, session_id

    def persist_refresh(self, user_id: int, session_id: str) -> RefreshToken:
        """Store the session row that makes a refresh token revocable."""
        record = self.tokens.create_token(
            user_id=user_id,
            token_hash=hash_token(session_id),
            expires_at=utcnow() + self._refresh_ttl(),
        )
        return record

    # ---- user management -------------------------------------------------
    def get_user(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    def get_user_or_none(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.users.get_by_email(email)

    def list_users_by_role(self, role: str | Role) -> list[User]:
        """Active and inactive users holding ``role``.

        Used to resolve who should receive organisational email; callers filter
        on ``is_active`` so a deactivated admin is not mailed.
        """
        value = role.value if isinstance(role, Role) else str(role)
        return list(self.users.list_by_role(value))

    def managers_for_department(self, department_id: int) -> list[User]:
        """Managers whose employee record sits in ``department_id``.

        Cross-feature read through the employees repository, which this feature
        is allowed to call at the service layer.
        """
        from app.features.employees.repository import EmployeeRepository

        employees = EmployeeRepository().search(
            department_id=department_id, is_active=True
        )
        managers: list[User] = []
        for employee in employees:
            if employee.user_id is None:
                continue
            user = self.users.get(employee.user_id)
            if user is not None and user.role == Role.MANAGER.value and user.is_active:
                managers.append(user)
        return managers

    def load_profile(self, user_id: int) -> dict:
        """Identity payload returned by ``GET /auth/me``."""
        user = self.users.get(user_id)
        if user is None:
            raise InvalidCredentialsError("Account no longer exists.")
        employee = user.employee
        return {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            # Surfaced so the UI (and API clients) can push the user to choose
            # their own password when it was issued on their behalf.
            "must_change_password": bool(user.must_change_password),
            "employee": None
            if employee is None
            else {
                "id": employee.id,
                "employee_no": employee.employee_no,
                "full_name": employee.full_name,
                "department_id": employee.department_id,
                "department": employee.department.name if employee.department else None,
                "position": employee.position.title if employee.position else None,
                "tz": employee.tz,
                "current_color": employee.current_color,
                "is_active": employee.is_active,
            },
        }

    def create_user(
        self,
        *,
        email: str,
        password: str,
        role: str | Role = Role.EMPLOYEE,
        is_active: bool = True,
        commit: bool = False,
        must_change_password: bool = False,
    ) -> User:
        """Register a new login.

        ``commit`` defaults to ``False``: the caller owns the transaction.  It
        exists because seeding runs outside a request and would otherwise have
        to know about the session.

        Set ``must_change_password`` when the password was chosen by someone
        else (HR provisioning), so the user is pushed to replace it.
        """
        normalised = (email or "").strip().lower()
        if not normalised or "@" not in normalised:
            raise ValidationError("A valid email address is required.")
        if self.users.email_exists(normalised):
            raise EmailAlreadyRegisteredError(
                f"{normalised} is already registered."
            )
        self.validate_password_strength(password)

        role_value = role.value if isinstance(role, Role) else str(role).upper()
        if role_value not in Role.values():
            raise ValidationError(f"Unknown role {role!r}.")

        user = self.users.create(
            email=normalised,
            password_hash=hash_password(password),
            role=role_value,
            is_active=is_active,
            must_change_password=must_change_password,
        )
        logger.info(
            "created user id=%s email=%s role=%s must_change=%s",
            user.id, user.email, user.role, must_change_password,
        )
        return user

    def set_password(self, user: User, new_password: str) -> User:
        """Admin-initiated password reset; revokes existing sessions.

        The user is flagged to choose their own password on next sign-in, since
        the administrator now knows this one.
        """
        self.validate_password_strength(new_password)
        self.users.update(
            user,
            password_hash=hash_password(new_password),
            must_change_password=True,
        )
        self.logout_all(user.id)
        return user

    def update_email(self, user: User, email: str) -> User:
        """Point a login at a new address.

        Uniqueness is enforced here as well as in the employees table, because
        both columns are unique and an unsynced pair would break sign-in.
        """
        normalised = (email or "").strip().lower()
        if not normalised or "@" not in normalised:
            raise ValidationError("A valid email address is required.")
        if self.users.email_exists(normalised, excluding_id=user.id):
            raise EmailAlreadyRegisteredError(
                f"{normalised} is already registered."
            )
        self.users.update(user, email=normalised)
        logger.info("user id=%s changed email", user.id)
        return user

    def change_own_password(
        self, user: User, *, current_password: str, new_password: str
    ) -> User:
        """Self-service password change.

        Requires the current password so a stolen session cannot lock the real
        owner out, rejects reuse of the same password, and revokes every other
        session on success.
        """
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("Your current password is incorrect.")

        if verify_password(new_password, user.password_hash):
            raise WeakPasswordError(
                "Your new password must be different from your current one."
            )

        self.validate_password_strength(new_password)
        # Choosing their own password clears the forced-change flag.
        self.users.update(
            user,
            password_hash=hash_password(new_password),
            must_change_password=False,
        )
        self.logout_all(user.id)
        logger.info("user id=%s changed their own password", user.id)
        return user

    def set_active(self, user: User, is_active: bool) -> User:
        """Activate/deactivate a login; deactivation kills its sessions."""
        self.users.update(user, is_active=is_active)
        if not is_active:
            self.logout_all(user.id)
        return user

    # ---- session lifecycle ----------------------------------------------
    def login(self, *, email: str, password: str) -> tuple[User, dict]:
        """Verify credentials and open a session.

        Returns the user and a fresh token pair.  The refresh row is created by
        :meth:`persist_refresh` so the caller can pick its own transaction
        boundary.
        """
        user = self.users.get_by_email(email)
        if user is None or not verify_password(password, user.password_hash):
            # Identical error for unknown email and bad password.
            raise InvalidCredentialsError()
        if not user.is_active:
            raise InactiveUserError()

        if needs_rehash(user.password_hash):
            self.users.update(user, password_hash=hash_password(password))
            logger.info("rehashed password for user id=%s", user.id)

        if user.employee is not None and not user.employee.is_active:
            raise InactiveUserError("This employee record is deactivated.")

        tokens, session_id = self._mint(user, user.employee)
        self.persist_refresh(user.id, session_id)
        return user, tokens

    def rotate(self, *, user_id: int, presented_token: str) -> tuple[User, dict]:
        """Server-side refresh rotation.

        The presented token must resolve to a live session; that session is
        revoked and a new pair is issued.  A token that is unknown, already
        revoked or expired is rejected.

        Lookup goes through the token's signed ``jti``, which is the same value
        stored as ``token_hash`` at login — the raw token itself is never
        persisted.
        """
        session_id = self.session_id_for(presented_token)
        record = self.tokens.get_by_session_id(session_id) if session_id else None
        if record is None or record.user_id != user_id:
            raise TokenRevokedError()

        if record.revoked_at is not None:
            # Possible replay of an already-rotated token: revoke the family.
            logger.warning(
                "refresh replay detected user=%s token_id=%s", user_id, record.id
            )
            self.tokens.revoke_all_for_user(user_id)
            raise TokenRevokedError()

        if record.expires_at <= utcnow():
            raise TokenExpiredError()

        user = self.users.get(user_id)
        if user is None or not user.is_active:
            raise InactiveUserError()

        self.tokens.revoke(record)
        tokens, new_session_id = self._mint(user, user.employee)
        self.persist_refresh(user.id, new_session_id)
        return user, tokens

    def logout(self, *, presented_token: str | None) -> bool:
        """Revoke one session.  Unknown or absent tokens are a no-op."""
        if not presented_token:
            return False
        session_id = self.session_id_for(presented_token)
        record = self.tokens.get_by_session_id(session_id) if session_id else None
        if record is None:
            return False
        self.tokens.revoke(record)
        return True

    @staticmethod
    def session_id_for(token: str | None) -> str | None:
        """Extract the signed session id from a refresh token.

        Returns ``None`` when the token cannot be decoded, so callers can treat
        a malformed token exactly like an unknown one.
        """
        if not token:
            return None
        try:
            claims = decode_token(token)
        except Exception:  # noqa: BLE001 - any decode failure means "not valid"
            return None
        session_id = claims.get("iid")
        return str(session_id) if session_id else None

    def logout_all(self, user_id: int) -> int:
        """Revoke every active refresh token for ``user_id``."""
        return self.tokens.revoke_all_for_user(user_id)

    def current_claims(self) -> dict:
        """The verified claims of the access token on this request."""
        try:
            claims = get_jwt()
        except RuntimeError as exc:  # no context / no token
            raise ValidationError("No active token on this request.") from exc
        return dict(claims)

    def role_of(self, user: User) -> str:
        return user.role if user else Role.EMPLOYEE.value
