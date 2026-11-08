# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""STEP 2 STAND-IN for the worker's Lua runtime; nexus-mdbds.5 replaces this file.

The seam (T2 ``nexus/rdr242-interfaces``, "The .4 / .5 seam") is
``run(source, grants, args, limits, call_verb) -> (value, stats)`` and the ``Limits``
dataclass; the worker (``worker.py``) calls nothing else here. What is below
is only enough to run a one-line fixture through the real worker process and
to carry three things Step 2 owns that Step 3 must keep:

* the verb-result bound and the protected table build (``luatable``, gate r3
  S3): a verb result reaches Lua only through ``LuaTableBuilder.build``;
* the count hook that feeds ``stats["instructions"]`` and, with the sampling
  at each verb call, ``stats["peak_lua_bytes"]``;
* the dispatcher shape of Technical Design item 9: it never raises into Lua,
  it returns an error message and the verb function raises it as a Lua error.

Replaced by Step 3: the restricted environment (``_ENV_CHUNK`` here is the
granted verbs plus a few base functions), ``load`` hardening, the guarded
``pcall``, the limit hook, the result walk.

``Limits`` is imported by the parent too, so this module imports ``lupa``
only inside ``run``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nexus.lua_runtime.errors import ScriptError, ScriptLimitError, Stats
from nexus.lua_runtime.frames import VerbReplyError
from nexus.lua_runtime.verb_types import JSONValue

_MIB = 1024 * 1024
HOOK_EVERY = 10_000


@dataclass(frozen=True)
class Limits:
    """Technical Design 11's defaults, each lowered by the caller and never raised."""

    instructions: int = 50_000_000
    memory: int = 64 * _MIB
    wall_time: float = 60.0
    result: int = 1 * _MIB
    depth: int = 32

    def lowered(self, **changes: float) -> Limits:
        values = dict(self.__dict__)
        for key, value in changes.items():
            if key not in values:
                raise ValueError(f"unknown limit {key!r}")
            if value > values[key]:
                raise ValueError(
                    f"limit {key} can be lowered, not raised ({value} > {values[key]})"
                )
            values[key] = type(values[key])(value)
        return Limits(**values)

    def to_frame(self) -> dict[str, float]:
        return dict(self.__dict__)

    @classmethod
    def from_frame(cls, raw: dict[str, Any]) -> Limits:
        base = cls()
        return cls(
            **{k: type(v)(raw[k]) if k in raw else v for k, v in base.__dict__.items()}
        )


DEFAULT_LIMITS = Limits()

#: The granted verbs under their namespaces plus a few base functions.
_ENV_CHUNK = """
return function(dispatch, fetch, names, base)
  local env = {}
  for k, v in pairs(base) do env[k] = v end
  for _, full in ipairs(names) do
    local ns, member = full:match("^([^.]+)%.(.+)$")
    local t = env[ns]
    if not t then t = {}; env[ns] = t end
    t[member] = function(...)
      local err = dispatch(full, select("#", ...), ...)
      if err then error(err, 0) end
      return fetch()
    end
  end
  return env
end
"""
_BASE_CHUNK = """
return {type = type, tostring = tostring, tonumber = tonumber, pairs = pairs, ipairs = ipairs,
        select = select, next = next, error = error, assert = assert, pcall = pcall,
        string = string, table = table, math = math}
"""
_LOAD_CHUNK = """
return function(src, env)
  local f, err = load(src, "=script", "t", env)
  if not f then error(err, 0) end
  return f
end
"""
_HOOK_CHUNK = """
return function(tick, every)
  debug.sethook(function() tick() end, "", every)
end
"""


def _refuse(obj: Any, attr: Any, is_setting: bool) -> Any:
    raise AttributeError("access refused")


def run(
    source: bytes,
    grants: frozenset[str],
    args: dict[str, Any],
    limits: Limits,
    call_verb: Callable[[str, list[Any]], JSONValue],
) -> tuple[JSONValue, Stats]:
    """Run *source* once; return its value and stats, or raise ``ScriptError`` / ``ScriptLimitError``.

    *args* is ``header.prepare_args``'s result (an absent optional is left out, never null); the script gets it
    as one table through the main chunk's vararg (``local args = ...``).
    """
    import lupa.lua54 as lua  # noqa: PLC0415

    from nexus.lua_runtime.luatable import LuaTableBuilder  # noqa: PLC0415

    rt = lua.LuaRuntime(
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=False,
        attribute_filter=_refuse,
        max_memory=limits.memory,
    )
    rt.set_overflow_handler(float)
    builder = LuaTableBuilder(rt)
    state: dict[str, Any] = {
        "peak": 0,
        "instructions": 0,
        "pending": None,
        "memory_tripped": False,
    }

    def sample() -> None:
        used = rt.get_memory_used() or 0
        if used > state["peak"]:
            state["peak"] = used

    def stats() -> Stats:
        sample()
        return {"peak_lua_bytes": state["peak"], "instructions": state["instructions"]}

    def tick() -> None:
        state["instructions"] += HOOK_EVERY
        sample()

    def dispatch(name: str, n: int, *args: Any) -> str | None:
        """Never raises into Lua: None on success, else the error message."""
        try:
            call_args = list(args[:n])
            for value in call_args:
                if value is not None and not isinstance(value, (bool, int, float, str)):
                    return f"{name}: arguments must be plain values (string, number, boolean, nil)"
            try:
                value = call_verb(name, call_args)
            except VerbReplyError as exc:
                return str(exc)
            try:
                state["pending"] = builder.build(value)
            except lua.LuaMemoryError:
                state["memory_tripped"] = True
                return "memory limit reached"
            sample()
            return None
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the dispatcher never raises into Lua
            return f"{name}: worker error ({type(exc).__name__})"

    def fetch() -> Any:
        value, state["pending"] = state["pending"], None
        return value

    try:
        base = rt.execute(_BASE_CHUNK)
        env = rt.execute(_ENV_CHUNK)(
            dispatch, fetch, rt.table_from(sorted(grants)), base
        )
        fn = rt.execute(_LOAD_CHUNK)(source.decode("utf-8", "replace"), env)
        rt.execute(_HOOK_CHUNK)(tick, HOOK_EVERY)
        result = fn(builder.build(args))
        if state["memory_tripped"]:
            raise lua.LuaMemoryError("verb result did not fit")
        value = _plain(result)
    except lua.LuaMemoryError:
        raise ScriptLimitError("memory", limits.memory, stats=stats()) from None
    except lua.LuaError as exc:
        if state["memory_tripped"]:
            raise ScriptLimitError("memory", limits.memory, stats=stats()) from None
        detail = str(exc).split("\nstack traceback:", 1)[0]
        raise ScriptError("lua", detail, stats=stats()) from None
    except _NotPlain as exc:
        raise ScriptError("result", str(exc), stats=stats()) from None
    import json  # noqa: PLC0415

    if (
        len(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        > limits.result
    ):
        raise ScriptLimitError("result", limits.result, stats=stats())
    return value, stats()


class _NotPlain(Exception):
    pass


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON value for a Lua scalar or table (Step 2 stand-in; Step 3 owns the real walk)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 32:
        raise _NotPlain("result nested too deeply")
    items = getattr(value, "items", None)
    if items is None:
        raise _NotPlain(f"a {type(value).__name__} is not a plain result")
    pairs = list(items())
    if pairs and all(isinstance(k, int) for k, _ in pairs):
        return [_plain(v, depth + 1) for _, v in sorted(pairs)]
    if all(isinstance(k, str) for k, _ in pairs):
        return {k: _plain(v, depth + 1) for k, v in pairs}
    raise _NotPlain("a table with mixed keys is not a plain result")
