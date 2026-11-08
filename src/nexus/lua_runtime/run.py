# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 4: the run path, ``run_script(name, args, limits=None)`` (nexus-mdbds.9).

The only way a script runs: by NAME, from a library root, never from source a caller hands in
(the no-source meta-test, ``tests/lua_runtime/test_runtime_origin.py``). In order, each step
refusing before any worker starts:

1. probe the repo once (``header.probe_repo`` over this process's working directory, or none
   when the directory is gone); the probe names the reserved ``repo`` and ``VerbContext.repo_root``;
2. find the file (``library.find``): a refusal is ``ScriptError("name", "<reason>: <detail>")``;
3. parse its header (``header.parse``): ``ScriptError("header", ...)``;
4. check and fill the arguments (``header.prepare_args``): ``ScriptError("args", ...)``;
5. lower the limits (``Limits.lowered``; a raised or invalid limit is ``ScriptError("args", ...)``);
6. run it in a worker (``supervisor.supervise``), whose errors pass through unchanged.

This module runs in the calling process (the MCP server or ``nx``), never in the worker, so it
may import the rest of nexus; ``nexus.lua_runtime.run_script`` loads it lazily, because the
worker imports the package under ``python -I``. Contract: T2 ``nexus/rdr242-interfaces``, section C
and the supervisor seam.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from nexus.lua_runtime import header, library, supervisor
from nexus.lua_runtime.errors import ScriptError, Stats
from nexus.lua_runtime.limits import DEFAULT_LIMITS
from nexus.lua_runtime.verb_types import JSONValue, VerbContext


def _cwd() -> Path | None:
    try:
        return Path(os.getcwd())
    except OSError:  # a deleted working directory (Claude Desktop's replaced plugin copy)
        return None


def _run_script_with_stats(
    name: str, args: Mapping[str, JSONValue] | None = None, limits: Mapping[str, float] | None = None,
) -> tuple[JSONValue, Stats]:
    """``run_script`` that also returns the worker's stats (peak Lua bytes, instructions)."""
    cwd = _cwd()
    try:
        probe = header.probe_repo(cwd)
    except header.RepoProbeError:
        # Only a header that declares repo needs it; prepare_args refuses that case itself.
        probe = None
        probe_failed = True
    else:
        probe_failed = False
    try:
        _path, text = library.find(name, cwd=cwd, probe=probe)
    except library.LibraryError as exc:
        raise ScriptError("name", f"{exc.reason}: {exc.detail}") from exc
    parsed = header.parse(text, name=name)
    if probe_failed and any(a.name == "repo" for a in parsed.args):
        prepared = header.prepare_args(parsed, dict(args or {}), cwd=cwd)  # re-probes and refuses
    else:
        prepared = header.prepare_args(parsed, dict(args or {}), cwd=cwd, repo_probe=lambda _: probe)
    try:
        lowered = DEFAULT_LIMITS.lowered(**dict(limits or {}))
    except (TypeError, ValueError) as exc:
        raise ScriptError("args", f"limits: {exc}") from exc
    outcome = supervisor.supervise(
        {"name": name, "source": text.decode("ascii", "replace"), "grants": sorted(parsed.grants),
         "args": prepared},
        limits=lowered,
        config=supervisor.WorkerConfig(ctx=VerbContext(repo_root=probe.root if probe else None)),
    )
    return outcome.value, outcome.stats


def run_script(
    name: str, args: Mapping[str, JSONValue] | None = None, limits: Mapping[str, float] | None = None,
) -> JSONValue:
    """Run the library procedure *name* with *args* and return its JSON result.

    *limits* lowers RDR-242's defaults (never raises them). Raises ``ScriptError`` (kinds ``name``,
    ``header``, ``args`` before any worker starts; ``static_check``, ``lua``, ``result`` from the
    worker), ``ScriptLimitError``, ``ScriptWorkerError`` or ``ScriptBusyError``.
    """
    return _run_script_with_stats(name, args, limits)[0]
