#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SessionStart hook: Load project context via nx CLI.
Surfaces T2 memory, beads, and scratch into Claude's session context.
Output goes to stdout and is injected into Claude's session context.
"""

import os
import shutil
import subprocess
import sys
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
        if ready_output:
            lines = ready_output.split('\n')[:10]  # cap at 10 lines
            output_lines.append("## Ready Beads")
            output_lines.append("```")
            output_lines.extend(line[:500] for line in lines)
            output_lines.append("```")
            output_lines.append("")
    else:
        debug("bd command not found")

    # T1 scratch reminder (only when other nx context was surfaced)
    if which('nx') and output_lines:
        output_lines.append("## Session Scratch (T1)")
        output_lines.append("Session-scoped ephemeral scratch available: `nx scratch put/get/list/search`")
        output_lines.append("Flag important scratch entries before session ends: `nx scratch flag <id>`")
        output_lines.append("")

    # --- Capabilities summary (compact, for main conversation awareness) ---
    output_lines.append("## nx Capabilities")
    output_lines.append("")
    output_lines.append("**Search**: `nx search QUERY` — semantic search across T3 collections")
    output_lines.append("  - `--where KEY>=VALUE` operators: `=`, `>=`, `<=`, `>`, `<`, `!=` (numeric fields auto-coerced)")
    output_lines.append("  - `--where chunk_type=table_page` for PDF pages containing tables")
    output_lines.append("  - `--where bib_year>=2024` for bibliographic metadata filtering")
    output_lines.append("")
    output_lines.append("**Analytical queries**: `/nx:query` — multi-step retrieval + analysis")
    output_lines.append("  - Dispatches `query-planner` → `analytical-operator` (extract/summarize/rank/compare/generate)")
    output_lines.append("  - Best for: cross-corpus consistency checks, structured extraction, multi-source synthesis")
    output_lines.append("  - For simple summarize/rank: dispatch `analytical-operator` directly")
    output_lines.append("")
    output_lines.append("**Enrichment**: `nx enrich COLLECTION` — backfill Semantic Scholar metadata (year, venue, authors, citations)")
    output_lines.append("")
    output_lines.append("**Plan library**: `plan_save`/`plan_search` MCP tools — save and reuse query execution plans (T2, project-scoped)")
    output_lines.append("")
    output_lines.append("**Pagination**: MCP `search`, `store_list`, `memory_search` return paged results.")
    output_lines.append("  - Default page size 10-20. Response footer shows `Next page: offset=N`.")
    output_lines.append("  - Re-call with `offset=N` to get the next page. Never truncates — all data accessible.")
    output_lines.append("")

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
