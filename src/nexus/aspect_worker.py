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
* Polls every ``poll_interval`` seconds (default ``DEFAULT_POLL_INTERVAL_S``,
  30 s — nexus-59611; the idle poll is edge load, see the constant).
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
* Uncaught exception in the worker body → routed by
  ``_mark_retry_or_fail_routed`` into one of THREE classes
  (nexus-yg70j): a transient remote failure retries with backoff;
  a bad document terminal-fails for triage; a SELF FAULT — this
  process's own environment is broken — writes no row state at
  all, stands the worker down, and leaves the claimed rows for
  ``reclaim_stale``. Only the middle class is terminal, and it is
  terminal until manually re-enqueued.

The hook function ``aspect_extraction_enqueue_hook`` is registered
in ``mcp/core.py`` alongside the other post-document consumers. It
checks ``select_config(collection)`` first — if the collection has
no registered extractor, the hook is a no-op (no queue row, no
worker spawn).
"""
from __future__ import annotations

import errno
import os
import random
import re
import threading
import time
from collections.abc import Callable
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
from nexus import config as _config

_log = structlog.get_logger(__name__)

#: RDR-173 P2: the tenant scope the enqueue hook ensures a leased aspect-worker
#: daemon for. v1 is single-tenant; multi-tenant routing derives this from
#: request context (see the TODO in _ensure_aspect_worker). Matches
#: HttpAspectQueue.DEFAULT_TENANT.
_ENQUEUE_TENANT: str = "default"

# ── Bounded backoff-retry ladder (RDR-163 P1, nexus-ztpt6) ──────────────────
#
# A transient failure re-queues the row with a backoff interval the WORKER
# chooses; the service stamps the absolute next_retry_at = now()+interval
# server-side. retry_count is monotonic and is the cap's source of truth, so
# termination is guaranteed even under the ±1 race of concurrent reclaim.
_RETRY_MAX_ATTEMPTS: int = 5          # terminal mark_failed once retry_count >= cap
_RETRY_BASE_SECONDS: float = 30.0     # interval = base * 2**retry_count, jittered
_RETRY_JITTER_FRACTION: float = 0.2   # ±20% — spreads re-claims when a wide outage clears

#: Idle ``claim_batch`` poll BACKSTOP cadence (nexus-59611 stage 2,
#: 2026-08-29). This default IS the production cadence: the leased daemon
#: host builds the worker zero-arg, there is no env/config override, and the
#: engine holds no long poll (``AspectHandler`` reads ``limit`` and answers
#: immediately). Real-time claiming for the arrivals that matter no longer
#: depends on this interval: the producer and consumer are both nexus, and
#: for a same-box enqueue (the common case — the indexing box's own
#: aspect-worker daemon claims what it just enqueued) the producer touches
#: the local wake file (``signal_aspect_wake``) after a successful enqueue,
#: which wakes ``_wait_for_wake_or_timeout`` within one ``WAKE_CHECK_INTERVAL_S``
#: tick. This constant now covers only the residual cases the wake file
#: cannot: cross-box arrivals (cloud, a different box's enqueue) and a missed
#: or lost wake signal. At the stage-1 30 s the poll was still ~93% empty
#: (conexus edge measurement, conexus-ppwe) against an arrival rate of ~16
#: items/hour; 300 s removes nearly all of the remaining self-generated edge
#: load while the wake path keeps same-box claim latency effectively
#: immediate. Deliberately NOT a knob.
DEFAULT_POLL_INTERVAL_S: float = 300.0

#: Filename of the local same-box wake signal, under ``nexus_config_dir()``.
#: No wire change, no LISTEN/NOTIFY: a producer that just enqueued a row
#: touches this file; the worker's poll wait notices the mtime change.
ASPECT_WAKE_FILENAME: str = "aspect_wake"

#: How often the poll wait re-checks the wake file's mtime (and the stop
#: event), in seconds. Bounds worst-case wake latency and stop-signal
#: latency to one tick regardless of ``poll_interval``.
WAKE_CHECK_INTERVAL_S: float = 1.0


def signal_aspect_wake(config_dir: Path | None = None) -> None:
    """Touch the local wake file so a same-box aspect worker's poll wait
    returns within one ``WAKE_CHECK_INTERVAL_S`` tick instead of waiting out
    ``DEFAULT_POLL_INTERVAL_S`` (nexus-59611 stage 2).

    Best-effort by design: producer and consumer are both nexus and, for the
    arrivals that matter, on the same box — but a cross-box arrival (cloud,
    a different box's enqueue) has no local worker to wake and is covered by
    the backstop poll instead, so a failure to touch here is never fatal to
    the caller. Any ``OSError`` (missing/unwritable config dir, races) is
    logged at debug and swallowed; it must never fail an enqueue.
    """
    path = (config_dir if config_dir is not None else _config.nexus_config_dir()) / ASPECT_WAKE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError as exc:
        _log.debug("aspect_wake_signal_failed", error=str(exc))


def _is_retryable(exc: BaseException) -> bool:
    """True if *exc* is a transient failure class worth a backoff retry.

    Reuses BOTH ``retry.py`` transient predicates: the vector-service class
    (transport errors, HTTP 429/502/503/504; the SQLite 'database is locked'
    leg is gone with the substrate) AND
    the Voyage class (API overload/timeout). Everything else — ValueError, type
    errors, malformed records, programming bugs — is non-retryable and
    terminal-fails immediately (no wasted retries). A classifier dependency that
    itself raises (e.g. the lazy ``voyageai`` import) must not break routing, so
    each predicate is guarded and defaults to "not retryable".
    """
    from nexus.retry import (  # noqa: PLC0415 — deferred to avoid import cost at module load
        _is_retryable_vector_error,
        _is_retryable_voyage_error,
    )
    for predicate in (_is_retryable_vector_error, _is_retryable_voyage_error):
        try:
            if predicate(exc):
                return True
        except Exception as cls_exc:  # noqa: BLE001 — a classifier dependency must not break routing
            # Observable signal: e.g. voyageai not installed makes the voyage
            # predicate raise, which would silently route all API-overload
            # failures to terminal. Log so an operator can see why.
            _log.debug(
                "aspect_worker_retry_classifier_unavailable",
                predicate=getattr(predicate, "__name__", repr(predicate)),
                error=str(cls_exc),
            )
            continue
    return False


# ── The third fault class: self / infrastructure (nexus-yg70j) ──────────────
#
# ``_is_retryable`` above describes a TWO-BUCKET world:
#
#     TRANSIENT-REMOTE   the far side is briefly unwell   -> retry
#     BAD-DOCUMENT       this row is malformed            -> terminal
#
# A fault in the worker's OWN environment is neither, and before this it
# reached terminal by OMISSION rather than by judgement: nothing claimed it
# was a bad document, it simply fell through to the else-branch. On
# 2026-08-24 that omission recorded "failed" against 26 healthy production
# rows because the daemon was standing in a deleted directory.
#
# The distinguishing question is not what the exception WAS. FileNotFoundError
# is a fact about the document when the document is missing and a fact about
# the process when ``getcwd()`` raised, and the type cannot tell you which. So
# this predicate MEASURES THE PROCESS instead of classifying the exception.

#: Errnos that can never be a property of a document — the host is out of a
#: resource, or its filesystem cannot be written. Deliberately EXCLUDED:
#: ENOENT / EACCES / EPERM / EISDIR / ENOTDIR, each of which is genuinely
#: ambiguous with "this row's path is wrong", which IS a fact about the row.
#: A false positive here strands healthy rows AND kills a healthy daemon, so
#: this set stays narrow; widening it is a decision, not a tidy-up.
_ENVIRONMENT_ERRNOS: frozenset[int] = frozenset(
    code for code in (
        getattr(errno, name, None)
        for name in ("ENOSPC", "EROFS", "EMFILE", "ENFILE", "ENOMEM", "EDQUOT")
    )
    if code is not None
)

#: Bounded so a self-referential ``__context__`` chain cannot spin here.
_CAUSE_CHAIN_MAX_DEPTH: int = 8


def _self_fault_reason(exc: BaseException) -> str | None:
    """Return a short reason iff the WORKER'S OWN environment is broken, else
    ``None``.

    A self fault means the failure had nothing to do with the row that happened
    to be in hand when it surfaced, so no verdict about that row is honest —
    neither ``mark_failed`` (terminal is a claim about the document) nor
    ``mark_retry`` (retry says the document may succeed later, which is true
    here but for reasons that are not about the document).

    Two arms:

    1. POSITIVE CONTROL on the process. ``os.getcwd()`` raising is proof — not
       inference — that this process is standing in a directory that no longer
       exists, and every relative path it resolves from here is nonsense. A
       successful call is equally load-bearing: it shows the check can return
       the healthy answer, so a failing one is evidence rather than absence.
    2. HOST RESOURCE EXHAUSTION by errno (see ``_ENVIRONMENT_ERRNOS``), walked
       down the cause chain because the batch extractor wraps.

    Cost is one ``getcwd()`` syscall per FAILED row, which is a rare path.
    """
    try:
        os.getcwd()
    except OSError as cwd_exc:
        return f"cwd_unresolvable ({type(cwd_exc).__name__}: {cwd_exc})"

    cursor: BaseException | None = exc
    for _ in range(_CAUSE_CHAIN_MAX_DEPTH):
        if cursor is None:
            break
        if isinstance(cursor, MemoryError):
            return "host_out_of_memory"
        if isinstance(cursor, OSError) and cursor.errno in _ENVIRONMENT_ERRNOS:
            name = errno.errorcode.get(cursor.errno, str(cursor.errno))
            return f"host_resource_exhausted ({name})"
        cursor = cursor.__cause__ or cursor.__context__
    return None


def _backoff_interval_seconds(retry_count: int, *, rng=random.random) -> int:
    """Worker-chosen backoff for the next retry, in integer seconds.

    ``base * 2**retry_count`` with ±``_RETRY_JITTER_FRACTION`` jitter. The
    service stamps the absolute ``next_retry_at = now()+interval``; only the
    interval is chosen here. ``rng`` is injectable so the jitter is deterministic
    under test without patching the global ``random`` module.
    """
    raw = _RETRY_BASE_SECONDS * (2 ** max(0, retry_count))
    jitter = 1.0 + (rng() - 0.5) * 2.0 * _RETRY_JITTER_FRACTION
    return max(0, int(raw * jitter))


# ── Drain protocol exceptions ───────────────────────────────────────────────


class DrainTimeoutError(RuntimeError):
    """Raised by ``drain_worker`` when the queue still has actionable rows
    (``status != 'failed'``) after the configured timeout.

    Actionable rows are those in ``pending`` or ``in_progress`` state.
    The operator must triage: inspect ``aspect_extraction_queue`` for
    stuck ``in_progress`` rows, kill any hung workers, and re-run the
    drain after the queue is clear.
    """

    def __init__(
        self,
        stuck_count: int,
        timeout: float,
        detail: str | None = None,
    ) -> None:
        self.stuck_count = stuck_count
        self.timeout = timeout
        self.detail = detail
        msg = (
            f"Drain timeout after {timeout:.1f}s: "
            f"{stuck_count} actionable row(s) remain in aspect_extraction_queue "
            f"(status pending or in_progress). "
            "Triage stuck workers before retrying the PK migration."
        )
        if detail:
            msg = f"{msg} {detail}"
        super().__init__(msg)


class AspectExtractionWorker:
    """Background drain thread for ``aspect_extraction_queue``.

    Owns one daemon thread that polls the queue and processes one
    row per iteration. Idle when the queue is empty (sleeps
    ``poll_interval`` seconds). Stop via ``stop()`` for clean
    shutdown; otherwise dies with the process (daemon).

    Constructor injection: caller may pass ``poll_interval`` and
    ``stale_timeout`` for tests; defaults are tuned for production.

    Stale-row reclamation is owned by the T2 daemon, not this worker
    (nexus-we61e) — see ``_run_loop``.
    """

    # Batch-extraction threshold (RDR-089 Phase D). When the queue
    # has at least this many pending rows, drain them in a single
    # ``extract_aspects_batch`` call rather than per-row. Below this,
    # use the single-paper path so small queues don't pay the
    # batch-overhead tax for one-or-two-paper drains.
    _DEFAULT_BATCH_SIZE = 5

    def __init__(
        self,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        stale_timeout_seconds: int = 60,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        on_self_fault: Callable[[str], None] | None = None,
        config_dir: Path | None = None,
    ) -> None:
        # nexus-59611 stage 2 (review): the wake file lives under the config
        # dir the PRODUCER writes to. A daemon started with --config-dir X
        # diverges from the ambient dir by design, so the host binds its own
        # dir here (or via bind_config_dir); None means the ambient
        # nexus_config_dir(), which is the loose in-process worker's case.
        self._config_dir: Path | None = config_dir
        self._poll_interval = poll_interval
        self._stale_timeout_seconds = stale_timeout_seconds
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # nexus-yg70j: the host's stand-down hook. A worker running loose in an
        # nx-mcp process has no host and simply stops claiming; a worker hosted
        # by AspectWorkerDaemon hands the fault up so the PROCESS exits, which
        # is what makes the failure loud and what gets a fresh environment.
        self._on_self_fault: Callable[[str], None] | None = on_self_fault
        self._self_fault: str | None = None
        self._self_fault_lock = threading.Lock()
        # nexus-59611 stage 2: mtime baseline for the local wake file, so a
        # touch is consumed exactly once. None means "no wake file observed
        # yet" (absent, or not yet baselined by start()).
        self._last_wake_mtime: float | None = None

    def set_self_fault_handler(self, handler: Callable[[str], None] | None) -> None:
        """Install the host's stand-down hook after construction.

        ``AspectWorkerDaemon`` receives its worker from an injected ZERO-ARG
        factory, so it cannot pass this through the constructor without
        changing that protocol for every caller and test fake. Direct
        constructions use the ``on_self_fault`` kwarg instead.
        """
        self._on_self_fault = handler

    def self_fault(self) -> str | None:
        """The reason this worker stood down, or ``None`` if it is healthy."""
        with self._self_fault_lock:
            return self._self_fault

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent — calling twice does
        not spawn a second thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            # A latched fault would otherwise survive the restart and suppress
            # the NEXT stand-down entirely (the latch also gates _stop_event).
            with self._self_fault_lock:
                self._self_fault = None
            # nexus-59611 stage 2: baseline the wake file's current mtime (or
            # None if absent) so a stale touch from BEFORE this start() does
            # not fire the very first wait — only a touch AFTER this point
            # counts as a wake.
            self._last_wake_mtime = self._read_wake_mtime()
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

    def bind_config_dir(self, config_dir: Path) -> None:
        """Point the wake-file read at *config_dir* (the host daemon's own
        dir) and re-baseline so a touch that predates the bind is not
        mistaken for a fresh wake (nexus-59611 stage 2)."""
        self._config_dir = Path(config_dir)
        self._last_wake_mtime = self._read_wake_mtime()

    def _wake_config_dir(self) -> Path:
        return self._config_dir if self._config_dir is not None else _config.nexus_config_dir()

    def _read_wake_mtime(self) -> float | None:
        """Current mtime of the local wake file, or ``None`` if absent."""
        try:
            return (self._wake_config_dir() / ASPECT_WAKE_FILENAME).stat().st_mtime
        except OSError:
            return None

    def _wait_for_wake_or_timeout(self, timeout: float) -> None:
        """Sleep up to *timeout* seconds, returning early on ``stop()`` /
        ``stop_claiming()`` or a NEW touch of the local wake file
        (nexus-59611 stage 2).

        Checked every ``WAKE_CHECK_INTERVAL_S`` so the stop signal keeps
        interrupting the wait promptly (unchanged contract — the stop event
        alone already wakes ``Event.wait`` immediately when set from another
        thread; the tick exists for the wake-file check, which has no
        cross-thread event to wait on). ``_last_wake_mtime`` is instance
        state, not re-baselined per call: a touch is consumed exactly
        once — a touch that landed before this wait began is already the
        remembered baseline and does not fire, and a touch observed mid-wait
        both wakes THIS call and becomes the new baseline so it cannot also
        wake the NEXT call.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._stop_event.wait(min(WAKE_CHECK_INTERVAL_S, remaining)):
                return
            current = self._read_wake_mtime()
            if current != self._last_wake_mtime:
                self._last_wake_mtime = current
                return

    def _run_loop(self) -> None:
        """Drain loop. Runs until ``_stop_event`` is set.

        Each iteration:
          1. Claim a batch of up to ``batch_size`` pending rows.
          2. If empty: sleep ``poll_interval``.
          3. If batch_size >= 2: invoke extract_aspects_batch (one
             Claude call extracts all rows). RDR-089 Phase D path —
             cost-amortises across rows.
          4. If batch_size == 1: invoke single-paper extract_aspects
             (existing path). Small-queue case where batch overhead
             is not worth it.

        Stale-row reclamation is NOT done here (nexus-we61e). It is a
        GLOBAL janitor op, but this worker runs inside every nx-mcp
        process, so N workers each RPC'd a redundant ``reclaim_stale``
        UPDATE into the single T2 daemon — N-fold WAL contention that
        pegged a core on ``database is locked`` after a restart with a
        stale-row backlog. Reclaim now runs exactly once, on the
        daemon's own periodic loop (``T2Daemon._reclaim_stale_loop``),
        which is singular by construction.

        All exceptions inside the loop are caught and recorded; the
        worker thread itself never dies from a row's failure.
        """
        from nexus.db.t2.records import QueueRow  # noqa: PLC0415 — deferred to avoid circular import (db.t2 <-> aspect_worker)
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra <-> aspect_worker)
        from nexus.migration.state import is_migrating as _migration_in_progress  # noqa: PLC0415 — deferred to avoid circular import (migration.state)
        while not self._stop_event.is_set():
            # RDR-159 P1c (S2 quiesce): while a guided upgrade migration holds
            # the migration.state sentinel, SUSPEND the claim/extract cycle.
            # This is the cross-process suspend the process-local drain_worker
            # cannot reach (workers resident in other nx-mcp processes). The
            # worker resumes on its next cycle once the sentinel clears (UNLOCK).
            if _migration_in_progress():
                self._wait_for_wake_or_timeout(self._poll_interval)
                continue
            try:
                # RDR-128 P3 (nexus-sbxbe.3): route the claim through the
                # T2 daemon so this worker — which runs inside every
                # nx-mcp process — stops opening memory.db directly and
                # contending on its single WAL writer lock. This is the
                # every-2s contention behind the recurring
                # `aspect_worker_claim_failed` / `database is locked`
                # incidents (memory: daemon-restart-not-worker-fix).
                # nexus-zir76: the persist path (_process_row /
                # _process_batch) now routes too, via the daemon-side
                # `complete_aspect` method (AspectRecord travels as an
                # asdict() field dict). The worker no longer opens
                # memory.db on ANY path.
                #
                # nexus-we61e: ``reclaim_stale`` is deliberately NOT
                # called here. It is a global janitor op and running it
                # from every per-process worker meant N redundant reclaim
                # UPDATEs RPC'd into the one daemon. The daemon now owns
                # reclaim on its own periodic loop.
                def _poll(t2):
                    return t2.aspect_queue.claim_batch(self._batch_size)

                rows = t2_index_write(_poll)
                # The daemon RPC decodes QueueRow to a plain dict; the
                # direct-fallback path returns QueueRow objects. Normalise
                # so downstream attribute access (row.collection, ...) is
                # uniform regardless of which path served the claim.
                rows = [
                    r if isinstance(r, QueueRow) else QueueRow(**r)
                    for r in rows
                ]
            except Exception:  # noqa: BLE001 — best-effort claim loop: must not crash worker thread, logged + retried
                _log.warning("aspect_worker_claim_failed", exc_info=True)
                self._wait_for_wake_or_timeout(self._poll_interval)
                continue

            if not rows:
                self._wait_for_wake_or_timeout(self._poll_interval)
                continue

            if len(rows) == 1:
                self._process_row(rows[0])
            else:
                self._process_batch(rows)

    def _process_batch(self, rows: list) -> None:
        """Run batch extraction on multiple queue rows in one
        Claude call. RDR-089 Phase D — cost-amortised drain.

        Each row's content was captured at enqueue time when in
        scope. CLI rows with empty content are sourced via
        ``chroma://`` URI inside ``extract_aspects_batch`` (RDR-096
        P5.1 / nexus-8g79.34 — the batch path now mirrors the
        single-doc extractor's read contract).
        """
        import dataclasses  # noqa: PLC0415 — stdlib import deferred to method scope (rare branch)

        from nexus.aspect_extractor import select_config  # noqa: PLC0415 — deferred to avoid circular import (aspect_extractor)
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)

        # extract_aspects_batch now supports mixed configs internally
        # (nexus-kmbys: it partitions by per-document resolved config and
        # runs deterministic parser_fn configs inline). This pre-filter is
        # retained as a conservative simplification: a claim mixing prefix
        # configs (knowledge__ + rdr__) falls back to per-row processing
        # rather than relying on the batch partition path for cross-prefix
        # mixes. Same-prefix claims (the common case) go through the batch.
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

        # nexus-8g79.34: build manifest_lookup once; pass queue-captured
        # doc_id per row via the 4-tuple form (extract_aspects_batch
        # uses it to construct the per-row doc_id_lookup, matching
        # _process_row's pattern at lines 406-411).
        manifest_lookup = None
        try:
            from nexus.commands.enrich import _build_catalog_manifest_lookup  # noqa: PLC0415 — deferred to avoid circular import (commands.enrich)
            manifest_lookup = _build_catalog_manifest_lookup()
        except Exception as exc:  # noqa: BLE001 — optional manifest-lookup enrichment; absence is non-fatal, falls through
            _log.debug("aspect_worker_manifest_lookup_unavailable", error=str(exc))

        items: list[tuple[str, str, str, str]] = [
            (row.collection, row.source_path, row.content,
             getattr(row, "doc_id", "") or "")
            for row in rows
        ]

        try:
            records = _extract_aspects_batch(items, manifest_lookup=manifest_lookup)
        except Exception as exc:  # noqa: BLE001 — batch extract is best-effort; failure logged and rows re-queued
            _log.warning(
                "aspect_worker_batch_extract_raised",
                row_count=len(rows),
                exc_info=True,
            )
            # nexus-yg70j defect A: ROUTE, do not terminal-fail the batch.
            #
            # This arm used to call mark_failed() on EVERY row -- terminal,
            # "terminal until re-enqueued" -- for ANY batch-extract exception.
            # The single-row path at the bottom of this method already routes
            # through _mark_retry_or_fail_routed, which carries the real
            # transient-vs-terminal taxonomy (RDR-163 P1, nexus-ztpt6) and a
            # retry budget with backoff. Only the batch path bypassed it.
            #
            # MEASURED COST of that bypass, on the 2026-08-24 incident's 26
            # terminally-failed rows:
            #     7  relative source_paths  -- genuine victims of the fault
            #    19  absolute source_paths  -- ALL 19 EXIST ON DISK
            # The 19 were healthy documents killed solely by sharing a batch of
            # five with one bad neighbour. 73% collateral. A per-batch verdict
            # on a per-row property is the defect: the batch is a transport
            # detail, not a fact about any document in it.
            #
            # The router decides per row: a SELF FAULT (this process's own
            # environment is broken -- a deleted cwd, this incident's actual
            # cause) writes no row state at all and stands the worker down;
            # otherwise a non-retryable exception or an exhausted budget is
            # terminal, and anything else is mark_retry with backoff. The self
            # fault is latched inside the router, so this loop reports one
            # episode rather than one per row.
            for row in rows:
                try:
                    self._mark_retry_or_fail_routed(row, exc)
                except Exception:  # noqa: BLE001 — per-row routing is best-effort; reclaim_stale backstops
                    _log.warning(
                        "aspect_worker_batch_route_failed",
                        collection=row.collection, source_path=row.source_path,
                        exc_info=True,
                    )
            return

        # nexus-zir76: one routed write_fn does the whole batch's persist;
        # each row clears via the daemon (``complete_aspect`` /
        # ``mark_done``) instead of a direct memory.db transaction.
        def _persist_all(db):  # noqa: ANN001
            for row, record in zip(rows, records):
                if record is None:
                    # Unsupported collection — drop silently.
                    db.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
                    continue
                # nexus-8g79.34: ExtractFail per-row — typed read-failure
                # sentinel from URI-based reading. Mark queue done so we
                # don't retry an unreadable source on every drain;
                # operators re-enqueue manually after fixing source
                # identity (mirrors _process_row's handling).
                if isinstance(record, ExtractFail):
                    _log.info(
                        "aspect_worker_batch_extract_skip",
                        uri=record.uri,
                        reason=record.reason,
                        detail=record.detail,
                    )
                    db.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
                    continue
                attributed = _attributed(record, row)
                if _is_unattributable(attributed):
                    # nexus-r0kum: refuse to upsert a file-routed row with
                    # no source_uri — it can never be attributed by
                    # aspects-004 and is invisible to search_aspect_scoped
                    # by construction. Terminal fail (never retried
                    # automatically): the identity gap is in the writer
                    # that enqueued it, not a transient condition a retry
                    # would clear.
                    _log.warning(
                        "aspect_worker_unattributable_identity",
                        collection=row.collection,
                        source_path=row.source_path,
                    )
                    db.aspect_queue.mark_failed(
                        row.collection, row.source_path,
                        error="unattributable_identity",
                    )
                    continue
                db.complete_aspect(dataclasses.asdict(attributed))
        try:
            t2_index_write(_persist_all)
        except Exception:  # noqa: BLE001 — batch persist best-effort; failure logged via log.warning
            _log.warning(
                "aspect_worker_batch_persist_failed",
                exc_info=True,
            )

    def _mark_failed_routed(self, row, error: str) -> None:
        """Route a queue ``mark_failed`` through the daemon (nexus-zir76).

        The failure path must not open ``memory.db`` directly either: a
        direct ``mark_failed`` losing the WAL writer race is exactly what
        orphaned rows ``in_progress`` until the reclaim backstop. If even
        the routed write raises (daemon down AND the direct fallback
        contended), ``reclaim_stale`` recovers the row; we log and move on
        without killing the worker thread.
        """
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
        try:
            t2_index_write(
                lambda db: db.aspect_queue.mark_failed(
                    row.collection, row.source_path, error=error,
                )
            )
        except Exception:  # noqa: BLE001 — mark-failed persist best-effort; logged via log.warning
            _log.warning(
                "aspect_worker_mark_failed_persist_failed",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )

    def _mark_retry_or_fail_routed(self, row, exc: BaseException) -> None:
        """Route a failed row to a backed-off retry, or terminal-fail it
        (RDR-163 P1, nexus-ztpt6).

        Decision, in order:
        * SELF FAULT — this process's own environment is broken (nexus-yg70j)
          → NO row-state write at all. Stand the worker down and leave the
          claimed rows for ``reclaim_stale``. Checked FIRST, because the two
          branches below both record a verdict about the document and neither
          verdict is honest when the fault was never about the document;
        * non-retryable exception (programming bug / malformed record) →
          terminal ``mark_failed``;
        * retry budget exhausted (``retry_count >= _RETRY_MAX_ATTEMPTS``) →
          terminal ``mark_failed``;
        * otherwise → ``mark_retry`` with a worker-chosen backoff interval; the
          service stamps ``next_retry_at = now()+interval`` so the claim gate
          holds the row back until the backoff elapses.

        Routed through ``t2_index_write`` (nexus-zir76) so the worker never opens
        ``memory.db`` directly. A routing failure is logged and falls to
        ``reclaim_stale`` rather than killing the worker thread — the same
        best-effort posture as ``_mark_failed_routed``.
        """
        self_fault = _self_fault_reason(exc)
        if self_fault is not None:
            self._stand_down(self_fault, row=row, exc=exc)
            return

        retry_count = getattr(row, "retry_count", 0) or 0
        if not _is_retryable(exc) or retry_count >= _RETRY_MAX_ATTEMPTS:
            self._mark_failed_routed(row, str(exc))
            return

        interval = _backoff_interval_seconds(retry_count)
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
        try:
            t2_index_write(
                lambda db: db.aspect_queue.mark_retry(
                    row.collection, row.source_path, interval_seconds=interval,
                )
            )
            _log.info(
                "aspect_worker_retry_scheduled",
                collection=row.collection,
                source_path=row.source_path,
                retry_count=retry_count + 1,
                interval_seconds=interval,
            )
        except Exception:  # noqa: BLE001 — retry persist best-effort; reclaim_stale backstops; logged
            _log.warning(
                "aspect_worker_mark_retry_persist_failed",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )

    def _stand_down(self, reason: str, *, row, exc: BaseException) -> None:
        """Report a self fault and stop claiming, WITHOUT touching row state.

        Latched, because the batch arm calls the router once per row: an
        episode produces one ERROR line and one stand-down, not five.

        The rows this worker holds stay ``in_progress``. They are recovered by
        the daemon's ``reclaim_stale`` sweep, which is documented as
        RECLAIM-FIRST precisely so a freshly respawned daemon clears the
        backlog a dead worker left behind rather than waiting an interval.

        Stopping the THREAD is not enough on its own. The host daemon
        heartbeats a per-tenant lease, so a process whose worker has quietly
        stopped keeps that lease looking healthy and is never respawned — a
        second silent-permanent-death, which is the thing this bead exists to
        remove. So the fault is handed to the host, which stops the PROCESS;
        the lease is relinquished and the next enqueue spawns a daemon with a
        fresh environment. A persistent host fault (a full disk) therefore
        becomes a respawn loop rather than a silent one. That is the intended
        trade: loud and non-destructive over quiet and destructive.
        """
        with self._self_fault_lock:
            first = self._self_fault is None
            if first:
                self._self_fault = reason
        if not first:
            return

        # ERROR, not WARNING: the 2026-08-24 incident ran 16 consecutive failed
        # batches at WARNING with nothing alerting (nexus-yg70j criterion 3).
        _log.error(
            "aspect_worker_self_fault",
            reason=reason,
            collection=getattr(row, "collection", None),
            source_path=getattr(row, "source_path", None),
            error=str(exc),
            action="row state untouched; worker standing down for reclaim_stale",
        )
        self._stop_event.set()

        handler = self._on_self_fault
        if handler is None:
            return
        try:
            handler(reason)
        except Exception:  # noqa: BLE001 — the host hook must not mask the fault it reports
            _log.warning("aspect_worker_self_fault_handler_failed", exc_info=True)

    def _process_row(self, row) -> None:
        """Run extraction on one queue row and dispatch on the result.

        nexus-zir76: every persist routes through ``t2_index_write`` (the
        daemon when reachable, a direct fallback when not) so the worker
        never opens ``memory.db`` directly and cannot contend with the
        daemon for the single WAL writer lock.
        """
        import dataclasses  # noqa: PLC0415 — stdlib import deferred to method scope (rare branch)

        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
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
            # nexus-8g79.2: manifest lookup gives the chroma reader the
            # canonical chunk-order from document_chunks instead of the
            # dropped chunk_index metadata field.
            manifest_lookup = None
            try:
                from nexus.commands.enrich import _build_catalog_manifest_lookup  # noqa: PLC0415 — deferred to avoid circular import (commands.enrich)
                manifest_lookup = _build_catalog_manifest_lookup()
            except Exception as exc:  # noqa: BLE001 — optional manifest-lookup enrichment; absence is non-fatal, falls through
                _log.debug("aspect_worker_manifest_lookup_unavailable", error=str(exc))
            record = _extract_aspects(
                content=row.content,
                source_path=row.source_path,
                collection=row.collection,
                doc_id_lookup=doc_id_lookup,
                manifest_lookup=manifest_lookup,
            )
        except Exception as exc:  # noqa: BLE001 — extract is best-effort; failure logged via log.warning
            _log.warning(
                "aspect_worker_extract_raised",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )
            self._mark_retry_or_fail_routed(row, exc)
            return

        try:
            if record is None:
                # Unsupported collection — drop silently.
                t2_index_write(
                    lambda db: db.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
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
                t2_index_write(
                    lambda db: db.aspect_queue.mark_done(
                        row.collection, row.source_path,
                    )
                )
                return
            # AspectRecord — either a populated record or a null-fields
            # record from a subprocess-side failure (the extractor
            # already retried up to 3 attempts internally for those
            # paths). nexus-zir76: persist + clear the queue row in one
            # daemon-routed call (``complete_aspect``) so the worker
            # never writes memory.db directly. asdict() because the wire
            # protocol decodes a dataclass arg to its field dict.
            attributed = _attributed(record, row)
            if _is_unattributable(attributed):
                # nexus-r0kum: same terminal refusal as the batch path —
                # see _persist_all's matching comment.
                _log.warning(
                    "aspect_worker_unattributable_identity",
                    collection=row.collection,
                    source_path=row.source_path,
                )
                self._mark_failed_routed(row, "unattributable_identity")
                return
            t2_index_write(
                lambda db, _r=attributed: db.complete_aspect(
                    dataclasses.asdict(_r)
                )
            )
        except Exception as exc:  # noqa: BLE001 — persist is best-effort; failure logged via log.warning
            _log.warning(
                "aspect_worker_persist_failed",
                collection=row.collection,
                source_path=row.source_path,
                exc_info=True,
            )
            self._mark_retry_or_fail_routed(row, exc)


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
    import os  # noqa: PLC0415 — stdlib os deferred to function scope

    base = locks_dir if locks_dir is not None else (
        _config.nexus_config_dir() / "locks"
    )
    return base / f"aspect_worker.{os.getpid()}"


def _sweep_dead_worker_locks(locks_dir: Path) -> None:
    """Remove ``aspect_worker.<pid>`` lock files whose PID is dead.

    nexus-zir76: ``_remove_worker_lock`` only runs on a clean
    ``stop_worker``; a ``-9`` or a crash leaks the file. Over many
    sessions these accumulate unbounded (85 found in the wild on
    2026-05-27). Sweeping dead-PID locks at worker startup bounds the
    pileup. Live locks (including this process's own) are left intact;
    non-PID-shaped files are ignored. Best-effort — never raises.
    """
    import os  # noqa: PLC0415 — stdlib os deferred to function scope

    if not locks_dir.exists():
        return
    own_pid = os.getpid()
    for lock_file in locks_dir.glob("aspect_worker.*"):
        try:
            pid = int(lock_file.name.rsplit(".", 1)[-1])
        except ValueError:
            continue  # not a PID-suffixed lock file
        if pid == own_pid:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            try:
                lock_file.unlink(missing_ok=True)
                _log.info("aspect_worker_stale_lock_swept", pid=pid)
            except Exception:  # noqa: BLE001 — liveness probe of foreign pid is best-effort; treat unknown as stale
                pass
        except PermissionError:
            # Alive under another user — leave it.
            continue


def _write_worker_lock(locks_dir: Path | None = None) -> None:
    """Write a process-scoped lock file advertising this worker.

    Sweeps dead-PID lock files first (nexus-zir76) so leaked locks from
    crashed/killed predecessors do not accumulate. Non-fatal if the locks
    directory cannot be created or the file cannot be written — the lock
    is advisory; a missing file merely means ``drain_worker`` from another
    process will not detect this worker.
    """
    import os  # noqa: PLC0415 — stdlib os deferred to function scope

    try:
        lock = _worker_lock_path(locks_dir)
        lock.parent.mkdir(parents=True, exist_ok=True)
        _sweep_dead_worker_locks(lock.parent)
        lock.write_text(str(os.getpid()))
    except Exception:  # noqa: BLE001 — lock-file write is best-effort; failure logged via log.warning
        _log.warning("aspect_worker_lock_write_failed", exc_info=True)


def _remove_worker_lock(locks_dir: Path | None = None) -> None:
    """Remove the process-scoped lock file when the worker stops.

    Non-fatal if the file does not exist or cannot be removed.
    """
    try:
        _worker_lock_path(locks_dir).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — lock-file remove is best-effort; failure logged via log.warning
        _log.warning("aspect_worker_lock_remove_failed", exc_info=True)


def ensure_worker_started(
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
    stale_timeout_seconds: int = 60,
    _locks_dir: Path | None = None,
) -> AspectExtractionWorker:
    """Lazy-start the singleton worker. Returns the worker.

    Idempotent -- calling once, twice, or N times all return the same
    instance and only spawn one thread. Tuning parameters apply only
    to the first call (subsequent calls cannot retune).

    Writes a process-scoped lock file (SIG-5 heritage). The drain-side
    reader that used to consume it (``_check_mcp_worker_lock``, a
    SQLite-queue concern) died with the =sqlite opt-out (RDR-158 P3,
    nexus-7bomn); the lock remains as a pid marker for operators and
    is swept/removed by this module's own lifecycle.
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

    Protocol (RDR-108 Phase 1 S1, nexus-he24; the SIG-5 lock-file
    pre-check that used to lead it was a SQLite-queue concern and died
    with the =sqlite opt-out — on the service queue PG's FOR UPDATE SKIP
    LOCKED owns cross-process claim safety):

      1. Set the stop signal on the singleton worker (if one exists) so
         it claims no new rows.  In-flight row processing continues.
      2. Poll the queue at ``poll_interval`` (default 100 ms) until
         ``is_drained()`` returns True or ``timeout`` elapses.
      3. On success: join the worker thread (it has already exited or will
         exit imminently after the last iteration).  If the thread does not
         exit within 2 seconds, log a warning (S-5) and continue — the
         thread is a daemon and will die with the process.
      4. On timeout: raise ``DrainTimeoutError`` so the operator is alerted
         to inspect stuck ``in_progress`` rows.

    Args:
        queue_path: Legacy path parameter, retained for signature stability.
            The queue is engine-side (``HttpAspectQueue``, nexus-i711w Stage 2
            sub-stage A); the ``is_drained()`` poll goes over HTTP and no
            local connection is opened from this path.
        timeout: Seconds to wait for the queue to drain.  Default 120s.
            RDR-089 P1.3 measured ~26.5s median extraction time with a
            tail to 90s for the scholarly-paper-v1 extractor.  120s
            (approximately 4x median) provides adequate margin while
            still surfacing genuinely stuck workers within two minutes.
            Callers that know their workload is lighter may pass a smaller
            value.
        poll_interval: Seconds between is_drained() checks (default 0.1).
        _locks_dir: Retained-and-ignored for signature stability. The
            lock-file pre-check this seam parameterised died with the
            =sqlite opt-out (RDR-158 P3, nexus-7bomn).

    Raises:
        DrainTimeoutError: Queue still has pending/in_progress rows after
            ``timeout`` seconds.

    Note:
        If no worker has been started in this process (``get_worker()``
        returns None), the function simply checks ``is_drained()`` once and
        returns — there is no thread to stop and no in-flight work to wait
        for.  This handles the quiescent case (e.g., the caller's process
        never ran the worker, or the worker finished and was reset).
    """
    # SIG-5 history (nexus-1091 / RDR-173 P4.1 nexus-4st62): the local
    # file-lock check that used to run here detected a live MCP worker in
    # another process before stopping the local singleton — a SQLite-queue
    # concern. On the service queue ALL workers share the same PG rows with
    # FOR UPDATE SKIP LOCKED, so cross-process claim safety is handled by
    # PG, not by the file lock; checking it would make drain always raise
    # DrainBlockedByActiveWorker while the MCP server is running, defeating
    # the primary use case (drain during migration while MCP is live). The
    # check went with the =sqlite opt-out (RDR-158 P3, nexus-7bomn).
    #
    # Validation only: drain constructs HttpAspectQueue directly (bypassing
    # T2Database's validation seam), so a stranded
    # NX_STORAGE_BACKEND[_ASPECT_QUEUE]=sqlite export hard-errors here
    # rather than being silently ignored on the drain path.
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid circular import (db.storage_mode)

    storage_backend_for("aspect_queue")
    worker = get_worker()
    if worker is not None:
        worker.stop_claiming()

    queue_path = Path(queue_path)
    deadline = time.monotonic() + timeout

    # RDR-173 P4.1 (nexus-4st62): in SERVICE mode the authoritative
    # aspect_extraction_queue is PG, reached via ``HttpAspectQueue``. The
    # local-sqlite ``AspectExtractionQueue`` is empty/stale there, so polling it
    # yields a spurious "drained" — leaving ``nx aspects drain`` and the
    # migration drain gate inert in service mode. Poll the SERVICE queue's
    # ``is_drained()`` instead. (Local/SQLite mode is unchanged.)
    # Seam COLLAPSED (nexus-i711w Stage 2 sub-stage A): HttpAspectQueue is
    # the only queue — the SQLite AspectExtractionQueue died with the store.
    from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred (optional service dep)
    queue = HttpAspectQueue()

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
        # The queue is HttpAspectQueue (the SQLite arm died in nexus-i711w
        # Stage 2 sub-stage A, taking the raw `status != 'failed'` count
        # with it). pending_count() counts only 'pending' rows — NOT
        # in_progress. A crashed worker leaves rows in in_progress with NO
        # lock held (FOR UPDATE locks are transaction-scoped; they are
        # released when the transaction ends, which happens when the worker
        # process dies). Those orphaned rows are recovered by
        # reclaim_stale(), not by a held lock. Consequence: if the drain
        # times out because rows are stuck in_progress (the common
        # crashed-worker scenario), pending_count() returns 0 even though
        # is_drained() is still False. We detect this gap and include an
        # honest operator hint in the error detail.
        pending = queue.pending_count()
        if pending == 0:
            # is_drained() returned False at timeout, but pending_count()
            # is 0 — rows are stuck in in_progress (crashed-worker case).
            # pending_count() is a pending-only proxy and cannot see them.
            detail = (
                "Note (service mode): pending_count is 0, but is_drained() "
                "is still False — rows may be stuck in in_progress from a "
                "crashed worker. A running aspect-worker's stale-reclaim "
                "loop resets them to pending automatically; ensure one is "
                "up ('nx daemon aspect-worker start', or it spawns on the "
                "next aspect enqueue), then retry the drain."
            )
            stuck_count = 0
        else:
            detail = None
            stuck_count = pending
        raise DrainTimeoutError(stuck_count=stuck_count, timeout=timeout, detail=detail)
    finally:
        queue.close()


# ── Hook function (wired by hook_registry.install_default_hooks) ────────────


#: A lowercase-hex chunk hash — 64-char since the RDR-180 identity flip;
#: 32-char in historical rows written before it. Note-backed aspect rows
#: carry this as ``source_path`` by design (RDR-172 owns note identity via
#: ``doc_id``); it is NOT a filesystem path, so Gap-2 canonicalization
#: skips it.
_CHASH_RE = re.compile(r"^([0-9a-f]{32}|[0-9a-f]{64})$")


def _attributed(record: "AspectRecord", row: object) -> "AspectRecord":
    """Stamp the queue row's catalog identity onto *record* before it is
    completed (nexus-x1de2 (52)). The extractor never sets ``doc_id``; the
    row carries the one captured at enqueue (nexus-tdgc). Both completion
    paths (batch ``_persist_all`` and the single-row ``_process_row``) go
    through here so a row with a known identity is never persisted
    unattributed.

    nexus-r0kum: also resolves a missing ``source_uri`` on a file-routed
    collection (``rdr__``/``docs__``/``code__``) via the record's
    (now-stamped) ``doc_id``. ``uri_for`` (the extractor's writer) only
    ever sees ``(collection, source_path)`` — for an MCP ``store_put``
    into one of these collections, ``source_path`` is the T3 chunk chash,
    not a filesystem path, so ``uri_for`` correctly returns ``None`` for
    it (a chash is never absolute and there is no ``repo_root`` to anchor
    a relative one against). When the catalog tumbler is known, its own
    ``file_path``/``source_uri`` IS the real identity — this looks it up
    and re-derives ``source_uri`` from it rather than persisting the
    chash-shaped non-identity. See :func:`_is_unattributable` for the
    refusal this enables when resolution still comes up empty."""
    from nexus.db.t2.records import with_doc_id  # noqa: PLC0415 — deferred to avoid circular import (db.t2 <-> aspect_worker)

    record = with_doc_id(record, getattr(row, "doc_id", "") or "")
    if not record.source_uri and record.doc_id:
        from nexus.aspect_readers import FILE_ROUTED_PREFIXES  # noqa: PLC0415 — deferred to avoid circular import (aspect_readers)
        if record.collection.startswith(FILE_ROUTED_PREFIXES):
            resolved_uri = _resolve_source_uri_from_doc_id(
                record.collection, record.doc_id,
            )
            if resolved_uri:
                import dataclasses  # noqa: PLC0415 — stdlib import deferred to function scope (rare branch)
                record = dataclasses.replace(record, source_uri=resolved_uri)
    return record


def _is_unattributable(record: "AspectRecord") -> bool:
    """True when *record* is a file-routed row (``rdr__``/``docs__``/
    ``code__``) that STILL carries no ``source_uri`` after
    :func:`_attributed`'s resolution attempt (nexus-r0kum).

    A file-routed row with no ``source_uri`` can never be attributed by
    aspects-004 (byte-equal ``source_uri`` join) and is invisible to
    ``search_aspect_scoped`` by construction — persisting it manufactures
    exactly the 51-row ``rdr-frontmatter-v1`` class the census found.
    Callers refuse to upsert when this is True: mark the queue row failed
    with reason ``unattributable_identity`` instead of writing a row that
    can never be found again. Not file-routed (``knowledge__`` etc.) is
    always attributable-by-title, so this only ever fires for the
    filesystem-identity collections.
    """
    from nexus.aspect_readers import FILE_ROUTED_PREFIXES  # noqa: PLC0415 — deferred to avoid circular import (aspect_readers)

    return bool(
        not record.source_uri and record.collection.startswith(FILE_ROUTED_PREFIXES)
    )


def _resolve_source_uri_from_doc_id(collection: str, doc_id: str) -> str:
    """Best-effort ``source_uri`` lookup for a known catalog tumbler.

    Returns ``""`` on any failure (no catalog, unresolvable tumbler,
    cross-collection mismatch) — the caller (`_attributed`) treats an
    empty result as "still unattributed", never as an error to raise.
    The cross-collection guard mirrors ``_entry_by_source_uri``'s
    precedent: a tumbler that resolves but belongs to a DIFFERENT
    collection is refused rather than used, so this can never re-point
    an aspect row at a document living in another collection.
    """
    try:
        cat = _resolve_catalog_reader()
    except Exception:  # noqa: BLE001 — best-effort catalog reader; any init failure degrades to empty
        cat = None
    if cat is None:
        return ""
    try:
        entry = cat.by_doc_id(doc_id)
    except Exception:  # noqa: BLE001 — best-effort resolve; any query error degrades to empty
        entry = None
    if entry is None:
        return ""
    if (getattr(entry, "physical_collection", "") or "") != collection:
        return ""
    return getattr(entry, "source_uri", "") or ""


def _resolve_catalog_reader():
    """Return a best-effort read catalog (or ``None``). Indirection seam so
    tests can inject a real tmp ``Catalog`` without env/service-mode coupling.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import (catalog.factory)
    return make_catalog_reader()


def _resolve_doc_id_for_enqueue(collection: str, source_path: str) -> str:
    """Best-effort catalog lookup for a doc_id, keyed on (collection,
    source_path) — hygiene-001 (nexus-tk070.p6a follow-on), review round
    item B (critic C2).

    Called by :func:`aspect_extraction_enqueue_hook` ONLY when its caller
    did not already supply a ``doc_id`` — never overrides a caller-supplied
    value. Returns ``""`` on any failure (no catalog, unresolvable, catalog
    error): the caller treats an empty result as still-unresolvable and
    SKIPS the enqueue entirely rather than calling it with a blank doc_id
    and letting the engine's 400 be swallowed as a best-effort log (the
    prior behaviour this replaces — a real attribution gap hiding behind a
    routine-looking warning).
    """
    try:
        cat = _resolve_catalog_reader()
    except Exception:  # noqa: BLE001 — best-effort catalog reader; any init failure degrades to unresolved
        cat = None
    if cat is None:
        return ""
    try:
        return cat.lookup_doc_id_by_collection_and_path(collection, source_path) or ""
    except Exception:  # noqa: BLE001 — best-effort resolve; any query error degrades to unresolved
        return ""


def _canonicalize_source_path(collection: str, source_path: str) -> str:
    """RDR-145 Gap-2: forward-only ``source_path`` canonicalization.

    Resolves ``source_path`` against the catalog for ``collection``. On a
    hit whose stored ``file_path`` is a *relative* form that differs, returns
    that canonical relative path; on a miss, returns ``source_path`` unchanged
    and emits a loud ``aspect_source_path_uncanonical`` warning. It NEVER
    synthesizes or guesses a path — doing so would re-introduce the
    ``nexus-3e4s`` CWD-anchoring contamination class. Best-effort and
    fail-open: any catalog unavailability degrades to a no-op (the document
    still enqueues; the one-time cleanup, RDR-145 Phase 2, handles legacy
    rows).

    Only file-backed (path-shaped) ``source_path`` values are considered.
    Note-backed rows carry a hex chash (64-hex today; 32-hex in historical
    rows) with no path separator and are returned untouched so the probe
    never false-warns on a correct chash.

    Reachability of the normalize branch (important — do not oversell this):
    the catalog probe matches ``physical_collection AND (file_path = source_path
    OR title = source_path)``. The documented contamination population
    (RDR-145 Bucket B: ~177 absolute paths that MISMATCH the catalog's stored
    *relative* ``file_path``) therefore MISSES the probe and lands in the WARN
    branch — the loud ``aspect_source_path_uncanonical`` tripwire is the real
    forward-only output for that population, not in-flight rewriting. The
    normalize branch fires only when ``source_path`` already equals a stored
    ``file_path``/``title``; correcting those Bucket-B rows is Phase 2's
    one-time migration cleanup (``nexus-nx9nx``, migration-gated), not this
    hook. In service mode the catalog reads are HTTP point-queries bounded by
    the client's 30s timeout and fail-open; batch CLI ingest latency is a
    tracked follow-up, not a regression.
    """
    # Skip note-backed (chash) and any non-path-shaped identifier (e.g. a
    # bare slug/title) — there is nothing to canonicalize and no contamination
    # risk for these by design.
    if _CHASH_RE.match(source_path) or (
        "/" not in source_path and os.sep not in source_path
    ):
        return source_path
    try:
        cat = _resolve_catalog_reader()
    except Exception:  # noqa: BLE001 — best-effort catalog reader; any init failure degrades to no-op
        cat = None
    if cat is None:
        # No catalog to canonicalize against — forward-only defense is a
        # no-op (do not warn: absence of a catalog is not an uncanonical path).
        return source_path
    try:
        doc_id = cat.lookup_doc_id_by_collection_and_path(collection, source_path)
    except Exception:  # noqa: BLE001 — best-effort resolve; any query error degrades to no-op
        doc_id = ""
    entry = None
    if not doc_id:
        # The first probe keys on file_path/title, both of which the catalog
        # stores RELATIVE. An ABSOLUTE source_path therefore always misses it
        # -- that is the whole documented Bucket-B population, and it warned
        # ~300x/day on this box while its one-time cleanup bead (nexus-nx9nx)
        # was closed as superseded without the normalization half ever running.
        #
        # The catalog also stores the ABSOLUTE form, as `source_uri`
        # (`file://<abspath>`, RDR-096 P3.1), and exposes `by_source_uri`. So
        # an absolute path has an exact, deterministic key into the catalog.
        # This is NOT the nexus-3e4s CWD-anchoring class: no base path is
        # chosen, nothing is synthesized, and the value is accepted only when
        # the catalog itself returns an entry IN THIS COLLECTION.
        entry = _entry_by_source_uri(cat, collection, source_path)
        if entry is None:
            _log.warning(
                "aspect_source_path_uncanonical",
                collection=collection,
                source_path=source_path,
            )
            return source_path
    # ``lookup_doc_id_by_collection_and_path`` returns either a legacy
    # ``metadata.doc_id`` or the catalog ``tumbler``; resolve the entry via
    # whichever the catalog honours (``by_doc_id`` keys on doc_id only).
    for getter in ("by_doc_id", "resolve") if entry is None else ():
        fn = getattr(cat, getter, None)
        if fn is None:
            continue
        try:
            entry = fn(doc_id)
        except Exception:  # noqa: BLE001 — best-effort entry read; try the next resolver
            entry = None
        if entry is not None:
            break
    canonical = (getattr(entry, "file_path", "") or "") if entry is not None else ""
    if canonical and not os.path.isabs(canonical) and canonical != source_path:
        _log.info(
            "aspect_source_path_canonicalized",
            collection=collection,
            was=source_path,
            now=canonical,
        )
        return canonical
    return source_path



def _entry_by_source_uri(cat: object, collection: str, source_path: str):
    """Catalog entry for an ABSOLUTE ``source_path`` via its ``source_uri``.

    Returns ``None`` unless the catalog resolves the URI to an entry whose
    ``physical_collection`` matches ``collection`` -- a cross-collection hit is
    refused rather than used, so this can never re-point an aspect row at a
    document in a different collection. Best-effort: any catalog error or a
    missing ``by_source_uri`` degrades to ``None`` (the caller then warns
    exactly as before).
    """
    if not os.path.isabs(source_path):
        return None
    fn = getattr(cat, "by_source_uri", None)
    if fn is None:
        return None
    try:
        entry = fn("file://" + source_path)
    except Exception:  # noqa: BLE001 — best-effort probe; any error degrades to the warn branch
        return None
    if entry is None:
        return None
    if (getattr(entry, "physical_collection", "") or "") != collection:
        return None
    return entry


def aspect_extraction_enqueue_hook(
    source_path: str,
    collection: str,
    content: str,
    *,
    doc_id: str = "",
) -> None:
    """Post-document hook: enqueue a row for async aspect extraction.

    Signature matches ``HookRegistry.fire_document(source_path,
    collection, content)``. This hook does NOT call extract_aspects
    inline — that would block the ingest path for ~25 s per document
    (RDR-089 P1.3 spike finding). Instead it:

      1. Skips collections without a registered extractor config
         (currently ``knowledge__*``, ``rdr__*``, ``docs__*``).
      2. Resolves ``doc_id`` when the caller did not supply one (hygiene-001,
         nexus-tk070.p6a follow-on, review round item B): the queue's
         ``doc_id`` carries a REAL FK to ``catalog_documents``, so a blank
         one is refused by the engine. This is the CHOKE POINT for that
         decision — every ``fire_document`` call site shares it, so the
         resolve-or-skip logic lives here once. A caller-supplied doc_id is
         never overridden; a blank one is looked up via the catalog reader
         by ``(collection, source_path)``. Still unresolvable -> the
         enqueue is SKIPPED entirely, logged as
         ``aspect_enqueue_skipped_no_doc_id`` — never enqueued with ``""``.
      3. Writes a pending row to ``aspect_extraction_queue``
         (microsecond-scale T2 INSERT). The row carries ``content``
         when non-empty (MCP path) so the worker has the document
         text without needing to re-read from disk; CLI paths pass
         ``content=""`` and the worker falls back to a file read.
      4. Lazy-starts the worker if not already running.

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
    from nexus.aspect_extractor import select_config  # noqa: PLC0415 — deferred to avoid circular import (aspect_extractor)
    if select_config(collection) is None:
        return  # No extractor for this collection — nothing to enqueue.
    # RDR-145 Gap-2: canonicalize a file-backed source_path against the
    # catalog before persisting the queue row (forward-only; never guesses).
    source_path = _canonicalize_source_path(collection, source_path)

    # hygiene-001 (nexus-tk070.p6a follow-on), review round item B (critic
    # C2): the CHOKE POINT for the blank-doc_id decision. All seven
    # fire_document call sites (mcp/core.py, code_indexer.py, prose_indexer.py,
    # doc_indexer.py x3, indexer.py) converge here, so the resolve-or-skip
    # logic lives ONCE, not duplicated at each caller. A caller-supplied
    # doc_id is never overridden; only a blank one triggers a catalog lookup
    # by (collection, source_path). Still unresolvable -> SKIP the enqueue
    # outright — never call enqueue() with doc_id="" and rely on the
    # engine's 400 to be caught by the generic exception handler below and
    # swallowed as a best-effort log (the prior behaviour: a real
    # attribution gap hid behind a routine-looking "enqueue failed"
    # warning, indistinguishable from a transient T2/network failure).
    if not doc_id:
        doc_id = _resolve_doc_id_for_enqueue(collection, source_path)
    if not doc_id:
        _log.warning(
            "aspect_enqueue_skipped_no_doc_id",
            collection=collection,
            source_path=source_path,
        )
        return

    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
    try:
        # RDR-128 P1 (kg8sj): route the enqueue through the daemon so the
        # indexer process does not open memory.db directly to write it.
        t2_index_write(
            lambda t2: t2.aspect_queue.enqueue(
                collection, source_path, content=content,
                doc_id=doc_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 — enqueue is best-effort; failure logged via log.warning
        # nexus-w8lg1: one-line warning at the CLI surface; the full
        # traceback goes to debug (it previously spammed raw structlog
        # tracebacks to the user's terminal on every enqueue failure).
        _log.warning(
            "aspect_extraction_enqueue_failed",
            source_path=source_path,
            collection=collection,
            error=str(exc)[:500],
        )
        _log.debug(
            "aspect_extraction_enqueue_failed_detail",
            source_path=source_path,
            collection=collection,
            exc_info=True,
        )
        # RDR-172 P2.1 (nexus-hlkvj): loudness tripwire. Keeping the enqueue
        # best-effort (never block ingest, RDR-089 P0.1) also hides this
        # failure from hook_registry's hook_failures recorder — the hook
        # swallows it here, so fire_document sees success. Persist a structured
        # hook_failures row directly so CI / --fullstack can assert ZERO
        # enqueue failures across an ingest E2E (the fail-closed recurrence
        # guard, RF-7 — the nexus-ov0sw silent-total-failure class). The
        # persist is itself best-effort: a telemetry-write failure (T2 down,
        # service 5xx) must never block ingest either.
        try:
            t2_index_write(
                lambda t2: t2.telemetry.record_hook_failure(
                    doc_id=source_path,
                    collection=collection,
                    hook_name="aspect_extraction_enqueue_hook",
                    error=str(exc)[:2000],  # match hook_registry's truncation
                    chain="document",
                )
            )
        except Exception:  # noqa: BLE001 — tripwire persist is best-effort; never block ingest
            _log.warning(
                "aspect_enqueue_tripwire_persist_failed",
                source_path=source_path,
                collection=collection,
                exc_info=True,
            )
        # Enqueue failure is non-fatal — ingest is never blocked.
        # The document_aspects row will simply not be populated until
        # a manual re-enqueue triggers extraction.
        return
    # Auto-spawn gate (nexus test-suite trim): the enqueue hook lazy-spawns
    # the singleton polling worker for every supported-collection document.
    # The unit suite sets ``NX_ASPECT_WORKER_AUTOSTART=0`` (conftest) so a
    # store_put / index test does NOT spawn a worker it never asserts on —
    # which otherwise costs a fixed ~5s teardown per test (the stop() join
    # waits on a worker stuck mid ``t2_index_write`` poll). This also removes
    # the leaked-singleton hazard (nexus-u0u8a) at its root for those tests.
    # Production leaves it unset → default-on. Worker-specific tests call
    # ``ensure_worker_started()`` directly, which ignores this gate.
    if os.environ.get("NX_ASPECT_WORKER_AUTOSTART", "1") not in (
        "0", "false", "False", "no", "",
    ):
        _ensure_aspect_worker()


def _best_effort_queue_depth() -> int | None:
    """Pending-row count for the diagnostic signal (RDR-173 P5), or None if it
    cannot be obtained cheaply. The service queue is usually reachable even when
    the WORKER daemon is not (the daemon-spawn path is only reached AFTER a
    successful enqueue), so this normally answers. A SHORT 2s timeout (review H1)
    keeps the observability path from blocking the store hook for the client's
    default 30s in a full outage. (HttpAspectQueue.__init__ emits an info on
    construct — expected on this diagnostic path.)"""
    try:
        from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred; service-side

        q = HttpAspectQueue(tenant=_ENQUEUE_TENANT, timeout=2.0)
        try:
            return int(q.pending_count())
        finally:
            q.close()
    except Exception:  # noqa: BLE001 — diagnostic only; never block the store
        return None


def _ensure_aspect_worker() -> None:
    """RDR-173 P2 (bead nexus-gtdtc): ensure aspect extraction will run, without
    tying it to the storing process's lifetime.

    Decision (gtdtc Open Question): ensure the leased aspect-worker DAEMON is
    up (discover/spawn-if-absent), so extraction completes for every store
    path including short-lived CLI / one-shot / batch. Spawn failure is
    best-effort (the row is already enqueued; the daemon self-heals on the
    next enqueue or via discover). The LOCAL-mode in-process worker-thread
    leg died with the =sqlite opt-out (RDR-158 P3, nexus-7bomn); the worker
    class itself survives — the daemon hosts it against the service queue.
    The resolver call below is validation only: this path reaches the queue
    without constructing T2Database, so a stranded =sqlite export must
    hard-error rather than be silently ignored.
    """
    from nexus.db.storage_mode import storage_backend_for  # noqa: PLC0415 — deferred to avoid circular import (db.storage_mode)

    storage_backend_for("aspect_queue")
    # TODO(RDR-173 multi-tenant): v1 runs a single default tenant, so the leased
    # daemon's scope key is the literal _ENQUEUE_TENANT. When per-request tenant
    # routing lands, derive the tenant from the store's request/connection context
    # here (the ensure_aspect_worker_daemon `tenant=` seam already supports it).
    try:
        from nexus.daemon.aspect_worker_daemon import ensure_aspect_worker_daemon  # noqa: PLC0415 — deferred; daemon module is CLI/service-side

        ensure_aspect_worker_daemon(config_dir=_config.nexus_config_dir(), tenant=_ENQUEUE_TENANT)
    except Exception as exc:  # noqa: BLE001 — best-effort spawn; the row is enqueued, do not fail the store
        # RDR-173 P5 observability (nexus-xv5fl): make the store-time failure
        # LOUD with diagnostic context (tenant + how many rows are now stranded
        # by the outage), AND persist the RDR-172 hook_failures tripwire CI /
        # --fullstack can assert ZERO on. This is the exact silent-store-time
        # failure class RDR-173 exists to eliminate.
        depth = _best_effort_queue_depth()
        fields = {"tenant": _ENQUEUE_TENANT, "error": str(exc)}
        if depth is not None:  # omit when unavailable rather than poison metrics with -1 (review M1)
            fields["queue_depth"] = depth
        _log.warning("aspect_worker.daemon_unreachable", exc_info=True, **fields)
        try:
            from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)

            t2_index_write(
                lambda t2: t2.telemetry.record_hook_failure(
                    doc_id="",
                    collection="",
                    hook_name="aspect_worker_daemon_spawn",
                    error=str(exc)[:2000],
                    chain="document",
                )
            )
        except Exception:  # noqa: BLE001 — tripwire persist is best-effort; never block the store
            _log.warning("aspect_worker.ensure_daemon_tripwire_persist_failed", error=str(exc))
