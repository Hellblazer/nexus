#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SessionStart hook: Load project context via nx CLI.
Surfaces T2 memory, beads, and scratch into Claude's session context.
Output goes to stdout and is injected into Claude's session context.
"""
from __future__ import annotations

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
    output_lines.append("Search: `search` MCP tool — `where=\"KEY>=VALUE\"` for metadata filtering, `cluster_by=\"semantic\"` for result grouping, `topic=\"Label\"` for topic-scoped search, `where=\"section_type!=references\"` to filter noise (results include `chunk_text_hash` metadata)")
    output_lines.append("Document search: `query` MCP tool — document-level results with catalog-aware routing (`author`, `content_type`, `subtree`, `follow_links`, `depth`), taxonomy-boosted ranking")
    output_lines.append("Analytical queries: `/nx:query` skill — multi-step retrieval and analysis")
    output_lines.append("Plan library: `plan_save`/`plan_search` MCP tools (T2, project-scoped)")
    output_lines.append("Scratch: `scratch` MCP tool — session-scoped, shared across agents")
    output_lines.append("Catalog: `search`/`links`/`link` MCP tools (nexus-catalog server) — metadata-first routing; link creation with `chash:` spans (content-addressed, preferred)")
    output_lines.append("Enrichment: `nx enrich COLLECTION` (CLI only)")
    output_lines.append("Pagination: search/store_list/memory_search return paged results. Footer shows `offset=N` for next page.")
    output_lines.append("")
    output_lines.append("MCP tool prefix: `mcp__plugin_nx_nexus__` (e.g. `mcp__plugin_nx_nexus__search`, `mcp__plugin_nx_nexus__query`)")
    output_lines.append("")

    # --- RDR-078 Plan Library (SC-7): try plans before decomposing queries ---
    output_lines.append("## Plan Library (RDR-078)")
    output_lines.append("")
    output_lines.append("Before any retrieval task, call `plan_match(intent, dimensions={verb:<verb>}, min_confidence=0.85, n=1)` and if a match lands, execute via `plan_run(match, bindings=...)`. Five scenario verbs ship at `scope:global`:")
    output_lines.append("")
    output_lines.append("- **research** — design / architecture / planning (walk from prose to implementing code)")
    output_lines.append("- **review** — critique / audit a change set (decision-evolution traversal)")
    output_lines.append("- **analyze** — synthesis across prose + code (reference-chain + rank + generate)")
    output_lines.append("- **debug** — dev / debug from a failing path (flat; Serena handles symbol-level)")
    output_lines.append("- **document** — doc authoring or coverage audit")
    output_lines.append("")
    output_lines.append("Plan-mgmt: `plan-author`, `plan-inspect` (default | dimensions), `plan-promote/propose`.")
    output_lines.append("Gate: `/nx:plan-first` before any retrieval. Fall through to `/nx:query` only on miss.")
    output_lines.append("Tools: `plan_match`, `plan_run`, `plan_save`, `plan_search`, `traverse`.")
    output_lines.append("")

    # --- L1 Knowledge Map (RDR-072) — per-repo cached topic labels ---
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

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
