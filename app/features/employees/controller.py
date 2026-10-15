"""Employees controller: orchestration plus the transaction boundary.

Controllers commit on success and roll back on failure.  They return
response-shaped dicts; blueprints own HTTP concerns.
"""

from __future__ import annotations

from app.core.database import db
from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.permissions import require_roles
from app.core.responses import created, ok
from app.features.auth.constants import Role
from app.features.employees.schemas import (
    department_create_schema,
    department_schema,
    department_update_schema,
    departments_schema,
    employee_create_schema,
    employee_filter_schema,
    employee_schema,
    employee_update_schema,
    employees_schema,
    load_or_raise,
    position_create_schema,
    position_schema,
    position_update_schema,
    positions_schema,
)
from app.features.employees.service import (
    DepartmentService,
    EmployeeService,
    PositionService,
)

logger = get_logger(__name__)


class DepartmentController:
    """HTTP-facing orchestration for departments."""

    def __init__(self, service: DepartmentService | None = None) -> None:
        self.service = service or DepartmentService()

    def list_departments(self):
        departments = self.service.list_departments()
        counts = self.service.employee_counts()
        payload = departments_schema.dump(departments)
        for row, department in zip(payload, departments):
            row["employee_count"] = counts.get(department.id, 0)
            row["position_count"] = len(department.positions)
        return ok(payload)

    def create_department(self, payload: dict):
        data = load_or_raise(department_create_schema, payload)
        try:
            department = self.service.create_department(
                name=data["name"], code=data["code"]
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return created(department_schema.dump(department))

    def update_department(self, department_id: int, payload: dict):
        data = load_or_raise(department_update_schema, payload)
        try:
            department = self.service.update_department(
                department_id, name=data.get("name"), code=data.get("code")
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(department_schema.dump(department))

    def delete_department(self, department_id: int):
        try:
            self.service.delete_department(department_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok({"deleted": True})


class PositionController:
    """HTTP-facing orchestration for positions."""

    def __init__(self, service: PositionService | None = None) -> None:
        self.service = service or PositionService()

    def list_positions(self, department_id: int | None = None):
        positions = self.service.list_positions(department_id)
        return ok(positions_schema.dump(positions))

    def create_position(self, payload: dict):
        data = load_or_raise(position_create_schema, payload)
        try:
            position = self.service.create_position(
                title=data["title"], department_id=data["department_id"]
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return created(position_schema.dump(position))

    def update_position(self, position_id: int, payload: dict):
        data = load_or_raise(position_update_schema, payload)
        try:
            position = self.service.update_position(
                position_id,
                title=data.get("title"),
                department_id=data.get("department_id"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(position_schema.dump(position))

    def delete_position(self, position_id: int):
        try:
            self.service.delete_position(position_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok({"deleted": True})


class EmployeeController:
    """HTTP-facing orchestration for employees."""

    def __init__(self, service: EmployeeService | None = None) -> None:
        self.service = service or EmployeeService()

    def list_employees(self, query: dict):
        data = load_or_raise(employee_filter_schema, query)
        page = max(1, int(data.get("page") or 1))
        per_page = int(data.get("per_page") or 25)
        per_page = max(1, min(per_page, 200))

        result = self.service.list_employees_paginated(
            page=page,
            per_page=per_page,
            sort=data.get("sort") or "last_name",
            q=data.get("q"),
            department_id=data.get("department_id"),
            position_id=data.get("position_id"),
            is_active=data.get("is_active"),
            color=data.get("color") or None,
        )
        return ok(employees_schema.dump(result.items), meta=result.info.to_dict())

    def get_employee(self, employee_id: int):
        employee = self.service.get_employee(employee_id)
        payload = employee_schema.dump(employee)
        payload["tenure"] = employee.tenure_label()
        return ok(payload)

    def create_employee(self, payload: dict):
        data = load_or_raise(employee_create_schema, payload)
        try:
            employee, password = self.service.create_employee(
                first_name=data["first_name"],
                last_name=data["last_name"],
                email=data["email"],
                department_id=data["department_id"],
                position_id=data.get("position_id"),
                employee_no=data.get("employee_no"),
                phone=data.get("phone"),
                tz=data.get("tz"),
                hired_at=data.get("hired_at"),
                is_active=data.get("is_active", True),
                create_user=data.get("create_user", False),
                user_role=data.get("user_role"),
                password=data.get("password"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        body = employee_schema.dump(employee)
        if password:
            body["initial_password"] = password
        return created(body)

    def update_employee(self, employee_id: int, payload: dict):
        data = load_or_raise(employee_update_schema, payload)
        changes = {key: value for key, value in data.items() if key in payload}
        if not changes:
            raise ValidationError("No updatable fields were supplied.")
        try:
            employee = self.service.update_employee(employee_id, **changes)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(employee_schema.dump(employee))

    def set_active(self, employee_id: int, is_active: bool):
        try:
            employee = self.service.set_active(employee_id, is_active)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(employee_schema.dump(employee))

    def delete_employee(self, employee_id: int):
        try:
            self.service.delete_employee(employee_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok({"deleted": True})
