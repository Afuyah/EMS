"""Pure unit tests for the leave state machine and the one-denial rule."""

from __future__ import annotations

import pytest

from app.core.exceptions import DenialLimitReached  # noqa: F401  (contract reference)
from app.features.leave.constants import MAX_DENIALS, LeaveStatus
from app.features.leave.exceptions import InvalidLeaveTransitionError
from app.features.leave.state_machine import (
    LEGAL_TRANSITIONS,
    Transition,
    apply_admin_decision,
    apply_cancellation,
    apply_employee_response,
    assert_transition,
    can_transition,
    is_admin_event,
    is_employee_event,
    windows_from_suggestions,
)

pytestmark = pytest.mark.unit


class TestLegalTransitions:
    def test_suggested_can_be_approved(self):
        assert can_transition(LeaveStatus.SUGGESTED, LeaveStatus.APPROVED)

    def test_pending_can_be_denied(self):
        assert can_transition(LeaveStatus.PENDING, LeaveStatus.DENIED)

    def test_denied_is_terminal(self):
        assert LEGAL_TRANSITIONS[LeaveStatus.DENIED] == frozenset()

    def test_cancelled_is_terminal(self):
        assert LEGAL_TRANSITIONS[LeaveStatus.CANCELLED] == frozenset()

    def test_cannot_move_from_denied(self):
        assert not can_transition(LeaveStatus.DENIED, LeaveStatus.APPROVED)
        with pytest.raises(InvalidLeaveTransitionError):
            assert_transition(LeaveStatus.DENIED, LeaveStatus.APPROVED)

    def test_approved_can_only_be_cancelled(self):
        assert can_transition(LeaveStatus.APPROVED, LeaveStatus.CANCELLED)
        assert not can_transition(LeaveStatus.APPROVED, LeaveStatus.DENIED)

    def test_accepts_string_statuses(self):
        assert can_transition("suggested", "approved")
        assert not can_transition("cancelled", "approved")


class TestEventClassification:
    def test_employee_events(self):
        assert is_employee_event("accept")
        assert is_employee_event(Transition.DENY)
        assert is_employee_event("cancel")

    def test_admin_events(self):
        assert is_admin_event("approve")
        assert not is_admin_event("accept")


class TestEmployeeResponse:
    def test_accept_approves_and_consumes_days(self):
        outcome = apply_employee_response(
            status=LeaveStatus.SUGGESTED,
            denial_count=0,
            event=Transition.ACCEPT,
            days=3,
        )
        assert outcome.to_status == LeaveStatus.APPROVED
        assert outcome.consume_days == 3
        assert not outcome.forced
        assert outcome.denial_count == 0

    def test_first_denial_increments_and_resuggests(self):
        outcome = apply_employee_response(
            status=LeaveStatus.SUGGESTED,
            denial_count=0,
            event=Transition.DENY,
            days=3,
        )
        assert outcome.to_status == LeaveStatus.DENIED
        assert outcome.denial_count == MAX_DENIALS
        assert "resuggest" in outcome.reasons
        assert not outcome.forced

    def test_second_denial_force_approves(self):
        """The one-denial rule is enforced here, not in the database."""
        outcome = apply_employee_response(
            status=LeaveStatus.SUGGESTED,
            denial_count=MAX_DENIALS,
            event=Transition.DENY,
            days=3,
        )
        assert outcome.to_status == LeaveStatus.APPROVED_FORCED
        assert outcome.forced
        assert outcome.reused_denial
        assert outcome.consume_days == 3
        assert "denial_limit_reached" in outcome.reasons

    def test_cannot_respond_to_a_pending_request(self):
        with pytest.raises(InvalidLeaveTransitionError):
            apply_employee_response(
                status=LeaveStatus.PENDING,
                denial_count=0,
                event=Transition.ACCEPT,
                days=3,
            )

    def test_cannot_respond_twice(self):
        with pytest.raises(InvalidLeaveTransitionError):
            apply_employee_response(
                status=LeaveStatus.APPROVED,
                denial_count=0,
                event=Transition.ACCEPT,
                days=3,
            )

    def test_approve_is_not_an_employee_response(self):
        with pytest.raises(InvalidLeaveTransitionError):
            apply_employee_response(
                status=LeaveStatus.SUGGESTED,
                denial_count=0,
                event=Transition.APPROVE,
                days=1,
            )


class TestAdminDecision:
    def test_approve_from_pending(self):
        outcome = apply_admin_decision(
            status=LeaveStatus.PENDING, approve=True, days=4
        )
        assert outcome.to_status == LeaveStatus.APPROVED
        assert outcome.consume_days == 4

    def test_deny_from_pending_consumes_nothing(self):
        outcome = apply_admin_decision(
            status=LeaveStatus.PENDING, approve=False, days=4
        )
        assert outcome.to_status == LeaveStatus.DENIED
        assert outcome.consume_days == 0

    def test_can_decide_a_suggestion_directly(self):
        outcome = apply_admin_decision(
            status=LeaveStatus.SUGGESTED, approve=True, days=2
        )
        assert outcome.to_status == LeaveStatus.APPROVED

    def test_cannot_decide_a_closed_request(self):
        for status in (LeaveStatus.DENIED, LeaveStatus.CANCELLED):
            with pytest.raises(InvalidLeaveTransitionError):
                apply_admin_decision(status=status, approve=True, days=1)


class TestCancellation:
    def test_cancelling_approved_releases_days(self):
        outcome = apply_cancellation(status=LeaveStatus.APPROVED, days=5)
        assert outcome.to_status == LeaveStatus.CANCELLED
        assert outcome.releasable_days == 5

    def test_cancelling_a_suggestion_releases_nothing_from_used(self):
        outcome = apply_cancellation(status=LeaveStatus.SUGGESTED, days=5)
        assert outcome.to_status == LeaveStatus.CANCELLED
        assert outcome.releasable_days == 0

    def test_cannot_cancel_a_denied_request(self):
        with pytest.raises(InvalidLeaveTransitionError):
            apply_cancellation(status=LeaveStatus.DENIED, days=5)

    def test_cancelling_forced_approval_releases_days(self):
        outcome = apply_cancellation(status=LeaveStatus.APPROVED_FORCED, days=3)
        assert outcome.releasable_days == 3


class TestSuggestionRebuild:
    def test_windows_are_rebuilt_from_payload(self):
        suggestions = [
            {"start_date": "2026-03-02", "end_date": "2026-03-04", "days": 3},
            {"start_date": "2026-03-09", "end_date": "2026-03-11", "days": 3},
        ]
        windows = windows_from_suggestions(suggestions)
        assert len(windows) == 2
        assert windows[0].start_date.isoformat() == "2026-03-02"

    def test_bad_entries_are_skipped(self):
        windows = windows_from_suggestions(
            [{"nonsense": True}, {"start_date": "2026-03-02", "end_date": "2026-03-04"}]
        )
        assert len(windows) == 1

    def test_empty_input(self):
        assert windows_from_suggestions(None) == []
        assert windows_from_suggestions([]) == []
