# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The sandboxed Lua runtime inside the worker (RDR-242 Technical Design 5, 7, 8, 9, 11, 12).

``run`` is the one entry point, and the worker is its one caller::

    run(source, grants, args, limits, call_verb) -> (value, stats)

``source`` is the bytes of a library file that the static check has already
passed; nothing under ``src/nexus/`` takes source from a caller (the origin
test, ``tests/lua_runtime/test_runtime_origin.py``). ``limits`` is read for five
attributes (``instructions``, ``memory``, ``wall_time``, ``result``,
``depth``); its class is the worker's, not this module's.

The pieces, in the order a run meets them:

* A ``lupa.lua54`` runtime with ``register_eval``, ``register_builtins`` and
  ``unpack_returned_tuples`` off, ``max_memory`` set, and an attribute filter
  that refuses every attribute (item 5). One runtime per run; the worker
  process is discarded after it, so no state carries over.
* A host chunk (``_BOOT``) runs in the full ``_G``, builds the script's
  environment from copies of the safe base functions and the allowed library
  members (the static check's lists, imported, not copied), a guarded
  ``pcall``, the string metatable's reduced ``__index`` and one Lua function
  per granted verb, and then wipes the real ``_G`` (items 7 and 9).
* The source is refused when its first byte is 27, then compiled in text mode
  with ``load(src, "=script", "t", env)`` (item 8).
* A count hook every ``HOOK_EVERY`` instructions enforces the instruction and
  wall-time limits; once a limit trips it rearms to fire on every instruction,
  and raises whenever the interrupted code is the script's. The guarded
  ``pcall`` re-raises a tripped limit, a memory error and a stack overflow
  (item 11 and the Key Discoveries on the two ``pcall`` escapes).
* The returned value is walked into JSON by host Lua, inside a protected call,
  bounded by ``limits.result`` bytes and ``limits.depth`` levels (item 12).

Everything that can fail runs in Lua and comes back as a status and a string,
so a limit is classified from state the script cannot reach (the ``tripped``
upvalue of the host chunk), never from a message the script can write. The
exceptions are a script that spells the message of a memory or a stack error
itself: it is then filed as that limit, which hurts only the script.

Standard library, ``lupa`` and the worker's own modules only: this runs in
``python -I`` with an empty environment.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import lupa.lua54 as lua

from nexus.lua_runtime.errors import ScriptError, ScriptLimitError, Stats
from nexus.lua_runtime.frames import VerbReplyError
from nexus.lua_runtime.luatable import LuaTableBuilder
from nexus.lua_runtime.static_check import BASE_FUNCTIONS, LIB_MEMBERS
from nexus.lua_runtime.verb_types import JSONValue

#: The count hook fires every this many VM instructions. A limit below it is reached at the first hook.
HOOK_EVERY: int = 10_000

#: ``LUAI_MAXSTACK`` of Lua 5.4, the value a ``stack`` limit error carries (both the Lua stack and
#: the C stack overflow are this limit).
STACK_LIMIT: int = 1_000_000

#: A Lua error message longer than this is cut (on a character boundary) before it leaves the worker.
MAX_MESSAGE_BYTES: int = 2048


class _LimitFields(Protocol):
    """What ``run`` reads of the worker's ``Limits`` (``limits.py``)."""

    @property
    def instructions(self) -> int: ...
    @property
    def memory(self) -> int: ...
    @property
    def wall_time(self) -> float: ...
    @property
    def result(self) -> int: ...
    @property
    def depth(self) -> int: ...


def _refuse(obj: Any, attr: Any, is_setting: bool) -> Any:
    raise AttributeError("access refused")


# --- the host chunk -----------------------------------------------------------------------------

_IDENT = re.compile(r"[A-Za-z0-9_]+")


def _lua_names(names: Any) -> str:
    items = sorted(names)
    for name in items:
        if not _IDENT.fullmatch(name):
            raise ValueError(f"not a plain name: {name!r}")
    return "{" + ",".join(f'"{name}"' for name in items) + "}"


def _lua_members(members: Mapping[str, Any]) -> str:
    for lib in members:
        if not _IDENT.fullmatch(lib):
            raise ValueError(f"not a plain name: {lib!r}")
    return (
        "{" + ",".join(f"{lib}={_lua_names(m)}" for lib, m in sorted(members.items())) + "}"
    )


# Everything the chunk needs from the real _G is taken into a local before the wipe; after it, no
# name the chunk's functions use may be a global.
_BOOT_TEMPLATE = r"""
local pcall, error, select, type, tostring, next, pairs = pcall, error, select, type, tostring, next, pairs
local load, getmetatable, collectgarbage = load, getmetatable, collectgarbage
local sethook, getinfo = debug.sethook, debug.getinfo
local find, byte, sub, gsub, format, match = string.find, string.byte, string.sub, string.gsub, string.format, string.match
local utf8_len = utf8.len
local math_type, huge = math.type, math.huge
local tsort, tconcat = table.sort, table.concat
local G = _G
local libs = {string = string, table = table, math = math, utf8 = utf8}
local BASE = __BASE__
local MEMBERS = __MEMBERS__
local MEMERR = "not enough memory"
local MAX_MESSAGE = __MAX_MESSAGE__

return function(dispatch, fetch, clock, t0, names, max_instr, wall, every, max_depth, max_bytes)
  local count, tripped, peak = 0, nil, 0
  local hook

  -- The limit state lives here, in upvalues no script can reach.
  local function trip(kind)
    tripped = kind
    sethook(hook, "", 1)
    error("script limit: " .. kind, 0)
  end

  hook = function()
    if tripped then
      -- rearmed: every instruction of the script raises again; the host's own code is left alone,
      -- so the runner below can report what happened
      local info = getinfo(2, "S")
      if info and info.source == "=script" then error("script limit: " .. tripped, 0) end
      return
    end
    count = count + every
    local kb = collectgarbage("count")
    if kb > peak then peak = kb end
    if count > max_instr then trip("instructions") end
    if clock() - t0 > wall then trip("wall_time") end
  end

  -- pcall for the script: an ordinary error is returned, a limit is not swallowed.
  local function settle(ok, ...)
    if ok then return true, ... end
    local e = ...
    if not tripped and type(e) == "string" then
      if e == MEMERR then
        tripped = "memory"
      elseif find(e, "stack overflow", 1, true) or e == "error in error handling" then
        tripped = "stack"
      end
      if tripped then sethook(hook, "", 1) end
    end
    if tripped then error(e, 0) end
    return false, ...
  end
  local function gpcall(f, ...) return settle(pcall(f, ...)) end

  -- Error messages leave as valid UTF-8 of bounded length.
  local function clean(s)
    if #s > MAX_MESSAGE then
      local cut = MAX_MESSAGE
      while cut > 0 and ((byte(s, cut + 1) or 0) & 0xC0) == 0x80 do cut = cut - 1 end
      s = sub(s, 1, cut) .. "..."
    end
    if not utf8_len(s) then s = gsub(s, "[\128-\255]", "?") end
    return s
  end

  -- One Lua function per granted verb. Arguments are checked here as well as in the dispatcher, so
  -- the dispatcher never sees a table, a function or bytes that do not decode.
  local function make_verb(full)
    return function(...)
      local n = select("#", ...)
      for i = 1, n do
        local a = select(i, ...)
        local t = type(a)
        if t == "string" then
          if not utf8_len(a) then error(full .. ": argument " .. i .. " is not valid UTF-8", 0) end
        elseif t == "number" then
          if a ~= a or a == huge or a == -huge then error(full .. ": argument " .. i .. " is not a finite number", 0) end
        elseif t ~= "nil" and t ~= "boolean" then
          error(full .. ": arguments must be plain values (string, number, boolean, nil)", 0)
        end
      end
      local err = dispatch(full, n, ...)
      if err then
        if err == true then trip("memory") end
        error(err, 0)
      end
      return fetch()
    end
  end

  -- The script's environment: copies, never the host's tables.
  local env = {}
  for i = 1, #BASE do
    local name = BASE[i]
    local fn = G[name]
    if fn == nil then error("host function missing: " .. name, 0) end
    env[name] = fn
  end
  env.pcall = gpcall
  for lib, members in next, MEMBERS do
    local src, copy = libs[lib], {}
    for i = 1, #members do
      local m = members[i]
      local v = src[m]
      if v == nil then error("host library member missing: " .. lib .. "." .. m, 0) end
      copy[m] = v
    end
    env[lib] = copy
  end
  getmetatable("").__index = env.string

  local namespaces = {}
  for i = 1, #names do
    local full = names[i]
    local ns, member = match(full, "^([^.]+)%.(.+)$")
    local t = namespaces[ns]
    if not t then
      if env[ns] ~= nil then error("verb namespace collides with the environment: " .. ns, 0) end
      t = {}
      namespaces[ns] = t
      env[ns] = t
    end
    t[member] = make_verb(full)
  end

  -- The result walk: JSON text, bounded in bytes and depth, built piece by piece.
  local LIMIT_RESULT, LIMIT_DEPTH = {}, {}
  local ESC = {['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t', ['\b'] = '\\b', ['\f'] = '\\f'}
  local function esc(c) return ESC[c] or format("\\u%04x", byte(c)) end
  local parts, np, chunks, nc, size, active

  local function fail(msg) error({msg = msg}, 0) end

  local function put(s)
    size = size + #s
    if size > max_bytes then error(LIMIT_RESULT, 0) end
    np = np + 1
    parts[np] = s
    if np >= 2048 then
      nc = nc + 1
      chunks[nc] = tconcat(parts, "", 1, np)
      np = 0
    end
  end

  local function put_string(s)
    if #s + size > max_bytes then error(LIMIT_RESULT, 0) end
    if not utf8_len(s) then fail("a string that is not valid UTF-8 is not a plain result") end
    -- explicit control bytes, not %c: iscntrl follows the process locale and reads UTF-8 lead bytes as controls
    put('"' .. gsub(s, '[\0-\31"\\]', esc) .. '"')
  end

  local function enc(v, depth)
    local t = type(v)
    if t == "string" then
      put_string(v)
    elseif t == "number" then
      if math_type(v) == "integer" then
        put(format("%d", v))
      else
        if v ~= v then fail("NaN is not a plain result (JSON has no NaN)") end
        if v == huge or v == -huge then fail("infinity is not a plain result (JSON has no infinity)") end
        local s = format("%.17g", v)
        if not find(s, "[.eE]") then s = s .. ".0" end
        put(s)
      end
    elseif t == "boolean" then
      put(v and "true" or "false")
    elseif t == "nil" then
      put("null")
    elseif t == "table" then
      if depth >= max_depth then error(LIMIT_DEPTH, 0) end
      if active[v] then fail("a cyclic table is not a plain result") end
      active[v] = true
      local ni, maxi, keys, nk = 0, 0, nil, 0
      for k in next, v do
        if math_type(k) == "integer" and k >= 1 then
          ni = ni + 1
          if k > maxi then maxi = k end
        elseif type(k) == "string" then
          if not keys then keys = {} end
          nk = nk + 1
          keys[nk] = k
        else
          fail("a table key of type " .. type(k) .. " (or a number that is not a positive integer) is not a plain result")
        end
      end
      if ni > 0 and nk > 0 then
        fail("a table with mixed keys (array and object) is not a plain result")
      elseif ni > 0 then
        if maxi ~= ni then fail("an array with holes is not a plain result") end
        put("[")
        for i = 1, ni do
          if i > 1 then put(",") end
          enc(v[i], depth + 1)
        end
        put("]")
      elseif nk > 0 then
        tsort(keys)
        put("{")
        for i = 1, nk do
          local k = keys[i]
          if i > 1 then put(",") end
          put_string(k)
          put(":")
          enc(v[k], depth + 1)
        end
        put("}")
      else
        put("{}")
      end
      active[v] = nil
    else
      fail("a " .. t .. " is not a plain result")
    end
  end

  local function encode(v)
    parts, np, chunks, nc, size, active = {}, 0, {}, 0, 0, {}
    enc(v, 0)
    return tconcat(chunks, "", 1, nc) .. tconcat(parts, "", 1, np)
  end

  local function classify(e)
    local t = type(e)
    if t == "string" then
      if e == MEMERR then return "limit", "memory" end
      if find(e, "stack overflow", 1, true) or e == "error in error handling" then return "limit", "stack" end
      return "lua", clean(e)
    end
    if t == "number" then return "lua", tostring(e) end
    if t == "nil" then return "lua", "nil" end
    return "lua", "(error object is a " .. t .. " value)"
  end

  local function classify_walk(e)
    if e == LIMIT_RESULT then return "limit", "result" end
    if e == LIMIT_DEPTH then return "limit", "depth" end
    if type(e) == "table" and type(e.msg) == "string" then return "result", e.msg end
    return classify(e)
  end

  local function compile(src)
    if byte(src, 1) == 27 then return nil, "binary chunks are refused" end
    local f, err = load(src, "=script", "t", env)
    if f then return f end
    return nil, clean(err)
  end

  -- Runs the script with the hook armed, then walks its value with the hook off.
  local function run_main(f, args)
    sethook(hook, "", every)
    local ok, v = pcall(f, args)
    sethook()
    if tripped then return "limit", tripped end
    if not ok then return classify(v) end
    local ok2, out = pcall(encode, v)
    if ok2 then return "ok", out end
    return classify_walk(out)
  end

  local function stats()
    local kb = collectgarbage("count")
    if kb > peak then peak = kb end
    return count, peak * 1024
  end

  local function status() return tripped end

  for k in next, G do G[k] = nil end
  return compile, run_main, stats, status
end
"""

_BOOT = (
    _BOOT_TEMPLATE.replace("__BASE__", _lua_names(BASE_FUNCTIONS))
    .replace("__MEMBERS__", _lua_members(LIB_MEMBERS))
    .replace("__MAX_MESSAGE__", str(MAX_MESSAGE_BYTES))
)

# --- grants and the dispatcher ----------------------------------------------------------------------

_GRANT = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_JSON_RESULT = (dict, list, str, int, float, bool, type(None))


def _validated_grants(grants: frozenset[str]) -> list[str]:
    taken = set(BASE_FUNCTIONS) | set(LIB_MEMBERS)
    names = sorted(grants)
    for grant in names:
        if not _GRANT.fullmatch(grant):
            raise ValueError(
                f"bad grant {grant!r}: a verb is <namespace>.<member> in lower case"
            )
        if grant.split(".", 1)[0] in taken:
            raise ValueError(f"grant {grant!r} collides with a name of the environment")
    return names


def _argument_problem(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else "arguments must be finite numbers"
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "arguments must be valid UTF-8"
        return None
    return "arguments must be plain values (string, number, boolean, nil)"


class _Dispatcher:
    """The one Python object that crosses into Lua (Technical Design 9).

    ``dispatch(name, n, *args)`` never raises into Lua: it returns ``None`` on success
    (the result waits for ``fetch``), ``True`` when the result did not fit the memory
    limit, and otherwise the error message, which the verb function raises as a Lua
    error string. A Python exception handed to ``pcall`` would be a host object.
    """

    def __init__(
        self,
        grants: frozenset[str],
        call_verb: Callable[[str, list[Any]], JSONValue],
        build: Callable[[Any], Any],
        sample: Callable[[], None],
    ) -> None:
        self._grants = grants
        self._call_verb = call_verb
        self._build = build
        self._sample = sample
        self._pending: Any = None
        #: set when the process itself ran out of memory inside a verb call; ``run`` re-raises it
        #: after the script has been stopped, so the worker exits 3 (the OS memory limit's end).
        self.out_of_memory: bool = False

    def fetch(self) -> Any:
        value, self._pending = self._pending, None
        return value

    def dispatch(self, name: str, n: int, *args: Any) -> str | bool | None:
        try:
            if name not in self._grants:
                return f"{name}: verb not granted"
            call_args = list(args[:n])
            for value in call_args:
                problem = _argument_problem(value)
                if problem:
                    return f"{name}: {problem}"
            try:
                result = self._call_verb(name, call_args)
            except VerbReplyError as exc:
                return str(exc)
            if not isinstance(result, _JSON_RESULT):
                return f"{name}: the verb returned a value that is not JSON"
            try:
                self._pending = self._build(result)
            except lua.LuaMemoryError:
                self._pending = None
                return True
            self._sample()
            return None
        except MemoryError:
            # Raised into Lua it would be a host object in the script's pcall; stop the script
            # as a memory trip and let run() re-raise.
            self._pending = None
            self.out_of_memory = True
            return True
        except Exception as exc:  # noqa: BLE001 -- the dispatcher never raises into Lua
            self._pending = None
            return f"{name}: worker error ({type(exc).__name__})"


# --- run ----------------------------------------------------------------------------------------------


def run(
    source: bytes,
    grants: frozenset[str],
    args: Mapping[str, JSONValue],
    limits: _LimitFields,
    call_verb: Callable[[str, list[Any]], JSONValue],
) -> tuple[JSONValue, Stats]:
    """Run *source* once; return its value and stats, or raise ``ScriptError`` / ``ScriptLimitError``.

    *args* reaches the script as one table through the main chunk's vararg
    (``local args = ...``), built through the protected table build. *call_verb*
    blocks and returns the verb's decoded JSON, or raises ``VerbReplyError`` for an
    ``ok: false`` reply; the script sees that as a Lua error string.
    """
    started = time.monotonic()
    names = _validated_grants(grants)
    rt = lua.LuaRuntime(
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=False,
        attribute_filter=_refuse,
        max_memory=limits.memory,
    )
    rt.set_overflow_handler(float)
    builder = LuaTableBuilder(rt)
    peak = 0
    lua_stats: Callable[[], tuple[int, float]] | None = None
    lua_status: Callable[[], str | None] | None = None

    def sample() -> None:
        nonlocal peak
        used = rt.get_memory_used() or 0
        if used > peak:
            peak = used

    def stats() -> Stats:
        sample()
        count, lua_peak = 0, 0.0
        if lua_stats is not None:
            try:
                count, lua_peak = lua_stats()
            except lua.LuaError:
                pass
        return {"peak_lua_bytes": max(peak, int(lua_peak)), "instructions": int(count)}

    dispatcher = _Dispatcher(frozenset(names), call_verb, builder.build, sample)
    limit_values = {
        "instructions": limits.instructions,
        "memory": limits.memory,
        "wall_time": limits.wall_time,
        "stack": STACK_LIMIT,
        "result": limits.result,
        "depth": limits.depth,
    }

    def limit_error(kind: str) -> ScriptLimitError:
        name = "result" if kind == "depth" else kind
        return ScriptLimitError(name, limit_values[kind], stats=stats())

    try:
        boot = rt.execute(_BOOT)
        compile_, run_main, lua_stats, lua_status = boot(
            dispatcher.dispatch,
            dispatcher.fetch,
            time.monotonic,
            started,
            rt.table_from(names),
            limits.instructions,
            float(limits.wall_time),
            HOOK_EVERY,
            limits.depth,
            limits.result,
        )
        compiled = compile_(source)
        if isinstance(compiled, tuple):
            raise ScriptError("lua", str(compiled[1]), stats=stats())
        status, payload = run_main(compiled, builder.build(dict(args)))
    except lua.LuaMemoryError:
        if dispatcher.out_of_memory:
            raise MemoryError from None
        raise limit_error("memory") from None
    except lua.LuaError as exc:
        # A trip that landed on the host's own instruction, outside the runner's pcall.
        kind = None
        if lua_status is not None:
            try:
                kind = lua_status()
            except lua.LuaError:
                kind = None
        if dispatcher.out_of_memory:
            raise MemoryError from None
        if kind:
            raise limit_error(kind) from None
        detail = str(exc).split("\nstack traceback:", 1)[0]
        raise ScriptError("lua", detail, stats=stats()) from None

    if dispatcher.out_of_memory:
        raise MemoryError
    if status == "ok":
        return json.loads(payload), stats()
    if status == "limit":
        raise limit_error(payload)
    raise ScriptError(status, payload, stats=stats())  # "lua" or "result"
