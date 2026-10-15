"""Reports and bulk-import views."""

from __future__ import annotations

from datetime import date, timedelta

from flask import request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.auth.constants import Role
from app.features.employees.service import DepartmentService
from app.features.exports.service import ExportsService
from app.features.leave.service import LeaveService
from app.features.reports.service import ReportsService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_info,
    form_bool,
    principal,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_reports = ReportsService()
_departments = DepartmentService()
_leave = LeaveService()
_exports = ExportsService()

#: Report tabs -> (label, description)
REPORT_TABS: tuple[tuple[str, str, str], ...] = (
    ("attendance", "Attendance summary", "Per-department attendance over a date range"),
    ("colors", "Attendance health", "Colour distribution across the workforce"),
    ("headcount", "Daily headcount", "Active employees, clock-ins and leave per day"),
    ("late", "Top late", "Employees with the most late arrivals"),
    ("leave", "Leave utilisation", "Entitlement versus usage by leave type"),
    ("ledger", "Balance ledger", "Per-employee balances for a department"),
)


@views_bp.get("/reports")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN, Role.MANAGER)
def reports():
    """Reports page: every aggregation is a single-query service call."""
    tab = request.args.get("tab") or "attendance"
    if tab not in {key for key, _label, _desc in REPORT_TABS}:
        tab = "attendance"

    today = today_local(None)
    default_start = today - timedelta(days=29)

    start = _parse_date(request.args.get("start")) or default_start
    end = _parse_date(request.args.get("end")) or today
    if end < start:
        start, end = end, start

    year = request.args.get("year", type=int) or today.year
    departments = _departments.list_departments()

    # No filter means *every* department (None), not an arbitrary first one.
    # Defaulting to departments[0] silently showed an empty report whenever that
    # department happened to have no staff, and there was no way to clear it.
    department_id = request.args.get("department_id", type=int)
    if department_id is not None and department_id not in {
        department.id for department in departments
    }:
        department_id = None

    query = {
        "tab": tab,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "year": year,
    }
    if department_id is not None:
        query["department_id"] = department_id

    context = dict(
        active_nav="Reports",
        tabs=REPORT_TABS,
        tab=tab,
        today=today,
        start=start,
        end=end,
        year=year,
        departments=departments,
        department_id=department_id,
        selected_department=next(
            (d for d in departments if d.id == department_id), None
        ),
        query=query,
        overview=_reports.overview(),
    )

    if tab == "attendance":
        context["summary"] = _reports.attendance_summary(
            start=start, end=end, department_id=department_id
        )
    elif tab == "colors":
        context["colors"] = _reports.attendance_colors(department_id=department_id)
    elif tab == "headcount":
        context["headcount"] = _reports.headcount(start=start, end=end)
    elif tab == "late":
        context["top_late"] = _reports.top_late(
            start=start, end=end, department_id=department_id, limit=25
        )
    elif tab == "leave":
        context["utilisation"] = _reports.leave_utilisation(
            year=year, department_id=department_id
        )
    elif tab == "ledger":
        context["ledger"] = (
            _reports.balance_ledger(department_id=department_id, year=year)
            if department_id
            else {"rows": []}
        )

    return render("reports/index.html", **context)


@views_bp.post("/reports/balances/import")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def reports_import_balances():
    """Bulk-import leave balances from an uploaded CSV."""
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        flash_error("Please choose a CSV file to upload.")
        from flask import redirect

        return redirect(url_for("views.reports", tab="ledger"))

    try:
        text = upload.read().decode("utf-8-sig", errors="replace")
    except Exception:  # noqa: BLE001 - decode problems are user errors
        logger.exception("balance import: could not read upload")
        flash_error("The file could not be read as text.")
        from flask import redirect

        return redirect(url_for("views.reports", tab="ledger"))

    overwrite = form_bool("overwrite", True)

    try:
        summary = _exports.import_balances(text, overwrite=overwrite)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        from flask import redirect

        return redirect(url_for("views.reports", tab="ledger"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("balance import failed")
        flash_error("The import could not be completed.")
        from flask import redirect

        return redirect(url_for("views.reports", tab="ledger"))

    if summary["imported"]:
        flash_info(
            f"Imported {summary['imported']} row(s); "
            f"{summary['skipped']} skipped, {summary['error_count']} error(s)."
        )
    else:
        flash_error(
            f"Nothing was imported. {summary['error_count']} row(s) had errors."
        )

    from flask import redirect

    return redirect(url_for("views.reports", tab="ledger"))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
