"""Exercise the multi-session punch rules."""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.features.attendance.engine import (  # noqa: E402
    PunchEvent,
    ShiftPolicy,
    resolve_punch,
)
from app.features.biometrics.constants import PunchType  # noqa: E402

PAY = ShiftPolicy(shift_start=time(9, 0), shift_end=time(17, 0), grace_minutes=10)
DAY = datetime(2026, 5, 4)  # a Monday

#: The employee's own timezone. The shift policy is expressed in local time
#: (09:00-17:00) and the engine converts using `tz_name`, so punches must be
#: built as real local instants. Nairobi has no DST, so a fixed offset is exact.
LOCAL = timezone(timedelta(hours=3))
TZ = "Africa/Nairobi"


def at(hour, minute=0):
    """A local wall-clock time, as the aware UTC instant the device would send."""
    return datetime(2026, 5, 4, hour, minute, tzinfo=LOCAL).astimezone(timezone.utc)


def local_str(moment):
    return moment.astimezone(LOCAL).strftime("%H:%M")


def feed(punches, *, policy=PAY):
    sessions = []
    last = None
    for moment in punches:
        last = resolve_punch(
            punch=PunchEvent(punch_time=moment, punch_type=PunchType.AUTO),
            work_date=DAY.date(),
            tz_name=TZ,
            policy=policy,
            existing_sessions=sessions,
        )
        sessions = last.sessions
    return last


def show(label, punches):
    result = feed(punches)
    shape = " | ".join(
        f"{local_str(s.clock_in)}-{local_str(s.clock_out)}" if s.clock_out
        else f"{local_str(s.clock_in)}-open"
        for s in result.sessions
    )
    print(f"  {label}")
    print(f"      punches : {', '.join(local_str(p) for p in punches)} (local)")
    print(f"      sessions: {shape}")
    print(f"      worked  : {result.worked_minutes} min ({result.worked_minutes / 60:.2f} h)"
          f"  status={result.status.value if result.status else None}")
    return result


def wide(label, punches):
    result = feed(punches)
    # The resolved day reports totals; an ignored punch does not alter them, so
    # recompute from the session list to show what is actually stored.
    total = sum(
        int((s.clock_out - s.clock_in).total_seconds() // 60)
        for s in result.sessions
        if s.clock_out
    )
    print(f"  [{label}]  {', '.join(local_str(p) for p in punches)} (local)")
    print(f"      -> {result.action:<9} {result.reason}")
    clock_in = local_str(result.clock_in) if result.clock_in else "-"
    clock_out = local_str(result.clock_out) if result.clock_out else "open"
    print(f"      sessions: {len(result.sessions)}"
          f"  stored_worked={total}min"
          f"  in={clock_in}  out={clock_out}")
    return result


print("=== the scenario you asked about ===")
show("in 07:00, out 11:00, back 15:00, out 17:00",
     [at(7), at(11), at(15), at(17)])
print()

print("=== ordinary single-session day ===")
show("in 08:55, out 17:05", [at(8, 55), at(17, 5)])
print()

print("=== late arrival still counts as one session ===")
show("in 09:40, out 17:00", [at(9, 40), at(17)])
print()

print("=== edge cases ===")
wide("double scan on arrival", [at(9), at(9)])
wide("double scan on departure", [at(9), at(17), at(17)])
wide("stray scan 20 min after leaving", [at(9), at(12), at(12, 20)])
wide("three sessions", [at(7), at(9), at(11), at(13), at(15), at(18)])
wide("punch before the clock-in", [at(9), at(8)])
wide("only a clock-in so far", [at(9)])
wide("clock-out 18h later (not credible)", [at(9), DAY + timedelta(hours=27)])

print("=== lunch break of exactly two hours ===")
wide("out 12:00, back 14:00 (== threshold)", [at(9), at(12), at(14), at(17)])
wide("out 12:00, back 13:59 (just under)", [at(9), at(12), at(13, 59), at(17)])

print("\n=== a full day total across three sessions ===")
r = show("07:00-09:00, 11:00-13:00, 15:00-18:00",
         [at(7), at(9), at(11), at(13), at(15), at(18)])
expected = 2 * 60 + 2 * 60 + 3 * 60
print(f"      expected {expected} min -> {'OK' if r.worked_minutes == expected else 'MISMATCH'}")

# ---------------------------------------------------------------------------
# Assertions, so this participates in the verification sweep rather than only
# printing a table somebody has to read.
# ---------------------------------------------------------------------------
problems: list[str] = []


def expect(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


print("\n=== the rules, asserted ===")

# The scenario the whole feature exists for.
split = feed([at(7), at(11), at(15), at(17)])
expect("a split shift makes two sessions", len(split.sessions) == 2, str(len(split.sessions)))
expect("both stints are credited", split.worked_minutes == 6 * 60, str(split.worked_minutes))
# Compared in local time: storage is UTC, so the raw hour is offset.
expect("first arrival is kept", local_str(split.clock_in) == "07:00",
       local_str(split.clock_in))
expect("latest departure is kept", local_str(split.clock_out) == "17:00",
       local_str(split.clock_out))

three = feed([at(7), at(9), at(11), at(13), at(15), at(18)])
expect("three sessions sum correctly", three.worked_minutes == 7 * 60, str(three.worked_minutes))

single = feed([at(9), at(17)])
expect("an ordinary day is one session", len(single.sessions) == 1)
expect("an ordinary day is eight hours", single.worked_minutes == 8 * 60,
       str(single.worked_minutes))

dup = feed([at(9), at(9)])
expect("a double scan at arrival adds no session", len(dup.sessions) == 1)

dup_out = feed([at(9), at(17), at(17)])
expect("a replayed departure adds no session", len(dup_out.sessions) == 1)

stray = feed([at(9), at(12), at(12, 20)])
expect("a stray scan does not invent a session", len(stray.sessions) == 1)

boundary = feed([at(9), at(12), at(14), at(17)])
expect("exactly the break threshold opens a second session",
       len(boundary.sessions) == 2, str(len(boundary.sessions)))

# Assert the 13:59 punch itself, not the whole day: the day's real last punch
# (17:00) legitimately opens a session of its own.
just_under = feed([at(9), at(12), at(13, 59)])
expect("just under the threshold opens no session",
       len(just_under.sessions) == 1 and not just_under.sessions[0].is_open,
       f"sessions={len(just_under.sessions)} action={just_under.action}")

early = feed([at(5, 55), at(17)])
expect("an early arrival is not late", early.late_minutes == 0, str(early.late_minutes))

late_ok = feed([at(9), at(12), at(15), at(19)])
expect("a late return does not make you late", late_ok.late_minutes == 0,
       str(late_ok.late_minutes))

on_time = feed([at(9, 5), at(17)])
expect("inside the grace period is not late", on_time.late_minutes == 0,
       str(on_time.late_minutes))

actually_late = feed([at(9, 40), at(17)])
expect("past the grace period IS late", actually_late.late_minutes == 30,
       str(actually_late.late_minutes))

print("\n" + "=" * 68)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 68)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
