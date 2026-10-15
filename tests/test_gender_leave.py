"""Tests for gender-gated parental leave.

Maternity applies to women and paternity to men. The rule is enforced in the
service layer, not only in the form, and an employee with no recorded gender
cannot take either until HR sets it.
"""

from __future__ import annotations

import pytest

from app.core.database import db
from app.features.employees.constants import Gender
from app.features.employees.exceptions import GenderLeaveMismatchError
from app.features.employees.service import EmployeeService
from app.features.leave.constants import LeaveType
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


@pytest.fixture()
def service():
    return EmployeeService()


class TestGenderEnum:
    def test_values_and_coercion(self):
        assert Gender.values() == ["male", "female"]
        assert Gender.coerce("FEMALE") is Gender.FEMALE
        assert Gender.coerce(" male ") is Gender.MALE

    def test_parental_type_mapping(self):
        assert Gender.FEMALE.parental_leave_type == "maternity"
        assert Gender.MALE.parental_leave_type == "paternity"


class TestGenderOnCreation:
    def test_gender_is_optional_at_the_service_layer(
        self, clean_db, app_context, department, position, service
    ):
        """The form requires it; the service allows it to be set later.

        This keeps the seed and the API usable, with the "needs gender" screen as
        the safety net. Neither parental type is available until it is set.
        """
        record, _ = service.create_employee(
            first_name="No", last_name="Gender", email="nogender@ems.local",
            department_id=department.id, position_id=position.id,
            gender=None,
        )
        db.session.commit()
        assert record.gender is None
        assert record.needs_gender
        assert record.parental_leave_type is None

    def test_invalid_gender_is_rejected(
        self, clean_db, app_context, department, position, service
    ):
        from app.core.exceptions import ValidationError

        with pytest.raises(ValidationError):
            service.create_employee(
                first_name="Bad", last_name="Gender", email="badgender@ems.local",
                department_id=department.id, position_id=position.id,
                gender="other",
            )

    def test_gender_is_stored(
        self, clean_db, app_context, department, position, service
    ):
        record, _ = service.create_employee(
            first_name="Grace", last_name="Achieng", email="grace.g@ems.local",
            department_id=department.id, position_id=position.id,
            gender="female",
        )
        db.session.commit()
        assert record.gender == "female"
        assert record.gender_enum is Gender.FEMALE
        assert not record.needs_gender
        assert record.parental_leave_type == "maternity"

    def test_gender_can_be_updated(self, clean_db, app_context, employee, service):
        updated = service.update_employee(employee.id, gender="female")
        db.session.commit()
        assert updated.gender == "female"

    def test_adding_gender_clears_the_needs_flag(
        self, clean_db, app_context, employee, service
    ):
        employee.gender = None
        db.session.commit()
        assert employee.needs_gender

        service.update_employee(employee.id, gender="male")
        db.session.commit()
        assert not employee.needs_gender


class TestLeaveEligibility:
    def test_female_gets_maternity_only(self, clean_db, app_context, employee, service):
        service.update_employee(employee.id, gender="female")
        db.session.commit()

        result = service.leave_eligibility(employee)
        assert "maternity" in result["allowed"]
        assert "paternity" not in result["allowed"]
        assert "paternity" in result["blocked"]
        assert result["parental"] == "maternity"

    def test_male_gets_paternity_only(self, clean_db, app_context, employee, service):
        service.update_employee(employee.id, gender="male")
        db.session.commit()

        result = service.leave_eligibility(employee)
        assert "paternity" in result["allowed"]
        assert "maternity" not in result["allowed"]
        assert result["parental"] == "paternity"

    def test_unrecorded_gender_gets_neither(
        self, clean_db, app_context, employee
    ):
        employee.gender = None
        db.session.commit()

        result = EmployeeService().leave_eligibility(employee)
        assert "maternity" not in result["allowed"]
        assert "paternity" not in result["allowed"]
        assert result["parental"] is None
        # And it explains what to do about it.
        assert "gender" in result["blocked"]["maternity"].lower()

    def test_other_leave_types_are_unaffected(
        self, clean_db, app_context, employee
    ):
        employee.gender = None
        db.session.commit()

        allowed = EmployeeService().leave_eligibility(employee)["allowed"]
        for kind in ("annual", "sick", "unpaid"):
            assert kind in allowed

    def test_assert_raises_for_the_wrong_type(
        self, clean_db, app_context, employee, service
    ):
        service.update_employee(employee.id, gender="male")
        db.session.commit()

        with pytest.raises(GenderLeaveMismatchError):
            service.assert_leave_type_allowed(employee, "maternity")
        # The matching one passes silently.
        service.assert_leave_type_allowed(employee, "paternity")

    def test_assert_ignores_non_parental_types(
        self, clean_db, app_context, employee
    ):
        employee.gender = None
        db.session.commit()
        # No gender recorded, but annual leave is still fine.
        EmployeeService().assert_leave_type_allowed(employee, "annual")


class TestLeaveRequestEnforcement:
    def test_male_cannot_request_maternity(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        EmployeeService().update_employee(employee.id, gender="male")
        db.session.commit()

        with pytest.raises(GenderLeaveMismatchError):
            LeaveService().request_leave(
                employee_id=employee.id, leave_type=LeaveType.MATERNITY
            )

    def test_female_cannot_request_paternity(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        EmployeeService().update_employee(employee.id, gender="female")
        db.session.commit()

        with pytest.raises(GenderLeaveMismatchError):
            LeaveService().request_leave(
                employee_id=employee.id, leave_type=LeaveType.PATERNITY
            )

    def test_female_can_request_maternity(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        EmployeeService().update_employee(employee.id, gender="female")
        db.session.commit()

        request = LeaveService().request_leave(
            employee_id=employee.id, leave_type=LeaveType.MATERNITY
        )
        db.session.commit()
        assert request.id is not None
        assert request.leave_type == LeaveType.MATERNITY.value

    def test_male_can_request_paternity(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        EmployeeService().update_employee(employee.id, gender="male")
        db.session.commit()

        request = LeaveService().request_leave(
            employee_id=employee.id, leave_type=LeaveType.PATERNITY
        )
        db.session.commit()
        assert request.leave_type == LeaveType.PATERNITY.value

    def test_unrecorded_gender_cannot_request_either(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        employee.gender = None
        db.session.commit()

        for kind in (LeaveType.MATERNITY, LeaveType.PATERNITY):
            with pytest.raises(GenderLeaveMismatchError):
                LeaveService().request_leave(
                    employee_id=employee.id, leave_type=kind
                )

    def test_annual_leave_still_works_without_gender(
        self, clean_db, app_context, employee, leave_rules, attendance_policy
    ):
        """Only parental leave is gated; nothing else regresses."""
        employee.gender = None
        db.session.commit()

        request = LeaveService().request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        assert request.id is not None


class TestGenderScreen:
    def test_lists_only_employees_without_a_gender(
        self, clean_db, app_context, client, hr_headers, employee, department, position
    ):
        # Alice has no gender yet, so she should be listed.
        page = client.get("/app/employees/gender", headers=hr_headers).get_data(as_text=True)
        assert "still need a gender" in page
        assert employee.employee_no in page

        # Once recorded she drops off the list.
        EmployeeService().update_employee(employee.id, gender="female")
        db.session.commit()
        page = client.get("/app/employees/gender", headers=hr_headers).get_data(as_text=True)
        assert "Every employee has a gender recorded" in page

    def test_saving_sets_the_gender(
        self, clean_db, app_context, client, hr_headers, employee
    ):
        response = client.post(
            "/app/employees/gender",
            data={f"gender_{employee.id}": "female"},
            headers=hr_headers,
            follow_redirects=True,
        )
        assert response.status_code == 200
        db.session.expire_all()
        assert EmployeeService().get_employee(employee.id).gender == "female"

    def test_blank_selections_are_skipped(
        self, clean_db, app_context, client, hr_headers, employee
    ):
        response = client.post(
            "/app/employees/gender", data={}, headers=hr_headers,
            follow_redirects=True,
        )
        assert response.status_code == 200
        db.session.expire_all()
        assert EmployeeService().get_employee(employee.id).gender is None

    def test_employee_cannot_reach_the_screen(
        self, clean_db, app_context, client, employee_headers
    ):
        assert client.get("/app/employees/gender", headers=employee_headers).status_code == 403
        assert client.post(
            "/app/employees/gender", data={}, headers=employee_headers
        ).status_code == 403


class TestLeaveFormFiltering:
    def test_form_offers_only_eligible_types(
        self, clean_db, app_context, client, employee, leave_rules, attendance_policy
    ):
        EmployeeService().update_employee(employee.id, gender="female")
        db.session.commit()

        # Sign in as the employee themselves.
        login = client.post(
            "/api/v1/auth/login",
            json={"email": employee.email, "password": "Password123!"},
        )
        assert login.status_code == 200

        page = client.get("/app/leave/request").get_data(as_text=True)
        assert 'value="maternity"' in page
        assert 'value="paternity"' not in page
        # And the reason is explained rather than the option silently vanishing.
        assert "Not available to you" in page

    def test_form_explains_a_missing_gender(
        self, clean_db, app_context, client, employee, leave_rules, attendance_policy
    ):
        employee.gender = None
        db.session.commit()

        client.post(
            "/api/v1/auth/login",
            json={"email": employee.email, "password": "Password123!"},
        )
        page = client.get("/app/leave/request").get_data(as_text=True)
        assert "Not available to you" in page
        assert "Speak to HR" in page
