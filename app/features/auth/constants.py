"""Fixed strings and enums for the auth feature."""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """Application roles, ordered from least to most privileged."""

    EMPLOYEE = "EMPLOYEE"
    MANAGER = "MANAGER"
    HR = "HR"
    ADMIN = "ADMIN"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "Role":
        return value if isinstance(value, cls) else cls(str(value).upper())

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: "Role") -> bool:
        return self.rank >= other.rank


_ROLE_RANK: dict[Role, int] = {
    Role.EMPLOYEE: 0,
    Role.MANAGER: 1,
    Role.HR: 2,
    Role.ADMIN: 3,
}

#: Roles allowed to manage the employee lifecycle.
HR_ROLES: tuple[str, ...] = (Role.HR.value, Role.ADMIN.value)

#: Roles allowed to approve or deny leave.
LEAVE_APPROVER_ROLES: tuple[str, ...] = (Role.HR.value, Role.ADMIN.value)

#: Roles allowed to see department-wide data.
DEPARTMENT_VISIBLE_ROLES: tuple[str, ...] = (
    Role.MANAGER.value,
    Role.HR.value,
    Role.ADMIN.value,
)

#: Roles allowed to configure the system.
ADMIN_ROLES: tuple[str, ...] = (Role.ADMIN.value,)

#: Cookie names for the browser flow.
ACCESS_COOKIE = "access_token_cookie"
REFRESH_COOKIE = "refresh_token_cookie"
CSRF_ACCESS_COOKIE = "csrf_access_token"
CSRF_REFRESH_COOKIE = "csrf_refresh_token"

#: JWT additional-claim keys.
CLAIM_ROLE = "role"
CLAIM_EMAIL = "email"
CLAIM_EMPLOYEE_ID = "employee_id"
CLAIM_DEPARTMENT_ID = "department_id"
CLAIM_ACTIVE = "is_active"
