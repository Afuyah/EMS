"""Verify the leave queue renders ONE working decision form per pending request.

The generic duplication check cannot assert on decision inputs because a queue
with nothing pending correctly shows none, so this creates two pending requests,
renders the queue, and then removes them.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
problems: list[str] = []

with app.app_context():
    from app.features.employees.models import Employee
    from app.features.leave.constants import LeaveType
    from app.features.leave.models import LeaveRequest
    from app.features.leave.service import LeaveService

    service = LeaveService()
    alice = Employee.query.filter_by(email="alice@ems.local").first()
    brian = Employee.query.filter_by(email="brian@ems.local").first()
    or_second = brian or Employee.query.filter(Employee.id != alice.id).first()

    created: list[int] = []
    # Two special-leave requests go straight to PENDING, which is what the HR
    # queue acts on.
    for employee in [alice, or_second]:
        if employee is None:
            continue
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        created.append(request.id)
    db.session.commit()
    print(f"created pending requests: {created}")

    client = app.test_client()
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "admin@ems.local", "password": "ChangeMe123!"},
    ).status_code == 200

    html = client.get("/app/leave/admin").get_data(as_text=True)
    start, end = html.find("<main"), html.find("</main>")
    content = re.sub(
        r"<!--.*?-->", "", html[start:end] if start >= 0 and end > start else html,
        flags=re.S,
    )

    forms = re.findall(r'action="/app/leave/(\d+)/decide"', content)
    approvals = re.findall(r'name="decision" value="approve"', content)
    denials = re.findall(r'name="decision" value="deny"', content)

    print(f"  decide-form actions   : {len(forms)} {forms}")
    print(f"  approve buttons       : {len(approvals)}")
    print(f"  deny buttons          : {len(denials)}")
    print(f"  pending rows          : {len(created)}")

    # Each pending request legitimately renders TWO forms (one approve and one
    # deny) pointing at the same endpoint, so the invariant is one of each per
    # request — not one form in total.
    if len(approvals) != len(created):
        problems.append(f"expected {len(created)} approve buttons, found {len(approvals)}")
    if len(denials) != len(created):
        problems.append(f"expected {len(created)} deny buttons, found {len(denials)}")
    if len(forms) != len(created) * 2:
        problems.append(
            f"expected {len(created) * 2} decide forms, found {len(forms)}"
        )

    # And each request must appear exactly twice (its approve and its deny),
    # never four times as it would if a block were rendered twice.
    for request_id in created:
        occurrences = content.count(f'action="/app/leave/{request_id}/decide"')
        if occurrences != 2:
            problems.append(
                f"request {request_id} renders {occurrences} decide form(s), expected 2"
            )
        print(f"  request {request_id}: {occurrences} decide form(s) (expect 2)")

    # Clean up: remove the requests and release anything they reserved.
    from app.features.leave.models import LeaveBalance

    for request_id in created:
        row = db.session.get(LeaveRequest, request_id)
        if row is not None:
            balance = LeaveBalance.query.filter_by(
                employee_id=row.employee_id,
                year=row.start_date.year,
                leave_type=row.leave_type,
            ).first()
            if balance is not None:
                balance.pending = max(0, balance.pending - row.days)
            db.session.delete(row)
    db.session.commit()
    print("cleaned up the pending requests and their reservations")

print(f"\n=== RESULT: {len(problems)} problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
