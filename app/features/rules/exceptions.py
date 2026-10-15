"""Rules-specific domain errors."""

from __future__ import annotations

from app.core.exceptions import AppError, ConflictError, NotFoundError, ValidationError


class RuleError(AppError):
    """Base class for rules feature failures."""

    status_code = 400
    error_code = "rule_error"
    message = "The rule operation could not be completed."


class ColorRuleNotFoundError(NotFoundError):
    error_code = "color_rule_not_found"
    message = "Colour rule not found."


class DisciplinaryRuleNotFoundError(NotFoundError):
    error_code = "disciplinary_rule_not_found"
    message = "Disciplinary rule not found."


class LeaveRuleNotFoundError(NotFoundError):
    error_code = "leave_rule_not_found"
    message = "Leave rule not found."


class DuplicateRuleError(ConflictError):
    error_code = "duplicate_rule"
    message = "An equivalent rule already exists for that scope."


class DisciplinaryActionExistsError(ConflictError):
    """``unique(employee_id, rule_id, triggered_on)`` guard."""

    error_code = "disciplinary_action_exists"
    message = "That disciplinary action was already recorded today."


class NoMatchingRuleError(RuleError):
    """A rule evaluation ran but nothing applied."""

    status_code = 409
    error_code = "no_matching_rule"
    message = "No rule matched this evaluation."


class InvalidRuleValueError(ValidationError):
    """A supplied threshold, window or enum value is not usable."""

    error_code = "invalid_rule_value"
    message = "The supplied rule value is not valid."


class RuleInUseError(ConflictError):
    """Delete guard: recorded actions still reference this rule.

    Deleting it would orphan the audit trail, so deactivate it instead.
    """

    error_code = "rule_in_use"
    message = (
        "This rule has already produced disciplinary actions; "
        "deactivate it instead of deleting it."
    )
