# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 3: the static check over Lua 5.4's own bytecode (nexus-mdbds.6).

The corpus in ``tests/fixtures/lua_static_check/`` was ported from the 2026-11-03 spike
(``scripts/spikes/rdr242-lua-sandbox/corpus``) and extended. Each file opens with
``-- expect: <rule|ok>`` and, when it names verbs, ``-- grants: a.b, c.d``. Only this
test reads those lines: the check takes its grants as an argument and reads no header
(RDR-242 gate r3 S4; RDR-240 Technical Design 1 makes ``lua_runtime.header.parse`` the
only header reader).

The in-worker tests (the calling process never compiles the source; a refusal arrives
as ``ScriptError("static_check", ...)``) need the worker of nexus-mdbds.4 and live with it.
"""
from __future__ import annotations

import ast
import pathlib
import struct
import sys

import pytest

from nexus.lua_runtime import static_check as sc

CORPUS = pathlib.Path(__file__).parent / "fixtures" / "lua_static_check"
MODULE = pathlib.Path(sc.__file__)

#: RDR-242 Technical Design 10: the version 1 verbs, all reads.
RDR_VERBS_V1 = {
    "memory.get", "memory.list", "memory.search", "transcripts.sessions",
    "git.diff_names", "git.diff_stat", "tables.get",
}
VERBS_V1 = sc.VERBS_V1


def _case(path: pathlib.Path) -> tuple[bytes, frozenset[str], str]:
    raw = path.read_bytes()
    src = raw.decode("latin-1")
    grants: frozenset[str] = frozenset()
    expect = "ok"
    for line in src.splitlines()[:3]:
        if line.startswith("-- grants:"):
            grants = frozenset(g.strip() for g in line[len("-- grants:"):].split(",") if g.strip())
        elif line.startswith("-- expect:"):
            expect = line[len("-- expect:"):].strip()
    return raw, grants, expect


def _b(src: str | bytes) -> bytes:
    return src if isinstance(src, bytes) else src.encode("utf-8")


def _rule(src: str | bytes, grants: frozenset[str] = frozenset(), verbs: frozenset[str] = VERBS_V1) -> str:
    try:
        sc.check(_b(src), grants, verbs)
    except sc.StaticCheckRefusal as e:
        return e.rule
    return "ok"


CASES = sorted(CORPUS.glob("*.lua"))


def test_the_corpus_is_not_vacuous() -> None:
    oks = [p for p in CASES if _case(p)[2] == "ok"]
    assert len(CASES) >= 57 and len(oks) >= 8, (len(CASES), len(oks))


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_corpus_case_gets_its_expected_verdict(path: pathlib.Path) -> None:
    src, grants, expect = _case(path)
    assert _rule(src, grants) == expect


def test_every_rule_of_the_table_is_exercised() -> None:
    rules = {_case(p)[2] for p in CASES} | {"binary", "size"}
    assert sc.RULES <= rules, sorted(sc.RULES - rules)
    assert set(sc.RULES) == {
        "binary", "bytes", "size", "parse", "global", "global-write", "env", "ns-value",
        "ns-dynamic", "ns-member", "field-dump", "verb-unknown", "verb-ungranted", "grant-unused",
    }


def test_a_binary_chunk_is_refused() -> None:
    assert _rule(b"\x1bLua\x54\x00") == "binary"


def test_the_size_bound_is_64_kib_of_source() -> None:
    body = "return 1\n"
    at = "-" * (sc.MAX_SOURCE - len(body) - 1) + "\n" + body
    assert len(at) == sc.MAX_SOURCE
    assert _rule(at) == "ok"
    assert _rule("-" + at) == "size"


@pytest.mark.parametrize("ch", ["\x00", "\x07", "\x0b", "\x0c", "\x1f", "\x7f", "\xa0", "\u2028", "\u0430"])
def test_a_byte_outside_printable_ascii_is_refused_with_its_line(ch: str) -> None:
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(_b(f"local a = 1\nlocal b = '{ch}'\nreturn b"), frozenset(), VERBS_V1)
    assert (e.value.rule, e.value.line) == ("bytes", 2)


@pytest.mark.parametrize("ch", ["\t", "\r", "\n"])
def test_tab_cr_and_lf_are_allowed(ch: str) -> None:
    assert _rule(f"local a = 1{ch}return a") == "ok"


def _table_of(n: int) -> bytes:
    return ("return {" + ",".join(f'"k{i}"' for i in range(n)) + "}\n").encode()


@pytest.mark.parametrize("n,expect", [(255, "ok"), (256, "env")])
def test_the_constant_bound_is_exactly_255(n: int, expect: str) -> None:
    assert len(sc.read_chunk(sc._dump(sc._new_runtime(), _table_of(n))).k) == n
    assert _rule(_table_of(n)) == expect


def test_over_255_constants_in_one_function_is_refused_even_with_no_global() -> None:
    many = "local t = {" + ",".join(f'"k{i}"' for i in range(300)) + "}\nreturn t\n"
    assert _rule(many) == "env"
    nested = "local function f()\n" + many + "end\nreturn f()\n"
    assert _rule(nested) == "env"
    under = "local t = {" + ",".join(f'"k{i}"' for i in range(200)) + "}\nreturn t\n"
    assert _rule(under) == "ok"


def test_a_grant_outside_the_verb_table_is_verb_unknown() -> None:
    assert _rule("return memory.get('p', 't')", frozenset({"memory.get"}), frozenset()) == "verb-unknown"


def test_the_refusal_names_the_rule_and_the_line_inside_a_nested_function() -> None:
    src = "local function outer()\n  local function inner()\n\n    return load('x')\n  end\n  return inner\nend\nreturn outer"
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(_b(src), frozenset(), VERBS_V1)
    assert (e.value.rule, e.value.line) == ("global", 4)
    assert e.value.detail == "load"
    # The worker sends str(refusal) as the error frame's detail (rdr242-interfaces B).
    assert str(e.value) == "global: load (line 4)"
    assert str(sc.StaticCheckRefusal("grant-unused", "memory.get")) == "grant-unused: memory.get"


def _lua_active_lines(src: bytes) -> set[int]:
    rt = sc._new_runtime()
    got = rt.execute(
        "local f = load(...) local r = {} "
        "for k in pairs(debug.getinfo(f, 'L').activelines) do r[#r + 1] = k end "
        "return table.concat(r, ' ')", src)
    return {int(x) for x in got.decode("ascii").split()}


@pytest.mark.parametrize("gap", [0, 126, 127, 128, 300, 1000])
def test_the_line_table_matches_lua_across_loops_and_long_gaps(gap: int) -> None:
    # Loops step lines backwards (a negative delta); a gap of 128 or more takes the
    # absolute line entry. Both decode to the lines Lua itself reports.
    src = ("local t = {}\nfor i = 1, 3 do\n  t[i] = i\nend\n" + "\n" * gap
           + "while #t > 0 do\n  table.remove(t)\nend\nreturn t\n").encode()
    p = sc.read_chunk(sc._dump(sc._new_runtime(), src))
    assert set(p.lines) == _lua_active_lines(src)
    refused = src.replace(b"return t", b"return load")
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(refused, frozenset())
    assert (e.value.rule, e.value.line) == ("global", 8 + gap)


@pytest.mark.parametrize("src,line", [
    (b"local a = 1\n\nlocal _ENV = {}\nreturn 1", 3),
    (b"local function f()\n  local _ENV = {}\nend\nreturn f", 2),
    (b"local function f(x,\n _ENV) end\nreturn f", 1),
    (b"local _ENV <const> = ''\nreturn memory", 1),
    (b"return 1\n-- _ENV", 2),
])
def test_an_env_refusal_names_the_line_of_the_declaration(src: bytes, line: int) -> None:
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(src, frozenset())
    assert (e.value.rule, e.value.line) == ("env", line)


@pytest.mark.parametrize("sep", [b"\n", b"\r", b"\r\n", b"\n\r"])
def test_a_bytes_refusal_counts_lines_as_lua_does(sep: bytes) -> None:
    src = b"local a = 1" + sep + b"local b = 2" + sep + b"return '\x07'"
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(src, frozenset())
    assert (e.value.rule, e.value.line) == ("bytes", 3)


def test_a_constant_index_outside_the_table_fails_closed() -> None:
    p = sc.read_chunk(sc._dump(sc._new_runtime(), b"local t = {}\nreturn t.x"))
    p.kstr = []
    with pytest.raises(sc.ChunkFormatError):
        sc._walk(p, frozenset(), sc.VERBS_V1, {"memory"}, set())


def test_check_returns_the_verbs_the_script_names() -> None:
    assert sc.check(b"return 1", frozenset()) == frozenset()
    src = b"local a = memory.get('p', 't')\nlocal function f() return git.diff_stat('main') end\nreturn a, f()"
    grants = frozenset({"memory.get", "git.diff_stat"})
    assert sc.check(src, grants) == grants


def test_the_default_verb_table_is_the_rdr_version_1_table() -> None:
    assert set(sc.VERBS_V1) == RDR_VERBS_V1


def test_an_unknown_rule_name_cannot_be_raised() -> None:
    with pytest.raises(ValueError):
        sc.StaticCheckRefusal("no-such-rule", "x")


def test_the_check_reads_no_header() -> None:
    # The grants are an argument. A header line naming a grant changes nothing.
    src = "-- grants: memory.get\nreturn memory.get('p', 't')"
    assert _rule(src) == "verb-ungranted"
    text = MODULE.read_text(encoding="utf-8")
    # No header reader: nothing names the header line or splits the source into lines.
    assert "-- grants" not in text and "-- expect" not in text
    assert "splitlines" not in text and ".startswith(" not in text


def test_the_module_imports_only_the_stdlib_and_lupa() -> None:
    # It runs in the python -I worker with an empty environment (rdr242-interfaces B).
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            roots.add((node.module or "").split(".")[0])
    roots.discard("__future__")
    assert roots - {"lupa"} <= set(sys.stdlib_module_names), roots


def test_each_check_compiles_in_a_runtime_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[object] = []
    real = sc._new_runtime

    def counting():
        rt = real()
        made.append(rt)
        return rt

    monkeypatch.setattr(sc, "_calibrated", True)
    monkeypatch.setattr(sc, "_new_runtime", counting)
    sc.check(b"return 1", frozenset())
    sc.check(b"return 2", frozenset())
    assert len(made) == 2 and made[0] is not made[1]
    assert not any(type(v).__name__ == "LuaRuntime" for v in vars(sc).values())


def test_the_library_allowlists_are_the_real_libraries_less_dump() -> None:
    # Item 7: string without dump, and all of table, math and utf8. The runtime of
    # nexus-mdbds.5 builds its environment from these same constants.
    rt = sc._new_runtime()
    for lib, members in sc.LIB_MEMBERS.items():
        real = set(rt.execute(f"local r = {{}} for k in pairs({lib}) do r[#r + 1] = k end return table.concat(r, ' ')").decode("ascii").split())
        assert set(members) == real - {"dump"}, (lib, sorted(real ^ set(members)))
    assert "dump" not in sc.LIB_MEMBERS["string"]
    assert set(sc.BASE_FUNCTIONS) == {
        "assert", "error", "ipairs", "next", "pairs", "pcall", "select", "tonumber",
        "tostring", "type", "rawequal", "rawlen",
    }


# ---- calibration: fails on any change to the bytecode layout or opcode numbering ----

def test_calibration_passes_on_the_pinned_lupa() -> None:
    sc.calibrate()


def test_check_calibrates_once_before_its_first_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def broken() -> None:
        calls.append(1)
        raise sc.ChunkFormatError("drift")

    monkeypatch.setattr(sc, "_calibrated", False)
    monkeypatch.setattr(sc, "calibrate", broken)
    with pytest.raises(sc.ChunkFormatError):
        sc.check(b"return 1", frozenset())
    monkeypatch.setattr(sc, "calibrate", lambda: calls.append(2))
    sc.check(b"return 1", frozenset())
    sc.check(b"return 2", frozenset())
    assert calls == [1, 2]


@pytest.mark.parametrize("shift", ["swap_b_c", "a_from_k", "b_off_by_one"])
def test_calibration_fails_when_the_operand_layout_moves(monkeypatch: pytest.MonkeyPatch, shift: str) -> None:
    real = sc._decode

    def moved(ins: int):
        op, a, k, b, c = real(ins)
        if shift == "swap_b_c":
            return op, a, k, c, b
        if shift == "a_from_k":
            return op, (ins >> 8) & 0xFF, k, b, c
        return op, a, k, (b + 1) & 0xFF, c

    monkeypatch.setattr(sc, "_decode", moved)
    with pytest.raises(sc.ChunkFormatError):
        sc.calibrate()


def test_calibration_fails_when_a_rule_stops_refusing_its_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "REFUSED_FIELDS", frozenset())
    with pytest.raises(sc.ChunkFormatError):
        sc.calibrate()


def test_calibration_fails_when_the_opcode_numbering_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    names = list(sc.OPNAMES)
    i, j = names.index("GETFIELD"), names.index("GETTABLE")
    names[i], names[j] = names[j], names[i]
    monkeypatch.setattr(sc, "OPNAMES", tuple(names))
    with pytest.raises(sc.ChunkFormatError):
        sc.calibrate()


def test_calibration_fails_when_an_opcode_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    names = list(sc.OPNAMES)
    names.insert(names.index("CALL"), "NEWOP")
    monkeypatch.setattr(sc, "OPNAMES", tuple(names))
    with pytest.raises(sc.ChunkFormatError):
        sc.calibrate()


@pytest.mark.parametrize("offset,value", [
    (0, 0x1c), (4, 0x55), (5, 1), (6, 0x18), (7, 0x92), (8, 0x0a), (9, 0x0d), (10, 0x1b), (11, 0x0d),
    (12, 8), (13, 4), (14, 4), (15, 0x79), (30, 0),
])
def test_the_reader_refuses_a_changed_chunk_header(offset: int, value: int) -> None:
    blob = bytearray(sc._dump(sc._new_runtime(), b"return 1"))
    blob[offset] = value
    with pytest.raises(sc.ChunkFormatError):
        sc.read_chunk(bytes(blob))


def test_the_reader_refuses_trailing_bytes_and_a_stripped_chunk() -> None:
    rt = sc._new_runtime()
    blob = sc._dump(rt, b"local x = 1 return function() return x end")
    with pytest.raises(sc.ChunkFormatError):
        sc.read_chunk(blob + b"\x00")
    stripped = rt.execute("return string.dump(load('local x = 1 return function() return x end', '=s', 't', {}), true)")
    with pytest.raises(sc.ChunkFormatError):
        sc.read_chunk(stripped)


def test_the_reader_decodes_lines_constants_and_nested_prototypes() -> None:
    p = sc.read_chunk(sc._dump(sc._new_runtime(), b"local a = 'x'\nlocal f = function()\n  return 2.5\nend\nreturn a, f"))
    assert b"x" in p.k
    assert len(p.protos) == 1
    q = p.protos[0]
    assert q.linedefined == 2
    assert struct.pack("<d", 2.5) in q.k
    assert 3 in q.lines


# ---- the bytecode rules on their own (the _ENV token check would otherwise shadow them) ----

def _walk_rule(src: bytes, grants: frozenset[str] = frozenset()) -> tuple[str, str]:
    p = sc.read_chunk(sc._dump(sc._new_runtime(), src))
    try:
        sc._walk(p, grants, sc.VERBS_V1, {v.split(".")[0] for v in sc.VERBS_V1}, set())
    except sc.StaticCheckRefusal as e:
        return e.rule, e.detail
    return "ok", ""


@pytest.mark.parametrize("src", [
    b"_ENV = {}\nreturn 1",
    b"local function f() _ENV = nil end\nreturn f",
    b"local function f() return _ENV end\nreturn f",
])
def test_the_walk_refuses_env_as_a_value_without_the_token_check(src: bytes) -> None:
    assert _walk_rule(src) == ("env", "_ENV used as a value")


def _iabc(op: str, a: int, b: int, c: int, k: int = 0) -> int:
    return sc.OPNAMES.index(op) | a << 7 | k << 15 | b << 16 | c << 24


def test_a_namespace_field_read_from_another_register_is_not_a_member_access() -> None:
    # GETTABUP R0 := _ENV.memory, then GETFIELD R0 := R1.get: the field is read from R1,
    # not from the namespace, so R0 holding memory is a namespace used as a value.
    p = sc.Proto(linedefined=0, code=[_iabc("GETTABUP", 0, 0, 0), _iabc("GETFIELD", 0, 1, 1)],
                 k=[b"memory", b"get"], kstr=["memory", "get"], upnames=[b"_ENV"], lines=[1, 1])
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc._walk(p, frozenset({"memory.get"}), sc.VERBS_V1, {"memory"}, set())
    assert e.value.rule == "ns-value"


def test_calibration_fails_when_a_probe_is_refused_under_another_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sc, "_CALIBRATION_PROBES", ((b"return load", "env"),))
    with pytest.raises(sc.ChunkFormatError):
        sc.calibrate()
