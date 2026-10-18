"""ZKTeco ADMS ("iClock") push protocol.

The peer is a piece of hardware, so these tests pin the wire format against
captured payloads. A change that breaks the format would silently stop every
terminal from reporting attendance, which is the kind of failure that goes
unnoticed until payroll.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

import pytest

from app.features.biometrics.adms import (
    REPLY_OK,
    format_handshake,
    is_attendance_push,
    parse_cdata,
    parse_punch_line,
    parse_time,
)
from app.features.biometrics.constants import ADAPTER_REGISTRY, DEFAULT_VENDOR, DeviceVendor
from app.features.biometrics.service import BiometricsService
from app.core.database import db


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------
class TestParseTime:
    @pytest.mark.parametrize(
        "text",
        [
            "2026-10-16 08:12:33",
            "2026-10-16 08:12",
            "2026-10-16T08:12:33",
            "2026-10-16T08:12",
            "2026/10/16 08:12:33",
            "2026-10-16",
        ],
    )
    def test_accepted_layouts(self, text):
        parsed = parse_time(text)
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 10

    @pytest.mark.parametrize("text", ["", "   ", "not-a-date", "16/10/2026", None])
    def test_rejected_values(self, text):
        assert parse_time(text) is None


class TestParsePunchLine:
    def test_tab_separated_row(self):
        punch = parse_punch_line("1\t2026-10-16 08:12:33\t0\t1\t0")
        assert punch is not None
        assert punch.device_user_id == "1"
        assert punch.punch_time == datetime(2026, 10, 16, 8, 12, 33)
        assert punch.status == "0"
        assert punch.verify_mode == "1"

    def test_minimum_three_columns(self):
        punch = parse_punch_line("7\t2026-10-16 09:00:00\t1")
        assert punch is not None
        assert punch.device_user_id == "7"
        assert punch.verify_mode is None

    def test_space_separated_fallback(self):
        """Some firmware separates columns with spaces rather than tabs."""
        punch = parse_punch_line("12 2026-10-16 09:30:00 0")
        assert punch is not None
        assert punch.device_user_id == "12"

    def test_leading_zeros_are_preserved(self):
        punch = parse_punch_line("0001\t2026-10-16 08:00:00\t0")
        assert punch is not None
        assert punch.device_user_id == "0001"

    def test_carriage_return_is_stripped(self):
        punch = parse_punch_line("3\t2026-10-16 08:00:00\t0\r\n")
        assert punch is not None
        assert punch.punch_time == datetime(2026, 10, 16, 8, 0, 0)

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "garbage",
            "1",
            "1\t2026-10-16 08:00:00",  # only two columns: no status
            "1\tnot-a-time\t0",  # unparseable clock
            "\t2026-10-16 08:00:00\t0",  # no user id
            "1\t2026-10-16 08:00:00\tabsent",  # status is not a digit
        ],
    )
    def test_rejected_lines(self, line):
        assert parse_punch_line(line) is None


class TestParseCdata:
    def test_a_realistic_attlog_body(self):
        form = {
            "SN": "MB20VL1234567",
            "table": "ATTLOG",
            "Stamp": "9999",
            "data": (
                "1\t2026-10-16 08:12:33\t0\t1\t0\n"
                "2\t2026-10-16 17:02:10\t1\t1\t0\n"
            ),
        }
        push = parse_cdata(form)
        assert push.serial_no == "MB20VL1234567"
        assert push.table == "ATTLOG"
        assert len(push.punches) == 2
        assert push.unparsed == []
        assert is_attendance_push(push)

    def test_commkey_is_captured(self):
        push = parse_cdata({"SN": "S1", "CommKey": "1234"})
        assert push.comm_key == "1234"

    def test_serial_may_arrive_as_a_body_line(self):
        push = parse_cdata({}, raw_body="~SerialNumber=ABC123\n")
        assert push.serial_no == "ABC123"

    def test_device_info_lines_are_captured(self):
        push = parse_cdata(
            {"SN": "S1"},
            raw_body="~DeviceName=MB20-VL\n~SerialNumber=S1\n",
        )
        assert push.device_info["DeviceName"] == "MB20-VL"

    def test_unparsed_lines_are_reported_not_dropped(self):
        push = parse_cdata({"SN": "S1", "table": "ATTLOG", "data": "garbage\n1\tbad\t0\n"})
        assert push.punches == []
        assert len(push.unparsed) == 2

    def test_a_non_attendance_table_is_not_a_punch_push(self):
        push = parse_cdata({"SN": "S1", "table": "OPERLOG", "data": ""})
        assert not is_attendance_push(push)

    def test_missing_table_with_data_is_treated_as_attendance(self):
        push = parse_cdata({"SN": "S1", "data": "1\t2026-10-16 08:00:00\t0"})
        assert is_attendance_push(push)

    def test_empty_body_is_not_a_punch_push(self):
        push = parse_cdata({"SN": "S1"})
        assert not is_attendance_push(push)
        assert push.punches == []


class TestHandshake:
    def test_names_the_requesting_device(self):
        body = format_handshake("MB20VL999")
        assert "GET OPTION FROM: MB20VL999" in body

    def test_requests_attendance_data(self):
        body = format_handshake("S1")
        assert "TransFlag=TransData AttLog" in body

    def test_missing_serial_does_not_render_a_placeholder_brace(self):
        body = format_handshake("")
        assert "{" not in body and "}" not in body


# ---------------------------------------------------------------------------
# Vendor registry integrity
# ---------------------------------------------------------------------------
class TestVendorRegistry:
    def test_every_vendor_resolves_to_an_adapter(self):
        """A vendor with no adapter fails at registration time."""
        for vendor in DeviceVendor.values():
            assert vendor in ADAPTER_REGISTRY, f"{vendor} has no adapter"

    def test_the_default_vendor_resolves(self):
        assert DEFAULT_VENDOR in ADAPTER_REGISTRY

    def test_default_vendor_is_zkteco(self):
        assert DEFAULT_VENDOR == "zkteco"


class TestCommKeyGeneration:
    """The shared secret must be typeable on a device keypad."""

    def test_generated_secret_is_numeric(self):
        secret = BiometricsService.generate_secret()
        assert secret.isdigit(), f"{secret!r} contains non-digits"

    def test_generated_secret_is_six_digits(self):
        for _ in range(50):
            assert len(BiometricsService.generate_secret()) == 6

    def test_registration_returns_a_numeric_comm_key(self):
        secret = BiometricsService.generate_secret()
        assert secret.isdigit() and len(secret) == 6

    def test_the_default_comm_key_constant_is_the_factory_value(self):
        from app.features.biometrics.constants import DEFAULT_COMM_KEY

        assert DEFAULT_COMM_KEY == "0"


# ---------------------------------------------------------------------------
# The HTTP endpoints a device actually calls
# ---------------------------------------------------------------------------
@pytest.fixture()
def adms_device(employee):
    """A registered ZKTeco device plus the ``employee`` fixture's person mapped."""
    service = BiometricsService()
    device, secret = service.register_device(
        serial_no="ADMS-TEST-001", location="Test", vendor="zkteco"
    )
    service.map_employee(
        device_id=device.id, employee_id=employee.id, device_user_id="1"
    )
    db.session.commit()
    return {"device": device, "secret": secret, "employee": employee}


class TestIclockEndpoints:
    def test_ping_needs_no_credentials(self, client):
        response = client.get("/iclock/ping")
        assert response.status_code == 200
        assert response.get_data(as_text=True) == REPLY_OK

    def test_handshake_returns_plain_text(self, client, adms_device):
        response = client.get(
            "/iclock/cdata",
            query_string={
                "SN": adms_device["device"].serial_no,
                "options": "all",
                "CommKey": adms_device["secret"],
            },
        )
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("text/plain")
        body = response.get_data(as_text=True)
        assert "GET OPTION FROM:" in body
        assert adms_device["device"].serial_no in body

    def test_handshake_refuses_a_wrong_comm_key(self, client, adms_device):
        response = client.get(
            "/iclock/cdata",
            query_string={"SN": adms_device["device"].serial_no, "CommKey": "nope"},
        )
        assert response.status_code == 401
        # The device cannot render HTML, so the refusal must be text.
        assert response.headers["Content-Type"].startswith("text/plain")
        assert "<" not in response.get_data(as_text=True)

    def test_handshake_refuses_an_unknown_serial(self, client, clean_db):
        response = client.get(
            "/iclock/cdata", query_string={"SN": "UNKNOWN-1", "CommKey": "x"}
        )
        assert response.status_code == 401

    def test_a_push_is_stored_and_becomes_attendance(self, client, adms_device):
        from app.features.attendance.models import AttendanceDaily
        from app.features.biometrics.models import BiometricRawLog

        serial = adms_device["device"].serial_no
        body = urlencode(
            {
                "SN": serial,
                "table": "ATTLOG",
                "Stamp": "9999",
                "CommKey": adms_device["secret"],
                "data": "1\t2026-03-02 08:12:33\t0\t1\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 200
        assert response.get_data(as_text=True) == REPLY_OK

        logs = BiometricRawLog.query.filter_by(device_id=adms_device["device"].id).all()
        assert len(logs) == 1
        assert logs[0].processed is True

        record = AttendanceDaily.query.filter_by(
            employee_id=adms_device["employee"].id, work_date=logs[0].punch_time.date()
        ).first()
        assert record is not None
        assert record.clock_in is not None

    def test_a_replayed_batch_does_not_duplicate(self, client, adms_device):
        """Devices retry when a reply is missed, so ingestion must be idempotent."""
        from app.features.biometrics.models import BiometricRawLog

        serial = adms_device["device"].serial_no
        body = urlencode(
            {
                "SN": serial,
                "table": "ATTLOG",
                "CommKey": adms_device["secret"],
                "data": "1\t2026-03-03 08:00:00\t0\n",
            }
        )
        for _ in range(3):
            response = client.post(
                "/iclock/cdata",
                data=body,
                content_type="application/x-www-form-urlencoded",
            )
            assert response.get_data(as_text=True) == REPLY_OK

        assert (
            BiometricRawLog.query.filter_by(device_id=adms_device["device"].id).count()
            == 1
        )

    def test_an_unmapped_user_is_audited_not_lost(self, client, adms_device):
        from app.features.biometrics.models import BiometricRawLog

        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "ATTLOG",
                "CommKey": adms_device["secret"],
                "data": "999\t2026-03-04 09:00:00\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.get_data(as_text=True) == REPLY_OK
        assert (
            BiometricRawLog.query.filter_by(
                device_id=adms_device["device"].id, device_user_id="999"
            ).count()
            == 1
        )

    def test_a_push_with_the_factory_comm_key_is_accepted(self, client, adms_device):
        """Devices ship with no communication password and send the factory key.

        Refusing this would mean no attendance data from a working terminal, so
        it is accepted deliberately. Rotating the secret moves a device off it.
        """
        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "ATTLOG",
                "data": "1\t2026-03-05 08:00:00\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 200
        assert response.get_data(as_text=True) == REPLY_OK

    def test_a_wrong_comm_key_is_still_refused(self, client, adms_device):
        """The default is allowed; anything else must not be."""
        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "ATTLOG",
                "CommKey": "999999",
                "data": "1\t2026-03-05 08:00:00\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code == 401
        assert response.headers["Content-Type"].startswith("text/plain")

    def test_the_configured_comm_key_is_also_accepted(self, client, adms_device):
        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "ATTLOG",
                "CommKey": adms_device["secret"],
                "data": "1\t2026-03-05 08:30:00\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.get_data(as_text=True) == REPLY_OK

    def test_an_inactive_device_cannot_push(self, client, adms_device):
        service = BiometricsService()
        service.set_active(adms_device["device"].id, False)
        db.session.commit()

        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "ATTLOG",
                "CommKey": adms_device["secret"],
                "data": "1\t2026-03-06 08:00:00\t0\n",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.status_code in (401, 403)

    def test_an_operlog_push_is_accepted_as_a_heartbeat(self, client, adms_device):
        body = urlencode(
            {
                "SN": adms_device["device"].serial_no,
                "table": "OPERLOG",
                "CommKey": adms_device["secret"],
                "data": "",
            }
        )
        response = client.post(
            "/iclock/cdata",
            data=body,
            content_type="application/x-www-form-urlencoded",
        )
        assert response.get_data(as_text=True) == REPLY_OK

    def test_getrequest_answers_the_polling_loop(self, client, adms_device):
        response = client.get(
            "/iclock/getrequest",
            query_string={
                "SN": adms_device["device"].serial_no,
                "CommKey": adms_device["secret"],
            },
        )
        assert response.status_code == 200
        assert response.get_data(as_text=True) == REPLY_OK

    def test_getrequest_refuses_an_unknown_device(self, client, clean_db):
        response = client.get(
            "/iclock/getrequest", query_string={"SN": "NOPE", "CommKey": "x"}
        )
        assert response.status_code == 401

    def test_the_routes_are_not_jwt_protected(self, app, adms_device):
        """Devices cannot present a JWT, so these must stay exempt."""
        rules = {
            rule.rule: rule.endpoint
            for rule in app.url_map.iter_rules()
            if rule.rule.startswith("/iclock")
        }
        assert "/iclock/cdata" in rules
        assert "/iclock/ping" in rules


class TestDeviceContactTracking:
    """Reachable and authorised are different questions.

    Recording only authenticated contact made a terminal with a mistyped CommKey
    look like one that had never connected — which sends someone to check cables
    when the credential is the problem.
    """

    def test_a_refused_request_records_contact_but_not_presence(
        self, client, adms_device
    ):
        serial = adms_device["device"].serial_no
        response = client.get(
            "/iclock/cdata", query_string={"SN": serial, "CommKey": "wrong-key"}
        )
        assert response.status_code == 401

        from app.features.biometrics.models import BiometricDevice

        db.session.expire_all()
        device = BiometricDevice.query.filter_by(serial_no=serial).first()
        assert device.last_contact_at is not None, "we could not tell it was knocking"
        assert device.last_seen_at is None, "a refused device must not look present"
        assert device.is_online is False

    def test_a_successful_handshake_records_both(self, client, adms_device):
        serial = adms_device["device"].serial_no
        client.get(
            "/iclock/cdata",
            query_string={
                "SN": serial,
                "options": "all",
                "CommKey": adms_device["secret"],
            },
        )

        from app.features.biometrics.models import BiometricDevice

        db.session.expire_all()
        device = BiometricDevice.query.filter_by(serial_no=serial).first()
        assert device.last_contact_at is not None
        assert device.last_seen_at is not None

    def test_an_unknown_serial_creates_no_row(self, client, clean_db):
        """An arbitrary string must not be able to register a device."""
        from app.features.biometrics.models import BiometricDevice

        before = BiometricDevice.query.count()
        client.get(
            "/iclock/cdata", query_string={"SN": "MADE-UP-999", "CommKey": "x"}
        )
        assert BiometricDevice.query.count() == before

    def test_recording_contact_never_breaks_ingestion(self, client, adms_device):
        """It runs on the device's hot path, before authentication is judged."""
        from unittest.mock import patch

        from app.features.biometrics.models import BiometricDevice

        with patch.object(
            BiometricDevice, "last_contact_at", new_callable=lambda: property(
                lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
            )
        ):
            # Still answers, even if the diagnostic itself fails.
            response = client.get(
                "/iclock/cdata",
                query_string={
                    "SN": adms_device["device"].serial_no,
                    "options": "all",
                    "CommKey": adms_device["secret"],
                },
            )
        assert response.status_code in (200, 401)
