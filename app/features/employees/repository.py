"""Employee repositories: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import date

from sqlalchemy import Select, func, or_, select

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.features.employees.constants import EMPLOYEE_SORT_FIELDS
from app.features.employees.models import Department, Employee, Position


class DepartmentRepository(BaseRepository[Department]):
    """Queries over ``departments``."""

    model = Department

    def get_by_code(self, code: str) -> Department | None:
        stmt = select(Department).where(
            func.lower(Department.code) == (code or "").strip().lower()
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def get_by_name(self, name: str) -> Department | None:
        stmt = select(Department).where(
            func.lower(Department.name) == (name or "").strip().lower()
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def code_taken(self, code: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(Department.id).where(
            func.lower(Department.code) == (code or "").strip().lower()
        )
        if excluding_id is not None:
            stmt = stmt.where(Department.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def name_taken(self, name: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(Department.id).where(
            func.lower(Department.name) == (name or "").strip().lower()
        )
        if excluding_id is not None:
            stmt = stmt.where(Department.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def ordered(self) -> list[Department]:
        stmt = select(Department).order_by(Department.name)
        return list(db.session.execute(stmt).scalars().all())

    def employee_counts(self) -> dict[int, int]:
        """department_id -> active employee count (one query)."""
        stmt = (
            select(Employee.department_id, func.count(Employee.id))
            .where(Employee.is_active.is_(True))
            .group_by(Employee.department_id)
        )
        return {row[0]: int(row[1]) for row in db.session.execute(stmt).all()}

    def distribution(self) -> list[dict]:
        """Department headcount split, for the list-page bar chart."""
        stmt = (
            select(
                Department.id.label("department_id"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                func.count(Employee.id).label("total"),
            )
            .join(Employee, Employee.department_id == Department.id, isouter=True)
            .where(or_(Employee.is_active.is_(True), Employee.id.is_(None)))
            .group_by(Department.id, Department.name, Department.code)
            .order_by(func.count(Employee.id).desc(), Department.name)
        )
        return self.rows(stmt)


class PositionRepository(BaseRepository[Position]):
    """Queries over ``positions``."""

    model = Position

    def list_for_department(self, department_id: int) -> list[Position]:
        stmt = (
            select(Position)
            .where(Position.department_id == department_id)
            .order_by(Position.title)
        )
        return list(db.session.execute(stmt).scalars().all())

    def title_taken(
        self, *, department_id: int, title: str, excluding_id: int | None = None
    ) -> bool:
        stmt = select(Position.id).where(
            Position.department_id == department_id,
            func.lower(Position.title) == (title or "").strip().lower(),
        )
        if excluding_id is not None:
            stmt = stmt.where(Position.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def employee_count(self, position_id: int) -> int:
        return self.count_positions(position_id)

    def count_positions(self, position_id: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Employee)
            .where(Employee.position_id == position_id)
        )
        return int(db.session.execute(stmt).scalar_one() or 0)


class EmployeeRepository(BaseRepository[Employee]):
    """Queries over ``employees``."""

    model = Employee

    # ---- filters ---------------------------------------------------------
    def _filtered_query(
        self,
        *,
        q: str | None = None,
        department_id: int | None = None,
        position_id: int | None = None,
        is_active: bool | None = None,
        color: str | None = None,
        employee_no: str | None = None,
    ) -> Select:
        stmt = select(Employee)
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        if position_id is not None:
            stmt = stmt.where(Employee.position_id == position_id)
        if is_active is not None:
            stmt = stmt.where(Employee.is_active.is_(is_active))
        if color:
            stmt = stmt.where(Employee.current_color == color)
        if employee_no:
            stmt = stmt.where(Employee.employee_no == employee_no)
        if q:
            needle = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Employee.first_name).like(needle),
                    func.lower(Employee.last_name).like(needle),
                    func.lower(Employee.email).like(needle),
                    func.lower(Employee.employee_no).like(needle),
                )
            )
        return stmt

    def search(self, **filters) -> list[Employee]:
        stmt = self._filtered_query(**filters).order_by(
            Employee.last_name, Employee.first_name
        )
        return list(db.session.execute(stmt).scalars().all())

    def search_paginated(self, *, page: int, per_page: int, sort: str = "last_name", **filters):
        stmt = self._filtered_query(**filters)
        stmt = stmt.order_by(*self._order_by(sort))
        return self.paginate(stmt, page=page, per_page=per_page)

    def count_filtered(self, **filters) -> int:
        return self.count_statement(self._filtered_query(**filters))

    @staticmethod
    def _order_by(sort: str):
        field = sort if sort in EMPLOYEE_SORT_FIELDS else "last_name"
        column = getattr(Employee, field, Employee.last_name)
        return (column.asc(), Employee.id.asc())

    # ---- single lookups --------------------------------------------------
    def get_by_employee_no(self, employee_no: str) -> Employee | None:
        stmt = select(Employee).where(Employee.employee_no == employee_no)
        return db.session.execute(stmt).scalar_one_or_none()

    def get_by_email(self, email: str) -> Employee | None:
        stmt = select(Employee).where(
            func.lower(Employee.email) == (email or "").strip().lower()
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def get_by_user_id(self, user_id: int) -> Employee | None:
        stmt = select(Employee).where(Employee.user_id == user_id)
        return db.session.execute(stmt).scalar_one_or_none()

    def email_taken(self, email: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(Employee.id).where(
            func.lower(Employee.email) == (email or "").strip().lower()
        )
        if excluding_id is not None:
            stmt = stmt.where(Employee.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def employee_no_taken(self, employee_no: str, *, excluding_id: int | None = None) -> bool:
        stmt = select(Employee.id).where(Employee.employee_no == employee_no)
        if excluding_id is not None:
            stmt = stmt.where(Employee.id != excluding_id)
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def next_employee_number(self, prefix: str = "EMP", width: int = 4) -> str:
        """Generate the next sequential employee number.

        Derived from the maximum existing numeric suffix so gaps are not reused.
        """
        stmt = select(Employee.employee_no).where(Employee.employee_no.like(f"{prefix}-%"))
        numbers = [row[0] for row in db.session.execute(stmt).all()]
        highest = 0
        for value in numbers:
            suffix = str(value).rsplit("-", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{prefix}-{highest + 1:0{width}d}"

    # ---- counts / aggregates --------------------------------------------
    def count_active(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Employee)
            .where(Employee.is_active.is_(True))
        )
        return int(db.session.execute(stmt).scalar_one() or 0)

    def count_by_color(self) -> list[dict]:
        """Colour -> active headcount (one query)."""
        stmt = (
            select(
                Employee.current_color.label("color"),
                func.count(Employee.id).label("total"),
            )
            .where(Employee.is_active.is_(True))
            .group_by(Employee.current_color)
            .order_by(Employee.current_color)
        )
        return self.rows(stmt)

    def count_by_department(self) -> list[dict]:
        stmt = (
            select(
                Department.id.label("department_id"),
                Department.name.label("department_name"),
                Department.code.label("department_code"),
                func.count(Employee.id).label("total"),
            )
            .join(Department, Employee.department_id == Department.id)
            .where(Employee.is_active.is_(True))
            .group_by(Department.id, Department.name, Department.code)
            .order_by(func.count(Employee.id).desc())
        )
        return self.rows(stmt)

    def ids_in_department(self, department_id: int, *, active_only: bool = True) -> list[int]:
        stmt = select(Employee.id).where(Employee.department_id == department_id)
        if active_only:
            stmt = stmt.where(Employee.is_active.is_(True))
        return [int(row[0]) for row in db.session.execute(stmt).all()]

    def headcount_by_day(self, start: date, end: date) -> list[dict]:
        """Daily headcount of active employees hired on/before each day.

        Single query using generate_series so there is no per-day round trip.
        """
        from sqlalchemy import literal_column, text

        stmt = text(
            """
            SELECT d::date AS work_date,
                   (SELECT count(*) FROM employees e
                     WHERE e.is_active = true
                       AND (e.hired_at IS NULL OR e.hired_at <= d::date)) AS headcount
              FROM generate_series(:start_date, :end_date, interval '1 day') AS d
             ORDER BY d
            """
        )
        result = db.session.execute(
            stmt, {"start_date": start, "end_date": end}
        )
        return [dict(row) for row in result.mappings().all()]

    def active_employee_ids(self) -> list[int]:
        stmt = select(Employee.id).where(Employee.is_active.is_(True))
        return [int(row[0]) for row in db.session.execute(stmt).all()]

    def missing_gender(self, *, department_id: int | None = None) -> list[Employee]:
        """Active employees whose gender has not been recorded yet."""
        stmt = select(Employee).where(
            Employee.is_active.is_(True),
            Employee.gender.is_(None),
        )
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        stmt = stmt.order_by(Employee.last_name, Employee.first_name)
        return list(db.session.execute(stmt).scalars().all())

    def count_active(self, *, department_id: int | None = None) -> int:
        """Active headcount."""
        stmt = select(func.count(Employee.id)).where(Employee.is_active.is_(True))
        if department_id is not None:
            stmt = stmt.where(Employee.department_id == department_id)
        return int(db.session.execute(stmt).scalar_one() or 0)
