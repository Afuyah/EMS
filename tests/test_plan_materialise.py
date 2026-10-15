"""Tests for materialising an annual plan into leave requests.

Regression coverage for a real defect: materialising wrote requests straight to
APPROVED without any capacity check, so a published roster could create an
overlap in a department that `decide()` would never have allowed.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.database import db
from app.features.employees.service import EmployeeService
from app.features.leave.constants import LeavePlanStatus, LeaveStatus, LeaveType
from app.features.leave.planner_service import LeavePlanService
from app.features.leave.service import LeaveService

pytestmark = pytest.mark.integration


def a_monday(offset_weeks: int = 8) -> date:
    day = date.today() + timedelta(weeks=offset_weeks)
    while day.weekday() != 0:
        day += timedelta(days=1)
    return day


class TestMaterialise:
    def _give_annual_balance(self, *, employee_id: int, year: int, entitled: int = 21):
        """Materialising needs a balance to consume."""
        from app.features.leave.repository import LeaveBalanceRepository

        balance = LeaveBalanceRepository().get_or_create(
            employee_id=employee_id,
            year=year,
            leave_type=LeaveType.ANNUAL.value,
            entitled=entitled,
        )
        balance.entitled = max(balance.entitled, entitled)
        db.session.flush()
        return balance

    def _plan_with_item(self, *, department_id: int, employee_id: int, start: date, days: int):
        """Create a published plan holding one placed item."""
        from app.features.leave.models import LeavePlan

        service = LeavePlanService()
        plan = service.plans.create(
            department_id=department_id,
            year=start.year,
            status=LeavePlanStatus.PUBLISHED.value,
            concurrency_limit=1,
        )
        end = start + timedelta(days=days - 1)
        service.plans.create_item(
            plan_id=plan.id,
            employee_id=employee_id,
            start_date=start,
            end_date=end,
            days=days,
            rationale="test",
        )
        db.session.flush()
        return plan

    def test_materialise_creates_approved_requests(
        self, clean_db, app_context, employee, department, admin_user,
        leave_rules, attendance_policy,
    ):
        start = a_monday()
        self._give_annual_balance(employee_id=employee.id, year=start.year)
        plan = self._plan_with_item(
            department_id=department.id, employee_id=employee.id,
            start=start, days=5,
        )
        db.session.commit()

        summary = LeavePlanService().create_requests_for_plan(
            plan_id=plan.id, actor_user_id=admin_user.id
        )
        db.session.commit()

        assert summary["created"] == 1, summary
        # Find the created request through the plan item link.
        item = plan.items[0]
        assert item.request_id is not None
        created = LeaveService().get_request(item.request_id)
        assert created.status == LeaveStatus.APPROVED.value
        assert created.employee_id == employee.id

    def test_materialise_respects_leave_already_approved(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        """The roster must not create an overlap with existing approved leave."""
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Colleague", email="bea.mat@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        start = a_monday()
        leave = LeaveService()

        # Alice already has this window approved outside the plan.
        existing = leave.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=4), days=5,
        )
        db.session.commit()
        leave.decide(
            request_id=existing.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        # The roster proposes the SAME window for her colleague.
        self._give_annual_balance(employee_id=colleague.id, year=start.year)
        plan = self._plan_with_item(
            department_id=department.id, employee_id=colleague.id,
            start=start, days=5,
        )
        db.session.commit()

        summary = LeavePlanService().create_requests_for_plan(
            plan_id=plan.id, actor_user_id=admin_user.id
        )
        db.session.commit()

        assert summary["created"] == 0, "an overlapping allocation was approved"
        assert summary["blocked"], "the clash should be reported, not silently skipped"
        reason = summary["blocked"][0]["reason"].lower()
        assert "department" in reason or "colleague" in reason

        # No request was created for the colleague, so the clash never landed.
        assert not LeaveService().requests.overlap_exists(
            employee_id=colleague.id, start=start, end=start + timedelta(days=4)
        )
        # Alice's approved leave is still the only one in that window.
        with_peers = LeaveService().capacity_breach(
            department_id=department.id,
            leave_type=LeaveType.ANNUAL.value,
            start=start,
            end=start + timedelta(days=4),
        )
        assert with_peers.breached, "Alice's own leave should occupy the window"

    def test_materialise_allows_a_clear_window(
        self, clean_db, app_context, employee, department, position, admin_user,
        leave_rules, attendance_policy,
    ):
        """A non-clashing window is still created normally."""
        colleague, _ = EmployeeService().create_employee(
            first_name="Bea", last_name="Clear", email="bea.clear@ems.local",
            department_id=department.id, position_id=position.id,
        )
        db.session.commit()

        start = a_monday()
        leave = LeaveService()
        existing = leave.request_leave(
            employee_id=employee.id, leave_type=LeaveType.ANNUAL,
            start_date=start, end_date=start + timedelta(days=4), days=5,
        )
        db.session.commit()
        leave.decide(
            request_id=existing.id, decision="approve", actor_user_id=admin_user.id
        )
        db.session.commit()

        # A window well after the first one.
        later = start + timedelta(days=60)
        self._give_annual_balance(employee_id=colleague.id, year=later.year)
        plan = self._plan_with_item(
            department_id=department.id, employee_id=colleague.id,
            start=later, days=5,
        )
        db.session.commit()

        summary = LeavePlanService().create_requests_for_plan(
            plan_id=plan.id, actor_user_id=admin_user.id
        )
        db.session.commit()
        assert summary["created"] == 1
        assert not summary["blocked"]

    def test_materialise_is_idempotent(
        self, clean_db, app_context, employee, department, admin_user,
        leave_rules, attendance_policy,
    ):
        """Running it twice must not create duplicates."""
        start = a_monday()
        self._give_annual_balance(employee_id=employee.id, year=start.year)
        plan = self._plan_with_item(
            department_id=department.id, employee_id=employee.id,
            start=start, days=5,
        )
        db.session.commit()

        service = LeavePlanService()
        first = service.create_requests_for_plan(
            plan_id=plan.id, actor_user_id=admin_user.id
        )
        db.session.commit()
        second = service.create_requests_for_plan(
            plan_id=plan.id, actor_user_id=admin_user.id
        )
        db.session.commit()

        assert first["created"] == 1
        assert second["created"] == 0
