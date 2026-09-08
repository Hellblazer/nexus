# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-fjwk7: session close reaps the config directory's mint-lock litter.

``t1_mint_<session>.lock`` files are the flock inode of a T1 mint and were
never reaped; hundreds accumulated on one box. ``nexus.garbage`` already
knew how to sweep them (``nx doctor --fix``); the session-end launcher now
runs that sweep as its own failure-isolated step.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from nexus import _session_end_launcher as launcher
from nexus.garbage import MINT_LOCK_MAX_AGE_DAYS


def test_session_end_sweeps_stale_mint_locks(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    stale = cfg / "t1_mint_deadbeef.lock"
    stale.touch()
    old = time.time() - (MINT_LOCK_MAX_AGE_DAYS + 1) * 86400
    os.utime(stale, (old, old))
    fresh = cfg / "t1_mint_cafebabe.lock"
    fresh.touch()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    launcher._sweep_local_garbage()
    assert not stale.exists(), "a day-old lock with no lease is reaped"
    assert fresh.exists(), "a fresh lock is left alone"


def test_sweep_failure_never_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("store unavailable")
    monkeypatch.setattr("nexus.garbage.sweep_local_garbage", boom)
    launcher._sweep_local_garbage()  # must not raise
