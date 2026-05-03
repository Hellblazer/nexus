# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-103 Phase 4 invariant: ``_repo_identity`` is stable.

The conformant collection-name migration uses repo identity to look up
the catalog owner (``Catalog.owner_for_repo(repo_hash)``) and the
legacy registry helpers use the same identity to construct legacy
collection names. If the two identities diverge for any reason
(symlinks, worktrees, environment variations), the migration would
look up an owner that does not exist OR rename a collection that
does not match the legacy name in T3.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.registry import _repo_identity


def _init_git(repo: Path) -> None:
    """Initialise a minimal git repo so ``rev-parse --git-common-dir``
    succeeds. The migration's repo-identity stability assumption rests
    on ``rev-parse``'s behaviour, so the tests must run against real
    git, not a path-only fallback.
    """
    import subprocess

    subprocess.run(
        ["git", "init", "--quiet"], cwd=repo, check=True,
        capture_output=True,
    )


def test_repo_identity_deterministic(tmp_path: Path) -> None:
    """Two ``_repo_identity`` calls against the same path return the
    same ``(name, hash)``. Path-derived; no clock or randomness."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    _init_git(repo)
    a = _repo_identity(repo)
    b = _repo_identity(repo)
    assert a == b


def test_repo_identity_path_hash_is_8_hex(tmp_path: Path) -> None:
    """Path hash slice is exactly 8 hex characters, lowercase."""
    repo = tmp_path / "myproject"
    repo.mkdir()
    _init_git(repo)
    _, h = _repo_identity(repo)
    assert len(h) == 8
    assert all(c in "0123456789abcdef" for c in h)


def test_repo_identity_different_paths_differ(tmp_path: Path) -> None:
    repo_a = tmp_path / "alpha"
    repo_a.mkdir()
    _init_git(repo_a)
    repo_b = tmp_path / "beta"
    repo_b.mkdir()
    _init_git(repo_b)
    assert _repo_identity(repo_a) != _repo_identity(repo_b)


def test_repo_identity_worktree_resolves_to_main_repo(tmp_path: Path) -> None:
    """``_repo_identity`` uses ``rev-parse --git-common-dir`` so a
    worktree resolves to the main repo's identity. Two calls, one
    from the main repo and one from the worktree, must return the
    same ``(name, hash)``.
    """
    import subprocess

    main = tmp_path / "main"
    main.mkdir()
    _init_git(main)
    # Make at least one commit so ``git worktree add`` succeeds.
    (main / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "seed.txt"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "seed", "--quiet"],
        cwd=main, check=True, capture_output=True,
    )
    worktree = tmp_path / "main-feature"
    subprocess.run(
        ["git", "worktree", "add", "--quiet", str(worktree), "-b", "feature"],
        cwd=main, check=True, capture_output=True,
    )
    main_id = _repo_identity(main)
    worktree_id = _repo_identity(worktree)
    assert main_id == worktree_id


def test_repo_identity_falls_back_to_path_when_not_a_git_repo(
    tmp_path: Path,
) -> None:
    """Non-git directory returns identity derived from the path
    itself. The hash is still 8 hex chars; the basename is the
    directory name. Migration must NOT crash on non-git invocations
    (e.g. ad-hoc test fixtures).
    """
    repo = tmp_path / "ungitted"
    repo.mkdir()
    name, h = _repo_identity(repo)
    assert name == "ungitted"
    assert len(h) == 8
