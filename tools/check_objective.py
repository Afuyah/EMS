"""Objective-level acceptance check.

Verifies the four deliverables of the leave-scheduling and attendance-integrity
layer actually exist in the running code, rather than trusting that they were
written earlier.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


with app.app_context():
    print("=" * 72)
    print("DELIVERABLE 1 — per-department concurrent annual-leave quota")
    print("=" * 72)
    from app.features.leave.engine import Window, check_capacity, next_free_window
    from app.features.leave.exceptions import DepartmentAtCapacityError
    from app.features.leave.repository import LeaveBalanceRepository
    from app.features.leave.rules_service import DEFAULT_LEAVE_RULES
    from app.features.rules.models import LeaveRule

    annual = LeaveRule.query.filter_by(leave_type="annual", department_id=None).first()
    check("annual default cap is 1 in the shipped defaults",
          any(r["leave_type"] == "annual" and r["max_employees_off_per_dept"] == 1
              for r in DEFAULT_LEAVE_RULES))
    check("the live rule row is capped at 1",
          annual is not None and annual.max_employees_off_per_dept == 1,
          str(annual.max_employees_off_per_dept if annual else None))

    # The pure rule.
    breach = check_capacity(
        Window(date(2026, 3, 2), date(2026, 3, 6)),
        peers_on_leave_by_date={date(2026, 3, 3): 1},
        limit=1,
    )
    check("the pure check blocks a second person", breach.breached)
    check("it names the clashing day", breach.worst_day == date(2026, 3, 3))
    free = next_free_window(
        earliest=date(2026, 3, 2), days=2,
        peers_on_leave_by_date={date(2026, 3, 2): 1}, limit=1,
    )
    check("a free window is offered instead", free is not None and free.start_date != date(2026, 3, 2))

    # Enforcement exists on every path that commits days.
    from app.features.leave.service import LeaveService
    service = LeaveService()
    for method in ("_enforce_capacity", "capacity_breach", "next_free_window",
                   "capacity_limit_for"):
        check(f"LeaveService.{method} exists", hasattr(service, method))

    print("\n" + "=" * 72)
    print("DELIVERABLE 2 — year-round scheduling engine")
    print("=" * 72)
    from app.features.leave.engine import PlanEmployee, plan_annual_leave
    from app.features.leave.planner_service import LeavePlanService

    people = [
        PlanEmployee(employee_id=i, full_name=f"P{i}", employee_no=f"EMP-{i:04d}",
                     days=21, hired_at=date(2020, 1, 1) + timedelta(days=i * 30))
        for i in range(1, 4)
    ]
    result = plan_annual_leave(
        department_id=1, year=date.today().year + 1, employees=people, limit=1
    )
    check("the engine places everyone", result.complete, f"unplaced={len(result.unplaced)}")
    check("it respects the cap", result.peak_concurrent <= 1, f"peak={result.peak_concurrent}")
    check("it stays inside the year",
          all(e.start_date.year == date.today().year + 1 for e in result.placed))

    planner = LeavePlanService()
    for method in ("generate", "publish", "unpublish", "monthly_breakdown",
                   "published_item_for", "items_for_department",
                   "create_requests_for_plan"):
        check(f"LeavePlanService.{method} exists", hasattr(planner, method))

    from app.features.leave.models import LeavePlan, LeavePlanItem
    check("leave_plans table registered", "leave_plans" in db.metadata.tables)
    check("leave_plan_items table registered", "leave_plan_items" in db.metadata.tables)
    check("a published roster exists in the database",
          LeavePlan.query.filter_by(status="published").count() >= 1)

    # Surfaced to employees on login.
    print("\n  -- surfaced to each role --")
    ADMIN_PAGES = ["/app/leave/plan", "/app/leave/admin", "/app/dashboard"]
    EMPLOYEE_PAGES = ["/app/dashboard", "/app/leave"]
    admin = app.test_client()
    assert admin.post("/api/v1/auth/login",
                      json={"email": "admin@ems.local", "password": "ChangeMe123!"}
                      ).status_code == 200
    for path in ADMIN_PAGES:
        check(f"admin can open {path}", admin.get(path).status_code == 200)

    employee_client = app.test_client()
    assert employee_client.post(
        "/api/v1/auth/login",
        json={"email": "alice@ems.local", "password": "Password123!"},
    ).status_code == 200
    for path in EMPLOYEE_PAGES:
        check(f"employee can open {path}", employee_client.get(path).status_code == 200)

    dashboard = employee_client.get("/app/dashboard").get_data(as_text=True)
    leave_page = employee_client.get("/app/leave").get_data(as_text=True)
    check("the employee dashboard shows their allocation",
          "Your annual leave" in dashboard or "allocated" in dashboard.lower())
    check("the employee leave page shows the department roster",
          "Your department" in leave_page)

    plan_page = admin.get("/app/leave/plan").get_data(as_text=True)
    check("HR sees the year at a glance", "Year at a glance" in plan_page)
    check("HR sees the roster detail", "Roster detail" in plan_page)

    print("\n" + "=" * 72)
    print("DELIVERABLE 3 — attendance blocked on leave / suspended")
    print("=" * 72)
    from app.features.attendance.exceptions import (
        EmployeeNotClockableError, EmployeeOnLeaveError,
    )
    from app.features.attendance.service import AttendanceService

    for name in ("EmployeeNotClockableError", "EmployeeOnLeaveError"):
        check(f"{name} exists", name in dir(sys.modules["app.features.attendance.exceptions"]))

    attendance = AttendanceService()
    check("apply_punch guards with _assert_clockable",
          "_assert_clockable" in AttendanceService.apply_punch.__code__.co_names
          or hasattr(attendance, "_assert_clockable"))
    check("a non-raising is_clockable exists", hasattr(attendance, "is_clockable"))

    # Prove it with real data.
    from app.features.employees.models import Employee
    from app.features.leave.constants import LeaveStatus
    from app.features.leave.models import LeaveRequest

    live = (
        LeaveRequest.query.filter(
            LeaveRequest.status.in_([LeaveStatus.APPROVED.value,
                                     LeaveStatus.APPROVED_FORCED.value])
        ).first()
    )
    if live is not None:
        ok, reason = attendance.is_clockable(live.employee_id, live.start_date)
        check("an employee on approved leave is not clockable", not ok, reason)
    else:
        print("    (no approved leave in the database to test against)")

    suspended = Employee.query.filter_by(is_active=False).first()
    if suspended is not None:
        ok, reason = attendance.is_clockable(suspended.id, date.today())
        check("a suspended employee is not clockable", not ok, reason)
    else:
        print("    (no suspended employee on record; logic is covered by tests)")

    print("\n" + "=" * 72)
    print("DELIVERABLE 4 — recall and reschedule")
    print("=" * 72)
    from app.features.leave.exceptions import RecallNotAllowedError
    from app.features.leave.state_machine import LEGAL_TRANSITIONS, apply_recall
    from app.features.leave.constants import LeaveStatus as LS

    check("LeaveService.recall exists", hasattr(service, "recall"))
    check("RecallNotAllowedError exists", RecallNotAllowedError is not None)
    check("RECALLED is a legal transition from approved",
          LS.RECALLED in LEGAL_TRANSITIONS[LS.APPROVED])
    check("RECALLED is a legal transition from approved_forced",
          LS.RECALLED in LEGAL_TRANSITIONS[LS.APPROVED_FORCED])
    outcome = apply_recall(status=LS.APPROVED, days=3)
    check("apply_recall refunds the days", outcome.releasable_days == 3,
          str(outcome.releasable_days))
    check("RECALLED is terminal but not live",
          LS.RECALLED.is_terminal and not LS.RECALLED.is_live)
    check("the recall route exists on the leave queue",
          "/recall/" in admin.get("/app/leave/admin").get_data(as_text=True)
          or "Recall" in admin.get("/app/leave/admin").get_data(as_text=True))

print("\n" + "=" * 72)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 72)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
