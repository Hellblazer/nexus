# SPDX-License-Identifier: Apache-2.0
"""Tests for nexus.tuplespace.watcher — _DataVersionWatcher (RDR-110 P1.4).

The watcher is direct-mode only: polls PRAGMA data_version every 1ms and
fires a threading.Event on any increment (any commit to tuples.db).

Tests are isolated to tmp_path SQLite files. NX_STORAGE_MODE env guard is
verified by a separate test.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest


class TestWatcher:
    def test_watcher_fires_on_commit(self, tmp_path: Path) -> None:
        """Watcher fires wake_event when a commit increments data_version."""
        from nexus.tuplespace.store import apply_tuples_schema
        from nexus.tuplespace.watcher import _DataVersionWatcher

        db_path = tmp_path / "tuples.db"
        # Open write connection
        write_conn = sqlite3.connect(str(db_path))
        write_conn.execute("PRAGMA journal_mode=WAL")
        apply_tuples_schema(write_conn)
        write_conn.commit()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()
        # Wait briefly for the watcher thread to start and read the initial
        # data_version so the first commit is guaranteed to be a fresh change.
        time.sleep(0.05)

        try:
            # Do a commit on the write connection
            write_conn.execute(
                "INSERT INTO tuples (id, subspace, template_name, content, "
                "dimensions_json, embed_text, created_at) "
                "VALUES ('t1', 'tasks/nexus', 'tasks/<project>', 'content', '{}', 'embed', ?)",
                (time.time(),),
            )
            write_conn.commit()

            # Wait for wake_event with generous timeout
            fired = wake_event.wait(timeout=2.0)
            assert fired, "wake_event should fire after a commit"
        finally:
            watcher.stop()
            write_conn.close()

    def test_watcher_does_not_fire_without_commit(self, tmp_path: Path) -> None:
        """Watcher does NOT fire if no commit happens within the window."""
        from nexus.tuplespace.store import apply_tuples_schema
        from nexus.tuplespace.watcher import _DataVersionWatcher

        db_path = tmp_path / "tuples2.db"
        write_conn = sqlite3.connect(str(db_path))
        write_conn.execute("PRAGMA journal_mode=WAL")
        apply_tuples_schema(write_conn)
        write_conn.commit()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()
        # Wait for watcher thread to settle before checking quiescence.
        time.sleep(0.05)

        try:
            # Sleep briefly but make no commits
            fired = wake_event.wait(timeout=0.1)
            assert not fired, "wake_event should NOT fire without a commit"
        finally:
            watcher.stop()
            write_conn.close()

    def test_watcher_stops_cleanly(self, tmp_path: Path) -> None:
        """stop() terminates the polling thread within 1 second."""
        from nexus.tuplespace.store import apply_tuples_schema
        from nexus.tuplespace.watcher import _DataVersionWatcher

        db_path = tmp_path / "tuples3.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        conn.commit()
        conn.close()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()
        watcher.stop()

        # After stop(), the thread should be dead
        assert not watcher._thread.is_alive(), "Watcher thread should be dead after stop()"

    def test_watcher_start_is_idempotent(self, tmp_path: Path) -> None:
        """Calling start() twice does not raise."""
        from nexus.tuplespace.store import apply_tuples_schema
        from nexus.tuplespace.watcher import _DataVersionWatcher

        db_path = tmp_path / "tuples4.db"
        conn = sqlite3.connect(str(db_path))
        apply_tuples_schema(conn)
        conn.commit()
        conn.close()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()
        try:
            watcher.start()  # Should not raise
        finally:
            watcher.stop()

    def test_watcher_refires_on_multiple_commits(self, tmp_path: Path) -> None:
        """Each commit increments data_version; wake_event can be cleared and re-fired."""
        from nexus.tuplespace.store import apply_tuples_schema
        from nexus.tuplespace.watcher import _DataVersionWatcher

        db_path = tmp_path / "tuples5.db"
        write_conn = sqlite3.connect(str(db_path))
        write_conn.execute("PRAGMA journal_mode=WAL")
        apply_tuples_schema(write_conn)
        write_conn.commit()

        wake_event = threading.Event()
        watcher = _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
        watcher.start()
        # Wait for watcher thread to settle before starting the commit loop.
        time.sleep(0.05)

        try:
            for i in range(3):
                wake_event.clear()
                write_conn.execute(
                    "INSERT INTO tuples (id, subspace, template_name, content, "
                    "dimensions_json, embed_text, created_at) "
                    "VALUES (?, 'tasks/nexus', 'tasks/<project>', 'c', '{}', 'e', ?)",
                    (f"t{i}", time.time()),
                )
                write_conn.commit()
                fired = wake_event.wait(timeout=2.0)
                assert fired, f"wake_event should fire on commit {i}"
        finally:
            watcher.stop()
            write_conn.close()


class TestWatcherStorageBoundary:
    def test_watcher_is_direct_mode_only(self, monkeypatch, tmp_path: Path) -> None:
        """_DataVersionWatcher raises if NX_STORAGE_MODE=daemon."""
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        from nexus.tuplespace.watcher import StorageModeError

        db_path = tmp_path / "tuples.db"
        wake_event = threading.Event()

        with pytest.raises(StorageModeError, match="direct"):
            from nexus.tuplespace.watcher import _DataVersionWatcher
            _DataVersionWatcher(db_path=db_path, wake_event=wake_event)
