"""Verify that any PostgreSQL URL form resolves to the installed driver.

This is a regression test for a production failure: Railway provides
``postgresql://...`` with no driver, and SQLAlchemy 2.x defaults that to
psycopg 3. This project installs psycopg2-binary instead, so the app died at
import with ``ModuleNotFoundError: No module named 'psycopg'``.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import normalise_database_url  # noqa: E402

failures: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        failures.append(label)


CASES = [
    # (input, expected driver, note)
    ("postgresql://u:p@host:5432/db", "psycopg2", "Railway's bare form"),
    ("postgres://u:p@host:5432/db", "psycopg2", "Heroku's legacy form"),
    ("postgresql+psycopg2://u:p@host:5432/db", "psycopg2", "already explicit"),
    ("postgresql+psycopg://u:p@host:5432/db", "psycopg", "explicitly psycopg 3"),
]

print("=== URL normalisation ===")
for raw, expected_driver, note in CASES:
    result = normalise_database_url(raw)
    url = make_url(result)
    ok = url.drivername == f"postgresql+{expected_driver}" or url.drivername == f"postgresql+{expected_driver}"
    # For the psycopg-3 case the normaliser intentionally leaves it alone.
    if expected_driver == "psycopg":
        ok = url.drivername == "postgresql+psycopg"
    else:
        ok = url.drivername == "postgresql+psycopg2"
    print(f"  [{'ok  ' if ok else 'FAIL'}] {note:<24} {raw[:38]:<38} -> {url.drivername}")
    if not ok:
        failures.append(f"{note}: got {url.drivername}")

print("\n=== non-Postgres URLs are untouched ===")
for raw in ["sqlite:///local.db", "mysql://u:p@host/db", ""]:
    result = normalise_database_url(raw)
    check(f"{raw!r} unchanged", result == raw, f"got {result!r}")

print("\n=== the credential and query string survive ===")
raw = "postgresql://user:secret@containers.railway.app:6543/railway?sslmode=require"
result = normalise_database_url(raw)
url = make_url(result)
check("driver pinned", url.drivername == "postgresql+psycopg2", url.drivername)
check("username preserved", url.username == "user", str(url.username))
check("password preserved", url.password == "secret")
check("host preserved", url.host == "containers.railway.app", str(url.host))
check("port preserved", url.port == 6543, str(url.port))
check("database preserved", url.database == "railway", str(url.database))
check("sslmode preserved", url.query.get("sslmode") == "require", str(dict(url.query)))

print("\n=== the app resolves a Railway-style URL ===")
# Point the app at a bare postgresql:// URL and confirm it builds a psycopg2 URI.
os.environ["DATABASE_URL"] = "postgresql://u:p@127.0.0.1:5432/ems_dev"
os.environ["SECRET_KEY"] = "a" * 48
os.environ["JWT_SECRET_KEY"] = "b" * 48
os.environ["CORS_ORIGINS"] = "https://example.up.railway.app"
os.environ["JWT_COOKIE_SECURE"] = "true"

from app import create_app  # noqa: E402

application = create_app("production")
resolved = application.config["SQLALCHEMY_DATABASE_URI"]
check("production config uses psycopg2", "postgresql+psycopg2://" in resolved, resolved[:60])
print(f"    resolved: {resolved.split('@')[-1]}")

print(f"\n=== RESULT: {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
