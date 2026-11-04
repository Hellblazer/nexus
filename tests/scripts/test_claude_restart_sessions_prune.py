# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/claude-restart-sessions: old handoffs are pruned when a roll is about to write new ones (nexus-2v7da).

59 ``/tmp/nexus-continuation-*.md`` files sat unpruned. The prune takes only regular files directly in the
directory whose whole name is ``nexus-continuation-<...>.md`` and whose mtime is over 7 days old.
"""
from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import os
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "claude-restart-sessions"
_DAY = 24 * 3600.0


@pytest.fixture(scope="module")
def crs():
    loader = importlib.machinery.SourceFileLoader("claude_restart_sessions_prune", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _aged(path: Path, days: float, now: float) -> Path:
    path.write_text("x", encoding="utf-8")
    os.utime(path, (now - days * _DAY, now - days * _DAY))
    return path


def test_prune_removes_only_old_files_with_the_exact_handoff_name(crs, tmp_path) -> None:
    now = time.time()
    old = _aged(tmp_path / "nexus-continuation-restart-linda-1a2b3c4d-20260101-101010.md", 8, now)
    old_plain = _aged(tmp_path / "nexus-continuation-nexus-main-2026-01-01.md", 30, now)
    fresh = _aged(tmp_path / "nexus-continuation-restart-linda-1a2b3c4d-20261101-101010.md", 6, now)
    draft = _aged(tmp_path / "nexus-continuation-restart-linda-1a2b3c4d-20260101-101010.draft.txt", 40, now)
    ask = _aged(tmp_path / "nexus-restart-ready-linda-1a2b3c4d-20260101-101010.json", 40, now)
    lookalike = _aged(tmp_path / "xnexus-continuation-a.md", 40, now)
    unrelated = _aged(tmp_path / "notes.md", 40, now)
    subdir = tmp_path / "nexus-continuation-dir.md"
    subdir.mkdir()

    removed = crs.prune_old_handoffs(tmp_path, now=now)

    assert sorted(Path(p).name for p in removed) == sorted([old.name, old_plain.name])
    assert not old.exists() and not old_plain.exists()
    for kept in (fresh, draft, ask, lookalike, unrelated):
        assert kept.exists(), kept
    assert subdir.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_prune_never_removes_a_symlink_or_its_target(crs, tmp_path) -> None:
    now = time.time()
    target = _aged(tmp_path / "precious.txt", 400, now)
    link = tmp_path / "nexus-continuation-link-2026-01-01.md"
    link.symlink_to(target)
    os.utime(link, (now - 40 * _DAY, now - 40 * _DAY), follow_symlinks=False)
    crs.prune_old_handoffs(tmp_path, now=now)
    assert link.is_symlink() and target.exists()


def test_prune_survives_a_missing_directory(crs, tmp_path) -> None:
    assert crs.prune_old_handoffs(tmp_path / "absent", now=time.time()) == []


def test_a_roll_that_writes_handoffs_prunes_first(crs, monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(crs, "_prune_handoffs_at_write", lambda: calls.append("prune"))
    monkeypatch.setattr(crs, "write_handoffs", lambda *_a, **_k: calls.append("write") or {})
    monkeypatch.setattr(crs, "converge", lambda *_a, **_k: True)
    a = argparse.Namespace(fresh=True, wait=1, handoff_timeout=1, nx=None, fallback_resume=True,
                           repo=str(tmp_path), endpoint="")
    try:
        crs._roll(a, [], [], [], {"idle"}, None)
    except Exception:  # noqa: BLE001 -- only the order of the first two calls is under test
        pass
    assert calls[:2] == ["prune", "write"]
