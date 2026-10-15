"""Colour recompute job.

Runs at 02:30.  Recomputes every active employee's attendance-health colour
from the configured ladder and notifies anyone whose colour escalated.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import today_local, utcnow
from app.features.employees.models import Employee
from app.features.employees.repository import EmployeeRepository
from app.features.notifications.constants import Channel
from app.features.notifications.service import NotificationService
from app.features.rules.color_evaluator import ColorRuleSpec, evaluate_color
from app.features.rules.constants import COLOR_WINDOW_DEFAULT_DAYS, EVENT_COLOR_ESCALATED
from app.features.rules.repository import ColorRuleRepository, RuleMetricsRepository

logger = get_logger(__name__)


def color_recompute_impl(
    *,
    department_id: int | None = None,
    window_days: int | None = None,
    notify: bool = True,
) -> dict:
    """Recompute colours and report the transitions.

    Counts are gathered in one grouped query per distinct window length, so a
    department of 200 employees still costs a handful of statements.
    """
    employees_repo = EmployeeRepository()
    rules_repo = ColorRuleRepository()
    metrics = RuleMetricsRepository()
    notifier = NotificationService()

    employees = (
        employees_repo.search(department_id=department_id, is_active=True)
        if department_id is not None
        else employees_repo.list(is_active=True)
    )
    if not employees:
        return {"evaluated": 0, "changed": 0, "escalated": 0, "windows": []}

    # Group employees by the window length their rule set asks for.
    rules_cache: dict[int | None, list] = {}
    window_cache: dict[int | None, int] = {}

    def rules_for(dept_id: int | None):
        if dept_id not in rules_cache:
            rules_cache[dept_id] = rules_repo.active_rules(dept_id)
        return rules_cache[dept_id]

    def window_for(dept_id: int | None) -> int:
        if dept_id not in window_cache:
            configured = [
                rule.window_days for rule in rules_for(dept_id) if rule.window_days
            ]
            window_cache[dept_id] = (
                window_days or max(configured) if configured else (window_days or COLOR_WINDOW_DEFAULT_DAYS)
            )
        return window_cache[dept_id]

    # window_days -> employees sharing it
    by_window: dict[int, list[Employee]] = {}
    for employee in employees:
        by_window.setdefault(window_for(employee.department_id), []).append(employee)

    evaluated = 0
    changed = 0
    escalated = 0
    transitions: list[dict] = []

    for window, group in by_window.items():
        employee_ids = [employee.id for employee in group]
        end = max((today_local(employee.tz) for employee in group), default=None)
        if end is None:
            continue
        start = end - timedelta(days=window - 1)
        counts = metrics.window_counts(
            employee_ids=employee_ids, start=start, end=end
        )

        for employee in group:
            counters = counts.get(employee.id, {"lates": 0, "absences": 0})
            specs = [
                ColorRuleSpec(
                    color=rule.color,
                    min_lates=rule.min_lates,
                    min_absences=rule.min_absences,
                    window_days=rule.window_days,
                    priority=rule.priority,
                    department_id=rule.department_id,
                    id=rule.id,
                )
                for rule in rules_for(employee.department_id)
                # Department rules shadow global rules of the same colour.
                if rule.department_id in (employee.department_id, None)
            ]
            outcome = evaluate_color(
                lates=int(counters.get("lates", 0)),
                absences=int(counters.get("absences", 0)),
                rules=specs,
                current=employee.current_color,
            )
            evaluated += 1

            if outcome.color.value != employee.current_color:
                transitions.append(
                    {
                        "employee_id": employee.id,
                        "employee_no": employee.employee_no,
                        "from": employee.current_color,
                        "to": outcome.color.value,
                        "lates": outcome.lates,
                        "absences": outcome.absences,
                        "escalated": outcome.escalated,
                    }
                )
                employee.current_color = outcome.color.value
                employees_repo.update(employee)
                changed += 1

            if outcome.escalated and notify:
                escalated += 1
                _notify_escalation(
                    notifier, employee, outcome, window=window
                )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    logger.info(
        "color_recompute evaluated=%s changed=%s escalated=%s",
        evaluated, changed, escalated,
    )
    return {
        "evaluated": evaluated,
        "changed": changed,
        "escalated": escalated,
        "transitions": transitions[:100],
    }


def _notify_escalation(notifier: NotificationService, employee: Employee, outcome, *, window: int) -> None:
    """Tell the employee (and their manager) that their colour worsened.

    Delivered in-app and by email.  The email is sent through ``send_safely`` so
    a mail outage can never roll back the colour change this job just made.
    """
    payload = {
        "employee_id": employee.id,
        "color": outcome.color.value,
        "lates": outcome.lates,
        "absences": outcome.absences,
        "window_days": window,
    }
    body = (
        f"Your attendance health indicator moved to "
        f"{outcome.color.label} based on {outcome.lates} late arrival(s) and "
        f"{outcome.absences} absence(s) in the last {window} days."
    )
    if employee.user_id is not None:
        notifier.enqueue(
            event_type=EVENT_COLOR_ESCALATED,
            subject=f"Attendance indicator: {outcome.color.label}",
            body=body,
            recipient_user_id=employee.user_id,
            recipient_employee_id=employee.id,
            channel=Channel.IN_APP,
            payload=payload,
        )

    _email_escalation(employee, outcome, window=window)


def _email_escalation(employee: Employee, outcome, *, window: int) -> None:
    """Send the colour-escalation email, contained so it cannot break the job."""
    if not getattr(employee, "email", None):
        return

    from flask import current_app

    from app.features.email.constants import EVENT_COLOR_ESCALATED
    from app.features.email.service import EmailService

    base = str(current_app.config.get("APP_BASE_URL", "")).rstrip("/")
    link = f"{base}/app/dashboard" if base else "/app/dashboard"

    EmailService().send_event(
        event_type=EVENT_COLOR_ESCALATED,
        to_email=employee.email,
        context={
            "employee_name": employee.full_name,
            "employee_no": employee.employee_no,
            "color": outcome.color.value,
            "color_label": outcome.color.label,
            "lates": outcome.lates,
            "absences": outcome.absences,
            "window_days": window,
            "link": link,
        },
        recipient_employee_id=employee.id,
        recipient_user_id=employee.user_id,
        payload={"color": outcome.color.value, "window_days": window},
    )


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.color_recompute.recompute_colors_task")
    def recompute_colors_task(department_id: int | None = None, window_days: int | None = None) -> dict:
        return color_recompute_impl(
            department_id=department_id, window_days=window_days
        )


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
