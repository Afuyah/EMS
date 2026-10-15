"""Live end-to-end: HR creates a login, the credentials arrive by email, and the
new employee is forced to change their password on first sign-in.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

BASE = "http://127.0.0.1:5065"
FAILURES: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        FAILURES.append(label)


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def login(self, email, password, expect=None):
        data = json.dumps({"email": email, "password": password}).encode()
        request = urllib.request.Request(
            BASE + "/api/v1/auth/login", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode())

    def form_login(self, email, password):
        data = urllib.parse.urlencode({"email": email, "password": password}).encode()
        request = urllib.request.Request(
            BASE + "/app/login", data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        # Follow redirects manually so we can inspect the Location header.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect()
        )
        try:
            with opener.open(request, timeout=20) as response:
                return response.status, response.headers.get("Location", "")
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Location", "")

    def get(self, path):
        try:
            with self.opener.open(urllib.request.Request(BASE + path), timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post_form(self, path, form):
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            BASE + path, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self.opener.open(request, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


print("=== 1. HR signs in and opens the new-employee form ===")
hr = Session()
hr.login("admin@ems.local", "ChangeMe123!")
status, form = hr.get("/app/employees/new")
check("the new-employee form loads", status == 200, str(status))
check("it offers to create a login", 'name="create_user"' in form)
check("it offers a role", 'name="user_role"' in form)

print("\n=== 2. HR creates an employee WITH a login, no password typed ===")
# Deliberately omit 'password' so the system must generate one.
status, body = hr.post_form(
    "/app/employees",
    {
        "first_name": "Zawadi",
        "last_name": "Mbeki",
        "email": "zawadi.mbeki@ems.local",
        "department_id": "1",
        "create_user": "true",
        "user_role": "EMPLOYEE",
        "is_active": "true",
    },
)
check("creation succeeds", status == 200, str(status))
check("HR is told the details were emailed",
      "emailed" in body.lower(), "no confirmation that email was sent")
check("the password is NOT shown to HR",
      not re.search(r"Initial password:", body), "a password was displayed on screen")
check("HR is told about biometrics",
      "biometric" in body.lower(), "no biometrics reminder in the confirmation")

print("\n=== 3. the employee detail page shows the pending hand-off ===")
match = re.search(r"/app/employees/(\d+)", body)
employee_id = match.group(1) if match else None
print(f"    employee id = {employee_id}")
if employee_id:
    status, detail = hr.get(f"/app/employees/{employee_id}")
    check("detail page loads", status == 200, str(status))
    check("shows 'has not signed in yet'", "Has not signed in yet" in detail)
    check("offers to resend the welcome email",
          "/resend-welcome" in detail, "no resend control")

print("\n=== 4. a bad password is still rejected ===")
newcomer = Session()
try:
    newcomer.login("zawadi.mbeki@ems.local", "definitely-wrong")
    check("wrong password is rejected", False, "login succeeded with a bad password")
except urllib.error.HTTPError as error:
    check("wrong password is rejected", error.code == 401, str(error.code))

print("\n=== 5. the emailed password signs them in and forces a change ===")
# Read the generated password from the outbox rather than guessing it.
from app import create_app  # noqa: E402
from app.core.database import db  # noqa: E402

app = create_app("development")
with app.app_context():
    from app.features.email.models import EmailLog

    row = (
        EmailLog.query.filter_by(event_type="welcome")
        .order_by(EmailLog.id.desc())
        .first()
    )
    check("a welcome email was recorded", row is not None)
    password = None
    if row:
        print(f"    to={row.to_email} status={row.status}")
        print(f"    subject={row.subject}")
        found = re.search(r"Password\s*:\s*(\S+)", row.body_text or "")
        password = found.group(1) if found else None
        check("the email contains the password", password is not None)
        body_text = row.body_text or ""
        check("the email links to sign-in", "/app/login" in body_text)
        check("the email tells them to change it",
              "change" in body_text.lower())
        check("the email mentions biometrics",
              "fingerprint" in body_text.lower() or "biometric" in body_text.lower())
        check("the email carries their staff number",
              bool(re.search(r"EMP-\d+", body_text)))

if password:
    status, location = newcomer.form_login("zawadi.mbeki@ems.local", password)
    check("the emailed password works", status in (302, 303), str(status))
    check("they are pushed to their profile, not the dashboard",
          "/app/profile" in location, location)

    status, profile = newcomer.get("/app/profile")
    check("the profile page loads for them", status == 200, str(status))
    check("it asks them to choose their own password",
          "temporary" in profile.lower() or "choose your own password" in profile.lower())

    print("\n=== 6. changing the password clears the forced change ===")
    status, after = newcomer.post_form(
        "/app/profile/password",
        {
            "current_password": password,
            "new_password": "MyOwnSecret123!",
            "confirm_password": "MyOwnSecret123!",
        },
    )
    check("the change is accepted", status == 200, str(status))

    # The old password must now fail, and the new one must work without a redirect.
    fresh = Session()
    try:
        fresh.login("zawadi.mbeki@ems.local", password)
        check("the temporary password no longer works", False, "old password still valid")
    except urllib.error.HTTPError as error:
        check("the temporary password no longer works", error.code == 401, str(error.code))

    fresh2 = Session()
    payload = fresh2.login("zawadi.mbeki@ems.local", "MyOwnSecret123!")
    check("the new password works", "data" in payload or "tokens" in payload)
    check("the forced-change flag is cleared",
          payload.get("data", {}).get("user", {}).get("must_change_password") is False,
          str(payload.get("data", {}).get("user", {}).get("must_change_password")))

    fresh3 = Session()
    status, location = fresh3.form_login("zawadi.mbeki@ems.local", "MyOwnSecret123!")
    check("a normal sign-in now goes to the dashboard",
          "/app/profile" not in location, location)

print("\n=== 7. tidy up the test employee ===")
with app.app_context():
    from app.features.employees.models import Employee
    from app.features.auth.models import User
    from app.features.email.models import EmailLog as Log

    Log.query.filter_by(to_email="zawadi.mbeki@ems.local").delete()
    employee = Employee.query.filter_by(email="zawadi.mbeki@ems.local").first()
    if employee is not None:
        user_id = employee.user_id
        db.session.delete(employee)
        db.session.flush()
        if user_id is not None:
            user = db.session.get(User, user_id)
            if user is not None:
                db.session.delete(user)
    db.session.commit()
    print("    removed the test employee and their login")

print(f"\n=== RESULT: {len(FAILURES)} failure(s) ===")
for failure in FAILURES:
    print("  -", failure)
sys.exit(1 if FAILURES else 0)
