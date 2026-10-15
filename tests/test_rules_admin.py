"""Tests for the lateness/absenteeism admin service and its HTTP surface."""

from __future__ import annotations

from datetime import date

import pytest

from app.core.database import db
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
from app.features.rules.repository import (
    ColorRuleRepository,
    DisciplinaryRuleRepository,
)
from app.features.rules.rules_admin_service import RuleAdminService

pytestmark = pytest.mark.integration


@pytest.fixture()
def service():
    return RuleAdminService()


# ---------------------------------------------------------------------------
# Colour rules
# ---------------------------------------------------------------------------
class TestColorRules:
    def test_create_and_list(self, clean_db, service):
        rule = service.create_color_rule(
            color=EmployeeColor.YELLOW.value, min_lates=3, min_absences=0, priority=10
        )
        db.session.commit()

        assert rule.id is not None
        assert rule.color == "yellow"
        assert [r.id for r in service.list_color_rules()] == [rule.id]

    def test_equivalence_with_the_evaluator(self, clean_db, service):
        """A rule made here must be the one evaluate_color() can consume."""
        from app.features.rules.color_evaluator import ColorRuleSpec, evaluate_color

        service.create_color_rule(
            color=EmployeeColor.RED.value, min_lates=10, min_absences=3, priority=30
        )
        db.session.commit()

        stored = ColorRuleRepository().list_all()
        specs = [
            ColorRuleSpec(
                color=rule.color, min_lates=rule.min_lates,
                min_absences=rule.min_absences, priority=rule.priority,
            )
            for rule in stored
        ]
        outcome = evaluate_color(lates=12, absences=4, rules=specs)
        assert outcome.color == EmployeeColor.RED

    def test_thresholds_must_both_be_met(self, clean_db, service):
        service.create_color_rule(
            color=EmployeeColor.ORANGE.value, min_lates=6, min_absences=1, priority=20
        )
        db.session.commit()

        from app.features.rules.color_evaluator import ColorRuleSpec, evaluate_color

        specs = [
            ColorRuleSpec(
                color=r.color, min_lates=r.min_lates,
                min_absences=r.min_absences, priority=r.priority,
            )
            for r in ColorRuleRepository().list_all()
        ]
        # 6 lates but no absences must not match orange.
        assert evaluate_color(lates=6, absences=0, rules=specs).color == EmployeeColor.GREEN

    def test_global_and_department_scopes_coexist(self, clean_db, service, department):
        service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        service.create_color_rule(
            color=EmployeeColor.YELLOW.value, min_lates=2, department_id=department.id
        )
        db.session.commit()
        assert len(service.list_color_rules()) == 2

    def test_duplicate_colour_in_same_scope_is_rejected(self, clean_db, service):
        service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        db.session.commit()
        with pytest.raises(DuplicateRuleError):
            service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=5)

    def test_same_colour_in_another_scope_is_allowed(self, clean_db, service, department):
        service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        db.session.commit()
        rule = service.create_color_rule(
            color=EmployeeColor.YELLOW.value, min_lates=3, department_id=department.id
        )
        assert rule.department_id == department.id

    def test_green_with_thresholds_is_rejected(self, clean_db, service):
        """Green is the implicit fallback, so thresholds there are a mistake."""
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(color=EmployeeColor.GREEN.value, min_lates=1)

    def test_green_with_zero_thresholds_is_allowed(self, clean_db, service):
        rule = service.create_color_rule(color=EmployeeColor.GREEN.value)
        db.session.commit()
        assert rule.color == "green"

    def test_negative_thresholds_are_rejected(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=-1)
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(
                color=EmployeeColor.YELLOW.value, min_absences=-2
            )

    def test_invalid_colour_is_rejected(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(color="purple")

    def test_window_bounds_enforced(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(color=EmployeeColor.YELLOW.value, window_days=0)
        with pytest.raises(InvalidRuleValueError):
            service.create_color_rule(color=EmployeeColor.YELLOW.value, window_days=400)

    def test_update_changes_values(self, clean_db, service):
        rule = service.create_color_rule(
            color=EmployeeColor.YELLOW.value, min_lates=3, priority=10
        )
        db.session.commit()

        updated = service.update_color_rule(rule.id, min_lates=4, priority=15)
        db.session.commit()
        assert updated.min_lates == 4
        assert updated.priority == 15

    def test_update_can_change_colour(self, clean_db, service):
        rule = service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        db.session.commit()
        updated = service.update_color_rule(rule.id, color=EmployeeColor.ORANGE.value)
        db.session.commit()
        assert updated.color == "orange"

    def test_update_to_an_existing_colour_is_rejected(self, clean_db, service):
        service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        orange = service.create_color_rule(
            color=EmployeeColor.ORANGE.value, min_lates=6, priority=20
        )
        db.session.commit()
        with pytest.raises(DuplicateRuleError):
            service.update_color_rule(orange.id, color=EmployeeColor.YELLOW.value)

    def test_update_rejects_making_green_thresholded(self, clean_db, service):
        green = service.create_color_rule(color=EmployeeColor.GREEN.value)
        db.session.commit()
        with pytest.raises(InvalidRuleValueError):
            service.update_color_rule(green.id, min_lates=5)

    def test_partial_update_keeps_other_fields(self, clean_db, service):
        rule = service.create_color_rule(
            color=EmployeeColor.ORANGE.value, min_lates=6, min_absences=1,
            window_days=45, priority=20,
        )
        db.session.commit()
        updated = service.update_color_rule(rule.id, min_lates=8)
        db.session.commit()
        assert updated.min_lates == 8
        assert updated.min_absences == 1
        assert updated.window_days == 45
        assert updated.priority == 20

    def test_delete_removes_the_rule(self, clean_db, service):
        rule = service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        db.session.commit()
        service.delete_color_rule(rule.id)
        db.session.commit()
        assert service.list_color_rules() == []

    def test_missing_rule_raises_not_found(self, clean_db, service):
        with pytest.raises(ColorRuleNotFoundError):
            service.get_color_rule(99999)


# ---------------------------------------------------------------------------
# Disciplinary rules
# ---------------------------------------------------------------------------
class TestDisciplinaryRules:
    def test_create_and_list(self, clean_db, service):
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value,
            threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
            auto_apply=True,
        )
        db.session.commit()
        assert rule.id is not None
        assert rule.auto_apply is True
        assert [r.id for r in service.list_disciplinary_rules()] == [rule.id]

    def test_equivalence_with_the_evaluator(self, clean_db, service):
        """A rule made here must fire in the disciplinary evaluator."""
        from app.features.rules.disciplinary_evaluator import (
            DisciplinaryRuleSpec,
            evaluate,
        )

        service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value, auto_apply=True,
        )
        db.session.commit()

        specs = [
            DisciplinaryRuleSpec(
                trigger_type=r.trigger_type, threshold=r.threshold, period=r.period,
                action=r.action, suspension_days=r.suspension_days,
                auto_apply=r.auto_apply, is_active=r.is_active, id=r.id,
            )
            for r in DisciplinaryRuleRepository().list_all()
        ]
        matches = evaluate(counters={"lates": 4}, rules=specs, on=date(2026, 3, 15))
        assert len(matches) == 1
        assert matches[0].rule.threshold == 3

    def test_threshold_of_zero_is_rejected(self, clean_db, service):
        """A zero threshold would fire for every employee."""
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=0,
                period=RulePeriod.MONTH.value,
                action=DisciplinaryActionType.WARNING.value,
            )

    def test_invalid_enums_are_rejected(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type="nonsense", threshold=3,
                period=RulePeriod.MONTH.value,
                action=DisciplinaryActionType.WARNING.value,
            )
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=3,
                period="fortnight",
                action=DisciplinaryActionType.WARNING.value,
            )
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=3,
                period=RulePeriod.MONTH.value, action="firing_squad",
            )

    def test_suspension_defaults_to_one_day_when_blank(self, clean_db, service):
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=10,
            period=RulePeriod.QUARTER.value,
            action=DisciplinaryActionType.SUSPENSION.value,
            suspension_days=None,
        )
        db.session.commit()
        assert rule.suspension_days == 1

    def test_suspension_days_on_a_warning_is_rejected(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=3,
                period=RulePeriod.MONTH.value,
                action=DisciplinaryActionType.WARNING.value,
                suspension_days=3,
            )

    def test_negative_suspension_is_rejected(self, clean_db, service):
        with pytest.raises(InvalidRuleValueError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=10,
                period=RulePeriod.QUARTER.value,
                action=DisciplinaryActionType.SUSPENSION.value,
                suspension_days=-1,
            )

    def test_duplicate_trigger_and_threshold_in_scope_is_rejected(self, clean_db, service):
        service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
        )
        db.session.commit()
        with pytest.raises(DuplicateRuleError):
            service.create_disciplinary_rule(
                trigger_type=TriggerType.LATE.value, threshold=3,
                period=RulePeriod.WEEK.value,
                action=DisciplinaryActionType.WRITTEN_WARNING.value,
            )

    def test_same_threshold_different_trigger_is_allowed(self, clean_db, service):
        service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
        )
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.ABSENT.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.SUSPENSION.value, suspension_days=2,
        )
        db.session.commit()
        assert rule.trigger_type == "absent"

    def test_update_changes_threshold_and_action(self, clean_db, service):
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value, auto_apply=True,
        )
        db.session.commit()

        updated = service.update_disciplinary_rule(
            rule.id, threshold=5, action=DisciplinaryActionType.WRITTEN_WARNING.value
        )
        db.session.commit()
        assert updated.threshold == 5
        assert updated.action == "written_warning"
        assert updated.auto_apply is True

    def test_toggle_active(self, clean_db, service):
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
        )
        db.session.commit()

        assert service.set_disciplinary_rule_active(rule.id, False).is_active is False
        db.session.commit()
        assert service.set_disciplinary_rule_active(rule.id, True).is_active is True

    def test_delete_unused_rule(self, clean_db, service):
        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
        )
        db.session.commit()
        service.delete_disciplinary_rule(rule.id)
        db.session.commit()
        assert service.list_disciplinary_rules() == []

    def test_delete_is_blocked_when_actions_reference_the_rule(
        self, clean_db, service, employee, admin_user
    ):
        """Deleting would orphan the audit trail, so it must be refused."""
        from app.features.rules.repository import DisciplinaryActionRepository

        rule = service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value,
        )
        db.session.commit()

        DisciplinaryActionRepository().create(
            employee_id=employee.id,
            rule_id=rule.id,
            action=DisciplinaryActionType.WARNING.value,
            triggered_on=date.today(),
            details={"observed": 3},
            applied_by=admin_user.id,
        )
        db.session.commit()

        with pytest.raises(RuleInUseError):
            service.delete_disciplinary_rule(rule.id)

    def test_missing_rule_raises_not_found(self, clean_db, service):
        with pytest.raises(DisciplinaryRuleNotFoundError):
            service.get_disciplinary_rule(99999)


class TestSummary:
    def test_counts_both_families(self, clean_db, service):
        service.create_color_rule(color=EmployeeColor.YELLOW.value, min_lates=3)
        service.create_color_rule(color=EmployeeColor.RED.value, min_lates=10, priority=30)
        service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=3,
            period=RulePeriod.MONTH.value,
            action=DisciplinaryActionType.WARNING.value, auto_apply=True,
        )
        service.create_disciplinary_rule(
            trigger_type=TriggerType.LATE.value, threshold=10,
            period=RulePeriod.QUARTER.value,
            action=DisciplinaryActionType.SUSPENSION.value,
            suspension_days=1, auto_apply=False,
        )
        db.session.commit()

        summary = service.summary()
        assert summary["color_total"] == 2
        assert summary["color_global"] == 2
        assert summary["disciplinary_total"] == 2
        assert summary["disciplinary_auto"] == 1
        assert summary["disciplinary_manual"] == 1
