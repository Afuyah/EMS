"""End-to-end: a split shift must be persisted and fully credited."""

from __future__ import annotations

import os
import sys
from datetime import datetime, time, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


with app.app_context():
    from app.features.attendance.models import AttendanceDaily
    from app.features.attendance.service import AttendanceService
    from app.features.biometrics.constants import PunchType
    from app.features.employees.models import Employee

    employee = Employee.query.filter_by(is_active=True).first()
    service = AttendanceService()
    day = datetime(2026, 6, 1)  # a Monday, outside any existing test data

    # Punches are wall-clock times in the employee's own timezone, which is what
    # the device reports. Building naive datetimes would make ensure_utc read
    # them as UTC and shift every result by the offset.
    from zoneinfo import ZoneInfo

    LOCAL = ZoneInfo(employee.tz)

    def local(hour, minute=0, base=None):
        moment = (base or day) + timedelta(hours=hour, minutes=minute)
        return moment.replace(tzinfo=LOCAL)

    # Start clean.
    AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day.date()
    ).delete()
    db.session.commit()

    def punch(hour, kind=PunchType.AUTO, minute=0, base=None):
        service.apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=local(hour, minute, base),
            punch_type=kind,
        )
        db.session.commit()
        row = AttendanceDaily.query.filter_by(
            employee_id=employee.id,
            work_date=(base or day).date(),
        ).first()
        return row

    print(f"employee: {employee.full_name}  tz={employee.tz}")
    print(f"day: {day.date()}\n")

    def hour_in_local(moment):
        """The wall-clock hour, which is what a person would recognise.

        Stored timestamps are UTC, so comparing their raw hour against the time
        someone punched would be wrong by the timezone offset.
        """
        return moment.astimezone(LOCAL).hour if moment else None

    print("=== punch 07:00 (clock in) ===")
    row = punch(7)
    check("a row was created", row is not None)
    check("one session recorded", len(row.sessions or []) == 1, str(row.sessions))
    check("the session is open", (row.sessions or [{}])[0].get("out") is None)
    check("clock_in is 07:00 local", hour_in_local(row.clock_in) == 7,
          str(row.clock_in))
    check("no clock_out yet", row.clock_out is None)
    print(f"    status={row.status} late={row.late_minutes} worked={row.worked_minutes}")

    print("\n=== punch 11:00 (clock out) ===")
    row = punch(11)
    check("still one session", len(row.sessions or []) == 1, str(row.sessions))
    check("the session is closed", (row.sessions or [{}])[0].get("out") is not None)
    check("worked is 240 minutes", row.worked_minutes == 240, str(row.worked_minutes))
    check("clock_out is 11:00 local", hour_in_local(row.clock_out) == 11,
          str(row.clock_out))

    print("\n=== punch 15:00 (back again) ===")
    row = punch(15)
    check("a SECOND session was opened", len(row.sessions or []) == 2, str(row.sessions))
    check("the new session is open", (row.sessions or [{}, {}])[1].get("out") is None)
    check("the first stint is still counted", row.worked_minutes == 240, str(row.worked_minutes))
    check("clock_in is still the morning arrival", hour_in_local(row.clock_in) == 7,
          str(row.clock_in))
    check("clock_out is cleared while in", row.clock_out is None, str(row.clock_out))

    print("\n=== punch 17:00 (clock out again) ===")
    row = punch(17)
    check("two closed sessions", len(row.sessions or []) == 2, str(row.sessions))
    check("both stints are open=False",
          all(s.get("out") for s in (row.sessions or [])))
    expected = 4 * 60 + 2 * 60
    check(f"worked is {expected} minutes (4h + 2h)", row.worked_minutes == expected,
          str(row.worked_minutes))
    check("clock_out is the latest departure", hour_in_local(row.clock_out) == 17,
          str(row.clock_out))
    check("clock_in is the first arrival", hour_in_local(row.clock_in) == 7,
          str(row.clock_in))
    print(f"    status={row.status} late={row.late_minutes} worked={row.worked_minutes}")
    for index, session in enumerate(row.sessions, 1):
        start = datetime.fromisoformat(session["in"]).astimezone(LOCAL)
        end = datetime.fromisoformat(session["out"]).astimezone(LOCAL)
        print(f"    session {index}: {start:%H:%M} -> {end:%H:%M} local")

    print("\n=== a duplicate scan must not add a session ===")
    row = punch(17)
    check("still two sessions", len(row.sessions or []) == 2, str(row.sessions))

    print("\n=== an early arrival is not late ===")
    day2 = day + timedelta(days=1)
    AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day2.date()
    ).delete()
    db.session.commit()
    punch(8, minute=55, base=day2)
    row2 = AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day2.date()
    ).first()
    check("an 08:55 arrival is not late", row2.late_minutes == 0, str(row2.late_minutes))
    check("status is not LATE", "late" not in row2.status.lower(), row2.status)

    print("\n=== a genuinely late arrival IS late ===")
    day3 = day + timedelta(days=2)
    AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day3.date()
    ).delete()
    db.session.commit()
    punch(9, minute=40, base=day3)
    row3 = AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day3.date()
    ).first()
    check("a 09:40 arrival is late", row3.late_minutes > 0, str(row3.late_minutes))
    check("the lateness is about 20 minutes past grace",
          row3.late_minutes == 20, str(row3.late_minutes))

    print("\n=== reading a legacy row with no session list still works ===")
    row2.sessions = None
    db.session.commit()
    punch(17, base=day2)
    row2 = AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=day2.date()
    ).first()
    check("a NULL session list is rebuilt from the scalar columns",
          row2.clock_out is not None and len(row2.sessions or []) == 1,
          str(row2.sessions))

    print("\n=== cleanup ===")
    AttendanceDaily.query.filter(
        AttendanceDaily.employee_id == employee.id,
        AttendanceDaily.work_date.in_([day.date(), day2.date(), day3.date()]),
    ).delete()
    db.session.commit()
    print("    removed the rows this check created")

print("\n" + "=" * 68)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 68)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
