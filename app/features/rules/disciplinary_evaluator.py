"""Pure disciplinary escalation evaluation.

No database, no Flask.  Rules are threshold-based; ``auto_apply`` decides
whether the action is recorded automatically or merely proposed for HR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from app.features.rules.constants import (
    DisciplinaryActionType,
    RulePeriod,
    TriggerType,
)
from app.core.time import quarter_bounds, week_bounds, month_bounds


@dataclass(frozen=True)
class DisciplinaryRuleSpec:
    """A disciplinary rule as the evaluator needs to see it."""

    trigger_type: str
    threshold: int
    period: str
    action: str
    suspension_days: int = 0
    auto_apply: bool = False
    is_active: bool = True
    department_id: int | None = None
    id: int | None = None

    @property
    def trigger_enum(self) -> TriggerType:
        return TriggerType.coerce(self.trigger_type)

    @property
    def period_enum(self) -> RulePeriod:
        return RulePeriod.coerce(self.period)

    @property
    def action_enum(self) -> DisciplinaryActionType:
        return DisciplinaryActionType.coerce(self.action)


@dataclass(frozen=True)
class DisciplinaryMatch:
    """A rule whose threshold has been crossed."""

    rule: DisciplinaryRuleSpec
    observed: int
    window_start: date
    window_end: date
    auto_apply: bool

    @property
    def action(self) -> str:
        return self.rule.action

    def as_details(self) -> dict:
        """Payload stored on ``disciplinary_actions.details``."""
        return {
            "trigger_type": self.rule.trigger_type,
            "threshold": self.rule.threshold,
            "observed": self.observed,
            "period": self.rule.period,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "suspension_days": self.rule.suspension_days,
            "auto_apply": self.auto_apply,
        }


def period_window(period: str | RulePeriod, *, on: date) -> tuple[date, date]:
    """The reporting window a period refers to, ending on ``on``."""
    kind = RulePeriod.coerce(period)
    if kind is RulePeriod.WEEK:
        return week_bounds(on)
    if kind is RulePeriod.QUARTER:
        return quarter_bounds(on)
    return month_bounds(on)


def observed_count(
    *,
    trigger_type: str | TriggerType,
    counters: dict,
) -> int:
    """Pull the relevant counter out of a metrics dict."""
    kind = TriggerType.coerce(trigger_type)
    key = {
        TriggerType.LATE: "lates",
        TriggerType.ABSENT: "absences",
        TriggerType.EARLY_LEAVE: "early_leaves",
    }[kind]
    return int(counters.get(key, 0) or 0)


def evaluate(
    *,
    counters: dict,
    rules: Sequence[DisciplinaryRuleSpec],
    on: date,
) -> list[DisciplinaryMatch]:
    """All rules whose threshold is crossed, most severe first.

    ``counters`` holds the observed counts for the *widest* window the caller
    gathered; each rule's period is reported alongside the threshold so HR can
    see which window was evaluated.
    """
    matches: list[DisciplinaryMatch] = []
    for rule in rules:
        if not rule.is_active:
            continue
        observed = observed_count(trigger_type=rule.trigger_type, counters=counters)
        if observed >= rule.threshold:
            start, end = period_window(rule.period, on=on)
            matches.append(
                DisciplinaryMatch(
                    rule=rule,
                    observed=observed,
                    window_start=start,
                    window_end=end,
                    auto_apply=rule.auto_apply,
                )
            )

    # Highest threshold first so the most serious action is applied first.
    matches.sort(
        key=lambda match: (match.rule.threshold, match.observed),
        reverse=True,
    )
    return matches


def pick_action(matches: Sequence[DisciplinaryMatch]) -> DisciplinaryMatch | None:
    """The single action to record when several rules fire at once."""
    if not matches:
        return None
    return max(
        matches,
        key=lambda match: (
            match.rule.threshold,
            match.rule.action_enum.value,
            match.observed,
        ),
    )


def should_auto_apply(matches: Sequence[DisciplinaryMatch]) -> bool:
    """Whether any firing rule is configured to apply itself."""
    return any(match.auto_apply for match in matches)


def escalation_days(period: str | RulePeriod) -> int:
    """Lookback length for gathering counters for a period."""
    return RulePeriod.coerce(period).days


def default_rules() -> list[DisciplinaryRuleSpec]:
    """The documented seed rules, useful for tests and seeding."""
    return [
        DisciplinaryRuleSpec(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value, auto_apply=True,
        ),
        DisciplinaryRuleSpec(
            trigger_type=TriggerType.LATE.value, threshold=6,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WRITTEN_WARNING.value, auto_apply=True,
        ),
        DisciplinaryRuleSpec(
            trigger_type=TriggerType.LATE.value, threshold=10,
            period=RulePeriod.QUARTER.value,
            action=DisciplinaryActionType.SUSPENSION.value,
            suspension_days=1, auto_apply=False,
        ),
        DisciplinaryRuleSpec(
            trigger_type=TriggerType.ABSENT.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.SUSPENSION.value,
            suspension_days=2, auto_apply=False,
        ),
    ]
