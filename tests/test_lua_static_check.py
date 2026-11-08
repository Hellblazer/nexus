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

lupa = pytest.importorskip("lupa")

from nexus.lua_runtime import static_check as sc  # noqa: E402 - after the importorskip

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
    assert len(CASES) >= 48 and len(oks) >= 7, (len(CASES), len(oks))


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


@pytest.mark.parametrize("ch", ["\x00", "\x07", "\x7f", "\xa0", " ", "а"])
def test_a_byte_outside_printable_ascii_is_refused_with_its_line(ch: str) -> None:
    with pytest.raises(sc.StaticCheckRefusal) as e:
        sc.check(_b(f"local a = 1\nlocal b = '{ch}'\nreturn b"), frozenset(), VERBS_V1)
    assert (e.value.rule, e.value.line) == ("bytes", 2)


@pytest.mark.parametrize("ch", ["\t", "\r", "\n"])
def test_tab_cr_and_lf_are_allowed(ch: str) -> None:
    assert _rule(f"local a = 1{ch}return a") == "ok"


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


@pytest.mark.parametrize("offset,value", [(4, 0x55), (5, 1), (12, 8), (15, 0x79), (30, 0)])
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
