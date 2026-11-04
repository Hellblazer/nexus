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

from nexus.commands import command_context as cc

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
    # Whole-name anchoring: a name that merely STARTS like a handoff, or carries one inside it.
    backup = _aged(tmp_path / "nexus-continuation-x.md.bak", 40, now)
    draft_md = _aged(tmp_path / "nexus-continuation-x.md.draft.txt", 40, now)
    unrelated = _aged(tmp_path / "notes.md", 40, now)
    subdir = tmp_path / "nexus-continuation-dir.md"
    subdir.mkdir()

    removed = crs.prune_old_handoffs(tmp_path, now=now)

    assert sorted(Path(p).name for p in removed) == sorted([old.name, old_plain.name])
    assert not old.exists() and not old_plain.exists()
    for kept in (fresh, draft, ask, lookalike, backup, draft_md, unrelated):
        assert kept.exists(), kept
    assert subdir.is_dir()


def test_the_seven_day_boundary_is_inclusive_of_the_keeper(crs, tmp_path) -> None:
    """Exactly seven days old is kept; a second more is removed. 6 d and 8 d alone would not pin this."""
    now = time.time()
    exact = _aged(tmp_path / "nexus-continuation-exact.md", 7, now)
    over = _aged(tmp_path / "nexus-continuation-over.md", 7, now)
    os.utime(over, (now - 7 * _DAY - 1, now - 7 * _DAY - 1))
    removed = crs.prune_old_handoffs(tmp_path, now=now)
    assert [Path(p).name for p in removed] == [over.name]
    assert exact.exists()


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


class TestTheWriteTimeWrapper:
    """`_prune_handoffs_at_write` is what `_roll` really calls: it must prune TMP, log, and never raise."""

    @pytest.fixture()
    def logged(self, crs, monkeypatch, tmp_path):
        monkeypatch.delenv("NX_RESTART_PRUNE_DIR", raising=False)
        monkeypatch.setattr(crs, "TMP", tmp_path)
        lines: list[str] = []
        monkeypatch.setattr(crs, "log", lines.append)
        return lines

    def test_it_prunes_tmp_and_logs_the_count(self, crs, logged, tmp_path) -> None:
        now = time.time()
        old = _aged(tmp_path / "nexus-continuation-old.md", 9, now)
        fresh = _aged(tmp_path / "nexus-continuation-fresh.md", 1, now)
        crs._prune_handoffs_at_write()
        assert not old.exists() and fresh.exists()
        assert any("pruned 1 handoff file(s)" in line for line in logged)

    def test_the_env_override_moves_the_prune_and_leaves_tmp_alone(self, crs, logged, tmp_path, monkeypatch) -> None:
        """The e2e harness sets it so a sandboxed roll cannot prune the operator's real /tmp."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        monkeypatch.setenv("NX_RESTART_PRUNE_DIR", str(sandbox))
        now = time.time()
        in_tmp = _aged(tmp_path / "nexus-continuation-in-tmp.md", 30, now)
        in_sandbox = _aged(sandbox / "nexus-continuation-in-sandbox.md", 30, now)
        crs._prune_handoffs_at_write()
        assert in_tmp.exists(), "TMP is untouched when the override is set"
        assert not in_sandbox.exists()

    def test_a_failing_prune_is_logged_and_never_raised(self, crs, logged, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(crs, "prune_old_handoffs", boom)
        crs._prune_handoffs_at_write()  # must not raise
        assert any("handoff prune skipped" in line and "disk on fire" in line for line in logged)

    def test_the_harness_sets_the_override_on_its_roll_command(self) -> None:
        """tests/e2e/restart-sessions/run.py runs the real script; without this its rolls prune real /tmp."""
        harness = (SCRIPT.parents[1] / "tests" / "e2e" / "restart-sessions" / "run.py").read_text(encoding="utf-8")
        assert "NX_RESTART_PRUNE_DIR=" in harness


def test_the_script_and_command_context_prune_by_the_same_rule(crs) -> None:
    """The script cannot import nexus, so it carries its own copy of the name pattern and the age. They
    must not drift from `nx command-context continuation`'s."""
    assert crs._HANDOFF_NAME_RE.pattern == cc._HANDOFF_NAME_RE.pattern
    assert crs._HANDOFF_NAME_RE.flags == cc._HANDOFF_NAME_RE.flags
    assert crs.HANDOFF_MAX_AGE_S == cc._HANDOFF_MAX_AGE_S == 7 * _DAY
