"""Pure colour-ladder evaluation.

No database, no Flask.  Given an employee's late/absence counts over a window
and the configured rules, decide which colour applies.

Selection rule, per the brief: prioritise by ``priority`` descending and take
the first rule whose thresholds are *both* satisfied
(``lates >= min_lates AND absences >= min_absences``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.features.employees.constants import EmployeeColor


@dataclass(frozen=True)
class ColorRuleSpec:
    """A colour rule as the evaluator needs to see it."""

    color: str
    min_lates: int = 0
    min_absences: int = 0
    window_days: int = 30
    priority: int = 0
    department_id: int | None = None
    id: int | None = None

    @property
    def color_enum(self) -> EmployeeColor:
        return EmployeeColor.coerce(self.color)


@dataclass(frozen=True)
class ColorOutcome:
    """The colour an employee should carry, and why."""

    color: EmployeeColor
    matched_rule: ColorRuleSpec | None
    lates: int
    absences: int
    escalated: bool


def evaluate_color(
    *,
    lates: int,
    absences: int,
    rules: Sequence[ColorRuleSpec],
    current: str | EmployeeColor | None = None,
) -> ColorOutcome:
    """Pick the applicable colour and report whether it is an escalation."""
    current_color = EmployeeColor.coerce(current) if current else EmployeeColor.GREEN
    # Unknown current colours degrade to green rather than raising.
    if current_color not in set(EmployeeColor):
        current_color = EmployeeColor.GREEN

    eligible = [rule for rule in rules if _matches(rule, lates=lates, absences=absences)]

    if not eligible:
        green = EmployeeColor.GREEN
        return ColorOutcome(
            color=green,
            matched_rule=None,
            lates=lates,
            absences=absences,
            # A move *to* green can only ever be an improvement.
            escalated=green.severity > current_color.severity,
        )

    # Highest priority wins; ties break on the more severe colour so that a
    # misconfigured equal-priority pair still escalates rather than hides.
    best = max(
        eligible,
        key=lambda rule: (rule.priority, rule.color_enum.severity),
    )
    colour = best.color_enum
    return ColorOutcome(
        color=colour,
        matched_rule=best,
        lates=lates,
        absences=absences,
        escalated=colour.severity > current_color.severity,
    )


def _matches(rule: ColorRuleSpec | object, *, lates: int, absences: int) -> bool:
    """Both thresholds must be satisfied."""
    min_lates = getattr(rule, "min_lates", 0) or 0
    min_absences = getattr(rule, "min_absences", 0) or 0
    return lates >= min_lates and absences >= min_absences


def evaluate_many(
    *,
    counts: dict[int, dict],
    rules_by_department: dict[int | None, Sequence[ColorRuleSpec]],
    current_colors: dict[int, str] | None = None,
) -> dict[int, ColorOutcome]:
    """Evaluate a batch of employees.

    ``counts`` maps ``employee_id`` to ``{lates, absences}``; the caller decides
    which rule set applies to each employee via ``rules_by_department`` keyed on
    the employee's department.
    """
    colors = current_colors or {}
    outcomes: dict[int, ColorOutcome] = {}
    for employee_id, counters in counts.items():
        department_id = counters.get("department_id")
        rules = rules_by_department.get(department_id) or rules_by_department.get(None) or ()
        outcomes[employee_id] = evaluate_color(
            lates=int(counters.get("lates", 0)),
            absences=int(counters.get("absences", 0)),
            rules=rules,
            current=colors.get(employee_id),
        )
    return outcomes


def default_ladder() -> list[ColorRuleSpec]:
    """The documented seed ladder, useful for tests and seeding."""
    return [
        ColorRuleSpec(color=EmployeeColor.YELLOW.value, min_lates=3, min_absences=0, priority=10),
        ColorRuleSpec(color=EmployeeColor.ORANGE.value, min_lates=6, min_absences=1, priority=20),
        ColorRuleSpec(color=EmployeeColor.RED.value, min_lates=10, min_absences=3, priority=30),
    ]


def summarise(outcomes: Iterable[ColorOutcome]) -> dict[str, int]:
    """Colour -> count, for dashboards."""
    tally: dict[str, int] = {colour.value: 0 for colour in EmployeeColor}
    for outcome in outcomes:
        tally[outcome.color.value] = tally.get(outcome.color.value, 0) + 1
    return tally
