"""Leave service: the rule-based request lifecycle and suggestion engine.

Stateless and Flask-free.  It reads the calendar, builds a
:class:`~app.features.leave.engine.DeptCalendar` from exactly two queries, runs
the pure engine, and records every transition in ``leave_request_history``.

Balance accounting follows the request state:

* ``SUGGESTED`` / ``PENDING`` reserve days in ``balance.pending``
* approval moves the reservation into ``balance.used``
* denial or cancellation releases the reservation
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.time import each_day, is_weekend, today_local
from app.features.employees.exceptions import EmployeeNotFoundError
from app.features.leave.constants import (
    DECIDABLE_STATUSES,
    EVENT_LEAVE_APPROVED,
    EVENT_LEAVE_CANCELLED,
    EVENT_LEAVE_DENIED,
    EVENT_LEAVE_FORCED,
    EVENT_LEAVE_RECALLED,
    EVENT_LEAVE_REQUESTED,
    EVENT_LEAVE_SUGGESTED,
    PLAN_SEARCH_HORIZON_DAYS,
    SUGGESTION_HORIZON_DAYS,
    SUGGESTION_LIMIT,
    LeaveStatus,
    LeaveType,
)
from app.features.leave.engine import (
    DeptCalendar,
    RuleConstraints,
    ScoredWindow,
    Window,
    chargeable_days,
    check_capacity,
    find_best_windows,
    next_free_window,
)
# Reused so the in-app HR alert and the HR email share one event name.
from app.features.email.constants import EVENT_LEAVE_PENDING_HR
from app.features.leave.exceptions import (
    DepartmentAtCapacityError,
    InsufficientBalanceError,
    InvalidLeaveTransitionError,
    LeaveBalanceNotFoundError,
    LeaveRequestNotFoundError,
    LeaveRuleError,
    NoSuggestionAvailableError,
    RecallNotAllowedError,
)
from app.features.leave.models import LeaveBalance, LeaveRequest
from app.features.leave.repository import (
    LeaveBalanceRepository,
    LeaveHistoryRepository,
    LeaveRequestRepository,
)
from app.features.leave.rules_service import LeaveRulesService
from app.features.leave.state_machine import (
    TransitionOutcome,
    apply_admin_decision,
    apply_cancellation,
    apply_employee_response,
    apply_recall,
    windows_from_suggestions,
)

logger = get_logger(__name__)


class LeaveService:
    """Use-cases for requesting, responding to, deciding and cancelling leave."""

    def __init__(
        self,
        requests: LeaveRequestRepository | None = None,
        balances: LeaveBalanceRepository | None = None,
        history: LeaveHistoryRepository | None = None,
        rules: LeaveRulesService | None = None,
        employees=None,
        notifications=None,
        email=None,
    ) -> None:
        self.requests = requests or LeaveRequestRepository()
        self.balances = balances or LeaveBalanceRepository()
        self.history = history or LeaveHistoryRepository()
        self.rules = rules or LeaveRulesService()
        self._employees = employees
        self._notifications = notifications
        self._email = email

    # ---- lazy cross-feature collaborators -------------------------------
    @property
    def employees(self):
        """Cross-feature reads go through the employees *service*."""
        if self._employees is None:
            from app.features.employees.service import EmployeeService

            self._employees = EmployeeService()
        return self._employees

    @property
    def notifications(self):
        if self._notifications is None:
            from app.features.notifications.service import NotificationService

            self._notifications = NotificationService()
        return self._notifications

    @property
    def email(self):
        """Outbound email service (cross-feature call through its service)."""
        if self._email is None:
            from app.features.email.service import EmailService

            self._email = EmailService()
        return self._email

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def chargeable_days(window: Window) -> int:
        """Leave charged against balance: weekdays inside the window."""
        return max(1, chargeable_days(window)) if window.days else 0

    def build_dept_calendar(
        self,
        *,
        department_id: int | None,
        start: date,
        end: date,
        exclude_employee_id: int | None = None,
    ) -> DeptCalendar:
        """Build the department availability map using exactly two queries."""
        # Query 1: who is in the department.
        employee_ids = (
            self.employees.ids_in_department(department_id)
            if department_id is not None
            else self.employees.active_employee_ids()
        )
        headcount = len(employee_ids)

        # Query 2: who already has live leave overlapping the window.
        live = self.requests.live_in_window(
            start=start, end=end, employee_ids=employee_ids
        )

        by_date: dict[date, set[int]] = {}
        for request in live:
            for day in each_day(request.start_date, request.end_date):
                if start <= day <= end:
                    by_date.setdefault(day, set()).add(request.employee_id)

        excluded = {exclude_employee_id} if exclude_employee_id else set()
        return DeptCalendar(
            department_id=department_id,
            headcount=headcount,
            on_leave_by_date={day: frozenset(ids) for day, ids in by_date.items()},
            excluded_employee_ids=frozenset(excluded),
        )

    # ---- requesting ------------------------------------------------------
    def request_leave(
        self,
        *,
        employee_id: int,
        leave_type: str | LeaveType,
        start_date: date | None = None,
        end_date: date | None = None,
        days: int | None = None,
        reason: str | None = None,
    ) -> LeaveRequest:
        """Create a leave request.

        Special leaves (sick/maternity/paternity) skip the suggestion engine and
        go straight to ``PENDING`` using the rule's ``default_days``.  Everything
        else is solved for the best window and lands in ``SUGGESTED``.
        """
        kind = LeaveType.coerce(leave_type)

        # Cross-feature read through the employees service.
        employee = self.employees.get_employee(employee_id)
        if employee is None:
            raise EmployeeNotFoundError(f"Employee {employee_id} was not found.")

        # Maternity and paternity are sex-specific, so a mismatch is refused here
        # rather than only hidden in the form (a form can be bypassed).
        self.employees.assert_leave_type_allowed(employee, kind)

        department_id = employee.department_id
        tz_name = employee.tz
        today = today_local(tz_name)

        constraints = self.rules.resolve_constraints(
            department_id=department_id, leave_type=kind
        )
        year = (start_date or today).year

        # ---- special leaves: no engine, straight to pending ---------------
        if kind.skips_suggestion_engine:
            default_days = self.rules.default_days(
                department_id=department_id, leave_type=kind
            )
            # Special leave is configured in calendar days (90 for maternity,
            # 14 for paternity), and produces no suggestion window, so the days
            # are charged as configured rather than collapsed to weekdays.
            length = max(1, int(days or default_days))
            window = Window(
                start_date=start_date or today,
                end_date=end_date or (start_date or today) + timedelta(days=length - 1),
            )
            charged = length
            self._assert_balance_available(
                employee_id=employee_id, year=year, leave_type=kind, days=charged
            )
            balance = self._balance_for(
                employee_id=employee_id, year=year, leave_type=kind, department_id=department_id
            )
            self.balances.add_pending(balance, charged)

            request = self.requests.create(
                employee_id=employee_id,
                leave_type=kind.value,
                start_date=window.start_date,
                end_date=window.end_date,
                days=charged,
                status=LeaveStatus.PENDING.value,
                suggestions=None,
                reason=reason,
            )
            self._record(
                request,
                from_status=None,
                to_status=LeaveStatus.PENDING.value,
                note=f"{kind.value} leave requested ({charged} day(s)).",
            )
            self._notify_requested(request, employee)
            logger.info(
                "leave pending id=%s employee=%s type=%s days=%s",
                request.id, employee_id, kind.value, charged,
            )
            return request

        # ---- annual-style leaves: solve for the best window ---------------
        requested_days = self._resolve_requested_days(
            employee_id=employee_id,
            year=year,
            leave_type=kind,
            department_id=department_id,
            today=today,
            days=days,
            start_date=start_date,
            end_date=end_date,
        )

        if start_date is not None and end_date is not None:
            preferred = Window(start_date=start_date, end_date=end_date)
            self._assert_window_allowed(
                window=preferred,
                constraints=constraints,
                today=today,
                calendar=None,
                leave_type=kind,
            )
            window = preferred
            scored: list[ScoredWindow] = []
        else:
            scored = self.suggest(
                employee_id=employee_id,
                leave_type=kind,
                days=requested_days,
                department_id=department_id,
                tz_name=tz_name,
            )
            if not scored:
                raise NoSuggestionAvailableError()
            window = scored[0].window

        charged = self.chargeable_days(window)
        self._assert_balance_available(
            employee_id=employee_id, year=year, leave_type=kind, days=charged
        )

        suggestions = self._serialise_suggestions(scored) if scored else None

        balance = self._balance_for(
            employee_id=employee_id, year=year, leave_type=kind, department_id=department_id
        )
        self.balances.add_pending(balance, charged)

        request = self.requests.create(
            employee_id=employee_id,
            leave_type=kind.value,
            start_date=window.start_date,
            end_date=window.end_date,
            days=charged,
            status=LeaveStatus.SUGGESTED.value,
            suggestions=suggestions,
            reason=reason,
        )
        self._record(
            request,
            from_status=None,
            to_status=LeaveStatus.SUGGESTED.value,
            note=f"Suggested window {window.start_date}..{window.end_date} "
                 f"({charged} day(s)).",
        )
        self._notify_suggested(request, employee, window)
        logger.info(
            "leave suggested id=%s employee=%s type=%s window=%s..%s days=%s",
            request.id, employee_id, kind.value,
            window.start_date, window.end_date, charged,
        )
        return request

    def suggest(
        self,
        *,
        employee_id: int,
        leave_type: str | LeaveType,
        days: int,
        department_id: int | None = None,
        tz_name: str | None = None,
        exclude_ranges: list[Window] | None = None,
    ) -> list[ScoredWindow]:
        """Run the pure engine over a freshly built department calendar."""
        kind = LeaveType.coerce(leave_type)
        if department_id is None or tz_name is None:
            employee = self.employees.get_employee(employee_id)
            if employee is None:
                raise EmployeeNotFoundError(f"Employee {employee_id} was not found.")
            department_id = department_id if department_id is not None else employee.department_id
            tz_name = tz_name or employee.tz

        today = today_local(tz_name)
        constraints = self.rules.resolve_constraints(
            department_id=department_id, leave_type=kind
        )
        horizon = min(
            SUGGESTION_HORIZON_DAYS,
            (constraints.max_consecutive_days or SUGGESTION_HORIZON_DAYS) + SUGGESTION_HORIZON_DAYS,
        )
        calendar = self.build_dept_calendar(
            department_id=department_id,
            start=today,
            end=today + timedelta(days=SUGGESTION_HORIZON_DAYS),
            exclude_employee_id=employee_id,
        )
        return find_best_windows(
            today=today,
            days=days,
            calendar=calendar,
            constraints=constraints,
            horizon_days=SUGGESTION_HORIZON_DAYS,
            limit=SUGGESTION_LIMIT,
            excluded_ranges=exclude_ranges or (),
        )

    # ---- department concurrency quota -----------------------------------
    def capacity_limit_for(
        self, *, department_id: int | None, leave_type: str
    ) -> int | None:
        """The configured per-day concurrency cap for this leave type."""
        constraints = self.rules.resolve_constraints(
            department_id=department_id, leave_type=leave_type
        )
        return constraints.max_employees_off_per_dept

    def peers_on_leave_by_date(
        self,
        *,
        department_id: int | None,
        start: date,
        end: date,
        exclude_employee_id: int | None = None,
        leave_types: list[str] | None = None,
    ) -> dict[date, int]:
        return self.requests.peers_on_leave_by_date(
            department_id=department_id,
            start=start,
            end=end,
            exclude_employee_id=exclude_employee_id,
            leave_types=leave_types,
        )

    def capacity_breach(
        self,
        *,
        department_id: int | None,
        leave_type: str,
        start: date,
        end: date,
        exclude_employee_id: int | None = None,
    ):
        """Pure capacity verdict for a proposed window (no exception raised)."""
        limit = self.capacity_limit_for(
            department_id=department_id, leave_type=leave_type
        )
        peers = self.peers_on_leave_by_date(
            department_id=department_id,
            start=start,
            end=end,
            exclude_employee_id=exclude_employee_id,
            leave_types=[leave_type],
        )
        return check_capacity(
            Window(start_date=start, end_date=end),
            peers_on_leave_by_date=peers,
            limit=limit,
        )

    def next_free_window(
        self,
        *,
        department_id: int | None,
        leave_type: str,
        earliest: date,
        days: int,
        exclude_employee_id: int | None = None,
    ) -> Window | None:
        """The soonest window of ``days`` that respects the cap."""
        limit = self.capacity_limit_for(
            department_id=department_id, leave_type=leave_type
        )
        if limit is None:
            # No cap configured: nothing to avoid.
            return Window(
                start_date=earliest,
                end_date=earliest + timedelta(days=max(0, days - 1)),
            )

        horizon_end = earliest + timedelta(days=PLAN_SEARCH_HORIZON_DAYS)
        peers = self.peers_on_leave_by_date(
            department_id=department_id,
            start=earliest,
            end=horizon_end,
            exclude_employee_id=exclude_employee_id,
            leave_types=[leave_type],
        )
        return next_free_window(
            earliest=earliest,
            days=days,
            peers_on_leave_by_date=peers,
            limit=limit,
            horizon_days=PLAN_SEARCH_HORIZON_DAYS,
        )

    def _enforce_capacity(
        self,
        *,
        request: LeaveRequest,
        force: bool = False,
        actor_user_id: int | None = None,
    ) -> None:
        """Refuse to put more colleagues off than the cap allows.

        Called from every path that commits days, so the cap cannot be bypassed
        by supplying explicit dates instead of accepting a suggestion.

        ``force`` is the audited HR override: it records a warning in the
        request's reason so the exception is visible later rather than silent.
        """
        employee = request.employee
        department_id = employee.department_id if employee else None

        breach = self.capacity_breach(
            department_id=department_id,
            leave_type=request.leave_type,
            start=request.start_date,
            end=request.end_date,
            exclude_employee_id=request.employee_id,
        )
        if not breach.breached:
            return

        alternative = self.next_free_window(
            department_id=department_id,
            leave_type=request.leave_type,
            earliest=request.end_date + timedelta(days=1),
            days=request.days,
            exclude_employee_id=request.employee_id,
        )

        if force:
            logger.warning(
                "leave capacity OVERRIDDEN request=%s by user=%s: %s",
                request.id, actor_user_id, breach.message,
            )
            marker = f"[capacity override: {breach.message}]"
            request.reason = (
                f"{request.reason}\n\n{marker}" if request.reason else marker
            )
            return

        raise DepartmentAtCapacityError(
            breach.message,
            breaches=[
                {"date": day.isoformat(), "on_leave": count}
                for day, count in breach.over_capacity
            ],
            alternative=alternative.as_dict() if alternative else None,
        )

    # ---- employee response ----------------------------------------------
    def respond(
        self,
        *,
        request_id: int,
        action: str,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> LeaveRequest:
        """Employee accepts or denies the suggested window.

        A second denial does not reject the request: it force-approves the
        current window and tells the caller the one denial has been spent.
        """
        request = self.get_request(request_id)
        current = LeaveStatus.coerce(request.status)

        outcome = apply_employee_response(
            status=current,
            denial_count=request.denial_count,
            event=action,
            days=request.days,
        )

        employee = request.employee

        # Committing days must respect the cap, whichever side triggers it.
        # Checked before the balance moves so a refusal changes nothing.
        if outcome.forced or outcome.to_status is LeaveStatus.APPROVED:
            self._enforce_capacity(request=request, actor_user_id=actor_user_id)

        if outcome.forced:
            # Denial limit reached: force-approve the current window.
            self._consume(request, days=request.days)
            self._apply_outcome(request, outcome, actor_user_id=actor_user_id, note=note)
            self._notify_forced(request, employee)
            logger.info("leave force-approved id=%s", request.id)
            return request

        if outcome.to_status is LeaveStatus.DENIED:
            # Release the reservation and offer the next-best window.
            self._refund(request, days=request.days)
            self._re_apply_suggestion(request, actor_user_id=actor_user_id, note=note)
            self._notify_denied(request, employee)
            logger.info("leave denied (1st) id=%s, re-suggested", request.id)
            return request

        # Accepted.
        self._consume(request, days=request.days)
        self._apply_outcome(request, outcome, actor_user_id=actor_user_id, note=note)
        self._notify_approved(request, employee)
        logger.info("leave approved by employee id=%s", request.id)
        return request

    # ---- admin decision --------------------------------------------------
    def decide(
        self,
        *,
        request_id: int,
        decision: str,
        actor_user_id: int,
        note: str | None = None,
        force: bool = False,
    ) -> LeaveRequest:
        """HR/Admin approves or denies a request.

        Approving respects the per-department concurrency cap.  ``force=True``
        is the audited HR override for genuine exceptions; the breach is written
        into the request reason so it stays visible.
        """
        request = self.get_request(request_id)
        current = LeaveStatus.coerce(request.status)
        if current not in DECIDABLE_STATUSES:
            raise InvalidLeaveTransitionError(
                f"This request is already {current.value} and cannot be decided."
            )

        approve = str(decision).strip().lower() == "approve"

        # Checked before any balance is consumed, so a refused approval leaves
        # the request exactly as it was.
        if approve:
            self._enforce_capacity(
                request=request, force=force, actor_user_id=actor_user_id
            )

        outcome = apply_admin_decision(
            status=current, approve=approve, days=request.days
        )

        employee = request.employee
        if approve:
            self._consume(request, days=request.days)
            self._apply_outcome(request, outcome, actor_user_id=actor_user_id, note=note)
            self._notify_approved(request, employee, by_hr=True)
        else:
            self._refund(request, days=request.days)
            self._apply_outcome(request, outcome, actor_user_id=actor_user_id, note=note)
            self._notify_denied(request, employee, by_hr=True)

        logger.info(
            "leave %s by hr id=%s%s",
            "approved" if approve else "denied",
            request.id,
            " (capacity overridden)" if approve and force else "",
        )
        return request

    # ---- cancellation ----------------------------------------------------
    def cancel(
        self,
        *,
        request_id: int,
        actor_user_id: int | None = None,
        note: str | None = None,
    ) -> LeaveRequest:
        """Cancel a request and release any days it was holding."""
        request = self.get_request(request_id)
        current = LeaveStatus.coerce(request.status)
        outcome = apply_cancellation(status=current, days=request.days)

        self._refund(request, days=request.days)
        self._apply_outcome(request, outcome, actor_user_id=actor_user_id, note=note)
        self._notify_cancelled(request, request.employee)
        logger.info("leave cancelled id=%s", request.id)
        return request

    # ---- recall ----------------------------------------------------------
    #: Statuses from which an employee can be recalled.
    RECALLABLE_STATUSES: frozenset[LeaveStatus] = frozenset(
        {LeaveStatus.APPROVED, LeaveStatus.APPROVED_FORCED}
    )

    def recall(
        self,
        *,
        request_id: int,
        actor_user_id: int,
        reason: str | None = None,
        propose_new_dates: bool = True,
    ) -> dict:
        """Recall an employee from approved leave and propose new dates.

        The recalled request keeps its history and its days are refunded.  When
        ``propose_new_dates`` is set, a fresh suggested request is created for
        the same number of days so the employee gets a concrete replacement
        window to confirm — nothing is silently rewritten onto the old row.
        """
        request = self.get_request(request_id)
        current = LeaveStatus.coerce(request.status)
        if current not in self.RECALLABLE_STATUSES:
            raise RecallNotAllowedError(
                f"Only approved leave can be recalled; this request is {current.value}."
            )

        outcome = apply_recall(status=current, days=request.days)
        self._refund(request, days=request.days)
        self._apply_outcome(
            request,
            outcome,
            actor_user_id=actor_user_id,
            note=reason or "Recalled by HR.",
        )
        request.recall_reason = reason or "Recalled by HR."
        self.requests.update(request)

        replacement = None
        if propose_new_dates and request.days > 0:
            try:
                replacement = self.request_leave(
                    employee_id=request.employee_id,
                    leave_type=request.leave_type,
                    days=request.days,
                    reason=(
                        f"Rescheduled after recall of request #{request.id}."
                        + (f" {reason}" if reason else "")
                    ),
                )
                request.rescheduled_to_id = replacement.id
                self.requests.update(request)
            except AppError:
                # No window fits (for example the department is booked out for
                # the whole horizon).  The recall still stands; HR is told.
                logger.warning(
                    "recall id=%s could not propose new dates", request.id
                )
                replacement = None

        self._notify_recalled(request, request.employee, reason=reason)
        logger.info(
            "leave recalled id=%s by user=%s replacement=%s",
            request.id, actor_user_id, getattr(replacement, "id", None),
        )
        return {"recalled": request, "replacement": replacement}

    # ---- reads -----------------------------------------------------------
    def get_request(self, request_id: int) -> LeaveRequest:
        request = self.requests.get(request_id)
        if request is None:
            raise LeaveRequestNotFoundError(
                f"Leave request {request_id} was not found."
            )
        return request
        request = self.requests.get(request_id)
        if request is None:
            raise LeaveRequestNotFoundError(f"Leave request {request_id} was not found.")
        return request

    def history_for(self, request_id: int):
        return self.history.for_request(request_id)

    def list_requests(self, **filters) -> list[LeaveRequest]:
        return list(self.requests.search(**filters))

    def list_requests_paginated(self, *, page: int, per_page: int, **filters):
        return self.requests.search_paginated(page=page, per_page=per_page, **filters)

    def my_requests(self, employee_id: int, *, statuses: list[str] | None = None):
        return list(self.requests.search(employee_id=employee_id, statuses=statuses))

    def my_pending(self, employee_id: int) -> list[LeaveRequest]:
        return list(self.requests.pending_for_employee(employee_id))

    def my_active(self, employee_id: int, *, today: date | None = None) -> list[LeaveRequest]:
        return list(
            self.requests.live_for_employee(employee_id, on_or_after=today)
        )

    def active_for_employee_on(self, employee_id: int, day: date) -> LeaveRequest | None:
        for request in self.requests.active_on(day):
            if request.employee_id == employee_id:
                return request
        return None

    def department_queue(self, department_id: int, *, statuses: list[str] | None = None):
        return list(
            self.requests.search(department_id=department_id, statuses=statuses)
        )

    def counts_by_status(
        self, *, year: int | None = None, department_id: int | None = None
    ) -> dict[str, int]:
        """Status -> count.  Pass ``department_id`` so the numbers match a
        department-filtered queue rather than always reporting global totals."""
        return {
            row["status"]: int(row["total"] or 0)
            for row in self.requests.counts_by_status(
                year=year, department_id=department_id
            )
        }

    def calendar(
        self, *, department_id: int | None, start: date, end: date
    ) -> list[dict]:
        """Approved leave in a window; ``None`` covers every department."""
        return self.requests.calendar_rows(
            department_id=department_id, start=start, end=end
        )

    def teammates_on_leave(
        self, *, department_id: int, start: date, end: date, exclude_employee_id: int | None = None
    ) -> list[dict]:
        return self.requests.teammates_on_leave(
            department_id=department_id,
            start=start,
            end=end,
            exclude_employee_id=exclude_employee_id,
        )

    # ---- balances --------------------------------------------------------
    def get_balance(
        self, *, employee_id: int, year: int, leave_type: str | LeaveType
    ) -> LeaveBalance | None:
        return self.balances.get_scope(
            employee_id=employee_id, year=year, leave_type=LeaveType.coerce(leave_type).value
        )

    def get_balance_or_raise(
        self, *, employee_id: int, year: int, leave_type: str | LeaveType
    ) -> LeaveBalance:
        balance = self.get_balance(
            employee_id=employee_id, year=year, leave_type=leave_type
        )
        if balance is None:
            raise LeaveBalanceNotFoundError(
                "No leave balance exists for that employee, year and type."
            )
        return balance

    def balances_for_employee(self, employee_id: int, *, year: int | None = None):
        if year is None:
            return list(self.balances.for_employee(employee_id))
        return list(self.balances.for_employee_year(employee_id, year))

    def department_ledger(self, department_id: int, *, year: int) -> list[dict]:
        return self.balances.for_department(department_id, year)

    def utilisation(self, *, year: int, department_id: int | None = None) -> list[dict]:
        return self.balances.utilisation_by_type(year=year, department_id=department_id)

    def allocate_year(
        self,
        *,
        year: int,
        employee_ids: list[int] | None = None,
        overwrite: bool = False,
    ) -> dict:
        """Ensure every active employee has balances for ``year``.

        Entitlement comes from the resolved rule for each employee's department.
        ``overwrite`` resets ``entitled`` from the rule rather than only filling
        in missing rows.
        """
        if employee_ids is None:
            employee_ids = self.employees.active_employee_ids()

        created = 0
        updated = 0
        skipped = 0

        for employee_id in employee_ids:
            employee = self.employees.get_employee(employee_id)
            if employee is None or not employee.is_active:
                skipped += 1
                continue
            for kind in LeaveType:
                try:
                    entitled = self.rules.annual_entitlement(
                        department_id=employee.department_id, leave_type=kind
                    )
                except Exception:  # noqa: BLE001 - a missing rule is not fatal
                    skipped += 1
                    continue
                if not entitled:
                    continue

                existing = self.balances.get_scope(
                    employee_id=employee_id, year=year, leave_type=kind.value
                )
                if existing is None:
                    self.balances.create(
                        employee_id=employee_id,
                        year=year,
                        leave_type=kind.value,
                        entitled=entitled,
                    )
                    created += 1
                elif overwrite:
                    if existing.entitled != entitled:
                        self.balances.update(existing, entitled=entitled)
                        updated += 1
                else:
                    skipped += 1

        logger.info(
            "allocate_year year=%s created=%s updated=%s skipped=%s",
            year, created, updated, skipped,
        )
        return {"year": year, "created": created, "updated": updated, "skipped": skipped}

    def adjust_balance(
        self,
        *,
        employee_id: int,
        year: int,
        leave_type: str | LeaveType,
        entitled: int | None = None,
        used: int | None = None,
        pending: int | None = None,
    ) -> LeaveBalance:
        """Manual balance correction (HR/Admin)."""
        kind = LeaveType.coerce(leave_type)
        balance = self.balances.get_or_create(
            employee_id=employee_id, year=year, leave_type=kind.value
        )
        if entitled is not None:
            balance.entitled = max(0, int(entitled))
        if used is not None:
            balance.used = max(0, int(used))
        if pending is not None:
            balance.pending = max(0, int(pending))
        return self.balances.update(balance)

    # ---- internal --------------------------------------------------------
    def _resolve_requested_days(
        self,
        *,
        employee_id: int,
        year: int,
        leave_type: LeaveType,
        department_id: int | None,
        today: date,
        days: int | None,
        start_date: date | None,
        end_date: date | None,
    ) -> int:
        """How many days the employee is asking for."""
        if start_date is not None and end_date is not None:
            if end_date < start_date:
                raise ValidationError("End date cannot be earlier than start date.")
            return self.chargeable_days(Window(start_date, end_date))
        if days:
            return int(days)

        # Default from the balance's remaining availability, else the rule.
        balance = self.balances.get_scope(
            employee_id=employee_id, year=year, leave_type=leave_type.value
        )
        if balance is not None and balance.available > 0:
            return int(balance.available)
        return int(
            self.rules.default_days(department_id=department_id, leave_type=leave_type)
        )

    def _serialise_suggestions(self, scored: list[ScoredWindow]) -> list[dict]:
        return [item.as_suggestion(rank=index + 1) for index, item in enumerate(scored)]

    def _balance_for(
        self,
        *,
        employee_id: int,
        year: int,
        leave_type: LeaveType,
        department_id: int | None,
    ) -> LeaveBalance:
        """Fetch or lazily create the balance row for this scope."""
        balance = self.balances.get_scope(
            employee_id=employee_id, year=year, leave_type=leave_type.value
        )
        if balance is not None:
            return balance
        entitled = 0
        try:
            entitled = self.rules.annual_entitlement(
                department_id=department_id, leave_type=leave_type
            )
        except Exception:  # noqa: BLE001 - fall back to zero entitlement
            entitled = 0
        return self.balances.create(
            employee_id=employee_id,
            year=year,
            leave_type=leave_type.value,
            entitled=entitled,
        )

    def _assert_balance_available(
        self, *, employee_id: int, year: int, leave_type: LeaveType, days: int
    ) -> None:
        """Refuse the request when entitlement is already exhausted."""
        balance = self.balances.get_scope(
            employee_id=employee_id, year=year, leave_type=leave_type.value
        )
        if balance is None:
            return  # first request of the year; entitlement is created below
        if days > balance.available:
            raise InsufficientBalanceError(
                f"Only {balance.available} day(s) of {leave_type.value} leave remain, "
                f"but {days} were requested."
            )

    def _assert_window_allowed(
        self,
        *,
        window: Window,
        constraints: RuleConstraints,
        today: date,
        calendar: DeptCalendar | None,
        leave_type: LeaveType,
    ) -> None:
        """Validate an explicitly requested window against the active rules."""
        if not constraints.notice_ok(window, today):
            raise LeaveRuleError(
                f"This leave type requires at least {constraints.min_notice_days} "
                f"day(s) of notice."
            )
        if (
            constraints.max_consecutive_days is not None
            and window.days > constraints.max_consecutive_days
        ):
            raise LeaveRuleError(
                f"The maximum consecutive duration for {leave_type.value} leave is "
                f"{constraints.max_consecutive_days} day(s)."
            )

    def _consume(self, request: LeaveRequest, *, days: int) -> None:
        """Move reserved days from ``pending`` into ``used``."""
        balance = self.balances.get_scope(
            employee_id=request.employee_id,
            year=request.start_date.year,
            leave_type=request.leave_type,
        )
        if balance is None:
            return
        self.balances.add_pending(balance, -days)
        self.balances.add_used(balance, days)

    def _refund(self, request: LeaveRequest, *, days: int) -> None:
        """Release days held by a denied or cancelled request."""
        balance = self.balances.get_scope(
            employee_id=request.employee_id,
            year=request.start_date.year,
            leave_type=request.leave_type,
        )
        if balance is None:
            return
        current = LeaveStatus.coerce(request.status)
        if current.is_live:
            # Already approved: give the consumed days back.
            self.balances.add_used(balance, -days)
        else:
            # Only reserved: release the reservation.
            self.balances.add_pending(balance, -days)

    def _apply_outcome(
        self,
        request: LeaveRequest,
        outcome: TransitionOutcome,
        *,
        actor_user_id: int | None,
        note: str | None,
    ) -> None:
        """Persist a state-machine outcome and append its history row."""
        from_status = request.status
        request.status = outcome.to_status.value
        request.denial_count = outcome.denial_count
        if outcome.to_status in {LeaveStatus.APPROVED, LeaveStatus.DENIED, LeaveStatus.APPROVED_FORCED}:
            request.decided_at = request.decided_at or _utcnow()
            if actor_user_id is not None:
                request.decided_by = actor_user_id
        if outcome.to_status is LeaveStatus.CANCELLED:
            if actor_user_id is not None:
                request.decided_by = request.decided_by or actor_user_id
        self.requests.update(request)

        self._record(
            request,
            from_status=from_status,
            to_status=outcome.to_status.value,
            actor_id=actor_user_id,
            note=note or outcome.note,
        )

    def _record(
        self,
        request: LeaveRequest,
        *,
        from_status: str | None,
        to_status: str,
        actor_id: int | None = None,
        note: str | None = None,
    ) -> None:
        """Append one row to ``leave_request_history``."""
        self.history.record(
            request_id=request.id,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            note=note,
        )

    def _re_apply_suggestion(
        self,
        request: LeaveRequest,
        *,
        actor_user_id: int | None,
        note: str | None,
    ) -> None:
        """Offer the next-best window after a first denial."""
        declined = windows_from_suggestions(request.suggestions)
        declined.append(Window(request.start_date, request.end_date))

        employee = request.employee
        day_count = request.days

        scored = self.suggest(
            employee_id=request.employee_id,
            leave_type=request.leave_type,
            days=day_count,
            department_id=employee.department_id if employee else None,
            tz_name=employee.tz if employee else None,
            exclude_ranges=declined,
        )

        from_status = request.status
        request.denial_count += 1
        request.status = LeaveStatus.SUGGESTED.value

        if scored:
            best = scored[0]
            request.suggestions = self._serialise_suggestions(scored)
            request.start_date = best.window.start_date
            request.end_date = best.window.end_date
            new_days = self.chargeable_days(best.window)
            request.days = new_days
            # Reserve the newly suggested window instead of the declined one.
            balance = self.balances.get_scope(
                employee_id=request.employee_id,
                year=best.window.start_date.year,
                leave_type=request.leave_type,
            )
            if balance is not None:
                self.balances.add_pending(balance, new_days)
            history_note = (
                f"{note or 'Employee declined.'} Re-suggested "
                f"{best.window.start_date}..{best.window.end_date}."
            )
        else:
            # Nothing left to offer: park the request as pending for HR.
            request.status = LeaveStatus.PENDING.value
            request.suggestions = None
            history_note = (
                f"{note or 'Employee declined.'} No further window is available; "
                f"escalated to HR."
            )

        self.requests.update(request)
        self._record(
            request,
            from_status=from_status,
            to_status=request.status,
            actor_id=actor_user_id,
            note=history_note,
        )

    # ---- notifications and email (best effort) ---------------------------
    #
    # Every method here is deliberately non-fatal.  In-app notifications flush;
    # email commits its own outbox row before touching the network.  Neither may
    # roll back the leave decision that triggered it.
    def _email_context(self, request: LeaveRequest, employee, **extra) -> dict:
        """Shared template context for every leave email."""
        department = getattr(employee, "department", None) if employee else None
        context = {
            "request_id": request.id,
            "leave_type": request.leave_type.replace("_", " "),
            "employee_name": getattr(employee, "full_name", "there") or "there",
            "employee_no": getattr(employee, "employee_no", "-"),
            "department_name": getattr(department, "name", "-") or "-",
            "start_date": request.start_date.strftime("%a %d %b %Y"),
            "end_date": request.end_date.strftime("%a %d %b %Y"),
            "days": request.days,
            "status": request.status,
            "reason": request.reason or "",
            "link": self._leave_link(request),
            "payload": {"request_id": request.id, "status": request.status},
        }
        context.update(extra)
        return context

    @staticmethod
    def _leave_link(request: LeaveRequest) -> str:
        from flask import current_app

        base = str(current_app.config.get("APP_BASE_URL", "")).rstrip("/")
        return f"{base}/app/leave/admin" if base else "/app/leave/admin"

    def _email_employee(
        self, request: LeaveRequest, employee, *, event_type: str, **extra
    ) -> None:
        """Email the employee who owns the request."""
        if employee is None:
            return
        address = (getattr(employee, "email", None) or "").strip()
        if not address:
            return
        self.email.send_event(
            event_type=event_type,
            to_email=address,
            context=self._email_context(request, employee, **extra),
            recipient_user_id=employee.user_id,
            recipient_employee_id=employee.id,
        )

    def _email_hr(
        self,
        request: LeaveRequest,
        employee,
        *,
        event_type: str = "leave_submitted_hr",
        include_managers: bool = False,
        **extra,
    ) -> int:
        """Email HR/Admins (and optionally the department's managers)."""
        from app.features.email.constants import HR_RECIPIENT_ROLES

        return self.email.send_to_roles(
            event_type=event_type,
            context=self._email_context(request, employee, **extra),
            roles=HR_RECIPIENT_ROLES,
            department_id=getattr(employee, "department_id", None),
            include_department_managers=include_managers,
            # The employee's own login may be HR; never send them HR mail about
            # their own request.
            exclude_user_ids={employee.user_id} if employee and employee.user_id else set(),
        )

    def _notify(self, employee, *, event_type: str, subject: str, body: str, payload: dict) -> None:
        """Enqueue an in-app notification without ever breaking the transaction."""
        if employee is None:
            return
        try:
            self.notifications.enqueue(
                event_type=event_type,
                subject=subject,
                body=body,
                recipient_user_id=employee.user_id,
                recipient_employee_id=employee.id,
                payload=payload,
            )
        except Exception as error:  # noqa: BLE001 - notifications are best effort
            logger.warning(
                "notification enqueue failed event=%s employee=%s: %s",
                event_type, getattr(employee, "id", None), error,
            )

    def _notify_hr_in_app(self, request: LeaveRequest, employee, *, verb: str) -> None:
        """Put an in-app notification in every HR/Admin inbox."""
        from app.features.email.constants import HR_RECIPIENT_ROLES

        try:
            recipients = self.email.resolve_recipients(roles=HR_RECIPIENT_ROLES)
            for recipient in recipients:
                self.notifications.enqueue(
                    event_type=EVENT_LEAVE_PENDING_HR,
                    subject=f"Leave {verb}: {getattr(employee, 'full_name', 'employee')}",
                    body=(
                        f"{getattr(employee, 'full_name', 'An employee')} "
                        f"({getattr(employee, 'employee_no', '-')}) has a "
                        f"{request.leave_type} leave request for "
                        f"{request.start_date} to {request.end_date} "
                        f"({request.days} day(s)) that needs a decision."
                    ),
                    recipient_user_id=recipient["user_id"],
                    recipient_employee_id=None,
                    payload={"request_id": request.id, "status": request.status},
                )
        except Exception as error:  # noqa: BLE001 - best effort
            logger.warning("HR in-app notification failed: %s", error)

    # ---- per-event fan-out ----------------------------------------------
    def _notify_requested(self, request: LeaveRequest, employee) -> None:
        self._notify(
            employee,
            event_type=EVENT_LEAVE_REQUESTED,
            subject=f"{request.leave_type.title()} leave requested",
            body=(
                f"Your {request.leave_type} leave from {request.start_date} to "
                f"{request.end_date} ({request.days} day(s)) is pending HR approval."
            ),
            payload={"request_id": request.id, "status": request.status},
        )
        # "Request received" acknowledgement to the employee...
        self._email_employee(request, employee, event_type="leave_requested")
        # ...and an actionable alert to HR/Admins.
        self._email_hr(request, employee, include_managers=True)
        self._notify_hr_in_app(request, employee, verb="requested")

    def _notify_suggested(self, request: LeaveRequest, employee, window: Window) -> None:
        self._notify(
            employee,
            event_type=EVENT_LEAVE_SUGGESTED,
            subject="A leave window is suggested for you",
            body=(
                f"The best available window for your {request.leave_type} leave is "
                f"{window.start_date} to {window.end_date} "
                f"({request.days} day(s)). Accept or decline it in the portal."
            ),
            payload={
                "request_id": request.id,
                "start_date": window.start_date.isoformat(),
                "end_date": window.end_date.isoformat(),
                "days": request.days,
            },
        )
        self._email_employee(request, employee, event_type="leave_requested")
        # HR/Admins are alerted even while the employee still holds an
        # unaccepted suggestion: the intent is visible immediately, and the
        # request may still need a decision from them.
        self._email_hr(request, employee, include_managers=True)
        self._notify_hr_in_app(request, employee, verb="requested")

    def _notify_approved(self, request: LeaveRequest, employee, *, by_hr: bool = False) -> None:
        who = "HR approved" if by_hr else "You accepted"
        self._notify(
            employee,
            event_type=EVENT_LEAVE_APPROVED,
            subject=f"{request.leave_type.title()} leave approved",
            body=(
                f"{who} your {request.leave_type} leave for "
                f"{request.start_date} to {request.end_date} ({request.days} day(s))."
            ),
            payload={"request_id": request.id, "status": request.status},
        )
        decided_by = "HR" if by_hr else None
        self._email_employee(
            request, employee, event_type="leave_approved", decided_by=decided_by
        )
        # HR/Admin get told once the decision lands, so the outcome is on record
        # in their inbox too.
        self._email_hr(request, employee, event_type="leave_approved", decided_by=decided_by)

    def _notify_denied(self, request: LeaveRequest, employee, *, by_hr: bool = False) -> None:
        who = "HR declined" if by_hr else "You declined"
        self._notify(
            employee,
            event_type=EVENT_LEAVE_DENIED,
            subject=f"{request.leave_type.title()} leave declined",
            body=(
                f"{who} the leave window {request.start_date} to {request.end_date}. "
                f"One further decline is available before a window is confirmed."
            ),
            payload={"request_id": request.id, "status": request.status},
        )
        decided_by = "HR" if by_hr else None
        self._email_employee(
            request, employee, event_type="leave_denied", decided_by=decided_by
        )
        self._email_hr(request, employee, event_type="leave_denied", decided_by=decided_by)

    def _notify_forced(self, request: LeaveRequest, employee) -> None:
        self._notify(
            employee,
            event_type=EVENT_LEAVE_FORCED,
            subject="Your leave window has been confirmed",
            body=(
                f"Your one decline has been used, so the window "
                f"{request.start_date} to {request.end_date} is now confirmed."
            ),
            payload={"request_id": request.id, "status": request.status},
        )
        self._email_employee(request, employee, event_type="leave_forced")
        self._email_hr(request, employee, event_type="leave_forced")

    def _notify_cancelled(self, request: LeaveRequest, employee) -> None:
        self._notify(
            employee,
            event_type=EVENT_LEAVE_CANCELLED,
            subject=f"{request.leave_type.title()} leave cancelled",
            body=(
                f"The leave request for {request.start_date} to {request.end_date} "
                f"was cancelled."
            ),
            payload={"request_id": request.id, "status": request.status},
        )
        self._email_employee(request, employee, event_type="leave_cancelled")
        self._email_hr(request, employee, event_type="leave_cancelled")

    def _notify_recalled(
        self, request: LeaveRequest, employee, *, reason: str | None = None
    ) -> None:
        """Tell the employee they were recalled and what happens next."""
        self._notify(
            employee,
            event_type=EVENT_LEAVE_RECALLED,
            subject=f"{request.leave_type.title()} leave recalled",
            body=(
                f"HR recalled your leave for {request.start_date} to "
                f"{request.end_date}. The {request.days} day(s) are back in your "
                f"balance."
                + (f" Reason: {reason}." if reason else "")
                + " A replacement window has been proposed for you to confirm."
            ),
            payload={
                "request_id": request.id,
                "status": request.status,
                "reason": reason,
            },
        )
        self._email_employee(request, employee, event_type="leave_recalled")
        self._email_hr(request, employee, event_type="leave_recalled")


def _utcnow():
    from app.core.time import utcnow

    return utcnow()


def weekday_count(start: date, end: date) -> int:
    """Utility kept public for reports/tests: weekdays in an inclusive range."""
    return sum(1 for day in each_day(start, end) if not is_weekend(day))
