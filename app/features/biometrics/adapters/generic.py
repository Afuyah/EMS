"""Generic adapter (registry shim)."""

from __future__ import annotations

from app.features.biometrics.adapters import DeviceAdapter, GenericAdapter, ParsedPunch

__all__ = ["GenericAdapter", "DeviceAdapter", "ParsedPunch"]
