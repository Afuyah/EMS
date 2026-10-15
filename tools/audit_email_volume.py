"""Audit: exactly how many emails does one ordinary action generate?

The user asked for important mail only and no inbox clogging, so the volume per
person per action is the thing worth measuring rather than assuming.
"""

from __future__ import annotations

import os
import sys
from collections import Counter

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
    from datetime import date, timedelta

    from app.features.email.models import EmailLog
    from app.features.leave.constants import LeaveType
    from app.features.leave.service import LeaveService

    EmailLog.query.delete()
    db.session.commit()

    service = LeaveService()
    # Alice = employee 1.
    start = date.today() + timedelta(weeks=12)
    while start.weekday() != 0:
        start += timedelta(days=1)

    print("=== one annual leave request ===")
    request = service.request_leave(
        employee_id=1,
        leave_type=LeaveType.ANNUAL,
        start_date=start,
        end_date=start + timedelta(days=4),
        days=5,
    )
    db.session.commit()

    rows = EmailLog.query.order_by(EmailLog.id).all()
    print(f"  emails generated: {len(rows)}")
    for row in rows:
        print(f"    {row.event_type:<22} -> {row.to_email}")
    by_recipient = Counter(row.to_email for row in rows)
    print(f"  per recipient: {dict(by_recipient)}")

    # The important property: no single person is spammed by one action.
    worst_recipient = max(by_recipient.values(), default=0)
    check("no recipient gets more than one email per action",
          worst_recipient <= 1, f"worst={worst_recipient} {dict(by_recipient)}")
    check("the employee is acknowledged exactly once",
          by_recipient.get("alice@ems.local", 0) == 1,
          str(dict(by_recipient)))

    # Clean up.
    EmailLog.query.delete()
    from app.features.leave.models import LeaveRequest

    db.session.delete(db.session.get(LeaveRequest, request.id))
    db.session.flush()
    balance = service.balances.get_scope(
        employee_id=1, year=start.year, leave_type=LeaveType.ANNUAL.value
    )
    if balance is not None:
        balance.pending = max(0, balance.pending - request.days)
    db.session.commit()

    print("\n=== which events produce email at all ===")
    from app.features.email import constants as email_constants

    events = sorted(
        value for name, value in vars(email_constants).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    )
    import pathlib

    template_dir = pathlib.Path("app/features/email/templates")
    have_template = {p.stem for p in template_dir.glob("*.txt")}
    for event in events:
        mark = "template" if event in have_template else "IN-APP ONLY"
        print(f"    {event:<24} {mark}")

    print(f"\n  {len(have_template)} email templates, {len(events)} declared events")

print(f"\n=== RESULT: {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
