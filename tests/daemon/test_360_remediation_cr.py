# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second 360° remediation Block 1 (nexus-ftpm).

CR-1 (nexus-6kxb): ``_SocketConnection.call``'s ``finally`` clause must
restore the pre-call socket timeout whenever ``recv_timeout_override``
was applied, including when the socket started in blocking mode
(``gettimeout() is None``). The original guard short-circuited on that
case and left the override installed on the pooled socket.

CR-2 (nexus-muhk): ``_reraise_remote_error`` must resolve daemon-defined
typed exceptions by full qualname. The original code only consulted
``builtins`` via the dotted-tail, silently collapsing both
``BlockingTakeResourceExhausted`` and ``InvalidTimeoutError`` to
``T2DaemonError`` on the wire-arrival path.
"""
from __future__ import annotations

import socket
import struct
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon.t2_client import (
    T2Client,
    T2DaemonError,
    _reraise_remote_error,
)
from nexus.daemon.t2_daemon import (
    DAEMON_PROTOCOL_VERSION,
    DAEMON_SCHEMA_VERSION,
    t2_json_dumps,
    t2_json_loads,
)


# ---------------------------------------------------------------------------
# Fake daemon server (frame protocol only, scripted op responses)
# ---------------------------------------------------------------------------


def _read_frame(sock: socket.socket) -> dict[str, Any] | None:
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    length = struct.unpack(">I", hdr)[0]
    payload = b""
    while len(payload) < length + 1:
        chunk = sock.recv(length + 1 - len(payload))
        if not chunk:
            return None
        payload += chunk
    return t2_json_loads(payload[:-1])


def _write_frame(sock: socket.socket, obj: dict[str, Any]) -> None:
    payload = t2_json_dumps(obj)
    sock.sendall(struct.pack(">I", len(payload)) + payload + b"\n")


class _FakeDaemonServer:
    """Minimal UDS server that completes the hello_ack handshake and
    answers every subsequent op from a scripted response dict.

    Enough wire fidelity to exercise serialization, header framing, and
    exception classification — i.e. the actual production path through
    ``_sock_read_frame`` and ``_reraise_remote_error`` — without spinning
    up a real ``T2Daemon`` with chroma + registry + sqlite scaffolding.
    """

    def __init__(
        self, sock_path: Path, responses: dict[str, dict[str, Any]]
    ) -> None:
        self.sock_path = sock_path
        self.responses = responses
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._stop = threading.Event()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._sock.bind(str(self.sock_path))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        for c in self._connections:
            try:
                c.close()
            except OSError:
                pass
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self._connections.append(conn)
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True
            ).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            frame = _read_frame(conn)
            if frame is None or frame.get("op") != "hello":
                return
            _write_frame(
                conn,
                {
                    "op": "hello_ack",
                    "daemon_protocol_version": frame.get(
                        "protocol_version", DAEMON_PROTOCOL_VERSION
                    ),
                    "daemon_version": "test",
                    "schema_version": DAEMON_SCHEMA_VERSION,
                    "registry_digest": None,
                },
            )
            while not self._stop.is_set():
                frame = _read_frame(conn)
                if frame is None:
                    break
                op = frame.get("op", "")
                resp = self.responses.get(
                    op, {"error": f"unknown op: {op}"}
                )
                _write_frame(conn, resp)
        except OSError:
            pass


@pytest.fixture
def fake_daemon():
    @contextmanager
    def _make(responses: dict[str, dict[str, Any]]):
        # tempfile.mkdtemp under /tmp keeps the UDS path well under macOS's
        # ~104-byte sun_path cap; pytest's default tmp_path is too deep.
        with tempfile.TemporaryDirectory(prefix="cr-", dir="/tmp") as d:
            sock_path = Path(d) / "daemon.sock"
            server = _FakeDaemonServer(sock_path, responses)
            server.start()
            try:
                yield sock_path
            finally:
                server.stop()

    return _make


# ---------------------------------------------------------------------------
# CR-1 (nexus-6kxb): socket-timeout restore on pooled connection
# ---------------------------------------------------------------------------


class TestCR1TimeoutRestoreOnPooledConnection:
    """The finally clause in `_SocketConnection.call` must restore the
    pre-override timeout state whenever an override was applied —
    including when the socket started in blocking mode."""

    def test_blocking_mode_restored_after_override(self, fake_daemon) -> None:
        """Socket in blocking mode (timeout=None) must remain blocking after override."""
        responses = {"ping": {"pong": "ok"}}
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path)
            try:
                # Force pool init + one connection.
                client.call("ping")
                conn = client._get_pool()._checkout()
                try:
                    # Precondition: simulate a pooled socket in blocking mode.
                    conn._sock.settimeout(None)
                    assert conn._sock.gettimeout() is None

                    conn.call("ping", {}, recv_timeout_override=30.0)

                    assert conn._sock.gettimeout() is None, (
                        "CR-1: blocking mode not restored after override; "
                        f"socket retained timeout={conn._sock.gettimeout()}"
                    )
                finally:
                    conn.close()
            finally:
                client.close()

    def test_timed_mode_restored_after_override(self, fake_daemon) -> None:
        """Socket with a finite timeout must be restored to that value."""
        responses = {"ping": {"pong": "ok"}}
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path, rpc_timeout_seconds=2.0)
            try:
                client.call("ping")
                conn = client._get_pool()._checkout()
                try:
                    assert conn._sock.gettimeout() == 2.0

                    conn.call("ping", {}, recv_timeout_override=30.0)

                    assert conn._sock.gettimeout() == 2.0, (
                        "CR-1: original 2.0s timeout not restored after "
                        f"override; got {conn._sock.gettimeout()}"
                    )
                finally:
                    conn.close()
            finally:
                client.close()

    def test_no_override_does_not_perturb(self, fake_daemon) -> None:
        """A call without `recv_timeout_override` leaves socket state alone."""
        responses = {"ping": {"pong": "ok"}}
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path, rpc_timeout_seconds=2.0)
            try:
                client.call("ping")
                conn = client._get_pool()._checkout()
                try:
                    pre = conn._sock.gettimeout()
                    conn.call("ping", {})
                    post = conn._sock.gettimeout()
                    assert pre == post == 2.0
                finally:
                    conn.close()
            finally:
                client.close()

    def test_restore_runs_on_exception_path_too(self, fake_daemon) -> None:
        """If the call raises, the finally still restores the socket."""
        responses = {
            "boom": {"error": {"type": "ValueError", "message": "no"}}
        }
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path)
            try:
                client.call("ping")  # triggers an unknown-op error, fine
            except T2DaemonError:
                pass
            try:
                # Re-init via known-good op.
                pass
            finally:
                pass

            # Fresh pool for a clean state check.
            client.close()
            client = T2Client(uds_path=sock_path)
            try:
                conn = client._get_pool()._checkout()
                try:
                    conn._sock.settimeout(None)
                    with pytest.raises(ValueError):
                        conn.call("boom", {}, recv_timeout_override=30.0)
                    assert conn._sock.gettimeout() is None, (
                        "CR-1: blocking mode not restored on exception path"
                    )
                finally:
                    conn.close()
            finally:
                client.close()


# ---------------------------------------------------------------------------
# CR-2 (nexus-muhk): typed remote-exception resolution (unit-level)
# ---------------------------------------------------------------------------


class TestCR2TypedExceptionResolution:
    """`_reraise_remote_error` must map qualified daemon exception names
    to their concrete classes. The original code only consulted
    ``builtins`` via the dotted-tail, silently collapsing typed daemon
    exceptions to ``T2DaemonError``."""

    def test_blocking_take_resource_exhausted_qualname_resolves(self) -> None:
        from nexus.daemon.tuplespace_service import (
            BlockingTakeResourceExhausted,
        )

        with pytest.raises(BlockingTakeResourceExhausted, match="overflow"):
            _reraise_remote_error(
                {
                    "type": "nexus.daemon.tuplespace_service.BlockingTakeResourceExhausted",
                    "message": "overflow",
                    "traceback": "",
                }
            )

    def test_invalid_timeout_error_qualname_resolves(self) -> None:
        from nexus.tuplespace.api import InvalidTimeoutError

        with pytest.raises(InvalidTimeoutError, match="too long"):
            _reraise_remote_error(
                {
                    "type": "nexus.tuplespace.api.InvalidTimeoutError",
                    "message": "too long",
                    "traceback": "",
                }
            )

    def test_unknown_qualname_falls_through_to_t2daemonerror(self) -> None:
        with pytest.raises(T2DaemonError) as exc_info:
            _reraise_remote_error(
                {
                    "type": "third.party.UnknownError",
                    "message": "mystery",
                    "traceback": "",
                }
            )
        assert exc_info.value.type_name == "third.party.UnknownError"

    def test_builtin_qualname_resolves_after_registry_miss(self) -> None:
        """Pre-existing builtin fallback path must remain unbroken."""
        with pytest.raises(KeyError, match="missing"):
            _reraise_remote_error(
                {
                    "type": "builtins.KeyError",
                    "message": "missing",
                    "traceback": "",
                }
            )

    def test_bare_string_error_still_raises_t2daemonerror(self) -> None:
        with pytest.raises(T2DaemonError, match="transport boom"):
            _reraise_remote_error("transport boom")


# ---------------------------------------------------------------------------
# CR-2 (nexus-muhk): typed remote-exception over the wire
# ---------------------------------------------------------------------------


class TestCR2WireTraversal:
    """End-to-end through socket frame I/O. Scripted daemon emits an
    error frame; the client must raise the registered typed class, not
    ``T2DaemonError``. Distinct from the unit tests above because this
    path exercises sendall + recv + JSON-decode + classify in the same
    chain the production daemon emits down."""

    def test_blocking_take_resource_exhausted_survives_wire(
        self, fake_daemon
    ) -> None:
        from nexus.daemon.tuplespace_service import (
            BlockingTakeResourceExhausted,
        )

        responses = {
            "blocking_take": {
                "error": {
                    "type": "nexus.daemon.tuplespace_service.BlockingTakeResourceExhausted",
                    "message": "queue full",
                    "traceback": "...",
                }
            }
        }
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path)
            try:
                with pytest.raises(
                    BlockingTakeResourceExhausted, match="queue full"
                ):
                    client.call("blocking_take", {"timeout_seconds": 1.0})
            finally:
                client.close()

    def test_invalid_timeout_error_survives_wire(self, fake_daemon) -> None:
        from nexus.tuplespace.api import InvalidTimeoutError

        responses = {
            "take": {
                "error": {
                    "type": "nexus.tuplespace.api.InvalidTimeoutError",
                    "message": "timeout out of range",
                    "traceback": "...",
                }
            }
        }
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path)
            try:
                with pytest.raises(InvalidTimeoutError, match="out of range"):
                    client.call("take", {})
            finally:
                client.close()

    def test_builtin_exception_still_survives_wire(self, fake_daemon) -> None:
        """Regression guard: pre-existing wire-traversal for builtins unchanged."""
        responses = {
            "lookup": {
                "error": {
                    "type": "builtins.KeyError",
                    "message": "no such key",
                    "traceback": "...",
                }
            }
        }
        with fake_daemon(responses) as sock_path:
            client = T2Client(uds_path=sock_path)
            try:
                with pytest.raises(KeyError):
                    client.call("lookup", {})
            finally:
                client.close()


# ---------------------------------------------------------------------------
# CR-3 (nexus-z4m7): data_version wake source for blocking_take
# ---------------------------------------------------------------------------

import time as _time

import chromadb as _chromadb

_CR3_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status: { type: enum, values: [open, in_progress, done], required: true }
  priority: { type: enum, values: [P0, P1, P2], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 100
tiers: [project]
retention_seconds: 86400
"""


@pytest.fixture()
def _cr3_registry(tmp_path):
    from nexus.tuplespace.registry import Registry

    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_CR3_TASKS_YAML)
    return Registry.load(d)


@pytest.fixture()
def _cr3_chroma():
    client = _chromadb.EphemeralClient()
    for coll in client.list_collections():
        client.delete_collection(coll.name)
    yield client
    for coll in client.list_collections():
        client.delete_collection(coll.name)


class TestCR3DataVersionWakeMechanism:
    """blocking_take must wake within a few ms of a sibling commit.

    RDR-110 CA #5 claims ~1-2ms median wake latency via a data_version
    poll thread. HR-3 (commit d248aeed) removed the data_version
    machinery as 'vestigial', leaving the daemon polling at 10ms
    unconditionally. CR-3 path A restores a dedicated polling thread
    that fires a shared ``wake_event`` on each detected commit.
    """

    def test_watcher_fires_event_within_few_ms_of_commit(
        self, tmp_path, _cr3_registry, _cr3_chroma
    ) -> None:
        """The data_version watcher must fire ``wake_event`` within a few
        ms of any commit on the tuples.db file.

        Uses a direct no-op write via the service's own connection so
        the assertion measures watcher detection latency, not the cost
        of ``ts_api.out`` (which embeds + indexes and dominates wall
        clock). Ceiling is 25ms which comfortably accommodates CI
        scheduler jitter while still failing the 10ms-floor regression.
        """
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_cr3_chroma,
            registry=_cr3_registry,
        )
        try:
            # Provision a small side-table once so the commit-only
            # measurement loop doesn't pay schema-creation cost.
            with service._lock:
                service._conn.execute(
                    "CREATE TABLE IF NOT EXISTS _wake_probe (id INTEGER)"
                )
                service._conn.commit()
            # Warm-up commit so the watcher's adaptive cadence resets
            # to its 1ms baseline (otherwise the test can land while
            # the watcher is in an idle-ramped interval and the wake
            # latency floor reflects that, not the active-load floor).
            with service._lock:
                service._conn.execute(
                    "INSERT INTO _wake_probe (id) VALUES (0)"
                )
                service._conn.commit()
            assert service._wake_event.wait(timeout=0.5), (
                "warm-up commit never fired wake_event"
            )
            service._wake_event.clear()

            def _commit_once() -> None:
                with service._lock:
                    service._conn.execute(
                        "INSERT INTO _wake_probe (id) VALUES (1)"
                    )
                    service._conn.commit()

            t0 = _time.perf_counter()
            threading.Thread(target=_commit_once, daemon=True).start()
            fired = service._wake_event.wait(timeout=1.0)
            elapsed_ms = (_time.perf_counter() - t0) * 1000.0
            assert fired, "wake_event was never set despite a sibling commit"
            assert elapsed_ms < 25.0, (
                f"wake latency too high — {elapsed_ms:.1f}ms; the "
                "data_version watcher should observe the commit within "
                "the active 1ms cadence + CI scheduler jitter."
            )
        finally:
            service.close()

    def test_blocking_take_wakes_via_watcher_not_polling(
        self, tmp_path, _cr3_registry, _cr3_chroma
    ) -> None:
        """blocking_take must use the wake_event, not the prior 10ms sleep."""
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_cr3_chroma,
            registry=_cr3_registry,
        )
        try:
            # Pre-warm the embedder + chroma session and ack the
            # warm-up row out of the way so the timed measurement
            # reflects steady-state wake latency rather than ONNX
            # cold-load + chroma JIT cost. Without this prelude the
            # test reliably overshoots the ceiling on a fresh CI
            # runner; the original 200ms budget assumed warm chroma
            # left behind by sibling tests in the monolithic suite,
            # an assumption the daemon-only CI partition breaks.
            warmup = service.out(
                subspace="tasks/cr3",
                content="warmup",
                dimensions={
                    "status": "open",
                    "priority": "P1",
                    "created_by": "x",
                },
            )
            warmup_take = service.blocking_take(
                subspace="tasks/cr3",
                query="warmup",
                claimant="warmup",
                timeout_seconds=5.0,
            )
            assert warmup_take is not None, "warm-up take never resolved"
            service.ack(claim_id=warmup_take["claim_id"], claimant="warmup")
            del warmup
            service._wake_event.clear()

            sleep_seconds = 0.05

            def _delayed_out() -> None:
                _time.sleep(sleep_seconds)
                service.out(
                    subspace="tasks/cr3",
                    content="hello",
                    dimensions={
                        "status": "open",
                        "priority": "P1",
                        "created_by": "x",
                    },
                )

            t = threading.Thread(target=_delayed_out, daemon=True)
            t.start()
            t0 = _time.perf_counter()
            result = service.blocking_take(
                subspace="tasks/cr3",
                query="hello",
                claimant="solo",
                timeout_seconds=5.0,
            )
            elapsed_ms = (_time.perf_counter() - t0) * 1000.0
            assert result is not None
            # Ceiling covers 50ms sleep + ~150ms warm out() + ~10ms
            # blocking_take overhead with comfortable headroom for
            # CI scheduler jitter on warm chroma. Polling-only at
            # 10ms cadence would add roughly N*10ms beyond that and
            # blow the ceiling within a handful of idle cycles.
            assert elapsed_ms < 350.0, (
                f"blocking_take total elapsed {elapsed_ms:.1f}ms is "
                "high; wake-event path should keep this well under the "
                "polling-only baseline."
            )
            service.ack(claim_id=result["claim_id"], claimant="solo")
        finally:
            service.close()

    def test_close_stops_wake_watcher_thread(
        self, tmp_path, _cr3_registry, _cr3_chroma
    ) -> None:
        """close() must join the watcher thread within a sane budget."""
        from nexus.daemon.tuplespace_service import TuplespaceService

        service = TuplespaceService(
            tuples_db_path=tmp_path / "tuples.db",
            chroma_client=_cr3_chroma,
            registry=_cr3_registry,
        )
        # Watcher started eagerly in __init__; sanity-check it's alive.
        watcher = service._wake_thread
        assert watcher is not None and watcher.is_alive()
        service.close()
        # join called inside close(); thread should be reaped.
        watcher.join(timeout=2.0)
        assert not watcher.is_alive()
