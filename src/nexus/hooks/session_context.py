# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``session-context`` hook verb (RDR-215 bead nexus-q02nx.21).

Port of ``conexus/hooks/scripts/session_start_hook.py`` (244 lines,
stdlib-only). **This is a different hook from ``session-start``**
(``nexus.hooks.session_start_verb``, bead nexus-q02nx.5): both fire on the
same ``SessionStart`` event, as two separate ``hooks.json`` command
entries, and both stay wired today. ``session-start`` wraps ``nx hook
session-start`` -- the guidance/imperative emitter, which needs
``nexus.session`` and pays for it legitimately. This module wraps the
OTHER SessionStart script.

**What it prints now (SessionStart progressive disclosure, T2
nexus/design-sessionstart-progressive-disclosure-2026-11-20).** The rules and the state
of THIS session, and one index line: the T1 contract, the threads due, the one
identity-notes line (RDR-253 Step 6 part 3), the fire-only T2 reachability line, ``bd
prime`` where the beads plugin is off for the project, ONE static index line naming ``nx
brief <topic>`` and when to load it, and the nexus-1if7b hygiene block. The reference
blocks that used to follow (the T2 memory scan, ``bd ready``, the capabilities block, the
RDR-072 L1 Knowledge Map) are rendered by the functions below on demand: ``nx brief`` and
the ``brief`` MCP tool (:mod:`nexus.brief`) call them, so there is one source and no second
copy. The hook no longer runs the scan, ``bd ready`` or ``nx``. It still spawns ``git`` once
(the repo name), calls T2 for the reachability probe, and, with a session id, calls
``board_due`` and ``identity_notes`` (both read T2). It stats the Knowledge Map cache's mtime
for the hygiene block and reads nothing from it.

What did NOT move behind a brief, because it is a rule or a failure signal and not context:
the T2 reachability line (nexus-8fvp2: a T2 that cannot be reached is never silent), and
``bd prime`` for a project whose beads plugin is off (nexus-dg7i6.4: its only primer). What
did move and is not repeated at start: the full-prefix MCP rule in the capabilities block
(``mcp__plugin_conexus_nexus__``), which CLAUDE.md and AGENTS.md carry. The index line is
static, so a failing ``nx brief`` costs a session that context and never the rest.

**"Move, do not rewrite" (RDR-215 Approach item 9).** The body below is
carried mechanically from the script: same helper functions, same control
flow, same env-var names, same silent-on-empty-output contract. Two
changes were unavoidable, not stylistic, and are called out here rather
than silently folded in:

1. **``main() -> None`` (print + ``sys.exit(0)``) becomes
   ``run(payload) -> HookResult``.** The script's ``if output_lines:
   print(...)`` becomes ``HookResult(stdout="\\n".join(output_lines))``
   when there is something to say and ``HookResult()`` (``stdout=None``)
   otherwise -- ``nexus._hook_runtime.entry.main`` writes
   ``result.stdout + "\\n"`` when it is not ``None``, which reproduces
   ``print()``'s own trailing newline byte for byte, and writes nothing at
   all when it is ``None``, matching the script's silence.

2. **The sibling-script path.** The script resolved ``t2_prefix_scan.py``
   with ``Path(__file__).parent`` because both files lived side by side in
   ``conexus/hooks/scripts/``. This module now lives in ``src/nexus/hooks/``
   and ``t2_prefix_scan.py`` has not moved (a separate bead's scope), so
   the literal ``Path(__file__).parent`` would resolve to
   ``src/nexus/hooks/t2_prefix_scan.py`` -- a file that does not exist --
   and silently produce no T2 memory context (``run_command`` swallows
   ``FileNotFoundError`` into ``None``) rather than erroring loudly. This
   is not a behavior change to author; it is the same class of fix an
   earlier hook made for its own sibling script (bead
   nexus-q02nx.13/.17): resolve the
   plugin root via ``CLAUDE_PLUGIN_ROOT`` (falling back to the dev-checkout
   layout for a from-source run), and reach the still-plugin-only sibling
   through it. Gone as of bead nexus-b5ugt: the T2 section calls
   ``nexus.hooks.t2_prefix_scan`` in-process and no path is resolved.

**Payload: the session id only.** The script never read stdin -- it
resolved everything from ``CLAUDE_PROJECT_DIR``/``os.getcwd()`` and local
environment variables (the renders ``nx brief`` calls still do; the hook itself reads ``os.getcwd()`` for the hygiene check). Since nexus-ct74m ``run`` reads one payload field,
``session_id``, to find the session's threads board entry and print its due
threads (``nexus.hooks.board_due``); a payload without one prints no such
section. Like the identity-notes line's ``identity_notes``, that module is imported
inside ``run`` and is not stdlib-only: the STDLIB ONLY rule below covers this
module's own top-level imports, not the sections it defers to.

**STDLIB ONLY, in this module.** No ``structlog``, no
``nexus.logging_setup``, no import from a sibling ``nexus.hooks`` module --
same constraint the script carried under ``_run_python_hook.sh`` (whichever
bare interpreter that resolves cannot import third-party packages).
**This constraint does not reach the dispatch cost, though**, and that gap
is worth stating plainly rather than passing over: ``nexus/hooks/__init__.py``
imports ``structlog`` and ``nexus.session`` at module scope, and Python
initializes a package's ``__init__`` before it can reach ANY submodule --
there is no way for ``import nexus.hooks.session_context`` (direct or via
``importlib.import_module``, which is how ``nx-hook`` resolves a verb) to
avoid that cost while this module lives inside the ``nexus.hooks`` package.
Measured: ``python -c "import nexus.hooks.session_context"`` puts
``structlog`` in ``sys.modules`` regardless of what this file itself
imports. That is a property of every verb module already living in
``nexus.hooks`` (``session_start_verb``, ``subagent_start``, ...), not something this port introduces or
could fix by itself -- fixing it means deferring ``nexus.hooks``'s own
package-level imports, a change shared by every hook this epic is
concurrently porting, and out of scope for a "move, do not rewrite" bead.
Reported rather than silently worked around.

No caller branches on this verb's exit code; ``entry.main`` forces 0 for
every verb.

**Wired since bead ``nexus-q02nx.21``.** ``conexus/hooks/hooks.json``
declares this as ``{"command": "nx-hook", "args": ["session-context"]}``;
``session_start_hook.py`` and the ``_run_python_hook.sh`` launcher that used
to run it are both gone. The declaration is INERT until the next plugin cut,
because ``marketplace.json`` pins ``source.ref`` to a release tag — see
``conexus/PENDING_RELEASE.md``, which is the ledger for exactly that gap.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from nexus._hook_runtime._io import HookResult

__all__ = ["run"]

# nexus-sby49 audience gates (stdlib only). ``maintainer`` and ``nexus_repo`` tighten (they add text), so
# an import failure binds them to True, the contract's direction: a broken helper gives the maintainer's
# full output, never a thinner one. ``account_flag`` loosens and binds to False.
try:
    from nexus.hooks._audience import account_flag as _account_flag, maintainer as _maintainer, nexus_repo as _nexus_repo
except BaseException:  # noqa: BLE001
    def _maintainer(payload: object) -> bool:
        return True

    def _nexus_repo(payload: object) -> bool:
        return True

    def _account_flag() -> bool:
        return False

# Configuration via environment variables
DEBUG = os.environ.get('NX_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))
BD_TIMEOUT = int(os.environ.get('BD_TIMEOUT', '5'))


#: The topics of ``nx brief`` / the ``brief`` MCP tool, in index order (:mod:`nexus.brief` renders them). Held here, with the
#: index line, because this module is stdlib-only and prints the line on every start.
BRIEF_TOPICS: tuple[str, ...] = ("memory", "beads", "caps", "map", "rdr", "me")

#: The ONE line SessionStart prints for everything that moved behind ``nx brief``. Static: it names no state, so it cannot
#: fail, and a brief that does not load leaves the session without that context, never without a rule.
BRIEF_INDEX_LINE: str = (
    "More on demand (`nx brief <topic>` or the `brief` MCP tool): "
    "beads (ready, bd usage) before choosing work; memory (T2 titles), map (topic labels) before researching; "
    "caps (tool notes) | rdr (RDR counts) | me (your startup note)"
)

#: nexus-sby49.3 (I2): the line's parts, so the two topics that need data are named only where the data is. The
#: full line above is these five joined, and is what a project with a ``.beads/`` directory and an RDR directory gets.
_INDEX_HEAD: str = "More on demand (`nx brief <topic>` or the `brief` MCP tool): "
_INDEX_BEADS: str = "beads (ready, bd usage) before choosing work; "
_INDEX_CORE: str = "memory (T2 titles), map (topic labels) before researching; caps (tool notes)"
_INDEX_RDR: str = " | rdr (RDR counts)"
_INDEX_ME: str = " | me (your startup note)"


def _has_beads(project_dir: Path) -> bool:
    """The beads topic is named where the project tracks work in beads: a ``.beads/`` directory with ``bd`` on PATH (plan
    S2-1). Names a topic more often than it must on any doubt: a data gate that fails shows the line it always showed."""
    try:
        return (project_dir / ".beads").is_dir() and which("bd")
    except Exception:  # noqa: BLE001 — a gate that cannot be read leaves the topic in
        return True


def _has_rdr_dir(project_dir: Path) -> bool:
    """The rdr topic is named where the project has an RDR directory (``docs/rdr``, or its ``.nexus.yml`` path)."""
    try:
        from nexus.hooks.rdr_verb import _rdr_dir  # noqa: PLC0415 — deferred: only this gate pays for the verb's imports

        return _rdr_dir(project_dir).is_dir()
    except Exception:  # noqa: BLE001 — a gate that cannot be read leaves the topic in
        return True


def brief_index_line(project_dir: Path) -> str:
    """The ONE index line for *project_dir*: ``beads`` and ``rdr`` only where the project has beads or an RDR directory."""
    return (_INDEX_HEAD + (_INDEX_BEADS if _has_beads(project_dir) else "") + _INDEX_CORE
            + (_INDEX_RDR if _has_rdr_dir(project_dir) else "") + _INDEX_ME)


def debug(msg: str) -> None:
    """Print debug message to stderr if debugging enabled."""
    if DEBUG:
        print(f"[session-hook] {msg}", file=sys.stderr)  # noqa: T201 — carried: stderr, not the stdout decision channel


def which(cmd: str) -> bool:
    """Return True if cmd is found on PATH."""
    return shutil.which(cmd) is not None


def run_command(args: list[str], timeout: int, cwd: str | None = None) -> str | None:
    """
    Run a command and return its stdout, or None on failure.
    Stderr is captured; printed to stderr only when DEBUG is set.
    """
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: a hook process pays its import cost on every invocation, and a module-scope import of this pulls structlog + ~231 modules (measured on verification_config: 14ms/106 -> 62-84ms/337). Deferred, it is paid only when we actually spawn

    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 - stdlib-only, function-level so the hook module itself stays import-light

    try:
        # Windows: a git.exe / bd.exe planted in the project (the hook's cwd) would
        # run ahead of PATH. Resolved off the cwd; FileNotFoundError is caught below.
        result = run_bounded(
            argv_off_cwd(args),
            timeout=timeout,
            cwd=cwd,
        )
        if DEBUG and result.stderr:
            print(  # noqa: T201 — carried: stderr, not the stdout decision channel
                f"[session-hook] stderr from {args[0]}: {result.stderr[:500]}", file=sys.stderr
            )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        debug(f"{args} timed out after {timeout}s")
    except FileNotFoundError:
        debug(f"{args[0]} command not found")
    except OSError as e:
        debug(f"{args} failed: {e}")
    return None


#: Ready-beads render caps (nexus-h33x8.5 fix-pass, VERIFICATION 1 combined
#: budget): was 10 lines / 500 chars each, uncapped total. An overflow
#: count line replaces the lines dropped, so the trim is visible rather
#: than a silent truncation.
_READY_BEADS_MAX_LINES = 5
_READY_BEADS_MAX_CHARS = 160


def _render_ready_beads(
    ready_output: str | None,
    *,
    max_lines: int = _READY_BEADS_MAX_LINES,
    max_chars: int = _READY_BEADS_MAX_CHARS,
) -> list[str]:
    """Render the ``## Ready Beads`` block from raw ``bd ready`` stdout.

    Pure function (no subprocess call) so the combined SessionStart byte
    budget can be tested against a representative fixture string instead
    of live, daily-varying ``bd ready`` output (nexus-h33x8.5 fix-pass).
    Returns ``[]`` for empty/None input — caller appends nothing.
    """
    if not ready_output:
        return []
    all_lines = ready_output.split("\n")
    shown = all_lines[:max_lines]
    lines = ["## Ready Beads", "```"]
    lines.extend(line[:max_chars] for line in shown)
    overflow = len(all_lines) - len(shown)
    if overflow > 0:
        lines.append(f"… ({overflow} more — `bd ready` for full list)")
    lines.append("```")
    lines.append("")
    return lines


#: nexus-mtheb: the top-level T1 contract, first in the SessionStart context.
#: T1 inter-agent use was designed five times and faded each time; the survey of
#: 2026-11-01 found 0 T1 writes in ~45 dispatches across 9 sessions, with the
#: only top-level mention one capability line deep in this block.
#:
#: nexus-sby49.3: one constant per line, each with its own audience in ``conexus/hooks/audience.yaml``. The heading and
#: the lifetime line are the product's (I1a; the retired-``share_t1`` parenthetical is gone for everyone, I1c). The
#: dispatch protocol sentence is Sam's own working rule (I1b, D10, nexus-mtheb) and goes only to ``maintainer()``.
_T1_HEADING: str = "## T1: the working layer this session shares with its subagents"
_T1_DISPATCH_PROTOCOL: str = (
    "Before a dispatch, `scratch put` the design-of-record and the shared brief context "
    "(ship-when, inputs, prior findings) and name its id in each brief; have agents put "
    "interim findings in T1 under the round tags you give them; read their entries "
    "before acting on a hand-back."
)
_T1_LIFETIME: str = (
    "T1 survives compaction; a `/clear`, `/resume` or fresh restart starts a new scope, "
    "an idle session is swept after 24 h, and flagged entries land in T2 at session end "
    "and on a move to a new id. A `claude -p` child always has its own T1. "
    "Working notes you want after `/clear` go to `scratch` with "
    "`scope=identity` and tag `working`; session scope stays the layer your subagents share."
)

#: The maintainer's block: what every session had before nexus-sby49.3, less the retired-``share_t1`` parenthetical.
_T1_TOP_LINES: list[str] = [_T1_HEADING, _T1_DISPATCH_PROTOCOL, _T1_LIFETIME, ""]


def _t1_top_lines(payload: object) -> list[str]:
    """The T1 block for this session: the dispatch protocol sentence only for ``maintainer()``."""
    if _maintainer(payload):
        return list(_T1_TOP_LINES)
    return [_T1_HEADING, _T1_LIFETIME, ""]


#: The one NEXUS-REPO line that replaces the hook-log tripwire (plan section 7 Phase 2, change 3): a checkout that
#: carries the nexus marker is held to nexus's rules whoever opens it, and this account has no maintainer flag, so
#: nothing outside this checkout is. Printed on every start in such a checkout; silent everywhere else.
_TRIPWIRE_LINE: str = (
    "This checkout carries the nexus marker, so nexus's own development rules apply in it, but this account has no "
    "maintainer flag (`~/.config/nexus/maintainer-profile`), so they apply nowhere else."
)


def _tripwire_lines(payload: object) -> list[str]:
    """The tripwire line when the repo marker is the only reason this session is a maintainer's; ``[]`` otherwise."""
    try:
        if _nexus_repo(payload) and not _account_flag():
            return [_TRIPWIRE_LINE]
    except BaseException:  # noqa: BLE001 — a line of text never costs the session its context
        pass
    return []


def _build_capabilities_block() -> list[str]:
    """Static ``## nx Capabilities`` reference lines.

    Condensed (nexus-h33x8.5 fix-pass, VERIFICATION 1 combined budget)
    from the original prose-heavy form — every distinct backtick-quoted
    token (tool name, flag, example) is preserved; only connective prose
    ("MCP tool", "for metadata filtering", a second redundant prefix
    example) was cut. Pure/static so it is directly measurable and
    testable without a subprocess.
    """
    return [
        "## nx Capabilities",
        "",
        '`search` MCP tool: `where="KEY>=VALUE"` filter, `cluster_by="semantic"` '
        'grouping, `topic="Label"` scoping, `where="section_type!=references"` '
        "noise filter (results carry `chunk_text_hash`)",
        "`query` MCP tool: document-level, catalog-aware (`author`, `content_type`, "
        "`subtree`, `follow_links`, `depth`), taxonomy-boosted",
        "`nx_answer` MCP tool: reduce many documents (minutes); `search`/`query`: lookups (seconds)",
        "`plan_save`/`plan_search` MCP tools: T2, project-scoped plan library",
        "`scratch` MCP tool: T1, shared with this session's subagents (see the T1 block above)",
        "`search`/`links`/`link` MCP tools (nexus-catalog): metadata-first; "
        "`chash:` spans preferred for link creation",
        "`nx enrich bib COLLECTION` (Semantic Scholar) | `nx enrich aspects "
        "COLLECTION` (RDR-089 aspects)",
        "Pagination: search/store_list/memory_search page; footer shows `offset=N`",
        "MCP prefix: `mcp__plugin_conexus_nexus__` "
        "(e.g. `mcp__plugin_conexus_nexus__search`)",
        "",
    ]


def _render_t2_memory(cwd: str) -> list[str]:
    """The ``## T2 Memory (Active Project)`` block for the repo at *cwd*: the project name from ``git rev-parse
    --show-toplevel``, the body from :func:`nexus.hooks.t2_prefix_scan.scan` in-process (bead nexus-b5ugt: no plugin path
    is resolved). ``[]`` when *cwd* is not in a repo or the scan found nothing. Carried from the hook; the caller decides
    whether ``nx`` is available, as :mod:`nexus.brief` runs inside it."""
    toplevel = run_command(['git', 'rev-parse', '--show-toplevel'], timeout=5, cwd=cwd)
    project_name = Path(toplevel).name if toplevel else None
    if not project_name:
        return []
    from nexus.hooks.t2_prefix_scan import scan  # noqa: PLC0415 — deferred: only a real T2 section pays the import

    memory_output = scan(project_name, configure_logging=False)
    if not memory_output:
        return []
    return ["## T2 Memory (Active Project)", memory_output, ""]


def _render_bd_prime(project_dir: Path) -> list[str]:
    """``bd prime``, when the beads plugin's own hook will not run (nexus-dg7i6.4).

    A project can disable the beads plugin to drop its skill-listing entries; its one load-bearing contribution, the
    SessionStart ``bd prime``, is printed here instead. ``[]`` outside a beads repo, with the plugin on, or when the
    primer is empty. It runs ``bd prime`` on its own: SessionStart no longer runs ``bd ready`` before it, and
    ``nx brief beads`` calls it after its own ``bd ready``."""
    cwd = str(project_dir)
    try:
        if (project_dir / '.beads').is_dir():
            from nexus.beads_prime import beads_plugin_enabled  # noqa: PLC0415 — deferred: only a beads repo pays the import

            if not beads_plugin_enabled(project_dir):
                prime_output = run_command(['bd', 'prime'], timeout=BD_TIMEOUT, cwd=cwd)
                if prime_output and prime_output.strip():
                    return [prime_output.strip(), ""]
    except Exception:  # noqa: BLE001 — a hook must never fail; keep the sections already gathered
        debug("bd prime section skipped")
    return []


def _render_beads(project_dir: Path) -> list[str]:
    """The ``## Ready Beads`` block, then ``bd prime`` when it applies. ``[]`` when ``bd`` is not on PATH."""
    if not which('bd'):
        debug("bd command not found")
        return []
    ready_output = run_command(['bd', 'ready'], timeout=BD_TIMEOUT, cwd=str(project_dir))
    lines = _render_ready_beads(ready_output)
    # A bd that just failed or timed out on `bd ready` is not asked again.
    if ready_output is not None:
        lines.extend(_render_bd_prime(project_dir))
    return lines


def _knowledge_map_path(cwd: str) -> str:
    """The cache file ``nx context refresh`` wrote for the repo at *cwd*: the per-repo file under ``NEXUS_CONFIG_DIR``
    (nexus-3fme8) when it exists, else the legacy global ``context_l1.txt`` (existing or not: the hygiene block stats it)."""
    import hashlib  # noqa: PLC0415 — carried verbatim from the script; not a deferred-import optimization

    real = os.path.realpath(cwd)
    repo_hash = hashlib.sha1(real.encode()).hexdigest()[:8]
    repo_name = os.path.basename(real)
    config_dir = os.environ.get("NEXUS_CONFIG_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".config", "nexus"
    )
    per_repo = os.path.join(config_dir, "context", f"{repo_name}-{repo_hash}.txt")
    return per_repo if os.path.exists(per_repo) else os.path.join(config_dir, "context_l1.txt")


def _render_knowledge_map(cwd: str) -> list[str]:
    """The RDR-072 L1 Knowledge Map: the cache file, verbatim, or ``[]`` when there is none or it cannot be read."""
    try:
        path = _knowledge_map_path(cwd)
        if os.path.exists(path):
            # UTF-8 as written; the locale default is cp1252 on Windows (nexus-3fme8).
            with open(path, encoding="utf-8") as f:
                context_l1 = f.read().strip()
            if context_l1:
                return [context_l1, ""]
    except Exception:  # noqa: BLE001 — carried: a hook must never fail
        pass  # Non-fatal — hook must never fail
    return []


def repo_names(cwd: str) -> tuple[str, str]:
    """``(toplevel, main)`` for the repo at *cwd*, from one ``git rev-parse``. *toplevel* is the checkout's own directory
    name, the T2 project the scan used and the reachability probe asks about; *main* is the parent of the git common
    dir, the same for every worktree of one repository, and is the ``repo`` of every usage line so a worktree session
    does not fragment the count. Outside a repo both are *cwd*'s own name."""
    own = os.path.basename(os.path.realpath(cwd))
    out = run_command(['git', 'rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir'],
                      timeout=5, cwd=cwd)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if not lines:
        return own, own
    toplevel = Path(lines[0]).name
    main = Path(lines[1]).parent.name if len(lines) > 1 and Path(lines[1]).name == ".git" else toplevel
    return toplevel, main or toplevel


def repo_name(cwd: str) -> str:
    """The checkout's own name (:func:`repo_names`' first half): the T2 project the scan used."""
    return repo_names(cwd)[0]


def _t2_reachability_lines(project: str) -> list[str]:
    """The fire-only T2 line (:func:`nexus.hooks.t2_prefix_scan.reachability_warning`): ``[]`` when T2 is healthy, and on any
    failure of the probe itself (a hook never fails)."""
    try:
        from nexus.hooks.t2_prefix_scan import reachability_warning  # noqa: PLC0415 — deferred: pays for the T2 client

        warning = reachability_warning(project)
        return [warning] if warning else []
    except Exception:  # noqa: BLE001 — a hook must never fail
        debug("T2 reachability probe skipped")
        return []


def _record_session_start(session_id: str, repo: str, source: str) -> None:
    """One ``session_start`` line in the usage file: the denominator for the 30-day ``nx brief`` review."""
    from nexus.hooks import usage_log  # noqa: PLC0415 — deferred with the rest of the optional work

    usage_log.append("session_start", session_id=session_id, repo=repo, source=source)


def run(payload: dict | None) -> HookResult:
    """Run the SessionStart context hook: the T1 contract, the due threads, the identity-notes line, the fire-only T2
    line, ``bd prime`` where the beads plugin is off, the one ``nx brief`` index line, and the hygiene block.

    Carried from ``session_start_hook.py``'s ``main()``: the ``HookResult`` return in place of ``print()``/``sys.exit(0)``
    is the one structural deviation. Everything that is reference context (T2 memory, ready beads, the bd primer, the
    capabilities block, the Knowledge Map) is not computed here any more; see the module docstring.
    """
    # nexus-mtheb: the T1 contract leads the context; its dispatch sentence is the maintainer's (nexus-sby49.3, I1b).
    audience_payload = payload if isinstance(payload, dict) else {}
    output_lines: list[str] = _t1_top_lines(audience_payload)
    project_dir = Path(os.environ.get('CLAUDE_PROJECT_DIR', os.getcwd())).resolve()
    repo, usage_repo = repo_names(str(project_dir))

    # --- Threads board: this session's threads whose due: has come (nexus-ct74m) ---
    # The one use of the payload: its session id finds the session's own board entry.
    session_id = str((payload or {}).get("session_id") or "")
    _record_session_start(session_id, usage_repo, str((payload or {}).get("source") or ""))
    if session_id:
        try:
            from nexus.hooks.board_due import due_lines  # noqa: PLC0415 — deferred: only a payload with a session id pays

            output_lines.extend(due_lines(session_id))
        except Exception:  # noqa: BLE001 — a hook must never fail; keep the rest of the context
            debug("threads board due section skipped")

    # --- The bound identity's own T2 notes: at most ONE line (RDR-253 Step 6 part 3, nexus-hczdo) ---
    if session_id:
        try:
            from nexus.hooks import identity_notes  # noqa: PLC0415 — deferred: only a payload with a session id pays

            output_lines.extend(identity_notes.notes_lines(session_id))
        except Exception:  # noqa: BLE001 — a hook must never fail; keep the rest of the context
            debug("identity notes line skipped")

    # --- NEXUS-REPO: the marker is the only reason this session is held to nexus's rules (nexus-sby49.3, the tripwire) ---
    output_lines.extend(_tripwire_lines(audience_payload))

    # --- Fire-only: a T2 that cannot be reached, or whose newest entry is stale, is never silent (nexus-8fvp2) ---
    output_lines.extend(_t2_reachability_lines(repo))

    # --- bd prime where the beads plugin is off: that project's only primer (nexus-dg7i6.4); the ready list is on demand ---
    output_lines.extend(_render_bd_prime(project_dir))

    # --- Everything else is on demand: one static line names it ---
    output_lines.append(brief_index_line(project_dir))

    # --- Hygiene (nexus-1if7b) — actionable maintenance signals ---
    # Strict "in moderation" surface: emit a section only when there's
    # something to act on; stay silent when everything is healthy.
    # Currently checks one signal (L1 cache staleness > 7 days, which
    # caught nexus-9iw41's 10-day-stale cache). Add more signals only
    # when each pays for the line-count it costs.
    try:
        _emit_hygiene_block(output_lines, _knowledge_map_path(os.getcwd()))
    except Exception:  # noqa: BLE001 — a hook must never fail
        pass

    return HookResult(stdout="\n".join(output_lines))


def _emit_hygiene_block(output_lines: list, context_l1_path: str | None) -> None:
    """Append a ``## Hygiene`` section to *output_lines* iff actionable.

    nexus-1if7b: high-leverage, low-volume curation prompts at session
    start. Stdlib-only so it runs under whichever bare interpreter
    ``_run_python_hook.sh`` resolves (same constraint as
    ``t2_prefix_scan.py``; see nexus-vg6d4).

    Signals (each line only when triggered):
      * L1 cache age > 7 days — actionable: ``nx context refresh``.

    Non-fatal: any check that raises is dropped silently.
    """
    import time as _time  # noqa: PLC0415 — carried verbatim from the script; not a deferred-import optimization

    signals: list[str] = []

    if context_l1_path:
        try:
            if os.path.exists(context_l1_path):
                age_days = int(
                    (_time.time() - os.path.getmtime(context_l1_path)) // 86400
                )
                if age_days > 7:
                    signals.append(
                        f"- L1 cache {age_days}d old — refresh: `nx context refresh`"
                    )
        except OSError:
            pass

    if signals:
        output_lines.append("## Hygiene")
        output_lines.append("")
        output_lines.extend(signals)
        output_lines.append("")
