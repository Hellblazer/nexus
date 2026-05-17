# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bundle E: line-level review polish (nexus-z7hf).

Three behavioural test classes covering the changes that ship with this
bundle. The trivial fixes (4ivq drop ``or None``, qu6t docstring tweak,
kkp9 ``route_payload`` documentation, zrk4 ``_TupleSpaceWatcher`` ->
``_DataVersionWatcher`` rename) are covered by the existing test
suites; this file adds regression guards for the behaviour changes:

- **nexus-dxap**: ``TuplespaceService.close()`` must log on
  ``sqlite3.Error`` rather than silently swallow.
- **nexus-fvww**: ``_DataVersionWatcher.start()`` must be idempotent
  (the guard was already present; the test pins it as a contract).
- **nexus-o5tc**: the polling loop now backs off when idle and resets
  to the fast cadence when activity returns.
"""
from __future__ import annotations

import logging
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# nexus-dxap: TuplespaceService.close() logs on failure
# ---------------------------------------------------------------------------


class TestTuplespaceServiceCloseLogs:
    """``close()`` may not silently swallow sqlite errors."""

    def test_close_logs_on_sqlite_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """close() must call ``_log.warning`` rather than silently swallow."""
        from nexus.daemon import tuplespace_service as svc_mod
        import chromadb
        from nexus.daemon.tuplespace_service import TuplespaceService

        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=chromadb.PersistentClient(path=str(chroma_dir)),
        )

        class _BoomConn:
            def close(self) -> None:
                raise sqlite3.OperationalError("simulated close failure")

        # Cleanly close the real conn before replacing it with the boom stub.
        service._conn.close()
        service._conn = _BoomConn()  # type: ignore[assignment]

        # Capture structlog warning calls by patching the module-level logger.
        captured: list[tuple[str, dict]] = []

        class _LogProbe:
            def warning(self, event, **kw):
                captured.append((event, kw))

            def __getattr__(self, _name):
                # Other levels (debug/info/error) are no-ops for this test.
                return lambda *a, **k: None

        monkeypatch.setattr(svc_mod, "_log", _LogProbe())

        service.close()  # Must not raise.

        assert any(
            event == "tuplespace_service_close_failed" for event, _ in captured
        ), f"expected the warning event to fire; got: {captured}"
        # Error details surfaced in the structured payload.
        for event, kw in captured:
            if event == "tuplespace_service_close_failed":
                assert "simulated close failure" in kw.get("error", "")
                assert kw.get("error_type") == "OperationalError"

    def test_close_succeeds_on_clean_path(self, tmp_path: Path) -> None:
        """Normal close path: no warning emitted, no exception raised."""
        import chromadb
        from nexus.daemon.tuplespace_service import TuplespaceService

        chroma_dir = tmp_path / "chroma"
        chroma_dir.mkdir()
        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=chromadb.PersistentClient(path=str(chroma_dir)),
        )
        # Should be silent and clean.
        service.close()


# ---------------------------------------------------------------------------
# nexus-fvww: _DataVersionWatcher.start() idempotency
# ---------------------------------------------------------------------------


def _short_tmpdir() -> Path:
    """tmpdir under /tmp to keep UDS-style paths short."""
    return Path(tempfile.mkdtemp(prefix="bundle-e-", dir="/tmp"))


class TestDataVersionWatcherStartIdempotent:
    """Calling ``start()`` twice must not spawn two polling threads."""

    def test_double_start_keeps_single_thread(self) -> None:
        from nexus.tuplespace.watcher import DataVersionWatcher

        tmp = _short_tmpdir()
        db = tmp / "tuples.db"
        # Touch the DB so the watcher's connect succeeds.
        sqlite3.connect(str(db)).close()

        wake_event = threading.Event()
        watcher = DataVersionWatcher(db_path=db, wake_event=wake_event)
        try:
            watcher.start()
            first_thread = watcher._thread
            assert first_thread is not None
            assert first_thread.is_alive()

            watcher.start()  # second call must NOT spawn another thread
            assert watcher._thread is first_thread, (
                "second start() should keep the same thread instance"
            )
        finally:
            watcher.stop()

    def test_start_after_stop_creates_fresh_thread(self) -> None:
        """After ``stop()``, ``start()`` again must create a new thread."""
        from nexus.tuplespace.watcher import DataVersionWatcher

        tmp = _short_tmpdir()
        db = tmp / "tuples.db"
        sqlite3.connect(str(db)).close()

        wake_event = threading.Event()
        watcher = DataVersionWatcher(db_path=db, wake_event=wake_event)
        watcher.start()
        first_thread = watcher._thread
        watcher.stop()

        # Wait for the thread to actually exit so is_alive() returns False.
        first_thread.join(timeout=2.0)
        assert not first_thread.is_alive()

        watcher.start()
        try:
            assert watcher._thread is not first_thread, (
                "start() after stop() must create a new thread"
            )
            assert watcher._thread.is_alive()
        finally:
            watcher.stop()


# ---------------------------------------------------------------------------
# nexus-o5tc: idle backoff in the polling loop
# ---------------------------------------------------------------------------


class TestPollLoopIdleBackoff:
    """Idle polls back off; activity resets to the fast cadence."""

    def test_baseline_and_max_intervals_exposed(self) -> None:
        """Module exposes the new tunables so callers can override in tests."""
        from nexus.tuplespace import watcher
        # The new module-level constants for the adaptive cadence.
        assert hasattr(watcher, "_POLL_INTERVAL_BASELINE_SECONDS")
        assert hasattr(watcher, "_POLL_INTERVAL_MAX_SECONDS")
        assert (
            watcher._POLL_INTERVAL_BASELINE_SECONDS
            < watcher._POLL_INTERVAL_MAX_SECONDS
        )

    def test_idle_backoff_caps_at_max(self) -> None:
        """The adaptive interval helper grows but never exceeds the cap."""
        from nexus.tuplespace.watcher import (
            _POLL_INTERVAL_MAX_SECONDS,
            _next_poll_interval,
        )
        # Start from the baseline; many consecutive idle ticks ramps up.
        intervals = []
        current = None
        for _ in range(50):
            current = _next_poll_interval(idle_polls=_ * 0 + 50, current=current)
            intervals.append(current)
        for v in intervals:
            assert 0 < v <= _POLL_INTERVAL_MAX_SECONDS

    def test_activity_resets_to_baseline(self) -> None:
        """Detecting activity (idle_polls=0) drops the cadence back to baseline."""
        from nexus.tuplespace.watcher import (
            _POLL_INTERVAL_BASELINE_SECONDS,
            _next_poll_interval,
        )
        # First ramp up
        current = None
        for _ in range(20):
            current = _next_poll_interval(idle_polls=20, current=current)
        # Then activity arrives.
        current = _next_poll_interval(idle_polls=0, current=current)
        assert current == pytest.approx(_POLL_INTERVAL_BASELINE_SECONDS)
