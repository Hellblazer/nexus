# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory curate --cold`` (RDR-238 Technical Design 4 and 5, nexus-qo7ie.19).

Lists each store of five kinds with no write and no use in the window. Report only: retiring
a cold store is a person's decision, and nothing here deletes, quarantines or rewrites anything.
Each row gives the store, its last write (where the store has writes), its last use, its size
and its owner (where known). Tuple subspaces wait for RDR-239's dictionary view (RDR-238 Phase 3
Step 3b).

Use, per kind (Sam's K6 decision, nexus-qo7ie.5; the report header prints these):

- memory file: a Read-tool open or a subagent load (``file_read``, ``loaded``); a write is a
  ``file_write`` row or an mtime inside the window. A ``MEMORY.md`` index line counts as used
  when its linked file is read, so the files are what is judged.
- T2 namespace (the repository's projects, ``<repo>`` and ``<repo>_*``): a ``memory_get`` of
  one of its titles, a ``memory_search`` naming it (one naming no project credits every project), or a
  row whose ``last_accessed`` is in the window (the engine stamps it on every get and search, which
  is the only trace of a Bash ``nx memory`` read); a write is a row timestamp in the window.
- MCP tool of the ``nexus`` and ``nexus-catalog`` servers: a call. The registry is the servers'
  own (``_tool_manager.list_tools()``), diffed with the called names, so a tool never called is
  listed. Keyed by ``(server, short name)``: ``search`` exists on both servers.
- CLI verb: ``nx <verb>`` for a command, ``nx <group> <verb>`` for a group's child, found in
  Bash command text by RDR-240's tools query. The list is the click tree of ``nexus.cli:main``.
- injected context: a tracked nested ``CLAUDE.md`` (not the repository root's) is used when a
  ``nested_memory`` or ``instructions`` load names it, by its path or a path ending in it (a session
  in another worktree); a ``CLAUDE.md`` symlinked to ``AGENTS.md`` is one file. Not judged, and
  printed so: a nested ``AGENTS.md`` with no ``CLAUDE.md`` (Claude Code loads only ``CLAUDE.md`` on
  demand), fixtures under ``tests/``, files symlinked to the root instruction file (always loaded);
  ``.claude/rules/*.md`` is not covered.

Hooks and subprocesses never appear as model tool calls, so a tool or verb the plugin's hooks run
(:func:`hook_invoked`: the ``mcp_tool`` entries of ``hooks.json``, the string-literal ``nx`` argvs of
the hook scripts) is printed as invoked by a hook, not as cold, and both sections end with a limits
line naming what a transcript count cannot see.

K6 exclusions: always-loaded items are not judged and carry no "never used" claim: the index
files as a whole, the root ``CLAUDE.md`` and ``AGENTS.md`` and every file an ``instructions``
attachment loads, and tool definitions as injected text (a tool is listed as a tool only, by its
calls). The report prints this and how many ``instructions`` files it saw.

Counts come from RDR-240's ``memory``, ``tools`` and ``loads`` queries through the shared page
loop (:func:`query_pages`, which ``--files`` uses too), over this repository's sessions only, in
the window ``--files`` uses. When the runtime is absent, a query fails, no session of the
repository started in the window, a tools or memory query returns no rows for sessions that did
start, or the memory directory or a path cannot be matched, the affected kinds
print ``not available: <reason>``, never a cold list: an empty count lists every store.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from nexus import memory_curate as mc
from nexus._hook_runtime.memory_scan import INDEX_FILES, scan

LABELS = ("memory files", "T2 namespaces", "MCP tools", "CLI verbs", "injected context")
_NEVER = "none in the window"
_SERVER_CORE = re.compile(r"(^|_)nexus$")
_SERVER_CATALOG = re.compile(r"(^|_)nexus[-_]catalog$")
_NESTED_NAMES = frozenset({"CLAUDE.md", "AGENTS.md"})


def _rules() -> list[str]:
    return [
        "cold = no write and no use in the window; report only, nothing is retired or changed",
        "use (K6): memory file = a Read-tool open or a subagent load (a write is a Write or Edit, or an mtime in the "
        "window); T2 namespace = a memory_get of one of its titles or a memory_search naming it; MCP tool = a call; "
        "CLI verb = `nx <verb> [<sub>]` in a Bash command; nested CLAUDE.md or AGENTS.md = a nested_memory load",
        f"excluded (K6), no 'never used' claim: {' and '.join(INDEX_FILES)} as a whole (an index line counts as used "
        "when its linked file is read), the root CLAUDE.md and AGENTS.md and every file an `instructions` attachment "
        "loads, and tool definitions as injected text",
        "scope: uses are counted in this repository's sessions only (the main checkout and its worktrees), so a store "
        "used only elsewhere reads as cold; tuple subspaces are not covered (they wait for RDR-239's dictionary view)",
        f"read scope: {mc.READ_SCOPE_NOTE}",
    ]


# ── time ─────────────────────────────────────────────────────────────────────────────


def _when(value: str | None) -> datetime | None:
    stamp = mc._parse_ts(value or "")
    if stamp is None:
        return None
    return stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp


def _later(current: str | None, new: str | None) -> str:
    """The later of two stamps; ``""`` is a use whose time the query did not record."""
    new = new or ""
    if current is None:
        return new
    a, b = _when(current), _when(new)
    if b is None:
        return current
    return new if a is None or b > a else current


def _show(stamp: str | None, absent: str) -> str:
    if stamp is None:
        return absent
    when = _when(stamp)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ") if when else "in the window"


def _in_window(stamp: str | None, since: datetime) -> bool:
    when = _when(stamp)
    return when is not None and when >= since


@dataclass(frozen=True)
class ColdRow:
    store: str
    last_write: str
    last_use: str
    size: str
    owner: str

    def line(self) -> str:
        return (f"  - {self.store} | last write {self.last_write} | last use {self.last_use} | "
                f"size {self.size} | owner {self.owner}")


def _listing(label: str, rows: list[ColdRow], total: int, what: str = "") -> list[str]:
    return [f"{label}: {len(rows)} cold of {total}{what}", *(r.line() for r in rows)]


def _unavailable(label: str, reason: str) -> list[str]:
    return [f"{label}: not available: {reason}"]


# ── the page loop ────────────────────────────────────────────────────────────────────


def query_pages(query: str, since: str, until: str, max_pages: int) -> tuple[list[dict[str, Any]], int] | str:
    """``(rows, truncated_records)`` of *query* over ``[since, until)`` for this repository
    (project ``repo``: the main checkout and its worktrees), or a reason string.

    Each page advances at least an hour, so a page that does not advance stops the loop at once
    and *max_pages* bounds one that never finishes. Called from Python only (RDR-238 Technical
    Design 5), so ``nx memory curate`` adds no Lua.
    """
    from nexus.lua_runtime import runner  # noqa: PLC0415 — deferred: only a read count pays for the runtime
    from nexus.lua_runtime.transcripts_join import JoinError, join_pages  # noqa: PLC0415 — deferred with it

    cursor, pages = since, []
    try:
        while True:
            page = runner.run_procedure("transcripts", {"query": query, "since": cursor, "until": until, "project": "repo"})
            if not isinstance(page, dict):
                return f"result: the {query} query returned {type(page).__name__}, not a page"
            pages.append(page)
            nxt = page.get("next_since")
            if not nxt:
                break
            if not isinstance(nxt, str) or nxt <= cursor:
                return f"result: the {query} query did not advance past {cursor}"
            if len(pages) >= max_pages:
                return f"result: the {query} query did not finish in {max_pages} pages"
            cursor = nxt
        joined = join_pages(query, pages)
    except runner.PROCEDURE_ERRORS as exc:
        return runner.describe_error(exc) or type(exc).__name__
    except JoinError as exc:
        return f"join: {exc}"
    return list(joined["rows"]), int(joined.get("truncated_records") or 0)


# ── the window and the queries ───────────────────────────────────────────────────────


def open_window(days: int, now: datetime, cwd: Path) -> tuple[mc.ReadWindow, int] | mc.ReadsUnavailable:
    """The window and the number of this repository's sessions that started in it, or why there is none."""
    starts = mc.repo_session_starts(cwd)
    window = mc.read_window(days, now, min(starts) if isinstance(starts, list) and starts else None)
    if isinstance(starts, str):
        return mc.ReadsUnavailable(window, starts)
    lo = datetime.strptime(window.since, mc._FMT).replace(tzinfo=timezone.utc)
    counted = sum(1 for s in starts if lo <= s < now)
    if not counted:
        return mc.ReadsUnavailable(window, "no session of this repository started in the window")
    return window, counted


class _Queries:
    """Each transcripts query at most once per report; a failure is kept as its reason."""

    #: What a query that returned no rows at all would have to mean, for the queries where a started session
    #: nearly always leaves a row; with sessions in the window, no rows is a failed read, not an idle store.
    _QUIET = {"tools": "calls", "memory": "memory reads, writes or T2 calls"}

    def __init__(self, window: mc.ReadWindow, sessions: int) -> None:
        self.window = window
        self.sessions = sessions
        self.cut: dict[str, int] = {}
        self._done: dict[str, list[dict[str, Any]] | str] = {}

    def rows(self, query: str) -> list[dict[str, Any]] | str:
        if query not in self._done:
            self._done[query] = self._fetch(query)
        return self._done[query]

    def _fetch(self, query: str) -> list[dict[str, Any]] | str:
        try:
            got = query_pages(query, self.window.since, self.window.until, mc.MAX_READ_PAGES)
        except Exception as exc:  # noqa: BLE001 — one failure is kept as the reason of every kind that shares the query
            return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        if isinstance(got, str):
            return got
        rows, truncated = got
        if truncated:
            self.cut[query] = truncated
        if not rows and query in self._QUIET:
            n = self.sessions
            return f"the {query} query returned no {self._QUIET[query]} for {n} session{'' if n == 1 else 's'}"
        return rows


def _used(rows: list[dict[str, Any]], kinds: frozenset[str] | str) -> dict[str, str]:
    """Name -> latest ``last_use`` over the rows of *kinds* (``""``: used, time not recorded)."""
    wanted = frozenset({kinds}) if isinstance(kinds, str) else kinds
    out: dict[str, str] = {}
    for r in rows:
        if r.get("kind") in wanted and isinstance(r.get("name"), str) and int(r.get("count") or 1) > 0:
            out[r["name"]] = _later(out.get(r["name"]), r.get("last_use"))
    return out


# ── memory files ─────────────────────────────────────────────────────────────────────


def _files(q: _Queries, directory: Path | None, repo: str) -> list[str]:
    label = "memory files"
    if directory is None:
        return _unavailable(label, "no memory directory found for this repository")
    blocked = mc._unmatchable(directory)
    if blocked:
        return _unavailable(label, blocked)
    rows = q.rows("memory")
    if isinstance(rows, str):
        return _unavailable(label, rows)
    since = _when(q.window.since)
    assert since is not None
    read: dict[str, str] = {}
    wrote: dict[str, str] = {}
    for kind, into in (("file_read", read), ("loaded", read), ("file_write", wrote)):
        for name, stamp in _used(rows, kind).items():
            own = mc._same_file(name, directory)
            if own is not None:
                into[own] = _later(into.get(own), stamp)
    files = list(scan(directory).files)
    cold: list[ColdRow] = []
    for name in sorted(files):
        try:
            st = (directory / name).stat()
        except OSError:
            continue  # unreadable: not judged
        mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc)
        if name in read or name in wrote or mtime >= since:
            continue
        cold.append(ColdRow(name, _show(mtime.isoformat(), "n/a"), _NEVER, f"{st.st_size} bytes", repo))
    return _listing(label, cold, len(files), " (index files excluded)")


# ── T2 namespaces ────────────────────────────────────────────────────────────────────


def _t2(q: _Queries, repo: str, open_db: Callable[[], AbstractContextManager[Any]]) -> list[str]:
    label = "T2 namespaces"
    rows = q.rows("memory")
    if isinstance(rows, str):
        return _unavailable(label, rows)
    with open_db() as db:
        projects = mc._repo_projects(db, repo)
        data = {p: list(db.get_all(p)) for p in projects}
    since = _when(q.window.since)
    assert since is not None
    used: dict[str, str] = {}
    for name, stamp in _used(rows, "t2_get").items():
        project = name.split("/", 1)[0]
        used[project] = _later(used.get(project), stamp)
    searched = _used(rows, "t2_search")
    for project, stamp in searched.items():
        used[project] = _later(used.get(project), stamp)
    all_search = "(all)" in searched  # a memory_search naming no project reached every project
    cold: list[ColdRow] = []
    for project in projects:
        written: str | None = None
        for r in data[project]:
            if r.get("timestamp"):
                written = _later(written, r["timestamp"])
            if _in_window(r.get("last_accessed"), since):  # the engine stamps it on every get and search, Bash ones too
                used[project] = _later(used.get(project), r["last_accessed"])
        if all_search or project in used or _in_window(written, since):
            continue
        n = len(data[project])
        cold.append(ColdRow(project, _show(written, "none"), _NEVER, f"{n} row{'' if n == 1 else 's'}", repo))
    out = _listing(label, cold, len(projects), f" (projects {repo} and {repo}_*)")
    if all_search:
        out.append("  (a memory_search naming no project counts as a use of every project)")
    out.append("  limits: Bash `nx memory` reads are attributed to a project only through last_accessed (the engine "
               "stamps it on every get and search); the repo's own namespace reads as written after the first run, "
               "since the stored report lands there")
    return out


# ── MCP tools ────────────────────────────────────────────────────────────────────────


def nexus_tool(name: str) -> tuple[str, str] | None:
    """A transcript tool name as ``(server, short name)`` for the nexus and nexus-catalog servers, else None.

    ``mcp__plugin_conexus_nexus__<t>`` and ``mcp__nexus__<t>`` are ``("nexus", t)``;
    ``mcp__plugin_conexus_nexus-catalog__<t>`` and ``mcp__nexus_catalog__<t>`` are ``("nexus-catalog", t)``.
    Another server's tool is None: it is not a nexus tool and is never counted as an unknown one.
    """
    parts = name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp" or not parts[2]:
        return None
    if _SERVER_CORE.search(parts[1]):
        return ("nexus", parts[2])
    if _SERVER_CATALOG.search(parts[1]):
        return ("nexus-catalog", parts[2])
    return None


def mcp_registry() -> dict[tuple[str, str], tuple[int, bool]]:
    """Every tool the two nexus MCP servers register: ``(server, name)`` -> (definition tokens, always loaded).

    Tokens are ``(len(description) + len(JSON schema)) / 4``, the size of the definition a session pays for.
    """
    from nexus.mcp import catalog as _catalog  # noqa: PLC0415 — deferred: only --cold pays for importing the servers
    from nexus.mcp import core as _core  # noqa: PLC0415 — deferred with it

    out: dict[tuple[str, str], tuple[int, bool]] = {}
    for server, srv in (("nexus", _core.mcp), ("nexus-catalog", _catalog.mcp)):
        for tool in srv._tool_manager.list_tools():
            tokens = (len(tool.description or "") + len(json.dumps(tool.parameters, default=str))) // 4
            out[(server, tool.name)] = (max(tokens, 1), bool((tool.meta or {}).get(_core._ALWAYS_LOAD_META_KEY)))
    return out


def _tools(q: _Queries, hooks: HookScan) -> list[str]:
    label = "MCP tools"
    rows = q.rows("tools")
    if isinstance(rows, str):
        return _unavailable(label, rows)
    registry = mcp_registry()
    called: dict[tuple[str, str], str] = {}
    for name, stamp in _used(rows, "tool").items():
        key = nexus_tool(name)
        if key is not None:
            called[key] = _later(called.get(key), stamp)
    idle = [(key, info) for key, info in registry.items() if key not in called]
    idle.sort(key=lambda kv: (-kv[1][0], kv[0]))

    def row(key: tuple[str, str], info: tuple[int, bool], use: str) -> ColdRow:
        return ColdRow(f"{key[0]}/{key[1]}", "n/a", use, f"~{info[0]} tokens{', always loaded' if info[1] else ''}", key[0])

    cold = [row(k, i, _NEVER) for k, i in idle if k not in hooks.tools]
    hooked = [row(k, i, _HOOK_USE) for k, i in idle if k in hooks.tools]
    return _with_hooks(label, cold, hooked, len(registry), " registered", hooks)


# ── CLI verbs ────────────────────────────────────────────────────────────────────────


class Unit(NamedTuple):
    name: str  # "nx a" or "nx a b"
    verb: str
    sub: str | None


def cli_units() -> list[Unit]:
    """The click tree of ``nexus.cli:main`` as the units the tools query can tell apart: a top-level command
    (``nx a``), or a group's child (``nx a b``, a nested group counted once as ``nx a b``). Hidden commands are skipped."""
    import click  # noqa: PLC0415 — deferred with the CLI import
    from nexus.cli import main  # noqa: PLC0415 — deferred: only --cold pays for importing the command tree

    root = click.Context(main)
    units: list[Unit] = []
    for verb in sorted(main.list_commands(root)):
        cmd = main.get_command(root, verb)
        if cmd is None or cmd.hidden:
            continue
        children: list[str] = []
        if isinstance(cmd, click.Group):
            sub_ctx = click.Context(cmd, parent=root)
            for sub in sorted(cmd.list_commands(sub_ctx)):
                child = cmd.get_command(sub_ctx, sub)
                if child is not None and not child.hidden:
                    children.append(sub)
        if children:
            units += [Unit(f"nx {verb} {sub}", verb, sub) for sub in children]
        else:
            units.append(Unit(f"nx {verb}", verb, None))
    return units


def _verbs(q: _Queries, hooks: HookScan) -> list[str]:
    label = "CLI verbs"
    rows = q.rows("tools")
    if isinstance(rows, str):
        return _unavailable(label, rows)
    units = cli_units()
    by_verb: set[str] = set()
    by_pair: set[tuple[str, str]] = set()
    for r in rows:
        name = r.get("name")
        if r.get("kind") != "nx" or not isinstance(name, str) or not name.startswith("nx "):
            continue
        by_verb.add(name[3:])
        if isinstance(r.get("sub"), str):
            by_pair.add((name[3:], r["sub"]))
    idle = [u for u in units if (u.verb, u.sub) not in by_pair and (u.sub is not None or u.verb not in by_verb)]

    def by_hook(u: Unit) -> bool:
        return (u.verb, u.sub) in hooks.verbs if u.sub is not None else any(v == u.verb for v, _ in hooks.verbs)

    cold = [ColdRow(u.name, "n/a", _NEVER, "n/a", "n/a") for u in idle if not by_hook(u)]
    hooked = [ColdRow(u.name, "n/a", _HOOK_USE, "n/a", "n/a") for u in idle if by_hook(u)]
    return _with_hooks(label, cold, hooked, len(units), "", hooks)


# ── hooks ────────────────────────────────────────────────────────────────────────────

_HOOK_USE = "invoked by a hook"
_BARE = re.compile(r"^[a-z][a-z0-9_-]*$")  # the tools query's VERB rule
_RUN_NX = re.compile(r"_run_nx\(\s*\[([^\]]*)\]")
_NX_LIST = re.compile(r"\[\s*[\"']nx[\"']\s*,([^\]]*)\]")
_STR = re.compile(r"^[\"']([^\"']+)[\"']$")


class HookScan(NamedTuple):
    """What the plugin's hooks run, which no transcript shows as a model tool call."""

    tools: set[tuple[str, str]]  # (server, short name) of every mcp_tool entry of hooks.json
    verbs: set[tuple[str, str | None]]  # (verb, sub) of every nx argv found in the hook scripts
    note: str


def _argv_verb(body: str) -> tuple[str, str | None] | None:
    """The leading string-literal words of an argv list body: verb, then a sub that is a bare word."""
    words: list[str] = []
    for item in body.split(","):
        m = _STR.match(item.strip())
        if m is None or len(words) == 2:
            break
        words.append(m.group(1))
    if not words or not _BARE.match(words[0]):
        return None
    return words[0], (words[1] if len(words) > 1 and _BARE.match(words[1]) else None)


def hook_invoked(hooks_dir: Path | None) -> HookScan:
    """The MCP tools and ``nx`` verbs the plugin's hooks run (nexus-qo7ie.19 review r1).

    Tools: every ``mcp_tool`` entry of ``hooks.json``. Verbs: the string-literal argv of a ``_run_nx([...])``
    call or a ``["nx", ...]`` list in a hook script (``**/*.py``), and the words after ``nx_launch.py`` in a
    ``command`` entry. An argv built from variables, a shell string or a call through another helper is not seen.
    """
    if hooks_dir is None or not (hooks_dir / "hooks.json").is_file():
        return HookScan(set(), set(), f"no hook directory with a hooks.json at {hooks_dir}")
    tools: set[tuple[str, str]] = set()
    verbs: set[tuple[str, str | None]] = set()
    try:
        data = json.loads((hooks_dir / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return HookScan(set(), set(), f"hooks.json could not be read: {type(exc).__name__}")
    for groups in (data.get("hooks") or {}).values():
        for group in groups if isinstance(groups, list) else []:
            for h in group.get("hooks") or []:
                if h.get("type") == "mcp_tool" and isinstance(h.get("server"), str) and isinstance(h.get("tool"), str):
                    key = nexus_tool(f"mcp__{h['server'].replace(':', '_')}__{h['tool']}")
                    if key is not None:
                        tools.add(key)
                args = h.get("args")
                if isinstance(args, list) and "nx_launch.py" in args:
                    tail = args[args.index("nx_launch.py") + 1:]
                    found = _argv_verb(",".join(json.dumps(a) for a in tail if isinstance(a, str)))
                    if found:
                        verbs.add(found)
    scripts = sorted(p for p in hooks_dir.rglob("*.py") if p.is_file())
    for path in scripts:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for rx in (_RUN_NX, _NX_LIST):
            for m in rx.finditer(text):
                found = _argv_verb(m.group(1))
                if found:
                    verbs.add(found)
    note = (f"{len(tools)} mcp_tool entr{'y' if len(tools) == 1 else 'ies'} in hooks.json and {len(verbs)} nx argv"
            f"{'' if len(verbs) == 1 else 's'} in {len(scripts)} hook script{'' if len(scripts) == 1 else 's'} "
            "(string-literal argvs only)")
    return HookScan(tools, verbs, note)


def _with_hooks(label: str, cold: list[ColdRow], hooked: list[ColdRow], total: int, what: str, hooks: HookScan) -> list[str]:
    head = f"{label}: {len(cold)} cold of {total}{what}" + (f" ({len(hooked)} invoked by a hook)" if hooked else "")
    out = [head, *(r.line() for r in cold)]
    if hooked:
        out.append(f"  invoked by a hook, not seen in transcripts: {len(hooked)}")
        out += [f"  {r.line()}" for r in hooked]
    out.append("  limits: not seen are calls by hooks and subprocesses (the hook scripts are scanned: "
               f"{hooks.note}), terminal and CI use, bash -c and ssh bodies (the Lua scanner skips them), a group "
               "option before the sub verb, and other repositories' sessions")
    return out


# ── injected context ─────────────────────────────────────────────────────────────────


def _tracked_nested(root: Path) -> list[str] | str:
    """Tracked ``CLAUDE.md`` and ``AGENTS.md`` below the repository root, repo-relative with ``/``, or a reason."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: only this lookup needs it
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — deferred with run_bounded

    proc = run_bounded(argv_off_cwd(["git", "-C", str(root), "ls-files", "-z"]), timeout=15)
    if proc.returncode != 0:
        return f"git ls-files failed in {root}"
    names = [p for p in proc.stdout.split("\0") if p]
    return sorted(p for p in names if len(PurePosixPath(p).parts) > 1 and PurePosixPath(p).name in _NESTED_NAMES)


def _loaded_by(aliases: list[str], loaded: dict[str, str], root: Path) -> bool:
    """A load names one of *aliases*: the checkout's own path, or any path ending in it (another worktree's)."""
    return any(p == str(root / a) or p.endswith("/" + a) for a in aliases for p in loaded)


def _injected(q: _Queries, root: Path) -> list[str]:
    label = "injected context"
    if os.name == "nt":
        return _unavailable(label, "the loads query matches '/'-separated paths only, and Windows paths use '\\'")
    rows = q.rows("loads")
    if isinstance(rows, str):
        return _unavailable(label, rows)
    tracked = _tracked_nested(root)
    if isinstance(tracked, str):
        return _unavailable(label, tracked)
    instructions = _used(rows, "instructions")
    loaded = {**_used(rows, "nested_memory"), **instructions}  # an instructions load of a nested file is a load too
    always = {(root / n).resolve() for n in ("CLAUDE.md", "AGENTS.md")}
    groups: dict[Path, list[str]] = {}
    for rel in tracked:
        groups.setdefault((root / rel).resolve(), []).append(rel)
    cold: list[tuple[int, ColdRow]] = []
    judged, fixtures, lone, linked = 0, [], [], []
    for target, aliases in sorted(groups.items(), key=lambda kv: kv[1]):
        if any("tests" in PurePosixPath(a).parts[:-1] for a in aliases):
            fixtures += aliases
        elif target in always:
            linked += aliases
        elif not any(PurePosixPath(a).name == "CLAUDE.md" for a in aliases):
            lone += aliases  # Claude Code nested-loads CLAUDE.md only
        else:
            judged += 1
            if not _loaded_by(aliases, loaded, root):
                real = [a for a in aliases if not (root / a).is_symlink()]
                store = (real or aliases)[0]
                others = [a for a in aliases if a != store]
                try:
                    size = target.stat().st_size
                except OSError:
                    size = -1
                cold.append((size, ColdRow(store + (f" (also {', '.join(others)})" if others else ""), "n/a", _NEVER,
                                           f"~{size // 4} tokens" if size >= 0 else "n/a", "n/a")))
    cold.sort(key=lambda t: (-t[0], t[1].store))
    out = _listing(label, [r for _, r in cold], judged, " (nested CLAUDE.md and AGENTS.md, one file when linked)")
    if lone:
        out.append(f"  not judged: no CLAUDE.md, Claude Code loads only CLAUDE.md on demand: {', '.join(sorted(lone))}")
    if fixtures:
        out.append(f"  not judged: fixtures under tests/: {len(fixtures)} file{'' if len(fixtures) == 1 else 's'}")
    if linked:
        out.append(f"  not judged: {', '.join(sorted(linked))} symlinked to the root instruction file, which is always "
                   "loaded (excluded, K6)")
    out.append("  (on-demand .claude/rules/*.md files are not covered by this section)")
    n = len(instructions)
    out.append(f"  ({n} file{'' if n == 1 else 's'} loaded by an `instructions` attachment in the window: "
               "excluded (K6), no 'never used' claim)")
    return out


# ── the section ──────────────────────────────────────────────────────────────────────


def _guarded(label: str, body: Callable[[], list[str]]) -> list[str]:
    try:
        return body()
    except Exception as exc:  # noqa: BLE001 — one kind failing never stops the others or the exit status
        return _unavailable(label, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__)


def cold_lines(
    days: int, now: datetime, cwd: Path, repo: str, directory: Path | None, root: Path,
    open_db: Callable[[], AbstractContextManager[Any]], hooks_dir: Path | None = None,
) -> list[str]:
    """The ``--cold`` body: the rules, the window, then one block per kind (see the module docstring).

    *open_db* is a context manager yielding a ``T2Database`` (``get_projects_with_prefix``, ``get_all``).
    *hooks_dir* is the plugin's hook directory (default ``<root>/conexus/hooks``): the tools and verbs its hooks
    run are printed as invoked by a hook, not as cold.
    """
    opened = open_window(days, now, cwd)
    if isinstance(opened, mc.ReadsUnavailable):
        return [f"window: {opened.window.describe()}", *_rules(),
                *(ln for label in LABELS for ln in _unavailable(label, opened.reason))]
    window, sessions = opened
    q = _Queries(window, sessions)
    hooks = hook_invoked(hooks_dir if hooks_dir is not None else root / "conexus" / "hooks")
    blocks = [
        _guarded("memory files", lambda: _files(q, directory, repo)),
        _guarded("T2 namespaces", lambda: _t2(q, repo, open_db)),
        _guarded("MCP tools", lambda: _tools(q, hooks)),
        _guarded("CLI verbs", lambda: _verbs(q, hooks)),
        _guarded("injected context", lambda: _injected(q, root)),
    ]
    cuts = [f"{query} query: {n} records were cut, so these counts are floors and stores used in a cut record may "
            "be listed although they were used" for query, n in sorted(q.cut.items())]
    head = f"window: {window.describe()}, {sessions} session{'' if sessions == 1 else 's'} started in it"
    return [head, *_rules(), *cuts, *(ln for block in blocks for ln in block)]
