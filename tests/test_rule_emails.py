"""Tests for the colour-escalation and disciplinary action emails."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.database import db
from app.features.email.constants import (
    EVENT_COLOR_ESCALATED,
    EVENT_DISCIPLINARY_ACTION,
)
from app.features.email.models import EmailLog
from app.features.email.senders import NullTransport
from app.features.email.service import EmailService

pytestmark = pytest.mark.integration


class TestColorEscalationEmail:
    def test_template_renders_for_every_colour(self, app_context):
        from app.features.email.service import render_email

        for colour, label in [
            ("yellow", "Yellow"),
            ("orange", "Orange"),
            ("red", "Red"),
        ]:
            subject, body, _html = render_email(
                EVENT_COLOR_ESCALATED,
                {
                    "employee_name": "Alice Njeri",
                    "color": colour,
                    "color_label": label,
                    "lates": 4,
                    "absences": 1,
                    "window_days": 30,
                    "link": "http://localhost/app/dashboard",
                },
            )
            assert label in subject
            assert "Alice Njeri" in body
            # The wording must match the severity, not be generic.
            if colour == "red":
                assert "most serious" in body
            elif colour == "orange":
                assert "formal warning" in body
            else:
                assert "early warning" in body

    def test_escalation_email_is_sent(self, clean_db, app_context, app, employee):
        """The job's email helper writes a delivered row."""
        from app.jobs.color_recompute import _email_escalation

        app.config["EMAIL_ENABLED"] = True
        # Point the service at the null transport for the duration of the test.
        import app.features.email.service as email_service_module

        original = email_service_module.EmailService

        class NullBacked(original):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", NullTransport())
                super().__init__(*args, **kwargs)

        email_service_module.EmailService = NullBacked
        try:
            class _Outcome:
                class color:  # noqa: N801 - mimics the engine's shape
                    value = "orange"
                    label = "Orange"
                lates = 5
                absences = 2

            _email_escalation(employee, _Outcome(), window=30)
            db.session.commit()
        finally:
            email_service_module.EmailService = original

        rows = EmailLog.query.filter_by(event_type=EVENT_COLOR_ESCALATED).all()
        assert len(rows) == 1, f"expected one escalation email, got {len(rows)}"
        assert rows[0].to_email == employee.email
        assert "Orange" in rows[0].subject

    def test_escalation_email_contained_when_mail_fails(
        self, clean_db, app_context, app, employee
    ):
        """A broken mail server must never break the colour job."""
        from app.jobs.color_recompute import _email_escalation

        app.config["EMAIL_ENABLED"] = True
        app.config["EMAIL_BACKEND"] = "smtp"
        app.config["SMTP_HOST"] = "127.0.0.1"
        app.config["SMTP_PORT"] = 9          # discard port: refuses connections
        app.config["SMTP_TIMEOUT"] = 2

        class _Outcome:
            class color:  # noqa: N801
                value = "red"
                label = "Red"
            lates = 12
            absences = 4

        # Must not raise.
        _email_escalation(employee, _Outcome(), window=30)
        db.session.commit()

        rows = EmailLog.query.filter_by(event_type=EVENT_COLOR_ESCALATED).all()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].last_error


class TestDisciplinaryEmail:
    def test_template_renders_applied_and_recommended(self, app_context):
        from app.features.email.service import render_email

        base = {
            "employee_name": "Brian Otieno",
            "employee_no": "EMP-0002",
            "action_label": "Written Warning",
            "action_id": 7,
            "trigger_type": "late",
            "observed": 6,
            "threshold": 6,
            "period": "month",
            "suspension_days": 0,
            "triggered_on": "12 Oct 2026",
            "link": "http://localhost/app/profile",
        }

        subject, body, _ = render_email(
            EVENT_DISCIPLINARY_ACTION, {**base, "applied": True}
        )
        assert "Written Warning" in subject
        assert "has been recorded" in body
        assert "#7" in body
        assert "raise it with HR" in body

        subject2, body2, _ = render_email(
            EVENT_DISCIPLINARY_ACTION,
            {**base, "applied": False, "action_id": "pending"},
        )
        assert "recommended" in body2
        assert "HR will review" in body2
        # A recommendation has no action row, so no reference number is claimed.
        assert "Reference" not in body2
        assert "#pending" not in body2

    def test_suspension_days_render_when_present(self, app_context):
        from app.features.email.service import render_email

        _subject, body, _ = render_email(
            EVENT_DISCIPLINARY_ACTION,
            {
                "employee_name": "Brian", "employee_no": "EMP-0002",
                "action_label": "Suspension", "action_id": 9,
                "trigger_type": "absent", "observed": 3, "threshold": 3,
                "period": "month", "suspension_days": 2,
                "triggered_on": "12 Oct 2026", "applied": True,
                "link": "x",
            },
        )
        assert "Suspension" in body
        assert "2 day(s)" in body

    def test_disciplinary_email_contained_when_mail_fails(
        self, clean_db, app_context, app, employee
    ):
        """The action row is the record of truth; mail failure is not fatal."""
        from datetime import date as _date

        from app.jobs.disciplinary_evaluator import _email_disciplinary

        app.config["EMAIL_ENABLED"] = True
        app.config["EMAIL_BACKEND"] = "smtp"
        app.config["SMTP_HOST"] = "127.0.0.1"
        app.config["SMTP_PORT"] = 9
        app.config["SMTP_TIMEOUT"] = 2

        class _Rule:
            trigger_type = "late"
            threshold = 3
            period = "month"
            suspension_days = 0

        class _Match:
            rule = _Rule()
            observed = 4
            action = "warning"

        # Must not raise.
        _email_disciplinary(
            employee, _Match(), applied=True, triggered_on=_date.today(), action_id=1
        )
        db.session.commit()

        rows = EmailLog.query.filter_by(event_type=EVENT_DISCIPLINARY_ACTION).all()
        assert len(rows) == 1
        assert rows[0].status == "failed"

    def test_employee_without_an_email_is_skipped(self, clean_db, app_context, employee):
        from app.jobs.disciplinary_evaluator import _email_disciplinary

        # Creating the employee also sends a welcome email, so count the event
        # under test rather than every row in the table.
        before = EmailLog.query.filter_by(event_type=EVENT_DISCIPLINARY_ACTION).count()

        employee.email = ""
        db.session.commit()

        class _Rule:
            trigger_type = "late"
            threshold = 3
            period = "month"
            suspension_days = 0

        class _Match:
            rule = _Rule()
            observed = 4
            action = "warning"

        _email_disciplinary(employee, _Match(), applied=True)
        db.session.commit()

        after = EmailLog.query.filter_by(event_type=EVENT_DISCIPLINARY_ACTION).count()
        assert after == before, "no disciplinary email should be sent without an address"
