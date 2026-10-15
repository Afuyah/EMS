"""Biometric punch parsing: the vendor adapter contract and registry."""

from __future__ import annotations

import hashlib
import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.exceptions import ValidationError
from app.core.time import ensure_utc
from app.features.biometrics.constants import ADAPTER_REGISTRY, PunchType
from app.features.biometrics.exceptions import MalformedPunchError, UnsupportedVendorError


@dataclass(frozen=True)
class ParsedPunch:
    """A vendor payload normalised into the fields the domain needs."""

    device_user_id: str
    punch_time: datetime
    punch_type: PunchType
    raw: dict[str, Any]


class DeviceAdapter(ABC):
    """Turns a device-specific JSON payload into a :class:`ParsedPunch`."""

    vendor: str = "generic"

    @abstractmethod
    def parse_punch(self, payload: dict[str, Any]) -> ParsedPunch:
        """Normalise one punch payload, raising on malformed input."""

    # ---- shared helpers -------------------------------------------------
    @staticmethod
    def _require(payload: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        raise MalformedPunchError(
            f"Payload is missing required field(s): {', '.join(keys)}."
        )

    @staticmethod
    def _parse_time(value: Any) -> datetime:
        """Accept ISO-8601 strings, epoch seconds, and epoch milliseconds."""
        if isinstance(value, datetime):
            return ensure_utc(value)
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=__import__("datetime").timezone.utc)
        if isinstance(value, str):
            text = value.strip().replace("Z", "+00:00")
            try:
                return ensure_utc(datetime.fromisoformat(text))
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                    try:
                        return ensure_utc(datetime.strptime(text, fmt))
                    except ValueError:
                        continue
        raise MalformedPunchError(f"Unrecognised punch time value: {value!r}.")

    @staticmethod
    def _parse_type(value: Any) -> PunchType:
        if value is None:
            return PunchType.AUTO
        if isinstance(value, int):
            # ZKTeco convention: 0 = check-in, 1 = check-out.
            return PunchType.IN if value == 0 else PunchType.OUT
        text = str(value).strip().lower()
        if text in {"in", "checkin", "check-in", "0", "i"}:
            return PunchType.IN
        if text in {"out", "checkout", "check-out", "1", "o"}:
            return PunchType.OUT
        return PunchType.AUTO

    @staticmethod
    def idempotency_hash(
        *, device_sn: str, device_user_id: str, punch_time: datetime, punch_type: PunchType
    ) -> str:
        """``sha256(device_sn|device_user_id|punch_time_iso|punch_type)``.

        Deterministic so a device retry during a network blip collides with the
        first insert instead of creating a second attendance row.
        """
        canonical = "|".join(
            [
                str(device_sn).strip(),
                str(device_user_id).strip(),
                ensure_utc(punch_time).isoformat(),
                PunchType.coerce(punch_type).value,
            ]
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class GenericAdapter(DeviceAdapter):
    """Fallback adapter accepting a documented, vendor-neutral payload shape."""

    vendor = "generic"

    def parse_punch(self, payload: dict[str, Any]) -> ParsedPunch:
        if not isinstance(payload, dict):
            raise MalformedPunchError("The punch payload must be a JSON object.")
        user_id = self._require(payload, "device_user_id", "user_id", "employee_no", "pin")
        raw_time = self._require(payload, "punch_time", "timestamp", "time", "datetime")
        return ParsedPunch(
            device_user_id=str(user_id).strip(),
            punch_time=self._parse_time(raw_time),
            punch_type=self._parse_type(payload.get("punch_type") or payload.get("type")),
            raw=payload,
        )


class ZKTecoAdapter(DeviceAdapter):
    """ZKTeco push-SDK / ADMS payloads.

    ZKTeco devices commonly push ``{"PIN": "42", "DateTime": "2026-05-04 08:12:33",
    "Status": 0}`` where ``Status`` 0 is check-in and 1 is check-out.
    """

    vendor = "zkteco"

    def parse_punch(self, payload: dict[str, Any]) -> ParsedPunch:
        if not isinstance(payload, dict):
            raise MalformedPunchError("The punch payload must be a JSON object.")

        # Some firmwares nest the record under "data" or "records".
        record = payload
        for key in ("data", "record", "punch"):
            if isinstance(payload.get(key), dict):
                record = payload[key]
                break

        user_id = self._require(record, "PIN", "pin", "user_id", "employee_no")
        raw_time = self._require(
            record, "DateTime", "datetime", "punch_time", "timestamp", "time"
        )
        status = record.get("Status", record.get("status", record.get("punch_type")))
        return ParsedPunch(
            device_user_id=str(user_id).strip(),
            punch_time=self._parse_time(raw_time),
            punch_type=self._parse_type(status),
            raw=payload,
        )


class HikvisionAdapter(DeviceAdapter):
    """Hikvision ISAPI event payloads (``{"employeeNoString": "42", ...}``)."""

    vendor = "hikvision"

    def parse_punch(self, payload: dict[str, Any]) -> ParsedPunch:
        if not isinstance(payload, dict):
            raise MalformedPunchError("The punch payload must be a JSON object.")

        event = payload
        for key in ("event", "AccessControllerEvent", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                event = nested
                break

        user_id = self._require(
            event, "employeeNoString", "employeeNo", "device_user_id", "user_id"
        )
        raw_time = self._require(
            event, "dateTime", "DateTime", "punch_time", "timestamp", "time"
        )
        # Hikvision reports the door event; major event type 1 is a valid card read.
        return ParsedPunch(
            device_user_id=str(user_id).strip(),
            punch_time=self._parse_time(raw_time),
            punch_type=self._parse_type(event.get("punch_type") or event.get("type")),
            raw=payload,
        )


def get_adapter(vendor: str | None) -> DeviceAdapter:
    """Resolve an adapter for ``vendor``, raising for unknown vendors."""
    key = (vendor or "generic").strip().lower()
    target = ADAPTER_REGISTRY.get(key)
    if target is None:
        raise UnsupportedVendorError(
            f"No biometric adapter is registered for vendor {vendor!r}. "
            f"Known vendors: {', '.join(sorted(ADAPTER_REGISTRY))}."
        )
    module_path, _, class_name = target.partition(":")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)()


def idempotency_hash(
    *, device_sn: str, device_user_id: str, punch_time: datetime, punch_type: PunchType | str
) -> str:
    """Module-level form of :meth:`DeviceAdapter.idempotency_hash`."""
    return DeviceAdapter.idempotency_hash(
        device_sn=device_sn,
        device_user_id=device_user_id,
        punch_time=punch_time,
        punch_type=PunchType.coerce(punch_type),
    )
