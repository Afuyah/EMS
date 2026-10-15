"""Disciplinary evaluator job.

Runs at 03:00.  For every active employee it counts late arrivals, absences and
early departures over each rule's window and records an action when a threshold
is crossed.  Rules with ``auto_apply`` are recorded automatically; the rest are
raised to HR as a recommendation.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.employees.repository import EmployeeRepository
from app.features.notifications.constants import Channel
from app.features.notifications.service import NotificationService
from app.features.rules.constants import EVENT_DISCIPLINARY_ACTION, RulePeriod
from app.features.rules.disciplinary_evaluator import (
    DisciplinaryRuleSpec,
    evaluate,
    pick_action,
)
from app.features.rules.repository import (
    DisciplinaryActionRepository,
    DisciplinaryRuleRepository,
    RuleMetricsRepository,
)

logger = get_logger(__name__)


def disciplinary_evaluator_impl(
    *,
    department_id: int | None = None,
    as_of: date | None = None,
    actor_user_id: int | None = None,
    notify: bool = True,
) -> dict:
    """Evaluate disciplinary thresholds and record/propose actions."""
    employees_repo = EmployeeRepository()
    rules_repo = DisciplinaryRuleRepository()
    actions_repo = DisciplinaryActionRepository()
    metrics = RuleMetricsRepository()
    notifier = NotificationService()

    employees = (
        employees_repo.search(department_id=department_id, is_active=True)
        if department_id is not None
        else employees_repo.list(is_active=True)
    )
    if not employees:
        return {"evaluated": 0, "applied": 0, "proposed": 0, "skipped_existing": 0}

    # Metrics are gathered once over the widest window any rule needs.
    widest = max((RulePeriod.coerce(rule.period).days for rule in rules_repo.active_rules(
        department_id
    )), default=90)
    reference = as_of or max((today_local(employee.tz) for employee in employees))
    start = reference - timedelta(days=widest)

    counts = metrics.window_counts(
        employee_ids=[employee.id for employee in employees], start=start, end=reference
    )

    applied = 0
    proposed = 0
    skipped = 0
    records: list[dict] = []

    for employee in employees:
        counters = counts.get(employee.id)
        if not counters:
            continue

        rules = [
            DisciplinaryRuleSpec(
                trigger_type=rule.trigger_type,
                threshold=rule.threshold,
                period=rule.period,
                action=rule.action,
                suspension_days=rule.suspension_days,
                auto_apply=rule.auto_apply,
                is_active=rule.is_active,
                department_id=rule.department_id,
                id=rule.id,
            )
            for rule in rules_repo.active_rules(employee.department_id)
            # Department rules take precedence over global ones.
            if rule.department_id in (employee.department_id, None)
        ]
        matches = evaluate(counters=counters, rules=rules, on=reference)
        if not matches:
            continue

        chosen = pick_action(matches)
        if chosen is None:
            continue

        triggered_on = reference

        # ``unique(employee_id, rule_id, triggered_on)`` guard: never double-apply.
        if actions_repo.exists_for(
            employee_id=employee.id,
            rule_id=chosen.rule.id,
            triggered_on=triggered_on,
        ):
            skipped += 1
            continue

        if not chosen.auto_apply:
            # Manual rules are surfaced for HR rather than applied silently.
            proposed += 1
            if notify:
                _notify(
                    notifier,
                    employee,
                    chosen,
                    applied=False,
                    triggered_on=triggered_on,
                )
            records.append(
                {
                    "employee_no": employee.employee_no,
                    "action": chosen.action,
                    "auto_apply": False,
                    "observed": chosen.observed,
                }
            )
            continue

        action_row = actions_repo.create(
            employee_id=employee.id,
            rule_id=chosen.rule.id,
            action=chosen.action,
            triggered_on=triggered_on,
            details=chosen.as_details(),
            applied_by=actor_user_id,
        )
        applied += 1
        if notify:
            _notify(
                notifier,
                employee,
                chosen,
                applied=True,
                triggered_on=triggered_on,
                action_id=getattr(action_row, "id", None),
            )
        records.append(
            {
                "employee_no": employee.employee_no,
                "action": chosen.action,
                "auto_apply": True,
                "observed": chosen.observed,
            }
        )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    summary = {
        "evaluated": len(employees),
        "applied": applied,
        "proposed": proposed,
        "skipped_existing": skipped,
        "as_of": reference.isoformat(),
        "records": records[:100],
    }
    logger.info(
        "disciplinary_evaluator evaluated=%s applied=%s proposed=%s skipped=%s",
        summary["evaluated"], applied, proposed, skipped,
    )
    return summary


def _notify(
    notifier: NotificationService,
    employee,
    match,
    *,
    applied: bool,
    triggered_on=None,
    action_id: int | None = None,
) -> None:
    """Tell the employee about an applied or recommended action.

    Delivered in-app and by email.  Both channels are best effort: the action row
    is the record of truth and must not be rolled back because a mail server is
    down.
    """
    verb = "has been recorded" if applied else "is recommended for HR review"
    body = (
        f"A {match.action.replace('_', ' ')} {verb} because "
        f"{match.rule.trigger_type.replace('_', ' ')} count reached "
        f"{match.observed} against a threshold of {match.rule.threshold} "
        f"for this {match.rule.period}."
    )
    if employee.user_id is not None:
        notifier.enqueue(
            event_type=EVENT_DISCIPLINARY_ACTION,
            subject=f"Disciplinary action: {match.action.replace('_', ' ').title()}",
            body=body,
            recipient_user_id=employee.user_id,
            recipient_employee_id=employee.id,
            channel=Channel.IN_APP,
            payload=match.as_details(),
        )

    _email_disciplinary(
        employee,
        match,
        applied=applied,
        triggered_on=triggered_on,
        action_id=action_id,
    )


def _email_disciplinary(
    employee,
    match,
    *,
    applied: bool,
    triggered_on=None,
    action_id: int | None = None,
) -> None:
    """Email the employee about the action, contained so it cannot break the job."""
    if not getattr(employee, "email", None):
        return

    from datetime import date as _date

    from flask import current_app

    from app.features.email.constants import EVENT_DISCIPLINARY_ACTION
    from app.features.email.service import EmailService

    base = str(current_app.config.get("APP_BASE_URL", "")).rstrip("/")
    link = f"{base}/app/profile" if base else "/app/profile"
    when = triggered_on or _date.today()

    EmailService().send_event(
        event_type=EVENT_DISCIPLINARY_ACTION,
        to_email=employee.email,
        context={
            "employee_name": employee.full_name,
            "employee_no": employee.employee_no,
            "action_label": match.action.replace("_", " ").title(),
            "action_id": action_id if action_id is not None else "pending",
            "trigger_type": match.rule.trigger_type.replace("_", " "),
            "observed": match.observed,
            "threshold": match.rule.threshold,
            "period": match.rule.period,
            "suspension_days": match.rule.suspension_days,
            "triggered_on": when.strftime("%d %b %Y"),
            "applied": applied,
            "link": link,
        },
        recipient_employee_id=employee.id,
        recipient_user_id=employee.user_id,
        payload={"action": match.action, "applied": applied},
    )


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.disciplinary_evaluator.evaluate_task")
    def evaluate_task(department_id: int | None = None) -> dict:
        return disciplinary_evaluator_impl(department_id=department_id)


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
