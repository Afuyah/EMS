"""Integration tests for the leave lifecycle and balance accounting."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.database import db
from app.features.leave.constants import LeaveStatus, LeaveType
from app.features.leave.exceptions import (
    InsufficientBalanceError,
    InvalidLeaveTransitionError,
    LeaveRuleError,
    NoSuggestionAvailableError,
)
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


@pytest.fixture()
def leave_ready(clean_db, employee, leave_rules, attendance_policy):
    """Employee with entitlements allocated for the current year."""
    service = LeaveService()
    service.allocate_year(year=date.today().year)
    db.session.commit()
    return service


class TestRequestLifecycle:
    def test_annual_request_produces_suggestions(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()

        assert request.status == LeaveStatus.SUGGESTED.value
        assert request.suggestions
        assert len(request.suggestions) <= 3
        assert request.days > 0
        # The top suggestion is the window on the request itself.
        assert request.suggestions[0]["start_date"] == request.start_date.isoformat()

    def test_suggested_request_reserves_balance(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()

        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        assert balance.pending == request.days
        assert balance.used == 0

    def test_request_writes_history(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        history = leave_ready.history_for(request.id)
        assert len(history) == 1
        assert history[0].from_status is None
        assert history[0].to_status == LeaveStatus.SUGGESTED.value

    def test_special_leave_skips_the_engine(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()

        assert request.status == LeaveStatus.PENDING.value
        assert request.suggestions is None
        # The seeded sick rule defaults to 3 days.
        assert request.days == 3

    def test_maternity_uses_its_default_days(self, leave_ready, employee):
        # Maternity applies to female employees, so record that first.
        from app.features.employees.service import EmployeeService

        EmployeeService().update_employee(employee.id, gender="female")
        db.session.commit()

        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.MATERNITY
        )
        db.session.commit()
        assert request.status == LeaveStatus.PENDING.value
        assert request.days == 90

    def test_balance_is_reserved_for_special_leave_too(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.SICK,
        )
        assert balance.pending == request.days


class TestAcceptAndDecide:
    def test_accept_moves_reservation_into_used(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        days = request.days

        updated = leave_ready.respond(request_id=request.id, action="accept")
        db.session.commit()

        assert updated.status == LeaveStatus.APPROVED.value
        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        assert balance.used == days
        assert balance.pending == 0

    def test_admin_approve_a_pending_request(self, leave_ready, employee, admin_user):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        days = request.days

        updated = leave_ready.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        assert updated.status == LeaveStatus.APPROVED.value
        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.SICK,
        )
        assert balance.used == days
        assert balance.pending == 0

    def test_admin_deny_releases_the_reservation(self, leave_ready, employee, admin_user):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()

        updated = leave_ready.decide(
            request_id=request.id, decision="deny", actor_user_id=admin_user.id
        )
        db.session.commit()

        assert updated.status == LeaveStatus.DENIED.value
        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.SICK,
        )
        assert balance.pending == 0
        assert balance.used == 0

    def test_cannot_decide_twice(self, leave_ready, employee, admin_user):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.SICK
        )
        db.session.commit()
        leave_ready.decide(
            request_id=request.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        with pytest.raises(InvalidLeaveTransitionError):
            leave_ready.decide(
                request_id=request.id, decision="deny", actor_user_id=admin_user.id
            )


class TestOneDenialRule:
    def test_first_denial_resuggests_a_different_window(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        original_start = request.start_date

        updated = leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        assert updated.denial_count == 1
        assert updated.start_date != original_start
        # A window that was already declined is not offered again.
        offered = {
            entry["start_date"] for entry in (updated.suggestions or [])
        }
        assert original_start.isoformat() not in offered

    def test_first_denial_releases_and_reserves_again(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()

        updated = leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=updated.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        # One reservation outstanding, for the newly suggested window only.
        assert balance.pending == updated.days
        assert balance.used == 0

    def test_second_denial_force_approves(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()

        leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        forced = leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        assert forced.status == LeaveStatus.APPROVED_FORCED.value
        assert forced.denial_count == 1  # the single denial is not incremented twice

    def test_force_approval_consumes_the_balance(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()
        forced = leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=forced.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        assert balance.used == forced.days
        assert balance.pending == 0

    def test_every_transition_is_recorded_in_history(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()
        leave_ready.respond(request_id=request.id, action="deny")
        db.session.commit()

        history = leave_ready.history_for(request.id)
        transitions = [(row.from_status, row.to_status) for row in history]
        # Creation, then the re-suggestion after the first denial (the status
        # stays SUGGESTED because a new window is offered), then force-approval.
        assert (None, "suggested") in transitions
        assert ("suggested", "suggested") in transitions
        assert ("suggested", "approved_forced") in transitions
        # One history row per transition, in order.
        assert len(history) == 3
        assert [row.to_status for row in history][-1] == "approved_forced"


class TestCancellation:
    def test_cancelling_approved_returns_days(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        approved = leave_ready.respond(request_id=request.id, action="accept")
        db.session.commit()
        days = approved.days

        leave_ready.cancel(request_id=request.id)
        db.session.commit()

        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=approved.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        assert balance.used == 0
        assert balance.pending == 0
        assert days > 0

    def test_cancelling_a_suggestion_releases_the_reservation(self, leave_ready, employee):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()

        leave_ready.cancel(request_id=request.id)
        db.session.commit()

        balance = leave_ready.get_balance(
            employee_id=employee.id,
            year=request.start_date.year,
            leave_type=LeaveType.ANNUAL,
        )
        assert balance.pending == 0


class TestDepartmentCalendar:
    def test_calendar_reflects_approved_leave(self, leave_ready, employee, department):
        request = leave_ready.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=3
        )
        db.session.commit()
        approved = leave_ready.respond(request_id=request.id, action="accept")
        db.session.commit()

        calendar = leave_ready.build_dept_calendar(
            department_id=department.id,
            start=approved.start_date,
            end=approved.end_date,
        )
        # The requesting employee is excluded from their own suggestion calendar
        # by default only when explicitly asked, so they appear here.
        assert calendar.on_leave(approved.start_date) >= 1
        assert calendar.headcount >= 1

    def test_capacity_cap_keeps_windows_off_a_full_day(
        self, clean_db, employee, department, position, leave_rules, attendance_policy
    ):
        """With max 1 off per department, no suggested window may overlap.

        The engine does not fail outright: it offers later windows where the
        department is clear again.
        """
        from app.core.time import each_day
        from app.features.employees.service import EmployeeService
        from app.features.leave.rules_service import LeaveRulesService

        rules = LeaveRulesService()
        annual = rules.rules.find(
            department_id=None, leave_type=LeaveType.ANNUAL.value
        )
        rules.update_rule(annual.id, max_employees_off_per_dept=1)
        db.session.commit()

        service = LeaveService()
        service.allocate_year(year=date.today().year)
        db.session.commit()

        # First employee books and is approved.
        first = service.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()
        approved = service.respond(request_id=first.id, action="accept")
        db.session.commit()
        taken = set(each_day(approved.start_date, approved.end_date))

        second_employee, _ = EmployeeService().create_employee(
            first_name="Brian",
            last_name="Otieno",
            email="brian@ems.local",
            department_id=department.id,
            position_id=position.id,
            hired_at=date.today() - timedelta(days=200),
        )
        db.session.commit()
        service.allocate_year(year=date.today().year)
        db.session.commit()

        second = service.request_leave(
            employee_id=second_employee.id, leave_type=LeaveType.ANNUAL, days=2
        )
        db.session.commit()

        # Every offered window avoids the days the department is already full.
        assert second.suggestions
        for suggestion in second.suggestions:
            window_days = set(
                each_day(
                    date.fromisoformat(suggestion["start_date"]),
                    date.fromisoformat(suggestion["end_date"]),
                )
            )
            assert not (window_days & taken), (
                f"suggested window {suggestion['start_date']}..{suggestion['end_date']} "
                f"overlaps a day the department is already at capacity"
            )


class TestBalanceAllocation:
    def test_allocation_creates_balances_for_every_type(self, clean_db, employee, leave_rules):
        summary = LeaveService().allocate_year(year=date.today().year)
        db.session.commit()

        assert summary["created"] >= 5
        balances = LeaveService().balances_for_employee(
            employee.id, year=date.today().year
        )
        types = {balance.leave_type for balance in balances}
        assert LeaveType.ANNUAL.value in types
        assert LeaveType.SICK.value in types

    def test_allocation_is_idempotent(self, clean_db, employee, leave_rules):
        service = LeaveService()
        first = service.allocate_year(year=date.today().year)
        db.session.commit()
        second = service.allocate_year(year=date.today().year)
        db.session.commit()

        assert second["created"] == 0
        assert second["skipped"] >= first["created"]

    def test_overwrite_resets_entitlement_from_the_rule(self, clean_db, employee, leave_rules):
        service = LeaveService()
        service.allocate_year(year=date.today().year)
        db.session.commit()

        balance = service.get_balance(
            employee_id=employee.id,
            year=date.today().year,
            leave_type=LeaveType.ANNUAL,
        )
        original = balance.entitled
        service.adjust_balance(
            employee_id=employee.id,
            year=date.today().year,
            leave_type=LeaveType.ANNUAL,
            entitled=1,
        )
        db.session.commit()

        service.allocate_year(year=date.today().year, overwrite=True)
        db.session.commit()

        refreshed = service.get_balance(
            employee_id=employee.id,
            year=date.today().year,
            leave_type=LeaveType.ANNUAL,
        )
        assert refreshed.entitled == original

    def test_insufficient_balance_blocks_an_overlong_request(self, clean_db, employee, leave_rules, attendance_policy):
        service = LeaveService()
        service.allocate_year(year=date.today().year)
        # Grant only two days of annual leave.
        service.adjust_balance(
            employee_id=employee.id,
            year=date.today().year,
            leave_type=LeaveType.ANNUAL,
            entitled=2,
            used=0,
            pending=0,
        )
        db.session.commit()

        with pytest.raises(InsufficientBalanceError):
            service.request_leave(
                employee_id=employee.id, leave_type=LeaveType.ANNUAL, days=10
            )


class TestExplicitWindowValidation:
    def test_requested_window_must_respect_min_notice(self, clean_db, employee, leave_rules, attendance_policy):
        """Annual leave requires 7 days' notice in the seeded rules."""
        service = LeaveService()
        service.allocate_year(year=date.today().year)
        db.session.commit()

        tomorrow = date.today() + timedelta(days=1)
        with pytest.raises(LeaveRuleError):
            service.request_leave(
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=tomorrow,
                end_date=tomorrow + timedelta(days=1),
            )

    def test_requested_window_beyond_max_consecutive_is_rejected(self, clean_db, employee, leave_rules, attendance_policy):
        from app.features.leave.rules_service import LeaveRulesService

        rules = LeaveRulesService()
        annual = rules.rules.find(department_id=None, leave_type=LeaveType.ANNUAL.value)
        rules.update_rule(annual.id, min_notice_days=0, max_consecutive_days=5)
        db.session.commit()

        service = LeaveService()
        service.allocate_year(year=date.today().year)
        db.session.commit()

        start = date.today() + timedelta(days=10)
        with pytest.raises(LeaveRuleError):
            service.request_leave(
                employee_id=employee.id,
                leave_type=LeaveType.ANNUAL,
                start_date=start,
                end_date=start + timedelta(days=20),
            )
