"""Tests for self-service profile management."""

from __future__ import annotations

import pytest

from app.core.database import db
from app.core.security import verify_password
from app.features.auth.repository import RefreshTokenRepository, UserRepository
from app.features.employees.exceptions import (
    ProfileFieldLockedError,
    ProfileNotAvailableError,
)
from app.features.employees.repository import EmployeeRepository
from app.features.employees.service import EmployeeService

pytestmark = pytest.mark.integration


@pytest.fixture()
def service():
    return EmployeeService()


class TestEditableFields:
    def test_can_change_name_and_email(self, clean_db, app_context, employee, service):
        updated = service.update_own_profile(
            employee.id, first_name="Alicia", last_name="Njeri-Smith",
            email="alicia@ems.local",
        )
        db.session.commit()

        assert updated.first_name == "Alicia"
        assert updated.last_name == "Njeri-Smith"
        assert updated.email == "alicia@ems.local"

    def test_can_change_timezone(self, clean_db, app_context, employee, service):
        updated = service.update_own_profile(employee.id, tz="Europe/London")
        db.session.commit()
        assert updated.tz == "Europe/London"

    def test_email_is_normalised(self, clean_db, app_context, employee, service):
        updated = service.update_own_profile(employee.id, email="  MixedCase@EMS.Local ")
        db.session.commit()
        assert updated.email == "mixedcase@ems.local"

    def test_no_changes_is_a_no_op(self, clean_db, app_context, employee, service):
        same = service.update_own_profile(employee.id)
        assert same.id == employee.id


class TestLockedFields:
    """Employee number and phone belong to HR, not the employee."""

    def test_employee_number_cannot_be_changed(self, clean_db, app_context, employee, service):
        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(employee.id, employee_no="EMP-9999")

    def test_phone_cannot_be_changed(self, clean_db, app_context, employee, service):
        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(employee.id, phone="+254700000000")

    def test_department_and_position_cannot_be_changed(self, clean_db, app_context, employee, service):
        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(employee.id, department_id=99)
        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(employee.id, position_id=99)

    def test_hire_date_cannot_be_changed(self, clean_db, app_context, employee, service):
        from datetime import date

        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(employee.id, hired_at=date(2020, 1, 1))

    def test_locked_field_is_not_partially_applied(self, clean_db, app_context, employee, service):
        """A rejected edit must not leak the allowed fields through."""
        with pytest.raises(ProfileFieldLockedError):
            service.update_own_profile(
                employee.id, first_name="Sneaky", phone="+254700000000"
            )
        db.session.rollback()
        db.session.expire_all()
        reloaded = EmployeeRepository().get(employee.id)
        assert reloaded.first_name == "Alice"
        assert reloaded.phone != "+254700000000"

    def test_unknown_field_is_rejected(self, clean_db, app_context, employee, service):
        from app.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.update_own_profile(employee.id, is_active=False)


class TestEmailSynchronisation:
    """The email is also the sign-in identity, so both rows must move together."""

    def test_login_email_follows_the_employee_email(
        self, clean_db, app_context, employee, service
    ):
        service.update_own_profile(employee.id, email="newsync@ems.local")
        db.session.commit()

        user = UserRepository().get(employee.user_id)
        assert user.email == "newsync@ems.local"

    def test_cannot_take_another_employees_email(
        self, clean_db, app_context, employee, position, department, service
    ):
        from app.features.employees.exceptions import EmployeeEmailTakenError

        other, _ = service.create_employee(
            first_name="Bea", last_name="Other", email="bea@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        with pytest.raises(EmployeeEmailTakenError):
            service.update_own_profile(employee.id, email="bea@ems.local")

    def test_cannot_take_an_email_used_by_a_login_without_an_employee(
        self, clean_db, app_context, employee, service
    ):
        """The users table has its own unique email, checked separately."""
        from app.features.auth.constants import Role
        from app.features.auth.exceptions import EmailAlreadyRegisteredError
        from app.features.auth.service import AuthService

        AuthService().create_user(
            email="orphan@ems.local", password="Password123!", role=Role.MANAGER
        )
        db.session.commit()

        with pytest.raises(EmailAlreadyRegisteredError):
            service.update_own_profile(employee.id, email="orphan@ems.local")

    def test_employee_without_a_login_can_still_edit_details(
        self, clean_db, app_context, department, position, service
    ):
        record, _ = service.create_employee(
            first_name="No", last_name="Login", email="nologin@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()
        assert record.user_id is None

        updated = service.update_own_profile(record.id, first_name="Now")
        db.session.commit()
        assert updated.first_name == "Now"


class TestPasswordChange:
    def test_correct_current_password_changes_it(
        self, clean_db, app_context, employee, service
    ):
        service.change_own_password(
            employee.id,
            current_password="Password123!",
            new_password="BrandNew456!",
        )
        db.session.commit()

        user = UserRepository().get(employee.user_id)
        assert verify_password("BrandNew456!", user.password_hash)
        assert not verify_password("Password123!", user.password_hash)

    def test_wrong_current_password_is_rejected(
        self, clean_db, app_context, employee, service
    ):
        from app.features.auth.exceptions import InvalidCredentialsError

        with pytest.raises(InvalidCredentialsError):
            service.change_own_password(
                employee.id,
                current_password="wrong-password",
                new_password="BrandNew456!",
            )

        db.session.rollback()
        user = UserRepository().get(employee.user_id)
        assert verify_password("Password123!", user.password_hash)

    def test_reusing_the_same_password_is_rejected(
        self, clean_db, app_context, employee, service
    ):
        from app.features.auth.exceptions import WeakPasswordError

        with pytest.raises(WeakPasswordError):
            service.change_own_password(
                employee.id,
                current_password="Password123!",
                new_password="Password123!",
            )

    def test_weak_new_password_is_rejected(self, clean_db, app_context, employee, service):
        from app.features.auth.exceptions import WeakPasswordError

        with pytest.raises(WeakPasswordError):
            service.change_own_password(
                employee.id, current_password="Password123!", new_password="short"
            )

    def test_password_change_revokes_other_sessions(
        self, clean_db, app_context, employee, service
    ):
        # Simulate an existing session row.
        from datetime import timedelta

        from app.core.security import hash_token
        from app.core.time import utcnow

        tokens = RefreshTokenRepository()
        tokens.create_token(
            user_id=employee.user_id,
            token_hash=hash_token("existing-session"),
            expires_at=utcnow() + timedelta(days=1),
        )
        db.session.commit()
        assert len(tokens.active_for_user(employee.user_id)) == 1

        service.change_own_password(
            employee.id,
            current_password="Password123!",
            new_password="BrandNew456!",
        )
        db.session.commit()

        assert tokens.active_for_user(employee.user_id) == []

    def test_employee_without_a_login_cannot_change_password(
        self, clean_db, app_context, department, position, service
    ):
        record, _ = service.create_employee(
            first_name="No", last_name="Login", email="nologin2@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        with pytest.raises(ProfileNotAvailableError):
            service.change_own_password(
                record.id,
                current_password="whatever",
                new_password="BrandNew456!",
            )


class TestProfileEndpoints:
    def test_profile_page_requires_login(self, client):
        response = client.get("/app/profile")
        assert response.status_code == 302
        assert "/app/login" in response.headers["Location"]

    def test_profile_page_renders(self, client, employee_headers, employee):
        response = client.get("/app/profile", headers=employee_headers)
        assert response.status_code == 200
        assert b"My profile" in response.data
        assert employee.employee_no.encode() in response.data

    def test_locked_fields_are_shown_read_only(self, client, employee_headers, employee):
        body = client.get("/app/profile", headers=employee_headers).data
        assert b"Employee number" in body
        assert b"Managed by HR" in body
        # The employee number must not sit in an editable input.
        assert b'name="employee_no"' not in body
        assert b'name="phone"' not in body

    def test_update_name_through_the_form(self, client, employee_headers, employee):
        response = client.post(
            "/app/profile",
            data={"first_name": "Alicia", "last_name": "Njeri", "email": employee.email, "tz": employee.tz},
            headers=employee_headers,
            follow_redirects=True,
        )
        assert response.status_code == 200
        db.session.expire_all()
        assert EmployeeRepository().get(employee.id).first_name == "Alicia"

    def test_email_update_syncs_the_login(self, client, employee_headers, employee):
        client.post(
            "/app/profile",
            data={"first_name": "Alice", "last_name": "Njeri", "email": "moved@ems.local", "tz": employee.tz},
            headers=employee_headers,
        )
        db.session.expire_all()
        user = UserRepository().get(employee.user_id)
        assert user.email == "moved@ems.local"

        # The new address now works as a login.
        client.get("/app/logout")
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "moved@ems.local", "password": "Password123!"},
        )
        assert login.status_code == 200

    def test_attempting_to_change_employee_number_is_refused(
        self, client, employee_headers, employee
    ):
        client.post(
            "/app/profile",
            data={
                "first_name": "Alice", "last_name": "Njeri",
                "email": employee.email, "tz": employee.tz,
                "employee_no": "EMP-9999",
            },
            headers=employee_headers,
        )
        db.session.expire_all()
        assert EmployeeRepository().get(employee.id).employee_no == employee.employee_no

    def test_attempting_to_change_phone_is_refused(
        self, client, employee_headers, employee
    ):
        original = employee.phone
        client.post(
            "/app/profile",
            data={
                "first_name": "Alice", "last_name": "Njeri",
                "email": employee.email, "tz": employee.tz,
                "phone": "+254700000000",
            },
            headers=employee_headers,
        )
        db.session.expire_all()
        assert EmployeeRepository().get(employee.id).phone == original

    def test_password_change_through_the_form(self, client, employee_headers, employee):
        client.post(
            "/app/profile/password",
            data={
                "current_password": "Password123!",
                "new_password": "BrandNew456!",
                "confirm_password": "BrandNew456!",
            },
            headers=employee_headers,
        )
        db.session.expire_all()
        user = UserRepository().get(employee.user_id)
        assert verify_password("BrandNew456!", user.password_hash)

    def test_mismatched_confirmation_is_refused(self, client, employee_headers, employee):
        client.post(
            "/app/profile/password",
            data={
                "current_password": "Password123!",
                "new_password": "BrandNew456!",
                "confirm_password": "Different789!",
            },
            headers=employee_headers,
        )
        db.session.expire_all()
        user = UserRepository().get(employee.user_id)
        # Unchanged.
        assert verify_password("Password123!", user.password_hash)

    def test_wrong_current_password_is_refused(self, client, employee_headers, employee):
        client.post(
            "/app/profile/password",
            data={
                "current_password": "not-my-password",
                "new_password": "BrandNew456!",
                "confirm_password": "BrandNew456!",
            },
            headers=employee_headers,
        )
        db.session.expire_all()
        user = UserRepository().get(employee.user_id)
        assert verify_password("Password123!", user.password_hash)

    def test_profile_link_is_in_the_nav(self, client, employee_headers, employee):
        body = client.get("/app/dashboard", headers=employee_headers).data
        assert b"/app/profile" in body
