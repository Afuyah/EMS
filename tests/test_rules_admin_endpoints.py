"""Endpoint tests for the lateness/absenteeism rules admin interface."""

from __future__ import annotations

import pytest

from app.core.database import db
from app.features.rules.repository import (
    ColorRuleRepository,
    DisciplinaryRuleRepository,
)

pytestmark = pytest.mark.endpoint


class TestRulesPageAccess:
    def test_login_required(self, client):
        response = client.get("/app/rules")
        assert response.status_code == 302
        assert "/app/login" in response.headers["Location"]

    def test_hr_can_open_the_page(self, client, hr_headers):
        response = client.get("/app/rules", headers=hr_headers)
        assert response.status_code == 200
        assert b"Lateness" in response.data
        assert b"Colour ladder" in response.data
        assert b"Disciplinary thresholds" in response.data

    def test_admin_can_open_the_page(self, client, auth_headers):
        response = client.get("/app/rules", headers=auth_headers)
        assert response.status_code == 200

    def test_employee_is_forbidden(self, client, employee_headers):
        response = client.get("/app/rules", headers=employee_headers)
        assert response.status_code == 403


class TestColorRuleEndpoints:
    def test_create_global_color_rule(self, client, hr_headers):
        response = client.post(
            "/app/rules/colors",
            data={
                "color": "yellow",
                "department_id": "",
                "min_lates": "3",
                "min_absences": "0",
                "window_days": "30",
                "priority": "10",
            },
            headers=hr_headers,
            follow_redirects=False,
        )
        assert response.status_code == 302

        rules = ColorRuleRepository().list_all()
        assert len(rules) == 1
        assert rules[0].color == "yellow"
        assert rules[0].min_lates == 3
        assert rules[0].department_id is None

    def test_create_department_scoped_rule(self, client, hr_headers, department):
        client.post(
            "/app/rules/colors",
            data={
                "color": "orange",
                "department_id": str(department.id),
                "min_lates": "6",
                "min_absences": "1",
                "window_days": "30",
                "priority": "20",
            },
            headers=hr_headers,
        )
        rules = ColorRuleRepository().list_all()
        assert rules[0].department_id == department.id

    def test_duplicate_is_rejected_without_creating_a_row(self, client, hr_headers):
        payload = {
            "color": "yellow", "department_id": "", "min_lates": "3",
            "min_absences": "0", "window_days": "30", "priority": "10",
        }
        client.post("/app/rules/colors", data=payload, headers=hr_headers)
        client.post("/app/rules/colors", data=payload, headers=hr_headers)

        assert len(ColorRuleRepository().list_all()) == 1

    def test_invalid_colour_creates_nothing(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "chartreuse", "department_id": "", "min_lates": "3",
                "min_absences": "0", "window_days": "30", "priority": "10",
            },
            headers=hr_headers,
        )
        assert ColorRuleRepository().list_all() == []

    def test_green_with_thresholds_creates_nothing(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "green", "department_id": "", "min_lates": "5",
                "min_absences": "0", "window_days": "30", "priority": "5",
            },
            headers=hr_headers,
        )
        assert ColorRuleRepository().list_all() == []

    def test_edit_form_renders(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "yellow", "department_id": "", "min_lates": "3",
                "min_absences": "0", "window_days": "30", "priority": "10",
            },
            headers=hr_headers,
        )
        rule = ColorRuleRepository().list_all()[0]

        response = client.get(f"/app/rules/colors/{rule.id}/edit", headers=hr_headers)
        assert response.status_code == 200
        assert b"Edit colour rule" in response.data

    def test_update_rule(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "yellow", "department_id": "", "min_lates": "3",
                "min_absences": "0", "window_days": "30", "priority": "10",
            },
            headers=hr_headers,
        )
        rule = ColorRuleRepository().list_all()[0]

        client.post(
            f"/app/rules/colors/{rule.id}",
            data={
                "color": "yellow", "min_lates": "4", "min_absences": "1",
                "window_days": "60", "priority": "12",
            },
            headers=hr_headers,
        )
        db.session.expire_all()
        updated = ColorRuleRepository().find(rule.id)
        assert updated.min_lates == 4
        assert updated.min_absences == 1
        assert updated.window_days == 60
        assert updated.priority == 12

    def test_delete_rule(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "yellow", "department_id": "", "min_lates": "3",
                "min_absences": "0", "window_days": "30", "priority": "10",
            },
            headers=hr_headers,
        )
        rule = ColorRuleRepository().list_all()[0]

        client.post(f"/app/rules/colors/{rule.id}/delete", headers=hr_headers)
        assert ColorRuleRepository().list_all() == []

    def test_employee_cannot_create(self, client, employee_headers):
        response = client.post(
            "/app/rules/colors",
            data={
                "color": "yellow", "department_id": "", "min_lates": "3",
                "min_absences": "0", "window_days": "30", "priority": "10",
            },
            headers=employee_headers,
        )
        assert response.status_code == 403
        assert ColorRuleRepository().list_all() == []


class TestDisciplinaryRuleEndpoints:
    def test_create_rule(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late",
                "threshold": "3",
                "period": "month",
                "action": "warning",
                "department_id": "",
                "auto_apply": "true",
                "is_active": "true",
            },
            headers=hr_headers,
        )
        rules = DisciplinaryRuleRepository().list_all()
        assert len(rules) == 1
        assert rules[0].trigger_type == "late"
        assert rules[0].threshold == 3
        assert rules[0].auto_apply is True

    def test_auto_apply_off_when_unchecked(self, client, hr_headers):
        """An unchecked checkbox sends nothing, which must mean "off"."""
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "absent", "threshold": "3", "period": "month",
                "action": "suspension", "department_id": "",
                "suspension_days": "2", "is_active": "true",
            },
            headers=hr_headers,
        )
        rules = DisciplinaryRuleRepository().list_all()
        assert rules[0].auto_apply is False
        assert rules[0].suspension_days == 2

    def test_disabled_when_is_active_unchecked(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "",
            },
            headers=hr_headers,
        )
        assert DisciplinaryRuleRepository().list_all()[0].is_active is False

    def test_zero_threshold_creates_nothing(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "0", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        assert DisciplinaryRuleRepository().list_all() == []

    def test_blank_suspension_defaults_to_one_day(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "10", "period": "quarter",
                "action": "suspension", "department_id": "",
                "suspension_days": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        assert DisciplinaryRuleRepository().list_all()[0].suspension_days == 1

    def test_edit_form_renders(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        rule = DisciplinaryRuleRepository().list_all()[0]

        response = client.get(
            f"/app/rules/disciplinary/{rule.id}/edit", headers=hr_headers
        )
        assert response.status_code == 200
        assert b"Edit disciplinary rule" in response.data

    def test_update_rule(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        rule = DisciplinaryRuleRepository().list_all()[0]

        client.post(
            f"/app/rules/disciplinary/{rule.id}",
            data={
                "trigger_type": "late", "threshold": "6", "period": "quarter",
                "action": "written_warning", "is_active": "true",
            },
            headers=hr_headers,
        )
        db.session.expire_all()
        updated = DisciplinaryRuleRepository().find(rule.id)
        assert updated.threshold == 6
        assert updated.period == "quarter"
        assert updated.action == "written_warning"

    def test_toggle_active(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        rule = DisciplinaryRuleRepository().list_all()[0]

        client.post(
            f"/app/rules/disciplinary/{rule.id}/active/false", headers=hr_headers
        )
        db.session.expire_all()
        assert DisciplinaryRuleRepository().find(rule.id).is_active is False

        client.post(
            f"/app/rules/disciplinary/{rule.id}/active/true", headers=hr_headers
        )
        db.session.expire_all()
        assert DisciplinaryRuleRepository().find(rule.id).is_active is True

    def test_delete_unused_rule(self, client, hr_headers):
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        rule = DisciplinaryRuleRepository().list_all()[0]

        client.post(f"/app/rules/disciplinary/{rule.id}/delete", headers=hr_headers)
        assert DisciplinaryRuleRepository().list_all() == []

    def test_delete_blocked_when_actions_exist(
        self, client, hr_headers, employee, admin_user
    ):
        """A rule that produced actions must not be deletable."""
        from datetime import date

        from app.features.rules.constants import DisciplinaryActionType
        from app.features.rules.repository import DisciplinaryActionRepository

        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "3", "period": "month",
                "action": "warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )
        rule = DisciplinaryRuleRepository().list_all()[0]

        DisciplinaryActionRepository().create(
            employee_id=employee.id,
            rule_id=rule.id,
            action=DisciplinaryActionType.WARNING.value,
            triggered_on=date.today(),
            details={"observed": 3},
            applied_by=admin_user.id,
        )
        db.session.commit()

        client.post(f"/app/rules/disciplinary/{rule.id}/delete", headers=hr_headers)
        # Still present, and the page reports why.
        assert DisciplinaryRuleRepository().find(rule.id) is not None


class TestRulesPageRendering:
    def test_empty_state_is_shown(self, client, hr_headers):
        response = client.get("/app/rules", headers=hr_headers)
        assert response.status_code == 200
        assert b"No colour rules configured" in response.data
        assert b"No disciplinary rules configured" in response.data

    def test_existing_rules_are_listed(self, client, hr_headers):
        client.post(
            "/app/rules/colors",
            data={
                "color": "red", "department_id": "", "min_lates": "10",
                "min_absences": "3", "window_days": "30", "priority": "30",
            },
            headers=hr_headers,
        )
        client.post(
            "/app/rules/disciplinary",
            data={
                "trigger_type": "late", "threshold": "6", "period": "month",
                "action": "written_warning", "department_id": "", "is_active": "true",
            },
            headers=hr_headers,
        )

        response = client.get("/app/rules", headers=hr_headers)
        body = response.data
        assert b"written_warning".replace(b"_", b" ") in body or b"Written Warning" in body
        assert b"30" in body  # the red rule's priority

    def test_nav_link_present_for_hr(self, client, hr_headers):
        response = client.get("/app/rules", headers=hr_headers)
        assert b"Attendance rules" in response.data

    def test_department_scope_is_labelled(self, client, hr_headers, department):
        client.post(
            "/app/rules/colors",
            data={
                "color": "orange", "department_id": str(department.id),
                "min_lates": "6", "min_absences": "1", "window_days": "30",
                "priority": "20",
            },
            headers=hr_headers,
        )
        response = client.get("/app/rules", headers=hr_headers)
        assert department.name.encode() in response.data
        assert b"overrides" in response.data
