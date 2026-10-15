"""Biometrics schemas."""

from __future__ import annotations

from marshmallow import Schema, ValidationError as MarshmallowValidationError, fields, validate

from app.core.exceptions import ValidationError
from app.features.biometrics.constants import ADAPTER_REGISTRY


def load_or_raise(schema: Schema, payload: dict | None):
    try:
        return schema.load(payload or {})
    except MarshmallowValidationError as exc:
        raise ValidationError(
            "Submitted data failed validation.", details=exc.messages
        ) from exc


class DeviceCreateSchema(Schema):
    """Register a biometric device."""

    serial_no = fields.String(required=True, validate=validate.Length(min=2, max=64))
    location = fields.String(required=False, allow_none=True, validate=validate.Length(max=160))
    vendor = fields.String(
        required=False,
        allow_none=True,
        validate=validate.OneOf(sorted(ADAPTER_REGISTRY)),
    )
    is_active = fields.Boolean(required=False, load_default=True)


class DeviceMappingSchema(Schema):
    """Map a device-local user id to an employee."""

    device_id = fields.Integer(required=True)
    employee_id = fields.Integer(required=True)
    device_user_id = fields.String(required=True, validate=validate.Length(min=1, max=64))


class PunchSchema(Schema):
    """Vendor-neutral punch shape accepted by the generic adapter."""

    device_user_id = fields.String(required=False, allow_none=True)
    employee_no = fields.String(required=False, allow_none=True)
    punch_time = fields.DateTime(required=False, allow_none=True)
    punch_type = fields.String(required=False, allow_none=True)


class RawLogQuerySchema(Schema):
    serial_no = fields.String(required=False, allow_none=True)
    limit = fields.Integer(required=False, load_default=100)
