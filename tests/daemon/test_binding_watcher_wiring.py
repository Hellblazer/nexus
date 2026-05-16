# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2Daemon binding-watcher wiring tests (RDR-111 Phase 2 Step 6, nexus-9eiw).

Verifies that the daemon constructs and starts the binding watcher
during ``start()``, cleans it up during ``stop()``, and honours the
``NX_COCKPIT_BINDINGS_DISABLE`` env override.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import chromadb
import pytest
import pytest_asyncio

from nexus.daemon.subspace_registry import RegistryStore
from nexus.daemon.t2_daemon import T2Daemon, _cockpit_bindings_disabled
from nexus.daemon.tuplespace_service import TuplespaceService
from nexus.db.t2 import T2Database


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config" / "nexus"
    d.mkdir(parents=True)
    return d


@pytest_asyncio.fixture()
async def daemon_with_tuplespace(config_dir: Path):
    """Yield a started T2Daemon wired with a tuplespace service."""
    memory_db_path = config_dir / "memory.db"
    tuples_db_path = config_dir / "tuples.db"
    chroma_client = chromadb.EphemeralClient()

    t2db = T2Database(memory_db_path)
    registry_store = RegistryStore(tuples_db_path=tuples_db_path)
    tuplespace_service = TuplespaceService(
        tuples_db_path=tuples_db_path,
        chroma_client=chroma_client,
    )
    daemon = T2Daemon(
        config_dir=config_dir,
        t2db=t2db,
        tuples_db_path=tuples_db_path,
        registry_store=registry_store,
        tuplespace_service=tuplespace_service,
    )
    await daemon.start()
    try:
        yield daemon
    finally:
        try:
            await daemon.stop()
        except Exception:
            pass
        t2db.close()


# ---------------------------------------------------------------------------
# Env-gate helper
# ---------------------------------------------------------------------------


class TestCockpitBindingsDisableEnv:
    @pytest.mark.parametrize("falsy", ["", "0", "false", "False"])
    def test_falsy_values_keep_watcher_enabled(
        self, monkeypatch: pytest.MonkeyPatch, falsy: str
    ) -> None:
        monkeypatch.setenv("NX_COCKPIT_BINDINGS_DISABLE", falsy)
        assert _cockpit_bindings_disabled() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "TRUE", " 1 "])
    def test_truthy_values_disable_watcher(
        self, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        monkeypatch.setenv("NX_COCKPIT_BINDINGS_DISABLE", truthy)
        assert _cockpit_bindings_disabled() is True

    def test_unset_keeps_watcher_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NX_COCKPIT_BINDINGS_DISABLE", raising=False)
        assert _cockpit_bindings_disabled() is False


# ---------------------------------------------------------------------------
# Daemon-level wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_start_constructs_binding_watcher(
    daemon_with_tuplespace: T2Daemon,
) -> None:
    """Default startup wires the watcher when profiles directory has profiles."""
    daemon = daemon_with_tuplespace
    # The builtin default profile ships under nx/tuplespace/builtin/bindings/profiles
    # so the watcher must have been constructed.
    assert daemon._binding_watcher is not None
    assert daemon._binding_watcher_conn is not None
    assert daemon._binding_watcher_memory_conn is not None
    # And a running task.
    assert daemon._binding_watcher._task is not None
    assert not daemon._binding_watcher._task.done()


@pytest.mark.asyncio
async def test_daemon_stop_cleans_up_binding_watcher(
    config_dir: Path,
) -> None:
    """stop() must await the watcher, close connections, and null its handles."""
    memory_db_path = config_dir / "memory.db"
    tuples_db_path = config_dir / "tuples.db"
    chroma_client = chromadb.EphemeralClient()

    t2db = T2Database(memory_db_path)
    registry_store = RegistryStore(tuples_db_path=tuples_db_path)
    tuplespace_service = TuplespaceService(
        tuples_db_path=tuples_db_path,
        chroma_client=chroma_client,
    )
    daemon = T2Daemon(
        config_dir=config_dir,
        t2db=t2db,
        tuples_db_path=tuples_db_path,
        registry_store=registry_store,
        tuplespace_service=tuplespace_service,
    )
    await daemon.start()
    assert daemon._binding_watcher is not None
    watcher_task = daemon._binding_watcher._task

    await daemon.stop()
    assert daemon._binding_watcher is None
    assert daemon._binding_watcher_conn is None
    assert daemon._binding_watcher_memory_conn is None
    assert watcher_task is not None
    assert watcher_task.done()
    t2db.close()


@pytest.mark.asyncio
async def test_disable_env_short_circuits_watcher_construction(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NX_COCKPIT_BINDINGS_DISABLE=1 must skip watcher construction entirely."""
    monkeypatch.setenv("NX_COCKPIT_BINDINGS_DISABLE", "1")
    memory_db_path = config_dir / "memory.db"
    tuples_db_path = config_dir / "tuples.db"
    chroma_client = chromadb.EphemeralClient()

    t2db = T2Database(memory_db_path)
    registry_store = RegistryStore(tuples_db_path=tuples_db_path)
    tuplespace_service = TuplespaceService(
        tuples_db_path=tuples_db_path,
        chroma_client=chroma_client,
    )
    daemon = T2Daemon(
        config_dir=config_dir,
        t2db=t2db,
        tuples_db_path=tuples_db_path,
        registry_store=registry_store,
        tuplespace_service=tuplespace_service,
    )
    await daemon.start()
    try:
        assert daemon._binding_watcher is None
    finally:
        await daemon.stop()
        t2db.close()


@pytest.mark.asyncio
async def test_binding_watcher_loop_processes_synthetic_event(
    daemon_with_tuplespace: T2Daemon,
) -> None:
    """The polling loop reacts to events inserted directly into the table.

    Bypasses ``out()`` so the test doesn't need every hook_events
    subspace YAML loaded into the daemon's registry. Inserts a row that
    matches the bundled ``notification_log_marker`` binding's predicate
    and verifies the watcher advances its cursor past it.
    """
    daemon = daemon_with_tuplespace
    watcher = daemon._binding_watcher
    assert watcher is not None
    conn = daemon._binding_watcher_conn
    assert conn is not None

    # Synthesize an events row matching the notification_log_marker binding.
    conn.execute(
        "INSERT INTO events "
        "(subspace, op, tuple_id, payload_summary, category, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "hook_events/notification",
            "out",
            "synthetic-tuple-id",
            None,
            "data",
            1700000000.0,
        ),
    )
    conn.commit()
    initial_rowid = conn.execute(
        "SELECT MAX(rowid) FROM events"
    ).fetchone()[0]
    assert initial_rowid is not None

    # Watcher poll interval is 0.05s; allow generous slack for slow CI.
    advanced = False
    for _ in range(40):  # up to ~2 seconds
        await asyncio.sleep(0.05)
        # Cursor for the default profile must be >= the row we inserted.
        cursor = watcher._cursors.get("default", -1)
        if cursor >= initial_rowid:
            advanced = True
            break

    assert advanced, (
        f"binding watcher did not advance past rowid {initial_rowid}; "
        f"cursors={watcher._cursors!r}"
    )
