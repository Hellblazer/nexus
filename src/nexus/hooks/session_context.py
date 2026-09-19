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
OTHER SessionStart script: T2 memory (via the sibling ``t2_prefix_scan.py``
subprocess), ``bd ready``, the static capabilities block, the RDR-072 L1
Knowledge Map cache, and the nexus-1if7b hygiene block. Do not conflate the
two; they are ported separately because they are separately wired.

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
   is not a behavior change to author; it is the same class of fix
   ``stop_verification.py`` already made for its own sibling script
   (``read_verification_config.py``, bead nexus-q02nx.13/.17): resolve the
   plugin root via ``CLAUDE_PLUGIN_ROOT`` (falling back to the dev-checkout
   layout for a from-source run), and reach the still-plugin-only sibling
   through it. See :func:`_plugin_root` below.

**Payload is unused.** The script never reads stdin at all -- it resolves
everything from ``CLAUDE_PROJECT_DIR``/``os.getcwd()`` and local
environment variables, so ``run``'s ``payload`` parameter is accepted (for
the shared ``run(payload: dict | None) -> HookResult`` contract every verb
implements) and ignored, exactly as the script ignored stdin.

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
``nexus.hooks`` (``session_start_verb``, ``ledger_verbs``,
``subagent_start_stamp``, ...), not something this port introduces or
could fix by itself -- fixing it means deferring ``nexus.hooks``'s own
package-level imports, a change shared by every hook this epic is
concurrently porting, and out of scope for a "move, do not rewrite" bead.
Reported rather than silently worked around.

**Not a ledger verb.** No caller branches on this verb's exit code; every
non-ledger verb is forced to exit 0 by ``entry.main`` regardless of what
:class:`~nexus._hook_runtime._io.HookResult.exit_code` says.

**Not wired into ``hooks.json`` yet.** The live ``conexus/hooks/hooks.json``
still runs ``session_start_hook.py`` directly via ``_run_python_hook.sh``;
re-declaring that entry as an ``nx-hook session-context`` exec-form command
is RDR-215 Approach item 6, a later bead. This module is reachable and
tested, but no ordinary session runs it today.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from nexus._hook_runtime._io import HookResult
from nexus.hooks._plugin import plugin_root

__all__ = ["run"]

# Configuration via environment variables
DEBUG = os.environ.get('NX_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))
BD_TIMEOUT = int(os.environ.get('BD_TIMEOUT', '5'))


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
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
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
        "`/conexus:query` skill: multi-step retrieval/analysis",
        "`plan_save`/`plan_search` MCP tools: T2, project-scoped plan library",
        "`scratch` MCP tool: session-scoped, shared across agents",
        "`search`/`links`/`link` MCP tools (nexus-catalog): metadata-first; "
        "`chash:` spans preferred for link creation",
        "`nx enrich bib COLLECTION` (Semantic Scholar) | `nx enrich aspects "
        "COLLECTION` (RDR-089 aspects)",
        "Pagination: search/store_list/memory_search page; footer shows `offset=N`",
        "MCP prefix: `mcp__plugin_conexus_nexus__` "
        "(e.g. `mcp__plugin_conexus_nexus__search`)",
        "",
    ]


def _plugin_root() -> Path:
    """Resolve the conexus plugin's root directory.

    ``CLAUDE_PLUGIN_ROOT`` first -- set by Claude Code for every plugin
    hook invocation, real or dispatched through ``nx-hook`` -- falling back
    to the dev-checkout layout (this file lives at
    ``<repo>/src/nexus/hooks/session_context.py``; the plugin root is
    ``<repo>/conexus``) for a from-source run with no plugin host in the
    picture. Mirrors ``stop_verification.py``'s ``_plugin_root()``
    (bead nexus-q02nx.13/.17), the established precedent for a ported hook
    that still needs a not-yet-ported plugin-only sibling script.
    """
    return plugin_root()


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — the script never reads stdin; payload is unused, matching it
    """Run the SessionStart T2/beads/capabilities/knowledge-map hook.

    Carried from ``session_start_hook.py``'s ``main()``: same helpers, same
    control flow, same env-var names. Two deviations from a literal
    line-for-line carry are unavoidable and documented in the module
    docstring above -- the ``HookResult`` return in place of
    ``print()``/``sys.exit(0)``, and the sibling-script path resolution via
    :func:`_plugin_root` in place of ``Path(__file__).parent`` (this
    module's parent directory no longer holds ``t2_prefix_scan.py``).
    """
    project_dir = Path(os.environ.get('CLAUDE_PROJECT_DIR', os.getcwd())).resolve()
    cwd = str(project_dir)

    output_lines: list[str] = []

    # --- T2 memory context ---
    if which('nx'):
        project_name = None
        toplevel = run_command(['git', 'rev-parse', '--show-toplevel'], timeout=5, cwd=cwd)
        if toplevel:
            project_name = Path(toplevel).name

        if project_name:
            # Use t2_prefix_scan to surface all namespaces (bare, _rdr, etc.)
            scan_script = _plugin_root() / "hooks" / "scripts" / "t2_prefix_scan.py"
            memory_output = run_command(
                [sys.executable, str(scan_script), project_name],
                timeout=NX_TIMEOUT, cwd=cwd
            )
            if memory_output:
                output_lines.append("## T2 Memory (Active Project)")
                output_lines.append(memory_output)
                output_lines.append("")
    else:
        debug("nx not found on PATH, skipping T2 memory context")

    # --- bd ready ---
    if which('bd'):
        ready_output = run_command(['bd', 'ready'], timeout=BD_TIMEOUT, cwd=cwd)
        output_lines.extend(_render_ready_beads(ready_output))
    else:
        debug("bd command not found")

    # --- Capabilities summary (AI-optimized, minimal tokens) ---
    output_lines.extend(_build_capabilities_block())

    # --- L1 Knowledge Map (RDR-072) — per-repo cached topic labels ---
    context_l1_path: str | None = None
    try:
        import hashlib  # noqa: PLC0415 — carried verbatim from the script; not a deferred-import optimization
        cwd = os.getcwd()
        repo_hash = hashlib.sha1(os.path.realpath(cwd).encode()).hexdigest()[:8]
        repo_name = os.path.basename(os.path.realpath(cwd))
        context_dir = os.path.join(os.path.expanduser("~"), ".config", "nexus", "context")
        context_l1_path = os.path.join(context_dir, f"{repo_name}-{repo_hash}.txt")
        # Fallback to legacy global file
        if not os.path.exists(context_l1_path):
            context_l1_path = os.path.join(os.path.expanduser("~"), ".config", "nexus", "context_l1.txt")
        if os.path.exists(context_l1_path):
            with open(context_l1_path) as f:
                context_l1 = f.read().strip()
            if context_l1:
                output_lines.append(context_l1)
                output_lines.append("")
    except Exception:  # noqa: BLE001 — carried: a hook must never fail
        pass  # Non-fatal — hook must never fail

    # --- Hygiene (nexus-1if7b) — actionable maintenance signals ---
    # Strict "in moderation" surface: emit a section only when there's
    # something to act on; stay silent when everything is healthy.
    # Currently checks one signal (L1 cache staleness > 7 days, which
    # caught nexus-9iw41's 10-day-stale cache). Add more signals only
    # when each pays for the line-count it costs.
    _emit_hygiene_block(output_lines, context_l1_path)

    if output_lines:
        return HookResult(stdout="\n".join(output_lines))
    return HookResult()


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
