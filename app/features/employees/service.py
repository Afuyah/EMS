"""Employee service: business rules for departments, positions and employees.

Stateless — every method takes what it needs and flushes rather than commits.
Cross-feature calls go through the other feature's service, injected rather than
imported at module scope so there is no import cycle.
"""

from __future__ import annotations

from datetime import date

from app.core.exceptions import ValidationError
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.employees.constants import (
    DEFAULT_TIMEZONE,
    EMPLOYEE_NO_PREFIX,
    EMPLOYEE_NO_WIDTH,
    EmployeeColor,
    Gender,
)
from app.features.employees.exceptions import (
    DepartmentCodeTakenError,
    DepartmentInUseError,
    DepartmentNameTakenError,
    DepartmentNotFoundError,
    EmployeeEmailTakenError,
    EmployeeHasUserError,
    EmployeeNoTakenError,
    EmployeeNotFoundError,
    GenderLeaveMismatchError,
    InvalidPositionDepartmentError,
    PositionInUseError,
    PositionNotFoundError,
    PositionTitleTakenError,
    ProfileFieldLockedError,
    ProfileNotAvailableError,
)
from app.features.employees.models import Department, Employee, Position
from app.features.employees.repository import (
    DepartmentRepository,
    EmployeeRepository,
    PositionRepository,
)

logger = get_logger(__name__)


class DepartmentService:
    """Use-cases for organisational units."""

    def __init__(
        self,
        departments: DepartmentRepository | None = None,
        employees: EmployeeRepository | None = None,
    ) -> None:
        self.departments = departments or DepartmentRepository()
        self.employees = employees or EmployeeRepository()

    # ---- reads -----------------------------------------------------------
    def list_departments(self) -> list[Department]:
        return list(self.departments.ordered())

    def get_department(self, department_id: int) -> Department:
        department = self.departments.get(department_id)
        if department is None:
            raise DepartmentNotFoundError(f"Department {department_id} was not found.")
        return department

    def distribution(self) -> list[dict]:
        """Headcount per department, descending."""
        return self.departments.distribution()

    def employee_counts(self) -> dict[int, int]:
        return self.departments.employee_counts()

    # ---- writes ----------------------------------------------------------
    def create_department(self, *, name: str, code: str) -> Department:
        clean_name = (name or "").strip()
        clean_code = (code or "").strip().upper()
        if not clean_name or not clean_code:
            raise ValidationError("Both department name and code are required.")
        if self.departments.name_taken(clean_name):
            raise DepartmentNameTakenError(f"'{clean_name}' is already a department.")
        if self.departments.code_taken(clean_code):
            raise DepartmentCodeTakenError(f"Code '{clean_code}' is already in use.")

        department = self.departments.create(name=clean_name, code=clean_code)
        logger.info("created department %s (%s)", clean_code, clean_name)
        return department

    def update_department(
        self, department_id: int, *, name: str | None = None, code: str | None = None
    ) -> Department:
        department = self.get_department(department_id)

        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise ValidationError("Department name cannot be empty.")
            if self.departments.name_taken(clean_name, excluding_id=department_id):
                raise DepartmentNameTakenError(f"'{clean_name}' is already a department.")
            department.name = clean_name

        if code is not None:
            clean_code = code.strip().upper()
            if not clean_code:
                raise ValidationError("Department code cannot be empty.")
            if self.departments.code_taken(clean_code, excluding_id=department_id):
                raise DepartmentCodeTakenError(f"Code '{clean_code}' is already in use.")
            department.code = clean_code

        return self.departments.update(department)

    def delete_department(self, department_id: int) -> None:
        """Delete guard: refuse while employees or positions remain."""
        department = self.get_department(department_id)
        assigned = self.employees.count(department_id=department_id)
        if assigned or department.positions:
            raise DepartmentInUseError(
                f"'{department.name}' still has {assigned} employee(s) and "
                f"{len(department.positions)} position(s) assigned."
            )
        self.departments.delete(department)
        logger.info("deleted department id=%s", department_id)


class PositionService:
    """Use-cases for job titles scoped to a department."""

    def __init__(
        self,
        positions: PositionRepository | None = None,
        departments: DepartmentRepository | None = None,
    ) -> None:
        self.positions = positions or PositionRepository()
        self.departments = departments or DepartmentRepository()

    # ---- reads -----------------------------------------------------------
    def list_positions(self, department_id: int | None = None) -> list[Position]:
        if department_id is None:
            return list(self.positions.list_all())
        return list(self.positions.list_for_department(department_id))

    def get_position(self, position_id: int) -> Position:
        position = self.positions.get(position_id)
        if position is None:
            raise PositionNotFoundError(f"Position {position_id} was not found.")
        return position

    # ---- writes ----------------------------------------------------------
    def create_position(self, *, title: str, department_id: int) -> Position:
        clean_title = (title or "").strip()
        if not clean_title:
            raise ValidationError("Position title is required.")

        department = self.departments.get(department_id)
        if department is None:
            raise DepartmentNotFoundError(f"Department {department_id} was not found.")

        if self.positions.title_taken(department_id=department_id, title=clean_title):
            raise PositionTitleTakenError(
                f"'{clean_title}' already exists in {department.name}."
            )

        position = self.positions.create(title=clean_title, department_id=department_id)
        logger.info("created position %s in dept=%s", clean_title, department_id)
        return position

    def update_position(
        self,
        position_id: int,
        *,
        title: str | None = None,
        department_id: int | None = None,
    ) -> Position:
        position = self.get_position(position_id)
        target_department = position.department_id

        if department_id is not None and department_id != position.department_id:
            if self.departments.get(department_id) is None:
                raise DepartmentNotFoundError(f"Department {department_id} was not found.")
            # Moving a position that already has employees would break the
            # employee/position department invariant.
            if self.positions.count_positions(position_id):
                raise InvalidPositionDepartmentError(
                    "Reassign this position's employees before moving it to "
                    "another department."
                )
            target_department = department_id
            position.department_id = department_id

        if title is not None:
            clean_title = title.strip()
            if not clean_title:
                raise ValidationError("Position title cannot be empty.")
            if self.positions.title_taken(
                department_id=target_department,
                title=clean_title,
                excluding_id=position_id,
            ):
                raise PositionTitleTakenError(
                    f"'{clean_title}' already exists in that department."
                )
            position.title = clean_title

        return self.positions.update(position)

    def delete_position(self, position_id: int) -> None:
        """Delete guard: refuse while employees hold the position."""
        position = self.get_position(position_id)
        assigned = self.positions.count_positions(position_id)
        if assigned:
            raise PositionInUseError(
                f"'{position.title}' still has {assigned} employee(s) assigned."
            )
        self.positions.delete(position)
        logger.info("deleted position id=%s", position_id)


class EmployeeService:
    """Use-cases for the employee lifecycle."""

    def __init__(
        self,
        employees: EmployeeRepository | None = None,
        departments: DepartmentRepository | None = None,
        positions: PositionRepository | None = None,
        auth_service=None,
    ) -> None:
        self.employees = employees or EmployeeRepository()
        self.departments = departments or DepartmentRepository()
        self.positions = positions or PositionRepository()
        self._auth_service = auth_service

    @property
    def auth(self):
        """Lazily resolve the auth service (cross-feature call)."""
        if self._auth_service is None:
            from app.features.auth.service import AuthService

            self._auth_service = AuthService()
        return self._auth_service

    # ---- reads -----------------------------------------------------------
    def get_employee(self, employee_id: int) -> Employee:
        employee = self.employees.get(employee_id)
        if employee is None:
            raise EmployeeNotFoundError(f"Employee {employee_id} was not found.")
        return employee

    def get_employee_by_user(self, user_id: int) -> Employee | None:
        return self.employees.get_by_user_id(user_id)

    def require_employee_for_user(self, user_id: int) -> Employee:
        """Resolve the employee record behind a login."""
        employee = self.employees.get_by_user_id(user_id)
        if employee is None:
            raise EmployeeNotFoundError(
                "This login is not linked to an employee record."
            )
        return employee

    def list_employees(self, filters: dict | None = None) -> list[Employee]:
        return list(self.employees.search(**(filters or {})))

    def list_employees_paginated(
        self, *, page: int, per_page: int, sort: str = "last_name", **filters
    ):
        return self.employees.search_paginated(
            page=page, per_page=per_page, sort=sort, **filters
        )

    def count_active(self) -> int:
        return self.employees.count_active()

    def count_by_color(self) -> list[dict]:
        return self.employees.count_by_color()

    def count_by_department(self) -> list[dict]:
        return self.employees.count_by_department()

    def active_employee_ids(self) -> list[int]:
        return self.employees.active_employee_ids()

    def headcount_by_day(self, start: date, end: date) -> list[dict]:
        return self.employees.headcount_by_day(start, end)

    def ids_in_department(self, department_id: int, *, active_only: bool = True) -> list[int]:
        return self.employees.ids_in_department(department_id, active_only=active_only)

    def team_of(self, manager_employee: Employee) -> list[Employee]:
        """Everyone in the manager's department."""
        if manager_employee.department_id is None:
            return []
        return list(
            self.employees.search(department_id=manager_employee.department_id)
        )

    # ---- writes ----------------------------------------------------------
    def create_employee(
        self,
        *,
        first_name: str,
        last_name: str,
        email: str,
        department_id: int,
        position_id: int | None = None,
        employee_no: str | None = None,
        phone: str | None = None,
        gender: str | None = None,
        tz: str | None = None,
        hired_at: date | None = None,
        is_active: bool = True,
        create_user: bool = False,
        user_role: str | None = None,
        password: str | None = None,
        color: str | None = None,
    ) -> tuple[Employee, str | None]:
        """Create an employee, optionally with a login.

        ``gender`` is required: it decides whether maternity or paternity leave
        applies, so an employee without one cannot be given either.

        Returns ``(employee, initial_password)``; the password is ``None`` when
        no login was created.
        """
        clean_first = (first_name or "").strip()
        clean_last = (last_name or "").strip()
        clean_email = (email or "").strip().lower()
        if not clean_first or not clean_last:
            raise ValidationError("Both first and last name are required.")
        if not clean_email or "@" not in clean_email:
            raise ValidationError("A valid email address is required.")

        recorded_gender = self._clean_gender(gender)

        if self.departments.get(department_id) is None:
            raise DepartmentNotFoundError(f"Department {department_id} was not found.")

        position = None
        if position_id is not None:
            position = self.positions.get(position_id)
            if position is None:
                raise PositionNotFoundError(f"Position {position_id} was not found.")
            if position.department_id != department_id:
                raise InvalidPositionDepartmentError()

        if self.employees.email_taken(clean_email):
            raise EmployeeEmailTakenError(f"{clean_email} is already in use.")

        resolved_no = (employee_no or "").strip().upper()
        if not resolved_no:
            resolved_no = self.employees.next_employee_number(
                prefix=EMPLOYEE_NO_PREFIX, width=EMPLOYEE_NO_WIDTH
            )
        elif self.employees.employee_no_taken(resolved_no):
            raise EmployeeNoTakenError(f"{resolved_no} is already assigned.")

        employee = self.employees.create(
            employee_no=resolved_no,
            first_name=clean_first,
            last_name=clean_last,
            email=clean_email,
            phone=(phone or None),
            gender=recorded_gender,
            department_id=department_id,
            position_id=position.id if position else None,
            tz=tz or DEFAULT_TIMEZONE,
            hired_at=hired_at,
            is_active=is_active,
            current_color=color or EmployeeColor.GREEN.value,
        )

        initial_password: str | None = None
        if create_user:
            from app.features.auth.constants import Role

            role = (user_role or Role.EMPLOYEE.value).upper()
            if role not in Role.values():
                raise ValidationError(f"Unknown role {user_role!r}.")
            initial_password = password or self._generate_password()
            user = self.auth.create_user(
                email=clean_email,
                password=initial_password,
                role=role,
                is_active=is_active,
                # HR chose this password on the employee's behalf, so it is
                # temporary until the employee replaces it.
                must_change_password=True,
            )
            employee.user_id = user.id
            provisioned_role = role

        self.employees.update(employee)
        logger.info("created employee %s id=%s", resolved_no, employee.id)

        # Email the sign-in details and the biometrics reminder.  Sent after the
        # employee row exists but before the caller commits, via send_safely so
        # a mail outage cannot undo the account that was just created.
        if initial_password is not None and self.email_enabled:
            self._send_welcome(
                employee=employee,
                password=initial_password,
                role=provisioned_role if create_user else None,
            )

        return employee, initial_password

    @property
    def email_enabled(self) -> bool:
        """Whether outbound email should be attempted at all."""
        try:
            from flask import current_app

            return bool(current_app.config.get("EMAIL_ENABLED", True))
        except RuntimeError:  # no application context (seeding, CLI)
            return True

    def _send_welcome(self, *, employee, password: str, role: str | None) -> None:
        """Email the new user their sign-in details and next steps."""
        from app.features.email.service import EmailService

        department_name = (
            employee.department.name if employee.department else None
        )
        EmailService().send_welcome(
            to_email=employee.email,
            first_name=employee.first_name,
            employee_no=employee.employee_no,
            temporary_password=password,
            role=role,
            recipient_user_id=employee.user_id,
            recipient_employee_id=employee.id,
            department_name=department_name,
        )

    def resend_welcome(self, employee_id: int) -> str:
        """Issue a fresh temporary password and email it again.

        The old password stops working immediately, so a welcome email that went
        astray cannot leave a live credential in an old inbox.
        """
        employee = self.get_employee(employee_id)
        if employee.user_id is None:
            raise ProfileNotAvailableError(
                "This employee has no login, so there is nothing to resend."
            )

        user = self.auth.get_user(employee.user_id)
        if user is None:
            raise ProfileNotAvailableError("The linked login no longer exists.")

        password = self._generate_password()
        self.auth.set_password(user, password)
        # ``set_password`` flags a forced change, which is what we want here too.
        user.must_change_password = True

        self._send_welcome(employee=employee, password=password, role=user.role)
        logger.info(
            "resent welcome credentials to employee id=%s", employee_id
        )
        return password

    @staticmethod
    def _clean_gender(gender: str | None) -> str | None:
        """Validate a recorded sex; ``None`` means it has not been recorded.

        The create form requires it (gender decides maternity versus paternity),
        but it is not enforced here: the seed and the API may legitimately create
        a record first and have HR set the gender afterwards.  The "needs gender"
        screen exists for exactly that, and neither parental leave type is
        available until it is set.
        """
        if gender is None or str(gender).strip() == "":
            return None
        try:
            return Gender.coerce(gender).value
        except ValueError as error:
            raise ValidationError(
                f"Unknown gender {gender!r}; expected one of "
                f"{', '.join(Gender.values())}."
            ) from error

    #: Leave types that depend on the employee's sex.
    PARENTAL_LEAVE_TYPES: frozenset[str] = frozenset({"maternity", "paternity"})

    def leave_eligibility(self, employee) -> dict:
        """Which leave types an employee may request, and why not for the rest.

        Used by the leave form and the annual planner so the rule is applied in
        one place rather than repeated per caller.
        """
        from app.features.leave.constants import LeaveType

        recorded = getattr(employee, "gender_enum", None)
        entitled = recorded.parental_leave_type if recorded else None

        allowed: list[str] = []
        blocked: dict[str, str] = {}
        for kind in LeaveType:
            if kind.value not in self.PARENTAL_LEAVE_TYPES:
                allowed.append(kind.value)
            elif entitled is None:
                blocked[kind.value] = (
                    "Record this employee's gender first — that decides whether "
                    "maternity or paternity leave applies."
                )
            elif kind.value == entitled:
                allowed.append(kind.value)
            else:
                other = "paternity" if entitled == "maternity" else "maternity"
                blocked[kind.value] = (
                    f"{kind.value.capitalize()} leave applies to "
                    f"{'women' if kind.value == 'maternity' else 'men'}. "
                    f"This employee's recorded gender makes them eligible for "
                    f"{other} leave instead."
                )
        return {"allowed": allowed, "blocked": blocked, "parental": entitled}

    def assert_leave_type_allowed(self, employee, leave_type: str) -> None:
        """Raise when the employee may not take this leave type at all."""
        kind = str(getattr(leave_type, "value", leave_type)).lower()
        if kind not in self.PARENTAL_LEAVE_TYPES:
            return
        reason = self.leave_eligibility(employee)["blocked"].get(kind)
        if reason:
            raise GenderLeaveMismatchError(reason)

    # ---- gender recording -------------------------------------------------
    def list_missing_gender(self, *, department_id: int | None = None) -> list:
        """Active employees with no recorded gender."""
        return self.employees.missing_gender(department_id=department_id)

    def count_employees(self, *, department_id: int | None = None) -> int:
        """Active headcount, for the 'x of y still need a gender' summary."""
        return self.employees.count_active(department_id=department_id)

    @staticmethod
    def _generate_password() -> str:
        """Readable temporary password for HR-provisioned logins."""
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits
        core = "".join(secrets.choice(alphabet) for _ in range(10))
        return f"{core}9!"

    def update_employee(self, employee_id: int, **changes) -> Employee:
        """Patch an employee, enforcing cross-field invariants."""
        employee = self.get_employee(employee_id)

        if "email" in changes and changes["email"] is not None:
            clean_email = changes["email"].strip().lower()
            if not clean_email or "@" not in clean_email:
                raise ValidationError("A valid email address is required.")
            if self.employees.email_taken(clean_email, excluding_id=employee_id):
                raise EmployeeEmailTakenError(f"{clean_email} is already in use.")
            changes["email"] = clean_email

        if "department_id" in changes and changes["department_id"] is not None:
            if self.departments.get(changes["department_id"]) is None:
                raise DepartmentNotFoundError(
                    f"Department {changes['department_id']} was not found."
                )

        # Keep position and department consistent whichever one moved.
        target_department = changes.get("department_id", employee.department_id)
        target_position = changes.get("position_id", employee.position_id)
        if target_position is not None:
            position = self.positions.get(target_position)
            if position is None:
                raise PositionNotFoundError(f"Position {target_position} was not found.")
            if position.department_id != target_department:
                raise InvalidPositionDepartmentError()
        elif "department_id" in changes and employee.position_id is not None:
            # Department changed without a new position: clear the stale one.
            changes["position_id"] = None

        if "current_color" in changes and changes["current_color"] is not None:
            changes["current_color"] = EmployeeColor.coerce(
                changes["current_color"]
            ).value

        if "gender" in changes and changes["gender"] is not None:
            changes["gender"] = self._clean_gender(changes["gender"])

        if "first_name" in changes and changes["first_name"] is not None:
            if not changes["first_name"].strip():
                raise ValidationError("First name cannot be empty.")
            changes["first_name"] = changes["first_name"].strip()

        if "last_name" in changes and changes["last_name"] is not None:
            if not changes["last_name"].strip():
                raise ValidationError("Last name cannot be empty.")
            changes["last_name"] = changes["last_name"].strip()

        return self.employees.update(employee, **changes)

    # ---- self-service profile -------------------------------------------
    #: Fields the employee may change about themselves.  Everything else
    #: (employee number, department, position, hire date, phone) is HR-owned.
    SELF_EDITABLE_FIELDS: frozenset[str] = frozenset(
        {"first_name", "last_name", "email", "tz"}
    )

    #: Explicitly immutable, listed so the refusal can name the field.
    SELF_LOCKED_FIELDS: frozenset[str] = frozenset(
        {"employee_no", "phone", "department_id", "position_id", "hired_at"}
    )

    def update_own_profile(self, employee_id: int, **changes) -> Employee:
        """Let an employee edit their own profile.

        Allowed: first name, last name, email and timezone.
        Refused: employee number, phone, department, position and hire date —
        those stay with HR.

        Changing the email also updates the linked login, because that address
        is the sign-in identity; leaving the two out of sync would lock the
        employee out of the account they just edited.
        """
        employee = self.get_employee(employee_id)

        locked = sorted(set(changes) & self.SELF_LOCKED_FIELDS)
        if locked:
            raise ProfileFieldLockedError(
                f"{', '.join(locked)} cannot be changed from your profile. "
                f"Please contact HR."
            )

        unknown = sorted(set(changes) - self.SELF_EDITABLE_FIELDS)
        if unknown:
            raise ValidationError(
                f"Unknown profile field(s): {', '.join(unknown)}."
            )

        if not changes:
            return employee

        # Reuse the HR path for validation, but only with the allowed subset.
        updated = self.update_employee(employee_id, **changes)

        new_email = changes.get("email")
        if new_email and updated.user_id is not None:
            self._sync_login_email(updated.user_id, new_email, excluding_employee=employee_id)

        logger.info(
            "employee id=%s updated their own profile: %s",
            employee_id, ", ".join(sorted(changes)),
        )
        return updated

    def _sync_login_email(
        self, user_id: int, email: str, *, excluding_employee: int
    ) -> None:
        """Point the linked login at ``email``, refusing collisions.

        The users table also has a unique email, so this is checked separately
        from the employees table.
        """
        from app.features.auth.exceptions import EmailAlreadyRegisteredError

        user = self.auth.get_user(user_id)
        if user is None:
            return
        if (user.email or "").strip().lower() == email:
            return

        if self.auth.get_by_email(email) is not None:
            raise EmailAlreadyRegisteredError(
                f"{email} is already used by another account."
            )

        self.auth.update_email(user, email)

    def change_own_password(
        self, employee_id: int, *, current_password: str, new_password: str
    ) -> None:
        """Change the password of the login attached to this employee.

        The current password is required so a hijacked session cannot lock the
        real owner out.  The change revokes every other session.
        """
        employee = self.get_employee(employee_id)
        if employee.user_id is None:
            raise ProfileNotAvailableError(
                "Your account has no login, so there is no password to change."
            )

        user = self.auth.get_user(employee.user_id)
        if user is None:
            raise ProfileNotAvailableError("Your login no longer exists.")

        self.auth.change_own_password(
            user, current_password=current_password, new_password=new_password
        )
        logger.info("employee id=%s changed their password", employee_id)

    def set_active(self, employee_id: int, is_active: bool) -> Employee:
        """Activate or deactivate an employee, cascading to their login."""
        employee = self.get_employee(employee_id)
        employee.is_active = is_active
        if employee.user_id is not None:
            user = self.auth.get_user(employee.user_id)
            if user is not None:
                self.auth.set_active(user, is_active)
        self.employees.update(employee)
        logger.info("employee id=%s active=%s", employee_id, is_active)
        return employee

    def deactivate(self, employee_id: int) -> Employee:
        return self.set_active(employee_id, False)

    def reactivate(self, employee_id: int) -> Employee:
        return self.set_active(employee_id, True)

    def delete_employee(self, employee_id: int) -> None:
        """Hard delete guard: employees with logins must be deactivated."""
        employee = self.get_employee(employee_id)
        if employee.user_id is not None:
            raise EmployeeHasUserError(
                f"{employee.full_name} has a login account; deactivate instead."
            )
        self.employees.delete(employee)
        logger.info("deleted employee id=%s", employee_id)

    def set_color(self, employee_id: int, color: str | EmployeeColor) -> Employee:
        employee = self.get_employee(employee_id)
        employee.current_color = EmployeeColor.coerce(color).value
        return self.employees.update(employee)

    # ---- presentation helpers -------------------------------------------
    def tenure_label(self, employee: Employee) -> str:
        return employee.tenure_label()

    def today_for(self, employee: Employee) -> date:
        return today_local(employee.tz)
