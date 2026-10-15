"""Employee, department and position routes for the JSON API."""

from __future__ import annotations

from flask import Blueprint, request
from flask_jwt_extended import jwt_required

from app.core.permissions import require_roles
from app.features.auth.constants import Role
from app.features.employees.controller import (
    DepartmentController,
    EmployeeController,
    PositionController,
)

employees_api = Blueprint("employees_api", __name__)

_departments = DepartmentController()
_positions = PositionController()
_employees = EmployeeController()


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
@employees_api.get("/departments")
@jwt_required()
def list_departments():
    """All departments with headcounts."""
    return _departments.list_departments()


@employees_api.post("/departments")
@jwt_required()
def create_department():
    """Create a department (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _departments.create_department(request.get_json(silent=True) or {})


@employees_api.patch("/departments/<int:department_id>")
@jwt_required()
def update_department(department_id: int):
    """Rename a department or change its code (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _departments.update_department(
        department_id, request.get_json(silent=True) or {}
    )


@employees_api.delete("/departments/<int:department_id>")
@jwt_required()
def delete_department(department_id: int):
    """Delete an empty department (Admin)."""
    require_roles(Role.ADMIN)
    return _departments.delete_department(department_id)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
@employees_api.get("/positions")
@jwt_required()
def list_positions():
    """Positions, optionally filtered by ``?department_id=``."""
    return _positions.list_positions(request.args.get("department_id", type=int))


@employees_api.post("/positions")
@jwt_required()
def create_position():
    """Create a position inside a department (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _positions.create_position(request.get_json(silent=True) or {})


@employees_api.patch("/positions/<int:position_id>")
@jwt_required()
def update_position(position_id: int):
    """Retitle or move a position (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _positions.update_position(position_id, request.get_json(silent=True) or {})


@employees_api.delete("/positions/<int:position_id>")
@jwt_required()
def delete_position(position_id: int):
    """Delete an unoccupied position (Admin)."""
    require_roles(Role.ADMIN)
    return _positions.delete_position(position_id)


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@employees_api.get("/employees")
@jwt_required()
def list_employees():
    """Paged, filtered employee list."""
    return _employees.list_employees(request.args.to_dict())


@employees_api.post("/employees")
@jwt_required()
def create_employee():
    """Create an employee, optionally with a login (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _employees.create_employee(request.get_json(silent=True) or {})


@employees_api.get("/employees/<int:employee_id>")
@jwt_required()
def get_employee(employee_id: int):
    """One employee with department and position expanded."""
    return _employees.get_employee(employee_id)


@employees_api.patch("/employees/<int:employee_id>")
@jwt_required()
def update_employee(employee_id: int):
    """Partially update an employee (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _employees.update_employee(employee_id, request.get_json(silent=True) or {})


@employees_api.post("/employees/<int:employee_id>/active/<any(true,false):state>")
@jwt_required()
def set_employee_active(employee_id: int, state: str):
    """Activate or deactivate an employee (HR/Admin)."""
    require_roles(Role.HR, Role.ADMIN)
    return _employees.set_active(employee_id, state == "true")


@employees_api.delete("/employees/<int:employee_id>")
@jwt_required()
def delete_employee(employee_id: int):
    """Delete an employee with no login (Admin)."""
    require_roles(Role.ADMIN)
    return _employees.delete_employee(employee_id)
