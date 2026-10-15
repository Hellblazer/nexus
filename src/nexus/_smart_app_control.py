# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smart App Control state on Windows (nexus-f9bgu.57, RDR-224 Gap 6).

With Smart App Control on, CodeIntegrity blocks the unsigned PostgreSQL bundle
and the unsigned Python that uv installs, so the local stack cannot be set up
(T2 ``nexus_rdr/224-smart-app-control-on-2026-10-14``). Windows records the
state in ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy``, value
``VerifiedAndReputablePolicyState``: 0 off, 1 on, 2 evaluation. The measured
guest went from 2 to 1 when it was switched on.

Stdlib only. ``platform`` and ``read_value`` are seams, so the Windows branch
runs under test on every host; production callers pass neither.
"""
from __future__ import annotations

import sys
from typing import Callable

__all__ = ["EVALUATION", "NOT_WINDOWS", "OFF", "ON", "UNKNOWN", "state"]

OFF = "off"
ON = "on"
EVALUATION = "evaluation"
UNKNOWN = "unknown"
NOT_WINDOWS = "not-windows"

_KEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
_VALUE = "VerifiedAndReputablePolicyState"
_BY_VALUE: dict[int, str] = {0: OFF, 1: ON, 2: EVALUATION}


def _read_policy_value() -> int | None:
    """The registry value, or None when the key or value is absent."""
    import winreg  # noqa: PLC0415 — Windows-only module

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as key:
            value, _kind = winreg.QueryValueEx(key, _VALUE)
    except FileNotFoundError:
        return None
    return int(value)


def state(
    *,
    platform: str | None = None,
    read_value: Callable[[], int | None] | None = None,
) -> str:
    """One of :data:`ON`, :data:`EVALUATION`, :data:`OFF`, :data:`UNKNOWN` or
    :data:`NOT_WINDOWS`. Never raises: a read that fails is :data:`UNKNOWN`."""
    if (platform if platform is not None else sys.platform) != "win32":
        return NOT_WINDOWS
    try:
        value = (read_value or _read_policy_value)()
    except Exception:  # noqa: BLE001 — a failed read is "unknown", never a crash
        return UNKNOWN
    if value is None:
        return UNKNOWN
    return _BY_VALUE.get(value, UNKNOWN)
