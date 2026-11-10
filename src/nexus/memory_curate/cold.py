# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory curate --cold`` (RDR-238 Technical Design 4 and 5, nexus-qo7ie.19).

Lists each store of six kinds with no write and no use in the window. Report only: retiring
a cold store is a person's decision, and nothing here deletes, quarantines or rewrites anything.
Each row gives the store, its last write (where the store has writes), its last use, its size
and its owner (where known). The sixth kind, tuple subspaces (RDR-238 Phase 3 Step 3b, nexus-qo7ie.20),
reads RDR-239's dictionary view from the engine, not the transcripts: see :func:`tuple_lines`.

Use, per kind (Sam's K6 decision, nexus-qo7ie.5; the report header prints these):

- memory file: a Read-tool open or a subagent load (``file_read``, ``loaded``); a write is a
  ``file_write`` row or an mtime inside the window. A ``MEMORY.md`` index line counts as used
  when its linked file is read, so the files are what is judged.
- T2 namespace (the repository's projects, ``<repo>`` and ``<repo>_*``): a ``memory_get`` of
  one of its titles, a ``memory_search`` naming it (one naming no project credits no project: the engine
  stamps only the rows it returns), or a row whose ``last_accessed`` is in the window (the engine stamps it
  on every get and search, which is the only trace of a Bash ``nx memory`` read, and of the SessionStart
  hook's own gets, :func:`nexus.memory_curate.hook_reads_note`); a write is a row timestamp in the window.
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
the plugin's hook scripts and of ``src/nexus/hooks``) is printed as invoked by a hook, and a verb a slash
command expands (a ``!`nx ...``` line of ``conexus/commands/*.md``) as run by a slash command, not as cold;
both sections end with a limits line naming what is scanned and what a transcript count cannot see.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from nexus import memory_curate as mc
from nexus._hook_runtime.memory_scan import INDEX_FILES, scan

LABELS = ("memory files", "T2 namespaces", "MCP tools", "CLI verbs", "injected context")
TUPLES = "tuple subspaces"
_UNVERIFIED = "Unverified: "  # RDR-239 Technical Design 4: the mark on a writers or readers string nothing in the repository backs
_NEVER = "none in the window"
_MCP_NAME = re.compile(r"^mcp__(?P<server>.+?)__(?P<tool>.+)$", re.DOTALL)
_SERVER_CORE = re.compile(r"(^|_)nexus$")
_SERVER_CATALOG = re.compile(r"(^|_)nexus[-_]catalog$")
_NESTED_NAMES = frozenset({"CLAUDE.md", "AGENTS.md"})


def _rules() -> list[str]:
    return [
        "cold = no write and no use in the window; report only, nothing is retired or changed",
        "use (K6): memory file = a Read-tool open or a subagent load (a write is a Write or Edit, or an mtime in the "
        "window); T2 namespace = a memory_get of one of its titles or a memory_search naming it; MCP tool = a call; "
        "CLI verb = `nx <verb> [<sub>]` in a Bash command; nested CLAUDE.md or AGENTS.md = a nested_memory load; "
        "tuple subspace = a post (the census's newest_created_at, consumed and expired rows included; it records no read time)",
        f"excluded (K6), no 'never used' claim: {' and '.join(INDEX_FILES)} as a whole (an index line counts as used "
        "when its linked file is read), the root CLAUDE.md and AGENTS.md and every file an `instructions` attachment "
        "loads, and tool definitions as injected text",
        "scope: uses are counted in this repository's sessions only (the main checkout and its worktrees), so a store "
        "used only elsewhere reads as cold (tuple subspaces are read from the engine and are not scoped by repository)",
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


def open_window(days: int, now: datetime, cwd: Path) -> tuple[mc.ReadWindow, int, int] | mc.ReadsUnavailable:
    """The window, the number of this repository's sessions that started in it and the number of transcripts
    left uncounted at the scan bound, or why there is none."""
    starts = mc.repo_session_starts(cwd, now)
    window = mc.read_window(days, now, min(starts.starts) if isinstance(starts, mc.SessionStarts) and starts.starts else None)
    if isinstance(starts, str):
        return mc.ReadsUnavailable(window, starts)
    lo = datetime.strptime(window.since, mc._FMT).replace(tzinfo=timezone.utc)
    counted = sum(1 for s in starts.starts if lo <= s < now)
    if not counted:
        return mc.ReadsUnavailable(window, "no session of this repository started in the window" + (f" ({mc.unscanned_note(starts.unscanned)})" if starts.unscanned else ""))
    return window, counted, starts.unscanned


class _Queries:
    """Each transcripts query at most once per report; a failure is kept as its reason."""

    #: What a query that returned no rows at all would have to mean, for the queries where a started session
    #: nearly always leaves a row; with sessions in the window, no rows is a failed read, not an idle store.
    _QUIET = {"tools": "calls", "memory": "memory reads, writes or T2 calls", "loads": "instruction loads"}

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
    all_search = "(all)" in searched  # a memory_search naming no project: it tracks only its hits, so it credits none
    used.pop("(all)", None)
    cold: list[ColdRow] = []
    for project in projects:
        written: str | None = None
        for r in data[project]:
            if r.get("timestamp"):
                written = _later(written, r["timestamp"])
            if _in_window(r.get("last_accessed"), since):  # the engine stamps it on every get and search, Bash ones too
                used[project] = _later(used.get(project), r["last_accessed"])
        if project in used or _in_window(written, since):
            continue
        n = len(data[project])
        cold.append(ColdRow(project, _show(written, "none"), _NEVER, f"{n} row{'' if n == 1 else 's'}", repo))
    out = _listing(label, cold, len(projects), f" (projects {repo} and {repo}_*)")
    if all_search:
        out.append("  (a memory_search naming no project was seen: it is credited to no project, since the engine "
                   "stamps last_accessed only on the rows it returned, so a project it hit is used only through "
                   "last_accessed)")
    out.append("  limits: Bash `nx memory` reads are attributed to a project only through last_accessed (the engine "
               "stamps it on every get and search); the repo's own namespace reads as written after the first run, "
               "since the stored report lands there; " + mc.hook_reads_note())
    return out


# ── MCP tools ────────────────────────────────────────────────────────────────────────


def nexus_tool(name: str) -> tuple[str, str] | None:
    """A transcript tool name as ``(server, short name)`` for the nexus and nexus-catalog servers, else None.

    ``mcp__plugin_conexus_nexus__<t>`` and ``mcp__nexus__<t>`` are ``("nexus", t)``;
    ``mcp__plugin_conexus_nexus-catalog__<t>`` and ``mcp__nexus_catalog__<t>`` are ``("nexus-catalog", t)``.
    Another server's tool is None: it is not a nexus tool and is never counted as an unknown one.
    """
    m = _MCP_NAME.match(name)  # a tool name, not a collection name: no "__" split here (the parse census counts those)
    if m is None:
        return None
    if _SERVER_CORE.search(m["server"]):
        return ("nexus", m["tool"])
    if _SERVER_CATALOG.search(m["server"]):
        return ("nexus-catalog", m["tool"])
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

    def named_in(u: Unit, found: set[tuple[str, str | None]]) -> bool:
        return (u.verb, u.sub) in found if u.sub is not None else any(v == u.verb for v, _ in found)

    cold = [ColdRow(u.name, "n/a", _NEVER, "n/a", "n/a") for u in idle
            if not named_in(u, hooks.verbs) and not named_in(u, hooks.slash)]
    hooked = [ColdRow(u.name, "n/a", _HOOK_USE, "n/a", "n/a") for u in idle if named_in(u, hooks.verbs)]
    slashed = [ColdRow(u.name, "n/a", _SLASH_USE, "n/a", "n/a") for u in idle
               if named_in(u, hooks.slash) and not named_in(u, hooks.verbs)]
    return _with_hooks(label, cold, hooked, len(units), "", hooks, slashed)


# ── hooks and slash commands ─────────────────────────────────────────────────────────

_HOOK_USE = "invoked by a hook"
_SLASH_USE = "run by a slash command"
_BARE = re.compile(r"^[a-z][a-z0-9_-]*$")  # the tools query's VERB rule
_RUN_NX = re.compile(r"_run_nx\(\s*\[([^\]]*)\]")
_NX_LIST = re.compile(r"\[\s*[\"']nx[\"']\s*,([^\]]*)\]")
_NX_ARGV_FOR = re.compile(r"\bnx_argv_for\(\s*[\w.]+\s*,([^)]*)\)")
_NX_ARGV = re.compile(r"\bnx_argv\(([^)]*)\)")
_SLASH_LINE = re.compile(r"^!`nx\s+([^`]*)`", re.MULTILINE)
_STR = re.compile(r"^[\"']([^\"']+)[\"']$")


class HookScan(NamedTuple):
    """What the plugin runs outside a model's tool calls: hooks, their scripts and slash commands."""

    tools: set[tuple[str, str]]  # (server, short name) of every mcp_tool entry of hooks.json
    verbs: set[tuple[str, str | None]]  # (verb, sub) of every nx argv found in the hook scripts
    slash: set[tuple[str, str | None]]  # (verb, sub) of every !`nx ...` line of the slash commands
    note: str


def _verb_of(words: list[str]) -> tuple[str, str | None] | None:
    """A verb and, when the next word is a bare word too, a sub, from the leading words of an argv."""
    if not words or not _BARE.match(words[0]):
        return None
    return words[0], (words[1] if len(words) > 1 and _BARE.match(words[1]) else None)


def _argv_verb(body: str) -> tuple[str, str | None] | None:
    """The leading string-literal words of an argv list body (``"memory", "get", row_id``)."""
    words: list[str] = []
    for item in body.split(","):
        m = _STR.match(item.strip())
        if m is None or len(words) == 2:
            break
        words.append(m.group(1))
    return _verb_of(words)


def hook_invoked(hooks_dir: Path | None, *, src_hooks_dir: Path | None = None,
                 commands_dir: Path | None = None) -> HookScan:
    """The MCP tools and ``nx`` verbs the plugin runs outside a model's tool calls.

    Tools: every ``mcp_tool`` entry of ``<hooks_dir>/hooks.json``. Hook verbs: the string-literal argv of a
    ``_run_nx([...])`` call or a ``["nx", ...]`` list in the plugin's hook scripts (``<hooks_dir>/**/*.py``), and of
    an ``nx_argv("a", "b")`` or ``nx_argv_for(nx, "a", "b")`` call in *src_hooks_dir* (``src/nexus/hooks``).
    Slash-command verbs: each ``!`nx a b ...`` line of the ``*.md`` files in *commands_dir*. (``nx_launch.py``
    runs ``nx-hook`` and ``nx-mcp``, not nx verbs, and is not read.) An argv built from variables, a shell string or
    a call through another helper is not seen; a missing directory contributes nothing.
    """
    tools: set[tuple[str, str]] = set()
    verbs: set[tuple[str, str | None]] = set()
    slash: set[tuple[str, str | None]] = set()
    problems: list[str] = []
    manifest = hooks_dir / "hooks.json" if hooks_dir is not None else None
    if manifest is None or not manifest.is_file():
        problems.append(f"no hook directory with a hooks.json at {hooks_dir}")
    else:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            data = {}
            problems.append(f"hooks.json could not be read: {type(exc).__name__}")
        for groups in (data.get("hooks") or {}).values():
            for group in groups if isinstance(groups, list) else []:
                for h in group.get("hooks") or []:
                    if h.get("type") == "mcp_tool" and isinstance(h.get("server"), str) and isinstance(h.get("tool"), str):
                        key = nexus_tool(f"mcp__{h['server'].replace(':', '_')}__{h['tool']}")
                        if key is not None:
                            tools.add(key)

    def texts(root: Path | None, pattern: str) -> list[str]:
        out: list[str] = []
        for path in sorted(root.rglob(pattern)) if root is not None and root.is_dir() else []:
            try:
                out.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return out

    plugin_scripts = texts(hooks_dir, "*.py")
    src_scripts = texts(src_hooks_dir, "*.py")
    for text in plugin_scripts:
        for rx in (_RUN_NX, _NX_LIST):
            verbs.update(v for m in rx.finditer(text) if (v := _argv_verb(m.group(1))))
    for text in src_scripts:
        for rx in (_NX_ARGV_FOR, _NX_ARGV):
            verbs.update(v for m in rx.finditer(text) if (v := _argv_verb(m.group(1))))
    commands = texts(commands_dir, "*.md")
    for text in commands:
        slash.update(v for m in _SLASH_LINE.finditer(text) if (v := _verb_of(m.group(1).split())))
    note = (f"{len(tools)} mcp_tool entr{'y' if len(tools) == 1 else 'ies'} in hooks.json; {len(verbs)} CLI command line"
            f"{'' if len(verbs) == 1 else 's'} in {len(plugin_scripts)} plugin hook script(s) and "
            f"{len(src_scripts)} file(s) of the harness hooks (string-literal argvs only); {len(slash)} CLI verb"
            f"{'' if len(slash) == 1 else 's'} in {len(commands)} slash command(s), so slash-command expansions are covered")
    if problems:
        note = "; ".join(problems) + "; " + note
    return HookScan(tools, verbs, slash, note)


def _with_hooks(label: str, cold: list[ColdRow], hooked: list[ColdRow], total: int, what: str, hooks: HookScan,
                slashed: list[ColdRow] | None = None) -> list[str]:
    slashed = slashed or []
    parts = ([f"{len(hooked)} invoked by a hook"] if hooked else []) + (
        [f"{len(slashed)} run by a slash command"] if slashed else [])
    out = [f"{label}: {len(cold)} cold of {total}{what}" + (f" ({', '.join(parts)})" if parts else ""),
           *(r.line() for r in cold)]
    for use, rows in ((_HOOK_USE, hooked), (_SLASH_USE, slashed)):
        if rows:
            out.append(f"  {use}, not seen in transcripts: {len(rows)}")
            out += [f"  {r.line()}" for r in rows]
    out.append("  limits: not seen are calls by hooks and subprocesses and runs by slash commands (scanned: "
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


def _is_fixture(rel: str) -> bool:
    """A tracked instruction file that is test data, not a rule: below ``tests/`` (``tests/CLAUDE.md`` itself is a real
    nested file), or under a ``fixtures`` or ``e2e`` directory."""
    dirs = PurePosixPath(rel).parts[:-1]
    return ("tests" in dirs and dirs.index("tests") < len(dirs) - 1) or "fixtures" in dirs or "e2e" in dirs


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
        if any(_is_fixture(a) for a in aliases):
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
                cold.append((size, ColdRow(store + (f" (also {', '.join(others)})" if others else ""), "n/a",
                                           "never loaded (on demand)", f"{size} bytes" if size >= 0 else "n/a", "n/a")))
    cold.sort(key=lambda t: (-t[0], t[1].store))
    out = _listing(label, [r for _, r in cold], judged, " (nested CLAUDE.md and AGENTS.md, one file when linked)")
    if lone:
        out.append(f"  not judged: no CLAUDE.md, Claude Code loads only CLAUDE.md on demand: {', '.join(sorted(lone))}")
    if fixtures:
        out.append("  not judged: fixtures (below tests/ or under a fixtures or e2e directory): "
                   f"{len(fixtures)} file{'' if len(fixtures) == 1 else 's'}")
    if linked:
        out.append(f"  not judged: {', '.join(sorted(linked))} symlinked to the root instruction file, which is always "
                   "loaded (excluded, K6)")
    out.append("  (on-demand rule files under the repository's .claude directory are not covered by this section)")
    n = len(instructions)
    out.append(f"  ({n} file{'' if n == 1 else 's'} loaded by an `instructions` attachment in the window: "
               "excluded (K6), no 'never used' claim)")
    return out


# ── tuple subspaces ──────────────────────────────────────────────────────────────────


def _default_tuple_store() -> Any:
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: only --cold pays for it

    return HttpTupleStore()


def has_no_verified_writer(doc: Any) -> bool:
    """A template doc names no writer that something in the repository backs: ``writers`` is absent or empty, or
    every entry starts with ``Unverified: `` (RDR-239 TD4 marks ``board/<topic>`` and ``queue/<name>`` so, which
    is why the list is read and not only its length)."""
    writers = doc.get("writers") if isinstance(doc, dict) else None
    items = [writers] if isinstance(writers, str) else list(writers or [])
    return not any(isinstance(w, str) and w.strip() and not w.startswith(_UNVERIFIED) for w in items)


def _writers_text(doc: dict[str, Any]) -> str:
    writers = doc.get("writers")
    items = [writers] if isinstance(writers, str) else [w if isinstance(w, str) else json.dumps(w) for w in writers or []]
    return " / ".join(items) if items else "none declared"


def tuple_lines(days: int, now: datetime, open_tuples: Callable[[], Any]) -> list[str]:
    """The tuple-subspace block: subspaces with no post in *days*, and templates nothing writes to.

    Read through the engine's own dictionary view, never by running the CLI: ``registry(doc=True)`` gives each
    template's ``doc`` (with ``writers``) and ``subspace_list`` a census row per concrete subspace, each naming its
    owning template (the engine's ``template`` field, or the shared client resolver for an engine that predates it).
    A subspace is cold when its newest post (``newest_created_at``: every row, consumed and expired ones too, so a
    take-enabled subspace that reads zero live rows is not cold while it is posted to) is before ``now - days``.
    A template is listed by name when it has no verified writer (:func:`has_no_verified_writer`) and no census row
    resolves to it: a subspace whose rows are all consumed or expired is still a subspace. An engine whose registry
    serves no ``doc`` for any template cannot tell either, so the block says so, lists the cold subspaces with the
    owner unknown, and judges no template. *open_tuples* returns a store (``registry``, ``subspace_list``, ``close``).
    """
    from nexus.tuple_templates import resolve_tuple_template  # noqa: PLC0415 — deferred with the engine client

    store = open_tuples()
    try:
        templates: list[dict[str, Any]] = store.registry(doc=True).get("templates", [])
        census = list(store.subspace_list(None))
    finally:
        store.close()
    by_name = {t.get("name"): t for t in templates}
    documented = any(t.get("doc") for t in templates)
    since = now - timedelta(days=days)

    def owner_of(row: Any) -> str | None:
        if row.template_known:
            return row.template
        resolved = resolve_tuple_template(templates, row.subspace)
        return None if resolved is None else resolved.get("name")

    cold: list[tuple[datetime, ColdRow]] = []
    unjudged = 0
    owned: set[str | None] = set()
    for row in census:
        name = owner_of(row)
        owned.add(name)
        posted = _when(row.newest_created_at)
        if posted is None:
            unjudged += 1
            continue
        if posted >= since:
            continue
        if not documented:
            owner = "unknown (the engine predates the dictionary)"
        elif name not in by_name:
            owner = "none (no registered template resolves)"
        elif not by_name[name].get("doc"):
            owner = f"{name}, which has no doc block"
        else:
            owner = _writers_text(by_name[name]["doc"])
        size = f"{row.available} available, {row.claimed} claimed, {row.consumed} consumed, {row.dead} dead"
        cold.append((posted, ColdRow(row.subspace, _show(row.newest_created_at, "n/a"), "n/a", size, owner)))
    cold.sort(key=lambda pr: (pr[0], pr[1].store))
    rows = [r for _, r in cold]
    if not documented:
        out = [f"{TUPLES}: not available: the engine predates the dictionary (its registry serves no doc block), so "
               "owners cannot be read and no template is judged; the cold subspaces below are from the census alone",
               *(r.line() for r in rows)]
    else:
        out = _listing(TUPLES, rows, len(census), f" subspaces in the census (no post in {days} days)")
        writerless = [t for t in templates if t.get("doc") and t.get("name") not in owned
                      and has_no_verified_writer(t["doc"])]
        out.append(f"  writerless templates (no verified writer) with no subspace in the census: {len(writerless)}")
        out += [f"  {ColdRow(str(t['name']), 'n/a', 'n/a', 'no subspace', _writers_text(t['doc'])).line()}"
                for t in sorted(writerless, key=lambda t: str(t["name"]))]
        blind = sum(1 for t in templates if not t.get("doc"))
        if blind:
            out.append(f"  ({blind} template{' has' if blind == 1 else 's have'} no doc block: not judged)")
    if unjudged:
        out.append(f"  ({unjudged} subspace{'' if unjudged == 1 else 's'} in the census with no readable post time: not judged)")
    out.append("  limits: last write is the census's newest post over every row, consumed and expired included, so a "
               "take-enabled subspace that reads zero live rows is cold only when it is not posted to; the census "
               "records no read time, so last use is n/a; a subspace whose rows were all purged is no longer in the "
               "census; the owner is read from the template's doc, which nothing keeps true")
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
    src_hooks_dir: Path | None = None, commands_dir: Path | None = None,
    open_tuples: Callable[[], Any] | None = None,
) -> list[str]:
    """The ``--cold`` body: the rules, the window, then one block per kind (see the module docstring).

    *open_db* is a context manager yielding a ``T2Database`` (``get_projects_with_prefix``, ``get_all``).
    *hooks_dir* is the plugin's hook directory (default ``<root>/conexus/hooks``), *src_hooks_dir* the harness hooks'
    Python (default ``<root>/src/nexus/hooks``) and *commands_dir* the plugin's slash commands (default
    ``<root>/conexus/commands``): the tools and verbs they run are printed as invoked by a hook or run by a slash
    command, not as cold (:func:`hook_invoked`). *open_tuples* returns the tuple-space client the tuple row reads
    (default: :class:`~nexus.db.t2.http_tuple_store.HttpTupleStore` on the configured service); that row reads the
    engine, not the transcripts, so it runs whether or not the transcript kinds do.
    """
    tuples = _guarded(TUPLES, lambda: tuple_lines(days, now, open_tuples or _default_tuple_store))
    try:
        opened = open_window(days, now, cwd)
    except Exception as exc:  # noqa: BLE001 — like a failed query: every transcript kind says why, the tuple row stays
        opened = mc.ReadsUnavailable(mc.read_window(days, now), f"{type(exc).__name__}: {exc}")
    if isinstance(opened, mc.ReadsUnavailable):
        return [f"window: {opened.window.describe()}", *_rules(),
                *(ln for label in LABELS for ln in _unavailable(label, opened.reason)), *tuples]
    window, sessions, unscanned = opened
    q = _Queries(window, sessions)
    hooks = hook_invoked(
        hooks_dir if hooks_dir is not None else root / "conexus" / "hooks",
        src_hooks_dir=src_hooks_dir if src_hooks_dir is not None else root / "src" / "nexus" / "hooks",
        commands_dir=commands_dir if commands_dir is not None else root / "conexus" / "commands")
    blocks = [
        _guarded("memory files", lambda: _files(q, directory, repo)),
        _guarded("T2 namespaces", lambda: _t2(q, repo, open_db)),
        _guarded("MCP tools", lambda: _tools(q, hooks)),
        _guarded("CLI verbs", lambda: _verbs(q, hooks)),
        _guarded("injected context", lambda: _injected(q, root)),
        tuples,
    ]
    cuts = [f"{query} query: {n} records were cut, so these counts are floors and stores used in a cut record may "
            "be listed although they were used" for query, n in sorted(q.cut.items())]
    head = f"window: {window.describe()}, {sessions} session{'' if sessions == 1 else 's'} started in it" + (
        f"; {mc.unscanned_note(unscanned)}" if unscanned else "")
    return [head, *_rules(), *cuts, *(ln for block in blocks for ln in block)]
