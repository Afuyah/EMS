"""Timezone helpers.

Rule: all timestamps are stored in UTC; conversion to an employee's local time
happens only at the edges (views, report formatting, notification rendering).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

FALLBACK_TZ = "Africa/Nairobi"


def utcnow() -> datetime:
    """Timezone-aware current UTC time."""
    return datetime.now(tz=UTC)


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; convert aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def get_zone(tz_name: str | None) -> ZoneInfo:
    """Resolve an IANA name, falling back rather than exploding."""
    for candidate in (tz_name, FALLBACK_TZ):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            continue
    return ZoneInfo("UTC")


def to_local(value: datetime, tz_name: str | None) -> datetime:
    """Convert a stored UTC timestamp into the employee's local time."""
    return ensure_utc(value).astimezone(get_zone(tz_name))


def local_date(value: datetime, tz_name: str | None) -> date:
    """The employee-local calendar date for a UTC timestamp."""
    return to_local(value, tz_name).date()


def today_local(tz_name: str | None) -> date:
    """Today's date in the employee's timezone."""
    return utcnow().astimezone(get_zone(tz_name)).date()


def local_noon_utc(day: date, tz_name: str | None) -> datetime:
    """Midday of ``day`` in local time, expressed in UTC.

    Used when a local calendar date must be anchored onto the UTC timeline
    without drifting across DST or day boundaries.
    """
    zone = get_zone(tz_name)
    return datetime.combine(day, time(12, 0), tzinfo=zone).astimezone(UTC)


def shift_bounds(
    work_date: date,
    shift_start: time,
    shift_end: time,
    tz_name: str | None,
) -> tuple[datetime, datetime]:
    """Return the UTC (start, end) instants of a shift on a local work date.

    Handles overnight shifts (``shift_end <= shift_start``) by rolling the end
    into the following day.
    """
    zone = get_zone(tz_name)
    start = datetime.combine(work_date, shift_start, tzinfo=zone)
    end = datetime.combine(work_date, shift_end, tzinfo=zone)
    if end <= start:
        end += timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def minutes_between(later: datetime, earlier: datetime) -> int:
    """Whole minutes from ``earlier`` to ``later``, floored at zero."""
    delta = ensure_utc(later) - ensure_utc(earlier)
    return max(0, int(delta.total_seconds() // 60))


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end``."""
    if end < start:
        return []
    span = (end - start).days
    return [start + timedelta(days=offset) for offset in range(span + 1)]


def each_day(start: date, end: date):
    """Generator form of :func:`date_range` for large spans."""
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def is_weekend(day: date) -> bool:
    """Saturday or Sunday."""
    return day.weekday() >= 5


def month_bounds(day: date) -> tuple[date, date]:
    """First and last day of the month containing ``day``."""
    first = day.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first, nxt - timedelta(days=1)


def quarter_bounds(day: date) -> tuple[date, date]:
    """First and last day of the quarter containing ``day``."""
    first_month = ((day.month - 1) // 3) * 3 + 1
    start = date(day.year, first_month, 1)
    if first_month + 3 > 12:
        nxt = date(day.year + 1, 1, 1)
    else:
        nxt = date(day.year, first_month + 3, 1)
    return start, nxt - timedelta(days=1)


def week_bounds(day: date) -> tuple[date, date]:
    """Monday..Sunday window containing ``day``."""
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def year_bounds(year: int) -> tuple[date, date]:
    """Jan 1 .. Dec 31 for ``year``."""
    return date(year, 1, 1), date(year, 12, 31)
