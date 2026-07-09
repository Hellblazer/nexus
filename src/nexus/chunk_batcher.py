# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-file chunk batching for the repo indexer (nexus-f55fu, duoak 2C).

The per-file upsert pattern amortizes ~nothing: the median source file is
3-15 chunks, so a 1,200-file index pays ~1,200 embed round trips. This
accumulator collects chunks across files per collection and flushes in
service-cap-sized batches (~30-40 calls for the same repo), collapsing
wall time toward the pure embed floor.

FILE-ATOMIC BATCHES (review Critical, nexus-1ugqs): a file's chunks never
straddle a flush boundary — ``add()`` pre-flushes the pending buffer when
the file wouldn't fit, and REFUSES files larger than one batch (caller
falls back to the legacy per-file upsert). Consequence: a failed flush
means NONE of its files' chunks landed, so the next run's staleness check
sees them stale and retries — identical healing contract to the legacy
per-file path. Without atomicity, a partially-landed file's chunks carry
current ``content_hash`` metadata, the staleness cache reads the file as
current, and the un-hooked chunks (no manifest/chash/taxonomy rows) are
orphaned permanently.

Failure containment (nexus-wcs39): every batch carries a file->chunk-count
attribution map. A flush that raises (after the transport's own gateway
retries) marks exactly the contributing files failed via
``on_file_failed``; other files and subsequent batches proceed.

Thread-safe: indexer workers call :meth:`add` concurrently. The network
flush runs with the lock RELEASED (review Medium) — only buffer mutation
and settlement hold it — so one worker's flush never blocks another
worker's staging. Completion/failure callbacks also run unlocked: they
fire post-store hook chains (network calls).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog

_log = structlog.get_logger(__name__)

#: Service write cap (chroma_quotas MAX_RECORDS_PER_WRITE parity).
DEFAULT_MAX_CHUNKS: int = 300

FlushFn = Callable[[str, list[str], list[str], list[dict]], None]

#: (path, error-or-None, context) settled-file record awaiting callback.
_Settled = tuple[str, "str | None", object]


@dataclass
class _Pending:
    """Accumulated, not-yet-flushed chunks for one collection."""

    ids: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    metadatas: list[dict] = field(default_factory=list)
    #: file path -> number of its chunks in THIS pending buffer
    file_counts: dict[str, int] = field(default_factory=dict)
    bytes: int = 0


@dataclass
class _FileState:
    """Bookkeeping for one staged file (always single-batch)."""

    outstanding: int = 0
    failed: str | None = None
    finished_adding: bool = False
    #: opaque caller payload handed back on completion/failure (e.g. the
    #: deferred post-store hook arguments for this file)
    context: object = None


class ChunkBatcher:
    """Accumulate chunks across files; flush per collection at the cap.

    ``add()`` is the whole per-file contract: pass every chunk of the
    file in one call. Returns ``True`` when staged; ``False`` when the
    file alone exceeds one batch (chunk cap or byte ceiling) — the
    caller must then use its legacy per-file upsert path, preserving
    per-file atomicity for oversize files too.

    ``on_file_complete(path, context)`` fires after the file's batch
    flushes successfully; ``on_file_failed(path, error, context)`` after
    its batch fails. Both run WITHOUT the internal lock held.
    """

    def __init__(
        self,
        *,
        flush: FlushFn,
        on_file_complete: Callable[[str, object], None] | None = None,
        on_file_failed: Callable[[str, str, object], None] | None = None,
        on_batch_complete: "Callable[[str, list[str], list[str], list[dict], list[tuple[str, object]]], None] | None" = None,
        max_chunks: "int | Callable[[str], int]" = DEFAULT_MAX_CHUNKS,
        max_bytes: int | None = None,
        flush_concurrency: int = 1,
    ) -> None:
        if isinstance(max_chunks, int) and max_chunks < 1:
            raise ValueError("max_chunks must be >= 1")
        self._flush = flush
        self._on_complete = on_file_complete or (lambda _p, _c=None: None)
        self._on_failed = on_file_failed or (lambda _p, _e, _c=None: None)
        #: nexus-duoak.7: fired once per SUCCESSFUL flush with the whole
        #: batch (collection, ids, documents, metadatas) — the seam for
        #: flush-grain hooks (taxonomy/chash run per upload batch, not per
        #: file). Runs unlocked, before the per-file completions.
        self._on_batch_complete = on_batch_complete or (lambda _c, _i, _d, _m, _fc: None)
        self._max_chunks = max_chunks
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._files: dict[str, _FileState] = {}
        self._failed_files: dict[str, str] = {}
        self._flush_count = 0
        self._flush_seconds = 0.0
        #: duoak follow-up: >1 dispatches flushes to a bounded pool so
        #: neither staging workers nor drain() serialize the network
        #: calls. 1 (default) = synchronous v1 behavior. Ceiling should
        #: respect the service's per-collection concurrent-write quota.
        self._flush_pool = None
        self._futures: list = []
        if flush_concurrency > 1:
            from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415 — only with concurrency enabled

            self._flush_pool = ThreadPoolExecutor(
                max_workers=flush_concurrency,
                thread_name_prefix="nx-flush",
            )

    def _cap(self, collection: str) -> int:
        """Per-collection chunk cap — CCE (docs/knowledge/rdr) collections
        embed much slower server-side and need smaller batches to stay
        inside the gateway timeout (live 504 at 172 CCE chunks,
        2026-07-04 2C smoke)."""
        if callable(self._max_chunks):
            return max(1, int(self._max_chunks(collection)))
        return self._max_chunks

    @property
    def failed_files(self) -> dict[str, str]:
        """file path -> error message, for end-of-run reporting."""
        with self._lock:
            return dict(self._failed_files)

    @property
    def stats(self) -> dict[str, float]:
        """Flush-count / cumulative flush seconds (--debug-timing report)."""
        with self._lock:
            return {
                "flushes": float(self._flush_count),
                "flush_seconds": self._flush_seconds,
            }

    @property
    def pending_summary(self) -> dict[str, int]:
        """What ``drain()`` would have to do right now (nexus-uizok).

        ``chunks``/``collections`` count the staged-but-unflushed buffers;
        ``in_flight`` counts pool flushes already dispatched by ``add()``
        overflows that ``drain()`` must still wait on. Lets the caller emit
        an honest phase marker before a potentially minutes-long drain.
        """
        with self._lock:
            return {
                "chunks": sum(len(p.ids) for p in self._pending.values()),
                "collections": sum(1 for p in self._pending.values() if p.ids),
                # not-done, not len(_futures): the list can still hold a few
                # settled entries between prunes (and kept-for-drain raisers).
                "in_flight": sum(1 for f in self._futures if not f.done()),
            }

    def add(
        self,
        file_path: str,
        collection: str,
        ids: list[str],
        documents: list[str],
        metadatas: list[dict],
        *,
        context: object = None,
    ) -> bool:
        """Stage all chunks of ``file_path``; ``False`` = use legacy path.

        ``context`` is an opaque payload returned verbatim to
        ``on_file_complete`` / ``on_file_failed`` — the wiring layer uses
        it to carry the file's deferred post-store hook arguments.
        """
        if not (len(ids) == len(documents) == len(metadatas)):
            raise ValueError(
                f"length mismatch: ids={len(ids)} documents={len(documents)} "
                f"metadatas={len(metadatas)}"
            )
        cap = self._cap(collection)
        file_bytes = sum(len(d.encode()) for d in documents if isinstance(d, str))
        if len(ids) > cap or (
            self._max_bytes is not None and file_bytes > self._max_bytes
        ):
            # Oversize: cannot be file-atomic in one batch — refuse; the
            # caller's legacy per-file upsert keeps today's semantics.
            return False

        to_flush: list[tuple[str, _Pending]] = []
        with self._lock:
            state = self._files.setdefault(file_path, _FileState())
            if state.finished_adding and state.outstanding > 0:
                # Contract: add() is called EXACTLY ONCE per file. A
                # re-add of an unsettled file would corrupt the
                # attribution map and _split()'s contiguity assumption,
                # silently stranding the file (critic finding). Loud.
                raise ValueError(
                    f"file staged twice before settling: {file_path}"
                )
            state.outstanding += len(ids)
            state.finished_adding = True
            state.context = context
            if not ids:
                settled: list[_Settled] = []
                self._settle_file_locked(file_path, settled)
            else:
                settled = []
                pend = self._pending.setdefault(collection, _Pending())
                would_overflow = len(pend.ids) + len(ids) > cap or (
                    self._max_bytes is not None
                    and pend.bytes + file_bytes > self._max_bytes
                )
                if would_overflow and pend.ids:
                    to_flush.append((collection, pend))
                    pend = _Pending()
                    self._pending[collection] = pend
                pend.ids.extend(ids)
                pend.documents.extend(documents)
                pend.metadatas.extend(metadatas)
                pend.bytes += file_bytes
                pend.file_counts[file_path] = len(ids)
                if len(pend.ids) >= cap or (
                    self._max_bytes is not None and pend.bytes >= self._max_bytes
                ):
                    to_flush.append((collection, pend))
                    del self._pending[collection]
        self._invoke_callbacks(settled)
        for coll, batch in to_flush:
            self._dispatch_flush(coll, batch)
        return True

    def drain(
        self,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        """Flush every non-empty pending buffer (end-of-run).

        ``on_progress(done, total)`` (nexus-uizok) fires after each flush
        completes — the operator heartbeat for a drain that can run
        minutes (one flush per pending collection, plus every genuinely
        outstanding pool flush from earlier ``add()`` overflows; settled
        futures are pruned at dispatch time so ``total`` reflects real
        work). ``total`` is fixed at drain start; runs unlocked. Pooled
        waiting uses ``as_completed``, so if an exception DOES escape a
        future (``_flush_batch`` contains its own failures; only a
        raising completion callback reaches here), which one surfaces
        first follows completion order, not submission order.

        Returns the number of flushes this drain performed/awaited, so
        the caller's closing marker can report drain-scoped volume
        (distinct from the run-wide ``stats["flushes"]``).
        """
        with self._lock:
            to_flush = [
                (coll, pend) for coll, pend in self._pending.items() if pend.ids
            ]
            self._pending = {}
        if self._flush_pool is None:
            done = 0
            for coll, batch in to_flush:
                self._dispatch_flush(coll, batch)
                done += 1
                if on_progress is not None:
                    on_progress(done, len(to_flush))
            return done
        for coll, batch in to_flush:
            self._dispatch_flush(coll, batch)
        # Wait for every in-flight flush (including ones dispatched by
        # earlier add() overflows) so callers see all callbacks fired.
        with self._lock:
            futures, self._futures = self._futures, []
        from concurrent.futures import as_completed  # noqa: PLC0415 — only with concurrency enabled

        # Futures already settled by now finished during the file loop —
        # surface any retained exception, but don't count them as drain
        # work (they'd inflate done/total with instant "progress" for
        # long-finished flushes — nexus-uizok critique HIGH-2).
        outstanding = []
        for f in futures:
            if f.done():
                f.result()
            else:
                outstanding.append(f)
        done = 0
        for f in as_completed(outstanding):
            f.result()
            done += 1
            if on_progress is not None:
                on_progress(done, len(outstanding))
        self._flush_pool.shutdown(wait=True)
        self._flush_pool = None  # post-drain adds fall back to sync
        return done

    # ── internals ────────────────────────────────────────────────────────

    def _dispatch_flush(self, collection: str, pend: _Pending) -> None:
        if self._flush_pool is None:
            self._flush_batch(collection, pend)
            return
        fut = self._flush_pool.submit(self._flush_batch, collection, pend)
        with self._lock:
            # Prune settled, exception-free futures so ``_futures`` tracks
            # genuinely outstanding work — unpruned it grew to "every flush
            # ever dispatched", making pending_summary's in_flight and
            # drain()'s progress denominator lies on long runs
            # (nexus-uizok critique HIGH-2). Futures that RAISED are kept
            # so drain() still surfaces the exception via f.result().
            self._futures = [
                f for f in self._futures
                if not f.done() or f.exception() is not None
            ]
            self._futures.append(fut)

    def _flush_batch(self, collection: str, pend: _Pending) -> None:
        """Network flush with the lock RELEASED; settle + callbacks after.

        On failure with >= 2 files, BISECT: split by files and flush each
        half independently. A batch too big for the gateway timeout
        self-tunes down; a genuinely poisoned file is isolated to itself
        (only it fails). Depth is naturally log2(files).
        """
        import time  # noqa: PLC0415 — leaf util; keep module import surface minimal

        error: str | None = None
        t0 = time.monotonic()
        try:
            self._flush(collection, pend.ids, pend.documents, pend.metadatas)
        except Exception as exc:  # noqa: BLE001 — attribution boundary: convert to per-file failure or bisect
            if len(pend.file_counts) >= 2:
                _log.warning(
                    "chunk_batch_flush_bisect",
                    collection=collection,
                    chunks=len(pend.ids),
                    files=len(pend.file_counts),
                    error=str(exc),
                )
                with self._lock:
                    self._flush_count += 1
                    self._flush_seconds += time.monotonic() - t0
                for half in self._split(pend):
                    self._flush_batch(collection, half)
                return
            error = str(exc)
            _log.warning(
                "chunk_batch_flush_failed",
                collection=collection,
                chunks=len(pend.ids),
                files=len(pend.file_counts),
                error=error,
            )
        elapsed = time.monotonic() - t0
        if error is None:
            with self._lock:
                file_contexts = [
                    (path, self._files[path].context)
                    for path in pend.file_counts
                    if path in self._files
                ]
            try:
                self._on_batch_complete(
                    collection, pend.ids, pend.documents, pend.metadatas,
                    file_contexts,
                )
            except Exception:  # noqa: BLE001 — flush-grain hooks are best-effort, never fail the batch
                _log.warning(
                    "chunk_batch_complete_callback_failed",
                    collection=collection,
                    chunks=len(pend.ids),
                    exc_info=True,
                )
        settled: list[_Settled] = []
        with self._lock:
            self._flush_count += 1
            self._flush_seconds += elapsed
            for path, count in pend.file_counts.items():
                state = self._files[path]
                state.outstanding -= count
                if error is not None and state.failed is None:
                    state.failed = error
                self._settle_file_locked(path, settled)
        self._invoke_callbacks(settled)

    @staticmethod
    def _split(pend: _Pending) -> "list[_Pending]":
        """Split a batch into two halves along FILE boundaries."""
        paths = list(pend.file_counts)
        mid = len(paths) // 2
        halves: list[_Pending] = []
        offset = 0
        boundaries = [paths[:mid], paths[mid:]]
        # file_counts preserves insertion order == chunk order in the lists
        for group in boundaries:
            n = sum(pend.file_counts[p] for p in group)
            half = _Pending(
                ids=pend.ids[offset : offset + n],
                documents=pend.documents[offset : offset + n],
                metadatas=pend.metadatas[offset : offset + n],
                file_counts={p: pend.file_counts[p] for p in group},
            )
            halves.append(half)
            offset += n
        return [h for h in halves if h.ids]

    def _settle_file_locked(self, path: str, settled: list[_Settled]) -> None:
        state = self._files.get(path)
        if state is None or not state.finished_adding or state.outstanding > 0:
            return
        del self._files[path]
        if state.failed is not None:
            self._failed_files[path] = state.failed
        settled.append((path, state.failed, state.context))

    def _invoke_callbacks(self, settled: list[_Settled]) -> None:
        """Run completion/failure callbacks with the lock RELEASED."""
        for path, failed, context in settled:
            if failed is not None:
                self._on_failed(path, failed, context)
            else:
                self._on_complete(path, context)
