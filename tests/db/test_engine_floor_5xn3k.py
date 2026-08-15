# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-5xn3k.3: RUNFENCE engine-floor tolerance.

Design memo (T2 nexus "5xn3k-design-2026-08-02") §6: "the client ships
WITHOUT waiting on REQUIRED_ENGINE_VERSION. A pre-fence engine 404s the
index-run routes and omits the fence fields; the client treats both as
'unknown' and falls through to verification (today's post-AC2 behaviour)."

A pre-fence engine (one built before catalog-020/021 landed) has no
``case "/index-run/..." -> ...`` / ``case "/manifest/verify..." -> ...`` in
CatalogHandler's dispatch switch, so every RUNFENCE route falls to the
generic ``default -> 404 "not found: <op>"``. This test drives a REAL
``HttpCatalogClient`` against a fake server that emulates exactly that —
pinning the ship order: the client half (.3) must run correctly against an
engine that has never heard of the fence, with the floor bump riding the
NEXT engine tag rather than gating this arc.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
import structlog

from nexus.catalog.http_catalog_client import HttpCatalogClient


class _PreFenceEngineHandler(BaseHTTPRequestHandler):
    """Emulates an engine binary built before catalog-020/021: EVERY
    RUNFENCE route 404s, mirroring CatalogHandler's real
    ``default -> HttpUtil.send(exchange, 404, "not found: " + op)`` switch
    fallthrough for an op its dispatch table doesn't recognize."""

    def log_message(self, *args: object) -> None:
        pass  # suppress test noise

    def _404(self) -> None:
        body = b'{"error":"not found: unknown op"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._404()

    def do_POST(self) -> None:
        self._404()


@pytest.fixture()
def pre_fence_client():
    server = HTTPServer(("127.0.0.1", 0), _PreFenceEngineHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    client = HttpCatalogClient(base_url=f"http://{host}:{port}", _token="tok")
    try:
        yield client
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=5)


def _configure_structlog_to_stdlib() -> None:
    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


# ── begin/complete/fail: advisory writes, 404 is SWALLOWED ──────────────────


def test_begin_index_run_404_is_swallowed_and_logged(pre_fence_client, caplog) -> None:
    """This is THE pinned scenario the bead names explicitly: a pre-fence
    engine 404s /index-run/begin; the client treats it as 'unknown' and
    falls through — i.e. it does NOT raise, so the caller's indexing run
    proceeds unaffected by an engine that has never heard of the fence."""
    _configure_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING, logger="nexus.catalog.http_catalog_client"):
        pre_fence_client.begin_index_run("1.1.1", "abc123", "run-1", "docs__o__v1")
    assert any(r.msg == "index_run_begin_engine_floor" for r in caplog.records)


def test_complete_index_run_404_is_swallowed_returns_none(pre_fence_client, caplog) -> None:
    """nexus-5xn3k.3 review: None, NOT {} — a caller bracketing an index run
    (.4) must be able to tell "never reached a fence-aware engine" (None)
    apart from "the engine answered with an empty body" ({})."""
    _configure_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING, logger="nexus.catalog.http_catalog_client"):
        result = pre_fence_client.complete_index_run("1.1.1", "abc123", 3)
    assert result is None
    assert any(r.msg == "index_run_complete_engine_floor" for r in caplog.records)


def test_fail_index_run_404_is_swallowed(pre_fence_client, caplog) -> None:
    _configure_structlog_to_stdlib()
    with caplog.at_level(logging.WARNING, logger="nexus.catalog.http_catalog_client"):
        pre_fence_client.fail_index_run("1.1.1", "boom")  # must not raise
    assert any(r.msg == "index_run_fail_engine_floor" for r in caplog.records)


# RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: manifest_verify/
# manifest_verify_all's client methods are RETIRED — the manifest-chunk FK
# makes the dangling state they diagnosed unreachable. test_manifest_verify_
# 404_propagates and test_manifest_verify_all_404_propagates DELETED
# (both called the now-removed methods directly).


def test_staleness_gate_returns_true_without_any_engine_call(
    pre_fence_client, monkeypatch,
) -> None:
    """RDR-191 Phase 6 rebase: the staleness gate's fallback,
    ``_manifest_is_fully_present``, no longer calls ``manifest_verify`` at
    all — the manifest-chunk FK makes the ``missing`` question it asked
    provably always False for any manifest row that exists, so the function
    is a bare ``return True`` now (see its own docstring for the full FK
    argument). This test pins that: even against a server that 404s EVERY
    route (the pre-fence emulator this module's other tests use for a
    genuinely different purpose — 404 SWALLOWING on the advisory
    begin/complete/fail writes), the staleness gate returns True WITHOUT
    ever making a request that could 404, and logs nothing (the old
    fail-open+WARNING contract had a real failure path to warn about; this
    one has none)."""
    _configure_structlog_to_stdlib()
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader",
        lambda *a, **k: pre_fence_client, raising=False,
    )
    from nexus.doc_indexer import _manifest_is_fully_present

    assert _manifest_is_fully_present(object(), "1.1.1") is True
