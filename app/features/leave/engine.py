"""Pure leave suggestion engine.

No database access, no Flask, no I/O.  Everything it needs is passed in, which
makes the whole constraint solver unit-testable in isolation.

Scoring model (higher is better), per the project brief:

* earlier start is preferred
* a midweek start is preferred
* a window where the department is clear is strongly preferred
* windows that do not pad the leave with a weekend at either end are preferred
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from app.core.time import each_day, is_weekend
from app.features.leave.constants import (
    PENALTY_FRAGMENTED,
    PENALTY_SHORT_NOTICE,
    SCORE_DEPARTMENT_CLEAR,
    SCORE_EARLIER_START,
    SCORE_MIDWEEK_START,
    SCORE_NO_WEEKEND_PADDING,
    SUGGESTION_CANDIDATE_LIMIT,
    SUGGESTION_HORIZON_DAYS,
    SUGGESTION_LIMIT,
)


@dataclass(frozen=True)
class Window:
    """A concrete start/end range."""

    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        """Inclusive calendar-day count."""
        return (self.end_date - self.start_date).days + 1

    def as_dict(self) -> dict[str, str | int]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "days": self.days,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "Window":
        start = payload["start_date"]
        end = payload["end_date"]
        return cls(
            start_date=date.fromisoformat(str(start)),
            end_date=date.fromisoformat(str(end)),
        )


@dataclass(frozen=True)
class DeptCalendar:
    """Department availability over the search window.

    Built by the service from exactly two queries (employees in the department
    plus active leaves in the window) and then handed to the pure engine.
    """

    department_id: int | None
    headcount: int
    #: date -> set of employee ids already on approved leave that day
    on_leave_by_date: Mapping[date, frozenset[int]] = field(default_factory=dict)
    #: employee ids excluded from the capacity count entirely
    excluded_employee_ids: frozenset[int] = frozenset()

    @property
    def capacity(self) -> int:
        """Colleagues who could still be working."""
        return max(0, self.headcount - len(self.excluded_employee_ids))

    def on_leave(self, day: date) -> int:
        return len(self.on_leave_by_date.get(day, frozenset()))

    def available(self, day: date) -> int:
        return max(0, self.capacity - self.on_leave(day))


@dataclass(frozen=True)
class CapacityBreach:
    """A proposed window that would put too many colleagues off at once."""

    #: The limit that applies (``None`` means unlimited, so never a breach).
    limit: int | None
    #: date -> how many colleagues are already off that day.
    over_capacity: tuple[tuple[date, int], ...] = ()

    @property
    def breached(self) -> bool:
        return bool(self.over_capacity)

    @property
    def worst_day(self) -> date | None:
        return self.over_capacity[0][0] if self.over_capacity else None

    @property
    def message(self) -> str:
        if not self.breached:
            return ""
        day, count = self.over_capacity[0]
        others = ", ".join(
            f"{when.strftime('%d %b')} ({how} off)" for when, how in self.over_capacity[:4]
        )
        more = (
            f" and {len(self.over_capacity) - 4} more day(s)"
            if len(self.over_capacity) > 4
            else ""
        )
        return (
            f"The department already has {count} colleague(s) on leave on "
            f"{day.strftime('%d %b %Y')}, and the limit is {self.limit} "
            f"per day. Clashing day(s): {others}{more}."
        )


def check_capacity(
    window: Window,
    *,
    peers_on_leave_by_date: Mapping[date, int],
    limit: int | None,
) -> CapacityBreach:
    """Whether adding this window would exceed the per-day concurrency limit.

    Pure: the caller supplies how many *other* colleagues are already off each
    day, so this can be reasoned about and tested without a database.

    The limit is inclusive — a limit of 1 means one colleague may be off on a
    given day, so a second one breaches it.
    """
    if limit is None:
        return CapacityBreach(limit=None)

    over: list[tuple[date, int]] = []
    for day in each_day(window.start_date, window.end_date):
        # Weekends are not working days, so they never consume capacity.
        if is_weekend(day):
            continue
        peers = peers_on_leave_by_date.get(day, 0)
        if peers >= limit:
            over.append((day, peers))

    return CapacityBreach(limit=limit, over_capacity=tuple(over))


def next_free_window(
    *,
    earliest: date,
    days: int,
    peers_on_leave_by_date: Mapping[date, int],
    limit: int | None,
    horizon_days: int = 400,
    blocked_weekdays: frozenset[int] = frozenset(),
) -> Window | None:
    """The earliest window of ``days`` working days that fits under ``limit``.

    Walks forward one day at a time until it finds a contiguous run of
    weekdays where every day is below the limit.  Returns ``None`` when nothing
    fits inside the horizon, so callers can say so rather than looping forever.
    """
    if days <= 0:
        return None

    candidate = earliest
    for _ in range(horizon_days):
        # A window never starts on a weekend or a blocked weekday.
        if is_weekend(candidate) or candidate.weekday() in blocked_weekdays:
            candidate += timedelta(days=1)
            continue

        end = candidate
        remaining = days
        fits = True
        while remaining > 0:
            if is_weekend(end) or end.weekday() in blocked_weekdays:
                end += timedelta(days=1)
                continue
            if limit is not None and peers_on_leave_by_date.get(end, 0) >= limit:
                fits = False
                break
            remaining -= 1
            if remaining:
                end += timedelta(days=1)

        if fits:
            return Window(start_date=candidate, end_date=end)

        candidate += timedelta(days=1)

    return None


# ---------------------------------------------------------------------------
# Annual roster planning
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlanEmployee:
    """One employee as the planner sees them."""

    employee_id: int
    full_name: str
    employee_no: str
    #: Days to place this year (their entitlement).
    days: int
    #: Earlier date sorts first, so the longest-serving pick first.
    hired_at: date | None = None
    #: Optional 1-12 month the employee asked for.
    preferred_month: int | None = None


@dataclass(frozen=True)
class PlanEntry:
    """One employee's place in the roster."""

    employee_id: int
    full_name: str
    employee_no: str
    start_date: date | None
    end_date: date | None
    days: int
    reason: str = ""
    preferred_month: int | None = None

    @property
    def placed(self) -> bool:
        return self.start_date is not None and self.end_date is not None

    @property
    def honoured_preference(self) -> bool:
        return (
            self.placed
            and self.preferred_month is not None
            and self.start_date.month == self.preferred_month
        )

    def as_dict(self) -> dict:
        return {
            "employee_id": self.employee_id,
            "full_name": self.full_name,
            "employee_no": self.employee_no,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "end_date": self.end_date.isoformat() if self.end_date else None,
            "days": self.days,
            "placed": self.placed,
            "reason": self.reason,
            "preferred_month": self.preferred_month,
        }


@dataclass(frozen=True)
class PlanResult:
    """A full-year proposal for one department."""

    department_id: int | None
    year: int
    entries: tuple[PlanEntry, ...]
    limit: int | None
    #: Highest number of people the plan puts off on any single day.
    peak_concurrent: int = 0
    #: True when nothing had to be left unplaced.
    complete: bool = True

    @property
    def placed(self) -> list[PlanEntry]:
        return [entry for entry in self.entries if entry.placed]

    @property
    def unplaced(self) -> list[PlanEntry]:
        return [entry for entry in self.entries if not entry.placed]

    def as_dict(self) -> dict:
        return {
            "department_id": self.department_id,
            "year": self.year,
            "limit": self.limit,
            "peak_concurrent": self.peak_concurrent,
            "complete": self.complete,
            "entries": [entry.as_dict() for entry in self.entries],
        }


def _default_leave_length(days: int, max_consecutive: int | None) -> int:
    """Split a year's entitlement into sensible blocks.

    A whole year's entitlement in one go is not a roster, so anything above two
    working weeks is split into equal blocks of at most ten working days.
    """
    cap = max_consecutive or 21
    if days <= 10:
        return max(1, days)
    block = min(10, cap)
    return max(1, block)


def plan_annual_leave(
    *,
    department_id: int | None,
    year: int,
    employees: Sequence[PlanEmployee],
    limit: int | None,
    blackout_dates: Iterable[date] = (),
    horizon_months: int = 12,
) -> PlanResult:
    """Draft a year of leave for one department under a concurrency cap.

    Greedy but deterministic and fair: longest-serving employees choose first,
    and each employee is placed in the earliest free block at or after their
    phase offset, so the department's leave is spread across the year instead of
    clustering in January.

    ``limit`` is the maximum number of people off on the same day.  A limit of
    ``None`` means unlimited, in which case everyone is simply placed back to
    back from their preferred start.
    """
    blocked = set(blackout_dates)

    #: Allocations must stay inside the calendar year; anything that will not
    #: fit is reported as unplaced rather than silently pushed into January of
    #: the following year (which the year view would then never show).
    year_end = date(year, 12, 31)

    # ---- the shared occupancy map, keyed by day -------------------------
    # day -> set of employee ids already placed that day.
    occupancy: dict[date, set[int]] = {}
    # Everyone who will ever be placed, so the cap counts the real cohort.
    cohort = {employee.employee_id for employee in employees}

    def free_on(day: date) -> bool:
        """Whether ``day`` can host another person under the cap."""
        if day in blocked or is_weekend(day):
            return True  # weekends/blackouts never consume capacity
        if limit is None:
            return True
        # Count people from this cohort; the caller passes everyone relevant.
        return len(occupancy.get(day, set())) < limit

    def claim(window: Window, employee_id: int) -> None:
        for day in each_day(window.start_date, window.end_date):
            if is_weekend(day):
                continue
            occupancy.setdefault(day, set()).add(employee_id)

    def find_slot(
        *, start_from: date, workdays: int, employee_id: int
    ) -> Window | None:
        """Earliest run of ``workdays`` working days that fits inside the year."""
        candidate = start_from
        # Bound the walk to the calendar year so this always terminates and
        # never offers dates outside the year being planned.
        hard_stop = min(start_from + timedelta(days=horizon_months * 40), year_end)
        while candidate <= hard_stop:
            if is_weekend(candidate) or candidate in blocked:
                candidate += timedelta(days=1)
                continue

            end = candidate
            remaining = workdays
            fits = True
            while remaining > 0:
                if is_weekend(end) or end in blocked:
                    end += timedelta(days=1)
                    continue
                # A block that would run past 31 December does not fit.
                if end > year_end:
                    fits = False
                    break
                if not free_on(end):
                    fits = False
                    break
                remaining -= 1
                if remaining:
                    end += timedelta(days=1)

            if fits:
                return Window(start_date=candidate, end_date=end)
            candidate += timedelta(days=1)
        return None

    # ---- order: longest serving first, then employee number -------------
    ordered = sorted(
        employees,
        key=lambda employee: (
            employee.hired_at or date.max,
            employee.employee_no,
        ),
    )

    entries: list[PlanEntry] = []
    now = date.today()

    # ---- work out the round structure ----------------------------------
    # Everyone takes one block before anyone takes a second, so a scarce year
    # is shared rather than consumed by whoever is senior.
    plans: list[dict] = []
    for index, employee in enumerate(ordered):
        workdays = max(1, int(employee.days or 0))
        if employee.days <= 0:
            plans.append(
                {
                    "employee": employee,
                    "target": 0,
                    "block": 0,
                    "placed": 0,
                    "cursor": None,
                    "index": index,
                    "exhausted": True,
                }
            )
            continue

        block = _default_leave_length(workdays, None)

        # Phase offset spreads the cohort across the year: employee i starts
        # looking around month (i+1), wrapping, so cover is maintained.
        phase_month = ((index % horizon_months) + 1) if limit is not None else 1
        if employee.preferred_month:
            phase_month = max(1, min(12, employee.preferred_month))

        base_year = max(year, now.year)
        start_from = max(
            date(base_year, phase_month, 1),
            now if base_year == now.year else date(base_year, 1, 1),
        )
        plans.append(
            {
                "employee": employee,
                "target": workdays,
                "block": block,
                "placed": 0,
                "cursor": start_from,
                "index": index,
                "exhausted": False,
            }
        )

    max_rounds = max(
        (len(range(0, plan["target"], plan["block"])) for plan in plans if plan["block"]),
        default=0,
    )

    for round_index in range(max_rounds):
        for plan in plans:
            if plan["exhausted"] or plan["placed"] >= plan["target"]:
                continue

            employee = plan["employee"]
            remaining = plan["target"] - plan["placed"]
            this_block = min(plan["block"], remaining)

            slot = find_slot(
                start_from=plan["cursor"],
                workdays=this_block,
                employee_id=employee.employee_id,
            )
            if slot is None:
                # This employee cannot take another block; they keep what they
                # already have and the shortfall is reported below.
                plan["exhausted"] = True
                continue

            claim(slot, employee.employee_id)
            entries.append(
                PlanEntry(
                    employee_id=employee.employee_id,
                    full_name=employee.full_name,
                    employee_no=employee.employee_no,
                    start_date=slot.start_date,
                    end_date=slot.end_date,
                    days=this_block,
                    reason=(
                        "Preferred month honoured."
                        if employee.preferred_month
                        and slot.start_date.month == employee.preferred_month
                        else (
                            "Placed in the first round so every colleague gets a "
                            "share of the year."
                            if round_index == 0
                            else "Placed in a later round from the remaining free days."
                        )
                    ),
                    preferred_month=employee.preferred_month,
                )
            )
            plan["placed"] += this_block
            plan["cursor"] = slot.end_date + timedelta(days=1)

    # ---- report anyone who could not be fully placed --------------------
    total_days = sum(plan["target"] for plan in plans)
    placed_days = sum(plan["placed"] for plan in plans)
    shortfall = sum(
        plan["target"] - plan["placed"]
        for plan in plans
        if plan["target"] > plan["placed"]
    )

    for plan in plans:
        employee = plan["employee"]
        outstanding = plan["target"] - plan["placed"]

        if plan["target"] == 0:
            entries.append(
                PlanEntry(
                    employee_id=employee.employee_id,
                    full_name=employee.full_name,
                    employee_no=employee.employee_no,
                    start_date=None,
                    end_date=None,
                    days=0,
                    reason="No annual entitlement configured for this year.",
                    preferred_month=employee.preferred_month,
                )
            )
            continue

        if outstanding > 0:
            if plan["placed"] == 0:
                reason = (
                    "No free window under the daily limit remains this year"
                    + (
                        f" ({total_days} day(s) of demand against "
                        f"{placed_days + shortfall} day(s) of capacity)."
                        if total_days
                        else "."
                    )
                )
            else:
                reason = (
                    f"Partly placed: {plan['placed']} of {plan['target']} day(s) "
                    f"fitted. {outstanding} day(s) remain, because the year only "
                    f"had room for {placed_days} of {total_days} requested day(s) "
                    f"at {limit} person(s) off per day."
                )
            entries.append(
                PlanEntry(
                    employee_id=employee.employee_id,
                    full_name=employee.full_name,
                    employee_no=employee.employee_no,
                    start_date=None,
                    end_date=None,
                    days=outstanding,
                    reason=reason,
                    preferred_month=employee.preferred_month,
                )
            )

    peak = max((len(ids) for ids in occupancy.values()), default=0)
    return PlanResult(
        department_id=department_id,
        year=year,
        entries=tuple(entries),
        limit=limit,
        peak_concurrent=peak,
        # A plan is only "complete" when nobody is left short.
        complete=all(plan["placed"] >= plan["target"] for plan in plans),
    )


@dataclass(frozen=True)
class RuleConstraints:
    """The subset of ``leave_rules`` the engine cares about."""

    max_consecutive_days: int | None = None
    max_employees_off_per_dept: int | None = None
    min_notice_days: int = 0
    max_per_month: int | None = None
    max_per_year: int | None = None

    def notice_ok(self, window: Window, today: date) -> bool:
        return (window.start_date - today).days >= self.min_notice_days


@dataclass(frozen=True)
class ScoredWindow:
    """A candidate plus why it scored the way it did."""

    window: Window
    score: float
    eligible: bool
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def start_date(self) -> date:
        return self.window.start_date

    @property
    def end_date(self) -> date:
        return self.window.end_date

    @property
    def days(self) -> int:
        return self.window.days

    def as_suggestion(self, rank: int) -> dict:
        """Serialisable form persisted onto ``leave_requests.suggestions``."""
        payload = self.window.as_dict()
        payload.update(
            {
                "rank": rank,
                "score": round(self.score, 2),
                "department_clear": "department_clear" in self.reasons,
                "reasons": list(self.reasons),
            }
        )
        return payload


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def weekdays_in(window: Window) -> list[date]:
    """Weekdays (Mon-Fri) covered by a window."""
    return [day for day in each_day(window.start_date, window.end_date) if not is_weekend(day)]


def chargeable_days(window: Window) -> int:
    """Days actually deducted from balance: weekdays only."""
    return len(weekdays_in(window))


def spans_month_boundary(window: Window) -> bool:
    return window.start_date.month != window.end_date.month


def peak_concurrent_leave(window: Window, calendar: DeptCalendar) -> int:
    """Highest number of colleagues simultaneously on leave in the window."""
    if not calendar.on_leave_by_date:
        return 0
    return max((calendar.on_leave(day) for day in each_day(window.start_date, window.end_date)), default=0)


def overlaps_blocked(window: Window, blocked: Iterable[date]) -> list[date]:
    """Blocked dates the window would collide with."""
    blocked_set = set(blocked)
    return [day for day in each_day(window.start_date, window.end_date) if day in blocked_set]


def month_day_usage(window: Window, existing_days_by_month: Mapping[tuple[int, int], int]) -> int:
    """Existing booked days in each month touched by the window (max)."""
    keys = {
        (day.year, day.month) for day in each_day(window.start_date, window.end_date)
    }
    return max((existing_days_by_month.get(key, 0) for key in keys), default=0)


def is_midweek_start(day: date) -> bool:
    """Tuesday, Wednesday or Thursday."""
    return day.weekday() in {1, 2, 3}


def weekend_padding(window: Window) -> int:
    """Count of weekend days directly attached to either end.

    A window starting Saturday and ending Monday has 1 padded day (the
    Saturday); one starting Friday and ending Monday pads the Sunday.
    """
    padding = 0
    if is_weekend(window.start_date):
        padding += 1
    if is_weekend(window.end_date) and window.end_date != window.start_date:
        padding += 1
    # A fully weekend-spanning window with no weekday is all padding.
    if not weekdays_in(window):
        padding = window.days
    return padding


def score_window(
    window: Window,
    *,
    calendar: DeptCalendar,
    constraints: RuleConstraints,
    today: date,
    rank_index: int = 0,
) -> ScoredWindow:
    """Score one candidate window and explain the result."""
    reasons: list[str] = []
    blockers: list[str] = []

    if window.days <= 0:
        return ScoredWindow(window, 0.0, False, (), ("empty_window",))

    if not constraints.notice_ok(window, today):
        blockers.append("insufficient_notice")

    if (
        constraints.max_consecutive_days is not None
        and window.days > constraints.max_consecutive_days
    ):
        blockers.append("exceeds_max_consecutive_days")

    # Department capacity: no day may exceed the configured concurrency cap.
    if constraints.max_employees_off_per_dept is not None:
        for day in each_day(window.start_date, window.end_date):
            if calendar.on_leave(day) >= constraints.max_employees_off_per_dept:
                blockers.append("department_at_capacity")
                break

    # A window containing no weekday cannot be taken as leave.
    if not weekdays_in(window):
        blockers.append("weekend_only")

    score = 0.0

    # 1. Earlier start preferred.  Anchor on a 120-day horizon.
    days_out = max(0, (window.start_date - today).days)
    score += SCORE_EARLIER_START * (1.0 - min(days_out, SUGGESTION_HORIZON_DAYS) / SUGGESTION_HORIZON_DAYS)
    if days_out == 0:
        reasons.append("starts_today")

    # 2. Midweek start preferred.
    if is_midweek_start(window.start_date):
        score += SCORE_MIDWEEK_START
        reasons.append("midweek_start")

    # 3. Department clear for the whole window.
    if constraints.max_employees_off_per_dept is None or all(
        calendar.on_leave(day) == 0 for day in each_day(window.start_date, window.end_date)
    ):
        score += SCORE_DEPARTMENT_CLEAR
        reasons.append("department_clear")

    # 4. No weekend padding.
    padding = weekend_padding(window)
    if padding == 0:
        score += SCORE_NO_WEEKEND_PADDING
        reasons.append("no_weekend_padding")
    else:
        score -= PENALTY_FRAGMENTED * padding

    # Short-notice pressure, but never below zero.
    if constraints.min_notice_days and (window.start_date - today).days <= constraints.min_notice_days:
        score -= PENALTY_SHORT_NOTICE

    # Slight preference for the earliest of otherwise-equal windows.
    score -= rank_index * 0.001

    return ScoredWindow(
        window=window,
        score=round(score, 4),
        eligible=not blockers,
        reasons=tuple(reasons),
        blockers=tuple(blockers),
    )


def generate_candidate_windows(
    *,
    today: date,
    days: int,
    horizon_days: int = SUGGESTION_HORIZON_DAYS,
    limit: int = SUGGESTION_CANDIDATE_LIMIT,
    earliest_start: date | None = None,
) -> list[Window]:
    """Enumerate plausible start/end pairs for the requested duration.

    Rather than every start with every length, we hold the requested length
    fixed and slide the window across the horizon: that keeps the search space
    small and matches how employees actually book leave.
    """
    if days <= 0:
        return []
    first_day = earliest_start or today
    span = days - 1
    windows: list[Window] = []
    for offset in range(0, horizon_days + 1):
        start = first_day + timedelta(days=offset)
        windows.append(Window(start_date=start, end_date=start + timedelta(days=span)))
        if len(windows) >= limit:
            break
    return windows


def find_best_windows(
    *,
    today: date,
    days: int,
    calendar: DeptCalendar,
    constraints: RuleConstraints,
    horizon_days: int = SUGGESTION_HORIZON_DAYS,
    limit: int = SUGGESTION_LIMIT,
    max_candidates: int = SUGGESTION_CANDIDATE_LIMIT,
    excluded_ranges: Sequence[Window] = (),
    earliest_start: date | None = None,
) -> list[ScoredWindow]:
    """Rank candidate windows and return the best eligible ones.

    ``excluded_ranges`` lets the caller pass already-used suggestions so a
    re-suggestion after a denial does not repeat the window just declined.
    """
    candidates = generate_candidate_windows(
        today=today,
        days=days,
        horizon_days=horizon_days,
        limit=max_candidates,
        earliest_start=earliest_start,
    )

    excluded = {
        (window.start_date, window.end_date) for window in excluded_ranges
    }

    scored: list[ScoredWindow] = []
    for index, window in enumerate(candidates):
        if (window.start_date, window.end_date) in excluded:
            continue
        result = score_window(
            window,
            calendar=calendar,
            constraints=constraints,
            today=today,
            rank_index=index,
        )
        if result.eligible:
            scored.append(result)

    # Best score first; ties break on the earlier start.
    scored.sort(key=lambda item: (-item.score, item.start_date))
    return scored[:limit]


def summarise(windows: Sequence[ScoredWindow]) -> dict | None:
    """Convenience wrapper returning the top window as a dict."""
    if not windows:
        return None
    return windows[0].as_suggestion(rank=1)
