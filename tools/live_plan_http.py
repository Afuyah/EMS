"""Live HTTP check: the annual plan page, employee visibility, and recall."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "http://127.0.0.1:5063"
FAILURES: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" â€” {detail}" if not condition else ""))
    if not condition:
        FAILURES.append(label)


class Session:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def login(self, email, password):
        data = json.dumps({"email": email, "password": password}).encode()
        request = urllib.request.Request(
            BASE + "/api/v1/auth/login", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode())["data"]["tokens"]["access_token"]

    def get(self, path):
        try:
            with self.opener.open(urllib.request.Request(BASE + path), timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def post_form(self, path, form):
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            BASE + path, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        try:
            with self.opener.open(request, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")


print("=== HR opens the annual plan ===")
hr = Session()
hr.login("admin@ems.local", "ChangeMe123!")
status, body = hr.get("/app/leave/plan")
check("plan page loads", status == 200, str(status))
check("shows the year", "Annual leave plan" in body)
check("has a generate control", "/app/leave/plan/generate" in body)
check("has the month grid", "Year at a glance" in body)
check("explains the daily limit", "same day" in body or "limit" in body.lower())
check("nav link present", "Annual plan" in body)

print("\n=== generating requires an explicit department ===")
status, body = hr.post_form("/app/leave/plan/generate", {"year": "2026", "department_id": ""})
check("an all-departments generate is refused with a reason",
      "Choose a department" in body or "per department" in body,
      "no explanation shown")

print("\n=== generate a roster for Alice's own department ===")
# The employee-visibility check below needs a plan covering Alice, so sign her
# in first and plan the department she is actually in.
import re

employee = Session()
employee.login("alice@ems.local", "Password123!")

options = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', body)
check("department options are offered", len(options) >= 1, f"options={options}")
print(f"    departments offered: {[name.strip() for _, name in options]}")

alice_dept = None
for candidate_id, candidate_name in options:
    if candidate_name.strip().lower() == "workshop":
        alice_dept = candidate_id
print(f"    Alice's department (Workshop) id = {alice_dept}")
dept_id = alice_dept or (options[0][0] if options else "4")

status, body = hr.post_form(
    "/app/leave/plan/generate",
    {"year": "2026", "department_id": dept_id, "overwrite": "true"},
)
check("generation succeeds", status == 200, str(status))
check("a confirmation or reason is shown",
      "Draft roster created" in body or "could not" in body)

# The publish controls only render on a department-scoped page.
status, body = hr.get(f"/app/leave/plan?year=2026&department_id={dept_id}")
check("plan page still loads after generating", status == 200, str(status))
check("shows a draft badge", "Draft" in body or "Published" in body)
has_publish = "/publish" in body
check("offers publish when a draft exists", has_publish or "Published" in body)
if not has_publish:
    print(f"    draft page for dept {dept_id}: 'No roster yet'={'No roster yet' in body}")

print("\n=== publish it ===")
if has_publish:
    # Find the plan id from the form action.
    import re

    match = re.search(r"/app/leave/plan/(\d+)/publish", body)
    if match:
        plan_id = match.group(1)
        status, body = hr.post_form(f"/app/leave/plan/{plan_id}/publish", {})
        check("publish succeeds", status == 200, str(status))
        check("published state shown",
              "Roster published" in body or "Published" in body)
    else:
        check("plan id discoverable", False, "no publish action found")

print("\n=== employees see their computed dates on login ===")
status, dash = employee.get("/app/dashboard")
check("employee dashboard loads", status == 200, str(status))
check("employee sees an annual leave banner",
      "Your annual leave" in dash,
      "no allocation banner found")
check("the banner shows a date range",
      "allocated" in dash or "until your leave starts" in dash)

print("\n=== a plain employee cannot reach the plan ===")
status, _ = employee.get("/app/leave/plan")
check("employee is refused (403)", status == 403, str(status))

print("\n=== recall is reachable from the leave queue ===")
status, queue = hr.get("/app/leave/admin")
check("leave queue loads", status == 200, str(status))
check("recall action present", "/recall/" in queue or "Recall" in queue)

print(f"\n=== RESULT: {len(FAILURES)} failure(s) ===")
for failure in FAILURES:
    print("  -", failure)
sys.exit(1 if FAILURES else 0)
