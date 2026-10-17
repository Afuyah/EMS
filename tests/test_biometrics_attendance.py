"""Integration tests for the punch -> attendance path against real PostgreSQL."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.database import db
from app.features.attendance.constants import AttendanceStatus
from app.features.attendance.service import AttendanceService
from app.features.biometrics.exceptions import (
    DeviceCredentialsError,
    DeviceNotFoundError,  # noqa: F401  kept for API parity in assertions
)
from app.features.biometrics.models import BiometricRawLog
from app.features.biometrics.service import BiometricsService

pytestmark = pytest.mark.integration

#: 08:00 UTC is 11:00 in Nairobi — comfortably after the 09:10 grace cutoff.
EARLY_UTC = datetime(2026, 3, 2, 5, 45, tzinfo=timezone.utc)   # 08:45 Nairobi
LATE_UTC = datetime(2026, 3, 2, 6, 30, tzinfo=timezone.utc)    # 09:30 Nairobi
OUT_UTC = datetime(2026, 3, 2, 14, 0, tzinfo=timezone.utc)     # 17:00 Nairobi


class TestDeviceAuthentication:
    def test_valid_credentials_return_the_device(self, device):
        record, secret = device
        authenticated = BiometricsService().authenticate(record.serial_no, secret)
        assert authenticated.id == record.id

    def test_unknown_serial_is_rejected(self, device):
        with pytest.raises(DeviceCredentialsError):
            BiometricsService().authenticate("NOPE", "whatever")

    def test_wrong_secret_is_rejected(self, device):
        record, _secret = device
        with pytest.raises(DeviceCredentialsError):
            BiometricsService().authenticate(record.serial_no, "wrong-secret")

    def test_secret_is_hashed_at_rest(self, device):
        record, secret = device
        assert record.secret_hash != secret
        assert secret not in record.secret_hash

    def test_rotation_invalidates_the_old_secret(self, device):
        record, old_secret = device
        service = BiometricsService()
        _device, new_secret = service.rotate_secret(record.id)
        db.session.commit()

        assert new_secret != old_secret
        # The new secret works, the old one does not.
        service.authenticate(record.serial_no, new_secret)
        with pytest.raises(DeviceCredentialsError):
            service.authenticate(record.serial_no, old_secret)


class TestAdapterParsing:
    def test_zkteco_payload_is_parsed(self, device):
        record, _secret = device
        service = BiometricsService()
        parsed = service.parse_punch(
            record,
            {"PIN": "42", "DateTime": "2026-03-02 08:45:00", "Status": 0},
        )
        assert parsed.device_user_id == "42"
        assert parsed.punch_type.value == "in"
        assert parsed.punch_time.year == 2026

    def test_zkteco_epoch_milliseconds_are_parsed(self, device):
        record, _secret = device
        millis = int(datetime(2026, 3, 2, 5, 45, tzinfo=timezone.utc).timestamp() * 1000)
        parsed = BiometricsService().parse_punch(
            record, {"PIN": "42", "DateTime": millis, "Status": 1}
        )
        assert parsed.punch_type.value == "out"

    def test_malformed_payload_raises(self, device):
        from app.features.biometrics.exceptions import MalformedPunchError

        record, _secret = device
        with pytest.raises(MalformedPunchError):
            BiometricsService().parse_punch(record, {"nothing": "useful"})


class TestPunchIngestion:
    def test_punch_creates_attendance_row(self, device_mapping, employee, attendance_policy):
        service = BiometricsService()
        parsed = service.parse_punch(
            device_mapping, {"PIN": "42", "DateTime": "2026-03-02 05:45:00", "Status": 0}
        )
        _mapping, raw_log, created, reason = service.record_punch(
            device=device_mapping, parsed=parsed
        )
        db.session.commit()

        assert created and reason == "accepted"
        assert raw_log is not None
        assert raw_log.processed is False

        record = AttendanceService().apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=parsed.punch_time,
            punch_type=parsed.punch_type,
        )
        db.session.commit()

        assert record.work_date == date(2026, 3, 2)
        assert record.clock_in is not None
        assert record.late_minutes == 0

    def test_replay_is_detected_by_idempotency_hash(
        self, device_mapping, attendance_policy
    ):
        service = BiometricsService()
        parsed = service.parse_punch(
            device_mapping, {"PIN": "42", "DateTime": "2026-03-02 05:45:00", "Status": 0}
        )

        _m1, first_log, created_once, _r1 = service.record_punch(
            device=device_mapping, parsed=parsed
        )
        db.session.commit()
        assert created_once

        _m2, second_log, created_twice, reason = service.record_punch(
            device=device_mapping, parsed=parsed
        )
        db.session.commit()

        assert not created_twice
        assert reason == "duplicate"
        # The existing row is returned, not None: a device retry must be able to
        # resume attendance resolution that failed on the first attempt. The row
        # itself is still the original one, not a second insert.
        assert second_log is not None
        assert second_log.id == first_log.id
        # The append-only log holds exactly one row for the retried punch.
        assert BiometricRawLog.query.count() == 1
        assert BiometricRawLog.query.first().id == first_log.id

    def test_unmapped_user_is_audited_but_not_attributed(
        self, device_mapping, attendance_policy
    ):
        service = BiometricsService()
        parsed = service.parse_punch(
            device_mapping, {"PIN": "9999", "DateTime": "2026-03-02 05:45:00", "Status": 0}
        )
        mapping, raw_log, created, reason = service.record_punch(
            device=device_mapping, parsed=parsed
        )
        db.session.commit()

        assert created
        assert mapping is None
        assert reason == "unmapped_device_user"
        assert raw_log is not None
        assert BiometricRawLog.query.count() == 1

    def test_raw_log_is_append_only(self, device_mapping, attendance_policy):
        service = BiometricsService()
        for hour in (5, 6, 7):
            parsed = service.parse_punch(
                device_mapping,
                {"PIN": "42", "DateTime": f"2026-03-02 0{hour}:45:00", "Status": 0},
            )
            service.record_punch(device=device_mapping, parsed=parsed)
        db.session.commit()
        assert BiometricRawLog.query.count() == 3


class TestAttendanceResolution:
    def test_late_punch_records_late_minutes(
        self, device_mapping, employee, attendance_policy
    ):
        record = AttendanceService().apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=LATE_UTC,
            punch_type="in",
        )
        db.session.commit()
        # 09:30 Nairobi against a 09:10 cutoff.
        assert record.late_minutes == 20
        assert record.status == AttendanceStatus.LATE.value

    def test_second_punch_closes_the_day(self, device_mapping, employee, attendance_policy):
        service = AttendanceService()
        service.apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=EARLY_UTC,
            punch_type="in",
        )
        db.session.commit()

        record = service.apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=OUT_UTC,
            punch_type="out",
        )
        db.session.commit()

        assert record.clock_out is not None
        assert record.worked_minutes == 8 * 60 + 15  # 08:45 -> 17:00
        assert record.status == AttendanceStatus.PRESENT.value

    def test_third_punch_is_ignored(self, employee, attendance_policy):
        service = AttendanceService()
        for punch_time, punch_type in ((EARLY_UTC, "in"), (OUT_UTC, "out"), (OUT_UTC, "out")):
            service.apply_punch(
                employee_id=employee.id,
                department_id=employee.department_id,
                tz_name=employee.tz,
                punch_time=punch_time,
                punch_type=punch_type,
            )
        db.session.commit()

        from app.features.attendance.models import AttendanceDaily

        assert AttendanceDaily.query.count() == 1

    def test_department_policy_overrides_the_global_default(
        self, employee, department, attendance_policy
    ):
        """A department shift starting later makes the same punch on time."""
        from app.features.attendance.service import AttendancePolicyService

        AttendancePolicyService().upsert(
            department_id=department.id,
            shift_start=__import__("datetime").time(11, 0),
            shift_end=__import__("datetime").time(19, 0),
            grace_minutes=15,
        )
        db.session.commit()

        record = AttendanceService().apply_punch(
            employee_id=employee.id,
            department_id=employee.department_id,
            tz_name=employee.tz,
            punch_time=datetime(2026, 3, 2, 8, 30, tzinfo=timezone.utc),
            punch_type="in",
        )
        db.session.commit()
        # 11:30 Nairobi against an 11:15 cutoff -> 15 minutes late.
        assert record.late_minutes == 15
