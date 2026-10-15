"""Reports service.

Read-only by design: it never flushes or commits, and every method resolves to
a single repository query.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.core.exceptions import ValidationError
from app.features.employees.constants import EmployeeColor
from app.features.reports.repository import ReportsRepository


class ReportsService:
    """Aggregations for the reports pages and CSV exports."""

    def __init__(self, reports: ReportsRepository | None = None) -> None:
        self.reports = reports or ReportsRepository()

    # ---- overview --------------------------------------------------------
    def overview(self) -> dict:
        return self.reports.workforce_overview()

    # ---- attendance ------------------------------------------------------
    def attendance_colors(self, *, department_id: int | None = None) -> dict:
        """Colour distribution, zero-filled so charts never gap."""
        rows = self.reports.color_distribution(department_id=department_id)
        counts = {colour.value: 0 for colour in EmployeeColor}
        for row in rows:
            counts[row["color"]] = int(row["total"] or 0)
        total = sum(counts.values())
        return {
            "total": total,
            "distribution": [
                {
                    "color": colour.value,
                    "label": colour.label,
                    "total": counts[colour.value],
                    "percent": round((counts[colour.value] / total) * 100) if total else 0,
                    "tailwind": colour.tailwind,
                }
                for colour in EmployeeColor
            ],
        }

    def attendance_summary(
        self, *, start: date, end: date, department_id: int | None = None
    ) -> dict:
        rows = self.reports.attendance_summary(
            start=start, end=end, department_id=department_id
        )
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "rows": rows,
            "totals": {
                "employees": sum(row["employees"] for row in rows),
                "records": sum(row["records"] for row in rows),
                "late_days": sum(row["late_days"] for row in rows),
                "absent_days": sum(row["absent_days"] for row in rows),
                "on_leave_days": sum(row["on_leave_days"] for row in rows),
                "worked_minutes": sum(row["worked_minutes"] for row in rows),
            },
        }

    def department_attendance(
        self, *, department_id: int, start: date, end: date
    ) -> dict:
        return self.attendance_summary(
            start=start, end=end, department_id=department_id
        )

    def headcount(self, *, start: date, end: date) -> list[dict]:
        rows = self.reports.headcount_by_day(start=start, end=end)
        for row in rows:
            row["work_date"] = _iso(row["work_date"])
            row["headcount"] = int(row["headcount"] or 0)
            row["clocked_in"] = int(row["clocked_in"] or 0)
            row["on_leave"] = int(row["on_leave"] or 0)
        return rows

    def daily_headcount(
        self, *, start: date | None = None, days: int = 30
    ) -> list[dict]:
        """Convenience window ending today."""
        end = start or date.today()
        return self.headcount(start=end - timedelta(days=days - 1), end=end)

    def top_late(
        self,
        *,
        start: date,
        end: date,
        department_id: int | None = None,
        limit: int = 10,
    ) -> list[dict]:
        return self.reports.top_late_employees(
            start=start, end=end, department_id=department_id, limit=limit
        )

    # ---- leave -----------------------------------------------------------
    def leave_utilisation(self, *, year: int, department_id: int | None = None) -> dict:
        rows = self.reports.leave_utilisation(year=year, department_id=department_id)
        return {
            "year": year,
            "rows": rows,
            "totals": {
                "entitled": sum(row["entitled"] for row in rows),
                "used": sum(row["used"] for row in rows),
                "pending": sum(row["pending"] for row in rows),
                "available": sum(row["available"] for row in rows),
            },
        }

    def leave_calendar(self, *, department_id: int, start: date, end: date) -> dict:
        rows = self.reports.department_leave_calendar(
            department_id=department_id, start=start, end=end
        )
        return {
            "department_id": department_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "entries": rows,
            "days": [
                {
                    "date": day.isoformat(),
                    "on_leave": sum(
                        1
                        for row in rows
                        if row["start_date"] <= day <= row["end_date"]
                    ),
                }
                for day in _date_span(start, end)
            ],
        }

    def balance_ledger(self, *, department_id: int, year: int) -> dict:
        rows = self.reports.balance_ledger(department_id=department_id, year=year)
        return {"department_id": department_id, "year": year, "rows": rows}

    # ---- validation helpers ---------------------------------------------
    @staticmethod
    def resolve_range(
        start: date | None, end: date | None, *, default_days: int = 30
    ) -> tuple[date, date]:
        """Normalise a date range, defaulting to the last ``default_days``."""
        today = date.today()
        resolved_end = end or today
        resolved_start = start or (resolved_end - timedelta(days=default_days - 1))
        if resolved_end < resolved_start:
            raise ValidationError("The end date cannot be earlier than the start date.")
        return resolved_start, resolved_end


def _iso(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _date_span(start: date, end: date) -> list[date]:
    from app.core.time import each_day

    return list(each_day(start, end))
