"""Tests for the department leave quota, attendance integrity, and recall."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.database import db
from app.features.attendance.constants import AttendanceStatus
from app.features.attendance.exceptions import (
    EmployeeNotClockableError,
    EmployeeOnLeaveError,
)
from app.features.attendance.service import AttendanceService
from app.features.biometrics.constants import PunchType
from app.features.employees.service import EmployeeService
from app.features.leave.constants import LeaveStatus, LeaveType
from app.features.leave.engine import Window, check_capacity, next_free_window
from app.features.leave.exceptions import (
    DepartmentAtCapacityError,
    RecallNotAllowedError,
)
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


def a_monday(offset_weeks: int = 6) -> date:
    """A weekday comfortably in the future, to satisfy notice rules."""
    day = date.today() + timedelta(weeks=offset_weeks)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


class TestCapacityCheck:
    """The pure rule everything else depends on."""

    def test_limit_none_never_breaches(self):
        breach = check_capacity(
            Window(date(2026, 3, 2), date(2026, 3, 6)),
            peers_on_leave_by_date={date(2026, 3, 2): 9},
            limit=None,
        )
        assert not breach.breached

    def test_one_peer_at_limit_blocks(self):
        breach = check_capacity(
            Window(date(2026, 3, 2), date(2026, 3, 6)),
            peers_on_leave_by_date={date(2026, 3, 3): 1},
            limit=1,
        )
        assert breach.breached
        assert breach.worst_day == date(2026, 3, 3)
        assert "1 colleague(s)" in breach.message

    def test_below_limit_is_fine(self):
        breach = check_capacity(
            Window(date(2026, 3, 2), date(2026, 3, 6)),
            peers_on_leave_by_date={date(2026, 3, 3): 1},
            limit=2,
        )
        assert not breach.breached

    def test_weekends_are_never_counted(self):
        # Only the Saturday has a peer, so a Mon-Fri window is unaffected.
        breach = check_capacity(
            Window(date(2026, 3, 2), date(2026, 3, 6)),
            peers_on_leave_by_date={date(2026, 3, 7): 5},
            limit=1,
        )
        assert not breach.breached

    def test_reports_every_clashing_day(self):
        breach = check_capacity(
            Window(date(2026, 3, 2), date(2026, 3, 6)),
            peers_on_leave_by_date={
                date(2026, 3, 2): 1,
                date(2026, 3, 4): 1,
            },
            limit=1,
        )
        assert len(breach.over_capacity) == 2

    def test_next_free_window_skips_busy_days(self):
        busy = {date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)}
        window = next_free_window(
            earliest=date(2026, 3, 2),
            days=3,
            peers_on_leave_by_date={day: 1 for day in busy},
            limit=1,
        )
        assert window is not None
        assert window.start_date not in busy

    def test_next_free_window_gives_up_inside_the_horizon(self):
        # Every weekday is busy, so nothing can fit.
        start = date(2026, 3, 2)
        every_day = {
            start + timedelta(days=offset): 1
            for offset in range(0, 30)
            if (start + timedelta(days=offset)).weekday() < 5
        }
        window = next_free_window(
            earliest=start, days=2,
            peers_on_leave_by_date=every_day,
            limit=1, horizon_days=21,
        )
        assert window is None


class TestApprovalQuota:
    """The bug: approvals ignored the cap entirely."""

    def _request_for(self, service, employee, *, days=2, start=None):
        return service.request_leave(
            employee_id=employee.id,
            leave_type=LeaveType.ANNUAL,
            start_date=start or a_monday(),
            end_date=(start or a_monday()) + timedelta(days=days - 1),
            days=days,
        )

    def test_second_approval_in_a_department_is_refused(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        window_start = a_monday()

        first = self._request_for(service, employee, start=window_start)
        db.session.commit()
        service.decide(
            request_id=first.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        second = self._request_for(service, colleague, start=window_start)
        db.session.commit()

        with pytest.raises(DepartmentAtCapacityError):
            service.decide(
                request_id=second.id, decision="approve",
                actor_user_id=admin_user.id,
            )

    def test_refused_approval_leaves_the_request_untouched(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea2@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        window_start = a_monday()
        first = self._request_for(service, employee, start=window_start)
        db.session.commit()
        service.decide(request_id=first.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        second = self._request_for(service, colleague, start=window_start)
        db.session.commit()

        with pytest.raises(DepartmentAtCapacityError):
            service.decide(
                request_id=second.id, decision="approve", actor_user_id=admin_user.id
            )
        db.session.rollback()

        # Nothing was consumed by the failed approval.
        balance_after = service.balances.get_scope(
            employee_id=colleague.id, year=window_start.year,
            leave_type=LeaveType.ANNUAL.value,
        )
        assert balance_after.used == 0

    def test_different_departments_do_not_interfere(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        from app.features.employees.service import DepartmentService

        other = DepartmentService().create_department(name="Finance", code="FIN")
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Finance", email="bea.fin@ems.local",
            department_id=other.id, position_id=None,
        )
        db.session.commit()

        service = LeaveService()
        window_start = a_monday()
        first = self._request_for(service, employee, start=window_start)
        db.session.commit()
        service.decide(request_id=first.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        # A different department is unaffected by Engineering's cap.
        second = self._request_for(service, colleague, start=window_start)
        db.session.commit()
        service.decide(request_id=second.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()
        assert service.get_request(second.id).status == LeaveStatus.APPROVED.value

    def test_force_override_approves_and_records_the_exception(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea3@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        window_start = a_monday()
        first = self._request_for(service, employee, start=window_start)
        db.session.commit()
        service.decide(request_id=first.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        second = self._request_for(service, colleague, start=window_start)
        db.session.commit()
        service.decide(
            request_id=second.id, decision="approve",
            actor_user_id=admin_user.id, force=True,
        )
        db.session.commit()

        decided = service.get_request(second.id)
        assert decided.status == LeaveStatus.APPROVED.value
        # The override is auditable, not silent.
        assert "capacity override" in (decided.reason or "").lower()

    def test_capacity_error_offers_an_alternative(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea4@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        window_start = a_monday()
        first = self._request_for(service, employee, start=window_start)
        db.session.commit()
        service.decide(request_id=first.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        second = self._request_for(service, colleague, start=window_start)
        db.session.commit()
        with pytest.raises(DepartmentAtCapacityError) as caught:
            service.decide(
                request_id=second.id, decision="approve", actor_user_id=admin_user.id
            )
        # HR is told which days clash and what to do instead.
        assert caught.value.breaches
        assert caught.value.alternative is not None

    def test_non_annual_leave_is_not_capped(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        """Sick leave has no department cap, so two people may overlap."""
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea5@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        first = service.request_leave(employee_id=employee.id, leave_type=LeaveType.SICK)
        db.session.commit()
        service.decide(request_id=first.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        second = service.request_leave(employee_id=colleague.id, leave_type=LeaveType.SICK)
        db.session.commit()
        service.decide(request_id=second.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()
        assert service.get_request(second.id).status == LeaveStatus.APPROVED.value


class TestAttendanceIntegrity:
    def test_employee_on_leave_cannot_clock_in(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        service.decide(request_id=request.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        attendance = AttendanceService()
        from datetime import time as clock
        from app.core.time import shift_bounds

        # A normal 09:00 arrival on a day covered by approved leave.
        start_utc, _ = shift_bounds(
            request.start_date, clock(9, 0), clock(17, 0), employee.tz
        )
        with pytest.raises(EmployeeOnLeaveError):
            attendance.apply_punch(
                employee_id=employee.id,
                department_id=employee.department_id,
                tz_name=employee.tz,
                punch_time=start_utc,
                punch_type=PunchType.IN,
            )

    def test_leave_day_stays_on_leave_after_a_rejected_punch(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        service.decide(request_id=request.id, decision="approve", actor_user_id=admin_user.id)
        db.session.commit()

        # Mark the day as on-leave the way the nightly job does.
        attendance = AttendanceService()
        attendance.mark_on_leave(employee.id, request.start_date)
        db.session.commit()

        from datetime import time as clock
        from app.core.time import shift_bounds

        start_utc, _ = shift_bounds(
            request.start_date, clock(9, 0), clock(17, 0), employee.tz
        )
        with pytest.raises(EmployeeOnLeaveError):
            attendance.apply_punch(
                employee_id=employee.id,
                department_id=employee.department_id,
                tz_name=employee.tz,
                punch_time=start_utc,
                punch_type=PunchType.IN,
            )
        db.session.rollback()

        record = attendance.get_day(employee.id, request.start_date)
        assert record.status == AttendanceStatus.ON_LEAVE.value

    def test_suspended_employee_cannot_clock_in(
        self, clean_db, app_context, employee, attendance_policy
    ):
        EmployeeService().deactivate(employee.id)
        db.session.commit()

        from datetime import datetime, time as clock, timezone

        moment = datetime.combine(date.today(), clock(9, 0), tzinfo=timezone.utc)
        attendance = AttendanceService()
        with pytest.raises(EmployeeNotClockableError):
            attendance.apply_punch(
                employee_id=employee.id,
                department_id=employee.department_id,
                tz_name=employee.tz,
                punch_time=moment,
                punch_type=PunchType.IN,
            )

    def test_active_employee_without_leave_can_clock_in(
        self, clean_db, app_context, employee, attendance_policy
    ):
        from datetime import datetime, time as clock, timezone

        moment = datetime.combine(date.today(), clock(6, 0), tzinfo=timezone.utc)
        record = AttendanceService().apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=moment,
            punch_type=PunchType.IN,
        )
        db.session.commit()
        assert record.clock_in is not None

    def test_is_clockable_reports_the_reason(
        self, clean_db, app_context, employee, attendance_policy
    ):
        ok, reason = AttendanceService().is_clockable(employee.id, date.today())
        assert ok and reason == ""

        EmployeeService().deactivate(employee.id)
        db.session.commit()
        ok, reason = AttendanceService().is_clockable(employee.id, date.today())
        assert not ok
        assert "suspended" in reason.lower()


class TestRecall:
    def _approved(self, service, employee, admin_user, *, days=3):
        start = a_monday(8)
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=days - 1), days=days,
        )
        db.session.commit()
        service.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id,
            force=True,
        )
        db.session.commit()
        return request

    def test_recall_refunds_the_days(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = self._approved(service, employee, admin_user)
        year = request.start_date.year
        before = service.balances.get_scope(
            employee_id=employee.id, year=year, leave_type=LeaveType.ANNUAL.value
        )
        used_before = before.used

        service.recall(request_id=request.id, actor_user_id=admin_user.id, reason="Crunch")
        db.session.commit()

        after = service.balances.get_scope(
            employee_id=employee.id, year=year, leave_type=LeaveType.ANNUAL.value
        )
        assert after.used == used_before - request.days

    def test_recall_sets_status_and_reason(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = self._approved(service, employee, admin_user)
        service.recall(
            request_id=request.id, actor_user_id=admin_user.id, reason="Quarter end"
        )
        db.session.commit()

        recalled = service.get_request(request.id)
        assert recalled.status == LeaveStatus.RECALLED.value
        assert "Quarter end" in (recalled.recall_reason or "")

    def test_recall_proposes_a_replacement(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = self._approved(service, employee, admin_user)
        result = service.recall(
            request_id=request.id, actor_user_id=admin_user.id, reason="Crunch"
        )
        db.session.commit()

        assert result["replacement"] is not None
        replacement = result["replacement"]
        # The replacement is a fresh suggestion, never a silent rewrite.
        assert replacement.id != request.id
        assert replacement.status == LeaveStatus.SUGGESTED.value
        assert replacement.days == request.days
        # And the two rows are linked for the audit trail.
        assert service.get_request(request.id).rescheduled_to_id == replacement.id

    def test_recalled_employee_can_clock_in_again(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = self._approved(service, employee, admin_user)
        attendance = AttendanceService()
        attendance.mark_on_leave(employee.id, request.start_date)
        db.session.commit()
        ok, _ = attendance.is_clockable(employee.id, request.start_date)
        assert not ok

        service.recall(
            request_id=request.id, actor_user_id=admin_user.id,
            propose_new_dates=False,
        )
        db.session.commit()

        ok, reason = attendance.is_clockable(employee.id, request.start_date)
        assert ok, reason

    def test_cannot_recall_unapproved_leave(
        self, clean_db, app_context, employee, admin_user, leave_rules, attendance_policy
    ):
        service = LeaveService()
        request = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()

        with pytest.raises(RecallNotAllowedError):
            service.recall(request_id=request.id, actor_user_id=admin_user.id)

    def test_recall_releases_department_capacity(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        """After a recall the slot frees up for a colleague."""
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea.recall@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        service = LeaveService()
        start = a_monday(8)
        first = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=2), days=3,
        )
        db.session.commit()
        service.decide(
            request_id=first.id, decision="approve", actor_user_id=admin_user.id, force=True
        )
        db.session.commit()

        second = service.request_leave(
            employee_id=colleague.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=2), days=3,
        )
        db.session.commit()
        with pytest.raises(DepartmentAtCapacityError):
            service.decide(
                request_id=second.id, decision="approve", actor_user_id=admin_user.id
            )

        service.recall(
            request_id=first.id, actor_user_id=admin_user.id, propose_new_dates=False
        )
        db.session.commit()

        # Now the window is free.
        service.decide(
            request_id=second.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()
        assert service.get_request(second.id).status == LeaveStatus.APPROVED.value
