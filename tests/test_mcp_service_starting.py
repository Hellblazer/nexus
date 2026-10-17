# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-5rs3i: a service that is configured but not answering yet is not an empty store.

A plugin MCP server started while the start-at-login service was still coming up answered
``search`` with ``No collections match corpus 'knowledge'``, which reads as "your notes are gone".
The collection listing swallowed the engine's 503 into ``[]`` (``HttpVectorClient.list_collections``
non-strict contract) and the MCP names cache then held that ``[]`` as a real, empty answer.

These tests run the real vector client over a real socket (an in-process server that behaves like
the engine's ``/v1/vectors/stats`` while it is starting: 503, then 200), so the status the client
really sees, and the cache the tools really read, are what is tested. Only the endpoint resolver
and the gateway-retry sleep schedule are replaced, both nexus-owned module attributes.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import nexus.db.http_vector_client as hvc
import nexus.mcp_infra as mcp_infra
from nexus.mcp import core

_NO_COLLECTIONS = "No collections match corpus"


class _Engine(BaseHTTPRequestHandler):
    """The slice of the engine a collection listing reads."""

    stats_status = 200
    stats_rows: list[dict] = []
    stats_hits = 0

    def log_message(self, *args):  # silence
        pass

    def _serve(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path.startswith("/v1/vectors/stats"):
            type(self).stats_hits += 1
            status, payload = type(self).stats_status, type(self).stats_rows
            if status != 200:
                payload = {"error": "vector backend not serving yet"}
        else:
            status, payload = 404, {"error": "not found"}
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _serve
    do_POST = _serve


@pytest.fixture
def engine(monkeypatch):
    _Engine.stats_status = 200
    _Engine.stats_rows = []
    _Engine.stats_hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{server.server_port}", "tok"))
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    mcp_infra.reset_singletons()
    mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
    try:
        yield _Engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        mcp_infra.reset_singletons()


def _assert_starting_message(text: str) -> None:
    assert _NO_COLLECTIONS not in text
    assert "starting" in text
    assert "retry" in text.lower()


@pytest.mark.parametrize("status", [502, 503, 504])
def test_search_on_a_service_that_answers_not_ready_says_it_is_starting(engine, status):
    engine.stats_status = status
    _assert_starting_message(core._search_render("anything", corpus="knowledge"))
    # The retry schedule really ran: the empty answer was reached the long way round.
    assert engine.stats_hits > 1


def test_search_on_a_service_that_is_not_listening_yet_says_it_is_starting(monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here: connection refused
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{port}", "tok"))
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    monkeypatch.setattr(hvc, "_wait_for_lease_republication", lambda: None)
    mcp_infra.reset_singletons()
    mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
    try:
        _assert_starting_message(core._search_render("anything", corpus="knowledge"))
    finally:
        mcp_infra.reset_singletons()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: core._search_render("anything", corpus="knowledge"), id="search"),
        pytest.param(lambda: core.query("anything", corpus="knowledge"), id="query"),
        pytest.param(lambda: core.store_list(), id="store_list"),
        pytest.param(lambda: core.store_get("0" * 64), id="store_get"),
        pytest.param(lambda: core.collection_list(), id="collection_list"),
    ],
)
def test_every_collection_reading_tool_says_starting_not_empty(engine, call):
    engine.stats_status = 503
    _assert_starting_message(call())


def test_a_not_ready_listing_is_not_remembered_as_an_empty_store(engine):
    """The server's warm-up listing runs at start. It must not leave ``[]`` behind for the
    cache window: once the service answers, the next read sees the collections."""
    engine.stats_status = 503
    with pytest.raises(mcp_infra.ServiceNotReadyError):
        mcp_infra.get_collection_names()

    engine.stats_status = 200
    engine.stats_rows = [{"name": "knowledge__notes", "count": 3}]
    assert mcp_infra.get_collection_names() == ["knowledge__notes"]


def test_the_startup_warmup_failing_does_not_poison_the_names_cache(engine):
    engine.stats_status = 503
    mcp_infra.warm_search_path()  # best-effort: swallows its own step failures

    engine.stats_status = 200
    engine.stats_rows = [{"name": "knowledge__notes", "count": 3}]
    assert mcp_infra.get_collection_names() == ["knowledge__notes"]


def test_a_genuinely_empty_store_still_says_no_collections_match(engine):
    engine.stats_status = 200
    engine.stats_rows = []
    text = core._search_render("anything", corpus="knowledge")
    assert text == "No collections match corpus 'knowledge'"
    assert core.collection_list() == "No collections found."


def test_a_hard_engine_error_is_not_reported_as_starting(engine):
    """A 500 is a fault, not a startup; it keeps its own error text."""
    engine.stats_status = 500
    text = core._search_render("anything", corpus="knowledge")
    assert "starting" not in text
    assert _NO_COLLECTIONS not in text
    assert "500" in text


def test_the_starting_message_is_the_service_not_ready_error_text():
    err = mcp_infra.ServiceNotReadyError("detail")
    out = core._mcp_tool_error("search", err)
    _assert_starting_message(out)

