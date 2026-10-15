"""Leave input/output schemas and serialisers."""

from __future__ import annotations

from marshmallow import (
    Schema,
    ValidationError as MarshmallowValidationError,
    fields,
    validate,
)

from app.core.exceptions import ValidationError
from app.features.leave.constants import LeaveStatus, LeaveType


def load_or_raise(schema: Schema, payload: dict | None):
    """Validate ``payload``, converting marshmallow errors to domain errors."""
    try:
        return schema.load(payload or {})
    except MarshmallowValidationError as exc:
        raise ValidationError(
            "Submitted data failed validation.", details=exc.messages
        ) from exc


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class LeaveRequestCreateSchema(Schema):
    """``POST /leave/request``.

    ``start_date``/``end_date`` are optional: when omitted the engine picks the
    best window.  When supplied they are treated as a preferred window that is
    still validated against the active rules.
    """

    leave_type = fields.String(
        required=True, validate=validate.OneOf(LeaveType.values())
    )
    employee_id = fields.Integer(required=False, allow_none=True)
    start_date = fields.Date(required=False, allow_none=True)
    end_date = fields.Date(required=False, allow_none=True)
    days = fields.Integer(required=False, allow_none=True, validate=validate.Range(min=1))
    reason = fields.String(required=False, allow_none=True, validate=validate.Length(max=2000))


class LeaveRespondSchema(Schema):
    """``POST /leave/requests/<id>/respond``."""

    action = fields.String(
        required=True, validate=validate.OneOf(["accept", "deny"])
    )
    note = fields.String(required=False, allow_none=True, validate=validate.Length(max=1000))


class LeaveDecideSchema(Schema):
    """``POST /leave/requests/<id>/decide``."""

    decision = fields.String(required=True, validate=validate.OneOf(["approve", "deny"]))
    note = fields.String(required=False, allow_none=True, validate=validate.Length(max=1000))


class LeaveCancelSchema(Schema):
    note = fields.String(required=False, allow_none=True, validate=validate.Length(max=1000))


class LeaveBalanceAdjustSchema(Schema):
    """Manual balance correction (HR/Admin)."""

    employee_id = fields.Integer(required=True)
    year = fields.Integer(required=True)
    leave_type = fields.String(required=True, validate=validate.OneOf(LeaveType.values()))
    entitled = fields.Integer(required=False, allow_none=True, validate=validate.Range(min=0))
    used = fields.Integer(required=False, allow_none=True, validate=validate.Range(min=0))
    pending = fields.Integer(required=False, allow_none=True, validate=validate.Range(min=0))


class LeaveFilterSchema(Schema):
    """Query-string filter for the leave queues."""

    employee_id = fields.Integer(required=False, allow_none=True)
    department_id = fields.Integer(required=False, allow_none=True)
    leave_type = fields.String(required=False, allow_none=True)
    status = fields.String(required=False, allow_none=True)
    start = fields.Date(required=False, allow_none=True)
    end = fields.Date(required=False, allow_none=True)
    page = fields.Integer(required=False, load_default=1)
    per_page = fields.Integer(required=False, load_default=25)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class LeaveBalanceSchema(Schema):
    id = fields.Integer(dump_only=True)
    employee_id = fields.Integer(dump_only=True)
    year = fields.Integer(dump_only=True)
    leave_type = fields.String(dump_only=True)
    entitled = fields.Integer(dump_only=True)
    used = fields.Integer(dump_only=True)
    pending = fields.Integer(dump_only=True)
    available = fields.Integer(dump_only=True)
    remaining = fields.Integer(dump_only=True)
    used_percent = fields.Integer(dump_only=True)
    pending_percent = fields.Integer(dump_only=True)
    label = fields.Method("get_label", dump_only=True)

    def get_label(self, obj) -> str:
        return obj.leave_type.replace("_", " ").title()


class LeaveRequestSchema(Schema):
    id = fields.Integer(dump_only=True)
    employee_id = fields.Integer(dump_only=True)
    leave_type = fields.String(dump_only=True)
    start_date = fields.Date(dump_only=True)
    end_date = fields.Date(dump_only=True)
    days = fields.Integer(dump_only=True)
    status = fields.String(dump_only=True)
    denial_count = fields.Integer(dump_only=True)
    suggestions = fields.Raw(dump_only=True, allow_none=True)
    reason = fields.String(dump_only=True, allow_none=True)
    decided_by = fields.Integer(dump_only=True, allow_none=True)
    decided_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)
    employee = fields.Method("get_employee", dump_only=True)
    top_suggestion = fields.Method("get_top_suggestion", dump_only=True)
    can_be_denied = fields.Boolean(dump_only=True)
    needs_employee_response = fields.Boolean(dump_only=True)

    def get_employee(self, obj):
        employee = getattr(obj, "employee", None)
        if employee is None:
            return None
        return {
            "id": employee.id,
            "employee_no": employee.employee_no,
            "full_name": employee.full_name,
            "department_id": employee.department_id,
        }

    def get_top_suggestion(self, obj):
        return obj.top_suggestion


class LeaveHistorySchema(Schema):
    id = fields.Integer(dump_only=True)
    from_status = fields.String(dump_only=True, allow_none=True)
    to_status = fields.String(dump_only=True)
    actor_id = fields.Integer(dump_only=True, allow_none=True)
    note = fields.String(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)


class LeaveWindowSchema(Schema):
    """A scored suggestion window."""

    start_date = fields.Date(dump_only=True)
    end_date = fields.Date(dump_only=True)
    days = fields.Integer(dump_only=True)
    rank = fields.Integer(dump_only=True)
    score = fields.Float(dump_only=True)
    department_clear = fields.Boolean(dump_only=True)
    reasons = fields.List(fields.String(), dump_only=True)


# Instantiated once; schemas are stateless.
leave_request_schema = LeaveRequestSchema()
leave_requests_schema = LeaveRequestSchema(many=True)
leave_balance_schema = LeaveBalanceSchema()
leave_balances_schema = LeaveBalanceSchema(many=True)
leave_history_schema = LeaveHistorySchema(many=True)
leave_window_schema = LeaveWindowSchema(many=True)

leave_request_create_schema = LeaveRequestCreateSchema()
leave_respond_schema = LeaveRespondSchema()
leave_decide_schema = LeaveDecideSchema()
leave_cancel_schema = LeaveCancelSchema()
leave_balance_adjust_schema = LeaveBalanceAdjustSchema()
leave_filter_schema = LeaveFilterSchema()
