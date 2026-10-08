# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qjjlz review fixes: stale-peer retry safety (M1), gzip body handling
(M2) and the connection-lifecycle gaps the first test file left (M3).

Same method as ``test_http_vector_client_pooling``: the REAL transport against
real loopback servers on port 0. The servers here are bare sockets so a test
can say exactly what the peer does with a request it has fully read.
"""
from __future__ import annotations

import gzip
import json
import socket
import ssl
import threading
import urllib.error
import urllib.request

import pytest
from structlog.testing import capture_logs

from nexus.db import http_vector_client as hvc
from nexus.db import pooled_http
from tests._module_seam import module_time


@pytest.fixture(autouse=True)
def _fresh_pool():
    hvc.reset_connection_pool_for_tests()
    yield
    hvc.reset_connection_pool_for_tests()


class _RawServer:
    """Reads each request IN FULL (headers and body), records it, then asks
    ``respond(path, nth_on_conn)`` what to do: a ``bytes`` value is written to
    the socket verbatim (a full HTTP response), a one-tuple ``(bytes,)`` is
    written and then the connection is closed, ``None`` closes the connection
    without a byte. ``received`` is ``(connection index, path)`` per request
    read, which is what "how many times was this sent" means."""

    def __init__(self, respond) -> None:
        self._respond = respond
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.received: list[tuple[int, str]] = []
        self.connections = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _accept(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                idx = self.connections
            threading.Thread(target=self._serve, args=(c, idx), daemon=True).start()

    def _serve(self, c: socket.socket, idx: int) -> None:
        nth = 0
        buf = b""
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = c.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                head, _, rest = buf.partition(b"\r\n\r\n")
                lines = head.decode("latin-1").split("\r\n")
                path = lines[0].split(" ")[1]
                length = 0
                for ln in lines[1:]:
                    if ln.lower().startswith("content-length:"):
                        length = int(ln.split(":", 1)[1])
                while len(rest) < length:
                    chunk = c.recv(65536)
                    if not chunk:
                        return
                    rest += chunk
                buf = rest[length:]
                nth += 1
                with self._lock:
                    self.received.append((idx, path))
                answer = self._respond(path, nth)
                if answer is None:
                    return  # the whole request was read; the answer never comes
                if isinstance(answer, tuple):  # (bytes,): send them, then close
                    c.sendall(answer[0])
                    return
                c.sendall(answer)
        finally:
            c.close()

    def sent(self, path: str) -> list[int]:
        return [i for i, p in self.received if p == path]

    def close(self) -> None:
        self.sock.close()


def _response(
    body: bytes = b'{"ok": true}', *, status: str = "200 OK",
    encoding: str | None = None, length: int | None = None, close: bool = False,
) -> bytes:
    head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
    head += f"Content-Length: {len(body) if length is None else length}\r\n"
    if encoding:
        head += f"Content-Encoding: {encoding}\r\n"
    if close:
        head += "Connection: close\r\n"
    return head.encode() + b"\r\n" + body


@pytest.fixture
def raw_server(monkeypatch):
    servers: list[_RawServer] = []

    def _make(respond) -> _RawServer:
        srv = _RawServer(respond)
        servers.append(srv)
        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (srv.url, "tok"))
        return srv

    yield _make
    for srv in servers:
        srv.close()


@pytest.fixture
def closed_connections(monkeypatch) -> list:
    """Every connection the transport closes, in order. Pool occupancy alone
    cannot tell "closed" from "leaked": a connection never released is simply
    absent from the pool."""
    closed: list = []
    real = pooled_http._close_quietly

    def _spy(conn) -> None:
        closed.append(conn)
        real(conn)

    monkeypatch.setattr(pooled_http, "_close_quietly", _spy)
    return closed


def _get(path: str = "/v1/probe", *, timeout: int = 10):
    return hvc._request_once("GET", path, tenant="default", timeout=timeout, body=None)


def _post(path: str, body: dict | None = None, *, timeout: int = 10):
    return hvc._request_once("POST", path, tenant="default", timeout=timeout, body=body or {"n": 1})


# ── M1: the inner stale-peer retry vs non-idempotent routes ──────────────────

_OK = _response()


def _drop_after_first_request(path: str, nth: int):
    """Healthy for the first request on a connection; on every later request
    the server reads it in full and closes: the stale-peer shape, which from
    the client is indistinguishable from "executed, then died"."""
    return _OK if nth == 1 else None


@pytest.mark.parametrize("path", [
    "/v1/vectors/gc/quarantine-orphans",
    "/v1/vectors/gc/restore-rereferenced",
    "/v1/vectors/gc/expire-quarantine",
    "/v1/vectors/gc/quarantine-restore",
    "/v1/vectors/store-delete",
])
def test_a_non_idempotent_route_is_sent_exactly_once_by_the_transport(
    raw_server, path,
) -> None:
    """The server reads the whole POST, then drops the connection. For a
    sweep or a delete the transport must not replay it: the server may have
    run it. Exactly one send, and the failure surfaces."""
    srv = raw_server(lambda p, nth: None if p == path else _OK)
    _get("/v1/warm")  # leaves a reusable connection in the pool
    assert hvc.connection_pool_idle_count() == 1

    with pytest.raises(ConnectionError):
        _post(path)

    assert len(srv.sent(path)) == 1, f"{path} was sent {len(srv.sent(path))} times"


def test_a_non_idempotent_route_does_not_take_a_pooled_connection(raw_server) -> None:
    """Preferably never reuse a pooled connection for it at all: a fresh one
    cannot be stale. The warm connection stays pooled and untouched."""
    srv = raw_server(lambda p, nth: _OK)
    _get("/v1/warm")
    assert srv.connections == 1
    _post("/v1/vectors/gc/quarantine-orphans")
    assert srv.connections == 2
    assert hvc.connection_pool_idle_count() == 2  # both are reusable afterwards


def test_an_idempotent_route_is_replayed_once_on_a_fresh_connection_and_logged(
    raw_server,
) -> None:
    srv = raw_server(_drop_after_first_request)
    _get("/v1/warm")
    with capture_logs() as logs:
        out = _post("/v1/vectors/search")
    assert out == {"ok": True}
    conns = srv.sent("/v1/vectors/search")
    assert len(conns) == 2, conns
    assert conns[0] != conns[1], "the replay must go over a different connection"
    retries = [e for e in logs if e["event"] == "pooled_http_stale_retry"]
    assert len(retries) == 1, logs
    assert retries[0]["log_level"] == "warning"
    assert retries[0]["path"] == "/v1/vectors/search"
    assert retries[0]["method"] == "POST"


def test_the_stale_retry_budget_is_one_when_a_pooled_connection_meets_a_server_that_drops_everything(
    raw_server,
) -> None:
    """The reused connection fails, the one transparent retry's fresh
    connection fails too, and the error surfaces: two sends, never a loop."""
    srv = raw_server(lambda p, nth: None if p == "/v1/vectors/search" else _OK)
    _get("/v1/warm")
    with pytest.raises(ConnectionError):
        _post("/v1/vectors/search")
    assert len(srv.sent("/v1/vectors/search")) == 2


def test_a_stale_failure_after_the_quick_window_is_not_replayed(
    raw_server, monkeypatch,
) -> None:
    """Failing more than 5 s after the send means the server may have been
    working on it: replaying is the caller's retry policy, not ours."""
    clock = {"mono": 1_000.0, "wall": 1_700_000_000.0}
    proxy = module_time(monkeypatch, pooled_http)
    proxy.monotonic = lambda: clock["mono"]
    proxy.time = lambda: clock["wall"]

    def respond(path: str, nth: int):
        if nth == 1:
            return _OK
        clock["mono"] += 6.0  # the server "took" 6 s, then dropped it
        clock["wall"] += 6.0
        return None

    srv = raw_server(respond)
    _get("/v1/warm")
    with pytest.raises(ConnectionError):
        _post("/v1/vectors/search")
    assert len(srv.sent("/v1/vectors/search")) == 1


# ── M2: gzip bodies ──────────────────────────────────────────────────────────

_PAYLOAD = json.dumps({"ok": True, "rows": list(range(200))}).encode()


def test_a_valid_gzip_body_is_decoded(raw_server) -> None:
    raw_server(lambda p, nth: _response(gzip.compress(_PAYLOAD), encoding="gzip"))
    assert _get()["rows"][:3] == [0, 1, 2]


def test_a_truncated_gzip_200_is_a_transient_connection_error(
    raw_server, closed_connections,
) -> None:
    """Not a bare OSError outside the taxonomy: a ConnectionResetError, which
    ``_request``'s restart classifier retries and ``_post`` reframes."""
    cut = gzip.compress(_PAYLOAD)[:-12]
    raw_server(lambda p, nth: _response(cut, encoding="gzip"))
    with pytest.raises(ConnectionResetError) as ei:
        _get()
    assert hvc._is_retryable_endpoint_error(ei.value)
    # a peer that sent an undecodable body is not trusted with another request
    assert hvc.connection_pool_idle_count() == 0
    assert len(closed_connections) == 1


def test_an_undecodable_gzip_200_is_a_transient_connection_error(raw_server) -> None:
    raw_server(lambda p, nth: _response(b"this is not gzip at all", encoding="gzip"))
    with pytest.raises(ConnectionResetError):
        _get()


@pytest.mark.parametrize("status", ["200 OK", "204 No Content"])
def test_an_empty_gzip_encoded_body_is_not_an_error(raw_server, status) -> None:
    """Content-Encoding: gzip with a zero-length body (a 204, or a 200 with
    nothing to say) is an empty body, not corruption. 204 has no JSON, so the
    read side sees the empty string."""
    raw_server(lambda p, nth: _response(b"", status=status, encoding="gzip"))
    req = _open_raw()
    assert req.read() == b""
    assert req.status == int(status[:3])


def _open_raw():
    """The pooled opener against the resolved endpoint, below the JSON layer."""
    url, token = hvc._resolve_endpoint()
    request = urllib.request.Request(
        url + "/v1/probe", headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
    )
    return hvc._keepalive_opener().open(request, timeout=10)


def test_a_truncated_gzip_502_is_still_a_502(raw_server, monkeypatch) -> None:
    """A gateway error whose body is not decodable keeps its status: the
    gateway retry must see 502, not a bare OSError."""
    cut = gzip.compress(b"<html>bad gateway</html>")[:-8]
    srv = raw_server(lambda p, nth: _response(cut, status="502 Bad Gateway", encoding="gzip"))
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get()
    assert ei.value.code == 502
    assert ei.value.read() == cut  # raw bytes kept

    # and the gateway retry wrapper treats it as the 502 it is
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    module_time(monkeypatch, hvc).sleep = lambda _s: None
    before = len(srv.received)
    with pytest.raises(urllib.error.HTTPError):
        hvc._request("GET", "/v1/probe", tenant="default", timeout=10, body=None)
    assert len(srv.received) - before == 3  # 1 attempt + 2 scheduled retries


def test_a_decompression_bomb_is_refused(raw_server, monkeypatch) -> None:
    monkeypatch.setattr(pooled_http, "MAX_DECODED_BYTES", 64 * 1024)
    bomb = gzip.compress(b"\0" * (1024 * 1024))
    raw_server(lambda p, nth: _response(bomb, encoding="gzip"))
    with pytest.raises(urllib.error.URLError) as ei:
        _get()
    assert "exceeds" in str(ei.value.reason)


# ── M3: connection lifecycle gaps ────────────────────────────────────────────

def test_a_partial_200_body_is_a_transient_error_and_leaves_the_connection_unpooled(
    raw_server, closed_connections,
) -> None:
    """The server promises 1000 bytes, sends 11, and closes: the read fails
    mid-body. A ConnectionResetError (the restart classifier retries it), and
    that connection never goes back to the pool."""
    raw_server(lambda p, nth: (_response(b'{"ok": tru', length=1000),))
    with pytest.raises(ConnectionResetError) as ei:
        _get()
    assert hvc._is_retryable_endpoint_error(ei.value)
    assert hvc.connection_pool_idle_count() == 0
    assert len(closed_connections) == 1, "the broken connection was leaked, not closed"


def test_a_partial_502_body_keeps_its_status_and_the_connection_unpooled(
    raw_server, closed_connections,
) -> None:
    raw_server(lambda p, nth: (_response(b"<html>bad", status="502 Bad Gateway", length=1000),))
    with pytest.raises(urllib.error.HTTPError) as ei:
        _get()
    assert ei.value.code == 502
    assert hvc.connection_pool_idle_count() == 0
    assert len(closed_connections) == 1


def test_a_connection_idle_for_the_limit_is_discarded_before_checkout(
    raw_server, monkeypatch,
) -> None:
    clock = {"mono": 1_000.0, "wall": 1_700_000_000.0}
    proxy = module_time(monkeypatch, pooled_http)
    proxy.monotonic = lambda: clock["mono"]
    proxy.time = lambda: clock["wall"]
    srv = raw_server(lambda p, nth: _OK)
    _get()
    clock["mono"] += pooled_http.MAX_IDLE_SECONDS + 1
    clock["wall"] += pooled_http.MAX_IDLE_SECONDS + 1
    _get()
    assert srv.connections == 2


# ── L1 / L2: SSL context identity, fork-time locks ───────────────────────────

def test_the_shared_ssl_context_follows_a_replaced_default_context_factory(
    monkeypatch,
) -> None:
    """``ssl._create_default_https_context`` is the documented override point
    for a trust policy. A context built before the override must not keep
    serving after it."""
    first = pooled_http._shared_ssl_context()
    assert pooled_http._shared_ssl_context() is first  # cached while nothing changes

    custom = ssl.create_default_context()
    monkeypatch.setattr(ssl, "_create_default_https_context", lambda: custom)
    assert pooled_http._shared_ssl_context() is custom


def test_the_fork_child_hook_replaces_every_module_lock() -> None:
    """A thread that does not exist in the child may have held any module lock
    at fork time and will never release it; the child must not inherit one."""
    held_ssl = pooled_http._ssl_context_lock
    held_handlers = pooled_http._handler_classes_lock
    held_ssl.acquire()
    held_handlers.acquire()
    try:
        pooled_http._after_fork_in_child()
        assert pooled_http._ssl_context_lock is not held_ssl
        assert pooled_http._handler_classes_lock is not held_handlers
        assert pooled_http._ssl_context_lock.acquire(blocking=False)
        pooled_http._ssl_context_lock.release()
        assert pooled_http._handler_classes_lock.acquire(blocking=False)
        pooled_http._handler_classes_lock.release()
    finally:
        held_ssl.release()
        held_handlers.release()
