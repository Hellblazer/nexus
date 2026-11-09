# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5): what the worker branch's process tests ask of ``run``.

The worker's tests (``test_worker_process.py``, ``test_worker_robustness.py``,
nexus-mdbds.4) ran against the Step 2 stand-in and now run against this
runtime through the real worker. Each test here asserts, in process, the same
thing about ``run`` that one of those asserts through the pipe, so a change
to the runtime that would turn one of them red is caught here first.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus.lua_runtime import luatable
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError
from nexus.lua_runtime.frames import VerbReplyError
from tests.lua_runtime._runtime_support import DEFAULT, run_lua

MIB = 1024 * 1024


def _echo(s: str) -> dict[str, Any]:
    return {"got": s, "list": [1, 2, 3]}


def test_a_one_line_fixture_returns_a_json_result_with_stats() -> None:
    value, stats = run_lua("return 1 + 1")
    assert value == 2
    assert stats["peak_lua_bytes"] > 0


def test_a_verb_call_is_a_round_trip_and_its_result_returns_as_json() -> None:
    value, _ = run_lua(
        'local r = fixture.echo("hi") return {got = r.got, n = #r.list, third = r.list[3]}',
        "fixture.echo",
        verbs={"fixture.echo": _echo},
    )
    assert value == {"got": "hi", "n": 3, "third": 3}


def test_a_lua_error_is_a_script_error_without_a_traceback() -> None:
    with pytest.raises(ScriptError) as err:
        run_lua('error("boom")')
    assert err.value.kind == "lua" and "boom" in err.value.detail
    assert "stack traceback" not in err.value.detail


def test_the_count_hook_reports_instructions_used() -> None:
    _, stats = run_lua("local n = 0 for i = 1, 2000000 do n = n + i end return n")
    assert stats["instructions"] >= 1_000_000, stats


def test_a_result_over_the_result_bound_is_a_limit_and_900_kb_returns_whole() -> None:
    with pytest.raises(ScriptLimitError) as err:
        run_lua('return string.rep("x", 1100000)')
    assert err.value.limit == "result"
    assert run_lua('return #string.rep("x", 900000)')[0] == 900000
    assert len(run_lua('return string.rep("x", 900000)')[0]) == 900000


def test_a_lua_memory_overrun_is_a_memory_limit_with_the_peak_reported() -> None:
    with pytest.raises(ScriptLimitError) as err:
        run_lua('return #string.rep("x", 1e9)')
    assert err.value.limit == "memory" and err.value.value == 64 * MIB
    assert err.value.stats is not None


def test_a_refusal_becomes_a_lua_error_the_script_can_catch() -> None:
    def call_verb(name: str, args: list[Any]) -> Any:
        raise VerbReplyError("fixture.echo: argument 1 must be a string")

    value, _ = run_lua(
        "local ok, e = pcall(fixture.echo, 5) return {ok = ok, e = e}",
        "fixture.echo",
        call_verb=call_verb,
    )
    assert value["ok"] is False and "fixture.echo" in value["e"]
    with pytest.raises(ScriptError) as err:
        run_lua("return fixture.echo(5)", "fixture.echo", call_verb=call_verb)
    assert err.value.kind == "lua" and "fixture.echo" in err.value.detail


def test_a_verb_failure_reaches_the_script_as_its_message() -> None:
    def call_verb(name: str, args: list[Any]) -> Any:
        raise VerbReplyError(
            "it exploded" if name == "fixture.boom" else "memory.get: no such project"
        )

    caught, _ = run_lua(
        'local _, a = pcall(fixture.boom, "x") local _, b = pcall(fixture.refuses, "x") return {a = a, b = b}',
        "fixture.boom",
        "fixture.refuses",
        call_verb=call_verb,
    )
    assert caught == {"a": "it exploded", "b": "memory.get: no such project"}


# ---- args ------------------------------------------------------------------------------------


def test_the_script_gets_its_args_as_one_table_through_the_main_chunk_vararg() -> None:
    src = 'local n = select("#", ...) local args = ... return {n = n, sum = args.a + args.b, s = args.name, absent = args.optional}'
    value, _ = run_lua(src, args={"a": 40, "b": 2, "name": "x"})
    assert value == {"n": 1, "sum": 42, "s": "x"}  # an absent optional is nil, so its key is absent


def test_a_start_with_no_args_gives_an_empty_table() -> None:
    assert run_lua("local a = ... return type(a) == 'table' and next(a) == nil")[0] is True


def test_a_nested_args_value_reaches_the_script_as_a_table() -> None:
    value, _ = run_lua(
        "local a = ... return a.list[2] .. a.map.k",
        args={"list": ["p", "q"], "map": {"k": "!"}},
    )
    assert value == "q!"


# ---- the worker's final frame ------------------------------------------------------------------


@pytest.mark.parametrize("source", ["return 0/0", "return 1/0", "return -1/0"])
def test_a_nan_or_infinite_result_is_a_result_error_naming_json(source: str) -> None:
    with pytest.raises(ScriptError) as err:
        run_lua(source)
    assert err.value.kind == "result" and "JSON" in err.value.detail


def test_a_lua_error_message_of_two_megabytes_arrives_cut_with_its_head() -> None:
    with pytest.raises(ScriptError) as err:
        run_lua('error(("x"):rep(2000000))')
    assert err.value.kind == "lua"
    assert 1000 < len(err.value.detail) <= 64 * 1024
    assert err.value.detail.startswith("script:1:")


# ---- verb results of an unusual shape ------------------------------------------------------------


def test_a_result_nested_far_past_the_result_depth_reaches_the_script() -> None:
    deep: list[Any] = []
    for _ in range(2000):
        deep = [deep]
    value, _ = run_lua("return type(fixture.abyss())", "fixture.abyss", verbs={"fixture.abyss": lambda: deep})
    assert value == "table"


def test_3400_chains_of_depth_300_are_a_memory_limit_not_an_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The builder's bound is 48 Lua bytes per JSON byte on the worker branch (9012c953e); this
    # branch carries the scaffolding value of 32, under which this shape aborts the process.
    # Pinning 48 keeps the test valid on both; after the merge the line is a no-op.
    monkeypatch.setattr(luatable, "WORST_BYTES_PER_JSON_BYTE", 48)
    chain: list[Any] = []
    for _ in range(299):
        chain = [chain]
    # through JSON, as a reply frame is decoded: 3400 distinct chains, not one shared object
    # (lupa's table_from maps a repeated Python object to one Lua table)
    result = json.loads(json.dumps([chain] * 3400))
    with pytest.raises(ScriptLimitError) as err:
        run_lua(
            "return #fixture.deep()",
            "fixture.deep",
            verbs={"fixture.deep": lambda: result},
            limits=DEFAULT,
        )
    assert err.value.limit == "memory", err.value
    assert run_lua("return 1")[0] == 1
