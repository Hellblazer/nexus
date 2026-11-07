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

  worker thread claims tasks (`AspectExtractionWorker._run_loop`)
    → calls extract_aspects (the synchronous P1.2 extractor)
    → upserts document_aspects on success
    → acks the claim (the task is consumed)

The synchronous extract_aspects is reusable verbatim — only the
dispatch shape changed from inline to queued. The worker calls it
identically.

Worker contract:

* Single worker per process (singleton: ``get_worker()``,
  ``ensure_worker_started()``).
* Daemon thread; dies with the process. The durable ``aspects`` tuple queue
  absorbs process death: a claim a dead worker held lapses with its lease and
  the engine returns the task on the next claim (an attempt counted, and a task
  that reaches the engine's ``max_attempts`` is dead-lettered).
* Waits ``poll_interval`` seconds (default ``DEFAULT_POLL_INTERVAL_S``) between
  idle rounds; the claim itself parks on the engine, and a same-box enqueue
  touches the wake file (nexus-59611).

Result handling (the verdicts live in ``aspect_worker_tuples``):

* ``record is not None and has populated fields`` → upsert
  document_aspects through the claim fence, ack.
* ``record is None`` (unsupported collection, the extractor's
  short-circuit before subprocess) → ack (drop silently).
* ``record is null-fields`` (extractor's internal 3-retry budget
  exhausted) → upsert null record, ack. The extractor
  already retried; the worker MUST NOT retry again.
* Uncaught exception in the worker body → nack into one of THREE classes
  (nexus-yg70j): a transient remote failure retries with backoff;
  a bad document dead-letters for triage; a SELF FAULT — this
  process's own environment is broken — changes no task state at
  all, stands the worker down, and leaves the claims to lapse or be released.

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
import secrets
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

# Re-export by module-qualified name so test patches at
# ``nexus.aspect_worker._extract_aspects`` swap the worker's
# extraction call cleanly without touching the public entrypoint.
from nexus.aspect_extractor import (
    SENSITIVE_DOCUMENT,
    ExtractFail,
    extract_aspects as _extract_aspects,
    extract_aspects_batch as _extract_aspects_batch,
)
from nexus.aspect_readers import READ_FAIL_TRANSPORT, TransportReadFailure
from nexus import config as _config
from nexus.collection_errors import SupersededCollectionWriteError
from nexus import doc_sensitivity
from nexus.doc_sensitivity import SensitivityUnknown
from nexus.sensitivity import SensitiveDocumentRefused, bound_to

_log = structlog.get_logger(__name__)

#: RDR-173 P2: the tenant scope the enqueue hook ensures a leased aspect-worker
#: daemon for. v1 is single-tenant; multi-tenant routing derives this from
#: request context (see the TODO in _ensure_aspect_worker). Matches
#: HttpAspectQueue.DEFAULT_TENANT.
_ENQUEUE_TENANT: str = "default"

# ── Bounded backoff-retry ladder (RDR-163 P1, nexus-ztpt6) ──────────────────
#
# A transient failure nacks the task with a backoff interval the WORKER
# chooses; the engine hides it from the claim until now()+interval on its own
# clock. The claim's attempts count is the cap's source of truth, so
# termination is guaranteed even under the ±1 race of a concurrent lapse.
_RETRY_MAX_ATTEMPTS: int = 5          # dead-letter once attempts >= cap
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
    if isinstance(exc, TransportReadFailure | SensitivityUnknown):
        # SensitivityUnknown: the catalog could not say whether the document is sensitive
        # (RDR-243). A fact about the moment, so retry; a clear answer is never assumed.
        return True
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
    neither a dead-letter (terminal is a claim about the document) nor a
    retry nack (retry says the document may succeed later, which is true
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


#: Wait after a failed tuple claim (see ``_run_loop``).
_TUPLE_CLAIM_RETRY_S: float = 30.0


def _backoff_interval_seconds(retry_count: int, *, rng=random.random) -> int:
    """Worker-chosen backoff for the next retry, in integer seconds.

    ``base * 2**retry_count`` with ±``_RETRY_JITTER_FRACTION`` jitter. The
    engine hides the task until ``now()+interval`` on its own clock; only the
    interval is chosen here. ``rng`` is injectable so the jitter is deterministic
    under test without patching the global ``random`` module.
    """
    raw = _RETRY_BASE_SECONDS * (2 ** max(0, retry_count))
    jitter = 1.0 + (rng() - 0.5) * 2.0 * _RETRY_JITTER_FRACTION
    return max(0, int(raw * jitter))


# ── Drain protocol exceptions ───────────────────────────────────────────────


class DrainTimeoutError(RuntimeError):
    """Raised by ``drain_worker`` when the queue still has actionable work
    after the configured timeout.

    Actionable work is an ``aspects`` task that is available or claimed (dead
    tasks are terminal). ``stuck_count`` is their number. The operator must
    triage: inspect ``aspects`` for claimed tasks, kill any hung workers, and
    re-run the drain after the queue is clear.
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
            f"{stuck_count} actionable task(s) remain in the 'aspects' tuple "
            "queue (available or claimed). "
            "Triage stuck workers before draining again."
        )
        if detail:
            msg = f"{msg} {detail}"
        super().__init__(msg)


class AspectExtractionWorker:
    """Background drain thread for the ``aspects`` tuple queue.

    Owns one daemon thread that claims tasks and processes them. Idle when
    the queue is empty (the claim parks, then sleeps ``poll_interval``
    seconds). Stop via ``stop()`` for clean shutdown; otherwise dies with the
    process (daemon).

    Constructor injection: caller may pass ``poll_interval`` for tests;
    defaults are tuned for production.

    A lapsed claim is returned by the engine on the next claim (an attempt
    counted); nothing here sweeps for it (see ``_run_loop``).
    """

    # Batch-extraction threshold (RDR-089 Phase D). A claim of at least this
    # many tasks drains in a single ``extract_aspects_batch`` call rather than
    # per-row. Below this, use the single-paper path so small queues don't pay
    # the batch-overhead tax for one-or-two-paper drains.
    _DEFAULT_BATCH_SIZE = 5

    def __init__(
        self,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_S,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        on_self_fault: Callable[[str], None] | None = None,
        config_dir: Path | None = None,
        tenant: str = _ENQUEUE_TENANT,
        tuple_options: dict[str, Any] | None = None,
    ) -> None:
        # nexus-59611 stage 2 (review): the wake file lives under the config
        # dir the PRODUCER writes to. A daemon started with --config-dir X
        # diverges from the ambient dir by design, so the host binds its own
        # dir here (or via bind_config_dir); None means the ambient
        # nexus_config_dir(), which is the loose in-process worker's case.
        self._config_dir: Path | None = config_dir
        self._poll_interval = poll_interval
        self._batch_size = batch_size
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Threads a stop() asked to exit and has not yet seen exit (nexus-8sqfs): a repeated stop re-joins them and
        # reports the truth instead of finding ``_thread`` already cleared and answering True.
        self._stopping: list[threading.Thread] = []
        self._lock = threading.Lock()
        # nexus-yg70j: the host's stand-down hook. A worker running loose in an
        # nx-mcp process has no host and simply stops claiming; a worker hosted
        # by AspectWorkerDaemon hands the fault up so the PROCESS exits, which
        # is what makes the failure loud and what gets a fresh environment.
        self._on_self_fault: Callable[[str], None] | None = on_self_fault
        self._self_fault: str | None = None
        self._self_fault_lock = threading.Lock()
        # nexus-wl9pk: the host's "the engine answered" hook (see
        # ``set_engine_contact_handler``); None for a loose in-process worker.
        self._on_engine_contact: Callable[[], None] | None = None
        self._on_engine_failure: Callable[[], None] | None = None
        # nexus-59611 stage 2: mtime baseline for the local wake file, so a
        # touch is consumed exactly once. None means "no wake file observed
        # yet" (absent, or not yet baselined by start()).
        self._last_wake_mtime: float | None = None
        # RDR-235: the aspects tuple queue's side of the worker. On an engine
        # without the template the worker sends one request, logs once and
        # skips claiming for an hour.
        # *tuple_options* overrides the side's lease, park, renewal and deadline
        # (tests).
        from nexus.aspect_worker_tuples import TupleQueueSide, worker_id  # noqa: PLC0415 — deferred: that module imports this one lazily

        self._worker_id: str = worker_id(tenant, pid=os.getpid(), nonce=secrets.token_hex(4))
        self._tuple_side = TupleQueueSide(
            worker_id=self._worker_id,
            batch_size=batch_size,
            tuples_call=self._tuples_call,
            complete=self._complete_with_claim,
            extract=self._extract_tuple_rows,
            on_self_fault=lambda reason, claim, exc: self._stand_down(
                reason, row=claim.row, exc=exc,
            ),
            document_gone=self._document_gone,
            document_sensitive=self._document_sensitive,
            **(tuple_options or {}),
        )

    @staticmethod
    def _tuples_call(fn: Callable[[Any], Any]) -> Any:
        """Run ``fn(tuple_store)`` on the process's service handle. Resolved at
        call time so a test that patches ``nexus.mcp_infra.t2_index_write``
        intercepts the tuple traffic too."""
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra <-> aspect_worker)

        return t2_index_write(lambda db: fn(db.tuples))

    @staticmethod
    def _complete_with_claim(fields: dict[str, Any], claim_id: str, claimant: str) -> Any:
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra <-> aspect_worker)

        return t2_index_write(
            lambda db: db.complete_aspect(fields, claim_id=claim_id, claimant=claimant)
        )

    @staticmethod
    def _document_sensitive(doc_id: str) -> bool:
        """True when the catalog row of *doc_id* is sensitive (RDR-243). Raises
        :class:`~nexus.doc_sensitivity.SensitivityUnknown` when the catalog cannot say: the
        tuple side retries such a claim and never extracts it."""
        return doc_sensitivity.is_sensitive(doc_id)

    @staticmethod
    def _document_gone(doc_id: str) -> bool:
        """True only when the catalog answered and has no such document (RDR-235
        Worker side: a deleted document's task is acked as dropped). No reader,
        or a reader that raises, is not an answer: extraction decides."""
        cat = _resolve_catalog_reader()
        if cat is None:
            return False
        return cat.by_doc_id(doc_id) is None

    def release_held_claims(self) -> int:
        """Release every tuple claim this process still holds, counting no
        attempt. The daemon's stop sweep calls this after ``stop()``."""
        return self._tuple_side.release_held_claims()

    def set_self_fault_handler(self, handler: Callable[[str], None] | None) -> None:
        """Install the host's stand-down hook after construction.

        ``AspectWorkerDaemon`` receives its worker from an injected ZERO-ARG
        factory, so it cannot pass this through the constructor without
        changing that protocol for every caller and test fake. Direct
        constructions use the ``on_self_fault`` kwarg instead.
        """
        self._on_self_fault = handler

    def set_engine_contact_handler(self, handler: Callable[[], None] | None) -> None:
        """Install the host's "the engine answered" hook (nexus-wl9pk).

        Called after each claim round trip the engine answered, empty or not,
        from this worker's thread. ``AspectWorkerDaemon`` uses it, with the
        failure hook, to tell a reachable engine from a dead one.
        """
        self._on_engine_contact = handler

    def set_engine_failure_handler(self, handler: Callable[[], None] | None) -> None:
        """Install the host's "a claim round trip failed" hook. The mirror of
        :meth:`set_engine_contact_handler`: the host starts its engine-unreachable
        clock on the first failure and clears it on the next answered claim."""
        self._on_engine_failure = handler

    def _note_engine_failure(self) -> None:
        handler = self._on_engine_failure
        if handler is None:
            return
        try:
            handler()
        except Exception:  # noqa: BLE001 — a host hook must not kill the claim loop
            _log.warning("aspect_worker_engine_failure_handler_failed", exc_info=True)

    def _note_engine_contact(self) -> None:
        handler = self._on_engine_contact
        if handler is None:
            return
        try:
            handler()
        except Exception:  # noqa: BLE001 — a host hook must not kill the claim loop
            _log.warning("aspect_worker_engine_contact_handler_failed", exc_info=True)

    def self_fault(self) -> str | None:
        """The reason this worker stood down, or ``None`` if it is healthy."""
        with self._self_fault_lock:
            return self._self_fault

    def start(self) -> None:
        """Spawn the daemon thread. Idempotent — calling twice does
        not spawn a second thread.

        Refused, with a warning, while a thread an earlier :meth:`stop` could
        not join is still alive (nexus-22out): the stop event is shared, so
        clearing it here would set that thread claiming again beside the new
        one, and :meth:`is_running` would count only one of them."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = [t for t in self._stopping if t.is_alive()]
            if self._stopping:
                _log.warning("aspect_worker_start_refused_stuck_thread", stuck=len(self._stopping))
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

    def stop(self, timeout: float = 10.0) -> bool:
        """Signal the worker to stop and join the thread.

        Idempotent — calling on a stopped worker is a no-op. Returns
        when the thread exits or after ``timeout``; failure to join
        is logged but not raised (a stuck worker is recoverable on
        process exit since the thread is a daemon). Returns ``True``
        when no thread is left running, ``False`` when the join timed out
        (nexus-8sqfs: a test fixture must not wipe a tenant under a worker
        that is still writing). A thread that outlived one stop stays on
        record, so a repeated stop re-joins it and answers ``False`` until
        it has actually exited.
        """
        with self._lock:
            if self._thread is not None:
                self._stopping.append(self._thread)
                self._thread = None
            threads = list(self._stopping)
        if not threads:
            return True
        self._stop_event.set()
        for thread in threads:
            thread.join(timeout=timeout)
        alive = [t for t in threads if t.is_alive()]
        if alive and self._tuple_side.parked:
            # RDR-235: slot 0's parked ``in`` cannot be cancelled, and a claim it
            # returns after this process is gone would sit live until its lease
            # lapsed (an attempt counted). Wait out the park: the loop releases
            # what the park returns once it sees the stop.
            for thread in alive:
                thread.join(timeout=self._tuple_side._park_timeout_s + 5.0)
            alive = [t for t in threads if t.is_alive()]
        with self._lock:
            self._stopping = [t for t in self._stopping if t.is_alive()]
        if alive:
            _log.warning(
                "aspect_worker_stop_timeout",
                timeout=timeout,
            )
            return False
        return True

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

        Each iteration claims up to ``batch_size`` tasks from the ``aspects``
        tuple queue, one claimant per slot. Slot 0 parks (25 s) on the engine
        when nothing else was found, so an idle worker is one parked request,
        not a poll. A batch of >= 2 takes extract_aspects_batch (one Claude
        call extracts all rows, RDR-089 Phase D); a batch of 1 uses the
        single-paper extract_aspects (``_extract_tuple_rows``), and the
        results finish through :class:`~nexus.aspect_worker_tuples.TupleQueueSide`.

        Nothing here sweeps for stranded work. A claim lives and dies by its
        lease: the engine returns a lapsed claim on the next claim, counting
        an attempt, so a worker that died holding tasks costs a lease, not a
        row.

        All exceptions inside the loop are caught and recorded; the
        worker thread itself never dies from a row's failure.
        """
        from nexus.migration.state import is_migrating as _migration_in_progress  # noqa: PLC0415 — deferred to avoid circular import (migration.state)

        side = self._tuple_side
        while not self._stop_event.is_set():
            # RDR-159 P1c (S2 quiesce): while a guided upgrade migration holds
            # the migration.state sentinel, SUSPEND the claim/extract cycle.
            # This is the cross-process suspend the process-local drain_worker
            # cannot reach (workers resident in other nx-mcp processes). The
            # worker resumes on its next cycle once the sentinel clears (UNLOCK).
            if _migration_in_progress():
                self._wait_for_wake_or_timeout(self._poll_interval)
                continue

            if not side.supported():
                # The engine has no ``aspects`` template (it predates
                # engine-service-v0.1.169; the pin makes this unreachable on a
                # supported install). The side re-probes after an hour. No claim round
                # trip is made meanwhile, so ask the engine one cheap question per
                # tick: the host's engine-unreachable clock must keep running (and
                # clearing) through the suspension, or an engine that goes away
                # during it is not noticed until the hour is out.
                if side.probe_engine():
                    self._note_engine_contact()
                else:
                    self._note_engine_failure()
                self._wait_for_wake_or_timeout(self._poll_interval)
                continue

            claims, failed = side.claim_batch(park=True)
            if failed:
                self._note_engine_failure()
            else:
                self._note_engine_contact()
            parked = side.supported() and not failed and not claims
            if claims and self._stop_event.is_set():
                side.release_held_claims()
                break
            if claims:
                side.process(claims)
                continue
            if parked:
                continue  # the park was the wait
            # A failed claim is the main path blipping, not an idle queue:
            # retry within half a minute, not a poll interval.
            retry_s = min(self._poll_interval, _TUPLE_CLAIM_RETRY_S) if failed else self._poll_interval
            self._wait_for_wake_or_timeout(retry_s)

    @bound_to("aspect_worker_drain")
    def _extract_tuple_rows(self, claims: list) -> list:
        """Extract the rows of a tuple batch: one result per claim, in order.

        One claim takes the single-paper extractor, several take the batch
        extractor (per-row when the batch mixes extractor configs), and none
        persists: each result is an ``AspectRecord``,
        ``None``, an ``ExtractFail`` or an :class:`ExtractRaised` for the verdict
        map. A task carries no text, so ``content`` is ``""`` and the extractor
        reads the document itself: the file for a CLI row, the chunks through
        the manifest lookup for an MCP note (A2, nexus-5r2sc.1.4: the manifest
        reassembly is an acceptable input).
        """
        from nexus.aspect_extractor import select_config  # noqa: PLC0415 — deferred to avoid circular import (aspect_extractor)
        from nexus.aspect_worker_tuples import ExtractRaised  # noqa: PLC0415 — deferred (see __init__)

        rows = [c.row for c in claims]
        manifest_lookup = None
        try:
            from nexus.commands.enrich import _build_catalog_manifest_lookup  # noqa: PLC0415 — deferred to avoid circular import (commands.enrich)
            manifest_lookup = _build_catalog_manifest_lookup()
        except Exception as exc:  # noqa: BLE001 — optional manifest-lookup enrichment; absence is non-fatal, falls through
            _log.debug("aspect_worker_manifest_lookup_unavailable", error=str(exc))

        def _one(row) -> object:  # noqa: ANN001
            try:
                queued_doc_id = row.doc_id or ""
                doc_id_lookup = (
                    (lambda _coll, _sid, _d=queued_doc_id: _d) if queued_doc_id else None
                )
                return _extract_aspects(
                    content=row.content,
                    source_path=row.source_path,
                    collection=row.collection,
                    doc_id_lookup=doc_id_lookup,
                    manifest_lookup=manifest_lookup,
                )
            except SensitiveDocumentRefused as exc:
                # The extractor's own gate (the flag landed after the claim's check): a verdict on
                # the document, so the task is acked as dropped, never retried.
                _log.info(
                    "aspect_worker_skipped_sensitive",
                    tumbler=exc.tumbler, surface=_SURFACE_DRAIN, collection=row.collection,
                    source_path=row.source_path,
                )
                return ExtractFail(uri="", reason=SENSITIVE_DOCUMENT, detail="document is sensitive")
            except Exception as exc:  # noqa: BLE001 — extract is best-effort; the verdict map routes the failure
                _log.warning(
                    "aspect_worker_extract_raised",
                    collection=row.collection, source_path=row.source_path, exc_info=True,
                )
                return ExtractRaised(exc)

        if len(rows) == 1:
            return [_one(rows[0])]
        configs = {select_config(row.collection) for row in rows}
        if len(configs - {None}) > 1:
            _log.info(
                "aspect_worker_batch_heterogeneous_fallback",
                row_count=len(rows),
                configs=sorted(c.extractor_name for c in configs if c is not None),
            )
            return [_one(row) for row in rows]
        items = [(r.collection, r.source_path, r.content, r.doc_id or "") for r in rows]
        try:
            return list(_extract_aspects_batch(items, manifest_lookup=manifest_lookup))
        except Exception as exc:  # noqa: BLE001 — batch extract is best-effort; every row is routed on its own
            _log.warning("aspect_worker_batch_extract_raised", row_count=len(rows), exc_info=True)
            if _self_fault_reason(exc) is None and not _is_retryable(exc):
                # A non-retryable batch failure is terminal for every row it is routed to
                # (dead-lettered). One bad document must not take its healthy neighbours with
                # it, so extract each row on its own and let each earn its own verdict. A self
                # fault (this process's environment is broken) and a retryable failure (the
                # whole batch backs off together) stay batch-level.
                _log.info("aspect_worker_batch_nonretryable_per_row_fallback", row_count=len(rows))
                return [_one(row) for row in rows]
            return [ExtractRaised(exc) for _ in rows]

    def _stand_down(self, reason: str, *, row, exc: BaseException) -> None:
        """Report a self fault and stop claiming, WITHOUT touching task state.

        Latched, because a batch routes each of its claims through here: an
        episode produces one ERROR line and one stand-down, not five.

        The claims this worker holds are released by the host's stop sweep
        (``release_held_claims``, no attempt counted) or lapse with their
        leases, and the engine returns them to the next claimant.

        Stopping the THREAD is not enough on its own. The host daemon
        heartbeats a per-tenant lease, so a process whose worker has quietly
        stopped keeps that lease looking healthy and is never respawned — a
        second silent-permanent-death, which is the thing this bead exists to
        remove. So the fault is handed to the host, which stops the PROCESS;
        the lease is relinquished and the next enqueue spawns a daemon with a
        fresh environment. A persistent host fault (a full disk) therefore
        becomes a respawn loop rather than a silent one. That is the intended
        trade: loud and non-destructive over quiet and destructive. The
        stopping daemon releases the claims its worker holds without spending
        an attempt (nexus-j67ix.3), so a respawn loop on a persistent fault
        does not dead-letter them; a claim whose daemon died without stopping
        lapses with its lease and spends one.
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
            action="task state untouched; worker standing down, claims released or lapse",
        )
        self._stop_event.set()

        handler = self._on_self_fault
        if handler is None:
            return
        try:
            handler(reason)
        except Exception:  # noqa: BLE001 — the host hook must not mask the fault it reports
            _log.warning("aspect_worker_self_fault_handler_failed", exc_info=True)

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


#: The open descriptor of this process's worker lock file, held locked for the worker's life
#: (nexus-dyfsw): the lock, not the PID in the name, is what tells a sweep the worker is alive.
_worker_lock_fd: int | None = None


def _try_unlink_unheld(path: Path) -> bool:
    """Unlink *path* when no process holds its platform lock; True when it was removed.

    POSIX holds the lock across the unlink. Windows cannot unlink an open file, so it releases
    first, and leaves a file someone opened in that gap.
    """
    import os  # noqa: PLC0415 — stdlib os deferred to function scope
    import sys  # noqa: PLC0415 — deferred with os above

    from nexus import _locking  # noqa: PLC0415 — deferred with os above

    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            _locking.lock_fd(fd, blocking=False)  # lifecycle-gate-allow: probes whether a worker's liveness marker is held; not an election (nexus-dyfsw)
        except (BlockingIOError, OSError):
            return False  # held: a live worker
        if sys.platform != "win32":
            path.unlink(missing_ok=True)
            return True
    finally:
        os.close(fd)
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        return False
    return True


def _sweep_dead_worker_locks(locks_dir: Path) -> None:
    """Remove ``aspect_worker.<pid>`` lock files that no live worker holds.

    nexus-zir76: ``_remove_worker_lock`` only runs on a clean
    ``stop_worker``; a ``-9`` or a crash leaks the file. Over many
    sessions these accumulate unbounded (85 found in the wild on
    2026-05-27). Sweeping at worker startup bounds the pileup. A live
    worker holds its file's platform lock for its whole life, so the
    sweep judges by that lock and not by the PID in the name, which a
    worker in another PID namespace makes invisible (nexus-dyfsw). This
    process's own file and non-PID-shaped files are left alone.
    Best-effort — never raises.
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
            if _try_unlink_unheld(lock_file):
                _log.info("aspect_worker_stale_lock_swept", pid=pid)
        except Exception:  # noqa: BLE001 — the sweep is best-effort
            pass


def _write_worker_lock(locks_dir: Path | None = None) -> None:
    """Write a process-scoped lock file advertising this worker, and hold its lock while the worker lives.

    Sweeps unheld lock files first (nexus-zir76, nexus-dyfsw) so leaked locks from
    crashed/killed predecessors do not accumulate. Non-fatal if the locks
    directory cannot be created or the file cannot be written — the lock
    is advisory; a missing file merely means ``drain_worker`` from another
    process will not detect this worker.
    """
    import os  # noqa: PLC0415 — stdlib os deferred to function scope

    from nexus import _locking  # noqa: PLC0415 — deferred with os above

    global _worker_lock_fd
    try:
        lock = _worker_lock_path(locks_dir)
        lock.parent.mkdir(parents=True, exist_ok=True)
        _sweep_dead_worker_locks(lock.parent)
        if _worker_lock_fd is not None:
            os.close(_worker_lock_fd)  # a restart in this process: the file is rewritten below
            _worker_lock_fd = None
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                _locking.lock_fd(fd, blocking=False)  # lifecycle-gate-allow: held for the worker's life as its liveness marker; not an election (nexus-dyfsw)
            except BlockingIOError:
                # Another process's sweep probes the file for microseconds; ask once more.
                time.sleep(0.05)
                _locking.lock_fd(fd, blocking=False)  # lifecycle-gate-allow: the same marker lock, retried once after a sweep's probe (nexus-dyfsw)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("ascii"))
        except BaseException:
            os.close(fd)
            raise
        _worker_lock_fd = fd
    except Exception:  # noqa: BLE001 — lock-file write is best-effort; failure logged via log.warning
        _log.warning("aspect_worker_lock_write_failed", exc_info=True)


def _remove_worker_lock(locks_dir: Path | None = None) -> None:
    """Remove the process-scoped lock file when the worker stops, and release its lock.

    Non-fatal if the file does not exist or cannot be removed.
    """
    import os  # noqa: PLC0415 — stdlib os deferred to function scope
    import sys  # noqa: PLC0415 — deferred with os above

    global _worker_lock_fd
    fd, _worker_lock_fd = _worker_lock_fd, None
    try:
        if fd is not None and sys.platform == "win32":
            os.close(fd)  # Windows cannot unlink an open file
            fd = None
        _worker_lock_path(locks_dir).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — lock-file remove is best-effort; failure logged via log.warning
        _log.warning("aspect_worker_lock_remove_failed", exc_info=True)
    finally:
        if fd is not None:
            os.close(fd)


def ensure_worker_started(
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_S,
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
            )
        _worker.start()
    _write_worker_lock(_locks_dir)
    return _worker


def stop_worker(timeout: float = 10.0, _locks_dir: Path | None = None) -> bool:
    """Stop the singleton worker if running. Idempotent.

    Note: leaves the singleton instance in place (stopped) so a
    subsequent ``ensure_worker_started`` call rebuilds the daemon
    thread on the existing instance. Tests that need a fresh
    instance should call ``reset_worker_for_tests`` instead.

    Removes the process-scoped lock file (SIG-5) when the worker stops.
    Returns ``False`` when the worker thread outlived *timeout*
    (see :meth:`AspectExtractionWorker.stop`), else ``True``.
    """
    global _worker
    with _worker_lock:
        worker = _worker
    stopped = True if worker is None else worker.stop(timeout=timeout)
    _remove_worker_lock(_locks_dir)
    return stopped


def reset_worker_for_tests(timeout: float = 5.0) -> bool:
    """Test helper: tear down the worker singleton so the next
    ``ensure_worker_started`` call rebuilds with fresh state. Returns
    ``False`` when the old worker's thread was still running after the stop
    (the singleton is dropped regardless).
    """
    global _worker
    stopped = stop_worker(timeout=timeout)
    with _worker_lock:
        _worker = None
    return stopped


def drain_worker(
    queue_path: Path | str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 0.1,
    _locks_dir: Path | None = None,
) -> None:
    """Stop the singleton worker and wait until the queue is fully drained.

    The queue is drained when the ``aspects`` tuple queue has
    ``available + claimed == 0``. A backoff-delayed task counts as available,
    so the test is conservative. Dead tasks are terminal and do not block a PK
    migration. On an engine with no ``aspects`` template the queue reads as
    empty.

    Protocol (RDR-108 Phase 1 S1, nexus-he24; PG's claim safety owns
    cross-process correctness, so no lock-file pre-check):

      1. Set the stop signal on the singleton worker (if one exists) so
         it claims no new tasks.  In-flight processing continues.
      2. Poll the census at ``poll_interval`` (default 100 ms) until no task is
         live, or ``timeout`` elapses.
      3. On success: join the worker thread (it has already exited or will
         exit imminently after the last iteration).  If the thread does not
         exit within 2 seconds, log a warning (S-5) and continue — the
         thread is a daemon and will die with the process.
      4. On timeout: raise ``DrainTimeoutError`` so the operator is alerted
         to inspect claimed tasks.

    Args:
        queue_path: Legacy path parameter, retained for signature stability.
            The queue is engine-side and the census poll goes over HTTP; no
            local connection is opened from this path.
        timeout: Seconds to wait for the queue to drain.  Default 120s.
            RDR-089 P1.3 measured ~26.5s median extraction time with a
            tail to 90s for the scholarly-paper-v1 extractor.  120s
            (approximately 4x median) provides adequate margin while
            still surfacing genuinely stuck workers within two minutes.
            Callers that know their workload is lighter may pass a smaller
            value.
        poll_interval: Seconds between census checks (default 0.1).
        _locks_dir: Retained-and-ignored for signature stability.

    Raises:
        DrainTimeoutError: ``aspects`` still has available/claimed tasks after
            ``timeout`` seconds.

    Note:
        If no worker has been started in this process (``get_worker()``
        returns None), the function simply checks the census once and
        returns — there is no thread to stop and no in-flight work to wait
        for.
    """
    # Validation only: drain builds its store directly (bypassing
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

    from nexus.db.t2.http_aspect_queue import aspects_census  # noqa: PLC0415 — deferred (optional service dep)
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred (optional service dep)
    tuples: HttpTupleStore | None = None

    def _tuple_live() -> int:
        """Tasks that still need a worker: available + claimed on ``aspects``
        (0 on an engine with no such template). Dead tasks do not count. A read
        that fails raises: an unreadable side is never read as drained."""
        census = aspects_census(tuples)
        return 0 if census is None else census.available + census.claimed

    def _drained() -> bool:
        return _tuple_live() == 0

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
            # nexus-22out: kept on the worker's record, so a later start() refuses beside it, as after a
            # stop() that timed out; dropped, start() would clear the shared stop event under it.
            with w._lock:
                w._stopping.append(thread)
            _log.warning(
                "drain_worker_thread_join_timeout",
                thread_id=thread.ident,
                timeout=2.0,
            )

    try:
        tuples = HttpTupleStore()
        # Short-circuit: if already drained, return immediately.
        if _drained():
            if worker is not None and worker.is_running():
                _join_worker_thread(worker)
            return

        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            if _drained():
                # Join the worker thread: it has stopped claiming and will
                # exit after its current iteration completes.
                if worker is not None:
                    _join_worker_thread(worker)
                return

        # Timeout: count the live tasks for the error message.
        census = aspects_census(tuples)
        tasks_live = 0 if census is None else census.available + census.claimed
        detail = None
        if tasks_live:
            # A lapsed claim is still counted as claimed; the next claim
            # releases it.
            detail = (
                f"Tuple queue 'aspects': {census.available} available, "
                f"{census.claimed} claimed ({census.lapsed} of them lapsed). "
                "A running aspect worker claims the available tasks and "
                "releases the lapsed ones; ensure one is up ('nx daemon "
                "aspect-worker start', or it spawns on the next aspect "
                "enqueue), then retry the drain."
            )
        raise DrainTimeoutError(stuck_count=tasks_live, timeout=timeout, detail=detail)
    finally:
        if tuples is not None:
            tuples.close()


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
    row carries the one captured at enqueue (nexus-tdgc). The completion
    path (``TupleQueueSide``'s verdict map) goes through here so a row with a
    known identity is never persisted unattributed.

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


#: The registry names (``src/nexus/tables/llm-surfaces.toml``) the document gates of this module log under.
_SURFACE_ENQUEUE = "aspect_enqueue_hook"
_SURFACE_DRAIN = "aspect_worker_drain"


def sensitive_enqueue_filter(rows: list[dict]) -> list[dict]:
    """RDR-243 census row 1: drop the rows of sensitive documents before anything is queued.

    The ONE enqueue gate: the hook, the indexer's batch (``enqueue_many``) and
    ``nx aspects requeue-failed`` all pass their rows through it, so no enqueue path reaches the
    queue (and persists a document's text in a row or a task) without it. The flag is read from the
    CATALOG row of each ``doc_id``, one batched read. It must run before the enqueue, which on the
    table form upserts a status back to ``pending``. Each dropped row logs
    ``aspect_enqueue_skipped_sensitive`` with the tumbler and the surface.

    Raises :class:`~nexus.doc_sensitivity.SensitivityUnknown` when the catalog cannot be read: a
    row whose flag is unknown is not queued, and the caller's failure handler reports it."""
    flagged = doc_sensitivity.flagged(r.get("doc_id", "") for r in rows)
    if not flagged:
        return list(rows)
    kept: list[dict] = []
    for row in rows:
        if row.get("doc_id", "") in flagged:
            _log.info(
                "aspect_enqueue_skipped_sensitive",
                tumbler=row.get("doc_id", ""), surface=_SURFACE_ENQUEUE,
                collection=row.get("collection", ""),
            )
        else:
            kept.append(row)
    return kept


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
    """Post-document hook: enqueue a task for async aspect extraction.

    Signature matches ``HookRegistry.fire_document(source_path,
    collection, content)``. This hook does NOT call extract_aspects
    inline — that would block the ingest path for ~25 s per document
    (RDR-089 P1.3 spike finding). Instead it:

      1. Skips collections without a registered extractor config
         (currently ``knowledge__*``, ``rdr__*``, ``docs__*``).
      2. Resolves ``doc_id`` when the caller did not supply one (hygiene-001,
         nexus-tk070.p6a follow-on, review round item B): the task's identity
         is its ``doc_id``, so a blank one cannot be enqueued. This is the CHOKE POINT for that
         decision — every ``fire_document`` call site shares it, so the
         resolve-or-skip logic lives here once. A caller-supplied doc_id is
         never overridden; a blank one is looked up via the catalog reader
         by ``(collection, source_path)``. Still unresolvable -> the
         enqueue is SKIPPED entirely, logged as
         ``aspect_enqueue_skipped_no_doc_id`` — never enqueued with ``""``.
      3. Writes one task to the ``aspects`` tuple queue (a replace by
         ``doc_id``). The task carries the document's identity, never its
         text: the worker re-reads the document from T3 chunks or the file.
      4. Lazy-starts the worker if not already running.

    The hook is synchronous (RDR-089 P0.1 contract). It does not
    block on extraction; the worker drains in a background thread.

    Content-sourcing contract: ``content`` is accepted for the hook signature
    and dropped; the task carries identity only (RDR-235 E2).
    """
    from nexus.aspect_extractor import extraction_applies_to_source, select_config  # noqa: PLC0415 — deferred to avoid circular import (aspect_extractor)
    if select_config(collection) is None:
        return  # No extractor for this collection — nothing to enqueue.
    if not extraction_applies_to_source(collection, source_path):
        # nexus-kk4ut: an opted-in docs__ collection's non-prose file.
        _log.debug("aspect_enqueue_skipped_non_prose", collection=collection, source_path=source_path)
        return
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

    # RDR-243 census row 1: a sensitive document is never queued, and its text is never persisted
    # in a row. The catalog row is read before the enqueue; a read that fails refuses (loudly).
    try:
        kept = sensitive_enqueue_filter([{"collection": collection, "source_path": source_path, "doc_id": doc_id}])
    except SensitivityUnknown as exc:
        _log.warning(
            "aspect_enqueue_skipped_sensitivity_unknown",
            tumbler=doc_id, surface=_SURFACE_ENQUEUE, collection=collection, error=exc.detail,
        )
        return
    if not kept:
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
        # RDR-235: doc_id and the error class are named, so a refusal that
        # repeats (MaxLiveRowsExceeded, an over-long source_path) is countable
        # per document; the hook_failures row below is the persistent count.
        _log.warning(
            "aspect_extraction_enqueue_failed",
            source_path=source_path,
            collection=collection,
            doc_id=doc_id,
            error_type=type(exc).__name__,
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


def record_batch_enqueue_failure(collection: str, count: int, exc: BaseException) -> None:
    """Persist a ``hook_failures`` row for a batched enqueue that failed (RDR-235).

    The batch form of the single-document tripwire in
    :func:`aspect_extraction_enqueue_hook`: the indexer's flush hook swallows the
    failure to keep ingest moving, which would hide a ``MaxLiveRowsExceeded`` or
    a retired collection from the recorder. Best-effort: a telemetry-write
    failure never blocks ingest."""
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred to avoid circular import (mcp_infra)
    try:
        t2_index_write(
            lambda t2: t2.telemetry.record_hook_failure(
                doc_id=f"<batch of {count}>",
                collection=collection,
                hook_name="aspect_extraction_enqueue_hook",
                error=f"{type(exc).__name__}: {exc}"[:2000],
                chain="document",
            )
        )
    except Exception:  # noqa: BLE001 — tripwire persist is best-effort; never block ingest
        _log.warning(
            "aspect_enqueue_tripwire_persist_failed", collection=collection, exc_info=True,
        )


def _best_effort_queue_depth() -> int | None:
    """Pending work for the diagnostic signal (RDR-173 P5), or None if it cannot
    be obtained cheaply: the ``aspects`` tuple queue's available tasks.

    The service queue is usually reachable even when the WORKER daemon is not
    (the daemon-spawn path is only reached AFTER a successful enqueue), so this
    normally answers. A SHORT 2s cap on the call (review H1) keeps the
    observability path from blocking the store hook for the client's default
    30s in a full outage.

    A failed read returns None ("not known"). An engine with no ``aspects``
    template (it predates ``engine-service-v0.1.169``) is genuinely empty and
    reads as 0, with one log line per process."""
    try:
        from nexus.db.t2.http_aspect_queue import aspects_census  # noqa: PLC0415 — deferred; service-side
        from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred; service-side
        from nexus.mcp_infra import current_index_run_t2_client  # noqa: PLC0415 — deferred to avoid circular import

        # nexus-m20mf P3 fold-in (round-2 critique finding 2): share the ACTIVE
        # index run's client when there is one (constructed WITHOUT a
        # constructor-time timeout= -- the mixin rejects client=+non-default-
        # timeout= together -- and cap THIS call via subspace_stats(timeout=2.0)
        # instead), falling back to this diagnostic's own dedicated
        # short-timeout client when no run is active (e.g. a live MCP
        # store-hook call, outside any index_repository() run).
        shared_client = current_index_run_t2_client()
        if shared_client is not None:
            t = HttpTupleStore(tenant=_ENQUEUE_TENANT, client=shared_client)
        else:
            t = HttpTupleStore(tenant=_ENQUEUE_TENANT, timeout=2.0)
        try:
            census = aspects_census(t, timeout=2.0 if shared_client is not None else None)
            return 0 if census is None else int(census.available)
        finally:
            t.close()  # no-op when the shared client is not owned by this store
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
