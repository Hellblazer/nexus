# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smart App Control state on Windows (nexus-f9bgu.57, RDR-224 Gap 6).

With Smart App Control on, CodeIntegrity blocks the unsigned PostgreSQL bundle
and the unsigned Python that uv installs, so the local stack cannot be set up
(T2 ``nexus_rdr/224-smart-app-control-on-2026-10-14``). Windows records the
state in ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy``, value
``VerifiedAndReputablePolicyState``: 0 off, 1 on, 2 evaluation. The measured
guest went from 2 to 1 when it was switched on. A Windows without Smart App
Control (Windows 10, older Windows 11) has no such value: :data:`ABSENT`.

Stdlib and structlog only. ``platform`` and ``read_value`` are seams, so the Windows branch
runs under test on every host; production callers pass neither.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

import structlog

from nexus.bounded_subprocess import run_bounded

_log = structlog.get_logger(__name__)

__all__ = [
    "ABSENT", "EVALUATION", "NOT_WINDOWS", "OFF", "ON", "UNKNOWN",
    "BLOCKED_WINERROR", "execution_blocked", "state",
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
BLOCKED_WINERROR = 4551


def _launch_unsigned_copy() -> None:
    """Run a never-seen executable: this interpreter copied with random bytes
    appended, so the hash is new on every probe.

    Measured (T2 ``nexus_rdr/224-sac-on-baseline-control-2026-10-17``): the same
    kind of control ran unblocked on a baseline whose Smart App Control was
    not enforcing. The True branch (a 4551 from this stand-in) has not been
    observed. A Authenticode-signed interpreter keeps its signature valid
    under an appended tail, so it would run under Smart App Control too; the
    stand-in is only faithful for an unsigned one such as uv's managed Python.
    """
    with tempfile.TemporaryDirectory(prefix="nx-sac-probe-", ignore_cleanup_errors=True) as tmp:
        probe = os.path.join(tmp, "nx-sac-probe.exe")
        shutil.copyfile(sys.executable, probe)
        with open(probe, "ab") as handle:
            handle.write(os.urandom(8))
        run_bounded([probe, "--version"], timeout=30, stdin=subprocess.DEVNULL)


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
        winerror = getattr(exc, "winerror", None)
        if winerror == BLOCKED_WINERROR:
            return True
        _log.warning("sac_probe_inconclusive", error=type(exc).__name__, winerror=winerror)
        return None
    except Exception as exc:  # noqa: BLE001 — an unrunnable probe is "inconclusive", never a crash
        _log.warning("sac_probe_inconclusive", error=type(exc).__name__)
        return None
    return False
