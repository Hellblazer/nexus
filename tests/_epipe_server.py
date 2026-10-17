# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real socket server that makes a client's request fail with EPIPE (nexus-40fjk).

Under load, a client that writes its request after a restarting service's RST gets
``BrokenPipeError`` (EPIPE), not ``ConnectionResetError``. A bare "accept, then close with an RST"
server produces whichever one the timing and the OS pick:

* macOS: a write on a socket that received an RST is always EPIPE (probed 8-wide, 24000 attempts,
  0 exceptions). But an RST that lands while the client is still inside ``connect()`` or the
  ``TCP_NODELAY`` ``setsockopt`` that ``HTTPConnection.connect`` runs after it makes that call fail
  instead: ``ConnectionResetError`` from ``connect()``, or ``OSError`` EINVAL from ``setsockopt``.
  Measured against the full client path at 8-wide, 4 to 9 of 40 runs failed that way.
* Linux: the first write (or read) after the RST returns the pending ECONNRESET, ONCE, and clears
  it; every later write is EPIPE. So a client whose first write lands after the RST sees
  ECONNRESET on Linux and EPIPE on macOS, and one whose write lands before it sees neither.

(A bare-socket probe on macOS and on Linux (chas). A body large enough to fail "on write" does not
help: the first write after the RST is what carries the error, and its kind is the OS's choice.)

To take the race out, :func:`order_rst_before_the_first_write` puts the server in hold mode and
wraps ``HTTPConnection.connect``. The server accepts but does not reset; the wrapper lets the real
connect finish completely, THEN has the server send the RST, waits (``select``, bounded, no sleep)
until the RST is readable, and reads once to take the pending ECONNRESET. After that the client's
first write is EPIPE on both OSes, from the real kernel, through the real ``urllib`` /
``http.client`` path, and no RST can land inside ``connect()``. The wrapper acts only on connections
to the server's own port. Without it the server is just a reset server, as in the ECONNRESET tests.
"""
from __future__ import annotations

import http.client
import json
import select
import socket
import struct
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

import nexus.db.http_vector_client as hvc


class ResettingServer:
    """Accepts connections; resets the first *reset_first* (all when None), answers the rest.

    A reset is ``SO_LINGER`` (on, 0) then ``close()``: the kernel sends an RST, not a FIN.
    An answered connection reads the request head and replies 200 with *payload* as JSON.
    """

    def __init__(self, reset_first: int | None = None, payload: object = None) -> None:
        self._reset_first = reset_first
        self._payload = [] if payload is None else payload
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self.port: int = self._listener.getsockname()[1]
        self.accepted = 0
        self.answered = 0
        self._hold = False
        self._held: dict[int, socket.socket | None] = {}  # client port -> connection to reset (None: answered)
        self._held_cv = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                continue
            self.accepted += 1
            reset = self._reset_first is None or self.accepted <= self._reset_first
            if self._hold:
                try:
                    client_port = conn.getpeername()[1]
                except OSError:  # the client is already gone: nobody will ask for this one
                    conn.close()
                    continue
                with self._held_cv:
                    self._held[client_port] = conn if reset else None
                    self._held_cv.notify_all()
            if reset:
                if not self._hold:
                    self._rst(conn)
                continue
            self._answer(conn)

    @staticmethod
    def _rst(conn: socket.socket) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    def hold_resets(self) -> None:
        """Accept but do not reset: each reset waits for :meth:`reset_held`. Call before any client connects."""
        self._hold = True

    def reset_held(self, client_port: int, timeout: float = 5.0) -> bool:
        """Send the RST for the connection whose client end is *client_port*, once the server has
        accepted it. True when it was reset, False when the server answers it instead. Matching on the
        port keeps one client's wait from taking another's connection."""
        with self._held_cv:
            if not self._held_cv.wait_for(lambda: client_port in self._held, timeout):
                raise TimeoutError(f"the server never accepted the connection from port {client_port}")
            conn = self._held.pop(client_port)
        if conn is None:
            return False
        self._rst(conn)
        return True

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            body = json.dumps(self._payload).encode()
            self.answered += 1  # counted before the reply, so a client that returns on it never beats the count
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()
        with self._held_cv:
            for held in self._held.values():
                if held is not None:
                    held.close()
            self._held.clear()


@contextmanager
def resetting_server(reset_first: int | None = None, payload: object = None) -> Iterator[ResettingServer]:
    server = ResettingServer(reset_first, payload)
    server._thread.start()
    try:
        yield server
    finally:
        server.close()


#: The EPIPE tests skip on Windows: a write after an RST is reported there as ``ConnectionAbortedError``
#: (WinError 10053) or ``ECONNRESET``, never as ``BrokenPipeError``, so there is no EPIPE to pin.
skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows reports a write after an RST as ConnectionAbortedError or ECONNRESET, not EPIPE",
)


def _rst_then_consume(server: ResettingServer, sock: socket.socket) -> None:
    """Send the held RST for the connection *sock* just made, and wait until the client has taken it.

    The wait is on the socket, not on time: it ends when the RST is readable, and fails the test if
    it never comes. The pending ECONNRESET is then read once, so the client's first write is EPIPE.
    """
    try:
        if not server.reset_held(sock.getsockname()[1]):
            return  # the server answers this one
    except TimeoutError as exc:
        raise AssertionError(str(exc)) from None
    readable, _, _ = select.select([sock], [], [], 5)
    assert readable, "the server's RST never arrived"
    try:
        sock.recv(1)  # takes the pending ECONNRESET: Linux clears it once, macOS raises it here too
    except OSError:
        pass


def order_rst_before_the_first_write(monkeypatch: pytest.MonkeyPatch, server: ResettingServer) -> None:
    """Make every ``http.client`` connection TO *server* that the server resets meet the RST before its first write.

    Pair with a :class:`ResettingServer` (``reset_first`` says which connections are reset). Only a
    connection to ``server.port`` is touched: a foreign connection made by the same process goes
    straight to the real connect, so it can neither wait on this server nor block in ``select``.
    """
    real_connect = http.client.HTTPConnection.connect
    server.hold_resets()

    def connect(self: http.client.HTTPConnection) -> None:
        real_connect(self)
        if self.port == server.port:
            assert self.sock is not None
            _rst_then_consume(server, self.sock)

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)


def order_rst_before_the_first_write_httpx(monkeypatch: pytest.MonkeyPatch, server: ResettingServer) -> None:
    """The same ordering for an ``httpx`` client (which connects through ``httpcore``, not ``http.client``)."""
    import httpcore._backends.sync as sync_backend

    real_connect_tcp = sync_backend.SyncBackend.connect_tcp
    server.hold_resets()

    def connect_tcp(self: object, host: str, port: int, *args: object, **kwargs: object) -> object:
        stream = real_connect_tcp(self, host, port, *args, **kwargs)  # type: ignore[arg-type]
        if port == server.port:
            _rst_then_consume(server, stream.get_extra_info("socket"))  # type: ignore[attr-defined]
        return stream

    monkeypatch.setattr(sync_backend.SyncBackend, "connect_tcp", connect_tcp)


def record_transport_reasons(monkeypatch: pytest.MonkeyPatch) -> list[type[BaseException]]:
    """Types of the transport errors the client's retry classifier was asked about.

    Non-vacuity for the EPIPE tests: the list must hold ``BrokenPipeError``, so a run in which the
    OS delivered ECONNRESET instead cannot pass for the wrong reason.
    """
    seen: list[type[BaseException]] = []
    real = hvc._is_retryable_endpoint_error

    def spy(exc: Exception) -> bool:
        reason = getattr(exc, "reason", exc)
        seen.append(type(reason))
        return real(exc)

    monkeypatch.setattr(hvc, "_is_retryable_endpoint_error", spy)
    return seen
