"""CSV export and balance import routes for the JSON API."""

from __future__ import annotations

from datetime import date

from flask import Blueprint, Response, request
from flask_jwt_extended import jwt_required

from app.core.database import db
from app.core.exceptions import ValidationError
from app.core.permissions import require_roles
from app.core.responses import ok
from app.features.auth.constants import Role
from app.features.exports.service import ExportsService
from app.features.reports.service import ReportsService

exports_api = Blueprint("exports_api", __name__, url_prefix="/exports")

_service = ExportsService()


def _date_arg(name: str) -> date | None:
    raw = request.args.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"Invalid {name} {raw!r}; expected YYYY-MM-DD.") from exc


def _csv_response(payload: str, filename: str) -> Response:
    """Stream a CSV download with an explicit charset."""
    return Response(
        payload,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@exports_api.get("/attendance/department/<int:department_id>.csv")
@jwt_required()
def attendance_department_csv(department_id: int):
    """Attendance summary CSV for one department."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    payload, filename = _service.attendance_department(
        department_id=department_id, start=start, end=end
    )
    return _csv_response(payload, filename)


@exports_api.get("/attendance/summary.csv")
@jwt_required()
def attendance_summary_csv():
    """Attendance summary CSV across every department."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    payload, filename = _service.attendance_summary_all(start=start, end=end)
    return _csv_response(payload, filename)


@exports_api.get("/attendance/headcount.csv")
@jwt_required()
def headcount_csv():
    """Daily headcount CSV."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    payload, filename = _service.headcount(start=start, end=end)
    return _csv_response(payload, filename)


@exports_api.get("/attendance/top_late.csv")
@jwt_required()
def top_late_csv():
    """Top late employees CSV."""
    start, end = ReportsService.resolve_range(_date_arg("start"), _date_arg("end"))
    payload, filename = _service.top_late(
        start=start,
        end=end,
        department_id=request.args.get("department_id", type=int),
        limit=min(request.args.get("limit", type=int) or 50, 500),
    )
    return _csv_response(payload, filename)


@exports_api.get("/attendance/colors.csv")
@jwt_required()
def attendance_colors_csv():
    """Attendance-health distribution CSV."""
    payload, filename = _service.attendance_colors(
        department_id=request.args.get("department_id", type=int)
    )
    return _csv_response(payload, filename)


@exports_api.get("/leave/utilization.csv")
@jwt_required()
def leave_utilisation_csv():
    """Leave utilisation CSV for a year."""
    year = request.args.get("year", type=int) or date.today().year
    payload, filename = _service.leave_utilisation(
        year=year, department_id=request.args.get("department_id", type=int)
    )
    return _csv_response(payload, filename)


@exports_api.get("/leave/balances/<int:department_id>.csv")
@jwt_required()
def balance_ledger_csv(department_id: int):
    """Balance ledger CSV for one department and year."""
    year = request.args.get("year", type=int) or date.today().year
    payload, filename = _service.balance_ledger(department_id=department_id, year=year)
    return _csv_response(payload, filename)


@exports_api.post("/leave/balances/import")
@jwt_required()
def import_balances():
    """Bulk-import balances from an uploaded CSV (HR/Admin).

    Accepts ``text/csv`` as the raw body or as a ``file`` form field.
    """
    require_roles(Role.HR, Role.ADMIN)

    text: str | None = None
    if "file" in request.files:
        text = request.files["file"].read().decode("utf-8-sig", errors="replace")
    else:
        text = request.get_data(as_text=True)

    if not text:
        raise ValidationError("No CSV content was supplied.")

    overwrite = request.args.get("overwrite", "true").lower() not in {"0", "false", "no"}

    try:
        summary = _service.import_balances(text, overwrite=overwrite)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return ok(summary)


@exports_api.get("/leave/balances/template.csv")
@jwt_required()
def balance_template():
    """A ready-to-fill template for the bulk import."""
    header = "employee_no,year,leave_type,entitled,used,pending\n"
    sample = "EMP-0001,2026,annual,21,0,0\n"
    return _csv_response(header + sample, "leave-balance-template.csv")
