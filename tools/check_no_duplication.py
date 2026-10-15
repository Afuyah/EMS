"""Verify no page renders duplicate content.

Content is isolated, HTML comments are stripped (a comment that repeats a card
title is not duplication), and each anchor is chosen so it appears exactly once
in a correct render.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app

app = create_app("development")

ADMIN_PAGES: dict[str, list[tuple[str, str]]] = {
    "/app/dashboard": [
        ("page heading", ">Overview</h1>"),
        ("kpi strip", "Clocked in today"),
        ("attention strip", "Needs attention"),
        ("attendance trend card", "Attendance trend"),
        ("health card", "Attendance health"),
        ("most late card", "Most late"),
        ("recent punches card", "Recent punches"),
        ("latest activity card", "Latest activity"),
    ],
    "/app/leave/request": [
        ("page heading", ">Request leave</h1>"),
        ("balances card", "Your balances"),
        ("form card", "Request details"),
        ("suggestion target", 'id="suggestion-panel"'),
        ("rules card", "Rules that apply to you"),
    ],
    "/app/leave/plan": [
        ("generate form", 'action="/app/leave/plan/generate"'),
        ("month grid", "Year at a glance"),
        ("roster table", "Roster detail"),
        ("department selector", 'name="department_id"'),
        ("year switcher", "All departments"),
    ],
    "/app/leave/admin": [
        ("pending queue", "Pending decisions"),
        ("calendar", "who is away"),
        ("recently decided", "Recently decided"),
    ],
    "/app/attendance/policies": [
        ("policy table", "Configured policies"),
        ("resolved table", "What each department actually gets"),
        ("explainer", "How a punch is judged"),
        ("new-policy link", 'href="/app/attendance/policies/new"'),
    ],
    "/app/attendance": [
        ("board", 'id="attendance-board"'),
        ("late panel", "Late today"),
        ("absent panel", "Absent today"),
        ("live stream", "Live punch stream"),
    ],
    "/app/employees": [
        ("distribution card", "Department distribution"),
        ("health card", "Attendance health"),
        ("add-employee link", "/app/employees/new"),
        ("gender link", "/app/employees/gender"),
    ],
    "/app/employees/gender": [
        # The list and its save button only render while somebody still needs a
        # gender; once everyone has one the page shows its empty state. The
        # explainer is present either way, so that is the stable anchor.
        ("explainer", "How this is used"),
    ],
    "/app/reports": [
        ("report chooser", "Attendance by department"),
        ("filters", "All departments"),
        # Two downloads are expected (the report CSV and the import template),
        # so anchor on the report-specific one.
        ("export control", "/api/v1/exports/attendance/summary.csv"),
    ],
    "/app/rules": [
        ("colour ladder", "Attendance health ladder"),
        ("policies link", "/app/attendance/policies"),
    ],
}

EMPLOYEE_PAGES: dict[str, list[tuple[str, str]]] = {
    "/app/dashboard": [
        ("page heading", "Today"),
        ("allocation banner", "Your annual leave"),
        ("balance cards", "Leave balances"),
        ("attendance table", "My attendance"),
        ("status tile", "Clock in"),
    ],
    "/app/leave": [
        ("page heading", ">Leave "),
        ("allocation banner", "Your allocated leave"),
        ("balances card", "Balance detail"),
        ("department roster", "Your department"),
        ("upcoming card", "Upcoming and active"),
        ("history card", "Past and closed requests"),
    ],
    "/app/profile": [
        ("details card", "Your details"),
        ("hr-owned card", "Managed by HR"),
        ("password card", "Change password"),
        ("account card", "Sign-in email"),
    ],
    "/app/attendance": [
        ("board", 'id="attendance-board"'),
    ],
}


def content_only(html: str) -> str:
    """The rendered content region, with HTML comments removed."""
    start = html.find("<!-- ============================== MAIN")
    start = html.find("<main", start if start >= 0 else 0)
    end = html.find("</main>", start if start >= 0 else 0)
    body = html[start:end] if start >= 0 and end > start else html
    # A comment repeating a heading is not visible duplication.
    return re.sub(r"<!--.*?-->", "", body, flags=re.S)


problems: list[str] = []


def audit(client, pages, role: str) -> None:
    print(f"\n{'#' * 72}\n# role: {role}\n{'#' * 72}")
    for path, anchors in pages.items():
        response = client.get(path)
        if response.status_code != 200:
            problems.append(f"[{role}] {path}: HTTP {response.status_code}")
            print(f"\n=== {path}  -> HTTP {response.status_code} (expected 200) ===")
            continue

        content = content_only(response.get_data(as_text=True))
        print(f"\n=== {path}  (content {len(content)}b) ===")
        for label, anchor in anchors:
            count = content.count(anchor)
            ok = count == 1
            if not ok:
                problems.append(f"[{role}] {path}: {label} appears {count}x")
            print(f"  [{'ok  ' if ok else 'FAIL'}] {label:<22} {count}x")

        h1_count = len(re.findall(r"<h1", content))
        if h1_count > 1:
            problems.append(f"[{role}] {path}: {h1_count} <h1> in content")
        print(f"  [{'ok  ' if h1_count <= 1 else 'FAIL'}] headings{'':<15} {h1_count}x")


admin = app.test_client()
assert admin.post(
    "/api/v1/auth/login", json={"email": "admin@ems.local", "password": "ChangeMe123!"}
).status_code == 200
audit(admin, ADMIN_PAGES, "admin")

employee = app.test_client()
assert employee.post(
    "/api/v1/auth/login", json={"email": "alice@ems.local", "password": "Password123!"}
).status_code == 200
audit(employee, EMPLOYEE_PAGES, "employee")

print(f"\n=== RESULT: {len(problems)} duplication problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
