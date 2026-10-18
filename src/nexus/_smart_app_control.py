# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smart App Control state on Windows (nexus-f9bgu.57, RDR-224 Gap 6).

With Smart App Control on, CodeIntegrity blocks the unsigned PostgreSQL bundle
and the unsigned Python that uv installs, so the local stack cannot be set up
(T2 ``nexus_rdr/224-smart-app-control-on-2026-10-14``). Windows records the
state in ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy``, value
``VerifiedAndReputablePolicyState``: 0 off, 1 on, 2 evaluation. The measured
guest went from 2 to 1 when it was switched on. A Windows without Smart App
Control (Windows 10, older Windows 11) has no such value: :data:`ABSENT`.

Stdlib only. ``platform`` and ``read_value`` are seams, so the Windows branch
runs under test on every host; production callers pass neither.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

__all__ = [
    "ABSENT", "EVALUATION", "NOT_WINDOWS", "OFF", "ON", "UNKNOWN",
    "execution_blocked", "state",
]

OFF = "off"
ON = "on"
EVALUATION = "evaluation"
UNKNOWN = "unknown"
NOT_WINDOWS = "not-windows"
ABSENT = "absent"

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
    """One of :data:`ON`, :data:`EVALUATION`, :data:`OFF`, :data:`ABSENT`,
    :data:`UNKNOWN` or :data:`NOT_WINDOWS`. Never raises: a read that fails is
    :data:`UNKNOWN`; a key or value that does not exist is :data:`ABSENT`."""
    if (platform if platform is not None else sys.platform) != "win32":
        return NOT_WINDOWS
    try:
        value = (read_value or _read_policy_value)()
    except Exception:  # noqa: BLE001 — a failed read is "unknown", never a crash
        return UNKNOWN
    if value is None:
        return ABSENT
    return _BY_VALUE.get(value, UNKNOWN)


#: ERROR_ACCESS_DISABLED_BY_POLICY: "An Application Control policy has blocked
#: this file", the code CreateProcess returns for a blocked unsigned binary.
_BLOCKED_WINERROR = 4551


def _launch_unsigned_copy() -> None:
    """Run a never-seen unsigned executable: this interpreter copied with one
    byte appended, so its signature no longer verifies and its hash is new.
    That is the control that separated an enforcing Smart App Control from a
    registry value that only says "on" (T2
    ``nexus_rdr/224-sac-on-baseline-control-2026-10-17``)."""
    with tempfile.TemporaryDirectory(prefix="nx-sac-probe-") as tmp:
        probe = os.path.join(tmp, "nx-sac-probe.exe")
        shutil.copyfile(sys.executable, probe)
        with open(probe, "ab") as handle:
            handle.write(b"\0")
        subprocess.run([probe, "--version"], check=False, capture_output=True, timeout=30)


def execution_blocked(
    *,
    platform: str | None = None,
    launch: Callable[[], None] | None = None,
) -> bool | None:
    """True when Windows blocks an unsigned executable from running, False when
    it runs, None when that cannot be told (not Windows, or the launch failed
    for another reason). Never raises. *platform* and *launch* are seams."""
    if (platform if platform is not None else sys.platform) != "win32":
        return None
    try:
        (launch or _launch_unsigned_copy)()
    except OSError as exc:
        return True if getattr(exc, "winerror", None) == _BLOCKED_WINERROR else None
    except Exception:  # noqa: BLE001 — an unrunnable probe is "inconclusive", never a crash
        return None
    return False
