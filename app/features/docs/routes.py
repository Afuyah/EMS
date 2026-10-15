"""OpenAPI 3.0 description of the EMS JSON API and the Swagger UI page.

The document is assembled from a declarative route table rather than by
introspecting Marshmallow schemas at import time, which keeps app startup free
of any documentation cost.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template

docs_bp = Blueprint("docs", __name__, url_prefix="/docs")

API_TITLE = "Employee Management System API"
API_VERSION = "1.0.0"
API_DESCRIPTION = (
    "JSON API for the EMS platform. Authenticate with "
    "`POST /api/v1/auth/login` and send the returned access token as "
    "`Authorization: Bearer <token>`. Browser clients receive the same tokens "
    "as HTTP-only cookies. Biometric devices post to `/hooks/biometrics/punch` "
    "authenticated with `X-Device-SN` and `X-Device-Secret` headers."
)

#: (method, path, summary, tag, auth-required)
ROUTES: tuple[tuple[str, str, str, str, bool], ...] = (
    # --- auth ---
    ("post", "/auth/login", "Exchange credentials for a token pair", "Auth", False),
    ("post", "/auth/refresh", "Rotate the refresh token", "Auth", True),
    ("post", "/auth/logout", "Revoke the presented refresh token", "Auth", True),
    ("post", "/auth/logout_all", "Revoke every session for the caller", "Auth", True),
    ("get", "/auth/me", "The authenticated caller's profile", "Auth", True),
    ("post", "/auth/users", "Create a login account (Admin)", "Auth", True),
    ("post", "/auth/users/{user_id}/active/{state}", "Activate or deactivate a login (Admin)", "Auth", True),
    ("post", "/auth/users/{user_id}/password", "Reset a user's password (Admin)", "Auth", True),
    # --- employees ---
    ("get", "/employees", "Paged, filtered employee list", "Employees", True),
    ("post", "/employees", "Create an employee (HR/Admin)", "Employees", True),
    ("get", "/employees/{employee_id}", "One employee", "Employees", True),
    ("patch", "/employees/{employee_id}", "Partially update an employee (HR/Admin)", "Employees", True),
    ("delete", "/employees/{employee_id}", "Delete an employee without a login (Admin)", "Employees", True),
    ("post", "/employees/{employee_id}/active/{state}", "Activate/deactivate an employee (HR/Admin)", "Employees", True),
    ("get", "/departments", "List departments with headcounts", "Employees", True),
    ("post", "/departments", "Create a department (HR/Admin)", "Employees", True),
    ("patch", "/departments/{department_id}", "Update a department (HR/Admin)", "Employees", True),
    ("delete", "/departments/{department_id}", "Delete an empty department (Admin)", "Employees", True),
    ("get", "/positions", "List positions", "Employees", True),
    ("post", "/positions", "Create a position (HR/Admin)", "Employees", True),
    ("patch", "/positions/{position_id}", "Update a position (HR/Admin)", "Employees", True),
    ("delete", "/positions/{position_id}", "Delete an unoccupied position (Admin)", "Employees", True),
    # --- attendance ---
    ("get", "/attendance/employees/{employee_id}", "Attendance history for one employee", "Attendance", True),
    ("get", "/attendance/board", "Attendance board for one day", "Attendance", True),
    ("post", "/attendance/{attendance_id}/correct", "Correct an attendance row (HR/Admin)", "Attendance", True),
    ("get", "/attendance/policies", "List shift policies", "Attendance", True),
    ("post", "/attendance/policies", "Create or update a shift policy (Admin)", "Attendance", True),
    # --- leave ---
    ("post", "/leave/request", "Request leave (returns suggested windows)", "Leave", True),
    ("get", "/leave/requests", "Leave queue scoped to the caller's role", "Leave", True),
    ("get", "/leave/requests/{request_id}", "One request with its history", "Leave", True),
    ("post", "/leave/requests/{request_id}/respond", "Accept or deny a suggested window", "Leave", True),
    ("post", "/leave/requests/{request_id}/decide", "Approve or deny (HR/Admin)", "Leave", True),
    ("post", "/leave/requests/{request_id}/cancel", "Cancel a request", "Leave", True),
    ("get", "/leave/balances/{employee_id}", "Balances for an employee and year", "Leave", True),
    ("post", "/leave/balances/adjust", "Adjust a balance (HR/Admin)", "Leave", True),
    ("post", "/leave/balances/allocate", "Allocate year-start entitlements (Admin)", "Leave", True),
    ("get", "/leave/rules", "Configured leave rules", "Leave", True),
    # --- notifications ---
    ("get", "/notifications", "The caller's notification inbox", "Notifications", True),
    ("get", "/notifications/unread_count", "Unread badge count", "Notifications", True),
    ("post", "/notifications/{notification_id}/read", "Mark one notification read", "Notifications", True),
    ("post", "/notifications/read_all", "Mark every notification read", "Notifications", True),
    ("post", "/notifications", "Queue a notification (Admin)", "Notifications", True),
    # --- reports ---
    ("get", "/reports", "Headline workforce counts", "Reports", True),
    ("get", "/reports/attendance/colors", "Attendance colour distribution", "Reports", True),
    ("get", "/reports/attendance/department/{department_id}", "Department attendance summary", "Reports", True),
    ("get", "/reports/attendance/headcount", "Daily headcount", "Reports", True),
    ("get", "/reports/attendance/top_late", "Top late employees", "Reports", True),
    ("get", "/reports/leave/utilization", "Leave utilisation by type", "Reports", True),
    ("get", "/reports/leave/calendar/{department_id}", "Department leave calendar", "Reports", True),
    ("get", "/reports/leave/balances/{department_id}", "Balance ledger", "Reports", True),
    ("get", "/reports/leave/requests", "Leave request counts by status (HR/Admin)", "Reports", True),
    # --- exports ---
    ("get", "/exports/attendance/department/{department_id}.csv", "Attendance CSV for a department", "Exports", True),
    ("get", "/exports/attendance/summary.csv", "Attendance CSV across departments", "Exports", True),
    ("get", "/exports/attendance/headcount.csv", "Headcount CSV", "Exports", True),
    ("get", "/exports/attendance/top_late.csv", "Top late employees CSV", "Exports", True),
    ("get", "/exports/leave/utilization.csv", "Leave utilisation CSV", "Exports", True),
    ("get", "/exports/leave/balances/{department_id}.csv", "Balance ledger CSV", "Exports", True),
    ("get", "/exports/leave/balances/template.csv", "Bulk-import template", "Exports", True),
    ("post", "/exports/leave/balances/import", "Bulk-import balances from CSV (HR/Admin)", "Exports", True),
    # --- devices ---
    ("get", "/devices", "List registered devices (Admin)", "Devices", True),
    ("post", "/devices", "Register a device (Admin)", "Devices", True),
    ("post", "/devices/{device_id}/rotate", "Rotate a device secret (Admin)", "Devices", True),
    ("get", "/devices/map", "Device-to-employee mappings (Admin)", "Devices", True),
    ("post", "/devices/map", "Map a device user to an employee (Admin)", "Devices", True),
    ("get", "/devices/raw-logs", "Recent raw punch rows (Admin)", "Devices", True),
)

TAGS = (
    ("Auth", "Authentication, sessions and user administration"),
    ("Employees", "Departments, positions and the employee lifecycle"),
    ("Attendance", "Daily attendance, shift policies and corrections"),
    ("Leave", "Leave requests, the suggestion engine and balances"),
    ("Notifications", "Inbox and delivery queue"),
    ("Reports", "Aggregated reporting endpoints"),
    ("Exports", "CSV downloads and bulk import"),
    ("Devices", "Biometric device management"),
)


def build_openapi_spec(*, server_url: str = "/api/v1") -> dict:
    """Assemble the OpenAPI document."""
    paths: dict[str, dict] = {}

    for method, path, summary, tag, secured in ROUTES:
        operation: dict = {
            "summary": summary,
            "tags": [tag],
            "responses": {
                "200": {"description": "Success"},
                "400": {"description": "Validation error"},
                "401": {"description": "Authentication required"},
                "403": {"description": "Insufficient role"},
                "404": {"description": "Not found"},
            },
        }
        if method in {"post", "patch"}:
            operation["requestBody"] = {
                "content": {
                    "application/json": {"schema": {"type": "object"}}
                }
            }
            operation["responses"]["201"] = {"description": "Created"}

        # Path parameters are derived from the placeholder names.
        parameters = [
            {
                "name": token.strip("{}"),
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
            for token in path.split("/")
            if token.startswith("{") and token.endswith("}")
        ]
        if parameters:
            operation["parameters"] = parameters

        if secured:
            operation["security"] = [{"bearerAuth": []}]

        paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": API_TITLE,
            "version": API_VERSION,
            "description": API_DESCRIPTION,
        },
        "servers": [{"url": server_url}],
        "tags": [{"name": name, "description": description} for name, description in TAGS],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "access_token_cookie"},
            },
            "schemas": {
                "Envelope": {
                    "type": "object",
                    "properties": {"data": {}, "meta": {"type": "object"}},
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {"type": "string"},
                        "message": {"type": "string"},
                        "details": {},
                    },
                },
            },
        },
        "paths": paths,
    }


@docs_bp.get("/openapi.json")
def openapi_json():
    """The generated OpenAPI document."""
    return jsonify(build_openapi_spec())


@docs_bp.get("/ui")
def swagger_ui():
    """Swagger UI rendering the document above."""
    return render_template("docs/swagger.html")


@docs_bp.get("")
@docs_bp.get("/")
def docs_index():
    """Small landing page linking to the UI and the raw document."""
    return render_template("docs/index.html", route_count=len(ROUTES))
