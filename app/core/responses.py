"""Response envelope helpers and content negotiation.

Every JSON response leaves through here so the shapes stay consistent::

    {"data": ..., "meta": {...}}          success
    {"error": "...", "message": "..."}    failure
"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Response, jsonify, request


def wants_json() -> bool:
    """True when the caller should receive JSON rather than HTML."""
    if request.path.startswith("/api/") or request.path.startswith("/hooks/"):
        return True
    if request.is_json:
        return True
    if request.headers.get("HX-Request") == "true":
        # HTMX swaps HTML fragments unless explicitly asked for JSON.
        return request.headers.get("Accept", "").startswith("application/json")
    accept = request.accept_mimetypes
    if accept["application/json"] >= accept["text/html"]:
        return accept["application/json"] > 0
    return False


def ok(data: Any = None, *, meta: Mapping[str, Any] | None = None, status: int = 200):
    """Success envelope."""
    payload: dict[str, Any] = {"data": data}
    if meta is not None:
        payload["meta"] = dict(meta)
    return jsonify(payload), status


def created(data: Any = None, *, meta: Mapping[str, Any] | None = None):
    """201 success envelope."""
    return ok(data, meta=meta, status=201)


def no_content():
    """204 with an empty body."""
    return Response(status=204)


def fail(
    message: str,
    *,
    status: int = 400,
    error: str = "bad_request",
    details: Any = None,
):
    """Failure envelope."""
    payload: dict[str, Any] = {"error": error, "message": message}
    if details is not None:
        payload["details"] = details
    return jsonify(payload), status


def paginated(items: list[Any], page_info: Mapping[str, Any]):
    """Success envelope carrying pagination metadata."""
    return ok(items, meta=dict(page_info))
