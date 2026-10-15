"""Employee, department and position views."""

from __future__ import annotations

from datetime import date, timedelta

from flask import flash, redirect, request, url_for

from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.core.time import today_local
from app.features.attendance.service import AttendanceService
from app.features.auth.constants import Role
from app.features.employees.service import (
    DepartmentService,
    EmployeeService,
    PositionService,
)
from app.features.leave.service import LeaveService
from app.features.rules.repository import DisciplinaryActionRepository
from app.features.reports.service import ReportsService
from app.views.blueprint import views_bp
from app.views.helpers import (
    commit_session,
    flash_error,
    flash_success,
    form_bool,
    form_int,
    form_str,
    principal,
    render,
    rollback_session,
    view_login_required,
    view_roles_required,
)

logger = get_logger(__name__)

_employees = EmployeeService()
_departments = DepartmentService()
_positions = PositionService()
_attendance = AttendanceService()
_leave = LeaveService()
_disciplinary = DisciplinaryActionRepository()
_reports = ReportsService()


# ---------------------------------------------------------------------------
# Employee list
# ---------------------------------------------------------------------------
@views_bp.get("/employees")
@view_login_required()
def employees():
    """Rich employee list with KPIs, distribution and per-row health."""
    q = request.args.get("q") or None
    department_id = request.args.get("department_id", type=int)
    is_active_arg = request.args.get("is_active")
    is_active = (
        None if is_active_arg in (None, "", "all") else is_active_arg.lower() == "true"
    )
    color = request.args.get("color") or None

    page = max(1, request.args.get("page", type=int) or 1)
    per_page = min(request.args.get("per_page", type=int) or 25, 100)

    result = _employees.list_employees_paginated(
        page=page,
        per_page=per_page,
        sort="last_name",
        q=q,
        department_id=department_id,
        is_active=is_active,
        color=color,
    )
    rows = result.items
    employee_ids = [employee.id for employee in rows]

    today = today_local(None)
    year_start = date(today.year, 1, 1)

    # Three batched queries instead of per-row lookups.
    health = _attendance.employee_stats(employee_ids, start=year_start, end=today)
    disciplinary_counts = _disciplinary.counts_for_employees(employee_ids)
    on_leave_ids = set(_attendance.employees_on_leave_on(today))

    enriched = []
    for employee in rows:
        stats = health.get(employee.id, {})
        enriched.append(
            {
                "employee": employee,
                "tenure": employee.tenure_label(),
                "late_days": stats.get("late_days", 0),
                "absent_days": stats.get("absent_days", 0),
                "avg_worked_minutes": stats.get("avg_worked_minutes", 0),
                "on_leave_today": employee.id in on_leave_ids,
                "disciplinary_count": disciplinary_counts.get(employee.id, 0),
                "leave_entitlement": _entitlement_for(employee.id, today.year),
            }
        )

    return render(
        "employees/list.html",
        active_nav="Employees",
        rows=enriched,
        page_info=result.info,
        departments=_departments.list_departments(),
        distribution=_departments.distribution(),
        total_active=_employees.count_active(),
        colors=_reports.attendance_colors(),
        filters={
            "q": q or "",
            "department_id": department_id,
            "is_active": is_active_arg or "all",
            "color": color or "",
        },
        today=today,
    )


def _entitlement_for(employee_id: int, year: int) -> dict:
    """Small balance summary used by the list row."""
    balances = _leave.balances_for_employee(employee_id, year=year)
    entitled = sum(balance.entitled for balance in balances)
    used = sum(balance.used for balance in balances)
    return {"entitled": entitled, "used": used, "available": max(0, entitled - used)}


# ---------------------------------------------------------------------------
# Employee detail
# ---------------------------------------------------------------------------
@views_bp.get("/employees/<int:employee_id>")
@view_login_required()
def employee_detail(employee_id: int):
    """Full profile: KPIs, balances, attendance, leave and disciplinary history."""
    try:
        employee = _employees.get_employee(employee_id)
    except AppError as error:
        flash(error.message, "error")
        return redirect(url_for("views.employees"))

    today = today_local(employee.tz)
    year = today.year

    history = _attendance.history(employee.id, limit=60)
    ytd = _attendance.employee_stats(
        [employee.id], start=date(year, 1, 1), end=today
    ).get(employee.id, {})

    balances = _leave.balances_for_employee(employee.id, year=year)
    leave_requests = _leave.my_requests(employee.id)
    upcoming = [
        request
        for request in _leave.my_active(employee.id, today=today)
        if request.end_date >= today
    ]
    actions = _disciplinary.for_employee_with_rules(employee.id)

    # HR otherwise has no way to tell whether the welcome email landed.  A user
    # still holding a provisioned password has not signed in and chosen one yet.
    waiting_on_password: set[int] = set()
    if employee.user_id is not None:
        from app.features.auth.service import AuthService

        user = AuthService().get_user(employee.user_id)
        if user is not None and user.must_change_password:
            waiting_on_password.add(user.id)

    return render(
        "employees/detail.html",
        active_nav="Employees",
        employee=employee,
        waiting_on_password=waiting_on_password,
        today=today,
        tenure=employee.tenure_label(),
        ytd=ytd,
        avg_worked=ytd.get("avg_worked_minutes", 0),
        history=history,
        balances=balances,
        leave_requests=leave_requests[:20],
        upcoming=upcoming,
        actions=actions,
        leave_taken_ytd=sum(
            request.days
            for request in leave_requests
            if request.status in {"approved", "approved_forced"}
            and request.start_date.year == year
        ),
    )


@views_bp.get("/employees/new")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employee_new():
    """Create-employee form."""
    return render(
        "employees/form.html",
        active_nav="Employees",
        departments=_departments.list_departments(),
        positions=_positions.list_positions(),
        form={},
        mode="create",
    )


@views_bp.get("/employees/gender")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employees_gender():
    """Set the recorded gender for staff who have none.

    Gender decides maternity versus paternity leave, so until it is recorded
    neither type can be requested. This is the one-pass screen for that, rather
    than editing each employee individually.
    """
    waiting = _employees.list_missing_gender()
    return render(
        "employees/gender.html",
        active_nav="Employees",
        waiting=waiting,
        total_employees=_employees.count_employees(),
    )


@views_bp.post("/employees/gender")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employees_gender_save():
    """Save every gender chosen on the one-pass screen."""
    from app.features.employees.constants import Gender

    updates: list[tuple[int, str]] = []
    for employee in _employees.list_missing_gender():
        chosen = form_str(f"gender_{employee.id}")
        if chosen in Gender.values():
            updates.append((employee.id, chosen))

    if not updates:
        flash_error("Choose a gender for at least one employee before saving.")
        return redirect(url_for("views.employees_gender"))

    try:
        for employee_id, chosen in updates:
            _employees.update_employee(employee_id, gender=chosen)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.employees_gender"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("bulk gender save failed")
        flash_error("The genders could not be saved.")
        return redirect(url_for("views.employees_gender"))

    flash_success(
        f"Recorded gender for {len(updates)} employee(s). "
        f"Parental leave is now decided for them."
    )
    return redirect(url_for("views.employees_gender"))


@views_bp.post("/employees")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employee_create():
    """Create an employee, optionally provisioning a login."""
    department_id = form_int("department_id")
    password = form_str("password")
    create_user = form_bool("create_user")

    if department_id is None:
        flash("Please choose a department.", "error")
        return redirect(url_for("views.employee_new"))

    if create_user and not password:
        password = _employees._generate_password()

    try:
        employee, initial_password = _employees.create_employee(
            first_name=form_str("first_name", "") or "",
            last_name=form_str("last_name", "") or "",
            email=form_str("email", "") or "",
            department_id=department_id,
            position_id=form_int("position_id"),
            employee_no=form_str("employee_no"),
            phone=form_str("phone"),
            gender=form_str("gender"),
            tz=form_str("tz"),
            hired_at=_parse_date(form_str("hired_at")),
            is_active=form_bool("is_active", True),
            create_user=create_user,
            user_role=form_str("user_role"),
            password=password,
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.employee_new"))
    except Exception:  # noqa: BLE001 - unexpected failure still rolls back
        rollback_session()
        logger.exception("employee create failed")
        flash("The employee could not be created.", "error")
        return redirect(url_for("views.employee_new"))

    if initial_password:
        # The password is emailed, not displayed: the administrator does not need
        # to know it, and putting it on screen invites it being written down.
        flash(
            f"{employee.full_name} created. Their sign-in details, including the "
            f"temporary password, have been emailed to {employee.email}. They will "
            f"be asked to choose their own password when they sign in, and they "
            f"have been reminded to see HR about biometric registration.",
            "success",
        )
    else:
        flash(f"{employee.full_name} created.", "success")
    return redirect(url_for("views.employee_detail", employee_id=employee.id))


@views_bp.post("/employees/<int:employee_id>/resend-welcome")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employee_resend_welcome(employee_id: int):
    """Issue a fresh temporary password and email it again.

    Useful when the first email bounced or was mistyped.  The previous password
    stops working immediately, so a welcome email sitting in a wrong inbox
    cannot leave a usable credential behind.
    """
    try:
        _employees.resend_welcome(employee_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash_error(error.message)
        return redirect(url_for("views.employee_detail", employee_id=employee_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("resend welcome failed for employee %s", employee_id)
        flash_error("The welcome email could not be resent.")
        return redirect(url_for("views.employee_detail", employee_id=employee_id))

    flash_success(
        "A new temporary password was generated and emailed. The previous one no "
        "longer works."
    )
    return redirect(url_for("views.employee_detail", employee_id=employee_id))


@views_bp.post("/employees/<int:employee_id>/set_active/<any(true,false):state>")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def employee_set_active(employee_id: int, state: str):
    """Activate or deactivate an employee."""
    is_active = state == "true"
    try:
        employee = _employees.set_active(employee_id, is_active)
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.employee_detail", employee_id=employee_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("set_active failed")
        flash("The change could not be saved.", "error")
        return redirect(url_for("views.employee_detail", employee_id=employee_id))

    flash(
        f"{employee.full_name} {'reactivated' if is_active else 'deactivated'}.",
        "success",
    )
    return redirect(url_for("views.employee_detail", employee_id=employee_id))


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------
@views_bp.get("/departments")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def departments():
    """Department list with headcounts and positions."""
    all_departments = _departments.list_departments()
    counts = _departments.employee_counts()
    return render(
        "departments/list.html",
        active_nav="Departments",
        departments=all_departments,
        counts=counts,
        distribution=_departments.distribution(),
    )


@views_bp.route("/departments/new", methods=["GET", "POST"])
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def department_new():
    """Create a department."""
    if request.method == "GET":
        return render("departments/form.html", active_nav="Departments", form={}, mode="create")

    try:
        department = _departments.create_department(
            name=form_str("name", "") or "", code=form_str("code", "") or ""
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return render(
            "departments/form.html",
            active_nav="Departments",
            form=request.form,
            mode="create",
        )
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("department create failed")
        flash("The department could not be created.", "error")
        return render(
            "departments/form.html",
            active_nav="Departments",
            form=request.form,
            mode="create",
        )

    flash(f"Department {department.code} created.", "success")
    return redirect(url_for("views.departments"))


@views_bp.route("/departments/<int:department_id>/edit", methods=["GET", "POST"])
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def department_edit(department_id: int):
    """Rename a department."""
    try:
        department = _departments.get_department(department_id)
    except AppError as error:
        flash(error.message, "error")
        return redirect(url_for("views.departments"))

    if request.method == "GET":
        return render(
            "departments/form.html",
            active_nav="Departments",
            form={"name": department.name, "code": department.code},
            department=department,
            mode="edit",
        )

    try:
        _departments.update_department(
            department_id,
            name=form_str("name"),
            code=form_str("code"),
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.department_edit", department_id=department_id))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("department update failed")
        flash("The department could not be updated.", "error")
        return redirect(url_for("views.department_edit", department_id=department_id))

    flash("Department updated.", "success")
    return redirect(url_for("views.departments"))


@views_bp.post("/departments/<int:department_id>/delete")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def department_delete(department_id: int):
    """Delete an empty department."""
    try:
        _departments.delete_department(department_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.departments"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("department delete failed")
        flash("The department could not be deleted.", "error")
        return redirect(url_for("views.departments"))

    flash("Department deleted.", "success")
    return redirect(url_for("views.departments"))


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
@views_bp.route("/departments/<int:department_id>/positions/new", methods=["GET", "POST"])
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def position_new(department_id: int):
    """Add a position to a department."""
    try:
        department = _departments.get_department(department_id)
    except AppError as error:
        flash(error.message, "error")
        return redirect(url_for("views.departments"))

    if request.method == "GET":
        return render(
            "positions/form.html",
            active_nav="Departments",
            department=department,
            form={},
            mode="create",
        )

    try:
        _positions.create_position(
            title=form_str("title", "") or "", department_id=department_id
        )
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return render(
            "positions/form.html",
            active_nav="Departments",
            department=department,
            form=request.form,
            mode="create",
        )
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("position create failed")
        flash("The position could not be created.", "error")
        return redirect(url_for("views.departments"))

    flash("Position created.", "success")
    return redirect(url_for("views.departments"))


@views_bp.route("/positions/<int:position_id>/edit", methods=["GET", "POST"])
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def position_edit(position_id: int):
    """Retitle a position."""
    try:
        position = _positions.get_position(position_id)
    except AppError as error:
        flash(error.message, "error")
        return redirect(url_for("views.departments"))

    if request.method == "GET":
        return render(
            "positions/form.html",
            active_nav="Departments",
            department=position.department,
            position=position,
            form={"title": position.title},
            mode="edit",
        )

    try:
        _positions.update_position(position_id, title=form_str("title"))
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.departments"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("position update failed")
        flash("The position could not be updated.", "error")
        return redirect(url_for("views.departments"))

    flash("Position updated.", "success")
    return redirect(url_for("views.departments"))


@views_bp.post("/positions/<int:position_id>/delete")
@view_login_required()
@view_roles_required(Role.HR, Role.ADMIN)
def position_delete(position_id: int):
    """Delete an unoccupied position."""
    try:
        _positions.delete_position(position_id)
        commit_session()
    except AppError as error:
        rollback_session()
        flash(error.message, "error")
        return redirect(url_for("views.departments"))
    except Exception:  # noqa: BLE001
        rollback_session()
        logger.exception("position delete failed")
        flash("The position could not be deleted.", "error")
        return redirect(url_for("views.departments"))

    flash("Position deleted.", "success")
    return redirect(url_for("views.departments"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
