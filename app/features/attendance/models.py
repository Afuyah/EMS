"""Attendance tables: daily rollups and shift policies.

Data containers only — no business rules, no queries.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import db
from app.features.attendance.constants import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_SHIFT_END,
    DEFAULT_SHIFT_START,
    DEFAULT_VERY_LATE_AFTER_MINUTES,
    AttendanceStatus,
)


class AttendanceDaily(db.Model):
    """One resolved attendance row per employee per local work date."""

    __tablename__ = "attendance_daily"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_attendance_employee_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=AttendanceStatus.ABSENT.value, index=True
    )
    late_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worked_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    employee: Mapped["Employee"] = relationship(back_populates="attendance_days")  # noqa: F821

    @property
    def status_enum(self) -> AttendanceStatus:
        return AttendanceStatus.coerce(self.status)

    @property
    def worked_hours(self) -> float:
        return round(self.worked_minutes / 60, 2)

    @property
    def is_open(self) -> bool:
        """Clocked in but never clocked out."""
        return self.clock_in is not None and self.clock_out is None

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<AttendanceDaily employee={self.employee_id} "
            f"{self.work_date} {self.status}>"
        )


class AttendancePolicy(db.Model):
    """Shift definition; ``department_id IS NULL`` is the global default."""

    __tablename__ = "attendance_policies"
    __table_args__ = (
        UniqueConstraint("department_id", name="uq_attendance_policies_department"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    shift_start: Mapped[time] = mapped_column(
        Time, nullable=False, default=DEFAULT_SHIFT_START
    )
    shift_end: Mapped[time] = mapped_column(
        Time, nullable=False, default=DEFAULT_SHIFT_END
    )
    grace_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_GRACE_MINUTES
    )
    #: Minutes past the grace period before an arrival escalates to "very late".
    very_late_after_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_VERY_LATE_AFTER_MINUTES
    )
    #: Minutes after shift start at which a still-missing punch becomes an
    #: absence.  ``NULL`` means "leave it to the nightly job".
    auto_absent_after_minutes: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    department: Mapped["Department | None"] = relationship()  # noqa: F821

    @property
    def is_global(self) -> bool:
        return self.department_id is None

    @property
    def very_late_cutoff_label(self) -> str:
        """Human-readable point at which "very late" begins, e.g. ``09:30``."""
        base = datetime.combine(date(2000, 1, 1), self.shift_start)
        offset = self.grace_minutes + max(0, self.very_late_after_minutes)
        return (base + timedelta(minutes=offset)).strftime("%H:%M")

    @property
    def auto_absent_label(self) -> str:
        """Human-readable absence cut-off, or ``—`` when not configured."""
        if self.auto_absent_after_minutes is None:
            return "—"
        base = datetime.combine(date(2000, 1, 1), self.shift_start)
        return (base + timedelta(minutes=self.auto_absent_after_minutes)).strftime("%H:%M")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        scope = "global" if self.is_global else f"dept={self.department_id}"
        return f"<AttendancePolicy {scope} {self.shift_start}-{self.shift_end}>"
