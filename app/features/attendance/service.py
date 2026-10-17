"""Attendance service: shift-policy resolution and punch application.

Stateless and Flask-free.  Reads the clock through :mod:`app.core.time` so the
behaviour is deterministic under test.
"""

from __future__ import annotations

from datetime import date, datetime, time

from app.core.logging import get_logger
from app.core.time import ensure_utc, shift_bounds, today_local, utcnow
from app.features.attendance.constants import (
    DEFAULT_GRACE_MINUTES,
    DEFAULT_SHIFT_END,
    DEFAULT_SHIFT_START,
    DEFAULT_VERY_LATE_AFTER_MINUTES,
    AttendanceStatus,
)
from app.features.attendance.engine import (
    PunchEvent,
    ShiftPolicy,
    classify,
    compute_early_leave_minutes,
    compute_late_minutes,
    compute_worked_minutes,
    parse_sessions,
    recompute,
    resolve_punch,
    should_mark_absent,
)
from app.features.attendance.exceptions import (
    AttendanceNotFoundError,
    EmployeeNotClockableError,
    EmployeeOnLeaveError,
    InvalidCorrectionError,
    PolicyNotFoundError,
)
from app.features.attendance.models import AttendanceDaily, AttendancePolicy
from app.features.attendance.repository import (
    AttendancePolicyRepository,
    AttendanceRepository,
)
from app.features.biometrics.constants import PunchType

logger = get_logger(__name__)


class AttendancePolicyService:
    """Resolves the shift policy that applies to an employee on a given day."""

    def __init__(self, policies: AttendancePolicyRepository | None = None) -> None:
        self.policies = policies or AttendancePolicyRepository()

    def list_policies(self) -> list[AttendancePolicy]:
        return self.policies.all_policies()

    def get_global(self) -> AttendancePolicy | None:
        return self.policies.global_policy()

    def resolve(self, department_id: int | None) -> AttendancePolicy:
        """Department override when present, otherwise the global default."""
        override = self.policies.for_department(department_id)
        if override is not None:
            return override
        fallback = self.policies.global_policy()
        if fallback is not None:
            return fallback
        # No configuration at all: synthesise the documented default so that
        # attendance still resolves instead of failing.
        return AttendancePolicy(
            department_id=None,
            shift_start=DEFAULT_SHIFT_START,
            shift_end=DEFAULT_SHIFT_END,
            grace_minutes=DEFAULT_GRACE_MINUTES,
            very_late_after_minutes=DEFAULT_VERY_LATE_AFTER_MINUTES,
            is_active=True,
        )

    def resolve_shift(self, department_id: int | None) -> ShiftPolicy:
        """The pure-engine view of the resolved policy."""
        policy = self.resolve(department_id)
        return ShiftPolicy(
            shift_start=policy.shift_start,
            shift_end=policy.shift_end,
            grace_minutes=policy.grace_minutes,
            very_late_after_minutes=policy.very_late_after_minutes,
            auto_absent_after_minutes=policy.auto_absent_after_minutes,
        )

    def upsert(
        self,
        *,
        department_id: int | None,
        shift_start: time,
        shift_end: time,
        grace_minutes: int,
        very_late_after_minutes: int | None = None,
        auto_absent_after_minutes: int | None = None,
        is_active: bool = True,
    ) -> AttendancePolicy:
        """Create or update the policy for one scope, validating the tiers."""
        self._validate(
            shift_start=shift_start,
            shift_end=shift_end,
            grace_minutes=grace_minutes,
            very_late_after_minutes=very_late_after_minutes,
            auto_absent_after_minutes=auto_absent_after_minutes,
        )
        return self.policies.upsert(
            department_id=department_id,
            shift_start=shift_start,
            shift_end=shift_end,
            grace_minutes=grace_minutes,
            very_late_after_minutes=(
                DEFAULT_VERY_LATE_AFTER_MINUTES
                if very_late_after_minutes is None
                else very_late_after_minutes
            ),
            auto_absent_after_minutes=auto_absent_after_minutes,
            is_active=is_active,
        )

    @staticmethod
    def _validate(
        *,
        shift_start: time,
        shift_end: time,
        grace_minutes: int,
        very_late_after_minutes: int | None,
        auto_absent_after_minutes: int | None,
    ) -> None:
        """Reject configurations that would make attendance meaningless."""
        if shift_start == shift_end:
            raise InvalidCorrectionError(
                "Shift start and end cannot be the same time."
            )
        if grace_minutes < 0:
            raise InvalidCorrectionError("Grace minutes cannot be negative.")
        if grace_minutes > 240:
            raise InvalidCorrectionError(
                "Grace minutes above 240 would make almost everyone on time."
            )
        if very_late_after_minutes is not None:
            if very_late_after_minutes < 1:
                raise InvalidCorrectionError(
                    "The very-late threshold must be at least 1 minute after the "
                    "grace period, otherwise every late arrival is 'very late'."
                )
            if very_late_after_minutes > 720:
                raise InvalidCorrectionError(
                    "The very-late threshold cannot exceed 720 minutes."
                )
        if auto_absent_after_minutes is not None and auto_absent_after_minutes < 1:
            raise InvalidCorrectionError(
                "The auto-absent cut-off must be at least 1 minute after shift start."
            )

    def set_active(self, policy_id: int, is_active: bool) -> AttendancePolicy:
        """Enable or disable a policy without losing its configuration."""
        policy = self.get_policy_or_raise(policy_id)
        policy.is_active = bool(is_active)
        return self.policies.update(policy)

    def delete(self, policy_id: int) -> None:
        """Delete a policy.  The global default cannot be removed."""
        policy = self.get_policy_or_raise(policy_id)
        if policy.is_global:
            raise InvalidCorrectionError(
                "The global default policy cannot be deleted; edit it instead."
            )
        self.policies.delete(policy)

    def get_policy_or_raise(self, policy_id: int) -> AttendancePolicy:
        policy = self.policies.get(policy_id)
        if policy is None:
            raise PolicyNotFoundError(f"Attendance policy {policy_id} was not found.")
        return policy

    def resolved_preview(self, department_id: int | None) -> dict:
        """Explain what a department actually gets, and from where.

        Makes the override chain visible in the UI instead of a silent fallback.
        """
        override = self.policies.for_department(department_id)
        source = "department" if override is not None else "global"
        policy = self.resolve(department_id)
        shift = self.resolve_shift(department_id)
        return {
            "policy": policy,
            "source": source,
            "is_default": override is None and self.policies.global_policy() is None,
            "shift_start": policy.shift_start.strftime("%H:%M"),
            "shift_end": policy.shift_end.strftime("%H:%M"),
            "grace_minutes": policy.grace_minutes,
            "very_late_after_minutes": policy.very_late_after_minutes,
            "very_late_cutoff": policy.very_late_cutoff_label,
            "auto_absent_after_minutes": policy.auto_absent_after_minutes,
            "auto_absent_cutoff": policy.auto_absent_label,
            "nominal_minutes": shift.nominal_minutes,
        }


class AttendanceService:
    """Use-cases for daily attendance."""

    def __init__(
        self,
        attendance: AttendanceRepository | None = None,
        policies: AttendancePolicyService | None = None,
    ) -> None:
        self.attendance = attendance or AttendanceRepository()
        self.policies = policies or AttendancePolicyService()

    # ---- reads -----------------------------------------------------------
    def get_or_create_day(self, employee_id: int, work_date: date) -> AttendanceDaily:
        return self.attendance.get_or_create_for_day(employee_id, work_date)

    def get_day(self, employee_id: int, work_date: date) -> AttendanceDaily | None:
        return self.attendance.get_for_day(employee_id, work_date)

    def get_day_or_raise(self, attendance_id: int) -> AttendanceDaily:
        record = self.attendance.get(attendance_id)
        if record is None:
            raise AttendanceNotFoundError(f"Attendance {attendance_id} was not found.")
        return record

    def history(
        self,
        employee_id: int,
        *,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> list[AttendanceDaily]:
        return list(
            self.attendance.list_for_employee(
                employee_id, start=start, end=end, limit=limit
            )
        )

    def board_rows(
        self,
        work_date: date,
        *,
        department_id: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> list[dict]:
        """Enriched rows for the attendance board (plain dicts by design)."""
        return self.attendance.list_for_day_enriched(
            work_date, department_id=department_id, status=status, search=search
        )

    def employee_stats(
        self, employee_ids: list[int], *, start: date | None = None, end: date | None = None
    ) -> dict[int, dict]:
        return self.attendance.employee_stats(employee_ids, start=start, end=end)

    def status_counts(self, *, start: date, end: date, department_id=None, employee_ids=None):
        return self.attendance.count_by_status(
            start=start, end=end, department_id=department_id, employee_ids=employee_ids
        )

    def trend(self, *, start: date, end: date, department_id=None, employee_ids=None):
        return self.attendance.daily_trend(
            start=start, end=end, department_id=department_id, employee_ids=employee_ids
        )

    def recent_punches(self, *, limit: int = 8) -> list[dict]:
        return self.attendance.recent_punches(limit=limit)

    def employees_on_leave_on(self, work_date: date) -> list[int]:
        return self.attendance.employees_on_leave_on(work_date)

    def longest_late_streak(self, employee_id: int) -> int:
        """Longest run of consecutive late days, counted backwards from today."""
        records = self.attendance.late_streaks(employee_id)
        streak = 0
        for record in records:
            if record.status == AttendanceStatus.LATE.value:
                streak += 1
            elif record.status == AttendanceStatus.ABSENT.value:
                # An absence does not break the streak, but is not counted.
                continue
            else:
                break
        return streak

    def on_time_percent(self, employee_id: int, *, start: date, end: date) -> int:
        """Share of recorded working days that were not late."""
        records = self.attendance.list_for_employee(employee_id, start=start, end=end)
        counted = [
            record
            for record in records
            if record.status
            in {
                AttendanceStatus.PRESENT.value,
                AttendanceStatus.LATE.value,
                AttendanceStatus.HALF_DAY.value,
            }
        ]
        if not counted:
            return 0
        on_time = sum(1 for record in counted if record.late_minutes <= 0)
        return round((on_time / len(counted)) * 100)

    def average_worked_minutes(self, employee_id: int, *, start: date, end: date) -> int:
        records = self.attendance.list_for_employee(employee_id, start=start, end=end)
        worked = [record.worked_minutes for record in records if record.worked_minutes > 0]
        return round(sum(worked) / len(worked)) if worked else 0

    # ---- writes ----------------------------------------------------------
    # ---- integrity guard -------------------------------------------------
    def _assert_clockable(
        self, *, employee_id: int, work_date: date, punch_time: datetime | None = None
    ) -> None:
        """Refuse attendance for a suspended employee or one on approved leave.

        Cross-feature reads go through the other feature's *service*, never its
        repository, so the rule stays owned by the leave feature.
        """
        from app.features.employees.repository import EmployeeRepository

        employee = EmployeeRepository().get(employee_id)
        if employee is None:
            raise EmployeeNotClockableError(
                f"Employee {employee_id} was not found."
            )

        if not employee.is_active:
            raise EmployeeNotClockableError(
                f"{employee.full_name} is suspended, so attendance cannot be recorded."
            )

        # Only a live (approved) leave blocks; pending or suggested does not.
        from app.features.leave.repository import LeaveRequestRepository

        covering = LeaveRequestRepository().active_on(work_date)
        on_leave = next(
            (request for request in covering if request.employee_id == employee_id),
            None,
        )
        if on_leave is not None:
            raise EmployeeOnLeaveError(
                f"{employee.full_name} is on approved "
                f"{on_leave.leave_type} leave from {on_leave.start_date} to "
                f"{on_leave.end_date}, so attendance cannot be recorded for "
                f"{work_date}."
            )

    def is_clockable(self, employee_id: int, work_date: date) -> tuple[bool, str]:
        """Non-raising form of :meth:`_assert_clockable` for UI use."""
        try:
            self._assert_clockable(employee_id=employee_id, work_date=work_date)
        except EmployeeNotClockableError as error:
            return False, error.message
        return True, ""

    def apply_punch(
        self,
        *,
        employee_id: int,
        department_id: int | None,
        tz_name: str,
        punch_time: datetime,
        punch_type: PunchType | str = PunchType.AUTO,
    ) -> AttendanceDaily:
        """Resolve one punch into the daily attendance row for that employee.

        The punch is attributed to the local work date, so a punch at 00:30 in a
        +03:00 timezone belongs to the *previous* Nairobi day only if the device
        clock says so — the employee's own ``tz`` decides.
        """
        event = PunchEvent(
            punch_time=ensure_utc(punch_time), punch_type=PunchType.coerce(punch_type)
        )
        work_date = _work_date_for(event.punch_time, tz_name)
        policy = self.policies.resolve_shift(department_id)

        # Integrity guard: a suspended employee and an employee on approved
        # leave are both refused before any row is touched, so the day stays
        # ON_LEAVE instead of silently becoming PRESENT.
        self._assert_clockable(
            employee_id=employee_id, work_date=work_date, punch_time=event.punch_time
        )

        record = self.attendance.get_or_create_for_day(employee_id, work_date)
        outcome = resolve_punch(
            punch=event,
            work_date=work_date,
            tz_name=tz_name,
            policy=policy,
            existing_sessions=parse_sessions(record.sessions),
            existing_clock_in=record.clock_in,
            existing_clock_out=record.clock_out,
        )

        if not outcome.changed:
            logger.debug(
                "punch ignored employee=%s date=%s reason=%s",
                employee_id,
                work_date,
                outcome.reason,
            )
            return record

        record.clock_in = outcome.clock_in
        record.clock_out = outcome.clock_out
        # The full session list is what makes a split shift countable; the scalar
        # columns above remain the day's first arrival and latest departure.
        record.sessions = [session.as_dict() for session in outcome.sessions]
        if outcome.status is not None:
            record.status = outcome.status.value
        record.late_minutes = outcome.late_minutes
        record.early_leave_minutes = outcome.early_leave_minutes
        record.worked_minutes = outcome.worked_minutes
        self.attendance.update(record)

        logger.info(
            "attendance %s employee=%s date=%s status=%s late=%s worked=%s sessions=%d",
            outcome.action,
            employee_id,
            work_date,
            record.status,
            record.late_minutes,
            record.worked_minutes,
            len(outcome.sessions),
        )
        return record

    def correct_day(
        self,
        attendance_id: int,
        *,
        clock_in: datetime | None = None,
        clock_out: datetime | None = None,
        status: str | None = None,
        notes: str | None = None,
        department_id: int | None = None,
        tz_name: str | None = None,
    ) -> AttendanceDaily:
        """Manual HR correction; derived minutes are recomputed the same way."""
        record = self.get_day_or_raise(attendance_id)

        resolved_tz = tz_name or _employee_tz(record)
        resolved_department = department_id
        if resolved_department is None:
            resolved_department = _employee_department(record)

        target_in = ensure_utc(clock_in) if clock_in is not None else record.clock_in
        target_out = ensure_utc(clock_out) if clock_out is not None else record.clock_out

        if target_in is not None and target_out is not None and target_out <= target_in:
            raise InvalidCorrectionError("Clock-out must be later than clock-in.")

        if status is not None:
            if status not in AttendanceStatus.values():
                raise InvalidCorrectionError(f"Unknown attendance status {status!r}.")
            record.status = status

        if clock_in is not None or clock_out is not None:
            policy = self.policies.resolve_shift(resolved_department)
            outcome = recompute(
                work_date=record.work_date,
                tz_name=resolved_tz,
                policy=policy,
                clock_in=target_in,
                clock_out=target_out,
            )
            record.clock_in = outcome.clock_in
            record.clock_out = outcome.clock_out
            record.late_minutes = outcome.late_minutes
            record.early_leave_minutes = outcome.early_leave_minutes
            record.worked_minutes = outcome.worked_minutes
            # An explicit status supplied by HR wins over the computed one.
            if status is None and outcome.status is not None:
                record.status = outcome.status.value

        if notes is not None:
            record.notes = notes

        return self.attendance.update(record)

    def correct_employee_day(
        self,
        *,
        employee_id: int,
        work_date: date,
        clock_in: datetime | None = None,
        clock_out: datetime | None = None,
        status: str | None = None,
        notes: str | None = None,
        department_id: int | None = None,
        tz_name: str | None = None,
    ) -> AttendanceDaily:
        """Manual HR correction addressed by employee and date.

        Unlike :meth:`correct_day` this does not require a row to already exist,
        because the days that most need correcting are the ones with no punch at
        all: a missed clock-in, a device that was down, a forgotten clock-out.

        The record is created on demand, then the same recompute path is used so
        a manual correction produces identical numbers to a real punch.
        """
        from app.features.employees.repository import EmployeeRepository

        employee = EmployeeRepository().get(employee_id)
        if employee is None:
            raise AttendanceNotFoundError(
                f"Employee {employee_id} was not found."
            )

        resolved_department = (
            department_id if department_id is not None else employee.department_id
        )
        resolved_tz = tz_name or getattr(employee, "tz", None)

        # Create the day if it does not exist yet.
        record = self.attendance.get_or_create_for_day(employee_id, work_date)

        # Leave and suspension still apply: an HR correction must not quietly
        # overwrite an approved leave day.
        if status not in {AttendanceStatus.ON_LEAVE.value, AttendanceStatus.HOLIDAY.value}:
            self._assert_clockable(employee_id=employee_id, work_date=work_date)

        return self.correct_day(
            record.id,
            clock_in=clock_in,
            clock_out=clock_out,
            status=status,
            notes=notes,
            department_id=resolved_department,
            tz_name=resolved_tz,
        )

    def correctable_employees(
        self, work_date: date, *, department_id: int | None = None
    ) -> list[dict]:
        """Everyone who could be corrected on ``work_date``.

        Includes employees with no attendance row for the day, so a missing
        punch can be recorded rather than only an existing one edited.
        """
        return self.attendance.correctable_employees(
            work_date, department_id=department_id
        )

    def mark_absence(self, employee_id: int, work_date: date, *, note: str | None = None) -> AttendanceDaily:
        """Record an absence for a day with no punches (nightly job)."""
        record = self.attendance.get_or_create_for_day(employee_id, work_date)
        record.status = AttendanceStatus.ABSENT.value
        if note:
            record.notes = note
        return self.attendance.update(record)

    def mark_due_absences(
        self,
        *,
        work_date: date,
        now: datetime | None = None,
        department_id: int | None = None,
        exclude_employee_ids: set[int] | None = None,
    ) -> dict:
        """Mark employees whose absence cut-off has passed with no punch.

        Only policies that configure ``auto_absent_after_minutes`` take part; a
        policy without one still relies on the nightly job.  Returns a summary so
        the caller can log or display what happened.
        """
        from app.features.employees.models import Employee

        moment = ensure_utc(now) if now is not None else utcnow()
        excluded = exclude_employee_ids or set()

        query = self.attendance.list_for_day(work_date, department_id=department_id)
        recorded = {record.employee_id: record for record in query}

        employee_ids = (
            _employee_ids_in_department(department_id)
            if department_id is not None
            else _active_employee_ids()
        )

        marked: list[int] = []
        skipped_open: list[int] = []
        skipped_no_cutoff: list[int] = []

        for employee_id in employee_ids:
            if employee_id in excluded:
                continue

            record = recorded.get(employee_id)
            has_clock_in = record is not None and record.clock_in is not None
            if has_clock_in:
                skipped_open.append(employee_id)
                continue

            tz_name = _employee_tz_by_id(employee_id)
            department = _employee_department_by_id(employee_id)
            policy = self.policies.resolve_shift(department)

            if policy.auto_absent_after_minutes is None:
                skipped_no_cutoff.append(employee_id)
                continue

            if should_mark_absent(
                now=moment,
                work_date=work_date,
                policy=policy,
                tz_name=tz_name,
                has_clock_in=has_clock_in,
            ):
                self.mark_absence(
                    employee_id,
                    work_date,
                    note=(
                        "Auto-marked absent: no punch by the configured cut-off "
                        f"({policy.auto_absent_after_minutes} minutes after shift start)."
                    ),
                )
                marked.append(employee_id)

        return {
            "work_date": work_date.isoformat(),
            "checked": len(employee_ids),
            "marked_absent": len(marked),
            "skipped_already_in": len(skipped_open),
            "skipped_no_cutoff": len(skipped_no_cutoff),
            "employee_ids": marked,
        }

    def mark_on_leave(self, employee_id: int, work_date: date) -> AttendanceDaily:
        """Flip a day to ON_LEAVE when an approved leave covers it."""
        record = self.attendance.get_or_create_for_day(employee_id, work_date)
        record.status = AttendanceStatus.ON_LEAVE.value
        record.clock_in = None
        record.clock_out = None
        return self.attendance.update(record)

    def recompute_day(self, attendance_id: int) -> AttendanceDaily:
        """Recalculate derived minutes from the existing punches."""
        record = self.get_day_or_raise(attendance_id)
        policy = self.policies.resolve_shift(_employee_department(record))
        outcome = recompute(
            work_date=record.work_date,
            tz_name=_employee_tz(record),
            policy=policy,
            clock_in=record.clock_in,
            clock_out=record.clock_out,
        )
        record.late_minutes = outcome.late_minutes
        record.early_leave_minutes = outcome.early_leave_minutes
        record.worked_minutes = outcome.worked_minutes
        if outcome.status is not None:
            record.status = outcome.status.value
        return self.attendance.update(record)

    def expected_clock_out(self, work_date: date, policy: ShiftPolicy, tz_name: str) -> datetime:
        """UTC instant at which the shift ends, used by board countdowns."""
        _, end = shift_bounds(work_date, policy.shift_start, policy.shift_end, tz_name)
        return end


# ---------------------------------------------------------------------------
# Attribute access that must not import the employees feature at module scope
# ---------------------------------------------------------------------------
def _work_date_for(punch_time: datetime, tz_name: str) -> date:
    """The employee-local calendar date a punch belongs to."""
    from app.core.time import local_date

    return local_date(punch_time, tz_name)


def _employee_tz(record: AttendanceDaily) -> str:
    from app.features.employees.constants import DEFAULT_TIMEZONE

    employee = getattr(record, "employee", None)
    return getattr(employee, "tz", None) or DEFAULT_TIMEZONE


def _employee_department(record: AttendanceDaily) -> int | None:
    employee = getattr(record, "employee", None)
    return getattr(employee, "department_id", None)


def _active_employee_ids() -> list[int]:
    from app.features.employees.repository import EmployeeRepository

    return EmployeeRepository().active_employee_ids()


def _employee_ids_in_department(department_id: int) -> list[int]:
    from app.features.employees.repository import EmployeeRepository

    return EmployeeRepository().ids_in_department(department_id, active_only=True)


def _employee_tz_by_id(employee_id: int) -> str:
    """Timezone lookup by id, used by the absence sweep."""
    from app.features.employees.constants import DEFAULT_TIMEZONE
    from app.features.employees.repository import EmployeeRepository

    employee = EmployeeRepository().get(employee_id)
    return getattr(employee, "tz", None) or DEFAULT_TIMEZONE


def _employee_department_by_id(employee_id: int) -> int | None:
    from app.features.employees.repository import EmployeeRepository

    employee = EmployeeRepository().get(employee_id)
    return getattr(employee, "department_id", None)
