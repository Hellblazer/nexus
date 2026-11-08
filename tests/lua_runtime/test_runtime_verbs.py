# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5): verbs at run time, grants, isolation (Technical Design 9).

One Python dispatcher is the only Python object that crosses into Lua; a Lua
function per granted verb is built by the host. With the static check
bypassed (``run`` is called directly) an ungranted verb has no function, a
namespace with no grant is absent, and the dispatcher refuses an ungranted
verb by name if it is ever called. A verb result reaches the script only as a
table built through the protected build.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nexus.lua_runtime import frames, runtime
from nexus.lua_runtime.errors import ScriptError
from nexus.lua_runtime.frames import VerbReplyError
from tests.lua_runtime._runtime_support import DEFAULT, run_lua

# ---- grants at run time ---------------------------------------------------------------------------


def test_a_granted_verb_is_called_with_its_name_and_arguments() -> None:
    seen: list[tuple[str, list[Any]]] = []

    def call_verb(name: str, args: list[Any]) -> Any:
        seen.append((name, args))
        return {"ok": True}

    value, _ = run_lua(
        "return memory.get('proj', 'title')", "memory.get", call_verb=call_verb
    )
    assert value == {"ok": True}
    assert seen == [("memory.get", ["proj", "title"])]


def test_an_ungranted_verb_fails_naming_it() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("return memory.list('p')", "memory.get", verbs={"memory.get": lambda *a: 1})
    assert exc.value.kind == "lua"
    assert "field 'list'" in exc.value.detail


def test_a_namespace_with_no_grant_is_absent() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("return git.diff_names('main')", "memory.get", verbs={"memory.get": lambda *a: 1})
    assert "global 'git'" in exc.value.detail
    value, _ = run_lua("return git == nil and tables == nil and transcripts == nil", "memory.get")
    assert value is True


def test_no_grants_means_no_verb_namespace_at_all() -> None:
    value, _ = run_lua("return memory == nil", )
    assert value is True


def test_the_dispatcher_refuses_an_ungranted_verb_by_name() -> None:
    called: list[str] = []
    d = runtime._Dispatcher(
        grants=frozenset({"memory.get"}),
        call_verb=lambda n, a: called.append(n),
        build=lambda v: v,
        sample=lambda: None,
    )
    reply = d.dispatch("memory.put", 2, "p", "t")
    assert isinstance(reply, str) and "memory.put" in reply
    assert called == []
    assert d.dispatch("memory.get", 2, "p", "t") is None
    assert called == ["memory.get"]


def test_the_dispatcher_never_raises_into_lua() -> None:
    def boom(name: str, args: list[Any]) -> Any:
        raise KeyError("x")

    d = runtime._Dispatcher(
        grants=frozenset({"a.b"}), call_verb=boom, build=lambda v: v, sample=lambda: None
    )
    assert d.dispatch("a.b", 0) == "a.b: worker error (KeyError)"
    ok = runtime._Dispatcher(
        grants=frozenset({"a.b"}),
        call_verb=lambda n, a: (_ for _ in ()).throw(VerbReplyError("refused: bad_args")),
        build=lambda v: v,
        sample=lambda: None,
    )
    assert ok.dispatch("a.b", 0) == "refused: bad_args"


@pytest.mark.parametrize(
    "bad",
    [{"a": 1}, [1], object(), b"bytes", float("nan"), float("inf"), "\ud800"],
    ids=["dict", "list", "object", "bytes", "nan", "inf", "lone_surrogate"],
)
def test_the_dispatcher_checks_its_arguments_too(bad: Any) -> None:
    d = runtime._Dispatcher(
        grants=frozenset({"a.b"}), call_verb=lambda n, a: 1, build=lambda v: v, sample=lambda: None
    )
    reply = d.dispatch("a.b", 1, bad)
    assert isinstance(reply, str) and "a.b" in reply


@pytest.mark.parametrize("value", [object(), {1, 2}, b"x", lambda: 1, 1j])
def test_the_dispatcher_refuses_a_verb_result_that_is_not_json(value: Any) -> None:
    d = runtime._Dispatcher(
        grants=frozenset({"a.b"}), call_verb=lambda n, a: value, build=lambda v: v, sample=lambda: None
    )
    reply = d.dispatch("a.b", 0)
    assert isinstance(reply, str) and "JSON" in reply
    assert d.fetch() is None


def test_a_verb_result_nested_object_that_is_not_json_is_an_error_string() -> None:
    value, _ = run_lua(
        "local ok, e = pcall(memory.get, 'p', 't') return {ok, type(e)}",
        "memory.get",
        verbs={"memory.get": lambda *a: {"nested": {1, 2}}},
    )
    assert value == [False, "string"]


# ---- argument and result values across the boundary -----------------------------------------------------


def test_argument_types_cross_as_json_values() -> None:
    seen: list[list[Any]] = []

    def call_verb(name: str, args: list[Any]) -> Any:
        seen.append(args)
        return None

    run_lua(
        "memory.get('s', 7, 2.5, true, false, nil) memory.get() memory.get(nil, nil)"
        " memory.get('caf\\u{e9}\\0nul')",
        "memory.get",
        call_verb=call_verb,
    )
    assert seen == [["s", 7, 2.5, True, False, None], [], [None, None], ["café\x00nul"]]
    assert type(seen[0][1]) is int and type(seen[0][2]) is float
    assert type(seen[0][3]) is bool


def test_a_verb_result_is_a_lua_table_the_script_can_read() -> None:
    result = {
        "title": "t",
        "n": 3,
        "ratio": 0.25,
        "flag": True,
        "tags": ["a", "b"],
        "rows": [{"id": 1}, {"id": 2}],
        "empty": [],
    }
    value, _ = run_lua(
        """
        local r = memory.get('p', 't')
        return {r.title, r.n, r.ratio, r.flag, #r.tags, r.rows[2].id, #r.empty, r.missing == nil}
        """,
        "memory.get",
        verbs={"memory.get": lambda *a: result},
    )
    assert value == ["t", 3, 0.25, True, 2, 2, 0, True]


@pytest.mark.parametrize("scalar", ["text", 5, 2.5, True, False])
def test_a_scalar_verb_result_is_the_scalar(scalar: Any) -> None:
    value, _ = run_lua("return memory.get('p', 't')", "memory.get", verbs={"memory.get": lambda *a: scalar})
    assert value == scalar and type(value) is type(scalar)


def test_a_none_verb_result_is_nil() -> None:
    value, _ = run_lua(
        "local r = memory.get('p', 't') return {r == nil, type(r)}",
        "memory.get",
        verbs={"memory.get": lambda *a: None},
    )
    assert value == [True, "nil"]


def test_a_big_integer_in_a_verb_result_becomes_a_float_not_an_error() -> None:
    value, _ = run_lua(
        "return memory.get('p', 't')", "memory.get", verbs={"memory.get": lambda *a: 2**70}
    )
    assert value == float(2**70)


def test_a_verb_result_is_one_fresh_table_per_call() -> None:
    value, _ = run_lua(
        "local a = memory.get('p', 't') local b = memory.get('p', 't') a.x = 1 return {ax = a.x, bx = tostring(b.x), same = a == b}",
        "memory.get",
        verbs={"memory.get": lambda *a: {}},
    )
    assert value == {"ax": 1, "bx": "nil", "same": False}


def test_many_calls_count_one_dispatcher_round_trip_each() -> None:
    n = 0

    def verb(*args: Any) -> int:
        nonlocal n
        n += 1
        return n

    value, _ = run_lua(
        "local s = 0 for i = 1, 500 do s = s + memory.get('p', 't') end return s",
        "memory.get",
        verbs={"memory.get": verb},
    )
    assert n == 500
    assert value == sum(range(1, 501))


def test_several_verbs_in_one_namespace_and_across_namespaces() -> None:
    verbs = {
        "memory.get": lambda *a: "g",
        "memory.list": lambda *a: "l",
        "git.diff_names": lambda *a: "d",
    }
    value, _ = run_lua(
        "return {memory.get(), memory.list(), git.diff_names('x')}",
        "memory.get",
        "memory.list",
        "git.diff_names",
        verbs=verbs,
    )
    assert value == ["g", "l", "d"]


# ---- bad grants ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant",
    ["nodot", "a.b.c", ".b", "a.", "A.b", "a.B", "string.rep", "table.get", "math.get", "utf8.get", "a b.c", "a.b;c"],
)
def test_a_malformed_or_colliding_grant_is_refused_before_anything_runs(grant: str) -> None:
    with pytest.raises(ValueError, match="grant"):
        run_lua("return 1", grant)


# ---- isolation ----------------------------------------------------------------------------------------------------


def test_a_global_set_by_one_run_is_absent_in_the_next() -> None:
    run_lua("x = 'planted' counter = 1 memory = {get = 1}", "memory.get", verbs={"memory.get": lambda *a: 1})
    value, _ = run_lua("return {tostring(x), tostring(counter), type(memory)}")
    assert value == ["nil", "nil", "nil"]


def test_the_string_library_is_fresh_in_every_run() -> None:
    run_lua("string.rep = nil string.planted = true table.sort = nil math.huge = 0")
    value, _ = run_lua(
        "return {string.rep ~= nil, string.planted == nil, table.sort ~= nil, math.huge > 1e300}"
    )
    assert value == [True, True, True, True]


def test_runs_do_not_share_a_lua_state() -> None:
    first, _ = run_lua("return tostring({})")
    runtimes = {run_lua("local t = {} t.k = 1 return t.k")[0] for _ in range(5)}
    assert runtimes == {1}
    assert isinstance(first, str)


# ---- text that crosses into Lua is valid UTF-8 (review r1 B1) -----------------------------------------
# The parent's reply encoder (json.dumps, ensure_ascii) carries a lone surrogate in an error message,
# frames._loads decodes it to a str that lupa cannot encode, and the failed encode used to reach the
# script as a userdata error value: a Python exception object inside the sandbox.

_SURROGATE_REPLY = (
    b'{"t":"reply","id":1,"ok":false,"error":{"kind":"verb_error","message":"title \\ud800 gone"}}'
)


def _wire_surrogate_error(name: str, args: list[Any]) -> Any:
    reply = frames._loads(_SURROGATE_REPLY)
    raise VerbReplyError(str(reply["error"]["message"]))


def test_a_surrogate_in_a_verb_error_message_reaches_a_protected_script_as_a_string() -> None:
    value, _ = run_lua(
        """
        local ok, e = pcall(memory.get)
        local ok2, e2 = pcall(function() return e.anything end)
        local ok3, e3 = pcall(function() return e:upper() end)
        return {ok, type(e), e, ok2, tostring(e2), ok3, type(e3)}
        """,
        "memory.get",
        call_verb=_wire_surrogate_error,
    )
    assert value[:2] == [False, "string"]
    assert value[2].startswith("title ") and value[2].endswith(" gone")
    assert value[3:] == [True, "nil", True, "string"]


def test_a_surrogate_in_a_verb_error_message_is_a_lua_error_string_when_unprotected() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("return memory.get()", "memory.get", call_verb=_wire_surrogate_error)
    assert exc.value.kind == "lua"
    assert exc.value.detail.startswith("title ") and "userdata" not in exc.value.detail


def test_the_dispatcher_returns_only_text_that_encodes() -> None:
    d = runtime._Dispatcher(
        grants=frozenset({"a.b"}),
        call_verb=lambda n, a: (_ for _ in ()).throw(VerbReplyError("x \ud800 y")),
        build=lambda v: v,
        sample=lambda: None,
    )
    reply = d.dispatch("a.b", 0)
    assert isinstance(reply, str)
    reply.encode("utf-8")


@pytest.mark.parametrize("protected", [True, False], ids=["pcall", "bare"])
def test_a_scalar_string_verb_result_with_a_surrogate_is_an_error_string_not_a_host_object(
    protected: bool,
) -> None:
    call = "pcall(memory.get)" if protected else "memory.get()"
    source = (
        f"local ok, r = {call} return {{ok, type(r)}}"
        if protected
        else "local r = memory.get() return {true, type(r)}"
    )
    if protected:
        value, _ = run_lua(source, "memory.get", verbs={"memory.get": lambda *a: "a\ud800b"})
        assert value == [False, "string"]
    else:
        with pytest.raises(ScriptError) as exc:
            run_lua(source, "memory.get", verbs={"memory.get": lambda *a: "a\ud800b"})
        assert exc.value.kind == "lua" and "userdata" not in exc.value.detail
        assert "UTF-8" in exc.value.detail


def test_a_nested_string_with_a_surrogate_in_a_verb_result_is_an_error_string() -> None:
    value, _ = run_lua(
        "local ok, r = pcall(memory.get) return {ok, type(r)}",
        "memory.get",
        verbs={"memory.get": lambda *a: {"k": ["x\ud800"]}},
    )
    assert value == [False, "string"]


# ---- the arity cap (review r1 S2) ----------------------------------------------------------------------------


def test_a_verb_call_with_a_hundred_thousand_arguments_is_refused_fast_and_never_dispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[object] = []
    real = runtime._Dispatcher.dispatch

    def counting(self: Any, *a: Any) -> Any:
        dispatched.append(a[:1])
        return real(self, *a)

    monkeypatch.setattr(runtime._Dispatcher, "dispatch", counting)
    started = time.monotonic()
    value, _ = run_lua(
        """
        local t = {}
        for i = 1, 100000 do t[i] = i end
        local ok, e = pcall(memory.get, table.unpack(t, 1, 100000))
        local ok2 = pcall(memory.get, table.unpack(t, 1, 16))
        local ok3, e3 = pcall(memory.get, table.unpack(t, 1, 17))
        return {ok, e, ok2, ok3, e3}
        """,
        "memory.get",
        verbs={"memory.get": lambda *a: 1},
        limits=DEFAULT.with_(wall_time=30.0),
    )
    assert time.monotonic() - started < 3
    assert value[0] is False and "too many arguments" in value[1] and "16" in value[1]
    assert value[2] is True  # sixteen is allowed
    assert value[3] is False and "too many arguments" in value[4]
    assert len(dispatched) == 1  # only the call with sixteen reached the dispatcher


# ---- JSON null in a verb result (documented in runtime.py) -------------------------------------------------


@pytest.mark.parametrize(
    ("result", "source", "expected"),
    [
        ([1, None, 3], "local r = memory.get() return {#r >= 1, r[1], tostring(r[2]), r[3]}", [True, 1, "nil", 3]),
        ([None, None], "local r = memory.get() return {next(r) == nil}", [True]),
        ({"a": None, "b": 1}, "local r = memory.get() return {tostring(r.a), r.b, (next(r))}", ["nil", 1, "b"]),
        ([], "local r = memory.get() return {next(r) == nil}", [True]),
    ],
    ids=["hole", "all_null_array", "null_key_dropped", "empty_list"],
)
def test_json_null_in_a_verb_result_reaches_lua_as_documented(
    result: Any, source: str, expected: Any
) -> None:
    value, _ = run_lua(source, "memory.get", verbs={"memory.get": lambda *a: result})
    assert value == expected


def test_a_verb_result_with_a_hole_cannot_be_returned_unchanged() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("return memory.get()", "memory.get", verbs={"memory.get": lambda *a: [1, None, 3]})
    assert exc.value.kind == "result" and "holes" in exc.value.detail
