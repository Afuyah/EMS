"""Leave tables: balances, requests, and the state-transition history.

Data containers only — no business rules, no queries.  The one-denial rule is
enforced in :mod:`app.features.leave.state_machine`, never in the database.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import db
from app.features.leave.constants import LeavePlanStatus, LeaveStatus, LeaveType


class LeaveBalance(db.Model):
    """Entitlement, consumption and reservation per employee/type/year."""

    __tablename__ = "leave_balances"
    __table_args__ = (
        UniqueConstraint(
            "employee_id", "year", "leave_type", name="uq_leave_balances_scope"
        ),
        CheckConstraint("entitled >= 0", name="ck_leave_balances_entitled_positive"),
        CheckConstraint("used >= 0", name="ck_leave_balances_used_positive"),
        CheckConstraint("pending >= 0", name="ck_leave_balances_pending_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    leave_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    entitled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    employee: Mapped["Employee"] = relationship(back_populates="leave_balances")  # noqa: F821

    # ---- derived (no queries) -------------------------------------------
    @property
    def leave_type_enum(self) -> LeaveType:
        return LeaveType.coerce(self.leave_type)

    @property
    def available(self) -> int:
        """Days the employee may still book."""
        return max(0, self.entitled - self.used - self.pending)

    @property
    def remaining(self) -> int:
        """Days left once reservations are excluded from ``used``."""
        return max(0, self.entitled - self.used)

    @property
    def used_percent(self) -> int:
        if self.entitled <= 0:
            return 0
        return min(100, round((self.used / self.entitled) * 100))

    @property
    def pending_percent(self) -> int:
        if self.entitled <= 0:
            return 0
        return min(100, round(((self.used + self.pending) / self.entitled) * 100))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<LeaveBalance employee={self.employee_id} {self.year} "
            f"{self.leave_type} {self.used}/{self.entitled}>"
        )


class LeaveRequest(db.Model):
    """One leave request and its suggestion/approval lifecycle."""

    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("days > 0", name="ck_leave_requests_days_positive"),
        CheckConstraint("end_date >= start_date", name="ck_leave_requests_date_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    leave_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LeaveStatus.PENDING.value, index=True
    )
    denial_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suggestions: Mapped[list | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    #: Why HR recalled this leave (kept on the row so it stays visible).
    recall_reason: Mapped[str | None] = mapped_column(Text)
    #: The request the recalled days were moved to, when rescheduled.
    rescheduled_to_id: Mapped[int | None] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="SET NULL")
    )
    decided_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    employee: Mapped["Employee"] = relationship(back_populates="leave_requests")  # noqa: F821
    decider: Mapped["User | None"] = relationship(foreign_keys=[decided_by])  # noqa: F821
    history: Mapped[list["LeaveRequestHistory"]] = relationship(
        back_populates="request",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LeaveRequestHistory.created_at",
    )

    # ---- derived (no queries) -------------------------------------------
    @property
    def status_enum(self) -> LeaveStatus:
        return LeaveStatus.coerce(self.status)

    @property
    def leave_type_enum(self) -> LeaveType:
        return LeaveType.coerce(self.leave_type)

    @property
    def is_live(self) -> bool:
        return self.status_enum.is_live

    @property
    def is_pending(self) -> bool:
        return self.status_enum == LeaveStatus.PENDING

    @property
    def needs_employee_response(self) -> bool:
        return self.status_enum == LeaveStatus.SUGGESTED

    @property
    def can_be_denied(self) -> bool:
        from app.features.leave.constants import MAX_DENIALS

        return self.denial_count < MAX_DENIALS

    @property
    def top_suggestion(self) -> dict | None:
        if not self.suggestions:
            return None
        return self.suggestions[0]

    @property
    def status_tailwind(self) -> str:
        return {
            LeaveStatus.SUGGESTED.value: "bg-indigo-100 text-indigo-700 ring-indigo-600/20",
            LeaveStatus.PENDING.value: "bg-amber-100 text-amber-700 ring-amber-600/20",
            LeaveStatus.APPROVED.value: "bg-emerald-100 text-emerald-700 ring-emerald-600/20",
            LeaveStatus.APPROVED_FORCED.value: "bg-emerald-100 text-emerald-700 ring-emerald-600/20",
            LeaveStatus.DENIED.value: "bg-rose-100 text-rose-700 ring-rose-600/20",
            LeaveStatus.CANCELLED.value: "bg-slate-100 text-slate-600 ring-slate-500/20",
        }.get(self.status, "bg-slate-100 text-slate-600 ring-slate-500/20")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<LeaveRequest {self.id} employee={self.employee_id} "
            f"{self.leave_type} {self.start_date}..{self.end_date} {self.status}>"
        )


class LeaveRequestHistory(db.Model):
    """Append-only audit of every leave status transition."""

    __tablename__ = "leave_request_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    request: Mapped[LeaveRequest] = relationship(back_populates="history")
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<LeaveRequestHistory request={self.request_id} "
            f"{self.from_status}->{self.to_status}>"
        )


class LeavePlan(db.Model):
    """One department's annual leave roster.

    DRAFT is HR's working copy.  Publishing it is what makes the dates visible
    to employees, so a half-finished roster is never mistaken for a decision.
    """

    __tablename__ = "leave_plans"
    __table_args__ = (
        UniqueConstraint("department_id", "year", name="uq_leave_plans_dept_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: ``NULL`` means the plan spans every department.
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LeavePlanStatus.DRAFT.value, index=True
    )
    #: The concurrency cap the plan was generated against, kept for the record.
    concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    items: Mapped[list["LeavePlanItem"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="LeavePlanItem.start_date",
    )
    department: Mapped["Department | None"] = relationship()  # noqa: F821

    @property
    def status_enum(self) -> LeavePlanStatus:
        return LeavePlanStatus.coerce(self.status)

    @property
    def is_published(self) -> bool:
        return self.status_enum is LeavePlanStatus.PUBLISHED

    @property
    def placed_count(self) -> int:
        return sum(1 for item in self.items if item.start_date is not None)

    @property
    def unplaced_count(self) -> int:
        return sum(1 for item in self.items if item.start_date is None)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        scope = self.department_id if self.department_id is not None else "all"
        return f"<LeavePlan {scope}/{self.year} {self.status}>"


class LeavePlanItem(db.Model):
    """One employee's allocated window inside a plan."""

    __tablename__ = "leave_plan_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("leave_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    start_date: Mapped[date | None] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, index=True)
    days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Why the planner chose this slot (shown to HR, never invented in the view).
    rationale: Mapped[str | None] = mapped_column(Text)
    #: The month the employee asked for, when they expressed a preference.
    preferred_month: Mapped[int | None] = mapped_column(Integer)
    #: Set once the window has been turned into a real leave request.
    request_id: Mapped[int | None] = mapped_column(
        ForeignKey("leave_requests.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    plan: Mapped[LeavePlan] = relationship(back_populates="items")
    employee: Mapped["Employee"] = relationship()  # noqa: F821

    @property
    def is_placed(self) -> bool:
        return self.start_date is not None and self.end_date is not None

    @property
    def window(self) -> str:
        if not self.is_placed:
            return "Not placed"
        return (
            f"{self.start_date.strftime('%d %b')} - "
            f"{self.end_date.strftime('%d %b %Y')}"
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<LeavePlanItem plan={self.plan_id} employee={self.employee_id}>"
