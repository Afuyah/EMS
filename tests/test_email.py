"""Tests for the outbound email pipeline."""

from __future__ import annotations

import pytest

from app.core.database import db
from app.features.email.constants import EmailStatus
from app.features.email.exceptions import MissingRecipientError
from app.features.email.models import EmailLog
from app.features.email.senders import (
    ConsoleTransport,
    NullTransport,
    OutboundMessage,
    get_transport,
)
from app.features.email.service import EmailService, render_email

pytestmark = pytest.mark.integration


class TestTransports:
    def test_get_transport_resolves_each_backend(self):
        assert isinstance(get_transport("console"), ConsoleTransport)
        assert isinstance(get_transport("null"), NullTransport)

    def test_unknown_backend_is_rejected(self):
        from app.features.email.exceptions import EmailBackendError

        with pytest.raises(EmailBackendError):
            get_transport("carrier-pigeon")

    def test_null_transport_delivers_without_side_effects(self):
        NullTransport().deliver(
            OutboundMessage(to_email="a@b.com", subject="x", body_text="y")
        )

    def test_cc_list_parses_comma_separated(self):
        message = OutboundMessage(
            to_email="a@b.com", subject="x", cc_email=" c@d.com , e@f.com "
        )
        assert message.cc_list == ["c@d.com", "e@f.com"]

    def test_cc_list_empty_when_unset(self):
        assert OutboundMessage(to_email="a@b.com", subject="x").cc_list == []


class TestEmailRendering:
    def test_leave_requested_template_renders_subject_and_body(self, app_context):
        subject, body, _html = render_email(
            "leave_requested",
            {
                "employee_name": "Alice Njeri",
                "leave_type": "annual",
                "request_id": 7,
                "start_date": "Mon 01 Jun 2026",
                "end_date": "Wed 03 Jun 2026",
                "days": 3,
                "status": "suggested",
                "link": "http://localhost/app/leave",
            },
        )
        # The subject comes from the template's `{% set subject %}` line rather
        # than being derived from the first body line.
        assert "leave request" in subject.lower()
        assert subject.startswith("[EMS]")
        assert "Reference : #7" in body
        assert "Mon 01 Jun 2026" in body
        # The greeting belongs to the body, not the subject.
        assert body.startswith("Hello Alice Njeri,")

    def test_body_is_dedented(self, app_context):
        _subject, body, _html = render_email(
            "leave_approved",
            {
                "employee_name": "Alice", "leave_type": "annual", "request_id": 1,
                "start_date": "Mon", "end_date": "Tue", "days": 2,
                "decided_by": "HR", "note": "", "link": "x",
            },
        )
        # No line should carry the template's four-space content indent.
        assert not any(line.startswith("    ") for line in body.splitlines())

    def test_unknown_event_renders_nothing(self, app_context):
        assert render_email("does_not_exist", {}) == ("", "", "")

    def test_hr_template_names_the_employee(self, app_context):
        subject, body, _html = render_email(
            "leave_submitted_hr",
            {
                "employee_name": "Alice Njeri", "employee_no": "EMP-0001",
                "department_name": "Workshop", "leave_type": "annual",
                "request_id": 5, "start_date": "Mon", "end_date": "Tue",
                "days": 2, "reason": "Family", "link": "x",
            },
        )
        assert "Alice Njeri" in subject
        assert "Action needed" in subject or "action" in subject.lower()
        assert "EMP-0001" in body
        assert "Family" in body


class TestQueueAndDelivery:
    def test_queue_creates_a_pending_row(self, clean_db, app_context):
        service = EmailService(transport=NullTransport())
        record = service.queue(
            to_email="a@b.com", subject="Hi", body_text="Body", event_type="probe"
        )
        db.session.commit()

        assert record.id is not None
        assert record.status == EmailStatus.PENDING.value
        assert record.attempts == 0

    def test_invalid_address_is_rejected(self, clean_db, app_context):
        service = EmailService(transport=NullTransport())
        with pytest.raises(MissingRecipientError):
            service.queue(to_email="not-an-address", subject="x", body_text="y")

    def test_send_marks_sent_and_records_backend(self, clean_db, app_context):
        service = EmailService(transport=NullTransport())
        record = service.send(
            to_email="a@b.com", subject="Hi", body_text="Body", event_type="probe"
        )
        assert record.status == EmailStatus.SENT.value
        assert record.attempts == 1
        assert record.sent_at is not None

    def test_send_returns_none_when_disabled(self, clean_db, app_context, app):
        app.config["EMAIL_ENABLED"] = False
        service = EmailService(transport=NullTransport())
        assert service.send(to_email="a@b.com", subject="x", body_text="y") is None
        assert EmailLog.query.count() == 0

    def test_failed_delivery_is_recorded_not_raised(self, clean_db, app_context):
        """A transport failure becomes data on the row, never an exception."""

        class ExplodingTransport:
            name = "exploding"

            def deliver(self, message):  # noqa: ARG002
                raise RuntimeError("smtp is on fire")

        service = EmailService(transport=ExplodingTransport())
        record = service.send(
            to_email="a@b.com", subject="Hi", body_text="Body", event_type="probe"
        )
        assert record.status == EmailStatus.FAILED.value
        assert "smtp is on fire" in (record.last_error or "")
        assert record.attempts == 1

    def test_send_safely_never_raises(self, clean_db, app_context):
        class ExplodingService(EmailService):
            def send(self, **kwargs):  # noqa: ARG002
                raise RuntimeError("catastrophe")

        result = ExplodingService(transport=NullTransport()).send_safely(
            to_email="a@b.com", subject="x", body_text="y"
        )
        assert result is None

    def test_retry_reattempts_pending_rows(self, clean_db, app_context):
        # Queue without delivering, leaving the row pending.
        queue_service = EmailService(transport=NullTransport())
        queue_service.queue(
            to_email="a@b.com", subject="Hi", body_text="Body", event_type="probe"
        )
        db.session.commit()
        assert EmailLog.query.filter_by(status="pending").count() == 1

        summary = EmailService(transport=NullTransport()).retry_pending(
            min_age_seconds=0
        )
        assert summary["attempted"] == 1
        assert summary["sent"] == 1
        assert EmailLog.query.filter_by(status="pending").count() == 0

    def test_retry_gives_up_after_max_attempts(self, clean_db, app_context, app):
        app.config["EMAIL_MAX_ATTEMPTS"] = 2

        class ExplodingTransport:
            name = "exploding"

            def deliver(self, message):  # noqa: ARG002
                raise RuntimeError("always fails")

        service = EmailService(transport=ExplodingTransport())
        service.queue(
            to_email="a@b.com", subject="Hi", body_text="Body", event_type="probe"
        )
        db.session.commit()

        for _ in range(4):
            service.retry_pending(min_age_seconds=0)

        # Never retried forever: it is parked as failed.
        assert EmailLog.query.filter_by(status="pending").count() == 0
        assert EmailLog.query.filter_by(status="failed").count() == 1

    def test_delivery_survives_commit_before_send(self, clean_db, app_context):
        """The row is committed before the transport runs, so a crash cannot
        lose the intent to send."""
        seen: list[int] = []

        class InspectingTransport:
            name = "inspecting"

            def deliver(self, message):
                # The row must already be durable at this point.
                seen.append(EmailLog.query.filter_by(status="pending").count())

        EmailService(transport=InspectingTransport()).send(
            to_email="a@b.com", subject="x", body_text="y", event_type="probe"
        )
        assert seen and seen[0] >= 1


class TestStats:
    def test_stats_report_the_configured_backend(self, clean_db, app_context, app):
        app.config["EMAIL_BACKEND"] = "console"
        stats = EmailService().stats()
        assert stats["backend"] == "console"
        assert stats["enabled"] is True
        assert stats["total"] == 0

    def test_stats_count_by_status(self, clean_db, app_context):
        service = EmailService(transport=NullTransport())
        service.send(to_email="a@b.com", subject="1", body_text="x", event_type="probe")
        service.send(to_email="c@d.com", subject="2", body_text="y", event_type="probe")

        stats = EmailService().stats()
        assert stats["sent"] == 2
        assert stats["total"] == 2
