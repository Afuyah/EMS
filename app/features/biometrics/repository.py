"""Biometrics repositories: the only place with SQL for this feature."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.common.base_repository import BaseRepository
from app.core.database import db
from app.core.time import utcnow
from app.features.biometrics.models import (
    BiometricDevice,
    BiometricEmployeeMap,
    BiometricRawLog,
)


class DeviceRepository(BaseRepository[BiometricDevice]):
    """Queries over ``biometric_devices``."""

    model = BiometricDevice

    def get_by_serial(self, serial_no: str) -> BiometricDevice | None:
        stmt = select(BiometricDevice).where(
            BiometricDevice.serial_no == (serial_no or "").strip()
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def serial_taken(self, serial_no: str) -> bool:
        stmt = select(BiometricDevice.id).where(
            BiometricDevice.serial_no == (serial_no or "").strip()
        )
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def ordered(self) -> list[BiometricDevice]:
        stmt = select(BiometricDevice).order_by(
            BiometricDevice.is_active.desc(), BiometricDevice.serial_no
        )
        return list(db.session.execute(stmt).scalars().all())

    def touch(self, device: BiometricDevice, *, when: datetime | None = None) -> BiometricDevice:
        """Record that a device just called in."""
        device.last_seen_at = when or utcnow()
        db.session.flush()
        return device

    def offline_since(self, cutoff: datetime) -> list[BiometricDevice]:
        """Active devices not seen since ``cutoff`` (never-seen included)."""
        stmt = select(BiometricDevice).where(
            BiometricDevice.is_active.is_(True),
            (BiometricDevice.last_seen_at.is_(None))
            | (BiometricDevice.last_seen_at < cutoff),
        )
        return list(db.session.execute(stmt).scalars().all())

    def health_counts(self) -> dict[str, int]:
        """Total / active device counts (one query)."""
        stmt = select(
            func.count(BiometricDevice.id).label("total"),
            func.sum(
                func.cast(BiometricDevice.is_active, db.Integer)
            ).label("active"),
        )
        row = db.session.execute(stmt).mappings().one()
        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
        }


class DeviceMapRepository(BaseRepository[BiometricEmployeeMap]):
    """Queries over ``biometric_employee_map``."""

    model = BiometricEmployeeMap

    def resolve(self, *, device_id: int, device_user_id: str) -> BiometricEmployeeMap | None:
        stmt = select(BiometricEmployeeMap).where(
            BiometricEmployeeMap.device_id == device_id,
            BiometricEmployeeMap.device_user_id == str(device_user_id).strip(),
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def for_device(self, device_id: int) -> list[BiometricEmployeeMap]:
        stmt = (
            select(BiometricEmployeeMap)
            .where(BiometricEmployeeMap.device_id == device_id)
            .order_by(BiometricEmployeeMap.device_user_id)
        )
        return list(db.session.execute(stmt).scalars().all())

    def all_mappings(self, *, serial_no: str | None = None) -> list[dict]:
        """Every mapping with device and employee context (one query)."""
        from app.features.employees.models import Department, Employee

        stmt = (
            select(
                BiometricEmployeeMap.id.label("id"),
                BiometricEmployeeMap.device_user_id.label("device_user_id"),
                BiometricEmployeeMap.device_id.label("device_id"),
                BiometricDevice.serial_no.label("serial_no"),
                BiometricDevice.vendor.label("vendor"),
                BiometricDevice.location.label("location"),
                BiometricEmployeeMap.employee_id.label("employee_id"),
                Employee.employee_no.label("employee_no"),
                Employee.first_name.label("first_name"),
                Employee.last_name.label("last_name"),
                Department.name.label("department_name"),
            )
            .join(BiometricDevice, BiometricDevice.id == BiometricEmployeeMap.device_id)
            .join(Employee, Employee.id == BiometricEmployeeMap.employee_id, isouter=True)
            .join(Department, Department.id == Employee.department_id, isouter=True)
        )
        if serial_no:
            stmt = stmt.where(BiometricDevice.serial_no == serial_no.strip())
        stmt = stmt.order_by(BiometricDevice.serial_no, BiometricEmployeeMap.device_user_id)
        return self.rows(stmt)

    def conflict_exists(
        self, *, device_id: int, device_user_id: str, employee_id: int
    ) -> bool:
        stmt = select(BiometricEmployeeMap.id).where(
            BiometricEmployeeMap.device_id == device_id,
            (BiometricEmployeeMap.device_user_id == str(device_user_id).strip())
            | (BiometricEmployeeMap.employee_id == employee_id),
        )
        return db.session.execute(stmt.limit(1)).scalar_one_or_none() is not None

    def count_for_device(self, device_id: int) -> int:
        return self.count(device_id=device_id)


class RawLogRepository(BaseRepository[BiometricRawLog]):
    """Queries over the append-only ``biometric_raw_logs``."""

    model = BiometricRawLog

    def insert_if_absent(
        self,
        *,
        device_id: int,
        device_user_id: str,
        punch_time: datetime,
        punch_type: str,
        raw_payload: dict | None,
        idempotency_hash: str,
    ) -> tuple[BiometricRawLog | None, bool]:
        """Insert a punch unless the idempotency hash already exists.

        Returns ``(record, created)``.  ``record`` is ``None`` when the insert
        was skipped, which lets the caller avoid re-reading the row.
        """
        stmt = (
            pg_insert(BiometricRawLog)
            .values(
                device_id=device_id,
                device_user_id=str(device_user_id).strip(),
                punch_time=punch_time,
                punch_type=punch_type,
                raw_payload=raw_payload,
                idempotency_hash=idempotency_hash,
                processed=False,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_hash"])
            .returning(BiometricRawLog.id)
        )
        inserted_id = db.session.execute(stmt).scalar_one_or_none()
        if inserted_id is None:
            return None, False

        record = self.get(int(inserted_id))
        return record, True

    def get_by_hash(self, idempotency_hash: str) -> BiometricRawLog | None:
        stmt = select(BiometricRawLog).where(
            BiometricRawLog.idempotency_hash == idempotency_hash
        )
        return db.session.execute(stmt).scalar_one_or_none()

    def mark_processed(self, record: BiometricRawLog) -> BiometricRawLog:
        record.processed = True
        record.processed_at = utcnow()
        db.session.flush()
        return record

    def unprocessed(self, *, limit: int = 100) -> list[BiometricRawLog]:
        stmt = (
            select(BiometricRawLog)
            .where(BiometricRawLog.processed.is_(False))
            .order_by(BiometricRawLog.punch_time)
            .limit(limit)
        )
        return list(db.session.execute(stmt).scalars().all())

    def recent(self, *, device_id: int | None = None, limit: int = 100) -> list[BiometricRawLog]:
        stmt = select(BiometricRawLog)
        if device_id is not None:
            stmt = stmt.where(BiometricRawLog.device_id == device_id)
        stmt = stmt.order_by(BiometricRawLog.punch_time.desc()).limit(limit)
        return list(db.session.execute(stmt).scalars().all())

    def recent_with_device(self, *, limit: int = 50) -> list[dict]:
        stmt = (
            select(
                BiometricRawLog.id.label("id"),
                BiometricRawLog.device_user_id.label("device_user_id"),
                BiometricRawLog.punch_time.label("punch_time"),
                BiometricRawLog.punch_type.label("punch_type"),
                BiometricRawLog.processed.label("processed"),
                BiometricDevice.serial_no.label("serial_no"),
                BiometricDevice.location.label("location"),
            )
            .join(BiometricDevice, BiometricDevice.id == BiometricRawLog.device_id)
            .order_by(BiometricRawLog.punch_time.desc())
            .limit(limit)
        )
        return self.rows(stmt)

    def count_since(self, since: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(BiometricRawLog)
            .where(BiometricRawLog.punch_time >= since)
        )
        return int(db.session.execute(stmt).scalar_one() or 0)
