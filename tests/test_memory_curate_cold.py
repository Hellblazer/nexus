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
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import nexus.lua_runtime as lua_runtime
from nexus import memory_curate as mc
from nexus.memory_curate import cold
from nexus.cli import main
from nexus.lua_runtime import runner
from tests.test_memory_curate import _fm, _git, _make_repo, _memdir
from tests._module_seam import setattr_in
from tests.test_memory_curate_reads import _NOW, _session, world  # noqa: F401 — `world` is a fixture

GET = "mcp__plugin_conexus_nexus__memory_get"
SEARCH = "mcp__plugin_conexus_nexus__memory_search"
CAT_SEARCH = "mcp__plugin_conexus_nexus-catalog__search"
LABELS = ("memory files", "T2 namespaces", "MCP tools", "CLI verbs", "injected context")
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
            src_hooks_dir=w["srchooks"], commands_dir=w["commands"])
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
    for label in LABELS:
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
    for label in LABELS:
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


# ── the docs match the code ──────────────────────────────────────────────────────────


def test_the_docs_describe_cold() -> None:
    root = Path(__file__).resolve().parents[1]
    ref = (root / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    section = ref.split("**`curate` flags:**", 1)[1].split("**`promote` flags:**", 1)[0]
    bullet = next(ln for ln in section.splitlines() if ln.startswith("- `--cold`"))
    for needle in ("MCP tool", "CLI verb", "nested", "K6", "not available", "Tuple subspaces", "invoked by a hook",
                   "last_accessed", ".claude/rules", "once per section", "no CLAUDE.md", "run by a slash command",
                   "src/nexus/hooks", "`tests/CLAUDE.md` itself is judged", "tools, memory or loads query"):
        assert needle in bullet, needle
    assert "three times" not in section
    assert "until Step 3" not in section and "RDR-240's tools, loads" not in section
    assert "K6" in cold.__doc__ and "--cold" in cold.__doc__
    fragment = (root / "changelog" / "unreleased" / "nexus-qo7ie.19.md").read_text(encoding="utf-8")
    assert fragment.startswith("### Added") and "--cold" in fragment
