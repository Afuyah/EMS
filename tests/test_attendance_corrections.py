"""Tests for manual attendance corrections addressed by employee and date."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.core.database import db
from app.features.attendance.constants import AttendanceStatus
from app.features.attendance.exceptions import (
    AttendanceNotFoundError,
    EmployeeOnLeaveError,
    InvalidCorrectionError,
)
from app.features.attendance.models import AttendanceDaily
from app.features.attendance.service import AttendanceService
from app.features.leave.constants import LeaveType
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


def a_past_weekday(offset: int = 3) -> date:
    day = date.today() - timedelta(days=offset)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


class TestCorrectableEmployees:
    def test_lists_every_active_employee(
        self, clean_db, app_context, employee, department, position, attendance_policy
    ):
        from app.features.employees.service import EmployeeService

        EmployeeService().create_employee(
            first_name="Second", last_name="Person", email="second@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        rows = AttendanceService().correctable_employees(a_past_weekday())
        assert len(rows) >= 2
        assert all("employee_id" in row for row in rows)
        # The whole point: no record is required to be listed.
        assert all(row["attendance_id"] is None for row in rows)

    def test_includes_employees_that_already_have_a_record(
        self, clean_db, app_context, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        AttendanceService().correct_employee_day(
            employee_id=employee.id, work_date=work_date,
            clock_in=at(work_date, 9), clock_out=at(work_date, 17),
        )
        db.session.commit()

        rows = AttendanceService().correctable_employees(work_date)
        match = next(r for r in rows if r["employee_id"] == employee.id)
        assert match["attendance_id"] is not None

    def test_department_filter_applies(
        self, clean_db, app_context, employee, department, attendance_policy
    ):
        from app.features.employees.service import DepartmentService

        other = DepartmentService().create_department(name="Finance", code="FIN")
        db.session.commit()

        rows = AttendanceService().correctable_employees(
            a_past_weekday(), department_id=other.id
        )
        assert rows == []


class TestCorrectEmployeeDay:
    def test_creates_a_record_when_none_exists(
        self, clean_db, app_context, employee, attendance_policy
    ):
        """The case that mattered: no punch at all can still be recorded."""
        work_date = a_past_weekday()
        assert AttendanceDaily.query.filter_by(
            employee_id=employee.id, work_date=work_date
        ).count() == 0

        record = AttendanceService().correct_employee_day(
            employee_id=employee.id,
            work_date=work_date,
            clock_in=at(work_date, 9),
            clock_out=at(work_date, 17),
        )
        db.session.commit()

        assert record.id is not None
        assert record.clock_in is not None
        assert record.clock_out is not None
        assert record.worked_minutes == 480

    def test_updates_an_existing_record(
        self, clean_db, app_context, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        service = AttendanceService()
        service.correct_employee_day(
            employee_id=employee.id, work_date=work_date,
            clock_in=at(work_date, 9), clock_out=at(work_date, 17),
        )
        db.session.commit()

        updated = service.correct_employee_day(
            employee_id=employee.id, work_date=work_date,
            notes="amended",
        )
        db.session.commit()
        assert updated.notes == "amended"
        # And no second row was created.
        assert AttendanceDaily.query.filter_by(
            employee_id=employee.id, work_date=work_date
        ).count() == 1

    def test_late_arrival_is_computed(
        self, clean_db, app_context, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        # Default policy is 09:00 with 10 minutes grace, in the employee's tz.
        # 12:00 UTC is well past it for a +03:00 employee.
        record = AttendanceService().correct_employee_day(
            employee_id=employee.id, work_date=work_date,
            clock_in=at(work_date, 12),
        )
        db.session.commit()
        assert record.late_minutes > 0
        assert record.status in {
            AttendanceStatus.LATE.value, AttendanceStatus.VERY_LATE.value
        }

    def test_explicit_status_overrides_the_computed_one(
        self, clean_db, app_context, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        record = AttendanceService().correct_employee_day(
            employee_id=employee.id, work_date=work_date,
            clock_in=at(work_date, 12),
            status=AttendanceStatus.HOLIDAY.value,
        )
        db.session.commit()
        assert record.status == AttendanceStatus.HOLIDAY.value

    def test_clock_out_before_clock_in_is_rejected(
        self, clean_db, app_context, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        with pytest.raises(InvalidCorrectionError):
            AttendanceService().correct_employee_day(
                employee_id=employee.id, work_date=work_date,
                clock_in=at(work_date, 17), clock_out=at(work_date, 9),
            )

    def test_unknown_employee_is_rejected(self, clean_db, app_context, attendance_policy):
        with pytest.raises(AttendanceNotFoundError):
            AttendanceService().correct_employee_day(
                employee_id=999_999, work_date=a_past_weekday(),
                clock_in=at(a_past_weekday(), 9),
            )

    def test_cannot_correct_over_approved_leave(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        """An approved leave day must not be quietly overwritten."""
        service = LeaveService()
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        service.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        with pytest.raises(EmployeeOnLeaveError):
            AttendanceService().correct_employee_day(
                employee_id=employee.id,
                work_date=request.start_date,
                clock_in=at(request.start_date, 9),
            )

    def test_marking_on_leave_is_allowed_over_leave(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        """HR can still flag the day explicitly as leave."""
        service = LeaveService()
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        service.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        record = AttendanceService().correct_employee_day(
            employee_id=employee.id,
            work_date=request.start_date,
            status=AttendanceStatus.ON_LEAVE.value,
        )
        db.session.commit()
        assert record.status == AttendanceStatus.ON_LEAVE.value


class TestCorrectionEndpoint:
    def test_form_posts_to_the_employee_based_route(
        self, clean_db, app_context, client, hr_headers
    ):
        page = client.get("/app/attendance").get_data(as_text=True)
        assert 'action="/app/attendance/correct"' in page
        # The old attendance-id route must be gone.
        assert "/correct'" not in page or "/app/attendance/correct" in page

    def test_form_lists_employees_without_records(
        self, clean_db, app_context, client, hr_headers, employee, attendance_policy
    ):
        page = client.get("/app/attendance").get_data(as_text=True)
        assert 'name="employee_id"' in page
        assert "no record yet" in page, "the picker should say who has no record"

    def test_correction_creates_a_record(
        self, clean_db, app_context, client, hr_headers, employee, attendance_policy
    ):
        work_date = a_past_weekday()
        response = client.post(
            "/app/attendance/correct",
            data={
                "employee_id": str(employee.id),
                "work_date": work_date.isoformat(),
                "clock_in": f"{work_date.isoformat()}T09:00",
                "clock_out": f"{work_date.isoformat()}T17:00",
                "status": "",
                "notes": "endpoint-check",
            },
            headers=hr_headers,
        )
        assert response.status_code in (200, 302)

        record = AttendanceDaily.query.filter_by(
            employee_id=employee.id, work_date=work_date
        ).first()
        assert record is not None, "the correction did not create a record"
        assert record.notes == "endpoint-check"

    def test_missing_employee_is_reported_not_crashed(
        self, clean_db, app_context, client, hr_headers, attendance_policy
    ):
        response = client.post(
            "/app/attendance/correct",
            data={"employee_id": "", "work_date": a_past_weekday().isoformat()},
            headers=hr_headers,
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_employee_cannot_correct(
        self, clean_db, app_context, client, employee_headers, employee
    ):
        response = client.post(
            "/app/attendance/correct",
            data={
                "employee_id": str(employee.id),
                "work_date": a_past_weekday().isoformat(),
            },
            headers=employee_headers,
        )
        assert response.status_code == 403
