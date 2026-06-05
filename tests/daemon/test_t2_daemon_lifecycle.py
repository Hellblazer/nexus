# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3a.A (nexus-7aayk): T2 daemon lifecycle tests.

Substrate-only daemon scaffold tests. Cover:

- Discovery file shape + uid suffix + atomic 0o600 write.
- start -> serve -> stop happy path (UDS + TCP both bind, discovery
  file written and removed).
- Spawn-lock invariant: a second start against the same config_dir
  raises T2DaemonError (refuses to run a second instance).
- Dispatch table: _build_dispatch_table enumerates the eight stores
  and the database pseudo-store; denylist filters apply.
- Frame protocol: write_frame / read_frame round-trip type-tagged
  payloads (datetime / bytes / Path / dataclass).
- T1/T2/T3 non-collision: discovery filename uses the t2_addr.<uid>
  pattern (distinct from t1_addr.<claude_pid> and t3_addr.<uid>).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Discovery file shape
# ---------------------------------------------------------------------------


class TestDiscoveryFileShape:
    def test_discovery_path_uses_uid_suffix(self, config_dir: Path) -> None:
        from nexus.daemon.t2_daemon import t2_discovery_path

        expected = config_dir / f"t2_addr.{os.getuid()}"
        assert t2_discovery_path(config_dir) == expected

    def test_discovery_filename_distinct_from_t1_and_t3(self, config_dir: Path) -> None:
        from nexus.daemon.t2_daemon import t2_discovery_path
        from nexus.daemon.t3_daemon import t3_discovery_path

        t2 = t2_discovery_path(config_dir)
        t3 = t3_discovery_path(config_dir)
        assert t2.name.startswith("t2_addr.")
        assert t3.name.startswith("t3_addr.")
        assert t2 != t3
        assert "t1_addr" not in t2.name


# ---------------------------------------------------------------------------
# Frame protocol round-trip
# ---------------------------------------------------------------------------


@dataclass
class QueueRow:
    """One of the wire-allowlisted dataclasses; mirrors the real
    aspect_extraction_queue.QueueRow shape closely enough for the
    encoder/decoder round-trip test."""
    doc_id: str
    status: str


class TestFrameProtocol:
    def test_t2_json_round_trip_primitives_and_collections(self) -> None:
        from nexus.daemon.t2_daemon import t2_json_dumps, t2_json_loads

        for value in (None, True, False, 0, -1, 3.14, "hello", [1, 2], {"a": 1}):
            encoded = t2_json_dumps(value)
            assert t2_json_loads(encoded) == value

    def test_t2_json_round_trip_datetime(self) -> None:
        from nexus.daemon.t2_daemon import t2_json_dumps, t2_json_loads

        now = datetime.now(timezone.utc).replace(microsecond=123456)
        decoded = t2_json_loads(t2_json_dumps(now))
        assert decoded == now

    def test_t2_json_round_trip_bytes(self) -> None:
        from nexus.daemon.t2_daemon import t2_json_dumps, t2_json_loads

        blob = b"\x00\x01\x02\xfe\xff"
        assert t2_json_loads(t2_json_dumps(blob)) == blob

    def test_t2_json_round_trip_path(self) -> None:
        from nexus.daemon.t2_daemon import t2_json_dumps, t2_json_loads

        p = Path("/tmp/some/path.txt")
        decoded = t2_json_loads(t2_json_dumps(p))
        assert decoded == p

    def test_t2_json_dataclass_allowlist_enforced_on_decode(self) -> None:
        """Unknown __dataclass__ tag must raise ValueError (defence-
        in-depth against same-UID peer feeding arbitrary tagged dicts).
        """
        from nexus.daemon.t2_daemon import t2_json_loads

        forged = b'{"__dataclass__":"NotInAllowlist","fields":{}}'
        with pytest.raises(ValueError):
            t2_json_loads(forged)

    def test_t2_json_dataclass_allowlist_permits_known(self) -> None:
        """An allowlisted qualname decodes to a plain dict of fields."""
        from nexus.daemon.t2_daemon import t2_json_dumps, t2_json_loads

        # Mock a QueueRow-shaped dataclass; the encoder tags by qualname.
        # We can't easily inject this dataclass into the real allowlist,
        # but we can construct the wire form directly and assert decode.
        wire = b'{"__dataclass__":"QueueRow","fields":{"doc_id":"x","status":"failed"}}'
        decoded = t2_json_loads(wire)
        assert decoded == {"doc_id": "x", "status": "failed"}

    def test_write_frame_read_frame_round_trip(self) -> None:
        """Length-prefixed framing: writer + reader round-trip
        through an in-memory asyncio StreamReader/Writer pair."""
        from nexus.daemon.t2_daemon import write_frame, read_frame

        async def _run() -> dict:
            reader = asyncio.StreamReader()
            transport_buffer = bytearray()

            class _FakeWriter:
                def write(self, data: bytes) -> None:
                    transport_buffer.extend(data)

                async def drain(self) -> None: ...
                def close(self) -> None: ...
                async def wait_closed(self) -> None: ...

            write_frame(_FakeWriter(), {"op": "memory.put", "args": [1, 2]})
            reader.feed_data(bytes(transport_buffer))
            reader.feed_eof()
            return await read_frame(reader)

        decoded = asyncio.run(_run())
        assert decoded == {"op": "memory.put", "args": [1, 2]}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


class TestDispatchTable:
    def test_builds_from_stores_and_database_methods(self, db_path: Path) -> None:
        from nexus.daemon.t2_daemon import _build_dispatch_table
        from nexus.db.t2 import T2Database

        db = T2Database(db_path)
        try:
            table = _build_dispatch_table(db)
        finally:
            db.close()

        # Expect at least one op per documented store. Seven stores
        # at P3a; the catalog eighth store joins at P5.
        for store in (
            "memory", "plans", "chash_index", "taxonomy", "telemetry",
            "document_aspects", "aspect_queue",
        ):
            prefix = f"{store}."
            assert any(k.startswith(prefix) for k in table), (
                f"dispatch table missing any {store}.* ops; got "
                f"{sorted(k for k in table if k.startswith(prefix))!r}"
            )

        # Pseudo-store: at least the documented top-level method.
        assert "database.rename_collection_cascade" in table

    def test_denylist_filters_close_method(self, db_path: Path) -> None:
        """Per _RPC_DENY_METHODS: clients must not be able to call
        store.close() via RPC (would tear down the daemon's SQLite
        handle)."""
        from nexus.daemon.t2_daemon import _build_dispatch_table
        from nexus.db.t2 import T2Database

        db = T2Database(db_path)
        try:
            table = _build_dispatch_table(db)
        finally:
            db.close()

        for op in table:
            assert not op.endswith(".close"), (
                f"op {op!r} must be filtered by _RPC_DENY_METHODS"
            )

    def test_denylist_filters_per_op(self, db_path: Path) -> None:
        """Per _RPC_DENY_OPS: document_aspects upsert/get/get_by_doc_id
        and catalog @contextmanager methods are excluded."""
        from nexus.daemon.t2_daemon import _RPC_DENY_OPS, _build_dispatch_table
        from nexus.db.t2 import T2Database

        db = T2Database(db_path)
        try:
            table = _build_dispatch_table(db)
        finally:
            db.close()

        for op in _RPC_DENY_OPS:
            assert op not in table, f"denied op {op!r} leaked into dispatch table"

    def test_reclaim_stale_rpc_is_noop(self, db_path: Path) -> None:
        """nexus-xmohw: ``aspect_queue.reclaim_stale`` over RPC must be a
        cheap no-op returning 0 that NEVER touches the DB.

        Reclaim is daemon-owned (nexus-we61e: ``_reclaim_stale_loop``
        calls it directly on ``t2db``). No current worker RPCs it, but
        version-skewed workers (<=5.10.0, pre-we61e) still do every poll.
        Honouring each as a real full-table UPDATE+commit floods the
        write lock and pegs the daemon at 100% CPU -> slow ``hello()`` ->
        takeover churn -> multi-daemon -> T2 write failures (nexus-x47yx).
        The client-facing dispatch entry must therefore bypass the DB
        entirely.
        """
        from unittest.mock import MagicMock

        from nexus.daemon.t2_daemon import _build_dispatch_table
        from nexus.db.t2 import T2Database

        db = T2Database(db_path)
        try:
            # Spy on the real reclaim_stale: the RPC entry must NOT call it.
            real = MagicMock(return_value=99)
            db.aspect_queue.reclaim_stale = real  # type: ignore[method-assign]
            table = _build_dispatch_table(db)
            rpc_entry = table["aspect_queue.reclaim_stale"]
            result = rpc_entry(60)  # what a stale worker sends (timeout_seconds)
        finally:
            db.close()

        assert result == 0, "RPC reclaim_stale must return 0 (daemon-owned no-op)"
        real.assert_not_called()  # must not touch the DB / real reclaim


# ---------------------------------------------------------------------------
# Daemon lifecycle (real sockets, in-process)
# ---------------------------------------------------------------------------


def _run_daemon_in_thread(daemon, ready: threading.Event, stop_evt: threading.Event):
    """Helper: drive a T2Daemon under a private event loop in a
    background thread so the test can poke it via real sockets."""
    async def _main() -> None:
        await daemon.start()
        ready.set()
        # Poll for the cross-thread stop signal.
        while not stop_evt.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


class TestStartStopLifecycle:
    def test_start_writes_discovery_then_stop_cleans_up(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        from nexus.daemon.t2_daemon import T2Daemon, t2_discovery_path

        daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(
            target=_run_daemon_in_thread, args=(daemon, ready, stop),
        )
        thread.start()
        try:
            assert ready.wait(timeout=10.0), "daemon did not start within 10s"

            disc_path = t2_discovery_path(config_dir)
            assert disc_path.exists()
            # RDR-149 P2: the discovery file is now a lease record; the
            # connection fields live under ``endpoint`` and liveness is the
            # lease (generation + heartbeat TTL), not a top-level pid.
            payload = json.loads(disc_path.read_text())
            assert payload["format_version"] == 1
            assert payload["generation"] == 1
            assert payload["owner_token"]
            endpoint = payload["endpoint"]
            assert endpoint["pid"] == os.getpid()
            assert endpoint["tcp_host"] == "127.0.0.1"
            assert isinstance(endpoint["tcp_port"], int) and endpoint["tcp_port"] > 0
            assert endpoint["uds_path"].endswith("t2.sock")

            # UDS and TCP both reachable.
            uds = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            uds.connect(endpoint["uds_path"])
            uds.close()
            tcp = socket.create_connection(
                (endpoint["tcp_host"], endpoint["tcp_port"]), timeout=2.0,
            )
            tcp.close()
        finally:
            stop.set()
            thread.join(timeout=10.0)
            assert not thread.is_alive(), "daemon thread did not stop"

        # Discovery file removed; spawn-lock file remains as an
        # artefact but should be unlockable.
        assert not t2_discovery_path(config_dir).exists()

    def test_second_start_against_same_config_dir_fails_loud(
        self, config_dir: Path, db_path: Path, tmp_path: Path,
    ) -> None:
        """Spawn lock invariant: a second daemon against the same
        config_dir raises T2DaemonError; the first remains running.
        """
        from nexus.daemon.t2_daemon import T2Daemon, T2DaemonError

        first = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(
            target=_run_daemon_in_thread, args=(first, ready, stop),
        )
        thread.start()
        try:
            assert ready.wait(timeout=10.0)

            second = T2Daemon(
                config_dir=config_dir, db_path=tmp_path / "second.db",
            )
            with pytest.raises(T2DaemonError) as excinfo:
                asyncio.run(second.start())
            assert "spawn lock" in str(excinfo.value)
        finally:
            stop.set()
            thread.join(timeout=10.0)


# ---------------------------------------------------------------------------
# RDR-140 P1.1 (nexus-266iu): spawn-lock loser quiet-attach
# ---------------------------------------------------------------------------


class TestSpawnLockLoserQuietAttach:
    """A second daemon that loses the spawn lock must quiet-attach, not crash.

    Intended behaviour (implemented in P1.3 / nexus-h2oko), pinned RED here:

    - The losing process NEVER constructs ``T2Database`` (spy call count == 0;
      A1-verified that the spawn lock at start() is strictly before the
      T2Database open).
    - ``run_t2_daemon`` returns cleanly (exit code 0 — no exception escapes).
    - It logs ``t2_daemon_spawn_lost`` at info exactly once, and does NOT log
      ``t2_daemon_crashed`` (no traceback).

    Against current code this FAILS: the spawn-loss raises T2DaemonError which
    lands in ``run_t2_daemon``'s ``except Exception: _log.exception(
    "t2_daemon_crashed"); raise`` — a crash with traceback and non-zero exit.
    """

    def test_loser_quiet_attaches_without_constructing_t2db(
        self, config_dir: Path, db_path: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        import logging
        from unittest.mock import MagicMock

        import structlog

        import nexus.db.t2 as t2_module
        import nexus.logging_setup as logging_setup
        from nexus.daemon.t2_daemon import T2Daemon, run_t2_daemon
        from structlog.testing import capture_logs

        # conftest pins structlog to WARNING by default, which would filter the
        # info-level spawn_lost event before capture_logs sees it. Lower to INFO
        # for this test; the autouse _restore_structlog_after_test fixture
        # restores the prior config afterwards.
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )

        # Hold the spawn lock with a real first daemon.
        first = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(
            target=_run_daemon_in_thread, args=(first, ready, stop),
        )
        thread.start()
        try:
            assert ready.wait(timeout=10.0)

            # Spy on T2Database construction. The first daemon already opened
            # its DB before this patch, so any call now can only come from the
            # losing second daemon.
            t2db_spy = MagicMock(name="T2Database")
            monkeypatch.setattr(t2_module, "T2Database", t2db_spy)
            # Keep capture_logs' structlog config in place: run_t2_daemon's
            # configure_logging would otherwise reconfigure structlog away.
            monkeypatch.setattr(logging_setup, "configure_logging", lambda *a, **k: None)

            raised: Exception | None = None
            with capture_logs() as logs:
                try:
                    result = run_t2_daemon(
                        config_dir=config_dir, db_path=tmp_path / "second.db",
                    )
                except Exception as exc:  # noqa: BLE001
                    raised = exc
                    result = None

            # Clean quiet-attach: no exception escaped, returned None (exit 0).
            assert raised is None
            assert result is None

            # The loser never constructed T2Database.
            assert t2db_spy.call_count == 0

            spawn_lost = [e for e in logs if e.get("event") == "t2_daemon_spawn_lost"]
            crashed = [e for e in logs if e.get("event") == "t2_daemon_crashed"]
            assert len(spawn_lost) == 1
            assert spawn_lost[0].get("log_level") == "info"
            # The winner is alive and its UDS is accepting, so the loser's
            # poll must report a live attach (not a vacuous timeout).
            assert spawn_lost[0].get("attached") is True
            assert len(crashed) == 0
        finally:
            stop.set()
            thread.join(timeout=10.0)

    def test_reassert_task_cancelled_before_discovery_unlink(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        """RDR-129 ordering: stop() must cancel the self-healing re-assert task
        BEFORE unlinking the discovery file, else the task could resurrect a
        mid-shutdown daemon's addr. Pin the order by recording the relative
        sequence of Task.cancel and Path.unlink.
        """
        from nexus.daemon.t2_daemon import T2Daemon, t2_discovery_path

        events: list[str] = []

        async def _main() -> None:
            daemon = T2Daemon(config_dir=config_dir, db_path=db_path)
            await daemon.start()
            assert daemon._reassert_task is not None

            real_cancel = daemon._reassert_task.cancel
            disc_path = t2_discovery_path(config_dir)
            orig_unlink = Path.unlink

            def spy_cancel(*a, **k):
                events.append("cancel")
                return real_cancel(*a, **k)

            def spy_unlink(self_path, *a, **k):
                if self_path == disc_path:
                    events.append("unlink")
                return orig_unlink(self_path, *a, **k)

            daemon._reassert_task.cancel = spy_cancel  # type: ignore[method-assign]
            import unittest.mock as _mock

            with _mock.patch.object(Path, "unlink", spy_unlink):
                await daemon.stop()

        asyncio.run(_main())

        assert "cancel" in events, "re-assert task was never cancelled"
        assert "unlink" in events, "discovery file was never unlinked"
        assert events.index("cancel") < events.index("unlink")


# ---------------------------------------------------------------------------
# Module exposes the expected public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_t2daemon_public_methods(self) -> None:
        from nexus.daemon.t2_daemon import T2Daemon

        for name in ("start", "stop", "run_until_signal",
                     "uds_path", "tcp_host", "tcp_port", "discovery_path"):
            assert hasattr(T2Daemon, name), f"T2Daemon missing {name}"

    def test_run_t2_daemon_sync_entrypoint(self) -> None:
        from nexus.daemon import t2_daemon

        assert hasattr(t2_daemon, "run_t2_daemon")
        assert callable(t2_daemon.run_t2_daemon)

    def test_protocol_error_subclasses_exception(self) -> None:
        from nexus.daemon.t2_daemon import ProtocolError

        assert issubclass(ProtocolError, Exception)


# ---------------------------------------------------------------------------
# RDR-129 B2 (nexus-qi1zb): serving-dispatch lock retry
# ---------------------------------------------------------------------------


class TestDispatchLockRetry:
    """The serving ``_dispatch`` retries on transient WAL writer-lock
    contention so a window past the per-store busy_timeout becomes a wait, not
    a dropped best-effort write. Non-lock errors are not retried; the final
    attempt re-raises so a genuinely stuck lock still surfaces.
    """

    @staticmethod
    def _daemon(config_dir: Path, db_path: Path):
        from nexus.daemon.t2_daemon import T2Daemon

        return T2Daemon(config_dir=config_dir, db_path=db_path)

    @staticmethod
    def _frame(op: str = "probe") -> dict:
        return {"op": op, "args": [], "kwargs": {}}

    def test_retries_then_succeeds(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import sqlite3

        from nexus.daemon import t2_daemon as td

        monkeypatch.setattr(td, "_DISPATCH_RETRY_SLEEPS", (0.0, 0.0))
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        d = self._daemon(config_dir, db_path)
        d._dispatch_table = {"probe": flaky}
        result = asyncio.run(d._dispatch(self._frame(), is_uds=True))
        assert result == "ok"
        assert calls["n"] == 3  # two retries then success

    def test_exhausts_retries_then_raises(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import sqlite3

        from nexus.daemon import t2_daemon as td

        monkeypatch.setattr(td, "_DISPATCH_RETRY_SLEEPS", (0.0, 0.0))
        calls = {"n": 0}

        def always_locked(*_a, **_k):
            calls["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        d = self._daemon(config_dir, db_path)
        d._dispatch_table = {"probe": always_locked}
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            asyncio.run(d._dispatch(self._frame(), is_uds=True))
        assert calls["n"] == 3  # len(_DISPATCH_RETRY_SLEEPS) + 1

    def test_non_lock_error_not_retried(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import sqlite3

        from nexus.daemon import t2_daemon as td

        monkeypatch.setattr(td, "_DISPATCH_RETRY_SLEEPS", (0.0, 0.0))
        calls = {"n": 0}

        def bad_schema(*_a, **_k):
            calls["n"] += 1
            raise sqlite3.OperationalError("no such table: foo")

        d = self._daemon(config_dir, db_path)
        d._dispatch_table = {"probe": bad_schema}
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            asyncio.run(d._dispatch(self._frame(), is_uds=True))
        assert calls["n"] == 1  # structural error surfaces on the first attempt


class TestStopBoundedClose:
    """nexus-azsqe (RDR-129 A2 follow-up): a hung T2Database.close() must
    not wedge stop() open-ended."""

    def test_stop_bounds_hung_t2db_close(
        self, config_dir: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import nexus.daemon.t2_daemon as t2d

        # Small bound so the test is fast; the close blocks far longer.
        monkeypatch.setattr(t2d, "_DB_CLOSE_TIMEOUT", 0.2)
        daemon = t2d.T2Daemon(config_dir=config_dir, db_path=db_path)

        class _HungDB:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                time.sleep(2.0)  # stalled WAL checkpoint, longer than the bound
                self.closed = True

        daemon._t2db = _HungDB()
        # servers / discovery untouched (None) so stop() only exercises close.

        loop = asyncio.new_event_loop()
        try:
            start = time.monotonic()
            loop.run_until_complete(daemon.stop())
            elapsed = time.monotonic() - start
        finally:
            loop.close()

        # Guard fired at ~0.2s; WITHOUT the fix the synchronous close would
        # block the event loop the full 2s. Ceiling has margin for CI jitter
        # while still well below the 2s unguarded path.
        assert elapsed < 1.2, f"stop() not bounded — took {elapsed:.2f}s"
        assert daemon._t2db is None
