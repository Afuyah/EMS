"""Fixed strings and enums for the biometrics feature."""

from __future__ import annotations

from enum import Enum


class PunchType(str, Enum):
    """How a device classified the punch."""

    IN = "in"
    OUT = "out"
    AUTO = "auto"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "PunchType":
        if value is None:
            return cls.AUTO
        return value if isinstance(value, cls) else cls(str(value).strip().lower())


class DeviceVendor(str, Enum):
    """Supported biometric device families."""

    ZKTECO = "zktco"
    ZKTECO_LEGACY = "zkteco"
    HIKVISION = "hikvision"
    GENERIC = "generic"

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def coerce(cls, value) -> "DeviceVendor":
        if value is None:
            return cls.GENERIC
        return value if isinstance(value, cls) else cls(str(value).strip().lower())


#: Vendor used when registering devices without an explicit vendor.
DEFAULT_VENDOR = DeviceVendor.ZKTECO.value

#: Adapter registry keys -> implementation module paths.
ADAPTER_REGISTRY: dict[str, str] = {
    "zkteco": "app.features.biometrics.adapters.zkteco:ZKTecoAdapter",
    "zk": "app.features.biometrics.adapters.zkteco:ZKTecoAdapter",
    "hikvision": "app.features.biometrics.adapters.hikvision:HikvisionAdapter",
    "generic": "app.features.biometrics.adapters.generic:GenericAdapter",
}

#: Header names the webhook authenticates with.
HEADER_DEVICE_SN = "X-Device-SN"
HEADER_DEVICE_SECRET = "X-Device-Secret"
HEADER_SIGNATURE = "X-Device-Signature"

#: Seconds of clock skew tolerated when validating signed payloads.
SIGNATURE_TOLERANCE_SECONDS = 300

#: A device unseen for this long is considered offline.
DEVICE_OFFLINE_AFTER_MINUTES = 30

#: Device health notification event.
EVENT_DEVICE_OFFLINE = "device_offline"
