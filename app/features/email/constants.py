"""Fixed strings and enums for the email feature."""

from __future__ import annotations

from enum import Enum


class EmailBackend(str, Enum):
    """How an outbound email is delivered."""

    CONSOLE = "console"
    SMTP = "smtp"
    NULL = "null"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "EmailBackend":
        if value is None:
            return cls.CONSOLE
        return value if isinstance(value, cls) else cls(str(value).strip().lower())


class EmailStatus(str, Enum):
    """Delivery state of one outbound email."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "EmailStatus":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def is_terminal(self) -> bool:
        return self in {EmailStatus.SENT, EmailStatus.FAILED}


#: Event types this feature sends.  Kept explicit so templates and business
#: code agree on the name.
EVENT_LEAVE_REQUESTED = "leave_requested"
EVENT_LEAVE_SUBMITTED_HR = "leave_submitted_hr"
EVENT_LEAVE_APPROVED = "leave_approved"
EVENT_LEAVE_DENIED = "leave_denied"
EVENT_LEAVE_FORCED = "leave_forced"
EVENT_LEAVE_CANCELLED = "leave_cancelled"
EVENT_LEAVE_PENDING_HR = "leave_pending_hr"

EVENT_COLOR_ESCALATED = "color_escalated"
EVENT_DISCIPLINARY_ACTION = "disciplinary_action"
EVENT_DEVICE_OFFLINE = "device_offline"
EVENT_WELCOME = "welcome"

#: Roles that receive HR-facing mail about leave.
HR_RECIPIENT_ROLES: tuple[str, ...] = ("HR", "ADMIN")

#: Category used for emails that do not map to a known event.
CATEGORY_GENERAL = "general"
