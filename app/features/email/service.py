"""Email service: queue, render, deliver and retry outbound mail.

Two invariants make this robust:

1. **The row is committed before the transport is touched.**  A crash, a hung
   SMTP server or a process restart cannot lose the fact that an email was due;
   it stays ``pending`` and the retry job picks it up.
2. **A delivery failure never propagates into the business transaction.**  Call
   sites invoke :meth:`send_safely`, so a broken mail server cannot roll back a
   leave approval.
"""

from __future__ import annotations

from datetime import timedelta

from jinja2 import Environment, PackageLoader, select_autoescape

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.email.constants import (
    CATEGORY_GENERAL,
    EVENT_WELCOME,
    HR_RECIPIENT_ROLES,
    EmailStatus,
)
from app.features.email.exceptions import EmailError, MissingRecipientError
from app.features.email.models import EmailLog
from app.features.email.repository import EmailRepository
from app.features.email.senders import OutboundMessage, get_transport

logger = get_logger(__name__)

#: Subject prefix keeps EMS mail identifiable in a busy inbox.
DEFAULT_MAX_ATTEMPTS = 3

#: Emails are rendered by a dedicated loader so mail templates can never
#: accidentally render a full web page.
_email_environment = Environment(
    loader=PackageLoader("app.features.email", "templates"),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


def _dedent(text: str) -> str:
    """Remove the four-space content indent used by the mail templates.

    Jinja has no ``dedent`` filter, and indenting templates for readability
    would otherwise leak into every delivered email body.
    """
    return "\n".join(
        line[4:] if line.startswith("    ") else line for line in text.split("\n")
    ).strip()


def render_email(event_type: str, context: dict) -> tuple[str, str, str]:
    """Render ``(subject, text, html)`` for an event.

    The subject comes from a ``{% set subject = "..." %}`` line at the top of the
    template, which Jinja exposes as a module attribute.  A missing template is
    not fatal: the caller's explicit subject/body are used instead, so adding an
    event never requires a template first.
    """
    from flask import current_app

    prefix = current_app.config.get("EMAIL_SUBJECT_PREFIX", "[EMS]")

    try:
        template = _email_environment.get_template(f"{event_type}.txt")
    except Exception:  # noqa: BLE001 - no template for this event yet
        return "", "", ""

    module = template.make_module(context)
    subject = str(getattr(module, "subject", "") or "").strip()
    body = _dedent(module.__str__())

    if not subject:
        # Fall back to the first body line so a template without an explicit
        # subject still produces something sensible.
        lines = [line for line in body.splitlines() if line.strip()]
        subject = lines[0] if lines else event_type.replace("_", " ").title()
        body = "\n".join(lines[1:])

    if prefix and not subject.startswith(prefix):
        subject = f"{prefix} {subject}"

    html = ""
    try:
        html = _dedent(
            _email_environment.get_template(f"{event_type}.html").render(**context)
        )
    except Exception:  # noqa: BLE001 - text-only is an acceptable fallback
        html = ""

    return subject, body, html


class EmailService:
    """Use-cases for outbound email."""

    def __init__(
        self,
        emails: EmailRepository | None = None,
        transport=None,
    ) -> None:
        self.emails = emails or EmailRepository()
        self._transport = transport

    # ---- configuration ---------------------------------------------------
    @staticmethod
    def _config(key: str, default=None):
        from flask import current_app

        try:
            return current_app.config.get(key, default)
        except RuntimeError:  # no app context (CLI/tests)
            return default

    @property
    def backend(self) -> str:
        return str(self._config("EMAIL_BACKEND", "console") or "console").lower()

    @property
    def enabled(self) -> bool:
        return bool(self._config("EMAIL_ENABLED", True))

    @property
    def max_attempts(self) -> int:
        return int(self._config("EMAIL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS) or 3)

    def transport(self):
        """Resolve the transport once per service instance."""
        if self._transport is None:
            self._transport = get_transport(self.backend)
        return self._transport

    # ---- queue -----------------------------------------------------------
    def queue(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        event_type: str = CATEGORY_GENERAL,
        body_html: str | None = None,
        cc_email: str | None = None,
        recipient_user_id: int | None = None,
        recipient_employee_id: int | None = None,
        payload: dict | None = None,
    ) -> EmailLog:
        """Record an email as pending.  Flushes only — the caller commits."""
        address = (to_email or "").strip()
        if not address or "@" not in address:
            raise MissingRecipientError(
                f"Cannot queue email {event_type!r}: {to_email!r} is not a valid address."
            )

        record = self.emails.create(
            event_type=event_type,
            to_email=address,
            cc_email=(cc_email or None),
            subject=(subject or event_type.replace("_", " ").title())[:300],
            body_text=body_text,
            body_html=body_html,
            recipient_user_id=recipient_user_id,
            recipient_employee_id=recipient_employee_id,
            payload=payload,
            status=EmailStatus.PENDING.value,
            backend=self.backend,
            attempts=0,
        )
        return record

    # ---- delivery --------------------------------------------------------
    def deliver(self, record: EmailLog) -> bool:
        """Attempt one delivery of an already-persisted email row.

        Returns ``True`` on success.  Never raises for transport problems: the
        failure is recorded on the row so it can be retried and inspected.
        """
        transport = None
        try:
            transport = self.transport()
        except EmailError as error:
            self.emails.mark_failed(record, error=str(error), backend=self.backend)
            logger.error("email backend unusable: %s", error)
            return False

        message = OutboundMessage(
            to_email=record.to_email,
            subject=record.subject,
            body_text=record.body_text or "",
            body_html=record.body_html,
            cc_email=record.cc_email,
            headers={"X-EMS-Event": record.event_type},
        )

        self.emails.bump_attempt(record)
        try:
            transport.deliver(message)
        except Exception as error:  # noqa: BLE001 - transport failures are data
            self.emails.mark_failed(record, error=str(error), backend=transport.name)
            logger.warning(
                "email delivery failed to=%s event=%s attempt=%s: %s",
                record.to_email, record.event_type, record.attempts, error,
            )
            return False

        self.emails.mark_sent(record, backend=transport.name)
        return True

    def send(
        self,
        *,
        to_email: str,
        subject: str,
        body_text: str,
        event_type: str = CATEGORY_GENERAL,
        **kwargs,
    ) -> EmailLog | None:
        """Queue and deliver an email, committing the attempt first.

        Returns the email row (``None`` when email is disabled or unaddressable).
        """
        if not self.enabled:
            logger.debug("email disabled; skipping %s to %s", event_type, to_email)
            return None

        try:
            record = self.queue(
                to_email=to_email,
                subject=subject,
                body_text=body_text,
                event_type=event_type,
                **kwargs,
            )
        except MissingRecipientError as error:
            logger.warning("skipping email: %s", error)
            return None

        # Commit *before* touching the network so the intent to send survives a
        # crash mid-delivery.
        db.session.commit()

        self.deliver(record)
        db.session.commit()
        return record

    def send_safely(self, **kwargs) -> EmailLog | None:
        """``send`` that can never break the caller's transaction.

        Use this from business services: a mail outage must not roll back a
        leave approval or an attendance correction.
        """
        try:
            return self.send(**kwargs)
        except Exception as error:  # noqa: BLE001 - email is best effort
            db.session.rollback()
            logger.warning(
                "email send failed and was contained (event=%s to=%s): %s",
                kwargs.get("event_type"), kwargs.get("to_email"), error,
            )
            return None

    def send_event(
        self,
        *,
        event_type: str,
        to_email: str,
        context: dict,
        subject: str | None = None,
        body_text: str | None = None,
        cc_email: str | None = None,
        recipient_user_id: int | None = None,
        recipient_employee_id: int | None = None,
        payload: dict | None = None,
    ) -> EmailLog | None:
        """Render a template for ``event_type`` and send it.

        Explicit ``subject``/``body_text`` win when no template exists.
        """
        rendered_subject, rendered_body, rendered_html = render_email(
            event_type, context
        )
        return self.send_safely(
            to_email=to_email,
            subject=subject or rendered_subject or event_type.replace("_", " ").title(),
            body_text=body_text or rendered_body or "",
            body_html=rendered_html or None,
            event_type=event_type,
            cc_email=cc_email,
            recipient_user_id=recipient_user_id,
            recipient_employee_id=recipient_employee_id,
            payload=payload,
        )

    # ---- account provisioning -------------------------------------------
    def send_welcome(
        self,
        *,
        to_email: str,
        first_name: str,
        employee_no: str,
        temporary_password: str,
        role: str | None = None,
        recipient_user_id: int | None = None,
        recipient_employee_id: int | None = None,
        department_name: str | None = None,
    ) -> EmailLog | None:
        """Send sign-in details to a newly provisioned user.

        The password is included because the account was created on the user's
        behalf and they have no other way to learn it.  That is only safe
        because the account is flagged ``must_change_password``, so this
        credential is single-use in practice.

        Sent through ``send_safely``: a mail outage must not roll back the
        employee record the administrator just created.
        """
        from flask import current_app

        base = str(current_app.config.get("APP_BASE_URL", "")).rstrip("/")
        return self.send_event(
            event_type=EVENT_WELCOME,
            to_email=to_email,
            context={
                "first_name": first_name,
                "email": to_email,
                "employee_no": employee_no,
                "temporary_password": temporary_password,
                "role": (role or "EMPLOYEE").replace("_", " ").title(),
                "department_name": department_name or "—",
                "company_name": current_app.config.get(
                    "APP_NAME", "Employee Management System"
                ),
                "hr_contact": current_app.config.get("HR_CONTACT", "the HR office"),
                "login_url": f"{base}/app/login" if base else "/app/login",
            },
            recipient_user_id=recipient_user_id,
            recipient_employee_id=recipient_employee_id,
            payload={"employee_no": employee_no, "role": role},
        )

    # ---- retries ---------------------------------------------------------
    def retry_pending(self, *, limit: int = 50, min_age_seconds: int = 30) -> dict:
        """Re-attempt pending emails, giving up after ``max_attempts``.

        Only rows older than ``min_age_seconds`` are retried so an in-flight
        first attempt is not duplicated.
        """
        cutoff = utcnow() - timedelta(seconds=min_age_seconds)
        records = self.emails.pending(
            limit=limit, max_attempts=self.max_attempts, older_than=cutoff
        )

        sent = 0
        failed = 0
        for record in records:
            if self.deliver(record):
                sent += 1
            else:
                failed += 1
                # Out of attempts: park it as permanently failed.
                if (record.attempts or 0) >= self.max_attempts:
                    self.emails.mark_failed(
                        record,
                        error=record.last_error or "exceeded retry limit",
                        backend=self.backend,
                    )

        db.session.commit()
        summary = {"attempted": len(records), "sent": sent, "failed": failed}
        if records:
            logger.info("email retry: %s", summary)
        return summary

    # ---- reads -----------------------------------------------------------
    def recent(self, *, limit: int = 100) -> list[EmailLog]:
        return list(self.emails.recent(limit=limit))

    def stats(self) -> dict:
        counts = self.emails.counts()
        return {
            "sent": counts.get(EmailStatus.SENT.value, 0),
            "pending": counts.get(EmailStatus.PENDING.value, 0),
            "failed": counts.get(EmailStatus.FAILED.value, 0),
            "total": sum(counts.values()),
            "backend": self.backend,
            "enabled": self.enabled,
        }

    def by_event(self, *, limit: int = 10) -> list[dict]:
        return self.emails.count_by_event(limit=limit)

    # ---- recipients ------------------------------------------------------
    def resolve_recipients(
        self,
        *,
        roles: tuple[str, ...] = (),
        department_id: int | None = None,
        include_department_managers: bool = False,
        exclude_user_ids: set[int] | None = None,
    ) -> list[dict]:
        """Resolve who should receive an organisational email.

        Crossing into the auth feature is done through its **service**, and the
        whole lookup is wrapped: if recipient resolution fails we return an empty
        list rather than breaking the business action that triggered the email.
        """
        excluded = exclude_user_ids or set()
        recipients: list[dict] = []
        seen: set[str] = set()

        def _add(user) -> None:
            address = (getattr(user, "email", None) or "").strip()
            if not address or "@" not in address:
                return
            if user.id in excluded or address.lower() in seen:
                return
            seen.add(address.lower())
            recipients.append(
                {
                    "email": address,
                    "user_id": user.id,
                    "role": user.role,
                }
            )

        try:
            from app.features.auth.service import AuthService

            auth = AuthService()

            for role in roles:
                for user in auth.list_users_by_role(role):
                    if user.is_active:
                        _add(user)

            if include_department_managers and department_id is not None:
                for user in auth.managers_for_department(department_id):
                    _add(user)
        except Exception as error:  # noqa: BLE001 - never break the caller
            logger.warning("recipient resolution failed: %s", error)
            return []

        return recipients

    def send_to_roles(
        self,
        *,
        event_type: str,
        context: dict,
        roles: tuple[str, ...] = (),
        department_id: int | None = None,
        include_department_managers: bool = False,
        exclude_user_ids: set[int] | None = None,
        cc_email: str | None = None,
    ) -> int:
        """Render and send one email per resolved recipient.

        Returns how many addresses were attempted.
        """
        recipients = self.resolve_recipients(
            roles=roles,
            department_id=department_id,
            include_department_managers=include_department_managers,
            exclude_user_ids=exclude_user_ids,
        )
        for recipient in recipients:
            self.send_event(
                event_type=event_type,
                to_email=recipient["email"],
                context=context,
                cc_email=cc_email,
                recipient_user_id=recipient["user_id"],
                payload=context.get("payload"),
            )
        return len(recipients)
