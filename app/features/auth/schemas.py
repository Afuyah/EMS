"""Auth input/output schemas."""

from __future__ import annotations

from marshmallow import Schema, ValidationError as MarshmallowValidationError, fields, validate

from app.core.exceptions import ValidationError
from app.features.auth.constants import Role


def _translate(error: MarshmallowValidationError) -> ValidationError:
    return ValidationError("Submitted data failed validation.", details=error.messages)


class LoginSchema(Schema):
    """Credentials for ``POST /auth/login``."""

    email = fields.Email(required=True, validate=validate.Length(max=255))
    password = fields.String(required=True, validate=validate.Length(min=1, max=256))


class RefreshSchema(Schema):
    """Optional explicit refresh token (cookie flow may omit it)."""

    refresh_token = fields.String(required=False, load_default=None)


class RegisterSchema(Schema):
    """Admin-initiated account creation."""

    email = fields.Email(required=True, validate=validate.Length(max=255))
    password = fields.String(required=True, validate=validate.Length(min=8, max=256))
    role = fields.String(
        required=False,
        load_default=Role.EMPLOYEE.value,
        validate=validate.OneOf(Role.values()),
    )
    is_active = fields.Boolean(required=False, load_default=True)


class ChangePasswordSchema(Schema):
    """Password reset payload."""

    password = fields.String(required=True, validate=validate.Length(min=8, max=256))


class UserSchema(Schema):
    """Public projection of a user row."""

    id = fields.Integer(dump_only=True)
    email = fields.String(dump_only=True)
    role = fields.String(dump_only=True)
    is_active = fields.Boolean(dump_only=True)
    created_at = fields.DateTime(dump_only=True)
    updated_at = fields.DateTime(dump_only=True)


class TokenPairSchema(Schema):
    """Token response projection."""

    access_token = fields.String(dump_only=True)
    refresh_token = fields.String(dump_only=True)
    token_type = fields.String(dump_only=True)
    expires_in = fields.Integer(dump_only=True)


# Instantiated once: schemas are stateless.
login_schema = LoginSchema()
refresh_schema = RefreshSchema()
register_schema = RegisterSchema()
change_password_schema = ChangePasswordSchema()
user_schema = UserSchema()
users_schema = UserSchema(many=True)
token_pair_schema = TokenPairSchema()


def load_or_raise(schema: Schema, payload: dict | None):
    """Validate ``payload``, converting marshmallow errors to domain errors."""
    try:
        return schema.load(payload or {})
    except MarshmallowValidationError as exc:
        raise _translate(exc) from exc
