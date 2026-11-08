# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Phase 1 Step 3 (nexus-mdbds.5): the escape suite, the second layer.

The static check (nexus-mdbds.6) refuses these scripts before they run. Here it
is bypassed on purpose: ``run`` is called with the source directly, so each
test shows what the sandbox itself does when a script gets that far. Every
class in the Key Discoveries and every probe of ``scripts/spikes/rdr242-lua-sandbox/``
is a test here (the map is in the T1 notes of nexus-mdbds.5): the empty
environment, the string metatable, binary chunks, host objects, the
dispatcher, a verb error as a string, a verb function's upvalues, and each
file of the static-check corpus run with the check bypassed.
"""

from __future__ import annotations

import pathlib

import lupa.lua54 as lua
import pytest

from nexus.lua_runtime import runtime, static_check
from nexus.lua_runtime.errors import ScriptError
from tests.lua_runtime._runtime_support import run_lua

#: Every global named in Key Discoveries and the probes, plus the neighbours a port could add.
DANGEROUS_GLOBALS = (
    "io os require load loadstring dofile loadfile debug package python "
    "getmetatable setmetatable getfenv setfenv ffi jit _G rawget rawset "
    "collectgarbage coroutine xpcall print newproxy module string_dump"
).split()


@pytest.mark.parametrize("name", DANGEROUS_GLOBALS)
def test_dangerous_global_is_nil(name: str) -> None:
    value, _ = run_lua(f"return {name} == nil")
    assert value is True


def test_the_environment_holds_exactly_the_listed_names() -> None:
    """One list: the base functions and libraries are the static check's, nothing else."""
    value, _ = run_lua(
        "local k = {} for n in pairs(_ENV) do k[#k + 1] = n end table.sort(k) return k"
    )
    expected = sorted(static_check.BASE_FUNCTIONS | set(static_check.LIB_MEMBERS))
    assert value == expected


@pytest.mark.parametrize("lib", sorted(static_check.LIB_MEMBERS))
def test_each_library_holds_exactly_its_listed_members(lib: str) -> None:
    value, _ = run_lua(
        f"local k = {{}} for n in pairs({lib}) do k[#k + 1] = n end table.sort(k) return k"
    )
    assert value == sorted(static_check.LIB_MEMBERS[lib])


def test_the_runtime_takes_the_static_checks_lists_not_a_copy() -> None:
    assert runtime.BASE_FUNCTIONS is static_check.BASE_FUNCTIONS
    assert runtime.LIB_MEMBERS is static_check.LIB_MEMBERS


def test_pcall_in_the_environment_is_the_guarded_one() -> None:
    value, _ = run_lua("return tostring(pcall):sub(1, 9), pcall ~= nil")
    assert value is not None  # an ordinary function value reaches the script


# ---- the string metatable and string.dump -------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        'return ("").dump == nil',
        'return string.dump == nil',
        'return ("x").dump == nil',
        'local s, k = "", "du" .. "mp" return s[k] == nil',  # corpus ok_layer2_dynamic_dump
        'return ("").__index == nil',
        'return (""):len() == 0 and ("abc"):upper() == "ABC"',
    ],
)
def test_string_dump_is_gone_and_strings_still_work(src: str) -> None:
    assert run_lua(src)[0] is True


def test_calling_dump_as_a_method_is_a_lua_error() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua('local s = "" return s:dump()')
    assert exc.value.kind == "lua"
    assert "dump" in exc.value.detail


def test_the_string_metatable_index_is_the_reduced_library() -> None:
    value, _ = run_lua(
        """
        local n = 0
        for _ in pairs(string) do n = n + 1 end
        local m = 0
        for _, name in ipairs({"byte","char","find","format","gmatch","gsub","len","lower",
                               "match","pack","packsize","rep","reverse","sub","unpack","upper"}) do
          if ("")[name] == string[name] then m = m + 1 end
        end
        return {n, m}
        """
    )
    assert value == [16, 16]


# ---- binary chunks ----------------------------------------------------------------------


def _bytecode() -> bytes:
    rt = lua.LuaRuntime(register_eval=False, encoding=None)
    return bytes(rt.eval("string.dump(function() return 7 end)"))


def test_a_binary_chunk_is_refused_before_compile() -> None:
    chunk = _bytecode()
    assert chunk[:1] == b"\x1b"
    with pytest.raises(ScriptError) as exc:
        run_lua(chunk)
    assert exc.value.kind == "lua"
    assert "binary" in exc.value.detail
    # refused by the byte test, not by load's mode argument
    assert "mode" not in exc.value.detail


def test_an_escape_byte_alone_is_refused_as_a_binary_chunk() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua(b"\x1bLua")
    assert "binary" in exc.value.detail


# ---- _ENV, the one name a script always has --------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        'return _ENV["lo" .. "ad"] == nil',  # corpus bad_env_index
        "local e = _ENV return e.load == nil and e.io == nil and e._G == nil",  # bad_env_value
        "local function f() return _ENV end return f().os == nil",  # bad_env_nested
        "local _ENV = {x = 1} return x",  # bad_env_local: a script's own table, nothing of the host's
    ],
)
def test_env_gives_nothing_of_the_hosts(src: str) -> None:
    value, _ = run_lua(src)
    assert value in (True, 1)


def test_a_global_set_by_a_script_lands_in_its_own_environment_only() -> None:
    run_lua("x = 1 string.planted = 2 table.insert = nil")
    value, _ = run_lua(
        'return {x == nil, string.planted == nil, ("").planted == nil, table.insert ~= nil}'
    )
    assert value == [True, True, True, True]


# ---- coroutine --------------------------------------------------------------------------


def test_coroutine_is_absent_so_a_new_thread_cannot_dodge_the_hook() -> None:
    with pytest.raises(ScriptError) as exc:
        run_lua("return coroutine.wrap(function() while true do end end)()")
    assert "coroutine" in exc.value.detail


# ---- no Python object in the script's reach -----------------------------------------------

_WALK = """
local seen, types = {}, {}
local function walk(v)
  local t = type(v)
  types[t] = true
  if t == "table" and not seen[v] then
    seen[v] = true
    for k, x in pairs(v) do walk(k) walk(x) end
  end
end
walk(_ENV)
local out = {}
for t in pairs(types) do out[#out + 1] = t end
table.sort(out)
return out
"""


def test_nothing_the_script_can_reach_is_a_python_object() -> None:
    """lupa shows a Python object to Lua as userdata; the dispatcher is held as an upvalue only."""
    value, _ = run_lua(
        _WALK, "memory.get", "memory.list", verbs={"memory.get": lambda *a: 1, "memory.list": lambda *a: 2}
    )
    assert "userdata" not in value
    assert "thread" not in value
    assert set(value) <= {"function", "table", "string", "number", "boolean"}


def _spy_on_runtimes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, object], lua.LuaRuntime]]:
    """Record the keyword arguments and the instance of every LuaRuntime that ``run`` builds."""
    made: list[tuple[dict[str, object], lua.LuaRuntime]] = []
    real = lua.LuaRuntime

    def build(**kwargs: object) -> lua.LuaRuntime:
        rt = real(**kwargs)
        made.append((dict(kwargs), rt))
        return rt

    monkeypatch.setattr(runtime.lua, "LuaRuntime", build)
    return made


def test_the_runtime_is_built_with_every_python_surface_off(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _spy_on_runtimes(monkeypatch)
    run_lua("return 1")
    ((seen, _),) = made
    assert seen["register_eval"] is False
    assert seen["register_builtins"] is False
    assert seen["unpack_returned_tuples"] is False
    assert seen["max_memory"] == 64 * 1024 * 1024
    refuse = seen["attribute_filter"]
    assert callable(refuse)
    for attr, setting in (("x", False), ("__class__", False), ("read", True), ("", False)):
        with pytest.raises(AttributeError):
            refuse(object(), attr, setting)


def test_the_real_globals_are_wiped_once_the_environment_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _spy_on_runtimes(monkeypatch)
    run_lua("return 1")
    ((_, rt),) = made
    assert list(rt.globals().keys()) == []


def test_a_python_object_that_reaches_the_script_is_refused_every_attribute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: build a verb result that is not JSON by force; the attribute filter holds."""

    class Secret:
        secret = "S-VALUE"

        @staticmethod
        def read() -> str:
            return "read-ok"

    monkeypatch.setattr(runtime.LuaTableBuilder, "build", lambda self, value, json_len=None: Secret())
    value, _ = run_lua(
        """
        local o = memory.get("p", "t")
        local ok1 = pcall(function() return o.secret end)
        local ok2 = pcall(function() return o.read() end)
        local ok3 = pcall(function() return o.__class__ end)
        local ok4 = pcall(function() o.secret = 1 end)
        return {ok1, ok2, ok3, ok4, type(o)}
        """,
        "memory.get",
        verbs={"memory.get": lambda *a: {"x": 1}},
    )
    assert value == [False, False, False, False, "userdata"]


# ---- a verb error is a string, never a Python exception ---------------------------------------


def test_a_verb_error_reaches_the_script_as_a_string() -> None:
    """design_probe case exc_attr: pcall must not hand the script a host object."""

    def boom(*args: object) -> None:
        raise ValueError("backend down")

    value, _ = run_lua(
        """
        local ok, e = pcall(memory.get, "p", "t")
        return {ok, type(e), e, e.args == nil, e.__traceback__ == nil, (pcall(e, 1))}
        """,
        "memory.get",
        verbs={"memory.get": boom},
    )
    assert value == [False, "string", "backend down", True, True, False]


def test_an_exception_the_call_path_did_not_expect_is_a_string_naming_only_its_type() -> None:
    def call_verb(name: str, args: list[object]) -> object:
        raise RuntimeError("secret detail /etc/passwd")

    value, _ = run_lua(
        'local ok, e = pcall(memory.get, "p", "t") return {ok, type(e), e}',
        "memory.get",
        call_verb=call_verb,
    )
    assert value == [False, "string", "memory.get: worker error (RuntimeError)"]


def test_an_unguarded_verb_error_ends_the_run_as_a_lua_error() -> None:
    def boom(*args: object) -> None:
        raise ValueError("backend down")

    with pytest.raises(ScriptError) as exc:
        run_lua('return memory.get("p", "t")', "memory.get", verbs={"memory.get": boom})
    assert exc.value.kind == "lua"
    assert exc.value.detail == "backend down"


def test_a_memory_error_in_call_verb_is_not_swallowed() -> None:
    """The worker exits 3 on MemoryError (the OS limit); the dispatcher must not turn it into a string."""

    def call_verb(name: str, args: list[object]) -> object:
        raise MemoryError

    with pytest.raises(MemoryError):
        run_lua(
            'local ok = pcall(memory.get, "p", "t") return ok',
            "memory.get",
            call_verb=call_verb,
        )


# ---- a verb function is opaque -----------------------------------------------------------------


def test_a_verb_function_cannot_be_opened() -> None:
    value, _ = run_lua(
        """
        local f = memory.get
        return {type(f), debug == nil, getmetatable == nil, rawget == nil,
                (pcall(function() return f.upvalue end)),
                (pcall(function() return f.__self end))}
        """,
        "memory.get",
        verbs={"memory.get": lambda *a: 1},
    )
    assert value == ["function", True, True, True, False, False]


@pytest.mark.parametrize(
    ("call", "fragment"),
    [
        ("memory.get({}, 'x')", "plain values"),
        ("memory.get(function() end, 'x')", "plain values"),
        ("memory.get(memory, 'x')", "plain values"),
        ("memory.get(0/0, 'x')", "finite"),
        ("memory.get(math.huge, 'x')", "finite"),
        ("memory.get('\\xff', 'x')", "UTF-8"),
        ("memory.get('\\xed\\xa0\\x80', 'x')", "UTF-8"),  # a lone surrogate
    ],
)
def test_a_verb_refuses_an_argument_that_is_not_a_plain_value_before_the_dispatcher(
    call: str, fragment: str
) -> None:
    calls: list[object] = []
    value, _ = run_lua(
        f"local ok, e = pcall(function() return {call} end) return {{ok, type(e), e}}",
        "memory.get",
        verbs={"memory.get": lambda *a: calls.append(a)},
    )
    assert value[0] is False
    assert value[1] == "string"
    assert fragment in value[2]
    assert calls == []


# ---- the corpus of the static check, run with the check bypassed --------------------------------

CORPUS = pathlib.Path(__file__).parents[1] / "fixtures" / "lua_static_check"

#: What the sandbox does with each refused script when the static check is bypassed.
#: ("error", fragment): a Lua error naming the missing thing; ("value", v): the script runs and
#: gets nothing the host did not give it. Scripts the check refuses for a property of their bytes
#: or grants (control char, non-ASCII, parse, grant lists) are not runtime questions.
RUNTIME_OUTCOME: dict[str, tuple[str, object]] = {
    "bad_collectgarbage": ("error", "global 'collectgarbage'"),
    "bad_coroutine_create": ("error", "global 'coroutine'"),
    "bad_coroutine_wrap": ("error", "global 'coroutine'"),
    "bad_debug_sethook": ("error", "global 'debug'"),
    "bad_debug_upvalue": ("error", "global 'debug'"),
    "bad_dofile": ("error", "global 'dofile'"),
    "bad_dump_lib": ("error", "field 'dump'"),
    "bad_dump_meta": ("error", "field 'dump'"),
    "bad_dump_method": ("error", "method 'dump'"),
    "bad_env_comment": ("value", 1),
    "bad_G": ("error", "global '_G'"),
    "bad_env_const": ("value", None),  # indexes the string "": its reduced library has no 'memory'
    "bad_env_write": ("value", 1),
    "bad_env_const_multi": ("error", "number value"),
    "bad_lib_integer_key": ("value", None),
    "bad_setfield_dump": ("value", {"dump": 1}),  # a field of the script's own table
    "bad_upvalue_dump": ("value", None),
    "bad_upvalue_dump_write": ("value", 1),
    "bad_getmetatable_string": ("error", "global 'getmetatable'"),
    "bad_lib_member": ("value", None),
    "bad_lib_namespace_unknown": ("error", "global 'os'"),
    "bad_load": ("error", "global 'load'"),
    "bad_loadstring": ("error", "global 'loadstring'"),
    "bad_rawget": ("error", "global 'rawget'"),
    "bad_rawset_env": ("error", "global 'rawset'"),
    "bad_require": ("error", "global 'require'"),
    "bad_setmetatable_shared": ("error", "global 'setmetatable'"),
}


def _corpus(stem: str) -> tuple[bytes, tuple[str, ...]]:
    raw = (CORPUS / f"{stem}.lua").read_bytes()
    grants: tuple[str, ...] = ()
    for line in raw.decode("latin-1").splitlines()[:3]:
        if line.startswith("-- grants:"):
            grants = tuple(g.strip() for g in line[len("-- grants:") :].split(",") if g.strip())
    return raw, grants


def test_the_runtime_outcome_table_names_corpus_files_that_exist() -> None:
    present = {p.stem for p in CORPUS.glob("*.lua")}
    assert set(RUNTIME_OUTCOME) <= present
    assert len(RUNTIME_OUTCOME) >= 20  # non-vacuity: the table is not quietly emptied


@pytest.mark.parametrize("stem", sorted(RUNTIME_OUTCOME))
def test_a_refused_corpus_script_gets_no_capability_when_the_check_is_bypassed(stem: str) -> None:
    source, grants = _corpus(stem)
    kind, expected = RUNTIME_OUTCOME[stem]
    if kind == "error":
        with pytest.raises(ScriptError) as exc:
            run_lua(source, *grants)
        assert exc.value.kind == "lua"
        assert expected in exc.value.detail, exc.value.detail
    else:
        value, _ = run_lua(source, *grants)
        assert value == expected


def test_verb_corpus_scripts_cannot_forge_widen_or_alias_beyond_the_grant() -> None:
    verbs = {"memory.get": lambda *a: {"content": "real"}, "memory.list": lambda *a: ["x"]}

    # bad_ns_overwrite: overwriting the verb only changes this run's own environment
    value, _ = run_lua(*(_corpus("bad_ns_overwrite")[0:1]), "memory.get", verbs=verbs)
    assert value == "forged"
    # ...and the next run has the real verb again
    value, _ = run_lua('return memory.get("p", "t").content', "memory.get", verbs=verbs)
    assert value == "real"

    # bad_verb_ungranted: the second verb has no function at all
    with pytest.raises(ScriptError) as exc:
        run_lua(*(_corpus("bad_verb_ungranted")[0:1]), "memory.get", verbs=verbs)
    assert "field 'list'" in exc.value.detail

    # bad_verb_write: no write verb exists to name
    with pytest.raises(ScriptError) as exc:
        run_lua(*(_corpus("bad_verb_write")[0:1]), "memory.get", verbs=verbs)
    assert "field 'put'" in exc.value.detail

    # bad_verb_dynamic / bad_verb_computed: a name built at run time finds nothing ungranted
    value, _ = run_lua('local v = "list" return memory[v] == nil', "memory.get", verbs=verbs)
    assert value is True

    # bad_verb_alias: an alias is the same function, not a wider one
    value, _ = run_lua(*(_corpus("bad_verb_alias")[0:1]), "memory.get", verbs=verbs)
    assert value == {"content": "real"}


def test_the_allowed_corpus_scripts_run() -> None:
    for stem in ("ok_pure", "ok_shadow", "ok_methods", "ok_lib_const_bracket", "ok_layer2_dynamic_dump"):
        source, grants = _corpus(stem)
        value, _ = run_lua(source, *grants)
        assert value is not None, stem
    value, _ = run_lua(_corpus("ok_pure")[0], )
    assert value["s"] == "1,1,2,3,5,8,13,21,34,55" and value["n"] == 3
