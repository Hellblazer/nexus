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

    # --- Capabilities summary (AI-optimized, minimal tokens) ---
    output_lines.append("## nx Capabilities")
    output_lines.append("")
    output_lines.append("Search: `nx search QUERY` — `--where KEY>=VALUE` (operators: = >= <= > < !=, numeric auto-coerced)")
    output_lines.append("Analytical queries: `/nx:query` — cross-corpus consistency, structured extraction, multi-source synthesis")
    output_lines.append("Enrichment: `nx enrich COLLECTION` — Semantic Scholar metadata backfill")
    output_lines.append("Plan library: `plan_save`/`plan_search` MCP tools (T2, project-scoped)")
    output_lines.append("Scratch: `nx scratch put/search/list/flag` — session-scoped, shared across agents")
    output_lines.append("Pagination: search/store_list/memory_search return paged results. Footer shows `offset=N` for next page.")
    output_lines.append("")

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
