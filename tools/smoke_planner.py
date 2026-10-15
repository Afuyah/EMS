"""Smoke test the annual leave planner (pure engine).

Two scenarios matter and they are different:

* a **tight** year, where remaining capacity is far below demand â€” the planner
  must share what exists fairly rather than filling the most senior people; and
* a **full** year, where everyone should get their whole entitlement spread out.
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_CONFIG"] = "development"

from app.features.leave.engine import PlanEmployee, plan_annual_leave

FAILURES: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" â€” {detail}" if not condition else ""))
    if not condition:
        failures.append(label)


failures = FAILURES

TODAY = date.today()
THIS_YEAR = TODAY.year
NEXT_YEAR = THIS_YEAR + 1


def employees(count, days=21):
    return [
        PlanEmployee(
            employee_id=index,
            full_name=f"Employee {index}",
            employee_no=f"EMP-{index:04d}",
            days=days,
            hired_at=date(2020, 1, 1) + timedelta(days=index * 30),
        )
        for index in range(1, count + 1)
    ]


def working_days_left_in(year: int) -> int:
    """Working days from today (or Jan 1) to 31 Dec of ``year``."""
    start = max(date(year, 1, 1), TODAY)
    total = 0
    day = start
    while day <= date(year, 12, 31):
        if day.weekday() < 5:
            total += 1
        day += timedelta(days=1)
    return total


def concurrency_of(result):
    """Highest number of people off on any single working day."""
    by_day: dict[date, int] = {}
    for entry in result.placed:
        day = entry.start_date
        while day <= entry.end_date:
            if day.weekday() < 5:
                by_day[day] = by_day.get(day, 0) + 1
            day += timedelta(days=1)
    return max(by_day.values(), default=0), by_day


print("=== TIGHT YEAR: capacity far below demand ===")
capacity = working_days_left_in(THIS_YEAR)
demand = 5 * 21
print(f"  {capacity} working day(s) remain in {THIS_YEAR}; demand is {demand} day(s)")

tight = plan_annual_leave(
    department_id=1, year=THIS_YEAR, employees=employees(5), limit=1
)
worst, _ = concurrency_of(tight)
check("cap of 1 is respected", worst <= 1, f"worst={worst}")

placed_people = {entry.employee_id for entry in tight.placed}
check("every employee got at least one block (fairness)",
      placed_people == {1, 2, 3, 4, 5}, f"placed={sorted(placed_people)}")
check("nobody got the whole year while others got nothing",
      len({entry.employee_id for entry in tight.placed}) == 5)
check("the plan reports itself as incomplete", not tight.complete)

total_placed = sum(entry.days for entry in tight.placed)
check("placed days do not exceed real capacity",
      total_placed <= capacity, f"placed={total_placed} capacity={capacity}")

short = [entry for entry in tight.entries if not entry.placed]
check("the shortfall is reported per employee", len(short) == 5, f"short={len(short)}")
if short:
    reason = short[0].reason.lower()
    check("the shortfall explains why", "remain" in reason or "no free window" in reason,
          short[0].reason)

print(f"\n  placed {total_placed} of {demand} requested day(s)")

print("\n=== FULL YEAR: everyone gets their whole entitlement ===")
full = plan_annual_leave(
    department_id=1, year=NEXT_YEAR, employees=employees(5, days=21), limit=1
)
check("a full year places everyone completely", full.complete,
      f"unplaced={len(full.unplaced)}")
worst_full, _ = concurrency_of(full)
check("cap of 1 still respected across the full year", worst_full <= 1,
      f"worst={worst_full}")

totals: dict[int, int] = {}
for entry in full.placed:
    totals[entry.employee_id] = totals.get(entry.employee_id, 0) + entry.days
check("each employee got exactly 21 days",
      all(total == 21 for total in totals.values()), f"totals={sorted(totals.values())}")

starts = sorted(entry.start_date for entry in full.placed)
check("leave spans at least 5 months of the year",
      (starts[-1] - starts[0]).days >= 120, f"spread={(starts[-1] - starts[0]).days} days")
check("at least 5 different months are used",
      len({entry.start_date.month for entry in full.placed}) >= 5,
      f"months={sorted({entry.start_date.month for entry in full.placed})}")
check("long entitlements are split into blocks", len(full.placed) > 5,
      f"rows={len(full.placed)}")
check("no allocation starts on a weekend",
      not [entry for entry in full.placed if entry.start_date.weekday() >= 5])

print("\n=== a higher cap lets a busy department fit ===")
# Demand is chosen so a cap of 1 cannot fit everyone but a cap of 2 can: this
# is what proves the cap is a real, load-bearing limit rather than decoration.
capacity_this = working_days_left_in(THIS_YEAR)
print(f"  {capacity_this} working day(s) remain in {THIS_YEAR}")
crowd = employees(8, days=10)          # 80 working days of demand
one = plan_annual_leave(
    department_id=1, year=THIS_YEAR, employees=crowd, limit=1
)
two = plan_annual_leave(
    department_id=1, year=THIS_YEAR, employees=crowd, limit=2
)
one_worst, _ = concurrency_of(one)
two_worst, _ = concurrency_of(two)
one_days = sum(entry.days for entry in one.placed)
two_days = sum(entry.days for entry in two.placed)

print(f"  cap 1 placed {one_days} day(s); cap 2 placed {two_days} day(s)")
check("cap of 1 is respected", one_worst <= 1, f"worst={one_worst}")
check("cap of 2 is respected", two_worst <= 2, f"worst={two_worst}")
check("a cap of 1 cannot fit this demand in one year", not one.complete,
      "the cap was not binding, so this scenario proves nothing")
check("a cap of 2 fits more of it", two_days > one_days,
      f"cap1={one_days} cap2={two_days}")

print("\n=== no cap means unlimited ===")
uncapped = plan_annual_leave(
    department_id=1, year=NEXT_YEAR, employees=employees(3, days=10), limit=None
)
check("uncapped plan placed everyone", uncapped.complete)
check("uncapped plan reports a real peak", uncapped.peak_concurrent >= 1,
      f"peak={uncapped.peak_concurrent}")

print("\n=== zero entitlement is reported, not invented ===")
mixed = employees(2, days=10) + [
    PlanEmployee(employee_id=99, full_name="No Entitlement",
                 employee_no="EMP-0099", days=0)
]
zero = plan_annual_leave(
    department_id=1, year=NEXT_YEAR, employees=mixed, limit=1
)
unplaced = [entry for entry in zero.entries if not entry.placed]
check("the zero-entitlement employee is not placed", len(unplaced) == 1,
      f"unplaced={len(unplaced)}")
check("and the reason says so", "entitlement" in unplaced[0].reason.lower(),
      unplaced[0].reason)

print("\n=== blackout dates are avoided ===")
blackout_start = date(NEXT_YEAR, 6, 1)
blackout = [blackout_start + timedelta(days=offset) for offset in range(14)]
blocked = plan_annual_leave(
    department_id=1, year=NEXT_YEAR, employees=employees(3, days=10),
    limit=1, blackout_dates=blackout,
)
blocked_set = set(blackout)
violations = 0
for entry in blocked.placed:
    day = entry.start_date
    while day <= entry.end_date:
        if day in blocked_set:
            violations += 1
        day += timedelta(days=1)
check("no leave falls in the blackout window", violations == 0,
      f"violations={violations}")

print("\n=== nothing is allocated outside the planned year ===")
for label, result in (("tight", tight), ("full", full), ("blackout", blocked)):
    outside = [
        entry for entry in result.placed
        if entry.start_date.year != NEXT_YEAR and entry.start_date.year != THIS_YEAR
    ]
    check(f"{label} plan stays inside its year", not outside, f"outside={len(outside)}")

print("\n=== determinism ===")
again = plan_annual_leave(
    department_id=1, year=NEXT_YEAR, employees=employees(5, days=21), limit=1
)
same = [
    (entry.employee_id, entry.start_date, entry.end_date) for entry in full.placed
] == [
    (entry.employee_id, entry.start_date, entry.end_date) for entry in again.placed
]
check("the same input produces the same roster", same)

print(f"\n=== RESULT: {len(FAILURES)} failure(s) ===")
for failure in FAILURES:
    print("  -", failure)
sys.exit(1 if FAILURES else 0)
