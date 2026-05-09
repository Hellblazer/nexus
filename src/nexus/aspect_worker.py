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
import time
from pathlib import Path

import structlog

# Re-export by module-qualified name so test patches at
# ``nexus.aspect_worker._extract_aspects`` swap the worker's
# extraction call cleanly without touching the public entrypoint.
from nexus.aspect_extractor import (
    ExtractFail,
    extract_aspects as _extract_aspects,
    extract_aspects_batch as _extract_aspects_batch,
)

_log = structlog.get_logger(__name__)

# ── Drain protocol exceptions ───────────────────────────────────────────────


class DrainTimeoutError(RuntimeError):
    """Raised by ``drain_worker`` when the queue still has actionable rows
    (``status != 'failed'``) after the configured timeout.

    Actionable rows are those in ``pending`` or ``in_progress`` state.
    The operator must triage: inspect ``aspect_extraction_queue`` for
    stuck ``in_progress`` rows, kill any hung workers, and re-run the
    drain after the queue is clear.
    """

    def __init__(self, stuck_count: int, timeout: float) -> None:
        self.stuck_count = stuck_count
        self.timeout = timeout
        super().__init__(
            f"Drain timeout after {timeout:.1f}s: "
            f"{stuck_count} actionable row(s) remain in aspect_extraction_queue "
            f"(status pending or in_progress). "
            "Triage stuck workers before retrying the PK migration."
        )


class DrainBlockedByActiveWorker(RuntimeError):
    """Raised by ``drain_worker`` when an active MCP-process worker is
    detected via a lock file.

    ``drain_worker`` is process-local: it only stops the worker running
    inside the *current* process.  If an MCP server has its own worker
    in a separate process, draining here leaves that process's queue
    rows untouched — the PK migration would then race against live
    ``in_progress`` rows it cannot see.

    The operator must either:
      1. Stop the MCP server before running the migration, **or**
      2. Invoke the migration from within the MCP process (e.g., via
         ``nx upgrade``).

    SIG-5 (nexus-1091): lock-file path is
    ``~/.config/nexus/locks/aspect_worker.<pid>``.
    """

    def __init__(self, blocking_pid: int, lock_file: Path) -> None:
        self.blocking_pid = blocking_pid
        self.lock_file = lock_file
        super().__init__(
            f"drain_worker blocked by active MCP worker in PID {blocking_pid} "
            f"(lock file: {lock_file}). "
            "Stop the MCP server before running the migration, or invoke the "
            "migration from within the MCP process (e.g., via `nx upgrade`)."
        )


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

    # Reclaim runs every N polls rather than every poll: at the
    # default poll_interval=2s and reclaim_every=15, stale-row
    # reclamation fires every ~30s. Suppresses the O(N) UPDATE
    # storm a large stuck queue would otherwise see, while still
    # recovering crashed-worker rows within one reclaim cycle.
    _RECLAIM_EVERY_N_POLLS = 15

    # Batch-extraction threshold (RDR-089 Phase D). When the queue
    # has at least this many pending rows, drain them in a single
    # ``extract_aspects_batch`` call rather than per-row. Below this,
    # use the single-paper path so small queues don't pay the
    # batch-overhead tax for one-or-two-paper drains.
    _DEFAULT_BATCH_SIZE = 5

    def __init__(
        self,
        *,
        poll_interval: float = 2.0,
        stale_timeout_seconds: int = 300,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._poll_interval = poll_interval
        self._stale_timeout_seconds = stale_timeout_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._poll_count = 0

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

    def stop_claiming(self) -> None:
        """Set the stop signal so the run loop exits after its current
        iteration without claiming any more rows.

        Unlike ``stop()``, this method does NOT join the thread — in-flight
        row processing continues to completion.  The thread will exit on its
        own once the current iteration finishes and it re-checks
        ``_stop_event`` at the top of the loop.

        Idempotent — safe to call multiple times or on an already-stopped
        worker.

        Used by the RDR-108 drain-before-PK-migration protocol
        (nexus-he24).  Call ``drain_worker()`` rather than this method
        directly — it coordinates stop_claiming + queue-empty wait.
        """
        self._stop_event.set()

    def is_claiming_stopped(self) -> bool:
        """True iff the stop signal is set (worker will not claim new rows).

        Note: a True result does not mean the thread has actually exited —
        an in-flight iteration may still be running.  Use ``is_running()``
        to check thread liveness.
        """
        return self._stop_event.is_set()

    def _run_loop(self) -> None:
        """Drain loop. Runs until ``_stop_event`` is set.

        Each iteration:
          1. Reclaim stale ``in_progress`` rows (every
             ``_RECLAIM_EVERY_N_POLLS`` iterations — frequency guard
             on the O(N) UPDATE for large stuck queues).
          2. Claim a batch of up to ``batch_size`` pending rows.
          3. If empty: sleep ``poll_interval``.
          4. If batch_size >= 2: invoke extract_aspects_batch (one
             Claude call extracts all rows). RDR-089 Phase D path —
             cost-amortises across rows.
          5. If batch_size == 1: invoke single-paper extract_aspects
             (existing path). Small-queue case where batch overhead
             is not worth it.

        All exceptions inside the loop are caught and recorded; the
        worker thread itself never dies from a row's failure.
        """
        from nexus.mcp_infra import t2_ctx
        while not self._stop_event.is_set():
            # Increment unconditionally so a sustained T2 unavailability
            # does not pin _poll_count at a multiple of
            # _RECLAIM_EVERY_N_POLLS and amplify the reclaim UPDATE
            # against an already-stressed database.
            self._poll_count += 1
            try:
                with t2_ctx() as t2:
                    if self._poll_count % self._RECLAIM_EVERY_N_POLLS == 0:
                        t2.aspect_queue.reclaim_stale(
                            timeout_seconds=self._stale_timeout_seconds,
                        )
                    rows = t2.aspect_queue.claim_batch(self._batch_size)
            except Exception:
                _log.warning("aspect_worker_claim_failed", exc_info=True)
                self._stop_event.wait(self._poll_interval)
                continue

            if not rows:
                self._stop_event.wait(self._poll_interval)
                continue

            if len(rows) == 1:
                self._process_row(rows[0])
            else:
                self._process_batch(rows)

    def _process_batch(self, rows: list) -> None:
        """Run batch extraction on multiple queue rows in one
        Claude call. RDR-089 Phase D — cost-amortised drain.

        Each row's content was captured at enqueue time when in scope.
        CLI rows with empty content get a per-row source-path read
        before the batch call (the batch extractor itself does NOT
        do disk reads — that responsibility lives here so the
        extractor stays a pure function of its inputs).
        """
        from pathlib import Path

        from nexus.aspect_extractor import (
            _source_content_from_t3,
            extract_aspects_batch,
            select_config,
        )
        from nexus.mcp_infra import t2_ctx

        # extract_aspects_batch requires every input to share a single
        # ExtractorConfig. claim_batch grabs FIFO across collections, so
        # a knowledge__ row enqueued before an rdr__ row lands in the
        # same claim and crosses the homogeneity boundary. Fall back to
        # per-row processing instead of marking the whole batch failed.
        configs = {select_config(row.collection) for row in rows}
        if len(configs - {None}) > 1:
            _log.info(
                "aspect_worker_batch_heterogeneous_fallback",
                row_count=len(rows),
                configs=sorted(c.extractor_name for c in configs if c is not None),
            )
            for row in rows:
                self._process_row(row)
            return

        # Per-row content source: prefer queued content, fall back to
        # T3 reassembly (same text we indexed; section-filtered for
        # scholarly papers), then to disk read as last resort. Mirrors
        # the single-doc path in extract_aspects so batch and single
        # share the same sourcing precedence.
        #
        # TODO(RDR-096 P5.1): adopt URI-based read_source + ExtractFail
        # in this batch path. The single-doc path migrated in P1.2;
        # the batch path still uses the deprecated _source_content_from_t3
        # + disk fallback because extract_aspects_batch returns
        # _empty_record on missing content rather than ExtractFail.
        # When the batch return type widens, this pre-fetch block can
        # delete itself in favour of read_source per row.
        items: list[tuple[str, str, str]] = []
        for row in rows:
            content = row.content
            if not content:
                content = _source_content_from_t3(row.collection, row.source_path)
            if not content:
                try:
                    content = Path(row.source_path).read_text(
                        encoding="utf-8", errors="replace",
                    )
                except (OSError, UnicodeDecodeError) as exc:
                    _log.warning(
                        "aspect_worker_batch_source_path_unreadable",
                        source_path=row.source_path,
                        error=str(exc),
                    )
                    # Push through with empty content; the batch
                    # extractor will null-field this entry.
                    content = ""
            items.append((row.collection, row.source_path, content))

        try:
            records = _extract_aspects_batch(items)
        except Exception as exc:
            _log.warning(
                "aspect_worker_batch_extract_raised",
                row_count=len(rows),
                exc_info=True,
            )
            try:
                with t2_ctx() as t2:
                    for row in rows:
                        t2.aspect_queue.mark_failed(
                            row.collection, row.source_path,
                            error=str(exc),
                        )
            except Exception:
                _log.warning(
                    "aspect_worker_batch_mark_failed_persist_failed",
                    exc_info=True,
                )
            return

        try:
            with t2_ctx() as t2:
                for row, record in zip(rows, records):
                    if record is None:
                        # Unsupported collection — drop silently.
                        t2.aspect_queue.mark_done(
                            row.collection, row.source_path,
                        )
                        continue
                    t2.document_aspects.upsert(record)
                    t2.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
        except Exception:
            _log.warning(
                "aspect_worker_batch_persist_failed",
                exc_info=True,
            )

    def _process_row(self, row) -> None:
        """Run extraction on one queue row and dispatch on the result."""
        from nexus.mcp_infra import t2_ctx
        try:
            # Content was captured at enqueue time when in scope (MCP
            # store_put). For CLI rows where content was not in scope
            # at enqueue, ``row.content`` is "" and the extractor's
            # content-sourcing fallback will read source_path.
            # nexus-tdgc: when the queue row carries a doc_id, build a
            # lookup callable so the chroma reader can route to the
            # doc_id-keyed chunk lookup. Empty doc_id passes through as
            # a None lookup; the extractor falls back to legacy probes.
            queued_doc_id = getattr(row, "doc_id", "") or ""
            doc_id_lookup = (
                (lambda _coll, _sid, _d=queued_doc_id: _d)
                if queued_doc_id
                else None
            )
            record = _extract_aspects(
                content=row.content,
                source_path=row.source_path,
                collection=row.collection,
                doc_id_lookup=doc_id_lookup,
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
                # RDR-096 P1.2: ExtractFail is the typed read-failure
                # sentinel. No row written; mark queue done so we
                # don't retry the unreadable source on every drain.
                # Operators can re-enqueue manually after fixing the
                # source identity.
                if isinstance(record, ExtractFail):
                    _log.info(
                        "aspect_worker_extract_skip",
                        uri=record.uri,
                        reason=record.reason,
                        detail=record.detail,
                    )
                    t2.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
                    return
                # AspectRecord — either a populated record or a null-
                # fields record from a subprocess-side failure (the
                # extractor already retried up to 3 attempts
                # internally for those paths). Persist and remove
                # from the queue.
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


def _worker_lock_path(locks_dir: Path | None = None) -> Path:
    """Return the canonical lock file path for this process.

    Lock file name: ``aspect_worker.<os.getpid()>``.

    SIG-5 (nexus-1091): the MCP server writes this file when its worker
    starts so that CLI-side ``drain_worker`` can detect the conflict.
    """
    import os

    base = locks_dir if locks_dir is not None else (
        Path.home() / ".config" / "nexus" / "locks"
    )
    return base / f"aspect_worker.{os.getpid()}"


def _write_worker_lock(locks_dir: Path | None = None) -> None:
    """Write a process-scoped lock file advertising this worker.

    Non-fatal if the locks directory cannot be created or the file cannot
    be written — the lock is advisory; a missing file merely means
    ``drain_worker`` from another process will not detect this worker.
    """
    import os

    try:
        lock = _worker_lock_path(locks_dir)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
    except Exception:
        _log.warning("aspect_worker_lock_write_failed", exc_info=True)


def _remove_worker_lock(locks_dir: Path | None = None) -> None:
    """Remove the process-scoped lock file when the worker stops.

    Non-fatal if the file does not exist or cannot be removed.
    """
    try:
        _worker_lock_path(locks_dir).unlink(missing_ok=True)
    except Exception:
        _log.warning("aspect_worker_lock_remove_failed", exc_info=True)


def ensure_worker_started(
    *,
    poll_interval: float = 2.0,
    stale_timeout_seconds: int = 300,
    _locks_dir: Path | None = None,
) -> AspectExtractionWorker:
    """Lazy-start the singleton worker. Returns the worker.

    Idempotent -- calling once, twice, or N times all return the same
    instance and only spawn one thread. Tuning parameters apply only
    to the first call (subsequent calls cannot retune).

    Writes a process-scoped lock file (SIG-5) so that CLI-side
    ``drain_worker`` calls in other processes can detect an active
    MCP worker and raise ``DrainBlockedByActiveWorker`` with operator
    guidance rather than silently missing queue rows.
    """
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = AspectExtractionWorker(
                poll_interval=poll_interval,
                stale_timeout_seconds=stale_timeout_seconds,
            )
        _worker.start()
    _write_worker_lock(_locks_dir)
    return _worker


def stop_worker(timeout: float = 10.0, _locks_dir: Path | None = None) -> None:
    """Stop the singleton worker if running. Idempotent.

    Note: leaves the singleton instance in place (stopped) so a
    subsequent ``ensure_worker_started`` call rebuilds the daemon
    thread on the existing instance. Tests that need a fresh
    instance should call ``reset_worker_for_tests`` instead.

    Removes the process-scoped lock file (SIG-5) when the worker stops.
    """
    global _worker
    with _worker_lock:
        worker = _worker
    if worker is not None:
        worker.stop(timeout=timeout)
    _remove_worker_lock(_locks_dir)


def reset_worker_for_tests() -> None:
    """Test helper: tear down the worker singleton so the next
    ``ensure_worker_started`` call rebuilds with fresh state.
    """
    global _worker
    stop_worker(timeout=5.0)
    with _worker_lock:
        _worker = None


def _check_mcp_worker_lock(locks_dir: Path) -> None:
    """Check for active MCP-process aspect workers via lock files.

    Scans ``locks_dir`` for ``aspect_worker.<pid>`` files.  For each:
      - If the PID matches the current process: skip (drain can stop its
        own worker directly; not a cross-process conflict).
      - If the PID is alive in a different process: raises
        ``DrainBlockedByActiveWorker``.
      - If the PID is dead (stale lock): removes the lock file silently.

    SIG-5 (nexus-1091): drain_worker is process-local.  An active MCP
    server holds its own worker in a separate OS process.  Draining here
    only stops the current process's worker; the MCP worker keeps running
    and may continue to write queue rows that the PK migration would race
    against.  The lock file check surfaces this cross-process conflict
    before the migration begins rather than after.

    Args:
        locks_dir: Directory to scan for lock files.  If it does not
            exist, the check is skipped (no MCP server ever registered).
    """
    import os

    if not locks_dir.exists():
        return

    own_pid = os.getpid()

    for lock_file in locks_dir.glob("aspect_worker.*"):
        try:
            pid = int(lock_file.name.rsplit(".", 1)[-1])
        except ValueError:
            continue  # Not a PID-suffixed lock file — skip.

        if pid == own_pid:
            # The current process wrote this lock; drain can stop its own
            # worker directly.  Not a cross-process conflict.
            continue

        # Probe whether the PID is alive.  os.kill(pid, 0) returns without
        # error if the process exists and we have permission to signal it.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # PID does not exist — stale lock; clean up and continue.
            _log.info(
                "drain_worker_stale_lock_removed",
                lock_file=str(lock_file),
                pid=pid,
            )
            lock_file.unlink(missing_ok=True)
            continue
        except PermissionError:
            # PID exists but we cannot signal it (different user) — treat
            # as alive and block the drain.
            pass

        # PID is alive in a different process — block the drain.
        raise DrainBlockedByActiveWorker(blocking_pid=pid, lock_file=lock_file)


def drain_worker(
    queue_path: Path | str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.1,
    _locks_dir: Path | None = None,
) -> None:
    """Stop the singleton worker and wait until the queue is fully drained.

    A queue is considered drained when
    ``SELECT count(*) FROM aspect_extraction_queue WHERE status != 'failed'``
    returns 0 — i.e., no rows are pending or in_progress.  Failed rows
    are terminal and do not block a PK migration.

    Protocol (RDR-108 Phase 1 S1, nexus-he24):

      1. Check for active MCP-process workers via lock files (SIG-5).
         If a live MCP worker is detected, raise ``DrainBlockedByActiveWorker``
         immediately with operator guidance.
      2. Set the stop signal on the singleton worker (if one exists) so
         it claims no new rows.  In-flight row processing continues.
      3. Poll the queue at ``poll_interval`` (default 100 ms) until
         ``is_drained()`` returns True or ``timeout`` elapses.
      4. On success: join the worker thread (it has already exited or will
         exit imminently after the last iteration).  If the thread does not
         exit within 2 seconds, log a warning (S-5) and continue — the
         thread is a daemon and will die with the process.
      5. On timeout: raise ``DrainTimeoutError`` so the operator is alerted
         to inspect stuck ``in_progress`` rows.

    Args:
        queue_path: Path to the T2 SQLite database file.  Used to open a
            short-lived read-only connection for the ``is_drained()`` poll
            separate from the worker's own T2 context so this function does
            not compete for locks.
        timeout: Seconds to wait for the queue to drain.  Default 120s.
            RDR-089 P1.3 measured ~26.5s median extraction time with a
            tail to 90s for the scholarly-paper-v1 extractor.  120s
            (approximately 4x median) provides adequate margin while
            still surfacing genuinely stuck workers within two minutes.
            Callers that know their workload is lighter may pass a smaller
            value.
        poll_interval: Seconds between is_drained() checks (default 0.1).
        _locks_dir: Override the locks directory for testing.  Production
            code should leave this as None (resolved to
            ``~/.config/nexus/locks``).

    Raises:
        DrainBlockedByActiveWorker: An active MCP-process worker was
            detected via a lock file.  Stop the MCP server before running
            the migration.
        DrainTimeoutError: Queue still has pending/in_progress rows after
            ``timeout`` seconds.

    Note:
        If no worker has been started in this process (``get_worker()``
        returns None), the function simply checks ``is_drained()`` once and
        returns — there is no thread to stop and no in-flight work to wait
        for.  This handles the quiescent case (e.g., the caller's process
        never ran the worker, or the worker finished and was reset).
    """
    from nexus.db.t2.aspect_extraction_queue import AspectExtractionQueue

    # SIG-5: detect active MCP-process workers before stopping the local
    # singleton.  A live MCP worker in another process drains its own
    # queue independently; the migration must not run while that worker is
    # alive or it will race against in_progress rows it cannot see.
    locks_dir = _locks_dir if _locks_dir is not None else (
        Path.home() / ".config" / "nexus" / "locks"
    )
    _check_mcp_worker_lock(locks_dir)

    worker = get_worker()
    if worker is not None:
        worker.stop_claiming()

    queue_path = Path(queue_path)
    deadline = time.monotonic() + timeout

    queue = AspectExtractionQueue(queue_path)

    def _join_worker_thread(w: AspectExtractionWorker) -> None:
        """Join the worker thread; log a warning if it does not exit in 2s.

        S-5 (nexus-1091): the original join was silent on timeout.  A stuck
        thread after stop_claiming() is unexpected and warrants an operator-
        visible warning so the hang is not silently ignored.
        """
        with _worker_lock:
            thread = w._thread
            w._thread = None
        if thread is None:
            return
        thread.join(timeout=2.0)
        if thread.is_alive():
            _log.warning(
                "drain_worker_thread_join_timeout",
                thread_id=thread.ident,
                timeout=2.0,
            )

    try:
        # Short-circuit: if already drained, return immediately.
        if queue.is_drained():
            if worker is not None and worker.is_running():
                _join_worker_thread(worker)
            return

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            if queue.is_drained():
                # Join the worker thread: it has stopped claiming and will
                # exit after its current iteration completes.
                if worker is not None:
                    _join_worker_thread(worker)
                return

        # Timeout: count stuck rows for the error message.
        stuck = queue.conn.execute(
            "SELECT COUNT(*) FROM aspect_extraction_queue WHERE status != 'failed'"
        ).fetchone()
        stuck_count = stuck[0] if stuck else 0
        raise DrainTimeoutError(stuck_count=stuck_count, timeout=timeout)
    finally:
        queue.close()


# ── Hook function (registered via register_post_document_hook) ──────────────


def aspect_extraction_enqueue_hook(
    source_path: str,
    collection: str,
    content: str,
    *,
    doc_id: str = "",
) -> None:
    """Post-document hook: enqueue a row for async aspect extraction.

    Signature matches ``fire_post_document_hooks(source_path,
    collection, content)``. This hook does NOT call extract_aspects
    inline — that would block the ingest path for ~25 s per document
    (RDR-089 P1.3 spike finding). Instead it:

      1. Skips collections without a registered extractor config
         (currently ``knowledge__*``, ``rdr__*``, ``docs__*``).
      2. Writes a pending row to ``aspect_extraction_queue``
         (microsecond-scale T2 INSERT). The row carries ``content``
         when non-empty (MCP path) so the worker has the document
         text without needing to re-read from disk; CLI paths pass
         ``content=""`` and the worker falls back to a file read.
      3. Lazy-starts the worker if not already running.

    The hook is synchronous (RDR-089 P0.1 contract). It does not
    block on extraction; the worker drains in a background thread.

    Content-sourcing contract (audit F4 + critical-issue fix):
      * MCP store_put → ``content=<full document text>`` is in scope.
        The hook persists it in the queue so the worker reads from
        the row, not from disk (``source_path`` is a doc_id at the
        MCP boundary, not a real filesystem path).
      * CLI ingest → ``content=""`` (chunk scope only). Queue row
        carries the empty string; worker falls back to
        ``Path(source_path).read_text()``.
    """
    from nexus.aspect_extractor import select_config
    if select_config(collection) is None:
        return  # No extractor for this collection — nothing to enqueue.
    from nexus.mcp_infra import t2_ctx
    try:
        with t2_ctx() as t2:
            t2.aspect_queue.enqueue(
                collection, source_path, content=content,
                doc_id=doc_id,
            )
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
