#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SessionStart hook: Load project context via nx CLI.
Surfaces T2 memory, beads, and scratch into Claude's session context.
Output goes to stdout and is injected into Claude's session context.
"""
from __future__ import annotations

import sys
if sys.version_info < (3, 12):
    sys.stderr.write(
        f"ERROR: conexus plugin hook requires Python 3.12+, got {sys.version.split()[0]}\n"
        f"  Resolved: {sys.executable}\n"
        f"  Install: brew install python@3.13 (macOS) | apt install python3.12 (Ubuntu) | uv python install 3.12\n"
    )
    sys.exit(1)

import os
import shutil
import subprocess
from pathlib import Path

# Configuration via environment variables
DEBUG = os.environ.get('NX_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))
BD_TIMEOUT = int(os.environ.get('BD_TIMEOUT', '5'))


def debug(msg: str) -> None:
    """Print debug message to stderr if debugging enabled."""
    if DEBUG:
        print(f"[session-hook] {msg}", file=sys.stderr)


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
            print(f"[session-hook] stderr from {args[0]}: {result.stderr[:500]}", file=sys.stderr)
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


def main() -> None:
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
            scan_script = Path(__file__).parent / "t2_prefix_scan.py"
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
        import hashlib
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
    except Exception:
        pass  # Non-fatal — hook must never fail

    # --- Hygiene (nexus-1if7b) — actionable maintenance signals ---
    # Strict "in moderation" surface: emit a section only when there's
    # something to act on; stay silent when everything is healthy.
    # Currently checks one signal (L1 cache staleness > 7 days, which
    # caught nexus-9iw41's 10-day-stale cache). Add more signals only
    # when each pays for the line-count it costs.
    _emit_hygiene_block(output_lines, context_l1_path)

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


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
    import time as _time

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


if __name__ == "__main__":
    main()
