"""Render the welcome email exactly as a new employee would receive it."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FLASK_CONFIG"] = "development"

from app import create_app
from app.core.database import db

app = create_app("development")

with app.app_context():
    from app.features.email.models import EmailLog
    from app.features.email.senders import NullTransport
    from app.features.email.service import EmailService
    from app.features.employees.service import EmployeeService

    EmailLog.query.delete()
    db.session.commit()

    password = EmployeeService._generate_password()
    EmailService(transport=NullTransport()).send_welcome(
        to_email="new.starter@example.com",
        first_name="Amina",
        employee_no="EMP-0042",
        temporary_password=password,
        role="EMPLOYEE",
        department_name="Finance",
    )
    db.session.commit()

    row = EmailLog.query.filter_by(event_type="welcome").first()
    print("SUBJECT:", row.subject)
    print("=" * 72)
    print(row.body_text)
    print("=" * 72)
    print(f"delivery status: {row.status}")

    EmailLog.query.delete()
    db.session.commit()
    print("(cleaned up)")
