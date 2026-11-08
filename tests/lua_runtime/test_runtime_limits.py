# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5): the limit suite, in process.

Each test asserts the limit the error names, and that the error carries stats.
A limit that does not stop a script would show here as a ``wall_time`` error
(each test sets a small wall time as its own backstop) or as a hang; the
assertion on the limit's name is what tells the two apart.

What this suite cannot test in process, and the worker suite owns: a pattern
match that the count hook cannot interrupt (A2, ``nexus_rdr/242-research-29``)
is ended by the parent's kill timer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time

import pytest

from nexus.lua_runtime import runtime
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError
from nexus.lua_runtime.frames import VerbReplyError
from tests.lua_runtime._runtime_support import DEFAULT, Limits, run_lua

#: A small budget for tests that loop forever: the instruction limit ends them in well under a second.
SMALL = DEFAULT.with_(instructions=1_000_000, wall_time=20.0)
MIB = 1024 * 1024


def _limit(source: str, *grants: str, limits: Limits = SMALL, **kw: object) -> ScriptLimitError:
    with pytest.raises(ScriptLimitError) as exc:
        run_lua(source, *grants, limits=limits, **kw)  # type: ignore[arg-type]
    return exc.value


def _assert_stats(err: ScriptLimitError) -> None:
    assert err.stats is not None
    assert set(err.stats) == {"peak_lua_bytes", "instructions"}
    assert err.stats["peak_lua_bytes"] > 0


# ---- instructions ------------------------------------------------------------------------


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
    err = _limit(source)
    assert err.limit == "instructions"
    assert err.value == 1_000_000
    _assert_stats(err)
    # the count is in steps of the hook interval: a little past the limit, never far
    assert 1_000_000 < err.stats["instructions"] <= 1_000_000 + 2 * runtime.HOOK_EVERY  # type: ignore[index]


def test_the_default_instruction_limit_is_fifty_million() -> None:
    err = _limit("while true do end", limits=DEFAULT.with_(wall_time=60.0))
    assert err.limit == "instructions"
    assert err.value == 50_000_000


def test_a_script_under_the_limit_counts_its_instructions() -> None:
    _, stats = run_lua("for i = 1, 1000000 do end return 1", limits=SMALL.with_(instructions=5_000_000))
    assert 980_000 <= stats["instructions"] <= 1_020_000
    assert stats["peak_lua_bytes"] > 0


def test_the_hook_runs_every_ten_thousand_instructions() -> None:
    assert runtime.HOOK_EVERY == 10_000


# ---- the two pcall escapes -------------------------------------------------------------------


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
def test_a_loop_that_swallows_the_limit_with_pcall_still_stops(source: str) -> None:
    err = _limit(source)
    assert err.limit == "instructions"
    _assert_stats(err)


_ISOLATED = textwrap.dedent(
    """
    import json, sys
    from dataclasses import dataclass
    from nexus.lua_runtime import runtime
    from nexus.lua_runtime.errors import ScriptError, ScriptLimitError

    @dataclass(frozen=True)
    class L:
        instructions: int = 1_000_000
        memory: int = 64 * 1024 * 1024
        wall_time: float = 20.0
        result: int = 1024 * 1024
        depth: int = 32

    patched = runtime._BOOT
    for old, new in json.loads(sys.argv[2]):
        assert old in patched, "patch site absent: " + repr(old)
        patched = patched.replace(old, new)
    runtime._BOOT = patched
    try:
        runtime.run(sys.argv[1].encode(), frozenset(), {}, L(), lambda n, a: None)
        out = {"ended": "returned"}
    except ScriptLimitError as exc:
        out = {"ended": "limit", "limit": exc.limit}
    except ScriptError as exc:
        out = {"ended": "error", "kind": exc.kind, "detail": exc.detail}
    print(json.dumps(out))
    """
)


def _run_isolated(
    source: str, timeout: float = 60.0, patches: tuple[tuple[str, str], ...] = ()
) -> dict[str, str]:
    """Run *source* in a fresh ``python -I`` process, so a guard that fails hangs a child, not the suite.

    A script at the C-stack limit is the one place the count hook cannot run, so in process nothing
    but the guarded ``pcall`` stops it; the parent's kill timer does that for the worker, and this
    helper does it for the test.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-c", _ISOLATED, source, json.dumps(patches)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"the script was still running after {timeout} s: a limit layer did not stop it")
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_pcall_recursion_to_the_c_stack_limit_stops_as_a_stack_overflow() -> None:
    """cstack_hook_probe.py: at ~198 nested pcalls the hook cannot run; the guard stops it."""
    started = time.monotonic()
    out = _run_isolated("local function g() while true do pcall(g) end end g()")
    assert out == {"ended": "limit", "limit": "stack"}
    assert time.monotonic() - started < 30


def test_a_deeper_pcall_tree_to_the_c_stack_limit_also_stops() -> None:
    out = _run_isolated("local function g(d) local ok = pcall(g, d + 1) local ok2 = pcall(g, d + 1) end g(1)")
    assert out == {"ended": "limit", "limit": "stack"}


def test_a_pcall_loop_that_dies_at_the_c_stack_limit_in_process_is_a_stack_limit_with_stats() -> None:
    """In process, with a script that reaches the C-stack limit once and returns (no loop to hang)."""
    err = _limit(
        "local function g(d) local ok, e = pcall(g, d + 1) return d end return g(1)",
        limits=SMALL,
    )
    assert err.limit == "stack"
    _assert_stats(err)


# Each layer alone. Two layers stop a script that swallows a tripped limit with pcall: the hook rearms
# to fire on every instruction once a limit trips (and raises in the script's code), and the guarded
# pcall re-raises a tripped limit. Either is enough; each is pinned with the other taken out of the
# boot chunk, in a child process because a layer that fails leaves a loop that never ends.

_NO_REARM = (('tripped = kind\n    sethook(hook, "", 1)\n', "tripped = kind\n"),)
_NO_GUARD = (("if tripped then error(e, 0) end\n    return false, ...", "return false, ..."),)
_NO_RAISE = (('if info and info.source == "=script" then error("script limit: " .. tripped, 0) end', ""),)

_SWALLOWERS = [
    "while true do pcall(function() while true do end end) end",
    "while true do pcall(function() pcall(function() while true do end end) end) end",
    "local n = 0 while true do local ok = pcall(function() n = n + 1 while true do end end) end",
]


@pytest.mark.parametrize("source", _SWALLOWERS)
def test_the_guarded_pcall_alone_stops_a_swallow_loop(source: str) -> None:
    out = _run_isolated(source, timeout=30.0, patches=_NO_REARM)
    assert out == {"ended": "limit", "limit": "instructions"}


@pytest.mark.parametrize("source", _SWALLOWERS)
def test_the_rearmed_hook_alone_stops_a_swallow_loop(source: str) -> None:
    out = _run_isolated(source, timeout=30.0, patches=_NO_GUARD)
    assert out == {"ended": "limit", "limit": "instructions"}


def test_the_rearmed_hooks_raise_is_what_stops_a_swallow_loop_without_the_guard() -> None:
    """Without the guard and without the hook's raise nothing is left, so the loop runs on (the child is killed)."""
    with pytest.raises(pytest.fail.Exception, match="still running"):
        _run_isolated(_SWALLOWERS[0], timeout=8.0, patches=_NO_GUARD + _NO_RAISE)


def test_a_layer_patch_that_matches_nothing_fails_rather_than_passing_vacuously() -> None:
    with pytest.raises(AssertionError):
        _run_isolated("return 1", patches=(("no such site", ""),))


def test_the_guarded_pcall_still_catches_an_ordinary_error() -> None:
    value, _ = run_lua(
        """
        local a, b = pcall(error, "boom")
        local c, d = pcall(function() local x = nil return x.y end)
        local e, f = pcall(error, {code = 7})
        local g, h, i, j = pcall(function(...) return ... end, 1, 2, 3)
        local k, l = pcall(nil)
        local m = select("#", pcall(function() end))
        return {a, b, c, d, e, type(f), f.code, g, h, i, j, k, l, m}
        """
    )
    assert value[:2] == [False, "boom"]
    assert value[2] is False and "attempt to index a nil value" in value[3]
    assert value[4:7] == [False, "table", 7]
    assert value[7:11] == [True, 1, 2, 3]
    assert value[11] is False and "attempt to call a nil value" in value[12]
    assert value[13] == 1


def test_pcall_around_a_verb_error_returns_to_the_script() -> None:
    def boom(*args: object) -> None:
        raise ValueError("backend down")

    value, _ = run_lua(
        'local ok, e = pcall(memory.get, "p", "t") local ok2 = pcall(memory.get, "p", "t") return {ok, e, ok2}',
        "memory.get",
        verbs={"memory.get": boom},
    )
    assert value == [False, "backend down", False]


@pytest.mark.parametrize(
    "message",
    [
        "stack overflow",
        "memory.get: ambiguous: 'stack overflow notes', 'stack overflow tips'",
        "not enough memory",
        "error in error handling",
    ],
    ids=["stack", "stack_in_text", "memory", "handling"],
)
def test_a_verb_error_that_reads_like_a_limit_message_is_a_lua_error_not_a_limit(message: str) -> None:
    """The classifier reads messages; a verb's message is data the script's backend chose (review r1 I1)."""

    def call_verb(name: str, args: list[object]) -> object:
        raise VerbReplyError(message)

    value, _ = run_lua(
        "local ok, e = pcall(memory.get) local ok2, e2 = pcall(memory.get) return {ok, e, ok2, e2 == e}",
        "memory.get",
        call_verb=call_verb,
        limits=SMALL,
    )
    assert value == [False, message, False, True]
    with pytest.raises(ScriptError) as exc:
        run_lua("return memory.get()", "memory.get", call_verb=call_verb, limits=SMALL)
    assert exc.value.kind == "lua"
    assert exc.value.detail == message


def test_a_caught_verb_error_does_not_excuse_a_real_memory_overrun_with_the_same_text() -> None:
    """The exemption is for the verb's own error, once; the allocator's later "not enough memory" still ends the run."""

    def call_verb(name: str, args: list[object]) -> object:
        raise VerbReplyError("not enough memory")

    err = _limit(
        "pcall(memory.get) while true do pcall(string.rep, 'x', 1000000000) end",
        "memory.get",
        call_verb=call_verb,
        limits=SMALL.with_(memory=16 * MIB),
    )
    assert err.limit == "memory"


def test_a_caught_verb_error_does_not_excuse_a_real_stack_overflow() -> None:
    def call_verb(name: str, args: list[object]) -> object:
        raise VerbReplyError("stack overflow")

    err = _limit(
        "pcall(memory.get) local function r() return 1 + r() end return r()",
        "memory.get",
        call_verb=call_verb,
        limits=SMALL.with_(memory=256 * MIB, instructions=50_000_000),
    )
    assert err.limit == "stack"


# ---- the lowest limits ---------------------------------------------------------------------------------------------
# The real Limits floors memory at 1 MiB (limits.py); these use the test support's own Limits, which
# does not, so the runtime itself maps an allocation refused at any point of a run to the memory limit.


@pytest.mark.parametrize("memory", [1000, 10_000, 30_000])
def test_a_memory_limit_the_runtime_cannot_even_start_in_is_the_memory_limit(memory: int) -> None:
    err = _limit("return 1", limits=SMALL.with_(memory=memory))
    assert err.limit == "memory"
    assert err.value == memory
    assert err.stats is not None and set(err.stats) == {"peak_lua_bytes", "instructions"}


def test_a_big_source_that_does_not_fit_the_memory_limit_is_the_memory_limit_not_a_lua_error() -> None:
    source = "return {" + ",".join(str(i) for i in range(20_000)) + "}"  # about 108 KB
    assert len(source) > 100_000
    err = _limit(source, limits=SMALL.with_(memory=300_000))
    assert err.limit == "memory"
    assert err.value == 300_000
    _assert_stats(err)


# ---- memory -------------------------------------------------------------------------------------


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
    err = _limit(source, limits=SMALL.with_(memory=16 * MIB, instructions=50_000_000))
    assert err.limit == "memory"
    assert err.value == 16 * MIB
    _assert_stats(err)


def test_a_script_cannot_swallow_the_memory_limit_with_pcall() -> None:
    err = _limit(
        "while true do pcall(string.rep, 'x', 1000000000) end",
        limits=SMALL.with_(memory=16 * MIB, instructions=50_000_000),
    )
    assert err.limit == "memory"


def test_a_memory_limit_trips_inside_a_pcall_around_a_table_build() -> None:
    err = _limit(
        "local ok = pcall(function() local t = {} for i = 1, 1e9 do t[i] = i end end) return ok",
        limits=SMALL.with_(memory=16 * MIB, instructions=50_000_000),
    )
    assert err.limit == "memory"


def test_a_verb_result_that_does_not_fit_is_the_memory_limit_not_an_abort() -> None:
    """The worst shape (a list of empty tables, ~27 Lua bytes per JSON byte) through the protected build."""
    worst = [{} for _ in range(200_000)]
    err = _limit(
        "local r = memory.list('p') return #r",
        "memory.list",
        limits=SMALL.with_(memory=8 * MIB),
        verbs={"memory.list": lambda *a: worst},
    )
    assert err.limit == "memory"
    _assert_stats(err)


def test_a_script_cannot_survive_a_verb_result_that_overran_memory() -> None:
    worst = [{} for _ in range(200_000)]
    err = _limit(
        "local ok = pcall(memory.list, 'p') while true do pcall(memory.list, 'p') end",
        "memory.list",
        limits=SMALL.with_(memory=8 * MIB),
        verbs={"memory.list": lambda *a: worst},
    )
    assert err.limit == "memory"


def test_a_verb_result_that_fits_is_built_and_read() -> None:
    value, stats = run_lua(
        "local r = memory.list('p') return {#r, r[1].n, r[#r].n}",
        "memory.list",
        verbs={"memory.list": lambda *a: [{"n": i} for i in range(50_000)]},
    )
    assert value == [50_000, 0, 49_999]
    assert stats["peak_lua_bytes"] > 1 * MIB


# ---- stack ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "local function r() return 1 + r() end return r()",
        "local function r(n) return r(n + 1) + 1 end return r(1)",
        "local function r(t) return {r(t)} end return r({})",
    ],
)
def test_unbounded_recursion_fails_with_a_stack_overflow(source: str) -> None:
    err = _limit(source, limits=SMALL.with_(instructions=50_000_000, memory=256 * MIB))
    assert err.limit == "stack"
    assert err.value == runtime.STACK_LIMIT
    _assert_stats(err)
    # Under the default 64 MiB a deep enough frame shape runs out of memory first (the Lua stack at
    # its 1,000,000-slot limit and its call records take about 80 MB): either limit ends the run.
    again = _limit(source, limits=SMALL.with_(instructions=50_000_000))
    assert again.limit in ("stack", "memory")


def test_a_tail_call_loop_is_not_a_stack_overflow_it_is_the_instruction_limit() -> None:
    err = _limit("local function r(n) return r(n + 1) end return r(1)")
    assert err.limit == "instructions"


# ---- the hook interval ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 500, 2000])
def test_an_instruction_limit_below_the_hook_interval_still_trips(limit: int) -> None:
    """The hook fires every min(HOOK_EVERY, limit) instructions, so a short script can still exceed a low limit."""
    err = _limit("local x = 0 for i = 1, 3000 do x = x + i end return x", limits=SMALL.with_(instructions=limit))
    assert err.limit == "instructions"
    assert err.value == limit
    assert err.stats is not None and err.stats["instructions"] > limit


def test_a_script_under_a_low_instruction_limit_returns() -> None:
    value, _ = run_lua("return 1", limits=SMALL.with_(instructions=2000))
    assert value == 1


def test_the_hook_samples_the_peak_so_freed_memory_still_counts() -> None:
    """20 MB held across a few hook ticks, then dropped and churned away: the peak is what the hook saw."""
    _, stats = run_lua(
        """
        local s = string.rep('x', 20 * 1024 * 1024)
        for i = 1, 60000 do end
        s = nil
        for i = 1, 300000 do local t = {i} end
        return 1
        """,
        limits=DEFAULT.with_(wall_time=30.0),
    )
    assert stats["peak_lua_bytes"] >= 20 * MIB


# ---- wall time, the inner layer --------------------------------------------------------------------


def test_the_hook_checks_the_wall_clock_from_the_runs_own_start() -> None:
    started = time.monotonic()
    err = _limit("while true do end", limits=DEFAULT.with_(wall_time=0.3, instructions=10**9))
    assert err.limit == "wall_time"
    assert err.value == 0.3
    assert time.monotonic() - started < 10
    _assert_stats(err)


def test_the_wall_clock_is_the_hosts_monotonic_clock_read_from_the_start_of_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fake clock: 0 at the start of run(), then past the limit. The script is not told."""
    ticks = iter([0.0] + [5.0] * 10_000)

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return next(ticks)

    monkeypatch.setattr(runtime, "time", FakeTime)
    err = _limit("while true do end", limits=DEFAULT.with_(wall_time=2.0, instructions=50_000_000))
    assert err.limit == "wall_time"
    assert err.value == 2.0


def test_a_script_that_swallows_the_wall_time_trip_still_stops() -> None:
    err = _limit(
        "while true do pcall(function() while true do end end) end",
        limits=DEFAULT.with_(wall_time=0.3, instructions=10**9),
    )
    assert err.limit == "wall_time"


# ---- result ---------------------------------------------------------------------------------------


def test_a_two_megabyte_result_stops_on_the_result_cap() -> None:
    err = _limit("return string.rep('x', 2 * 1024 * 1024)", limits=DEFAULT.with_(wall_time=20.0))
    assert err.limit == "result"
    assert err.value == 1 * MIB
    _assert_stats(err)


def test_forty_deep_nesting_stops_on_the_result_cap() -> None:
    err = _limit(
        "local t = {} for i = 1, 40 do t = {t} end return t", limits=DEFAULT.with_(wall_time=20.0)
    )
    assert err.limit == "result"
    assert err.value == 32  # the depth that was in force
    _assert_stats(err)


def test_a_result_of_many_small_pieces_stops_on_the_byte_cap_with_memory_to_spare() -> None:
    err = _limit(
        "local t = {} for i = 1, 600000 do t[i] = i % 10 end return t",
        limits=DEFAULT.with_(wall_time=20.0, instructions=50_000_000),
    )
    assert err.limit == "result"
    assert err.value == 1 * MIB


def test_a_shared_subtable_cannot_blow_up_the_walk() -> None:
    """2^25 paths through a 25-deep DAG: the walk ends at the byte cap, not after 33 million nodes."""
    started = time.monotonic()
    err = _limit(
        "local a = {1} for i = 1, 25 do a = {a, a} end return a",
        limits=DEFAULT.with_(wall_time=20.0, instructions=50_000_000),
    )
    assert err.limit == "result"
    assert time.monotonic() - started < 10


# ---- a trip at any instruction maps to the limit -------------------------------------------------------


def test_a_limit_that_trips_at_any_instruction_of_the_run_maps_to_a_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook every instruction and sweep the instruction limit across the script and the host code around it.

    The rearmed hook also fires in the host's own wrapper after the script returns; whatever instruction
    the trip lands on, the outcome is the value or the instruction limit, never a Lua error.
    """
    monkeypatch.setattr(runtime, "HOOK_EVERY", 1)
    source = "local x = 0 for i = 1, 30 do x = x + i end return x"
    outcomes = set()
    for n in range(0, 400):
        try:
            value, _ = run_lua(source, limits=SMALL.with_(instructions=n))
        except ScriptLimitError as err:
            assert err.limit == "instructions"
            outcomes.add("limit")
        else:
            assert value == 465
            outcomes.add("value")
    assert outcomes == {"limit", "value"}


def test_a_trip_inside_a_verb_call_is_a_limit_not_a_verb_error() -> None:
    """The instruction limit lands while the host code of a verb function is running."""
    calls = 0

    def verb(*args: object) -> int:
        nonlocal calls
        calls += 1
        return calls

    err = _limit(
        "local n = 0 while true do n = memory.get('p', 't') end",
        "memory.get",
        verbs={"memory.get": verb},
        limits=SMALL.with_(instructions=200_000),
    )
    assert err.limit == "instructions"
    assert calls > 100


def test_a_plain_lua_error_is_not_a_limit() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("error('boom')", limits=SMALL)
    assert exc.value.kind == "lua"
    assert exc.value.stats is not None


def test_a_verb_reply_error_is_not_a_limit() -> None:
    def call_verb(name: str, args: list[object]) -> object:
        raise VerbReplyError("ungranted: nope")

    with pytest.raises(ScriptError) as exc:
        run_lua("return memory.get('p', 't')", "memory.get", call_verb=call_verb)
    assert exc.value.detail == "ungranted: nope"
