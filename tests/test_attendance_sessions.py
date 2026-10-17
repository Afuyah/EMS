"""Split shifts: a day can hold several attendance sessions.

A day used to be one clock-in/clock-out pair, and every later punch was
discarded. That silently lost the afternoon of anyone who went out mid-day:

    07:00 in, 11:00 out, 15:00 in, 17:00 out

was recorded as four hours and the second stint vanished. These tests pin the
behaviour that replaced it, including the cases where a punch must still be
ignored.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.core.database import db
from app.features.attendance.models import AttendanceDaily
from app.features.attendance.service import AttendanceService
from app.features.biometrics.constants import PunchType

WORK_DATE = date(2026, 6, 1)  # a Monday
TZ = "Africa/Nairobi"


@pytest.fixture()
def attendance(clean_db, employee):
    """The service plus an employee, with the work date cleared first."""
    AttendanceDaily.query.filter(
        AttendanceDaily.employee_id == employee.id,
        AttendanceDaily.work_date == WORK_DATE,
    ).delete()
    db.session.commit()
    return AttendanceService(), employee


def _row(employee):
    return AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=WORK_DATE
    ).first()


def _punch(service, employee, hour, minute=0, punch_type=PunchType.AUTO):
    service.apply_punch(
        employee_id=employee.id,
        department_id=employee.department_id,
        tz_name=TZ,
        punch_time=datetime(
            WORK_DATE.year, WORK_DATE.month, WORK_DATE.day,
            hour, minute, tzinfo=timezone.utc,
        ),
        punch_type=punch_type,
    )
    db.session.commit()
    return _row(employee)


class TestSplitShift:
    def test_the_morning_stint_is_recorded(self, attendance):
        service, employee = attendance
        row = _punch(service, employee, 6)  # 09:00 local
        assert row is not None
        assert len(row.sessions) == 1
        assert row.sessions[0]["out"] is None, "the session should still be open"
        assert row.clock_out is None

    def test_clocking_out_closes_the_stint(self, attendance):
        service, employee = attendance
        _punch(service, employee, 6)
        row = _punch(service, employee, 10)  # 13:00 local
        assert len(row.sessions) == 1
        assert row.sessions[0]["out"] is not None
        assert row.worked_minutes == 4 * 60

    def test_returning_opens_a_second_stint(self, attendance):
        service, employee = attendance
        _punch(service, employee, 6)
        _punch(service, employee, 10)
        row = _punch(service, employee, 14)  # 17:00 local, a 4h gap

        assert len(row.sessions) == 2, "the afternoon must not be discarded"
        assert row.sessions[1]["out"] is None
        # The first stint's hours survive.
        assert row.worked_minutes == 4 * 60
        assert row.clock_out is None, "still in, so there is no final departure"

    def test_both_stints_are_credited_at_the_end(self, attendance):
        service, employee = attendance
        _punch(service, employee, 6)
        _punch(service, employee, 10)
        _punch(service, employee, 14)
        row = _punch(service, employee, 16)  # 19:00 local

        assert len(row.sessions) == 2
        assert all(session["out"] for session in row.sessions)
        assert row.worked_minutes == 4 * 60 + 2 * 60

    def test_three_stints_all_count(self, attendance):
        service, employee = attendance
        # 07:00-09:00, 11:00-13:00, 15:00-18:00 local == 04:00-06:00, 08:00-10:00, 12:00-15:00 UTC
        for hour in (4, 6, 8, 10, 12, 15):
            row = _punch(service, employee, hour)
        assert len(row.sessions) == 3
        assert row.worked_minutes == 2 * 60 + 2 * 60 + 3 * 60

    def test_the_day_keeps_first_arrival_and_last_departure(self, attendance):
        service, employee = attendance
        for hour in (6, 10, 14, 16):
            row = _punch(service, employee, hour)
        # Deliberately distinct from each other so the assertion is meaningful.
        assert row.clock_in == datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)
        assert row.clock_out == datetime(2026, 6, 1, 16, 0, tzinfo=timezone.utc)


class TestPunchesThatMustBeIgnored:
    def test_a_second_scan_at_arrival_adds_nothing(self, attendance):
        service, employee = attendance
        _punch(service, employee, 6)
        row = _punch(service, employee, 6)
        assert len(row.sessions) == 1
        assert row.worked_minutes == 0

    def test_replaying_the_departure_adds_nothing(self, attendance):
        service, employee = attendance
        _punch(service, employee, 6)
        _punch(service, employee, 10)
        row = _punch(service, employee, 10)
        assert len(row.sessions) == 1, "a replayed clock-out is not a new session"
        assert row.worked_minutes == 4 * 60

    def test_a_stray_scan_soon_after_leaving_adds_nothing(self, attendance):
        """Too soon to be a return to work, so no session is invented."""
        service, employee = attendance
        _punch(service, employee, 6)
        _punch(service, employee, 10)
        row = _punch(service, employee, 10, minute=30)
        assert len(row.sessions) == 1
        assert row.worked_minutes == 4 * 60

    def test_a_punch_before_the_clock_in_is_ignored(self, attendance):
        service, employee = attendance
        _punch(service, employee, 9)
        row = _punch(service, employee, 5)
        assert len(row.sessions) == 1
        assert row.clock_in == datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


class TestLegacyRows:
    def test_a_row_without_a_session_list_is_rebuilt(self, attendance):
        """Rows written before sessions existed must keep working."""
        service, employee = attendance
        _punch(service, employee, 6)
        row = _row(employee)
        sessions_before = row.sessions
        row.sessions = None
        db.session.commit()

        row = _punch(service, employee, 10)
        assert row.sessions is not None
        assert len(row.sessions) == 1
        assert row.worked_minutes == 4 * 60
        assert sessions_before is not None

    def test_a_corrupt_session_list_does_not_break_a_punch(self, attendance):
        """Attendance must never fail to record because one row is malformed."""
        service, employee = attendance
        _punch(service, employee, 6)
        row = _row(employee)
        row.sessions = ["not-a-dict", {"in": "nonsense"}, 42]
        db.session.commit()

        row = _punch(service, employee, 10)
        assert row.clock_out is not None


class TestStatusWithSessions:
    def test_an_early_arrival_is_not_late(self, attendance):
        service, employee = attendance
        # 08:55 local == 05:55 UTC
        row = _punch(service, employee, 5, minute=55)
        assert row.late_minutes == 0
        assert "late" not in row.status.lower()

    def test_lateness_comes_from_the_first_arrival_not_the_return(self, attendance):
        """Coming back in the afternoon does not make you late."""
        service, employee = attendance
        _punch(service, employee, 6)  # on time
        _punch(service, employee, 10)
        row = _punch(service, employee, 14)  # a late return
        assert row.late_minutes == 0, "already arrived on time this morning"
