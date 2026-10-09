"""Off-request writer for per-search threshold telemetry (nexus-vpa9q).

``search_cross_corpus`` hands one batch of ``(ts, query_hash, collection,
raw_count, kept_count, top_distance, threshold)`` rows per call to whatever
it was given as *telemetry* (RDR-087 Phase 2.2). On the MCP search and query
paths that was a per-call ``T2Database`` store: a fresh ``httpx.Client``
whose POST /v1/telemetry/search/batch paid a TLS handshake plus the engine
write on the caller's clock, 0.42-0.47 s of a 2.6-3.3 s default search
(measured 2026-10-09 01:07:42Z, published 7.75.0 client, managed engine
v0.1.154).

:class:`BackgroundSearchTelemetry` keeps the same ``log_search_batch``
surface, so ``search_cross_corpus`` and its opt-out
(``telemetry.search_enabled``) are unchanged. It queues the batch and
returns. One daemon worker drains the queue through an injected *write*
callable. The queue is bounded: when it is full, or after :meth:`close`,
a batch is dropped and counted, never waited on. Telemetry is diagnostic, so
losing a batch under back-pressure is the right trade against a slower
search.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Queued batches before new ones are dropped. One batch per search call; a
#: healthy engine drains a batch in ~0.1-0.2 s, so 64 is minutes of backlog
#: only when the engine is unreachable, and then the drop is wanted.
DEFAULT_MAX_PENDING: int = 64

SearchRows = Sequence[tuple[Any, ...]]


class BackgroundSearchTelemetry:
    """Bounded, single-worker, drop-on-full writer with the
    ``log_search_batch`` surface ``search_cross_corpus`` expects."""

    def __init__(
        self,
        write: Callable[[list[tuple[Any, ...]]], object],
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        thread_name: str = "nexus-search-telemetry",
    ) -> None:
        if max_pending < 1:
            raise ValueError(f"max_pending must be >= 1, got {max_pending}")
        self._write = write
        self._max_pending = max_pending
        self._thread_name = thread_name
        self._cv = threading.Condition()
        self._pending: deque[list[tuple[Any, ...]]] = deque()
        self._in_flight = 0
        self._closed = False
        self._thread: threading.Thread | None = None
        #: Batches refused because the queue was full or the sink was closed.
        self.dropped = 0
        #: Batches whose write raised.
        self.failed = 0

    def log_search_batch(self, rows: SearchRows) -> int:
        """Queue *rows* for the worker and return at once.

        Returns the number of rows queued: ``len(rows)``, or 0 when *rows*
        is empty or the batch was dropped.
        """
        if not rows:
            return 0
        batch = list(rows)
        with self._cv:
            if self._closed or len(self._pending) >= self._max_pending:
                self.dropped += 1
                _log.debug(
                    "search_telemetry_batch_dropped",
                    rows=len(batch), closed=self._closed,
                    pending=len(self._pending), dropped=self.dropped,
                )
                return 0
            if self._thread is None or not self._thread.is_alive():
                # First batch, or the worker died on a BaseException that
                # ``_run`` does not catch: start a fresh one. Assigned only
                # after start() succeeds (it raises at interpreter shutdown).
                thread = threading.Thread(
                    target=self._run, name=self._thread_name, daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    self.dropped += 1
                    _log.debug("search_telemetry_worker_start_failed", rows=len(batch))
                    return 0
                self._thread = thread
            self._pending.append(batch)
            self._cv.notify_all()
        return len(batch)

    def flush(self, timeout: float) -> bool:
        """Wait up to *timeout* seconds for every queued batch to finish.

        Returns True when nothing is queued or in flight.
        """
        with self._cv:
            return self._cv.wait_for(
                lambda: not self._pending and self._in_flight == 0, timeout,
            )

    def close(self, timeout: float) -> bool:
        """Refuse new batches, drain what is queued, stop the worker.

        Bounded by *timeout*: returns False when the worker is still busy at
        the deadline (a hung write). Batches still queued then are discarded
        and counted as dropped, so the abandoned worker exits after its
        current write instead of writing on into whatever comes next. The
        worker is a daemon thread, so it never holds up interpreter exit.
        Idempotent.
        """
        with self._cv:
            self._closed = True
            self._cv.notify_all()
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        if not thread.is_alive():
            return True
        with self._cv:
            self.dropped += len(self._pending)
            self._pending.clear()
        return False

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._pending and not self._closed:
                    self._cv.wait()
                if not self._pending:
                    return  # closed and drained
                batch = self._pending.popleft()
                self._in_flight += 1
            try:
                self._write(batch)
            except Exception:  # noqa: BLE001 — best-effort telemetry; a failed write must not kill the worker
                with self._cv:
                    self.failed += 1
                _log.debug("search_telemetry_write_failed", rows=len(batch), exc_info=True)
            finally:
                with self._cv:
                    self._in_flight -= 1
                    self._cv.notify_all()
