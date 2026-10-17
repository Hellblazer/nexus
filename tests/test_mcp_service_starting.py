# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-5rs3i: a service that is configured but not answering is not an empty store.

A plugin MCP server started while the start-at-login service was still coming up answered
``search`` with ``No collections match corpus 'knowledge'``, which reads as "your notes are gone".
The collection listing swallowed the engine's 503 into ``[]`` (``HttpVectorClient.list_collections``
non-strict contract) and the MCP names cache then held that ``[]`` as a real, empty answer.

The names readers (``get_collection_names`` and the MCP corpus resolution) raise
``ServiceNotReadyError``; ``get_collection_row``, the identity read the write path, corpus
resolution and gc share, keeps its old contract and answers ``None`` on a failed listing. A failed
refresh is remembered for a few seconds so a burst of row lookups pays the gateway retries once.

These tests run the real vector client over a real socket (an in-process server that behaves like
the engine's ``/v1/vectors/stats`` while it is starting: 503, then 200), so the status the client
really sees, and the cache the tools really read, are what is tested. Only the endpoint resolver
and the gateway-retry sleep schedule are replaced, both nexus-owned module attributes.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from structlog.testing import capture_logs

import nexus.db.http_vector_client as hvc
import nexus.mcp_infra as mcp_infra
from nexus.mcp import core
from tests._epipe_server import (
    order_rst_before_the_first_write,
    record_transport_reasons,
    resetting_server,
)

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
    # No failure memory by default: the tests that flip the engine from 503 to 200 expect the
    # very next read to see it. The memo tests set a window.
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 0.0)
    mcp_infra.reset_singletons()
    mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
    try:
        yield _Engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        mcp_infra.reset_singletons()


def _assert_not_answering_message(text: str) -> None:
    """The local wording: no claim that the service is starting, no empty-store reading, no
    claim that a search ran, and the doctor pointer exactly once."""
    assert _NO_COLLECTIONS not in text
    assert "not answering" in text
    assert "This is not an empty store" in text
    assert "just after login or a restart" in text
    assert "retry in a few seconds" in text
    assert text.count("nx doctor") == 1
    assert "starting" not in text
    assert "nothing was searched" not in text


@pytest.mark.parametrize("status", [502, 503, 504])
def test_search_on_a_service_that_answers_not_ready_says_it_is_not_answering(engine, status):
    engine.stats_status = status
    _assert_not_answering_message(core._search_render("anything", corpus="knowledge"))
    # The retry schedule really ran: the empty answer was reached the long way round.
    assert engine.stats_hits > 1


def test_search_on_a_service_that_is_not_listening_yet_says_it_is_not_answering(monkeypatch):
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
        _assert_not_answering_message(core._search_render("anything", corpus="knowledge"))
    finally:
        mcp_infra.reset_singletons()


def test_search_on_a_service_that_resets_the_connection_says_it_is_not_answering(monkeypatch):
    """A connection dropped mid-request (the supervisor killed the JVM): the server accepts and
    closes with an RST, which the client sees as a ConnectionResetError."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def reset_every_connection():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()

    thread = threading.Thread(target=reset_every_connection, daemon=True)
    thread.start()
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{port}", "tok"))
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    monkeypatch.setattr(hvc, "_wait_for_lease_republication", lambda: None)
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 0.0)
    mcp_infra.reset_singletons()
    mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
    try:
        text = core._search_render("anything", corpus="knowledge")
        _assert_not_answering_message(text)
        assert "(connection reset)" in text
    finally:
        stop.set()
        thread.join(timeout=5)
        listener.close()
        mcp_infra.reset_singletons()


def test_search_on_a_service_that_breaks_the_pipe_says_it_is_not_answering(monkeypatch):
    """nexus-40fjk: a client that writes its request after the restarting service's RST gets EPIPE
    (BrokenPipeError), not ECONNRESET. Same restart, same text. tests/_epipe_server.py says how the
    EPIPE is pinned on macOS and Linux without sleeps or load."""
    with resetting_server() as server:
        order_rst_before_the_first_write(monkeypatch)
        reasons = record_transport_reasons(monkeypatch)
        monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (server.base_url, "tok"))
        monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
        monkeypatch.setattr(hvc, "_wait_for_lease_republication", lambda: None)
        monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 0.0)
        mcp_infra.reset_singletons()
        mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
        try:
            text = core._search_render("anything", corpus="knowledge")
            _assert_not_answering_message(text)
            assert "(connection reset)" in text
            # Non-vacuity: every failure the client classified was a write-side EPIPE.
            assert reasons and set(reasons) == {BrokenPipeError}, reasons
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
def test_every_collection_reading_tool_says_not_answering_not_empty(engine, call):
    engine.stats_status = 503
    _assert_not_answering_message(call())


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


def test_a_hard_engine_error_is_not_reported_as_not_answering(engine):
    """A 500 is a fault, not a startup; it keeps its own error text."""
    engine.stats_status = 500
    text = core._search_render("anything", corpus="knowledge")
    assert "not answering" not in text
    assert _NO_COLLECTIONS not in text
    assert "500" in text


def test_the_local_not_ready_text_names_the_status_and_the_remedies(engine):
    engine.stats_status = 503
    text = core._search_render("anything", corpus="knowledge")
    assert text == (
        "Error: The nexus service is not answering (HTTP 503). This is not an empty store. If "
        "this was just after login or a restart, retry in a few seconds; if it persists, run "
        "`nx doctor`."
    )


def test_a_managed_endpoint_gets_no_login_or_restart_wording(engine, monkeypatch):
    import nexus.config as config

    monkeypatch.setattr(config, "is_local_mode", lambda: False)
    engine.stats_status = 503
    text = core._search_render("anything", corpus="knowledge")
    assert text == (
        "Error: The managed nexus endpoint did not answer (HTTP 503). This is not an empty "
        "store. Retry shortly; if it persists, run `nx doctor`."
    )
    assert "login" not in text and "restart" not in text


def test_the_text_is_neutral_about_the_operation(engine):
    """A write tool or a listing reaches the same text; it must not claim a search ran."""
    engine.stats_status = 503
    for text in (core.collection_list(), core.store_list()):
        assert "search" not in text.lower()


def test_mcp_tool_error_renders_a_not_ready_error_as_its_own_text_alone():
    """Pins the ServiceNotReadyError branch of ``_mcp_tool_error``: without it the generic
    "may be unreachable" hint is appended after the text (a ConnectionError subclass would also
    get it), and the doctor pointer would be said twice."""

    class _NotReadyConnection(mcp_infra.ServiceNotReadyError, ConnectionError):
        """A not-ready error that also matches the generic connection-error test."""

    for err in (
        mcp_infra.ServiceNotReadyError("HTTP 503"),
        mcp_infra.ServiceNotReadyError("connection refused", cloud=True),
        _NotReadyConnection("connection refused"),
    ):
        out = core._mcp_tool_error("search", err)
        assert out == f"Error: {err}"
        assert "may be unreachable" not in out
        assert out.count("nx doctor") == 1


def test_a_not_ready_error_logs_one_warning_without_a_traceback():
    err = mcp_infra.ServiceNotReadyError("HTTP 503")
    with capture_logs() as logs:
        core._mcp_tool_error("search", err)
    assert [(e["log_level"], e["event"]) for e in logs] == [("warning", "mcp_search_service_not_ready")]
    assert "exc_info" not in logs[0]


def test_another_error_still_logs_an_error_with_a_traceback():
    with capture_logs() as logs:
        core._mcp_tool_error("search", RuntimeError("boom"))
    assert [(e["log_level"], e["event"]) for e in logs] == [("error", "mcp_search_failed")]
    assert logs[0]["exc_info"] is True


# ── nexus-5rs3i fix round: get_collection_row keeps its old contract ───────────────────────


_ROW_NAME = "code__nexus-1-1__voyage-code-3__v1"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_failed_listing_is_no_row_for_a_write_path_caller(engine, status):
    """``HttpVectorClient._resolve_collection_row`` is what the chunk cap and the upsert byte
    budget read before a write. A listing that failed (500, or not ready) has always meant "no
    row" there, so the write goes on with its name-parsed defaults; it must not raise."""
    engine.stats_status = status
    client = mcp_infra.get_t3()
    assert client._resolve_collection_row(_ROW_NAME) is None
    assert mcp_infra.get_collection_row(_ROW_NAME) is None


def test_a_refused_listing_is_no_row_for_get_collection_row(monkeypatch):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: (f"http://127.0.0.1:{port}", "tok"))
    monkeypatch.setattr(hvc, "_GATEWAY_RETRY_SLEEPS", (0.0, 0.0))
    monkeypatch.setattr(hvc, "_wait_for_lease_republication", lambda: None)
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 0.0)
    mcp_infra.reset_singletons()
    mcp_infra.inject_t3(hvc.HttpVectorClient(tenant=hvc._process_default_tenant()))
    try:
        assert mcp_infra.get_collection_row(_ROW_NAME) is None
    finally:
        mcp_infra.reset_singletons()


def test_get_collection_row_still_answers_from_a_healthy_listing(engine):
    engine.stats_rows = [{"name": _ROW_NAME, "count": 3, "content_type": "code", "owner_id": "o"}]
    row = mcp_infra.get_collection_row(_ROW_NAME)
    assert row is not None and row["content_type"] == "code"


_NAMES_READERS = [
    pytest.param(mcp_infra.get_collection_names, id="get_collection_names"),
    pytest.param(mcp_infra.get_live_collection_names, id="get_live_collection_names"),
    pytest.param(mcp_infra.get_collection_counts, id="get_collection_counts"),
]


@pytest.mark.parametrize("reader", _NAMES_READERS)
@pytest.mark.parametrize("status", [502, 503, 504])
def test_names_readers_raise_not_ready(engine, reader, status):
    engine.stats_status = status
    with pytest.raises(mcp_infra.ServiceNotReadyError):
        reader()


@pytest.mark.parametrize("reader", _NAMES_READERS)
def test_names_readers_raise_a_hard_error_rather_than_return_empty(engine, reader):
    engine.stats_status = 500
    with pytest.raises(hvc.VectorServiceError) as raised:
        reader()
    assert raised.value.code == 500


@pytest.mark.parametrize("status", [401, 422])
def test_a_listing_error_is_not_an_empty_store_for_the_names_readers(engine, status):
    engine.stats_status = status
    with pytest.raises(hvc.VectorServiceError):
        mcp_infra.get_collection_names()


# ── the failed-refresh memo ──────────────────────────────────────────────────────────────────


def test_a_not_ready_refresh_is_asked_once_for_calls_inside_the_memo_window(engine, monkeypatch):
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 60.0)
    engine.stats_status = 503

    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    after_first = engine.stats_hits
    assert after_first > 0  # the listing was really requested (with its gateway retries)

    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    assert engine.stats_hits == after_first  # the second call made no listing request

    # The names readers share the memo and raise the same not-ready error without a request.
    with pytest.raises(mcp_infra.ServiceNotReadyError):
        mcp_infra.get_collection_names()
    assert engine.stats_hits == after_first


def test_a_hard_error_refresh_is_also_asked_once_inside_the_window(engine, monkeypatch):
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 60.0)
    engine.stats_status = 500
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    after_first = engine.stats_hits
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    assert engine.stats_hits == after_first


def test_the_memo_expires_with_its_window(engine, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mcp_infra, "_now", lambda: clock[0])
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 5.0)
    engine.stats_status = 503
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    after_first = engine.stats_hits

    clock[0] += 4.0
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    assert engine.stats_hits == after_first

    clock[0] += 2.0  # 6 s since the failure: past the window
    engine.stats_status = 200
    engine.stats_rows = [{"name": _ROW_NAME, "count": 3, "content_type": "code", "owner_id": "o"}]
    assert mcp_infra.get_collection_row(_ROW_NAME) is not None
    assert engine.stats_hits > after_first


def test_a_cache_invalidation_forgets_the_remembered_failure(engine, monkeypatch):
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 60.0)
    engine.stats_status = 503
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    after_first = engine.stats_hits

    mcp_infra.invalidate_collections_cache()  # a committed write proves the service answered
    engine.stats_status = 200
    engine.stats_rows = [{"name": _ROW_NAME, "count": 3, "content_type": "code", "owner_id": "o"}]
    assert mcp_infra.get_collection_row(_ROW_NAME) is not None
    assert engine.stats_hits > after_first


def test_a_good_cache_install_forgets_the_remembered_failure(engine, monkeypatch):
    # A listing that succeeds elsewhere inside the window (prime_collections_cache, or a
    # concurrent refresh) must not leave a forced listing re-raising the stale failure.
    monkeypatch.setattr(mcp_infra, "_COLLECTIONS_REFRESH_FAILURE_MEMO", 60.0)
    engine.stats_status = 503
    assert mcp_infra.get_collection_row(_ROW_NAME) is None
    after_first = engine.stats_hits

    row = {"name": _ROW_NAME, "count": 3, "content_type": "code", "owner_id": "o"}
    mcp_infra.prime_collections_cache([row])
    engine.stats_status = 200
    engine.stats_rows = [row]
    assert mcp_infra.get_collection_counts(max_age=0) == {_ROW_NAME: 3}
    assert engine.stats_hits > after_first
