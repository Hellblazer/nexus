# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wehp: SQLite writer-lock contention between concurrent Catalog processes.

Original failure mode: end users running CLI write operations (most
reproducibly ``nx catalog backfill-collections --no-dry-run``) while
``nx-mcp`` held a Catalog connection hit::

    sqlite3.OperationalError: database is locked
      File "nexus/catalog/projector.py", line 340, in _v0_collection_created

Two contributing factors:

1. **Marker not persisted across processes.** Each fresh ``Catalog()``
   reset ``_last_consistency_mtime`` to ``0.0`` and re-triggered the
   full DELETE+replay rebuild. (Fixed in conexus 4.23.1 / PR #505.)
2. **Full rebuild dominated the writer-hold window.** Even with the
   marker, any canonical-truth file mtime advance forced a
   ``DELETE FROM`` + replay-from-zero of the entire event log inside
   one transaction. On a 450K-event catalog this took ~4 s and
   occasionally bumped against the 5 s ``busy_timeout``, producing
   intermittent ``OperationalError`` for any concurrent writer.
   (Fixed in RDR-104: incremental delta replay drops the steady-state
   rebuild to <100 ms.)

These tests cover the residual surface after both fixes:

* :func:`test_writer_lock_contention_is_observable` proves the test
  harness can detect the failure mode by directly holding the writer
  slot longer than ``busy_timeout``.
* :func:`test_concurrent_register_collection_no_contention` is the
  nexus-wehp regression guard: two processes concurrently registering
  collections through the real Catalog API must succeed without
  ``database is locked``.
"""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


# Fork is required so the workers inherit pytest's event_sourced env.
# spawn would re-import everything fresh; fork keeps the parent's flags.
_CTX = mp.get_context("fork")


def _bootstrap_catalog(catalog_dir: Path, n_events: int) -> None:
    """Seed a catalog with ``n_events`` worth of writes and persist the marker.

    Uses :meth:`Catalog.register_owner` + :meth:`Catalog.register` to
    drive real event-sourced writes through the projector. Closes the
    DB before returning so the parent process holds no connection when
    the worker children open theirs.
    """
    cat = Catalog(catalog_dir, catalog_dir / "catalog.db")
    owner = cat.register_owner(
        name="seed-owner",
        owner_type="repo",
        repo_hash="seed-hash",
    )
    for i in range(n_events):
        cat.register(
            owner=owner,
            title=f"seed-doc-{i}",
            content_type="prose",
            file_path=f"seed-{i}.md",
        )
    cat._db.close()


def _hold_writer_for(db_path: Path, hold_seconds: float, ready: mp.Event) -> None:
    """Open a raw SQLite connection, BEGIN IMMEDIATE, sleep, then commit.

    Simulates the pre-fix MCP-side hold: a long-running write
    transaction occupying the WAL writer slot. The ``ready`` event
    is set once the BEGIN IMMEDIATE has acquired the slot so the
    parent test can release the contender.
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # busy_timeout on this connection only matters if the slot is
        # already held by another writer; here we are the writer.
        conn.execute("BEGIN IMMEDIATE")
        # Touch a row to make the transaction non-empty; no-op writes
        # under ``BEGIN IMMEDIATE`` still hold the writer slot but
        # making it visibly write-y helps if the test ever needs
        # debugging.
        conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
            ("nexus_wehp_test_lock_hold", "1"),
        )
        ready.set()
        time.sleep(hold_seconds)
        conn.rollback()
    finally:
        conn.close()


def _register_one_collection(
    catalog_dir: Path,
    coll_name: str,
    result_q: mp.Queue,
) -> None:
    """Open Catalog, call register_collection, push outcome to ``result_q``.

    Runs in a child process. Outcome is one of:
    ``("ok", elapsed_seconds)`` or ``("err", repr(exc))``.
    """
    started = time.perf_counter()
    try:
        cat = Catalog(catalog_dir, catalog_dir / "catalog.db")
        cat.register_collection(
            name=coll_name,
            content_type="prose",
            owner_id="test-owner",
            embedding_model="voyage-context-3",
            model_version="v1",
        )
        cat._db.close()
        elapsed = time.perf_counter() - started
        result_q.put(("ok", elapsed))
    except Exception as exc:
        result_q.put(("err", repr(exc)))


@pytest.fixture
def seeded_catalog_dir(tmp_path: Path) -> Path:
    """Catalog directory with a small but non-trivial bootstrap log."""
    _bootstrap_catalog(tmp_path, n_events=20)
    return tmp_path


@pytest.mark.slow
def test_writer_lock_contention_is_observable(seeded_catalog_dir: Path) -> None:
    """Mechanism validation: the test harness can detect lock contention.

    A child process holds the SQLite writer slot for longer than
    ``busy_timeout`` (5 s). A second child opens a real Catalog and
    attempts a write. The contender should fail with an
    ``OperationalError: database is locked`` — confirming our
    multiprocessing harness reproduces the failure shape that
    nexus-wehp described.

    Marked ``slow`` because the holder must outlive both
    ``_ensure_consistent``'s busy_timeout *and* a subsequent
    ``register_collection`` busy_timeout (~10 s minimum hold,
    15 s with safety margin).
    """
    db_path = seeded_catalog_dir / "catalog.db"
    ready = _CTX.Event()
    result_q: mp.Queue = _CTX.Queue()

    # Hold the writer slot for 15 s. ``Catalog.__init__`` runs
    # ``_ensure_consistent`` first; on this seeded catalog that
    # triggers a bootstrap rebuild whose ``OperationalError`` is
    # caught (``self.degraded = True``) after ~5 s of busy_timeout.
    # ``register_collection`` then acquires the dir flock and calls
    # ``projector.apply``, which executes ``INSERT INTO collections``
    # on the same connection — that INSERT has no try/except wrapper
    # and will propagate the OperationalError that nexus-wehp's stack
    # trace recorded. We need the holder to still own the slot when
    # the second INSERT busy_timeout exhausts: 5 s rebuild wait + 5 s
    # INSERT wait = 10 s minimum hold, plus margin.
    holder = _CTX.Process(
        target=_hold_writer_for,
        args=(db_path, 15.0, ready),
    )
    holder.start()

    try:
        # Wait for the holder to actually own the writer slot before
        # spawning the contender, so we don't race on BEGIN IMMEDIATE.
        assert ready.wait(timeout=5.0), "writer-holder failed to acquire slot"

        contender = _CTX.Process(
            target=_register_one_collection,
            args=(seeded_catalog_dir, "test-collection-during-hold", result_q),
        )
        contender.start()
        contender.join(timeout=20.0)
        assert not contender.is_alive(), "contender hung past timeout"

        outcome, payload = result_q.get(timeout=5.0)
        assert outcome == "err", (
            "expected OperationalError under writer-slot starvation, "
            f"got success: {payload}"
        )
        assert "database is locked" in payload or "OperationalError" in payload, (
            f"expected lock-contention error, got: {payload}"
        )
    finally:
        holder.join(timeout=15.0)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5.0)


def test_concurrent_register_collection_no_contention(
    seeded_catalog_dir: Path,
) -> None:
    """nexus-wehp regression: two concurrent registers must succeed.

    With the consistency-marker fix (PR #505) and the RDR-104
    incremental rebuild path, two processes opening Catalog and
    registering collections concurrently should both succeed:

    * Process A's ``_ensure_consistent`` either takes the empty-delta
      fast path (mtime unchanged) or the incremental path (<100 ms).
    * Process B's ``_ensure_consistent`` does the same.
    * Neither holds the writer slot long enough for the other to time
      out at ``busy_timeout=5000``.

    Pre-fix this would intermittently fail under load because both
    rebuilds raced and one exceeded the 5 s budget.
    """
    result_q: mp.Queue = _CTX.Queue()

    # Two children, each opens its own Catalog and registers a
    # different collection name. They should serialize on the catalog
    # directory flock (for the event-log append) and on the SQLite
    # writer slot (for the projector INSERT) without exceeding
    # busy_timeout on either.
    workers = [
        _CTX.Process(
            target=_register_one_collection,
            args=(seeded_catalog_dir, f"concurrent-coll-{i}", result_q),
        )
        for i in range(2)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30.0)
        assert not w.is_alive(), "worker hung past timeout"

    outcomes = [result_q.get(timeout=5.0) for _ in workers]
    errors = [(tag, payload) for tag, payload in outcomes if tag != "ok"]
    assert not errors, (
        f"expected both workers to succeed under post-fix conditions, "
        f"got errors: {errors}"
    )

    # Verify both collections actually landed in the projection.
    cat = Catalog(seeded_catalog_dir, seeded_catalog_dir / "catalog.db")
    try:
        names = {c["name"] for c in cat.list_collections()}
        assert "concurrent-coll-0" in names
        assert "concurrent-coll-1" in names
    finally:
        cat._db.close()
