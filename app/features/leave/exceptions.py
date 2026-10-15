"""Leave-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import (
    AppError,
    ConflictError,
    NotFoundError,
    ValidationError,
)


class LeaveError(AppError):
    """Base class for leave feature failures."""

    status_code = 400
    error_code = "leave_error"
    message = "The leave request could not be processed."


class LeaveRuleError(ConflictError):
    """A configured leave rule blocks the request."""

    error_code = "leave_rule_violation"
    message = "A configured leave rule blocks this request."


class InsufficientBalanceError(LeaveRuleError):
    """Not enough entitled days remain."""

    error_code = "insufficient_balance"
    message = "Insufficient leave balance for this request."


class DepartmentAtCapacityError(LeaveRuleError):
    """Approving this would put too many colleagues off on the same day.

    Raised at *approval* time, not just during suggestion, so the configured
    per-department concurrency cap cannot be bypassed by requesting explicit
    dates.
    """

    error_code = "department_at_capacity"
    message = "Too many colleagues are already on leave on those dates."

    def __init__(
        self,
        message: str | None = None,
        *,
        breaches: list[dict] | None = None,
        alternative: dict | None = None,
    ) -> None:
        super().__init__(message)
        #: Per-day detail, surfaced to the UI so HR can see exactly which days clash.
        self.breaches = breaches or []
        #: The next free window the employee could take instead.
        self.alternative = alternative


class RecallNotAllowedError(LeaveRuleError):
    """Recall was attempted on leave that cannot be recalled."""

    error_code = "recall_not_allowed"
    message = "This leave cannot be recalled."


class NoSuggestionAvailableError(LeaveError):
    """The engine found no window satisfying the active constraints."""

    status_code = 409
    error_code = "no_suggestion_available"
    message = "No valid leave window is available within the search horizon."


class LeavePlanNotFoundError(NotFoundError):
    """The requested annual roster does not exist."""

    error_code = "leave_plan_not_found"
    message = "That annual leave plan was not found."


class InvalidLeaveTransitionError(ConflictError):
    """The requested state change is not legal from the current status."""

    error_code = "invalid_leave_transition"
    message = "That status change is not allowed from the current state."


class LeaveRequestNotFoundError(NotFoundError):
    """Unknown leave request."""

    error_code = "leave_request_not_found"
    message = "Leave request not found."


class LeaveBalanceNotFoundError(NotFoundError):
    """Unknown balance row."""

    error_code = "leave_balance_not_found"
    message = "Leave balance not found."


class LeaveTypeNotConfiguredError(ValidationError):
    """No active rule exists for the requested leave type."""

    error_code = "leave_type_not_configured"
    message = "No active leave rule is configured for this leave type."
