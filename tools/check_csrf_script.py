"""Verify the CSRF form-stamping script renders as valid JavaScript.

The fix relies on a hidden input being injected into every POST form. If the
rendered <script> contains a syntax error, the injection silently never happens
and the bug returns — so check the output, not just the source.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["FLASK_CONFIG"] = "production"
os.environ["SECRET_KEY"] = "a" * 48
os.environ["JWT_SECRET_KEY"] = "b" * 48
os.environ["DATABASE_URL"] = "postgresql+psycopg2://postgres:Henrix1044314@127.0.0.1:5432/ems_dev"
os.environ["CORS_ORIGINS"] = "http://localhost"
os.environ["JWT_COOKIE_SECURE"] = "true"
os.environ["RATELIMIT_ENABLED"] = "false"
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
os.environ["EMAIL_ENABLED"] = "false"

from app import create_app  # noqa: E402

app = create_app("production")
app.config["PREFERRED_URL_SCHEME"] = "https"

problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


client = app.test_client()
login = client.post(
    "/app/login",
    data={"email": "admin@ems.local", "password": "ChangeMe123!"},
    follow_redirects=False,
)
check("signed in", login.status_code == 302, str(login.status_code))

page = client.get("/app/employees").get_data(as_text=True)
matches = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)
stamping = [block for block in matches if "stampForms" in block]
check("the stamping script is present exactly once", len(stamping) == 1, f"found {len(stamping)}")
check("the token is embedded as a string literal",
      bool(re.search(r'var TOKEN = "[^"]+";', stamping[0] if stamping else "")),
      "token not found as a literal")

if stamping:
    js = stamping[0]
    print("\n  --- first 6 lines of the rendered script ---")
    for line in js.strip().splitlines()[:6]:
        print(f"      {line.strip()}")

    print("\n=== syntax-check the rendered JavaScript ===")
    node = None
    for candidate in ("node", "node.exe"):
        try:
            subprocess.run([candidate, "--version"], capture_output=True, check=True)
            node = candidate
            break
        except Exception:  # noqa: BLE001
            continue

    if node is None:
        print("  (node not available; checking balance of braces instead)")
        check("braces balanced", js.count("{") == js.count("}"),
              f"{js.count('{')} vs {js.count('}')}")
        check("parens balanced", js.count("(") == js.count(")"),
              f"{js.count('(')} vs {js.count(')')}")
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(js)
            path = handle.name
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check("node --check passes", result.returncode == 0,
              (result.stderr or "").strip()[:200])
        if result.returncode == 0:
            print("      valid JavaScript")

print("\n=== the hidden-field name matches the server's expectation ===")
check("server expects csrf_token",
      app.config["JWT_ACCESS_CSRF_FIELD_NAME"] == "csrf_token",
      app.config["JWT_ACCESS_CSRF_FIELD_NAME"])
check("the script injects csrf_token",
      'var FIELD = "csrf_token"' in (stamping[0] if stamping else ""))
check("form-field checking is enabled", app.config["JWT_CSRF_CHECK_FORM"] is True)

print(f"\n=== RESULT: {len(problems)} problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
