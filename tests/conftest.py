"""Shared pytest fixtures.

Unit tests (engine, state machine, evaluators) touch no database.  Integration
tests run against ``TEST_DATABASE_URL`` and rebuild the schema once per session.
"""

from __future__ import annotations

import os
import sys
from datetime import date, time, timedelta
from pathlib import Path

import pytest

# Make the application importable when pytest is run from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("FLASK_CONFIG", "testing")

from app import create_app  # noqa: E402
from app.core.database import db  # noqa: E402


# ---------------------------------------------------------------------------
# Application / database
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def app():
    """A testing-configured application with a freshly built schema."""
    application = create_app("testing")
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def app_context(app):
    """An application context for integration tests."""
    with app.app_context():
        yield app
        db.session.rollback()


@pytest.fixture()
def client(app):
    """Flask test client."""
    return app.test_client()


@pytest.fixture(autouse=True)
def _restore_app_config(app):
    """Undo config mutations after every test.

    ``app`` is session-scoped, so a test that sets ``app.config[...]`` would
    otherwise leak that value into every later test — which silently changed
    email behaviour across the suite.

    The comparison is deliberately defensive: some config values (SQLAlchemy
    engines, session makers) do not support equality the way plain scalars do,
    and a raising cleanup would turn one test's failure into errors across the
    whole module.  Keys whose values cannot be compared are restored anyway,
    since assigning the snapshot back is always safe.
    """
    snapshot = dict(app.config)
    yield
    restore: dict = {}
    for key, value in snapshot.items():
        try:
            unchanged = app.config.get(key) == value
        except Exception:  # noqa: BLE001 - uncomparable value; restore it
            unchanged = False
        if not unchanged:
            restore[key] = value
    app.config.update(restore)


@pytest.fixture()
def session(app_context):
    """The SQLAlchemy session bound to a rolled-back transaction."""
    return db.session


@pytest.fixture()
def clean_db(app_context):
    """Give each database test an empty schema.

    Truncates both before and after the test: before, so a test never inherits
    rows from one that did not request this fixture; after, so a test that fails
    mid-way cannot poison the next one.
    """
    from sqlalchemy import text

    def truncate_all() -> None:
        db.session.rollback()
        table_names = ", ".join(
            f'"{name}"' for name in reversed(list(db.metadata.tables.keys()))
        )
        db.session.execute(text(f"TRUNCATE {table_names} RESTART IDENTITY CASCADE"))
        db.session.commit()

    truncate_all()
    yield
    truncate_all()


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def attendance_policy(clean_db):
    """Global 09:00-17:00 policy with a 10 minute grace period."""
    from app.features.attendance.service import AttendancePolicyService

    policy = AttendancePolicyService().upsert(
        department_id=None,
        shift_start=time(9, 0),
        shift_end=time(17, 0),
        grace_minutes=10,
    )
    db.session.commit()
    return policy


@pytest.fixture()
def leave_rules(clean_db):
    """The documented global leave rule set."""
    from app.features.leave.rules_service import LeaveRulesService

    rules = LeaveRulesService().ensure_defaults(department_id=None)
    db.session.commit()
    return rules


@pytest.fixture()
def department(clean_db):
    from app.features.employees.service import DepartmentService

    result = DepartmentService().create_department(name="Engineering", code="ENG")
    db.session.commit()
    return result


@pytest.fixture()
def position(clean_db, department):
    from app.features.employees.service import PositionService

    result = PositionService().create_position(
        title="Backend Engineer", department_id=department.id
    )
    db.session.commit()
    return result


@pytest.fixture()
def admin_user(clean_db):
    from app.features.auth.constants import Role
    from app.features.auth.service import AuthService

    user = AuthService().create_user(
        email="admin@ems.local", password="ChangeMe123!", role=Role.ADMIN
    )
    db.session.commit()
    return user


@pytest.fixture()
def employee(clean_db, department, position):
    """Alice, with a login linked to her employee record.

    ``create_employee(create_user=True)`` provisions the login and links it, so
    the fixture must not also create the user by hand.
    """
    from app.features.auth.constants import Role
    from app.features.employees.service import EmployeeService

    record, _password = EmployeeService().create_employee(
        first_name="Alice",
        last_name="Njeri",
        email="alice@ems.local",
        department_id=department.id,
        position_id=position.id,
        hired_at=date.today() - timedelta(days=400),
        create_user=True,
        user_role=Role.EMPLOYEE.value,
        password="Password123!",
    )
    db.session.commit()
    assert record.user_id is not None, "employee login was not linked"
    return record


@pytest.fixture()
def device(clean_db):
    """A registered ZKTeco device, with the shared secret returned."""
    from app.features.biometrics.service import BiometricsService

    service = BiometricsService()
    record, secret = service.register_device(
        serial_no="ZK-001", location="Test Lab", vendor="zkteco"
    )
    db.session.commit()
    return record, secret


@pytest.fixture()
def device_mapping(clean_db, device, employee, department, position):
    """The test device mapped to the test employee as device user ``42``."""
    from app.features.biometrics.service import BiometricsService

    record, _secret = device
    BiometricsService().map_employee(
        device_id=record.id, employee_id=employee.id, device_user_id="42"
    )
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def auth_headers(client, admin_user):
    """Bearer headers for the admin user."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@ems.local", "password": "ChangeMe123!"},
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["data"]["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def employee_headers(client, employee):
    """Bearer headers for the employee user."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@ems.local", "password": "Password123!"},
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["data"]["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def hr_user(clean_db):
    """An HR login (owns attendance policy and rules, unlike a plain employee)."""
    from app.features.auth.constants import Role
    from app.features.auth.service import AuthService

    user = AuthService().create_user(
        email="david@ems.local", password="Password123!", role=Role.HR
    )
    db.session.commit()
    return user


@pytest.fixture()
def hr_headers(client, hr_user):
    """Bearer headers for the HR user."""
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "david@ems.local", "password": "Password123!"},
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["data"]["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}
