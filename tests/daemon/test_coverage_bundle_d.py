# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundle D: untested error / loop paths in cockpit + daemon.

- **nexus-a79y**: ``_BindingWatcher._tick`` has two ``sqlite3.Error``
  handlers (around ``_fetch_event_batch`` and ``_save_cursor``) that
  log-and-continue. Both were 0% covered. Two tests patch the underlying
  calls to raise ``sqlite3.OperationalError`` mid-tick and assert the
  watcher carries on through the rest of the dispatch.
- **nexus-qhxf**: ``T2Daemon._retention_loop`` body after the
  ``await asyncio.sleep`` was 0% covered. A test shrinks
  ``_RETENTION_SWEEP_INTERVAL_SECONDS`` to milliseconds, spins the loop
  through a couple of iterations, and asserts ``_run_retention_sweep_sync``
  fires at least twice before the cancellation.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from nexus.cockpit.bindings import (
    BindingContext,
    BindingProfile,
    EventRecord,
    BindingWatcher,
)
from nexus.daemon.t2_daemon import T2Daemon


# ---------------------------------------------------------------------------
# nexus-a79y: bindings.py sqlite3.Error handlers
# ---------------------------------------------------------------------------


def _make_events_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE events (
            rowid           INTEGER PRIMARY KEY AUTOINCREMENT,
            subspace        TEXT NOT NULL,
            op              TEXT NOT NULL,
            tuple_id        TEXT NOT NULL,
            payload_summary TEXT,
            category        TEXT,
            ts              REAL NOT NULL
        );
        CREATE INDEX idx_events_subspace_rowid ON events (subspace, rowid);
        CREATE TABLE watcher_state (
            subspace   TEXT NOT NULL,
            profile    TEXT NOT NULL,
            last_rowid INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL,
            PRIMARY KEY (subspace, profile)
        );
        """
    )
    conn.commit()


def _empty_profile(name: str = "test") -> BindingProfile:
    """A profile with no bindings: never matches, so _dispatch_event is a no-op."""
    return BindingProfile(name=name, bindings=())


@pytest.fixture
def tuples_conn(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "tuples.db"))
    _make_events_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def binding_context(tuples_conn: sqlite3.Connection) -> BindingContext:
    return BindingContext(conn=tuples_conn, index=None, registry=None)


class TestBindingWatcherSqliteErrorHandlers:
    """`_BindingWatcher._tick` swallows sqlite3.Error and continues."""

    def test_tick_swallows_fetch_oserror(
        self,
        tuples_conn: sqlite3.Connection,
        binding_context: BindingContext,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """_fetch_event_batch raising sqlite3.OperationalError must not crash _tick."""
        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[_empty_profile("p1"), _empty_profile("p2")],
            context=binding_context,
        )
        watcher._cursors = {"p1": 0, "p2": 0}

        call_count = {"n": 0}

        def _boom(*args, **kwargs):
            call_count["n"] += 1
            raise sqlite3.OperationalError("simulated fetch failure")

        # Patch the module-level helper that _tick calls.
        from nexus.cockpit import bindings
        monkeypatch.setattr(bindings, "_fetch_event_batch", _boom)

        # Should NOT raise; should log + continue for each profile.
        total = asyncio.run(watcher._tick())
        assert total == 0
        # Both profiles attempted, both swallowed.
        assert call_count["n"] == 2
        # Cursors unchanged (no events advanced).
        assert watcher._cursors == {"p1": 0, "p2": 0}

    def test_tick_swallows_save_cursor_oserror(
        self,
        tuples_conn: sqlite3.Connection,
        binding_context: BindingContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """_save_cursor raising sqlite3.OperationalError must not crash _tick."""
        # Insert one event so the fetch returns something and _save_cursor runs.
        tuples_conn.execute(
            "INSERT INTO events (subspace, op, tuple_id, payload_summary, "
            "category, ts) VALUES ('test', 'out', 't1', '', 'data', 0.0)"
        )
        tuples_conn.commit()

        watcher = BindingWatcher(
            conn=tuples_conn,
            profiles=[_empty_profile("p1")],
            context=binding_context,
            subspace_glob="*",
            batch_limit=10,
        )
        watcher._cursors = {"p1": 0}

        def _boom_save(*args, **kwargs):
            raise sqlite3.OperationalError("simulated save failure")

        from nexus.cockpit import bindings
        monkeypatch.setattr(bindings, "_save_cursor", _boom_save)

        # Should NOT raise. Cursor still advances in-memory even though
        # the save failed (event was processed; cursor reflects the work).
        total = asyncio.run(watcher._tick())
        assert total == 1
        # In-memory cursor advanced (the save failure is silent except for the log).
        assert watcher._cursors["p1"] == 1


# ---------------------------------------------------------------------------
# nexus-qhxf: T2Daemon._retention_loop body coverage
# ---------------------------------------------------------------------------


class TestRetentionLoopBody:
    """`_retention_loop` body past ``await asyncio.sleep`` runs."""

    def _short_sock_path(self) -> Path:
        """Return a /tmp config dir under the 104-char UDS path limit."""
        return Path(tempfile.mkdtemp(prefix="rl-", dir="/tmp"))

    def test_retention_loop_runs_multiple_iterations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a sub-second sleep interval, the loop body fires multiple times."""
        config_dir = self._short_sock_path()
        tuples_db = config_dir / "tuples.db"
        config_dir.mkdir(parents=True, exist_ok=True)
        # tuples.db doesn't need a schema for the test; _run_retention_sweep_sync
        # opens its own connection. We monkeypatch the sweep itself so the body
        # of _retention_loop runs without needing real data.
        tuples_db.touch()

        daemon = T2Daemon(config_dir, tuples_db_path=tuples_db)
        # Drive iterations every 50 ms so the test finishes in well under a
        # second even when CI is slow.
        monkeypatch.setattr(T2Daemon, "_RETENTION_SWEEP_INTERVAL_SECONDS", 0.05)

        sweep_calls = {"n": 0}

        def _stub_sweep(self_daemon) -> int:
            sweep_calls["n"] += 1
            return 0

        monkeypatch.setattr(T2Daemon, "_run_retention_sweep_sync", _stub_sweep)

        async def _drive() -> None:
            task = asyncio.create_task(daemon._retention_loop())
            try:
                # Wait long enough for at least 2 iterations at 50 ms cadence.
                await asyncio.sleep(0.25)
            finally:
                daemon._stopping = True
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(_drive())

        # At least two full iterations (the body past the sleep must have run).
        assert sweep_calls["n"] >= 2, (
            f"expected >=2 iterations, observed {sweep_calls['n']}"
        )

    def test_retention_loop_exits_promptly_when_stopping_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting ``_stopping`` after the sleep returns the loop without sweeping."""
        config_dir = self._short_sock_path()
        tuples_db = config_dir / "tuples.db"
        config_dir.mkdir(parents=True, exist_ok=True)
        tuples_db.touch()

        daemon = T2Daemon(config_dir, tuples_db_path=tuples_db)
        # Sleep interval short enough for one iteration but long enough that
        # we can race in the stop flag.
        monkeypatch.setattr(T2Daemon, "_RETENTION_SWEEP_INTERVAL_SECONDS", 0.05)

        sweep_calls = {"n": 0}

        def _stub_sweep(self_daemon) -> int:
            sweep_calls["n"] += 1
            # Block briefly so we can flip the stop flag before the next
            # iteration fires.
            return 0

        monkeypatch.setattr(T2Daemon, "_run_retention_sweep_sync", _stub_sweep)

        async def _drive() -> None:
            task = asyncio.create_task(daemon._retention_loop())
            # Let the first iteration land, then signal stop.
            await asyncio.sleep(0.1)
            daemon._stopping = True
            # Give the loop a tick to observe the flag and exit.
            await asyncio.sleep(0.15)
            assert task.done(), "loop must exit promptly when _stopping is set"
            # Should have completed without exception (returned cleanly
            # via the ``if self._stopping: return`` mid-loop check or via
            # the outer ``while not self._stopping`` predicate).
            assert task.exception() is None
            assert task.result() is None

        asyncio.run(_drive())
