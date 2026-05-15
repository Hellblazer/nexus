# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T2 daemon EventStream RPC — RDR-112 P1.3 (nexus-m4gm).

Test matrix (all 6 from the bead acceptance criteria):
  (a) Backfill correctness: out() N tuples, subscribe cursor=0, assert N events.
  (b) Live streaming: subscribe, out() in another thread, assert events arrive.
  (c) Cursor restart: receive 5 events, disconnect, reconnect cursor=5, no dups.
  (d) Failure-category demux: nack with category='timeout', filter by category.
  (e) Connection close cleanup: 10 open+close cycles, no leaked handlers.
  (f) Backfill cap: out() 1500 tuples, cursor=0, first burst 1000 then 500 more.

Helpers
-------
All tests use:
  - ``tmp_path`` for tuples.db and config_dir isolation.
  - ``port=0`` / UDS for transport.
  - ``threading.Event`` for cross-thread synchronisation in live-streaming test.
  - Direct sqlite3 inserts to seed tuples (bypassing the API layer) for speed.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from nexus.daemon.t2_client import T2Client
from nexus.daemon.t2_daemon import T2Daemon
from nexus.tuplespace.store import apply_tuples_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_tuples_db(path: Path) -> sqlite3.Connection:
    """Open (or create) a tuples.db at *path* with schema applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    apply_tuples_schema(conn)
    return conn


def _insert_tuple(
    conn: sqlite3.Connection,
    *,
    tuple_id: str,
    subspace: str = "tuples/test",
    content: str = "hello",
    created_at: float | None = None,
) -> None:
    """Insert a tuple row directly (triggers trg_tuples_out -> events)."""
    if created_at is None:
        created_at = time.time()
    conn.execute(
        """\
        INSERT INTO tuples
            (id, subspace, template_name, content, dimensions_json, embed_text,
             created_at)
        VALUES (?, ?, 'test', ?, '{}', ?, ?)
        """,
        (tuple_id, subspace, content, content, created_at),
    )
    conn.commit()


def _insert_nack(
    conn: sqlite3.Connection,
    *,
    tuple_id: str,
    subspace: str = "tuples/test",
    failure_category: str = "timeout",
) -> None:
    """Insert a nack row into tuple_claim_log (triggers trg_claim_log_event)."""
    conn.execute(
        """\
        INSERT INTO tuple_claim_log
            (tuple_id, subspace, claim_id, claimant, transition, failure_category, at)
        VALUES (?, ?, 'clm1', 'agent1', 'nack', ?, ?)
        """,
        (tuple_id, subspace, failure_category, time.time()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def tuples_db_path(tmp_path: Path) -> Path:
    return tmp_path / "tuples.db"


@pytest.fixture()
def seeded_db(tuples_db_path: Path) -> sqlite3.Connection:
    """Open tuples.db with schema applied; caller closes."""
    conn = _open_tuples_db(tuples_db_path)
    yield conn
    conn.close()


@pytest_asyncio.fixture()
async def daemon(config_dir: Path, tuples_db_path: Path):
    """T2Daemon with tuples_db_path configured, started, yielded, stopped."""
    d = T2Daemon(config_dir=config_dir, tuples_db_path=tuples_db_path)
    await d.start()
    yield d
    await d.stop()


def _make_client(daemon: T2Daemon) -> T2Client:
    return T2Client(uds_path=daemon.uds_path)


async def _collect_n(
    client: T2Client,
    subspace_prefix: str,
    n: int,
    *,
    since_cursor: int = 0,
    where: dict | None = None,
    timeout: float = 5.0,
) -> list[dict]:
    """Collect exactly *n* events from event_stream then close the generator.

    Runs the blocking generator in a thread pool executor so the asyncio
    event loop remains free to serve the daemon during collection.
    Raises ``TimeoutError`` if *n* events do not arrive within *timeout* seconds.
    """
    def _reader() -> list[dict]:
        collected: list[dict] = []
        gen = client.event_stream(subspace_prefix, since_cursor=since_cursor, where=where)
        for event in gen:
            collected.append(event)
            if len(collected) >= n:
                gen.close()
                break
        return collected

    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _reader),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(
            f"_collect_n: did not receive {n} events within {timeout}s"
        ) from exc


# ---------------------------------------------------------------------------
# (a) Backfill correctness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_correctness(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Insert 10 tuples before subscribing; backfill must deliver all 10."""
    n = 10
    for i in range(n):
        _insert_tuple(seeded_db, tuple_id=f"t{i}", subspace="tuples/backfill")

    client = _make_client(daemon)
    events = await _collect_n(client, "tuples/backfill", n, since_cursor=0)
    client.close()

    assert len(events) == n
    # All should be 'out' ops (inserts into tuples)
    assert all(e["op"] == "out" for e in events)
    # Cursors must be monotonically increasing
    cursors = [e["cursor"] for e in events]
    assert cursors == sorted(cursors)
    assert cursors == list(range(cursors[0], cursors[0] + n))


# ---------------------------------------------------------------------------
# (b) Live streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_streaming(
    daemon: T2Daemon,
    tuples_db_path: Path,
) -> None:
    """Subscribe, then insert from a background thread; events must arrive."""
    # Open the DB separately (the daemon does NOT hold a write connection
    # in this test — we drive inserts from the test thread)
    conn = _open_tuples_db(tuples_db_path)
    try:
        ready = threading.Event()
        collected: list[dict[str, Any]] = []

        client_holder: list[T2Client] = []

        def _stream_reader() -> None:
            client = _make_client(daemon)
            client_holder.append(client)
            try:
                for event in client.event_stream("tuples/live", since_cursor=0):
                    collected.append(event)
                    ready.set()
                    break  # one event is enough; gen.close() via context exits socket
            finally:
                client.close()

        t = threading.Thread(target=_stream_reader, daemon=True)
        t.start()

        # Give subscriber time to connect
        await asyncio.sleep(0.05)

        # Insert a tuple from a thread (simulates out())
        _insert_tuple(conn, tuple_id="live1", subspace="tuples/live")

        # Wait for the event to arrive (max 1 second)
        arrived = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ready.wait(timeout=2.0)
        )
        assert arrived, "live event did not arrive within 2 seconds"
        # Join in executor so we don't block the event loop.
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: t.join(timeout=3.0)),
            timeout=4.0,
        )

        assert len(collected) >= 1
        assert collected[0]["tuple_id"] == "live1"
        assert collected[0]["op"] == "out"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (c) Cursor restart — no duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_restart_no_duplicates(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Receive 5 events, disconnect, reconnect with cursor=last; assert no dups."""
    # Insert 10 tuples
    for i in range(10):
        _insert_tuple(seeded_db, tuple_id=f"cr{i}", subspace="tuples/restart")

    client = _make_client(daemon)
    # Run in a thread to avoid blocking the asyncio event loop.
    first_five = await _collect_n(client, "tuples/restart", 5, since_cursor=0)
    client.close()

    last_cursor = first_five[-1]["cursor"]

    # Reconnect with since_cursor = last seen; collect the remaining 5.
    client2 = _make_client(daemon)
    remaining = await _collect_n(client2, "tuples/restart", 5, since_cursor=last_cursor)
    client2.close()

    assert len(remaining) == 5  # 10 total - 5 already seen
    # No duplicates: all remaining cursors > last_cursor
    assert all(e["cursor"] > last_cursor for e in remaining)
    # Cursors monotonically increasing
    cursors = [e["cursor"] for e in remaining]
    assert cursors == sorted(cursors)


# ---------------------------------------------------------------------------
# (d) Failure-category demux
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_category_demux(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Nack with category='timeout'; subscribe with where={category:'timeout'}."""
    # Insert a tuple first (trg_tuples_out fires an 'out' event, category='data')
    _insert_tuple(seeded_db, tuple_id="demux1", subspace="tuples/demux")
    # Now insert a nack with category='timeout'
    _insert_nack(seeded_db, tuple_id="demux1", subspace="tuples/demux", failure_category="timeout")
    # Insert another nack with category='disk_error'
    _insert_nack(seeded_db, tuple_id="demux1", subspace="tuples/demux", failure_category="disk_error")

    client = _make_client(daemon)
    # Subscribe with category filter = 'timeout'; collect the one matching event.
    events = await _collect_n(
        client,
        "tuples/demux",
        1,
        since_cursor=0,
        where={"category": "timeout"},
    )
    client.close()

    # Only the nack with category='timeout' should appear
    assert len(events) == 1
    assert events[0]["op"] == "nack"
    assert events[0]["category"] == "timeout"
    assert events[0]["tuple_id"] == "demux1"


# ---------------------------------------------------------------------------
# (e) Connection close cleanup — no leaked handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_close_cleanup(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Open and immediately close 10 event_stream subscriptions; no handler leaks."""
    _insert_tuple(seeded_db, tuple_id="cl1", subspace="tuples/cleanup")

    initial_handlers = len(daemon._active_handlers)

    for _ in range(10):
        client = _make_client(daemon)
        # _collect_n runs the generator in a thread pool executor, reads 1 event,
        # then closes the generator and socket — event loop stays free.
        await _collect_n(client, "tuples/cleanup", 1, since_cursor=0)
        client.close()

    # Allow asyncio to clean up handler tasks
    await asyncio.sleep(0.1)

    # No more active handlers than before (they should have exited)
    assert len(daemon._active_handlers) == initial_handlers


# ---------------------------------------------------------------------------
# (f) Backfill cap — 1500 tuples, burst 1000 then 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_cap_1500(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Insert 1500 tuples; backfill delivers all via two bursts (1000 + 500)."""
    n = 1500
    for i in range(n):
        # Batch inserts without triggering per-row commits to speed up the test
        seeded_db.execute(
            """\
            INSERT INTO tuples
                (id, subspace, template_name, content, dimensions_json, embed_text,
                 created_at)
            VALUES (?, 'tuples/bigbatch', 'test', 'x', '{}', 'x', ?)
            """,
            (f"big{i}", time.time()),
        )
    seeded_db.commit()  # Single commit triggers 1500 trigger-events atomically

    client = _make_client(daemon)
    events = await _collect_n(client, "tuples/bigbatch", n, since_cursor=0, timeout=15.0)
    client.close()

    assert len(events) == n
    # Cursors must be monotonically increasing and gapless
    cursors = [e["cursor"] for e in events]
    assert cursors == sorted(set(cursors))
    assert len(cursors) == n
    # All are 'out' ops from the tuples trigger
    assert all(e["op"] == "out" for e in events)


# ---------------------------------------------------------------------------
# (g) Live mode >cap: single transaction emits 1500 rows AFTER subscribe;
#     all must arrive (regression for the live-mode silent-drop bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_mode_above_cap_drains_all_events(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Subscribe FIRST, then commit one >cap transaction; all rows arrive.

    Regression: live-mode previously advanced last_data_version after a single
    fetch capped at BACKFILL_BURST_LIMIT (1000). A transaction with >1000
    trigger-fired rows would silently strand the remainder until another
    unrelated write bumped data_version. Fixed by looping the live fetch
    until an empty / under-cap batch returns.
    """
    n = 1500
    client = _make_client(daemon)
    # Spawn the collector first so it's in live mode when the transaction commits.
    collector_task = asyncio.create_task(
        _collect_n(client, "tuples/livecap", n, since_cursor=0, timeout=20.0)
    )
    # Yield enough times for the subscription to reach Phase 2 (live mode).
    for _ in range(50):
        await asyncio.sleep(0.01)

    # Single transaction, single commit -> single data_version bump -> 1500 trigger rows.
    for i in range(n):
        seeded_db.execute(
            """\
            INSERT INTO tuples
                (id, subspace, template_name, content, dimensions_json, embed_text,
                 created_at)
            VALUES (?, 'tuples/livecap', 'test', 'x', '{}', 'x', ?)
            """,
            (f"livecap{i}", time.time()),
        )
    seeded_db.commit()

    events = await collector_task
    client.close()

    assert len(events) == n, (
        f"live mode dropped {n - len(events)} events under one data_version bump; "
        "the per-poll fetch must drain in bursts until empty before advancing last_data_version"
    )
    cursors = [e["cursor"] for e in events]
    assert cursors == sorted(set(cursors))


# ---------------------------------------------------------------------------
# Schema / migration smoke tests
# ---------------------------------------------------------------------------


def test_events_table_created_by_schema(tmp_path: Path) -> None:
    """apply_tuples_schema creates the events table and both triggers."""
    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "events" in tables

        triggers = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert "trg_tuples_out" in triggers
        assert "trg_claim_log_event" in triggers
    finally:
        conn.close()


def test_failure_category_column_on_claim_log(tmp_path: Path) -> None:
    """tuple_claim_log has failure_category column after schema apply."""
    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tuple_claim_log)").fetchall()}
        assert "failure_category" in cols
    finally:
        conn.close()


def test_out_trigger_inserts_event(tmp_path: Path) -> None:
    """Inserting a tuple fires trg_tuples_out and creates an events row."""
    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        _insert_tuple(conn, tuple_id="trg1", subspace="tuples/trigger")
        rows = conn.execute("SELECT op, subspace, tuple_id, category FROM events").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("out", "tuples/trigger", "trg1", "data")
    finally:
        conn.close()


def test_nack_trigger_inserts_event_with_category(tmp_path: Path) -> None:
    """Nack insert fires trg_claim_log_event with correct failure_category."""
    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        _insert_tuple(conn, tuple_id="nk1", subspace="tuples/nack")
        _insert_nack(conn, tuple_id="nk1", subspace="tuples/nack", failure_category="disk_error")
        rows = conn.execute(
            "SELECT op, tuple_id, category FROM events ORDER BY rowid"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == ("out", "nk1", "data")
        assert rows[1] == ("nack", "nk1", "disk_error")
    finally:
        conn.close()


def test_migrate_tuples_failure_category_idempotent(tmp_path: Path) -> None:
    """migrate_tuples_failure_category is idempotent on a fresh schema."""
    from nexus.db.migrations import migrate_tuples_failure_category

    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        # Should be a no-op (column already present via fresh schema)
        migrate_tuples_failure_category(conn)
        migrate_tuples_failure_category(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tuple_claim_log)").fetchall()}
        assert "failure_category" in cols
    finally:
        conn.close()


def test_migrate_tuples_events_table_idempotent(tmp_path: Path) -> None:
    """migrate_tuples_events_table is idempotent on a fresh schema."""
    from nexus.db.migrations import migrate_tuples_events_table

    conn = _open_tuples_db(tmp_path / "t.db")
    try:
        migrate_tuples_events_table(conn)
        migrate_tuples_events_table(conn)
        tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "events" in tables
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (g) nexus-pce1.4: nack-after-tuple-delete still delivers event to subscriber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nack_after_tuple_deleted_still_delivered(
    daemon: T2Daemon,
    seeded_db: sqlite3.Connection,
) -> None:
    """Retention-sweep simulation: delete the tuple, then write the nack.

    Pre-pce1.4 trg_claim_log_event used COALESCE-on-JOIN with empty-string
    fallback, which left no subspace-matched subscriber able to see the
    event. With subspace denormalized into tuple_claim_log, the trigger
    uses NEW.subspace directly and the event reaches subscribers.
    """
    subspace = "tuples/retention"
    _insert_tuple(seeded_db, tuple_id="rt1", subspace=subspace)
    # Hard-delete the tuple BEFORE the nack lands (retention sweep race).
    seeded_db.execute("DELETE FROM tuples WHERE id = 'rt1'")
    seeded_db.commit()
    # Now write the nack — subspace must be supplied because the parent row is gone.
    _insert_nack(seeded_db, tuple_id="rt1", subspace=subspace, failure_category="late-nack")

    client = _make_client(daemon)
    # We expect two events for this subspace: the 'out' and the 'nack'.
    events = await _collect_n(client, subspace, 2, since_cursor=0, timeout=5.0)
    client.close()

    ops = sorted(e["op"] for e in events)
    assert ops == ["nack", "out"]
    nack_event = next(e for e in events if e["op"] == "nack")
    assert nack_event["subspace"] == subspace
    assert nack_event["category"] == "late-nack"


# ---------------------------------------------------------------------------
# (h) nexus-pce1.5: subspace_prefix GLOB injection rejected
# ---------------------------------------------------------------------------


def test_validate_subspace_prefix_accepts_safe_inputs() -> None:
    from nexus.daemon.event_stream import _validate_subspace_prefix

    assert _validate_subspace_prefix("tuples/tasks") is None
    assert _validate_subspace_prefix("tuples/tasks*") is None
    assert _validate_subspace_prefix("tuples/tasks/coordinator") is None
    assert _validate_subspace_prefix("tuples/v1.2") is None
    assert _validate_subspace_prefix("tuples/with-dash") is None
    assert _validate_subspace_prefix("tuples/with_underscore") is None


def test_validate_subspace_prefix_rejects_glob_metachars() -> None:
    from nexus.daemon.event_stream import _validate_subspace_prefix

    assert "'?'" in (_validate_subspace_prefix("tuples/foo?") or "")
    assert "[" in (_validate_subspace_prefix("tuples/[abc]") or "")
    assert "]" in (_validate_subspace_prefix("tuples/abc]") or "")


def test_validate_subspace_prefix_rejects_non_terminal_star() -> None:
    from nexus.daemon.event_stream import _validate_subspace_prefix

    err = _validate_subspace_prefix("tuples/*/foo")
    assert err is not None
    assert "trailing" in err or "at most one" in err


def test_validate_subspace_prefix_rejects_multiple_stars() -> None:
    from nexus.daemon.event_stream import _validate_subspace_prefix

    err = _validate_subspace_prefix("tuples/*/*")
    assert err is not None


def test_validate_subspace_prefix_rejects_disallowed_chars() -> None:
    from nexus.daemon.event_stream import _validate_subspace_prefix

    assert _validate_subspace_prefix("tuples/foo bar") is not None  # space
    assert _validate_subspace_prefix("tuples/foo;DROP") is not None  # semicolon
    assert _validate_subspace_prefix("tuples/foo'") is not None  # quote
