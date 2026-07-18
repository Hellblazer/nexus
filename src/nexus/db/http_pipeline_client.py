# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""HttpPipelineDB — the engine-backed streaming-PDF buffer (RDR-186 .16).

Drop-in for the retired ``pipeline_buffer.PipelineDB`` over the engine's
``/v1/pipeline`` endpoints (``nexus.pdf_pipeline``/``pdf_pages``/
``pdf_chunks``, engine >= v0.1.47). The local ``pipeline.db`` SQLite buffer
retires with the cutover; state hosts engine-side while the extraction
compute stays client-side (Hal's P0 ruling; RDR-048 lineage).

CHATTINESS DESIGN (the .16 critic's stage-coupling finding): the SQLite
buffer absorbed per-page/per-chunk writes at sub-ms cost; naive 1:1 HTTP
would turn a 500-page PDF into ~1000 round trips. This client therefore
BUFFERS writes in-process and flushes them BATCHED, with READ-YOUR-WRITES
as the flushing trigger: any read touching a content_hash first flushes
that hash's buffered pages/chunks/progress. The three stage threads share
ONE instance (exactly as they shared one SQLite handle), so the chunker's
poll both batches the extractor's pages onto the wire AND observes them —
the RDR-048 coupling survives with the wire traffic ~batch-sized.
Progress updates coalesce (latest value per field) and ride each flush.
HEARTBEAT is write-cadence-bound, not wall-clock-guaranteed: flush() is a
no-op on empty buffers, so ``updated_at`` only refreshes when pages/
chunks/progress actually arrive — a single page taking >5min to extract
(OCR/MinerU-heavy) leaves a real staleness gap during which a concurrent
orphan scan could misjudge the run. This is the SAME page-granularity gap
the SQLite buffer had (its heartbeat was also per-write); batching does
not widen it beyond one flush batch.

CRASH-WINDOW DELTA vs the SQLite buffer: per-page commits gave page-level
durability; buffering trades that for a bounded recompute window — a
client crash loses at most one unflushed batch (≤PAGE_FLUSH_BATCH pages /
≤ one poll interval), and resume re-extracts from the last FLUSHED
``pages_extracted``, so the loss is recomputed work, never data (the
RDR-048 per-batch crash contract, now with batch = flush batch).

Embedding wire mapping (nexus-9n1u3 sentinel, both directions): bytes
``None`` ↔ JSON null (not embedded); ``b""`` ↔ ``""`` (service-mode
sentinel: the JVM embeds at upload); packed floats ↔ base64.

Thread-safety: a single lock guards the buffers; HTTP calls happen outside
it. Matches PipelineDB's cross-thread usage contract (three stage threads,
one store). Known transient: two threads flushing the same hash race —
the loser's read can miss rows the winner is still POSTing (its flush saw
an empty buffer while the in-flight POST hadn't landed). Self-healing:
the next poll observes them; the stages' poll loops tolerate exactly this
kind of not-yet-visible tail by design (RDR-048 stable-prefix chunking).
"""
from __future__ import annotations

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

#: A running/resuming pipeline whose heartbeat is older than this is
#: considered crashed (create() returns "resuming"; the orphan scan flags
#: it). Mirrors the server-side threshold in PipelineRepository — the
#: server judges create()/staleness against its own clock; this constant
#: serves the orphan scan's client half.
STALE_THRESHOLD = timedelta(minutes=5)

#: Buffered pages per content_hash before an eager flush (reads also flush).
PAGE_FLUSH_BATCH: int = 32
#: Buffered chunks per content_hash before an eager flush.
CHUNK_FLUSH_BATCH: int = 100


def _encode_embedding(embedding: bytes | None) -> str | None:
    if embedding is None:
        return None
    if embedding == b"":
        return ""
    return base64.b64encode(embedding).decode("ascii")


def _decode_embedding(value: str | None) -> bytes | None:
    if value is None:
        return None
    if value == "":
        return b""
    return base64.b64decode(value)


class HttpPipelineDB(RefreshableHttpStoreMixin):
    """Thin, write-buffering HTTP client for ``/v1/pipeline``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Overridable for deterministic staleness tests (fixed-clock house
        # rule); used only by scan_orphaned_pipelines.
        self._clock = lambda: datetime.now(UTC)
        self._buffer_lock = threading.Lock()
        self._page_buffer: dict[str, list[dict[str, Any]]] = {}
        self._chunk_buffer: dict[str, list[dict[str, Any]]] = {}
        self._progress_buffer: dict[str, dict[str, int]] = {}

    # ── pipeline lifecycle ──────────────────────────────────────────────────

    def create_pipeline(self, content_hash: str, pdf_path: str, collection: str) -> str:
        result = self._post("/v1/pipeline/create", {
            "content_hash": content_hash,
            "pdf_path": str(pdf_path),
            "collection": collection,
        })
        return result["status"]

    def get_pipeline_state(self, content_hash: str) -> dict[str, Any] | None:
        self.flush(content_hash)
        return self._get("/v1/pipeline/state", {"content_hash": content_hash})["pipeline"]

    def update_progress(self, content_hash: str, **fields: int) -> None:
        """Coalesced (latest value per field); rides the next flush — the
        SQLite version's per-call write becomes per-batch on the wire."""
        eager = False
        with self._buffer_lock:
            self._progress_buffer.setdefault(content_hash, {}).update(fields)
            eager = (
                not self._page_buffer.get(content_hash)
                and not self._chunk_buffer.get(content_hash)
            )
        # No pending page/chunk batch to ride: flush the progress now so
        # standalone progress updates (e.g. total_pages at extraction end)
        # are not deferred behind a poll that may never come.
        if eager:
            self.flush(content_hash)

    def store_extraction_metadata(self, content_hash: str, metadata: dict) -> None:
        self.flush(content_hash)
        self._post("/v1/pipeline/extraction_meta", {
            "content_hash": content_hash,
            "metadata_json": json.dumps(metadata),
        })

    def mark_completed(self, content_hash: str) -> None:
        self.flush(content_hash)
        self._post("/v1/pipeline/complete", {"content_hash": content_hash})

    def mark_failed(self, content_hash: str, error: str = "") -> None:
        self.flush(content_hash)
        self._post("/v1/pipeline/fail", {"content_hash": content_hash, "error": error})

    # ── pages ───────────────────────────────────────────────────────────────

    def write_page(
        self,
        content_hash: str,
        page_index: int,
        page_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._buffer_lock:
            buffer = self._page_buffer.setdefault(content_hash, [])
            buffer.append({
                "page_index": page_index,
                "page_text": page_text,
                "metadata_json": json.dumps(metadata or {}),
            })
            needs_flush = len(buffer) >= PAGE_FLUSH_BATCH
        if needs_flush:
            self.flush(content_hash)

    def read_pages(self, content_hash: str) -> list[dict[str, Any]]:
        return self.read_pages_from(content_hash, 0)

    def read_pages_from(self, content_hash: str, start_index: int) -> list[dict[str, Any]]:
        self.flush(content_hash)  # read-your-writes: the chunker sees the extractor's pages
        rows = self._get(
            "/v1/pipeline/pages",
            {"content_hash": content_hash, "start": start_index},
        )["pages"]
        return rows

    # ── chunks ──────────────────────────────────────────────────────────────

    def write_chunk(
        self,
        content_hash: str,
        chunk_index: int,
        chunk_text: str,
        chunk_id: str,
        metadata: dict[str, Any] | None = None,
        embedding: bytes | None = None,
    ) -> None:
        with self._buffer_lock:
            buffer = self._chunk_buffer.setdefault(content_hash, [])
            buffer.append({
                "chunk_index": chunk_index,
                "chunk_text": chunk_text,
                "chunk_id": chunk_id,
                "metadata_json": json.dumps(metadata or {}),
                "embedding": _encode_embedding(embedding),
            })
            needs_flush = len(buffer) >= CHUNK_FLUSH_BATCH
        if needs_flush:
            self.flush(content_hash)

    def read_ready_chunks(self, content_hash: str) -> list[dict[str, Any]]:
        return self._read_chunks(content_hash, uploadable=False, limit=0)

    def read_uploadable_chunks(self, content_hash: str, limit: int = 0) -> list[dict[str, Any]]:
        return self._read_chunks(content_hash, uploadable=True, limit=limit)

    def _read_chunks(self, content_hash: str, *, uploadable: bool, limit: int) -> list[dict[str, Any]]:
        self.flush(content_hash)
        params: dict[str, Any] = {"content_hash": content_hash}
        if uploadable:
            params["uploadable"] = "1"
        if limit > 0:
            params["limit"] = limit
        rows = self._get("/v1/pipeline/chunks", params)["chunks"]
        for row in rows:
            row["embedding"] = _decode_embedding(row.get("embedding"))
        return rows

    def mark_uploaded(self, content_hash: str, chunk_indices: list[int]) -> None:
        if not chunk_indices:
            return
        self.flush(content_hash)
        self._post("/v1/pipeline/mark_uploaded", {
            "content_hash": content_hash,
            "chunk_indices": chunk_indices,
        })

    def count_embedded_chunks(self, content_hash: str) -> int:
        self.flush(content_hash)
        return int(self._get(
            "/v1/pipeline/counts", {"content_hash": content_hash}
        )["embedded_chunks"])

    def count_pipelines(self) -> int:
        self.flush_all()
        return int(self._get("/v1/pipeline/counts")["pipelines"])

    # ── flushing ────────────────────────────────────────────────────────────

    def flush(self, content_hash: str) -> None:
        """Send *content_hash*'s buffered pages, chunks, and progress.

        Raises on HTTP failure with the buffers RESTORED (prepended), so a
        transient engine error loses nothing — the next flush retries; the
        engine's upserts (REPLACE pages / IGNORE chunks) make the retry
        idempotent.
        """
        with self._buffer_lock:
            pages = self._page_buffer.pop(content_hash, [])
            chunks = self._chunk_buffer.pop(content_hash, [])
            progress = self._progress_buffer.pop(content_hash, {})
        try:
            if pages:
                self._post("/v1/pipeline/pages", {
                    "content_hash": content_hash, "pages": pages,
                })
            if chunks:
                self._post("/v1/pipeline/chunks", {
                    "content_hash": content_hash, "chunks": chunks,
                })
            if progress:
                self._post("/v1/pipeline/progress", {
                    "content_hash": content_hash, "fields": progress,
                })
        except BaseException:
            with self._buffer_lock:
                self._page_buffer[content_hash] = pages + self._page_buffer.get(content_hash, [])
                self._chunk_buffer[content_hash] = chunks + self._chunk_buffer.get(content_hash, [])
                merged = dict(progress)
                merged.update(self._progress_buffer.get(content_hash, {}))
                self._progress_buffer[content_hash] = merged
            raise

    def flush_all(self) -> None:
        with self._buffer_lock:
            hashes = set(self._page_buffer) | set(self._chunk_buffer) | set(self._progress_buffer)
        for content_hash in hashes:
            self.flush(content_hash)

    # ── cleanup / scan ──────────────────────────────────────────────────────

    def clear_orphan_wal(self, content_hash: str) -> None:
        self._drop_buffers(content_hash)
        self._post("/v1/pipeline/clear_wal", {"content_hash": content_hash})

    def delete_pipeline_data(self, content_hash: str) -> None:
        self._drop_buffers(content_hash)
        self._post("/v1/pipeline/delete", {"content_hash": content_hash})

    def delete_pipeline_data_for_collection(self, collection: str) -> int:
        # Flush first: the client cannot map buffered hashes to collections,
        # so land pending writes and let the server-side delete sweep them —
        # otherwise a later flush could resurrect rows for the deleted
        # collection.
        self.flush_all()
        result = self._post("/v1/pipeline/delete_collection", {"collection": collection})
        return int(result["deleted"])

    def scan_orphaned_pipelines(self, *, delete: bool = False) -> list[str]:
        """The orphan scan's CLIENT half: the engine serves the rows; the
        pdf_path existence check happens HERE (only this process sees its
        disk), and staleness is judged HERE with the client clock against
        the server-stamped ``updated_at`` — clock skew is bounded by the
        5-minute threshold, and the server independently applies its own
        staleness rule at create(). Mirrors the retired
        PipelineDB.scan_orphaned_pipelines."""
        self.flush_all()
        rows = self._get("/v1/pipeline/list")["pipelines"]
        now = self._clock()
        orphans: list[str] = []
        for row in rows:
            content_hash = row["content_hash"]
            if not Path(row["pdf_path"]).exists():
                orphans.append(content_hash)
                if delete:
                    self.delete_pipeline_data(content_hash)
                continue
            if row["status"] in ("running", "resuming"):
                updated_at = datetime.fromisoformat(row["updated_at"])
                if now - updated_at > STALE_THRESHOLD:
                    orphans.append(content_hash)
                    if delete:
                        self.delete_pipeline_data(content_hash)
        return orphans

    def _drop_buffers(self, content_hash: str) -> None:
        with self._buffer_lock:
            self._page_buffer.pop(content_hash, None)
            self._chunk_buffer.pop(content_hash, None)
            self._progress_buffer.pop(content_hash, None)

    def __enter__(self) -> "HttpPipelineDB":
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.flush_all()
        finally:
            self.close()
