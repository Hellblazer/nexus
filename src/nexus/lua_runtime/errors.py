# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The errors a script run raises (RDR-242 Technical Design 6, the ends table).

Standard library only: the worker imports this module too.
"""

from __future__ import annotations


class ScriptRunError(Exception):
    """Base of every error ``run_script`` raises."""


class ScriptError(ScriptRunError):
    """The script was refused or raised.

    ``kind`` is ``"static_check"`` (a static-check refusal naming its rule and
    line), ``"lua"`` (a Lua error with its message) or ``"result"`` (a result
    the host's walk rejects).
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class ScriptLimitError(ScriptRunError):
    """A limit ended the run.

    ``limit`` is one of ``instructions``, ``memory``, ``stack``, ``result``,
    ``wall_time`` or ``cpu_time``; ``value`` is the limit that was in force.
    """

    def __init__(self, limit: str, value: object) -> None:
        super().__init__(f"script limit {limit} ({value}) reached")
        self.limit = limit
        self.value = value


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
