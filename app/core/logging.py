"""Structured logging configuration."""

from __future__ import annotations

import logging
import sys

from flask import Flask, g, has_request_context, request

from app.core.time import utcnow

_CONFIGURED = False


class RequestContextFilter(logging.Filter):
    """Attach request-scoped identifiers to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(g, "request_id", "-") if has_request_context() else "-"
        record.path = request.path if has_request_context() else "-"
        record.method = request.method if has_request_context() else "-"
        record.user_id = (
            getattr(getattr(g, "current_principal", None), "user_id", "-")
            if has_request_context()
            else "-"
        )
        return True


def configure_logging(app: Flask | None = None) -> None:
    """Install a single stdout handler with a stable, greppable format."""
    global _CONFIGURED

    level_name = "DEBUG" if app is not None and app.debug else "INFO"
    level = getattr(logging, level_name, logging.INFO)

    if _CONFIGURED:
        logging.getLogger().setLevel(level)
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s [%(request_id)s] "
            "%(name)s %(method)s %(path)s user=%(user_id)s :: %(message)s"
        )
    )
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # SQLAlchemy's echo logger is noisy and never wanted in normal runs.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.INFO)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Feature-scoped logger."""
    return logging.getLogger(name)


def log_exception(logger: logging.Logger, message: str, **fields) -> None:
    """Log an exception with structured extras, never raising."""
    if fields:
        rendered = " ".join(f"{key}={value!r}" for key, value in fields.items())
        logger.exception("%s | %s | at=%s", message, rendered, utcnow().isoformat())
    else:
        logger.exception("%s | at=%s", message, utcnow().isoformat())
