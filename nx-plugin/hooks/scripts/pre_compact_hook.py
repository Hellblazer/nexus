#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PreCompact hook: Remind to save context before compaction.
Uses nx CLI for T2/T3 storage; keeps bead tracking with bd.
"""

import os
import subprocess
import sys
from pathlib import Path

# Configuration via environment variables
DEBUG = os.environ.get('PM_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))


def debug(msg: str) -> None:
    """Print debug message to stderr if debugging enabled."""
    if DEBUG:
        print(f"[compact-hook] {msg}", file=sys.stderr)


def which(cmd: str) -> bool:
    """Return True if cmd is found on PATH."""
    result = subprocess.run(
        ['which', cmd],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def run_command(args: list[str], timeout: int, cwd: str | None = None) -> str | None:
    """Run a command and return its stdout, or None on failure."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
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


def main() -> None:
    project_dir = Path(os.environ.get('CLAUDE_PROJECT_DIR', os.getcwd())).resolve()
    cwd = str(project_dir)

    print("# Context Save Reminder")
    print("")
    print("Before compaction, ensure you have saved important context:")
    print("")
    print("1. Update bead status:")
    print("   `bd update <id> --status [done|in_progress|blocked]`")
    print("")
    print("2. Save session context to T2 memory (nx memory):")
    print("   `nx memory put \"<summary>\" --project <project> --title context.md`")
    print("")
    print("3. Persist knowledge findings to T3 store (if applicable):")
    print("   `nx store put \"<content>\" --collection knowledge__<topic>`")
    print("")

    # Show current PM status if nx is available
    if which('nx'):
        status_output = run_command(['nx', 'pm', 'status'], timeout=NX_TIMEOUT, cwd=cwd)
        if status_output:
            print("## Current PM State")
            print(status_output)
            print("")
    else:
        debug("nx not found on PATH, skipping pm status check")

    sys.exit(0)


if __name__ == "__main__":
    main()
