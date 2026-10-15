"""Check for duplicate DOM ids, which is the signature of a block rendered twice."""

from __future__ import annotations

import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app

app = create_app("development")
problems: list[str] = []

ADMIN = [
    "/app/dashboard", "/app/attendance", "/app/employees", "/app/departments",
    "/app/rules", "/app/attendance/policies", "/app/leave/admin",
    "/app/leave/plan", "/app/leave/request", "/app/notifications",
    "/app/reports", "/app/devices",
]
EMPLOYEE = ["/app/dashboard", "/app/leave", "/app/profile", "/app/attendance"]


def audit(client, paths, role):
    print(f"\n{'#' * 70}\n# {role}\n{'#' * 70}")
    for path in paths:
        response = client.get(path)
        if response.status_code != 200:
            print(f"  {path:<28} HTTP {response.status_code}")
            continue
        html = response.get_data(as_text=True)
        ids = re.findall(r'\sid="([^"]+)"', html)
        duplicates = {name: n for name, n in Counter(ids).items() if n > 1}
        if duplicates:
            problems.append(f"[{role}] {path}: duplicate ids {duplicates}")
        status = "ok  " if not duplicates else "FAIL"
        print(f"  [{status}] {path:<28} {len(ids)} ids"
              + (f"  DUPLICATES: {duplicates}" if duplicates else ""))


admin = app.test_client()
assert admin.post(
    "/api/v1/auth/login", json={"email": "admin@ems.local", "password": "ChangeMe123!"}
).status_code == 200
audit(admin, ADMIN, "admin")

employee = app.test_client()
assert employee.post(
    "/api/v1/auth/login", json={"email": "alice@ems.local", "password": "Password123!"}
).status_code == 200
audit(employee, EMPLOYEE, "employee")

print(f"\n=== RESULT: {len(problems)} page(s) with duplicate ids ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
