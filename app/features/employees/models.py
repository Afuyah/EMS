"""Employee-domain tables: departments, positions, employees.

Data containers only — no business rules, no queries.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import TimestampMixin, db
from app.core.time import today_local
from app.features.employees.constants import DEFAULT_TIMEZONE, EmployeeColor, Gender


class Department(TimestampMixin, db.Model):
    """An organisational unit owning positions and employees."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)

    positions: Mapped[list["Position"]] = relationship(
        back_populates="department",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    employees: Mapped[list["Employee"]] = relationship(
        back_populates="department",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Department {self.code} {self.name}>"


class Position(TimestampMixin, db.Model):
    """A job title scoped to one department."""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("department_id", "title", name="uq_positions_department_title"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    department: Mapped[Department] = relationship(back_populates="positions")
    employees: Mapped[list["Employee"]] = relationship(
        back_populates="position",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Position {self.id} {self.title!r} dept={self.department_id}>"


class Employee(TimestampMixin, db.Model):
    """A person employed by the organisation."""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), unique=True, index=True
    )
    employee_no: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    #: ``male``/``female``.  NULL means HR has not recorded it yet: neither
    #: maternity nor paternity leave is available until they do, because the
    #: system has no basis for choosing between them.
    gender: Mapped[str | None] = mapped_column(String(10), index=True)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id", ondelete="SET NULL"), index=True
    )
    tz: Mapped[str] = mapped_column(
        String(64), nullable=False, default=DEFAULT_TIMEZONE
    )
    hired_at: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    current_color: Mapped[str] = mapped_column(
        String(10), nullable=False, default=EmployeeColor.GREEN.value, index=True
    )

    # ---- relationships ---------------------------------------------------
    user: Mapped["User | None"] = relationship(back_populates="employee")  # noqa: F821
    department: Mapped[Department] = relationship(back_populates="employees")
    position: Mapped[Position | None] = relationship(back_populates="employees")

    attendance_days: Mapped[list["AttendanceDaily"]] = relationship(  # noqa: F821
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    leave_requests: Mapped[list["LeaveRequest"]] = relationship(  # noqa: F821
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    leave_balances: Mapped[list["LeaveBalance"]] = relationship(  # noqa: F821
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    disciplinary_actions: Mapped[list["DisciplinaryAction"]] = relationship(  # noqa: F821
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    device_maps: Mapped[list["BiometricEmployeeMap"]] = relationship(  # noqa: F821
        back_populates="employee",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    notifications: Mapped[list["Notification"]] = relationship(  # noqa: F821
        back_populates="recipient_employee",
        passive_deletes=True,
    )

    # ---- derived (no queries) -------------------------------------------
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def color_enum(self) -> EmployeeColor:
        return EmployeeColor.coerce(self.current_color)

    @property
    def is_manager(self) -> bool:
        return self.user is not None and self.user.role == "MANAGER"

    @property
    def gender_enum(self) -> Gender | None:
        """Recorded sex, or ``None`` when HR has not set it."""
        if not self.gender:
            return None
        try:
            return Gender.coerce(self.gender)
        except ValueError:  # unexpected value in the column
            return None

    @property
    def needs_gender(self) -> bool:
        return self.gender_enum is None

    @property
    def parental_leave_type(self) -> str | None:
        """Which parental leave applies: ``maternity``, ``paternity`` or none."""
        recorded = self.gender_enum
        return recorded.parental_leave_type if recorded else None

    def tenure_days(self, *, as_of: date | None = None) -> int | None:
        """Days since hire, or ``None`` when the hire date is unknown."""
        if self.hired_at is None:
            return None
        reference = as_of or today_local(self.tz)
        return (reference - self.hired_at).days

    def tenure_label(self, *, as_of: date | None = None) -> str:
        """Human readable tenure such as ``3y 4m``."""
        days = self.tenure_days(as_of=as_of)
        if days is None:
            return "—"
        if days < 0:
            return "starts soon"
        years, remainder = divmod(days, 365)
        months = remainder // 30
        if years and months:
            return f"{years}y {months}m"
        if years:
            return f"{years}y"
        if months:
            return f"{months}m"
        return f"{days}d"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Employee {self.employee_no} {self.full_name}>"
