"""Biometrics service: devices, employee mappings and punch ingestion.

The service is Flask-free.  Punch ingestion is split so the *pure* mapping and
idempotency decisions stay testable, while the database write happens through
the repository using ``INSERT ... ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.logging import get_logger
from app.core.security import hash_device_secret, new_token, verify_device_secret
from app.core.time import utcnow
from app.features.biometrics.adapters import ParsedPunch, get_adapter, idempotency_hash
from app.features.biometrics.constants import (
    DEVICE_OFFLINE_AFTER_MINUTES,
    DEFAULT_VENDOR,
    PunchType,
)
from app.features.biometrics.exceptions import (
    DeviceInactiveError,
    DeviceMappingConflictError,
    DeviceNotFoundError,
    DeviceSerialTakenError,
    UnknownDeviceUserError,
)
from app.features.biometrics.models import (
    BiometricDevice,
    BiometricEmployeeMap,
    BiometricRawLog,
)
from app.features.biometrics.repository import (
    DeviceMapRepository,
    DeviceRepository,
    RawLogRepository,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class IngestionResult:
    """Outcome of accepting a punch from a device."""

    accepted: bool
    duplicate: bool
    reason: str
    raw_log_id: int | None = None
    parsed: ParsedPunch | None = None

    @property
    def http_status(self) -> int:
        """200 for anything the device should consider handled.

        Duplicates and unmapped users are not errors from the device's point of
        view: re-sending will not help, so answering 4xx would only make the
        device queue and retry forever.
        """
        return 200


class BiometricsService:
    """Use-cases for device lifecycle and punch bookkeeping."""

    def __init__(
        self,
        devices: DeviceRepository | None = None,
        maps: DeviceMapRepository | None = None,
        raw_logs: RawLogRepository | None = None,
    ) -> None:
        self.devices = devices or DeviceRepository()
        self.maps = maps or DeviceMapRepository()
        self.raw_logs = raw_logs or RawLogRepository()

    # ---- device authentication ------------------------------------------
    def authenticate(
        self, serial_no: str, secret: str, *, allow_default_comm_key: bool = False
    ) -> BiometricDevice:
        """Verify device credentials; raises on any failure.

        An unknown serial and a wrong secret raise the *same* error so serials
        cannot be probed, and both answer 401 rather than leaking which part
        failed.

        ``allow_default_comm_key`` additionally accepts the factory
        communication key. ADMS is driven from a device keypad, and a terminal
        whose communication password was never set sends ``0``; without this,
        such a device could never push. It is opt-in so the JSON webhook keeps
        requiring its real secret.
        """
        from app.features.biometrics.constants import DEFAULT_COMM_KEY
        from app.features.biometrics.exceptions import DeviceCredentialsError

        device = self.devices.get_by_serial(serial_no)
        if device is None:
            logger.warning("device auth failed: unknown serial=%s", serial_no)
            raise DeviceCredentialsError("Device authentication failed.")

        supplied = (secret or "").strip() or DEFAULT_COMM_KEY
        matched = verify_device_secret(supplied, device.secret_hash, salt=device.serial_no)

        if not matched and allow_default_comm_key and supplied == DEFAULT_COMM_KEY:
            matched = True
            logger.info(
                "device %s authenticated with the factory CommKey; "
                "rotate its secret and set the same value on the device to "
                "move off the default",
                device.serial_no,
            )

        if not matched:
            logger.warning("device auth failed: bad secret serial=%s", serial_no)
            raise DeviceCredentialsError("Device authentication failed.")

        if not device.is_active:
            raise DeviceInactiveError(f"Device {serial_no} is deactivated.")

        return device

    # ---- device administration ------------------------------------------
    def list_devices(self) -> list[BiometricDevice]:
        return self.devices.ordered()

    def get_device(self, device_id: int) -> BiometricDevice:
        device = self.devices.get(device_id)
        if device is None:
            raise DeviceNotFoundError(f"Device {device_id} was not found.")
        return device

    def get_device_by_serial(self, serial_no: str) -> BiometricDevice:
        device = self.devices.get_by_serial(serial_no)
        if device is None:
            raise DeviceNotFoundError(f"Device {serial_no!r} was not found.")
        return device

    @staticmethod
    def generate_secret() -> str:
        """A fresh shared secret, shown to the admin exactly once.

        Numeric and short, because the primary user of this value is a device
        keypad: a ZKTeco terminal's "Comm Key" field accepts digits only, so an
        alphanumeric token could never be entered on the hardware. Six digits is
        also what the protocol's own ``CommKey`` parameter expects.
        """
        from app.core.security import new_device_comm_key

        return new_device_comm_key(6)

    def register_device(
        self,
        *,
        serial_no: str,
        location: str | None = None,
        vendor: str | None = DEFAULT_VENDOR,
        secret: str | None = None,
        is_active: bool = True,
    ) -> tuple[BiometricDevice, str]:
        """Register a device.  Returns ``(device, plaintext_secret)``."""
        clean_serial = (serial_no or "").strip()
        if not clean_serial:
            from app.core.exceptions import ValidationError

            raise ValidationError("A device serial number is required.")
        if self.devices.serial_taken(clean_serial):
            raise DeviceSerialTakenError(f"{clean_serial} is already registered.")

        # Validate the vendor early so a typo fails at registration.
        get_adapter(vendor)

        plaintext = secret or self.generate_secret()
        device = self.devices.create(
            serial_no=clean_serial,
            location=(location or "").strip() or None,
            vendor=(vendor or DEFAULT_VENDOR).strip().lower(),
            secret_hash=hash_device_secret(plaintext, salt=clean_serial),
            is_active=is_active,
        )
        logger.info("registered device serial=%s vendor=%s", clean_serial, device.vendor)
        return device, plaintext

    def rotate_secret(self, device_id: int, *, secret: str | None = None) -> tuple[BiometricDevice, str]:
        """Issue a new shared secret, invalidating the previous one."""
        device = self.get_device(device_id)
        plaintext = secret or self.generate_secret()
        device.secret_hash = hash_device_secret(plaintext, salt=device.serial_no)
        self.devices.update(device)
        logger.info("rotated secret serial=%s", device.serial_no)
        return device, plaintext

    def set_active(self, device_id: int, is_active: bool) -> BiometricDevice:
        device = self.get_device(device_id)
        device.is_active = is_active
        return self.devices.update(device)

    def touch(self, device: BiometricDevice, *, when: datetime | None = None) -> BiometricDevice:
        return self.devices.touch(device, when=when)

    def health(self) -> dict:
        """Device counts plus the offline list, for the devices page."""
        counts = self.devices.health_counts()
        cutoff = utcnow().replace(microsecond=0)
        from datetime import timedelta

        cutoff = utcnow() - timedelta(minutes=DEVICE_OFFLINE_AFTER_MINUTES)
        offline = self.devices.offline_since(cutoff)
        return {
            "total": counts["total"],
            "active": counts["active"],
            "offline": [
                {
                    "id": device.id,
                    "serial_no": device.serial_no,
                    "location": device.location,
                    "last_seen_at": device.last_seen_at,
                }
                for device in offline
            ],
        }

    # ---- employee mapping ------------------------------------------------
    def list_mappings(self, *, serial_no: str | None = None) -> list[dict]:
        return self.maps.all_mappings(serial_no=serial_no)

    def map_employee(
        self, *, device_id: int, employee_id: int, device_user_id: str
    ) -> BiometricEmployeeMap:
        """Link a device-local user id to an employee on one device."""
        device = self.get_device(device_id)
        clean_user = str(device_user_id or "").strip()
        if not clean_user:
            from app.core.exceptions import ValidationError

            raise ValidationError("A device user id is required.")

        # Cross-feature read through the employees service, never its repository.
        from app.features.employees.service import EmployeeService

        employee = EmployeeService().get_employee(employee_id)

        if self.maps.conflict_exists(
            device_id=device.id, device_user_id=clean_user, employee_id=employee.id
        ):
            raise DeviceMappingConflictError(
                f"Device user {clean_user} or {employee.full_name} is already "
                f"mapped on {device.serial_no}."
            )

        mapping = self.maps.create(
            device_id=device.id, employee_id=employee.id, device_user_id=clean_user
        )
        logger.info(
            "mapped device=%s user=%s employee=%s", device.serial_no, clean_user, employee.id
        )
        return mapping

    def unmap(self, mapping_id: int) -> None:
        mapping = self.maps.get(mapping_id)
        if mapping is not None:
            self.maps.delete(mapping)

    def resolve_employee(self, *, device_id: int, device_user_id: str) -> BiometricEmployeeMap:
        """Find the employee behind a device user id, or raise."""
        mapping = self.maps.resolve(device_id=device_id, device_user_id=device_user_id)
        if mapping is None:
            raise UnknownDeviceUserError(
                f"Device user {device_user_id!r} is not mapped to an employee."
            )
        return mapping

    # ---- punch ingestion -------------------------------------------------
    def parse_punch(self, device: BiometricDevice, payload: dict) -> ParsedPunch:
        """Normalise a vendor payload through the device's adapter."""
        return get_adapter(device.vendor).parse_punch(payload)

    def record_punch(
        self,
        *,
        device: BiometricDevice,
        parsed: ParsedPunch,
        raw_payload: dict | None = None,
    ) -> tuple[BiometricEmployeeMap | None, BiometricRawLog | None, bool, str]:
        """Persist an accepted punch and resolve its employee.

        Returns ``(mapping, raw_log, created, reason)``.  When ``created`` is
        ``False`` the punch was a duplicate and ``raw_log`` is ``None``: the
        caller must not touch attendance again.
        """
        punch_hash = idempotency_hash(
            device_sn=device.serial_no,
            device_user_id=parsed.device_user_id,
            punch_time=parsed.punch_time,
            punch_type=parsed.punch_type,
        )

        raw_log, created = self.raw_logs.insert_if_absent(
            device_id=device.id,
            device_user_id=parsed.device_user_id,
            punch_time=parsed.punch_time,
            punch_type=PunchType.coerce(parsed.punch_type).value,
            raw_payload=raw_payload if raw_payload is not None else parsed.raw,
            idempotency_hash=punch_hash,
        )
        if not created:
            # Return the existing row rather than None. A device retrying a punch
            # whose attendance resolution failed must be able to resume it, and
            # the caller needs the row to know whether that work is still
            # outstanding.
            existing = self.raw_logs.get_by_hash(punch_hash)
            logger.info(
                "duplicate punch serial=%s user=%s at=%s (processed=%s)",
                device.serial_no,
                parsed.device_user_id,
                parsed.punch_time,
                bool(existing and existing.processed),
            )
            return None, existing, False, "duplicate"

        mapping = self.maps.resolve(
            device_id=device.id, device_user_id=parsed.device_user_id
        )
        if mapping is None:
            # Keep the raw log for audit, but there is no employee to attribute.
            logger.warning(
                "unmapped device user serial=%s user=%s",
                device.serial_no,
                parsed.device_user_id,
            )
            return None, raw_log, True, "unmapped_device_user"

        return mapping, raw_log, True, "accepted"

    def mark_processed(self, raw_log: BiometricRawLog | None) -> None:
        if raw_log is not None:
            self.raw_logs.mark_processed(raw_log)

    def recent_raw_logs(self, *, device_serial: str | None = None, limit: int = 100) -> list[dict]:
        device_id = None
        if device_serial:
            device = self.devices.get_by_serial(device_serial)
            device_id = device.id if device else -1
        return self.raw_logs.recent_with_device(limit=limit)

    def punch_volume_since(self, since: datetime) -> int:
        return self.raw_logs.count_since(since)
