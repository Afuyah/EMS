"""Fixed strings and enums for the employees feature."""

from __future__ import annotations

from enum import Enum


class Gender(str, Enum):
    """Employee sex, used to decide maternity versus paternity leave.

    Deliberately binary because it exists to answer one question: which
    statutory parental leave applies.  ``None`` (not this enum) means HR has not
    recorded it yet, and neither parental type is available until they do.
    """

    MALE = "male"
    FEMALE = "female"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "Gender":
        return value if isinstance(value, cls) else cls(str(value).strip().lower())

    @property
    def label(self) -> str:
        return self.value.capitalize()

    @property
    def parental_leave_type(self) -> str:
        """The parental leave this employee is entitled to request."""
        return "maternity" if self is Gender.FEMALE else "paternity"


class EmployeeColor(str, Enum):
    """Attendance health badge, recomputed nightly by the rules feature."""

    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "EmployeeColor":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def severity(self) -> int:
        return _COLOR_SEVERITY[self]

    @property
    def label(self) -> str:
        return self.value.capitalize()

    @property
    def tailwind(self) -> str:
        """Badge classes for the HTML UI."""
        return _COLOR_TAILWIND[self]


_COLOR_SEVERITY: dict[EmployeeColor, int] = {
    EmployeeColor.GREEN: 0,
    EmployeeColor.YELLOW: 1,
    EmployeeColor.ORANGE: 2,
    EmployeeColor.RED: 3,
}

_COLOR_TAILWIND: dict[EmployeeColor, str] = {
    EmployeeColor.GREEN: "bg-emerald-100 text-emerald-700 ring-emerald-600/20",
    EmployeeColor.YELLOW: "bg-amber-100 text-amber-700 ring-amber-600/20",
    EmployeeColor.ORANGE: "bg-orange-100 text-orange-700 ring-orange-600/20",
    EmployeeColor.RED: "bg-rose-100 text-rose-700 ring-rose-600/20",
}

#: Column length for employee_no (e.g. ``EMP-0001``).
EMPLOYEE_NO_PREFIX = "EMP"
EMPLOYEE_NO_WIDTH = 4

#: Seed department codes referenced by the seed package.
DEFAULT_TIMEZONE = "Africa/Nairobi"

#: Sort keys accepted by the employee list endpoint.
EMPLOYEE_SORT_FIELDS: tuple[str, ...] = (
    "last_name",
    "first_name",
    "employee_no",
    "hired_at",
    "created_at",
)
