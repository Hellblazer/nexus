# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Async aspect-extraction worker (RDR-089 follow-up nexus-qeo8).

P1.3 spike on ``knowledge__delos`` invalidated Critical Assumption #2
(per-document extraction <3 s) — measured median 26.5 s / p95 38.1 s.
The synchronous-inline shape is therefore replaced by an async
pattern (RDR-048 ``pipeline_buffer`` reuse):

  hook fires (`aspect_extraction_enqueue_hook`)
    → enqueue (microseconds)
    → ingest path returns

  worker thread polls queue (`AspectExtractionWorker._run_loop`)
    → calls extract_aspects (the synchronous P1.2 extractor)
    → upserts document_aspects on success
    → mark_done (DELETE row from queue)

The synchronous extract_aspects is reusable verbatim — only the
dispatch shape changed from inline to queued. The worker calls it
identically.

Worker contract:

* Single worker per process (singleton: ``get_worker()``,
  ``ensure_worker_started()``).
* Daemon thread; dies with the process. Durable queue absorbs
  process death — the next worker run picks up unprocessed rows.
* Polls every ``poll_interval`` seconds (default 2 s).
* Reclaims rows stuck in ``in_progress`` for > ``stale_timeout``
  seconds (default 300 s) before each poll, so worker process
  death does not wedge a row forever.

Result handling:

* ``record is not None and has populated fields`` → upsert
  document_aspects, mark_done (DELETE).
* ``record is None`` (unsupported collection, the extractor's
  short-circuit before subprocess) → mark_done (drop silently).
* ``record is null-fields`` (extractor's internal 3-retry budget
  exhausted) → upsert null record, mark_done. The extractor
  already retried; the worker MUST NOT retry again.
* Uncaught exception in the worker body (T2 connection lost,
  programming bug) → mark_failed for triage; the worker keeps
  running. Failed rows are terminal until manually re-enqueued.

The hook function ``aspect_extraction_enqueue_hook`` is registered
in ``mcp/core.py`` alongside the other post-document consumers. It
checks ``select_config(collection)`` first — if the collection has
no registered extractor, the hook is a no-op (no queue row, no
worker spawn).
"""
from __future__ import annotations

import threading
from pathlib import Path

import structlog

# Re-export by module-qualified name so test patches at
# ``nexus.aspect_worker._extract_aspects`` swap the worker's
# extraction call cleanly without touching the public entrypoint.
from nexus.aspect_extractor import extract_aspects as _extract_aspects

_log = structlog.get_logger(__name__)


# ── Worker class ────────────────────────────────────────────────────────────


class AspectExtractionWorker:
    """Background drain thread for ``aspect_extraction_queue``.

    Owns one daemon thread that polls the queue and processes one
    row per iteration. Idle when the queue is empty (sleeps
    ``poll_interval`` seconds). Stop via ``stop()`` for clean
    shutdown; otherwise dies with the process (daemon).

    Constructor injection: caller may pass ``poll_interval`` and
    ``stale_timeout`` for tests; defaults are tuned for production.
    """

    def __init__(
        self,
        *,
        poll_interval: float = 2.0,
        stale_timeout_seconds: int = 300,
    ) -> None:
        self._poll_interval = poll_interval
        self._stale_timeout_seconds = stale_timeout_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent — calling twice does
        not spawn a second thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="aspect-extraction-worker",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the worker to stop and join the thread.

        Idempotent — calling on a stopped worker is a no-op. Returns
        when the thread exits or after ``timeout``; failure to join
        is logged but not raised (a stuck worker is recoverable on
        process exit since the thread is a daemon).
        """
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout=timeout)
        if thread.is_alive():
            _log.warning(
                "aspect_worker_stop_timeout",
                timeout=timeout,
            )

    def is_running(self) -> bool:
        """True iff the worker thread exists and is alive."""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        """Drain loop. Runs until ``_stop_event`` is set.

        Each iteration:
          1. Reclaim stale ``in_progress`` rows (cheap O(N) UPDATE
             once per poll — bounded by the indexed status filter).
          2. Claim the next pending row.
          3. If no row: sleep ``poll_interval``.
          4. If row: invoke extract_aspects, dispatch on the result.

        All exceptions inside the loop are caught and recorded;
        the worker thread itself never dies from a row's failure.
        """
        from nexus.mcp_infra import t2_ctx
        while not self._stop_event.is_set():
            try:
                with t2_ctx() as t2:
                    t2.aspect_queue.reclaim_stale(
                        timeout_seconds=self._stale_timeout_seconds,
                    )
                    row = t2.aspect_queue.claim_next()
            except Exception:
                _log.warning("aspect_worker_claim_failed", exc_info=True)
                self._stop_event.wait(self._poll_interval)
                continue

            if row is None:
                self._stop_event.wait(self._poll_interval)
                continue

            self._process_row(row)

    def _process_row(self, row) -> None:
        """Run extraction on one queue row and dispatch on the result."""
        from nexus.mcp_infra import t2_ctx
        try:
            # content="" — the worker has no content in scope; the
            # extractor's content-sourcing fallback reads source_path.
            record = _extract_aspects(
                content="",
                source_path=row.source_path,
                collection=row.collection,
            )
        except Exception as exc:
            _log.warning(
                "aspect_worker_extract_raised",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )
            try:
                with t2_ctx() as t2:
                    t2.aspect_queue.mark_failed(
                        row.collection, row.source_path,
                        error=str(exc),
                    )
            except Exception:
                _log.warning(
                    "aspect_worker_mark_failed_persist_failed",
                    exc_info=True,
                )
            return

        try:
            with t2_ctx() as t2:
                if record is None:
                    # Unsupported collection — drop silently.
                    t2.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
                    return
                # Either a populated record or a null-fields record;
                # either way, the extractor already retried up to
                # 3 attempts internally. Persist whatever it gave us
                # and remove the row from the queue.
                t2.document_aspects.upsert(record)
                t2.aspect_queue.mark_done(
                    row.collection, row.source_path,
                )
        except Exception as exc:
            _log.warning(
                "aspect_worker_persist_failed",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )
            try:
                with t2_ctx() as t2:
                    t2.aspect_queue.mark_failed(
                        row.collection, row.source_path,
                        error=str(exc),
                    )
            except Exception:
                _log.warning(
                    "aspect_worker_mark_failed_secondary_persist_failed",
                    exc_info=True,
                )


# ── Module-level singleton ──────────────────────────────────────────────────

_worker: AspectExtractionWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> AspectExtractionWorker | None:
    """Return the current singleton worker, or ``None`` if no worker
    has been started in this process."""
    return _worker


def ensure_worker_started(
    *,
    poll_interval: float = 2.0,
    stale_timeout_seconds: int = 300,
) -> AspectExtractionWorker:
    """Lazy-start the singleton worker. Returns the worker.

    Idempotent — calling once, twice, or N times all return the same
    instance and only spawn one thread. Tuning parameters apply only
    to the first call (subsequent calls cannot retune).
    """
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = AspectExtractionWorker(
                poll_interval=poll_interval,
                stale_timeout_seconds=stale_timeout_seconds,
            )
        _worker.start()
        return _worker


def stop_worker(timeout: float = 10.0) -> None:
    """Stop the singleton worker if running. Idempotent."""
    global _worker
    with _worker_lock:
        worker = _worker
    if worker is not None:
        worker.stop(timeout=timeout)


def reset_worker_for_tests() -> None:
    """Test helper: tear down the worker singleton so the next
    ``ensure_worker_started`` call rebuilds with fresh state.
    """
    global _worker
    stop_worker(timeout=5.0)
    with _worker_lock:
        _worker = None


# ── Hook function (registered via register_post_document_hook) ──────────────


def aspect_extraction_enqueue_hook(
    source_path: str,
    collection: str,
    content: str,
) -> None:
    """Post-document hook: enqueue a row for async aspect extraction.

    Signature matches ``fire_post_document_hooks(source_path,
    collection, content)``. This hook does NOT call extract_aspects
    inline — that would block the ingest path for ~25 s per document
    (RDR-089 P1.3 spike finding). Instead it:

      1. Skips collections without a registered extractor config
         (Phase 1 = ``knowledge__*`` only).
      2. Writes a pending row to ``aspect_extraction_queue``
         (microsecond-scale T2 INSERT).
      3. Lazy-starts the worker if not already running.

    The hook is synchronous (RDR-089 P0.1 contract). It does not
    block on extraction; the worker drains in a background thread.

    ``content`` is ignored — the worker re-reads ``source_path`` from
    disk because most CLI sites already pass ``content=""`` and we
    want consistent behaviour across the CLI / MCP entry points.
    Phase 2 of this work may revisit caching content into the queue
    row to avoid double-reads.
    """
    from nexus.aspect_extractor import select_config
    if select_config(collection) is None:
        return  # No extractor for this collection — nothing to enqueue.
    from nexus.mcp_infra import t2_ctx
    try:
        with t2_ctx() as t2:
            t2.aspect_queue.enqueue(collection, source_path)
    except Exception:
        _log.warning(
            "aspect_extraction_enqueue_failed",
            source_path=source_path,
            collection=collection,
            exc_info=True,
        )
        # Enqueue failure is non-fatal — ingest is never blocked.
        # The document_aspects row will simply not be populated until
        # a manual re-enqueue triggers extraction.
        return
    ensure_worker_started()
