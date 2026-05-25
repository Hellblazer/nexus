# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-128 P0a (RF-3): startup-migration lock tolerance.

The T2 daemon's startup migration (``T2Database.bootstrap_schema`` ->
``apply_pending``) must tolerate another process (typically ``nx index
repo``) holding memory.db's single WAL writer lock. Before this fix the
migration connection used a 5s ``busy_timeout`` with no retry, so a
concurrent indexer could push a migration step past the limit and crash
the freshly-spawned daemon on ``database is locked`` — and because
``ensure-running`` is one-shot, the daemon was then left down (the
post-5.0.4 crash-loop, RF-3 x RF-4).

Two layers of tolerance, both tested here:

* a >= 30s ``busy_timeout`` so each statement waits out the realistic
  intra-host contention window (mirrors aspect_extraction_queue,
  nexus-v4m7y);
* a bounded Python-level retry around ``apply_pending`` so a migration
  that still trips ``database is locked`` is re-attempted (idempotent)
  rather than crashing.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from nexus.db.t2 import (
    _BOOTSTRAP_BUSY_TIMEOUT_MS,
    T2Database,
    _apply_pending_with_lock_retry,
)


def test_busy_timeout_constant_is_at_least_30s() -> None:
    """RDR-128 P0a spec: busy_timeout >= 30000 ms."""
    assert _BOOTSTRAP_BUSY_TIMEOUT_MS >= 30000


def test_retry_recovers_after_one_transient_lock() -> None:
    """A single ``database is locked`` is absorbed; the second attempt
    succeeds and ``apply_pending`` is not retried further."""
    calls = {"n": 0}

    def _fake_apply_pending(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")

    # Patch the symbol the helper imports lazily.
    import nexus.db.migrations as _mig

    orig = _mig.apply_pending
    _mig.apply_pending = _fake_apply_pending  # type: ignore[assignment]
    try:
        # No real sleep delay needed for correctness, but the helper does
        # sleep between attempts; keep it tiny by patching the sleep table.
        import nexus.db.t2 as _t2

        orig_sleeps = _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN
        _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = (0.0, 0.0)  # type: ignore[attr-defined]
        try:
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
        finally:
            _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = orig_sleeps  # type: ignore[attr-defined]
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]

    assert calls["n"] == 2, "expected exactly one retry then success"


def test_retry_exhausts_and_reraises_on_persistent_lock() -> None:
    """A lock that never clears re-raises after the bounded attempts —
    the helper must NOT hang indefinitely."""
    calls = {"n": 0}

    def _always_locked(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    import nexus.db.migrations as _mig
    import nexus.db.t2 as _t2

    orig = _mig.apply_pending
    orig_sleeps = _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN
    _mig.apply_pending = _always_locked  # type: ignore[assignment]
    _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = (0.0, 0.0)  # type: ignore[attr-defined]
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]
        _t2._BOOTSTRAP_RETRY_SLEEPS_BETWEEN = orig_sleeps  # type: ignore[attr-defined]

    assert calls["n"] == len(orig_sleeps) + 1, "must try exactly max_attempts times"


def test_non_lock_operational_error_propagates_immediately() -> None:
    """A non-lock OperationalError (e.g. schema corruption) is not a
    contention signal — propagate on the first attempt, no retry."""
    calls = {"n": 0}

    def _schema_error(conn, current_version):  # noqa: ANN001
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: bogus")

    import nexus.db.migrations as _mig

    orig = _mig.apply_pending
    _mig.apply_pending = _schema_error  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            _apply_pending_with_lock_retry(sqlite3.connect(":memory:"), "9.9.9")
    finally:
        _mig.apply_pending = orig  # type: ignore[assignment]

    assert calls["n"] == 1, "non-lock errors must not be retried"


def test_bootstrap_schema_waits_for_held_writer_lock_then_succeeds(
    tmp_path: Path,
) -> None:
    """Integration: a competing connection holds memory.db's writer lock
    when bootstrap_schema starts; bootstrap_schema must WAIT for the lock
    to free and then complete the migration, not crash."""
    db = tmp_path / "memory.db"

    # Seed the file in WAL mode (closed before threading — a sqlite3
    # connection can only be used from its creating thread).
    seed = sqlite3.connect(str(db))
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE _seed (x INTEGER)")
    seed.commit()
    seed.close()

    hold_seconds = 0.4
    locked = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        # Own connection, own thread: acquire the single WAL writer lock,
        # signal that it's held, wait for the release cue, then free it.
        h = sqlite3.connect(str(db))
        h.execute("PRAGMA busy_timeout=30000")
        h.execute("BEGIN IMMEDIATE")
        h.execute("INSERT INTO _seed VALUES (1)")
        locked.set()
        release.wait(timeout=10)
        h.rollback()
        h.close()

    holder = threading.Thread(target=_holder)
    holder.start()
    assert locked.wait(timeout=5), "holder thread failed to acquire writer lock"

    def _release_after() -> None:
        time.sleep(hold_seconds)
        release.set()

    releaser = threading.Thread(target=_release_after)
    releaser.start()

    start = time.monotonic()
    T2Database.bootstrap_schema(db)  # must wait ~hold_seconds, then succeed
    elapsed = time.monotonic() - start

    releaser.join()
    holder.join()

    # It genuinely waited for the lock rather than failing fast.
    assert elapsed >= hold_seconds - 0.1
    # And the migration actually ran: a real T2 table now exists.
    check = sqlite3.connect(str(db))
    try:
        tables = {
            r[0]
            for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        check.close()
    assert "_nexus_version" in tables, f"migration did not run; tables={tables}"
