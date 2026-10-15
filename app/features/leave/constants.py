"""Fixed strings and enums for the leave feature."""

from __future__ import annotations

from enum import Enum


class LeaveType(str, Enum):
    """Configured leave categories."""

    ANNUAL = "annual"
    SICK = "sick"
    MATERNITY = "maternity"
    PATERNITY = "paternity"
    UNPAID = "unpaid"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "LeaveType":
        return value if isinstance(value, cls) else cls(str(value))

    @property
    def skips_suggestion_engine(self) -> bool:
        """Special leaves go straight to PENDING using ``default_days``."""
        return self in SPECIAL_LEAVE_TYPES


class LeaveStatus(str, Enum):
    """Lifecycle states for a leave request."""

    SUGGESTED = "suggested"
    PENDING = "pending"
    APPROVED = "approved"
    APPROVED_FORCED = "approved_forced"
    DENIED = "denied"
    CANCELLED = "cancelled"
    #: HR pulled the employee back to work; the days are refunded and the
    #: employee is expected to take them later (a reschedule is proposed).
    RECALLED = "recalled"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "LeaveStatus":
        return value if isinstance(value, cls) else cls(str(value))

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_LEAVE_STATUSES

    @property
    def is_live(self) -> bool:
        """Counts against department capacity / appears on calendars."""
        return self in LIVE_LEAVE_STATUSES


#: Leaves that bypass the suggestion engine and use the rule's default_days.
SPECIAL_LEAVE_TYPES: frozenset[LeaveType] = frozenset(
    {LeaveType.SICK, LeaveType.MATERNITY, LeaveType.PATERNITY}
)

#: Statuses that consume balance and occupy the department calendar.
LIVE_LEAVE_STATUSES: frozenset[LeaveStatus] = frozenset(
    {LeaveStatus.APPROVED, LeaveStatus.APPROVED_FORCED}
)

#: Statuses that end the request lifecycle.
TERMINAL_LEAVE_STATUSES: frozenset[LeaveStatus] = frozenset(
    {
        LeaveStatus.APPROVED,
        LeaveStatus.APPROVED_FORCED,
        LeaveStatus.DENIED,
        LeaveStatus.CANCELLED,
        LeaveStatus.RECALLED,
    }
)

#: Statuses an HR/Admin decision may act upon.
DECIDABLE_STATUSES: frozenset[LeaveStatus] = frozenset(
    {LeaveStatus.PENDING, LeaveStatus.SUGGESTED}
)

#: Statuses the employee themselves may still act on.
EMPLOYEE_ACTIONABLE_STATUSES: frozenset[LeaveStatus] = frozenset(
    {LeaveStatus.SUGGESTED}
)

#: How many suggestions are persisted onto the request.
SUGGESTION_LIMIT = 3

#: Candidate windows are searched this far ahead, in days.
SUGGESTION_HORIZON_DAYS = 120

#: How many alternative windows the engine evaluates before ranking.
SUGGESTION_CANDIDATE_LIMIT = 240

#: How far ahead the annual planner and the "next free window" search look.
PLAN_SEARCH_HORIZON_DAYS = 400

#: Statuses of a generated annual roster entry.
class LeavePlanStatus(str, Enum):
    """Lifecycle of a department's annual leave plan."""

    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "LeavePlanStatus":
        return value if isinstance(value, cls) else cls(str(value))

#: An employee gets exactly one denial before force-approval.
MAX_DENIALS = 1

#: Scoring weights for the suggestion engine (higher is better).
SCORE_EARLIER_START = 3.0
SCORE_MIDWEEK_START = 2.0
SCORE_DEPARTMENT_CLEAR = 5.0
SCORE_NO_WEEKEND_PADDING = 2.0
PENALTY_SHORT_NOTICE = 1.5
PENALTY_FRAGMENTED = 1.0

#: Fields accepted when importing balances from CSV.
BALANCE_IMPORT_COLUMNS: tuple[str, ...] = (
    "employee_no",
    "year",
    "leave_type",
    "entitled",
    "used",
    "pending",
)

#: Notification event types emitted by this feature.
EVENT_LEAVE_REQUESTED = "leave_requested"
EVENT_LEAVE_SUGGESTED = "leave_suggested"
EVENT_LEAVE_APPROVED = "leave_approved"
EVENT_LEAVE_DENIED = "leave_denied"
EVENT_LEAVE_FORCED = "leave_forced"
EVENT_LEAVE_CANCELLED = "leave_cancelled"
EVENT_LEAVE_RECALLED = "leave_recalled"
