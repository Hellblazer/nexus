"""BackgroundSearchTelemetry: the MCP search path's off-request telemetry writer (nexus-vpa9q).

A default MCP search paid 0.42-0.47 s for its POST /v1/telemetry/search/batch,
synchronously, on a fresh httpx.Client per call (measured 2026-10-09
01:07:42Z on the published 7.75.0 client). The sink takes the write off the
request path and sends it through the pooled shared T2 writer.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from nexus import mcp_infra
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore
from nexus.mcp import core
from nexus.search_telemetry_sink import BackgroundSearchTelemetry
from tests._module_seam import setattr_in

_ROW = ("2026-10-09T00:00:00+00:00", "h" * 64, "code__a", 5, 3, 0.2, 0.65)


class _GatedWriter:
    """Records batches; each write blocks until the gate opens."""

    def __init__(self, *, open_gate: bool = True) -> None:
        self.batches: list[list[tuple]] = []
        self.gate = threading.Event()
        if open_gate:
            self.gate.set()
        self.started = threading.Event()

    def __call__(self, rows: list[tuple]) -> None:
        self.started.set()
        assert self.gate.wait(10), "test gate never opened"
        self.batches.append(list(rows))


def test_log_returns_before_the_write_completes() -> None:
    writer = _GatedWriter(open_gate=False)
    sink = BackgroundSearchTelemetry(writer, max_pending=4)
    try:
        t0 = time.monotonic()
        assert sink.log_search_batch([_ROW]) == 1
        assert time.monotonic() - t0 < 0.5
        assert writer.started.wait(5)
        assert writer.batches == []  # still blocked: the caller did not wait
        writer.gate.set()
        assert sink.flush(5)
        assert writer.batches == [[_ROW]]
    finally:
        writer.gate.set()
        sink.close(5)


def test_batches_are_written_in_order() -> None:
    writer = _GatedWriter()
    sink = BackgroundSearchTelemetry(writer, max_pending=16)
    try:
        for i in range(5):
            sink.log_search_batch([(*_ROW[:3], i, i, None, None)])
        assert sink.flush(5)
        assert [b[0][3] for b in writer.batches] == [0, 1, 2, 3, 4]
    finally:
        sink.close(5)


def test_full_queue_drops_instead_of_blocking() -> None:
    writer = _GatedWriter(open_gate=False)
    sink = BackgroundSearchTelemetry(writer, max_pending=2)
    try:
        sink.log_search_batch([_ROW])
        assert writer.started.wait(5)  # first batch is in flight, queue empty
        assert sink.log_search_batch([_ROW]) == 1
        assert sink.log_search_batch([_ROW]) == 1
        t0 = time.monotonic()
        assert sink.log_search_batch([_ROW]) == 0  # queue holds 2: dropped
        assert time.monotonic() - t0 < 0.5
        assert sink.dropped == 1
        writer.gate.set()
        assert sink.flush(5)
        assert len(writer.batches) == 3
    finally:
        writer.gate.set()
        sink.close(5)


def test_write_failure_is_counted_and_the_worker_survives() -> None:
    calls: list[list[tuple]] = []

    def flaky(rows: list[tuple]) -> None:
        calls.append(rows)
        if len(calls) == 1:
            raise RuntimeError("engine down")

    sink = BackgroundSearchTelemetry(flaky, max_pending=4)
    try:
        sink.log_search_batch([_ROW])
        sink.log_search_batch([_ROW])
        assert sink.flush(5)
        assert len(calls) == 2
        assert sink.failed == 1
    finally:
        sink.close(5)


def test_empty_rows_write_nothing_and_start_no_thread() -> None:
    writer = _GatedWriter()
    sink = BackgroundSearchTelemetry(writer)
    assert sink.log_search_batch([]) == 0
    assert sink.flush(1)
    assert writer.batches == []
    assert sink._thread is None
    sink.close(1)


def test_close_drains_pending_then_refuses_new_rows() -> None:
    writer = _GatedWriter()
    sink = BackgroundSearchTelemetry(writer, max_pending=8)
    sink.log_search_batch([_ROW])
    sink.log_search_batch([_ROW])
    assert sink.close(5)
    assert len(writer.batches) == 2
    assert sink.log_search_batch([_ROW]) == 0
    assert sink.dropped == 1
    assert len(writer.batches) == 2


def test_close_is_bounded_when_a_write_hangs() -> None:
    writer = _GatedWriter(open_gate=False)
    sink = BackgroundSearchTelemetry(writer)
    sink.log_search_batch([_ROW])
    assert writer.started.wait(5)
    t0 = time.monotonic()
    assert sink.close(0.2) is False
    assert time.monotonic() - t0 < 2
    writer.gate.set()


def test_max_pending_must_be_positive() -> None:
    with pytest.raises(ValueError):
        BackgroundSearchTelemetry(lambda rows: None, max_pending=0)


def test_mcp_sink_writes_through_the_shared_t2_writer(monkeypatch) -> None:
    """The MCP sink routes each batch through ``t2_index_write`` (the pooled
    process-lifetime T2 client), never a per-call ``T2Database``."""
    written: list[list[tuple]] = []
    ops: list[str] = []

    class _Telemetry:
        def log_search_batch(self, rows):
            written.append(list(rows))
            return len(rows)

    class _DB:
        telemetry = _Telemetry()

    def _fake_write(fn, *, op="t2_write"):
        ops.append(op)
        return fn(_DB())

    monkeypatch.setattr(mcp_infra, "t2_index_write", _fake_write)
    mcp_infra.reset_search_telemetry_sink()
    try:
        sink = mcp_infra.search_telemetry_sink()
        assert mcp_infra.search_telemetry_sink() is sink
        sink.log_search_batch([_ROW])
        assert sink.flush(5)
        assert written == [[_ROW]]
        assert ops == ["search_telemetry"]
    finally:
        mcp_infra.reset_search_telemetry_sink()


def test_a_dead_worker_is_replaced_on_the_next_batch() -> None:
    """A BaseException in a write kills the worker; the next batch must
    start a new one instead of filling the queue behind a dead thread."""
    calls: list[list[tuple]] = []

    def dies_once(rows: list[tuple]) -> None:
        calls.append(rows)
        if len(calls) == 1:
            raise SystemExit("worker killed")

    sink = BackgroundSearchTelemetry(dies_once, max_pending=4)
    try:
        sink.log_search_batch([_ROW])
        first = sink._thread
        assert first is not None
        first.join(5)
        assert not first.is_alive()
        assert sink.log_search_batch([_ROW]) == 1
        assert sink.flush(5)
        assert len(calls) == 2
        assert sink._thread is not first
    finally:
        sink.close(5)


class _UnstartableThread(threading.Thread):
    def start(self) -> None:
        raise RuntimeError("can't create new thread at interpreter shutdown")


def test_a_worker_that_cannot_start_drops_the_batch(monkeypatch) -> None:
    # Only the sink module sees the fake Thread (tests/_module_seam.py).
    setattr_in(monkeypatch, "nexus.search_telemetry_sink", "threading.Thread", _UnstartableThread)
    sink = BackgroundSearchTelemetry(lambda rows: None)
    assert sink.log_search_batch([_ROW]) == 0
    assert sink.dropped == 1
    assert sink._thread is None
    assert sink.close(1) is True  # nothing started, nothing to join


def test_close_timeout_discards_the_backlog_of_an_abandoned_worker() -> None:
    writer = _GatedWriter(open_gate=False)
    sink = BackgroundSearchTelemetry(writer, max_pending=8)
    sink.log_search_batch([_ROW])
    assert writer.started.wait(5)
    sink.log_search_batch([_ROW])
    sink.log_search_batch([_ROW])
    assert sink.close(0.1) is False
    assert sink.dropped == 2
    writer.gate.set()
    sink._thread.join(5)
    assert not sink._thread.is_alive()
    assert len(writer.batches) == 1  # only the batch already in flight


# ── MCP wiring, driven through the real tools ─────────────────────────────────

_COLLECTION = "knowledge__vpa9q__minilm-l6-v2__v1"


class _FakeT3:
    _voyage_client = "fake"

    def search(self, query, collection_names, n_results=10, where=None, **_kw):
        return [
            {"id": f"chunk-{i}", "content": f"text {i}", "distance": 0.1 + 0.01 * i,
             "title": f"doc {i}", "chunk_text_hash": f"{i:064d}"}
            for i in range(3)
        ]


class _RecordingEngine:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.posts.append((request.url.path, body))
        return httpx.Response(200, json={"inserted": len(body.get("rows", []))})


@pytest.fixture
def mcp_wired(monkeypatch):
    """Real MCP tools, no taxonomy store, and the
    sink's shared writer pointed at a real HttpTelemetryStore over a
    recording transport. A row can reach the engine only through the sink."""
    engine = _RecordingEngine()
    store = HttpTelemetryStore(
        base_url="http://engine.invalid", _token="t",
        client=httpx.Client(transport=httpx.MockTransport(engine.handler)),
    )
    writer_threads: list[str] = []

    def _shared_write(fn, *, op="t2_write"):
        writer_threads.append(threading.current_thread().name)
        return fn(SimpleNamespace(telemetry=store))

    cfg = {"search": {}, "telemetry": {"search_enabled": True}}
    monkeypatch.setattr(mcp_infra, "t2_index_write", _shared_write)
    monkeypatch.setattr(core, "_get_t3", lambda: _FakeT3())
    monkeypatch.setattr(core, "_get_catalog", lambda **_kw: None)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3, **_kw: [_COLLECTION])
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: None)
    monkeypatch.setattr("nexus.config.load_config", lambda **_kw: cfg)
    monkeypatch.setattr("nexus.search_engine.load_config", lambda **_kw: cfg)
    mcp_infra.reset_search_telemetry_sink()
    yield SimpleNamespace(engine=engine, cfg=cfg, writer_threads=writer_threads)
    mcp_infra.reset_search_telemetry_sink()


@pytest.mark.parametrize("tool", ["search", "query"])
def test_mcp_tool_writes_telemetry_through_the_sink(mcp_wired, tool) -> None:
    if tool == "search":
        core.search("anything", limit=3)
    else:
        core.query("anything", limit=3)
    assert mcp_infra.search_telemetry_sink().flush(5)

    batches = [b for p, b in mcp_wired.engine.posts if p == "/v1/telemetry/search/batch"]
    assert len(batches) == 1, mcp_wired.engine.posts
    (row,) = batches[0]["rows"]
    assert row[2] == _COLLECTION
    assert mcp_wired.writer_threads == ["nexus-search-telemetry"]


@pytest.mark.parametrize("tool", ["search", "query"])
def test_mcp_tool_honours_the_search_telemetry_opt_out(mcp_wired, tool) -> None:
    mcp_wired.cfg["telemetry"]["search_enabled"] = False
    if tool == "search":
        core.search("anything", limit=3)
    else:
        core.query("anything", limit=3)
    assert mcp_infra.search_telemetry_sink().flush(5)
    assert mcp_wired.engine.posts == []
