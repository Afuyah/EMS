"""Biometrics controller: device administration and punch ingestion.

This is the one place that deliberately joins two features: a device punch is a
biometrics concern, but turning it into an attendance row belongs to the
attendance service.  The controller orchestrates, then commits once.
"""

from __future__ import annotations

from app.core.database import db
from app.core.logging import get_logger
from app.core.time import utcnow
from app.features.attendance.service import AttendanceService
from app.features.biometrics.exceptions import (
    MalformedPunchError,
    UnknownDeviceUserError,
)
from app.features.biometrics.schemas import (
    DeviceCreateSchema,
    DeviceMappingSchema,
    load_or_raise,
)
from app.features.biometrics.service import BiometricsService

logger = get_logger(__name__)


class BiometricsController:
    """Orchestrates device use-cases and punch ingestion."""

    def __init__(
        self,
        service: BiometricsService | None = None,
        attendance: AttendanceService | None = None,
    ) -> None:
        self.service = service or BiometricsService()
        self._attendance = attendance

    @property
    def attendance(self) -> AttendanceService:
        """Lazily construct so importing the controller stays cheap."""
        if self._attendance is None:
            self._attendance = AttendanceService()
        return self._attendance

    # ---- device authentication (used by the webhook guard) ---------------
    def authenticate_device(self, serial_no: str, secret: str):
        """Verify device headers during ``before_request`` (no commit)."""
        return self.service.authenticate(serial_no, secret)

    # ---- punch ingestion -------------------------------------------------
    def ingest_punch(self, *, device, payload: dict, raw_headers: dict | None = None):
        """Accept one punch and resolve it into attendance.

        Returns ``(envelope, status)``.  Always commits exactly once: the raw
        log and the attendance row are one unit of work.
        """
        try:
            parsed = self.service.parse_punch(device, payload or {})
        except MalformedPunchError:
            db.session.rollback()
            raise

        try:
            mapping, raw_log, created, reason = self.service.record_punch(
                device=device,
                parsed=parsed,
                raw_payload=payload,
            )
            self.service.touch(device)

            if not created:
                db.session.commit()
                return (
                    {
                        "data": {
                            "accepted": True,
                            "duplicate": True,
                            "reason": reason,
                        }
                    },
                    200,
                )

            if mapping is None:
                # Audited, but nothing to attribute to an employee.
                db.session.commit()
                return (
                    {
                        "data": {
                            "accepted": True,
                            "duplicate": False,
                            "reason": reason,
                            "raw_log_id": raw_log.id if raw_log else None,
                        }
                    },
                    200,
                )

            employee = mapping.employee
            record = self.attendance.apply_punch(
                employee_id=employee.id,
                department_id=employee.department_id,
                tz_name=employee.tz,
                punch_time=parsed.punch_time,
                punch_type=parsed.punch_type,
            )
            self.service.mark_processed(raw_log)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        return (
            {
                "data": {
                    "accepted": True,
                    "duplicate": False,
                    "reason": reason,
                    "raw_log_id": raw_log.id if raw_log else None,
                    "employee": {
                        "id": employee.id,
                        "employee_no": employee.employee_no,
                        "full_name": employee.full_name,
                    },
                    "attendance": {
                        "id": record.id,
                        "work_date": record.work_date.isoformat(),
                        "status": record.status,
                        "clock_in": record.clock_in.isoformat() if record.clock_in else None,
                        "clock_out": record.clock_out.isoformat() if record.clock_out else None,
                        "late_minutes": record.late_minutes,
                        "early_leave_minutes": record.early_leave_minutes,
                        "worked_minutes": record.worked_minutes,
                    },
                }
            },
            201,
        )

    def heartbeat(self, *, device):
        """Record that a device is alive."""
        try:
            self.service.touch(device)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return (
            {
                "data": {
                    "serial_no": device.serial_no,
                    "last_seen_at": (device.last_seen_at or utcnow()).isoformat(),
                }
            },
            200,
        )

    # ---- device administration ------------------------------------------
    def list_devices(self):
        """Plain list of device dicts; the blueprint wraps the envelope."""
        devices = self.service.list_devices()
        return [
            {
                "id": device.id,
                "serial_no": device.serial_no,
                "location": device.location,
                "vendor": device.vendor,
                "is_active": device.is_active,
                "is_online": device.is_online,
                "last_seen_at": device.last_seen_at.isoformat()
                if device.last_seen_at
                else None,
                "mapped_employees": len(device.mappings),
            }
            for device in devices
        ]

    def register_device(self, payload: dict):
        """Create a device; the plaintext secret is returned exactly once."""
        from app.core.responses import ok

        data = load_or_raise(DeviceCreateSchema(), payload)
        try:
            device, secret = self.service.register_device(
                serial_no=data["serial_no"],
                location=data.get("location"),
                vendor=data.get("vendor"),
                is_active=data.get("is_active", True),
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return (
            {
                "id": device.id,
                "serial_no": device.serial_no,
                "location": device.location,
                "vendor": device.vendor,
                "is_active": device.is_active,
            },
            secret,
        )

    def rotate_secret(self, device_id: int):
        from app.core.responses import ok

        try:
            device, secret = self.service.rotate_secret(device_id)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return (
            {
                "id": device.id,
                "serial_no": device.serial_no,
                "rotated_at": utcnow().isoformat(),
            },
            secret,
        )

    def set_device_active(self, device_id: int, is_active: bool):
        try:
            device = self.service.set_active(device_id, is_active)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return {"id": device.id, "is_active": device.is_active}

    # ---- mappings --------------------------------------------------------
    def list_mappings(self, *, serial_no: str | None = None):
        """Plain list of mapping rows; the blueprint wraps the envelope."""
        return self.service.list_mappings(serial_no=serial_no)

    def map_employee(self, payload: dict):
        data = load_or_raise(DeviceMappingSchema(), payload)
        try:
            mapping = self.service.map_employee(
                device_id=data["device_id"],
                employee_id=data["employee_id"],
                device_user_id=data["device_user_id"],
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return {
            "id": mapping.id,
            "device_id": mapping.device_id,
            "employee_id": mapping.employee_id,
            "device_user_id": mapping.device_user_id,
        }

    def recent_raw_logs(self, *, device_serial: str | None = None, limit: int = 100):
        """Plain list of rows; the blueprint wraps it in the response envelope."""
        return self.service.recent_raw_logs(device_serial=device_serial, limit=limit)
