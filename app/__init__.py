"""Application factory.

Wiring order matters: configuration, extensions, models (so SQLAlchemy knows
every table), error handling, request hooks, then feature blueprints.
"""

from __future__ import annotations

import logging

from flask import Flask, g, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
from flask_jwt_extended import verify_jwt_in_request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate

from app.config import get_config
from app.core.database import db
from app.core.error_handlers import register_error_handlers
from app.core.logging import configure_logging, get_logger
from app.core.permissions import load_principal, set_current_principal
from app.core.responses import wants_json
from app.core.security import new_csrf_token

logger = get_logger(__name__)

migrate = Migrate()
limiter = Limiter(key_func=get_remote_address)
cors = CORS()


def create_app(config_name: str | None = None, **overrides) -> Flask:
    """Build a fully wired Flask application."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    config = get_config(config_name)
    app.config.from_object(config)
    if overrides:
        app.config.update(overrides)

    configure_logging(app)
    logger.info("config=%s debug=%s", config.__name__, app.debug)

    _apply_proxy_fix(app)
    _init_extensions(app)
    _init_models()
    register_error_handlers(app)
    _register_jwt(app)
    _register_request_hooks(app)
    _register_blueprints(app)
    _register_template_globals(app)
    _register_cli(app)

    if app.config.get("TESTING"):
        # Keep test runs quiet and deterministic.
        logging.getLogger("app").setLevel(logging.WARNING)

    return app


def _apply_proxy_fix(app: Flask) -> None:
    """Trust the reverse proxy's forwarding headers.

    Railway (like any PaaS) terminates TLS and forwards the request, so without
    this every request appears to come from the proxy's address.  That breaks
    rate limiting — ``get_remote_address`` would see one client for the whole
    world, turning per-user limits into a single global one — and makes
    ``url_for(_external=True)`` emit ``http://`` behind an HTTPS site.

    ``x_for=1`` trusts exactly one hop, which is what a managed proxy provides;
    trusting more would let a client spoof its own address.
    """
    if not app.config.get("TRUST_PROXY_HEADERS", True):
        return
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
    )
    logger.debug("ProxyFix applied (x_for=1, x_proto=1)")


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    migrate.init_app(app, db)

    origins = app.config.get("CORS_ORIGINS", "*")
    if isinstance(origins, str):
        origins = "*" if origins.strip() == "*" else [o.strip() for o in origins.split(",")]
    cors.init_app(
        app,
        resources={r"/api/*": {"origins": origins}, r"/hooks/*": {"origins": "*"}},
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization", "X-CSRF-TOKEN",
                       "X-Device-SN", "X-Device-Secret", "X-Device-Signature"],
        expose_headers=["X-Request-Id"],
    )
    limiter.init_app(app)
    limiter.enabled = app.config.get("RATELIMIT_ENABLED", True) is not False
    limiter.default_limits = [app.config.get("RATELIMIT_DEFAULT", "600 per minute")]

    from flask_jwt_extended import JWTManager

    # Stashed on the app so feature modules can decorate the same instance.
    app.extensions["ems_jwt_manager"] = JWTManager(app)


def _init_models() -> None:
    """Import every feature's models so ``db.metadata`` is complete."""
    from app.features.attendance import models as attendance_models  # noqa: F401
    from app.features.auth import models as auth_models  # noqa: F401
    from app.features.biometrics import models as biometrics_models  # noqa: F401
    from app.features.email import models as email_models  # noqa: F401
    from app.features.employees import models as employees_models  # noqa: F401
    from app.features.leave import models as leave_models  # noqa: F401
    from app.features.notifications import models as notifications_models  # noqa: F401
    from app.features.rules import models as rules_models  # noqa: F401


def _register_jwt(app: Flask) -> None:
    """Attach the auth-owned JWT callbacks to this app's manager.

    ``core`` must not import feature models, so the callbacks that read
    ``refresh_tokens`` live with the auth feature.
    """
    from app.features.auth.jwt_callbacks import register_jwt_callbacks

    register_jwt_callbacks(app)


def _register_request_hooks(app: Flask) -> None:
    @app.before_request
    def _identify_principal():
        """Populate ``g.current_principal`` when a valid JWT is present."""
        g.request_id = request.headers.get("X-Request-Id") or "-"
        g.current_principal = None

        # Device webhooks authenticate with their own headers, and the docs
        # and health endpoints stay open.
        path = request.path
        if (
            path.startswith("/hooks/")
            # ADMS devices authenticate with their CommKey in the body, not with
            # a JWT, and cannot send an Authorization header at all.
            or path.startswith("/iclock/")
            or path.startswith("/api/docs")
            or path in {"/api/v1/health", "/health", "/app/health"}
            or path.startswith("/static/")
        ):
            return None

        try:
            verify_jwt_in_request(optional=True)
        except Exception:  # noqa: BLE001 - an invalid token simply means anonymous
            g.current_principal = None
            return None

        from flask_jwt_extended import get_jwt

        try:
            claims = get_jwt()
        except RuntimeError:
            return None
        if claims:
            g.current_principal = load_principal(claims)
        return None

    @app.after_request
    def _tag_response(response):
        if getattr(g, "request_id", "-") != "-":
            response.headers.setdefault("X-Request-Id", g.request_id)
        return response


def _register_blueprints(app: Flask) -> None:
    """Attach the JSON API, the Swagger UI, the HTML views and the webhooks."""
    from app.features.api import api_v1
    from app.features.docs.routes import docs_bp
    from app.views.blueprint import views_bp
    from app.features.biometrics.blueprint import hooks_bp
    from app.features.biometrics.iclock import iclock_bp

    # /api/v1 is the versioned API; /api/docs is its sibling.
    app.register_blueprint(api_v1, url_prefix="/api/v1")
    app.register_blueprint(docs_bp, url_prefix="/api/docs")
    app.register_blueprint(views_bp)
    app.register_blueprint(hooks_bp)
    # The ZKTeco ADMS protocol the physical terminals speak.
    app.register_blueprint(iclock_bp)

    @app.get("/api/v1/health")
    @app.get("/health")
    def _health():
        from sqlalchemy import text

        database_ok = True
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - health must never raise
            database_ok = False
            db.session.rollback()
        payload = {
            "status": "ok" if database_ok else "degraded",
            "database": "up" if database_ok else "down",
            "version": "1.0.0",
        }
        status = 200 if database_ok else 503
        if wants_json():
            return jsonify(payload), status
        return jsonify(payload), status


def _register_template_globals(app: Flask) -> None:
    """Expose the CSRF token to every template as a ready-to-use string.

    In production ``JWT_COOKIE_CSRF_PROTECT`` is on, so Flask-JWT-Extended
    requires the access cookie and its matching CSRF value to arrive together on
    every write. The templates emit that value into an ``X-CSRF-TOKEN`` header
    for HTMX, so it must be a **value**, not a callable: the templates use
    ``{{ csrf_token or '' }}`` rather than ``{{ csrf_token() }}``.

    Registering this as a Jinja *global* would expose the function itself and
    render its repr into the header, so it is a context processor instead.

    Without it the header renders empty. Every POST then fails CSRF
    verification with a 401 and the user is bounced to the sign-in page — which
    looks exactly like being logged out on every action.
    """
    from app.features.auth.jwt_callbacks import current_csrf_token

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": current_csrf_token()}


def _register_cli(app: Flask) -> None:
    try:
        from app.cli import register_cli
    except ImportError:  # pragma: no cover - during incremental bring-up
        return
    register_cli(app)
