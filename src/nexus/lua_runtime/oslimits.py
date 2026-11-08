# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The worker's OS limits (RDR-242 Technical Design 6, "OS limits").

CPU time is the wall-time limit plus 5 s (soft) and plus 10 s (hard):
``RLIMIT_CPU`` on Linux and macOS (the worker sets its own, first, before it
reads anything), a per-process user-time limit of the job on native Windows
(the parent sets it, ``nexus.util.win_job``). Memory is 1 GiB: ``RLIMIT_AS``
on Linux, a per-process memory limit of the job on Windows. On macOS
``setrlimit(RLIMIT_AS)`` raises ``ValueError`` (T2 ``nexus_rdr/242-research-32``),
so the worker sets it in a ``try`` and ``max_memory`` is the only memory bound
there. Any other failure to set a limit ends the run (the worker exits 4
before the source is read; the parent files that as ``ScriptWorkerError``).

The limits reach the worker as arguments after the module name, because the
environment is empty and the worker may read nothing before it has set them.

Standard library only: the worker imports this module under ``python -I``.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Any

#: The worker could not set an OS limit.
EXIT_LIMIT_SETUP_FAILED: int = 4

DEFAULT_CPU_MARGINS: tuple[int, int] = (5, 10)
DEFAULT_AS_BYTES: int = 1 << 30


@dataclass(frozen=True)
class OsLimits:
    cpu_soft_s: int
    cpu_hard_s: int
    as_bytes: int

    def to_args(self) -> list[str]:
        return [
            "--cpu-soft",
            str(self.cpu_soft_s),
            "--cpu-hard",
            str(self.cpu_hard_s),
            "--as-bytes",
            str(self.as_bytes),
        ]

    @classmethod
    def from_args(cls, args: list[str]) -> OsLimits:
        values: dict[str, int] = {}
        it = iter(args)
        for flag in it:
            if flag not in ("--cpu-soft", "--cpu-hard", "--as-bytes"):
                raise ValueError(f"unknown worker argument {flag!r}")
            values[flag] = int(next(it))
        return cls(values["--cpu-soft"], values["--cpu-hard"], values["--as-bytes"])


def limits_for(
    wall_time_s: float,
    *,
    cpu_margins: tuple[int, int] = DEFAULT_CPU_MARGINS,
    as_bytes: int = DEFAULT_AS_BYTES,
) -> OsLimits:
    """The OS limits for a run of *wall_time_s* seconds of wall time."""
    base = math.ceil(wall_time_s)
    return OsLimits(base + cpu_margins[0], base + cpu_margins[1], as_bytes)


def apply_in_worker(
    limits: OsLimits, *, platform: str | None = None, resource_module: Any = None
) -> dict[str, Any]:
    """Set this process's limits (POSIX); report what was set.

    On Windows nothing is set here: the parent's job carries the limits.
    A ``ValueError`` from ``RLIMIT_AS`` is caught on macOS only; every other
    failure propagates. ``platform`` and ``resource_module`` are test seams.
    """
    plat = sys.platform if platform is None else platform
    if plat == "win32":
        return {"cpu": None, "as": None, "as_error": None, "by": "job"}
    res = resource_module
    if res is None:
        import resource as res  # noqa: PLC0415 -- absent on Windows
    report: dict[str, Any] = {
        "cpu": (limits.cpu_soft_s, limits.cpu_hard_s),
        "as": None,
        "as_error": None,
        "by": "rlimit",
    }
    res.setrlimit(res.RLIMIT_CPU, (limits.cpu_soft_s, limits.cpu_hard_s))
    try:
        res.setrlimit(res.RLIMIT_AS, (limits.as_bytes, limits.as_bytes))
        report["as"] = limits.as_bytes
    except ValueError as exc:
        if plat != "darwin":
            raise
        report["as_error"] = f"ValueError: {exc}"
    return report
