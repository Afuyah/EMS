"""Nightly attendance job.

Runs at 02:00.  For the target date it:

* marks employees covered by approved leave as ``ON_LEAVE``
* marks active employees with no punches as ``ABSENT``
* leaves partial days (clocked in, never out) untouched for HR review
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import today_local, utcnow
from app.features.attendance.constants import AttendanceStatus
from app.features.attendance.repository import AttendanceRepository
from app.features.attendance.service import AttendanceService

logger = get_logger(__name__)


def nightly_attendance_impl(*, work_date: date | None = None, tz_name: str | None = None) -> dict:
    """Reconcile one day of attendance.

    Defaults to *yesterday* in the default timezone: at 02:00 the day that just
    ended is the one that needs closing.
    """
    from app.core.time import get_zone

    resolved_date = work_date or (utcnow().astimezone(get_zone(tz_name)).date() - timedelta(days=1))

    attendance = AttendanceRepository()
    service = AttendanceService(attendance=attendance)

    # Employees with an approved leave covering the date.
    from app.features.leave.repository import LeaveRequestRepository

    on_leave_ids = {
        request.employee_id
        for request in LeaveRequestRepository().active_on(resolved_date)
    }

    # Anyone whose policy configures an intra-day absence cut-off is closed out
    # first, so the sweep below only has to handle the policy-less remainder.
    cutoff_summary = service.mark_due_absences(
        work_date=resolved_date,
        now=utcnow(),
        exclude_employee_ids=on_leave_ids,
    )

    # Everyone active, and any row already recorded for the date.
    from app.features.employees.repository import EmployeeRepository

    employees = EmployeeRepository()
    active_ids = employees.active_employee_ids()
    existing = {
        record.employee_id: record
        for record in attendance.list_for_day(resolved_date)
    }

    marked_leave = 0
    marked_absent = 0
    skipped_recorded = 0
    skipped_open = 0

    for employee_id in active_ids:
        record = existing.get(employee_id)

        if record is not None and record.clock_in is not None:
            if record.clock_out is None:
                # Clocked in but never out: leave it for a human correction.
                skipped_open += 1
                continue
            skipped_recorded += 1
            continue

        if employee_id in on_leave_ids:
            service.mark_on_leave(employee_id, resolved_date)
            marked_leave += 1
            continue

        if record is not None and record.status not in {
            AttendanceStatus.ABSENT.value,
        }:
            skipped_recorded += 1
            continue

        service.mark_absence(
            employee_id, resolved_date, note="Auto-marked absent by nightly job."
        )
        marked_absent += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    summary = {
        "work_date": resolved_date.isoformat(),
        "on_leave": marked_leave,
        "absent": marked_absent,
        "skipped_recorded": skipped_recorded,
        "skipped_open": skipped_open,
        "considered": len(active_ids),
        "cutoff_sweep": cutoff_summary,
    }
    logger.info("nightly_attendance %s", summary)
    return summary


def backfill_absences_impl(*, days: int = 7, end_date: date | None = None) -> dict:
    """Run the reconciliation backwards over ``days`` days."""
    from app.core.time import get_zone

    end = end_date or (utcnow().astimezone(get_zone(None)).date() - timedelta(days=1))
    results = []
    for offset in range(days):
        target = end - timedelta(days=offset)
        results.append(nightly_attendance_impl(work_date=target))
    return {
        "days": days,
        "results": results,
        "total_absent": sum(item["absent"] for item in results),
        "total_on_leave": sum(item["on_leave"] for item in results),
    }


def register(celery) -> None:
    """Attach the Celery task wrappers."""

    @celery.task(name="app.jobs.nightly_attendance.mark_absences_task")
    def mark_absences_task(days: int = 1) -> dict:
        if days and days > 1:
            return backfill_absences_impl(days=days)
        return nightly_attendance_impl()


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
