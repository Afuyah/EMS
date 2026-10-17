"""Schema drift and punch durability.

Two related production failures:

1. A deploy that lands before its migration makes every query touching the new
   column fail. The symptom appears far from the cause — "The correction could
   not be saved." — so the schema check exists to name it.

2. Before this change a failed attendance write rolled the whole transaction
   back, silently discarding the punch. A punch is evidence: it must survive a
   later failure and be replayable.

**No test here alters the schema.** Dropping a real column breaks the shared
session-wide schema and cascades into every later test — exactly the kind of
self-inflicted chaos these tests exist to prevent. The comparison is exercised as
a pure function, and failures are injected by patching instead of with DDL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.core.database import db
from app.core.schema_check import SchemaReport, diff_schema


class TestDiffSchema:
    """The comparison rules, with no database involved."""

    def test_a_matching_schema_has_no_gaps(self):
        expected = {"t": {"a", "b"}}
        actual = {"t": {"a", "b"}}
        assert diff_schema(expected, actual) == ([], [], [])

    def test_a_missing_column_is_named_as_table_column(self):
        missing_tables, missing_columns, extra = diff_schema(
            {"attendance_daily": {"id", "sessions"}},
            {"attendance_daily": {"id"}},
        )
        assert missing_tables == []
        assert missing_columns == ["attendance_daily.sessions"]
        assert extra == []

    def test_a_missing_table_is_reported_once(self):
        missing_tables, missing_columns, extra = diff_schema(
            {"attendance_daily": {"id"}}, {}
        )
        assert missing_tables == ["attendance_daily"]
        assert missing_columns == [], "columns of a missing table are noise"
        assert extra == []

    def test_extra_columns_are_reported_but_not_fatal(self):
        missing_tables, missing_columns, extra = diff_schema(
            {"t": {"a"}}, {"t": {"a", "gone"}}
        )
        assert missing_tables == []
        assert missing_columns == []
        assert extra == ["t.gone"]

    def test_is_stale_ignores_extra_columns(self):
        """An extra column means a rollback; it does not break the code."""
        report = SchemaReport(extra_columns=["t.gone"])
        assert report.is_stale is False

    def test_is_stale_true_for_a_missing_column(self):
        assert SchemaReport(missing_columns=["t.a"]).is_stale is True

    def test_is_stale_true_for_a_missing_table(self):
        assert SchemaReport(missing_tables=["t"]).is_stale is True


class TestTheLiveSchema:
    def test_the_test_database_matches_the_models(self, app):
        """A gap here means the models themselves are inconsistent."""
        from app.core.schema_check import check_schema

        report = check_schema(app)
        assert report.missing_tables == []
        assert report.missing_columns == []

    def test_the_sessions_column_is_expected_by_the_models(self):
        """The exact column whose absence broke production."""
        from app.features.attendance.models import AttendanceDaily

        assert "sessions" in {column.name for column in AttendanceDaily.__table__.columns}


@pytest.fixture()
def adms_device(employee):
    """A registered ZKTeco device with one mapped employee."""
    from app.features.biometrics.service import BiometricsService

    service = BiometricsService()
    device, secret = service.register_device(
        serial_no="DURABLE-001", location="Test", vendor="zkteco"
    )
    service.map_employee(
        device_id=device.id, employee_id=employee.id, device_user_id="1"
    )
    db.session.commit()
    return {"device": device, "secret": secret, "employee": employee}


def _push(client, device, secret, line: str):
    return client.post(
        "/iclock/cdata",
        data=(
            f"SN={device.serial_no}&table=ATTLOG&CommKey={secret}&data={line}"
        ),
        content_type="application/x-www-form-urlencoded",
    )


class TestPunchDurability:
    def test_a_punch_survives_a_failed_attendance_write(self, client, adms_device):
        """The raw log must be committed before attendance is resolved.

        The failure is injected by patching the attendance service — no schema is
        touched, so this cannot leak into other tests.
        """
        from app.features.attendance.service import AttendanceService
        from app.features.biometrics.models import BiometricRawLog

        device = adms_device["device"]

        with patch.object(
            AttendanceService, "apply_punch", side_effect=RuntimeError("boom")
        ):
            response = _push(
                client, device, adms_device["secret"],
                "1%092026-04-01%2008%3A00%3A00%090%0A",
            )

        # The device is told to retry because the punch is not fully handled.
        assert response.get_data(as_text=True).strip() != "OK"

        logs = BiometricRawLog.query.filter_by(device_id=device.id).all()
        assert len(logs) == 1, "the punch was lost when attendance failed"
        assert logs[0].processed is False, "it must stay replayable"

    def test_an_unmapped_punch_is_closed_out(self, client, adms_device):
        """`processed = false` must never mean "nobody looked at it"."""
        from app.features.biometrics.models import BiometricRawLog

        device = adms_device["device"]
        _push(
            client, device, adms_device["secret"],
            "999%092026-04-02%2009%3A00%3A00%090%0A",
        )

        log = BiometricRawLog.query.filter_by(
            device_id=device.id, device_user_id="999"
        ).first()
        assert log is not None, "an unmapped punch must still be stored"
        assert log.processed is True, "otherwise it shows as 'pending' forever"

    def test_a_duplicate_exposes_the_existing_row(self, client, adms_device):
        """A retry must be able to resume work left unfinished."""
        from app.features.biometrics.adapters import ParsedPunch
        from app.features.biometrics.constants import PunchType
        from app.features.biometrics.service import BiometricsService

        device = adms_device["device"]
        line = "1%092026-04-03%2008%3A00%3A00%090%0A"
        _push(client, device, adms_device["secret"], line)
        _push(client, device, adms_device["secret"], line)

        _mapping, raw_log, created, reason = BiometricsService().record_punch(
            device=device,
            parsed=ParsedPunch(
                device_user_id="1",
                punch_time=datetime(2026, 4, 3, 8, 0, tzinfo=timezone.utc),
                # Status 0 in the wire line maps to IN, and the idempotency hash
                # includes the punch type — so this must match the push exactly.
                punch_type=PunchType.IN,
                raw={},
            ),
        )
        assert created is False
        assert reason == "duplicate"
        assert raw_log is not None, "a duplicate must expose the existing row"


class TestReprocessCommand:
    def test_the_commands_register(self, app):
        commands = set(app.cli.list_commands(None))
        assert "reprocess-punches" in commands
        assert "check-schema" in commands

    def test_reprocess_is_a_no_op_when_nothing_is_pending(self, app, clean_db):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["reprocess-punches"])
        assert result.exit_code == 0
        assert "No unprocessed punches" in result.output

    def test_check_schema_passes_on_a_current_database(self, app):
        runner = app.test_cli_runner()
        result = runner.invoke(args=["check-schema"])
        assert result.exit_code == 0
        assert "Schema matches" in result.output
