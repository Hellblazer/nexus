# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-tjyzn: the vector client asks for gzip and reads a gzipped answer.

The engine compresses a response of 1 KiB or more for a client that sends ``Accept-Encoding: gzip``
(``HttpUtil.send``, Java). This pins the client half over a REAL socket: an in-process HTTP server on
an ephemeral port that behaves like the engine (gzips above the threshold when asked, identity
otherwise), so the request header the client really sends and the bytes it really receives are what
is tested, for a 200 body and for an error body.
"""
from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import nexus.db.http_vector_client as hvc

_THRESHOLD = 1024


class _Engine(BaseHTTPRequestHandler):
    seen_accept_encoding: list[str | None] = []
    status = 200
    payload: object = {}

    def log_message(self, *args):  # silence
        pass

    def _serve(self):
        type(self).seen_accept_encoding.append(self.headers.get("Accept-Encoding"))
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps(type(self).payload).encode()
        encoding = None
        if len(body) >= _THRESHOLD and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body)
            encoding = "gzip"
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        if encoding:
            self.send_header("Content-Encoding", encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve
    do_POST = _serve


@pytest.fixture
def engine(monkeypatch):
    _Engine.seen_accept_encoding = []
    _Engine.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{server.server_port}", "tok"))
    try:
        yield _Engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _big() -> dict:
    return {"results": [{"id": f"id{i}", "content": "lorem ipsum " * 20, "distance": 0.1 * i} for i in range(40)]}


def test_the_client_sends_accept_encoding_gzip_and_reads_the_compressed_body(engine):
    engine.payload = _big()
    assert len(json.dumps(engine.payload)) > _THRESHOLD
    got = hvc._post("/v1/vectors/search", {"query": "q"})
    assert got == engine.payload
    assert engine.seen_accept_encoding == ["gzip"]


def test_an_identity_answer_is_read_unchanged(engine):
    engine.payload = {"ok": True}      # under the threshold: the engine sends identity
    assert hvc._get("/v1/vectors/count?collection=x") == {"ok": True}
    assert engine.seen_accept_encoding == ["gzip"]


def test_a_compressed_error_body_is_decoded_into_the_error_message(engine):
    engine.status = 422
    engine.payload = {"error": "field 'collections' is invalid: " + "x" * 3000}
    with pytest.raises(hvc.VectorServiceError) as exc:
        hvc._post("/v1/vectors/search", {"query": "q"})
    assert exc.value.code == 422
    assert "field 'collections' is invalid" in str(exc.value)
    assert exc.value.engine_body is not None and exc.value.engine_body["error"].startswith("field 'collections'")


def test_a_compressed_error_body_on_get_is_decoded_too(engine):
    engine.status = 404
    engine.payload = {"error": "not found: " + "y" * 3000}
    with pytest.raises(hvc.VectorServiceError) as exc:
        hvc._get("/v1/vectors/stats")
    assert exc.value.code == 404
    assert str(exc.value).startswith("GET /v1/vectors/stats")
    assert "not found: yyy" in str(exc.value)
