"""Celery application factory and Beat schedule.

Each job module under ``app/jobs`` exposes a pure ``*_impl`` function plus a
``@celery.task`` wrapper.  The split exists so jobs remain runnable from the
CLI (``flask run-job <name>``) without a broker.

Start a worker with::

    celery -A celery_worker.celery worker -l info -B
"""

from celery import Celery
from celery.schedules import crontab

from app import create_app
from app.config import get_config

flask_app = create_app()

celery = Celery(
    flask_app.import_name,
    broker=flask_app.config["CELERY_BROKER_URL"],
    backend=flask_app.config["CELERY_RESULT_BACKEND"],
    include=[
        "app.jobs.notifications_dispatcher",
        "app.jobs.email_dispatch",
        "app.jobs.device_health",
        "app.jobs.nightly_attendance",
        "app.jobs.color_recompute",
        "app.jobs.disciplinary_evaluator",
        "app.jobs.allocate_balances",
    ],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=flask_app.config["DEFAULT_TIMEZONE"],
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,
    task_soft_time_limit=25 * 60,
    worker_max_tasks_per_child=200,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "notifications-dispatcher": {
            "task": "app.jobs.notifications_dispatcher.dispatch_pending",
            "schedule": 60.0,
        },
        # Drains the email outbox: anything still pending is re-attempted.
        "email-dispatch": {
            "task": "app.jobs.email_dispatch.retry_pending",
            "schedule": 60.0,
        },
        "device-health": {
            "task": "app.jobs.device_health.check_devices",
            "schedule": 300.0,
        },
        "nightly-attendance": {
            "task": "app.jobs.nightly_attendance.mark_absences_task",
            "schedule": crontab(hour=2, minute=0),
        },
        "color-recompute": {
            "task": "app.jobs.color_recompute.recompute_colors_task",
            "schedule": crontab(hour=2, minute=30),
        },
        "disciplinary-evaluator": {
            "task": "app.jobs.disciplinary_evaluator.evaluate_task",
            "schedule": crontab(hour=3, minute=0),
        },
        "allocate-balances": {
            "task": "app.jobs.allocate_balances.allocate_balances_task",
            "schedule": crontab(month_of_year=1, day_of_month=1, hour=0, minute=5),
        },
    },
)


class _FlaskTask(celery.Task):
    """Base task that pushes a Flask app context around every execution."""

    abstract = True

    def __call__(self, *args, **kwargs):
        with flask_app.app_context():
            return self.run(*args, **kwargs)


# Install the Flask-aware base class *before* the job modules import, so their
# ``@celery.task`` wrappers bind to it.  ``include=`` only loads lazily when a
# worker actually starts, which would leave the tasks unregistered for anything
# that imports this module directly (tests, tooling, ``flask jobs``).
celery.Task = _FlaskTask

from app.jobs import (  # noqa: E402  (must follow the Task base swap)
    allocate_balances,
    color_recompute,
    device_health,
    disciplinary_evaluator,
    email_dispatch,
    nightly_attendance,
    notifications_dispatcher,
)

for _module in (
    notifications_dispatcher,
    email_dispatch,
    device_health,
    nightly_attendance,
    color_recompute,
    disciplinary_evaluator,
    allocate_balances,
):
    _module.register(celery)


def make_celery_app(config_name: str | None = None):
    """Build a Celery app bound to an explicit config (used by tests/CLI)."""
    cfg = get_config(config_name)
    app = Celery(
        "ems",
        broker=cfg.CELERY_BROKER_URL,
        backend=cfg.CELERY_RESULT_BACKEND,
    )
    app.conf.update(task_serializer="json", result_serializer="json")
    return app
