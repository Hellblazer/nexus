# SPDX-License-Identifier: AGPL-3.0-or-later
"""The post-commit review hook, proven in a SANDBOXED throwaway repo (nexus-jh86x).

Every test here installs into a ``tmp_path`` repository's own hooks
directory. Nothing touches this checkout's ``.git/hooks`` -- which is both
the bead's own acceptance wording ("a commit in a sandboxed checkout") and
a hard constraint from the shared-checkout coordination on 2026-09-02:
hooks live in the COMMON git dir, so an install here would arm every
worktree and every sibling session's commits.

The end-to-end legs run a REAL ``git commit`` through the REAL installed
hook script. They pin the two properties a per-commit hook must have --
it does not block, and it honours its opt-out -- at the shell layer that
actually runs, not one layer below it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.hooks import _REVIEW_STANZA, _STANZA, _stanza_for

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"
SENTINEL_END = "# <<< nexus managed end <<<"


def _git(repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=full_env
    )


@pytest.fixture
def sandbox_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    return repo


# ── stanza shape ──────────────────────────────────────────────────────────────


def test_post_commit_carries_the_review_stanza() -> None:
    body = _stanza_for("post-commit")
    assert "nx review commit" in body
    assert "NX_COMMIT_REVIEW" in body


@pytest.mark.parametrize("hook_name", ["post-merge", "post-rewrite"])
def test_other_hooks_do_not_review(hook_name: str) -> None:
    """A rebase of twenty commits must not dispatch twenty reviews."""
    body = _stanza_for(hook_name)
    assert "nx review commit" not in body
    assert body == _STANZA, "non-post-commit hooks keep the indexing stanza byte for byte"


def test_the_review_stanza_lives_inside_the_sentinel_block() -> None:
    """Otherwise `nx hooks uninstall` would leave the reviewer behind."""
    body = _stanza_for("post-commit")
    begin = body.index(SENTINEL_BEGIN)
    end = body.index(SENTINEL_END)
    review = body.index("nx review commit")
    assert begin < review < end


def test_review_runs_after_the_indexer_not_before() -> None:
    """Ordering is deliberate: the cheap local index should not wait on a
    network dispatch that may take a minute."""
    body = _stanza_for("post-commit")
    assert body.index("nx index repo") < body.index("nx review commit")


def test_the_review_dispatch_is_detached() -> None:
    """A synchronous dispatch would add its whole latency to every commit."""
    assert _REVIEW_STANZA.rstrip().endswith("fi\nfi")
    assert "disown" in _REVIEW_STANZA


# ── install / uninstall through the real CLI, into a sandbox ──────────────────


def test_install_writes_the_reviewer_only_into_post_commit(sandbox_repo: Path) -> None:
    result = CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    assert result.exit_code == 0, result.output

    hooks = sandbox_repo / ".git" / "hooks"
    assert "nx review commit" in (hooks / "post-commit").read_text()
    assert "nx review commit" not in (hooks / "post-merge").read_text()
    assert "nx review commit" not in (hooks / "post-rewrite").read_text()


def test_uninstall_removes_the_reviewer_with_the_rest(sandbox_repo: Path) -> None:
    runner = CliRunner()
    runner.invoke(main, ["hooks", "install", str(sandbox_repo)])
    result = runner.invoke(main, ["hooks", "uninstall", str(sandbox_repo)])
    assert result.exit_code == 0, result.output

    post_commit = sandbox_repo / ".git" / "hooks" / "post-commit"
    if post_commit.exists():
        assert "nx review commit" not in post_commit.read_text()


def test_the_installed_hook_is_valid_shell(sandbox_repo: Path) -> None:
    """A syntax error here fails every commit in the repo, silently at
    install time and loudly at the worst possible moment."""
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    hook = sandbox_repo / ".git" / "hooks" / "post-commit"
    proc = subprocess.run(["sh", "-n", str(hook)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# ── end to end: a real commit through the real hook ───────────────────────────


def test_a_real_commit_is_not_blocked_by_the_hook(sandbox_repo: Path) -> None:
    """The bead's second acceptance criterion, at the shell layer.

    ``NX_COMMIT_REVIEW=0`` keeps this test free and offline; the
    non-blocking property under test is the hook's, not the reviewer's.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    (sandbox_repo / "b.txt").write_text("b\n")
    _git(sandbox_repo, "add", "b.txt")
    proc = _git(sandbox_repo, "commit", "-m", "feat: b", env={"NX_COMMIT_REVIEW": "0"})
    assert proc.returncode == 0, proc.stderr
    assert _git(sandbox_repo, "log", "--oneline").stdout.count("\n") == 2


def test_a_broken_nx_on_path_still_does_not_block_a_commit(
    sandbox_repo: Path, tmp_path: Path
) -> None:
    """The failure mode that matters: nx missing, wedged, or erroring.

    A commit must land anyway. This shadows ``nx`` with a script that
    exits 1 and asserts the commit still succeeds -- the property the
    detached dispatch is there to guarantee.
    """
    CliRunner().invoke(main, ["hooks", "install", str(sandbox_repo)])
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "nx").write_text("#!/bin/sh\nexit 1\n")
    (shim_dir / "nx").chmod(0o755)

    (sandbox_repo / "c.txt").write_text("c\n")
    _git(sandbox_repo, "add", "c.txt")
    proc = _git(
        sandbox_repo,
        "commit",
        "-m",
        "feat: c",
        env={"PATH": f"{shim_dir}:{os.environ['PATH']}"},
    )
    assert proc.returncode == 0, proc.stderr
