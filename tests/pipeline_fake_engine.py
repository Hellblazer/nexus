# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-memory fake of the engine's ``/v1/pipeline`` surface (RDR-186 .16).

Test infrastructure for the streaming-PDF suites after the ``pipeline.db``
SQLite buffer retired: stage tests exercise the REAL ``HttpPipelineDB``
client (buffering, read-your-writes flushing, tri-state embedding wire
mapping) against this fake, which mirrors the Java ``PipelineHandler`` +
``PipelineRepository`` semantics — create created/resuming/skip with the
5-minute staleness rule, page REPLACE upserts, chunk IGNORE inserts,
uploadable = embedding-present (the ``b""`` service sentinel counts) and
not yet uploaded. The authoritative server contract is pinned by the Java
``PipelineHandlerTest``; ``tests/db/test_pipeline_fake_engine_parity.py``
keeps this fake honest against the same scenarios.

``clock`` is injectable for staleness tests (fixed clocks per house rule).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

import httpx

from nexus.db.http_pipeline_client import STALE_THRESHOLD, HttpPipelineDB

_PROGRESS_FIELDS = {
    "total_pages", "pages_extracted", "chunks_created", "chunks_embedded", "chunks_uploaded",
}


class FakePipelineEngine:
    """Dict-backed twin of the three ``nexus.pdf_*`` tables."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self.pipelines: dict[str, dict[str, Any]] = {}
        self.pages: dict[tuple[str, int], dict[str, Any]] = {}
        self.chunks: dict[tuple[str, int], dict[str, Any]] = {}

    # ── endpoint semantics ──────────────────────────────────────────────────

    def create(self, body: dict) -> dict:
        h = body["content_hash"]
        now = self.clock().isoformat()
        row = self.pipelines.get(h)
        if row is None:
            self.pipelines[h] = {
                "content_hash": h, "pdf_path": body["pdf_path"],
                "collection": body["collection"], "total_pages": None,
                "pages_extracted": 0, "chunks_created": None,
                "chunks_embedded": None, "chunks_uploaded": 0,
                "status": "running", "error": "", "extraction_meta": "",
                "started_at": now, "updated_at": now,
            }
            return {"status": "created"}
        if row["status"] == "completed":
            return {"status": "skip"}
        if row["status"] == "failed":
            row.update(status="resuming", updated_at=now)
            return {"status": "resuming"}
        stale = self.clock() - datetime.fromisoformat(row["updated_at"]) > STALE_THRESHOLD
        if stale:
            row.update(status="resuming", updated_at=now)
            return {"status": "resuming"}
        return {"status": "skip"}

    def state(self, params: dict) -> dict:
        row = self.pipelines.get(params["content_hash"])
        return {"pipeline": dict(row) if row else None}

    def write_pages(self, body: dict) -> dict:
        h = body["content_hash"]
        now = self.clock().isoformat()
        for p in body["pages"]:
            self.pages[(h, int(p["page_index"]))] = {
                "content_hash": h, "page_index": int(p["page_index"]),
                "page_text": p["page_text"],
                "metadata_json": p.get("metadata_json", "{}"),
                "created_at": now,
            }
        return {"written": len(body["pages"])}

    def read_pages(self, params: dict) -> dict:
        h = params["content_hash"]
        start = int(params.get("start", 0))
        rows = sorted(
            (dict(r) for (ch, idx), r in self.pages.items() if ch == h and idx >= start),
            key=lambda r: r["page_index"],
        )
        return {"pages": rows}

    def write_chunks(self, body: dict) -> dict:
        h = body["content_hash"]
        now = self.clock().isoformat()
        inserted = 0
        for c in body["chunks"]:
            key = (h, int(c["chunk_index"]))
            if key in self.chunks:
                continue  # INSERT OR IGNORE / ON CONFLICT DO NOTHING
            self.chunks[key] = {
                "content_hash": h, "chunk_index": int(c["chunk_index"]),
                "chunk_text": c["chunk_text"], "chunk_id": c["chunk_id"],
                "metadata_json": c.get("metadata_json", "{}"),
                "embedding": c.get("embedding"),  # wire form: None | "" | base64
                "uploaded": 0, "created_at": now,
            }
            inserted += 1
        return {"inserted": inserted}

    def read_chunks(self, params: dict) -> dict:
        h = params["content_hash"]
        uploadable = params.get("uploadable") in ("1", 1, True)
        limit = int(params.get("limit", 0))
        rows = sorted(
            (dict(r) for (ch, _), r in self.chunks.items() if ch == h and r["uploaded"] == 0),
            key=lambda r: r["chunk_index"],
        )
        if uploadable:
            rows = [r for r in rows if r["embedding"] is not None]
        if limit > 0:
            rows = rows[:limit]
        return {"chunks": rows}

    def progress(self, body: dict) -> dict:
        h = body["content_hash"]
        fields = body["fields"]
        bad = set(fields) - _PROGRESS_FIELDS
        if bad:
            raise ValueError(f"Unknown progress fields: {bad}")
        row = self.pipelines.get(h)
        if row is not None:
            row.update(fields)
            row["updated_at"] = self.clock().isoformat()
        return {"updated": row is not None}

    def extraction_meta(self, body: dict) -> dict:
        row = self.pipelines.get(body["content_hash"])
        if row is not None:
            row["extraction_meta"] = body["metadata_json"]
            row["updated_at"] = self.clock().isoformat()
        return {"updated": row is not None}

    def complete(self, body: dict) -> dict:
        return self._set_status(body["content_hash"], "completed")

    def fail(self, body: dict) -> dict:
        result = self._set_status(body["content_hash"], "failed")
        row = self.pipelines.get(body["content_hash"])
        if row is not None:
            row["error"] = body.get("error", "")
        return result

    def _set_status(self, h: str, status: str) -> dict:
        row = self.pipelines.get(h)
        if row is not None:
            row["status"] = status
            row["updated_at"] = self.clock().isoformat()
        return {"updated": row is not None}

    def mark_uploaded(self, body: dict) -> dict:
        h = body["content_hash"]
        n = 0
        for idx in body["chunk_indices"]:
            row = self.chunks.get((h, int(idx)))
            if row is not None:
                row["uploaded"] = 1
                n += 1
        return {"updated": n}

    def counts(self, params: dict) -> dict:
        # Mirrors PipelineHandler.handleCounts exactly: a blank/absent
        # content_hash yields embedded_chunks=0 (NOT a global sum) — the
        # count is per-pipeline-only by contract (.16 critic Significant #3).
        h = params.get("content_hash")
        if not h:
            embedded = 0
        else:
            embedded = sum(
                1 for (ch, _), r in self.chunks.items()
                if ch == h and r["embedding"] is not None
            )
        return {"embedded_chunks": embedded, "pipelines": len(self.pipelines)}

    def clear_wal(self, body: dict) -> dict:
        h = body["content_hash"]
        self.pages = {k: v for k, v in self.pages.items() if k[0] != h}
        self.chunks = {k: v for k, v in self.chunks.items() if k[0] != h}
        return {"cleared": True}

    def delete(self, body: dict) -> dict:
        h = body["content_hash"]
        self.clear_wal(body)
        self.pipelines.pop(h, None)
        return {"deleted": True}

    def delete_collection(self, body: dict) -> dict:
        collection = body["collection"]
        hashes = [h for h, r in self.pipelines.items() if r["collection"] == collection]
        for h in hashes:
            self.delete({"content_hash": h})
        return {"deleted": len(hashes)}

    def list_pipelines(self, params: dict) -> dict:
        return {"pipelines": [dict(r) for r in self.pipelines.values()]}

    # ── httpx transport ─────────────────────────────────────────────────────

    _ROUTES = {
        ("POST", "/v1/pipeline/create"): "create",
        ("GET", "/v1/pipeline/state"): "state",
        ("POST", "/v1/pipeline/pages"): "write_pages",
        ("GET", "/v1/pipeline/pages"): "read_pages",
        ("POST", "/v1/pipeline/chunks"): "write_chunks",
        ("GET", "/v1/pipeline/chunks"): "read_chunks",
        ("POST", "/v1/pipeline/progress"): "progress",
        ("POST", "/v1/pipeline/extraction_meta"): "extraction_meta",
        ("POST", "/v1/pipeline/complete"): "complete",
        ("POST", "/v1/pipeline/fail"): "fail",
        ("POST", "/v1/pipeline/mark_uploaded"): "mark_uploaded",
        ("GET", "/v1/pipeline/counts"): "counts",
        ("POST", "/v1/pipeline/clear_wal"): "clear_wal",
        ("POST", "/v1/pipeline/delete"): "delete",
        ("POST", "/v1/pipeline/delete_collection"): "delete_collection",
        ("GET", "/v1/pipeline/list"): "list_pipelines",
    }

    def handler(self, request: httpx.Request) -> httpx.Response:
        method_name = self._ROUTES.get((request.method, request.url.path))
        if method_name is None:
            return httpx.Response(404, json={"error": f"no route {request.url.path}"})
        payload = (
            json.loads(request.content) if request.method == "POST"
            else dict(request.url.params)
        )
        try:
            return httpx.Response(200, json=getattr(self, method_name)(payload))
        except ValueError as exc:
            return httpx.Response(400, json={"error": str(exc)})


def make_fake_engine_db(
    clock: Callable[[], datetime] | None = None,
) -> tuple[HttpPipelineDB, FakePipelineEngine]:
    """A real ``HttpPipelineDB`` wired to a fresh :class:`FakePipelineEngine`."""
    engine = FakePipelineEngine(clock=clock)
    db = HttpPipelineDB(base_url="http://fake-engine", _token="fake-token")
    db._client = httpx.Client(transport=httpx.MockTransport(engine.handler))
    db._clock = engine.clock  # one clock on both sides (deterministic staleness)
    return db, engine
