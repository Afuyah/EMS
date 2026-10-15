"""Attendance-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, ConflictError, NotFoundError


class AttendanceError(AppError):
    """Base class for attendance feature failures."""

    status_code = 400
    error_code = "attendance_error"
    message = "The attendance operation could not be completed."


class AttendanceNotFoundError(NotFoundError):
    error_code = "attendance_not_found"
    message = "Attendance record not found."


class PolicyNotFoundError(NotFoundError):
    error_code = "policy_not_found"
    message = "Attendance policy not found."


class DuplicateAttendanceError(ConflictError):
    error_code = "duplicate_attendance"
    message = "An attendance record already exists for that employee and date."


class PunchOutOfOrderError(ConflictError):
    """A clock-out arrived before the recorded clock-in."""

    error_code = "punch_out_of_order"
    message = "This punch is earlier than the recorded clock-in."


class InvalidCorrectionError(AttendanceError):
    error_code = "invalid_correction"
    message = "The attendance correction is not valid."


class EmployeeNotClockableError(ConflictError):
    """The employee's current state does not allow attendance.

    Covers both a suspended/deactivated employee and one on approved leave.
    """

    error_code = "employee_not_clockable"
    message = "This employee cannot clock in right now."


class EmployeeOnLeaveError(EmployeeNotClockableError):
    """A punch arrived for a day covered by approved leave."""

    error_code = "employee_on_leave"
    message = "The employee is on approved leave for that day."
