"""Leave repositories: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, or_, select

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.core.time import each_day
from app.features.leave.constants import (
    LIVE_LEAVE_STATUSES,
    LeavePlanStatus,
    LeaveStatus,
    LeaveType,
)
from app.features.leave.models import (
    LeaveBalance,
    LeavePlan,
    LeavePlanItem,
    LeaveRequest,
    LeaveRequestHistory,
)


class LeaveBalanceRepository(BaseRepository[LeaveBalance]):
    """Queries over ``leave_balances``."""

    model = LeaveBalance

    def get_scope(
        self, *, employee_id: int, year: int, leave_type: str
    ) -> LeaveBalance | None:
        stmt = select(LeaveBalance).where(
            LeaveBalance.employee_id == employee_id,
            LeaveBalance.year == year,
            LeaveBalance.leave_type == leave_type,
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def get_or_create(
        self,
        *,
        employee_id: int,
        year: int,
        leave_type: str,
        entitled: int = 0,
    ) -> LeaveBalance:
        record = self.get_scope(
            employee_id=employee_id, year=year, leave_type=leave_type
        )
        if record is None:
            record = self.create(
                employee_id=employee_id,
                year=year,
                leave_type=leave_type,
                entitled=entitled,
            )
        return record

    def for_employee_year(self, employee_id: int, year: int) -> list[LeaveBalance]:
        stmt = (
            select(LeaveBalance)
            .where(LeaveBalance.employee_id == employee_id, LeaveBalance.year == year)
            .order_by(LeaveBalance.leave_type)
        )
        return list(db.session.execute(stmt).scalars().all())

    def for_employee(self, employee_id: int) -> list[LeaveBalance]:
        stmt = (
            select(LeaveBalance)
            .where(LeaveBalance.employee_id == employee_id)
            .order_by(LeaveBalance.year.desc(), LeaveBalance.leave_type)
        )
        return list(db.session.execute(stmt).scalars().all())

    def for_department(self, department_id: int, year: int) -> list[dict]:
        """Department balance ledger in one query."""
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.code.label("department_code"),
                LeaveBalance.leave_type.label("leave_type"),
                LeaveBalance.entitled.label("entitled"),
                LeaveBalance.used.label("used"),
                LeaveBalance.pending.label("pending"),
                (LeaveBalance.entitled - LeaveBalance.used - LeaveBalance.pending).label(
                    "available"
                ),
            )
            .select_from(LeaveBalance)
            .join(Employee, Employee.id == LeaveBalance.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                Employee.department_id == department_id,
                LeaveBalance.year == year,
            )
            .order_by(Employee.last_name, Employee.first_name, LeaveBalance.leave_type)
        )
        return self.rows(stmt)

    def for_year(self, year: int) -> list[LeaveBalance]:
        stmt = select(LeaveBalance).where(LeaveBalance.year == year)
        return list(db.session.execute(stmt).scalars().all())

    def add_used(self, record: LeaveBalance, days: int) -> LeaveBalance:
        """Consume entitlement, clamped so ``used`` never goes negative."""
        record.used = max(0, record.used + days)
        db.session.flush()
        return record

    def add_pending(self, record: LeaveBalance, days: int) -> LeaveBalance:
        record.pending = max(0, record.pending + days)
        db.session.flush()
        return record

    def utilisation_by_type(self, *, year: int, department_id: int | None = None) -> list[dict]:
        """Entitled vs used per leave type (one query)."""
        from app.features.employees.models import Employee

        stmt = (
            select(
                LeaveBalance.leave_type.label("leave_type"),
                func.sum(LeaveBalance.entitled).label("entitled"),
                func.sum(LeaveBalance.used).label("used"),
                func.sum(LeaveBalance.pending).label("pending"),
                func.count(func.distinct(LeaveBalance.employee_id)).label("employees"),
            )
            .where(LeaveBalance.year == year)
            .group_by(LeaveBalance.leave_type)
            .order_by(LeaveBalance.leave_type)
        )
        if department_id is not None:
            stmt = stmt.join(
                Employee, Employee.id == LeaveBalance.employee_id
            ).where(Employee.department_id == department_id)
        rows = self.rows(stmt)
        for row in rows:
            entitled = int(row["entitled"] or 0)
            used = int(row["used"] or 0)
            row["entitled"] = entitled
            row["used"] = used
            row["pending"] = int(row["pending"] or 0)
            row["available"] = max(0, entitled - used - row["pending"])
            row["utilisation_percent"] = round((used / entitled) * 100) if entitled else 0
        return rows


class LeaveRequestRepository(BaseRepository[LeaveRequest]):
    """Queries over ``leave_requests``."""

    model = LeaveRequest

    # ---- filters ---------------------------------------------------------
    def _filtered_query(
        self,
        *,
        employee_id: int | None = None,
        employee_ids: list[int] | None = None,
        department_id: int | None = None,
        leave_type: str | None = None,
        status: str | None = None,
        statuses: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> Select:
        stmt = select(LeaveRequest)
        if employee_id is not None:
            stmt = stmt.where(LeaveRequest.employee_id == employee_id)
        if employee_ids is not None:
            stmt = stmt.where(LeaveRequest.employee_id.in_(employee_ids))
        if department_id is not None:
            from app.features.employees.models import Employee

            stmt = stmt.join(
                Employee, Employee.id == LeaveRequest.employee_id
            ).where(Employee.department_id == department_id)
        if leave_type:
            stmt = stmt.where(LeaveRequest.leave_type == leave_type)
        if status:
            stmt = stmt.where(LeaveRequest.status == status)
        if statuses:
            stmt = stmt.where(LeaveRequest.status.in_(statuses))
        # Overlap, not containment: a request that merely touches the window counts.
        if start is not None:
            stmt = stmt.where(LeaveRequest.end_date >= start)
        if end is not None:
            stmt = stmt.where(LeaveRequest.start_date <= end)
        return stmt

    def search(self, **filters) -> list[LeaveRequest]:
        stmt = self._filtered_query(**filters).order_by(
            LeaveRequest.start_date.desc(), LeaveRequest.id.desc()
        )
        return list(db.session.execute(stmt).scalars().all())

    def search_paginated(self, *, page: int, per_page: int, **filters):
        stmt = self._filtered_query(**filters).order_by(
            LeaveRequest.created_at.desc(), LeaveRequest.id.desc()
        )
        return self.paginate(stmt, page=page, per_page=per_page)

    def count_filtered(self, **filters) -> int:
        return self.count_statement(self._filtered_query(**filters))

    # ---- single lookups --------------------------------------------------
    def pending_for_employee(self, employee_id: int) -> list[LeaveRequest]:
        stmt = (
            select(LeaveRequest)
            .where(
                LeaveRequest.employee_id == employee_id,
                LeaveRequest.status == LeaveStatus.SUGGESTED.value,
            )
            .order_by(LeaveRequest.created_at)
        )
        return list(db.session.execute(stmt).scalars().all())

    def live_for_employee(self, employee_id: int, *, on_or_after: date | None = None):
        stmt = select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
        )
        if on_or_after is not None:
            stmt = stmt.where(LeaveRequest.end_date >= on_or_after)
        stmt = stmt.order_by(LeaveRequest.start_date)
        return list(db.session.execute(stmt).scalars().all())

    def active_on(self, day: date) -> list[LeaveRequest]:
        """Approved leaves covering ``day`` (the department calendar query)."""
        stmt = select(LeaveRequest).where(
            LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
            LeaveRequest.start_date <= day,
            LeaveRequest.end_date >= day,
        )
        return list(db.session.execute(stmt).scalars().all())

    # ---- calendar / capacity --------------------------------------------
    def live_in_window(
        self, *, start: date, end: date, employee_ids: list[int] | None = None
    ) -> list[LeaveRequest]:
        """The second of the two queries that build a ``DeptCalendar``."""
        stmt = select(LeaveRequest).where(
            LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
            LeaveRequest.end_date >= start,
            LeaveRequest.start_date <= end,
        )
        if employee_ids is not None:
            stmt = stmt.where(LeaveRequest.employee_id.in_(employee_ids))
        return list(db.session.execute(stmt).scalars().all())

    def overlap_exists(
        self,
        *,
        employee_id: int,
        start: date,
        end: date,
        statuses: list[str] | None = None,
        excluding_id: int | None = None,
    ) -> bool:
        """Whether the employee already holds leave overlapping the window."""
        wanted = statuses or [s.value for s in LIVE_LEAVE_STATUSES]
        stmt = select(LeaveRequest.id).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(wanted),
            LeaveRequest.end_date >= start,
            LeaveRequest.start_date <= end,
        )
        if excluding_id is not None:
            stmt = stmt.where(LeaveRequest.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def days_taken_in_month(
        self, *, employee_id: int, year: int, month: int, leave_type: str | None = None
    ) -> int:
        """Days already booked in a calendar month (live + pending)."""
        start = date(year, month, 1)
        end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        stmt = select(func.coalesce(func.sum(LeaveRequest.days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(
                [
                    LeaveStatus.APPROVED.value,
                    LeaveStatus.APPROVED_FORCED.value,
                    LeaveStatus.PENDING.value,
                    LeaveStatus.SUGGESTED.value,
                ]
            ),
            LeaveRequest.start_date >= start,
            LeaveRequest.start_date < end,
        )
        if leave_type:
            stmt = stmt.where(LeaveRequest.leave_type == leave_type)
        return int(db.session.execute(stmt).scalar_one() or 0)

    def days_taken_in_year(
        self, *, employee_id: int, year: int, leave_type: str | None = None
    ) -> int:
        stmt = select(func.coalesce(func.sum(LeaveRequest.days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_(
                [
                    LeaveStatus.APPROVED.value,
                    LeaveStatus.APPROVED_FORCED.value,
                    LeaveStatus.PENDING.value,
                    LeaveStatus.SUGGESTED.value,
                ]
            ),
            LeaveRequest.start_date >= date(year, 1, 1),
            LeaveRequest.start_date <= date(year, 12, 31),
        )
        if leave_type:
            stmt = stmt.where(LeaveRequest.leave_type == leave_type)
        return int(db.session.execute(stmt).scalar_one() or 0)

    def concurrent_on(self, day: date, *, employee_ids: list[int] | None = None) -> int:
        stmt = select(func.count()).select_from(LeaveRequest).where(
            LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
            LeaveRequest.start_date <= day,
            LeaveRequest.end_date >= day,
        )
        if employee_ids is not None:
            stmt = stmt.where(LeaveRequest.employee_id.in_(employee_ids))
        return int(db.session.execute(stmt).scalar_one() or 0)

    # ---- reporting -------------------------------------------------------
    def counts_by_status(
        self, *, year: int | None = None, department_id: int | None = None
    ) -> list[dict]:
        """Status -> count, optionally scoped to one department.

        Scoping matters: an unfiltered count shown next to a filtered queue
        makes a page report requests that have no visible row.
        """
        stmt = select(
            LeaveRequest.status.label("status"),
            func.count(LeaveRequest.id).label("total"),
            func.coalesce(func.sum(LeaveRequest.days), 0).label("days"),
        ).group_by(LeaveRequest.status)
        if year is not None:
            stmt = stmt.where(LeaveRequest.start_date >= date(year, 1, 1)).where(
                LeaveRequest.start_date <= date(year, 12, 31)
            )
        if department_id is not None:
            from app.features.employees.models import Employee

            stmt = stmt.join(
                Employee, Employee.id == LeaveRequest.employee_id
            ).where(Employee.department_id == department_id)
        return self.rows(stmt)

    def peers_on_leave_by_date(
        self,
        *,
        department_id: int | None,
        start: date,
        end: date,
        exclude_employee_id: int | None = None,
        leave_types: list[str] | None = None,
    ) -> dict[date, int]:
        """How many colleagues are already off on each day of a window.

        This is the single source of truth for the per-day concurrency quota.
        Counting is done per *employee-day*: one request spanning ten days adds
        one to each of those ten days, and a colleague with two overlapping
        requests is still only one person.

        Live statuses only (pending/approved), because a pending request has
        already reserved its days.
        """
        from app.features.employees.models import Employee

        stmt = (
            select(
                LeaveRequest.start_date.label("start_date"),
                LeaveRequest.end_date.label("end_date"),
                LeaveRequest.employee_id.label("employee_id"),
            )
            .select_from(LeaveRequest)
            .join(Employee, Employee.id == LeaveRequest.employee_id)
            .where(
                LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
                LeaveRequest.end_date >= start,
                LeaveRequest.start_date <= end,
            )
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        if exclude_employee_id is not None:
            stmt = stmt.where(LeaveRequest.employee_id != exclude_employee_id)
        if leave_types:
            stmt = stmt.where(LeaveRequest.leave_type.in_(leave_types))

        per_day: dict[date, set[int]] = {}
        for row in db.session.execute(stmt).all():
            window_start = max(row.start_date, start)
            window_end = min(row.end_date, end)
            for day in each_day(window_start, window_end):
                per_day.setdefault(day, set()).add(row.employee_id)

        return {day: len(ids) for day, ids in per_day.items()}

    def calendar_rows(
        self, *, department_id: int | None, start: date, end: date
    ) -> list[dict]:
        """Everyone on leave in a window, with employee context.

        ``department_id=None`` returns every department, which is what the HR
        queue shows when no filter is applied.
        """
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                LeaveRequest.id.label("id"),
                LeaveRequest.leave_type.label("leave_type"),
                LeaveRequest.start_date.label("start_date"),
                LeaveRequest.end_date.label("end_date"),
                LeaveRequest.days.label("days"),
                LeaveRequest.status.label("status"),
                Employee.id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.code.label("department_code"),
            )
            .select_from(LeaveRequest)
            .join(Employee, Employee.id == LeaveRequest.employee_id)
            .join(Department, Department.id == Employee.department_id, isouter=True)
            .where(
                LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
                LeaveRequest.end_date >= start,
                LeaveRequest.start_date <= end,
            )
            .order_by(LeaveRequest.start_date)
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return self.rows(stmt)

    def teammates_on_leave(self, *, department_id: int, start: date, end: date, exclude_employee_id: int | None = None) -> list[dict]:
        """Colleagues away in the same window, for the employee dashboard."""
        from app.features.employees.models import Employee

        stmt = (
            select(
                Employee.id.label("employee_id"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                LeaveRequest.leave_type.label("leave_type"),
                LeaveRequest.start_date.label("start_date"),
                LeaveRequest.end_date.label("end_date"),
                LeaveRequest.status.label("status"),
            )
            .select_from(LeaveRequest)
            .join(Employee, Employee.id == LeaveRequest.employee_id)
            .where(
                Employee.department_id == department_id,
                LeaveRequest.status.in_([s.value for s in LIVE_LEAVE_STATUSES]),
                LeaveRequest.end_date >= start,
                LeaveRequest.start_date <= end,
            )
            .order_by(LeaveRequest.start_date)
        )
        if exclude_employee_id is not None:
            stmt = stmt.where(Employee.id != exclude_employee_id)
        return self.rows(stmt)


class LeaveHistoryRepository(BaseRepository[LeaveRequestHistory]):
    """Queries over the append-only ``leave_request_history``."""

    model = LeaveRequestHistory

    def for_request(self, request_id: int) -> list[LeaveRequestHistory]:
        stmt = (
            select(LeaveRequestHistory)
            .where(LeaveRequestHistory.request_id == request_id)
            .order_by(LeaveRequestHistory.created_at, LeaveRequestHistory.id)
        )
        return list(db.session.execute(stmt).scalars().all())

    def record(
        self,
        *,
        request_id: int,
        from_status: str | None,
        to_status: str,
        actor_id: int | None = None,
        note: str | None = None,
    ) -> LeaveRequestHistory:
        """Append one transition row."""
        return self.create(
            request_id=request_id,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            note=note,
        )

    def recent(self, *, limit: int = 20) -> list[dict]:
        from app.features.employees.models import Employee

        stmt = (
            select(
                LeaveRequestHistory.id.label("id"),
                LeaveRequestHistory.request_id.label("request_id"),
                LeaveRequestHistory.from_status.label("from_status"),
                LeaveRequestHistory.to_status.label("to_status"),
                LeaveRequestHistory.note.label("note"),
                LeaveRequestHistory.created_at.label("created_at"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
            )
            .join(LeaveRequest, LeaveRequest.id == LeaveRequestHistory.request_id)
            .join(Employee, Employee.id == LeaveRequest.employee_id)
            .order_by(LeaveRequestHistory.created_at.desc())
            .limit(limit)
        )
        return self.rows(stmt)


class LeavePlanRepository(BaseRepository[LeavePlan]):
    """Queries over ``leave_plans`` and their roster items."""

    model = LeavePlan

    def for_department_year(
        self, *, department_id: int | None, year: int
    ) -> LeavePlan | None:
        stmt = select(LeavePlan).where(LeavePlan.year == year)
        if department_id is None:
            stmt = stmt.where(LeavePlan.department_id.is_(None))
        else:
            stmt = stmt.where(LeavePlan.department_id == department_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def all_plans(self, *, year: int | None = None) -> list[LeavePlan]:
        stmt = select(LeavePlan)
        if year is not None:
            stmt = stmt.where(LeavePlan.year == year)
        stmt = stmt.order_by(LeavePlan.year.desc(), LeavePlan.department_id.nullsfirst())
        return list(db.session.execute(stmt).scalars().all())

    def published_item_for(
        self, *, employee_id: int, year: int
    ) -> LeavePlanItem | None:
        """The employee's allocation from a *published* plan."""
        stmt = (
            select(LeavePlanItem)
            .join(LeavePlan, LeavePlan.id == LeavePlanItem.plan_id)
            .where(
                LeavePlanItem.employee_id == employee_id,
                LeavePlan.year == year,
                LeavePlan.status == LeavePlanStatus.PUBLISHED.value,
                LeavePlanItem.start_date.is_not(None),
            )
            .order_by(LeavePlanItem.start_date)
        )
        return db.session.execute(stmt).scalars().first()

    def items_for_department(
        self,
        *,
        department_id: int | None,
        year: int,
        published_only: bool = True,
    ) -> list[LeavePlanItem]:
        from app.features.employees.models import Employee

        stmt = (
            select(LeavePlanItem)
            .join(LeavePlan, LeavePlan.id == LeavePlanItem.plan_id)
            .join(Employee, Employee.id == LeavePlanItem.employee_id)
            .where(LeavePlan.year == year)
            .order_by(LeavePlanItem.start_date.nulls_last(), Employee.last_name)
        )
        if published_only:
            stmt = stmt.where(LeavePlan.status == LeavePlanStatus.PUBLISHED.value)
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return list(db.session.execute(stmt).scalars().all())

    def create_item(self, **values) -> LeavePlanItem:
        item = LeavePlanItem(**values)
        db.session.add(item)
        db.session.flush()
        return item

    def update_item(self, item: LeavePlanItem) -> LeavePlanItem:
        db.session.add(item)
        db.session.flush()
        return item

    def clear_items(self, plan: LeavePlan) -> None:
        """Drop every roster item so a re-draft starts clean."""
        for item in list(plan.items):
            db.session.delete(item)
        db.session.flush()
