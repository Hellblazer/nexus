# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3a.B (nexus-iebjm): T2Client substrate-only tests.

Cover:

- make_t2_client returns a T2Client with the expected store proxies.
- Fail-loud when no daemon is reachable (T2DaemonNotReachableError
  with the `nx daemon t2 start` recovery hint).
- End-to-end RPC: spawn a real T2Daemon in a background thread; the
  client connects via UDS, drives memory.put / memory.search; results
  match the in-process T2Database round trip.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Short config_dir under /tmp because macOS limits AF_UNIX paths
    to 104 chars and pytest's tmp_path already eats ~75 of those."""
    import shutil
    import tempfile

    cd = Path(tempfile.mkdtemp(prefix="nxt2-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


@pytest.fixture(autouse=True)
def _clear_t2_env(monkeypatch):
    monkeypatch.delenv("NX_T2_SOCK", raising=False)
    monkeypatch.delenv("NX_T2_ADDR", raising=False)


# ---------------------------------------------------------------------------
# Construction + store proxies
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_make_t2_client_returns_t2client(self) -> None:
        from nexus.daemon.t2_client import T2Client, make_t2_client

        client = make_t2_client()
        try:
            assert isinstance(client, T2Client)
        finally:
            client.close()

    def test_store_proxies_present(self) -> None:
        from nexus.daemon.t2_client import T2Client

        client = T2Client()
        try:
            for store in (
                "memory", "plans", "chash_index", "taxonomy", "telemetry",
                "document_aspects", "aspect_queue", "database",
            ):
                assert hasattr(client, store), f"missing store proxy: {store}"
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Fail-loud
# ---------------------------------------------------------------------------


class TestFailLoud:
    def test_no_daemon_surfaces_t2_daemon_not_reachable(
        self, config_dir: Path,
    ) -> None:
        from nexus.daemon.t2_client import (
            T2Client, T2DaemonNotReachableError,
        )

        client = T2Client(config_dir=config_dir)
        with pytest.raises(T2DaemonNotReachableError) as excinfo:
            client.memory.list_recent(limit=1)
        assert "nx daemon t2 start" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end RPC against a real daemon
# ---------------------------------------------------------------------------


def _drive_daemon(daemon, ready: threading.Event, stop: threading.Event):
    async def _main() -> None:
        await daemon.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


@pytest.fixture
def live_daemon(config_dir: Path, db_path: Path):
    from nexus.daemon.t2_daemon import T2Daemon

    daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
    ready = threading.Event()
    stop = threading.Event()
    thread = threading.Thread(target=_drive_daemon, args=(daemon, ready, stop))
    thread.start()
    assert ready.wait(timeout=10.0), "daemon did not start within 10s"
    try:
        yield daemon
    finally:
        stop.set()
        thread.join(timeout=10.0)


class TestEndToEndRpc:
    def test_memory_put_then_search_round_trip(
        self, config_dir: Path, live_daemon,
    ) -> None:
        """Client connects via UDS (preferred per discovery file),
        writes a memory entry, searches for it, gets it back."""
        from nexus.daemon.t2_client import T2Client

        with T2Client(config_dir=config_dir) as client:
            row_id = client.memory.put(
                content="rdr-120 p3a test memory entry",
                project="nexus_test",
                title="p3a-test",
                tags="rdr-120,test",
            )
            assert isinstance(row_id, int) and row_id > 0

            # memory.search returns rows whose content matches the query.
            results = client.memory.search(
                "rdr-120 p3a test", project="nexus_test",
            )
            assert isinstance(results, list)
            assert any(
                "rdr-120 p3a test memory entry" in (r.get("content") or "")
                for r in results
            ), f"expected hit not found in {results!r}"

    def test_unknown_op_surfaces_t2_client_error(
        self, config_dir: Path, live_daemon,
    ) -> None:
        """Daemon-side ProtocolError for an unknown op round-trips
        to a T2ClientError with the op + error type populated."""
        from nexus.daemon.t2_client import T2Client, T2ClientError

        with T2Client(config_dir=config_dir) as client:
            with pytest.raises(T2ClientError) as excinfo:
                client.call("nonexistent.op")
            assert excinfo.value.op == "nonexistent.op"
            assert "ProtocolError" in excinfo.value.error_type or \
                   "unknown op" in excinfo.value.message


# ---------------------------------------------------------------------------
# RDR-120 P3b: schema-version handshake
# ---------------------------------------------------------------------------


class TestSchemaHandshake:
    """RDR-120 P3b (nexus-e9x4l): T2Client invokes ``database.hello`` on
    first connect; mismatch raises ``T2SchemaVersionMismatchError``.
    """

    def test_hello_round_trip_returns_daemon_schema_version(
        self, config_dir: Path, live_daemon,
    ) -> None:
        from nexus.daemon.t2_client import T2Client

        with T2Client(config_dir=config_dir) as client:
            result = client.database.hello(client_schema_version="probe")
            assert isinstance(result, dict)
            assert "daemon_schema_version" in result
            assert result["client_schema_version"] == "probe"

    def test_handshake_runs_once_per_connection(
        self, config_dir: Path, live_daemon,
    ) -> None:
        """Two RPCs in a row over a single connection produce exactly
        one handshake; second call skips via ``_handshake_done``."""
        from nexus.daemon.t2_client import T2Client

        with T2Client(config_dir=config_dir) as client:
            client.memory.put(content="a", project="p", title="t1")
            assert client._handshake_done is True
            # A second call must not raise; handshake gate stays set.
            client.memory.put(content="b", project="p", title="t2")
            assert client._handshake_done is True

    def test_handshake_mismatch_raises_schema_version_mismatch(
        self, config_dir: Path, live_daemon, monkeypatch,
    ) -> None:
        """Force the client to claim a fake build-version that diverges
        from the daemon's actual stored ``cli_version``; the handshake
        must raise ``T2SchemaVersionMismatchError`` before any
        substrate op runs.
        """
        from nexus.daemon import t2_client as _t2c
        from nexus.daemon.t2_client import (
            T2Client,
            T2SchemaVersionMismatchError,
        )

        # Pin a non-trivial daemon-side stored schema version directly
        # in _nexus_version so the handshake fail-loud branch (both
        # sides non-trivial AND disagreeing) is the one under test.
        import sqlite3 as _sqlite3

        with T2Client(config_dir=config_dir) as warm:
            warm.database.hello()  # ensure _nexus_version table exists
        daemon_db_path = live_daemon._db_path
        conn = _sqlite3.connect(str(daemon_db_path))
        try:
            conn.execute(
                "UPDATE _nexus_version SET value=? WHERE key='cli_version'",
                ("9.99.99",),
            )
            conn.commit()
        finally:
            conn.close()

        from nexus.db.migrations import expected_t2_schema_version
        fake_version = expected_t2_schema_version() + "-mismatch"
        monkeypatch.setattr(
            "nexus.db.migrations.expected_t2_schema_version",
            lambda: fake_version,
        )
        # The handshake imports expected_t2_schema_version inside
        # _do_handshake_locked, so the monkeypatch takes effect on
        # the next client.
        client = T2Client(config_dir=config_dir)
        try:
            with pytest.raises(T2SchemaVersionMismatchError) as excinfo:
                client.memory.put(content="x", project="p", title="t")
            assert excinfo.value.client_version == fake_version
            # Daemon side may report real_version OR "0.0.0" when
            # migrations deferred; either is fine — the mismatch only
            # fires when daemon side is non-trivial AND disagrees.
            assert excinfo.value.daemon_version != fake_version
        finally:
            client.close()


# ---------------------------------------------------------------------------
# RDR-120 P3b: T2Database direct-open no longer auto-migrates
# ---------------------------------------------------------------------------


class TestDirectOpenNoMigrate:
    """Direct-open ``T2Database`` construction in production code paths
    must NOT call ``apply_pending``. Verified by inspecting the
    ``_upgrade_done`` registry after a default construction with the
    test-suite's auto-migrate flag temporarily off.
    """

    def test_default_init_skips_apply_pending(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.db import t2 as _t2
        from nexus.db.migrations import _upgrade_done
        from nexus.db.t2 import T2Database

        # Temporarily restore production semantics for this assertion:
        # clear both the module global and the env-var fallback that
        # conftest sets to propagate auto-migrate into subprocesses.
        monkeypatch.setattr(_t2, "_DEFAULT_RUN_MIGRATIONS", False)
        monkeypatch.delenv(_t2._RUN_MIGRATIONS_ENV, raising=False)

        db_path = tmp_path / "no-migrate.db"
        try:
            path_key = str(db_path.resolve())
        except OSError:
            path_key = str(db_path)
        # Pre-condition: path not yet registered.
        _upgrade_done.discard(path_key)

        db = T2Database(db_path)
        try:
            # Post-condition: path STILL not registered — apply_pending
            # was skipped entirely (the direct-open path no longer
            # triggers it).
            assert path_key not in _upgrade_done
        finally:
            db.close()

    def test_run_migrations_true_invokes_apply_pending(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        from nexus.db import t2 as _t2
        from nexus.db.migrations import _upgrade_done
        from nexus.db.t2 import T2Database

        monkeypatch.setattr(_t2, "_DEFAULT_RUN_MIGRATIONS", False)

        db_path = tmp_path / "yes-migrate.db"
        try:
            path_key = str(db_path.resolve())
        except OSError:
            path_key = str(db_path)
        _upgrade_done.discard(path_key)

        db = T2Database(db_path, run_migrations=True)
        try:
            assert path_key in _upgrade_done
        finally:
            db.close()
