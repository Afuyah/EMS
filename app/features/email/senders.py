"""Pluggable outbound email transports.

Each sender performs one delivery attempt and either returns normally or raises.
Retry policy lives in :mod:`app.features.email.service`, not here.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as StdlibEmailMessage
from typing import Protocol

from app.core.logging import get_logger
from app.features.email.exceptions import EmailBackendError, EmailDeliveryError

logger = get_logger(__name__)


@dataclass
class OutboundMessage:
    """A fully rendered message, ready to hand to a transport."""

    to_email: str
    subject: str
    body_text: str = ""
    body_html: str | None = None
    cc_email: str | None = None
    from_email: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def cc_list(self) -> list[str]:
        if not self.cc_email:
            return []
        return [part.strip() for part in self.cc_email.split(",") if part.strip()]


class EmailTransport(Protocol):
    """Delivers one message."""

    name: str

    def deliver(self, message: OutboundMessage) -> None:  # pragma: no cover
        ...


class ConsoleTransport:
    """Logs the email instead of sending it, and reports success.

    This is the development default so the whole pipeline — rendering, retry,
    auditing, links — is exercised without needing SMTP credentials.
    """

    name = "console"

    def deliver(self, message: OutboundMessage) -> None:
        separator = "-" * 72
        logger.info(
            "\n%s\nEMAIL (console backend)\n"
            "  to      : %s\n"
            "  cc      : %s\n"
            "  subject : %s\n%s\n%s\n%s",
            separator,
            message.to_email,
            message.cc_email or "-",
            message.subject,
            separator,
            message.body_text or "(no text body)",
            separator,
        )


class NullTransport:
    """Records nothing and sends nothing.  Used by tests."""

    name = "null"

    def deliver(self, message: OutboundMessage) -> None:
        return None


class SmtpTransport:
    """Real SMTP delivery.

    Supports implicit TLS (port 465), STARTTLS (usually 587), or plain SMTP for
    a local relay.  Credentials are optional so a local mailcatcher works with
    no configuration.
    """

    name = "smtp"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config

    @property
    def config(self) -> dict:
        if self._config is None:
            from flask import current_app

            self._config = {
                "host": current_app.config.get("SMTP_HOST"),
                "port": int(current_app.config.get("SMTP_PORT") or 25),
                "user": current_app.config.get("SMTP_USER"),
                "password": current_app.config.get("SMTP_PASSWORD"),
                "sender": current_app.config.get("SMTP_FROM", "no-reply@ems.local"),
                "tls": bool(current_app.config.get("SMTP_TLS")),
                "ssl": bool(current_app.config.get("SMTP_SSL")),
                "timeout": int(current_app.config.get("SMTP_TIMEOUT") or 20),
            }
        return self._config

    def deliver(self, message: OutboundMessage) -> None:
        config = self.config
        host = config.get("host")
        if not host:
            raise EmailBackendError(
                "SMTP_HOST is not configured, so nothing could be sent."
            )

        sender = message.from_email or config["sender"]
        payload = StdlibEmailMessage()
        payload["Subject"] = message.subject
        payload["From"] = sender
        payload["To"] = message.to_email
        if message.cc_list:
            payload["Cc"] = ", ".join(message.cc_list)
        for key, value in message.headers.items():
            payload[key] = value

        payload.set_content(message.body_text or "")
        if message.body_html:
            payload.add_alternative(message.body_html, subtype="html")

        recipients = [message.to_email, *message.cc_list]

        try:
            if config.get("ssl"):
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(
                    host, config["port"], timeout=config["timeout"], context=context
                ) as server:
                    self._authenticate_and_send(server, config, sender, recipients, payload)
            else:
                with smtplib.SMTP(host, config["port"], timeout=config["timeout"]) as server:
                    server.ehlo()
                    if config.get("tls"):
                        server.starttls(context=ssl.create_default_context())
                        server.ehlo()
                    self._authenticate_and_send(server, config, sender, recipients, payload)
        except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
            # Wrapped so the caller has one error type to catch and record.
            raise EmailDeliveryError(
                f"SMTP delivery to {message.to_email} failed: {error}"
            ) from error

        logger.info(
            "email sent to=%s subject=%r via=%s:%s",
            message.to_email, message.subject, host, config["port"],
        )

    @staticmethod
    def _authenticate_and_send(server, config, sender, recipients, payload) -> None:
        if config.get("user"):
            server.login(config["user"], config.get("password") or "")
        server.send_message(payload, from_addr=sender, to_addrs=recipients)


#: backend name -> transport class
TRANSPORT_REGISTRY: dict[str, type] = {
    "console": ConsoleTransport,
    "null": NullTransport,
    "smtp": SmtpTransport,
}


def get_transport(backend: str | None) -> EmailTransport:
    """Resolve a transport for ``backend``, defaulting to console."""
    from app.features.email.constants import EmailBackend

    try:
        kind = EmailBackend.coerce(backend)
    except ValueError as error:
        # Surface a domain error rather than a raw ValueError, so callers only
        # ever have to handle AppError.
        raise EmailBackendError(
            f"Unknown email backend {backend!r}. "
            f"Known backends: {', '.join(sorted(TRANSPORT_REGISTRY))}."
        ) from error

    factory = TRANSPORT_REGISTRY.get(kind.value)
    if factory is None:
        raise EmailBackendError(
            f"Unknown email backend {backend!r}. "
            f"Known backends: {', '.join(sorted(TRANSPORT_REGISTRY))}."
        )
    return factory()
