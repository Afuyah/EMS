"""Reproduce the production logout-on-every-action bug.

Hypothesis: in production ``JWT_COOKIE_CSRF_PROTECT`` is True, but the templates
render ``{{ csrf_token }}`` while nothing ever provides that variable. Every
cookie-authenticated POST therefore fails CSRF verification with a 401, which
presents to the user as being logged out.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Production-shaped environment.
os.environ["FLASK_CONFIG"] = "production"
os.environ["SECRET_KEY"] = "a" * 48
os.environ["JWT_SECRET_KEY"] = "b" * 48
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres:Henrix1044314@127.0.0.1:5432/ems_dev"
os.environ["CORS_ORIGINS"] = "http://localhost"
os.environ["JWT_COOKIE_SECURE"] = "true"
os.environ["RATELIMIT_ENABLED"] = "false"
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
os.environ["EMAIL_ENABLED"] = "false"
os.environ.pop("TESTING", None)

from app import create_app  # noqa: E402

app = create_app("production")
# The production config marks the auth cookies Secure-only. Drive the test client
# over HTTPS so those cookies are actually sent back, exactly as a browser would
# over the real TLS-terminated deployment.
app.config["PREFERRED_URL_SCHEME"] = "https"
print(f"JWT_COOKIE_CSRF_PROTECT = {app.config['JWT_COOKIE_CSRF_PROTECT']}")
print(f"JWT_CSRF_IN_COOKIES     = {app.config['JWT_CSRF_IN_COOKIES']}")
print(f"JWT_TOKEN_LOCATION      = {app.config['JWT_TOKEN_LOCATION']}")

client = app.test_client()

print("\n=== 1. log in over the HTML form ===")
response = client.post(
    "/app/login",
    data={"email": "admin@ems.local", "password": "ChangeMe123!"},
    follow_redirects=False,
)
print(f"  login status: {response.status_code}")
set_cookies = [
    item.split("=", 1)[0] for item in response.headers.getlist("Set-Cookie")
]
print(f"  cookies set : {set_cookies}")

csrf_value = None
for item in response.headers.getlist("Set-Cookie"):
    if "csrf_access_token=" in item:
        csrf_value = item.split("csrf_access_token=")[1].split(";")[0]
print(f"  csrf_access_token present: {csrf_value is not None}")

print("\n=== 2. is csrf_token available to templates? ===")
page = client.get("/app/employees", follow_redirects=False)
print(f"  GET /app/employees -> {page.status_code}")
if page.status_code == 200:
    import re  # noqa: E402

    match = re.search(r'X-CSRF-TOKEN":\s*"([^"]*)"', page.get_data(as_text=True))
    rendered = match.group(1) if match else "(header not found)"
    print(f"  rendered X-CSRF-TOKEN = {rendered!r}")
    if rendered == "":
        print("  --> EMPTY: the template variable is undefined.")
        print("      With JWT_COOKIE_CSRF_PROTECT=True, every POST must then fail.")

print("\n=== 3. cookie-authenticated POST with NO CSRF header (what the app does today) ===")
blocked = client.post(
    "/app/employees",
    data={"first_name": "Csrf", "last_name": "Probe", "email": "csrf.probe@ems.local"},
    follow_redirects=False,
)
print(f"  status: {blocked.status_code}")
location = blocked.headers.get("Location", "-")
print(f"  Location: {location}")
blocked_without_token = blocked.status_code == 401 or "login" in location
if blocked.status_code == 401:
    print("  --> 401: CSRF verification rejected it.")
if blocked.status_code == 302 and "login" in location:
    print("  --> 302 to /app/login: without a token the user IS thrown out.")
    print("      That is why the forms must carry one.")

print("\n=== 4. the same POST with the CSRF header the template now renders ===")
import re as _re  # noqa: E402

page_body = client.get("/app/employees").get_data(as_text=True)
found = _re.search(r'X-CSRF-TOKEN":\s*"([^"]*)"', page_body)
rendered_token = found.group(1) if found else ""
print(f"  token rendered in the page: {rendered_token!r}")
header_ok = False
if rendered_token:
    allowed = client.post(
        "/app/employees",
        data={"first_name": "Csrf", "last_name": "Probe", "email": "csrf.probe@ems.local"},
        headers={"X-CSRF-TOKEN": rendered_token},
        follow_redirects=False,
    )
    print(f"  status WITH the header: {allowed.status_code}")
    print(f"  Location: {allowed.headers.get('Location', '-')}")
    header_ok = allowed.status_code == 302 and "login" not in allowed.headers.get(
        "Location", ""
    )
    if header_ok:
        print("  --> SUCCESS: the action went through instead of logging the user out.")


print("\n=== 5. a form POST carries the hidden field (browser behaviour) ===")
# The template injects a hidden csrf_token input via JS; posting it as a form
# field is the server-side equivalent, and must also be accepted.
form_ok = False
if rendered_token:
    form_post = client.post(
        "/app/employees",
        data={
            "first_name": "Csrf",
            "last_name": "Formfield",
            "email": "csrf.formfield@ems.local",
            "csrf_token": rendered_token,
        },
        follow_redirects=False,
    )
    print(f"  status with csrf_token form field: {form_post.status_code}")
    print(f"  Location: {form_post.headers.get('Location', '-')}")
    form_ok = form_post.status_code == 302 and "login" not in form_post.headers.get(
        "Location", ""
    )
    if form_ok:
        print("  --> SUCCESS: posting it as a form field also works.")

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
problems: list[str] = []
if not rendered_token:
    problems.append("the template rendered an empty CSRF token")
if not header_ok:
    problems.append("the X-CSRF-TOKEN header path was rejected")
if not form_ok:
    problems.append("the csrf_token form-field path was rejected")
if not blocked_without_token:
    problems.append("a write with no token was allowed through (CSRF disabled)")

print("\n" + "=" * 72)
print(f"RESULT: {len(problems)} problem(s)")
print("=" * 72)
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)



