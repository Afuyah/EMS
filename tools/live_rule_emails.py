"""Force a real colour escalation and disciplinary action so the email paths run."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

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
    from app.features.email.models import EmailLog
    from app.features.employees.models import Employee

    employee = Employee.query.filter_by(email="alice@ems.local").first()
    print(f"=== target: {employee.full_name} (colour={employee.current_color}) ===")

    before = EmailLog.query.count()

    # ---- colour escalation: call the job's notifier directly with a real
    # outcome, which is the code path that sends the email.
    from app.jobs.color_recompute import _email_escalation

    class Colour:
        value = "orange"
        label = "Orange"

    class Outcome:
        color = Colour()
        lates = 5
        absences = 2
        escalated = True

    _email_escalation(employee, Outcome(), window=30)
    db.session.commit()

    escalation = (
        EmailLog.query.filter_by(event_type="color_escalated")
        .order_by(EmailLog.id.desc())
        .first()
    )
    check("colour escalation email was written", escalation is not None)
    if escalation:
        print(f"    to={escalation.to_email} status={escalation.status}")
        print(f"    subject={escalation.subject}")
        check("it went to the employee", escalation.to_email == employee.email)
        check("it was delivered", escalation.status == "sent", escalation.status)
        check("the body names the level", "Orange" in (escalation.body_text or ""))

    # ---- disciplinary action email, including a suspension so the extra line renders
    from app.jobs.disciplinary_evaluator import _email_disciplinary

    class Rule:
        trigger_type = "late"
        threshold = 10
        period = "quarter"
        suspension_days = 1

    class Match:
        rule = Rule()
        observed = 11
        action = "suspension"

    _email_disciplinary(
        employee, Match(), applied=True, triggered_on=date.today(), action_id=4242
    )
    db.session.commit()

    action_mail = (
        EmailLog.query.filter_by(event_type="disciplinary_action")
        .order_by(EmailLog.id.desc())
        .first()
    )
    check("disciplinary email was written", action_mail is not None)
    if action_mail:
        print(f"    to={action_mail.to_email} status={action_mail.status}")
        print(f"    subject={action_mail.subject}")
        check("it was delivered", action_mail.status == "sent", action_mail.status)
        body = action_mail.body_text or ""
        check("the reference is shown", "#4242" in body)
        check("the suspension is shown", "1 day(s)" in body)
        check("the threshold is explained", "threshold 10" in body)

    after = EmailLog.query.count()
    print(f"\n  email_logs: {before} -> {after}")
    check("both emails were recorded", after >= before + 2, f"{before} -> {after}")

    # Leave the database tidy.
    EmailLog.query.filter(
        EmailLog.event_type.in_(["color_escalated", "disciplinary_action"])
    ).delete(synchronize_session=False)
    db.session.commit()
    print("  cleaned up the demonstration emails")

print(f"\n=== RESULT: {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
