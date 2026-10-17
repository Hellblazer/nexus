# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real socket server that makes a client's request fail with EPIPE, deterministically (nexus-40fjk).

Under load, a client that writes its request after a restarting service's RST gets
``BrokenPipeError`` (EPIPE), not ``ConnectionResetError``. A bare "accept, then close with an RST"
server produces whichever one the timing and the OS pick:

* macOS: a write on a socket that received an RST is always EPIPE.
* Linux: the first write (or read) after the RST returns the pending ECONNRESET, ONCE, and clears
  it; every later write is EPIPE. So a client whose first write lands after the RST sees
  ECONNRESET on Linux and EPIPE on macOS, and one whose write lands before it sees neither.

(Measured 2026-10-17 with a bare-socket probe on macOS and on Linux (chas). A body large enough to
fail "on write" does not help: the first write after the RST is what carries the error, and its
kind is the OS's choice.)

To pin EPIPE on both, :func:`order_rst_before_the_first_write` wraps ``HTTPConnection.connect``
and, once the real connect is done, waits (``select``, bounded, no sleep) until the server's RST
has arrived, then reads once to take the pending ECONNRESET. After that, the client's first write
is EPIPE on both OSes, from the real kernel, through the real ``urllib`` / ``http.client`` path.
Without the wrapper the server is just a reset server, as in the ECONNRESET tests.
"""
from __future__ import annotations

import http.client
import json
import select
import socket
import struct
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
            if self._reset_first is None or self.accepted <= self._reset_first:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()
                continue
            self._answer(conn)

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
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            self.answered += 1

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._listener.close()


@contextmanager
def resetting_server(reset_first: int | None = None, payload: object = None) -> Iterator[ResettingServer]:
    server = ResettingServer(reset_first, payload)
    server._thread.start()
    try:
        yield server
    finally:
        server.close()


def order_rst_before_the_first_write(monkeypatch: pytest.MonkeyPatch, connections: int | None = None) -> None:
    """Make the next *connections* (all when None) client connections fail their first write with EPIPE.

    Pair with a :class:`ResettingServer` that resets the same connections. The wait is on the
    socket, not on time: it ends when the RST is readable, and fails the test if it never comes.
    """
    real_connect = http.client.HTTPConnection.connect
    seen = {"n": 0}
    lock = threading.Lock()

    def connect(self: http.client.HTTPConnection) -> None:
        real_connect(self)
        with lock:
            seen["n"] += 1
            armed = connections is None or seen["n"] <= connections
        if not armed:
            return
        sock = self.sock
        assert sock is not None
        readable, _, _ = select.select([sock], [], [], 5)
        assert readable, "the server's RST never arrived"
        try:
            sock.recv(1)  # takes the pending ECONNRESET: Linux clears it once, macOS raises it here too
        except ConnectionResetError:
            pass

    monkeypatch.setattr(http.client.HTTPConnection, "connect", connect)


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
