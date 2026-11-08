# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5): the result walk (Technical Design 12) and error shapes.

The host walks the returned Lua value into JSON itself, inside the worker and
inside a protected call. A function, a userdata or a table with mixed keys is a
``ScriptError("result", ...)`` naming what was wrong; bytes and depth are the
``result`` limit. A Lua error is ``ScriptError("lua", message)`` with a
message that is always valid UTF-8 and bounded.
"""

from __future__ import annotations

import json
import math
import time

import pytest

from nexus.lua_runtime import runtime
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError
from tests.lua_runtime._runtime_support import DEFAULT, run_lua

MIB = 1024 * 1024


def _value(source: str, **kw: object) -> object:
    return run_lua(source, **kw)[0]  # type: ignore[arg-type]


def _result_error(source: str, *grants: str, **kw: object) -> ScriptError:
    with pytest.raises(ScriptError) as exc:
        run_lua(source, *grants, **kw)  # type: ignore[arg-type]
    assert exc.value.kind == "result", exc.value
    assert exc.value.stats is not None
    return exc.value


# ---- what comes back -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("return", None),
        ("return nil", None),
        ("return true", True),
        ("return false", False),
        ("return 0", 0),
        ("return -7", -7),
        ("return math.maxinteger", 2**63 - 1),
        ("return math.mininteger", -(2**63)),
        ("return 0.5", 0.5),
        ("return 0.1", 0.1),
        ("return 1e300", 1e300),
        ("return 1/3", 1 / 3),
        ("return 6 / 2", 3.0),
        ("return 1e15", 1e15),
        ("return 2^53", 2.0**53),
        ("return 'abc'", "abc"),
        ("return ''", ""),
        ("return 'caf\\u{e9} \\u{1F600}'", "café \U0001f600"),
        ("return 'a\"b\\\\c\\n\\t\\r\\0\\1\\31\\127'", 'a"b\\c\n\t\r\x00\x01\x1f\x7f'),
        ("return 1, 2, 3", 1),  # the first value only
        ("return {}", {}),
        ("return {1, 2, 3}", [1, 2, 3]),
        ("return {'a', {'b', {'c'}}}", ["a", ["b", ["c"]]]),
        ("return {x = 1, y = {z = true}}", {"x": 1, "y": {"z": True}}),
        ("return {{}, {}}", [{}, {}]),
        ("return {[1] = 'a', [2] = 'b'}", ["a", "b"]),
        ("return {[2] = 'b', [1] = 'a', [3] = 'c'}", ["a", "b", "c"]),
    ],
)
def test_the_walk_maps_lua_values_to_json_types(source: str, expected: object) -> None:
    value = _value(source)
    assert value == expected
    assert type(value) is type(expected)


def test_a_float_with_an_integer_value_stays_a_float() -> None:
    value = _value("return {6 / 2, 7 // 2, 2^2}")
    assert [type(v) for v in value] == [float, int, float]  # type: ignore[union-attr]


def test_negative_zero_is_a_float() -> None:
    value = _value("return -0.0")
    assert isinstance(value, float) and math.copysign(1, value) == -1


def test_object_keys_come_back_sorted_so_a_result_is_the_same_every_run() -> None:
    value = _value("return {zeta = 1, alpha = 2, mid = 3, Beta = 4, a1 = 5}")
    assert list(value) == sorted(["zeta", "alpha", "mid", "Beta", "a1"])  # type: ignore[call-overload]


def test_an_empty_table_is_an_object() -> None:
    """Lua has one empty table; it is the empty object (json.dumps of the result is '{}')."""
    assert json.dumps(_value("return {}")) == "{}"


def test_a_900_kilobyte_result_returns_whole() -> None:
    value = _value("return string.rep('x', 900 * 1024)")
    assert isinstance(value, str) and len(value) == 900 * 1024


def test_a_wide_result_under_the_cap_returns_whole() -> None:
    value = _value("local t = {} for i = 1, 20000 do t[i] = {id = i, name = 'item' .. i} end return t")
    assert len(value) == 20000  # type: ignore[arg-type]
    assert value[19999] == {"id": 20000, "name": "item20000"}  # type: ignore[index]


# ---- bounds ------------------------------------------------------------------------------------------


def test_the_byte_cap_is_on_the_encoded_json_and_exact() -> None:
    limits = DEFAULT.with_(result=1002)
    assert len(_value("return string.rep('x', 1000)", limits=limits)) == 1000  # type: ignore[arg-type]  # 1000 + 2 quotes
    with pytest.raises(ScriptLimitError) as exc:
        run_lua("return string.rep('x', 1001)", limits=limits)
    assert exc.value.limit == "result" and exc.value.value == 1002


def test_the_cap_counts_escapes_and_separators_not_lua_bytes() -> None:
    limits = DEFAULT.with_(result=20)
    assert _value("return {1,2,3,4,5,6,7}", limits=limits) == [1, 2, 3, 4, 5, 6, 7]  # 15 bytes
    with pytest.raises(ScriptLimitError):
        run_lua("return {1,2,3,4,5,6,7,8,9,10,11,12}", limits=limits)  # 30 bytes
    with pytest.raises(ScriptLimitError):
        run_lua("return string.rep('\\n', 12)", limits=limits)  # 12 Lua bytes, 26 encoded


def test_depth_32_is_allowed_and_33_is_not() -> None:
    nested = "local t = 1 for i = 1, {n} do t = {{t}} end return t"
    value = _value(nested.format(n=32))
    depth = 0
    while isinstance(value, list):
        value, depth = value[0], depth + 1
    assert depth == 32
    with pytest.raises(ScriptLimitError) as exc:
        run_lua(nested.format(n=33))
    assert exc.value.limit == "result" and exc.value.value == 32


def test_the_depth_is_the_callers_lowered_limit() -> None:
    limits = DEFAULT.with_(depth=3)
    assert _value("return {{{1}}}", limits=limits) == [[[1]]]
    with pytest.raises(ScriptLimitError) as exc:
        run_lua("return {{{{1}}}}", limits=limits)
    assert exc.value.value == 3


# ---- what the walk refuses ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("return function() end", "function"),
        ("return {f = function() end}", "function"),
        ("return {1, {2, function() end}}", "function"),
        ("return {1, 2, x = 3}", "mixed"),
        ("return {[1] = 'a', [3] = 'c'}", "hole"),
        ("return {[0] = 'a'}", "key"),
        ("return {[-1] = 'a'}", "key"),
        ("return {[1.5] = 'a'}", "key"),
        ("return {[true] = 'a'}", "key"),
        ("return {[{}] = 'a'}", "key"),
        ("return 0/0", "NaN"),
        ("return math.huge", "infinity"),
        ("return -math.huge", "infinity"),
        ("return {x = 0/0}", "NaN"),
        ("return '\\xff'", "UTF-8"),
        ("return {['\\xff'] = 1}", "UTF-8"),
        ("return '\\xed\\xa0\\x80'", "UTF-8"),
        ("local t = {} t[1] = t return t", "cyclic"),
        ("local t = {} t.self = t return t", "cyclic"),
        ("local a, b = {}, {} a.b = b b.a = a return a", "cyclic"),
    ],
)
def test_the_walk_refuses_what_is_not_json_and_names_it(source: str, fragment: str) -> None:
    assert fragment in _result_error(source).detail


def test_a_shared_but_acyclic_subtable_is_fine() -> None:
    assert _value("local s = {1} return {s, s, {s}}") == [[1], [1], [[1]]]


@pytest.mark.parametrize("size", [10 * MIB, 20 * MIB])
def test_a_result_string_of_control_bytes_over_the_cap_is_the_result_limit_checked_before_copying(size: int) -> None:
    """Each \\1 escapes to six bytes; without the size check the escape copy runs out of memory first."""
    started = time.monotonic()
    with pytest.raises(ScriptLimitError) as exc:
        run_lua(f"return string.rep('\\1', {size})", limits=DEFAULT.with_(wall_time=30.0))
    assert exc.value.limit == "result"
    assert exc.value.value == 1 * MIB
    assert time.monotonic() - started < 3


def test_a_userdata_result_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class Thing:
        pass

    monkeypatch.setattr(runtime.LuaTableBuilder, "build", lambda self, value, json_len=None: Thing())
    err = _result_error("return memory.get('p', 't')", "memory.get", verbs={"memory.get": lambda *a: {}})
    assert "userdata" in err.detail


def test_a_returned_function_never_reaches_the_caller_as_a_proxy() -> None:
    err = _result_error("return memory.get", "memory.get", verbs={"memory.get": lambda *a: 1})  # a verb function is a function
    assert "function" in err.detail


# ---- Lua errors -----------------------------------------------------------------------------------------------


def test_a_lua_error_carries_its_message_and_position() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("local x = 1\nerror('boom')")
    assert exc.value.kind == "lua"
    assert exc.value.detail == "script:2: boom"
    assert set(exc.value.stats) == {"peak_lua_bytes", "instructions"}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("source", "detail"),
    [
        ("error('plain', 0)", "plain"),
        ("error()", "nil"),
        ("error(42)", "42"),
        ("error({code = 1})", "(error object is a table value)"),
        ("error(true)", "(error object is a boolean value)"),
        ("local x = nil return x.y", "script:1: attempt to index a nil value (local 'x')"),
        ("return 1 + {}", "script:1: attempt to perform arithmetic on a table value"),
        ("return #5", "script:1: attempt to get length of a number value"),
        ("assert(false)", "script:1: assertion failed!"),
        ("assert(false, 'why')", "script:1: why"),
        ("assert(nil, {})", "(error object is a table value)"),
    ],
)
def test_a_lua_error_is_a_script_error_with_the_message_a_lua_user_expects(source: str, detail: str) -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua(source)
    assert exc.value.kind == "lua"
    assert exc.value.detail == detail


def test_an_error_message_with_bytes_that_are_not_utf8_still_arrives() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("error('bad \\xff\\xfe bytes', 0)")
    assert exc.value.kind == "lua"
    assert exc.value.detail.startswith("bad ")
    exc.value.detail.encode("utf-8")  # a str, valid


def test_an_error_message_with_utf8_arrives_unchanged() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("error('caf\\u{e9} \\u{1F600}', 0)")
    assert exc.value.detail == "café \U0001f600"


def test_a_long_error_message_is_cut_on_a_character_boundary() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("error(string.rep('\\u{e9}', 100000), 0)")
    detail = exc.value.detail
    assert 1000 < len(detail.encode("utf-8")) < 8192
    assert detail.endswith("...")
    detail[:-3].encode("utf-8")
    assert set(detail[:-3]) == {"é"}


def test_a_long_error_message_with_three_byte_characters_is_cut_back_to_a_boundary() -> None:
    """2048 is not a multiple of 3, so the cut lands inside a character and has to move back to its start."""
    with pytest.raises(ScriptError) as exc:
        run_lua("error(string.rep('\\u{20ac}', 100000), 0)")
    detail = exc.value.detail
    assert detail.endswith("...")
    assert set(detail[:-3]) == {"\u20ac"}
    assert len(detail[:-3]) == 682  # 682 whole characters, 2046 bytes: the cut moved back two bytes


@pytest.mark.parametrize(
    "source",
    ["return function(", "x = = 1", "return 'unfinished", "return 'unfinished \xff\xfe\n", "goto nowhere", "return \xff"],
)
def test_source_that_does_not_compile_is_a_lua_error(source: str) -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua(source.encode("latin-1"))
    assert exc.value.kind == "lua"
    assert exc.value.stats is not None
    exc.value.detail.encode("utf-8")


# ---- args --------------------------------------------------------------------------------------------------------


def test_args_reach_the_script_as_the_main_chunks_vararg() -> None:
    value = _value(
        "local args = ... return {select('#', ...), args.rdr, args.n, args.flag, args.nested.list[2], args.missing == nil}",
        args={"rdr": "RDR-242", "n": 3, "flag": True, "nested": {"list": [10, 20]}},
    )
    assert value == [1, "RDR-242", 3, True, 20, True]


def test_no_args_is_an_empty_table_not_nil() -> None:
    assert _value("local args = ... return {type(args), next(args) == nil}") == ["table", True]


def test_args_are_a_copy_each_run_gets_its_own() -> None:
    mine = {"k": [1, 2]}
    run_lua("local args = ... args.k[1] = 99 args.extra = true", args=mine)
    assert mine == {"k": [1, 2]}
    assert _value("local args = ... return args.extra", args=mine) is None


def test_args_too_big_for_the_memory_limit_are_the_memory_limit_not_an_abort() -> None:
    """Args go through the protected build like a verb result: the worst shape at 8 MiB must not abort."""
    rows = [{} for _ in range(200_000)]
    with pytest.raises(ScriptLimitError) as exc:
        run_lua("local a = ... return #a.rows", args={"rows": rows}, limits=DEFAULT.with_(memory=8 * MIB))
    assert exc.value.limit == "memory"


def test_args_add_no_global() -> None:
    assert _value("return args == nil", args={"x": 1}) is True


# ---- stats ---------------------------------------------------------------------------------------------------------


def test_stats_on_success() -> None:
    _, stats = run_lua("local s = string.rep('x', 2 * 1024 * 1024) return #s")
    assert set(stats) == {"peak_lua_bytes", "instructions"}
    assert all(type(v) is int for v in stats.values())
    assert stats["peak_lua_bytes"] >= 2 * MIB


def test_stats_on_a_result_error_and_a_lua_error() -> None:
    err = _result_error("local s = string.rep('x', 3 * 1024 * 1024) return function() end")
    assert err.stats["peak_lua_bytes"] >= 3 * MIB  # type: ignore[index]


def test_a_lua_error_outside_the_scripts_own_pcall_still_carries_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch for a Lua error that escapes the runner (here the boot chunk fails): kind lua, with stats."""
    monkeypatch.setattr(runtime, "_BOOT", 'error("boot failed", 0)')
    with pytest.raises(ScriptError) as exc:
        run_lua("return 1")
    assert exc.value.kind == "lua"
    assert "boot failed" in exc.value.detail
    assert exc.value.stats is not None
    assert set(exc.value.stats) == {"peak_lua_bytes", "instructions"}
    assert exc.value.stats["peak_lua_bytes"] > 0
