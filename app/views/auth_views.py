"""HTML auth views: login, logout and session refresh."""

from __future__ import annotations

from flask import (
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_jwt_extended import jwt_required

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.features.auth.controller import AuthController
from app.views.blueprint import views_bp

logger = get_logger(__name__)
_controller = AuthController()


def _safe_next(target: str | None) -> str:
    """Only allow same-site, absolute-path redirects."""
    if not target:
        return url_for("views.dashboard")
    if not target.startswith("/") or target.startswith("//"):
        return url_for("views.dashboard")
    return target


@views_bp.route("/login", methods=["GET", "POST"])
def login():
    """Render the login form and authenticate on POST."""
    if request.method == "GET":
        return render_template(
            "auth/login.html",
            next=request.args.get("next", ""),
            active_nav="",
        )

    payload = {
        "email": request.form.get("email", ""),
        "password": request.form.get("password", ""),
    }

    try:
        response, _status = _controller.login_response(payload)
    except AppError as error:
        logger.info("login failed for %s: %s", payload["email"], error.message)
        flash(error.message, "error")
        return (
            render_template(
                "auth/login.html",
                next=request.form.get("next", ""),
                email=payload["email"],
                active_nav="",
            ),
            401,
        )

    # Reuse the controller's JSON body but answer with a redirect, copying the
    # auth cookies the controller already set.
    destination = _safe_next(request.form.get("next"))

    # A password issued on the user's behalf (HR provisioning) is temporary, so
    # send them straight to their profile to replace it.  This is what makes
    # emailing a password safe: it is single-use in practice.
    if _must_change_password(payload["email"]):
        destination = url_for("views.profile")
        flash(
            "Please choose your own password before continuing. "
            "Your current one was issued for you and is temporary.",
            "info",
        )

    redirect_response = redirect(destination)
    for header, value in response.headers:
        if header.lower() == "set-cookie":
            redirect_response.headers.add(header, value)

    return redirect_response


def _must_change_password(email: str) -> bool:
    """Whether this account still holds a password someone else chose."""
    from app.features.auth.service import AuthService

    try:
        user = AuthService().get_by_email(email)
    except Exception:  # noqa: BLE001 - a lookup failure must not block sign-in
        logger.warning("could not check must_change_password for %s", email)
        return False
    return bool(user is not None and user.must_change_password)


@views_bp.get("/logout")
def logout():
    """Revoke the current session and return to the login screen."""
    from app.features.auth.jwt_callbacks import clear_auth_cookies

    try:
        response, _status = _controller.logout(all_sessions=False)
    except AppError:
        response = current_app.response_class("")

    redirect_response = redirect(url_for("views.login"))
    for header, value in response.headers:
        if header.lower() == "set-cookie":
            redirect_response.headers.add(header, value)
    clear_auth_cookies(redirect_response)
    flash("You have been signed out.", "info")
    return redirect_response


@views_bp.post("/refresh-session")
@jwt_required(refresh=True)
def refresh_session():
    """HTMX/JS endpoint that rotates the session cookies."""
    return _controller.refresh()


@views_bp.get("/health")
def health():
    """Lightweight HTML-adjacent health probe."""
    from sqlalchemy import text

    from app.core.database import db

    database_ok = True
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - health must never raise
        database_ok = False
        db.session.rollback()

    return (
        {"status": "ok" if database_ok else "degraded", "database": "up" if database_ok else "down"},
        200 if database_ok else 503,
    )
