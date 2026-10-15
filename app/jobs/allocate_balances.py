"""Year-start balance allocation job.

Runs on Jan 1 at 00:05.  Gives every active employee their entitlement for the
new year, derived from the leave rules that apply to their department.
"""

from __future__ import annotations

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.leave.service import LeaveService

logger = get_logger(__name__)


def allocate_balances_impl(*, year: int | None = None, overwrite: bool = False) -> dict:
    """Allocate entitlements for ``year`` (defaults to the current UTC year)."""
    resolved_year = year or utcnow().year
    service = LeaveService()
    try:
        summary = service.allocate_year(year=resolved_year, overwrite=overwrite)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    logger.info("allocate_balances %s", summary)
    return summary


def carry_over_impl(*, from_year: int, to_year: int, max_carry: int = 5) -> dict:
    """Optional carry-over: move unused days from one year into the next.

    Capped by ``max_carry`` so unused entitlement cannot compound indefinitely.
    """
    from app.features.leave.repository import LeaveBalanceRepository

    balances = LeaveBalanceRepository()
    source = balances.for_year(from_year)
    carried = 0

    for record in source:
        unused = max(0, record.entitled - record.used - record.pending)
        if unused <= 0:
            continue
        transfer = min(unused, max_carry)
        target = balances.get_or_create(
            employee_id=record.employee_id,
            year=to_year,
            leave_type=record.leave_type,
        )
        balances.update(target, entitled=target.entitled + transfer)
        carried += 1

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    logger.info("carry_over %s->%s employees=%s", from_year, to_year, carried)
    return {"from_year": from_year, "to_year": to_year, "employees": carried}


def register(celery) -> None:
    """Attach the Celery task wrapper."""

    @celery.task(name="app.jobs.allocate_balances.allocate_balances_task")
    def allocate_balances_task(year: int | None = None, overwrite: bool = False) -> dict:
        return allocate_balances_impl(year=year, overwrite=overwrite)


try:  # pragma: no cover - only when a Celery app exists
    from celery_worker import celery as _celery

    register(_celery)
except Exception:  # noqa: BLE001 - CLI usage has no Celery app
    pass
