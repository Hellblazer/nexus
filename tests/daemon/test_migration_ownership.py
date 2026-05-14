# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for daemon-startup migration runner (RDR-112 P1.4 / nexus-w0et).

Contract:
- T2Daemon.start() applies all pending migrations to memory.db AND
  creates/applies the tuples.db schema BEFORE binding sockets.
- watcher_state table exists in tuples.db after first daemon start.
- Schema-version mismatch in hello_ack raises T2DaemonError with a
  directional instruction message.
- The lifecycle log event "daemon/t2/lifecycle" fires with
  op="migration-applied", from, and to keys.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
import structlog
import structlog.testing

from nexus.daemon.t2_client import T2Client, T2DaemonError, T2_SCHEMA_VERSION_EXPECTED
from nexus.daemon.t2_daemon import DAEMON_SCHEMA_VERSION, T2Daemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_daemon_sync(daemon: T2Daemon) -> None:
    """Start *daemon* on the calling thread's new asyncio event loop."""
    asyncio.run(daemon.start())


def _get_watcher_state_info(db_path: Path) -> dict[str, Any]:
    """Return PRAGMA table_info and index_list for watcher_state."""
    conn = sqlite3.connect(str(db_path))
    try:
        cols = {
            row[1]: row
            for row in conn.execute(
                "PRAGMA table_info(watcher_state)"
            ).fetchall()
        }
        indexes = conn.execute(
            "PRAGMA index_list(watcher_state)"
        ).fetchall()
        return {"cols": cols, "indexes": indexes}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Test (a): startup applies pending migration before bind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_state_exists_before_first_rpc(tmp_path):
    """Start a fresh daemon; verify watcher_state exists in tuples.db
    before any client RPC can succeed.

    The table must be created by the migration runner in T2Daemon.start()
    BEFORE the sockets are bound, so that the moment a client connects,
    the schema is already in place.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    daemon = T2Daemon(config_dir)
    await daemon.start()

    tuples_path = config_dir / "tuples.db"
    assert tuples_path.exists(), "tuples.db must be created at daemon startup"

    conn = sqlite3.connect(str(tuples_path))
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='watcher_state'"
        ).fetchone()
        assert row is not None, (
            "watcher_state table must exist in tuples.db after daemon.start() "
            "— migration runner must apply schema before bind"
        )
    finally:
        conn.close()
        await daemon.stop()


# ---------------------------------------------------------------------------
# Test (b): version handshake mismatch raises T2DaemonError with direction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_version_mismatch_raises_directional_error(tmp_path):
    """Verify that the daemon's hello_ack carries schema_version, and that
    T2Client raises T2DaemonError with directional instruction on mismatch.

    Uses asyncio primitives (matching the existing transport test pattern)
    to drive the handshake from within the daemon's event loop — avoids the
    synchronous-socket-in-async-loop deadlock that T2Client.ping() would
    cause when called on the event-loop thread.

    Validates the client-side mismatch detection by calling _connect_once()
    in a thread executor so both event-loop sides can progress concurrently.

    Two sub-cases:
      - client_version > daemon_version: "restart daemon" instruction
      - client_version < daemon_version: "upgrade conexus" instruction
    """
    import nexus.daemon.t2_client as _client_mod
    from nexus.daemon.t2_daemon import read_frame, write_frame, DAEMON_PROTOCOL_VERSION

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    daemon = T2Daemon(config_dir)
    await daemon.start()

    try:
        # Sub-case A: verify daemon's hello_ack includes schema_version.
        reader, writer = await asyncio.open_connection(
            daemon.tcp_host, daemon.tcp_port
        )
        write_frame(writer, {"op": "hello", "protocol_version": DAEMON_PROTOCOL_VERSION})
        await writer.drain()
        ack = await read_frame(reader)
        writer.close()
        await writer.wait_closed()

        assert "schema_version" in ack, (
            f"hello_ack must include schema_version, got keys: {list(ack.keys())}"
        )
        daemon_sv = ack["schema_version"]
        assert daemon_sv == DAEMON_SCHEMA_VERSION, (
            f"schema_version in hello_ack must equal DAEMON_SCHEMA_VERSION "
            f"({DAEMON_SCHEMA_VERSION}), got {daemon_sv}"
        )

        # Sub-case B: client expects NEWER schema → "restart daemon" message.
        original = _client_mod.T2_SCHEMA_VERSION_EXPECTED
        try:
            _client_mod.T2_SCHEMA_VERSION_EXPECTED = DAEMON_SCHEMA_VERSION + 9999

            def _client_connect_newer() -> None:
                c = T2Client(tcp_addr=(daemon.tcp_host, daemon.tcp_port))
                c.ping()

            with pytest.raises(T2DaemonError) as exc_info:
                await asyncio.to_thread(_client_connect_newer)

            msg = str(exc_info.value)
            assert "nx daemon t2 stop" in msg or "restart" in msg.lower(), (
                f"Expected restart instruction, got: {msg!r}"
            )

            # Sub-case C: client expects OLDER schema → "upgrade" message.
            _client_mod.T2_SCHEMA_VERSION_EXPECTED = DAEMON_SCHEMA_VERSION - 9999

            def _client_connect_older() -> None:
                c2 = T2Client(tcp_addr=(daemon.tcp_host, daemon.tcp_port))
                c2.ping()

            with pytest.raises(T2DaemonError) as exc_info2:
                await asyncio.to_thread(_client_connect_older)

            msg2 = str(exc_info2.value)
            assert "upgrade" in msg2.lower() or "uv pip install" in msg2, (
                f"Expected upgrade instruction, got: {msg2!r}"
            )
        finally:
            _client_mod.T2_SCHEMA_VERSION_EXPECTED = original
    finally:
        await daemon.stop()


# ---------------------------------------------------------------------------
# Test (c): watcher_state has correct primary key shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_state_primary_key_shape(tmp_path):
    """After first daemon start, watcher_state must have exactly
    (subspace, profile) as PRIMARY KEY per RDR-111 line 864.

    Checks:
    - Column names: subspace, profile, last_rowid, updated_at
    - PK columns: subspace (pk=1) AND profile (pk=2)
    - At least one index (the autoindex for the PK)
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    daemon = T2Daemon(config_dir)
    await daemon.start()
    await daemon.stop()

    tuples_path = config_dir / "tuples.db"
    info = _get_watcher_state_info(tuples_path)

    cols = info["cols"]
    assert "subspace" in cols, "watcher_state must have 'subspace' column"
    assert "profile" in cols, "watcher_state must have 'profile' column"
    assert "last_rowid" in cols, "watcher_state must have 'last_rowid' column"
    assert "updated_at" in cols, "watcher_state must have 'updated_at' column"

    # pk flag: 0 = not PK; > 0 = PK member (value is position in composite PK)
    pk_cols = {name for name, row in cols.items() if row[5] > 0}
    assert pk_cols == {"subspace", "profile"}, (
        f"PRIMARY KEY must be exactly (subspace, profile) per RDR-111, got: {pk_cols}"
    )

    # There must be at least one index (SQLite creates a unique index for
    # composite PKs automatically: sqlite_autoindex_watcher_state_1)
    assert len(info["indexes"]) >= 1, (
        "watcher_state must have at least one index (auto-PK index)"
    )


# ---------------------------------------------------------------------------
# Test (d): lifecycle log event fires during daemon start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_migration_applied_event(tmp_path):
    """T2Daemon.start() must emit a structured log event:
    event='daemon/t2/lifecycle' with op='migration-applied', 'from', 'to' keys.
    """
    import logging

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Temporarily set structlog to DEBUG so INFO events are not filtered
    # by the conftest's WARNING-level wrapper_class before reaching capture_logs.
    saved_config = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    try:
        with structlog.testing.capture_logs() as captured:
            daemon = T2Daemon(config_dir)
            await daemon.start()
            await daemon.stop()
    finally:
        structlog.configure(**saved_config)

    # Look for the migration-applied event
    migration_events = [
        e for e in captured
        if e.get("event") == "daemon/t2/lifecycle"
        and e.get("op") == "migration-applied"
    ]
    assert len(migration_events) >= 1, (
        "Expected at least one 'daemon/t2/lifecycle' event with op='migration-applied'. "
        f"Got events: {[e.get('event') for e in captured]}"
    )
    evt = migration_events[0]
    assert "from" in evt, f"Event must have 'from' key, got: {evt}"
    assert "to" in evt, f"Event must have 'to' key, got: {evt}"


# ---------------------------------------------------------------------------
# Test (e): direct-mode and daemon paths converge on same tuples.db schema
# ---------------------------------------------------------------------------


def test_direct_and_daemon_paths_converge_on_same_tuples_schema(tmp_path):
    """Both NX_STORAGE_MODE=direct (using open_tuples_db) and the daemon
    startup path must produce the same tuples.db schema (same tables,
    same indexes, same columns).
    """
    from nexus.tuplespace.store import open_tuples_db

    # Direct path: open_tuples_db applies the full schema
    direct_path = tmp_path / "tuples_direct.db"
    conn_direct = open_tuples_db(direct_path)
    conn_direct.close()

    # Daemon path: daemon startup via run_daemon_migrations
    daemon_path = tmp_path / "tuples_daemon.db"
    memory_path = tmp_path / "memory_daemon.db"
    from nexus.db.migrations import run_daemon_migrations
    run_daemon_migrations(memory_path, daemon_path)

    def _schema_rows(path: Path) -> list[tuple[str, str]]:
        c = sqlite3.connect(str(path))
        try:
            return sorted(
                c.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL "
                    "ORDER BY name"
                ).fetchall()
            )
        finally:
            c.close()

    direct_schema = _schema_rows(direct_path)
    daemon_schema = _schema_rows(daemon_path)

    assert direct_schema == daemon_schema, (
        "Direct-mode (open_tuples_db) and daemon-mode (run_daemon_migrations) "
        "must produce identical tuples.db schemas.\n"
        f"Direct: {[r[0] for r in direct_schema]}\n"
        f"Daemon: {[r[0] for r in daemon_schema]}"
    )


# ---------------------------------------------------------------------------
# Test (f): second daemon start on same data dir is a no-op (idempotency)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_daemon_start_is_idempotent(tmp_path):
    """A second daemon start against the same config_dir must be a no-op
    (already at the current schema version). The tuples.db schema must
    be byte-identical across both starts.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    tuples_path = config_dir / "tuples.db"

    def _schema(path: Path) -> list[tuple[str, str]]:
        c = sqlite3.connect(str(path))
        try:
            return sorted(
                c.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE sql IS NOT NULL ORDER BY name"
                ).fetchall()
            )
        finally:
            c.close()

    daemon1 = T2Daemon(config_dir)
    await daemon1.start()
    schema_after_first = _schema(tuples_path)
    await daemon1.stop()

    # Second start on the same data dir — must not raise, must not change schema
    daemon2 = T2Daemon(config_dir)
    await daemon2.start()
    schema_after_second = _schema(tuples_path)
    await daemon2.stop()

    assert schema_after_first == schema_after_second, (
        "Second daemon start must produce identical schema "
        "(CREATE TABLE IF NOT EXISTS idempotency)."
    )
