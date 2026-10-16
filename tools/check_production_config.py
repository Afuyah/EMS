"""Verify the app boots in production mode and refuses weak configuration."""

from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Start from a clean slate: the .env file must not mask what we set here.
os.environ["FLASK_CONFIG"] = "production"

import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: None  # ignore .env for this check

failures: list[str] = []


def boot(label: str) -> tuple[bool, str]:
    """Try to create the app, returning (ok, message)."""
    try:
        import app as app_package

        importlib.reload(app_package)
        from app.config import ProductionConfig

        app_package.create_app("production")
        return True, ""
    except Exception as error:  # noqa: BLE001 - the message is the point
        return False, f"{type(error).__name__}: {error}"


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        failures.append(label)


print("=== 1. weak configuration must be refused ===")
for key, value in {
    "SECRET_KEY": "short",
    "JWT_SECRET_KEY": "short",
    "DATABASE_URL": "postgresql+psycopg2://u:p@h/db",
    "CORS_ORIGINS": "https://ems.example.com",
    "JWT_COOKIE_SECURE": "true",
}.items():
    os.environ[key] = value
ok, message = boot("weak")
check("short secrets are refused", not ok, "app booted despite weak secrets")
check("the error explains why", "SECRET_KEY" in message or "JWT_SECRET_KEY" in message,
      message[:200])

print("\n=== 2. non-postgres database must be refused ===")
os.environ["SECRET_KEY"] = "a" * 48
os.environ["JWT_SECRET_KEY"] = "b" * 48
os.environ["DATABASE_URL"] = "sqlite:///local.db"
ok, message = boot("sqlite")
check("a non-Postgres database is refused", not ok, "app booted on SQLite")
check("the error names the database", "DATABASE_URL" in message, message[:200])

print("\n=== 3. wildcard CORS must be refused ===")
os.environ["DATABASE_URL"] = "postgresql+psycopg2://u:p@127.0.0.1:5432/db"
os.environ["CORS_ORIGINS"] = "*"
ok, message = boot("cors")
check("a wildcard CORS origin is refused", not ok, "app booted with CORS *")

print("\n=== 4. valid production configuration must boot ===")
os.environ["CORS_ORIGINS"] = "https://ems.example.com"
os.environ["JWT_COOKIE_SECURE"] = "true"
os.environ["RATELIMIT_STORAGE_URI"] = "memory://"
try:
    from app.config import ProductionConfig

    ProductionConfig.validate()
    check("validate() passes with strong settings", True)
except Exception as error:  # noqa: BLE001
    check("validate() passes with strong settings", False, str(error))

print("\n=== 4b. a bare managed-platform DATABASE_URL is accepted ===")
# Railway hands over ``postgresql://...`` with no driver. SQLAlchemy would
# resolve that to psycopg 3, which is not installed, so the config must
# normalise it onto psycopg2 rather than pass it through.
from app.config import normalise_database_url  # noqa: E402

for raw, note in [
    ("postgresql://u:p@h:5432/db", "Railway's bare form"),
    ("postgres://u:p@h:5432/db", "Heroku's legacy form"),
]:
    os.environ["DATABASE_URL"] = raw
    accepted = True
    detail = ""
    try:
        ProductionConfig.validate()
    except Exception as error:  # noqa: BLE001
        accepted = False
        detail = str(error)
    normalised = normalise_database_url(raw)
    check(
        f"{note} is accepted and pinned to psycopg2",
        accepted and normalised.startswith("postgresql+psycopg2://"),
        detail or normalised[:50],
    )
os.environ["DATABASE_URL"] = "postgresql+psycopg2://u:p@127.0.0.1:5432/db"

print("\n=== 5. production hardening (JWT cookies, not Flask sessions) ===")
from app.config import ProductionConfig

# The session is carried by JWT cookies, so these are the settings that matter:
# they are what stop the token being sniffed or sent cross-site.
check("auth cookies are secure-only", ProductionConfig.JWT_COOKIE_SECURE is True,
      str(ProductionConfig.JWT_COOKIE_SECURE))
check("auth cookies are SameSite=Strict",
      ProductionConfig.JWT_COOKIE_SAMESITE == "Strict",
      str(ProductionConfig.JWT_COOKIE_SAMESITE))
check("CSRF protection is on", ProductionConfig.JWT_COOKIE_CSRF_PROTECT is True)
check("debug is off", ProductionConfig.DEBUG is False)
check("testing mode is off", ProductionConfig.TESTING is False)
check("rate limiting is not in memory",
      not str(ProductionConfig.RATELIMIT_STORAGE_URI).startswith("memory"),
      ProductionConfig.RATELIMIT_STORAGE_URI)
check("CORS origins are explicit", "*" not in ProductionConfig.CORS_ORIGINS,
      str(ProductionConfig.CORS_ORIGINS))

print(f"\n=== RESULT: {len(failures)} failure(s) ===")
for failure in failures:
    print("  -", failure)
sys.exit(1 if failures else 0)
