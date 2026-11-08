# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The errors a script run raises (RDR-242 Technical Design 6, the ends table).

Standard library only: the worker imports this module too.
"""

from __future__ import annotations

from collections.abc import Mapping

# ScriptError kinds. The worker's error frame carries the first three;
# run_script raises "header", "args" and "name" before any worker starts
# (RDR-240 Technical Design 1; RDR-242 Technical Design 4, nexus-mdbds.9).
# "name": the name breaks the name rule, resolves outside its root, is in
# neither library root, a repo name shadows a shipped one, or names a
# repo-root procedure while the repo root is off.
KINDS: tuple[str, ...] = ("static_check", "lua", "result", "header", "args", "name")

# ScriptLimitError limits: the inner limits (a limit frame), the parent's
# kill timer (wall_time) and the OS limits (cpu_time, memory).
LIMITS: tuple[str, ...] = (
    "instructions",
    "memory",
    "stack",
    "result",
    "wall_time",
    "cpu_time",
)

# Worker counters, when known: peak_lua_bytes, instructions.
Stats = Mapping[str, int]


class ScriptRunError(Exception):
    """Base of every error ``run_script`` raises."""


class ScriptError(ScriptRunError):
    """The script was refused or raised; ``kind`` is one of ``KINDS``.

    ``static_check`` (a refusal naming its rule and line), ``lua`` (a Lua
    error with its message), ``result`` (a result the host's walk rejects),
    ``header``, ``args`` and ``name`` (refused by run_script before a worker
    starts).
    """

    def __init__(self, kind: str, detail: str, *, stats: Stats | None = None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown ScriptError kind {kind!r}")
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.stats = stats


class ScriptLimitError(ScriptRunError):
    """A limit ended the run.

    ``limit`` is one of ``instructions``, ``memory``, ``stack``, ``result``,
    ``wall_time`` or ``cpu_time``; ``value`` is the limit that was in force.
    """

    def __init__(
        self, limit: str, value: object, *, stats: Stats | None = None
    ) -> None:
        if limit not in LIMITS:
            raise ValueError(f"unknown ScriptLimitError limit {limit!r}")
        super().__init__(f"script limit {limit} ({value}) reached")
        self.limit = limit
        self.value = value
        self.stats = stats


class ScriptWorkerError(ScriptRunError):
    """The worker ended in a way the ends table does not map to the two above.

    Another signal, another exit code, end of file with no result frame, a bad
    or over-cap frame, or the 10,001st verb call. Carries the exit status or
    signal and the first 64 KB of the worker's stderr.
    """

    def __init__(
        self,
        detail: str,
        *,
        returncode: int | None = None,
        signal: int | None = None,
        stderr_head: str = "",
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.returncode = returncode
        self.signal = signal
        self.stderr_head = stderr_head


class ScriptBusyError(ScriptRunError):
    """The client already runs its maximum of concurrent workers (4)."""
