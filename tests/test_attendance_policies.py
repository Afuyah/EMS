"""Tests for configurable shift/lateness policies and the arrival tiers."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from app.core.database import db
from app.features.attendance.constants import ArrivalTier, AttendanceStatus
from app.features.attendance.engine import (
    ShiftPolicy,
    compute_late_minutes,
    recompute,
    resolve_punch,
    should_mark_absent,
)
from app.features.attendance.exceptions import InvalidCorrectionError
from app.features.attendance.models import AttendancePolicy
from app.features.attendance.service import AttendancePolicyService, AttendanceService
from app.features.biometrics.constants import PunchType

pytestmark = pytest.mark.integration

TZ = "Africa/Nairobi"  # UTC+3, so 09:00 local == 06:00 UTC
WORK_DATE = date(2026, 3, 2)  # Monday


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 3, 2, hour, minute, tzinfo=timezone.utc)


class TestPolicyValidation:
    def test_identical_shift_bounds_are_rejected(self, clean_db, app_context):
        with pytest.raises(InvalidCorrectionError):
            AttendancePolicyService().upsert(
                department_id=None, shift_start=time(9, 0), shift_end=time(9, 0),
                grace_minutes=10,
            )

    def test_negative_grace_is_rejected(self, clean_db, app_context):
        with pytest.raises(InvalidCorrectionError):
            AttendancePolicyService().upsert(
                department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
                grace_minutes=-1,
            )

    def test_excessive_grace_is_rejected(self, clean_db, app_context):
        with pytest.raises(InvalidCorrectionError):
            AttendancePolicyService().upsert(
                department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
                grace_minutes=300,
            )

    def test_zero_very_late_threshold_is_rejected(self, clean_db, app_context):
        """Zero would make every late arrival 'very late'."""
        with pytest.raises(InvalidCorrectionError):
            AttendancePolicyService().upsert(
                department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
                grace_minutes=10, very_late_after_minutes=0,
            )

    def test_zero_absence_cutoff_is_rejected(self, clean_db, app_context):
        with pytest.raises(InvalidCorrectionError):
            AttendancePolicyService().upsert(
                department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
                grace_minutes=10, auto_absent_after_minutes=0,
            )

    def test_valid_policy_is_stored_with_tiers(self, clean_db, app_context):
        policy = AttendancePolicyService().upsert(
            department_id=None, shift_start=time(8, 30), shift_end=time(17, 30),
            grace_minutes=15, very_late_after_minutes=45,
            auto_absent_after_minutes=240,
        )
        db.session.commit()

        assert policy.shift_start == time(8, 30)
        assert policy.grace_minutes == 15
        assert policy.very_late_after_minutes == 45
        assert policy.auto_absent_after_minutes == 240
        # 08:30 + 15 + 45 = 09:30
        assert policy.very_late_cutoff_label == "09:30"
        # 08:30 + 240 = 12:30
        assert policy.auto_absent_label == "12:30"

    def test_blank_absence_cutoff_is_stored_as_null(self, clean_db, app_context):
        policy = AttendancePolicyService().upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10, auto_absent_after_minutes=None,
        )
        db.session.commit()
        assert policy.auto_absent_after_minutes is None
        assert policy.auto_absent_label == "—"


class TestPolicyResolution:
    def test_department_override_wins(self, clean_db, app_context, department):
        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10, very_late_after_minutes=60,
        )
        service.upsert(
            department_id=department.id, shift_start=time(7, 0), shift_end=time(15, 0),
            grace_minutes=5, very_late_after_minutes=30,
        )
        db.session.commit()

        resolved = service.resolve_shift(department.id)
        assert resolved.shift_start == time(7, 0)
        assert resolved.grace_minutes == 5
        assert resolved.very_late_after_minutes == 30

    def test_department_without_override_uses_global(self, clean_db, app_context, department):
        from app.features.employees.service import DepartmentService

        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10,
        )
        other = DepartmentService().create_department(name="Finance", code="FIN")
        db.session.commit()

        assert service.resolve_shift(other.id).shift_start == time(9, 0)

    def test_builtin_default_when_nothing_configured(self, clean_db, app_context):
        resolved = AttendancePolicyService().resolve_shift(None)
        assert resolved.shift_start == time(9, 0)
        assert resolved.grace_minutes == 10

    def test_preview_reports_the_source_of_the_policy(
        self, clean_db, app_context, department
    ):
        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10,
        )
        db.session.commit()
        assert service.resolved_preview(department.id)["source"] == "global"

        service.upsert(
            department_id=department.id, shift_start=time(8, 0), shift_end=time(16, 0),
            grace_minutes=5,
        )
        db.session.commit()
        assert service.resolved_preview(department.id)["source"] == "department"

    def test_global_policy_cannot_be_deleted(self, clean_db, app_context):
        service = AttendancePolicyService()
        policy = service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10,
        )
        db.session.commit()
        with pytest.raises(InvalidCorrectionError):
            service.delete(policy.id)

    def test_department_override_can_be_deleted(self, clean_db, app_context, department):
        service = AttendancePolicyService()
        policy = service.upsert(
            department_id=department.id, shift_start=time(8, 0), shift_end=time(16, 0),
            grace_minutes=5,
        )
        db.session.commit()

        service.delete(policy.id)
        db.session.commit()
        assert AttendancePolicy.query.filter_by(department_id=department.id).count() == 0


class TestArrivalTiers:
    def policy(self, *, very_late_after=60, auto_absent=None) -> ShiftPolicy:
        return ShiftPolicy(
            shift_start=time(9, 0), shift_end=time(17, 0), grace_minutes=10,
            very_late_after_minutes=very_late_after,
            auto_absent_after_minutes=auto_absent,
        )

    def test_before_shift_is_on_time(self):
        policy = self.policy()
        late = compute_late_minutes(utc(5, 30), work_date=WORK_DATE, policy=policy, tz_name=TZ)
        assert late == 0
        assert policy.arrival_tier(late) == ArrivalTier.ON_TIME

    def test_inside_grace_is_on_time(self):
        policy = self.policy()
        # 06:05 UTC == 09:05 local, grace ends 09:10
        late = compute_late_minutes(utc(6, 5), work_date=WORK_DATE, policy=policy, tz_name=TZ)
        assert late == 0

    def test_past_grace_is_late(self):
        policy = self.policy()
        # 06:30 UTC == 09:30 local -> 20 min past grace
        late = compute_late_minutes(utc(6, 30), work_date=WORK_DATE, policy=policy, tz_name=TZ)
        assert late == 20
        assert policy.arrival_tier(late) == ArrivalTier.LATE

    def test_boundary_minute_stays_late(self):
        """The threshold is exclusive: exactly 60 is late, 61 is very late."""
        policy = self.policy(very_late_after=60)
        assert policy.arrival_tier(60) == ArrivalTier.LATE
        assert policy.arrival_tier(61) == ArrivalTier.VERY_LATE

    def test_tier_is_configurable(self):
        assert self.policy(very_late_after=20).arrival_tier(30) == ArrivalTier.VERY_LATE
        assert self.policy(very_late_after=180).arrival_tier(30) == ArrivalTier.LATE

    def test_resolve_punch_returns_very_late_status(self):
        policy = self.policy(very_late_after=60)
        # 07:30 UTC == 10:30 local -> 80 min past grace
        result = resolve_punch(
            punch=__import__(
                "app.features.attendance.engine", fromlist=["PunchEvent"]
            ).PunchEvent(utc(7, 30), PunchType.IN),
            work_date=WORK_DATE, tz_name=TZ, policy=policy,
        )
        assert result.status == AttendanceStatus.VERY_LATE
        assert result.tier == ArrivalTier.VERY_LATE

    def test_recompute_returns_very_late_status(self):
        policy = self.policy(very_late_after=60)
        result = recompute(
            work_date=WORK_DATE, tz_name=TZ, policy=policy,
            clock_in=utc(7, 30), clock_out=utc(14, 0),
        )
        assert result.status == AttendanceStatus.VERY_LATE

    def test_status_helpers_treat_both_tiers_as_late(self):
        assert AttendanceStatus.LATE.is_late
        assert AttendanceStatus.VERY_LATE.is_late
        assert AttendanceStatus.VERY_LATE.counts_as_present
        assert not AttendanceStatus.VERY_LATE.counts_as_absence


class TestAutoAbsence:
    def policy(self, *, auto_absent=None) -> ShiftPolicy:
        return ShiftPolicy(
            shift_start=time(9, 0), shift_end=time(17, 0), grace_minutes=10,
            very_late_after_minutes=60, auto_absent_after_minutes=auto_absent,
        )

    def test_no_cutoff_never_marks_absent(self):
        assert not self.policy().is_auto_absent(600)

    def test_cutoff_inclusive(self):
        policy = self.policy(auto_absent=240)
        assert not policy.is_auto_absent(239)
        assert policy.is_auto_absent(240)
        assert policy.is_auto_absent(241)

    def test_has_clock_in_blocks_absence(self):
        assert not should_mark_absent(
            now=utc(12, 0), work_date=WORK_DATE, policy=self.policy(auto_absent=240),
            tz_name=TZ, has_clock_in=True,
        )

    def test_missing_punch_past_cutoff_is_absent(self):
        assert should_mark_absent(
            now=utc(12, 0), work_date=WORK_DATE, policy=self.policy(auto_absent=240),
            tz_name=TZ, has_clock_in=False,
        )

    def test_sweep_marks_employees_past_the_cutoff(
        self, clean_db, app_context, employee, department, attendance_policy
    ):
        """A configured cut-off closes the day without waiting for the night."""
        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10, very_late_after_minutes=60,
            auto_absent_after_minutes=120,
        )
        db.session.commit()

        attendance = AttendanceService()
        # 12:00 UTC on the same day is 15:00 Nairobi: 6 hours after the shift
        # start, comfortably past the 120-minute cut-off.
        summary = attendance.mark_due_absences(
            work_date=WORK_DATE, now=utc(12, 0)
        )
        db.session.commit()

        assert summary["marked_absent"] >= 1
        record = attendance.get_day(employee.id, WORK_DATE)
        assert record is not None
        assert record.status == AttendanceStatus.ABSENT.value

    def test_sweep_skips_employees_who_clocked_in(
        self, clean_db, app_context, employee, attendance_policy
    ):
        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10, very_late_after_minutes=60,
            auto_absent_after_minutes=120,
        )
        db.session.commit()

        attendance = AttendanceService()
        attendance.apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=utc(6, 0),
            punch_type=PunchType.IN,
        )
        db.session.commit()

        summary = attendance.mark_due_absences(work_date=WORK_DATE, now=utc(12, 0))
        db.session.commit()

        record = attendance.get_day(employee.id, WORK_DATE)
        assert record.status != AttendanceStatus.ABSENT.value
        assert summary["skipped_already_in"] >= 1

    def test_sweep_ignores_policies_without_a_cutoff(
        self, clean_db, app_context, employee, attendance_policy
    ):
        # The default policy has no cut-off configured.
        attendance = AttendanceService()
        summary = attendance.mark_due_absences(work_date=WORK_DATE, now=utc(12, 0))
        db.session.commit()

        assert summary["marked_absent"] == 0
        assert summary["skipped_no_cutoff"] >= 1

    def test_sweep_respects_exclusions(
        self, clean_db, app_context, employee, attendance_policy
    ):
        """People already on approved leave must not be marked absent."""
        service = AttendancePolicyService()
        service.upsert(
            department_id=None, shift_start=time(9, 0), shift_end=time(17, 0),
            grace_minutes=10, very_late_after_minutes=60,
            auto_absent_after_minutes=120,
        )
        db.session.commit()

        attendance = AttendanceService()
        summary = attendance.mark_due_absences(
            work_date=WORK_DATE, now=utc(12, 0),
            exclude_employee_ids={employee.id},
        )
        db.session.commit()

        assert employee.id not in summary["employee_ids"]
