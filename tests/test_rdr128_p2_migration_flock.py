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


# nexus-aqbrk: PINNED, per-test. The subject is bootstrap_schema's LOCK-WAITING
# behaviour, which does not exist in service mode: the function early-returns
# (RDR-176 Gap 2 — the local .db is a frozen migration source) and therefore
# never takes the flock at all, so the test measured a ~0.0002s "wait".
#
# SERVICE HALF IS OWNED by tests/db/test_rdr176_non_mutation.py
# ::test_service_mode_bootstrap_does_not_mutate_legacy_source_db, which asserts
# that early-return leaves the legacy DB byte-identical. NOTE: that owner was
# ITSELF broken on the engine arm until earlier in this same commit — it built
# its fixture with the very function it is asserting about. Pinning against a
# red owner would have claimed coverage that did not hold, so it was fixed
# first.
@pytest.mark.usefixtures("local_t2_backend")
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


# NO nx-upgrade quiesce-ordering section: it pinned the
# quiesce -> migrate -> restore sequence around `nx upgrade`, and both ends of
# that sequence (`_quiesce_daemon`, `_cycle_daemon_to_current`) retired with the
# T2 daemon (nexus-i711w Stage 2 sub-stage B). The flock half of RDR-128 P2 —
# which is what actually serializes two MIGRATOR processes — is untouched above
# and below; only the daemon-connection-release half is gone, because there are
# no daemon connections left to release.


# ── Acceptance criterion: two real migrators don't collide ───────────────────


def test_two_real_migration_paths_serialize_no_database_locked(
    tmp_path: Path,
) -> None:
    """The literal acceptance criterion: the daemon-startup migration
    (``bootstrap_schema``) and an ``nx upgrade``-style ``apply_pending``
    (flock-wrapped) racing on the SAME memory.db both complete without a
    ``database is locked`` error.

    (Cross-process flock serialization itself is proven by
    ``test_flock_serializes_two_holders``; this exercises the two real
    migration paths integrated on one DB.)
    """
    from nexus.db.migrations import apply_pending, t2_migration_flock
    from nexus.db.t2 import T2Database

    db = tmp_path / "memory.db"
    errors: list[tuple[str, Exception]] = []
    start = threading.Barrier(2)

    def _daemon_startup_path() -> None:
        try:
            start.wait(timeout=5)
            T2Database.bootstrap_schema(db)
        except Exception as exc:  # noqa: BLE001
            errors.append(("daemon", exc))

    def _upgrade_path() -> None:
        try:
            start.wait(timeout=5)
            conn = sqlite3.connect(str(db))
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                with t2_migration_flock(tmp_path):
                    apply_pending(conn, "9.9.9")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(("upgrade", exc))

    t1 = threading.Thread(target=_daemon_startup_path)
    t2 = threading.Thread(target=_upgrade_path)
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert not errors, f"migration paths collided: {errors}"
    # Schema is present and consistent.
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
