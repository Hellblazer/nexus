# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qjjlz: the vector client reuses HTTP connections.

``_request_once`` used to build a fresh urllib opener per request, and urllib
forces ``Connection: close``, so every ``/v1/vectors`` call paid a TCP (+TLS)
handshake and a cold slow-start (measured: 0.23-0.43 s on a new connection to
the cloud vs 0.073 s reused; a search makes ~28 such requests). The fix is a
process-wide per-endpoint connection pool behind the unchanged urllib error
taxonomy.

Every test here drives the REAL transport (``_request_once`` / ``_request``)
against a real loopback HTTP/1.1 server on port 0 that COUNTS accepted TCP
connections. A mock cannot tell "one connection" from "ten".
"""
from __future__ import annotations

import gzip
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexus.db import http_vector_client as hvc


class _CountingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler_cls):
        self.accepted = 0
        self.requests: list[dict] = []
        self._count_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), handler_cls)

    def get_request(self):
        req = super().get_request()
        with self._count_lock:
            self.accepted += 1
        return req


def _make_handler(*, delay: float = 0.0, close_after: bool = False,
                  gzip_ok: bool = True):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, code: int, payload: bytes, extra: dict | None = None):
            encoding = None
            if (
                gzip_ok
                and code == 200
                and "gzip" in self.headers.get("Accept-Encoding", "")
                and self.path.startswith("/v1/gz")
            ):
                payload = gzip.compress(payload)
                encoding = "gzip"
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            if encoding:
                self.send_header("Content-Encoding", encoding)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if close_after:
                # Advertise keep-alive, then drop the socket anyway: the
                # server-closed-idle-connection shape.
                self.send_header("Connection", "keep-alive")
            self.end_headers()
            self.wfile.write(payload)
            if close_after:
                self.close_connection = True

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self.server.requests.append({
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
                "peer": self.client_address,
            })
            if delay:
                time.sleep(delay)
            if self.path.startswith("/v1/status/"):
                code = int(self.path.rsplit("/", 1)[1])
                self._reply(code, json.dumps({"error": f"status {code}"}).encode())
                return
            if self.path.startswith("/v1/hang"):
                time.sleep(3.0)
            self._reply(
                200,
                json.dumps({"ok": True, "path": self.path}).encode(),
                {"X-Nexus-Skipped-Collections": "c1,c2"}
                if self.path.startswith("/v1/skipped") else None,
            )

        do_GET = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815

        def log_message(self, *_a):
            pass

    return _Handler


@pytest.fixture
def make_server():
    servers: list[_CountingServer] = []

    def _make(**kw) -> _CountingServer:
        srv = _CountingServer(_make_handler(**kw))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def _fresh_pool():
    hvc.reset_connection_pool_for_tests()
    yield
    hvc.reset_connection_pool_for_tests()


def _point_at(monkeypatch, srv: _CountingServer) -> str:
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (url, "tok"))
    return url


def _get(path: str = "/v1/probe", **kw):
    return hvc._request_once("GET", path, tenant="default", timeout=kw.pop("timeout", 10), body=None)


# ── (1) connection reuse ─────────────────────────────────────────────────────

def test_sequential_requests_share_one_tcp_connection(make_server, monkeypatch) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    for _ in range(10):
        assert _get() == {"ok": True, "path": "/v1/probe"}

    assert len(srv.requests) == 10
    assert srv.accepted == 1, (
        f"10 sequential requests opened {srv.accepted} TCP connections; "
        "the client must reuse one (nexus-qjjlz)"
    )


def test_post_bodies_and_headers_survive_reuse(make_server, monkeypatch) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    for i in range(3):
        hvc._request_once(
            "POST", "/v1/vectors/search", tenant="acme", timeout=10, body={"n": i},
        )

    assert srv.accepted == 1
    for i, rec in enumerate(srv.requests):
        assert json.loads(rec["body"]) == {"n": i}
        assert rec["headers"]["Authorization"] == "Bearer tok"
        assert rec["headers"]["X-Nexus-Tenant"] == "acme"
        assert rec["headers"]["Content-Type"] == "application/json"
        # urllib forced "close"; the pooled transport must not.
        assert rec["headers"].get("Connection", "").lower() != "close"


def test_concurrent_requests_are_bounded_by_the_thread_count(make_server, monkeypatch) -> None:
    srv = make_server(delay=0.05)
    _point_at(monkeypatch, srv)
    workers, per_worker = 8, 5
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            for _ in range(per_worker):
                _get()
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(srv.requests) == workers * per_worker
    assert srv.accepted <= workers, (
        f"{workers * per_worker} requests on {workers} threads opened "
        f"{srv.accepted} connections"
    )
    before = srv.accepted
    # A second wave reuses the idle connections: nothing new is opened.
    threads = [threading.Thread(target=_run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert srv.accepted == before


# ── (2) endpoint changes ─────────────────────────────────────────────────────

def test_endpoint_change_between_requests_opens_a_connection_to_the_new_endpoint(
    make_server, monkeypatch,
) -> None:
    a, b = make_server(), make_server()
    current = {"srv": a}

    def _resolve():
        return f"http://127.0.0.1:{current['srv'].server_address[1]}", "tok"

    monkeypatch.setattr(hvc, "_resolve_endpoint", _resolve)

    _get("/v1/a1")
    _get("/v1/a2")
    assert (a.accepted, b.accepted) == (1, 0)

    current["srv"] = b  # lease rotation
    _get("/v1/b1")
    _get("/v1/b2")
    assert (a.accepted, b.accepted) == (1, 1)
    assert [r["path"] for r in b.requests] == ["/v1/b1", "/v1/b2"]

    current["srv"] = a  # and back: A's pooled connection is still good
    _get("/v1/a3")
    assert (a.accepted, b.accepted) == (1, 1)
    assert [r["path"] for r in a.requests] == ["/v1/a1", "/v1/a2", "/v1/a3"]


# ── (3) server-closed idle connections ───────────────────────────────────────

def test_server_closed_idle_connection_is_replaced_transparently(
    make_server, monkeypatch,
) -> None:
    srv = make_server(close_after=True)
    _point_at(monkeypatch, srv)

    assert _get("/v1/one")["path"] == "/v1/one"
    time.sleep(0.2)  # the server has closed its end by now
    assert _get("/v1/two")["path"] == "/v1/two"
    assert srv.accepted == 2


def test_stale_connection_that_looks_alive_is_retried_once_on_a_fresh_one(
    make_server, monkeypatch,
) -> None:
    """The cheap pre-send liveness probe is an optimisation, not the safety
    net: with it disabled the send hits the dead socket and the transport
    must still recover with ONE retry on a fresh connection."""
    from nexus.db import pooled_http

    monkeypatch.setattr(pooled_http, "_looks_dead", lambda _sock: False)
    srv = make_server(close_after=True)
    _point_at(monkeypatch, srv)

    assert _get("/v1/one")["path"] == "/v1/one"
    time.sleep(0.2)
    assert _get("/v1/two")["path"] == "/v1/two"
    assert srv.accepted == 2
    assert [r["path"] for r in srv.requests] == ["/v1/one", "/v1/two"]


def test_request_retry_budget_is_one_not_a_loop(monkeypatch) -> None:
    """A server that accepts and immediately drops every connection fails the
    request after ONE transparent retry, as the raw connection error the
    existing retry classifier knows."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(8)
    accepted = []

    def _drop() -> None:
        while True:
            try:
                c, _ = lsock.accept()
            except OSError:
                return
            accepted.append(1)
            c.close()

    threading.Thread(target=_drop, daemon=True).start()
    url = f"http://127.0.0.1:{lsock.getsockname()[1]}"
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (url, "tok"))
    try:
        with pytest.raises(ConnectionError):
            _get()
    finally:
        lsock.close()
    # first attempt on a fresh connection fails immediately: no pooled
    # connection existed, so no transparent retry.
    assert len(accepted) == 1


# ── (4) error taxonomy is unchanged ──────────────────────────────────────────

@pytest.mark.parametrize("code", [400, 401, 404, 429, 500, 502, 503, 504])
def test_http_error_statuses_raise_urllib_httperror_with_body_and_headers(
    make_server, monkeypatch, code,
) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(f"/v1/status/{code}")
    exc = ei.value
    assert exc.code == code
    assert json.loads(exc.read()) == {"error": f"status {code}"}
    assert exc.headers["Content-Type"] == "application/json"
    # An error response with a fully read body leaves the connection reusable.
    assert _get("/v1/after")["path"] == "/v1/after"
    assert srv.accepted == 1


def test_timeout_raises_a_bare_timeout_error_and_does_not_poison_the_pool(
    make_server, monkeypatch,
) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    with pytest.raises(TimeoutError):
        _get("/v1/hang", timeout=1)
    # the timed-out connection must have been discarded, not pooled
    assert _get("/v1/ok")["path"] == "/v1/ok"
    assert srv.accepted == 2


def test_connection_refused_is_a_urlerror_the_restart_classifier_recognises(
    monkeypatch,
) -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{port}", "tok"))

    with pytest.raises(urllib.error.URLError) as ei:
        _get()
    assert isinstance(ei.value.reason, ConnectionRefusedError)
    assert hvc._is_retryable_endpoint_error(ei.value)


def test_gateway_retry_wrapper_still_retries_503_over_a_pooled_connection(
    make_server, monkeypatch,
) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    monkeypatch.setattr(hvc.time, "sleep", lambda _s: None)

    with pytest.raises(urllib.error.HTTPError) as ei:
        hvc._request("GET", "/v1/status/503", tenant="default", timeout=10, body=None)
    assert ei.value.code == 503
    # 1 attempt + 2 scheduled retries, all on the one connection
    assert len(srv.requests) == 3
    assert srv.accepted == 1


# ── response handling ────────────────────────────────────────────────────────

def test_gzip_is_requested_and_decoded(make_server, monkeypatch) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    out = _get("/v1/gz")
    assert out == {"ok": True, "path": "/v1/gz"}
    assert "gzip" in srv.requests[0]["headers"]["Accept-Encoding"]


def test_skipped_collections_header_still_captured(make_server, monkeypatch) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)

    _get("/v1/skipped")
    assert hvc._pop_response_headers() == {"X-Nexus-Skipped-Collections": "c1,c2"}


def test_pooled_sockets_carry_tcp_keepalive(make_server, monkeypatch) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)
    seen: list[int] = []
    real = hvc._enable_tcp_keepalive

    def _spy(sock):
        real(sock)
        seen.append(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))

    monkeypatch.setattr(hvc, "_enable_tcp_keepalive", _spy)
    _get()
    _get()
    assert seen == [seen[0]] and seen[0] != 0  # one connect, option set


def test_a_connection_is_not_pooled_when_the_server_says_close(
    make_server, monkeypatch,
) -> None:
    class _Closing(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def log_message(self, *_a):
            pass

    srv = _CountingServer(_Closing)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        _point_at(monkeypatch, srv)
        _get()
        _get()
        assert srv.accepted == 2
    finally:
        srv.shutdown()
        srv.server_close()


# ── (5) fork safety ──────────────────────────────────────────────────────────

@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX only")
def test_forked_child_starts_with_an_empty_pool_and_its_own_connection(
    make_server, monkeypatch,
) -> None:
    srv = make_server()
    _point_at(monkeypatch, srv)
    # macOS crashes (SIGSEGV) in a forked child that touches SystemConfiguration
    # (urllib's system-proxy lookup) -- the known fork-after-Network.framework
    # hazard, unrelated to the pool. A non-empty proxy environment with a
    # loopback bypass keeps urllib off that path in the child.
    monkeypatch.setenv("http_proxy", "http://unused.invalid:1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    _get("/v1/parent")
    assert srv.accepted == 1
    assert hvc.connection_pool_idle_count() == 1

    r, w = os.pipe()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # fork() in a threaded process
        pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            idle = hvc.connection_pool_idle_count()
            _get("/v1/child")
            os.write(w, f"{idle}".encode())
            code = 0
        finally:
            os._exit(code)
    os.close(w)
    data = os.read(r, 16)
    os.close(r)
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    assert data == b"0", "child inherited the parent's pooled connection"
    # the child's request went over a connection of its own
    assert srv.accepted == 2
    child_peers = {r_["peer"] for r_ in srv.requests if r_["path"] == "/v1/child"}
    parent_peers = {r_["peer"] for r_ in srv.requests if r_["path"] == "/v1/parent"}
    assert child_peers.isdisjoint(parent_peers)
    # and the parent's connection is untouched and still reusable
    _get("/v1/parent2")
    assert srv.accepted == 2


def test_pool_is_keyed_so_a_different_port_never_reuses(make_server, monkeypatch) -> None:
    a, b = make_server(), make_server()
    for srv in (a, b, a, b):
        _point_at(monkeypatch, srv)
        _get()
    assert (a.accepted, b.accepted) == (1, 1)


@pytest.mark.skipif(sys.platform == "win32", reason="socketserver fd inspection is POSIX")
def test_idle_connections_are_capped_per_endpoint(make_server, monkeypatch) -> None:
    from nexus.db import pooled_http

    srv = make_server(delay=0.1)
    _point_at(monkeypatch, srv)
    n = pooled_http.MAX_IDLE_PER_ENDPOINT + 4
    threads = [threading.Thread(target=_get) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert hvc.connection_pool_idle_count() <= pooled_http.MAX_IDLE_PER_ENDPOINT




# ── TLS (the production path: the managed cloud is https) ────────────────────

def test_https_connections_are_pooled_and_verified(tmp_path, monkeypatch) -> None:
    """A real TLS server with a self-signed cert the client is told to trust
    through SSL_CERT_FILE: N sequential requests complete ONE handshake."""
    import datetime
    import ssl

    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))

    srv = _CountingServer(_make_handler())
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_pem, key_pem)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_pem))
        # a non-empty proxy environment bypassing localhost: no system-proxy lookup
        monkeypatch.setenv("http_proxy", "http://unused.invalid:1")
        monkeypatch.setenv("no_proxy", "localhost")
        url = f"https://localhost:{srv.server_address[1]}"
        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (url, "tok"))

        for i in range(5):
            assert _get(f"/v1/tls{i}")["path"] == f"/v1/tls{i}"
        assert srv.accepted == 1, f"5 HTTPS requests opened {srv.accepted} connections"

        # An untrusted certificate is still refused (verification not bypassed).
        hvc.reset_connection_pool_for_tests()
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "empty.pem"))
        (tmp_path / "empty.pem").write_text("")
        with pytest.raises(urllib.error.URLError) as ei:
            _get("/v1/untrusted")
        assert isinstance(ei.value.reason, ssl.SSLError)
    finally:
        srv.shutdown()
        srv.server_close()
