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

ROW HANDLE (nexus-edjmu). The engine keys a pipeline row on one RUN of
one document (``pipeline_id``, unique on content_hash + collection +
pdf_path; pipeline-002-per-row-identity.xml), not on the content hash, so
a byte-identical PDF at a second path or in a second collection gets its
own row and its own WAL instead of skipping on a leftover. ``create_pipeline``
sends ``identity="document"`` and remembers the ``pipeline_id`` the engine
returns per content_hash; every later call for that hash rides the id on
the wire. The stage functions and the buffers stay keyed by content_hash,
which makes ONE INSTANCE PER RUN the contract: two concurrent runs sharing
a hash in one instance would merge their page buffers, so
``create_pipeline`` refuses the second while the first is live
(production builds one instance per ``pipeline_index_pdf`` call; a
sequential re-run of the same hash on one instance, after the first run
completed or failed, is fine and replaces the mapping).

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

import httpx
import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

#: A running/resuming pipeline whose heartbeat is older than this is
#: considered crashed (create() returns "resuming"; the orphan scan flags
#: it). nexus-lcmbp fix-list #6: MUST stay numerically identical to the
#: Java analog, ``PipelineRepository.STALE_THRESHOLD``
#: (``Duration.ofMinutes(5)``,
#: service/src/main/java/dev/nexus/service/db/PipelineRepository.java) —
#: the server judges create()/staleness (and stamps
#: ``stale_threshold_seconds`` on a 409 ``conflict_running`` body) against
#: its own clock with ITS constant; this constant serves the orphan scan's
#: client half AND is what a caller compares
#: ``PipelineConflictRunning.stale_threshold_seconds`` against. A drift
#: here would not fail loudly — it would silently make one side's
#: staleness judgment disagree with the other's. Pinned by
#: ``tests/db/test_pipeline_fake_engine_parity.py::
#: test_stale_threshold_agrees_across_client_and_wire``.
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


class PipelineConflictRunning(RuntimeError):
    """POST /v1/pipeline/create hit HTTP 409 ``conflict_running`` (nexus-lcmbp).

    The engine refuses a ``create()`` retry against a ``running`` row whose
    heartbeat is still fresh (younger than ``stale_threshold_seconds``) —
    see ``PipelineRepository.create`` / ``PipelineConflictException`` on the
    Java side. Prior client behaviour treated the matching 200 ``{"status":
    "skip"}`` response identically to every other short-circuit (including a
    genuinely completed run), so ``nx index`` exited ``rc=0`` having written
    zero chunks — a silent no-op reported as success, and the SAME retry
    would become a loud ``RuntimeError`` once the row aged past the stale
    threshold. This type makes the fresh-heartbeat case loud too: callers
    (``pipeline_stages.pipeline_index_pdf`` and, through it, ``nx index``)
    let it propagate rather than translating it into a chunk count, so a
    stranded row is never mistaken for a successful re-index.

    Subclasses :class:`RuntimeError` deliberately: every ``nx index``
    command entry point already converts an escaping ``RuntimeError`` into a
    ``click.ClickException`` (see ``commands/index.py``), so no CLI wiring
    is required for this to surface as a non-zero exit printing the message
    below — no separate except clause needed there.
    """

    def __init__(
        self,
        error: str,
        *,
        content_hash: str,
        started_at: str,
        heartbeat_age_seconds: int,
        stale_threshold_seconds: int,
        remedy: str,
    ) -> None:
        message = error
        if remedy and remedy not in error:
            message = f"{error} (remedy: {remedy})"
        super().__init__(message)
        self.error = error
        self.content_hash = content_hash
        self.started_at = started_at
        self.heartbeat_age_seconds = heartbeat_age_seconds
        self.stale_threshold_seconds = stale_threshold_seconds
        self.remedy = remedy


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
        # nexus-edjmu: content_hash -> the engine's pipeline_id for THIS
        # instance's run of that document, and the hashes whose run has not
        # reached a terminal call yet (see the module docstring).
        self._pipeline_ids: dict[str, int] = {}
        self._live: dict[str, tuple[str, str]] = {}

    def _ref(self, content_hash: str) -> dict[str, Any]:
        """The wire fields naming *content_hash*'s row: the engine's
        ``pipeline_id`` once ``create_pipeline`` learned it, plus the hash
        for readability; the bare hash before that."""
        with self._buffer_lock:
            pipeline_id = self._pipeline_ids.get(content_hash)
        if pipeline_id is None:
            return {"content_hash": content_hash}
        return {"content_hash": content_hash, "pipeline_id": pipeline_id}

    def pipeline_id_for(self, content_hash: str) -> int | None:
        """The engine ``pipeline_id`` this instance holds for *content_hash*,
        or ``None`` before ``create_pipeline`` ran for it here."""
        with self._buffer_lock:
            return self._pipeline_ids.get(content_hash)

    # ── pipeline lifecycle ──────────────────────────────────────────────────

    def create_pipeline(self, content_hash: str, pdf_path: str, collection: str) -> str:
        """Start (or resume) a pipeline run.

        Raises :class:`PipelineConflictRunning` (never returns a "skip"
        that could be mistaken for a completed run) when the engine
        answers 409 ``conflict_running`` — a retry against a ``running``
        row whose heartbeat is still fresh. Not retried: 409 is a terminal
        business-logic refusal, outside both retry axes in
        ``RefreshableHttpStoreMixin`` (the endpoint-refresh axis only
        treats 401 as retryable; the gateway axis only treats
        502/503/504).

        nexus-edjmu: sends ``identity="document"`` so the engine keys the
        row on (content_hash, collection, pdf_path), and keeps the returned
        ``pipeline_id`` for every later call on *content_hash*. Never
        returns ``"skip"`` on this path: a leftover completed row is reset
        by the engine and answered ``"created"``. Raises
        :class:`PipelineConflictRunning` before any HTTP call when THIS
        instance already has a live run for *content_hash* on a DIFFERENT
        document (the one-instance-per-run contract in the module
        docstring: two documents sharing bytes would merge their page
        buffers here; a re-create of the SAME document is the engine's
        call, resume or 409), and
        ``RuntimeError`` when the engine answers without a ``pipeline_id``
        (an engine older than pipeline-002: a hand-mixed install, since a
        released client is pinned to ``REQUIRED_ENGINE_VERSION``).
        """
        document = (collection, str(pdf_path))
        with self._buffer_lock:
            live = self._live.get(content_hash)
        if live is not None and live != document:
            raise PipelineConflictRunning(
                f"pipeline for content_hash={content_hash} is already running "
                f"on this HttpPipelineDB instance for collection={live[0]} "
                f"pdf_path={live[1]}",
                content_hash=content_hash,
                started_at="",
                heartbeat_age_seconds=0,
                stale_threshold_seconds=int(STALE_THRESHOLD.total_seconds()),
                remedy="run each document through its own HttpPipelineDB "
                       "instance (one instance per pipeline_index_pdf call)",
            )
        try:
            result = self._post("/v1/pipeline/create", {
                "content_hash": content_hash,
                "pdf_path": str(pdf_path),
                "collection": collection,
                "identity": "document",
            })
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                try:
                    body = exc.response.json()
                except ValueError:
                    body = {}
                if body.get("status") == "conflict_running":
                    # A malformed body (e.g. an explicit JSON null on a
                    # numeric field, tripping `int(None)`) must fall
                    # through to the bare `raise` below — re-raising the
                    # ORIGINAL httpx.HTTPStatusError — rather than escape
                    # as an unrelated TypeError that hides the real 409.
                    try:
                        heartbeat_age_seconds = int(body.get("heartbeat_age_seconds", 0))
                        stale_threshold_seconds = int(body.get("stale_threshold_seconds", 0))
                    except (TypeError, ValueError):
                        pass
                    else:
                        raise PipelineConflictRunning(
                            body.get("error", "pipeline is already running"),
                            content_hash=body.get("content_hash", content_hash),
                            started_at=body.get("started_at", ""),
                            heartbeat_age_seconds=heartbeat_age_seconds,
                            stale_threshold_seconds=stale_threshold_seconds,
                            remedy=body.get("remedy", ""),
                        ) from exc
            raise
        status = result["status"]
        pipeline_id = result.get("pipeline_id")
        if not isinstance(pipeline_id, int) or isinstance(pipeline_id, bool):
            from nexus.engine_version import REQUIRED_ENGINE_VERSION  # noqa: PLC0415 - deferred: message-only import

            raise RuntimeError(
                "POST /v1/pipeline/create answered without a pipeline_id: the "
                "engine predates pipeline-002 (per-document pipeline rows); "
                "this client requires engine-service-v"
                + ".".join(str(n) for n in REQUIRED_ENGINE_VERSION)
                + " or newer"
            )
        with self._buffer_lock:
            self._pipeline_ids[content_hash] = pipeline_id
            self._live[content_hash] = document
        return status

    def get_pipeline_state(self, content_hash: str) -> dict[str, Any] | None:
        self.flush(content_hash)
        return self._get("/v1/pipeline/state", self._ref(content_hash))["pipeline"]

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
            **self._ref(content_hash),
            "metadata_json": json.dumps(metadata),
        })

    def mark_completed(self, content_hash: str) -> None:
        self.flush(content_hash)
        self._post("/v1/pipeline/complete", self._ref(content_hash))
        self._retire(content_hash)

    def mark_failed(self, content_hash: str, error: str = "") -> None:
        """Mark *content_hash* failed with *error* as the audit record.

        Flushing pending writes first is still attempted (nexus-146xx.16
        intent: buffered pages should land before the terminal state when
        that's possible) but is best-effort here — never load-bearing. When
        the ORIGINAL failure came FROM a flush, flush()'s own except block
        (the buffer-restoring BaseException handler in flush) has already
        restored the failing pages into the buffer,
        so this cleanup flush would otherwise re-POST the IDENTICAL payload
        and hit the IDENTICAL error, raising before /fail is ever issued
        (nexus-ptctu: the pipeline row strands 'running' with no error, and
        the caller sees the cleanup failure instead of the original one).
        The failure record is the thing that must survive; buffered pages
        are not — so a failing cleanup flush is logged and swallowed, and
        /fail is always ATTEMPTED with the caller's ORIGINAL error text.
        The /fail POST itself can still fail and raises to the caller —
        callers on an error path must wrap this call so their original
        exception propagates regardless (pipeline_stages does; see
        nexus-rewgw). Note the asymmetry with flush(): flush restores its
        buffer on BaseException, while the cleanup wrapper here catches
        only Exception — a KeyboardInterrupt during cleanup propagates
        immediately by design (control-flow exceptions are not swallowed).
        """
        try:
            self.flush(content_hash)
        except Exception:  # noqa: BLE001 — boundary catch: the cleanup flush is best-effort, never load-bearing; swallow-and-log so /fail is always attempted with the caller's ORIGINAL error
            _log.warning(
                "pipeline_mark_failed_cleanup_flush_failed",
                content_hash=content_hash,
                exc_info=True,
            )
        self._post("/v1/pipeline/fail", {**self._ref(content_hash), "error": error})
        self._retire(content_hash)

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
            {**self._ref(content_hash), "start": start_index},
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
        params: dict[str, Any] = self._ref(content_hash)
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
            **self._ref(content_hash),
            "chunk_indices": chunk_indices,
        })

    def count_embedded_chunks(self, content_hash: str) -> int:
        self.flush(content_hash)
        return int(self._get(
            "/v1/pipeline/counts", self._ref(content_hash)
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
        ref = self._ref(content_hash)
        try:
            if pages:
                self._post("/v1/pipeline/pages", {**ref, "pages": pages})
            if chunks:
                self._post("/v1/pipeline/chunks", {**ref, "chunks": chunks})
            if progress:
                self._post("/v1/pipeline/progress", {**ref, "fields": progress})
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
        self._post("/v1/pipeline/clear_wal", self._ref(content_hash))

    def delete_pipeline_data(
        self,
        content_hash: str,
        *,
        collection: str = "",
        pdf_path: str = "",
        pipeline_id: int | None = None,
    ) -> bool:
        """Delete ONE run's row (its pages and chunks cascade engine-side).

        Which run: *pipeline_id* when given (a row read from
        ``scan_orphaned_pipelines``'s listing, which belongs to whatever
        process made it); else *content_hash* narrowed by *collection* and
        *pdf_path* when either is given (the caller names the document, so
        an id this instance may hold from an earlier run is not consulted);
        else the id this instance holds for *content_hash*; else the bare
        hash. The ``--force`` pre-flight in ``pipeline_index_pdf``
        runs BEFORE ``create_pipeline`` and MUST pass both narrowing
        fields: a bare hash would resolve to whichever row the engine
        prefers for it, and a sibling document sharing the bytes would
        lose its run (the cross-row WAL destruction the parked key-widen
        attempt shipped; T2 nexus/critique-nexus-edjmu-33q80-pipeline-key).
        Returns whether a row was deleted.
        """
        self._drop_buffers(content_hash)
        if pipeline_id is not None:
            body: dict[str, Any] = {"content_hash": content_hash, "pipeline_id": pipeline_id}
        elif collection or pdf_path:
            body = {"content_hash": content_hash}
            if collection:
                body["collection"] = collection
            if pdf_path:
                body["pdf_path"] = str(pdf_path)
        else:
            body = self._ref(content_hash)
        result = self._post("/v1/pipeline/delete", body)
        with self._buffer_lock:
            held = self._pipeline_ids.get(content_hash)
            if held is not None and held == body.get("pipeline_id"):
                self._pipeline_ids.pop(content_hash, None)
                self._live.pop(content_hash, None)
        return bool(result.get("deleted", False))

    def _retire(self, content_hash: str) -> None:
        """The run reached a terminal call: a later ``create_pipeline`` for
        the same hash on this instance is a sequential re-run, not a
        concurrent one. The id mapping stays until ``delete_pipeline_data``
        so the post-passes' reads keep addressing the same row."""
        with self._buffer_lock:
            self._live.pop(content_hash, None)

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
            # nexus-edjmu: delete exactly the LISTED row. Several rows can
            # share a hash now (one per document), and this process holds
            # no mapping for rows other processes made.
            pipeline_id = int(row["pipeline_id"])
            if not Path(row["pdf_path"]).exists():
                orphans.append(content_hash)
                if delete:
                    self.delete_pipeline_data(content_hash, pipeline_id=pipeline_id)
                continue
            if row["status"] in ("running", "resuming"):
                updated_at = datetime.fromisoformat(row["updated_at"])
                if now - updated_at > STALE_THRESHOLD:
                    orphans.append(content_hash)
                    if delete:
                        self.delete_pipeline_data(content_hash, pipeline_id=pipeline_id)
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
