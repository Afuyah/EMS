"""Verify the Celery app configuration the production deployment depends on."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from celery.schedules import crontab  # noqa: E402
from celery_worker import celery  # noqa: E402

conf = celery.conf

print("=== broker / backend ===")
print(f"  broker configured     : {bool(conf.broker_url)}")
print(f"  result backend set    : {bool(conf.result_backend)}")
print(f"  broker retry on start : {conf.broker_connection_retry_on_startup}")

print("\n=== scheduling ===")
print(f"  timezone              : {conf.timezone}")
print(f"  enable_utc            : {conf.enable_utc}")
print(f"  beat entries          : {len(conf.beat_schedule)}")

print("\n=== the nightly schedule ===")
failures: list[str] = []


def _crontab_field(value) -> str:
    """Celery stores these as sets; render them readably."""
    if value is None:
        return "*"
    if isinstance(value, (set, list, tuple)):
        return ",".join(str(v) for v in sorted(value))
    return str(value)


expected_nightly = None
for name, entry in sorted(conf.beat_schedule.items()):
    schedule = entry["schedule"]
    if isinstance(schedule, crontab):
        when = f"{_crontab_field(schedule.hour)}:{_crontab_field(schedule.minute)}"
    else:
        when = f"every {schedule}s"
    print(f"  {name:<24} {when:<14} -> {entry['task']}")
    if name == "nightly-attendance":
        expected_nightly = (_crontab_field(schedule.hour), _crontab_field(schedule.minute))

print("\n=== the sweep is scheduled for the small hours ===")
if expected_nightly is None:
    failures.append("nightly-attendance is not in the beat schedule")
else:
    hour, minute = expected_nightly
    ok = hour == "2" and minute == "0"
    print(f"  [{'ok  ' if ok else 'FAIL'}] nightly-attendance at {hour}:{minute} "
          f"({conf.timezone})")
    if not ok:
        failures.append(f"nightly-attendance is at {hour}:{minute}, expected 2:0")

print("\n=== task registration ===")
jobs = sorted(t for t in celery.tasks if t.startswith("app.jobs"))
print(f"  registered app.jobs tasks: {len(jobs)}")
if len(jobs) < 7:
    failures.append(f"only {len(jobs)} app.jobs tasks registered, expected 7")
print(f"  base task class: {celery.Task.__name__}")

print(f"\n=== RESULT: {len(failures)} problem(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
