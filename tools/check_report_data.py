"""Prove the attendance report renders real rows, using temporary records."""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, time as clock, timedelta, timezone

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
    from app.features.attendance.constants import AttendanceStatus
    from app.features.attendance.models import AttendanceDaily
    from app.features.employees.models import Employee

    # Two days inside the default 30-day window.
    today = date.today()
    days = [today - timedelta(days=2), today - timedelta(days=1)]
    employees = Employee.query.filter_by(is_active=True).limit(4).all()

    # Skip any (employee, day) that already has a row: there is a unique
    # constraint on that pair, and real data must not be overwritten by a check.
    existing = {
        (row.employee_id, row.work_date)
        for row in AttendanceDaily.query.filter(
            AttendanceDaily.work_date.in_(days)
        ).all()
    }
    print(
        f"seeding attendance for {len(employees)} employees over {len(days)} days "
        f"({len(existing)} pair(s) already present, will be skipped)"
    )

    created_ids: list[int] = []
    for offset, employee in enumerate(employees):
        for day_offset, work_date in enumerate(days):
            if (employee.id, work_date) in existing:
                continue
            # Alternate present / late so the report has something to show.
            is_late = (offset + day_offset) % 2 == 0
            record = AttendanceDaily(
                employee_id=employee.id,
                work_date=work_date,
                clock_in=datetime.combine(
                    work_date, clock(9, 0), tzinfo=timezone.utc
                ),
                clock_out=datetime.combine(
                    work_date, clock(17, 0), tzinfo=timezone.utc
                ),
                status=(
                    AttendanceStatus.LATE.value if is_late
                    else AttendanceStatus.PRESENT.value
                ),
                late_minutes=25 if is_late else 0,
                worked_minutes=480,
                notes="report-check",
            )
            db.session.add(record)
            db.session.flush()
            created_ids.append(record.id)
    db.session.commit()
    print(f"  created {len(created_ids)} temporary records")

    try:
        client = app.test_client()
        assert client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "ChangeMe123!"},
        ).status_code == 200

        html = client.get("/app/reports?tab=attendance").get_data(as_text=True)
        rows = re.findall(
            r'<tr class="transition hover:bg-slate-50/60">(.*?)</tr>', html, re.S
        )
        print(f"\n  attendance rows rendered: {len(rows)}")
        for row in rows[:6]:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            clean = [" ".join(re.sub(r"<[^>]+>", " ", c).split()) for c in cells]
            print("    " + " | ".join(clean))

        check("the report renders department rows", len(rows) >= 1,
              "no rows rendered despite data existing")
        check("the total footer is shown", ">Total<" in html)
        check("KPI figures are present", "Days recorded" in html)

        # Late tab should pick up the late records.
        late = client.get("/app/reports?tab=late").get_data(as_text=True)
        late_rows = re.findall(
            r'<tr class="transition hover:bg-slate-50/60">(.*?)</tr>', late, re.S
        )
        print(f"\n  top-late rows rendered: {len(late_rows)}")
        check("the late report renders rows", len(late_rows) >= 1)

        # Headcount tab is per-day.
        hc = client.get("/app/reports?tab=headcount").get_data(as_text=True)
        hc_rows = re.findall(
            r'<tr class="transition hover:bg-slate-50/60">(.*?)</tr>', hc, re.S
        )
        print(f"  headcount rows rendered: {len(hc_rows)}")
        check("the headcount report renders rows", len(hc_rows) >= 1)

        # CSV must contain the seeded data, not just a header.
        csv = client.get("/api/v1/exports/attendance/summary.csv").get_data(as_text=True)
        lines = [line for line in csv.splitlines() if line.strip()]
        print(f"\n  attendance CSV lines: {len(lines)}")
        check("the CSV has a header plus data", len(lines) >= 2, str(lines[:2]))
    finally:
        for record_id in created_ids:
            row = db.session.get(AttendanceDaily, record_id)
            if row is not None:
                db.session.delete(row)
        db.session.commit()
        print("\n  removed the temporary records")

print(f"\n=== RESULT: {len(problems)} problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
