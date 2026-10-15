"""Tests for leave email fan-out and the HR queue filtering fix."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.database import db
from app.features.email.models import EmailLog
from app.features.email.senders import NullTransport
from app.features.email.service import EmailService
from app.features.leave.constants import LeaveStatus, LeaveType
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


@pytest.fixture()
def mail(clean_db, app_context, app):
    """Force the null transport so tests never touch a real mailbox."""
    app.config["EMAIL_ENABLED"] = True
    app.config["EMAIL_MAX_ATTEMPTS"] = 3
    return EmailService(transport=NullTransport())


@pytest.fixture()
def leave_with_mail(clean_db, app_context, employee, leave_rules, attendance_policy, mail):
    return LeaveService(email=mail)


def emails_for(event_type: str) -> list[EmailLog]:
    return list(EmailLog.query.filter_by(event_type=event_type).all())


class TestLeaveEmailFanOut:
    def test_request_emails_employee_and_hr(self, leave_with_mail, employee, hr_user):
        request = leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()

        # The employee gets an acknowledgement.
        ack = EmailLog.query.filter_by(
            event_type="leave_requested", to_email=employee.email
        ).count()
        assert ack == 1, "employee should receive a request acknowledgement"

        # HR gets an actionable alert.
        hr_mail = EmailLog.query.filter_by(
            event_type="leave_submitted_hr", to_email=hr_user.email
        ).count()
        assert hr_mail == 1, "HR should be alerted about the request"
        assert request.id is not None

    def test_hr_email_links_to_the_queue(self, leave_with_mail, employee, hr_user):
        leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()

        alert = EmailLog.query.filter_by(event_type="leave_submitted_hr").first()
        assert "/app/leave/admin" in (alert.body_text or "")

    def test_employee_is_not_emailed_hr_mail_about_themselves(
        self, clean_db, app_context, department, position, leave_rules, attendance_policy, mail
    ):
        """An HR user requesting their own leave must not get the HR alert."""
        from app.features.auth.constants import Role
        from app.features.employees.service import EmployeeService

        record, _ = EmployeeService().create_employee(
            first_name="Dana", last_name="HR", email="dana@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True, user_role=Role.HR.value, password="Password123!",
        )
        db.session.commit()

        LeaveService(email=mail).request_leave(
            employee_id=record.id, leave_type=LeaveType.SICK
        )
        db.session.commit()

        self_alerts = EmailLog.query.filter_by(
            event_type="leave_submitted_hr", to_email="dana@ems.local"
        ).count()
        assert self_alerts == 0, "HR should not be alerted about their own request"

    def test_special_leave_still_emails_hr(self, leave_with_mail, employee, hr_user):
        leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        assert EmailLog.query.filter_by(event_type="leave_submitted_hr").count() >= 1

    def test_approval_emails_both_employee_and_hr(
        self, leave_with_mail, employee, hr_user, admin_user
    ):
        request = leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        EmailLog.query.delete()  # focus on the decision emails
        db.session.commit()

        leave_with_mail.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        assert EmailLog.query.filter_by(
            event_type="leave_approved", to_email=employee.email
        ).count() == 1
        assert EmailLog.query.filter_by(event_type="leave_approved").count() >= 2

    def test_denial_emails_the_employee(
        self, leave_with_mail, employee, admin_user
    ):
        request = leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        EmailLog.query.delete()
        db.session.commit()

        leave_with_mail.decide(
            request_id=request.id, decision="deny", actor_user_id=admin_user.id
        )
        db.session.commit()

        assert EmailLog.query.filter_by(
            event_type="leave_denied", to_email=employee.email
        ).count() == 1

    def test_forced_approval_emails_the_employee(self, leave_with_mail, employee):
        request = leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        EmailLog.query.delete()
        db.session.commit()

        leave_with_mail.respond(request_id=request.id, action="deny")
        db.session.commit()
        leave_with_mail.respond(request_id=request.id, action="deny")
        db.session.commit()

        assert EmailLog.query.filter_by(
            event_type="leave_forced", to_email=employee.email
        ).count() == 1

    def test_cancellation_emails_the_employee(self, leave_with_mail, employee):
        request = leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        EmailLog.query.delete()
        db.session.commit()

        leave_with_mail.cancel(request_id=request.id)
        db.session.commit()

        assert EmailLog.query.filter_by(
            event_type="leave_cancelled", to_email=employee.email
        ).count() == 1

    def test_email_failure_does_not_break_the_request(
        self, clean_db, app_context, employee, leave_rules, attendance_policy, app
    ):
        """A broken mail server must not roll back a leave decision."""

        class ExplodingTransport:
            name = "exploding"

            def deliver(self, message):  # noqa: ARG002
                raise RuntimeError("smtp down")

        service = LeaveService(email=EmailService(transport=ExplodingTransport()))
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()

        assert request.id is not None
        assert request.status == LeaveStatus.SUGGESTED.value
        # The failures were recorded rather than swallowed.
        assert EmailLog.query.filter_by(status="failed").count() >= 1

    def test_all_emails_are_sent_with_the_null_transport(
        self, leave_with_mail, employee, hr_user
    ):
        leave_with_mail.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        assert EmailLog.query.filter_by(status="failed").count() == 0
        assert EmailLog.query.filter_by(status="sent").count() >= 2


class TestRecipientResolution:
    def test_resolves_hr_and_admin_roles(self, clean_db, app_context, hr_user, admin_user, mail):
        recipients = mail.resolve_recipients(roles=("HR", "ADMIN"))
        addresses = {r["email"] for r in recipients}
        assert hr_user.email in addresses
        assert admin_user.email in addresses

    def test_excludes_inactive_users(self, clean_db, app_context, hr_user, mail):
        hr_user.is_active = False
        db.session.commit()
        recipients = mail.resolve_recipients(roles=("HR",))
        assert hr_user.email not in {r["email"] for r in recipients}

    def test_honours_exclude_ids(self, clean_db, app_context, hr_user, mail):
        recipients = mail.resolve_recipients(
            roles=("HR",), exclude_user_ids={hr_user.id}
        )
        assert hr_user.email not in {r["email"] for r in recipients}

    def test_returns_empty_when_no_roles_match(self, clean_db, app_context, mail):
        assert mail.resolve_recipients(roles=("NOPE",)) == []


class TestQueueFiltering:
    """The bug where the queue filtered to an empty department."""

    def test_queue_is_unfiltered_by_default(self, clean_db, app_context, employee, leave_rules, attendance_policy):
        service = LeaveService(email=EmailService(transport=NullTransport()))
        service.request_leave(employee_id=employee.id, leave_type=LeaveType.SICK)
        db.session.commit()

        # No department filter must still find the request.
        everything = service.list_requests(statuses=[LeaveStatus.PENDING.value])
        assert len(everything) == 1

    def test_counts_match_the_queue_for_the_same_filter(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        service = LeaveService(email=EmailService(transport=NullTransport()))
        service.request_leave(employee_id=employee.id, leave_type=LeaveType.SICK)
        db.session.commit()

        unfiltered = service.list_requests(statuses=[LeaveStatus.PENDING.value])
        counts = service.counts_by_status(department_id=None)
        assert counts.get("pending", 0) == len(unfiltered)

    def test_department_filter_excludes_other_departments(
        self, clean_db, app_context, employee, department, position,
        leave_rules, attendance_policy,
    ):
        from app.features.employees.service import DepartmentService, EmployeeService

        other = DepartmentService().create_department(name="Finance", code="FIN")
        other_employee, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Finance", email="bea@ems.local",
            department_id=other.id,
        )
        db.session.commit()

        service = LeaveService(email=EmailService(transport=NullTransport()))
        service.request_leave(employee_id=other_employee.id, leave_type=LeaveType.SICK)
        db.session.commit()

        # The request belongs to Finance, so it is invisible to Engineering.
        assert service.list_requests(
            statuses=[LeaveStatus.PENDING.value], department_id=department.id
        ) == []
        # ...but visible with no filter, and to its own department.
        assert len(service.list_requests(statuses=[LeaveStatus.PENDING.value])) == 1
        assert len(service.list_requests(
            statuses=[LeaveStatus.PENDING.value], department_id=other.id
        )) == 1

    def test_calendar_supports_all_departments(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        service = LeaveService(email=EmailService(transport=NullTransport()))
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        service.respond(request_id=request.id, action="accept")
        db.session.commit()

        entries = service.calendar(
            department_id=None,
            start=request.start_date,
            end=request.end_date,
        )
        assert len(entries) >= 1
