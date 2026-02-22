#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SessionStart hook: Load project context via nx CLI.
Runs nx pm resume and nx pm status to inject PM context into Claude's session.
Output goes to stdout and is injected into Claude's session context.
"""

import os
import subprocess
import sys
from pathlib import Path

# Configuration via environment variables
DEBUG = os.environ.get('PM_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))
BD_TIMEOUT = int(os.environ.get('BD_TIMEOUT', '5'))


def debug(msg: str) -> None:
    """Print debug message to stderr if debugging enabled."""
    if DEBUG:
        print(f"[session-hook] {msg}", file=sys.stderr)


def which(cmd: str) -> bool:
    """Return True if cmd is found on PATH."""
    result = subprocess.run(
        ['which', cmd],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


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

    # --- nx pm context ---
    if not which('nx'):
        debug("nx not found on PATH, skipping nx pm context")
    else:
        resume_output = run_command(['nx', 'pm', 'resume'], timeout=NX_TIMEOUT, cwd=cwd)
        status_output = run_command(['nx', 'pm', 'status'], timeout=NX_TIMEOUT, cwd=cwd)

        if resume_output or status_output:
            output_lines.append("# Project Management Context")
            output_lines.append("")

        if resume_output:
            output_lines.append("## Resume")
            output_lines.append(resume_output)
            output_lines.append("")

        if status_output:
            output_lines.append("## Status")
            output_lines.append(status_output)
            output_lines.append("")

        # Show available T2 memory docs for active project
        project_name = None
        try:
            import subprocess as sp
            git_result = sp.run(
                ['git', 'rev-parse', '--show-toplevel'],
                capture_output=True, text=True, timeout=5, cwd=cwd
            )
            if git_result.returncode == 0:
                project_name = Path(git_result.stdout.strip()).name
        except Exception:
            pass

        if project_name:
            memory_output = run_command(
                ['nx', 'memory', 'list', '--project', f'{project_name}_active'],
                timeout=NX_TIMEOUT, cwd=cwd
            )
            if memory_output:
                lines = memory_output.split('\n')[:8]  # cap at 8 lines
                output_lines.append("## T2 Memory (Active Project)")
                output_lines.append("```")
                output_lines.extend(line[:200] for line in lines)
                output_lines.append("```")
                output_lines.append("")

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

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
