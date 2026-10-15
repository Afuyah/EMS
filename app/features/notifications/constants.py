"""Fixed strings and enums for the notifications feature."""

from __future__ import annotations

from enum import Enum


class Channel(str, Enum):
    """Delivery channels."""

    IN_APP = "in_app"
    EMAIL = "email"
    SMS = "sms"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "Channel":
        return value if isinstance(value, cls) else cls(str(value).lower())


class NotificationStatus(str, Enum):
    """Delivery state."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    READ = "read"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "NotificationStatus":
        return value if isinstance(value, cls) else cls(str(value).lower())

    @property
    def is_terminal(self) -> bool:
        return self in {NotificationStatus.SENT, NotificationStatus.READ, NotificationStatus.FAILED}


#: Attempts before a notification is parked as failed.
MAX_DELIVERY_ATTEMPTS = 5

#: Rows pulled per dispatcher run.
DISPATCH_BATCH_SIZE = 100

#: Generic event types used across features.
EVENT_SYSTEM = "system"
EVENT_WELCOME = "welcome"

#: Channel priority when a feature does not state one.
DEFAULT_CHANNEL = Channel.IN_APP.value
