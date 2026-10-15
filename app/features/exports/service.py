"""CSV export and bulk balance import.

Writing CSV is the only thing this feature does; it reads through the reports
and leave services rather than issuing its own SQL.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.features.leave.constants import BALANCE_IMPORT_COLUMNS, LeaveType
from app.features.reports.service import ReportsService

logger = get_logger(__name__)

#: Columns emitted per export, in order.
ATTENDANCE_DEPARTMENT_COLUMNS = (
    "department_code",
    "department_name",
    "employees",
    "records",
    "late_days",
    "absent_days",
    "on_leave_days",
    "worked_minutes",
    "avg_worked_minutes",
    "attendance_rate",
)

HEADCOUNT_COLUMNS = ("work_date", "headcount", "clocked_in", "on_leave")

LEAVE_UTILISATION_COLUMNS = (
    "leave_type",
    "employees",
    "entitled",
    "used",
    "pending",
    "available",
    "utilisation_percent",
)

BALANCE_LEDGER_COLUMNS = (
    "employee_no",
    "first_name",
    "last_name",
    "department_code",
    "leave_type",
    "entitled",
    "used",
    "pending",
    "available",
)

TOP_LATE_COLUMNS = (
    "employee_no",
    "first_name",
    "last_name",
    "department_code",
    "late_days",
    "late_minutes",
    "avg_late_minutes",
)

COLOR_DISTRIBUTION_COLUMNS = (
    "color",
    "label",
    "total",
    "percent",
)


class ExportsService:
    """Builds CSV payloads and ingests balance CSVs."""

    def __init__(self, reports: ReportsService | None = None, leave=None) -> None:
        self.reports = reports or ReportsService()
        self._leave = leave

    @property
    def leave(self):
        """Cross-feature access goes through the leave *service*."""
        if self._leave is None:
            from app.features.leave.service import LeaveService

            self._leave = LeaveService()
        return self._leave

    # ---- CSV rendering ---------------------------------------------------
    @staticmethod
    def write_csv(columns: tuple[str, ...], rows: list[dict]) -> str:
        """Render rows as CSV text with a stable header."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _cell(row.get(column)) for column in columns})
        return buffer.getvalue()

    @staticmethod
    def write_rows_csv(rows: list[dict]) -> str:
        """Render rows using their own keys (header from the first row)."""
        if not rows:
            return ""
        return ExportsService.write_csv(tuple(rows[0].keys()), rows)

    @staticmethod
    def filename(prefix: str, *, start: date | None = None, end: date | None = None) -> str:
        """Deterministic, sortable download name."""
        parts = [prefix]
        if start:
            parts.append(start.isoformat())
        if end:
            parts.append(end.isoformat())
        return "-".join(parts) + ".csv"

    # ---- exports ---------------------------------------------------------
    def attendance_department(
        self, *, department_id: int, start: date, end: date
    ) -> tuple[str, str]:
        report = self.reports.department_attendance(
            department_id=department_id, start=start, end=end
        )
        payload = self.write_csv(ATTENDANCE_DEPARTMENT_COLUMNS, report["rows"])
        return payload, self.filename(
            f"attendance-dept-{department_id}", start=start, end=end
        )

    def attendance_summary_all(self, *, start: date, end: date) -> tuple[str, str]:
        report = self.reports.attendance_summary(start=start, end=end)
        payload = self.write_csv(ATTENDANCE_DEPARTMENT_COLUMNS, report["rows"])
        return payload, self.filename("attendance-summary", start=start, end=end)

    def headcount(self, *, start: date, end: date) -> tuple[str, str]:
        rows = self.reports.headcount(start=start, end=end)
        return self.write_csv(HEADCOUNT_COLUMNS, rows), self.filename(
            "headcount", start=start, end=end
        )

    def top_late(
        self, *, start: date, end: date, department_id: int | None = None, limit: int = 50
    ) -> tuple[str, str]:
        rows = self.reports.top_late(
            start=start, end=end, department_id=department_id, limit=limit
        )
        return self.write_csv(TOP_LATE_COLUMNS, rows), self.filename(
            "top-late", start=start, end=end
        )

    def leave_utilisation(self, *, year: int, department_id: int | None = None) -> tuple[str, str]:
        report = self.reports.leave_utilisation(year=year, department_id=department_id)
        payload = self.write_csv(LEAVE_UTILISATION_COLUMNS, report["rows"])
        return payload, f"leave-utilisation-{year}.csv"

    def attendance_colors(self, *, department_id: int | None = None) -> tuple[str, str]:
        """Attendance-health distribution CSV."""
        report = self.reports.attendance_colors(department_id=department_id)
        payload = self.write_csv(
            COLOR_DISTRIBUTION_COLUMNS, report["distribution"]
        )
        return payload, "attendance-health.csv"

    def balance_ledger(self, *, department_id: int, year: int) -> tuple[str, str]:
        report = self.reports.balance_ledger(department_id=department_id, year=year)
        payload = self.write_csv(BALANCE_LEDGER_COLUMNS, report["rows"])
        return payload, f"leave-balances-dept-{department_id}-{year}.csv"

    # ---- bulk import -----------------------------------------------------
    @staticmethod
    def parse_balance_csv(text: str) -> tuple[list[dict], list[str]]:
        """Parse and validate a balance CSV.

        Returns ``(valid_rows, errors)``.  Validation is strict per row so a
        partially bad file still imports its good rows.
        """
        if not text or not text.strip():
            raise ValidationError("The uploaded CSV is empty.")

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValidationError("The uploaded CSV has no header row.")

        header = {(name or "").strip().lower() for name in reader.fieldnames}
        missing = {"employee_no", "year", "leave_type"} - header
        if missing:
            raise ValidationError(
                f"CSV is missing required column(s): {', '.join(sorted(missing))}."
            )

        valid: list[dict] = []
        errors: list[str] = []

        for index, raw in enumerate(reader, start=2):
            row = {(key or "").strip().lower(): (value or "").strip()
                   for key, value in raw.items() if key}
            employee_no = row.get("employee_no", "")
            if not employee_no:
                errors.append(f"Row {index}: employee_no is required.")
                continue

            try:
                year = int(row.get("year") or 0)
            except ValueError:
                errors.append(f"Row {index}: year {row.get('year')!r} is not a number.")
                continue
            if not (2000 <= year <= 2100):
                errors.append(f"Row {index}: year {year} is out of range.")
                continue

            leave_type = row.get("leave_type", "").lower()
            if leave_type not in LeaveType.values():
                errors.append(
                    f"Row {index}: leave_type {leave_type!r} is not one of "
                    f"{', '.join(LeaveType.values())}."
                )
                continue

            counts: dict[str, int | None] = {}
            bad_count = False
            for column in ("entitled", "used", "pending"):
                raw_value = row.get(column)
                if raw_value in (None, ""):
                    counts[column] = None
                    continue
                try:
                    parsed = int(raw_value)
                except ValueError:
                    errors.append(
                        f"Row {index}: {column} {raw_value!r} is not a number."
                    )
                    bad_count = True
                    break
                if parsed < 0:
                    errors.append(f"Row {index}: {column} cannot be negative.")
                    bad_count = True
                    break
                counts[column] = parsed
            if bad_count:
                continue

            valid.append(
                {
                    "employee_no": employee_no,
                    "year": year,
                    "leave_type": leave_type,
                    "entitled": counts["entitled"],
                    "used": counts["used"],
                    "pending": counts["pending"],
                }
            )

        return valid, errors

    def import_balances(self, text: str, *, overwrite: bool = True) -> dict:
        """Apply a parsed balance CSV.

        Flushes only; the controller commits.  Unknown employee numbers are
        reported rather than silently ignored.
        """
        from app.features.employees.service import EmployeeService

        employees = EmployeeService()
        rows, errors = self.parse_balance_csv(text)

        imported = 0
        skipped = 0

        for row in rows:
            employee = employees.employees.get_by_employee_no(row["employee_no"])
            if employee is None:
                errors.append(f"Unknown employee number {row['employee_no']!r}.")
                skipped += 1
                continue

            existing = self.leave.get_balance(
                employee_id=employee.id, year=row["year"], leave_type=row["leave_type"]
            )
            if existing is not None and not overwrite:
                skipped += 1
                continue

            self.leave.adjust_balance(
                employee_id=employee.id,
                year=row["year"],
                leave_type=row["leave_type"],
                entitled=row["entitled"],
                used=row["used"],
                pending=row["pending"],
            )
            imported += 1

        logger.info(
            "balance import rows=%s imported=%s skipped=%s errors=%s",
            len(rows), imported, skipped, len(errors),
        )
        return {
            "imported": imported,
            "skipped": skipped,
            "errors": errors[:50],
            "error_count": len(errors),
            "columns": list(BALANCE_IMPORT_COLUMNS),
        }


def _cell(value) -> str:
    """CSV-safe cell: dates ISO-formatted, ``None`` blank."""
    if value is None:
        return ""
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
