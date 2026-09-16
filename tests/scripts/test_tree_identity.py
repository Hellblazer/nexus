# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/lib/tree_identity.py: the artifact manifest's tree identity.

An artifact may be reused only against a tree proven identical to the one it
was built from. The first cut hashed tracked entries from the index, so an
UNSTAGED edit to a tracked file changed what a build produced without
changing the identity; the 7.49.0 battery ran with every version bump and the
engine floor unstaged and reported the underlying commit's identity
(2026-09-16). These tests build a real git repository and pin that the
identity follows the working tree: an unstaged edit, an executable bit, a
symlink retarget and an untracked file each move it, and restoring the file
restores it.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "e2e" / "lib" / "tree_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("tree_identity_under_test", _HELPER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("one\n")
    (root / "run.sh").write_text("#!/bin/sh\necho hi\n")
    os.symlink("a.txt", root / "link")
    _git(root, "add", "a.txt", "run.sh", "link")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _hash(mod, root: Path) -> str:
    return mod.tree_identity(str(root))["tree_hash"]


def test_clean_tree_is_stable_and_not_dirty(repo: Path) -> None:
    mod = _load()
    first = mod.tree_identity(str(repo))
    assert first["dirty"] is False
    assert first["file_count"] == 3
    assert _hash(mod, repo) == first["tree_hash"]


def test_an_unstaged_edit_to_a_tracked_file_moves_the_identity(repo: Path) -> None:
    """The 7.49.0 defect: staged blob unchanged, working tree changed."""
    mod = _load()
    before = _hash(mod, repo)
    (repo / "a.txt").write_text("two\n")
    assert _git(repo, "diff", "--cached", "--name-only") == "", "nothing staged, by construction"
    after = mod.tree_identity(str(repo))
    assert after["tree_hash"] != before
    assert after["dirty"] is True
    (repo / "a.txt").write_text("one\n")
    assert _hash(mod, repo) == before, "restoring the bytes restores the identity"


def test_a_staged_edit_moves_it_the_same_way(repo: Path) -> None:
    mod = _load()
    before = _hash(mod, repo)
    (repo / "a.txt").write_text("two\n")
    unstaged = _hash(mod, repo)
    _git(repo, "add", "a.txt")
    assert _hash(mod, repo) == unstaged, "staging is not identity; the bytes on disk are"
    assert unstaged != before


def test_the_executable_bit_is_identity(repo: Path) -> None:
    mod = _load()
    before = _hash(mod, repo)
    os.chmod(repo / "run.sh", 0o755)
    assert _hash(mod, repo) != before


def test_a_symlink_hashes_its_target_string_not_the_file_it_points_at(repo: Path) -> None:
    mod = _load()
    before = _hash(mod, repo)
    # Editing the file the link points at moves the identity through a.txt
    # itself; the LINK's own entry stays the same, which the next assertion
    # isolates by retargeting the link instead.
    os.unlink(repo / "link")
    os.symlink("run.sh", repo / "link")
    assert _hash(mod, repo) != before


def test_an_untracked_file_moves_it_and_an_ignored_one_does_not(repo: Path) -> None:
    mod = _load()
    before = _hash(mod, repo)
    (repo / "new.txt").write_text("x\n")
    with_untracked = _hash(mod, repo)
    assert with_untracked != before
    (repo / ".gitignore").write_text("junk.txt\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    base = _hash(mod, repo)
    (repo / "junk.txt").write_text("y\n")
    assert _hash(mod, repo) == base


def test_the_stamp_file_is_excluded(repo: Path) -> None:
    mod = _load()
    stamp = repo / Path(mod.STAMP_FILE)
    stamp.parent.mkdir(parents=True)
    stamp.write_text("release_version=\n")
    _git(repo, "add", str(stamp.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", "stamp")
    before = _hash(mod, repo)
    stamp.write_text("release_version=0.1.125\n")
    assert _hash(mod, repo) == before
