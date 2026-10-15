"""Admin service for lateness and absenteeism rules.

This is the write path behind the ``/app/rules`` admin interface.  It governs
both rule families:

* **colour rules** — the attendance-health ladder (green → red) that the
  ``color_recompute`` job applies, keyed on ``min_lates``/``min_absences``
  over ``window_days``.
* **disciplinary rules** — the HR escalation ladder (warning → suspension)
  that the ``disciplinary_evaluator`` job applies, keyed on one
  ``trigger_type`` crossing ``threshold`` within ``period``.

Services only flush; the caller (a view) owns the transaction boundary.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.features.employees.constants import EmployeeColor
from app.features.rules.constants import (
    DisciplinaryActionType,
    RulePeriod,
    TriggerType,
)
from app.features.rules.exceptions import (
    ColorRuleNotFoundError,
    DisciplinaryRuleNotFoundError,
    DuplicateRuleError,
    InvalidRuleValueError,
    RuleInUseError,
)
from app.features.rules.models import ColorRule, DisciplinaryRule
from app.features.rules.repository import (
    ColorRuleRepository,
    DisciplinaryActionRepository,
    DisciplinaryRuleRepository,
)

logger = get_logger(__name__)

#: A colour window shorter than this would flag almost everyone, so refuse it.
MIN_COLOR_WINDOW_DAYS = 1
MAX_COLOR_WINDOW_DAYS = 365

#: Suspension lengths applied when an admin leaves the field blank.
DEFAULT_SUSPENSION_DAYS: dict[str, int] = {
    DisciplinaryActionType.SUSPENSION.value: 1,
    DisciplinaryActionType.TERMINATION_REVIEW.value: 0,
}

#: Actions that only make sense with a non-zero suspension length.
_SUSPENSION_ACTIONS = {
    DisciplinaryActionType.SUSPENSION.value,
}


class RuleAdminService:
    """Use-cases for viewing and maintaining both rule families."""

    def __init__(
        self,
        color_rules: ColorRuleRepository | None = None,
        disciplinary_rules: DisciplinaryRuleRepository | None = None,
        actions: DisciplinaryActionRepository | None = None,
    ) -> None:
        self.color_rules = color_rules or ColorRuleRepository()
        self.disciplinary_rules = disciplinary_rules or DisciplinaryRuleRepository()
        self.actions = actions or DisciplinaryActionRepository()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def list_color_rules(self) -> list[ColorRule]:
        """Every colour rule, global first, then by descending priority."""
        return list(self.color_rules.list_all())

    def list_disciplinary_rules(self) -> list[DisciplinaryRule]:
        """Every disciplinary rule, global first, then by trigger/threshold."""
        return list(self.disciplinary_rules.list_all())

    def get_color_rule(self, rule_id: int) -> ColorRule:
        rule = self.color_rules.find(rule_id)
        if rule is None:
            raise ColorRuleNotFoundError(f"Colour rule {rule_id} was not found.")
        return rule

    def get_disciplinary_rule(self, rule_id: int) -> DisciplinaryRule:
        rule = self.disciplinary_rules.find(rule_id)
        if rule is None:
            raise DisciplinaryRuleNotFoundError(
                f"Disciplinary rule {rule_id} was not found."
            )
        return rule

    def summary(self) -> dict:
        """Counters for the page header."""
        colors = self.list_color_rules()
        rules = self.list_disciplinary_rules()
        return {
            "color_total": len(colors),
            "color_global": sum(1 for rule in colors if rule.department_id is None),
            "color_department": sum(1 for rule in colors if rule.department_id is not None),
            "disciplinary_total": len(rules),
            "disciplinary_active": sum(1 for rule in rules if rule.is_active),
            "disciplinary_auto": sum(1 for rule in rules if rule.auto_apply),
            "disciplinary_manual": sum(1 for rule in rules if not rule.auto_apply),
        }

    # ------------------------------------------------------------------
    # Colour rules
    # ------------------------------------------------------------------
    def create_color_rule(
        self,
        *,
        color: str,
        department_id: int | None = None,
        min_lates: int = 0,
        min_absences: int = 0,
        window_days: int = 30,
        priority: int = 0,
    ) -> ColorRule:
        """Add a rung to the attendance-health ladder."""
        colour = self._valid_color(color)
        lates, absences, window, prio = self._valid_color_numbers(
            min_lates=min_lates, min_absences=min_absences,
            window_days=window_days, priority=priority,
        )
        department_id = self._valid_department_id(department_id)

        # Green is the implicit fallback when nothing matches, so a green rule
        # with thresholds is almost always a mistake worth catching early.
        if colour == EmployeeColor.GREEN and (lates or absences):
            raise InvalidRuleValueError(
                "Green is the fallback when no rule matches, so it must keep "
                "zero thresholds. Set the thresholds on the yellow rule instead."
            )

        existing = self.color_rules.find_scoped(
            department_id=department_id, color=colour.value
        )
        if existing is not None:
            raise DuplicateRuleError(
                f"A '{colour.value}' rule already exists for "
                f"{self._scope_label(department_id)}."
            )

        rule = self.color_rules.create_rule(
            color=colour.value,
            department_id=department_id,
            min_lates=lates,
            min_absences=absences,
            window_days=window,
            priority=prio,
        )
        logger.info(
            "created colour rule %s lates>=%s absences>=%s window=%sd dept=%s",
            colour.value, lates, absences, window, department_id,
        )
        return rule

    def update_color_rule(
        self,
        rule_id: int,
        *,
        color: str | None = None,
        min_lates: int | None = None,
        min_absences: int | None = None,
        window_days: int | None = None,
        priority: int | None = None,
    ) -> ColorRule:
        """Edit an existing colour rule.  Department scope is immutable."""
        rule = self.get_color_rule(rule_id)

        target_color = self._valid_color(color) if color is not None else rule.color_enum
        lates = rule.min_lates if min_lates is None else min_lates
        absences = rule.min_absences if min_absences is None else min_absences
        window = rule.window_days if window_days is None else window_days
        prio = rule.priority if priority is None else priority

        lates, absences, window, prio = self._valid_color_numbers(
            min_lates=lates, min_absences=absences,
            window_days=window, priority=prio,
        )

        if target_color == EmployeeColor.GREEN and (lates or absences):
            raise InvalidRuleValueError(
                "Green is the fallback when no rule matches, so it must keep "
                "zero thresholds."
            )

        clash = self.color_rules.find_scoped(
            department_id=rule.department_id,
            color=target_color.value,
            excluding_id=rule.id,
        )
        if clash is not None:
            raise DuplicateRuleError(
                f"A '{target_color.value}' rule already exists for "
                f"{self._scope_label(rule.department_id)}."
            )

        rule.color = target_color.value
        rule.min_lates = lates
        rule.min_absences = absences
        rule.window_days = window
        rule.priority = prio
        return self.color_rules.update(rule)

    def delete_color_rule(self, rule_id: int) -> None:
        """Remove a colour rule.  Colour rules produce no action history."""
        rule = self.get_color_rule(rule_id)
        self.color_rules.delete(rule)
        logger.info("deleted colour rule id=%s", rule_id)

    # ------------------------------------------------------------------
    # Disciplinary rules
    # ------------------------------------------------------------------
    def create_disciplinary_rule(
        self,
        *,
        trigger_type: str,
        threshold: int,
        period: str,
        action: str,
        department_id: int | None = None,
        suspension_days: int | None = None,
        auto_apply: bool = False,
        is_active: bool = True,
    ) -> DisciplinaryRule:
        """Add a threshold that triggers an HR escalation."""
        trigger = self._valid_trigger(trigger_type)
        window = self._valid_period(period)
        action_type = self._valid_action(action)
        threshold = self._valid_threshold(threshold)
        days = self._valid_suspension(action_type, suspension_days)
        department_id = self._valid_department_id(department_id)

        existing = self.disciplinary_rules.find_scoped(
            department_id=department_id,
            trigger_type=trigger.value,
            threshold=threshold,
        )
        if existing is not None:
            raise DuplicateRuleError(
                f"A rule already triggers at {threshold} {trigger.value} "
                f"occurrence(s) for {self._scope_label(department_id)}."
            )

        rule = self.disciplinary_rules.create_rule(
            trigger_type=trigger.value,
            threshold=threshold,
            period=window.value,
            action=action_type.value,
            department_id=department_id,
            suspension_days=days,
            auto_apply=bool(auto_apply),
            is_active=bool(is_active),
        )
        logger.info(
            "created disciplinary rule %s>=%s/%s -> %s auto=%s dept=%s",
            trigger.value, threshold, window.value, action_type.value,
            auto_apply, department_id,
        )
        return rule

    def update_disciplinary_rule(
        self,
        rule_id: int,
        *,
        trigger_type: str | None = None,
        threshold: int | None = None,
        period: str | None = None,
        action: str | None = None,
        suspension_days: int | None = None,
        auto_apply: bool | None = None,
        is_active: bool | None = None,
    ) -> DisciplinaryRule:
        """Edit an existing disciplinary rule.  Department scope is immutable."""
        rule = self.get_disciplinary_rule(rule_id)

        trigger = (
            self._valid_trigger(trigger_type) if trigger_type is not None
            else rule.trigger_enum
        )
        window = self._valid_period(period) if period is not None else rule.period_enum
        action_type = (
            self._valid_action(action) if action is not None else rule.action_enum
        )
        new_threshold = self._valid_threshold(
            rule.threshold if threshold is None else threshold
        )
        # Re-derive suspension days when the action changes, unless supplied.
        days = self._valid_suspension(
            action_type,
            rule.suspension_days if suspension_days is None else suspension_days,
        )

        clash = self.disciplinary_rules.find_scoped(
            department_id=rule.department_id,
            trigger_type=trigger.value,
            threshold=new_threshold,
            excluding_id=rule.id,
        )
        if clash is not None:
            raise DuplicateRuleError(
                f"Another rule already triggers at {new_threshold} "
                f"{trigger.value} occurrence(s) for "
                f"{self._scope_label(rule.department_id)}."
            )

        rule.trigger_type = trigger.value
        rule.threshold = new_threshold
        rule.period = window.value
        rule.action = action_type.value
        rule.suspension_days = days
        if auto_apply is not None:
            rule.auto_apply = bool(auto_apply)
        if is_active is not None:
            rule.is_active = bool(is_active)
        return self.disciplinary_rules.update(rule)

    def set_disciplinary_rule_active(
        self, rule_id: int, is_active: bool
    ) -> DisciplinaryRule:
        """Enable or disable a rule without losing its configuration."""
        rule = self.get_disciplinary_rule(rule_id)
        rule.is_active = bool(is_active)
        self.disciplinary_rules.update(rule)
        logger.info("disciplinary rule id=%s active=%s", rule_id, is_active)
        return rule

    def delete_disciplinary_rule(self, rule_id: int) -> None:
        """Remove a rule, refusing when recorded actions still reference it."""
        rule = self.get_disciplinary_rule(rule_id)
        in_use = self.actions.count_for_rule(rule.id)
        if in_use:
            raise RuleInUseError(
                f"{in_use} recorded disciplinary action(s) reference this rule. "
                f"Deactivate it instead of deleting it."
            )
        self.disciplinary_rules.delete(rule)
        logger.info("deleted disciplinary rule id=%s", rule_id)

    # ------------------------------------------------------------------
    # Validation helpers (pure)
    # ------------------------------------------------------------------
    @staticmethod
    def _scope_label(department_id: int | None) -> str:
        return "all departments" if department_id is None else f"department {department_id}"

    @staticmethod
    def _valid_department_id(department_id: int | None) -> int | None:
        """``None`` means "global"; anything else must be a positive id."""
        if department_id is None:
            return None
        try:
            value = int(department_id)
        except (TypeError, ValueError) as exc:
            raise InvalidRuleValueError("Department must be a number or blank.") from exc
        if value <= 0:
            return None
        return value

    @staticmethod
    def _valid_color(color: str) -> EmployeeColor:
        try:
            return EmployeeColor.coerce(color)
        except ValueError as exc:
            raise InvalidRuleValueError(
                f"Colour must be one of: {', '.join(EmployeeColor.values())}."
            ) from exc

    @staticmethod
    def _valid_color_numbers(
        *, min_lates: int, min_absences: int, window_days: int, priority: int
    ) -> tuple[int, int, int, int]:
        def _non_negative(value, label: str) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise InvalidRuleValueError(f"{label} must be a whole number.") from exc
            if number < 0:
                raise InvalidRuleValueError(f"{label} cannot be negative.")
            return number

        lates = _non_negative(min_lates, "Minimum lates")
        absences = _non_negative(min_absences, "Minimum absences")
        window = _non_negative(window_days, "Window days")
        prio = _non_negative(priority, "Priority")

        if window < MIN_COLOR_WINDOW_DAYS:
            raise InvalidRuleValueError(
                f"Window must be at least {MIN_COLOR_WINDOW_DAYS} day(s)."
            )
        if window > MAX_COLOR_WINDOW_DAYS:
            raise InvalidRuleValueError(
                f"Window cannot exceed {MAX_COLOR_WINDOW_DAYS} days."
            )
        return lates, absences, window, prio

    @staticmethod
    def _valid_trigger(trigger_type: str) -> TriggerType:
        try:
            return TriggerType.coerce(trigger_type)
        except ValueError as exc:
            raise InvalidRuleValueError(
                f"Trigger must be one of: {', '.join(TriggerType.values())}."
            ) from exc

    @staticmethod
    def _valid_period(period: str) -> RulePeriod:
        try:
            return RulePeriod.coerce(period)
        except ValueError as exc:
            raise InvalidRuleValueError(
                f"Period must be one of: {', '.join(RulePeriod.values())}."
            ) from exc

    @staticmethod
    def _valid_action(action: str) -> DisciplinaryActionType:
        try:
            return DisciplinaryActionType.coerce(action)
        except ValueError as exc:
            raise InvalidRuleValueError(
                f"Action must be one of: {', '.join(DisciplinaryActionType.values())}."
            ) from exc

    @staticmethod
    def _valid_threshold(threshold: int) -> int:
        try:
            value = int(threshold)
        except (TypeError, ValueError) as exc:
            raise InvalidRuleValueError("Threshold must be a whole number.") from exc
        if value < 1:
            raise InvalidRuleValueError(
                "Threshold must be at least 1; a rule that triggers at zero "
                "would fire for every employee."
            )
        return value

    @staticmethod
    def _valid_suspension(
        action: DisciplinaryActionType, suspension_days: int | None
    ) -> int:
        """Blank suspension lengths default sensibly; explicit values are checked."""
        if suspension_days in (None, ""):
            if action.value in _SUSPENSION_ACTIONS:
                return DEFAULT_SUSPENSION_DAYS.get(action.value, 1)
            return 0

        try:
            days = int(suspension_days)
        except (TypeError, ValueError) as exc:
            raise InvalidRuleValueError("Suspension days must be a whole number.") from exc

        if days < 0:
            raise InvalidRuleValueError("Suspension days cannot be negative.")
        if days > 365:
            raise InvalidRuleValueError("Suspension days cannot exceed 365.")
        if days and action.value not in _SUSPENSION_ACTIONS:
            raise InvalidRuleValueError(
                f"A '{action.value}' action cannot carry suspension days; "
                f"only '{DisciplinaryActionType.SUSPENSION.value}' can."
            )
        return days
