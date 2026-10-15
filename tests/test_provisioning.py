"""Tests for account provisioning: emailed credentials and forced password change."""

from __future__ import annotations

import pytest

from app.core.database import db
from app.core.security import verify_password
from app.features.auth.repository import UserRepository
from app.features.auth.service import AuthService
from app.features.email.constants import EVENT_WELCOME
from app.features.email.models import EmailLog
from app.features.email.senders import NullTransport
from app.features.email.service import EmailService
from app.features.employees.exceptions import ProfileNotAvailableError
from app.features.employees.service import EmployeeService

pytestmark = pytest.mark.integration


@pytest.fixture()
def service():
    return EmployeeService()


class TestPasswordGeneration:
    def test_generated_password_meets_the_strength_rules(self):
        for _ in range(20):
            password = EmployeeService._generate_password()
            # Would raise WeakPasswordError if it did not satisfy the policy.
            AuthService.validate_password_strength(password)

    def test_generated_passwords_are_unique(self):
        passwords = {EmployeeService._generate_password() for _ in range(200)}
        assert len(passwords) == 200

    def test_generated_password_is_long_enough(self):
        assert len(EmployeeService._generate_password()) >= 12


class TestWelcomeEmail:
    def test_creation_emails_the_credentials(
        self, clean_db, app_context, app, department, position
    ):
        app.config["EMAIL_ENABLED"] = True
        import app.features.email.service as email_module

        original = email_module.EmailService

        class NullBacked(original):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", NullTransport())
                super().__init__(*args, **kwargs)

        email_module.EmailService = NullBacked
        try:
            employee, password = EmployeeService().create_employee(
                first_name="New", last_name="Starter", email="starter@ems.local",
                department_id=department.id, position_id=position.id,
                create_user=True,
            )
            db.session.commit()
        finally:
            email_module.EmailService = original

        rows = EmailLog.query.filter_by(event_type=EVENT_WELCOME).all()
        assert len(rows) == 1, f"expected one welcome email, got {len(rows)}"
        row = rows[0]
        assert row.to_email == "starter@ems.local"
        assert row.status == "sent"

        body = row.body_text or ""
        # The essentials must all be present.
        assert password in body, "the temporary password must be in the email"
        assert employee.employee_no in body, "the staff number must be in the email"
        assert "/app/login" in body, "a sign-in link must be in the email"
        assert "change" in body.lower(), "it must tell them to change the password"

    def test_email_reminds_them_about_biometrics(
        self, clean_db, app_context, app, department, position
    ):
        app.config["EMAIL_ENABLED"] = True
        import app.features.email.service as email_module

        original = email_module.EmailService

        class NullBacked(original):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", NullTransport())
                super().__init__(*args, **kwargs)

        email_module.EmailService = NullBacked
        try:
            EmployeeService().create_employee(
                first_name="Bio", last_name="Metric", email="bio@ems.local",
                department_id=department.id, position_id=position.id,
                create_user=True,
            )
            db.session.commit()
        finally:
            email_module.EmailService = original

        body = EmailLog.query.filter_by(event_type=EVENT_WELCOME).first().body_text
        assert "fingerprint" in body.lower() or "biometric" in body.lower()
        assert "HR" in body or "hr_" in body

    def test_no_login_means_no_welcome_email(
        self, clean_db, app_context, app, department, position
    ):
        app.config["EMAIL_ENABLED"] = True
        employee, password = EmployeeService().create_employee(
            first_name="No", last_name="Login", email="nologin@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=False,
        )
        db.session.commit()
        assert password is None
        assert employee.user_id is None
        assert EmailLog.query.filter_by(event_type=EVENT_WELCOME).count() == 0

    def test_email_disabled_sends_nothing(
        self, clean_db, app_context, app, department, position
    ):
        app.config["EMAIL_ENABLED"] = False
        employee, password = EmployeeService().create_employee(
            first_name="Off", last_name="Switch", email="off@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()
        # The account is still created; only the email is skipped.
        assert employee.user_id is not None
        assert password is not None
        assert EmailLog.query.count() == 0

    def test_mail_failure_does_not_lose_the_employee(
        self, clean_db, app_context, app, department, position
    ):
        """A broken mail server must not undo the account creation."""
        app.config["EMAIL_ENABLED"] = True
        app.config["EMAIL_BACKEND"] = "smtp"
        app.config["SMTP_HOST"] = "127.0.0.1"
        app.config["SMTP_PORT"] = 9
        app.config["SMTP_TIMEOUT"] = 2

        employee, password = EmployeeService().create_employee(
            first_name="Mail", last_name="Down", email="maildown@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()

        # The employee and login exist regardless.
        assert employee.id is not None
        assert employee.user_id is not None
        reloaded = UserRepository().get(employee.user_id)
        assert verify_password(password, reloaded.password_hash)


class TestForcedPasswordChange:
    def test_new_login_is_flagged(self, clean_db, app_context, department, position):
        employee, _ = EmployeeService().create_employee(
            first_name="Flag", last_name="Me", email="flag@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()
        user = UserRepository().get(employee.user_id)
        assert user.must_change_password is True

    def test_profile_exposes_the_flag(self, clean_db, app_context, department, position):
        employee, _ = EmployeeService().create_employee(
            first_name="Expose", last_name="Flag", email="expose@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()
        profile = AuthService().load_profile(employee.user_id)
        assert profile["must_change_password"] is True

    def test_changing_your_own_password_clears_the_flag(
        self, clean_db, app_context, department, position
    ):
        employee, password = EmployeeService().create_employee(
            first_name="Clear", last_name="Flag", email="clear@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()

        EmployeeService().change_own_password(
            employee.id, current_password=password, new_password="MyOwnPass123!"
        )
        db.session.commit()

        user = UserRepository().get(employee.user_id)
        assert user.must_change_password is False

    def test_admin_reset_flags_the_user_again(
        self, clean_db, app_context, admin_user, employee
    ):
        """An admin now knows the password, so it must be replaced again."""
        auth = AuthService()
        user = UserRepository().get(employee.user_id)
        auth.set_password(user, "AdminChosen123!")
        db.session.commit()
        assert UserRepository().get(employee.user_id).must_change_password is True

    def test_login_redirects_to_profile_while_flagged(
        self, clean_db, app_context, client, department, position
    ):
        employee, password = EmployeeService().create_employee(
            first_name="Redirect", last_name="Me", email="redirect@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()

        response = client.post(
            "/app/login", data={"email": "redirect@ems.local", "password": password}
        )
        assert response.status_code == 302
        assert "/app/profile" in response.headers["Location"]

    def test_login_goes_to_dashboard_once_cleared(
        self, clean_db, app_context, client, department, position
    ):
        employee, password = EmployeeService().create_employee(
            first_name="Cleared", last_name="User", email="cleared@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()

        # The employee changes their own password first.
        EmployeeService().change_own_password(
            employee.id, current_password=password, new_password="MyOwnPass123!"
        )
        db.session.commit()

        client.get("/app/logout")
        response = client.post(
            "/app/login",
            data={"email": "cleared@ems.local", "password": "MyOwnPass123!"},
        )
        assert response.status_code == 302
        assert "/app/profile" not in response.headers["Location"]

    def test_profile_shows_the_prompt_while_flagged(
        self, clean_db, app_context, client, department, position
    ):
        employee, password = EmployeeService().create_employee(
            first_name="Prompt", last_name="Me", email="prompt@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=True,
        )
        db.session.commit()

        client.post(
            "/app/login", data={"email": "prompt@ems.local", "password": password}
        )
        page = client.get("/app/profile").get_data(as_text=True)
        assert "choose your own password" in page or "temporary" in page.lower()


class TestResendWelcome:
    def test_resend_issues_a_new_password_and_emails_it(
        self, clean_db, app_context, app, department, position
    ):
        app.config["EMAIL_ENABLED"] = True
        import app.features.email.service as email_module

        original = email_module.EmailService

        class NullBacked(original):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("transport", NullTransport())
                super().__init__(*args, **kwargs)

        email_module.EmailService = NullBacked
        try:
            employee, first_password = EmployeeService().create_employee(
                first_name="Re", last_name="Send", email="resend@ems.local",
                department_id=department.id, position_id=position.id,
                create_user=True,
            )
            db.session.commit()
            before = EmailLog.query.filter_by(event_type=EVENT_WELCOME).count()

            new_password = EmployeeService().resend_welcome(employee.id)
            db.session.commit()
        finally:
            email_module.EmailService = original

        assert new_password != first_password
        assert EmailLog.query.filter_by(event_type=EVENT_WELCOME).count() == before + 1

        user = UserRepository().get(employee.user_id)
        # The NEW password works and the OLD one no longer does.
        assert verify_password(new_password, user.password_hash)
        assert not verify_password(first_password, user.password_hash)

    def test_resend_refuses_an_employee_without_a_login(
        self, clean_db, app_context, department, position
    ):
        employee, _ = EmployeeService().create_employee(
            first_name="No", last_name="Account", email="noaccount@ems.local",
            department_id=department.id, position_id=position.id,
            create_user=False,
        )
        db.session.commit()
        with pytest.raises(ProfileNotAvailableError):
            EmployeeService().resend_welcome(employee.id)
