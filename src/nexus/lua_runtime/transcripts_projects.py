# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Which Claude Code project directories hold one repository's sessions.

``transcripts.sessions`` takes ``project="repo"`` (T2 nexus/rdr242-interfaces
v13): every project directory whose sessions belong to the repository the run
started in. A repository's sessions sit in one directory per working tree, the
main checkout, each worktree and Claude's own isolation worktrees under
``.claude/worktrees``. Counting the main checkout's directory alone missed 80
of 178 nexus main sessions (T2 nexus_rdr/238-research-rederived-2026-11-08).

A directory name is a lossy encoding of a path (``/`` and ``_`` both become
``-``), so membership is read from the ``cwd`` a session transcript records,
and it must hold for a worktree that has since been removed. A directory
belongs to the repository whose main checkout is P when a recorded cwd:

1. is P or lies under it (Claude's isolation worktrees included);
2. lies under the sibling worktree root ``P-wt/`` (this repository's layout);
3. is, or lies under, a worktree that ``git worktree list`` names; or
4. exists and git places it in P's repository (same common dir).

Rules 1 to 3 need no existing path; rule 4 catches a live worktree that none
of them names, such as one moved by hand. A clone is another repository and
never belongs. Prefixes compare whole path components.

Parent side only (the verbs run in the parent).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

from nexus.lua_runtime.verb_types import VerbTimeout

# How many transcripts of a directory are read, and how many lines of each,
# looking for a recorded cwd. A session writes its cwd on its first user line.
_FILES_PER_DIR: int = 3
_LINES_PER_FILE: int = 40


def _left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise VerbTimeout(
            "transcripts.sessions: the deadline passed while resolving the repo"
        )
    return left


def _git(args: list[str], cwd: Path, deadline: float) -> str | None:
    """git's stdout, or None when git refuses; the caller's GIT_* settings never reach it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_left(deadline),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise VerbTimeout(
            "transcripts.sessions: git did not answer before the deadline"
        ) from None
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _common_dir(path: Path, deadline: float) -> Path | None:
    out = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], path, deadline
    )
    return Path(os.path.realpath(out)) if out else None


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _recorded_cwds(directory: Path) -> Iterable[Path]:
    for transcript in sorted(directory.glob("*.jsonl"))[:_FILES_PER_DIR]:
        try:
            with transcript.open(encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh):
                    if n >= _LINES_PER_FILE:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    cwd = record.get("cwd") if isinstance(record, dict) else None
                    if isinstance(cwd, str) and os.path.isabs(cwd):
                        yield Path(os.path.realpath(cwd))
                        break
        except OSError:
            continue


def repo_project_dirs(
    projects_root: Path, repo_root: Path, *, deadline: float
) -> list[str]:
    """The sorted names of the project directories holding *repo_root*'s sessions.

    *repo_root* may be the main checkout or any of its worktrees; both give the
    same set. Raises ``ValueError`` when it is not in a git repository, and
    ``VerbTimeout`` once *deadline* (a ``time.monotonic()`` value) has passed.
    """
    _left(deadline)
    common = _common_dir(Path(repo_root), deadline)
    if common is None:
        raise ValueError(f"{repo_root} is not a git repository")
    main = common.parent
    sibling = main.parent / f"{main.name}-wt"
    listed = _git(["worktree", "list", "--porcelain"], main, deadline) or ""
    worktrees = [
        Path(os.path.realpath(line[len("worktree ") :]))
        for line in listed.splitlines()
        if line.startswith("worktree ")
    ]

    def belongs(cwd: Path) -> bool:
        if _under(cwd, main) or _under(cwd, sibling):
            return True
        if any(_under(cwd, wt) for wt in worktrees):
            return True
        return cwd.is_dir() and _common_dir(cwd, deadline) == common

    found: list[str] = []
    for directory in sorted(p for p in Path(projects_root).iterdir() if p.is_dir()):
        _left(deadline)
        if any(belongs(cwd) for cwd in _recorded_cwds(directory)):
            found.append(directory.name)
    return found
