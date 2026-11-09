# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Test support for the in-worker Lua runtime (RDR-242 Phase 1 Step 3, nexus-mdbds.5).

The only source-taking entry point in the tree. Nothing under ``src/nexus/``
takes script source from a caller (RDR-242 Technical Design 2, gate r3 S1;
``test_runtime_origin.py`` pins that): the code that runs a script is
``runtime.run``, which only the worker calls with the bytes of a library file.
Tests drive ``run`` in process through ``run_lua`` below, with the verbs as
plain Python functions and a small frozen ``Limits`` of their own (the real
``Limits`` is the worker's, ``limits.py``; ``run`` reads five attributes of it).

This module is independent of ``_support.py`` on purpose: that one imports the
worker-side ``Limits`` and the supervisor, and the runtime suites need neither.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from nexus.lua_runtime import runtime
from nexus.lua_runtime.frames import VerbReplyError

_MIB = 1024 * 1024


@dataclass(frozen=True)
class Limits:
    """Technical Design 11's defaults; ``run`` reads these five attributes."""

    instructions: int = 50_000_000
    memory: int = 64 * _MIB
    wall_time: float = 60.0
    result: int = 1 * _MIB
    depth: int = 32

    def with_(self, **changes: Any) -> Limits:
        return replace(self, **changes)


DEFAULT = Limits()


def run_lua(
    source: str | bytes,
    *grants: str,
    args: Mapping[str, Any] | None = None,
    limits: Limits = DEFAULT,
    verbs: Mapping[str, Callable[..., Any]] | None = None,
    call_verb: Callable[[str, list[Any]], Any] | None = None,
) -> tuple[Any, Mapping[str, int]]:
    """Run *source* in process; return ``(value, stats)`` or raise what ``run`` raises.

    *verbs* maps a verb name to a Python function of its arguments; an exception
    from one arrives in the script as a Lua error string, as the worker's
    ``call_verb`` does for an ``ok: false`` reply (``VerbReplyError``).
    """
    data = source.encode("utf-8") if isinstance(source, str) else source
    table = dict(verbs or {})

    def default_call(name: str, call_args: list[Any]) -> Any:
        try:
            fn = table[name]
        except KeyError:
            raise VerbReplyError(f"{name}: no such fixture verb") from None
        try:
            return fn(*call_args)
        except VerbReplyError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a verb error is a reply with ok: false
            raise VerbReplyError(str(exc)) from None

    return runtime.run(
        data,
        frozenset(grants),
        dict(args or {}),
        limits,
        call_verb or default_call,
    )
