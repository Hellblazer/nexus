# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242: sandboxed Lua scripts run in a worker process.

The shared seams (T2 ``nexus/rdr242-interfaces``): ``errors`` (what a run
raises), ``verb_types`` (what a verb is) and ``verbs`` (the verb table).
``errors`` and ``verb_types`` import nothing from nexus, so the worker loads
them under ``python -I`` with an empty environment.
"""


def run_script(name, args=None, limits=None):  # type: ignore[no-untyped-def]
    """RDR-242's run path: run the library procedure *name* (see :mod:`nexus.lua_runtime.run`).

    Loaded lazily: the worker imports this package under ``python -I`` with an empty
    environment, so the package itself imports nothing from the rest of nexus.
    """
    from nexus.lua_runtime.run import run_script as _run_script  # noqa: PLC0415 - parent-only module

    return _run_script(name, args, limits)
