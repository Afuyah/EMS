"""Leave controller: orchestration plus the transaction boundary.

Commit happens here, never inside the service.  Responses are shaped dicts that
the blueprint serialises.
"""

from __future__ import annotations

from app.core.database import db
from app.core.exceptions import AuthorizationError
from app.core.logging import get_logger
from app.core.responses import created, ok
from app.features.leave.schemas import (
    leave_balance_adjust_schema,
    leave_balances_schema,
    leave_cancel_schema,
    leave_decide_schema,
    leave_filter_schema,
    leave_history_schema,
    leave_request_create_schema,
    leave_request_schema,
    leave_requests_schema,
    leave_respond_schema,
    load_or_raise,
)
from app.features.leave.service import LeaveService

logger = get_logger(__name__)


class LeaveController:
    """Use-case orchestration for the leave lifecycle."""

    def __init__(self, service: LeaveService | None = None) -> None:
        self.service = service or LeaveService()

    # ---- request ---------------------------------------------------------
    def request_leave(self, payload: dict, *, actor_employee_id: int | None = None):
        """Create a leave request.

        ``actor_employee_id`` is the caller's own employee record; when an
        HR/Admin passes another ``employee_id`` that value is used instead.
        """
        data = load_or_raise(leave_request_create_schema, payload)

        employee_id = data.get("employee_id") or actor_employee_id
        if employee_id is None:
            raise AuthorizationError(
                "Your account is not linked to an employee record."
            )

        try:
            request = self.service.request_leave(
                employee_id=employee_id,
                leave_type=data["leave_type"],
                start_date=data.get("start_date"),
                end_date=data.get("end_date"),
                days=data.get("days"),
                reason=data.get("reason"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        return created(self._serialise(request))

    # ---- employee response ----------------------------------------------
    def respond(self, request_id: int, payload: dict, *, actor_user_id: int | None):
        data = load_or_raise(leave_respond_schema, payload)
        try:
            request = self.service.respond(
                request_id=request_id,
                action=data["action"],
                actor_user_id=actor_user_id,
                note=data.get("note"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(self._serialise(request))

    # ---- admin decision --------------------------------------------------
    def decide(self, request_id: int, payload: dict, *, actor_user_id: int):
        data = load_or_raise(leave_decide_schema, payload)
        try:
            request = self.service.decide(
                request_id=request_id,
                decision=data["decision"],
                actor_user_id=actor_user_id,
                note=data.get("note"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(self._serialise(request))

    # ---- cancel ----------------------------------------------------------
    def cancel(self, request_id: int, payload: dict, *, actor_user_id: int | None):
        data = load_or_raise(leave_cancel_schema, payload or {})
        try:
            request = self.service.cancel(
                request_id=request_id,
                actor_user_id=actor_user_id,
                note=data.get("note"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(self._serialise(request))

    # ---- reads -----------------------------------------------------------
    def get_request(self, request_id: int):
        request = self.service.get_request(request_id)
        return ok(self._serialise(request, with_history=True))

    def list_requests(self, query: dict):
        data = load_or_raise(leave_filter_schema, query)
        page = max(1, int(data.get("page") or 1))
        per_page = max(1, min(int(data.get("per_page") or 25), 200))

        result = self.service.list_requests_paginated(
            page=page,
            per_page=per_page,
            employee_id=data.get("employee_id"),
            department_id=data.get("department_id"),
            leave_type=data.get("leave_type") or None,
            status=data.get("status") or None,
            start=data.get("start"),
            end=data.get("end"),
        )
        return ok(
            leave_requests_schema.dump(result.items), meta=result.info.to_dict()
        )

    def my_requests(self, employee_id: int, query: dict):
        data = load_or_raise(leave_filter_schema, query)
        records = self.service.my_requests(
            employee_id,
            statuses=[data["status"]] if data.get("status") else None,
        )
        return ok(leave_requests_schema.dump(records))

    def my_balances(self, employee_id: int, year: int):
        balances = self.service.balances_for_employee(employee_id, year=year)
        return ok(leave_balances_schema.dump(balances))

    def department_ledger(self, department_id: int, year: int):
        return ok(self.service.department_ledger(department_id, year=year))

    def utilisation(self, year: int, department_id: int | None = None):
        return ok(self.service.utilisation(year=year, department_id=department_id))

    def calendar(self, department_id: int, start, end):
        return ok(self.service.calendar(department_id=department_id, start=start, end=end))

    # ---- balances --------------------------------------------------------
    def adjust_balance(self, payload: dict):
        data = load_or_raise(leave_balance_adjust_schema, payload)
        try:
            balance = self.service.adjust_balance(
                employee_id=data["employee_id"],
                year=data["year"],
                leave_type=data["leave_type"],
                entitled=data.get("entitled"),
                used=data.get("used"),
                pending=data.get("pending"),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        from app.features.leave.schemas import leave_balance_schema

        return ok(leave_balance_schema.dump(balance))

    def allocate_year(self, year: int, *, overwrite: bool = False):
        try:
            summary = self.service.allocate_year(year=year, overwrite=overwrite)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return ok(summary)

    # ---- serialisation ---------------------------------------------------
    @staticmethod
    def _serialise(request, *, with_history: bool = False) -> dict:
        payload = leave_request_schema.dump(request)
        if with_history:
            payload["history"] = leave_history_schema.dump(request.history)
        return payload
