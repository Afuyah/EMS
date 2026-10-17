"""Helpers shared by the HTML view blueprints.

Views are HTTP glue only: they call feature **services** (never repositories or
models) and own the transaction boundary, committing on success and rolling
back on failure.
"""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import (
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_jwt_extended import (
    get_jwt_identity,
    verify_jwt_in_request,
)

from app.core.permissions import Principal, current_principal
from app.features.auth.constants import Role


def is_htmx_request() -> bool:
    """Whether this request came from HTMX rather than a page navigation."""
    return request.headers.get("HX-Request") == "true"


def sign_in_again_response():
    """Send an expired caller back to sign-in, cleanly.

    A plain ``302`` is wrong for HTMX: it follows redirects transparently, so it
    would fetch the sign-in **page** and swap that HTML into whatever fragment
    was refreshing. With a live board, a punch stream and a notification bell all
    polling, the user ends up with a stack of sign-in pages nested inside the
    page they were on.

    ``HX-Redirect`` tells HTMX to perform a real navigation instead of a swap, so
    exactly one clean sign-in page appears. Ordinary navigations keep the plain
    redirect, and the ``next`` parameter still round-trips.

    Note the ``next`` value differs by caller. For a real navigation the current
    path *is* the page the user wanted, so it is worth returning to. For an HTMX
    poll it is a fragment URL — frequently polled, never a destination — so
    sending it would drop the user on a bare partial after signing in. HTMX
    callers therefore get a plain sign-in URL, and their dashboard is the landing
    page instead.
    """
    if is_htmx_request():
        # 200 with HX-Redirect: HTMX acts on the header and never swaps. A
        # non-2xx status would take the error path instead.
        response = current_app.response_class("", status=200, mimetype="text/plain")
        response.headers["HX-Redirect"] = url_for("views.login")
        return response

    return redirect(url_for("views.login", next=request.full_path))


def view_login_required(view: Callable | None = None) -> Callable:
    """Require a valid access token, redirecting browsers to the login page.

    Usable bare (``@view_login_required``) or called
    (``@view_login_required()``) so both styles read naturally at the call site.
    """

    def decorator(inner: Callable) -> Callable:
        @wraps(inner)
        def wrapper(*args, **kwargs):
            try:
                verify_jwt_in_request()
            except Exception:  # noqa: BLE001 - any failure means "not logged in"
                return sign_in_again_response()
            return inner(*args, **kwargs)

        return wrapper

    if view is None:
        return decorator
    return decorator(view)


def view_roles_required(*roles: str | Role) -> Callable:
    """Require one of ``roles``; HTML callers get a 403 page."""

    def decorator(view: Callable) -> Callable:
        @wraps(view)
        def wrapper(*args, **kwargs):
            principal = current_principal()
            if not principal.has_role(*roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def principal() -> Principal:
    """Shorthand for the current request's principal."""
    return current_principal()


def current_user_id() -> int | None:
    """The authenticated user id as an int, or ``None``."""
    identity = get_jwt_identity()
    if identity is None:
        return None
    try:
        return int(identity)
    except (TypeError, ValueError):
        return None


def is_signed_in() -> bool:
    """Whether the request carries a usable signed-in principal.

    Used by the entry-point redirects, which must send a returning user to their
    dashboard and a visitor to the sign-in page. Reads the principal the
    ``before_request`` hook already resolved, so it costs no extra lookup.
    """
    try:
        return getattr(g, "current_principal", None) is not None
    except RuntimeError:  # no application context (should not happen in a view)
        return False


def render(template: str, **context: Any):
    """Render a template with the common layout context pre-filled."""
    context.setdefault("current_user", getattr(g, "current_principal", None))
    context.setdefault("active_nav", "")
    return render_template(template, **context)


def flash_success(message: str) -> None:
    flash(message, "success")


def flash_error(message: str) -> None:
    flash(message, "error")


def flash_info(message: str) -> None:
    flash(message, "info")


def form_bool(name: str, default: bool = False) -> bool:
    """Read a checkbox/boolean form field."""
    raw = request.form.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def form_int(name: str, default: int | None = None) -> int | None:
    """Read an optional integer form field."""
    raw = request.form.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def form_str(name: str, default: str | None = None) -> str | None:
    """Read an optional trimmed string form field."""
    raw = request.form.get(name)
    if raw is None:
        return default
    value = str(raw).strip()
    return value or default


def form_checkbox(name: str) -> bool:
    """Read a checkbox using standard HTML semantics.

    An unticked checkbox submits *nothing*, so absence means ``False`` — never
    "fall back to the default". Treating absence as a default silently flips a
    user's explicit choice (e.g. creating a rule left disabled).
    """
    raw = request.form.get(name)
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def go(endpoint: str, **values: Any):
    """Redirect to an endpoint, preserving flash behaviour."""
    return redirect(url_for(endpoint, **values))


def wants_partial() -> bool:
    """True when HTMX issued the request and expects a fragment."""
    return request.headers.get("HX-Request") == "true"


def commit_session() -> None:
    """Commit the unit of work; used by every mutating view."""
    from app.core.database import db

    db.session.commit()


def rollback_session() -> None:
    from app.core.database import db

    db.session.rollback()
