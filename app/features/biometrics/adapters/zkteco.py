"""ZKTeco adapter.

Re-exported from :mod:`app.features.biometrics.adapters` so the registry path
``app.features.biometrics.adapters.zkteco:ZKTecoAdapter`` resolves.
"""

from __future__ import annotations

from app.features.biometrics.adapters import DeviceAdapter, ParsedPunch, ZKTecoAdapter

__all__ = ["ZKTecoAdapter", "DeviceAdapter", "ParsedPunch"]
