# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1610: the guard on the complete walk's same-run retirement of files gone from the main checkout.

A document is retired only when its file is positively gone: this run walks the main checkout, which is the owner's
recorded root; the path is not in the walk's own set; git does not track it; and nothing exists at it. Every other case,
and any failure to tell, keeps the engine's two-walk rule.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from nexus import indexer


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "kept.md").write_text("kept\n")
    (r / "tracked_but_deleted.md").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    (r / "tracked_but_deleted.md").unlink()  # deleted on disk, not committed: git still tracks it
    return r


class _Cat:
    def __init__(self, root: str, paths: list[str]) -> None:
        self.root, self.paths = root, paths

    def owners_with_roots(self) -> dict[str, str]:
        return {"1.22": self.root}

    def by_owner(self, owner: Any) -> list[Any]:
        return [type("E", (), {"file_path": p})() for p in self.paths]


_PATHS = ["kept.md", "docs/gone.md", "tracked_but_deleted.md", "/abs/elsewhere.md", "", "docs/seen.md", "docs/link.md",
          "bare-name.md", "chroma://code__x/y.py"]


def test_only_a_positively_gone_untracked_path_is_retired(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "link.md").symlink_to(repo / "nowhere")  # a dangling symlink is still something at the path
    gone = indexer._gone_from_main_checkout(_Cat(str(repo), _PATHS), "1.22", repo, repo, {"docs/seen.md"})
    assert gone == ["docs/gone.md"], "a basename-only or URI path is not judged, as prune-stale does not judge it"


def test_a_worktree_run_retires_nothing(repo: Path, tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert indexer._gone_from_main_checkout(_Cat(str(repo), _PATHS), "1.22", worktree, repo, set()) == []


def test_a_root_that_is_not_this_checkout_retires_nothing(repo: Path, tmp_path: Path) -> None:
    moved = tmp_path / "old-clone"
    assert indexer._gone_from_main_checkout(_Cat(str(moved), _PATHS), "1.22", repo, repo, set()) == []
    assert indexer._gone_from_main_checkout(_Cat("", _PATHS), "1.22", repo, repo, set()) == []


def test_a_failed_git_listing_retires_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = subprocess.run

    def failing(argv, *a, **k):  # noqa: ANN001
        if "ls-files" in argv:
            return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")
        return real(argv, *a, **k)

    monkeypatch.setattr(indexer.subprocess, "run", failing)
    assert indexer._gone_from_main_checkout(_Cat(str(repo), _PATHS), "1.22", repo, repo, set()) == []


def test_an_unreadable_catalog_retires_nothing(repo: Path) -> None:
    class _Broken(_Cat):
        def by_owner(self, owner: Any) -> list[Any]:
            raise RuntimeError("engine gone")

    assert indexer._gone_from_main_checkout(_Broken(str(repo), _PATHS), "1.22", repo, repo, set()) == []
