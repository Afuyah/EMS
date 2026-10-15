"""Fixed strings and enums for the attendance feature."""

from __future__ import annotations

from datetime import time
from enum import Enum


class AttendanceStatus(str, Enum):
    """Daily attendance outcome."""

    PRESENT = "present"
    LATE = "late"
    VERY_LATE = "very_late"
    ABSENT = "absent"
    ON_LEAVE = "on_leave"
    HOLIDAY = "holiday"
    HALF_DAY = "half_day"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "AttendanceStatus":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def counts_as_present(self) -> bool:
        return self in {
            AttendanceStatus.PRESENT,
            AttendanceStatus.LATE,
            AttendanceStatus.VERY_LATE,
            AttendanceStatus.HALF_DAY,
        }

    @property
    def counts_as_absence(self) -> bool:
        return self == AttendanceStatus.ABSENT

    @property
    def is_late(self) -> bool:
        """Both lateness tiers count as late for reporting."""
        return self in {AttendanceStatus.LATE, AttendanceStatus.VERY_LATE}

    @property
    def tailwind(self) -> str:
        return _STATUS_TAILWIND[self]


class ArrivalTier(str, Enum):
    """How an arrival compares to the configured shift, before any leave or
    half-day logic is applied."""

    ON_TIME = "on_time"
    LATE = "late"
    VERY_LATE = "very_late"

    @classmethod
    def coerce(cls, value) -> "ArrivalTier":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def label(self) -> str:
        return {
            ArrivalTier.ON_TIME: "On time",
            ArrivalTier.LATE: "Late",
            ArrivalTier.VERY_LATE: "Very late",
        }[self]

    @property
    def tone(self) -> str:
        return {
            ArrivalTier.ON_TIME: "emerald",
            ArrivalTier.LATE: "amber",
            ArrivalTier.VERY_LATE: "rose",
        }[self]


_STATUS_TAILWIND: dict[AttendanceStatus, str] = {
    AttendanceStatus.PRESENT: "bg-emerald-100 text-emerald-700 ring-emerald-600/20",
    AttendanceStatus.LATE: "bg-amber-100 text-amber-700 ring-amber-600/20",
    AttendanceStatus.VERY_LATE: "bg-rose-100 text-rose-700 ring-rose-600/20",
    AttendanceStatus.ABSENT: "bg-rose-100 text-rose-700 ring-rose-600/20",
    AttendanceStatus.ON_LEAVE: "bg-sky-100 text-sky-700 ring-sky-600/20",
    AttendanceStatus.HOLIDAY: "bg-slate-100 text-slate-600 ring-slate-500/20",
    AttendanceStatus.HALF_DAY: "bg-violet-100 text-violet-700 ring-violet-600/20",
}

#: Fallback shift used when no policy row exists at all.
DEFAULT_SHIFT_START = time(9, 0)
DEFAULT_SHIFT_END = time(17, 0)
DEFAULT_GRACE_MINUTES = 10

#: Minutes past the grace period before an arrival escalates from "late" to
#: "very late".  Configurable per policy; this value is only the fallback.
DEFAULT_VERY_LATE_AFTER_MINUTES = 60

#: Minutes after shift start at which a still-missing punch becomes an absence.
#: ``None`` leaves the decision to the nightly job.
DEFAULT_AUTO_ABSENT_AFTER_MINUTES: int | None = None

#: A late arrival at or beyond this many minutes is recorded as LATE.
LATE_THRESHOLD_MINUTES = 1

#: Punches closer together than this are treated as the same clock-in.
MIN_PUNCH_GAP_MINUTES = 1

#: Beyond this, a second punch no longer reads as a clock-out.
MAX_CLOCK_OUT_GAP_MINUTES = 16 * 60

#: Worked minutes below this share of the shift flag a half day.
HALF_DAY_RATIO = 0.5

#: Notification events emitted by this feature.
EVENT_ATTENDANCE_ABSENT = "attendance_absent"
EVENT_ATTENDANCE_LATE = "attendance_late"
