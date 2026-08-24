# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The nx-managed post-commit hook must not index a linked worktree.

nexus-ws67k. A worktree is a transient VIEW of a repository. Indexing one is
waste by construction: it is deleted when the branch is done, N worktrees hold
byte-identical content, and the stanza's own pgrep guard compares RESOLVED
PATHS so it cannot see a sibling worktree indexing the same repo.

Nobody opts in, either -- hooks live in the COMMON git dir, so a single
`nx hooks install` in the primary checkout arms every present and future
worktree.

Measured 2026-08-23 over ~/.config/nexus/index.log: 433 runs in 14 days, NINE
of them worktree-targeted in one day, each re-embedding hundreds of files of a
~2151-file tree, several projected at 64-131 minutes, all detached so the cost
never lands on the committing session's clock.

These tests execute the real stanza against real git repositories rather than
grepping it. A guard asserted by substring match is a guard nobody has watched
run -- and this stanza's existing pgrep guard is itself an example of a check
that reads as protection while providing none across worktrees.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus.commands.hooks import _STANZA


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "primary"
    r.mkdir()
    _git("init", "-q", "-b", "main", cwd=r)
    _git("config", "user.email", "t@example.com", cwd=r)
    _git("config", "user.name", "t", cwd=r)
    (r / "a.txt").write_text("hello\n")
    _git("add", "a.txt", cwd=r)
    _git("commit", "-qm", "init", cwd=r)
    return r


def _run_guard(cwd: Path, log: Path) -> tuple[int, str]:
    """Execute ONLY the stanza's guard prologue, with the dispatch stubbed.

    The real stanza ends in a detached `nx index repo ... &`. Replacing that
    line with a marker echo lets the test observe whether control REACHED the
    dispatch, without spawning an indexer.
    """
    body = _STANZA.replace(
        'nx index repo "$REPO_TOP" --on-locked=skip', 'echo REACHED_DISPATCH #'
    )
    script = cwd / "hook.sh"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    # HOME must be the dir CONTAINING .config -- the stanza writes to
    # $HOME/.config/nexus/index.log.
    home = log.parent.parent.parent
    env = {**os.environ, "HOME": str(home)}
    log.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        ["/bin/sh", str(script)], cwd=cwd, capture_output=True, text=True, env=env,
    )
    return p.returncode, p.stdout + p.stderr


def test_primary_checkout_still_reaches_the_dispatch(repo: Path, tmp_path: Path) -> None:
    """Positive control. Without this, a guard that skips EVERYTHING would
    pass the worktree test below and silently disable all indexing."""
    log = tmp_path / "home" / ".config" / "nexus" / "index.log"
    rc, out = _run_guard(repo, log)
    assert rc == 0, out
    text = log.read_text() if log.exists() else ""
    assert "SKIPPED (linked worktree" not in text, (
        "the primary checkout must NOT be treated as a worktree"
    )
    assert "REACHED_DISPATCH" in out or "post-commit" in text, (
        f"primary checkout should reach the dispatch; got {out!r} / {text!r}"
    )


def test_linked_worktree_is_skipped(repo: Path, tmp_path: Path) -> None:
    wt = tmp_path / "linked"
    _git("worktree", "add", "-q", "-b", "feature/x", str(wt), cwd=repo)
    log = tmp_path / "home" / ".config" / "nexus" / "index.log"
    rc, out = _run_guard(wt, log)
    assert rc == 0, out
    assert "REACHED_DISPATCH" not in out, (
        "a linked worktree must never reach the indexer dispatch"
    )
    assert "SKIPPED (linked worktree" in log.read_text(), (
        "the skip must be RECORDED -- invisibility is what let a 403 abort run "
        "for four days unnoticed; a silent skip repeats that mistake"
    )


def test_the_discriminator_is_git_dir_vs_common_dir_not_a_path_prefix(
    repo: Path, tmp_path: Path,
) -> None:
    """Pins the mechanism, not just the outcome.

    Four of the nine worktree runs on 2026-08-23 were under
    `.claude/worktrees/` INSIDE the repo, not under /private/tmp. A guard keyed
    on a temp-path prefix would have looked like it worked while leaving 44% of
    the runs firing. This asserts the git-level discriminator actually
    separates the two cases, so a future refactor to a path test fails here.
    """
    wt = tmp_path / "inside"
    _git("worktree", "add", "-q", "-b", "feature/y", str(wt), cwd=repo)
    for where, expect_same in ((repo, True), (wt, False)):
        gd = Path(_git("rev-parse", "--absolute-git-dir", cwd=where)).resolve()
        raw = _git("rev-parse", "--git-common-dir", cwd=where)
        gc = Path(raw)
        gc = (gc if gc.is_absolute() else where / gc).resolve()
        assert (gd == gc) is expect_same, (
            f"{where}: --git-dir={gd} --git-common-dir={gc}"
        )


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_guard_precedes_the_pgrep_guard_in_the_stanza() -> None:
    """Ordering matters: the pgrep guard is the one that cannot see across
    worktrees, so the worktree check must run BEFORE it, not after."""
    i_wt = _STANZA.index("LINKED-WORKTREE GUARD")
    i_pgrep = _STANZA.index("pgrep guard")
    assert i_wt < i_pgrep, "worktree guard must precede the pgrep guard"
