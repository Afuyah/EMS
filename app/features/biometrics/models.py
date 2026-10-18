"""Biometrics tables: devices, employee mappings, append-only raw logs.

``biometric_raw_logs`` is append-only for audit: it is never updated or
deleted, only inserted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import CreatedAtMixin, TimestampMixin, db
from app.features.biometrics.constants import DEFAULT_VENDOR, PunchType


class BiometricDevice(TimestampMixin, db.Model):
    """A registered punch clock."""

    __tablename__ = "biometric_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    serial_no: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    location: Mapped[str | None] = mapped_column(String(160))
    vendor: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DEFAULT_VENDOR
    )
    secret_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Last time this device authenticated and delivered data ("working").
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Last time *any* request arrived claiming this serial ("knocking"), even one
    #: that failed authentication.
    #:
    #: These answer two different questions, and conflating them is misleading:
    #: a terminal with a mistyped CommKey is reaching the server perfectly well
    #: while being refused, and would otherwise look like it had never connected
    #: — sending someone to check cables when the credential is the problem.
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mappings: Mapped[list["BiometricEmployeeMap"]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    raw_logs: Mapped[list["BiometricRawLog"]] = relationship(
        back_populates="device",
        passive_deletes=True,
    )

    @property
    def is_online(self) -> bool:
        from app.features.biometrics.constants import DEVICE_OFFLINE_AFTER_MINUTES
        from app.core.time import utcnow

        if self.last_seen_at is None:
            return False
        delta = utcnow() - self.last_seen_at
        return delta.total_seconds() <= DEVICE_OFFLINE_AFTER_MINUTES * 60

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<BiometricDevice {self.serial_no} {self.vendor}>"


class BiometricEmployeeMap(db.Model):
    """Maps a device-local user id onto an employee, per device."""

    __tablename__ = "biometric_employee_map"
    __table_args__ = (
        UniqueConstraint(
            "device_id", "device_user_id", name="uq_biometric_map_device_user"
        ),
        UniqueConstraint(
            "device_id", "employee_id", name="uq_biometric_map_device_employee"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("biometric_devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    device: Mapped[BiometricDevice] = relationship(back_populates="mappings")
    employee: Mapped["Employee"] = relationship(back_populates="device_maps")  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<BiometricEmployeeMap device={self.device_id} "
            f"user={self.device_user_id} employee={self.employee_id}>"
        )


class BiometricRawLog(CreatedAtMixin, db.Model):
    """Append-only record of every accepted punch. Never updated or deleted."""

    __tablename__ = "biometric_raw_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("biometric_devices.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    device_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    punch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    punch_type: Mapped[str] = mapped_column(
        String(8), nullable=False, default=PunchType.AUTO.value
    )
    raw_payload: Mapped[dict | None] = mapped_column(JSON)
    idempotency_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    processed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    device: Mapped[BiometricDevice] = relationship(back_populates="raw_logs")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<BiometricRawLog {self.id} device={self.device_id} "
            f"user={self.device_user_id} at={self.punch_time}>"
        )
