# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qo7ie.19 (RDR-238 Phase 3 Step 3a): ``nx memory curate --cold``.

The planted world goes through the real run path (``run_procedure`` -> ``run_script`` -> the
``transcripts`` procedure in its worker) over fixture transcripts under ``CLAUDE_CONFIG_DIR``, in a
fixture git repository, and runs once per module: every per-kind test reads the same report. T2 is a
small fake object (``get_projects_with_prefix``, ``get_all``), so no test mints a tenant. The guards a
fixture cannot reach (a failed query, a pager) use a stub procedure with a hard call limit.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.lua_runtime as lua_runtime
from nexus import memory_curate as mc
from nexus.memory_curate import cold
from nexus.cli import main
from nexus.db.t2.http_tuple_store import HttpTupleStore
from nexus.db.t2.records import SubspaceCensus
from nexus.lua_runtime import runner
from tests.test_memory_curate import _fm, _git, _make_repo, _memdir
from tests._module_seam import setattr_in
from tests.test_memory_curate_reads import _NOW, _session, world  # noqa: F401 — `world` is a fixture

GET = "mcp__plugin_conexus_nexus__memory_get"
SEARCH = "mcp__plugin_conexus_nexus__memory_search"
CAT_SEARCH = "mcp__plugin_conexus_nexus-catalog__search"
LABELS = ("memory files", "T2 namespaces", "MCP tools", "CLI verbs", "injected context", "tuple subspaces")
TRANSCRIPT_LABELS = LABELS[:5]  # the kinds counted from transcripts; the tuple row reads the engine instead
OLD = "2026-09-01T00:00:00+00:00"
EMPTY: dict = {"rows": []}
STAMP = "2026-11-08T10:00:01Z"
#: A quiet window that is not an empty one: a T2 read of another project and one Bash call.
MEM: dict = {"rows": [{"kind": "t2_get", "name": "elsewhere/x", "count": 1, "last_use": STAMP}]}
TOOLS: dict = {"rows": [{"kind": "tool", "name": "Bash", "count": 1, "last_use": STAMP}]}
LOADS: dict = {"rows": [{"kind": "instructions", "name": "/x/CLAUDE.md", "count": 1, "last_use": STAMP}]}


def _rows(lines: list[str], label: str) -> list[str]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{label}:"))
    out: list[str] = []
    for ln in lines[start + 1:]:
        if not ln.startswith("  - "):
            break
        out.append(ln[4:])
    return out


def _stores(lines: list[str], label: str) -> list[str]:
    return [r.split(" | ")[0] for r in _rows(lines, label)]


def _block(lines: list[str], label: str) -> list[str]:
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{label}:"))
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if any(ln.startswith(f"{other}:") for other in LABELS):
            break
        out.append(ln)
    return out


def _hooked(lines: list[str], label: str, kind: str = "hook") -> list[str]:
    """The stores a section prints as invoked by a hook ("hook") or run by a slash command ("slash"),
    not seen in transcripts."""
    out: list[str] = []
    current = ""
    for ln in _block(lines, label):
        if ln.startswith("  invoked by a hook, not seen in transcripts:"):
            current = "hook"
        elif ln.startswith("  run by a slash command, not seen in transcripts:"):
            current = "slash"
        elif ln.startswith("    - ") and current == kind:
            out.append(ln.strip()[2:].split(" | ")[0])
    return out


def _head(lines: list[str], label: str) -> str:
    return next(ln for ln in lines if ln.startswith(f"{label}:"))


class FakeT2:
    """The two reads the cold section makes of a ``T2Database``; no tenant."""

    def __init__(self, projects: dict[str, list[dict]]) -> None:
        self.projects = projects

    def get_projects_with_prefix(self, prefix: str) -> list[dict]:
        return [{"project": p} for p in sorted(self.projects) if p.startswith(prefix)]

    def get_all(self, project: str) -> list[dict]:
        return list(self.projects[project])


def _t2(projects: dict[str, list[dict]]):
    @contextlib.contextmanager
    def open_db() -> Iterator[FakeT2]:
        yield FakeT2(projects)

    return open_db


def _row(project: str, title: str, ts: str) -> dict:
    return {"id": f"{project}/{title}", "project": project, "title": title, "timestamp": ts, "content": "x"}


def _stamp(path: Path, iso: str) -> None:
    t = datetime.fromisoformat(iso).timestamp()
    os.utime(path, (t, t))


def _attach(path: Path, sid: str, cwd: Path, att: dict) -> None:
    rec = {"type": "attachment", "timestamp": "2026-11-08T10:30:00Z", "sessionId": sid, "isSidechain": False,
           "cwd": str(cwd), "attachment": att}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _plant(tmp: Path) -> dict[str, Path]:
    repo = _make_repo(tmp, "coldrepo")
    config = tmp / "cfg"
    mem = _memdir(config, repo)
    (mem / "MEMORY.md").write_text(
        "- [Used](used.md) - read\n- [Stale](stale.md) - nobody opens it\n- [Edited](edited.md) - x\n"
        "- [Row](rowwritten.md) - y\n")
    for n in ("used", "stale", "edited", "rowwritten"):
        (mem / f"{n}.md").write_text(_fm(n, f"{n} file"))
        _stamp(mem / f"{n}.md", "2026-09-01T00:00:00+00:00")
    _stamp(mem / "edited.md", "2026-11-08T11:00:00+00:00")  # written in the window, by its mtime
    # Tracked instruction files: a root pair (not nested), a nested pair joined by a symlink, a lone nested one.
    (repo / "AGENTS.md").write_text("root\n")
    (repo / "CLAUDE.md").symlink_to("AGENTS.md")
    (repo / "docs").mkdir()
    (repo / "docs" / "AGENTS.md").write_text("docs rules\n")
    (repo / "docs" / "CLAUDE.md").symlink_to("AGENTS.md")
    (repo / "sub").mkdir()
    (repo / "sub" / "CLAUDE.md").write_text("x" * 400)
    for d in ("other", "viaitx", "lone", "linked", "tests/fx", "docs/e2e"):
        (repo / d).mkdir(parents=True)
    (repo / "other" / "CLAUDE.md").write_text("loaded from another worktree\n")
    (repo / "viaitx" / "CLAUDE.md").write_text("loaded as an instructions attachment\n")
    (repo / "lone" / "AGENTS.md").write_text("no CLAUDE.md beside it\n")
    (repo / "linked" / "CLAUDE.md").symlink_to("../AGENTS.md")  # the root file: always loaded
    (repo / "tests" / "fx" / "CLAUDE.md").write_text("a fixture\n")
    (repo / "docs" / "e2e" / "CLAUDE.md").write_text("an e2e fixture\n")
    (repo / "tests" / "AGENTS.md").write_text("tests/ is a real instruction directory\n")
    (repo / "tests" / "CLAUDE.md").symlink_to("AGENTS.md")
    _git(repo, "add", "AGENTS.md", "CLAUDE.md", "docs/AGENTS.md", "docs/CLAUDE.md", "sub/CLAUDE.md", "other/CLAUDE.md",
         "viaitx/CLAUDE.md", "lone/AGENTS.md", "linked/CLAUDE.md", "tests/fx/CLAUDE.md", "docs/e2e/CLAUDE.md",
         "tests/AGENTS.md", "tests/CLAUDE.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "files")
    project = mem.parent
    _session(project / "s0.jsonl", "s0", repo, "2026-10-20T10:00:00Z", [("Bash", {"command": "ls"})])
    _session(project / "s1.jsonl", "s1", repo, "2026-11-08T10:00:00Z", [
        ("Read", {"file_path": str(mem / "used.md")}),
        ("Write", {"file_path": str(mem / "rowwritten.md"), "content": "x"}),
        (GET, {"project": "coldrepo_used", "title": "x"}),
        (SEARCH, {"project": "coldrepo_searched", "query": "q"}),
        (CAT_SEARCH, {"query": "q"}),
        ("mcp__other__thing", {}),
        ("Bash", {"command": "nx search foo"}),
        ("Bash", {"command": "nx memory curate --files"}),
    ])
    _attach(project / "s1.jsonl", "s1", repo, {
        "type": "nested_memory", "path": str(repo / "docs" / "AGENTS.md"),
        "content": {"type": "Project", "content": "docs rules"}})
    _attach(project / "s1.jsonl", "s1", repo, {
        "type": "nested_memory", "path": "/elsewhere/other-worktree/other/CLAUDE.md",  # a session in another worktree
        "content": {"type": "Project", "content": "other"}})
    _attach(project / "s1.jsonl", "s1", repo, {
        "type": "instructions", "files": [
            {"path": str(repo / "CLAUDE.md"), "type": "Project", "content": "root"},
            {"path": str(repo / "viaitx" / "CLAUDE.md"), "type": "Project", "content": "x"}]})
    hooks = tmp / "hooks"
    (hooks / "scripts").mkdir(parents=True)
    (hooks / "hooks.json").write_text(json.dumps({"hooks": {
        "SubagentStart": [{"hooks": [{"type": "mcp_tool", "server": "plugin:conexus:nexus", "tool": "store_put",
                                      "input": {}}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "uv", "args": ["run", "x.py"]}]}]}}))
    (hooks / "scripts" / "h.py").write_text(
        'state = _run_nx(["memory", "rollup", entry_id, "--project", P], cwd, 5)\n'
        'ok = run_cmd(["nx", "doctor"], timeout=9)\nnote = "nx search appears in prose only"\n')
    (tmp / "srchooks").mkdir()
    (tmp / "srchooks" / "self_gc.py").write_text(
        'argv = nx_argv_for(nx, "self", "gc")\ndef nx_argv(*args: str):\n    return args\n')
    (tmp / "srchooks" / "sub.py").write_text(
        'a = nx_argv("catalog", "links-for-file", "--for-model", fp)\nb = nx_argv("--version")\n')
    (tmp / "commands").mkdir()
    (tmp / "commands" / "debug.md").write_text(
        "# debug\n\n!`nx command-context debug`\n\nprose: nx search appears here only\n!`ls -l`\n")
    return {"repo": repo, "config": config, "mem": mem, "hooks": hooks, "srchooks": tmp / "srchooks",
            "commands": tmp / "commands"}


def _no_tuple_engine():
    raise RuntimeError("no tuple engine in this test")


@pytest.fixture(autouse=True)
def _never_the_ambient_tuple_engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that did not ask for the engine substrate must not reach one: the default tuple store is the
    configured service, which on a developer's machine is a real tenant."""
    if "t2_service_env" not in request.fixturenames:
        monkeypatch.setattr(cold, "_default_tuple_store", _no_tuple_engine)


_PROJECTS = {
    "coldrepo": [_row("coldrepo", "a", OLD)],
    "coldrepo_old": [_row("coldrepo_old", "a", OLD), _row("coldrepo_old", "b", "2026-09-02T00:00:00+00:00")],
    "coldrepo_used": [_row("coldrepo_used", "x", OLD)],
    "coldrepo_searched": [_row("coldrepo_searched", "y", OLD)],
    "coldrepo_fresh": [_row("coldrepo_fresh", "z", "2026-11-08T09:00:00+00:00")],
    # read in the window by something the transcripts do not show (a Bash `nx memory get`): the engine stamped it
    "coldrepo_accessed": [{**_row("coldrepo_accessed", "q", OLD), "last_accessed": "2026-11-08T09:30:00Z"}],
    "coldrepo_stale_access": [{**_row("coldrepo_stale_access", "q", OLD), "last_accessed": OLD}],
    "coldrepo_never_read": [{**_row("coldrepo_never_read", "q", OLD), "last_accessed": ""}],
    "coldrepo2": [_row("coldrepo2", "n", OLD)],  # another repository's project: not this repo's
}


@pytest.fixture(scope="module")
def planted(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    with pytest.MonkeyPatch.context() as mp:
        w = _plant(tmp_path_factory.mktemp("cold"))
        mp.chdir(w["repo"])
        mp.setenv("CLAUDE_CONFIG_DIR", str(w["config"]))
        mp.setenv("NX_NO_TELEMETRY", "1")
        w["lines"] = cold.cold_lines(
            30, _NOW, w["repo"], "coldrepo", w["mem"], w["repo"], _t2(_PROJECTS), hooks_dir=w["hooks"],
            src_hooks_dir=w["srchooks"], commands_dir=w["commands"], open_tuples=_no_tuple_engine)
        yield w


# ── each kind: a planted cold store is listed, a used one is not ─────────────────────


def test_memory_files_list_the_unused_and_unwritten_and_not_the_used(planted: dict) -> None:
    lines = planted["lines"]
    assert _stores(lines, "memory files") == ["stale.md"]
    row = _rows(lines, "memory files")[0]
    assert "last write 2026-09-01" in row and "last use none in the window" in row
    assert "bytes" in row and row.endswith("owner coldrepo")
    # used.md (Read), edited.md (mtime in the window), rowwritten.md (a Write row), MEMORY.md (K6): none listed.
    assert _head(lines, "memory files").startswith("memory files: 1 cold of 4")


def test_t2_namespaces_list_the_idle_projects_of_this_repo_only(planted: dict) -> None:
    lines = planted["lines"]
    assert _stores(lines, "T2 namespaces") == [
        "coldrepo", "coldrepo_never_read", "coldrepo_old", "coldrepo_stale_access"]
    assert _head(lines, "T2 namespaces").startswith("T2 namespaces: 4 cold of 8 ")
    old = next(r for r in _rows(lines, "T2 namespaces") if r.startswith("coldrepo_old "))
    assert "last write 2026-09-02" in old and "last use none in the window" in old
    assert "2 rows" in old and old.endswith("owner coldrepo")
    # used (memory_get), searched (memory_search), fresh (a row in the window), accessed (last_accessed in the window:
    # a Bash `nx memory get` the transcripts do not show) and coldrepo2 (not this repo): none listed.
    limit = " ".join(_block(lines, "T2 namespaces"))
    assert "Bash `nx memory` reads are attributed to a project only through last_accessed" in limit
    assert "reads as written after the first run" in limit


def test_mcp_tools_list_every_registered_tool_never_called_keyed_by_server(planted: dict) -> None:
    lines = planted["lines"]
    registry = cold.mcp_registry()
    expected = {f"{server}/{tool}" for server, tool in registry} - {"nexus/memory_get", "nexus/memory_search", "nexus-catalog/search", "nexus/store_put"}
    assert set(_stores(lines, "MCP tools")) == expected
    assert "nexus/search" in expected and "nexus/memory_get" not in expected  # one short name, two servers
    assert _head(lines, "MCP tools") == f"MCP tools: {len(expected)} cold of {len(registry)} registered (1 invoked by a hook)"
    # store_put is invoked by a hook (hooks.json mcp_tool): printed as such, not as cold
    assert _hooked(lines, "MCP tools") == ["nexus/store_put"] and "nexus/store_put" not in _stores(lines, "MCP tools")
    assert "invoked by a hook, not seen in transcripts: 1" in "\n".join(_block(lines, "MCP tools"))
    assert not any("other" in s for s in _stores(lines, "MCP tools"))  # a foreign server is dropped, never counted
    assert all("tokens" in r and r.endswith(r.split(" | ")[0].split("/")[0]) for r in _rows(lines, "MCP tools"))


def test_cli_verbs_list_every_leaf_never_used(planted: dict) -> None:
    lines = planted["lines"]
    leaves = {u.name for u in cold.cli_units()}
    hooked = {"nx doctor", "nx memory rollup"}  # the planted hook script runs both as subprocesses
    hooked |= {"nx self gc", "nx catalog links-for-file"}  # src/nexus/hooks argvs
    slashed = {"nx command-context debug"}  # a !`nx ...` line of a slash command
    assert set(_stores(lines, "CLI verbs")) == leaves - {"nx search", "nx memory curate"} - hooked - slashed
    assert set(_hooked(lines, "CLI verbs")) == hooked and set(_hooked(lines, "CLI verbs", "slash")) == slashed
    assert "nx memory list" in _stores(lines, "CLI verbs")
    assert _head(lines, "CLI verbs") == (
        f"CLI verbs: {len(leaves) - 7} cold of {len(leaves)} (4 invoked by a hook, 1 run by a slash command)")
    assert "run by a slash command, not seen in transcripts: 1" in "\n".join(_block(lines, "CLI verbs"))
    for label in ("MCP tools", "CLI verbs"):  # the limits a transcript count has, printed under both
        limits = " ".join(_block(lines, label))
        for needle in ("calls by hooks and subprocesses", "terminal and CI use", "bash -c and ssh bodies",
                       "a group option before the sub verb", "other repositories' sessions",
                       "slash-command expansions", "the harness hooks"):
            assert needle in limits, (label, needle)


def test_injected_context_lists_the_nested_instruction_file_never_loaded(planted: dict) -> None:
    lines = planted["lines"]
    assert _stores(lines, "injected context") == ["sub/CLAUDE.md", "tests/AGENTS.md (also tests/CLAUDE.md)"]  # other/ was loaded from another worktree's path,
    # viaitx/ only through an `instructions` attachment, linked/ is the root file: none of them listed
    row = _rows(lines, "injected context")[0]
    assert "~100 tokens" in row and "last use none in the window" in row
    # docs/AGENTS.md was loaded (its CLAUDE.md link names the same file) and the root pair is not nested.
    assert _head(lines, "injected context").startswith("injected context: 2 cold of 5")
    block = "\n".join(_block(lines, "injected context"))
    assert "not judged: no CLAUDE.md, Claude Code loads only CLAUDE.md on demand: lone/AGENTS.md" in block
    assert "not judged: fixtures (below tests/ or under a fixtures or e2e directory): 2 files" in block
    assert "tests/CLAUDE.md" not in block.split("not judged: fixtures")[1].split("\n")[0]  # tests/CLAUDE.md itself is judged
    assert "linked/CLAUDE.md" in block and "symlinked to the root instruction file" in block
    assert "on-demand rule files under the repository's .claude directory are not covered" in block


# ── K6 ───────────────────────────────────────────────────────────────────────────────


def test_k6_always_loaded_items_are_excluded_and_the_report_says_so(planted: dict) -> None:
    lines = planted["lines"]
    text = "\n".join(lines)
    assert "excluded (K6), no 'never used' claim: MEMORY.md" in text
    assert "2 files loaded by an `instructions` attachment in the window" in text
    for store in ("MEMORY.md", "CLAUDE.md", "AGENTS.md"):  # whole-store names, never a row
        assert not any(s == store for label in LABELS for s in _stores(lines, label))
    assert "tool definitions" in text
    assert mc.READ_SCOPE_NOTE in text
    assert "no write and no use" in text and "covered 2026-" in text and "2 sessions started in it" in text


# ── non-vacuity and names ────────────────────────────────────────────────────────────


def test_the_registry_and_the_cli_tree_are_not_empty() -> None:
    registry = cold.mcp_registry()
    assert len(registry) >= 40 and ("nexus", "memory_get") in registry and ("nexus-catalog", "search") in registry
    assert all(tokens > 0 for tokens, _ in registry.values())
    units = cold.cli_units()
    names = {u.name for u in units}
    assert len(units) >= 30 and {"nx search", "nx memory curate", "nx doctor"} <= names
    assert len(names) == len(units)


@pytest.mark.parametrize(("name", "expected"), [
    ("mcp__plugin_conexus_nexus__memory_get", ("nexus", "memory_get")),
    ("mcp__plugin_conexus_nexus-catalog__search", ("nexus-catalog", "search")),
    ("mcp__nexus__search", ("nexus", "search")),
    ("mcp__nexus_catalog__search", ("nexus-catalog", "search")),
    ("mcp__plugin_sn_serena__find_symbol", None),
    ("mcp__other__thing", None),
    ("mcp__plugin_conexus_nexus-other__x", None),
    ("Bash", None),
    ("mcp__nexus", None),
])
def test_a_transcript_tool_name_maps_to_its_server_and_short_name(name: str, expected: tuple[str, str] | None) -> None:
    assert cold.nexus_tool(name) == expected


# ── failure paths: never a cold list, and the command exits 0 ────────────────────────


def _no_rows(lines: list[str]) -> bool:
    return not any(ln.startswith("  - ") for ln in lines)


def test_without_the_runtime_every_transcript_row_says_not_available(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    for label in TRANSCRIPT_LABELS:
        assert _head(lines, label).startswith(f"{label}: not available: unavailable: "), label
    assert _no_rows(lines)
    assert any(ln.startswith("window: covered ") for ln in lines)  # the span prints on failure too


def test_the_command_exits_0_with_not_available_rows_without_the_runtime(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")

    def no_store():
        raise RuntimeError("no store in this test")

    with patch("nexus.commands.memory.t2_handle", side_effect=no_store):
        res = CliRunner().invoke(main, ["memory", "curate", "--cold", "--days", "7"])
    assert res.exit_code == 0, res.output
    assert "== --cold ==" in res.output and "== --files ==" not in res.output
    assert "memory files: not available: " in res.output and "CLI verbs: not available: " in res.output
    assert "report not stored: not available" in res.output
    assert not any(ln.startswith("  - ") for ln in res.output.splitlines())


def test_the_command_hands_days_and_the_t2_handle_to_the_cold_section(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    seen: dict = {}

    def fake(days, now, cwd, repo, directory, root, open_db):
        seen.update(days=days, repo=repo, directory=directory, root=root, open_db=open_db)
        return ["the cold lines"]

    monkeypatch.setattr(cold, "cold_lines", fake)
    with patch("nexus.commands.memory.t2_handle", side_effect=RuntimeError("no store")):
        res = CliRunner().invoke(main, ["memory", "curate", "--cold", "--days", "9"])
    assert res.exit_code == 0, res.output
    assert "== --cold ==\nthe cold lines" in res.output
    assert seen["days"] == 9 and seen["directory"] == world["mem"] and seen["root"] == world["repo"]
    assert seen["repo"] == "readrepo" and callable(seen["open_db"])


def test_with_no_session_started_in_the_window_nothing_is_listed(world: dict[str, Path]) -> None:  # noqa: F811
    _session(world["project"] / "long.jsonl", "long", world["repo"], "2026-10-01T10:00:00Z",
             [("Read", {"file_path": str(world["mem"] / "read_me.md")})], resumed="2026-11-08T10:00:00Z")
    lines = cold.cold_lines(7, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    for label in TRANSCRIPT_LABELS:
        assert _head(lines, label) == f"{label}: not available: no session of this repository started in the window"
    assert _no_rows(lines)


@pytest.fixture
def stubbed(world: dict[str, Path], monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    for f in world["mem"].glob("*.md"):
        _stamp(f, "2026-09-01T00:00:00+00:00")  # written before the window: a fresh mtime is a write in it
    calls: list[str] = []

    def install(by_query: dict):
        def stub(name: str, args: dict) -> object:
            calls.append(args["query"])
            assert len(calls) <= 20, "a broken guard would loop here"
            got = by_query[args["query"]]
            if isinstance(got, Exception):
                raise got
            return {"since": args["since"], "until": args["until"], **got} if isinstance(got, dict) else got

        monkeypatch.setattr(runner, "run_procedure", stub)
        return calls

    return install


def test_a_failed_tools_query_blanks_only_the_tool_rows(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    calls = stubbed({"memory": MEM, "loads": LOADS, "tools": ["not", "a", "page"]})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    reason = "not available: result: the tools query returned list, not a page"
    assert _head(lines, "MCP tools") == f"MCP tools: {reason}"
    assert _head(lines, "CLI verbs") == f"CLI verbs: {reason}"
    assert _head(lines, "memory files").startswith("memory files: 2 cold of 2")  # never.md and read_me.md, unread
    assert "T2 namespaces: " in "\n".join(lines) and "not available" not in _head(lines, "T2 namespaces")
    assert calls.count("tools") == 1  # asked once for both kinds


def test_a_failed_memory_query_blanks_the_file_and_t2_rows_and_a_failed_loads_query_the_injected_ones(
    world: dict[str, Path], stubbed,  # noqa: F811
) -> None:
    stubbed({"memory": ["x"], "loads": ["y"], "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    for label in ("memory files", "T2 namespaces"):
        assert _head(lines, label) == f"{label}: not available: result: the memory query returned list, not a page"
    assert _head(lines, "injected context") == "injected context: not available: result: the loads query returned list, not a page"
    assert _head(lines, "MCP tools").startswith("MCP tools: ") and "not available" not in _head(lines, "MCP tools")


def test_cut_records_say_the_cold_lists_may_hold_a_used_store(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    cut = {**MEM, "truncated_records": 2}
    stubbed({"memory": cut, "loads": LOADS, "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    assert any("records were cut" in ln and "may be listed although they were used" in ln for ln in lines)


def test_a_memory_directory_the_query_cannot_see_blanks_only_the_file_rows(
    world: dict[str, Path], stubbed, tmp_path: Path,  # noqa: F811
) -> None:
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text(_fm("A", "x"))
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", notes, world["repo"], _t2(_PROJECTS))
    assert "memory files: not available: the memory query only sees paths containing /memory/" in _head(lines, "memory files")
    assert "not available" not in _head(lines, "T2 namespaces")


def test_a_t2_that_cannot_be_read_blanks_only_the_t2_rows(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})

    @contextlib.contextmanager
    def broken() -> Iterator[None]:
        raise RuntimeError("t2 is down")
        yield  # pragma: no cover

    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], broken)
    assert _head(lines, "T2 namespaces") == "T2 namespaces: not available: RuntimeError: t2 is down"
    assert "not available" not in _head(lines, "memory files")


# ── review round 1 ───────────────────────────────────────────────────────────────────


def test_a_memory_file_used_only_through_a_subagent_load_is_not_cold(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    load = {"kind": "loaded", "name": str(world["mem"] / "read_me.md"), "count": 1, "last_use": STAMP}
    stubbed({"memory": {"rows": [*MEM["rows"], load]}, "loads": LOADS, "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    assert _stores(lines, "memory files") == ["never.md"]


def test_no_memory_directory_is_not_available_with_its_reason(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", None, world["repo"], _t2(_PROJECTS))
    assert _head(lines, "memory files") == "memory files: not available: no memory directory found for this repository"


def test_a_failed_git_listing_is_not_available_never_zero_cold(world: dict[str, Path], stubbed, tmp_path: Path) -> None:  # noqa: F811
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})
    plain = tmp_path / "not-a-checkout"
    plain.mkdir()
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], plain, _t2(_PROJECTS))
    assert _head(lines, "injected context") == f"injected context: not available: git ls-files failed in {plain}"
    assert "0 cold of 0" not in "\n".join(_block(lines, "injected context"))


def test_a_query_that_succeeds_with_no_rows_for_started_sessions_is_not_available(
    world: dict[str, Path], stubbed,  # noqa: F811
) -> None:
    stubbed({"memory": EMPTY, "loads": EMPTY, "tools": EMPTY})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    quiet = "not available: the {} query returned no {} for 1 session"
    assert _head(lines, "MCP tools") == "MCP tools: " + quiet.format("tools", "calls")
    assert _head(lines, "CLI verbs") == "CLI verbs: " + quiet.format("tools", "calls")
    for label in ("memory files", "T2 namespaces"):
        assert _head(lines, label) == f"{label}: " + quiet.format("memory", "memory reads, writes or T2 calls")
    assert _head(lines, "injected context") == "injected context: " + quiet.format("loads", "instruction loads")
    assert _no_rows(lines)


def test_a_memory_search_naming_no_project_counts_as_a_use_of_every_project(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    stubbed({"memory": {"rows": [{"kind": "t2_search", "name": "(all)", "count": 1, "last_use": STAMP}]},
             "loads": LOADS, "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "coldrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    assert _head(lines, "T2 namespaces").startswith("T2 namespaces: 0 cold of 8 ")
    assert "a memory_search naming no project counts as a use of every project" in " ".join(_block(lines, "T2 namespaces"))


def test_the_injected_kind_is_refused_on_windows_like_the_file_kind(
    world: dict[str, Path], stubbed, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})
    setattr_in(monkeypatch, cold, "os.name", "nt")  # this module's os only (tests/_module_seam.py)
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    assert "Windows paths" in _head(lines, "injected context") and "not available" in _head(lines, "injected context")


def test_one_failed_query_is_run_once_and_blanks_both_kinds_that_share_it(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    calls = stubbed({"memory": MEM, "loads": LOADS, "tools": RuntimeError("boom")})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS))
    assert _head(lines, "MCP tools") == "MCP tools: not available: RuntimeError: boom"
    assert _head(lines, "CLI verbs") == "CLI verbs: not available: RuntimeError: boom"
    assert calls.count("tools") == 1


def test_the_hook_scan_reads_mcp_tool_entries_and_nx_argvs_from_the_scripts(tmp_path: Path) -> None:
    hooks = tmp_path / "h"
    (hooks / "scripts" / "routing").mkdir(parents=True)
    (hooks / "hooks.json").write_text(json.dumps({"hooks": {"SubagentStart": [{"hooks": [
        {"type": "mcp_tool", "server": "plugin:conexus:nexus", "tool": "hook_subagent_start"},
        {"type": "mcp_tool", "server": "plugin:conexus:nexus-catalog", "tool": "show"},
        {"type": "mcp_tool", "server": "plugin:other:thing", "tool": "x"},
        # nx_launch.py runs nx-hook and nx-mcp, not nx verbs: never read as one
        {"type": "command", "command": "uv",
         "args": ["run", "${CLAUDE_PLUGIN_ROOT}/launch/nx_launch.py", "nx-hook", "preflight"]}]}]}}))
    (hooks / "scripts" / "routing" / "a.py").write_text(
        'x = _run_nx(["command-context", "continuation"], cwd, 1)\n'
        'y = _run_nx(["memory", "get", row_id, "--project", P], cwd, 1)\n'
        'run_cmd(["nx", "self", "install"] if g else ["nx", "upgrade"], timeout=1)\n'
        'z = _run_nx(["--version"], cwd, 1)\nw = _run_nx([variable], cwd, 1)\n')
    scan = cold.hook_invoked(hooks)
    assert scan.tools == {("nexus", "hook_subagent_start"), ("nexus-catalog", "show")}
    assert scan.verbs == {("command-context", "continuation"), ("memory", "get"), ("self", "install"),
                          ("upgrade", None)}
    none = cold.hook_invoked(tmp_path / "missing")
    assert none.tools == set() and none.verbs == set() and none.note.startswith("no hook directory")


def test_the_scan_reads_src_hook_argvs_and_slash_command_lines(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "self_gc.py").write_text('argv = nx_argv_for(nx, "self", "gc")\ndef nx_argv(*args: str):\n    pass\n')
    (src / "sub.py").write_text('x = nx_argv("catalog", "links-for-file", "--for-model", fp)\ny = nx_argv("--version")\n'
                                'z = nx_argv(variable)\nw = nx_argv_for(nx, "upgrade", "--auto")\n')
    cmds = tmp_path / "cmds"
    cmds.mkdir()
    (cmds / "a.md").write_text("# a\n\n!`nx command-context debug`\n!`nx rdr preamble rdr-audit`\n"
                               "nx search is prose\n!`ls -l`\n!`nx --version`\n")
    (cmds / "b.md").write_text("```\n!`nx tuple stats --json`\n```\n")
    hooks = tmp_path / "h"
    hooks.mkdir()
    (hooks / "hooks.json").write_text(json.dumps({"hooks": {}}))
    scan = cold.hook_invoked(hooks, src_hooks_dir=src, commands_dir=cmds)
    assert scan.verbs == {("self", "gc"), ("catalog", "links-for-file"), ("upgrade", None)}
    assert scan.slash == {("command-context", "debug"), ("rdr", "preamble"), ("tuple", "stats")}
    assert "slash-command expansions" in scan.note and "the harness hooks" in scan.note
    gone = cold.hook_invoked(hooks, src_hooks_dir=tmp_path / "nope", commands_dir=tmp_path / "nope2")
    assert gone.verbs == set() and gone.slash == set()  # a missing root is no hit and no error


def test_only_files_deeper_than_tests_or_under_fixtures_or_e2e_are_fixtures() -> None:
    fixture = cold._is_fixture
    assert not fixture("tests/CLAUDE.md") and not fixture("tests/AGENTS.md") and not fixture("docs/CLAUDE.md")
    assert fixture("tests/fx/CLAUDE.md") and fixture("tests/a/b/AGENTS.md")
    assert fixture("docs/e2e/CLAUDE.md") and fixture("src/fixtures/x/CLAUDE.md") and fixture("tests/e2e/CLAUDE.md")
    assert not fixture("src/nexus/mcp/CLAUDE.md")


# ── tuple subspaces (nexus-qo7ie.20, RDR-238 Phase 3 Step 3b) ────────────────────────
#
# Two tests mint a tenant (tests/storage_touch_ceiling.toml counts them): one report over a real engine with
# every scenario planted, and one CLI run. Everything else reads a hand-built registry and census through the
# ``open_tuples`` seam, which needs no engine. The window of the engine test is moved with ``now`` (the report's
# clock is a parameter): it is placed between two real posts, using the engine's own stamps.

TUPLES = "tuple subspaces"


def _uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


def _post(subspace: str, key: str, name: str) -> None:
    """One real post: ``board/ci/<topic>``, ``board/<topic>`` and ``queue/<name>`` all take ``from`` and a nonce."""
    store = HttpTupleStore()
    try:
        store.out(subspace, {key: name}, {"from": "test"}, body="x", nonce=_uniq("n"))
    finally:
        store.close()


def _consume(subspace: str, key: str, name: str) -> None:
    """Take the queue row and ack it: the row stays in the census, consumed, no longer live."""
    store = HttpTupleStore()
    try:
        claimed = store.in_(subspace, {key: name}, claimant="curate-test", lease_s=30)
        assert claimed is not None, "the posted task was not claimable"
        store.ack(claimed[1], "curate-test")
    finally:
        store.close()


def _doc_writers(template: str) -> list[str]:
    store = HttpTupleStore()
    try:
        return next(t for t in store.registry(doc=True)["templates"] if t["name"] == template)["doc"]["writers"]
    finally:
        store.close()


def _stats(subspace: str) -> SubspaceCensus:
    store = HttpTupleStore()
    try:
        return store.subspace_stats(subspace)
    finally:
        store.close()


def _tuple_section(lines: list[str]) -> list[str]:
    assert any(ln.startswith(f"{TUPLES}:") for ln in lines), f"no '{TUPLES}' section in the report: {lines}"
    return _block(lines, TUPLES)


def _writerless(lines: list[str]) -> list[str]:
    """The templates the section lists by name as having no verified writer and no subspace."""
    out: list[str] = []
    inside = False
    for ln in _tuple_section(lines):
        if ln.startswith("  writerless templates"):
            inside = True
        elif ln.startswith("    - ") and inside:
            out.append(ln.strip()[2:].split(" | ")[0])
    return out


def test_one_engine_report_lists_the_cold_subspace_and_the_writerless_template_and_nothing_else(t2_service_env: str) -> None:
    # What the writerless check reads: today's board and queue carry writers, every one marked Unverified, so a
    # test for an EMPTY list alone would examine nothing on this engine.
    for template in ("board/<topic>", "queue/<name>"):
        writers = _doc_writers(template)
        assert writers and all(w.startswith("Unverified: ") for w in writers), template
    old, new, work = _uniq("example-old"), _uniq("example-new"), _uniq("example-work")
    cold_sub, fresh_sub, queue_sub = f"board/ci/{old}", f"board/ci/{new}", f"queue/{work}"
    _post(cold_sub, "topic", old)
    time.sleep(1.1)  # the engine stamps to the microsecond; a gap makes the window edge unambiguous
    _post(fresh_sub, "topic", new)
    _post(queue_sub, "queue", work)
    _consume(queue_sub, "queue", work)  # one consumed row, no live one: a take-enabled subspace that reads zero live rows
    queue = _stats(queue_sub)
    assert (queue.total, queue.consumed) == (0, 1)
    t_old, t_new = cold._when(_stats(cold_sub).newest_created_at), cold._when(_stats(fresh_sub).newest_created_at)
    assert t_old is not None and t_new is not None and t_new - t_old >= timedelta(seconds=1)
    now = t_old + (t_new - t_old) / 2 + timedelta(days=1)  # a one-day window whose edge falls between the two posts
    lines = cold.tuple_lines(1, now, HttpTupleStore)

    # 1. the subspace with no post in the window is listed, with its last post, its counts and its template's writers
    assert _stores(lines, TUPLES) == [cold_sub]
    row = _rows(lines, TUPLES)[0]
    assert f"last write {cold._show(_stats(cold_sub).newest_created_at, 'n/a')}" in row  # the engine's own stamp
    assert "last use n/a" in row  # the tuple census records no read time
    assert "1 available, 0 claimed, 0 consumed, 0 dead" in row
    writers = _doc_writers("board/ci/<topic>")
    assert writers and all(w in row for w in writers)  # the template's own list, every entry of it
    assert _head(lines, TUPLES).startswith(f"{TUPLES}: 1 cold of 3 ")
    # 2. the subspace posted to inside the window is not listed; nor is the consumed-only queue subspace posted to in it
    assert fresh_sub not in _stores(lines, TUPLES) and queue_sub not in _stores(lines, TUPLES)
    # 3. a writerless template with no subspace is listed by name, with what it says; the queue template has a
    #    subspace (consumed rows only), which still counts, and mailbox, lock, aspects and the rest have a verified writer
    assert _writerless(lines) == ["board/<topic>"]
    text = "\n".join(_tuple_section(lines))
    assert _doc_writers("board/<topic>")[0] in text and "no subspace in the census" in text


def test_the_command_reads_the_engine_by_default_and_exits_0(
    t2_service_env: str, world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    topic = _uniq("example-branch")
    _post(f"board/ci/{topic}", "topic", topic)
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")  # the transcript kinds are not available; the tuple row does not need them
    res = CliRunner().invoke(main, ["memory", "curate", "--cold", "--days", "30"])
    assert res.exit_code == 0, res.output
    assert "memory files: not available: " in res.output
    assert f"{TUPLES}: 0 cold of 1 " in res.output  # default store: the engine this test's env points at
    assert "writerless templates" in res.output and "board/<topic>" in res.output


# ── the same row over a hand-built registry and census (no engine, no tenant) ────────


class _Tuples:
    """A tuple-space client over data written by hand: ``registry(doc=True)``, ``subspace_list(None)``, ``close()``."""

    def __init__(self, templates: list[dict], census: list[SubspaceCensus]) -> None:
        self.templates, self.census, self.closed = templates, census, False

    def registry(self, doc: bool = False) -> dict:
        return {"digest": "d", "sources": [], "templates": [dict(t) for t in self.templates]}

    def subspace_list(self, prefix: str | None = None, **kw: object) -> list[SubspaceCensus]:
        return list(self.census)

    def close(self) -> None:
        self.closed = True


def _tpl(name: str, writers: list[str] | None = None, *, doc: bool = True) -> dict:
    if not doc:
        return {"name": name}
    d: dict = {"summary": f"{name} summary", "example": "x"}
    if writers is not None:
        d["writers"] = writers
    return {"name": name, "doc": d}


def _sub(subspace: str, newest: str | None, *, template: str | None = None, known: bool = True,
         available: int = 1, consumed: int = 0) -> SubspaceCensus:
    return SubspaceCensus(subspace=subspace, total=available, available=available, claimed=0, dead=0, consumed=consumed,
                          expired_unpurged=0, oldest_created_at=newest, newest_created_at=newest,
                          template=template, template_known=known)


FAR = datetime(2026, 11, 9, tzinfo=UTC)
LONG_AGO = "2026-09-01T00:00:00Z"
RECENT = "2026-11-08T00:00:00Z"


def test_an_engine_that_serves_no_doc_says_not_available_and_lists_no_writerless_template() -> None:
    store = _Tuples([_tpl("board/<topic>", doc=False), _tpl("queue/<name>", doc=False)],
                    [_sub("board/a", LONG_AGO, template="board/<topic>"), _sub("board/b", RECENT, template="board/<topic>")])
    lines = cold.tuple_lines(30, FAR, lambda: store)
    head = _head(lines, TUPLES)
    assert head.startswith(f"{TUPLES}: not available: ") and "predates the dictionary" in head
    assert _writerless(lines) == []  # every template would read as writerless: none is judged
    assert _stores(lines, TUPLES) == ["board/a"]  # the census is still valid: the cold subspace is listed, owner unknown
    assert "owner unknown" in _rows(lines, TUPLES)[0]
    assert store.closed


def test_a_template_with_no_doc_among_templates_that_have_one_is_not_judged_writerless() -> None:
    store = _Tuples([_tpl("board/<topic>", ["Unverified: x"]), _tpl("queue/<name>", doc=False)], [])
    lines = cold.tuple_lines(30, FAR, lambda: store)
    assert _writerless(lines) == ["board/<topic>"]
    assert "1 template has no doc block" in "\n".join(_tuple_section(lines))


def test_a_template_with_an_empty_or_absent_writers_list_and_no_subspace_is_listed_and_one_with_a_writer_is_not() -> None:
    store = _Tuples([_tpl("a/<x>", []), _tpl("b/<x>", None), _tpl("c/<x>", ["a real writer"]), _tpl("d/<x>", [])],
                    [_sub("d/one", RECENT, template="d/<x>")])
    lines = cold.tuple_lines(30, FAR, lambda: store)
    assert _writerless(lines) == ["a/<x>", "b/<x>"]  # c has a writer, d has a subspace
    assert "none declared" in "\n".join(_tuple_section(lines))  # an empty list is shown as such


def test_a_census_row_with_no_post_time_is_counted_as_not_judged_and_never_listed_cold() -> None:
    store = _Tuples([_tpl("board/<topic>", ["w"])],
                    [_sub("board/none", None, template="board/<topic>"), _sub("board/old", LONG_AGO, template="board/<topic>")])
    lines = cold.tuple_lines(30, FAR, lambda: store)
    assert _stores(lines, TUPLES) == ["board/old"]
    assert _head(lines, TUPLES).startswith(f"{TUPLES}: 1 cold of 2 ")
    assert "1 subspace in the census with no readable post time: not judged" in "\n".join(_tuple_section(lines))


def test_the_owner_is_the_engines_template_when_it_names_one_and_the_resolver_only_when_it_does_not() -> None:
    templates = [_tpl("queue/<name>", ["the queue writer"]), _tpl("board/<topic>", ["the board writer"])]
    store = _Tuples(templates, [
        # the engine says queue/<name> for a name the resolver would give to board/<topic>: the engine wins
        _sub("board/x", LONG_AGO, template="queue/<name>", known=True),
        # an engine that predates the field: the shared resolver decides
        _sub("board/y", LONG_AGO, template=None, known=False),
        # the engine names no template: no owner, and the resolver is not asked
        _sub("board/z", LONG_AGO, template=None, known=True),
    ])
    rows = {r.split(" | ")[0]: r for r in _rows(cold.tuple_lines(30, FAR, lambda: store), TUPLES)}
    assert rows["board/x"].endswith("owner the queue writer")
    assert rows["board/y"].endswith("owner the board writer")
    assert rows["board/z"].endswith("owner none (no registered template resolves)")


@pytest.mark.parametrize(("writers", "writerless"), [
    ([], True), (None, True), ("", True),
    (["Unverified: any session"], True), (["Unverified: a", "Unverified: b"], True),
    (["Unverified: a", "the mailbox_send MCP tool"], False), (["the ci-board-adapter lambda"], False),
    ("a single string writer", False),
])
def test_writerless_reads_the_list_not_its_prefix_alone(writers: object, writerless: bool) -> None:
    doc = {"summary": "s"} if writers is None else {"summary": "s", "writers": writers}
    assert cold.has_no_verified_writer(doc) is writerless


def test_a_cold_report_on_an_engine_without_doc_exits_0_with_not_available(
    world: dict[str, Path], monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    _session(world["project"] / "s1.jsonl", "s1", world["repo"], "2026-11-08T10:00:00Z", [("Bash", {"command": "ls"})])
    monkeypatch.delattr(lua_runtime, "run_script")
    store = _Tuples([_tpl("board/<topic>", doc=False)], [_sub("board/a", LONG_AGO, template="board/<topic>")])
    monkeypatch.setattr(cold, "_default_tuple_store", lambda: store)
    with patch("nexus.commands.memory.t2_handle", side_effect=RuntimeError("no store")):  # no tenant: the report is not stored
        res = CliRunner().invoke(main, ["memory", "curate", "--cold"])
    assert res.exit_code == 0, res.output
    assert f"{TUPLES}: not available: " in res.output and "predates the dictionary" in res.output


def test_an_unreachable_tuple_engine_is_not_available_and_blanks_only_the_tuple_row(world: dict[str, Path], stubbed) -> None:  # noqa: F811
    stubbed({"memory": MEM, "loads": LOADS, "tools": TOOLS})
    lines = cold.cold_lines(30, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS),
                            open_tuples=_no_tuple_engine)
    assert _head(lines, TUPLES) == f"{TUPLES}: not available: RuntimeError: no tuple engine in this test"
    assert "not available" not in _head(lines, "T2 namespaces")


def test_the_tuple_row_does_not_wait_for_the_transcripts(world: dict[str, Path]) -> None:  # noqa: F811
    """With no session in the window every transcript kind is not available, and the census still reads."""
    _session(world["project"] / "long.jsonl", "long", world["repo"], "2026-10-01T10:00:00Z",
             [("Bash", {"command": "ls"})], resumed="2026-11-08T10:00:00Z")
    store = _Tuples([_tpl("board/<topic>", ["w"])], [_sub("board/a", LONG_AGO, template="board/<topic>")])
    lines = cold.cold_lines(7, _NOW, world["repo"], "readrepo", world["mem"], world["repo"], _t2(_PROJECTS),
                            open_tuples=lambda: store)
    assert "no session of this repository started in the window" in _head(lines, "memory files")
    assert _stores(lines, TUPLES) == ["board/a"]  # reached, and read, with every transcript kind unavailable


# ── the docs match the code ──────────────────────────────────────────────────────────


def test_the_docs_describe_cold() -> None:
    root = Path(__file__).resolve().parents[1]
    ref = (root / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    section = ref.split("**`curate` flags:**", 1)[1].split("**`promote` flags:**", 1)[0]
    bullet = next(ln for ln in section.splitlines() if ln.startswith("- `--cold`"))
    for needle in ("MCP tool", "CLI verb", "nested", "K6", "not available", "Tuple subspaces", "invoked by a hook",
                   "last_accessed", ".claude/rules", "once per section", "no CLAUDE.md", "run by a slash command",
                   "src/nexus/hooks", "`tests/CLAUDE.md` itself is judged", "tools, memory or loads query",
                   "Six kinds", "`newest_created_at`", "`Unverified: `", "the engine predates the dictionary",
                   "does not depend on the transcripts"):
        assert needle in bullet, needle
    assert "three times" not in section
    assert "until Step 3" not in section and "RDR-240's tools, loads" not in section
    assert "not covered yet" not in section and "wait for RDR-239" not in section
    assert "K6" in cold.__doc__ and "--cold" in cold.__doc__
    # The entry lives in its fragment until a release folds it into CHANGELOG.md
    # (scripts/changelog_fold.py deletes the fragment), so read whichever holds it.
    frag_path = root / "changelog" / "unreleased" / "nexus-qo7ie.19.md"
    if frag_path.exists():
        fragment = frag_path.read_text(encoding="utf-8")
        assert fragment.startswith("### Added") and "--cold" in fragment
    else:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
        entry = next(ln for ln in changelog.splitlines() if "(nexus-qo7ie.19," in ln)
        assert "--cold" in entry
