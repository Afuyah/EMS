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

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.core.time import ensure_utc, minutes_between, shift_bounds
from app.features.attendance.constants import (
    DEFAULT_VERY_LATE_AFTER_MINUTES,
    HALF_DAY_RATIO,
    LATE_THRESHOLD_MINUTES,
    MAX_CLOCK_OUT_GAP_MINUTES,
    MIN_PUNCH_GAP_MINUTES,
    MIN_SESSION_BREAK_MINUTES,
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
    #: How long a gap after clocking out must be before the next punch counts as
    #: a second session rather than a stray scan. Raise it where staff take long
    #: breaks; lower it where they routinely come and go.
    session_break_minutes: int = MIN_SESSION_BREAK_MINUTES

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
class WorkSession:
    """One continuous period of attendance: a clock-in and its clock-out.

    A day may contain several — someone who leaves at lunchtime and returns in
    the afternoon has two. Only a ``clock_out`` that has happened is stored, so
    an open session has ``clock_out is None`` and is still in progress.
    """

    clock_in: datetime
    clock_out: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.clock_out is None

    @property
    def worked_minutes(self) -> int:
        """Completed minutes in this session; 0 while it is still open."""
        if self.clock_out is None:
            return 0
        return compute_worked_minutes(self.clock_in, self.clock_out)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "in": ensure_utc(self.clock_in).isoformat(),
            "out": ensure_utc(self.clock_out).isoformat() if self.clock_out else None,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkSession":
        """Rebuild from stored JSON. Tolerates the key names we have used."""
        start = raw.get("in") or raw.get("clock_in")
        end = raw.get("out") or raw.get("clock_out")
        return cls(
            clock_in=datetime.fromisoformat(str(start)),
            clock_out=datetime.fromisoformat(str(end)) if end else None,
        )


def parse_sessions(raw) -> list[WorkSession]:
    """Read the stored session list, ignoring anything malformed.

    Attendance must never fail to render because one historical row holds an
    unexpected shape, so bad entries are skipped rather than raised.
    """
    if not isinstance(raw, list):
        return []
    sessions: list[WorkSession] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            sessions.append(WorkSession.from_dict(item))
        except (TypeError, ValueError):
            continue
    return sessions


def total_worked_minutes(sessions: list[WorkSession]) -> int:
    """Sum of every completed session. The open one contributes nothing yet."""
    return sum(session.worked_minutes for session in sessions)


def first_clock_in(sessions: list[WorkSession]) -> datetime | None:
    real = [session.clock_in for session in sessions]
    return min(real) if real else None


def last_clock_out(sessions: list[WorkSession]) -> datetime | None:
    """The latest *completed* clock-out, or ``None`` if any session is open.

    An open session means the employee is currently in, so the day has no final
    clock-out to show yet.
    """
    if any(session.is_open for session in sessions):
        return None
    ends = [session.clock_out for session in sessions if session.clock_out]
    return max(ends) if ends else None


@dataclass
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
    #: Every session of the day after this punch, in order.
    sessions: list[WorkSession] = field(default_factory=list)

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


def _close_session(
    *,
    sessions: list[WorkSession],
    punch_time: datetime,
    closed_index: int,
    work_date,
    tz_name: str,
    policy: ShiftPolicy,
) -> ResolvedDay:
    """Finish the session at ``closed_index`` and recompute the day.

    The day's lateness comes from the **first** clock-in (when they arrived),
    and its worked total from **every** completed session, so a split shift is
    credited in full rather than only its first half.
    """
    clock_in = sessions[0].clock_in
    late = compute_late_minutes(
        clock_in, work_date=work_date, policy=policy, tz_name=tz_name
    )
    early = compute_early_leave_minutes(
        punch_time, work_date=work_date, policy=policy, tz_name=tz_name
    )
    worked = total_worked_minutes(sessions)
    return ResolvedDay(
        action="clock_out",
        reason="session_closed",
        status=classify(
            late_minutes=late,
            early_leave_minutes=early,
            worked_minutes=worked,
            policy=policy,
            # The day is only complete once no session is still open.
            has_clock_out=not any(item.is_open for item in sessions),
        ),
        clock_in=clock_in,
        clock_out=last_clock_out(sessions),
        late_minutes=late,
        early_leave_minutes=early,
        worked_minutes=worked,
        tier=policy.arrival_tier(late),
        sessions=sessions,
    )


def resolve_punch(
    *,
    punch: PunchEvent,
    work_date,
    tz_name: str,
    policy: ShiftPolicy,
    existing_sessions: list[WorkSession] | None = None,
    existing_clock_in: datetime | None = None,
    existing_clock_out: datetime | None = None,
) -> ResolvedDay:
    """Decide what one punch does to a daily attendance row.

    A day is a sequence of sessions. Successive punches alternate: an open
    session is closed by the next punch, and after a long enough gap the punch
    after that opens a new one. So

        07:00 in, 11:00 out, 15:00 in, 17:00 out

    is two sessions and four hours plus two hours — the afternoon is *not*
    discarded, which is what a single clock-in/clock-out pair would do.

    ``existing_sessions`` is authoritative; the scalar ``existing_clock_in`` /
    ``existing_clock_out`` are accepted for callers holding rows recorded before
    sessions existed.
    """
    punch_time = ensure_utc(punch.punch_time)
    punch_type = PunchType.coerce(punch.punch_type)

    # Rebuild the day when only the legacy scalar columns were supplied.
    sessions = list(existing_sessions or [])
    if not sessions and existing_clock_in is not None:
        sessions = [
            WorkSession(
                clock_in=ensure_utc(existing_clock_in),
                clock_out=ensure_utc(existing_clock_out) if existing_clock_out else None,
            )
        ]

    # --- nobody has arrived yet: this punch opens the day ------------------
    if not sessions:
        late = compute_late_minutes(
            punch_time, work_date=work_date, policy=policy, tz_name=tz_name
        )
        opening = WorkSession(clock_in=punch_time)
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
            sessions=[opening],
        )

    open_index = next(
        (index for index, item in enumerate(sessions) if item.is_open), None
    )

    # --- a session is running ---------------------------------------------
    if open_index is not None:
        current = sessions[open_index]
        clock_in = ensure_utc(current.clock_in)

        # A punch that cannot start a session because it precedes the clock-in.
        # Handled first: a negative elapsed time would otherwise satisfy any
        # "less than a threshold" test below and be misreported as a duplicate.
        if punch_time < clock_in:
            return ResolvedDay(
                action="ignored",
                reason="punch_before_clock_in",
                clock_in=sessions[0].clock_in,
                sessions=sessions,
            )

        # Two scans at effectively the same moment: a duplicate, not a session.
        if punch_time - clock_in < timedelta(minutes=MIN_PUNCH_GAP_MINUTES):
            return ResolvedDay(
                action="ignored",
                reason="within_minimum_gap",
                clock_in=sessions[0].clock_in,
                sessions=sessions,
            )

        # An explicit "in" never closes a session.
        if punch_type is PunchType.IN:
            return ResolvedDay(
                action="ignored",
                reason="duplicate_clock_in_type",
                clock_in=sessions[0].clock_in,
                sessions=sessions,
            )

        # Too far from the clock-in to be credible as its clock-out. The punch is
        # reported rather than silently absorbed, so the day is not left open.
        if (
            punch_type is not PunchType.OUT
            and punch_time - clock_in > timedelta(minutes=MAX_CLOCK_OUT_GAP_MINUTES)
        ):
            return ResolvedDay(
                action="ignored",
                reason="clock_out_too_far_from_clock_in",
                clock_in=sessions[0].clock_in,
                sessions=sessions,
            )

        sessions[open_index] = WorkSession(clock_in=clock_in, clock_out=punch_time)
        return _close_session(
            sessions=sessions,
            punch_time=punch_time,
            closed_index=open_index,
            work_date=work_date,
            tz_name=tz_name,
            policy=policy,
        )

    # --- every session is closed: is this a new one, or a stray duplicate? --
    last_out = last_clock_out(sessions)
    if last_out is None:  # pragma: no cover - defensive
        return ResolvedDay(action="ignored", reason="no_open_session", sessions=sessions)

    gap = punch_time - ensure_utc(last_out)
    # A repeat scan of the same departure, not a return to work. An exact match
    # is what makes a device replaying a record a no-op.
    if gap <= timedelta(0) or gap < timedelta(minutes=MIN_PUNCH_GAP_MINUTES):
        return ResolvedDay(
            action="ignored",
            reason="duplicate_after_clock_out",
            clock_in=sessions[0].clock_in,
            clock_out=last_out,
            sessions=sessions,
        )

    if gap >= timedelta(minutes=policy.session_break_minutes):
        # A genuine return: the day continues with a second session. Lateness is
        # unchanged — they were already in this morning.
        sessions.append(WorkSession(clock_in=punch_time))
        late = compute_late_minutes(
            sessions[0].clock_in, work_date=work_date, policy=policy, tz_name=tz_name
        )
        return ResolvedDay(
            action="clock_in",
            reason="new_session_after_break",
            status=classify(
                late_minutes=late,
                early_leave_minutes=0,
                worked_minutes=total_worked_minutes(sessions),
                policy=policy,
                has_clock_out=False,
            ),
            clock_in=sessions[0].clock_in,
            clock_out=None,
            late_minutes=late,
            worked_minutes=total_worked_minutes(sessions),
            tier=policy.arrival_tier(late),
            sessions=sessions,
        )

    # A short gap after clocking out with nothing in between: ignore it rather
    # than invent a session. Anything beyond a duplicate but short of a real
    # break is ambiguous, and inventing hours here would be worse than dropping
    # a stray scan.
    return ResolvedDay(
        action="ignored",
        reason="brief_absence_not_a_new_session",
        clock_in=sessions[0].clock_in,
        clock_out=last_out,
        sessions=sessions,
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
