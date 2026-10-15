"""Fixed strings and enums for the rules feature."""

from __future__ import annotations

from enum import Enum


class TriggerType(str, Enum):
    """What a disciplinary rule counts."""

    LATE = "late"
    ABSENT = "absent"
    EARLY_LEAVE = "early_leave"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "TriggerType":
        return value if isinstance(value, cls) else cls(str(value).lower())


class RulePeriod(str, Enum):
    """Rolling window a disciplinary rule evaluates over."""

    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "RulePeriod":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def days(self) -> int:
        return _PERIOD_DAYS[self]


_PERIOD_DAYS: dict[RulePeriod, int] = {
    RulePeriod.WEEK: 7,
    RulePeriod.MONTH: 30,
    RulePeriod.QUARTER: 90,
}


class DisciplinaryActionType(str, Enum):
    """Action taken when a threshold is crossed."""

    WARNING = "warning"
    WRITTEN_WARNING = "written_warning"
    SUSPENSION = "suspension"
    TERMINATION_REVIEW = "termination_review"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "DisciplinaryActionType":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()

    @property
    def tailwind(self) -> str:
        return {
            DisciplinaryActionType.WARNING.value: "bg-amber-100 text-amber-700 ring-amber-600/20",
            DisciplinaryActionType.WRITTEN_WARNING.value: "bg-orange-100 text-orange-700 ring-orange-600/20",
            DisciplinaryActionType.SUSPENSION.value: "bg-rose-100 text-rose-700 ring-rose-600/20",
            DisciplinaryActionType.TERMINATION_REVIEW.value: "bg-rose-200 text-rose-800 ring-rose-700/30",
        }.get(self.value, "bg-slate-100 text-slate-600 ring-slate-500/20")


#: Notification events emitted by this feature.
EVENT_COLOR_ESCALATED = "color_escalated"
EVENT_DISCIPLINARY_ACTION = "disciplinary_action"

#: Default lookback used by the colour recompute job, in days.
COLOR_WINDOW_DEFAULT_DAYS = 30
