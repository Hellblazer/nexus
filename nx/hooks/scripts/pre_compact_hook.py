#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
PreCompact hook: Output structured ground truth before context compaction.
No prose, no reminders — just data.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Configuration via environment variables
DEBUG = os.environ.get('PM_HOOK_DEBUG', '0') == '1'
NX_TIMEOUT = int(os.environ.get('NX_TIMEOUT', '10'))
BD_TIMEOUT = int(os.environ.get('BD_TIMEOUT', '5'))
GIT_TIMEOUT = int(os.environ.get('GIT_TIMEOUT', '5'))


def debug(msg: str) -> None:
    """Print debug message to stderr if debugging enabled."""
    if DEBUG:
        print(f"[compact-hook] {msg}", file=sys.stderr)


def which(cmd: str) -> bool:
    """Return True if cmd is found on PATH."""
    return shutil.which(cmd) is not None


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


def _git_branch(cwd: str) -> str:
    """Return current git branch name or 'unknown'."""
    output = run_command(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], timeout=GIT_TIMEOUT, cwd=cwd)
    return output or "unknown"


def _git_uncommitted_count(cwd: str) -> int:
    """Return count of uncommitted files from git status --porcelain."""
    output = run_command(['git', 'status', '--porcelain'], timeout=GIT_TIMEOUT, cwd=cwd)
    if not output:
        return 0
    return len(output.splitlines())


def _git_recent_commits(cwd: str, count: int = 5) -> str:
    """Return last N one-line commit messages."""
    output = run_command(
        ['git', 'log', f'--oneline', f'-{count}'],
        timeout=GIT_TIMEOUT, cwd=cwd,
    )
    return output or "(no commits)"


def _repo_name(cwd: str) -> str:
    """Return repo basename from git toplevel, or cwd basename."""
    toplevel = run_command(['git', 'rev-parse', '--show-toplevel'], timeout=GIT_TIMEOUT, cwd=cwd)
    if toplevel:
        return Path(toplevel).name
    return Path(cwd).name


def main() -> None:
    project_dir = Path(os.environ.get('CLAUDE_PROJECT_DIR', os.getcwd())).resolve()
    cwd = str(project_dir)

    lines: list[str] = ["## Context Snapshot (pre-compaction)", ""]

    # Git info
    branch = _git_branch(cwd)
    uncommitted = _git_uncommitted_count(cwd)
    lines.append(f"Git: branch={branch}, {uncommitted} uncommitted files")

    recent = _git_recent_commits(cwd, 5)
    lines.append(f"Recent commits: {recent}")
    lines.append("")

    # Beads in_progress
    if which('bd'):
        beads_output = run_command(
            ['bd', 'list', '--status=in_progress'],
            timeout=BD_TIMEOUT, cwd=cwd,
        )
        if beads_output:
            bead_lines = beads_output.splitlines()[:10]
            lines.append(f"Beads (in_progress): {chr(10).join(bead_lines)}")
        else:
            lines.append("Beads (in_progress): (none)")
    else:
        lines.append("Beads (in_progress): (bd not available)")
    lines.append("")

    # PM status
    if which('nx'):
        pm_output = run_command(['nx', 'pm', 'status'], timeout=NX_TIMEOUT, cwd=cwd)
        lines.append(f"PM: {pm_output or '(no PM project)'}")
    else:
        lines.append("PM: (nx not available)")
    lines.append("")

    # Memory entries
    repo = _repo_name(cwd)
    if which('nx'):
        memory_output = run_command(
            ['nx', 'memory', 'list', '--project', repo],
            timeout=NX_TIMEOUT, cwd=cwd,
        )
        if memory_output:
            mem_lines = memory_output.splitlines()[:8]
            lines.append(f"Memory ({repo}):")
            lines.extend(f"  {ml}" for ml in mem_lines)
        else:
            lines.append(f"Memory ({repo}): (empty)")
    else:
        lines.append(f"Memory ({repo}): (nx not available)")

    print("\n".join(lines))
    sys.exit(0)


if __name__ == "__main__":
    main()
