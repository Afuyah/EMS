"""Attendance HTTP routes for the JSON API."""

from __future__ import annotations

from datetime import date, datetime, time

from flask import Blueprint, request
from flask_jwt_extended import jwt_required

from app.core.exceptions import ValidationError
from app.core.permissions import require_roles
from app.core.responses import ok
from app.core.time import today_local
from app.features.attendance.service import AttendancePolicyService, AttendanceService
from app.features.auth.constants import Role

attendance_api = Blueprint("attendance_api", __name__, url_prefix="/attendance")

_service = AttendanceService()
_policies = AttendancePolicyService()


def _parse_date(value: str | None, *, default: date | None = None) -> date | None:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"Invalid date {value!r}; expected YYYY-MM-DD.") from exc


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(
            f"Invalid timestamp {value!r}; expected ISO-8601."
        ) from exc


def _serialise(record) -> dict:
    return {
        "id": record.id,
        "employee_id": record.employee_id,
        "work_date": record.work_date.isoformat(),
        "clock_in": record.clock_in.isoformat() if record.clock_in else None,
        "clock_out": record.clock_out.isoformat() if record.clock_out else None,
        "status": record.status,
        "late_minutes": record.late_minutes,
        "early_leave_minutes": record.early_leave_minutes,
        "worked_minutes": record.worked_minutes,
        "worked_hours": record.worked_hours,
        "notes": record.notes,
    }


@attendance_api.get("/employees/<int:employee_id>")
@jwt_required()
def employee_attendance(employee_id: int):
    """Attendance history for one employee over an optional date range."""
    start = _parse_date(request.args.get("start"))
    end = _parse_date(request.args.get("end"))
    limit = request.args.get("limit", type=int)

    records = _service.history(employee_id, start=start, end=end, limit=limit)
    return ok([_serialise(record) for record in records])


@attendance_api.get("/board")
@jwt_required()
def board():
    """The attendance board for one day (defaults to today)."""
    work_date = _parse_date(request.args.get("date"), default=today_local(None))
    rows = _service.board_rows(
        work_date,
        department_id=request.args.get("department_id", type=int),
        status=request.args.get("status") or None,
        search=request.args.get("q") or None,
    )
    return ok(
        [
            {
                **{k: v for k, v in row.items() if k not in {"clock_in", "clock_out"}},
                "work_date": work_date.isoformat(),
                "clock_in": row["clock_in"].isoformat() if row.get("clock_in") else None,
                "clock_out": row["clock_out"].isoformat() if row.get("clock_out") else None,
            }
            for row in rows
        ],
        meta={"work_date": work_date.isoformat(), "count": len(rows)},
    )


@attendance_api.get("/policies")
@jwt_required()
def list_policies():
    """All shift policies (global plus per-department overrides)."""
    policies = _policies.list_policies()
    return ok(
        [
            {
                "id": policy.id,
                "department_id": policy.department_id,
                "scope": "global" if policy.is_global else "department",
                "shift_start": policy.shift_start.strftime("%H:%M"),
                "shift_end": policy.shift_end.strftime("%H:%M"),
                "grace_minutes": policy.grace_minutes,
                "is_active": policy.is_active,
            }
            for policy in policies
        ]
    )


@attendance_api.post("/policies")
@jwt_required()
def upsert_policy():
    """Create or update a shift policy (Admin)."""
    require_roles(Role.ADMIN)
    from app.core.database import db

    payload = request.get_json(silent=True) or {}

    def _clock(field: str, fallback: time) -> time:
        raw = payload.get(field)
        if not raw:
            return fallback
        try:
            return time.fromisoformat(str(raw))
        except ValueError as exc:
            raise ValidationError(
                f"Invalid {field} {raw!r}; expected HH:MM."
            ) from exc

    department_id = payload.get("department_id")
    try:
        policy = _policies.upsert(
            department_id=department_id,
            shift_start=_clock("shift_start", time(9, 0)),
            shift_end=_clock("shift_end", time(17, 0)),
            grace_minutes=int(payload.get("grace_minutes") or 0),
            is_active=bool(payload.get("is_active", True)),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return ok(
        {
            "id": policy.id,
            "department_id": policy.department_id,
            "shift_start": policy.shift_start.strftime("%H:%M"),
            "shift_end": policy.shift_end.strftime("%H:%M"),
            "grace_minutes": policy.grace_minutes,
        }
    )


@attendance_api.post("/<int:attendance_id>/correct")
@jwt_required()
def correct(attendance_id: int):
    """Manually correct an attendance row (HR/Admin)."""
    from app.core.database import db

    require_roles(Role.HR, Role.ADMIN)
    payload = request.get_json(silent=True) or {}
    try:
        record = _service.correct_day(
            attendance_id,
            clock_in=_parse_datetime(payload.get("clock_in")),
            clock_out=_parse_datetime(payload.get("clock_out")),
            status=payload.get("status"),
            notes=payload.get("notes"),
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return ok(_serialise(record))
