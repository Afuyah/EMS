"""Current-user accessors and role plumbing.

Kept free of SQLAlchemy queries so services and views can both use it without
creating a dependency inversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import g, has_request_context

from app.core.exceptions import AuthenticationError, AuthorizationError
from app.features.auth.constants import Role


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, derived from JWT claims."""

    user_id: int
    role: str
    email: str | None = None
    employee_id: int | None = None
    department_id: int | None = None
    is_active: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN.value

    @property
    def is_hr(self) -> bool:
        return self.role == Role.HR.value

    @property
    def is_manager(self) -> bool:
        return self.role == Role.MANAGER.value

    @property
    def is_employee(self) -> bool:
        return self.role == Role.EMPLOYEE.value

    @property
    def is_hr_or_admin(self) -> bool:
        return self.role in {Role.HR.value, Role.ADMIN.value}

    def has_role(self, *roles: str | Role) -> bool:
        wanted = {r.value if isinstance(r, Role) else r for r in roles}
        return self.role in wanted


def load_principal(claims: dict[str, Any]) -> Principal:
    """Build a :class:`Principal` from verified JWT claims."""
    identity = claims.get("sub")
    if identity is None:
        raise AuthenticationError("Token is missing a subject claim.")
    return Principal(
        user_id=int(identity),
        role=claims.get("role", Role.EMPLOYEE.value),
        email=claims.get("email"),
        employee_id=claims.get("employee_id"),
        department_id=claims.get("department_id"),
        is_active=bool(claims.get("is_active", True)),
    )


def set_current_principal(principal: Principal) -> Principal:
    """Cache the principal on the request globals."""
    g.current_principal = principal
    return principal


def current_principal() -> Principal:
    """Return the principal for this request, raising when unauthenticated."""
    if not has_request_context():
        raise AuthenticationError("No request context is active.")
    principal = getattr(g, "current_principal", None)
    if principal is None:
        raise AuthenticationError("Authentication is required.")
    return principal


def require_roles(*roles: str | Role) -> Principal:
    """Assert the caller holds one of ``roles``."""
    principal = current_principal()
    if not principal.has_role(*roles):
        expected = ", ".join(sorted(r.value if isinstance(r, Role) else r for r in roles))
        raise AuthorizationError(
            f"This action requires one of the following roles: {expected}."
        )
    return principal


def require_self_or_roles(subject_user_id: int, *roles: str | Role) -> Principal:
    """Allow the owner of a record, or any caller holding ``roles``."""
    principal = current_principal()
    if principal.user_id == subject_user_id or principal.has_role(*roles):
        return principal
    raise AuthorizationError("You may only act on your own records.")
