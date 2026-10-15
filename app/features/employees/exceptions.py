"""Employees-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError


class EmployeeError(AppError):
    """Base class for employees feature failures."""

    status_code = 400
    error_code = "employee_error"
    message = "The employee operation could not be completed."


class DepartmentNotFoundError(NotFoundError):
    error_code = "department_not_found"
    message = "Department not found."


class PositionNotFoundError(NotFoundError):
    error_code = "position_not_found"
    message = "Position not found."


class EmployeeNotFoundError(NotFoundError):
    error_code = "employee_not_found"
    message = "Employee not found."


class DepartmentCodeTakenError(ConflictError):
    error_code = "department_code_taken"
    message = "That department code is already in use."


class DepartmentNameTakenError(ConflictError):
    error_code = "department_name_taken"
    message = "That department name is already in use."


class PositionTitleTakenError(ConflictError):
    error_code = "position_title_taken"
    message = "That position title already exists in this department."


class EmployeeNoTakenError(ConflictError):
    error_code = "employee_no_taken"
    message = "That employee number is already in use."


class EmployeeEmailTakenError(ConflictError):
    error_code = "employee_email_taken"
    message = "That employee email is already in use."


class DepartmentInUseError(ConflictError):
    """Delete guard: employees still assigned."""

    error_code = "department_in_use"
    message = "This department still has employees or positions assigned."


class PositionInUseError(ConflictError):
    """Delete guard: employees still assigned."""

    error_code = "position_in_use"
    message = "This position still has employees assigned."


class InvalidPositionDepartmentError(ValidationError):
    """Position must belong to the employee's department."""

    error_code = "position_department_mismatch"
    message = "The selected position does not belong to that department."


class EmployeeHasUserError(ConflictError):
    """Delete guard: an employee record with a login cannot be hard-deleted."""

    error_code = "employee_has_user"
    message = "This employee has a login account; deactivate them instead."


class ProfileFieldLockedError(ConflictError):
    """Self-service edit attempted on an HR-owned field."""

    error_code = "profile_field_locked"
    message = "That field cannot be changed from your profile."


class GenderLeaveMismatchError(ConflictError):
    """The requested leave type does not match the employee's recorded gender."""

    error_code = "gender_leave_mismatch"
    message = "That leave type does not apply to this employee."


class ProfileNotAvailableError(EmployeeError):
    """No linked login, so there is no profile credential to manage."""

    error_code = "profile_not_available"
    message = "This account has no login attached to it."
