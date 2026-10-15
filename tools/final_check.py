"""Final consolidated verification of everything built in this session."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_CONFIG"] = "development"

from app import create_app
from app.core.database import db

app = create_app("development")
failures: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        failures.append(label)


with app.app_context():
    from app.features.attendance.models import AttendancePolicy
    from app.features.email.models import EmailLog
    from app.features.leave.models import LeavePlan
    from app.features.rules.models import LeaveRule

    print("=== structure ===")
    check("email_logs table registered", "email_logs" in db.metadata.tables)
    check("leave_plans table registered", "leave_plans" in db.metadata.tables)
    check("leave_plan_items table registered", "leave_plan_items" in db.metadata.tables)

    print("\n=== shift/lateness policy ===")
    policy = AttendancePolicy.query.filter_by(department_id=None).first()
    check("a global policy exists", policy is not None)
    if policy:
        check("it has a very-late threshold", policy.very_late_after_minutes is not None,
              str(policy.very_late_after_minutes))
        check("it exposes the cutoff label", bool(policy.very_late_cutoff_label))

    print("\n=== department quota ===")
    annual = LeaveRule.query.filter_by(leave_type="annual", department_id=None).first()
    check("annual leave is capped at 1 per department",
          annual is not None and annual.max_employees_off_per_dept == 1,
          str(annual.max_employees_off_per_dept if annual else None))

    print("\n=== published rosters ===")
    published = LeavePlan.query.filter_by(status="published").all()
    check("at least one published roster exists", len(published) >= 1, f"{len(published)}")
    for plan in published:
        placed = plan.placed_count
        print(f"    plan {plan.id}: dept={plan.department_id} placed={placed} "
              f"unplaced={plan.unplaced_count} limit={plan.concurrency_limit}")

    print("\n=== jobs registered ===")
    from app.jobs import JOB_REGISTRY

    for name in ["color_recompute", "disciplinary_evaluator", "email_dispatch",
                 "nightly_attendance", "allocate_balances"]:
        check(f"job {name} registered", name in JOB_REGISTRY)

    print("\n=== pages render for each role ===")
    PAGES = [
        "/app/dashboard", "/app/attendance", "/app/employees", "/app/leave",
        "/app/leave/plan", "/app/leave/admin", "/app/rules",
        "/app/attendance/policies", "/app/profile", "/app/notifications",
        "/app/reports", "/app/devices",
    ]
    client = app.test_client()
    for email, password in [
        ("admin@ems.local", "ChangeMe123!"),
        ("alice@ems.local", "Password123!"),
    ]:
        login = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if login.status_code != 200:
            failures.append(f"login {email}")
            print(f"  [FAIL] login {email}")
            continue
        label = email.split("@")[0]
        bad = []
        for path in PAGES:
            response = client.get(path)
            # 403/302 are legitimate for a role that may not see a page.
            if response.status_code not in (200, 302, 403):
                bad.append(f"{path}={response.status_code}")
        check(f"{label}: all {len(PAGES)} pages answer cleanly", not bad, ", ".join(bad))
        client.get("/app/logout")

    print("\n=== email is non-destructive ===")
    check("no leftover verification emails", EmailLog.query.count() == 0,
          f"{EmailLog.query.count()} row(s)")

    print("\n=== templates render each block exactly once ===")
    # The card macro used to invoke caller() twice, which duplicated every form
    # and panel passed to it.  These guards make that regression impossible.
    import re as _re
    from collections import Counter as _Counter

    for path in ["/app/dashboard", "/app/leave/plan", "/app/leave/admin",
                 "/app/leave/request", "/app/attendance/policies", "/app/leave"]:
        response = client.get(path)
        if response.status_code != 200:
            continue
        html = response.get_data(as_text=True)
        ids = _re.findall(r'\sid="([^"]+)"', html)
        dupes = {k: v for k, v in _Counter(ids).items() if v > 1}
        check(f"{path}: no duplicated element ids", not dupes, str(dupes))

        start, end = html.find("<main"), html.find("</main>")
        body = html[start:end] if start >= 0 and end > start else html
        body = _re.sub(r"<!--.*?-->", "", body, flags=_re.S)
        headings = len(_re.findall(r"<h1", body))
        check(f"{path}: exactly one heading", headings == 1, f"{headings}")

print(f"\n=== RESULT: {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
