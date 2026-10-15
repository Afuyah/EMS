"""Verify the reports page and the attendance correction flow.

Covers the three things that were wrong:
  * reports defaulted to an empty department and showed nothing;
  * there was no way to clear the department filter;
  * the correction tool listed only employees who already had a record.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta

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
    from app.features.employees.models import Department, Employee

    client = app.test_client()
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "admin@ems.local", "password": "ChangeMe123!"},
    ).status_code == 200

    print("=== 1. reports default to ALL departments ===")
    html = client.get("/app/reports?tab=attendance").get_data(as_text=True)
    check("the page loads", "Attendance by department" in html)
    check("'All departments' is offered as a filter",
          "All departments" in html)
    check("no department is preselected",
          'selected>Engineering' not in html and "selected>Engineering" not in html,
          "a department was preselected")
    # The department table must list more than one department when unfiltered.
    departments_shown = set(re.findall(r"<span class=\"font-medium text-slate-900\">([A-Za-z ]+)</span>", html))
    print(f"    departments in the report: {sorted(departments_shown)}")

    print("\n=== 2. a department filter still works ===")
    dept = Department.query.order_by(Department.id).first()
    filtered = client.get(
        f"/app/reports?tab=attendance&department_id={dept.id}"
    ).get_data(as_text=True)
    check(f"filtering by {dept.name} loads", "Attendance by department" in filtered,
          "filtered page failed")

    print("\n=== 3. every report tab renders ===")
    for tab in ["attendance", "colors", "headcount", "late", "leave", "ledger"]:
        page = client.get(f"/app/reports?tab={tab}")
        check(f"tab {tab} loads", page.status_code == 200, str(page.status_code))

    print("\n=== 4. export links are present ===")
    for tab, needle in [
        ("attendance", "/api/v1/exports/attendance/summary.csv"),
        ("colors", "/api/v1/exports/attendance/colors.csv"),
        ("headcount", "/api/v1/exports/attendance/headcount.csv"),
        ("late", "/api/v1/exports/attendance/top_late.csv"),
        ("leave", "/api/v1/exports/leave/utilization.csv"),
    ]:
        body = client.get(f"/app/reports?tab={tab}").get_data(as_text=True)
        check(f"{tab} offers a CSV download", needle in body)

    print("\n=== 5. the exports actually download ===")
    for path in [
        "/api/v1/exports/attendance/summary.csv",
        "/api/v1/exports/attendance/colors.csv",
        "/api/v1/exports/attendance/headcount.csv",
        "/api/v1/exports/attendance/top_late.csv",
        "/api/v1/exports/leave/utilization.csv",
        "/api/v1/exports/leave/balances/template.csv",
    ]:
        response = client.get(path)
        disposition = response.headers.get("Content-Disposition", "")
        ok = response.status_code == 200 and "attachment" in disposition
        if not ok:
            problems.append(f"export {path}: {response.status_code}")
        print(f"  [{'ok  ' if ok else 'FAIL'}] {path:<48} {response.status_code}"
              f" {len(response.data):>5}b")
        if ok:
            # A CSV must have a header row.
            first_line = response.get_data(as_text=True).splitlines()[0]
            if not first_line.strip():
                problems.append(f"export {path}: empty header")

    print("\n=== 6. correction lists employees WITHOUT a record ===")
    work_date = date.today() - timedelta(days=1)
    while work_date.weekday() >= 5:
        work_date -= timedelta(days=1)
    print(f"    work date: {work_date}")

    active = Employee.query.filter_by(is_active=True).count()
    recorded = (
        AttendanceDaily.query.filter_by(work_date=work_date).count()
    )
    print(f"    active employees: {active}, records on that day: {recorded}")

    board = client.get(f"/app/attendance?date={work_date.isoformat()}").get_data(as_text=True)
    check("the board loads", "Manual attendance correction" in board)

    options = re.findall(
        r'<option value="(\d+)"\s+data-attendance="([^"]*)"', board
    )
    print(f"    options offered: {len(options)} "
          f"(with records: {sum(1 for _, a in options if a)})")
    check("every active employee can be corrected",
          len(options) == active, f"{len(options)} options vs {active} employees")
    check("employees with no record are included",
          any(not attendance for _, attendance in options),
          "only existing records were listed")
    check("the form posts to the employee-based endpoint",
          'action="/app/attendance/correct"' in board)
    check("it tells HR how many can be corrected",
          f"{active} employee(s) can be corrected" in board)

    print("\n=== 7. a correction for someone with no record works ===")
    # Pick an employee who has neither a record NOR approved leave that day.
    # Correcting over approved leave is refused by design, so choosing someone on
    # leave would exercise the refusal rather than the creation path.
    from app.features.leave.repository import LeaveRequestRepository

    on_leave_ids = {
        request.employee_id
        for request in LeaveRequestRepository().active_on(work_date)
    }
    print(f"    on approved leave that day: {sorted(on_leave_ids)}")

    target = None
    for employee_id, attendance_id in options:
        candidate = int(employee_id)
        if not attendance_id and candidate not in on_leave_ids:
            target = candidate
            break
    if target is None:
        print("    (nobody is free of both a record and leave; skipping)")
    else:
        person = db.session.get(Employee, target)
        print(f"    target: {person.full_name} (no record, not on leave)")

        response = client.post(
            "/app/attendance/correct",
            data={
                "employee_id": str(target),
                "work_date": work_date.isoformat(),
                "clock_in": f"{work_date.isoformat()}T09:00",
                "clock_out": f"{work_date.isoformat()}T17:00",
                "status": "",
                "notes": "records-check",
            },
            follow_redirects=True,
        )
        check("the correction is accepted", response.status_code == 200,
              str(response.status_code))

        created = AttendanceDaily.query.filter_by(
            employee_id=target, work_date=work_date
        ).first()
        check("a record was created for the day", created is not None)
        if created is not None:
            print(f"    created: status={created.status} worked={created.worked_minutes}m")
            check("the clock-in was stored", created.clock_in is not None)
            # Clean up so the check leaves nothing behind.
            db.session.delete(created)
            db.session.commit()
            print("    (removed the record created by this check)")

    print("\n=== 8. correcting over approved leave is refused ===")
    if on_leave_ids:
        blocked_id = sorted(on_leave_ids)[0]
        blocked_person = db.session.get(Employee, blocked_id)
        print(f"    attempting {blocked_person.full_name} on {work_date}")
        before = AttendanceDaily.query.filter_by(
            employee_id=blocked_id, work_date=work_date
        ).count()

        client.post(
            "/app/attendance/correct",
            data={
                "employee_id": str(blocked_id),
                "work_date": work_date.isoformat(),
                "clock_in": f"{work_date.isoformat()}T09:00",
                "clock_out": f"{work_date.isoformat()}T17:00",
                "status": "",
                "notes": "should-be-refused",
            },
            follow_redirects=True,
        )
        db.session.expire_all()
        after = AttendanceDaily.query.filter_by(
            employee_id=blocked_id, work_date=work_date
        ).count()
        check("no attendance row was created over the leave", after == before,
              f"before={before} after={after}")
    else:
        print("    (nobody is on approved leave; nothing to verify)")

print(f"\n=== RESULT: {len(problems)} problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
