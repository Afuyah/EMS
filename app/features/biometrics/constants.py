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
    """Supported biometric device families.

    Every value here must be a key in :data:`ADAPTER_REGISTRY`, because
    registering a device resolves its adapter through this value. A mismatch
    raises ``UnsupportedVendorError`` at registration time.
    """

    ZKTECO = "zkteco"
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

#: The factory value of a ZKTeco "Comm Key" / communication password.
#:
#: Devices ship with no communication password, and many installations never set
#: one. Such a device sends ``CommKey=0`` (or omits the parameter entirely), so
#: this is accepted as the shared secret. The trade-off is deliberate: an
#: unattended terminal has no way to hold a stronger credential, and refusing it
#: would mean no attendance data at all. Rotate the device's secret and set the
#: same value on the device to move it off this default.
DEFAULT_COMM_KEY = "0"

#: Device health notification event.
EVENT_DEVICE_OFFLINE = "device_offline"
