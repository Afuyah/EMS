"""Background jobs.

Every module here exposes a pure ``*_impl`` function plus a ``@celery.task``
wrapper, which keeps each job runnable from the CLI without a broker::

    flask run-job nightly_attendance
"""

from app.jobs import (  # noqa: F401
    allocate_balances,
    color_recompute,
    device_health,
    disciplinary_evaluator,
    email_dispatch,
    nightly_attendance,
    notifications_dispatcher,
)

#: Job name -> (module attribute, callable) used by ``flask run-job``.
JOB_REGISTRY: dict[str, str] = {
    "notifications_dispatcher": "app.jobs.notifications_dispatcher:notifications_dispatcher_impl",
    "email_dispatch": "app.jobs.email_dispatch:email_dispatch_impl",
    "device_health": "app.jobs.device_health:device_health_impl",
    "nightly_attendance": "app.jobs.nightly_attendance:nightly_attendance_impl",
    "color_recompute": "app.jobs.color_recompute:color_recompute_impl",
    "disciplinary_evaluator": "app.jobs.disciplinary_evaluator:disciplinary_evaluator_impl",
    "allocate_balances": "app.jobs.allocate_balances:allocate_balances_impl",
    "backfill_absences": "app.jobs.nightly_attendance:backfill_absences_impl",
    "carry_over": "app.jobs.allocate_balances:carry_over_impl",
}
