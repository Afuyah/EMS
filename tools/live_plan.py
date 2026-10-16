"""Live check: annual plan generation, publish, employee visibility, recall."""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_CONFIG"] = "development"

from app import create_app
from app.core.database import db

app = create_app("development")
FAILURES: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" â€” {detail}" if not condition else ""))
    if not condition:
        FAILURES.append(label)


with app.app_context():
    from app.features.employees.models import Employee
    from app.features.employees.repository import EmployeeRepository
    from app.features.employees.models import Department
    from app.features.leave.constants import LeaveStatus, LeavePlanStatus, LeaveType
    from app.features.leave.models import LeavePlan, LeavePlanItem
    from app.features.leave.planner_service import LeavePlanService
    from app.features.leave.repository import LeaveBalanceRepository
    from app.features.leave.rules_service import LeaveRulesService

    year = date.today().year
    service = LeavePlanService()
    employees = EmployeeRepository()

    departments = Department.query.order_by(Department.name).all()
    # Use the department with the most employees so the cap is actually tested.
    busiest = max(
        departments,
        key=lambda d: len(employees.ids_in_department(d.id, active_only=True)),
    )
    headcount = len(employees.ids_in_department(busiest.id, active_only=True))
    print(f"=== planning '{busiest.name}' ({headcount} active employees) for {year} ===")
    check("the test department has at least 2 people", headcount >= 2,
          f"headcount={headcount}")

    limit = service._concurrency_limit(busiest.id)
    print(f"  configured concurrency limit: {limit}")
    check("annual leave is capped at 1 per department", limit == 1, f"limit={limit}")

    # Make sure everyone has a balance to plan against.
    balances = LeaveBalanceRepository()
    rules = LeaveRulesService()
    for employee_id in employees.ids_in_department(busiest.id, active_only=True):
        employee = employees.get(employee_id)
        entitled = rules.annual_entitlement(
            department_id=employee.department_id, leave_type=LeaveType.ANNUAL
        )
        balance = balances.get_or_create(
            employee_id=employee_id,
            year=year,
            leave_type=LeaveType.ANNUAL.value,
            entitled=entitled,
        )
        balance.entitled = max(balance.entitled, entitled)
    db.session.commit()

    print("\n=== generate a draft roster ===")
    # An earlier run may have left a published plan; confirm the regeneration
    # pulls it back to draft, which is what hides the dates from employees.
    was_published = service.find_plan(
        department_id=busiest.id, year=year
    ) is not None and service.find_plan(
        department_id=busiest.id, year=year
    ).is_published

    plan, result = service.generate(
        department_id=busiest.id, year=year, actor_user_id=1, overwrite=True
    )
    db.session.commit()

    check("a plan row was created", plan.id is not None)
    check("it starts as a draft", plan.status == LeavePlanStatus.DRAFT.value,
          plan.status)
    check("regenerating pulled a published plan back to draft",
          (not was_published) or plan.status == LeavePlanStatus.DRAFT.value)
    check("roster items were written", len(plan.items) > 0, f"items={len(plan.items)}")
    check("peak concurrency respects the cap", result.peak_concurrent <= (limit or 99),
          f"peak={result.peak_concurrent}")
    unnamed = [e for e in result.placed]
    check("leave is spread over several months",
          len({e.start_date.month for e in unnamed}) >= 2,
          f"months={sorted({e.start_date.month for e in unnamed})}")

    print("\n=== verify no working day breaks the cap, from the database ===")
    by_day: dict[date, int] = {}
    for item in plan.items:
        if not item.is_placed:
            continue
        day = item.start_date
        while day <= item.end_date:
            if day.weekday() < 5:
                by_day[day] = by_day.get(day, 0) + 1
            day += timedelta(days=1)
    worst = max(by_day.values(), default=0)
    check("no working day has 2+ people on planned leave", worst <= 1, f"worst={worst}")

    print("\n=== employees cannot see a draft ===")
    sample_id = employees.ids_in_department(busiest.id, active_only=True)[0]
    check("no allocation visible while draft",
          service.published_item_for(employee_id=sample_id, year=year) is None)

    print("\n=== publish, then employees can see it ===")
    plan = service.publish(plan_id=plan.id, actor_user_id=1)
    db.session.commit()
    check("plan is published", plan.is_published, plan.status)

    item = service.published_item_for(employee_id=sample_id, year=year)
    check("the employee now has a visible allocation", item is not None)
    if item:
        print(f"    {item.employee.full_name}: {item.window} ({item.days} days)")
        check("the allocation has real dates", item.start_date is not None)

    print("\n=== month-by-month view is populated ===")
    months = service.monthly_breakdown(department_id=busiest.id, year=year)
    populated = [m for m in months if m["count"]]
    check("at least one month has entries", len(populated) >= 1,
          f"populated={len(populated)}")

    # An allocation can touch two months, so counting occurrences would
    # over-count. What matters is that no placed window is invisible.
    attributed = {
        (row["employee_id"], row["start_date"], row["end_date"])
        for month in months
        for row in month["items"]
    }
    placed_items = [
        item for item in plan.items if item.is_placed
    ]
    visible = [
        item for item in placed_items
        if any(
            row["employee_id"] == item.employee_id
            and row["start_date"] <= item.end_date
            and row["end_date"] >= item.start_date
            for month in months
            for row in month["items"]
        )
    ]
    check("every placed allocation is visible in at least one month",
          len(visible) == len(placed_items),
          f"visible={len(visible)} placed={len(placed_items)}")
    print(f"    {len(placed_items)} placed allocation(s), "
          f"{len(attributed)} month entries")

    print("\n=== the quota is enforced against real published leave ===")
    ids = employees.ids_in_department(busiest.id, active_only=True)
    if len(ids) >= 2:
        first, second = ids[0], ids[1]
        first_item = service.published_item_for(employee_id=first, year=year)
        if first_item:
            # Re-approve the first person's window, then try the second on the
            # same dates: the cap must refuse it.
            from app.features.leave.service import LeaveService

            leave = LeaveService()
            from app.features.leave.constants import LeaveStatus as LS

            created = leave.requests.create(
                employee_id=first, leave_type=LeaveType.ANNUAL.value,
                start_date=first_item.start_date, end_date=first_item.end_date,
                days=first_item.days, status=LS.APPROVED.value,
                suggestions=None, reason="live-check",
            )
            db.session.commit()

            from app.features.leave.exceptions import DepartmentAtCapacityError

            try:
                service._concurrency_limit(busiest.id)
                breach = leave.capacity_breach(
                    department_id=busiest.id, leave_type=LeaveType.ANNUAL.value,
                    start=first_item.start_date, end=first_item.end_date,
                    exclude_employee_id=second,
                )
                check("a second person on the same dates breaches the cap",
                      breach.breached, f"breach={breach.breached}")
            finally:
                db.session.delete(created)
                db.session.commit()

    print("\n=== recall returns the days and proposes dates ===")
    ids = employees.ids_in_department(busiest.id, active_only=True)
    target = ids[0]
    from app.features.leave.service import LeaveService

    leave = LeaveService()
    start = date.today() + timedelta(weeks=10)
    while start.weekday() != 0:
        start += timedelta(days=1)
    request = leave.requests.create(
        employee_id=target, leave_type=LeaveType.ANNUAL.value,
        start_date=start, end_date=start + timedelta(days=2), days=3,
        status=LeaveStatus.APPROVED.value, suggestions=None, reason="live-check recall",
    )
    db.session.commit()

    balance_before = leave.balances.get_scope(
        employee_id=target, year=start.year, leave_type=LeaveType.ANNUAL.value
    )
    used_before = balance_before.used if balance_before else 0

    outcome = leave.recall(
        request_id=request.id, actor_user_id=1, reason="Live check"
    )
    db.session.commit()

    check("the request is now recalled",
          leave.get_request(request.id).status == LeaveStatus.RECALLED.value,
          leave.get_request(request.id).status)
    balance_after = leave.balances.get_scope(
        employee_id=target, year=start.year, leave_type=LeaveType.ANNUAL.value
    )
    check("days were not left consumed",
          (balance_after.used if balance_after else 0) <= used_before,
          f"before={used_before} after={balance_after.used if balance_after else 'n/a'}")
    check("the recall reason is recorded",
          "Live check" in (leave.get_request(request.id).recall_reason or ""))
    replacement = outcome["replacement"]
    print(f"    replacement proposed: {replacement.id if replacement else None}")
    check("a replacement window was proposed", replacement is not None)

    # ---- tidy up every row this check created ----------------------------
    from app.features.leave.models import LeaveRequest

    for request_id in [
        request.id,
        replacement.id if replacement else None,
    ]:
        if request_id is None:
            continue
        row = db.session.get(LeaveRequest, request_id)
        if row is not None:
            db.session.delete(row)
    db.session.flush()
    # Reset the affected balance to a clean state.
    balance = leave.balances.get_scope(
        employee_id=target, year=start.year, leave_type=LeaveType.ANNUAL.value
    )
    if balance is not None:
        balance.used = 0
        balance.pending = 0
    db.session.commit()
    print("\n  cleaned up the rows created by this check")

print(f"\n=== RESULT: {len(FAILURES)} failure(s) ===")
for failure in FAILURES:
    print("  -", failure)
sys.exit(1 if FAILURES else 0)
