# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-fltb4 — `nx index repo --since-head`: git-diff-driven incremental scope.

The contract: a usable delta walks ONLY the changed files, skips the
full-collection staleness pulls and every full-tree pass that would misread
a partial walk (housekeeping miss-counting above all — the mass-delete
hazard), deletes D/R-old paths' docs explicitly, and falls back to a FULL
index on any doubt (no base, unreachable base, parse surprise, mid-merge).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.indexer import _git_changed_since


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def g(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "a.py").write_text("print('a')\n")
    (repo / "b.py").write_text("print('b')\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "d.md").write_text("# doc\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


# ── _git_changed_since parsing ──────────────────────────────────────────────


def test_delta_add_modify_delete(git_repo):
    base = _head(git_repo)
    (git_repo / "a.py").write_text("print('a2')\n")     # M
    (git_repo / "new.py").write_text("print('new')\n")  # A
    (git_repo / "b.py").unlink()                        # D
    subprocess.run(["git", "-C", str(git_repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "x"], check=True)

    delta = _git_changed_since(git_repo, base)
    assert delta is not None
    changed, deleted = delta
    assert set(changed) == {"a.py", "new.py"}
    assert deleted == ["b.py"]


def test_delta_rename_is_delete_plus_add(git_repo):
    base = _head(git_repo)
    subprocess.run(["git", "-C", str(git_repo), "mv", "a.py", "renamed.py"], check=True)
    subprocess.run(["git", "-C", str(git_repo), "commit", "-q", "-m", "mv"], check=True)

    changed, deleted = _git_changed_since(git_repo, base)
    assert "renamed.py" in changed
    assert deleted == ["a.py"]


def test_delta_includes_uncommitted_worktree_edits(git_repo):
    base = _head(git_repo)
    (git_repo / "a.py").write_text("print('dirty')\n")  # NOT committed

    changed, deleted = _git_changed_since(git_repo, base)
    assert changed == ["a.py"]
    assert deleted == []


def test_delta_empty_when_nothing_changed(git_repo):
    base = _head(git_repo)
    changed, deleted = _git_changed_since(git_repo, base)
    assert changed == [] and deleted == []


def test_delta_unreachable_base_falls_back_none(git_repo):
    assert _git_changed_since(git_repo, "0" * 40) is None


def test_delta_not_a_repo_falls_back_none(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git_changed_since(plain, "abc123") is None


# ── _run_index delta wiring (seam-level) ────────────────────────────────────


class _Reg:
    def __init__(self, repo):
        self._repo = repo

    def get(self, repo):
        return {"collection": "code__t__voyage-code-3__v1",
                "code_collection": "code__t__voyage-code-3__v1",
                "docs_collection": "docs__t__voyage-context-3__v1"}


def test_since_head_no_delta_short_circuits(git_repo, monkeypatch):
    """Empty delta: _run_index returns zero-stats without touching T3 or
    walking anything (the per-commit no-op case — the whole point)."""
    from nexus import indexer as idx

    base = _head(git_repo)
    monkeypatch.setattr(idx, "_get_owner_head_hash", lambda repo: base)
    phases: list[str] = []
    # Any T3 touch would blow up loudly: no credentials patched, no client —
    # reaching the T3 connect would raise before returning stats.
    stats = idx._run_index(
        git_repo, _Reg(git_repo), since_head=True, on_phase=phases.append,
    )
    assert stats == {"rdr_indexed": 0, "rdr_current": 0, "rdr_failed": 0,
                     "files_changed": 0}
    assert any("nothing changed" in p for p in phases)


def test_since_head_without_base_logs_fallback(git_repo, monkeypatch):
    """No stored head_hash: the delta path must announce the full-index
    fallback (and then proceed as a normal full run)."""
    from nexus import indexer as idx

    monkeypatch.setattr(idx, "_get_owner_head_hash", lambda repo: None)
    phases: list[str] = []

    # Stop the run right after the fallback decision: patch the frecency
    # pass (first heavy step after the delta block) to raise a sentinel.
    class _Stop(Exception):
        pass

    monkeypatch.setattr(idx, "_git_metadata", lambda repo: {})
    monkeypatch.setattr(
        idx, "batch_frecency",
        lambda *a, **k: (_ for _ in ()).throw(_Stop()), raising=False,
    )
    try:
        idx._run_index(git_repo, _Reg(git_repo), since_head=True,
                       on_phase=phases.append)
    except Exception:  # noqa: BLE001 — sentinel or later-stage failure both fine here
        pass
    assert any("no usable base — full index" in p for p in phases)


def test_since_head_ignored_with_force(git_repo, monkeypatch):
    """--force + --since-head: full pass by definition; the delta block must
    not even consult the base."""
    from nexus import indexer as idx

    consulted: list[bool] = []
    monkeypatch.setattr(
        idx, "_get_owner_head_hash",
        lambda repo: consulted.append(True) or None,
    )

    class _Stop(Exception):
        pass

    monkeypatch.setattr(idx, "_git_metadata", lambda repo: {})
    monkeypatch.setattr(
        idx, "batch_frecency",
        lambda *a, **k: (_ for _ in ()).throw(_Stop()), raising=False,
    )
    try:
        idx._run_index(git_repo, _Reg(git_repo), since_head=True, force=True)
    except Exception:  # noqa: BLE001 — sentinel; the assertion below is the test
        pass
    assert consulted == []


# ── the mass-delete hazard pin ──────────────────────────────────────────────


def test_catalog_hook_skip_housekeeping_flag():
    """The load-bearing safety pin, BEHAVIORAL (nexus-4kh95, upgraded from a
    source-text grep): skip_housekeeping=True must NEVER reach
    _run_housekeeping — a miss-count sweep against a delta-filtered walk
    marks every unvisited doc as gone (mass delete after two runs). The
    gate lives in the extracted _maybe_run_housekeeping seam, exercised
    here by CALLING it both ways."""
    from nexus import indexer as idx

    called: list[bool] = []
    with patch.object(idx, "_run_housekeeping",
                      side_effect=lambda *a, **k: called.append(True)):
        idx._maybe_run_housekeeping(
            object(), object(), [], Path("/tmp"),
            writer=None, skip_housekeeping=True,
        )
        assert called == [], "partial-walk housekeeping must be gated OFF"

        idx._maybe_run_housekeeping(
            object(), object(), [], Path("/tmp"),
            writer=None, skip_housekeeping=False,
        )
        assert called == [True], "full-walk housekeeping must still run"

    # The hook must actually route through the tested seam — otherwise the
    # behavioral test above pins a helper nobody calls.
    import inspect

    hook_src = inspect.getsource(idx._catalog_hook)
    assert "_maybe_run_housekeeping(" in hook_src
