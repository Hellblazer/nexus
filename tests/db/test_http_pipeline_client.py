# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for HttpPipelineDB (RDR-186 nexus-146xx.16, client half).

httpx.MockTransport idiom. Pins the CLIENT contract only (server semantics
live in the Java PipelineHandlerTest):

- write buffering with READ-YOUR-WRITES flushing (the chattiness design:
  the chunker's poll both batches the extractor's pages onto the wire and
  observes them)
- batch-size eager flush (PAGE_FLUSH_BATCH / CHUNK_FLUSH_BATCH)
- progress coalescing (latest value per field, riding the flush) + eager
  standalone progress
- flush-failure buffer restoration (transient engine error loses nothing)
- embedding tri-state wire mapping (None/b""/bytes ↔ null/""/base64)
- the orphan scan's client half (path existence checked locally)
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from nexus.db.http_pipeline_client import (
    CHUNK_FLUSH_BATCH,
    PAGE_FLUSH_BATCH,
    HttpPipelineDB,
)

TOKEN = "fake-pipeline-token"
HASH = "c" * 32


class _Server:
    """Scriptable fake for the /v1/pipeline surface; records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.fail_next_post = False
        self.pages_response: list[dict] = []
        self.chunks_response: list[dict] = []
        self.pipelines_response: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else dict(request.url.params)
        self.requests.append((path, body))
        if request.method == "POST" and self.fail_next_post:
            self.fail_next_post = False
            return httpx.Response(500, json={"error": "transient"})
        return {
            "/v1/pipeline/create": lambda: httpx.Response(200, json={"status": "created"}),
            "/v1/pipeline/state": lambda: httpx.Response(200, json={"pipeline": None}),
            "/v1/pipeline/list": lambda: httpx.Response(200, json={"pipelines": self.pipelines_response}),
            "/v1/pipeline/pages": lambda: httpx.Response(
                200, json={"pages": self.pages_response} if request.method == "GET"
                else {"written": len(body.get("pages", []))}),
            "/v1/pipeline/chunks": lambda: httpx.Response(
                200, json={"chunks": self.chunks_response} if request.method == "GET"
                else {"inserted": len(body.get("chunks", []))}),
            "/v1/pipeline/progress": lambda: httpx.Response(200, json={"updated": True}),
            "/v1/pipeline/extraction_meta": lambda: httpx.Response(200, json={"updated": True}),
            "/v1/pipeline/complete": lambda: httpx.Response(200, json={"updated": True}),
            "/v1/pipeline/fail": lambda: httpx.Response(200, json={"updated": True}),
            "/v1/pipeline/mark_uploaded": lambda: httpx.Response(200, json={"updated": 1}),
            "/v1/pipeline/counts": lambda: httpx.Response(
                200, json={"embedded_chunks": 2, "pipelines": 1}),
            "/v1/pipeline/clear_wal": lambda: httpx.Response(200, json={"cleared": True}),
            "/v1/pipeline/delete": lambda: httpx.Response(200, json={"deleted": True}),
            "/v1/pipeline/delete_collection": lambda: httpx.Response(200, json={"deleted": 2}),
        }[path]()

    def posts(self, path: str) -> list[dict]:
        return [b for p, b in self.requests if p == path and "pages" in b or p == path and "pages" not in b][:]

    def calls(self, path: str) -> list[dict]:
        return [b for p, b in self.requests if p == path]


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def db(server: _Server) -> HttpPipelineDB:
    store = HttpPipelineDB(base_url="http://svc", _token=TOKEN)
    store._client = httpx.Client(transport=httpx.MockTransport(server.handler))
    return store


def test_pages_buffer_and_flush_on_read(db: HttpPipelineDB, server: _Server) -> None:
    """The stage-coupling contract: per-page writes make NO HTTP calls; the
    chunker's read flushes them as ONE batch then reads."""
    for i in range(5):
        db.write_page(HASH, i, f"page {i}")
    assert server.calls("/v1/pipeline/pages") == [], "writes buffered, zero HTTP"

    db.read_pages_from(HASH, 0)

    page_posts = [b for b in server.calls("/v1/pipeline/pages") if "pages" in b]
    assert len(page_posts) == 1, "ONE batched POST carried all five pages"
    assert [p["page_index"] for p in page_posts[0]["pages"]] == [0, 1, 2, 3, 4]
    # ...and the GET followed the flush (read-your-writes ordering).
    paths = [p for p, _ in server.requests]
    assert paths.index("/v1/pipeline/pages") < len(paths) - 1


def test_page_batch_threshold_flushes_eagerly(db: HttpPipelineDB, server: _Server) -> None:
    for i in range(PAGE_FLUSH_BATCH):
        db.write_page(HASH, i, "t")
    page_posts = [b for b in server.calls("/v1/pipeline/pages") if "pages" in b]
    assert len(page_posts) == 1
    assert len(page_posts[0]["pages"]) == PAGE_FLUSH_BATCH


def test_chunk_batch_threshold_flushes_eagerly(db: HttpPipelineDB, server: _Server) -> None:
    for i in range(CHUNK_FLUSH_BATCH):
        db.write_chunk(HASH, i, "t", f"id{i}")
    chunk_posts = [b for b in server.calls("/v1/pipeline/chunks") if "chunks" in b]
    assert len(chunk_posts) == 1
    assert len(chunk_posts[0]["chunks"]) == CHUNK_FLUSH_BATCH


def test_progress_coalesces_and_rides_the_flush(db: HttpPipelineDB, server: _Server) -> None:
    """Per-page update_progress calls collapse to ONE wire update with the
    LATEST values (the throttle the critic mandated)."""
    for i in range(4):
        db.write_page(HASH, i, "t")
        db.update_progress(HASH, pages_extracted=i + 1)
    assert server.calls("/v1/pipeline/progress") == [], "coalesced, not sent per page"

    db.read_pages_from(HASH, 0)

    progress = server.calls("/v1/pipeline/progress")
    assert len(progress) == 1
    assert progress[0]["fields"] == {"pages_extracted": 4}, "latest value won"


def test_standalone_progress_flushes_eagerly(db: HttpPipelineDB, server: _Server) -> None:
    """With no page/chunk batch pending, progress goes straight out (e.g.
    total_pages at extraction end must not wait for a poll)."""
    db.update_progress(HASH, total_pages=9)
    progress = server.calls("/v1/pipeline/progress")
    assert len(progress) == 1
    assert progress[0]["fields"] == {"total_pages": 9}


def test_flush_failure_restores_buffers(db: HttpPipelineDB, server: _Server) -> None:
    """A transient engine error loses nothing: the buffered writes are
    restored and the next flush retries (idempotent server upserts)."""
    db.write_page(HASH, 0, "t")
    server.fail_next_post = True
    with pytest.raises(Exception):
        db.flush(HASH)

    db.flush(HASH)  # retry succeeds
    page_posts = [b for b in server.calls("/v1/pipeline/pages") if "pages" in b]
    assert len(page_posts) == 2, "failed attempt + successful retry"
    assert page_posts[1]["pages"] == page_posts[0]["pages"], "nothing was lost"


def test_embedding_tri_state_round_trip(db: HttpPipelineDB, server: _Server) -> None:
    db.write_chunk(HASH, 0, "t", "id0", embedding=None)
    db.write_chunk(HASH, 1, "t", "id1", embedding=b"")
    db.write_chunk(HASH, 2, "t", "id2", embedding=b"\x01\x02")
    db.flush(HASH)

    sent = [b for b in server.calls("/v1/pipeline/chunks") if "chunks" in b][0]["chunks"]
    assert sent[0]["embedding"] is None
    assert sent[1]["embedding"] == ""
    assert sent[2]["embedding"] == base64.b64encode(b"\x01\x02").decode()

    server.chunks_response = [
        {"chunk_index": 0, "embedding": None},
        {"chunk_index": 1, "embedding": ""},
        {"chunk_index": 2, "embedding": base64.b64encode(b"\x01\x02").decode()},
    ]
    rows = db.read_ready_chunks(HASH)
    assert rows[0]["embedding"] is None
    assert rows[1]["embedding"] == b""
    assert rows[2]["embedding"] == b"\x01\x02"


def test_orphan_scan_checks_paths_client_side(
    db: HttpPipelineDB, server: _Server, tmp_path
) -> None:
    """The split scan: the engine serves rows; path existence is judged
    HERE. A missing pdf_path orphans regardless of status; a live path with
    fresh heartbeat survives."""
    live = tmp_path / "live.pdf"
    live.write_bytes(b"pdf")
    from datetime import UTC, datetime

    now_iso = datetime.now(UTC).isoformat()
    server.pipelines_response = [
        {"content_hash": "a" * 32, "pdf_path": str(live), "status": "running",
         "updated_at": now_iso},
        {"content_hash": "b" * 32, "pdf_path": str(tmp_path / "gone.pdf"),
         "status": "completed", "updated_at": now_iso},
    ]

    orphans = db.scan_orphaned_pipelines(delete=True)

    assert orphans == ["b" * 32]
    deletes = server.calls("/v1/pipeline/delete")
    assert [d["content_hash"] for d in deletes] == ["b" * 32]


def test_mark_and_lifecycle_flush_first(db: HttpPipelineDB, server: _Server) -> None:
    """Lifecycle calls (mark_uploaded/completed/failed, extraction_meta)
    flush pending writes first — ordering the server sees matches the
    stages' local ordering."""
    db.write_chunk(HASH, 0, "t", "id0", embedding=b"")
    db.mark_uploaded(HASH, [0])

    paths = [p for p, b in server.requests]
    chunks_at = paths.index("/v1/pipeline/chunks")
    mark_at = paths.index("/v1/pipeline/mark_uploaded")
    assert chunks_at < mark_at, "the chunk batch landed before mark_uploaded"
