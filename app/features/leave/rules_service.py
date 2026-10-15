"""Leave rule administration and resolution.

Rule resolution mirrors attendance policies: a department-specific rule wins,
otherwise the global rule (``department_id IS NULL``) applies.
"""

from __future__ import annotations

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.features.leave.constants import LeaveType
from app.features.leave.engine import RuleConstraints
from app.features.leave.exceptions import LeaveTypeNotConfiguredError
from app.features.rules.exceptions import (
    DuplicateRuleError,
    LeaveRuleNotFoundError,
)
from app.features.rules.models import LeaveRule
from app.features.rules.repository import LeaveRuleRepository

logger = get_logger(__name__)


#: The documented default rule set.  Single source of truth shared by
#: ``LeaveRulesService.ensure_defaults`` and the seed package, so a field added
#: here cannot be silently dropped by one of the two call paths.
DEFAULT_LEAVE_RULES: tuple[dict, ...] = (
    dict(
        leave_type=LeaveType.ANNUAL.value,
        max_per_year=21,
        max_per_month=5,
        max_consecutive_days=21,
        # One person per department at a time — the default concurrency cap.
        # Enforced at approval as well as during suggestion.
        max_employees_off_per_dept=1,
        min_notice_days=7,
        default_days=21,
    ),
    dict(
        leave_type=LeaveType.SICK.value,
        max_per_year=14,
        max_per_month=None,
        max_consecutive_days=14,
        max_employees_off_per_dept=None,
        min_notice_days=0,
        default_days=3,
    ),
    dict(
        leave_type=LeaveType.MATERNITY.value,
        max_per_year=90,
        max_per_month=None,
        max_consecutive_days=90,
        max_employees_off_per_dept=None,
        min_notice_days=30,
        default_days=90,
    ),
    dict(
        leave_type=LeaveType.PATERNITY.value,
        max_per_year=14,
        max_per_month=None,
        max_consecutive_days=14,
        max_employees_off_per_dept=None,
        min_notice_days=14,
        default_days=14,
    ),
    dict(
        leave_type=LeaveType.UNPAID.value,
        max_per_year=None,
        max_per_month=2,
        max_consecutive_days=30,
        max_employees_off_per_dept=None,
        min_notice_days=7,
        default_days=1,
    ),
)


class LeaveRulesService:
    """Reads and writes ``leave_rules``, and converts them for the engine."""

    def __init__(self, rules: LeaveRuleRepository | None = None) -> None:
        self.rules = rules or LeaveRuleRepository()

    # ---- reads -----------------------------------------------------------
    def list_rules(self, department_id: int | None = None) -> list[LeaveRule]:
        return list(self.rules.list_rules(department_id))

    def get_rule(self, rule_id: int) -> LeaveRule:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise LeaveRuleNotFoundError(f"Leave rule {rule_id} was not found.")
        return rule

    def resolve_rule(
        self, *, department_id: int | None, leave_type: str | LeaveType
    ) -> LeaveRule:
        """Department rule, else global rule, else raise."""
        kind = LeaveType.coerce(leave_type)
        rule = self.rules.find(department_id=department_id, leave_type=kind.value)
        if rule is None:
            raise LeaveTypeNotConfiguredError(
                f"No active leave rule is configured for "
                f"'{kind.value}' in this department."
            )
        return rule

    def resolve_constraints(
        self, *, department_id: int | None, leave_type: str | LeaveType
    ) -> RuleConstraints:
        """The pure-engine view of the resolved rule."""
        rule = self.resolve_rule(department_id=department_id, leave_type=leave_type)
        return RuleConstraints(
            max_consecutive_days=rule.max_consecutive_days,
            max_employees_off_per_dept=rule.max_employees_off_per_dept,
            min_notice_days=rule.min_notice_days or 0,
            max_per_month=rule.max_per_month,
            max_per_year=rule.max_per_year,
        )

    def default_days(self, *, department_id: int | None, leave_type: str | LeaveType) -> int:
        """Configured default duration, used for special leaves."""
        rule = self.resolve_rule(department_id=department_id, leave_type=leave_type)
        if rule.default_days:
            return int(rule.default_days)
        # Sensible fallbacks so a half-configured system still behaves.
        kind = LeaveType.coerce(leave_type)
        return {
            LeaveType.SICK.value: 3,
            LeaveType.MATERNITY.value: 90,
            LeaveType.PATERNITY.value: 14,
            LeaveType.UNPAID.value: 1,
        }.get(kind.value, 1)

    def annual_entitlement(
        self, *, department_id: int | None, leave_type: str | LeaveType
    ) -> int:
        """Days to allocate for a year (``max_per_year`` or ``default_days``)."""
        rule = self.resolve_rule(department_id=department_id, leave_type=leave_type)
        return int(rule.max_per_year or rule.default_days or 0)

    # ---- writes ----------------------------------------------------------
    def create_rule(
        self,
        *,
        leave_type: str,
        department_id: int | None = None,
        max_per_year: int | None = None,
        max_per_month: int | None = None,
        max_consecutive_days: int | None = None,
        max_employees_off_per_dept: int | None = None,
        min_notice_days: int = 0,
        default_days: int | None = None,
        is_active: bool = True,
    ) -> LeaveRule:
        kind = LeaveType.coerce(leave_type)
        if self.rules.find(department_id=department_id, leave_type=kind.value) is not None:
            scope = "global" if department_id is None else f"department {department_id}"
            raise DuplicateRuleError(
                f"A {kind.value} rule already exists for {scope}."
            )
        if min_notice_days < 0:
            raise ValidationError("Minimum notice days cannot be negative.")

        rule = self.rules.create(
            department_id=department_id,
            leave_type=kind.value,
            max_per_year=max_per_year,
            max_per_month=max_per_month,
            max_consecutive_days=max_consecutive_days,
            max_employees_off_per_dept=max_employees_off_per_dept,
            min_notice_days=min_notice_days,
            default_days=default_days,
            is_active=is_active,
        )
        logger.info("created leave rule %s dept=%s", kind.value, department_id)
        return rule

    def update_rule(self, rule_id: int, **changes) -> LeaveRule:
        rule = self.get_rule(rule_id)
        allowed = {
            "max_per_year",
            "max_per_month",
            "max_consecutive_days",
            "max_employees_off_per_dept",
            "min_notice_days",
            "default_days",
            "is_active",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(
                f"Unknown leave rule field(s): {', '.join(sorted(unknown))}."
            )
        if changes.get("min_notice_days") is not None and changes["min_notice_days"] < 0:
            raise ValidationError("Minimum notice days cannot be negative.")
        for key, value in changes.items():
            setattr(rule, key, value)
        return self.rules.update(rule)

    def delete_rule(self, rule_id: int) -> None:
        rule = self.get_rule(rule_id)
        self.rules.delete(rule)

    def ensure_defaults(self, *, department_id: int | None = None) -> list[LeaveRule]:
        """Idempotently create the documented default rule set for a scope."""
        created: list[LeaveRule] = []
        for spec in DEFAULT_LEAVE_RULES:
            existing = self.rules.find(
                department_id=department_id, leave_type=spec["leave_type"]
            )
            if existing is not None:
                continue
            created.append(self.create_rule(department_id=department_id, **spec))
        return created
