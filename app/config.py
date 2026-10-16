"""Application configuration objects.

The factory reads ``FLASK_CONFIG`` (default ``development``) and instantiates
the matching class from :data:`config_by_name`.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root before anything reads os.environ.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


#: The DBAPI this project installs. ``psycopg2-binary`` provides the
#: ``psycopg2`` driver; psycopg 3 is a different package.
POSTGRES_DRIVER = "psycopg2"


def normalise_database_url(url: str | None) -> str | None:
    """Force the PostgreSQL driver to the one we actually install.

    Managed platforms hand you a bare URL like ``postgresql://...`` (Railway)
    or the legacy ``postgres://...`` (Heroku). SQLAlchemy 2.x resolves a bare
    ``postgresql://`` to **psycopg 3**, which this project does not depend on,
    so the app dies at import with ``ModuleNotFoundError: No module named
    'psycopg'``.

    Pinning the driver makes any of these forms work:

        postgres://...              -> postgresql+psycopg2://...
        postgresql://...            -> postgresql+psycopg2://...
        postgresql+psycopg2://...   -> unchanged
        postgresql+psycopg://...    -> unchanged (explicitly asked for)

    Non-PostgreSQL URLs (SQLite in tests) are returned untouched.
    """
    if not url:
        return url

    text = url.strip()
    for prefix in ("postgres://", "postgresql://"):
        if text.startswith(prefix):
            return f"postgresql+{POSTGRES_DRIVER}://" + text[len(prefix):]
    return text


class BaseConfig:
    """Settings shared by every environment."""

    # --- Flask ---
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
    JSON_SORT_KEYS = False
    PROPAGATE_EXCEPTIONS = False
    TEMPLATES_AUTO_RELOAD = False

    # --- SQLAlchemy ---
    # Normalised so a bare ``postgresql://`` from a managed platform still uses
    # the psycopg2 driver this project ships.
    SQLALCHEMY_DATABASE_URI = normalise_database_url(
        os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/ems_dev",
        )
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
        "pool_size": 5,
        "max_overflow": 10,
        # Timestamps are stored in UTC; keep the session in UTC too so values
        # read back are not silently rendered in the server's local offset.
        "connect_args": {"options": "-c timezone=UTC"},
    }

    # --- JWT ---
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-jwt-secret-key-change-me-32bytes")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    JWT_TOKEN_LOCATION = ["headers", "cookies"]
    JWT_COOKIE_SECURE = _env_bool("JWT_COOKIE_SECURE", False)
    JWT_COOKIE_SAMESITE = "Lax"
    JWT_COOKIE_CSRF_PROTECT = False
    JWT_CSRF_IN_COOKIES = True
    JWT_ACCESS_COOKIE_PATH = "/"
    JWT_REFRESH_COOKIE_PATH = "/"
    JWT_ACCESS_COOKIE_NAME = "access_token_cookie"
    JWT_REFRESH_COOKIE_NAME = "refresh_token_cookie"
    JWT_ERROR_MESSAGE_KEY = "message"

    # --- CORS / limits ---
    CORS_ORIGINS = "*"
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_DEFAULT = "600 per minute"
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_ENABLED = True

    # --- Celery ---
    CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    CELERY_RESULT_BACKEND = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/1")
    CELERY_TASK_ALWAYS_EAGER = False

    # --- Reverse proxy ---
    #: Trust ``X-Forwarded-*`` from the platform proxy.  Keep True on any PaaS
    #: (Railway, Render, Fly): without it every request looks like it comes from
    #: the proxy, so rate limiting collapses into one global limit.
    TRUST_PROXY_HEADERS = _env_bool("TRUST_PROXY_HEADERS", True)

    # --- Domain defaults ---
    DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Africa/Nairobi")
    ATTENDANCE_LOOKBACK_DAYS = 120
    LEAVE_SUGGESTION_HORIZON_DAYS = 120
    AUTH_REQUIRED = True

    # --- Mail ---
    SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@ems.local")
    SMTP_TLS = _env_bool("SMTP_TLS", False)
    SMTP_SSL = _env_bool("SMTP_SSL", False)
    SMTP_TIMEOUT = int(os.getenv("SMTP_TIMEOUT", "20"))
    SMS_ENABLED = _env_bool("SMS_ENABLED", False)

    # --- Outbound email ---
    # console : render the message to the log and mark it sent (no SMTP needed)
    # smtp    : deliver for real over SMTP
    # null    : record without delivering (tests)
    EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "console")
    EMAIL_ENABLED = _env_bool("EMAIL_ENABLED", True)
    EMAIL_SUBJECT_PREFIX = os.getenv("EMAIL_SUBJECT_PREFIX", "[EMS]")
    EMAIL_MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_ATTEMPTS", "3"))
    #: Base address used to build links inside email bodies.
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000")
    #: Organisation name shown to users and in email signatures.
    APP_NAME = os.getenv("APP_NAME", "Employee Management System")
    #: Where employees go to have their fingerprint registered.
    HR_CONTACT = os.getenv("HR_CONTACT", "the HR office")

    # --- Misc ---
    PAGINATION_DEFAULT_SIZE = 25
    PAGINATION_MAX_SIZE = 200


class DevelopmentConfig(BaseConfig):
    """Local development: CSRF off, in-memory limiter, no real mail."""

    DEBUG = True
    TESTING = False
    JWT_COOKIE_CSRF_PROTECT = False
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
    # Slightly longer TTLs make manual poking at the UI less annoying.
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=60)
    SQLALCHEMY_DATABASE_URI = normalise_database_url(
        os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/ems_dev",
        )
    )


class TestingConfig(BaseConfig):
    """pytest: memory limiter, no SMTP, no SMS, fresh schema per session."""

    DEBUG = False
    TESTING = True
    JWT_COOKIE_CSRF_PROTECT = False
    RATELIMIT_ENABLED = False
    RATELIMIT_STORAGE_URI = "memory://"
    SMTP_HOST = None
    SMS_ENABLED = False
    AUTH_REQUIRED = True
    CELERY_TASK_ALWAYS_EAGER = True
    SQLALCHEMY_DATABASE_URI = normalise_database_url(
        os.getenv(
            "TEST_DATABASE_URL",
            "postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/ems_test",
        )
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "connect_args": {"options": "-c timezone=UTC"},
    }


class ProductionConfig(BaseConfig):
    """Production: CSRF on, strict environment validation."""

    DEBUG = False
    TESTING = False
    JWT_COOKIE_CSRF_PROTECT = True
    # The HTML interface is ordinary server-rendered forms, which cannot send a
    # custom header. Accept the token as a form field as well, so the hidden
    # input the templates inject is actually honoured. Without this, every
    # non-HTMX POST would fail verification and bounce the user to sign-in.
    JWT_CSRF_CHECK_FORM = True
    JWT_COOKIE_SECURE = _env_bool("JWT_COOKIE_SECURE", True)
    JWT_COOKIE_SAMESITE = "Strict"
    CORS_ORIGINS = _env_list("CORS_ORIGINS")
    SMTP_TLS = _env_bool("SMTP_TLS", True)
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "redis://127.0.0.1:6379/2")

    @classmethod
    def validate(cls) -> None:
        """Fail fast when a production-critical setting is missing/weak."""
        problems: list[str] = []

        secret = os.getenv("SECRET_KEY", "")
        if len(secret) < 32:
            problems.append("SECRET_KEY must be set to at least 32 characters")

        jwt_secret = os.getenv("JWT_SECRET_KEY", "")
        if len(jwt_secret) < 32:
            problems.append("JWT_SECRET_KEY must be set to at least 32 characters")

        database_url = os.getenv("DATABASE_URL", "")
        # Accept the bare forms managed platforms provide (``postgres://`` on
        # Heroku, ``postgresql://`` on Railway) as well as an explicit driver.
        if not database_url.startswith(("postgresql", "postgres://")):
            problems.append("DATABASE_URL must point at PostgreSQL")

        origins = _env_list("CORS_ORIGINS")
        if not origins:
            problems.append("CORS_ORIGINS must list explicit origins in production")
        elif "*" in origins:
            # A wildcard lets any site call the API with the browser's cookies,
            # so it is never acceptable on a public deployment.
            problems.append(
                "CORS_ORIGINS cannot contain '*' in production; list explicit origins"
            )

        if os.getenv("FLASK_CONFIG", "").strip().lower() == "production" and not _env_bool(
            "JWT_COOKIE_SECURE", True
        ):
            problems.append("JWT_COOKIE_SECURE cannot be disabled in production")

        if problems:
            raise RuntimeError(
                "Invalid production configuration:\n  - " + "\n  - ".join(problems)
            )


config_by_name: dict[str, type[BaseConfig]] = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}


def get_config(config_name: str | None = None) -> type[BaseConfig]:
    """Resolve a config class from an explicit name or ``FLASK_CONFIG``."""
    name = (config_name or os.getenv("FLASK_CONFIG") or "development").strip().lower()
    if name not in config_by_name:
        raise RuntimeError(
            f"Unknown FLASK_CONFIG {name!r}; expected one of {sorted(config_by_name)}"
        )
    config = config_by_name[name]
    if name == "production":
        config.validate()
    return config
