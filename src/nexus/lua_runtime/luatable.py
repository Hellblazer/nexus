# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Build a verb result into a Lua table without letting an overrun abort the worker (RDR-242 gate r3 S3).

``LuaRuntime.table_from`` calls the Lua C API outside any protected call, so
when the table does not fit under ``max_memory`` the allocator's refusal is an
unprotected Lua error: lua prints ``PANIC: unprotected error in call to Lua
API (not enough memory)`` and the process aborts (exit 134), which the parent
could only file as ``ScriptWorkerError``. Building through a Lua function
(``t[k] = v`` inside ``lua_pcall``) is protected and raises
``LuaMemoryError``, but costs a Python-to-Lua call per node.

The two are combined. A subtree is handed to ``table_from`` only when its
worst-case Lua size, ``WORST_BYTES_PER_JSON_BYTE`` times its JSON length,
fits the memory still free, so that call cannot overrun. Otherwise the table
is created by a Lua function and filled one child at a time through another,
each child recursing the same way, so an overrun raises ``LuaMemoryError``.
Near the limit the walk degrades to one protected call per node; far from it
the whole result is one ``table_from``.

``WORST_BYTES_PER_JSON_BYTE`` is a measured bound
(``tests/lua_runtime/test_cost_measurement.py``): the worst shape found is a
list of empty tables or objects at 27.2 Lua bytes per JSON byte (peak equals
final use), so 32 leaves headroom for allocator rounding. A session-like
record list is about 4. A test builds the worst shape at the cap and must see
``LuaMemoryError``, not an abort.

Needs ``lupa`` only through the runtime handed in.
"""

from __future__ import annotations

import json
from typing import Any

#: Lua bytes per JSON byte that no shape was measured to exceed (27.2 on a
#: list of empty tables), with headroom.
WORST_BYTES_PER_JSON_BYTE: int = 32

#: Memory left free for the call frames and strings the surrounding run needs.
RESERVE_BYTES: int = 256 * 1024


class LuaTableBuilder:
    """Builds Lua tables from decoded JSON values on one ``LuaRuntime``."""

    def __init__(self, rt: Any) -> None:
        self._rt = rt
        self._new_table = rt.eval("function() return {} end")
        self._setitem = rt.eval("function(t, k, v) t[k] = v end")

    def free_bytes(self) -> int:
        limit = self._rt.get_max_memory()
        if not limit:
            return 1 << 62
        return max(0, limit - self._rt.get_memory_used() - RESERVE_BYTES)

    def build(self, value: Any, json_len: int | None = None) -> Any:
        """The Lua value for *value*; raises ``LuaMemoryError`` when it does not fit."""
        if not isinstance(value, (dict, list)):
            return value
        if json_len is None:
            json_len = len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
        if json_len * WORST_BYTES_PER_JSON_BYTE <= self.free_bytes():
            return self._rt.table_from(value, recursive=True)
        table = self._new_table()
        if isinstance(value, dict):
            for key, child in value.items():
                self._setitem(table, key, self.build(child))
        else:
            for index, child in enumerate(value, 1):
                self._setitem(table, index, self.build(child))
        return table
