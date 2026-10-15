"""Pluggable notification senders.

Each sender takes a notification row and reports success.  Adding a channel
means adding a sender and registering it in :data:`SENDER_REGISTRY`; nothing
else in the system changes.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger(__name__)


class Sender(Protocol):
    """Delivers one notification on one channel."""

    channel: str

    def send(self, notification) -> None:  # pragma: no cover - protocol
        ...


class InAppSender:
    """In-app delivery is just the row becoming visible in the inbox."""

    channel = "in_app"

    def send(self, notification) -> None:
        logger.debug("in_app notification delivered id=%s", notification.id)


class EmailSender:
    """SMTP delivery.  Silently no-ops when no host is configured."""

    channel = "email"

    def __init__(self, config: dict | None = None) -> None:
        self._config = config

    @property
    def config(self) -> dict:
        if self._config is None:
            from flask import current_app

            self._config = {
                "host": current_app.config.get("SMTP_HOST"),
                "port": current_app.config.get("SMTP_PORT", 25),
                "user": current_app.config.get("SMTP_USER"),
                "password": current_app.config.get("SMTP_PASSWORD"),
                "sender": current_app.config.get("SMTP_FROM", "no-reply@ems.local"),
                "tls": current_app.config.get("SMTP_TLS", False),
            }
        return self._config

    def send(self, notification) -> None:
        config = self.config
        host = config.get("host")
        if not host:
            logger.debug(
                "email skipped (no SMTP host) id=%s", notification.id
            )
            return

        recipient = None
        if notification.recipient_user is not None:
            recipient = notification.recipient_user.email
        elif notification.recipient_employee is not None:
            recipient = notification.recipient_employee.email
        if not recipient:
            raise ValueError("Notification has no email recipient.")

        message = EmailMessage()
        message["Subject"] = notification.subject or "Notification"
        message["From"] = config["sender"]
        message["To"] = recipient
        message.set_content(notification.body or "")

        with smtplib.SMTP(host, int(config.get("port") or 25), timeout=15) as server:
            if config.get("tls"):
                server.starttls(context=ssl.create_default_context())
            if config.get("user"):
                server.login(config["user"], config.get("password") or "")
            server.send_message(message)

        logger.info("email notification sent id=%s to=%s", notification.id, recipient)


class SmsSender:
    """SMS stub: the seam a real provider plugs into.

    Deliberately records intent instead of pretending to deliver, so enabling
    SMS in production forces a conscious implementation.
    """

    channel = "sms"

    def __init__(self, enabled: bool | None = None) -> None:
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        if self._enabled is None:
            from flask import current_app

            self._enabled = bool(current_app.config.get("SMS_ENABLED", False))
        return self._enabled

    def send(self, notification) -> None:
        if not self.enabled:
            logger.debug("sms skipped (disabled) id=%s", notification.id)
            return
        raise NotImplementedError(
            "SMS delivery is not implemented. Register a provider sender in "
            "app.features.notifications.senders.SENDER_REGISTRY."
        )


#: channel -> sender factory
SENDER_REGISTRY: dict[str, type] = {
    "in_app": InAppSender,
    "email": EmailSender,
    "sms": SmsSender,
}


def get_sender(channel: str) -> Sender:
    """Resolve the sender for ``channel``, defaulting to in-app."""
    key = (channel or "in_app").strip().lower()
    factory = SENDER_REGISTRY.get(key)
    if factory is None:
        from app.features.notifications.exceptions import SenderNotConfiguredError

        raise SenderNotConfiguredError(
            f"No sender is registered for channel {channel!r}."
        )
    return factory()
