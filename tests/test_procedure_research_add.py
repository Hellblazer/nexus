# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The research_add procedure (RDR-240 Technical Design 7, nexus-qo7ie.33).

Until the runtime of nexus-mdbds.5 lands, the script runs here in a test-local harness
that builds the environment Technical Design 7 of RDR-242 describes from the static
check's own allowlists (the base functions; string without dump, with the string
metatable's __index the reduced table; table, math, utf8; the granted verbs), loads it
in mode "t" and passes the arguments as the main chunk's vararg (contract v6). The
header is read by ``header.parse`` and the static check passes the file with those
grants, exactly as run_script will. The run_script-level tests and the memory_put round
trip run once the runtime lands (recorded on the bead).
"""
from __future__ import annotations

import time
from pathlib import Path

import lupa.lua54 as lua54
import pytest

from nexus.commands import rdr as rdr_cmd
from nexus.lua_runtime import header as hd
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime.verb_types import VerbContext
from nexus.lua_runtime.verbs_tables import VERBS as TABLE_VERBS
from nexus.tables.rdr_research_vocab import CLASSIFICATIONS, METHODS

SCRIPT = Path(hd.__file__).parent.parent / "procedures" / "research_add.lua"
SOURCE = SCRIPT.read_bytes()


class _Run:
    """One script run in a sandbox-shaped environment, with fake verbs."""

    def __init__(self, titles: list[str]) -> None:
        self.titles = titles
        self.list_calls: list[str] = []
        self.rt = lua54.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=False)

    def _memory_list(self, project: str) -> object:
        self.list_calls.append(project)
        rows = [t if isinstance(t, dict) else {"title": t, "content": ""} for t in self.titles]
        return self.rt.table_from(rows, recursive=True)

    def _tables_get(self, name: str) -> object:
        # The real verb, so the script's reading of its result shape is pinned against it.
        (spec,) = [v for v in TABLE_VERBS if v.name == "tables.get"]
        value = spec.fn(name, deadline=time.monotonic() + 30, ctx=VerbContext(repo_root=None))
        return self.rt.table_from(value, recursive=True)

    def __call__(self, **args: object) -> dict:
        rt = self.rt
        build = rt.execute(
            """
            return function(base, libs, memory_list, tables_get)
            local env = {}
            for _, name in ipairs(base) do env[name] = _G[name] end
            for lib, members in pairs(libs) do
              local t = {}
              for _, m in ipairs(members) do t[m] = _G[lib][m] end
              env[lib] = t
            end
            getmetatable("").__index = env.string
            env.memory = {list = function(p) return memory_list(p) end}
            env.tables = {get = function(n) return tables_get(n) end}
            return env
            end
            """
        )
        env = build(
            rt.table_from(sorted(sc.BASE_FUNCTIONS)),
            rt.table_from({lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}),
            self._memory_list,
            self._tables_get,
        )
        load = rt.eval("load")
        loaded = load(SOURCE.decode("ascii"), "=research_add", "t", env)
        fn, err = loaded if isinstance(loaded, tuple) else (loaded, None)
        assert fn is not None, err
        out = fn(rt.table_from(args))
        return dict(out.items())


ARGS = {"rdr": 240, "classification": "verified", "method": "source_search", "finding": "x", "repo": "nexus"}


def test_the_header_parses_and_the_static_check_passes_with_its_grants() -> None:
    h = hd.parse(SOURCE, name="research_add")
    assert [a.name for a in h.args] == ["rdr", "classification", "method", "finding", "repo"]
    assert h.grants == frozenset({"memory.list", "tables.get"})
    assert sc.check(SOURCE, h.grants) == h.grants


def test_the_reserved_repo_is_declared_and_now_is_not() -> None:
    names = {a.name: a for a in hd.parse(SOURCE, name="research_add").args}
    assert names["repo"] == hd.ArgSpec("repo", "string", False)
    assert "now" not in names


def test_the_next_sequence_follows_the_rdrs_highest() -> None:
    run = _Run(["240-research-3", "240-research-10", "0240-research-2", "24-research-99",
                "2400-research-50", "1240-research-77", "240-research-x", "240-gate-latest",
                "240-research-50b", "x240-research-60", "240-research-16: the Key Discoveries bullet",
                "240-research-90 :space before the colon", {"content": "a row with no title"}])
    out = run(**ARGS)
    assert out["title"] == "240-research-17"
    assert out["project"] == "nexus_rdr"
    assert run.list_calls == ["nexus_rdr"]


def test_padding_is_one_rdr_and_the_title_is_padded_to_three() -> None:
    out = _Run(["097-research-9", "97-research-12", "0097-research-4"])(**{**ARGS, "rdr": 97})
    assert out["title"] == "097-research-13"
    assert out["content"].startswith("rdr_id: 097\nseq: 13\n")


def test_the_first_record_of_an_rdr_is_seq_1() -> None:
    assert _Run([])(**ARGS)["title"] == "240-research-1"


def test_a_finding_with_shell_metacharacters_comes_back_verbatim() -> None:
    finding = "ran `rm -rf $(pwd)`; echo $HOME && x=\"a b\" | tee /dev/null # not a comment"
    out = _Run([])(**{**ARGS, "finding": finding})
    assert out["content"].endswith(f"finding: {finding}\n")
    assert rdr_cmd._preamble_parse_t2_field(out["content"], "finding") == finding


def test_the_finding_is_trimmed_as_nx_rdr_trims_it() -> None:
    out = _Run([])(**{**ARGS, "finding": " \t padded finding \t "})
    assert out["content"].endswith("finding: padded finding\n")


def test_the_record_is_the_one_nx_rdr_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[dict] = []

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def get_all(self, project: str) -> list[dict]:
            return [{"title": "097-research-4"}]

        def get(self, project: str, title: str) -> None:
            return None

        def put(self, **kw: object) -> None:
            written.append(kw)

    monkeypatch.setattr(rdr_cmd, "_t2_client_factory", lambda: _Client())
    rdr_cmd._rdr_research_add("097", "a finding; with `ticks`", "nexus", classification="assumed", method="spike")
    out = _Run(["097-research-4"])(rdr=97, classification="assumed", method="spike",
                                   finding="a finding; with `ticks`", repo="nexus")
    (cli,) = written
    assert (out["project"], out["title"], out["content"], out["tags"]) == (
        cli["project"], cli["title"], cli["content"], cli["tags"])


@pytest.mark.parametrize("field,value,words", [
    ("classification", "guessed", "classification must be one of verified, documented, assumed; got guessed"),
    ("method", "intuition", "method must be one of source_search, spike, docs_only; got intuition"),
    ("classification", "Verified", "classification must be one of"),
    ("rdr", 0, "rdr must be a positive integer"),
    ("finding", "", "finding must not be empty"),
    ("finding", " \t ", "finding must not be empty"),
    ("finding", "line one\nacknowledged: true", "finding must be one line"),
    ("finding", "line one\rline two", "finding must be one line"),
])
def test_a_bad_value_is_refused_naming_it(field: str, value: object, words: str) -> None:
    run = _Run(["240-research-1"])
    with pytest.raises(lua54.LuaError) as e:
        run(**{**ARGS, field: value})
    assert words in str(e.value)


def test_a_vocabulary_refusal_happens_before_any_listing() -> None:
    run = _Run([])
    with pytest.raises(lua54.LuaError):
        run(**{**ARGS, "method": "intuition"})
    assert run.list_calls == []


def test_the_script_reads_the_vocabulary_from_the_table_not_a_copy() -> None:
    text = SOURCE.decode("ascii")
    for word in (*CLASSIFICATIONS, *METHODS):
        assert f'"{word}"' not in text


@pytest.mark.parametrize("title", ["240-research-9223372036854775807", "240-research-99999999999999999999"])
def test_a_sequence_number_out_of_range_is_refused_not_wrapped(title: str) -> None:
    with pytest.raises(lua54.LuaError) as e:
        _Run([title])(**ARGS)
    assert "out of range" in str(e.value)


@pytest.mark.parametrize("titles", [
    ["097-research-4"],
    ["097-research-4", "97-research-12"],
    ["097-research-4", "097-research-20: a summary suffix"],
])
def test_the_procedure_and_nx_rdr_take_the_same_number(titles: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    # nx rdr now counts any zero padding of the id, as the gate reader and the procedure do.
    entries = [{"title": t} for t in titles]
    cli = rdr_cmd._rdr_research_next_seq(entries, "097")
    out = _Run(titles)(rdr=97, classification="assumed", method="spike", finding="x", repo="nexus")
    assert out["title"] == f"097-research-{cli}"
