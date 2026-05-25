# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-128 P2: cross-process migration flock + daemon quiesce.

`nx upgrade` and the daemon's own startup migration both run `apply_pending`.
Before P2 they could race on SQLite's single WAL writer lock (the structural
contention behind the 5.0.2-5.0.4 incidents + the post-5.0.4 crash-loop).
P2 serializes them with an exclusive `fcntl.flock` on
`<config_dir>/t2_migration.lock`, taken by BOTH paths, and quiesces the
daemon (stop + wait) during `nx upgrade`'s migration so its live connections
don't contend with the migration DDL.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner


# ── flock mechanics ──────────────────────────────────────────────────────────


def test_flock_serializes_two_holders(tmp_path: Path) -> None:
    """A second acquirer of the same lock dir BLOCKS until the first
    releases — the core serialization guarantee."""
    from nexus.db.migrations import t2_migration_flock

    order: list[str] = []
    a_holding = threading.Event()
    a_may_release = threading.Event()

    def holder_a() -> None:
        with t2_migration_flock(tmp_path):
            order.append("A-enter")
            a_holding.set()
            a_may_release.wait(timeout=5)
            order.append("A-exit")

    def holder_b() -> None:
        a_holding.wait(timeout=5)  # ensure A holds the lock first
        with t2_migration_flock(tmp_path):
            order.append("B-enter")

    ta = threading.Thread(target=holder_a)
    tb = threading.Thread(target=holder_b)
    ta.start()
    tb.start()

    assert a_holding.wait(timeout=5)
    # B is now blocked on the flock while A holds it.
    time.sleep(0.3)
    assert order == ["A-enter"], "B must NOT enter while A holds the lock"

    a_may_release.set()  # let A release
    ta.join(timeout=5)
    tb.join(timeout=5)

    assert order == ["A-enter", "A-exit", "B-enter"], (
        "B must enter only after A exits"
    )


def test_flock_released_on_context_exit_is_reacquirable(tmp_path: Path) -> None:
    """The lock is freed on context exit, so a subsequent acquire succeeds
    without blocking."""
    from nexus.db.migrations import t2_migration_flock

    with t2_migration_flock(tmp_path):
        pass
    # Second acquisition must not hang (guard with a thread + timeout).
    done = threading.Event()

    def _acquire() -> None:
        with t2_migration_flock(tmp_path):
            done.set()

    t = threading.Thread(target=_acquire)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), "lock was not released on context exit"


def test_flock_not_stranded_after_holder_thread_finishes(tmp_path: Path) -> None:
    """After a holder thread completes its with-block, the lock is free."""
    from nexus.db.migrations import t2_migration_flock

    def _hold_briefly() -> None:
        with t2_migration_flock(tmp_path):
            time.sleep(0.05)

    h = threading.Thread(target=_hold_briefly)
    h.start()
    h.join(timeout=5)

    acquired = threading.Event()

    def _reacquire() -> None:
        with t2_migration_flock(tmp_path):
            acquired.set()

    r = threading.Thread(target=_reacquire)
    r.start()
    r.join(timeout=5)
    assert acquired.is_set()


# ── bootstrap_schema honors the flock ────────────────────────────────────────


def test_bootstrap_schema_waits_on_held_migration_flock(tmp_path: Path) -> None:
    """The daemon's startup migration (bootstrap_schema) takes the same
    flock — while an external holder holds it, bootstrap_schema BLOCKS, then
    completes once the lock frees."""
    from nexus.db.migrations import t2_migration_flock
    from nexus.db.t2 import T2Database

    db = tmp_path / "memory.db"
    hold_seconds = 0.4
    holding = threading.Event()
    release = threading.Event()

    def _external_holder() -> None:
        with t2_migration_flock(tmp_path):  # same dir as db
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_external_holder)
    holder.start()
    assert holding.wait(timeout=5)

    def _release_after() -> None:
        time.sleep(hold_seconds)
        release.set()

    rel = threading.Thread(target=_release_after)
    rel.start()

    start = time.monotonic()
    T2Database.bootstrap_schema(db)  # must wait for the flock, then succeed
    elapsed = time.monotonic() - start

    rel.join(timeout=5)
    holder.join(timeout=5)

    assert elapsed >= hold_seconds - 0.1, "bootstrap_schema did not wait on the flock"
    # Migration actually ran.
    check = sqlite3.connect(str(db))
    try:
        tables = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        check.close()
    assert "_nexus_version" in tables


# ── nx upgrade quiesce ordering ──────────────────────────────────────────────


def _patch_upgrade_phases(monkeypatch) -> list[str]:
    import nexus.commands.upgrade as up

    order: list[str] = []
    monkeypatch.setattr(up, "_quiesce_daemon", lambda: order.append("quiesce"))
    monkeypatch.setattr(
        up, "_run_upgrade", lambda **kw: order.append("migrate"),
    )
    monkeypatch.setattr(
        up, "_cycle_daemon_to_current", lambda: order.append("restore"),
    )
    return order


def test_upgrade_quiesces_before_migrating_and_restores_after(monkeypatch) -> None:
    from nexus.cli import main

    order = _patch_upgrade_phases(monkeypatch)
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert order == ["quiesce", "migrate", "restore"]


def test_upgrade_dry_run_does_not_touch_daemon(monkeypatch) -> None:
    from nexus.cli import main

    order = _patch_upgrade_phases(monkeypatch)
    result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert order == ["migrate"], "dry-run must not stop or restart the daemon"


def test_upgrade_restores_daemon_even_when_migration_fails(monkeypatch) -> None:
    import nexus.commands.upgrade as up
    from nexus.cli import main

    order: list[str] = []
    monkeypatch.setattr(up, "_quiesce_daemon", lambda: order.append("quiesce"))

    def _boom(**kw):  # noqa: ANN001
        order.append("migrate")
        raise RuntimeError("migration boom")

    monkeypatch.setattr(up, "_run_upgrade", _boom)
    monkeypatch.setattr(
        up, "_cycle_daemon_to_current", lambda: order.append("restore"),
    )

    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code != 0  # non-auto mode re-raises
    # finally-clause must still bring the daemon back.
    assert order == ["quiesce", "migrate", "restore"]


def test_quiesce_daemon_shells_t2_stop(monkeypatch, tmp_path: Path) -> None:
    """_quiesce_daemon issues `nx daemon t2 stop` (best-effort, never raises)."""
    import subprocess

    import nexus.commands.upgrade as up

    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0),
    )
    # No discovery file under a tmp config dir → no pid → no wait loop.
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path,
    )

    up._quiesce_daemon()

    assert len(calls) == 1
    assert calls[0][-3:] == ["daemon", "t2", "stop"]
