"""Render the changed pages to confirm they still work."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_CONFIG"] = "development"

from app import create_app
from app.core.database import db

app = create_app("development")
failures: list[str] = []

with app.app_context():
    from app.features.employees.models import Employee

    # Report what the page should contain, so the check is meaningful.
    alice = Employee.query.filter_by(email="alice@ems.local").first()
    print(f"alice: {alice.full_name if alice else 'none'} dept={alice.department_id if alice else '-'}")

    client = app.test_client()
    for email, password, paths in [
        ("alice@ems.local", "Password123!", ["/app/leave", "/app/dashboard"]),
        ("admin@ems.local", "ChangeMe123!", ["/app/leave/plan", "/app/leave/admin"]),
    ]:
        login = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        if login.status_code != 200:
            failures.append(f"login {email}")
            print(f"  [FAIL] login {email}: {login.get_json()}")
            continue
        for path in paths:
            response = client.get(path)
            body = response.get_data(as_text=True)
            ok = response.status_code == 200 and "Traceback" not in body
            if not ok:
                failures.append(f"{email} {path}")
            print(f"  [{'ok  ' if ok else 'FAIL'}] {path:<22} {response.status_code} {len(body):>6}b")
        client.get("/app/logout")

print(f"\n=== {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
