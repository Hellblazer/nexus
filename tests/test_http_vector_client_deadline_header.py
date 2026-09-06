# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8hdg9 phase 5: the upsert-chunks POST declares the client's embed
budget as ``X-Nexus-Request-Deadline-Ms``; the search path does not.

Asserted against a real local HTTP server (the header the SERVER received,
not a captured dict), following ``tests/db/test_http_client_keepalive.py``'s
``_request_once`` transmit-level pattern.
"""
from __future__ import annotations

import http.server
import threading

import pytest

from nexus.db import http_vector_client as hvc


@pytest.fixture
def server():
    seen: dict[str, dict[str, str]] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            seen[self.path] = dict(self.headers.items())
            payload = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a: object) -> None:
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd, seen
    httpd.shutdown()
    httpd.server_close()


def _point_client_at(server, monkeypatch) -> None:
    httpd, _ = server
    host, port = httpd.server_address[0], httpd.server_address[1]
    monkeypatch.setattr(
        hvc, "_resolve_endpoint", lambda: (f"http://{host}:{port}", "tok"),
    )


def test_upsert_post_carries_deadline_header(server, monkeypatch) -> None:
    _point_client_at(server, monkeypatch)
    _, seen = server

    out = hvc._request_once(
        "POST", "/v1/vectors/upsert-chunks", tenant="default", timeout=10,
        body={"ids": []},
    )

    assert out == {"ok": True}
    got = seen["/v1/vectors/upsert-chunks"]
    assert got.get(hvc._REQUEST_DEADLINE_HEADER) == str(hvc._UPSERT_CHUNKS_DEADLINE_MS)
    assert int(got[hvc._REQUEST_DEADLINE_HEADER]) < hvc._UPSERT_CHUNKS_TIMEOUT_S * 1000


def test_search_post_does_not_carry_deadline_header(server, monkeypatch) -> None:
    _point_client_at(server, monkeypatch)
    _, seen = server

    hvc._request_once(
        "POST", "/v1/vectors/search", tenant="default", timeout=10,
        body={"query": "x"},
    )

    got = seen["/v1/vectors/search"]
    assert hvc._REQUEST_DEADLINE_HEADER not in got
    assert hvc._request_deadline_ms_for("/v1/vectors/search") is None
