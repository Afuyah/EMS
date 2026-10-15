"""Pure leave state machine.

No database access, no Flask.  Enforces the one-denial rule in code rather than
in the schema, per the project brief.

State graph::

    SUGGESTED --accept--------> APPROVED
    SUGGESTED --deny(1st)-----> DENIED + next suggestion
    SUGGESTED --deny(2nd)-----> APPROVED_FORCED
    PENDING   --approve-------> APPROVED
    PENDING   --deny----------> DENIED
    any live  --cancel--------> CANCELLED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.features.leave.constants import MAX_DENIALS, LeaveStatus
from app.features.leave.engine import Window, find_best_windows
from app.features.leave.exceptions import InvalidLeaveTransitionError


class Transition(str, Enum):
    """The events a leave request can receive."""

    ACCEPT = "accept"
    DENY = "deny"
    APPROVE = "approve"
    CANCEL = "cancel"

    @classmethod
    def coerce(cls, value) -> "Transition":
        return value if isinstance(value, cls) else cls(str(value).strip().lower())


#: Legal from -> to edges.  Anything absent is rejected.
LEGAL_TRANSITIONS: dict[LeaveStatus, frozenset[LeaveStatus]] = {
    LeaveStatus.SUGGESTED: frozenset(
        {
            LeaveStatus.PENDING,
            LeaveStatus.APPROVED,
            LeaveStatus.DENIED,
            LeaveStatus.APPROVED_FORCED,
            LeaveStatus.CANCELLED,
        }
    ),
    LeaveStatus.PENDING: frozenset(
        {
            LeaveStatus.APPROVED,
            LeaveStatus.DENIED,
            LeaveStatus.CANCELLED,
        }
    ),
    LeaveStatus.APPROVED: frozenset(
        {LeaveStatus.CANCELLED, LeaveStatus.RECALLED}
    ),
    LeaveStatus.APPROVED_FORCED: frozenset(
        {LeaveStatus.CANCELLED, LeaveStatus.RECALLED}
    ),
    LeaveStatus.DENIED: frozenset(),
    LeaveStatus.CANCELLED: frozenset(),
    LeaveStatus.RECALLED: frozenset(),
}

#: Transitions only HR/Admin may perform.
ADMIN_ONLY_EVENTS: frozenset[Transition] = frozenset({Transition.APPROVE})

#: Transitions only the requesting employee may perform.
EMPLOYEE_ONLY_EVENTS: frozenset[Transition] = frozenset(
    {Transition.ACCEPT, Transition.DENY, Transition.CANCEL}
)

#: Which event maps to which target status (ignoring the denial special case).
_EVENT_TARGETS: dict[Transition, LeaveStatus] = {
    Transition.ACCEPT: LeaveStatus.APPROVED,
    Transition.APPROVE: LeaveStatus.APPROVED,
    Transition.DENY: LeaveStatus.DENIED,
    Transition.CANCEL: LeaveStatus.CANCELLED,
}


@dataclass(frozen=True)
class TransitionOutcome:
    """What the service should persist after a legal transition."""

    to_status: LeaveStatus
    from_status: LeaveStatus
    denial_count: int
    forced: bool = False
    reused_denial: bool = False
    releasable_days: int = 0
    consume_days: int = 0
    note: str | None = None
    requires_reapproval: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.from_status != self.to_status or self.forced


def can_transition(current: LeaveStatus | str, target: LeaveStatus | str) -> bool:
    """Whether ``current -> target`` is a legal edge."""
    source = LeaveStatus.coerce(current)
    destination = LeaveStatus.coerce(target)
    return destination in LEGAL_TRANSITIONS.get(source, frozenset())


def assert_transition(current: LeaveStatus | str, target: LeaveStatus | str) -> None:
    """Raise :class:`InvalidLeaveTransitionError` when the edge is illegal."""
    if not can_transition(current, target):
        raise InvalidLeaveTransitionError(
            f"Cannot move a leave request from {LeaveStatus.coerce(current).value} "
            f"to {LeaveStatus.coerce(target).value}."
        )


def is_employee_event(event: Transition | str) -> bool:
    return Transition.coerce(event) in EMPLOYEE_ONLY_EVENTS


def is_admin_event(event: Transition | str) -> bool:
    return Transition.coerce(event) in ADMIN_ONLY_EVENTS


# ---------------------------------------------------------------------------
# The four use-cases
# ---------------------------------------------------------------------------
def apply_employee_response(
    *,
    status: LeaveStatus | str,
    denial_count: int,
    event: Transition | str,
    days: int = 0,
) -> TransitionOutcome:
    """Handle the employee's accept/deny decision on a suggestion.

    Denial is allowed exactly once.  A second denial force-approves the current
    window instead of rejecting it, and reports ``reused_denial`` so the caller
    can tell the employee their denial has been spent.
    """
    current = LeaveStatus.coerce(status)
    action = Transition.coerce(event)

    if action not in {Transition.ACCEPT, Transition.DENY}:
        raise InvalidLeaveTransitionError(
            f"'{action.value}' is not a valid employee response."
        )

    if current != LeaveStatus.SUGGESTED:
        raise InvalidLeaveTransitionError(
            f"An employee can only respond to a suggested request "
            f"(current status: {current.value})."
        )

    if action == Transition.ACCEPT:
        assert_transition(current, LeaveStatus.APPROVED)
        return TransitionOutcome(
            to_status=LeaveStatus.APPROVED,
            from_status=current,
            denial_count=denial_count,
            consume_days=days,
            note="Employee accepted the suggested window.",
            reasons=("employee_accepted",),
        )

    # --- denial path ---
    if denial_count < MAX_DENIALS:
        assert_transition(current, LeaveStatus.DENIED)
        return TransitionOutcome(
            to_status=LeaveStatus.DENIED,
            from_status=current,
            denial_count=denial_count + 1,
            note="Employee declined the suggested window; a new window was offered.",
            reasons=("denial_recorded", "resuggest"),
        )

    # Denial limit already reached: force-approve.
    assert_transition(current, LeaveStatus.APPROVED_FORCED)
    return TransitionOutcome(
        to_status=LeaveStatus.APPROVED_FORCED,
        from_status=current,
        denial_count=denial_count,
        forced=True,
        reused_denial=True,
        consume_days=days,
        note="Denial limit reached; the current window was force-approved.",
        reasons=("denial_limit_reached", "force_approved"),
    )


def apply_admin_decision(
    *,
    status: LeaveStatus | str,
    approve: bool,
    days: int = 0,
) -> TransitionOutcome:
    """Handle an HR/Admin approve or deny decision."""
    current = LeaveStatus.coerce(status)
    target = LeaveStatus.APPROVED if approve else LeaveStatus.DENIED
    assert_transition(current, target)

    return TransitionOutcome(
        to_status=target,
        from_status=current,
        denial_count=0,
        consume_days=days if approve else 0,
        note="Approved by HR." if approve else "Denied by HR.",
        reasons=("admin_approved",) if approve else ("admin_denied",),
    )


def apply_cancellation(
    *,
    status: LeaveStatus | str,
    days: int = 0,
) -> TransitionOutcome:
    """Cancel a request, releasing any consumed balance."""
    current = LeaveStatus.coerce(status)
    assert_transition(current, LeaveStatus.CANCELLED)

    consumed = days if current.is_live else 0
    return TransitionOutcome(
        to_status=LeaveStatus.CANCELLED,
        from_status=current,
        denial_count=0,
        releasable_days=consumed,
        note="Request cancelled.",
        reasons=("cancelled",),
    )


def apply_recall(
    *,
    status: LeaveStatus | str,
    days: int = 0,
) -> TransitionOutcome:
    """Recall an employee from approved leave.

    The days are refunded to the balance so they can be taken later; only a
    live (already-approved) request has days to give back.
    """
    current = LeaveStatus.coerce(status)
    assert_transition(current, LeaveStatus.RECALLED)

    return TransitionOutcome(
        to_status=LeaveStatus.RECALLED,
        from_status=current,
        denial_count=0,
        # Releasing rather than consuming: the entitlement is not spent.
        releasable_days=days if current.is_live else 0,
        note="Recalled by HR; the days were returned for rescheduling.",
        reasons=("recalled",),
    )


def next_window_after_denial(    *,
    today,
    days: int,
    calendar,
    constraints,
    excluded_ranges=(),
    horizon_days: int = 120,
):
    """Re-suggest after a denial, skipping the declined window.

    Delegates entirely to the pure engine so the ranking logic has exactly one
    implementation.
    """
    return find_best_windows(
        today=today,
        days=days,
        calendar=calendar,
        constraints=constraints,
        excluded_ranges=excluded_ranges,
        horizon_days=horizon_days,
    )


def windows_from_suggestions(suggestions) -> list[Window]:
    """Rebuild :class:`Window` objects from a stored ``suggestions`` payload."""
    windows: list[Window] = []
    for entry in suggestions or []:
        try:
            windows.append(Window.from_dict(entry))
        except (KeyError, TypeError, ValueError):
            continue
    return windows
