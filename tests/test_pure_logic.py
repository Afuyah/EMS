"""Pure unit tests for the attendance punch resolver and colour evaluator."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.features.attendance.engine import (
    PunchEvent,
    ShiftPolicy,
    classify,
    compute_early_leave_minutes,
    compute_late_minutes,
    recompute,
    resolve_punch,
)
from app.features.attendance.constants import AttendanceStatus
from app.features.biometrics.constants import PunchType
from app.features.employees.constants import EmployeeColor
from app.features.rules.color_evaluator import (
    ColorRuleSpec,
    default_ladder,
    evaluate_color,
    summarise,
)
from app.features.rules.disciplinary_evaluator import (
    DisciplinaryRuleSpec,
    evaluate,
    observed_count,
    pick_action,
    should_auto_apply,
)

pytestmark = pytest.mark.unit

TZ = "Africa/Nairobi"
WORK_DATE = date(2026, 3, 2)  # Monday
POLICY = ShiftPolicy(shift_start=time(9, 0), shift_end=time(17, 0), grace_minutes=10)


def utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """A UTC instant on the work date."""
    return datetime(2026, 3, 2, hour, minute, second, tzinfo=timezone.utc)


class TestShiftPolicy:
    def test_nominal_minutes(self):
        assert POLICY.nominal_minutes == 8 * 60

    def test_overnight_shift_wraps(self):
        overnight = ShiftPolicy(shift_start=time(22, 0), shift_end=time(6, 0))
        assert overnight.nominal_minutes == 8 * 60


class TestMinuteComputations:
    def test_on_time_is_not_late(self):
        """09:00 shift in Nairobi is 06:00 UTC; 05:30 UTC is early."""
        assert compute_late_minutes(
            utc(5, 30), work_date=WORK_DATE, policy=POLICY, tz_name=TZ
        ) == 0

    def test_late_minutes_measured_past_grace(self):
        # 06:30 UTC = 09:30 Nairobi; grace ends 09:10, so 20 minutes late.
        assert compute_late_minutes(
            utc(6, 30), work_date=WORK_DATE, policy=POLICY, tz_name=TZ
        ) == 20

    def test_early_leave_minutes(self):
        # 13:00 UTC = 16:00 Nairobi, one hour before the 17:00 shift end.
        assert compute_early_leave_minutes(
            utc(13, 0), work_date=WORK_DATE, policy=POLICY, tz_name=TZ
        ) == 60

    def test_leaving_on_time_is_not_early(self):
        # 14:00 UTC = 17:00 Nairobi.
        assert compute_early_leave_minutes(
            utc(14, 0), work_date=WORK_DATE, policy=POLICY, tz_name=TZ
        ) == 0


class TestClassify:
    def test_late_beats_present(self):
        assert classify(
            late_minutes=5, early_leave_minutes=0, worked_minutes=480,
            policy=POLICY, has_clock_out=True,
        ) == AttendanceStatus.LATE

    def test_short_day_is_half_day(self):
        assert classify(
            late_minutes=0, early_leave_minutes=0, worked_minutes=120,
            policy=POLICY, has_clock_out=True,
        ) == AttendanceStatus.HALF_DAY

    def test_full_day_is_present(self):
        assert classify(
            late_minutes=0, early_leave_minutes=0, worked_minutes=480,
            policy=POLICY, has_clock_out=True,
        ) == AttendanceStatus.PRESENT

    def test_open_day_is_present(self):
        assert classify(
            late_minutes=0, early_leave_minutes=0, worked_minutes=0,
            policy=POLICY, has_clock_out=False,
        ) == AttendanceStatus.PRESENT


class TestResolvePunch:
    def test_first_punch_sets_clock_in(self):
        result = resolve_punch(
            punch=PunchEvent(utc(5, 45), PunchType.IN),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
        )
        assert result.action == "clock_in"
        assert result.clock_in == utc(5, 45)
        assert result.late_minutes == 0

    def test_first_punch_late_computes_late(self):
        result = resolve_punch(
            punch=PunchEvent(utc(6, 30), PunchType.IN),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
        )
        assert result.late_minutes == 20
        assert result.status == AttendanceStatus.LATE

    def test_second_punch_sets_clock_out(self):
        result = resolve_punch(
            punch=PunchEvent(utc(14, 0), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "clock_out"
        assert result.worked_minutes == 8 * 60

    def test_third_punch_after_a_break_opens_a_second_session(self):
        """A day may hold several sessions, so a later punch is not discarded.

        Someone who clocks out at 14:00 and returns at 17:00 has worked two
        stints. Discarding the second one — which a single clock-in/clock-out
        pair did — silently loses their afternoon.
        """
        result = resolve_punch(
            punch=PunchEvent(utc(17, 0), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0), existing_clock_out=utc(14, 0),
        )
        assert result.action == "clock_in"
        assert result.reason == "new_session_after_break"
        assert len(result.sessions) == 2
        assert result.sessions[1].is_open
        # The first stint is preserved, not overwritten.
        assert result.worked_minutes == 8 * 60

    def test_a_stray_scan_shortly_after_clocking_out_is_ignored(self):
        """Too soon to be a return to work, too late to be a duplicate."""
        result = resolve_punch(
            punch=PunchEvent(utc(14, 30), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0), existing_clock_out=utc(14, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "brief_absence_not_a_new_session"
        assert len(result.sessions) == 1

    def test_replaying_the_clock_out_is_a_duplicate(self):
        result = resolve_punch(
            punch=PunchEvent(utc(14, 0), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0), existing_clock_out=utc(14, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "duplicate_after_clock_out"

    def test_punch_before_clock_in_is_ignored(self):
        result = resolve_punch(
            punch=PunchEvent(utc(5, 0), PunchType.IN),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "punch_before_clock_in"

    def test_duplicate_in_type_does_not_close_day(self):
        result = resolve_punch(
            punch=PunchEvent(utc(10, 0), PunchType.IN),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "duplicate_clock_in_type"

    def test_far_apart_auto_punch_is_ignored(self):
        """An AUTO punch a full day later is a forgotten clock-out, not an out."""
        result = resolve_punch(
            punch=PunchEvent(datetime(2026, 3, 3, 6, 0, tzinfo=timezone.utc), PunchType.AUTO),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "clock_out_too_far_from_clock_in"

    def test_explicit_out_closes_even_when_far(self):
        result = resolve_punch(
            punch=PunchEvent(datetime(2026, 3, 3, 6, 0, tzinfo=timezone.utc), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "clock_out"

    def test_within_minimum_gap_is_ignored(self):
        """A punch seconds after clock-in is the same moment, not a clock-out."""
        result = resolve_punch(
            punch=PunchEvent(utc(6, 0, 30), PunchType.OUT),
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            existing_clock_in=utc(6, 0),
        )
        assert result.action == "ignored"
        assert result.reason == "within_minimum_gap"


class TestRecompute:
    def test_recomputes_a_closed_day(self):
        result = recompute(
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            clock_in=utc(6, 30), clock_out=utc(13, 0),
        )
        assert result.late_minutes == 20
        assert result.early_leave_minutes == 60
        assert result.worked_minutes == 390
        assert result.status == AttendanceStatus.LATE

    def test_open_day_only_has_late_minutes(self):
        result = recompute(
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            clock_in=utc(6, 30), clock_out=None,
        )
        assert result.late_minutes == 20
        assert result.worked_minutes == 0
        assert result.reason == "open_day"

    def test_no_clock_in_is_ignored(self):
        result = recompute(
            work_date=WORK_DATE, tz_name=TZ, policy=POLICY,
            clock_in=None, clock_out=utc(14, 0),
        )
        assert result.action == "ignored"


class TestColorEvaluator:
    def test_no_rules_means_green(self):
        outcome = evaluate_color(lates=50, absences=10, rules=[], current="green")
        assert outcome.color == EmployeeColor.GREEN

    def test_first_matching_rule_by_priority(self):
        outcome = evaluate_color(lates=7, absences=2, rules=default_ladder())
        # Orange requires >=6 lates and >=1 absence; Red needs >=10 and >=3.
        assert outcome.color == EmployeeColor.ORANGE

    def test_both_thresholds_required(self):
        # 6 lates but zero absences cannot satisfy orange (needs >=1 absence).
        outcome = evaluate_color(lates=6, absences=0, rules=default_ladder())
        assert outcome.color == EmployeeColor.YELLOW

    def test_red_requires_lates_and_absences(self):
        outcome = evaluate_color(lates=12, absences=4, rules=default_ladder())
        assert outcome.color == EmployeeColor.RED

    def test_escalation_flag(self):
        worse = evaluate_color(lates=12, absences=4, rules=default_ladder(), current="green")
        assert worse.escalated
        better = evaluate_color(lates=0, absences=0, rules=default_ladder(), current="red")
        assert not better.escalated

    def test_high_priority_wins(self):
        rules = [
            ColorRuleSpec(color="yellow", min_lates=1, priority=1),
            ColorRuleSpec(color="red", min_lates=1, priority=99),
        ]
        assert evaluate_color(lates=5, absences=0, rules=rules).color == EmployeeColor.RED

    def test_summarise_counts_every_colour(self):
        outcomes = [
            evaluate_color(lates=0, absences=0, rules=[]),
            evaluate_color(lates=12, absences=4, rules=default_ladder()),
        ]
        tally = summarise(outcomes)
        assert set(tally) == {colour.value for colour in EmployeeColor}
        assert tally["green"] == 1 and tally["red"] == 1


class TestDisciplinaryEvaluator:
    def rules(self):
        return [
            DisciplinaryRuleSpec(
                trigger_type="late", threshold=3, period="month", action="warning",
                auto_apply=True, id=1,
            ),
            DisciplinaryRuleSpec(
                trigger_type="late", threshold=6, period="month",
                action="written_warning", auto_apply=True, id=2,
            ),
        ]

    def test_observed_count_maps_trigger_to_counter(self):
        counters = {"lates": 4, "absences": 1, "early_leaves": 2}
        assert observed_count(trigger_type="late", counters=counters) == 4
        assert observed_count(trigger_type="absent", counters=counters) == 1
        assert observed_count(trigger_type="early_leave", counters=counters) == 2

    def test_no_match_below_threshold(self):
        matches = evaluate(
            counters={"lates": 2}, rules=self.rules(), on=date(2026, 3, 31)
        )
        assert matches == []

    def test_matches_at_threshold(self):
        matches = evaluate(
            counters={"lates": 3}, rules=self.rules(), on=date(2026, 3, 31)
        )
        assert len(matches) == 1
        assert matches[0].rule.threshold == 3

    def test_multiple_matches_when_both_crossed(self):
        matches = evaluate(
            counters={"lates": 9}, rules=self.rules(), on=date(2026, 3, 31)
        )
        assert len(matches) == 2

    def test_pick_action_prefers_highest_threshold(self):
        matches = evaluate(
            counters={"lates": 9}, rules=self.rules(), on=date(2026, 3, 31)
        )
        assert pick_action(matches).rule.threshold == 6

    def test_auto_apply_flag(self):
        matches = evaluate(
            counters={"lates": 3}, rules=self.rules(), on=date(2026, 3, 31)
        )
        assert should_auto_apply(matches)

    def test_inactive_rules_are_skipped(self):
        rules = [
            DisciplinaryRuleSpec(
                trigger_type="late", threshold=1, period="month", action="warning",
                is_active=False,
            )
        ]
        assert evaluate(counters={"lates": 99}, rules=rules, on=date(2026, 3, 31)) == []

    def test_details_payload_shape(self):
        matches = evaluate(
            counters={"lates": 3}, rules=self.rules(), on=date(2026, 3, 31)
        )
        details = matches[0].as_details()
        assert details["observed"] == 3
        assert details["threshold"] == 3
        assert details["trigger_type"] == "late"
