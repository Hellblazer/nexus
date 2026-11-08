# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5), S1: the runtime's escape, limit, result and verb cases through the real worker.

``test_runtime_escape/limits/results/verbs`` drive ``runtime.run`` in process
with the static check bypassed. These run the same classes through
``python -I -m nexus.lua_runtime.worker`` (``_frame_driver.run_worker``): the
start frame, the worker's own static check, ``runtime.run``, the frame
protocol for verb calls, and the one final frame. Each case asserts the final
frame's tag and its kind or limit exactly.

What changes on this path, and the tests say so:

* The static check runs first, in the worker. A script that names ``load``,
  ``os``, ``_G`` or ``.dump`` is refused as ``static_check`` before the sandbox
  builds, so the sandbox's own refusal of those names is the in-process suite's
  to show. Here the check's refusal is asserted by rule, and the layer-2 cases
  (the ones the check passes on purpose, ``ok_layer2_dynamic_dump``) are
  asserted to reach the sandbox and find nothing.
* A script is JSON text in the start frame, so a binary chunk reaches the worker
  only as text that starts with ESC; the check's ``binary`` rule refuses it.
* A verb error is a ``reply`` frame with ``ok: false``; the script sees its
  message as a plain string.

Nothing here is platform-specific: the OS limits (``RLIMIT_CPU``, ``RLIMIT_AS``,
the Windows job) are not the subject, and none of these cases comes near them.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
from typing import Any

import lupa.lua54 as lua
import pytest

from nexus.lua_runtime import frames
from nexus.lua_runtime.limits import Limits
from nexus.lua_runtime.static_check import RULES, VERBS_V1
from tests.lua_runtime import _support
from tests.lua_runtime._frame_driver import (
    ProtocolViolation,
    WorkerDied,
    WorkerHung,
    WorkerRun,
    run_worker,
)
from tests.lua_runtime._support import limits, verb, verb_table

MIB = 1024 * 1024
#: The count hook runs every 10,000 instructions (runtime.HOOK_EVERY; test_runtime_limits pins it).
HOOK_EVERY = 10_000
#: A small instruction budget for a loop that never ends: the limit stops it in well under a second.
SMALL = 1_000_000


def _run(source: str, *grants: str, **kw: Any) -> WorkerRun:
    """Run through the worker; the worker must have exited 0 with nothing on stderr."""
    run = run_worker(source, *grants, **kw)
    assert run.exit_code == 0, (run.exit_code, run.stderr)
    assert run.stderr == "", run.stderr
    return run


def _result(source: str, *grants: str, **kw: Any) -> Any:
    run = _run(source, *grants, **kw)
    assert run.kind == "result", dict(run.frame)
    return run.value


def _error(kind: str, source: str, *grants: str, **kw: Any) -> WorkerRun:
    run = _run(source, *grants, **kw)
    assert run.kind == "error", dict(run.frame)
    assert run.error_kind == kind, dict(run.frame)
    return run


def _limit(name: str, source: str, *grants: str, **kw: Any) -> WorkerRun:
    run = _run(source, *grants, **kw)
    assert run.kind == "limit", dict(run.frame)
    assert run.limit == name, dict(run.frame)
    assert set(run.stats) == {"peak_lua_bytes", "instructions"}, dict(run.frame)
    return run


def _lenient(name: str, fn: Any) -> Any:
    """A fixture verb that takes up to four strings, all optional: the corpus scripts' calls are not its subject."""
    return verb(name, fn, "string", "string", "string", "string", optional_from=0)


# ---- the driver itself: it reaches the real worker, and it fails instead of hanging -----------------------


def test_the_driver_runs_the_real_worker_module() -> None:
    run = _run("return 40 + 2")
    assert run.value == 42
    assert run.argv[1:4] == ("-I", "-m", "nexus.lua_runtime.worker")
    assert run.argv[0] == sys.executable
    assert run.pid != os.getpid()
    # only the worker's runtime makes these; a driver that computed the answer itself would not
    assert set(run.stats) == {"peak_lua_bytes", "instructions"}
    assert run.stats["peak_lua_bytes"] > 0


def test_the_worker_runs_the_static_check_before_the_script() -> None:
    run = _error("static_check", "x = 1 return x")
    assert run.detail == "global-write: x (line 1)"
    assert run.stats == {}  # the runtime was never built


def test_a_worker_that_never_speaks_is_killed_by_the_watchdog() -> None:
    started = time.monotonic()
    with pytest.raises(WorkerHung):
        run_worker(
            "return 1",
            argv=(sys.executable, "-I", "-c", "import time; time.sleep(120)"),
            timeout=1.0,
        )
    assert time.monotonic() - started < 15


def test_a_worker_that_hangs_after_the_start_frame_is_killed_by_the_watchdog() -> None:
    argv = _support.ready_then("time.sleep(120)")
    started = time.monotonic()
    with pytest.raises(WorkerHung):
        run_worker("return 1", argv=argv, timeout=1.0)
    assert time.monotonic() - started < 15


def test_a_worker_that_dies_before_its_final_frame_is_reported_with_its_status() -> (
    None
):
    argv = _support.ready_then("os._exit(7)")
    with pytest.raises(WorkerDied, match="worker exit 7"):
        run_worker("return 1", argv=argv)


def test_a_worker_that_opens_with_the_wrong_frame_is_a_protocol_violation() -> None:
    argv = _support.fake_worker('send({"t": "result", "value": 1, "stats": {}})')
    with pytest.raises(ProtocolViolation):
        run_worker("return 1", argv=argv)


# ---- escape: what the static check refuses, by rule ----------------------------------------------------------

#: Names the check refuses as a global (rule ``global``): Key Discoveries and the probes of test_runtime_escape.
REFUSED_GLOBALS = (
    "io os require load loadstring dofile loadfile debug package python "
    "getmetatable setmetatable getfenv setfenv ffi jit _G rawget rawset "
    "collectgarbage coroutine xpcall print newproxy module string_dump"
).split()


@pytest.mark.parametrize("name", REFUSED_GLOBALS)
def test_a_dangerous_global_is_refused_by_the_static_check_in_the_worker(
    name: str,
) -> None:
    run = _error("static_check", f"return {name} == nil")
    assert run.detail == f"global: {name} (line 1)"
    assert run.stats == {}


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ('return ("").dump == nil', "field-dump"),
        ("return string.dump == nil", "field-dump"),
        ("return string == nil", "ns-value"),
        ("local e = _ENV return e.load == nil", "env"),
        ("return coroutine.wrap(function() while true do end end)()", "global"),
        ("x = 1", "global-write"),
        ("string.planted = 2 return 1", "ns-value"),
        ("return function(", "parse"),
    ],
)
def test_other_escape_shapes_are_refused_by_rule(source: str, rule: str) -> None:
    run = _error("static_check", source)
    assert run.detail.startswith(f"{rule}:"), run.detail


def test_a_binary_chunk_is_refused_before_it_runs() -> None:
    """The text of a real bytecode chunk, as the start frame can carry it: it starts with ESC (``\\x1b``)."""
    rt = lua.LuaRuntime(register_eval=False, encoding=None)
    chunk = bytes(rt.eval("string.dump(function() return 7 end)"))
    assert chunk[:1] == b"\x1b"
    run = _error("static_check", chunk.decode("latin-1"))
    assert run.detail == "binary: binary chunk"
    assert _error("static_check", "\x1bLua").detail == "binary: binary chunk"


# ---- escape: the layer-2 cases the check passes on purpose, which the sandbox answers -------------------------


@pytest.mark.parametrize(
    "source",
    [
        'local s, k = "", "du" .. "mp" return s[k] == nil',  # corpus ok_layer2_dynamic_dump
        'local s = "x" local k = "du" .. "mp" return s[k] == nil',
        'return ("").__index == nil',
        'return (""):len() == 0 and ("abc"):upper() == "ABC"',
        'local k = "lo" .. "ad" local t = {} return t[k] == nil',
    ],
)
def test_string_dump_is_gone_and_strings_still_work_in_the_worker(source: str) -> None:
    assert _result(source) is True


def test_calling_dump_through_a_computed_key_is_a_lua_error() -> None:
    run = _error("lua", 'local s = "" local k = "du" .. "mp" return s[k](s)')
    assert "attempt to call a nil value" in run.detail
    assert set(run.stats) == {"peak_lua_bytes", "instructions"}


def test_the_reduced_string_library_is_the_one_strings_index() -> None:
    """Each of the 16 members string keeps is what a string value finds by method lookup (the check forbids a
    computed key on ``string``, so the names are spelled out)."""
    names = (
        "byte char find format gmatch gsub len lower match pack packsize rep reverse sub unpack upper"
    ).split()
    assert len(names) == 16
    source = "return {" + ", ".join(f'("").{n} == string.{n}' for n in names) + "}"
    assert _result(source) == [True] * 16


# ---- escape: the static-check corpus, through the worker ------------------------------------------------------

CORPUS = pathlib.Path(__file__).parents[1] / "fixtures" / "lua_static_check"


def _corpus_case(path: pathlib.Path) -> tuple[str, tuple[str, ...], str]:
    text = path.read_text(encoding="utf-8")
    grants: tuple[str, ...] = ()
    expect = "ok"
    for line in text.splitlines()[:3]:
        if line.startswith("-- grants:"):
            grants = tuple(
                g.strip() for g in line[len("-- grants:") :].split(",") if g.strip()
            )
        elif line.startswith("-- expect:"):
            expect = line[len("-- expect:") :].strip()
    return text, grants, expect


CASES = sorted(CORPUS.glob("*.lua"))

#: The seven version 1 verbs, each answering a fixed value, so a corpus script that calls one runs to its end.
_V1 = verb_table(
    *(
        _lenient(n, lambda *a, deadline, ctx: {"content": "real"})
        for n in sorted(VERBS_V1)
    )
)


def test_the_corpus_is_not_vacuous() -> None:
    expects = [_corpus_case(p)[2] for p in CASES]
    assert len(CASES) >= 57
    assert expects.count("ok") >= 8
    assert set(expects) - {"ok"} >= RULES - {
        "binary",
        "size",
    }  # every rule the corpus can show is shown


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_every_corpus_script_gets_its_static_check_verdict_from_the_worker(
    path: pathlib.Path,
) -> None:
    source, grants, expect = _corpus_case(path)
    run = _run(source, *grants, verbs=_V1)
    if expect == "ok":
        # the check let it through: it ran, and ended as a result or as its own Lua error, never as a refusal
        assert not (run.kind == "error" and run.frame["kind"] == "static_check"), dict(
            run.frame
        )
        assert run.kind in ("result", "error"), dict(run.frame)
        assert run.stats, dict(run.frame)  # the runtime was built
    else:
        assert run.kind == "error" and run.error_kind == "static_check", dict(run.frame)
        assert run.detail.startswith(f"{expect}:"), run.detail
        assert run.stats == {}
        assert run.calls == ()  # the script never ran, so it called nothing


def test_the_ok_corpus_scripts_return_what_they_compute() -> None:
    pure = _result(_corpus_case(CORPUS / "ok_pure.lua")[0])
    assert pure["s"] == "1,1,2,3,5,8,13,21,34,55" and pure["n"] == 3
    methods = _result(_corpus_case(CORPUS / "ok_methods.lua")[0])
    assert methods[:3] == ["ABC", "xxx", "abc 3"]
    assert _result(_corpus_case(CORPUS / "ok_layer2_dynamic_dump.lua")[0]) is not None


# ---- escape: a verb error is a string, never a Python exception ------------------------------------------------


def _boom(*a: Any, deadline: float, ctx: Any) -> None:
    raise ValueError("backend down")


def test_a_verb_error_reaches_the_script_as_a_plain_string() -> None:
    table = verb_table(verb("memory.get", _boom, "string", "string"))
    value = _result(
        """
        local ok, e = pcall(memory.get, "p", "t")
        return {ok, type(e), e, e.args == nil, (pcall(e, 1))}
        """,
        "memory.get",
        verbs=table,
    )
    assert value == [False, "string", "backend down", True, False]


def test_an_unguarded_verb_error_ends_the_run_as_a_lua_error() -> None:
    table = verb_table(verb("memory.get", _boom, "string", "string"))
    run = _error("lua", 'return memory.get("p", "t")', "memory.get", verbs=table)
    assert run.detail == "backend down"
    assert run.calls == (("memory.get", ["p", "t"]),)


def test_a_bad_argument_is_the_parents_refusal_as_a_string() -> None:
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: 1, "string", "string")
    )
    value = _result(
        'local ok, e = pcall(memory.get, 1, "t") return {ok, type(e), e}',
        "memory.get",
        verbs=table,
    )
    assert value == [False, "string", "memory.get: p0 must be a string, got int"]
    assert _result(
        'local ok, e = pcall(memory.get, "only") return {ok, e}',
        "memory.get",
        verbs=table,
    ) == [
        False,
        "memory.get takes 2 argument(s) (2 required), got 1",
    ]


def test_a_verb_result_that_is_not_json_is_a_string_error() -> None:
    table = verb_table(
        verb(
            "memory.get",
            lambda *a, deadline, ctx: {"nested": {1, 2}},
            "string",
            "string",
        )
    )
    value = _result(
        "local ok, e = pcall(memory.get, 'p', 't') return {ok, type(e)}",
        "memory.get",
        verbs=table,
    )
    assert value == [False, "string"]


# ---- limits: instructions ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "while true do end",
        "local x = 0 for i = 1, 1e12 do x = x + i end return x",
        "local function f(a) return a + 1 end local x = 0 while true do x = f(x) end",
        "local t = {} while true do t[#t % 100 + 1] = #t end",
    ],
    ids=["while", "for", "hot_call", "table_write"],
)
def test_a_runaway_loop_stops_on_the_instruction_limit(source: str) -> None:
    run = _limit("instructions", source, limits=limits(20.0, instructions=SMALL))
    assert run.limit_value == SMALL
    # counted in steps of the hook interval: a little past the limit, never far
    assert SMALL < run.stats["instructions"] <= SMALL + 2 * HOOK_EVERY


def test_the_default_instruction_limit_is_fifty_million() -> None:
    run = _limit("instructions", "while true do end", limits=limits(60.0))
    assert run.limit_value == 50_000_000


def test_a_script_under_the_limit_counts_its_instructions() -> None:
    run = _run(
        "for i = 1, 1000000 do end return 1",
        limits=limits(20.0, instructions=5_000_000),
    )
    assert run.kind == "result" and run.value == 1
    assert 980_000 <= run.stats["instructions"] <= 1_020_000


@pytest.mark.parametrize(
    "source",
    [
        "while true do pcall(function() while true do end end) end",
        "while true do pcall(error, 'x') end",
        "local n = 0 while true do pcall(function() n = n + 1 while true do end end) end",
        "while true do pcall(function() local t = {} while true do t[1] = 1 end end) end",
    ],
    ids=["swallow", "tight_pcall", "swallow_counting", "swallow_table"],
)
def test_a_loop_that_swallows_the_limit_with_pcall_still_stops_on_instructions(
    source: str,
) -> None:
    run = _limit("instructions", source, limits=limits(20.0, instructions=SMALL))
    assert run.limit_value == SMALL


def test_a_trip_inside_a_verb_call_loop_is_a_limit_not_a_verb_error() -> None:
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: 1, "string", "string")
    )
    run = _limit(
        "instructions",
        "local n = 0 while true do n = memory.get('p', 't') end",
        "memory.get",
        verbs=table,
        limits=limits(20.0, instructions=200_000),
    )
    assert run.limit_value == 200_000
    assert len(run.calls) > 100


# ---- limits: the guarded pcall and the C stack ---------------------------------------------------------------------


def test_pcall_recursion_to_the_c_stack_limit_ends_as_a_stack_overflow() -> None:
    """At ~198 nested pcalls the count hook cannot run: only the guarded pcall stops this, and the driver's watchdog
    would fail the test if it did not."""
    started = time.monotonic()
    run = _limit(
        "stack",
        "local function g() while true do pcall(g) end end g()",
        limits=limits(20.0),
        timeout=60,
    )
    assert time.monotonic() - started < 30
    assert run.limit_value == 1_000_000


def test_a_deeper_pcall_tree_to_the_c_stack_limit_also_stops() -> None:
    _limit(
        "stack",
        "local function g(d) local ok = pcall(g, d + 1) local ok2 = pcall(g, d + 1) end g(1)",
        limits=limits(20.0),
        timeout=60,
    )


def test_the_guarded_pcall_still_catches_an_ordinary_error() -> None:
    value = _result(
        """
        local a, b = pcall(error, "boom")
        local c, d = pcall(function() local x = nil return x.y end)
        local g, h, i, j = pcall(function(...) return ... end, 1, 2, 3)
        return {a, b, c, d, g, h, i, j}
        """
    )
    assert value[:2] == [False, "boom"]
    assert value[2] is False and "attempt to index a nil value" in value[3]
    assert value[4:] == [True, 1, 2, 3]


# ---- limits: memory ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "return #string.rep('a', 1000000000)",
        "local t = {} for i = 1, 1e9 do t[i] = i end return #t",
        "local s = 'x' while true do s = s .. s end",
        "local t = {} for i = 1, 1e9 do t[i] = {i} end return #t",
    ],
    ids=["rep", "table", "double", "nested_tables"],
)
def test_memory_growth_stops_on_the_memory_limit(source: str) -> None:
    run = _limit("memory", source, limits=limits(20.0, memory=16 * MIB))
    assert run.limit_value == 16 * MIB


def test_a_script_cannot_swallow_the_memory_limit_with_pcall() -> None:
    _limit(
        "memory",
        "while true do pcall(string.rep, 'x', 1000000000) end",
        limits=limits(20.0, memory=16 * MIB),
    )


def test_a_memory_limit_trips_inside_a_pcall_around_a_table_build() -> None:
    _limit(
        "memory",
        "local ok = pcall(function() local t = {} for i = 1, 1e9 do t[i] = i end end) return ok",
        limits=limits(20.0, memory=16 * MIB),
    )


def test_a_verb_result_that_does_not_fit_is_the_memory_limit_not_an_abort() -> None:
    """The worst shape (a list of empty tables, ~27 Lua bytes per JSON byte) read from a reply frame."""
    worst = [{} for _ in range(200_000)]
    table = verb_table(verb("memory.list", lambda *a, deadline, ctx: worst, "string"))
    run = _limit(
        "memory",
        "local r = memory.list('p') return #r",
        "memory.list",
        verbs=table,
        limits=limits(20.0, memory=8 * MIB),
    )
    assert run.limit_value == 8 * MIB
    assert run.calls == (("memory.list", ["p"]),)


def test_a_script_cannot_survive_a_verb_result_that_overran_memory() -> None:
    worst = [{} for _ in range(200_000)]
    table = verb_table(verb("memory.list", lambda *a, deadline, ctx: worst, "string"))
    run = _limit(
        "memory",
        "local ok = pcall(memory.list, 'p') while true do pcall(memory.list, 'p') end",
        "memory.list",
        verbs=table,
        limits=limits(20.0, memory=8 * MIB),
    )
    assert len(run.calls) == 1  # the first overrun ended the run: no second call


def test_args_too_big_for_the_memory_limit_are_the_memory_limit_not_an_abort() -> None:
    rows = [{} for _ in range(200_000)]
    run = _limit(
        "memory",
        "local a = ... return #a.rows",
        args={"rows": rows},
        limits=limits(20.0, memory=8 * MIB),
    )
    assert run.limit_value == 8 * MIB


# ---- limits: stack ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "local function r(n) return r(n + 1) + 1 end return r(1)",
        "local function r(t) return {r(t)} end return r({})",
        "local function r(a, b, c, d, e, f) local x, y, z = 1, 2, 3 return 1 + r(a, b, c, d, e, f) end return r(1, 2, 3, 4, 5, 6)",
    ],
    ids=["arg", "table", "wide_frame"],
)
def test_unbounded_recursion_fails_with_a_stack_overflow(source: str) -> None:
    run = _limit("stack", source)
    assert run.limit_value == 1_000_000  # LUAI_MAXSTACK


def test_the_smallest_frame_recursion_ends_on_the_stack_or_the_memory_limit() -> None:
    """``return 1 + r()`` has the smallest frames: under the default 64 MiB its call records fill memory just before
    the stack reaches 1,000,000 slots (the in-process suite says the same), so either limit ends it."""
    run = _run("local function r() return 1 + r() end return r()")
    assert run.kind == "limit" and run.limit in ("stack", "memory"), dict(run.frame)


def test_a_tail_call_loop_is_the_instruction_limit_not_a_stack_overflow() -> None:
    _limit(
        "instructions",
        "local function r(n) return r(n + 1) end return r(1)",
        limits=limits(20.0, instructions=SMALL),
    )


# ---- limits: wall time, the inner clock -----------------------------------------------------------------------------------

#: A loop whose every turn is a C call, so 50 million instructions take far longer than the wall limits below.
_HEAVY = "local i = 0 while true do i = i + 1 local s = string.rep('x', 100) end"


def test_the_hook_checks_the_wall_clock_from_the_start_of_the_run() -> None:
    started = time.monotonic()
    run = _limit("wall_time", _HEAVY, limits=limits(0.2))
    assert run.limit_value == 0.2
    assert time.monotonic() - started < 10


def test_a_script_that_swallows_the_wall_time_trip_still_stops() -> None:
    run = _limit(
        "wall_time",
        f"while true do pcall(function() {_HEAVY} end) end",
        limits=limits(0.2),
    )
    assert run.limit_value == 0.2


# ---- limits: result -----------------------------------------------------------------------------------------------------------


def test_a_two_megabyte_result_stops_on_the_result_cap() -> None:
    run = _limit("result", "return string.rep('x', 2 * 1024 * 1024)")
    assert run.limit_value == 1 * MIB


def test_forty_deep_nesting_stops_on_the_depth_limit() -> None:
    run = _limit("result", "local t = {} for i = 1, 40 do t = {t} end return t")
    assert run.limit_value == 32  # the depth that was in force


def test_depth_32_is_allowed_and_33_is_not() -> None:
    nested = "local t = 1 for i = 1, {n} do t = {{t}} end return t"
    value = _result(nested.format(n=32))
    depth = 0
    while isinstance(value, list):
        value, depth = value[0], depth + 1
    assert depth == 32
    assert _limit("result", nested.format(n=33)).limit_value == 32


def test_the_depth_is_the_callers_lowered_limit() -> None:
    lim = Limits().lowered(wall_time=20.0, depth=3)
    assert _result("return {{{1}}}", limits=lim) == [[[1]]]
    assert _limit("result", "return {{{{1}}}}", limits=lim).limit_value == 3


def test_a_result_of_many_small_pieces_stops_on_the_byte_cap_with_memory_to_spare() -> (
    None
):
    run = _limit(
        "result", "local t = {} for i = 1, 600000 do t[i] = i % 10 end return t"
    )
    assert run.limit_value == 1 * MIB


def test_a_shared_subtable_cannot_blow_up_the_walk() -> None:
    """2^25 paths through a 25-deep DAG: the walk ends at the byte cap, not after 33 million nodes."""
    started = time.monotonic()
    _limit("result", "local a = {1} for i = 1, 25 do a = {a, a} end return a")
    assert time.monotonic() - started < 10


def test_the_byte_cap_is_on_the_encoded_json_and_exact() -> None:
    lim = limits(20.0, result=1002)
    value = _result("return string.rep('x', 1000)", limits=lim)
    assert len(value) == 1000  # 1000 + 2 quotes
    assert (
        _limit("result", "return string.rep('x', 1001)", limits=lim).limit_value == 1002
    )


# ---- the final frame: what comes back, and the error shapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("return", None),
        ("return true", True),
        ("return -7", -7),
        ("return math.maxinteger", 2**63 - 1),
        ("return math.mininteger", -(2**63)),
        ("return 0.5", 0.5),
        ("return 1/3", 1 / 3),
        ("return 6 / 2", 3.0),
        ("return 2^53", 2.0**53),
        ("return 'caf\\u{e9} \\u{1F600}'", "café \U0001f600"),
        ("return 'a\"b\\\\c\\n\\t\\0\\1\\127'", 'a"b\\c\n\t\x00\x01\x7f'),
        ("return 1, 2, 3", 1),
        ("return {}", {}),
        ("return {1, 2, 3}", [1, 2, 3]),
        ("return {x = 1, y = {z = true}}", {"x": 1, "y": {"z": True}}),
        ("return {[2] = 'b', [1] = 'a', [3] = 'c'}", ["a", "b", "c"]),
    ],
)
def test_the_final_frame_carries_the_lua_value_as_json(
    source: str, expected: Any
) -> None:
    value = _result(source)
    assert value == expected
    assert type(value) is type(expected)


def test_a_float_with_an_integer_value_stays_a_float() -> None:
    assert [type(v) for v in _result("return {6 / 2, 7 // 2, 2^2}")] == [
        float,
        int,
        float,
    ]


def test_a_900_kilobyte_result_returns_whole() -> None:
    assert len(_result("return string.rep('x', 900 * 1024)")) == 900 * 1024


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("return function() end", "function"),
        ("return {1, 2, x = 3}", "mixed"),
        ("return {[1] = 'a', [3] = 'c'}", "hole"),
        ("return 0/0", "NaN"),
        ("return math.huge", "infinity"),
        ("return '\\xff'", "UTF-8"),
        ("local t = {} t.self = t return t", "cyclic"),
    ],
)
def test_a_result_the_walk_rejects_is_a_result_error_naming_it(
    source: str, fragment: str
) -> None:
    run = _error("result", source)
    assert fragment in run.detail
    assert set(run.stats) == {"peak_lua_bytes", "instructions"}


def test_a_lua_error_carries_its_message_and_position() -> None:
    run = _error("lua", "local x = 1\nerror('boom')")
    assert run.detail == "script:2: boom"
    assert set(run.stats) == {"peak_lua_bytes", "instructions"}


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        ("error('plain', 0)", "plain"),
        ("error(42)", "42"),
        ("error({code = 1})", "(error object is a table value)"),
        (
            "local x = nil return x.y",
            "script:1: attempt to index a nil value (local 'x')",
        ),
        ("return 1 + {}", "script:1: attempt to perform arithmetic on a table value"),
        ("assert(false, 'why')", "script:1: why"),
    ],
)
def test_a_lua_error_message_is_the_one_a_lua_user_expects(
    source: str, detail: str
) -> None:
    assert _error("lua", source).detail == detail


def test_an_error_message_with_utf8_arrives_unchanged() -> None:
    assert (
        _error("lua", "error('caf\\u{e9} \\u{1F600}', 0)").detail == "café \U0001f600"
    )


def test_an_error_message_with_bytes_that_are_not_utf8_still_arrives() -> None:
    detail = _error("lua", "error('bad \\xff\\xfe bytes', 0)").detail
    assert detail.startswith("bad ")
    detail.encode("utf-8")


def test_a_long_error_message_is_cut_on_a_character_boundary() -> None:
    detail = _error("lua", "error(string.rep('\\u{e9}', 100000), 0)").detail
    assert 1000 < len(detail.encode("utf-8")) < 8192
    assert detail.endswith("...")
    assert set(detail[:-3]) == {"é"}


# ---- args ----------------------------------------------------------------------------------------------------------------------


def test_args_reach_the_script_as_the_main_chunks_vararg() -> None:
    value = _result(
        "local args = ... return {select('#', ...), args.rdr, args.n, args.flag, args.nested.list[2], args.missing == nil}",
        args={"rdr": "RDR-242", "n": 3, "flag": True, "nested": {"list": [10, 20]}},
    )
    assert value == [1, "RDR-242", 3, True, 20, True]


def test_no_args_is_an_empty_table_not_nil() -> None:
    assert _result("local args = ... return {type(args), next(args) == nil}") == [
        "table",
        True,
    ]


# ---- verbs: the round trip through call and reply frames ---------------------------------------------------------------------


def test_a_granted_verb_is_called_with_its_arguments_and_answers_a_structured_result() -> (
    None
):
    result = {
        "title": "t",
        "n": 3,
        "ratio": 0.25,
        "flag": True,
        "tags": ["a", "b"],
        "rows": [{"id": 1}, {"id": 2}],
        "empty": [],
    }
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: result, "string", "string")
    )
    run = _run(
        """
        local r = memory.get('proj', 'title')
        return {r.title, r.n, r.ratio, r.flag, #r.tags, r.rows[2].id, #r.empty, r.missing == nil}
        """,
        "memory.get",
        verbs=table,
    )
    assert run.kind == "result"
    assert run.value == ["t", 3, 0.25, True, 2, 2, 0, True]
    assert run.calls == (("memory.get", ["proj", "title"]),)


def test_argument_types_cross_as_json_values() -> None:
    seen: list[tuple[Any, ...]] = []

    def get(*a: Any, deadline: float, ctx: Any) -> None:
        seen.append(a)

    table = verb_table(
        verb(
            "memory.get", get, "string", "integer", "number", "boolean", optional_from=3
        )
    )
    run = _run(
        "memory.get('s', 7, 2.5, true) memory.get('caf\\u{e9}\\0nul', 10 / 2, 1)",
        "memory.get",
        verbs=table,
    )
    assert run.kind == "result"
    assert run.calls == (
        ("memory.get", ["s", 7, 2.5, True]),
        ("memory.get", ["café\x00nul", 5.0, 1]),
    )
    # the parent's check_args hands the verb normalized values: a Lua 5.0 for an integer parameter is the int 5
    assert seen == [("s", 7, 2.5, True), ("café\x00nul", 5, 1, None)]
    assert type(seen[1][1]) is int


def test_the_verb_gets_a_deadline_and_a_context() -> None:
    def get(*a: Any, deadline: float, ctx: Any) -> bool:
        return deadline > time.monotonic() and ctx.repo_root is None

    table = verb_table(verb("memory.get", get, "string", "string"))
    assert _result("return memory.get('p', 't')", "memory.get", verbs=table) is True


@pytest.mark.parametrize("scalar", ["text", 5, 2.5, True, False])
def test_a_scalar_verb_result_is_the_scalar(scalar: Any) -> None:
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: scalar, "string", "string")
    )
    value = _result("return memory.get('p', 't')", "memory.get", verbs=table)
    assert value == scalar and type(value) is type(scalar)


def test_a_none_verb_result_is_nil() -> None:
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: None, "string", "string")
    )
    value = _result(
        "local r = memory.get('p', 't') return {r == nil, type(r)}",
        "memory.get",
        verbs=table,
    )
    assert value == [True, "nil"]


def test_a_verb_result_that_fits_is_built_and_read() -> None:
    rows = [{"n": i} for i in range(50_000)]
    table = verb_table(verb("memory.list", lambda *a, deadline, ctx: rows, "string"))
    run = _run(
        "local r = memory.list('p') return {#r, r[1].n, r[#r].n}",
        "memory.list",
        verbs=table,
    )
    assert run.value == [50_000, 0, 49_999]
    assert run.stats["peak_lua_bytes"] > 1 * MIB


def test_many_calls_are_one_round_trip_each() -> None:
    n = 0

    def get(*a: Any, deadline: float, ctx: Any) -> int:
        nonlocal n
        n += 1
        return n

    table = verb_table(verb("memory.get", get, "string", "string"))
    run = _run(
        "local s = 0 for i = 1, 500 do s = s + memory.get('p', 't') end return s",
        "memory.get",
        verbs=table,
    )
    assert run.value == sum(range(1, 501))
    assert len(run.calls) == 500 == n


def test_several_verbs_in_one_namespace_and_across_namespaces() -> None:
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: "g", "string", "string"),
        verb("memory.list", lambda *a, deadline, ctx: "l", "string"),
        verb("git.diff_names", lambda *a, deadline, ctx: "d", "string"),
    )
    run = _run(
        "return {memory.get('p', 't'), memory.list('p'), git.diff_names('x')}",
        "memory.get",
        "memory.list",
        "git.diff_names",
        verbs=table,
    )
    assert run.value == ["g", "l", "d"]
    assert [c[0] for c in run.calls] == ["memory.get", "memory.list", "git.diff_names"]


def test_an_ungranted_verb_in_the_table_is_refused_by_the_check_and_never_called() -> (
    None
):
    table = verb_table(
        verb("memory.get", lambda *a, deadline, ctx: "g", "string", "string"),
        verb("memory.list", lambda *a, deadline, ctx: "l", "string"),
    )
    run = _error("static_check", "return memory.list('p')", "memory.get", verbs=table)
    assert run.detail.startswith("verb-ungranted:")
    assert run.calls == ()


def test_a_script_cannot_name_a_verb_the_table_does_not_hold() -> None:
    run = _error(
        "static_check",
        "return memory.put('p', 't')",
        "memory.get",
        verbs=verb_table(
            verb("memory.get", lambda *a, deadline, ctx: "g", "string", "string")
        ),
    )
    assert run.detail.startswith("verb-unknown:")
