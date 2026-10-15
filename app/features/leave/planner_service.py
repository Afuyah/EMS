"""Annual leave planning: draft a department roster, publish it, and arrange it.

The planner itself is pure (see :func:`app.features.leave.engine.plan_annual_leave`);
this service supplies it with real data, persists the result, and owns the
draft -> published lifecycle.

Publishing is the moment the dates become visible to employees, so a
half-finished roster can never be mistaken for a decision.
"""

from __future__ import annotations

from datetime import date

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.leave.constants import (
    LeavePlanStatus,
    LeaveStatus,
    LeaveType,
)
from app.features.leave.engine import (
    PlanEmployee,
    PlanResult,
    Window,
    plan_annual_leave,
)
from app.features.leave.exceptions import (
    LeavePlanNotFoundError,
    LeaveRuleError,
)
from app.features.leave.models import LeavePlan, LeavePlanItem, LeaveRequest
from app.features.leave.repository import (
    LeavePlanRepository,
    LeaveBalanceRepository,
    LeaveRequestRepository,
)
from app.features.leave.rules_service import LeaveRulesService

logger = get_logger(__name__)


class LeavePlanService:
    """Generate and manage annual leave rosters."""

    def __init__(
        self,
        *,
        plans: LeavePlanRepository | None = None,
        requests: LeaveRequestRepository | None = None,
        balances: LeaveBalanceRepository | None = None,
        rules: LeaveRulesService | None = None,
    ) -> None:
        self.plans = plans or LeavePlanRepository()
        self.requests = requests or LeaveRequestRepository()
        self.balances = balances or LeaveBalanceRepository()
        self.rules = rules or LeaveRulesService()

    # ---- reads -----------------------------------------------------------
    def get_plan(self, plan_id: int) -> LeavePlan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise LeavePlanNotFoundError(f"Leave plan {plan_id} was not found.")
        return plan

    def find_plan(self, *, department_id: int | None, year: int) -> LeavePlan | None:
        return self.plans.for_department_year(
            department_id=department_id, year=year
        )

    def list_plans(self, *, year: int | None = None) -> list[LeavePlan]:
        return self.plans.all_plans(year=year)

    def published_item_for(
        self, *, employee_id: int, year: int
    ) -> LeavePlanItem | None:
        """The employee's published allocation, if HR has published one.

        This is what the employee sees on their dashboard: a firm allocated
        window rather than a suggestion they have to act on.
        """
        return self.plans.published_item_for(employee_id=employee_id, year=year)

    def items_for_department(
        self, *, department_id: int | None, year: int, published_only: bool = True
    ) -> list[LeavePlanItem]:
        return self.plans.items_for_department(
            department_id=department_id, year=year, published_only=published_only
        )

    def monthly_breakdown(
        self, *, department_id: int | None, year: int
    ) -> list[dict]:
        """Who is off in each month, for the year-at-a-glance view.

        Built from published plan items plus live leave requests, so approved
        leave that was never planned still appears.

        An allocation may legitimately spill past 31 December (a December start
        plus its working days).  Those days must not be silently dropped, so
        they are attributed to the months they actually fall in, and anything
        landing in a later year is reported separately rather than raising an
        index error.
        """
        months: list[dict] = [
            {"month": month, "label": _MONTHS[month - 1], "items": []}
            for month in range(1, 13)
        ]

        def attribute(
            *,
            kind: str,
            employee_id: int,
            name: str,
            employee_no: str,
            start_date: date,
            end_date: date,
            days: int,
        ) -> None:
            """Add one absence to every month it touches within ``year``."""
            for month in range(1, 13):
                month_start = date(year, month, 1)
                month_end = _last_day_of_month(year, month)
                # Only count the overlap with this month.
                if end_date < month_start or start_date > month_end:
                    continue
                months[month - 1]["items"].append(
                    {
                        "kind": kind,
                        "employee_id": employee_id,
                        "name": name,
                        "employee_no": employee_no,
                        "start_date": max(start_date, month_start),
                        "end_date": min(end_date, month_end),
                        "days": days,
                    }
                )

        for item in self.items_for_department(
            department_id=department_id, year=year, published_only=True
        ):
            if not item.is_placed:
                continue
            attribute(
                kind="plan",
                employee_id=item.employee_id,
                name=item.employee.full_name if item.employee else "—",
                employee_no=item.employee.employee_no if item.employee else "",
                start_date=item.start_date,
                end_date=item.end_date,
                days=item.days,
            )

        # Approved requests that are not part of the plan.
        planned = {
            item.request_id
            for item in self.items_for_department(
                department_id=department_id, year=year, published_only=True
            )
            if item.request_id
        }
        live = self.requests.live_in_window(
            start=date(year, 1, 1), end=date(year, 12, 31)
        )
        for request in live:
            if request.id in planned or request.leave_type != LeaveType.ANNUAL.value:
                continue
            employee = request.employee
            if department_id is not None and (
                employee is None or employee.department_id != department_id
            ):
                continue
            attribute(
                kind="request",
                employee_id=request.employee_id,
                name=employee.full_name if employee else "—",
                employee_no=employee.employee_no if employee else "",
                start_date=request.start_date,
                end_date=request.end_date,
                days=request.days,
            )

        for month in months:
            month["items"].sort(key=lambda row: (row["start_date"], row["name"]))
            month["count"] = len(month["items"])
        return months

    # ---- generation ------------------------------------------------------
    def generate(
        self,
        *,
        department_id: int | None,
        year: int,
        actor_user_id: int | None = None,
        overwrite: bool = False,
        blackout_dates: list[date] | None = None,
    ) -> tuple[LeavePlan, PlanResult]:
        """Draft (or re-draft) the roster for one department and year.

        Existing *published* plans are protected unless ``overwrite`` is set, so
        regenerating cannot silently change dates employees have already seen.
        """
        existing = self.find_plan(department_id=department_id, year=year)
        if existing is not None and existing.is_published and not overwrite:
            raise LeaveRuleError(
                "This plan is already published. Re-generating it would change "
                "dates employees have already been told, so pass overwrite to "
                "replace it deliberately."
            )

        limit = self._concurrency_limit(department_id)
        employees = self._plan_employees(department_id=department_id, year=year)

        result = plan_annual_leave(
            department_id=department_id,
            year=year,
            employees=employees,
            limit=limit,
            blackout_dates=blackout_dates or (),
        )

        if existing is None:
            plan = self.plans.create(
                department_id=department_id,
                year=year,
                status=LeavePlanStatus.DRAFT.value,
                concurrency_limit=limit,
                created_by=actor_user_id,
            )
        else:
            plan = existing
            plan.status = LeavePlanStatus.DRAFT.value
            plan.concurrency_limit = limit
            plan.published_at = None
            plan.published_by = None
            # Re-drafting replaces the roster wholesale.
            self.plans.clear_items(plan)
            self.plans.update(plan)

        for entry in result.entries:
            self.plans.create_item(
                plan_id=plan.id,
                employee_id=entry.employee_id,
                start_date=entry.start_date,
                end_date=entry.end_date,
                days=entry.days,
                rationale=entry.reason,
                preferred_month=entry.preferred_month,
            )

        self.plans.update(plan)
        logger.info(
            "leave plan generated dept=%s year=%s placed=%s peak=%s limit=%s",
            department_id, year, len(result.placed), result.peak_concurrent, limit,
        )
        return plan, result

    def publish(self, *, plan_id: int, actor_user_id: int) -> LeavePlan:
        """Make the roster visible to employees."""
        plan = self.get_plan(plan_id)
        unplaced = plan.unplaced_count
        plan.status = LeavePlanStatus.PUBLISHED.value
        plan.published_at = utcnow()
        plan.published_by = actor_user_id
        self.plans.update(plan)
        logger.info(
            "leave plan published id=%s dept=%s year=%s unplaced=%s",
            plan.id, plan.department_id, plan.year, unplaced,
        )
        return plan

    def unpublish(self, *, plan_id: int) -> LeavePlan:
        """Pull the roster back to draft so employees stop seeing the dates."""
        plan = self.get_plan(plan_id)
        plan.status = LeavePlanStatus.DRAFT.value
        plan.published_at = None
        self.plans.update(plan)
        return plan

    def delete(self, *, plan_id: int) -> None:
        plan = self.get_plan(plan_id)
        self.plans.delete(plan)

    # ---- materialising into real requests -------------------------------
    def create_requests_for_plan(
        self, *, plan_id: int, actor_user_id: int, only_employee_ids: list[int] | None = None
    ) -> dict:
        """Turn published allocations into approved leave requests.

        Approval is deliberate and immediate: the roster *is* the decision.
        Each request still goes through the normal balance accounting **and the
        department concurrency check**, because the roster was drafted against
        the plan's own occupancy — leave that was already approved outside the
        plan can still clash with it.
        """
        plan = self.get_plan(plan_id)
        wanted = set(only_employee_ids) if only_employee_ids else None

        # The same rule the approval path uses, so materialising a roster can
        # never create an overlap that decide() would have refused.
        from app.features.leave.service import LeaveService

        leave = LeaveService()

        created = 0
        skipped = 0
        blocked: list[dict] = []
        failures: list[dict] = []

        for item in plan.items:
            if not item.is_placed:
                skipped += 1
                continue
            if wanted is not None and item.employee_id not in wanted:
                skipped += 1
                continue
            if item.request_id is not None:
                skipped += 1
                continue

            # Never double-book: skip if live leave already covers the window.
            if self.requests.overlap_exists(
                employee_id=item.employee_id,
                start=item.start_date,
                end=item.end_date,
            ):
                skipped += 1
                continue

            # Respect the per-department cap against leave already approved.
            breach = leave.capacity_breach(
                department_id=self._department_of(item.employee_id),
                leave_type=LeaveType.ANNUAL.value,
                start=item.start_date,
                end=item.end_date,
                exclude_employee_id=item.employee_id,
            )
            if breach.breached:
                logger.warning(
                    "plan id=%s skipped employee=%s: %s",
                    plan.id, item.employee_id, breach.message,
                )
                blocked.append(
                    {
                        "employee_id": item.employee_id,
                        "employee_no": (
                            item.employee.employee_no if item.employee else ""
                        ),
                        "start_date": item.start_date.isoformat(),
                        "end_date": item.end_date.isoformat(),
                        "reason": breach.message,
                    }
                )
                continue

            try:
                request = self._approve_allocation(
                    item=item, actor_user_id=actor_user_id
                )
            except Exception as error:  # noqa: BLE001 - reported, not fatal
                failures.append(
                    {
                        "employee_id": item.employee_id,
                        "error": str(error),
                    }
                )
                continue

            item.request_id = request.id
            self.plans.update_item(item)
            created += 1

        logger.info(
            "leave plan id=%s materialised created=%s skipped=%s blocked=%s failed=%s",
            plan.id, created, skipped, len(blocked), len(failures),
        )
        return {
            "plan_id": plan.id,
            "created": created,
            "skipped": skipped,
            "blocked": blocked,
            "failures": failures,
        }

    def _department_of(self, employee_id: int) -> int | None:
        """The employee's department, read through the employees repository."""
        from app.features.employees.repository import EmployeeRepository

        employee = EmployeeRepository().get(employee_id)
        return getattr(employee, "department_id", None)

    def _approve_allocation(self, *, item: LeavePlanItem, actor_user_id: int):
        """Create an already-approved request for a planned window."""
        from app.features.leave.models import LeaveBalance

        year = item.start_date.year
        balance = self.balances.get_scope(
            employee_id=item.employee_id,
            year=year,
            leave_type=LeaveType.ANNUAL.value,
        )
        if balance is None:
            raise LeaveRuleError(
                "No annual leave balance is allocated for that year."
            )
        if balance.available < item.days:
            raise LeaveRuleError(
                f"Only {balance.available} day(s) available but {item.days} planned."
            )

        # The roster is the approval, so consume straight into used.
        self.balances.add_used(balance, item.days)

        request = self.requests.create(
            employee_id=item.employee_id,
            leave_type=LeaveType.ANNUAL.value,
            start_date=item.start_date,
            end_date=item.end_date,
            days=item.days,
            status=LeaveStatus.APPROVED.value,
            suggestions=None,
            reason="Allocated in the published annual leave plan.",
            decided_by=actor_user_id,
            decided_at=utcnow(),
        )
        return request

    # ---- internals -------------------------------------------------------
    def _concurrency_limit(self, department_id: int | None) -> int | None:
        constraints = self.rules.resolve_constraints(
            department_id=department_id, leave_type=LeaveType.ANNUAL
        )
        return constraints.max_employees_off_per_dept

    def _plan_employees(
        self, *, department_id: int | None, year: int
    ) -> list[PlanEmployee]:
        """Everyone to plan for, with their entitlement and any preference."""
        from app.features.employees.repository import EmployeeRepository

        employees = EmployeeRepository()
        # ``ids_in_department`` reads better than filtering the full list, so each
        # case calls the query that matches it.
        ids = (
            employees.ids_in_department(department_id, active_only=True)
            if department_id is not None
            else employees.active_employee_ids()
        )

        planned: list[PlanEmployee] = []
        for employee_id in ids:
            employee = employees.get(employee_id)
            if employee is None:
                continue

            entitlement = self.rules.annual_entitlement(
                department_id=employee.department_id,
                leave_type=LeaveType.ANNUAL,
            )

            # Subtract days already approved this year so the roster reflects
            # the real remaining entitlement.
            balance = self.balances.get_scope(
                employee_id=employee.id,
                year=year,
                leave_type=LeaveType.ANNUAL.value,
            )
            if balance is not None:
                remaining = max(0, balance.entitled - balance.used - balance.pending)
            else:
                remaining = max(0, int(entitlement or 0))

            planned.append(
                PlanEmployee(
                    employee_id=employee.id,
                    full_name=employee.full_name,
                    employee_no=employee.employee_no,
                    days=remaining,
                    hired_at=employee.hired_at,
                    preferred_month=None,
                )
            )
        return planned


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _last_day_of_month(year: int, month: int) -> date:
    """The final calendar day of a month."""
    import calendar as _calendar

    return date(year, month, _calendar.monthrange(year, month)[1])
