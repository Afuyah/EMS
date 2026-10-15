"""Pure attendance resolver.

No database, no Flask.  Given a punch instant, the existing daily row and the
department's shift policy, it decides what the attendance row should become.

Rules, per the project brief:

* the first punch of the day sets ``clock_in``
* a second, later punch sets ``clock_out``
* extra punches are ignored
* ``late_minutes`` / ``early_leave_minutes`` / ``worked_minutes`` are recomputed
  against the department's shift policy
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.core.time import ensure_utc, minutes_between, shift_bounds
from app.features.attendance.constants import (
    DEFAULT_VERY_LATE_AFTER_MINUTES,
    HALF_DAY_RATIO,
    LATE_THRESHOLD_MINUTES,
    MAX_CLOCK_OUT_GAP_MINUTES,
    MIN_PUNCH_GAP_MINUTES,
    ArrivalTier,
    AttendanceStatus,
)
from app.features.biometrics.constants import PunchType


@dataclass(frozen=True)
class ShiftPolicy:
    """The resolved policy for one employee on one day."""

    shift_start: time
    shift_end: time
    grace_minutes: int = 0
    #: Minutes past the grace period before an arrival counts as "very late".
    very_late_after_minutes: int = DEFAULT_VERY_LATE_AFTER_MINUTES
    #: Minutes after shift start at which a missing punch becomes an absence.
    #: ``None`` defers that decision to the nightly job.
    auto_absent_after_minutes: int | None = None

    @property
    def nominal_minutes(self) -> int:
        base = datetime.combine(datetime(2000, 1, 1).date(), self.shift_start)
        end = datetime.combine(datetime(2000, 1, 1).date(), self.shift_end)
        if end <= base:
            end += timedelta(days=1)
        return int((end - base).total_seconds() // 60)

    @property
    def very_late_cutoff_minutes(self) -> int:
        """Total minutes past shift start at which "very late" begins."""
        return self.grace_minutes + max(0, self.very_late_after_minutes)

    def arrival_tier(self, late_minutes: int) -> ArrivalTier:
        """Classify minutes-past-grace into an arrival tier.

        The threshold is exclusive: a policy set to 60 marks exactly 60 minutes
        as ``LATE`` and 61 as ``VERY_LATE``, which matches the wording
        "very late after 60 minutes".
        """
        if late_minutes <= 0:
            return ArrivalTier.ON_TIME
        if self.very_late_after_minutes and late_minutes > self.very_late_after_minutes:
            return ArrivalTier.VERY_LATE
        return ArrivalTier.LATE

    def is_auto_absent(self, minutes_past_shift_start: int) -> bool:
        """Whether a still-missing punch should now count as an absence."""
        if self.auto_absent_after_minutes is None:
            return False
        return minutes_past_shift_start >= self.auto_absent_after_minutes


@dataclass(frozen=True)
class PunchEvent:
    """One punch as reported by a device."""

    punch_time: datetime
    punch_type: PunchType = PunchType.AUTO


@dataclass(frozen=True)
class ResolvedDay:
    """The attendance values a punch resolves to."""

    action: str  # "clock_in" | "clock_out" | "ignored"
    reason: str
    status: AttendanceStatus | None = None
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    late_minutes: int = 0
    early_leave_minutes: int = 0
    worked_minutes: int = 0
    tier: ArrivalTier = ArrivalTier.ON_TIME

    @property
    def changed(self) -> bool:
        return self.action != "ignored"


def compute_late_minutes(
    clock_in: datetime, *, work_date, policy: ShiftPolicy, tz_name: str
) -> int:
    """Minutes past ``shift_start + grace``; zero when on time."""
    shift_start_utc, _ = shift_bounds(work_date, policy.shift_start, policy.shift_end, tz_name)
    grace_cutoff = shift_start_utc + timedelta(minutes=policy.grace_minutes)
    late = minutes_between(ensure_utc(clock_in), grace_cutoff)
    return late if late >= LATE_THRESHOLD_MINUTES else 0


def minutes_past_shift_start(
    moment: datetime, *, work_date, policy: ShiftPolicy, tz_name: str
) -> int:
    """Whole minutes between shift start and ``moment`` (0 before the shift)."""
    shift_start_utc, _ = shift_bounds(work_date, policy.shift_start, policy.shift_end, tz_name)
    return minutes_between(ensure_utc(moment), shift_start_utc)


def should_mark_absent(
    *,
    now: datetime,
    work_date,
    policy: ShiftPolicy,
    tz_name: str,
    has_clock_in: bool,
) -> bool:
    """Whether a missing punch has now passed the configured absence cut-off.

    Pure so the nightly job and any intra-day sweep share one rule.
    """
    if has_clock_in:
        return False
    elapsed = minutes_past_shift_start(now, work_date=work_date, policy=policy, tz_name=tz_name)
    return policy.is_auto_absent(elapsed)


def compute_early_leave_minutes(
    clock_out: datetime, *, work_date, policy: ShiftPolicy, tz_name: str
) -> int:
    """Minutes the employee left before ``shift_end``; zero when full shift."""
    _, shift_end_utc = shift_bounds(work_date, policy.shift_start, policy.shift_end, tz_name)
    early = minutes_between(shift_end_utc, ensure_utc(clock_out))
    # Leaving after the shift end is not "early", so clamp to the shift window.
    return min(early, policy.nominal_minutes)


def compute_worked_minutes(clock_in: datetime, clock_out: datetime) -> int:
    """Minutes actually worked between two punches."""
    return minutes_between(ensure_utc(clock_out), ensure_utc(clock_in))


def classify(
    *,
    late_minutes: int,
    early_leave_minutes: int,
    worked_minutes: int,
    policy: ShiftPolicy,
    has_clock_out: bool,
    tier: ArrivalTier | None = None,
) -> AttendanceStatus:
    """Map computed minutes onto a daily status.

    The arrival tier decides between ``LATE`` and ``VERY_LATE``.  When no tier
    is supplied it is derived from ``late_minutes`` via the policy, so callers
    only need the minutes.
    """
    if late_minutes > 0:
        resolved = tier if tier is not None else policy.arrival_tier(late_minutes)
        if resolved is ArrivalTier.VERY_LATE:
            return AttendanceStatus.VERY_LATE
        return AttendanceStatus.LATE
    if has_clock_out and policy.nominal_minutes:
        if worked_minutes < policy.nominal_minutes * HALF_DAY_RATIO:
            return AttendanceStatus.HALF_DAY
    return AttendanceStatus.PRESENT


def resolve_punch(
    *,
    punch: PunchEvent,
    work_date,
    tz_name: str,
    policy: ShiftPolicy,
    existing_clock_in: datetime | None = None,
    existing_clock_out: datetime | None = None,
) -> ResolvedDay:
    """Decide what one punch does to a daily attendance row."""
    punch_time = ensure_utc(punch.punch_time)
    punch_type = PunchType.coerce(punch.punch_type)

    # --- no clock-in yet: this punch opens the day -----------------------
    if existing_clock_in is None:
        late = compute_late_minutes(
            punch_time, work_date=work_date, policy=policy, tz_name=tz_name
        )
        return ResolvedDay(
            action="clock_in",
            reason="first_punch_of_day",
            status=classify(
                late_minutes=late,
                early_leave_minutes=0,
                worked_minutes=0,
                policy=policy,
                has_clock_out=False,
            ),
            clock_in=punch_time,
            late_minutes=late,
            tier=policy.arrival_tier(late),
        )

    clock_in = ensure_utc(existing_clock_in)

    # --- already closed: extra punches are ignored ------------------------
    if existing_clock_out is not None:
        return ResolvedDay(
            action="ignored",
            reason="day_already_closed",
            clock_in=clock_in,
            clock_out=ensure_utc(existing_clock_out),
        )

    # --- a punch earlier than the clock-in does not close the day ---------
    if punch_time <= clock_in:
        return ResolvedDay(
            action="ignored",
            reason="punch_before_clock_in",
            clock_in=clock_in,
        )

    # --- successive punches that are effectively the same moment ----------
    if punch_time - clock_in < timedelta(minutes=MIN_PUNCH_GAP_MINUTES):
        return ResolvedDay(
            action="ignored",
            reason="within_minimum_gap",
            clock_in=clock_in,
        )

    # --- an explicit "in" never becomes a clock-out ------------------------
    if punch_type is PunchType.IN:
        return ResolvedDay(
            action="ignored",
            reason="duplicate_clock_in_type",
            clock_in=clock_in,
        )

    # --- a "not out" punch without an explicit out type, too far away ------
    if (
        punch_type is not PunchType.OUT
        and punch_time - clock_in > timedelta(minutes=MAX_CLOCK_OUT_GAP_MINUTES)
    ):
        return ResolvedDay(
            action="ignored",
            reason="clock_out_too_far_from_clock_in",
            clock_in=clock_in,
        )

    # --- close the day ----------------------------------------------------
    late = compute_late_minutes(
        clock_in, work_date=work_date, policy=policy, tz_name=tz_name
    )
    early = compute_early_leave_minutes(
        punch_time, work_date=work_date, policy=policy, tz_name=tz_name
    )
    worked = compute_worked_minutes(clock_in, punch_time)
    return ResolvedDay(
        action="clock_out",
        reason="second_punch_of_day",
        status=classify(
            late_minutes=late,
            early_leave_minutes=early,
            worked_minutes=worked,
            policy=policy,
            has_clock_out=True,
        ),
        clock_in=clock_in,
        clock_out=punch_time,
        late_minutes=late,
        early_leave_minutes=early,
        worked_minutes=worked,
        tier=policy.arrival_tier(late),
    )


def recompute(
    *,
    work_date,
    tz_name: str,
    policy: ShiftPolicy,
    clock_in: datetime | None,
    clock_out: datetime | None,
) -> ResolvedDay:
    """Recalculate all derived minutes for a pair of punches.

    Used by the attendance-correction path and the nightly job so a manual edit
    produces the same numbers as a device punch would have.
    """
    if clock_in is None:
        return ResolvedDay(action="ignored", reason="no_clock_in")

    clock_in = ensure_utc(clock_in)
    late = compute_late_minutes(clock_in, work_date=work_date, policy=policy, tz_name=tz_name)

    if clock_out is None:
        return ResolvedDay(
            action="clock_in",
            reason="open_day",
            status=classify(
                late_minutes=late,
                early_leave_minutes=0,
                worked_minutes=0,
                policy=policy,
                has_clock_out=False,
            ),
            clock_in=clock_in,
            late_minutes=late,
            tier=policy.arrival_tier(late),
        )

    clock_out = ensure_utc(clock_out)
    early = compute_early_leave_minutes(
        clock_out, work_date=work_date, policy=policy, tz_name=tz_name
    )
    worked = compute_worked_minutes(clock_in, clock_out)
    return ResolvedDay(
        action="clock_out",
        reason="closed_day",
        status=classify(
            late_minutes=late,
            early_leave_minutes=early,
            worked_minutes=worked,
            policy=policy,
            has_clock_out=True,
        ),
        clock_in=clock_in,
        clock_out=clock_out,
        late_minutes=late,
        early_leave_minutes=early,
        worked_minutes=worked,
        tier=policy.arrival_tier(late),
    )
