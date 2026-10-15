"""Pure unit tests for the leave suggestion engine. No database involved."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.features.employees.constants import EmployeeColor
from app.features.leave.constants import SUGGESTION_LIMIT
from app.features.leave.engine import (
    DeptCalendar,
    RuleConstraints,
    Window,
    chargeable_days,
    find_best_windows,
    generate_candidate_windows,
    is_midweek_start,
    score_window,
    weekdays_in,
    weekend_padding,
)

pytestmark = pytest.mark.unit

# A Monday, so weekday/weekend reasoning is easy to follow.
MONDAY = date(2026, 3, 2)


def empty_calendar(headcount: int = 10) -> DeptCalendar:
    return DeptCalendar(department_id=1, headcount=headcount)


class TestWindow:
    def test_days_is_inclusive(self):
        assert Window(MONDAY, MONDAY).days == 1
        assert Window(MONDAY, MONDAY + timedelta(days=4)).days == 5

    def test_round_trips_through_dict(self):
        window = Window(MONDAY, MONDAY + timedelta(days=2))
        restored = Window.from_dict(window.as_dict())
        assert restored == window

    def test_as_dict_days_matches(self):
        payload = Window(MONDAY, MONDAY + timedelta(days=2)).as_dict()
        assert payload["days"] == 3
        assert payload["start_date"] == "2026-03-02"


class TestWeekdayHelpers:
    def test_weekdays_excludes_weekends(self):
        # Mon..Sun is 5 weekdays.
        window = Window(MONDAY, MONDAY + timedelta(days=6))
        assert len(weekdays_in(window)) == 5

    def test_chargeable_days_ignores_weekends(self):
        # Fri..Mon spans a weekend, so only Fri and Mon are chargeable.
        friday = date(2026, 3, 6)
        window = Window(friday, date(2026, 3, 9))
        assert window.days == 4
        assert chargeable_days(window) == 2

    def test_midweek_start(self):
        assert is_midweek_start(date(2026, 3, 3))  # Tuesday
        assert is_midweek_start(date(2026, 3, 4))  # Wednesday
        assert is_midweek_start(date(2026, 3, 5))  # Thursday
        assert not is_midweek_start(date(2026, 3, 2))  # Monday
        assert not is_midweek_start(date(2026, 3, 6))  # Friday

    def test_weekend_padding_counts_edges(self):
        # Saturday..Monday pads one day.
        assert weekend_padding(Window(date(2026, 3, 7), date(2026, 3, 9))) == 1
        # Monday..Friday pads nothing.
        assert weekend_padding(Window(date(2026, 3, 2), date(2026, 3, 6))) == 0
        # A weekend-only window is all padding.
        assert weekend_padding(Window(date(2026, 3, 7), date(2026, 3, 8))) == 2


class TestCandidateGeneration:
    def test_respects_horizon(self):
        windows = generate_candidate_windows(today=MONDAY, days=3, horizon_days=10)
        assert len(windows) == 11
        assert windows[0].start_date == MONDAY
        assert windows[-1].start_date == MONDAY + timedelta(days=10)

    def test_respects_limit(self):
        windows = generate_candidate_windows(
            today=MONDAY, days=3, horizon_days=100, limit=5
        )
        assert len(windows) == 5

    def test_duration_is_preserved(self):
        windows = generate_candidate_windows(today=MONDAY, days=5, horizon_days=3)
        assert all(window.days == 5 for window in windows)

    def test_zero_days_yields_nothing(self):
        assert generate_candidate_windows(today=MONDAY, days=0) == []

    def test_earliest_start_shifts_the_sweep(self):
        start = MONDAY + timedelta(days=7)
        windows = generate_candidate_windows(
            today=MONDAY, days=1, horizon_days=2, earliest_start=start
        )
        assert windows[0].start_date == start


class TestScoring:
    def test_earlier_window_scores_higher(self):
        constraints = RuleConstraints()
        early = score_window(
            Window(MONDAY, MONDAY),
            calendar=empty_calendar(),
            constraints=constraints,
            today=MONDAY,
        )
        late = score_window(
            Window(MONDAY + timedelta(days=60), MONDAY + timedelta(days=60)),
            calendar=empty_calendar(),
            constraints=constraints,
            today=MONDAY,
        )
        assert early.score > late.score

    def test_department_clear_is_flagged_and_rewarded(self):
        constraints = RuleConstraints()
        busy = DeptCalendar(
            department_id=1,
            headcount=10,
            on_leave_by_date={MONDAY: frozenset({99})},
        )
        clear = score_window(
            Window(MONDAY + timedelta(days=1), MONDAY + timedelta(days=1)),
            calendar=empty_calendar(),
            constraints=constraints,
            today=MONDAY,
        )
        crowded = score_window(
            Window(MONDAY, MONDAY),
            calendar=busy,
            constraints=constraints,
            today=MONDAY,
        )
        assert "department_clear" in clear.reasons
        assert clear.score > crowded.score

    def test_capacity_cap_blocks_a_window(self):
        constraints = RuleConstraints(max_employees_off_per_dept=2)
        busy = DeptCalendar(
            department_id=1,
            headcount=10,
            on_leave_by_date={MONDAY: frozenset({1, 2})},
        )
        result = score_window(
            Window(MONDAY, MONDAY),
            calendar=busy,
            constraints=constraints,
            today=MONDAY,
        )
        assert not result.eligible
        assert "department_at_capacity" in result.blockers

    def test_min_notice_blocks_short_notice(self):
        constraints = RuleConstraints(min_notice_days=7)
        result = score_window(
            Window(MONDAY + timedelta(days=1), MONDAY + timedelta(days=1)),
            calendar=empty_calendar(),
            constraints=constraints,
            today=MONDAY,
        )
        assert not result.eligible
        assert "insufficient_notice" in result.blockers

    def test_max_consecutive_days_blocks_long_window(self):
        constraints = RuleConstraints(max_consecutive_days=3)
        result = score_window(
            Window(MONDAY, MONDAY + timedelta(days=9)),
            calendar=empty_calendar(),
            constraints=constraints,
            today=MONDAY,
        )
        assert not result.eligible
        assert "exceeds_max_consecutive_days" in result.blockers

    def test_weekend_only_window_is_ineligible(self):
        saturday = date(2026, 3, 7)
        result = score_window(
            Window(saturday, saturday + timedelta(days=1)),
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
            today=MONDAY,
        )
        assert not result.eligible
        assert "weekend_only" in result.blockers


class TestFindBestWindows:
    def test_returns_ranked_eligible_windows(self):
        windows = find_best_windows(
            today=MONDAY,
            days=3,
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
        )
        assert windows
        assert len(windows) <= SUGGESTION_LIMIT
        scores = [window.score for window in windows]
        assert scores == sorted(scores, reverse=True)

    def test_excluded_ranges_are_skipped(self):
        first = find_best_windows(
            today=MONDAY,
            days=3,
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
        )
        assert first
        excluded = [first[0].window]
        second = find_best_windows(
            today=MONDAY,
            days=3,
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
            excluded_ranges=excluded,
        )
        assert second
        assert second[0].window != first[0].window

    def test_no_candidates_when_notice_exceeds_horizon(self):
        windows = find_best_windows(
            today=MONDAY,
            days=3,
            calendar=empty_calendar(),
            constraints=RuleConstraints(min_notice_days=365),
            horizon_days=30,
        )
        assert windows == []

    def test_capacity_blocks_everything_when_full(self):
        busy = DeptCalendar(
            department_id=1,
            headcount=1,
            on_leave_by_date={},
        )
        # Cap of 1 with one colleague already away on every candidate day.
        constraints = RuleConstraints(max_employees_off_per_dept=1)
        from app.core.time import each_day

        full = DeptCalendar(
            department_id=1,
            headcount=10,
            on_leave_by_date={
                day: frozenset({1})
                for day in each_day(MONDAY, MONDAY + timedelta(days=40))
            },
        )
        assert find_best_windows(
            today=MONDAY, days=1, calendar=full, constraints=constraints, horizon_days=30
        ) == []

    def test_suggestion_payload_shape(self):
        windows = find_best_windows(
            today=MONDAY,
            days=2,
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
        )
        suggestion = windows[0].as_suggestion(rank=1)
        assert suggestion["rank"] == 1
        assert set(suggestion) >= {
            "start_date", "end_date", "days", "score", "department_clear", "reasons"
        }

    def test_respects_limit(self):
        windows = find_best_windows(
            today=MONDAY,
            days=1,
            calendar=empty_calendar(),
            constraints=RuleConstraints(),
            limit=2,
        )
        assert len(windows) == 2
