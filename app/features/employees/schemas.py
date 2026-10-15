"""Employee input/output schemas and serialisers."""

from __future__ import annotations

from marshmallow import (
    Schema,
    ValidationError as MarshmallowValidationError,
    fields,
    validate,
)

from app.core.exceptions import ValidationError
from app.features.employees.constants import DEFAULT_TIMEZONE, EmployeeColor


def load_or_raise(schema: Schema, payload: dict | None):
    """Validate ``payload``, converting marshmallow errors to domain errors."""
    try:
        return schema.load(payload or {})
    except MarshmallowValidationError as exc:
        raise ValidationError(
            "Submitted data failed validation.", details=exc.messages
        ) from exc


def dump_list(schema: Schema, items) -> list[dict]:
    return schema.dump(list(items), many=True)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class DepartmentCreateSchema(Schema):
    name = fields.String(required=True, validate=validate.Length(min=2, max=120))
    code = fields.String(required=True, validate=validate.Length(min=2, max=20))


class DepartmentUpdateSchema(Schema):
    name = fields.String(required=False, validate=validate.Length(min=2, max=120))
    code = fields.String(required=False, validate=validate.Length(min=2, max=20))


class PositionCreateSchema(Schema):
    title = fields.String(required=True, validate=validate.Length(min=2, max=120))
    department_id = fields.Integer(required=True)


class PositionUpdateSchema(Schema):
    title = fields.String(required=False, validate=validate.Length(min=2, max=120))
    department_id = fields.Integer(required=False)


class EmployeeCreateSchema(Schema):
    """``POST /employees``."""

    employee_no = fields.String(required=False, allow_none=True, validate=validate.Length(max=32))
    first_name = fields.String(required=True, validate=validate.Length(min=1, max=80))
    last_name = fields.String(required=True, validate=validate.Length(min=1, max=80))
    email = fields.Email(required=True, validate=validate.Length(max=255))
    phone = fields.String(required=False, allow_none=True, validate=validate.Length(max=40))
    department_id = fields.Integer(required=True)
    position_id = fields.Integer(required=False, allow_none=True)
    tz = fields.String(required=False, load_default=DEFAULT_TIMEZONE, validate=validate.Length(max=64))
    hired_at = fields.Date(required=False, allow_none=True)
    is_active = fields.Boolean(required=False, load_default=True)
    # Optional login creation alongside the employee record.
    create_user = fields.Boolean(required=False, load_default=False)
    user_role = fields.String(required=False, allow_none=True)
    password = fields.String(required=False, allow_none=True, validate=validate.Length(min=8, max=256))


class EmployeeUpdateSchema(Schema):
    """``PATCH /employees/<id>`` — every field optional."""

    first_name = fields.String(required=False, validate=validate.Length(min=1, max=80))
    last_name = fields.String(required=False, validate=validate.Length(min=1, max=80))
    email = fields.Email(required=False, validate=validate.Length(max=255))
    phone = fields.String(required=False, allow_none=True, validate=validate.Length(max=40))
    department_id = fields.Integer(required=False)
    position_id = fields.Integer(required=False, allow_none=True)
    tz = fields.String(required=False, validate=validate.Length(max=64))
    hired_at = fields.Date(required=False, allow_none=True)
    is_active = fields.Boolean(required=False)
    current_color = fields.String(
        required=False, validate=validate.OneOf(EmployeeColor.values())
    )


class EmployeeFilterSchema(Schema):
    """Query-string filter for the employee list."""

    q = fields.String(required=False, allow_none=True)
    department_id = fields.Integer(required=False, allow_none=True)
    position_id = fields.Integer(required=False, allow_none=True)
    is_active = fields.Boolean(required=False, allow_none=True)
    color = fields.String(required=False, allow_none=True)
    employee_no = fields.String(required=False, allow_none=True)
    sort = fields.String(required=False, load_default="last_name")
    page = fields.Integer(required=False, load_default=1)
    per_page = fields.Integer(required=False, load_default=25)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class DepartmentSchema(Schema):
    id = fields.Integer(dump_only=True)
    name = fields.String(dump_only=True)
    code = fields.String(dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    employee_count = fields.Method("get_employee_count", dump_only=True)
    position_count = fields.Method("get_position_count", dump_only=True)

    def get_employee_count(self, obj) -> int:
        return len(obj.employees) if "employees" in obj.__dict__ else 0

    def get_position_count(self, obj) -> int:
        return len(obj.positions) if "positions" in obj.__dict__ else 0


class PositionSchema(Schema):
    id = fields.Integer(dump_only=True)
    title = fields.String(dump_only=True)
    department_id = fields.Integer(dump_only=True)
    department_code = fields.Method("get_department_code", dump_only=True)

    def get_department_code(self, obj) -> str | None:
        return obj.department.code if obj.department is not None else None


class EmployeeSchema(Schema):
    """Full employee projection."""

    id = fields.Integer(dump_only=True)
    user_id = fields.Integer(dump_only=True, allow_none=True)
    employee_no = fields.String(dump_only=True)
    first_name = fields.String(dump_only=True)
    last_name = fields.String(dump_only=True)
    full_name = fields.Method("get_full_name", dump_only=True)
    email = fields.String(dump_only=True)
    phone = fields.String(dump_only=True, allow_none=True)
    department_id = fields.Integer(dump_only=True)
    position_id = fields.Integer(dump_only=True, allow_none=True)
    tz = fields.String(dump_only=True)
    hired_at = fields.Date(dump_only=True, allow_none=True)
    is_active = fields.Boolean(dump_only=True)
    current_color = fields.String(dump_only=True)
    tenure_label = fields.Method("get_tenure", dump_only=True)
    department = fields.Method("get_department", dump_only=True)
    position = fields.Method("get_position", dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)

    def get_full_name(self, obj) -> str:
        return obj.full_name

    def get_tenure(self, obj) -> str:
        return obj.tenure_label()

    def get_department(self, obj):
        if obj.department is None:
            return None
        return {"id": obj.department.id, "name": obj.department.name, "code": obj.department.code}

    def get_position(self, obj):
        if obj.position is None:
            return None
        return {"id": obj.position.id, "title": obj.position.title}


class EmployeeSummarySchema(Schema):
    """Lightweight projection for pickers and embedded lists."""

    id = fields.Integer(dump_only=True)
    employee_no = fields.String(dump_only=True)
    full_name = fields.Method("get_full_name", dump_only=True)
    email = fields.String(dump_only=True)
    department_id = fields.Integer(dump_only=True)
    current_color = fields.String(dump_only=True)
    is_active = fields.Boolean(dump_only=True)

    def get_full_name(self, obj) -> str:
        return obj.full_name


# Instantiated once; schemas are stateless.
department_schema = DepartmentSchema()
departments_schema = DepartmentSchema(many=True)
position_schema = PositionSchema()
positions_schema = PositionSchema(many=True)
employee_schema = EmployeeSchema()
employees_schema = EmployeeSchema(many=True)
employee_summary_schema = EmployeeSummarySchema(many=True)

department_create_schema = DepartmentCreateSchema()
department_update_schema = DepartmentUpdateSchema()
position_create_schema = PositionCreateSchema()
position_update_schema = PositionUpdateSchema()
employee_create_schema = EmployeeCreateSchema()
employee_update_schema = EmployeeUpdateSchema()
employee_filter_schema = EmployeeFilterSchema()
