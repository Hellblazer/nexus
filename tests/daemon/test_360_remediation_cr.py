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
