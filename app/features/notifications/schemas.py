"""Notification schemas."""

from __future__ import annotations

from marshmallow import (
    Schema,
    ValidationError as MarshmallowValidationError,
    fields,
    validate,
)

from app.core.exceptions import ValidationError


def load_or_raise(schema: Schema, payload: dict | None):
    """Validate ``payload``, converting marshmallow errors to domain errors."""
    try:
        return schema.load(payload or {})
    except MarshmallowValidationError as exc:
        raise ValidationError(
            "Submitted data failed validation.", details=exc.messages
        ) from exc


class NotificationSchema(Schema):
    """Public projection of a notification row."""

    id = fields.Integer(dump_only=True)
    event_type = fields.String(dump_only=True)
    channel = fields.String(dump_only=True)
    status = fields.String(dump_only=True)
    subject = fields.String(dump_only=True, allow_none=True)
    body = fields.String(dump_only=True, allow_none=True)
    payload = fields.Raw(dump_only=True, allow_none=True)
    attempts = fields.Integer(dump_only=True)
    sent_at = fields.DateTime(dump_only=True, allow_none=True)
    read_at = fields.DateTime(dump_only=True, allow_none=True)
    created_at = fields.DateTime(dump_only=True)
    is_unread = fields.Boolean(dump_only=True)
    icon = fields.String(dump_only=True)


class NotificationCreateSchema(Schema):
    """Admin/test helper for queueing a notification directly."""

    event_type = fields.String(required=True, validate=validate.Length(min=1, max=64))
    subject = fields.String(required=True, validate=validate.Length(min=1, max=255))
    body = fields.String(required=True, validate=validate.Length(min=1, max=4000))
    recipient_user_id = fields.Integer(required=False, allow_none=True)
    recipient_employee_id = fields.Integer(required=False, allow_none=True)
    channel = fields.String(
        required=False, load_default="in_app",
        validate=validate.OneOf(["in_app", "email", "sms"]),
    )
    payload = fields.Dict(required=False, allow_none=True)


notification_schema = NotificationSchema()
notifications_schema = NotificationSchema(many=True)
notification_create_schema = NotificationCreateSchema()
