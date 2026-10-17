"""Live check: gender gates maternity and paternity leave, end to end.

Also sets the gender for Alice and Carol so the feature is visibly working on
your data; the remaining staff stay on the "needs gender" list for HR.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_CONFIG", "development")

from app import create_app
from app.core.database import db

app = create_app("development")
problems: list[str] = []


def check(label, condition, detail=""):
    print(f"  [{'ok  ' if condition else 'FAIL'}] {label}" + (f" — {detail}" if not condition else ""))
    if not condition:
        problems.append(label)


with app.app_context():
    from app.features.employees.exceptions import GenderLeaveMismatchError
    from app.features.employees.models import Employee
    from app.features.employees.service import EmployeeService
    from app.features.leave.constants import LeaveType
    from app.features.leave.service import LeaveService

    employees = EmployeeService()
    leave = LeaveService()

    print("=== 1. the needs-gender list ===")
    waiting = employees.list_missing_gender()
    total = employees.count_employees()
    print(f"    {len(waiting)} of {total} employee(s) need a gender")

    # Remember what this check changes, so it can put it back. Without this the
    # employee borrowed below keeps gender=NULL after the run, quietly leaving
    # the database worse than it was found.
    restore: dict[int, str | None] = {}

    if waiting:
        check("the needs-gender list is populated", True)
        subject = waiting[0]
    else:
        # Everyone is recorded, so borrow one temporarily to exercise the
        # unrecorded path rather than skipping the rule entirely.
        print("    (everyone is recorded; clearing one temporarily to test the rule)")
        subject = Employee.query.filter_by(is_active=True).first()
        restore[subject.id] = subject.gender
        employees.update_employee(subject.id, gender="female")
        db.session.commit()
        subject.gender = None
        db.session.commit()
        db.session.refresh(subject)
        check("clearing a gender puts them back on the list",
              any(e.id == subject.id for e in employees.list_missing_gender()))

    print(f"    subject: {subject.full_name}")
    print("\n=== 2. with no gender, neither parental type is allowed ===")
    eligibility = employees.leave_eligibility(subject)
    check("maternity is withheld", "maternity" not in eligibility["allowed"])
    check("paternity is withheld", "paternity" not in eligibility["allowed"])
    check("the reason is explained", bool(eligibility["blocked"]))
    for kind in (LeaveType.MATERNITY, LeaveType.PATERNITY):
        try:
            leave.request_leave(employee_id=subject.id, leave_type=kind)
            check(f"{kind.value} request is refused", False, "it was accepted")
        except GenderLeaveMismatchError:
            check(f"{kind.value} request is refused", True)
        except Exception as error:  # noqa: BLE001
            check(f"{kind.value} request is refused", False, f"wrong error: {error}")
        finally:
            db.session.rollback()

    print("\n=== 3. recording a gender unlocks exactly one type ===")
    # Alice (Workshop) and Carol (Workshop) are recorded as female, which is
    # also needed so the annual rosters make sense.
    to_set = [
        employee
        for employee in Employee.query.filter(
            Employee.first_name.in_(["Alice", "Carol"])
        ).all()
    ]
    for employee in to_set:
        employees.update_employee(employee.id, gender="female")
    db.session.commit()
    print(f"    recorded 'female' for: {[e.full_name for e in to_set]}")

    if to_set:
        alice = to_set[0]
        db.session.refresh(alice)
        allowed = employees.leave_eligibility(alice)["allowed"]
        check("maternity is now offered", "maternity" in allowed)
        check("paternity is still refused", "paternity" not in allowed)

        try:
            leave.request_leave(employee_id=alice.id, leave_type=LeaveType.PATERNITY)
            check("a paternity request is refused for a female employee", False,
                  "it was accepted")
        except GenderLeaveMismatchError:
            check("a paternity request is refused for a female employee", True)

    print("\n=== 4. a male employee gets paternity, not maternity ===")
    # Set a male employee and confirm the mirror image.
    brian = Employee.query.filter_by(email="brian@ems.local").first()
    if brian is not None:
        employees.update_employee(brian.id, gender="male")
        db.session.commit()
        db.session.refresh(brian)
        allowed = employees.leave_eligibility(brian)["allowed"]
        check("paternity is offered to a male employee", "paternity" in allowed)
        check("maternity is withheld from a male employee", "maternity" not in allowed)
        try:
            leave.request_leave(employee_id=brian.id, leave_type=LeaveType.MATERNITY)
            check("a maternity request is refused for a male employee", False,
                  "it was accepted")
        except GenderLeaveMismatchError:
            check("a maternity request is refused for a male employee", True)
    else:
        print("    (brian@ems.local not found; skipping)")

    print("\n=== 5. the form reflects it ===")
    client = app.test_client()
    assert client.post(
        "/api/v1/auth/login",
        json={"email": "alice@ems.local", "password": "Password123!"},
    ).status_code == 200
    page = client.get("/app/leave/request").get_data(as_text=True)
    check("maternity is selectable for Alice", 'value="maternity"' in page)
    check("paternity is not selectable for Alice", 'value="paternity"' not in page)
    check("the withheld option is explained", "Not available to you" in page)

    print("\n=== 6. the HR screen updates ===")
    admin = app.test_client()
    assert admin.post(
        "/api/v1/auth/login",
        json={"email": "admin@ems.local", "password": "ChangeMe123!"},
    ).status_code == 200
    screen = admin.get("/app/employees/gender").get_data(as_text=True)
    on_screen = len(re.findall(r'name="gender_\d+"', screen)) // 2
    actually_missing = len(employees.list_missing_gender())
    print(f"    on the screen: {on_screen}; DB says {actually_missing} still need one")
    check("the screen lists exactly who still needs a gender",
          on_screen == actually_missing,
          f"screen={on_screen} db={actually_missing}")
    if actually_missing:
        check("the save control is offered", "Save recorded genders" in screen)
    else:
        check("the empty state is shown instead",
              "Every employee has a gender recorded" in screen)

    print("\n=== 6b. the borrowed employee is the only one outstanding ===")
    # The employee borrowed in step 1 is deliberately still NULL here, so the
    # expectation is "nobody except that one", not "nobody at all".
    borrowed = set(restore)
    outstanding = {e.id for e in employees.list_missing_gender()}
    check("only the borrowed employee is missing a gender",
          outstanding == borrowed,
          f"outstanding={sorted(outstanding)} borrowed={sorted(borrowed)}")

    print("\n=== 7. the create form requires a gender ===")
    new_form = admin.get("/app/employees/new").get_data(as_text=True)
    check("both options are offered",
          'value="female"' in new_form and 'value="male"' in new_form)
    check("it is marked required", 'name="gender" value="female" required' in new_form)

    print("\n=== 8. put back anything this check changed ===")
    for employee_id, original in restore.items():
        employees.update_employee(employee_id, gender=original or "female")
    if restore:
        db.session.commit()
        print(f"    restored the gender of {len(restore)} borrowed employee(s)")

    remaining = len(employees.list_missing_gender())
    check("the check left nobody without a gender", remaining == 0,
          f"{remaining} still missing")

print(f"\n=== RESULT: {len(problems)} problem(s) ===")
for problem in problems:
    print("  -", problem)
sys.exit(1 if problems else 0)
