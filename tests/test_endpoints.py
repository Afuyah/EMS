"""Endpoint tests through the Flask test client."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.database import db

pytestmark = pytest.mark.endpoint


class TestAuthEndpoints:
    def test_login_returns_tokens_and_sets_cookies(self, client, admin_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "ChangeMe123!"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["tokens"]["access_token"]
        assert body["data"]["user"]["role"] == "ADMIN"
        cookies = " ".join(response.headers.getlist("Set-Cookie"))
        assert "access_token_cookie" in cookies

    def test_login_with_wrong_password_is_401(self, client, admin_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "nope"},
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "invalid_credentials"

    def test_unknown_email_gives_the_same_error(self, client, admin_user):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@ems.local", "password": "whatever"},
        )
        assert response.status_code == 401
        assert response.get_json()["error"] == "invalid_credentials"

    def test_login_validation_error(self, client):
        response = client.post("/api/v1/auth/login", json={"email": "not-an-email"})
        assert response.status_code == 400
        assert response.get_json()["error"] == "validation_error"

    def test_me_requires_a_token(self, client):
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_me_returns_the_profile(self, client, auth_headers):
        response = client.get("/api/v1/auth/me", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["data"]["email"] == "admin@ems.local"

    def test_refresh_rotates_and_invalidates_the_old_token(self, client, admin_user):
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "ChangeMe123!"},
        ).get_json()
        old_refresh = login["data"]["tokens"]["refresh_token"]
        old_access = login["data"]["tokens"]["access_token"]

        rotated = client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {old_refresh}"},
        )
        assert rotated.status_code == 200
        new_refresh = rotated.get_json()["data"]["tokens"]["refresh_token"]
        assert new_refresh != old_refresh

        # Replaying the rotated token is rejected.
        replay = client.post(
            "/api/v1/auth/refresh",
            headers={"Authorization": f"Bearer {old_refresh}"},
        )
        assert replay.status_code == 401

        # The access token from the revoked session is also rejected.
        stale = client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {old_access}"}
        )
        assert stale.status_code == 401

    def test_logout_revokes_the_session(self, client, admin_user):
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "ChangeMe123!"},
        ).get_json()
        refresh = login["data"]["tokens"]["refresh_token"]

        out = client.post(
            "/api/v1/auth/logout", headers={"Authorization": f"Bearer {refresh}"}
        )
        assert out.status_code == 200

        reused = client.post(
            "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {refresh}"}
        )
        assert reused.status_code == 401

    def test_non_admin_cannot_create_users(self, client, employee_headers):
        response = client.post(
            "/api/v1/auth/users",
            json={"email": "new@ems.local", "password": "Password123"},
            headers=employee_headers,
        )
        assert response.status_code == 403


class TestEmployeeEndpoints:
    def test_list_is_paginated_with_meta(self, client, auth_headers, employee):
        response = client.get("/api/v1/employees", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        assert "meta" in body
        assert body["meta"]["total"] >= 1
        assert body["data"][0]["employee_no"] == "EMP-0001"

    def test_get_one_employee(self, client, auth_headers, employee):
        response = client.get(
            f"/api/v1/employees/{employee.id}", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["full_name"] == "Alice Njeri"

    def test_missing_employee_is_404(self, client, auth_headers):
        response = client.get("/api/v1/employees/99999", headers=auth_headers)
        assert response.status_code == 404
        assert response.get_json()["error"] == "employee_not_found"

    def test_create_employee_auto_numbers(self, client, auth_headers, department):
        response = client.post(
            "/api/v1/employees",
            json={
                "first_name": "Brian",
                "last_name": "Otieno",
                "email": "brian@ems.local",
                "department_id": department.id,
            },
            headers=auth_headers,
        )
        assert response.status_code == 201
        assert response.get_json()["data"]["employee_no"] == "EMP-0001"

    def test_duplicate_email_is_409(self, client, auth_headers, employee, department):
        response = client.post(
            "/api/v1/employees",
            json={
                "first_name": "Copy",
                "last_name": "Cat",
                "email": "alice@ems.local",
                "department_id": department.id,
            },
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_position_from_another_department_is_rejected(
        self, client, auth_headers, department
    ):
        from app.features.employees.service import DepartmentService, PositionService

        other = DepartmentService().create_department(name="Finance", code="FIN")
        stray = PositionService().create_position(
            title="Accountant", department_id=other.id
        )
        db.session.commit()

        response = client.post(
            "/api/v1/employees",
            json={
                "first_name": "Mis",
                "last_name": "Matched",
                "email": "mismatch@ems.local",
                "department_id": department.id,
                "position_id": stray.id,
            },
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "position_department_mismatch"

    def test_department_delete_guard(self, client, auth_headers, department, employee):
        """A department with employees cannot be deleted."""
        response = client.delete(
            f"/api/v1/departments/{department.id}", headers=auth_headers
        )
        assert response.status_code == 409
        assert response.get_json()["error"] == "department_in_use"

    def test_set_employee_active(self, client, auth_headers, employee):
        response = client.post(
            f"/api/v1/employees/{employee.id}/active/false", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["is_active"] is False

    def test_employee_cannot_create_employees(self, client, employee_headers, department):
        response = client.post(
            "/api/v1/employees",
            json={
                "first_name": "X",
                "last_name": "Y",
                "email": "xy@ems.local",
                "department_id": department.id,
            },
            headers=employee_headers,
        )
        assert response.status_code == 403


class TestWebhookEndpoint:
    def test_missing_credentials_are_401(self, client):
        response = client.post(
            "/hooks/biometrics/punch", json={"PIN": "42", "DateTime": "2026-03-02 08:00:00"}
        )
        assert response.status_code == 401

    def test_wrong_secret_is_401(self, client, device_mapping):
        response = client.post(
            "/hooks/biometrics/punch",
            json={"PIN": "42", "DateTime": "2026-03-02 08:00:00", "Status": 0},
            headers={
                "X-Device-SN": device_mapping.serial_no,
                "X-Device-Secret": "wrong",
            },
        )
        assert response.status_code == 401

    def test_unknown_device_is_401(self, client, device):
        _record, secret = device
        response = client.post(
            "/hooks/biometrics/punch",
            json={"PIN": "42", "DateTime": "2026-03-02 08:00:00", "Status": 0},
            headers={"X-Device-SN": "GHOST", "X-Device-Secret": secret},
        )
        assert response.status_code == 401

    def test_accepted_punch_returns_attendance(
        self, client, device_mapping, employee, attendance_policy
    ):
        _record, secret = device_mapping, None
        # Fetch the secret by rotating it, so the test needs no fixture leak.
        from app.features.biometrics.service import BiometricsService

        _device, secret = BiometricsService().rotate_secret(device_mapping.id)
        db.session.commit()

        response = client.post(
            "/hooks/biometrics/punch",
            json={"PIN": "42", "DateTime": "2026-03-02 05:45:00", "Status": 0},
            headers={
                "X-Device-SN": device_mapping.serial_no,
                "X-Device-Secret": secret,
            },
        )
        assert response.status_code == 201
        payload = response.get_json()["data"]
        assert payload["duplicate"] is False
        assert payload["employee"]["employee_no"] == employee.employee_no
        assert payload["attendance"]["work_date"] == "2026-03-02"

    def test_replay_reports_duplicate(self, client, device_mapping, attendance_policy):
        from app.features.biometrics.service import BiometricsService

        _device, secret = BiometricsService().rotate_secret(device_mapping.id)
        db.session.commit()

        headers = {
            "X-Device-SN": device_mapping.serial_no,
            "X-Device-Secret": secret,
        }
        payload = {"PIN": "42", "DateTime": "2026-03-02 05:45:00", "Status": 0}

        first = client.post("/hooks/biometrics/punch", json=payload, headers=headers)
        assert first.status_code == 201

        second = client.post("/hooks/biometrics/punch", json=payload, headers=headers)
        assert second.status_code == 200
        assert second.get_json()["data"]["duplicate"] is True

    def test_unmapped_device_user_is_acknowledged(self, client, device_mapping):
        from app.features.biometrics.service import BiometricsService

        _device, secret = BiometricsService().rotate_secret(device_mapping.id)
        db.session.commit()

        response = client.post(
            "/hooks/biometrics/punch",
            json={"PIN": "9999", "DateTime": "2026-03-02 05:45:00", "Status": 0},
            headers={
                "X-Device-SN": device_mapping.serial_no,
                "X-Device-Secret": secret,
            },
        )
        # Answered 200 so the device does not queue and retry forever.
        assert response.status_code == 200
        assert response.get_json()["data"]["reason"] == "unmapped_device_user"


class TestLeaveEndpoints:
    def test_request_leave_returns_suggestions(
        self, client, employee_headers, employee, leave_rules, attendance_policy
    ):
        from app.features.leave.service import LeaveService

        LeaveService().allocate_year(year=date.today().year)
        db.session.commit()

        response = client.post(
            "/api/v1/leave/request",
            json={"leave_type": "annual", "days": 3},
            headers=employee_headers,
        )
        assert response.status_code == 201
        body = response.get_json()["data"]
        assert body["status"] == "suggested"
        assert body["suggestions"]

    def test_respond_accept_approves(
        self, client, employee_headers, employee, leave_rules, attendance_policy
    ):
        from app.features.leave.service import LeaveService

        LeaveService().allocate_year(year=date.today().year)
        db.session.commit()

        created = client.post(
            "/api/v1/leave/request",
            json={"leave_type": "annual", "days": 2},
            headers=employee_headers,
        ).get_json()["data"]

        response = client.post(
            f"/api/v1/leave/requests/{created['id']}/respond",
            json={"action": "accept"},
            headers=employee_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] == "approved"

    def test_employee_cannot_decide(
        self, client, employee_headers, employee, leave_rules, attendance_policy
    ):
        from app.features.leave.service import LeaveService

        LeaveService().allocate_year(year=date.today().year)
        db.session.commit()

        created = client.post(
            "/api/v1/leave/request",
            json={"leave_type": "sick"},
            headers=employee_headers,
        ).get_json()["data"]

        response = client.post(
            f"/api/v1/leave/requests/{created['id']}/decide",
            json={"decision": "approve"},
            headers=employee_headers,
        )
        assert response.status_code == 403

    def test_admin_can_decide(
        self, client, admin_user, employee, leave_rules, attendance_policy
    ):
        from app.features.leave.service import LeaveService

        service = LeaveService()
        service.allocate_year(year=date.today().year)
        db.session.commit()
        request = service.request_leave(
            employee_id=employee.id, leave_type="sick"
        )
        db.session.commit()

        login = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@ems.local", "password": "ChangeMe123!"},
        ).get_json()
        headers = {"Authorization": f"Bearer {login['data']['tokens']['access_token']}"}

        response = client.post(
            f"/api/v1/leave/requests/{request.id}/decide",
            json={"decision": "approve"},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.get_json()["data"]["status"] == "approved"


class TestReportsExportsEndpoints:
    def test_reports_overview(self, client, auth_headers):
        response = client.get("/api/v1/reports", headers=auth_headers)
        assert response.status_code == 200
        assert "active_employees" in response.get_json()["data"]

    def test_colour_distribution_is_zero_filled(self, client, auth_headers, employee):
        response = client.get("/api/v1/reports/attendance/colors", headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert len(data["distribution"]) == 4

    def test_headcount_csv_has_a_header(self, client, auth_headers):
        response = client.get(
            "/api/v1/exports/attendance/headcount.csv", headers=auth_headers
        )
        assert response.status_code == 200
        assert response.mimetype == "text/csv"
        assert b"work_date" in response.data

    def test_balance_import_rejects_a_bad_header(self, client, auth_headers):
        response = client.post(
            "/api/v1/exports/leave/balances/import",
            data="nonsense,columns\n1,2\n",
            content_type="text/csv",
            headers=auth_headers,
        )
        assert response.status_code == 400

    def test_balance_import_accepts_a_valid_file(
        self, client, auth_headers, employee, leave_rules
    ):
        response = client.post(
            "/api/v1/exports/leave/balances/import",
            data="employee_no,year,leave_type,entitled,used,pending\n"
            f"{employee.employee_no},{date.today().year},annual,25,1,0\n",
            content_type="text/csv",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.get_json()["data"]
        assert body["imported"] == 1
        assert body["error_count"] == 0

    def test_balance_import_reports_unknown_employees(
        self, client, auth_headers, leave_rules
    ):
        response = client.post(
            "/api/v1/exports/leave/balances/import",
            data="employee_no,year,leave_type,entitled,used,pending\n"
            f"EMP-9999,{date.today().year},annual,25,1,0\n",
            content_type="text/csv",
            headers=auth_headers,
        )
        assert response.status_code == 200
        body = response.get_json()["data"]
        assert body["imported"] == 0
        assert body["error_count"] == 1


class TestDocsEndpoints:
    def test_openapi_document_is_served(self, client):
        response = client.get("/api/docs/openapi.json")
        assert response.status_code == 200
        document = response.get_json()
        assert document["openapi"].startswith("3.")
        assert "/auth/login" in document["paths"]

    def test_swagger_ui_renders(self, client):
        response = client.get("/api/docs/ui")
        assert response.status_code == 200
        assert b"swagger" in response.data.lower()

    def test_health_reports_database_up(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.get_json()["database"] == "up"


class TestHtmlViews:
    def test_login_page_renders(self, client):
        response = client.get("/app/login")
        assert response.status_code == 200
        assert b"Sign in" in response.data

    def test_dashboard_requires_login(self, client):
        response = client.get("/app/dashboard")
        assert response.status_code == 302
        assert "/app/login" in response.headers["Location"]

    def test_employee_list_renders_for_hr(self, client, auth_headers, employee):
        response = client.get("/app/employees", headers=auth_headers)
        assert response.status_code == 200
        assert b"Alice Njeri" in response.data

    def test_attendance_board_renders(self, client, auth_headers, employee):
        response = client.get("/app/attendance", headers=auth_headers)
        assert response.status_code == 200
        assert b"Attendance board" in response.data

    def test_leave_admin_queue_renders(self, client, auth_headers):
        response = client.get("/app/leave/admin", headers=auth_headers)
        assert response.status_code == 200

    def test_reports_page_renders(self, client, auth_headers, employee):
        response = client.get("/app/reports", headers=auth_headers)
        assert response.status_code == 200
        assert b"Reports" in response.data

    def test_devices_page_renders_for_admin(self, client, auth_headers):
        response = client.get("/app/devices", headers=auth_headers)
        assert response.status_code == 200

    def test_employee_cannot_reach_departments(self, client, employee_headers):
        response = client.get("/app/departments", headers=employee_headers)
        assert response.status_code == 403

    def test_htmx_suggestion_returns_a_fragment(
        self, client, employee_headers, employee, leave_rules, attendance_policy
    ):
        from app.features.leave.service import LeaveService

        LeaveService().allocate_year(year=date.today().year)
        db.session.commit()

        response = client.post(
            "/app/leave/suggest",
            data={"leave_type": "annual", "days": "3"},
            headers={**employee_headers, "HX-Request": "true"},
        )
        assert response.status_code == 200
        assert b"Best window found" in response.data
