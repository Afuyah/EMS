"""Hikvision adapter (registry shim)."""

from __future__ import annotations

from app.features.biometrics.adapters import DeviceAdapter, HikvisionAdapter, ParsedPunch

__all__ = ["HikvisionAdapter", "DeviceAdapter", "ParsedPunch"]
