"""Live end-to-end check of the ZKTeco ADMS path.

Simulates exactly what a physical terminal does:

1. register the device (admin) and keep its CommKey
2. ``GET /iclock/cdata?SN=...`` handshake
3. ``POST /iclock/cdata`` with a real ``table=ATTLOG`` form body
4. confirm the punch became attendance

Runs the pure parser too, since the wire format is the risky part.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


# ---------------------------------------------------------------------------
print("=== 1. the parser against realistic device bodies ===")
from app.features.biometrics.adms import (
    REPLY_OK,
    format_handshake,
    is_attendance_push,
    parse_cdata,
    parse_punch_line,
)

# A real ZKTeco ATTLOG line, tab separated.
line = "1\t2026-10-16 08:12:33\t0\t1\t0"
punch = parse_punch_line(line)
check("a tab-separated ATTLOG row parses", punch is not None)
if punch:
    check("the user id is read", punch.device_user_id == "1", punch.device_user_id)
    check("the timestamp is read",
          punch.punch_time.year == 2026 and punch.punch_time.hour == 8,
          str(punch.punch_time))
    check("the status digit is kept", punch.status == "0", punch.status)

check("a malformed line is rejected, not guessed",
      parse_punch_line("garbage") is None)
check("an unparseable time is rejected",
      parse_punch_line("1\tnot-a-date\t0") is None)

body_form = {
    "SN": "MB20VL1234567",
    "table": "ATTLOG",
    "Stamp": "9999",
    "data": "1\t2026-10-16 08:12:33\t0\t1\t0\n2\t2026-10-16 17:02:10\t1\t1\t0\n",
}
push = parse_cdata(body_form)
check("the serial is read from the form", push.serial_no == "MB20VL1234567", push.serial_no)
check("both rows parse", len(push.punches) == 2, str(len(push.punches)))
check("the table is recognised", is_attendance_push(push))
check("nothing was left unparsed", not push.unparsed, str(push.unparsed))

check("the handshake names the device",
      "GET OPTION FROM: MB20VL1234567" in format_handshake("MB20VL1234567"))
check("the handshake asks for attendance pushes",
      "TransFlag=TransData AttLog" in format_handshake("SN1"))

# ---------------------------------------------------------------------------
print("\n=== 2. register a device and get its CommKey ===")
with app.app_context():
    from app.features.biometrics.service import BiometricsService
    from app.features.employees.models import Employee
    from app.features.biometrics.models import BiometricDevice, BiometricRawLog

    # Clean any previous run.
    serial = "ADMS-CHECK-001"
    old = BiometricDevice.query.filter_by(serial_no=serial).first()
    if old is not None:
        BiometricRawLog.query.filter_by(device_id=old.id).delete()
        for mapping in list(old.mappings):
            db.session.delete(mapping)
        db.session.delete(old)
        db.session.commit()

    service = BiometricsService()
    device, secret = service.register_device(serial_no=serial, location="ADMS Check")
    employee = Employee.query.filter_by(is_active=True).first()
    service.map_employee(
        device_id=device.id, employee_id=employee.id, device_user_id="1"
    )
    db.session.commit()
    print(f"    device id={device.id} serial={device.serial_no}")
    print(f"    employee {employee.full_name} mapped to device user '1'")
    print(f"    CommKey = {secret[:12]}... (truncated)")

client = app.test_client()

# ---------------------------------------------------------------------------
print("\n=== 3. the device handshake ===")
handshake = client.get(
    "/iclock/cdata",
    query_string={"SN": serial, "options": "all", "CommKey": secret},
)
check("handshake returns 200", handshake.status_code == 200, str(handshake.status_code))
text = handshake.get_data(as_text=True)
check("handshake is plain text, not HTML",
      handshake.headers["Content-Type"].startswith("text/plain"),
      handshake.headers.get("Content-Type", ""))
check("handshake instructs the device", "GET OPTION FROM:" in text, text[:60])
check("handshake names the right serial", serial in text)

print("\n=== 4. a wrong CommKey is refused ===")
bad = client.get("/iclock/cdata", query_string={"SN": serial, "CommKey": "wrong-key"})
check("bad key is refused", bad.status_code == 401, str(bad.status_code))
check("the refusal is plain text", bad.headers["Content-Type"].startswith("text/plain"))
check("the refusal body is not HTML", "<" not in bad.get_data(as_text=True))

print("\n=== 5. push an attendance record (what the terminal does) ===")
# The CommKey travels in the body, because the hardware cannot set headers.
form = {
    "SN": serial,
    "table": "ATTLOG",
    "Stamp": "9999",
    "CommKey": secret,
    "data": "1\t2026-10-16 08:12:33\t0\t1\t0\n",
}
push_response = client.post(
    "/iclock/cdata",
    data=urlencode(form),
    content_type="application/x-www-form-urlencoded",
)
check("the push is accepted", push_response.status_code == 200,
      str(push_response.status_code))
check("the reply is exactly OK", push_response.get_data(as_text=True) == REPLY_OK,
      repr(push_response.get_data(as_text=True)))

with app.app_context():
    from app.features.attendance.models import AttendanceDaily
    from app.features.biometrics.models import BiometricEmployeeMap, BiometricRawLog

    device_row = BiometricDevice.query.filter_by(serial_no=serial).first()
    logs = BiometricRawLog.query.filter_by(device_id=device_row.id).all()
    check("a raw log was stored", len(logs) == 1, str(len(logs)))
    if logs:
        check("the raw log is marked processed", logs[0].processed)

    record = AttendanceDaily.query.filter_by(
        employee_id=employee.id, work_date=logs[0].punch_time.date() if logs else None
    ).first() if logs else None
    check("attendance was created from the punch", record is not None)
    if record is not None:
        print(f"    attendance id={record.id} status={record.status} "
              f"clock_in={record.clock_in}")

print("\n=== 6. a retry of the same batch must not duplicate ===")
again = client.post(
    "/iclock/cdata",
    data=urlencode(form),
    content_type="application/x-www-form-urlencoded",
)
check("the retry still answers OK", again.get_data(as_text=True) == REPLY_OK)
with app.app_context():
    from app.features.attendance.models import AttendanceDaily
    from app.features.biometrics.models import BiometricRawLog

    device_row = BiometricDevice.query.filter_by(serial_no=serial).first()
    count = BiometricRawLog.query.filter_by(device_id=device_row.id).count()
    check("no duplicate raw log", count == 1, str(count))

print("\n=== 7. an unmapped device user is audited, not lost ===")
unmapped = dict(form, data="999\t2026-10-16 09:00:00\t0\t1\t0\n")
resp = client.post(
    "/iclock/cdata",
    data=urlencode(unmapped),
    content_type="application/x-www-form-urlencoded",
)
check("still answers OK", resp.get_data(as_text=True) == REPLY_OK)
with app.app_context():
    from app.features.biometrics.models import BiometricDevice, BiometricRawLog

    device_row = BiometricDevice.query.filter_by(serial_no=serial).first()
    unmapped_logs = BiometricRawLog.query.filter_by(
        device_id=device_row.id, device_user_id="999"
    ).count()
    check("the unmapped punch is recorded", unmapped_logs == 1, str(unmapped_logs))

print("\n=== 8. liveness probe (no credentials) ===")
ping = client.get("/iclock/ping")
check("ping answers OK", ping.get_data(as_text=True) == REPLY_OK)
check("ping needs no auth", ping.status_code == 200)

print("\n=== 9. an unregistered serial cannot push ===")
rogue = client.post(
    "/iclock/cdata",
    data=urlencode(dict(form, SN="NOT-REGISTERED")),
    content_type="application/x-www-form-urlencoded",
)
check("unknown serial is refused", rogue.status_code == 401, str(rogue.status_code))

# ---------------------------------------------------------------------------
print("\n=== cleanup ===")
with app.app_context():
    from app.features.attendance.models import AttendanceDaily
    from app.features.biometrics.models import BiometricDevice, BiometricRawLog

    device_row = BiometricDevice.query.filter_by(serial_no=serial).first()
    if device_row is not None:
        BiometricRawLog.query.filter_by(device_id=device_row.id).delete()
        for mapping in list(device_row.mappings):
            db.session.delete(mapping)
        db.session.delete(device_row)
    AttendanceDaily.query.filter(
        AttendanceDaily.employee_id == employee.id,
        AttendanceDaily.work_date == __import__("datetime").date(2026, 10, 16),
    ).delete()
    db.session.commit()
    print("    removed the device, logs, mapping and attendance row")

print("\n" + "=" * 72)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 72)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
