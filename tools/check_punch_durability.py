"""A punch must never be lost because a later step failed.

Reproduces the production failure without touching the schema: attendance
resolution is made to fail, and the punch must still be safely stored and
replayable.

Before this change the whole transaction rolled back, so the punch vanished and
the device's retry hit the duplicate check and gave up — the punch was simply
gone. Now the punch is committed first, left ``processed = false`` when
resolution fails, and completed by ``flask reprocess-punches``.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
# Development sets DEBUG, which makes the test client re-raise server exceptions
# instead of returning a 500. This check needs the *response*, so behave like the
# production server.
app.config["PROPAGATE_EXCEPTIONS"] = False
app.config["DEBUG"] = False
problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


with app.app_context():
    from app.features.attendance.models import AttendanceDaily
    from app.features.biometrics.models import BiometricDevice, BiometricRawLog
    from app.features.biometrics.service import BiometricsService
    from app.features.employees.models import Employee

    serial = "ROBUST-001"
    old = BiometricDevice.query.filter_by(serial_no=serial).first()
    if old is not None:
        BiometricRawLog.query.filter_by(device_id=old.id).delete()
        for mapping in list(old.mappings):
            db.session.delete(mapping)
        db.session.delete(old)
        db.session.commit()

    service = BiometricsService()
    device, key = service.register_device(serial_no=serial, location="robustness")
    employee = Employee.query.filter_by(is_active=True).first()
    service.map_employee(
        device_id=device.id, employee_id=employee.id, device_user_id="1"
    )
    db.session.commit()
    device_id = device.id
    print(f"employee: {employee.full_name}, device {serial}, user '1' mapped\n")

client = app.test_client()


def push(line):
    return client.post(
        "/iclock/cdata",
        data=urlencode(
            {"SN": serial, "table": "ATTLOG", "CommKey": key, "data": line}
        ),
        content_type="application/x-www-form-urlencoded",
    )


# ---------------------------------------------------------------------------
print("=== 1. normal push ===")
response = push("1\t2026-08-01 08:00:00\t0\n")
check("the push is accepted", response.get_data(as_text=True) == "OK",
      repr(response.get_data(as_text=True)))
with app.app_context():
    logs = BiometricRawLog.query.filter_by(device_id=device_id).all()
    check("one raw log", len(logs) == 1, str(len(logs)))
    check("it is processed", logs[0].processed is True)

# ---------------------------------------------------------------------------
print("\n=== 2. attendance resolution fails (the production failure) ===")
from app.features.attendance.service import AttendanceService  # noqa: E402

with patch.object(AttendanceService, "apply_punch", side_effect=RuntimeError("boom")):
    response = push("1\t2026-08-02 08:00:00\t0\n")

body = response.get_data(as_text=True).strip()
check("the device is asked to retry (not silently told OK)", body != "OK", repr(body))

with app.app_context():
    logs = BiometricRawLog.query.filter_by(device_id=device_id).all()
    check("THE PUNCH WAS STILL STORED", len(logs) == 2, f"{len(logs)} log(s)")
    unprocessed = [row for row in logs if not row.processed]
    check("it is left unprocessed for replay", len(unprocessed) == 1,
          f"{len(unprocessed)} unprocessed")

# ---------------------------------------------------------------------------
print("\n=== 3. the device retries the same punch (still failing) ===")
with app.app_context():
    before = BiometricRawLog.query.filter_by(device_id=device_id).count()

with patch.object(AttendanceService, "apply_punch", side_effect=RuntimeError("boom")):
    push("1\t2026-08-02 08:00:00\t0\n")

with app.app_context():
    after = BiometricRawLog.query.filter_by(device_id=device_id).count()
check("the retry does not duplicate the raw log", after == before,
      f"before={before} after={after}")

# ---------------------------------------------------------------------------
print("\n=== 4. the cause is fixed, then replay ===")
import subprocess  # noqa: E402

result = subprocess.run(
    [sys.executable, "-m", "flask", "--app", "wsgi", "reprocess-punches"],
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    capture_output=True,
    text=True,
    env={**os.environ, "FLASK_CONFIG": "development"},
)
summary = [
    line.strip()
    for line in (result.stdout or "").splitlines()
    if "resolved=" in line or "No unprocessed" in line
]
print(f"    {summary[0] if summary else (result.stdout or '')[-200:]}")
check("the replay succeeded", result.returncode == 0, (result.stderr or "")[-200:])

with app.app_context():
    logs = BiometricRawLog.query.filter_by(device_id=device_id).all()
    check("no punch is left unprocessed", all(row.processed for row in logs),
          str([row.processed for row in logs]))
    check("both punches are now recorded", len(logs) == 2, str(len(logs)))

# ---------------------------------------------------------------------------
print("\n=== cleanup ===")
with app.app_context():
    BiometricRawLog.query.filter_by(device_id=device_id).delete()
    device_row = BiometricDevice.query.get(device_id)
    for mapping in list(device_row.mappings):
        db.session.delete(mapping)
    db.session.delete(device_row)
    AttendanceDaily.query.filter(
        AttendanceDaily.employee_id == employee.id,
        AttendanceDaily.work_date >= "2026-08-01",
        AttendanceDaily.work_date <= "2026-08-31",
    ).delete()
    db.session.commit()
    print("    removed the device, logs, mapping and attendance rows")

print("\n" + "=" * 68)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 68)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
