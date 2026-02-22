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
    Stderr is suppressed unless DEBUG is set.
    """
    stderr = None if DEBUG else subprocess.DEVNULL
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

    if output_lines:
        print("\n".join(output_lines))

    sys.exit(0)


if __name__ == "__main__":
    main()
