"""Central error handling.

Domain errors (:class:`~app.core.exceptions.AppError`) become JSON for API and
webhook callers and flashed redirects/messages for HTML callers.  Unexpected
errors are logged with a traceback and reported as a generic 500 so internals
never leak.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.core.exceptions import AppError, AuthenticationError
from app.core.logging import get_logger
from app.core.responses import wants_json

logger = get_logger(__name__)

#: Handled failures that are expected during normal operation.
_EXPECTED = (AppError,)


def _is_html_request() -> bool:
    return not wants_json()


def register_error_handlers(app: Flask) -> None:
    """Attach app-wide handlers."""

    @app.errorhandler(AppError)
    def handle_app_error(error: AppError):
        status = error.status_code
        if status >= 500:
            logger.error("domain error: %s", error.message, exc_info=error)
        else:
            logger.info("domain error %s: %s", error.error_code, error.message)

        if _is_html_request():
            flash_message = error.message
            if status == 401:
                # Views handle redirects themselves; re-raise for clarity.
                return render_template(
                    "errors/error.html",
                    code=status,
                    title="Authentication required",
                    message=flash_message,
                ), status
            return render_template(
                "errors/error.html",
                code=status,
                title=_title_for(status),
                message=flash_message,
            ), status

        return jsonify(error.to_dict()), status

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        status = error.code or 500
        message = error.description or "Request failed."
        if _is_html_request():
            return render_template(
                "errors/error.html",
                code=status,
                title=_title_for(status),
                message=message,
            ), status
        return (
            jsonify({"error": _slug_for(status), "message": message}),
            status,
        )

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        # Let HTTPException subclasses reach their own handler.
        if isinstance(error, HTTPException):
            return handle_http_error(error)

        logger.exception("unhandled exception on %s %s", request.method, request.path)

        detail = f"{type(error).__name__}: {error}" if app.debug else None
        if _is_html_request():
            return render_template(
                "errors/error.html",
                code=500,
                title="Something went wrong",
                message="An unexpected error occurred. The incident has been logged.",
                detail=detail,
            ), 500
        payload = {
            "error": "internal_error",
            "message": "An unexpected error occurred.",
        }
        if detail:
            payload["details"] = detail
        return jsonify(payload), 500


def _title_for(status: int) -> str:
    return {
        400: "Bad request",
        401: "Authentication required",
        403: "Not allowed",
        404: "Not found",
        405: "Method not allowed",
        409: "Conflict",
        422: "Unprocessable request",
        429: "Too many requests",
        500: "Something went wrong",
    }.get(status, "Request failed")


def _slug_for(status: int) -> str:
    return {
        400: "bad_request",
        401: "authentication_error",
        403: "authorization_error",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        422: "unprocessable",
        429: "rate_limited",
        500: "internal_error",
    }.get(status, "error")
