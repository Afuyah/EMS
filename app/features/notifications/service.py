"""Notification service: enqueue, read and flush.

``enqueue`` deliberately **flushes without committing**.  The caller owns the
transaction, which is what lets a leave approval and its notification commit
together, and what stops a notification failure from poisoning the leave
transaction.
"""

from __future__ import annotations

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.notifications.constants import (
    DEFAULT_CHANNEL,
    DISPATCH_BATCH_SIZE,
    MAX_DELIVERY_ATTEMPTS,
    Channel,
    NotificationStatus,
)
from app.features.notifications.exceptions import NotificationNotFoundError
from app.features.notifications.models import Notification
from app.features.notifications.repository import NotificationRepository
from app.features.notifications.senders import get_sender

logger = get_logger(__name__)


class NotificationService:
    """Use-cases for the notification inbox and the delivery queue."""

    def __init__(
        self,
        notifications: NotificationRepository | None = None,
        senders: dict | None = None,
    ) -> None:
        self.notifications = notifications or NotificationRepository()
        self._senders = senders or {}

    # ---- enqueue ---------------------------------------------------------
    def enqueue(
        self,
        *,
        event_type: str,
        subject: str,
        body: str,
        recipient_user_id: int | None = None,
        recipient_employee_id: int | None = None,
        channel: str | Channel = DEFAULT_CHANNEL,
        payload: dict | None = None,
    ) -> Notification:
        """Queue a notification for delivery.

        Flushes only: the caller commits.  A delivery failure surfaces later in
        the dispatcher, never here.
        """
        record = self.notifications.create(
            recipient_user_id=recipient_user_id,
            recipient_employee_id=recipient_employee_id,
            event_type=event_type,
            channel=Channel.coerce(channel).value,
            status=NotificationStatus.PENDING.value,
            subject=subject[:255] if subject else None,
            body=body,
            payload=payload,
            attempts=0,
        )
        return record

    def notify_user(
        self,
        *,
        user_id: int,
        event_type: str,
        subject: str,
        body: str,
        channel: str | Channel = DEFAULT_CHANNEL,
        payload: dict | None = None,
        employee_id: int | None = None,
    ) -> Notification:
        """Convenience wrapper when only the user id is known."""
        return self.enqueue(
            event_type=event_type,
            subject=subject,
            body=body,
            recipient_user_id=user_id,
            recipient_employee_id=employee_id,
            channel=channel,
            payload=payload,
        )

    def broadcast(
        self,
        *,
        user_ids: list[int],
        event_type: str,
        subject: str,
        body: str,
        channel: str | Channel = DEFAULT_CHANNEL,
        payload: dict | None = None,
    ) -> list[Notification]:
        """Queue the same notification for several recipients."""
        return [
            self.enqueue(
                event_type=event_type,
                subject=subject,
                body=body,
                recipient_user_id=user_id,
                channel=channel,
                payload=payload,
            )
            for user_id in user_ids
        ]

    # ---- reads -----------------------------------------------------------
    def inbox(
        self,
        *,
        user_id: int,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        return list(
            self.notifications.inbox(
                recipient_user_id=user_id,
                unread_only=unread_only,
                limit=limit,
                offset=offset,
            )
        )

    def unread_count(self, *, user_id: int) -> int:
        return self.notifications.unread_count(recipient_user_id=user_id)

    def recent(self, *, user_id: int, limit: int = 8) -> list[Notification]:
        return list(
            self.notifications.recent_for_user(recipient_user_id=user_id, limit=limit)
        )

    def get(self, notification_id: int) -> Notification:
        record = self.notifications.get(notification_id)
        if record is None:
            raise NotificationNotFoundError(
                f"Notification {notification_id} was not found."
            )
        return record

    def status_counts(self) -> dict[str, int]:
        return {
            row["status"]: int(row["total"] or 0)
            for row in self.notifications.count_by_status()
        }

    def channel_counts(self) -> dict[str, int]:
        return {
            row["channel"]: int(row["total"] or 0)
            for row in self.notifications.count_by_channel()
        }

    # ---- mark read -------------------------------------------------------
    def mark_read(self, *, notification_id: int, user_id: int) -> Notification:
        """Mark one notification read, refusing to touch another user's row."""
        record = self.get(notification_id)
        if record.recipient_user_id not in (None, user_id):
            # Same error as "missing" so inbox contents cannot be probed.
            raise NotificationNotFoundError(
                f"Notification {notification_id} was not found."
            )
        return self.notifications.mark_read(record)

    def mark_all_read(self, *, user_id: int) -> int:
        return self.notifications.mark_all_read(recipient_user_id=user_id)

    # ---- dispatcher ------------------------------------------------------
    def dispatch_pending(self, *, limit: int = DISPATCH_BATCH_SIZE) -> dict:
        """Attempt delivery of queued notifications.

        Returns a small summary.  Each row is independent: one failure never
        aborts the batch.
        """
        pending = self.notifications.pending_batch(
            limit=limit, max_attempts=MAX_DELIVERY_ATTEMPTS
        )
        sent = 0
        failed = 0
        skipped = 0

        for record in pending:
            try:
                sender = self._sender_for(record.channel)
            except Exception as error:  # noqa: BLE001 - unknown channel
                self.notifications.bump_attempt(record)
                self.notifications.mark_failed(record, error=str(error))
                failed += 1
                logger.warning("notification id=%s unknown channel", record.id)
                continue

            # An in-app notification is "sent" the moment it exists.
            if record.channel == Channel.IN_APP.value:
                self.notifications.mark_sent(record)
                sent += 1
                continue

            try:
                sender.send(record)
            except NotImplementedError as error:
                # Channel intentionally unimplemented: leave it queued rather
                # than burning attempts and marking it failed.
                skipped += 1
                logger.debug("notification id=%s skipped: %s", record.id, error)
                continue
            except Exception as error:  # noqa: BLE001 - transport failures
                self.notifications.bump_attempt(record)
                if (record.attempts or 0) >= MAX_DELIVERY_ATTEMPTS:
                    self.notifications.mark_failed(record, error=str(error))
                    failed += 1
                    logger.error(
                        "notification id=%s failed permanently: %s", record.id, error
                    )
                else:
                    logger.warning(
                        "notification id=%s attempt %s failed: %s",
                        record.id, record.attempts, error,
                    )
                continue

            self.notifications.mark_sent(record)
            sent += 1

        logger.info(
            "dispatcher processed=%s sent=%s failed=%s skipped=%s",
            len(pending), sent, failed, skipped,
        )
        return {
            "processed": len(pending),
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }

    def _sender_for(self, channel: str):
        """Resolve a sender, memoising instances for the process."""
        key = (channel or DEFAULT_CHANNEL).strip().lower()
        if key not in self._senders:
            self._senders[key] = get_sender(key)
        return self._senders[key]

    def purge_read(self, *, user_id: int, older_than=None) -> int:
        """Housekeeping: drop read notifications (optionally age-limited)."""
        records = self.notifications.list(
            recipient_user_id=user_id, status=NotificationStatus.READ.value
        )
        removed = 0
        for record in records:
            if older_than is not None and record.read_at and record.read_at > older_than:
                continue
            db.session.delete(record)
            removed += 1
        db.session.flush()
        return removed
