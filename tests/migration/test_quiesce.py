# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P1c.T (nexus-ue6g7.9) — cross-process indexing quiesce (S2) + the
write-lock pre-gate audit (RF-6).

Three locked properties (RDR-159 §"Indexing quiesce (S2)" + RF-6):

(a) **Suspend while migrating.** The aspect worker checks the ``migration.state``
    phase at the top of each work cycle and SUSPENDS while ``migrating`` — a
    cross-process suspend the process-local ``drain_worker`` cannot provide
    (it stops only the calling process's worker). ``nx index`` is blocked the
    same way at its command entry.
(b) **Pre-gate write-lock audit.** Before T3 starts, the migration verifies no
    live FOREIGN aspect-worker write-locks remain across processes; if any
    persist it BLOCKS with the offending pids rather than running into the
    RF-6 false-failure count mismatch. Dead locks are swept; the own pid is
    not a conflict.
(c) **Attributed mismatch.** A count mismatch under a concurrent write is
    ATTRIBUTED loudly (which collection, expected vs actual, the foreign pids),
    never a silent rollback.

The audit mirrors the existing ``aspect_worker._check_mcp_worker_lock`` SIG-5
primitive: PID 1 (init/launchd) is the always-alive foreign stand-in; a pid far
above the kernel ceiling is the guaranteed-dead stale lock.

Isolation: ``NEXUS_CONFIG_DIR`` → ``tmp_path``; the worker test patches the T2
claim RPC so no real daemon/queue is touched.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_worker import live_foreign_worker_pids
from nexus.migration.quiesce import (
    MigrationQuiesceBlocked,
    assert_quiescent_for_migration,
    explain_count_mismatch,
)
from nexus.migration.state import begin_migration, clear_state

_FIXED_STARTED_AT = "2026-06-13T00:00:00+00:00"
_LIVE_FOREIGN_PID = 1  # init / launchd — always alive, always another process
_DEAD_PID = 99999999  # above the kernel PID ceiling — guaranteed absent


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _write_lock(locks_dir: Path, pid: int) -> Path:
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock = locks_dir / f"aspect_worker.{pid}"
    lock.write_text(str(pid))
    return lock


# --------------------------------------------------------------------------
# (b) live_foreign_worker_pids — the shared scan
# --------------------------------------------------------------------------


def test_live_foreign_pids_reports_live_foreign(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    _write_lock(locks, _LIVE_FOREIGN_PID)
    assert live_foreign_worker_pids(locks) == [_LIVE_FOREIGN_PID]


def test_live_foreign_pids_skips_own_pid(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    _write_lock(locks, os.getpid())
    assert live_foreign_worker_pids(locks) == []


def test_live_foreign_pids_sweeps_dead_lock(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    dead = _write_lock(locks, _DEAD_PID)
    assert live_foreign_worker_pids(locks) == []
    assert not dead.exists()  # stale lock swept


def test_live_foreign_pids_empty_when_no_dir(tmp_path: Path) -> None:
    assert live_foreign_worker_pids(tmp_path / "does-not-exist") == []


# --------------------------------------------------------------------------
# (b) assert_quiescent_for_migration — the pre-gate
# --------------------------------------------------------------------------


def test_pregate_blocks_on_live_foreign_worker(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    _write_lock(locks, _LIVE_FOREIGN_PID)
    with pytest.raises(MigrationQuiesceBlocked) as exc:
        assert_quiescent_for_migration(locks_dir=locks)
    assert exc.value.pids == [_LIVE_FOREIGN_PID]
    assert str(_LIVE_FOREIGN_PID) in str(exc.value)


def test_pregate_passes_when_only_dead_locks(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    _write_lock(locks, _DEAD_PID)
    assert_quiescent_for_migration(locks_dir=locks)  # must not raise


def test_pregate_passes_when_only_own_lock(tmp_path: Path) -> None:
    locks = tmp_path / "locks"
    _write_lock(locks, os.getpid())
    assert_quiescent_for_migration(locks_dir=locks)  # must not raise


def test_pregate_passes_when_no_locks_dir(tmp_path: Path) -> None:
    assert_quiescent_for_migration(locks_dir=tmp_path / "nope")  # must not raise


# --------------------------------------------------------------------------
# (c) explain_count_mismatch — loud attribution, never a silent rollback
# --------------------------------------------------------------------------


def test_count_mismatch_is_attributed_loud() -> None:
    msg = explain_count_mismatch(
        collection="knowledge__art__voyage-context-3__v1",
        expected=1200,
        actual=1187,
        foreign_pids=[4321, 8765],
    )
    assert "knowledge__art__voyage-context-3__v1" in msg
    assert "1200" in msg
    assert "1187" in msg
    assert "4321" in msg and "8765" in msg
    assert "attributed" in msg.lower()


def test_count_mismatch_without_pids_still_loud() -> None:
    msg = explain_count_mismatch(
        collection="code__nexus__voyage-code-3__v1",
        expected=50,
        actual=50,
        foreign_pids=[],
    )
    assert "code__nexus__voyage-code-3__v1" in msg
    assert "attributed" in msg.lower()


# --------------------------------------------------------------------------
# (a) aspect worker suspends while migrating (cross-cycle)
# --------------------------------------------------------------------------


def test_aspect_worker_suspends_while_migrating_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_worker import AspectExtractionWorker

    claims: list[int] = []

    def _fake_t2_index_write(fn):  # type: ignore[no-untyped-def]
        claims.append(1)
        return []  # empty queue — no row processing

    # The worker imports t2_index_write from nexus.mcp_infra inside _run_loop.
    monkeypatch.setattr("nexus.mcp_infra.t2_index_write", _fake_t2_index_write)

    # Enter the migrating phase BEFORE the worker starts.
    begin_migration(collections_total=3, started_at=_FIXED_STARTED_AT)

    worker = AspectExtractionWorker(poll_interval=0.02)
    worker.start()
    try:
        # Several poll intervals elapse — a suspended worker never claims.
        time.sleep(0.3)
        assert claims == [], "worker claimed while migration.state==migrating"

        # UNLOCK → the worker resumes claiming on its next cycle.
        clear_state()
        deadline = time.time() + 5.0
        while not claims and time.time() < deadline:
            time.sleep(0.02)
        assert claims, "worker did not resume claiming after unlock"
    finally:
        worker.stop(timeout=5.0)


# --------------------------------------------------------------------------
# (a) nx index is suspended at command entry while migrating
# --------------------------------------------------------------------------


def test_nx_index_blocked_while_migrating(tmp_path: Path) -> None:
    from nexus.commands.index import index

    runner = CliRunner()
    target = tmp_path / "repo"
    target.mkdir()

    # not-migrating: the migration guard does not fire (the command may still
    # fail for unrelated reasons — we only assert the guard message is absent).
    baseline = runner.invoke(index, ["repo", str(target)], catch_exceptions=True)
    assert "migrating" not in (baseline.output + baseline.stderr).lower()

    begin_migration(collections_total=4, started_at=_FIXED_STARTED_AT)
    blocked = runner.invoke(index, ["repo", str(target)], catch_exceptions=True)
    assert blocked.exit_code != 0
    combined = (blocked.output + blocked.stderr).lower()
    assert "migrat" in combined  # LOUD: index suspended during migration
