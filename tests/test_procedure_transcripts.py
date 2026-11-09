# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The transcripts procedure (RDR-240 Technical Design 5, nexus-qo7ie.31): its seven queries,
day stepping, the hour split, paging, and the Python page join.

Until the runtime of nexus-mdbds.5 lands, the script runs in a test-local harness (as
tests/test_procedure_research_add.py's): the environment is built from the static check's own
allowlists, the script is loaded in mode "t", and its arguments arrive as the main chunk's
vararg. ``transcripts.sessions`` is a fake that serves fixture records built here, per
window, and counts its calls; a few tests use the real verb over fixture JSONL files instead.
The paging tests patch a bound constant in the source text so a page fills with a small
fixture; one test per bound runs at the real value.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import lupa.lua54 as lua54
import pytest

from nexus.lua_runtime import header as hd
from nexus.lua_runtime import static_check as sc
from nexus.lua_runtime import transcripts_join as tj
from nexus.lua_runtime import verbs_transcripts as vt
from nexus.lua_runtime.verb_types import VerbContext, VerbError
from nexus.operators.llm_models import PRICES
from tests.lua_runtime.test_verbs_transcripts import QUERIES as PINNED_FIELDS  # the verb's pin of each query's fields

SCRIPT = Path(hd.__file__).parent.parent / "procedures" / "transcripts.lua"
SOURCE = SCRIPT.read_bytes()
TEXT = SOURCE.decode("ascii")
NOW = "2026-11-08T12:00:00Z"
EM = "—"
MEM = "/h/.claude/projects/x/memory"
GET = "mcp__plugin_conexus_nexus__memory_get"
SEARCH = "mcp__plugin_conexus_nexus__memory_search"
PUT = "mcp__plugin_conexus_nexus__memory_put"
RUN = "mcp__plugin_conexus_nexus__procedure_run"


# ---- fixtures: records in the shape transcripts.sessions returns --------------------------


def call(name: str, inp: dict | None = None, t: str = "10:01:00", *, err: bool | None = None,
         day: str = "2026-11-03") -> dict:
    return {"name": name, "input": inp or {}, "time": f"{day}T{t}Z",
            "result_time": f"{day}T{t}Z", "is_error": err}


def rec(sid: str, start: str, *, main: bool = True, calls: list | None = None, agent: str | None = None,
        models: list | None = None, turns: int = 3, ctx: int | None = 1000, tokens: dict | None = None,
        loads: list | None = None, slash: list | None = None, deferred: list | None = None,
        text: list | None = None, truncated: bool = False, project: str = "p") -> dict:
    r: dict[str, Any] = {
        "session_id": sid, "main": main, "agent_id": agent, "project": project,
        "start": start, "end": start, "models": models or ["claude-opus-4-8"],
        "assistant_turns": turns,
        "tokens": tokens or {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "first_context_tokens": ctx, "tool_calls": calls or [], "instruction_loads": loads or [],
        "slash_commands": slash or [], "deferred_tools": deferred or [], "deferred_tools_dropped": 0,
        "bytes": 10, "records": 3, "truncated": truncated,
    }
    if text is not None:
        r["assistant_text"] = text
    return r


def load(path: str, size: int, *, ftype: str = "Project", kind: str = "instructions") -> dict:
    return {"path": path, "kind": kind, "type": ftype, "size": size, "time": "2026-11-03T10:00:03Z"}


JOINED = "deferred_tools"  # a read of these alone would be a second read of a window, which no query makes


def _call_shape(c: dict, f: vt._Fields) -> dict:
    """One fixture tool call as the real verb returns it under ``f``."""
    keys = f.keys["tool_calls"]
    out = {k: c[k] for k in ("name", "time", "result_time", "is_error") if k in keys}
    if f.wants_input(c["name"]):
        out["input"] = c["input"]
    if "input_bytes" in keys:  # measured from the whole input, before any filter
        out["input_bytes"] = vt._input_bytes(c["input"])
    return out


def shape(r: dict, f: vt._Fields) -> dict:
    """A fixture record as the real verb returns it under ``f``: an unrequested field is dropped
    (the four always-returned ones are in ``f.top``), a list keeps only its selected sub-keys, an
    input rides only on a call an ``input@`` form names, and ``tool_call_count`` is the number of
    calls before any of that (a fixture may set it above its own list, as a cut record's is)."""
    full = {**r, "tool_call_count": r.get("tool_call_count", len(r.get("tool_calls", [])))}
    out: dict[str, Any] = {}
    for k, v in full.items():
        if k not in f.top:
            continue
        keys = f.keys.get(k)
        if keys is None:
            out[k] = v
        elif k == "tool_calls":
            out[k] = [_call_shape(c, f) for c in v]
        else:
            out[k] = [{sk: sv for sk, sv in e.items() if sk in keys} for e in v]
    return out


class Sessions:
    """A fake transcripts.sessions that filters as the real verb does: window by start, ``fields``
    parsed by the real parser and applied by ``shape`` (nil is every field; assistant_text only
    when bodies is true), calls counted. ``calls`` holds the window reads of a run, ``joins`` any
    read of ``deferred_tools`` alone (none: one read a unit); ``asked`` holds the ``fields``
    of every call in order."""

    def __init__(self, records: list[dict], *, max_records: int | None = None) -> None:
        self.records = records
        self.max_records = max_records
        self.calls: list[tuple] = []
        self.joins: list[tuple] = []
        self.asked: list[str | None] = []

    def __call__(self, since: str, until: str, project: str | None, bodies: bool | None,
                 fields: str | None = None) -> Any:
        (self.joins if fields == JOINED else self.calls).append((since, until, project, bodies))
        self.asked.append(fields)
        f = vt._parse_fields(fields, bool(bodies))  # an unknown field is a VerbArgError, as in the verb
        lo, hi = vt._bound(since, "since"), vt._bound(until, "until")
        got = sorted((r for r in self.records if lo <= vt._bound(r["start"], "start") < hi),
                     key=lambda r: r["start"])
        if self.max_records is not None and len(got) > self.max_records:
            return f"result-too-large: transcripts.sessions: {len(got)} records; narrow the window"
        return [shape(r, f) for r in got]


class Run:
    """One script run in a sandbox-shaped environment with a fake (or real) verb."""

    def __init__(self, verb: Callable[..., Any], *, patch: dict[str, str] | None = None,
                 count: bool = False) -> None:
        self.verb = verb
        self.count = count
        self.instructions = 0  # Lua VM instructions of the last call, when count is set
        self.source = TEXT
        for name, value in (patch or {}).items():  # a bound constant, patched in the text
            new, n = re.subn(rf"^local {name} = .*$", f"local {name} = {value}", self.source, flags=re.M)
            assert n == 1, name
            self.source = new
        self.rt = lua54.LuaRuntime(register_eval=False, register_builtins=False, unpack_returned_tuples=False)

    def _sessions(self, since: str, until: str, project: Any, bodies: Any, fields: Any = None) -> Any:
        try:
            out = self.verb(since, until, project, bodies, fields)
        except VerbError as exc:
            return str(exc)
        return out if isinstance(out, str) else self.rt.table_from(out, recursive=True)

    def __call__(self, **args: object) -> dict:
        rt = self.rt
        build = rt.execute(
            """
            return function(base, libs, sessions)
            local env = {}
            for _, name in ipairs(base) do env[name] = _G[name] end
            for lib, members in pairs(libs) do
              local t = {}
              for _, m in ipairs(members) do t[m] = _G[lib][m] end
              env[lib] = t
            end
            getmetatable("").__index = env.string
            env.transcripts = {sessions = function(a, b, p, bo, fi)
              local r = sessions(a, b, p, bo, fi)
              if type(r) == "string" then error(r, 0) end
              return r
            end}
            return env
            end
            """
        )
        env = build(
            rt.table_from(sorted(sc.BASE_FUNCTIONS)),
            rt.table_from({lib: rt.table_from(sorted(m)) for lib, m in sc.LIB_MEMBERS.items()}),
            self._sessions,
        )
        loaded = rt.eval("load")(self.source, "=transcripts", "t", env)
        fn, err = loaded if isinstance(loaded, tuple) else (loaded, None)
        assert fn is not None, err
        given = {"since": "2026-11-03", "now": NOW, **args}
        table = rt.table_from({k: v for k, v in given.items() if v is not None})
        if self.count:  # the hook lives here, never in the script (which has no debug library)
            rt.execute("COUNT = 0; debug.sethook(function() COUNT = COUNT + 1 end, '', 1000)")
        try:
            result = fn(table)
        finally:
            if self.count:
                rt.execute("debug.sethook()")
                self.instructions = int(rt.globals().COUNT) * 1000
        return _py(result)


def _py(x: Any) -> Any:
    if lua54.lua_type(x) != "table":
        return x
    keys = list(x.keys())
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_py(x[k]) for k in sorted(keys)]
    if not keys:
        return []
    return {k: _py(v) for k, v in x.items()}


def run(records: list[dict], query: str, **kw: Any) -> tuple[dict, Sessions]:
    fake = Sessions(records, max_records=kw.pop("max_records", None))
    out = Run(fake, patch=kw.pop("patch", None))(query=query, **kw)
    return out, fake


def measure(records: list[dict], query: str, **kw: Any) -> tuple[dict, int]:
    """One run and the Lua VM instructions it took (to the nearest thousand)."""
    runner = Run(Sessions(records), patch=kw.pop("patch", None), count=True)
    out = runner(query=query, **kw)
    return out, runner.instructions


def by(rows: list[dict], *keys: str) -> dict[tuple, dict]:
    return {tuple(r.get(k) for k in keys): r for r in rows}


# ---- the script itself ----------------------------------------------------------------------


def test_the_header_parses_and_the_static_check_passes_with_its_grants() -> None:
    h = hd.parse(SOURCE, name="transcripts")
    assert [(a.name, a.type, a.optional) for a in h.args] == [
        ("query", "string", False), ("since", "string", False), ("until", "string", True),
        ("project", "string", True), ("now", "string", False)]
    assert h.grants == frozenset({"transcripts.sessions"})
    assert sc.check(SOURCE, h.grants) == h.grants


def test_the_source_is_printable_ascii_and_never_names_the_environment() -> None:
    assert all(c in (9, 10, 13) or 32 <= c <= 126 for c in SOURCE)
    assert "_ENV" not in TEXT
    assert len(SOURCE) < sc.MAX_SOURCE * 3 // 4  # headroom under the static check's 64 KiB (was half)


def test_the_cost_is_an_estimate_from_one_dated_price_table() -> None:
    assert TEXT.count("2026-10-23") == 2  # the header comment and the table's own
    assert "cost_estimate_usd" in TEXT and "ESTIMATE" in TEXT
    assert TEXT.count('["claude-opus-4-8"]') == 1


def test_an_unknown_query_and_bad_times_are_refused() -> None:
    with pytest.raises(lua54.LuaError, match="query must be one of"):
        run([], "nope")
    for bad in ("2026-13-01", "2026-02-30", "yesterday", "2026-11-03T10:00:00+01:00",
                "2026-11-03T10:00:00.5Z", "2026-11-03T24:00:00Z"):
        with pytest.raises(lua54.LuaError, match="since"):
            run([], "tools", since=bad)
    with pytest.raises(lua54.LuaError, match="until must"):
        run([], "tools", until="tomorrow")


def test_to_defaults_to_now_and_until_and_since_are_the_values_used() -> None:
    out, fake = run([], "tools", since="2026-11-07")
    assert (out["until"], out["since"]) == (NOW, "2026-11-07")
    assert fake.calls == [("2026-11-07", "2026-11-08", None, False),
                          ("2026-11-08", "2026-11-08T12:00:00Z", None, False)]
    out, _ = run([], "tools", since="2026-11-07", until="2026-11-07T06:00:00Z")
    assert out["until"] == "2026-11-07T06:00:00Z"
    assert "next_since" not in out


def test_a_window_that_does_not_start_before_it_ends_is_an_error_and_makes_no_call() -> None:
    for since, until in (("2026-11-05", "2026-11-05"), ("2026-11-06", "2026-11-05"),
                         ("2026-11-05T10:00:00Z", "2026-11-05T10:00:00Z"), ("2026-11-09", None)):
        fake = Sessions([])
        with pytest.raises(lua54.LuaError, match="since must be before until"):
            Run(fake)(query="tools", since=since, until=until)
        assert fake.calls == []
    run([], "tools", since="2026-11-05", until="2026-11-05T00:00:01Z")  # one second is a window


# ---- the queries, against one fixture with known counts -------------------------------------

DAY = "2026-11-03"


def fixture_day() -> list[dict]:
    m1 = rec(
        "m1", f"{DAY}T10:00:00Z", turns=60,
        calls=[
            call("Read", {"file_path": f"{MEM}/feedback_a.md"}, "10:01:00"),
            call("Read", {"file_path": f"{MEM}/feedback_a.md"}, "10:02:00"),
            call("Read", {"file_path": "/h/src/not_memory.py"}),
            call("Edit", {"file_path": "/h/.claude/projects/x/MEMORY.md"}, "10:03:00"),
            call(GET, {"project": "nexus", "title": "t1"}, "10:04:00"),
            call(GET, {"project": "nexus", "title": "t1"}, "10:05:00"),
            call(SEARCH, {"project": "nexus", "query": "q"}, "10:06:00"),
            call(SEARCH, {"query": "no project"}, "10:06:30"),
            call("mcp__plugin_conexus_nexus__search", {"query": "a"}, "10:07:00"),
            call("mcp__plugin_conexus_nexus__search", {"query": "b"}, "10:08:00"),
            call("mcp__plugin_conexus_nexus__search", {"query": "c"}, "10:09:00"),
            call("WebFetch", {"url": "u"}, "10:10:00"),
            call("Bash", {"command": "nx memory curate --cold && uv run nx store put x"}, "10:11:00"),
            call("Bash", {"command": "nx run transcripts --args-file -"}, "10:12:00"),
            call("Bash", {"command": "git stash"}, "10:13:00"),
            call("Bash", {"command": "cat ~/.claude/projects/x/y.jsonl | jq ."}, "10:14:00"),
            call("Skill", {"skill": "conexus:orchestration"}, "10:15:00"),
            call("Skill", {"skill": "conexus:orchestration"}, "10:16:00"),
            call("Agent", {"subagent_type": "developer", "model": "opus"}, "10:17:00"),
            call("Agent", {"subagent_type": "developer"}, "10:18:00"),
            call("Task", {}, "10:19:00", err=True),
            call(RUN, {"name": "gate_record"}, "10:20:00"),
            call(PUT, {"title": "review-abc-b-2026", "tags": "kind:gate,bead:b"}, "10:21:00"),
            call(PUT, {"title": "review-abd-b-2026", "tags": "kind:gate,bead:b"}, "10:22:00"),
            call(PUT, {"title": "240-research-9", "content": "x"}, "10:23:00"),
        ],
        deferred=["WebFetch", "mcp__x__y", "Unused"],
        loads=[load("/r/CLAUDE.md", 900), load("/r/CLAUDE.md", 1000), load("/r/rules/a.md", 200, ftype="Local")],
        slash=[{"name": "commit", "time": f"{DAY}T10:30:00Z"}],
        text=[{"time": f"{DAY}T10:40:00Z", "text": f"plain {EM} dash"},
              {"time": f"{DAY}T10:41:00Z", "text": f"```\n{EM}\n``` and `{EM}` only"}],
    )
    m2 = rec("m2", f"{DAY}T11:00:00Z", turns=60, text=[{"time": f"{DAY}T11:01:00Z", "text": "clean"}])
    s1 = rec(
        "m1", f"{DAY}T10:05:00Z", main=False, agent="a1", turns=4, ctx=5000,
        tokens={"input": 1_000_000, "output": 1_000_000, "cache_read": 0, "cache_creation": 0},
        calls=[call("Read", {"file_path": "/x"}, err=False), call("Bash", {"command": "false"}, err=True)],
        loads=[load("/r/CLAUDE.md", 1000)],
    )
    s2 = rec("m1", f"{DAY}T12:00:00Z", main=False, agent="a2", turns=2, ctx=3000, models=["claude-unknown-9"],
             tokens={"input": 10, "output": 10, "cache_read": 0, "cache_creation": 0})
    return [m1, m2, s1, s2]


def test_agents_counts_dispatches_and_runs_and_labels_the_cost_an_estimate() -> None:
    out, _ = run(fixture_day(), "agents", until="2026-11-04")
    rows = by(out["rows"], "kind", "agent_type", "model")
    d = rows[("dispatch", "developer", "opus")]
    assert (d["dispatches"], d["errors"], d["runs"]) == (1, 0, 0)
    assert rows[("dispatch", "developer", "(default)")]["dispatches"] == 1
    t = rows[("dispatch", "(default)", "(default)")]
    assert (t["dispatches"], t["errors"]) == (1, 1)
    r1 = rows[("run", "(unattributed)", "claude-opus-4-8")]
    assert (r1["runs"], r1["turns"], r1["errors"]) == (1, 4, 1)
    assert (r1["context_total"], r1["context_count"], r1["max_context"]) == (5000, 1, 5000)
    assert r1["cost_estimate_usd"] == pytest.approx(5.0 + 25.0)  # 1M input at 5, 1M output at 25
    # s1 starts before any dispatch of its session; s2 (12:00) takes the first one, the developer on opus
    r2 = rows[("run", "developer", "opus")]
    assert (r2["unpriced"], r2["cost_estimate_usd"], r2["context_total"]) == (1, 0, 3000)
    assert set(r1) == tj.SHAPES["agents"].fields


def test_the_lua_price_table_equals_the_python_one_for_every_model_it_lists() -> None:
    listed = set(re.findall(r'\["(claude-[a-z0-9\-]+)"\] = \{', TEXT))
    assert listed == set(PRICES)
    for i, field in enumerate(("input", "output", "cache_creation", "cache_read")):
        records = [rec(f"s{m}", f"{DAY}T10:00:00Z", main=False, agent=f"a{m}", models=[model],
                       tokens={"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, field: 1_000_000})
                   for m, model in enumerate(sorted(PRICES))]
        out, _ = run(records, "agents", until="2026-11-04")
        got = {r["model"]: r["cost_estimate_usd"] for r in out["rows"]}
        want = {m: (p.input, p.output, p.cache_write, p.cache_read)[i] for m, p in PRICES.items()}
        assert got == pytest.approx(want), field
        assert all(r["unpriced"] == 0 for r in out["rows"])


def test_a_dated_model_id_prices_as_its_base_model() -> None:
    s = rec("s", f"{DAY}T10:00:00Z", main=False, agent="a", models=["claude-haiku-4-5-20251001"],
            tokens={"input": 2_000_000, "output": 0, "cache_read": 0, "cache_creation": 0})
    out, _ = run([s], "agents", until="2026-11-04")
    (row,) = out["rows"]
    assert row["cost_estimate_usd"] == pytest.approx(2.0) and row["unpriced"] == 0


def _sub(sid: str, start: str, agent: str, model: str = "claude-opus-4-8", **kw: Any) -> dict:
    return rec(sid, f"{DAY}T{start}Z", main=False, agent=agent, models=[model], **kw)


def test_a_subagent_run_takes_the_first_unmatched_dispatch_at_or_before_its_start() -> None:
    parent = rec("P", f"{DAY}T10:00:00Z", calls=[
        call("Agent", {"subagent_type": "reviewer", "model": "opus"}, "10:01:00"),
        call("Task", {"subagent_type": "developer"}, "10:02:00"),
        call("Agent", {"subagent_type": "never-ran"}, "10:30:00"),
        call("Read", {"file_path": "/x"}, "10:01:30")])
    def one_m() -> dict:  # a fresh dict each: lupa does not convert one shared dict twice
        return {"input": 1_000_000, "output": 0, "cache_read": 0, "cache_creation": 0}

    subs = [
        _sub("P", "10:00:30", "a0"),  # before any dispatch: nothing at or before its start
        _sub("P", "10:01:05", "a1", tokens=one_m()),  # reviewer, asked for opus, ran opus-4-8
        _sub("P", "10:02:07", "a2", "claude-sonnet-4-6", tokens=one_m()),  # developer, no model asked
        _sub("P", "10:02:09", "a3"),  # the next dispatch (10:30) is after its start
        _sub("Q", "10:05:00", "q1"),  # a session with no main record in the unit
    ]
    out, _ = run([parent, *subs], "agents", until="2026-11-04")
    rows = by(out["rows"], "kind", "agent_type", "model")
    a1 = rows[("run", "reviewer", "opus")]
    assert a1["runs"] == 1 and a1["cost_estimate_usd"] == pytest.approx(5.0)  # priced by the model it ran
    a2 = rows[("run", "developer", "claude-sonnet-4-6")]
    assert a2["runs"] == 1 and a2["cost_estimate_usd"] == pytest.approx(3.0)
    assert rows[("run", "(unattributed)", "claude-opus-4-8")]["runs"] == 3  # a0, a3, q1
    assert rows[("dispatch", "never-ran", "(default)")]["dispatches"] == 1
    assert sum(r["runs"] for r in out["rows"]) == 5


def test_attribution_orders_by_parsed_time_so_a_fraction_is_not_misread() -> None:
    parent = rec("P", f"{DAY}T10:00:00Z", calls=[call("Agent", {"subagent_type": "t"}, "10:01:00.500")])
    early = _sub("P", "10:01:00.250", "a1")  # before the dispatch by parsed time
    late = _sub("P", "10:01:00.900", "a2")
    out, _ = run([parent, early, late], "agents", until="2026-11-04")
    rows = by(out["rows"], "kind", "agent_type")
    assert rows[("run", "(unattributed)")]["runs"] == 1 and rows[("run", "t")]["runs"] == 1
    whole = _sub("P", "10:01:01", "a3")  # no fraction, half a second after the dispatch
    out, _ = run([parent, whole], "agents", until="2026-11-04")
    assert by(out["rows"], "kind", "agent_type")[("run", "t")]["runs"] == 1


def test_a_subagent_in_another_unit_than_its_parent_stays_unattributed() -> None:
    parent = rec("P", f"{DAY}T10:00:00Z", calls=[call("Agent", {"subagent_type": "t"}, "10:01:00")])
    child = rec("P", "2026-11-04T00:05:00Z", main=False, agent="a1")
    out, _ = run([parent, child], "agents", until="2026-11-05")
    assert by(out["rows"], "kind", "agent_type")[("run", "(unattributed)")]["runs"] == 1


def test_tools_counts_per_tool_server_and_nx_verb_with_deferred_against_called() -> None:
    out, _ = run(fixture_day(), "tools", until="2026-11-04")
    m1 = [r for r in out["rows"] if r["session_id"] == "m1" and r["main"]]
    rows = by(m1, "kind", "name", "sub")
    s = rows[("tool", "mcp__plugin_conexus_nexus__search", None)]
    assert (s["count"], s["server"], s["last_use"]) == (3, "plugin_conexus_nexus", f"{DAY}T10:09:00Z")
    assert s["size"] == 3  # the bytes of the string inputs
    w = rows[("tool", "WebFetch", None)]
    assert (w["count"], w["deferred"]) == (1, True)
    assert "deferred" not in rows[("tool", "Skill", None)]
    assert rows[("nx", "nx memory", "curate")]["count"] == 1
    assert rows[("nx", "nx store", "put")]["count"] == 1
    assert rows[("nx", "nx run", "transcripts")]["count"] == 1
    summary = rows[("deferred_summary", "(deferred)", None)]
    assert (summary["count"], summary["called"]) == (3, 1)
    assert all(r["session_id"] == "m1" and r["main"] is True for r in m1)
    assert tj.SESSION_FIELDS["tools"] >= {k for r in out["rows"] for k in r}


def test_skills_ports_the_census() -> None:
    out, _ = run(fixture_day(), "skills", until="2026-11-04")
    rows = by(out["rows"], "scope", "kind", "name")
    assert rows[("main", "skill", "conexus:orchestration")]["count"] == 2
    assert rows[("main", "dispatch", "developer")]["count"] == 2
    assert rows[("main", "dispatch", "(default)")]["count"] == 1
    assert rows[("main", "slash", "commit")]["count"] == 1
    main = rows[("main", "runs", "(all)")]
    assert (main["count"], main["runs_with_skill"], main["long_runs"], main["long_runs_without_skill"]) == (2, 1, 2, 1)
    sub = rows[("subagent", "runs", "(all)")]
    assert (sub["count"], sub["runs_with_skill"], sub["long_runs"]) == (2, 0, 0)
    assert {k for r in out["rows"] for k in r} == tj.SHAPES["skills"].fields


def test_a_long_run_is_31_api_messages_which_is_about_the_censuss_50_records() -> None:
    assert re.search(r"^local LONG_RUN_TURNS = 31$", TEXT, re.M)
    records = [rec("a", f"{DAY}T10:00:00Z", turns=30), rec("b", f"{DAY}T11:00:00Z", turns=31)]
    out, _ = run(records, "skills", until="2026-11-04")
    main = by(out["rows"], "scope", "kind", "name")[("main", "runs", "(all)")]
    assert (main["count"], main["long_runs"]) == (2, 1)


def test_memory_counts_file_t2_and_subagent_loads() -> None:
    out, _ = run(fixture_day(), "memory", until="2026-11-04")
    rows = by(out["rows"], "session_id", "main", "kind", "name")
    assert rows[("m1", True, "file_read", f"{MEM}/feedback_a.md")]["count"] == 2
    assert rows[("m1", True, "file_write", "/h/.claude/projects/x/MEMORY.md")]["count"] == 1
    g = rows[("m1", True, "t2_get", "nexus/t1")]
    assert (g["count"], g["last_use"]) == (2, f"{DAY}T10:05:00Z")
    assert rows[("m1", True, "t2_search", "nexus")]["count"] == 1
    assert rows[("m1", True, "t2_search", "(all)")]["count"] == 1
    loaded = rows[("m1", False, "loaded", "/r/CLAUDE.md")]
    assert (loaded["size"], loaded["agent_id"], loaded["type"]) == (1000, "a1", "Project")
    assert not any(r["kind"] == "loaded" and r["main"] for r in out["rows"])
    assert not any("not_memory" in r["name"] for r in out["rows"])


def test_memory_read_and_get_rows_carry_no_size_and_last_use_compares_parsed_times() -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[
        call(GET, {"project": "n", "title": "t"}, "10:00:00"),
        call(GET, {"project": "n", "title": "t"}, "10:00:00.500"),
        call(GET, {"project": "n", "title": "t"}, "10:00:00.250"),
        call("Read", {"file_path": f"{MEM}/a.md"}, "10:00:01.500"),
        call("Read", {"file_path": f"{MEM}/a.md"}, "10:00:01")])
    out, _ = run([s], "memory", until="2026-11-04")
    rows = by(out["rows"], "kind")
    assert rows[("t2_get",)]["last_use"] == f"{DAY}T10:00:00.500Z"  # string order puts "...00Z" last
    assert rows[("file_read",)]["last_use"] == f"{DAY}T10:00:01.500Z"
    assert all("size" not in r for r in out["rows"])  # unknown is absent, never 0
    loaded = rec("m", f"{DAY}T10:00:00Z", main=False, agent="a", loads=[load("/r/x.md", 40)])
    out, _ = run([loaded], "memory", until="2026-11-04")
    assert out["rows"][0]["size"] == 40  # a measured size stays


def test_loads_counts_instruction_files_per_main_session_and_per_dispatch() -> None:
    out, _ = run(fixture_day(), "loads", until="2026-11-04")
    rows = by(out["rows"], "session_id", "agent_id", "name")
    c = rows[("m1", None, "/r/CLAUDE.md")]
    assert (c["count"], c["size"], c["type"], c["kind"], c["main"]) == (2, 1000, "Project", "instructions", True)
    assert rows[("m1", None, "/r/rules/a.md")]["type"] == "Local"
    d = rows[("m1", "a1", "/r/CLAUDE.md")]
    assert (d["main"], d["count"], d["size"]) == (False, 1, 1000)


def test_procedures_counts_runs_against_hand_written_equivalents() -> None:
    out, _ = run(fixture_day(), "procedures", until="2026-11-04")
    rows = by(out["rows"], "procedure")
    # one gate_record run, two kind:gate puts: the first carries the run's record
    assert (rows[("gate_record",)]["runs"], rows[("gate_record",)]["hand_written"]) == (1, 1)
    assert (rows[("transcripts",)]["runs"], rows[("transcripts",)]["hand_written"]) == (1, 1)
    assert (rows[("research_add",)]["runs"], rows[("research_add",)]["hand_written"]) == (0, 1)
    assert {k for r in out["rows"] for k in r} == tj.SHAPES["procedures"].fields


def test_violations_counts_main_sessions_with_a_hit_and_carries_every_denominator() -> None:
    out, fake = run(fixture_day(), "violations", until="2026-11-04")
    rows = by(out["rows"], "rule")
    assert set(k[0] for k in rows) == {"em-dash", "git-stash", "full-disk-find", "claude-session-trailer",
                                       "backtick-in-quoted-arg"}
    assert {k: (v["count"], v["main_sessions"]) for (k,), v in rows.items()} == {
        "em-dash": (1, 2), "git-stash": (1, 2), "full-disk-find": (0, 2),
        "claude-session-trailer": (0, 2), "backtick-in-quoted-arg": (0, 2)}
    assert fake.calls[0][3] is True
    empty, _ = run([], "violations", until="2026-11-04")
    assert {r["rule"]: (r["count"], r["main_sessions"]) for r in empty["rows"]}["em-dash"] == (0, 0)


BT = "backtick-in-quoted-arg"
STASH, FIND, TRAILER = "git-stash", "full-disk-find", "claude-session-trailer"


@pytest.mark.parametrize("command,rule", [
    ("find / -name x", FIND),
    ("find ~ -type f", FIND),
    ("find $HOME -name x", FIND),
    ("find -L / -name x", FIND),
    ("find /Users/hal -name x", FIND),
    ("find /home -name x", FIND),
    ('find "$HOME" -name x', FIND),
    ('find "/" -name x', FIND),
    ("find '/Users' -name x", FIND),
    ("sudo find / -name x", FIND),
    ("ls && find ~/ -name x", FIND),
    ("find . -name x", None),
    ("find /Users/hal/git/nexus -name x", None),
    ("find src -name '/'", None),
    ('find . -name "/"', None),
    ("find src -path ~", None),
    ('echo "find / -name x"', None),
    ("grep -r find /", None),
    ('git commit -m "x\n\nClaude-Session: abc"', TRAILER),
    ("git -C /x commit -m 'Claude-Session: a'", TRAILER),
    ("git --no-pager commit --amend -m 'Claude-Session: a'", TRAILER),
    ("git commit -F - <<'EOF'\nsubject\n\nClaude-Session: abc\nEOF", TRAILER),
    ("git log --grep 'Claude-Session:'", None),
    ("grep 'Claude-Session:' notes.txt", None),
    ("git commit -m ok\ngrep Claude-Session: notes.txt", None),
    ("git status # git commit Claude-Session:", None),
    ("git commit -m ok # Claude-Session: x", None),
    ("git log -1 --format=Claude-Session:", None),
    ('bd create "fix `x` thing"', BT),
    ('uv run nx store put "a `b`"', BT),
    ('echo $(bd create "a `b`")', BT),
    ('nx store put "it\'s `x`"', BT),
    ("nx store put 'it'\"'\"'s' \"`x`\"", BT),
    ("nx store put 'a `b`'", None),  # single quotes: the backtick is literal
    ("nx store put 'a \"b `c`\"'", None),
    ("bd create \"a\" 'b `c`'", None),
    ('nx store put "a \\`b\\`"', None),  # an escaped backtick is literal too
    ('echo "a `b`"', None),
    ('echo "`x`" && nx memory put ok', None),  # the backtick is in another segment
    ('nx x <<EOF\n`y` "`z`"\nEOF', None),  # a heredoc body is not an argument
    ('nx x # "a `b`"', None),
    ('nx memory put "a $(date)"', None),
    ("nx memory put plain", None),
    ("nx store put `date`", None),
    # wrappers before the real command: timeout (duration, -s/-k options), nice, watch, xargs
    ("timeout 5 git stash", STASH),
    ("timeout 1.5m git stash", STASH),
    ("timeout -s KILL -k 10 5 git stash", STASH),
    ("timeout --signal=KILL --kill-after=2s 30 git stash pop", STASH),
    ("timeout --foreground -v 9 git stash", STASH),
    ("sudo timeout 5 git stash", STASH),
    ("timeout 5 find / -name x", FIND),
    ("nice git stash", STASH),
    ("nice -n 10 git stash", STASH),
    ("nice -10 git stash", STASH),
    ("nice --adjustment=5 git stash", STASH),
    ("watch git stash", STASH),
    ("watch -n 2 -d git stash", STASH),
    ("watch --interval 3 -t git stash", STASH),
    ("xargs git stash", STASH),
    ("xargs -n 1 -I {} git stash", STASH),
    ("xargs -0 -r -P 4 git stash", STASH),
    ("xargs find /", FIND),
    ("ls | xargs -I{} find ~ -name x", FIND),
    ("timeout 5 nice -n 3 xargs git stash", STASH),
    ("timeout 5", None),
    ("nice -n", None),
    # look-alikes are not wrappers; a wrapper word that is an argument is not a command
    ("timeouts 5 git stash", None),
    ("nicely git stash", None),
    ("watcher git stash", None),
    ("xargs-lite git stash", None),
    ("echo timeout 5 git stash", None),
    ("git log timeout 5 stash", None),
    ("timeout 5 echo git stash", None),
    ("xargs echo find /", None),
    ('timeout 5 bd create "fix `x` thing"', BT),
])
def test_the_bash_rules(command: str, rule: str | None) -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[call("Bash", {"command": command})])
    out, _ = run([s], "violations", until="2026-11-04")
    assert {r["rule"] for r in out["rows"] if r["count"]} == ({rule} if rule else set())


@pytest.mark.parametrize("command,verbs", [
    ("echo 'use nx store here'", []),
    ("rg nx src", []),
    ("grep -rn nx README", []),
    ("cat <<EOF\nnx store put\nEOF", []),
    ("nx --version", []),
    ("nx Store put", []),
    # sub is the next word when it is a bare lowercase word: so for a verb that takes a free
    # argument (nx search foo) it is that argument; a quoted word, a path or a flag never is
    ("nx search foo", [("search", "foo")]),
    ('nx search "foo bar"', [("search", None)]),
    ("nx scratch ./file", [("scratch", None)]),
    ("nx store 'put'", [("store", None)]),
    ("nx store put x", [("store", "put")]),
    ("nx index repo .", [("index", "repo")]),
    ("/opt/bin/nx doctor --fix", [("doctor", None)]),
    ("nx memory curate --cold && uv run nx store put x", [("memory", "curate"), ("store", "put")]),
    ("cd x; FOO=1 nx run gate_record -", [("run", "gate_record")]),
    ("echo hi | nx search q", [("search", "q")]),
    ("timeout 5 nx search foo", [("search", "foo")]),
    ("nice -n 5 nx doctor", [("doctor", None)]),
    ("xargs -n1 nx store put", [("store", "put")]),
    ("watch -n 2 nx scratch list", [("scratch", "list")]),
    ("timeouts 5 nx search foo", []),
])
def test_nx_verbs_are_read_from_the_command_word_of_a_segment(command: str, verbs: list) -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[call("Bash", {"command": command})])
    out, _ = run([s], "tools", until="2026-11-04")
    got = sorted((r["name"][3:], r.get("sub")) for r in out["rows"] if r["kind"] == "nx")
    assert got == sorted(verbs), command


def test_a_procedure_run_is_read_the_same_way() -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[
        call("Bash", {"command": "echo 'nx run decoy'"}),
        call("Bash", {"command": "rg nx run src"}),
        call("Bash", {"command": "nx run real_one --args-file -"})])
    out, _ = run([s], "procedures", until="2026-11-04")
    assert by(out["rows"], "procedure") == {("real_one",): {"procedure": "real_one", "runs": 1, "hand_written": 0}}


@pytest.mark.parametrize("command,hand", [
    # a hand-written equivalent is a command, read by segment: not an echo, a quote, a comment or a heredoc
    ("nx rdr research add 240 --finding x", {"research_add": 1}),
    ("timeout 9 nx rdr research add 240", {"research_add": 1}),
    ("echo 'nx rdr research'", {}),
    ("echo nx rdr research", {}),
    ("rg 'nx rdr research' docs", {}),
    ("cat <<EOF\nnx rdr research\nEOF", {}),
    ("ls # nx rdr research", {}),
    ("nx memory put p t --tags kind:gate,bead:b", {"gate_record": 1}),
    ("cd x && nx memory put p t --tags 'kind:gate,bead:b' -", {"gate_record": 1}),
    ("echo 'nx memory put p t --tags kind:gate'", {}),
    ("nx memory put p t --tags plain", {}),
    ("nx memory put p t; echo kind:gate", {}),
    ("cat ~/.claude/projects/x/y.jsonl | jq .", {"transcripts": 1}),
    ('jq . "$HOME/.claude/projects/x/y.jsonl"', {"transcripts": 1}),
    ("grep -h tool_use ~/.claude/projects/x/*.jsonl", {"transcripts": 1}),
    ('echo "cat ~/.claude/projects/x/y.jsonl"', {}),
    ("ls # ~/.claude/projects/x/y.jsonl", {}),
    ("cat ~/.claude/projects/x/notes.txt", {}),
    ("python scripts/select_affected_integration_tests.py a b", {"preflight": 1}),
    ("echo select_affected_integration_tests", {}),
])
def test_hand_written_equivalents_are_read_by_segment(command: str, hand: dict) -> None:
    s = rec("s", f"{DAY}T10:00:00Z", calls=[call("Bash", {"command": command})])
    out, _ = run([s], "procedures", until="2026-11-04")
    assert {r["procedure"]: r["hand_written"] for r in out["rows"] if r["hand_written"]} == hand, command


@pytest.mark.parametrize("text,hit", [
    (f"a {EM} b", True), ("no dash", False), (f"```\n{EM}\n```", False), (f"see `{EM}` here", False),
    (f"```\ncode\n``` then {EM}", True), (f"a - b", False),
])
def test_the_em_dash_rule_reads_prose_not_code(text: str, hit: bool) -> None:
    s = rec("s", f"{DAY}T10:00:00Z", text=[{"time": f"{DAY}T10:00:01Z", "text": text}])
    out, _ = run([s], "violations", until="2026-11-04")
    assert by(out["rows"], "rule")[("em-dash",)]["count"] == int(hit)


def test_only_violations_asks_for_bodies() -> None:
    assert "plain" in json.dumps(fixture_day()) and "clean" in json.dumps(fixture_day())  # the fixture has bodies
    for query in ("agents", "tools", "skills", "memory", "loads", "procedures"):
        out, fake = run(fixture_day(), query, until="2026-11-04")
        assert [c[3] for c in fake.calls] == [False], query
        assert "plain" not in json.dumps(out) and "clean" not in json.dumps(out), query  # no body text
    _, fake = run(fixture_day(), "violations", until="2026-11-04")
    assert [c[3] for c in fake.calls] == [True]


def test_a_truncated_record_is_counted() -> None:
    records = [rec("a", f"{DAY}T10:00:00Z", truncated=True), rec("b", f"{DAY}T11:00:00Z"),
               rec("c", "2026-11-04T10:00:00Z", truncated=True)]
    out, _ = run(records, "tools", until="2026-11-05")
    assert out["truncated_records"] == 2


# ---- the real verb over fixture transcripts -------------------------------------------------


def _jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _assistant(ts: str, sid: str, blocks: list, mid: str = "m1") -> dict:
    return {"type": "assistant", "timestamp": ts, "sessionId": sid, "isSidechain": False,
            "message": {"id": mid, "model": "claude-opus-4-8", "role": "assistant", "content": blocks,
                        "usage": {"input_tokens": 1, "output_tokens": 1}}}


def _result(ts: str, sid: str, tid: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "sessionId": sid, "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": [{"type": "text", "text": text}]}]}}


class RealVerb:
    """The real verb over a fixture config dir; ``calls``, ``joins`` and ``asked`` are the fake's."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(root))
        self.calls: list[tuple] = []
        self.joins: list[tuple] = []
        self.asked: list[str | None] = []

    def __call__(self, since: str, until: str, project: Any, bodies: Any, fields: Any = None) -> Any:
        (self.joins if fields == JOINED else self.calls).append((since, until, project, bodies))
        self.asked.append(fields)
        return vt.sessions(since, until, project, bodies, fields, deadline=time.monotonic() + 60,
                           ctx=VerbContext(repo_root=None))


def test_bodies_true_carries_assistant_text_and_never_a_tool_result_or_a_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cfg"
    p = root / "projects" / "proj"
    _jsonl(p / "s1.jsonl", [
        {"type": "user", "timestamp": f"{DAY}T10:00:00Z", "sessionId": "s1",
         "message": {"role": "user", "content": f"prompt {EM} with a dash and git stash"}},
        _assistant(f"{DAY}T10:00:01Z", "s1", [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        _result(f"{DAY}T10:00:02Z", "s1", "t1", f"result {EM} git stash"),
        _assistant(f"{DAY}T10:00:03Z", "s1", [{"type": "text", "text": f"said {EM} this"}], mid="m2"),
    ])
    _jsonl(p / "s2.jsonl", [
        {"type": "user", "timestamp": f"{DAY}T11:00:00Z", "sessionId": "s2",
         "message": {"role": "user", "content": f"prompt {EM}"}},
        _assistant(f"{DAY}T11:00:01Z", "s2", [{"type": "tool_use", "id": "t9", "name": "Bash",
                                                "input": {"command": "ls"}}]),
        _result(f"{DAY}T11:00:02Z", "s2", "t9", f"result {EM} git stash"),
    ])
    verb = RealVerb(monkeypatch, root)
    out = Run(verb)(query="violations", until="2026-11-04")
    rows = by(out["rows"], "rule")
    assert (rows[("em-dash",)]["count"], rows[("em-dash",)]["main_sessions"]) == (1, 2)
    assert rows[("git-stash",)]["count"] == 0
    assert [c[3] for c in verb.calls] == [True]
    tools = Run(verb)(query="tools", until="2026-11-04")
    assert verb.calls[-1][3] is False
    assert EM not in json.dumps(tools, ensure_ascii=False)


def test_a_record_over_1_mb_arrives_cut_marked_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cfg"
    big = "x" * (3 * 1024 * 1024)
    _jsonl(root / "projects" / "proj" / "big.jsonl", [
        _assistant(f"{DAY}T10:00:00Z", "big", [
            {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": big[:300_000]}}
            for i in range(10)])])
    verb = RealVerb(monkeypatch, root)
    out = Run(verb)(query="tools", until="2026-11-04")
    assert out["truncated_records"] == 1
    assert out["rows"] and all(r["session_id"] == "big" for r in out["rows"])


def test_a_session_spanning_midnight_is_counted_once_on_its_start_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cfg"
    _jsonl(root / "projects" / "proj" / "late.jsonl", [
        _assistant("2026-11-03T23:50:00Z", "late", [
            {"type": "tool_use", "id": "a", "name": "Skill", "input": {"skill": "s"}}]),
        _assistant("2026-11-04T00:10:00Z", "late", [
            {"type": "tool_use", "id": "b", "name": "Skill", "input": {"skill": "s"}}], mid="m2"),
    ])
    verb = RealVerb(monkeypatch, root)
    out = Run(verb)(query="skills", since="2026-11-02", until="2026-11-06")
    rows = by(out["rows"], "scope", "kind", "name")
    assert rows[("main", "runs", "(all)")]["count"] == 1
    assert rows[("main", "skill", "s")]["count"] == 2  # both calls, one session, the start day's
    assert [c[:2] for c in verb.calls] == [("2026-11-02", "2026-11-03"), ("2026-11-03", "2026-11-04"),
                                           ("2026-11-04", "2026-11-05"), ("2026-11-05", "2026-11-06")]


# ---- the fields filter (RDR-242 A2, nexus-qo7ie.31) ---------------------------------------------

QUERIES = ("agents", "tools", "skills", "memory", "violations", "loads", "procedures")


def _everything(bodies: bool) -> str:
    """A fields string that selects every field of a record, with bodies when asked."""
    names = list(vt._SCALARS)
    for lst, subs in vt._SUBS.items():
        if lst != "assistant_text" or bodies:
            names += [f"{lst}.{s}" for s in subs]
    return ",".join(names)


class Everything:
    """A verb that answers every read in full, whatever fields the script asked for: the
    reference a query's field set must reproduce."""

    def __init__(self, verb: Callable[..., Any]) -> None:
        self.verb = verb

    def __call__(self, since: str, until: str, project: Any, bodies: Any, fields: Any = None) -> Any:
        return self.verb(since, until, project, bodies, _everything(bool(bodies)))


def rich_extra() -> list[dict]:
    """What fixture_day lacks, so that every entry of a field set is read by some record: a memory
    Write and Edit and MultiEdit, a Task that names its agent, a deferred tool that is called."""
    return [rec("e1", f"{DAY}T13:00:00Z", turns=5, deferred=["Write", "mcp__x__never"], calls=[
        call("Write", {"file_path": f"{MEM}/w.md"}, "13:01:00"),
        call("MultiEdit", {"file_path": f"{MEM}/m.md"}, "13:02:00"),
        call("Edit", {"file_path": f"{MEM}/e.md"}, "13:03:00"),
        call("Task", {"subagent_type": "planner", "model": "haiku"}, "13:04:00", err=True),
        call("Skill", {"skill": "x:y"}, "13:05:00")])]


def test_every_query_asks_for_a_field_set_and_its_rows_equal_those_of_a_full_read() -> None:
    for query in QUERIES:
        out, fake = run(fixture_day() + rich_extra(), query, until="2026-11-04")
        full = Run(Everything(Sessions(fixture_day() + rich_extra())))(query=query, until="2026-11-04")
        assert out["rows"], query  # a sweep of nothing proves nothing
        assert out["rows"] == full["rows"], query
        assert fake.asked and all(isinstance(f, str) and f for f in fake.asked), (query, fake.asked)


def _lua_fields(query: str) -> tuple[list[str], str | None, bool]:
    """The fields string, the joined-read string (or None) and the bodies flag of a builder."""
    block = TEXT.split(f"local function build_{query}()\n", 1)[1].split("\nend\n", 1)[0]
    m = re.search(r'^\s*fields = ((?:"[^"]*"(?: \.\.\n\s*)?)+),$', block, re.M)
    assert m, query
    got = "".join(re.findall(r'"([^"]*)"', m.group(1))).split(",")
    j = re.search(r'^\s*joined = "([^"]*)",$', block, re.M)
    return got, (j.group(1) if j else None), re.search(r"bodies = (true|false)", block).group(1) == "true"


def test_each_querys_field_set_agrees_with_the_verbs_pinned_table() -> None:
    for query in QUERIES:
        got, joined, bodies = _lua_fields(query)
        assert len(got) == len(set(got)), query  # no entry twice
        assert joined is None, query  # one read per unit: a second read decodes the window again
        # the pin is the verb's: the keys each query returns beyond the four that always come back;
        # it measures tools' deferred_tools as its own set, and tools reads both in one call
        pinned = set(PINNED_FIELDS[query][0])
        if query == "tools":
            pinned |= set(PINNED_FIELDS["deferred_tools"][0])
        assert set(got) == pinned, query
        assert bodies == PINNED_FIELDS[query][1], query


def test_every_query_makes_one_read_per_unit_and_only_tools_asks_for_deferred_tools() -> None:
    for query in QUERIES:
        _, fake = run(fixture_day(), query, since="2026-11-02", until="2026-11-04", project="p")
        assert fake.joins == [] and len(fake.calls) == len(fake.asked) == 2, query
        has = ["deferred_tools" in f.split(",") for f in fake.asked]
        assert has == [query == "tools"] * 2, query


def test_tools_flags_each_records_own_deferred_tools_session_and_agent_apart() -> None:
    main = rec("S", f"{DAY}T10:00:00Z", deferred=["A", "B"],
               calls=[call("A", {}), call("B", {}), call("C", {})])
    sub = rec("S", f"{DAY}T10:05:00Z", main=False, agent="a1", deferred=["C"],
              calls=[call("A", {}), call("B", {}), call("C", {})])
    none = rec("S", f"{DAY}T10:06:00Z", main=False, agent="a2", calls=[call("C", {})])
    other = rec("T", f"{DAY}T11:00:00Z", calls=[call("A", {})])
    out, _ = run([main, sub, none, other], "tools", until="2026-11-04")
    rows = by(out["rows"], "session_id", "agent_id", "kind", "name")
    assert rows[("S", None, "tool", "A")]["deferred"] is True
    assert rows[("S", None, "tool", "B")]["deferred"] is True
    assert "deferred" not in rows[("S", None, "tool", "C")]  # C is deferred for the agent, not the main
    assert rows[("S", "a1", "tool", "C")]["deferred"] is True
    assert "deferred" not in rows[("S", "a1", "tool", "A")] and "deferred" not in rows[("S", "a1", "tool", "B")]
    assert not any(r.get("deferred") for r in out["rows"] if r["session_id"] == "T" or r.get("agent_id") == "a2")
    summaries = {(r["session_id"], r.get("agent_id")): (r["count"], r["called"])
                 for r in out["rows"] if r["kind"] == "deferred_summary"}
    assert summaries == {("S", None): (2, 2), ("S", "a1"): (1, 1)}  # none for the two with nothing deferred


def test_the_units_cap_stops_a_page_at_its_count_of_verb_reads() -> None:
    for query in ("skills", "tools"):
        out, fake = run(sparse(30, 1), query, since=_day(0), until=_day(30), patch={"MAX_UNITS_PER_RUN": "7"})
        assert out["next_since"] == _day(7) and len(fake.calls) == 7 and fake.joins == [], query


def test_the_tool_call_bound_counts_the_calls_a_record_had_before_the_filter() -> None:
    def day(n: int) -> dict:
        r = rec(f"s{n}", f"{_day(n)}T10:00:00Z", calls=[call("Skill", {"skill": "k"}, day=_day(n))],
                loads=[load("/r/x.md", 5)])
        r["tool_call_count"] = 20  # as a cut record's is: more than the calls it kept
        return r

    for query in QUERIES:  # loads asks for no call at all, and still pages by the calls it ran past
        out, _ = run([day(0), day(1)], query, since=_day(0), until=_day(2), patch={"MAX_TOOL_CALLS_PER_RUN": "25"})
        assert out["next_since"] == _day(1), query


def test_a_record_with_no_count_is_counted_by_the_calls_it_lists() -> None:
    def verb(since: str, until: str, project: Any, bodies: Any, fields: Any = None) -> Any:
        return [{"session_id": f"s{since}", "main": True, "assistant_turns": 1,
                 "tool_calls": [{"name": "Skill", "input": {"skill": "k"}, "time": f"{since}T10:00:00Z"}
                                for _ in range(20)]}]

    out = Run(verb, patch={"MAX_TOOL_CALLS_PER_RUN": "25"})(query="skills", since="2026-11-01", until="2026-11-03")
    assert out["next_since"] == "2026-11-02"


def _att(ts: str, sid: str, att: dict) -> dict:
    return {"type": "attachment", "timestamp": ts, "sessionId": sid, "isSidechain": False, "attachment": att}


def _prompt(ts: str, sid: str, text: str) -> dict:
    return {"type": "user", "timestamp": ts, "sessionId": sid, "isSidechain": False,
            "message": {"role": "user", "content": text}}


def _failed(ts: str, sid: str, tid: str) -> dict:
    return {"type": "user", "timestamp": ts, "sessionId": sid, "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": "boom"}]}}


def _use(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _write_rich(root: Path) -> None:
    """One main session that does something for every query, and two subagents, on disk."""
    p = root / "projects" / "proj"
    sid = "rich1"
    t = lambda hms: f"{DAY}T{hms}Z"  # noqa: E731
    calls = [
        _use("c1", "Read", {"file_path": f"{MEM}/a.md"}),
        _use("c2", "Write", {"file_path": f"{MEM}/b.md"}),
        _use("c3", "Edit", {"file_path": f"{MEM}/c.md"}),
        _use("c4", "MultiEdit", {"file_path": f"{MEM}/d.md"}),
        _use("c5", GET, {"project": "nexus", "title": "t1"}),
        _use("c6", SEARCH, {"project": "nexus", "query": "q"}),
        _use("c7", "WebFetch", {"url": "u", "note": "é" * 10}),
        _use("c8", "Bash", {"command": "nx memory curate && git stash"}),
        _use("c9", "Skill", {"skill": "conexus:orchestration"}),
        _use("c10", "Agent", {"subagent_type": "developer", "model": "opus"}),
        _use("c11", "Task", {"subagent_type": "planner"}),
        _use("c12", RUN, {"name": "gate_record"}),
        _use("c13", PUT, {"title": "review-x", "tags": "kind:gate,bead:b"}),
        _use("c14", "Bash", {"command": "nx run transcripts --args-file -"}),
        _use("c15", PUT, {"title": "review-y", "tags": "kind:gate,bead:c"}),  # a gate put no run pairs
    ]
    main: list[dict] = [
        _prompt(t("10:00:00"), sid, "start"),
        _att(t("10:00:01"), sid, {"type": "deferred_tools_record",
                                  "entries": [{"name": "WebFetch", "defer_loading": True}]}),
        _att(t("10:00:02"), sid, {"type": "deferred_tools_delta", "addedNames": ["mcp__x__y"]}),
        _att(t("10:00:03"), sid, {"type": "instructions",
                                  "files": [{"path": "/r/CLAUDE.md", "type": "Project", "content": "abc"}]}),
        _prompt(t("10:00:04"), sid, "<command-name>/commit</command-name> go"),
        _assistant(t("10:00:05"), sid, [{"type": "text", "text": f"said {EM} this"}], mid="t0"),
    ]
    for i, block in enumerate(calls):
        main.append(_assistant(t(f"10:{i + 1:02d}:00"), sid, [block], mid=f"m{i}"))
        if block["id"] in ("c11", "c3"):
            main.append(_failed(t(f"10:{i + 1:02d}:30"), sid, block["id"]))
        else:
            main.append(_result(t(f"10:{i + 1:02d}:30"), sid, block["id"], "ok"))
    _jsonl(p / f"{sid}.jsonl", main)
    for agent, start in (("a1", "10:40:00"), ("a2", "10:41:00")):
        sub = [
            {**_prompt(t(start), sid, "go"), "agentId": agent, "isSidechain": True},
            {**_att(t("10:40:01"), sid, {"type": "instructions", "files": [
                {"path": f"/r/{agent}.md", "type": "Local", "content": "xy"}]}), "agentId": agent},
            {**_assistant(t("10:40:02"), sid, [_use(f"u{agent}", "Bash", {"command": "false"})],
                          mid=f"{agent}-m"), "agentId": agent},
            {**_failed(t("10:40:03"), sid, f"u{agent}"), "agentId": agent},
        ]
        _jsonl(p / sid / "subagents" / f"agent-{agent}.jsonl", sub)
    long = [{**_prompt(t("10:50:00"), sid, "go"), "agentId": "a3", "isSidechain": True}]
    for i in range(31):  # past the skills query's turn threshold
        long.append({**_assistant(t(f"10:5{i // 10}:{i % 10 + 10:02d}"), sid, [{"type": "text", "text": "x"}],
                                  mid=f"a3-m{i}"), "agentId": "a3"})
    _jsonl(p / sid / "subagents" / "agent-a3.jsonl", long)


def test_the_real_verb_accepts_every_querys_fields_and_a_filtered_read_equals_a_full_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cfg"
    _write_rich(root)
    for query in QUERIES:
        verb = RealVerb(monkeypatch, root)
        out = Run(verb)(query=query, until="2026-11-04")
        full = Run(Everything(RealVerb(monkeypatch, root)))(query=query, until="2026-11-04")
        assert out["rows"], query
        assert out["rows"] == full["rows"], query
        assert verb.asked and all(isinstance(f, str) and f for f in verb.asked), query
    tools = Run(RealVerb(monkeypatch, root))(query="tools", until="2026-11-04")["rows"]
    assert by(tools, "kind", "name")[("tool", "WebFetch")]["deferred"] is True
    assert any(r["kind"] == "deferred_summary" and r["count"] == 2 for r in tools)


def test_tools_size_is_the_true_input_size_though_the_input_is_not_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "cfg"
    big = "é" * 300_000  # 600,000 bytes: over the verb's 256 Ki-character cut, and never carried here
    _jsonl(root / "projects" / "proj" / "big.jsonl", [
        _assistant(f"{DAY}T10:00:00Z", "big", [
            {"type": "tool_use", "id": "g", "name": "Grep", "input": {"pattern": big, "n": 5}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": big}}])])
    verb = RealVerb(monkeypatch, root)
    out = Run(verb)(query="tools", until="2026-11-04")
    sizes = {r["name"]: r["size"] for r in out["rows"] if r["kind"] == "tool"}
    assert sizes == {"Grep": 600_000, "Bash": 600_000}  # the cut command would sum to 524,288
    assert out["truncated_records"] == 1  # the Bash command is carried, and cut at 256 Ki characters


# ---- day stepping, the hour split, paging ----------------------------------------------------


def test_a_three_day_window_makes_three_verb_calls_one_utc_day_each() -> None:
    records = [rec(f"s{i}", f"2026-11-0{i}T10:00:00Z") for i in (1, 2, 3)]
    out, fake = run(records, "tools", since="2026-11-01", until="2026-11-04")
    assert fake.calls == [("2026-11-01", "2026-11-02", None, False), ("2026-11-02", "2026-11-03", None, False),
                          ("2026-11-03", "2026-11-04", None, False)]
    assert out["truncated_records"] == 0 and "next_since" not in out


def test_the_project_is_passed_through_unchanged() -> None:
    for project in ("-Users-me-proj", "all", "repo"):  # a directory name, every project, the repo's directories
        _, fake = run([], "tools", until="2026-11-04", project=project)
        assert fake.calls[0][2] == project
    _, fake = run([], "tools", until="2026-11-04")
    assert fake.calls[0][2] is None  # absent stays absent: the verb reads nil as all


def test_a_timestamp_window_steps_to_the_next_midnight_and_ends_at_to() -> None:
    _, fake = run([], "tools", since="2026-11-03T22:30:00Z", until="2026-11-05T06:00:00Z")
    assert fake.calls == [("2026-11-03T22:30:00Z", "2026-11-04", None, False),
                          ("2026-11-04", "2026-11-05", None, False),
                          ("2026-11-05", "2026-11-05T06:00:00Z", None, False)]


def test_the_month_and_year_and_leap_day_step_correctly() -> None:
    _, fake = run([], "tools", since="2027-12-31", until="2028-01-02")
    assert [c[:2] for c in fake.calls] == [("2027-12-31", "2028-01-01"), ("2028-01-01", "2028-01-02")]
    _, fake = run([], "tools", since="2028-02-28", until="2028-03-01")
    assert [c[:2] for c in fake.calls] == [("2028-02-28", "2028-02-29"), ("2028-02-29", "2028-03-01")]


def test_a_boundary_session_is_in_the_later_window_only() -> None:
    records = [rec("mid", "2026-11-04T00:00:00Z"), rec("end", "2026-11-04T10:00:00Z")]
    out, _ = run(records, "tools", since="2026-11-03", until="2026-11-04")
    assert out["rows"] == []
    out, _ = run([rec("mid", "2026-11-04T00:00:00Z", calls=[call("Read", {})])], "tools", since="2026-11-04",
                 until="2026-11-05")
    assert [r["session_id"] for r in out["rows"]] == ["mid"]


def hourly(n: int, day: str, hour: int, title: str = "t" * 600) -> list[dict]:
    """n main sessions starting in one hour, each with a long memory_get title (a ~1.4 KB row)."""
    return [rec(f"{day}-{hour:02d}-{i:04d}", f"{day}T{hour:02d}:{i % 60:02d}:{i // 60:02d}Z",
                calls=[call(GET, {"project": "nexus", "title": f"{title}{i:04d}"})])
            for i in range(n)]


def oracle_memory(records: list[dict]) -> Counter:
    return Counter(r["session_id"] for r in records)


def pages_of(records: list[dict], query: str, *, since: str, until: str, patch: dict | None = None,
             max_records: int | None = None) -> tuple[list[dict], list[Sessions]]:
    """Every page of a window, run the way a caller loops on next_since."""
    pages: list[dict] = []
    fakes: list[Sessions] = []
    cursor = since
    while True:
        out, fake = run(records, query, since=cursor, until=until, patch=patch, max_records=max_records)
        pages.append(out)
        fakes.append(fake)
        assert len(pages) < 200
        if "next_since" not in out:
            return pages, fakes
        assert out["next_since"] > cursor
        cursor = out["next_since"]


def test_rows_passing_768_kb_return_next_since_and_the_joined_pages_equal_one_pass() -> None:
    # three days of ~500 rows of ~1.4 KB: one day fits a page, two do not
    records = [r for d in ("2026-11-01", "2026-11-02", "2026-11-03") for r in hourly(500, d, 9)]
    pages, _ = pages_of(records, "memory", since="2026-11-01", until="2026-11-04")
    assert [p.get("next_since") for p in pages] == ["2026-11-02", "2026-11-03", None]
    assert all(400 * 1024 < _estimate(p["rows"]) <= 768 * 1024 for p in pages)
    joined = tj.join_pages("memory", pages)
    assert oracle_memory(records) == Counter(r["session_id"] for r in joined["rows"])
    assert len(joined["rows"]) == 1500
    one, _ = run(records, "memory", since="2026-11-01", until="2026-11-04", patch={"PAGE_BYTES": "10 * 1024 * 1024"})
    assert one["rows"] == joined["rows"] and "next_since" not in one


def _estimate(rows: list[dict]) -> int:
    """The script's row estimate: 16 bytes a field, and twice the UTF-8 bytes of every string."""
    return sum(16 + (2 * len(v.encode("utf-8")) if isinstance(v, str) else 0) for r in rows for v in r.values())


def test_a_page_ending_mid_day_returns_a_timestamp_and_the_pages_still_equal_one_pass() -> None:
    # one day of three hours of ~500 rows each: the day is over a page, each hour fits
    records = [r for h in (0, 1, 2) for r in hourly(500, "2026-11-03", h)]
    pages, fakes = pages_of(records, "memory", since="2026-11-03", until="2026-11-04")
    nexts = [p.get("next_since") for p in pages]
    assert nexts[0] == "2026-11-03T01:00:00Z" and nexts[-1] is None
    assert all(n is None or re.fullmatch(r"2026-11-03T\d\d:00:00Z", n) for n in nexts)
    joined = tj.join_pages("memory", pages)
    assert oracle_memory(records) == Counter(r["session_id"] for r in joined["rows"])
    assert len(joined["rows"]) == 1500
    # the day is read once and divided by the hours in hand; each later page reads the rest of the day once
    assert [[c[:2] for c in f.calls] for f in fakes] == [
        [("2026-11-03", "2026-11-04")],
        [("2026-11-03T01:00:00Z", "2026-11-04")],
        [("2026-11-03T02:00:00Z", "2026-11-04")],
    ]


def test_a_single_hour_over_the_page_bound_fails_with_result_window() -> None:
    records = hourly(1200, "2026-11-03", 5)
    with pytest.raises(lua54.LuaError, match=r"^result-window: 2026-11-03T05:00:00Z to 2026-11-03T06:00:00Z"):
        run(records, "memory", since="2026-11-03", until="2026-11-04")


def test_a_day_the_verb_refuses_is_split_into_hours_and_every_session_is_counted_once() -> None:
    records = [rec(f"s{i}", f"2026-11-03T{i % 24:02d}:{i // 24:02d}:00Z", calls=[call("Read", {})])
               for i in range(60)]
    out, fake = run(records, "tools", since="2026-11-03", until="2026-11-04", max_records=40)
    assert fake.calls[0][:2] == ("2026-11-03", "2026-11-04")
    assert [c[:2] for c in fake.calls[1:3]] == [("2026-11-03", "2026-11-03T01:00:00Z"),
                                                ("2026-11-03T01:00:00Z", "2026-11-03T02:00:00Z")]
    assert len(fake.calls) == 1 + 24
    assert Counter(r["session_id"] for r in out["rows"]) == Counter(r["session_id"] for r in records)
    unsplit, _ = run(records, "tools", since="2026-11-03", until="2026-11-04")
    assert unsplit["rows"] == out["rows"]


def test_an_hour_the_verb_refuses_fails_with_result_window_and_a_record_error_passes_through() -> None:
    records = [rec(f"s{i}", f"2026-11-03T05:{i:02d}:00Z") for i in range(50)]
    with pytest.raises(lua54.LuaError, match=r"^result-window: 2026-11-03T05:00:00Z to 2026-11-03T06:00:00Z"):
        run(records, "tools", since="2026-11-03", until="2026-11-04", max_records=30)

    def refuses(*_: Any) -> Any:
        raise VerbError("record-too-large: transcripts.sessions: one transcript's record does not fit in 1 MB")

    with pytest.raises(lua54.LuaError, match=r"^record-too-large:"):
        Run(refuses)(query="tools", until="2026-11-04")
    calls: list = []

    def refuses_once_counted(*a: Any) -> Any:
        calls.append(a)
        raise VerbError("transcripts.sessions: the run's time budget ran out reading transcripts")

    with pytest.raises(lua54.LuaError, match="time budget"):
        Run(refuses_once_counted)(query="tools", until="2026-11-04")
    assert len(calls) == 1


def test_sessions_on_a_page_boundary_and_on_an_hour_boundary_are_counted_once() -> None:
    # day 2's first session starts exactly at midnight and no hour of day 2 fits after day 1, so
    # the page ends there (a page boundary); in day 3 a session starts exactly on each hour
    # boundary of a divided day
    records = (hourly(500, "2026-11-01", 9)
               + hourly(500, "2026-11-02", 0)
               + [rec(f"on-hour-{h}", f"2026-11-03T{h:02d}:00:00Z", calls=[call(GET, {"title": "h"})])
                  for h in range(24)]
               + [r for h in (3, 4, 5) for r in hourly(500, "2026-11-03", h)])
    pages, _ = pages_of(records, "memory", since="2026-11-01", until="2026-11-04")
    assert len(pages) > 3
    joined = tj.join_pages("memory", pages)
    got = Counter(r["session_id"] for r in joined["rows"])
    assert got == oracle_memory(records)
    assert all(n == 1 for n in got.values())
    stops = [p["next_since"] for p in pages if "next_since" in p]
    assert "2026-11-02" in stops and any(s.endswith(":00:00Z") for s in stops)


def test_the_work_trigger_pages_the_keyed_queries_by_sessions_and_the_joined_pages_equal_one_pass() -> None:
    records = [rec(f"s{d}-{i}", f"2026-11-{d:02d}T10:0{i}:00Z",
                   calls=[call("Skill", {"skill": f"k{i % 2}"})]) for d in range(1, 13) for i in range(2)]
    pages, _ = pages_of(records, "skills", since="2026-11-01", until="2026-11-13", patch={"MAX_SESSIONS_PER_RUN": "5"})
    assert len(pages) == 6  # two sessions a day, five a page: two days a page
    assert [p.get("next_since") for p in pages[:2]] == ["2026-11-03", "2026-11-05"]
    one, _ = run(records, "skills", since="2026-11-01", until="2026-11-13")
    assert "next_since" not in one
    assert tj.join_pages("skills", pages)["rows"] == tj.join_pages("skills", [one])["rows"]
    # per-session queries are bounded by bytes, not by the work trigger
    solo, _ = run(records, "tools", since="2026-11-01", until="2026-11-13", patch={"MAX_SESSIONS_PER_RUN": "5"})
    assert "next_since" not in solo


def test_a_busy_first_unit_is_taken_whole_so_a_run_always_makes_progress() -> None:
    records = [rec(f"s{i}", f"2026-11-01T10:{i:02d}:00Z", calls=[call("Skill", {"skill": "k"})]) for i in range(20)]
    out, _ = run(records, "skills", since="2026-11-01", until="2026-11-03", patch={"MAX_SESSIONS_PER_RUN": "5"})
    assert out["next_since"] == "2026-11-02"
    assert by(out["rows"], "scope", "kind", "name")[("main", "runs", "(all)")]["count"] == 20


def test_keyed_pages_at_the_real_work_bound() -> None:
    n = int(re.search(r"^local MAX_SESSIONS_PER_RUN = (\d+)$", TEXT, re.M).group(1))
    assert 100 <= n <= 5000
    records = [rec(f"s{d}-{i}", f"2026-11-0{d}T10:{i % 60:02d}:{i // 60:02d}Z", calls=[call("Skill", {"skill": "k"})])
               for d in (1, 2) for i in range(n // 2 + 1)]
    out, _ = run(records, "skills", since="2026-11-01", until="2026-11-03")
    assert out["next_since"] == "2026-11-02"


def _day(n: int) -> str:
    return (date(2025, 11, 1) + timedelta(days=n)).isoformat()


def test_the_estimate_helper_counts_utf8_bytes_and_a_field_is_16_bytes() -> None:
    assert _estimate([{"a": "é", "b": 1, "c": True}]) == 16 * 3 + 2 * 2
    r = rec("s", f"{DAY}T10:00:00Z", calls=[call(GET, {"project": "n", "title": "t"})])
    out, _ = run([r], "memory", until="2026-11-04")
    # session_id, main, kind, name, count, last_use: six fields; strings "s", "t2_get", "n/t", 20 chars
    assert _estimate(out["rows"]) == 6 * 16 + 2 * (1 + 6 + 3 + 20) == 156


def test_a_page_holds_exactly_page_bytes_and_one_byte_less_does_not() -> None:
    def one(day: str) -> dict:
        return rec("s", f"{day}T10:00:00Z", calls=[call(GET, {"project": "n", "title": "t"}, day=day)])

    # a unit's own rows: exactly PAGE_BYTES is accepted, one byte less is split down to the hour and refused
    out, fake = run([one(DAY)], "memory", until="2026-11-04", patch={"PAGE_BYTES": "156"})
    assert len(out["rows"]) == 1 and len(fake.calls) == 1 and "next_since" not in out
    with pytest.raises(lua54.LuaError, match="^result-window"):
        run([one(DAY)], "memory", until="2026-11-04", patch={"PAGE_BYTES": "155"})
    # the page: two units of 156 fit in 312 and not in 311
    two = [one(DAY), one("2026-11-04")]
    out, _ = run(two, "memory", until="2026-11-05", patch={"PAGE_BYTES": "312"})
    assert len(out["rows"]) == 2 and "next_since" not in out
    out, _ = run(two, "memory", until="2026-11-05", patch={"PAGE_BYTES": "311"})
    assert len(out["rows"]) == 1 and out["next_since"] == "2026-11-04"


def test_hour_pieces_align_to_the_hour_from_a_mid_hour_since() -> None:
    records = [rec(f"s{i}", f"2026-11-03T{6 + i // 20:02d}:{i % 20:02d}:00Z", calls=[call("Read", {})])
               for i in range(60)]
    out, fake = run(records, "tools", since="2026-11-03T05:30:00Z", until="2026-11-04", max_records=40)
    pieces = [c[:2] for c in fake.calls]
    assert pieces[0] == ("2026-11-03T05:30:00Z", "2026-11-04")
    assert pieces[1:5] == [("2026-11-03T05:30:00Z", "2026-11-03T06:00:00Z"),
                           ("2026-11-03T06:00:00Z", "2026-11-03T07:00:00Z"),
                           ("2026-11-03T07:00:00Z", "2026-11-03T08:00:00Z"),
                           ("2026-11-03T08:00:00Z", "2026-11-03T09:00:00Z")]
    assert len(out["rows"]) == 60


def sparse(days: int, every: int) -> list[dict]:
    """One small session every ``every`` days: a sparse year of the kind the units cap exists for."""
    return [rec(f"s{d}", f"{_day(d)}T10:00:00Z", calls=[
        call("Skill", {"skill": f"k{d % 3}"}, day=_day(d)),
        call(GET, {"project": "n", "title": f"t{d % 5}"}, day=_day(d)),
        call("Agent", {"subagent_type": "developer"}, "10:05:00", day=_day(d))])
        for d in range(0, days, every)]


def test_a_year_long_sparse_window_pages_by_units_and_the_joined_pages_equal_one_pass() -> None:
    records = sparse(400, 17)
    units = int(re.search(r"^local MAX_UNITS_PER_RUN = (\d+)$", TEXT, re.M).group(1))
    assert 30 <= units <= 400
    for query in ("memory", "tools", "skills", "agents", "violations"):
        pages, _ = pages_of(records, query, since=_day(0), until=_day(400))
        assert len(pages) >= 400 // units, query  # per-session queries are paged by units too
        one, _ = run(records, query, since=_day(0), until=_day(400), patch={"MAX_UNITS_PER_RUN": "100000"})
        assert "next_since" not in one, query
        joined = tj.join_pages(query, pages)["rows"]
        assert len(joined) == len(one["rows"]), query
        for x, y in zip(joined, tj.join_pages(query, [one])["rows"], strict=True):
            assert x == pytest.approx(y), query


def test_a_page_stops_at_the_units_cap_and_always_takes_at_least_one_unit() -> None:
    for query in ("skills", "tools"):
        out, fake = run(sparse(30, 1), query, since=_day(0), until=_day(30), patch={"MAX_UNITS_PER_RUN": "0"})
        assert out["next_since"] == _day(1) and len(fake.calls) == 1, query


def test_a_365_day_sparse_window_costs_well_under_the_instruction_cap() -> None:
    records = sparse(365, 9)
    for query in ("agents", "tools", "skills", "memory", "violations", "loads", "procedures"):
        out, n = measure(records, query, since=_day(0), until=_day(365))
        assert 0 < n < 10_000_000, (query, n)  # the runtime stops a run at 50 million
        assert "next_since" in out, query  # a year does not fit one run: it pages
    big = fixture_day()
    dense = [json.loads(json.dumps(r).replace(DAY, _day(d)).replace("m1", f"m1-{d}"))
             for d in range(0, 365, 30) for r in big]
    for query in ("agents", "tools", "skills", "memory", "violations"):
        _, n = measure(dense, query, since=_day(0), until=_day(365))
        assert n < 10_000_000, (query, n)


def test_a_year_of_per_session_rows_in_one_run_costs_millions_not_tens_of_millions() -> None:
    # 365 units, ~4.4K rows held by the end: the copy-and-rescan merge took 50 million instructions
    # (the runtime's whole cap) for this; appending to a running estimate takes about one
    recs = [rec(f"s{d}-{i}", f"{_day(d)}T10:{i:02d}:00Z", calls=[call(GET, {"title": f"t{i}"}, day=_day(d))])
            for d in range(365) for i in range(12)]
    out, n = measure(recs, "memory", since=_day(0), until=_day(365), patch={"MAX_UNITS_PER_RUN": "100000"})
    assert len(out["rows"]) == 365 * 12 and "next_since" not in out
    assert n < 5_000_000, n


def test_a_page_costs_the_same_however_many_rows_it_already_holds() -> None:
    def instructions(days: int) -> int:
        recs = [rec(f"s{d}-{i}", f"{_day(d)}T10:{i:02d}:00Z", calls=[call(GET, {"title": f"t{i}"}, day=_day(d))])
                for d in range(days) for i in range(40)]
        return measure(recs, "memory", since=_day(0), until=_day(days), patch={"MAX_UNITS_PER_RUN": "1000"})[1]

    few, many = instructions(10), instructions(80)
    assert many < few * 8 * 1.5  # linear in the units: appending, never copying the held rows


BUSY_COMMANDS = [
    "git status --short",
    "cd /h/git/nexus && uv run pytest tests/test_x.py -q 2>&1 | tail -5",
    "nx search 'chunk hash' --corpus code | head -20",
    "timeout 60 uv run ruff check src && git diff --stat",
    "git commit -m 'fix the thing\n\nbead nexus-1' && git log -1",
    "find . -name '*.py' -newer x | xargs grep -n foo",
    "cat <<'EOF' > /tmp/x\nline one\nline two\nEOF\nnx memory put p t --tags kind:gate,bead:b",
    "for f in a b c; do echo \"$f\" | wc -l; done",
]


def busy(days: int, *, sessions: int = 100, per_session: int = 149) -> list[dict]:
    """Busy days: ``sessions`` main sessions a day, each with ``per_session`` Bash calls and a dispatch."""
    return [rec(f"b{d}-{i}", f"{_day(d)}T{9 + i // 60:02d}:{i % 60:02d}:00Z", calls=[
        *(call("Bash", {"command": BUSY_COMMANDS[(i + j) % len(BUSY_COMMANDS)]}, "10:01:00", day=_day(d))
          for j in range(per_session)),
        call("Agent", {"subagent_type": "developer"}, "10:02:00", day=_day(d))])
        for d in range(days) for i in range(sessions)]


def _budgeted_pages(records: list[dict], query: str, *, since: str, until: str) -> tuple[list[dict], list[int]]:
    """Every page of a window, with the Lua instructions each took."""
    pages: list[dict] = []
    costs: list[int] = []
    cursor = since
    while True:
        out, n = measure(records, query, since=cursor, until=until)
        pages.append(out)
        costs.append(n)
        assert len(pages) < 100
        if "next_since" not in out:
            return pages, costs
        cursor = out["next_since"]


def _tool_calls_cap() -> int:
    return int(re.search(r"^local MAX_TOOL_CALLS_PER_RUN = (\d+)$", TEXT, re.M).group(1))


def test_a_busy_window_pages_by_tool_calls_and_every_page_is_under_the_instruction_cap() -> None:
    cap = _tool_calls_cap()
    assert 10_000 <= cap <= 20_000
    records = busy(14)  # 100 main sessions x 150 calls (149 Bash and a dispatch) a day: 15,000 calls a day
    pages, costs = _budgeted_pages(records, "violations", since=_day(0), until=_day(14))
    assert len(pages) == 14  # a day is 15,000 calls, so a second day would pass the cap
    assert all(c < 50_000_000 for c in costs), costs  # the runtime stops a run at 50 million
    assert all(c < 20_000_000 for c in costs), costs  # and a page stays well under it
    assert [p.get("next_since") for p in pages] == [_day(d) for d in range(1, 14)] + [None]
    unbounded = {"MAX_TOOL_CALLS_PER_RUN": "10000000", "MAX_SESSIONS_PER_RUN": "10000000", "MAX_UNITS_PER_RUN": "10000000"}
    one, _ = run(records, "violations", since=_day(0), until=_day(14), patch=unbounded)
    assert "next_since" not in one
    joined = tj.join_pages("violations", pages)["rows"]
    assert joined == tj.join_pages("violations", [one])["rows"]


def test_every_query_fills_a_page_to_the_tool_call_bound_under_the_instruction_cap() -> None:
    cap = _tool_calls_cap()
    records = busy(3, per_session=cap // 200 - 1)  # 100 sessions x cap/200 calls: two days fill a page exactly
    assert sum(len(r["tool_calls"]) for r in records if r["start"].startswith(_day(0))) * 2 == cap
    for query in ("agents", "tools", "skills", "memory", "violations", "loads", "procedures"):
        pages, costs = _budgeted_pages(records, query, since=_day(0), until=_day(3))
        assert [p.get("next_since") for p in pages] == [_day(2), None], query
        assert all(0 < c < 20_000_000 for c in costs), (query, costs)


def test_the_tool_call_bound_acts_on_per_session_queries_and_a_page_always_advances() -> None:
    records = busy(4, sessions=4, per_session=5)  # 4 sessions x (5 Bash + 1 dispatch) = 24 calls a day
    for query in ("tools", "skills", "memory"):
        out, fake = run(records, query, since=_day(0), until=_day(4), patch={"MAX_TOOL_CALLS_PER_RUN": "50"})
        assert out["next_since"] == _day(2) and len(fake.calls) == 3, query  # two days are 48 calls, a third 72
    out, _ = run(records, "tools", since=_day(0), until=_day(4), patch={"MAX_TOOL_CALLS_PER_RUN": "47"})
    assert out["next_since"] == _day(1)
    out, _ = run(records, "tools", since=_day(0), until=_day(4), patch={"MAX_TOOL_CALLS_PER_RUN": "0"})
    assert out["next_since"] == _day(1)  # progress: the day's one hour with calls is taken whole
    out, _ = run(records, "tools", since=_day(0), until=_day(4))
    assert "next_since" not in out  # the real bound does not split a small window
    pages, _ = pages_of(records, "tools", since=_day(0), until=_day(4), patch={"MAX_TOOL_CALLS_PER_RUN": "30"})
    assert len(pages) == 4
    whole, _ = run(records, "tools", since=_day(0), until=_day(4))
    assert tj.join_pages("tools", pages)["rows"] == whole["rows"]


def test_a_first_day_over_the_bound_is_divided_by_its_hours_and_the_pages_equal_one_pass() -> None:
    # 30 sessions a day, one an hour from 08:00, 2 calls each (60 a day) against a bound of 25
    records = [rec(f"h{d}-{i}", f"{_day(d)}T{8 + i:02d}:00:00Z",
                   calls=[call("Skill", {"skill": "k"}, day=_day(d)), call("Skill", {"skill": "j"}, day=_day(d))])
               for d in range(2) for i in range(15)]
    patch = {"MAX_TOOL_CALLS_PER_RUN": "25"}
    for query in ("skills", "tools"):
        pages, fakes = pages_of(records, query, since=_day(0), until=_day(2), patch=patch)
        assert pages[0]["next_since"] == f"{_day(0)}T20:00:00Z", query  # 12 hours of 2 calls, 24 <= 25
        assert [c[:2] for c in fakes[0].calls] == [(_day(0), _day(1))]  # the day is read once, divided in hand
        assert fakes[1].calls[0][:2] == (f"{_day(0)}T20:00:00Z", _day(1))  # the rest of the day, once
        assert all(re.fullmatch(r"\d{4}-\d\d-\d\d(T\d\d:00:00Z)?", p["next_since"]) for p in pages[:-1])
        whole, _ = run(records, query, since=_day(0), until=_day(2))
        assert tj.join_pages(query, pages)["rows"] == tj.join_pages(query, [whole])["rows"], query
    # an hour over the bound is taken whole: the page always makes progress
    heavy = [rec("big", f"{_day(0)}T09:00:00Z", calls=[call("Skill", {"skill": "k"}, day=_day(0)) for _ in range(40)])]
    pages, _ = pages_of(heavy, "skills", since=_day(0), until=_day(1), patch=patch)
    assert [p.get("next_since") for p in pages] == [None]  # the day's one hour, whole, on one page
    assert by(pages[0]["rows"], "kind", "name")[("skill", "k")]["count"] == 40


def test_a_row_with_a_key_the_page_already_holds_adds_no_bytes_to_the_running_estimate() -> None:
    def day(n: int, skill: str = "k") -> dict:
        return rec(f"s{n}", f"{_day(n)}T10:00:00Z", calls=[call("Skill", {"skill": skill}, day=_day(n))])

    one, _ = run([day(0)], "skills", since=_day(0), until=_day(1))
    size = _estimate(one["rows"])  # a unit's rows: the skill row and the runs row
    # the second day has the same two keys: the page holds both days in exactly one day's bytes
    out, _ = run([day(0), day(1)], "skills", since=_day(0), until=_day(2), patch={"PAGE_BYTES": str(size)})
    assert "next_since" not in out
    assert by(out["rows"], "kind", "name")[("skill", "k")]["count"] == 2
    # a third day with a new key grows the page, and that is where it stops
    out, _ = run([day(0), day(1), day(2, "z")], "skills", since=_day(0), until=_day(3), patch={"PAGE_BYTES": str(size)})
    assert out["next_since"] == _day(2)
    assert by(out["rows"], "kind", "name")[("skill", "k")]["count"] == 2


# ---- the join ---------------------------------------------------------------------------------


def _agents(**rows: Any) -> dict:
    return {"rows": rows["rows"], "truncated_records": rows.get("t", 0), "until": NOW,
            "since": rows.get("since", "2026-11-01"), **({"next_since": rows["next"]} if rows.get("next") else {})}


def test_join_sums_means_and_maxes_by_key_and_derives_the_mean() -> None:
    key = {"kind": "run", "agent_type": "(unattributed)", "model": "m"}

    def row(runs: int, ctx_total: int, ctx_count: int, mx: int, cost: float, **extra: Any) -> dict:
        return {**key, "dispatches": 0, "runs": runs, "turns": runs * 2, "errors": 1, "context_total": ctx_total,
                "context_count": ctx_count, "max_context": mx, "cost_estimate_usd": cost, "unpriced": 0, **extra}

    p1 = _agents(rows=[row(2, 3000, 2, 2500, 1.5), {**row(1, 0, 0, 0, 0), "model": "other"}], t=1, next="2026-11-02")
    p2 = _agents(rows=[row(1, 500, 1, 9000, 0.25)], t=2, since="2026-11-02")
    joined = tj.join_pages("agents", [p1, p2])
    rows = by(joined["rows"], "model")
    r = rows[("m",)]
    assert (r["runs"], r["turns"], r["errors"]) == (3, 6, 2)  # sum
    assert r["max_context"] == 9000  # max
    assert (r["context_total"], r["context_count"]) == (3500, 3)
    assert r["context_mean"] == pytest.approx(3500 / 3)  # mean of the total over the count, not of means
    assert r["cost_estimate_usd"] == pytest.approx(1.75)
    assert rows[("other",)]["context_mean"] is None
    assert joined["truncated_records"] == 3 and joined["pages"] == 2 and joined["until"] == NOW


def test_join_concatenates_per_session_rows_in_page_order() -> None:
    a = {"rows": [{"session_id": "a", "name": "x", "count": 1}], "truncated_records": 0, "until": NOW,
         "since": "2026-11-01", "next_since": "2026-11-02"}
    b = {"rows": [{"session_id": "b", "name": "x", "count": 1}, {"session_id": "a", "name": "x", "count": 1}],
         "truncated_records": 0, "until": NOW, "since": "2026-11-02"}
    for q in ("memory", "tools", "loads"):
        assert [r["session_id"] for r in tj.join_pages(q, [a, b])["rows"]] == ["a", "b", "a"]


def test_join_violations_keeps_every_pages_denominator_and_derives_a_rate() -> None:
    def page(count: int, n: int, **kw: Any) -> dict:
        return {"rows": [{"rule": "em-dash", "count": count, "main_sessions": n},
                         {"rule": "git-stash", "count": 0, "main_sessions": n}],
                "truncated_records": 0, "until": NOW, "since": "2026-11-01", **kw}

    joined = tj.join_pages("violations", [page(2, 10, next_since="2026-11-02"), page(0, 30, since="2026-11-02")])
    rows = by(joined["rows"], "rule")
    assert (rows[("em-dash",)]["count"], rows[("em-dash",)]["main_sessions"]) == (2, 40)
    assert rows[("em-dash",)]["per_100"] == pytest.approx(5.0)
    assert tj.join_pages("violations", [{"rows": [], "until": NOW}])["rows"] == []


def test_join_refuses_pages_that_do_not_make_one_window() -> None:
    ok = {"rows": [], "truncated_records": 0, "until": NOW, "since": "2026-11-01"}
    with pytest.raises(tj.JoinError, match="unknown query"):
        tj.join_pages("nope", [ok])
    with pytest.raises(tj.JoinError, match="no pages"):
        tj.join_pages("tools", [])
    with pytest.raises(tj.JoinError, match="different windows"):
        tj.join_pages("tools", [{**ok, "next_since": "2026-11-02"}, {**ok, "since": "2026-11-02", "until": "2026-11-09"}])
    with pytest.raises(tj.JoinError, match="before the last"):
        tj.join_pages("tools", [ok, ok])
    with pytest.raises(tj.JoinError, match="not done"):
        tj.join_pages("tools", [{**ok, "next_since": "2026-11-02"}])
    assert tj.join_pages("tools", [{**ok, "next_since": "2026-11-02"}], allow_partial=True)["pages"] == 1


def test_join_refuses_pages_that_do_not_continue_each_other() -> None:
    first = {"rows": [], "truncated_records": 0, "until": NOW, "since": "2026-11-01", "next_since": "2026-11-03"}
    last = {"rows": [], "truncated_records": 0, "until": NOW, "since": "2026-11-03"}
    assert tj.join_pages("tools", [first, last])["pages"] == 2
    with pytest.raises(tj.JoinError, match="starts at '2026-11-02'.*ended at '2026-11-03'"):
        tj.join_pages("tools", [first, {**last, "since": "2026-11-02"}])  # a gap
    with pytest.raises(tj.JoinError, match="starts at"):
        tj.join_pages("tools", [first, {k: v for k, v in last.items() if k != "since"}])  # no since at all
    third = {**last, "since": "2026-11-03", "next_since": "2026-11-05"}
    final = {**last, "since": "2026-11-04"}
    with pytest.raises(tj.JoinError, match="starts at '2026-11-04'.*ended at '2026-11-05'"):
        tj.join_pages("tools", [first, third, final])  # the second join is checked too


def test_real_pages_carry_the_since_the_next_page_continues_from() -> None:
    records = [r for d in ("2026-11-01", "2026-11-02", "2026-11-03") for r in hourly(500, d, 9)]
    pages, _ = pages_of(records, "memory", since="2026-11-01", until="2026-11-04")
    assert [p["since"] for p in pages] == ["2026-11-01", "2026-11-02", "2026-11-03"]
    with pytest.raises(tj.JoinError, match="starts at"):
        tj.join_pages("memory", [pages[0], pages[2]])


def test_the_join_tags_name_exactly_the_fields_the_script_returns() -> None:
    for query in ("agents", "skills", "violations", "procedures"):
        out, _ = run(fixture_day(), query, until="2026-11-04")
        assert {k for r in out["rows"] for k in r} <= tj.SHAPES[query].fields, query
        # every keyed row carries every field (a tag starts at 0), so the sets are equal, none optional
        assert tj.SHAPES[query].fields == {k for r in out["rows"] for k in r}, query
    for query in ("memory", "tools", "loads"):
        out, _ = run(fixture_day(), query, until="2026-11-04")
        assert {k for r in out["rows"] for k in r} <= tj.SESSION_FIELDS[query], query
    assert set(tj.SHAPES) == {"agents", "tools", "skills", "memory", "violations", "loads", "procedures"}
    for q in tj.SHAPES:
        assert re.search(rf"^local function build_{q}\(\)$", TEXT, re.M)


def test_joined_pages_of_a_keyed_query_equal_one_pass_for_every_keyed_query() -> None:
    records = []
    for d in range(1, 7):
        for r in fixture_day():
            r = json.loads(json.dumps(r).replace(DAY, f"2026-11-0{d}"))
            r["session_id"] = f"{r['session_id']}-{d}"
            records.append(r)
    for query in ("agents", "skills", "violations", "procedures"):
        pages, _ = pages_of(records, query, since="2026-11-01", until="2026-11-07", patch={"MAX_SESSIONS_PER_RUN": "5"})
        one, _ = run(records, query, since="2026-11-01", until="2026-11-07")
        assert len(pages) > 1 and "next_since" not in one
        a, b = tj.join_pages(query, pages)["rows"], tj.join_pages(query, [one])["rows"]
        assert len(a) == len(b)
        for x, y in zip(a, b, strict=True):
            assert x == pytest.approx(y), query


# ---- a day that does not fit is divided by the hours of the records already read ----------


def calls_cap(n: int) -> dict[str, str]:
    """A patch that sets the page's tool-call bound to *n*."""
    return {"MAX_TOOL_CALLS_PER_RUN": str(n)}


def _hourly(days: int, hours: tuple[int, ...] = (9, 10, 11, 12), per: int = 10) -> list[dict]:
    """A session at each of ``hours`` every day, each with ``per`` Skill calls."""
    return [rec(f"h{d}-{h}", f"{_day(d)}T{h:02d}:00:00Z",
                calls=[call("Skill", {"skill": "k"}, f"{h:02d}:01:00", day=_day(d)) for _ in range(per)])
            for d in range(days) for h in hours]


def test_a_page_that_fills_inside_a_day_ends_at_an_hour_of_the_records_it_read_with_no_hour_reads() -> None:
    records = _hourly(3)  # 40 calls a day, 10 an hour from 09:00 to 12:00
    out, fake = run(records, "skills", since=_day(0), until=_day(3), patch=calls_cap(60))
    assert out["next_since"] == f"{_day(1)}T11:00:00Z"  # day 0 and two hours of day 1 are 60 calls
    assert [c[:2] for c in fake.calls] == [(_day(0), _day(1)), (_day(1), _day(2))]  # no hour reads
    out, fake = run(records, "skills", since=out["next_since"], until=_day(3), patch=calls_cap(60))
    assert [c[:2] for c in fake.calls][0] == (f"{_day(1)}T11:00:00Z", _day(2))  # the rest of the day, once
    pages, _ = pages_of(records, "skills", since=_day(0), until=_day(3), patch=calls_cap(60))
    whole, _ = run(records, "skills", since=_day(0), until=_day(3))
    assert tj.join_pages("skills", pages)["rows"] == tj.join_pages("skills", [whole])["rows"]


def test_a_day_whose_calls_alone_pass_the_bound_is_divided_by_the_hours_already_read() -> None:
    records = _hourly(1, per=20)  # 80 calls in one day, 20 an hour
    for query in ("skills", "tools", "violations"):
        pages, fakes = pages_of(records, query, since=_day(0), until=_day(1), patch=calls_cap(50))
        assert [p.get("next_since") for p in pages] == [f"{_day(0)}T11:00:00Z", None], query
        assert [len(f.calls) for f in fakes] == [1, 1], query  # each page reads its window once
        whole, _ = run(records, query, since=_day(0), until=_day(1))
        assert tj.join_pages(query, pages)["rows"] == tj.join_pages(query, [whole])["rows"], query


def test_an_hour_over_the_bound_on_its_own_is_taken_whole_so_a_page_advances() -> None:
    records = _hourly(1, hours=(9, 10), per=30)
    pages, fakes = pages_of(records, "skills", since=_day(0), until=_day(1), patch=calls_cap(20))
    assert [p.get("next_since") for p in pages] == [f"{_day(0)}T10:00:00Z", None]
    assert [len(f.calls) for f in fakes] == [1, 1]


def test_a_start_the_script_cannot_place_never_divides_the_records_read_and_each_session_counts_once() -> None:
    # The verb takes any ISO offset; the script reads only UTC. Put in the wrong hour, the 12:00
    # session would be admitted with 09:00 and read again by the page that starts at 10:00.
    records = _hourly(1, per=20)
    records[-1]["start"] = records[-1]["start"].replace("Z", "+00:00")
    pages, fakes = pages_of(records, "skills", since=_day(0), until=_day(1), patch=calls_cap(50))
    assert fakes[0].calls[1][:2] == (_day(0), f"{_day(0)}T01:00:00Z")  # the old hour reads, not the records in hand
    whole, _ = run(records, "skills", since=_day(0), until=_day(1))
    assert tj.join_pages("skills", pages)["rows"] == tj.join_pages("skills", [whole])["rows"]


def test_agents_keeps_whole_days_so_a_subagent_is_matched_to_its_parent_on_every_page() -> None:
    # A parent at 14:50 dispatching a reviewer, its subagent at 15:05, on a day the page has no
    # room for after a 25-call day: divided by hour, the two would be reduced apart and the run
    # would read as unattributed (critic r1, nexus-qo7ie.38).
    d0, d1 = "2026-11-03", "2026-11-04"
    fill = [rec(f"f{i}", f"{d0}T10:0{i}:00Z", calls=[call("Read", {}, "10:00:00", day=d0) for _ in range(5)])
            for i in range(5)]
    parent = rec("m", f"{d1}T14:50:00Z", calls=[call("Agent", {"subagent_type": "reviewer"}, "14:55:00", day=d1)]
                 + [call("Read", {}, "14:56:00", day=d1) for _ in range(4)])
    child = rec("m", f"{d1}T15:05:00Z", main=False, agent="a1",
                calls=[call("Read", {}, "15:06:00", day=d1) for _ in range(2)])
    records = [*fill, parent, child]
    pages, _ = pages_of(records, "agents", since=d0, until="2026-11-05", patch=calls_cap(30))
    assert [p.get("next_since") for p in pages] == [d1, None]
    whole, _ = run(records, "agents", since=d0, until="2026-11-05")
    joined = tj.join_pages("agents", pages)["rows"]
    assert joined == tj.join_pages("agents", [whole])["rows"]
    assert any(r.get("agent_type") == "reviewer" for r in joined)


def test_a_long_fraction_at_the_end_of_an_hour_stays_in_that_hour() -> None:
    # 05:59:59.9999999 as a float rounds to 06:00; the verb puts it in hour 5, so must the script,
    # or a page ending at 06:00 drops it.
    day = "2026-11-03"
    records = [rec("b", f"{day}T05:10:00Z", calls=[call("Skill", {"skill": "k"}, "05:11:00", day=day)]),
               rec("a", f"{day}T05:59:59.9999999Z", calls=[call("Skill", {"skill": "k"}, "05:59:59", day=day)]),
               rec("c", f"{day}T06:10:00Z", calls=[call("Skill", {"skill": "k"}, "06:11:00", day=day) for _ in range(10)])]
    for query in ("skills", "tools"):
        pages, _ = pages_of(records, query, since=day, until="2026-11-04", patch=calls_cap(2))
        whole, _ = run(records, query, since=day, until="2026-11-04")
        assert tj.join_pages(query, pages)["rows"] == tj.join_pages(query, [whole])["rows"], query
