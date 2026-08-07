# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared in-process fake-HTTP-server scaffolding for the per-store T2 unit
test files (test-suite-compression P1b, nexus-test-cleanup 2026-08-05).

Every ``test_http_<store>.py`` file in this directory that pins an
``Http<Store>`` contract against a faithful in-process replica of the real
Java handler shape re-implemented the SAME four helper methods on its own
``BaseHTTPRequestHandler`` subclass:

  - ``_check_auth``: Bearer-token + ``X-Nexus-Tenant`` header check
    (401 on bad/missing bearer, 400 on missing tenant)
  - ``_send``: JSON response writer (with an optional 204-no-content path)
  - ``_read_body`` / ``_body``: request-body JSON reader
  - ``_qs`` / ``_params``: query-string dict reader

plus the same module-scoped ``fake_server`` fixture boilerplate (bind a free
port, start ``HTTPServer`` in a daemon thread, yield the base URL, shut down).

The per-endpoint routing (``do_GET``/``do_POST``/``do_DELETE`` bodies) is
genuinely store-specific — different paths, different payload shapes,
different in-memory state — and stays in each store's own test file. Only
the boilerplate above (identical across memory/telemetry/plan_library/
chash_index/scratch) is factored up here.

NOT shared with: ``test_http_taxonomy_store.py`` (its fake handler uses
different method names — ``_auth_ok``/``_json``/``_query_params`` — and
different auth semantics — no missing-tenant 400 — so forcing this base
class onto it would mean rewriting ~40 endpoint branches for a file whose
value is its 85 tests of genuine taxonomy algorithm coverage, not the
handler shape); ``test_http_centroid_store.py`` / ``test_http_aspects_stores.py``
(both already use the much lighter ``httpx.MockTransport`` pattern instead
of a real socket server — no comparable boilerplate to extract).
"""
from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


def free_port() -> int:
    """Bind on port 0 to obtain an OS-assigned free port, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeT2HandlerBase(BaseHTTPRequestHandler):
    """Shared auth/send/body/query-string plumbing for per-store fake T2
    HTTP handlers. Subclasses set the ``TOKEN`` class attribute and
    implement their own ``do_GET``/``do_POST``/``do_DELETE`` against their
    own endpoint paths and in-memory state — the request/response schema
    genuinely differs per store, so routing stays bespoke; only the
    boilerplate every original file duplicated verbatim is factored up
    here.
    """

    #: Set by each subclass to its module's expected bearer token.
    TOKEN: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 — matches base signature
        pass  # suppress request-log noise in test output

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        tenant = self.headers.get("X-Nexus-Tenant", "")
        if auth != f"Bearer {self.TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return False
        if not tenant:
            self._send(400, {"error": "missing X-Nexus-Tenant header"})
            return False
        return True

    def _send(self, status: int, body: Any, no_content: bool = False) -> None:
        self.send_response(status)
        if no_content:
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    # Alias: telemetry/plan_library/chash_index name this method ``_body``.
    _body = _read_body

    def _qs(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return {k: v[0] for k, v in qs.items()}

    # Alias: memory names this method ``_params``.
    _params = _qs


@contextmanager
def fake_http_server(handler_cls: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    """Start ``handler_cls`` on a free port in a daemon thread; yield its
    base URL; shut it down on exit. Replaces the ~15-line bind/start/yield/
    shutdown block every ``fake_server`` fixture in this cluster repeated.
    """
    port = free_port()
    server = HTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
