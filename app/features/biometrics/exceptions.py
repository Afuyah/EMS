"""Biometrics-specific domain errors.

Where an error is conceptually a 404/409/403, the core exception provides the
status code and this module only supplies the feature's ``error_code`` and
message.  Inheriting an ``AppError`` subclass that hardcodes 400 as an earlier
base would otherwise mask the correct status.
"""

from __future__ import annotations

from app.core.exceptions import (
    AppError,
    ConflictError,
    DeviceAuthError,  # noqa: F401  re-exported for the webhook guard
    NotFoundError,
    ValidationError,
)


class BiometricError(AppError):
    """Base class for biometrics feature failures."""

    status_code = 400
    error_code = "biometric_error"
    message = "The biometric operation could not be completed."


class DeviceNotFoundError(NotFoundError):
    error_code = "device_not_found"
    message = "Biometric device not found."


class DeviceCredentialsError(DeviceAuthError):
    """Device headers were absent or wrong (webhook authentication)."""

    error_code = "device_auth_failed"
    message = "Device authentication failed."


class DeviceInactiveError(BiometricError):
    status_code = 403
    error_code = "device_inactive"
    message = "This device is deactivated."


class DeviceSerialTakenError(ConflictError):
    error_code = "device_serial_taken"
    message = "That device serial number is already registered."


class UnknownDeviceUserError(BiometricError):
    status_code = 422
    error_code = "unknown_device_user"
    message = "This device user id is not mapped to any employee on this device."


class UnsupportedVendorError(ValidationError):
    error_code = "unsupported_vendor"
    message = "No adapter is registered for that device vendor."


class MalformedPunchError(ValidationError):
    error_code = "malformed_punch"
    message = "The punch payload is malformed."


class DeviceMappingConflictError(ConflictError):
    error_code = "device_mapping_conflict"
    message = "That device user id or employee is already mapped on this device."
