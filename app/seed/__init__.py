"""Idempotent seed data.

Running ``flask seed`` repeatedly is safe: every section checks for what already
exists and only fills the gaps.  ``seed_minimal`` creates just the admin user
and the global rules, which is what a fresh environment needs to be usable.
"""

from __future__ import annotations

import random
from datetime import date, time, timedelta

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.attendance.service import AttendancePolicyService
from app.features.auth.constants import Role
from app.features.auth.service import AuthService
from app.features.biometrics.service import BiometricsService
from app.features.employees.constants import EmployeeColor
from app.features.employees.service import (
    DepartmentService,
    EmployeeService,
    PositionService,
)
from app.features.leave.constants import LeaveType
from app.features.leave.rules_service import LeaveRulesService
from app.features.leave.service import LeaveService
from app.features.rules.constants import (
    DisciplinaryActionType,
    RulePeriod,
    TriggerType,
)
from app.features.rules.models import ColorRule, DisciplinaryRule
from app.features.rules.repository import (
    ColorRuleRepository,
    DisciplinaryRuleRepository,
)

logger = get_logger(__name__)

ADMIN_EMAIL = "admin@ems.local"
ADMIN_PASSWORD = "ChangeMe123!"
SAMPLE_EMAIL = "alice@ems.local"
SAMPLE_PASSWORD = "Password123!"

#: code, name, [(position title, ...)]
DEPARTMENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ENG",
        "Engineering",
        ("Engineering Manager", "Backend Engineer", "Frontend Engineer", "QA Engineer"),
    ),
    (
        "HR",
        "Human Resources",
        ("HR Manager", "HR Officer", "Recruiter"),
    ),
    (
        "FIN",
        "Finance",
        ("Finance Manager", "Accountant", "Payroll Officer"),
    ),
    (
        "OPS",
        "Operations",
        ("Operations Manager", "Logistics Officer", "Support Agent"),
    ),
)

#: The documented colour ladder.
COLOR_LADDER: tuple[dict, ...] = (
    dict(color=EmployeeColor.YELLOW.value, min_lates=3, min_absences=0, priority=10),
    dict(color=EmployeeColor.ORANGE.value, min_lates=6, min_absences=1, priority=20),
    dict(color=EmployeeColor.RED.value, min_lates=10, min_absences=3, priority=30),
)

#: The documented disciplinary ladder.
DISCIPLINARY_LADDER: tuple[dict, ...] = (
    dict(
        trigger_type=TriggerType.LATE.value,
        threshold=3,
        period=RulePeriod.MONTH.value,
        action=DisciplinaryActionType.WARNING.value,
        auto_apply=True,
    ),
    dict(
        trigger_type=TriggerType.LATE.value,
        threshold=6,
        period=RulePeriod.MONTH.value,
        action=DisciplinaryActionType.WRITTEN_WARNING.value,
        auto_apply=True,
    ),
    dict(
        trigger_type=TriggerType.LATE.value,
        threshold=10,
        period=RulePeriod.QUARTER.value,
        action=DisciplinaryActionType.SUSPENSION.value,
        suspension_days=1,
        auto_apply=False,
    ),
    dict(
        trigger_type=TriggerType.ABSENT.value,
        threshold=3,
        period=RulePeriod.MONTH.value,
        action=DisciplinaryActionType.SUSPENSION.value,
        suspension_days=2,
        auto_apply=False,
    ),
)

#: Leave rules come from the rules service so the seed and the runtime defaults
#: can never drift apart.
from app.features.leave.rules_service import DEFAULT_LEAVE_RULES as LEAVE_LADDER  # noqa: E402

#: Sample people used to make the UI non-empty: (first, last, dept code, position, role)
SAMPLE_PEOPLE: tuple[tuple[str, str, str, str, str], ...] = (
    ("Alice", "Njeri", "ENG", "Backend Engineer", Role.EMPLOYEE.value),
    ("Brian", "Otieno", "ENG", "Frontend Engineer", Role.EMPLOYEE.value),
    ("Carol", "Mwangi", "ENG", "Engineering Manager", Role.MANAGER.value),
    ("David", "Kimani", "HR", "HR Officer", Role.HR.value),
    ("Esther", "Wanjiru", "FIN", "Accountant", Role.EMPLOYEE.value),
    ("Felix", "Omondi", "OPS", "Support Agent", Role.EMPLOYEE.value),
    ("Grace", "Achieng", "OPS", "Operations Manager", Role.MANAGER.value),
    ("Henry", "Mutua", "FIN", "Finance Manager", Role.MANAGER.value),
)


def seed_defaults() -> dict:
    """Create the global shift policy and the default rule ladders.

    **Production-safe and idempotent.**  No accounts are created here, so this
    is the command a fresh deployment runs to get a working rule set:

        flask --app wsgi seed-defaults

    Without it a production install has no shift policy, no colour ladder and no
    leave rules, so attendance would never be classified and every leave request
    would be rejected for a missing rule.
    """
    created: dict[str, list] = {
        "policies": [],
        "leave_rules": [],
        "color_rules": [],
        "disciplinary_rules": [],
    }

    # Global shift policy: 09:00-17:00 with a 10 minute grace period.
    policies = AttendancePolicyService()
    if policies.get_global() is None:
        policies.upsert(
            department_id=None,
            shift_start=time(9, 0),
            shift_end=time(17, 0),
            grace_minutes=10,
        )
        created["policies"].append("global")

    rules = LeaveRulesService()
    for spec in LEAVE_LADDER:
        if rules.rules.find(department_id=None, leave_type=spec["leave_type"]) is None:
            rules.create_rule(department_id=None, **spec)
            created["leave_rules"].append(spec["leave_type"])

    colors = ColorRuleRepository()
    if not colors.list_all():
        for spec in COLOR_LADDER:
            colors.create_rule(department_id=None, **spec)
            created["color_rules"].append(spec["color"])

    disciplinary = DisciplinaryRuleRepository()
    if not disciplinary.list_all():
        for spec in DISCIPLINARY_LADDER:
            disciplinary.create_rule(department_id=None, **spec)
            created["disciplinary_rules"].append(spec["action"])

    db.session.commit()
    logger.info("seed_defaults created=%s", {k: len(v) for k, v in created.items()})
    return created


def seed_minimal() -> dict:
    """Create the admin login **and** the global defaults.

    Development convenience only: the admin it creates uses a published default
    password, so this refuses to run in production.  Use ``seed-defaults`` for
    the rules and ``create-admin`` for the account there.
    """
    import os as _os

    if _os.getenv("FLASK_CONFIG", "").strip().lower() == "production":
        raise RuntimeError(
            "seed_minimal creates an account with a well-known default password "
            "and must not run in production. Use 'flask seed-defaults' for the "
            "rules and 'flask create-admin --email you@example.com' for the "
            "account instead."
        )

    created = seed_defaults()
    created.setdefault("users", [])

    auth = AuthService()
    if auth.get_by_email(ADMIN_EMAIL) is None:
        auth.create_user(
            email=ADMIN_EMAIL, password=ADMIN_PASSWORD, role=Role.ADMIN
        )
        created["users"].append(ADMIN_EMAIL)

    db.session.commit()
    logger.info("seed_minimal created=%s", {k: len(v) for k, v in created.items()})
    return created


def seed_all(*, with_demo_data: bool = True) -> dict:
    """Full seed: rules, departments, positions, people, devices and balances."""
    created = seed_minimal()
    created.update({"positions": [], "employees": [], "devices": [], "balances": 0})

    departments = DepartmentService()
    positions = PositionService()
    employees = EmployeeService()
    auth = AuthService()
    owners = BiometricsService()
    leave = LeaveService()

    # ---- departments and positions --------------------------------------
    department_by_code: dict[str, object] = {}
    for code, name, titles in DEPARTMENTS:
        department = departments.departments.get_by_code(code)
        if department is None:
            department = departments.create_department(name=name, code=code)
            created["departments"].append(code)
        department_by_code[code] = department

        for title in titles:
            if not positions.positions.title_taken(department_id=department.id, title=title):
                positions.create_position(title=title, department_id=department.id)
                created["positions"].append(f"{code}:{title}")

    db.session.commit()

    if not with_demo_data:
        return created

    # ---- people ----------------------------------------------------------
    today = today_local(None)
    for first, last, code, title, role in SAMPLE_PEOPLE:
        email = f"{first.lower()}@ems.local"
        if employees.employees.get_by_email(email) is not None:
            continue

        department = department_by_code[code]
        position = positions.positions.get_or_none(
            department_id=department.id, title=title
        )
        password = SAMPLE_PASSWORD
        employee, _ = employees.create_employee(
            first_name=first,
            last_name=last,
            email=email,
            department_id=department.id,
            position_id=position.id if position else None,
            phone=f"+2547{random.randint(10000000, 99999999)}",
            hired_at=today - timedelta(days=random.randint(90, 1500)),
            create_user=True,
            user_role=role,
            password=password,
            is_active=True,
            color=random.choice(EmployeeColor.values()),
        )
        created["employees"].append(employee.employee_no)

    db.session.commit()

    # ---- devices and mappings -------------------------------------------
    device, secret = _ensure_device(
        owners,
        serial_no="ZK-001",
        location="Nairobi HQ — Main Entrance",
        vendor="zkteco",
    )
    if device is not None:
        created["devices"].append({"serial_no": device.serial_no, "secret": secret})

    second, second_secret = _ensure_device(
        owners,
        serial_no="HIK-002",
        location="Nairobi HQ — Staff Door",
        vendor="hikvision",
    )
    if second is not None:
        created["devices"].append(
            {"serial_no": second.serial_no, "secret": second_secret}
        )

    db.session.commit()

    # Map every sample employee onto the ZKTeco device so punches resolve.
    if device is not None:
        for index, employee in enumerate(
            employees.employees.search(is_active=True), start=1
        ):
            mapping = owners.maps.resolve(
                device_id=device.id, device_user_id=str(index)
            )
            if mapping is None:
                try:
                    owners.map_employee(
                        device_id=device.id,
                        employee_id=employee.id,
                        device_user_id=str(index),
                    )
                except Exception as error:  # noqa: BLE001 - conflicts are fine
                    logger.debug("mapping skipped for %s: %s", employee.employee_no, error)
        db.session.commit()

    # ---- balances for the current year ----------------------------------
    summary = leave.allocate_year(year=today.year, overwrite=False)
    db.session.commit()
    created["balances"] = summary["created"]

    logger.info(
        "seed_all created=%s",
        {key: (len(value) if isinstance(value, list) else value) for key, value in created.items()},
    )
    return created


def _ensure_device(service: BiometricsService, *, serial_no: str, location: str, vendor: str):
    """Register a device if absent; return ``(device, secret_or_None)``."""
    existing = service.devices.get_by_serial(serial_no)
    if existing is not None:
        return existing, None
    device, secret = service.register_device(
        serial_no=serial_no, location=location, vendor=vendor
    )
    db.session.flush()
    return device, secret


def reset_demo_data() -> dict:
    """Delete seeded demo rows, leaving rules and the admin account intact."""
    from sqlalchemy import delete

    from app.features.attendance.models import AttendanceDaily
    from app.features.biometrics.models import (
        BiometricEmployeeMap,
        BiometricRawLog,
    )
    from app.features.leave.models import (
        LeaveBalance,
        LeaveRequest,
        LeaveRequestHistory,
    )
    from app.features.notifications.models import Notification
    from app.features.rules.models import DisciplinaryAction

    deleted: dict[str, int] = {}
    for model in (
        BiometricRawLog,
        BiometricEmployeeMap,
        AttendanceDaily,
        LeaveRequestHistory,
        LeaveRequest,
        LeaveBalance,
        DisciplinaryAction,
        Notification,
    ):
        result = db.session.execute(delete(model))
        deleted[model.__tablename__] = int(result.rowcount or 0)

    db.session.commit()
    logger.info("reset_demo_data deleted=%s", deleted)
    return deleted
