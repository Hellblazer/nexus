# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The runner: ``run_procedure(name, args)`` (RDR-240 Technical Design 2 and 3; nexus-qo7ie.31).

One Python function calls RDR-242's ``run_script(name, args)`` and returns its JSON result.
It reads no file and checks nothing: the lookup, the name rule, the header, the argument
check, the static check, the grants and the limits all run inside ``run_script``, and its
errors (:class:`~nexus.lua_runtime.errors.ScriptError`, ``ScriptLimitError``,
``ScriptWorkerError``, ``ScriptBusyError``) pass through as the same objects.

``run_script`` is looked up on :mod:`nexus.lua_runtime` when the call is made, not when this
module is imported, because the wiring that adds it (nexus-mdbds.9) lands separately; until
then the call raises :class:`RunnerUnavailableError`.

The front doors, the MCP tool ``procedure_run`` and the CLI verb ``nx run``, call
``run_procedure`` and nothing else, and show an error through :func:`describe_error`
(a lint in ``tests/test_procedure_front_doors_lint.py`` pins both).
"""
from __future__ import annotations

from collections.abc import Mapping

import nexus.lua_runtime as _runtime
from nexus.lua_runtime.errors import (
    ScriptBusyError,
    ScriptError,
    ScriptLimitError,
    ScriptRunError,
    ScriptWorkerError,
)
from nexus.lua_runtime.header import RepoProbeError
from nexus.lua_runtime.library import LibraryError
from nexus.lua_runtime.verb_types import JSONValue


class RunnerUnavailableError(RuntimeError):
    """``nexus.lua_runtime.run_script`` does not exist in this build."""


#: Every error a procedure run can end in that a front door shows as text. Anything else is
#: a programming error and propagates.
PROCEDURE_ERRORS: tuple[type[Exception], ...] = (
    ScriptRunError,
    LibraryError,
    RunnerUnavailableError,
    RepoProbeError,
)


def run_procedure(name: str, args: Mapping[str, JSONValue]) -> JSONValue:
    """Run the library procedure *name* with *args* and return its JSON result."""
    run_script = getattr(_runtime, "run_script", None)
    if run_script is None:
        raise RunnerUnavailableError(
            "nexus.lua_runtime.run_script is not installed in this build, so no procedure can run"
        )
    return run_script(name, args)


def describe_error(exc: BaseException) -> str | None:
    """``"<kind>: <detail>"`` for an error of :data:`PROCEDURE_ERRORS`, else ``None``.

    The kind is the :class:`ScriptError` kind (``static_check``, ``lua``, ``result``,
    ``header``, ``args``) or one of ``limit``, ``worker``, ``busy``, ``library`` (detail
    ``<reason>: <what>``), ``unavailable`` and ``repo``.
    """
    if isinstance(exc, ScriptError):
        return f"{exc.kind}: {exc.detail}"
    if isinstance(exc, ScriptLimitError):
        return f"limit: {exc.limit} ({exc.value}) reached"
    if isinstance(exc, ScriptWorkerError):
        extra = "".join(
            f" ({label} {value})"
            for label, value in (("exit", exc.returncode), ("signal", exc.signal))
            if value is not None
        )
        return f"worker: {exc.detail}{extra}"
    if isinstance(exc, ScriptBusyError):
        return f"busy: {exc}" if str(exc) else "busy: the client already runs its maximum of workers"
    if isinstance(exc, LibraryError):
        return f"library: {exc.reason}: {exc.detail}"
    if isinstance(exc, RunnerUnavailableError):
        return f"unavailable: {exc}"
    if isinstance(exc, RepoProbeError):
        return f"repo: {exc}"
    return None
