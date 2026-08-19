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

#: Service write cap (nexus.db.limits MAX_RECORDS_PER_WRITE parity).
DEFAULT_MAX_CHUNKS: int = 300

#: (collection, ids, documents, metadatas, file_contexts) -> None.
#: nexus-wxjr6: file_contexts (the same (path, context) pairs
#: on_batch_begin/on_batch_complete already receive) is threaded into the
#: flush closure itself so a combined-write flush fn can build per-doc
#: manifest rows in the SAME call that uploads chunk content — no new
#: plumbing, this is the identical shape computed once per flush at
#: _flush_batch's top (see the call site below).
FlushFn = Callable[[str, list[str], list[str], list[dict], "list[tuple[str, object]]"], None]

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
        on_batch_begin: "Callable[[str, list[tuple[str, object]]], None] | None" = None,
        on_flush: "Callable[[int, int, str, float, str | None], None] | None" = None,
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
        #: nexus-vw594 F1: the symmetric counterpart, fired ONCE PER FLUSH
        #: BEFORE the network upload (``self._flush`` below) instead of
        #: after — the seam for the index-run fence's begin stamp
        #: (RUNFENCE memo §3.5 T0 ordering: begin before the first byte
        #: lands, complete only after the last). Same ``(path, context)``
        #: shape as ``on_batch_complete`` so a caller can extract the same
        #: per-file ``catalog_doc_id`` / ``content_hash`` pair from either.
        #: Best-effort: a failure here must never block the actual upload.
        self._on_batch_begin = on_batch_begin or (lambda _c, _fc: None)
        #: nexus-rhwg5 / GH #1432 ask 3 residue: fired once per SETTLED
        #: flush (never a bisect attempt -- same contract as the
        #: ``chunk_flush_complete`` structlog event below) with
        #: ``(flush_num, chunks, collection, elapsed, error)``. Unlike
        #: ``drain()``'s ``on_progress`` (a per-call parameter -- drain()
        #: is called once), this is a CONSTRUCTOR callback: ``add()`` is
        #: called once per file and dispatches flushes internally, so
        #: there is no per-call site to hand a callback to. Suppressed
        #: while ``drain()`` is running (``self._draining``) so an
        #: in-flight flush dispatched earlier by ``add()`` is reported by
        #: exactly one voice -- drain's own ``on_progress`` already
        #: counts "awaited" futures, so both firing would be the same
        #: two-liveness-voices class nexus-1iw8k fixed for the ETA
        #: ticker / phase heartbeat.
        self._on_flush = on_flush
        self._draining = False
        self._max_chunks = max_chunks
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._files: dict[str, _FileState] = {}
        self._failed_files: dict[str, str] = {}
        self._flush_count = 0
        #: FULL flush wall: upload (self._flush) + on_batch_complete
        #: (flush-grain hooks) + the per-file callback chain
        #: (nexus-lde88 G3 — previously stopped before the hooks, so
        #: this under-reported flush cost by ~4x against the real wall).
        self._flush_seconds = 0.0
        #: The self._flush(...) network write ALONE — what "flush_seconds"
        #: used to (mis)represent as the whole flush. Kept as its own
        #: counter so both numbers stay available (nexus-lde88 G3).
        self._upload_seconds = 0.0
        #: The file-settlement bookkeeping block (state.outstanding
        #: decrement + _settle_file_locked, under self._lock) — review
        #: round 2 (both reviewers, critic-proven: 0.03s injected delay
        #: there showed up as 0.0000s across every other bucket AND in
        #: flush_seconds). Its own counter closes that gap.
        self._settle_seconds = 0.0
        #: The ``on_batch_begin`` callback (nexus-vw594 F1's RUNFENCE
        #: ``_fence_begin_many`` stamp) — a real network round trip that
        #: fires BEFORE the upload timer starts, so until nexus-jb4pp it
        #: was excluded from ``upload_s``/``flush_hook_s``/``settle_s``/
        #: ``file_hook_s``, from ``flush_seconds``, AND from the indexer's
        #: end-of-run per-grain report: a whole leg attributed nowhere,
        #: which is why the flush-grain split could not be made to sum.
        #: Same class of gap ``settle_seconds`` closed in lde88 round 2.
        self._begin_hook_seconds = 0.0
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
        """Flush-count / cumulative flush seconds (--debug-timing report).

        ``flush_seconds`` is the FULL flush wall — the sum of every
        attributed phase (``upload_seconds`` + flush-grain hooks +
        ``settle_seconds`` + file-grain hooks; the latter two are not
        separately exposed here, only via the per-flush
        ``chunk_flush_complete`` event's ``flush_hook_s``/``settle_s``/
        ``file_hook_s`` fields). ``upload_seconds`` is the network write
        alone (nexus-lde88 G3 — the two used to be conflated under one
        name that only ever measured the upload leg). ``settle_seconds``
        is the file-settlement bookkeeping block alone (review round 2 —
        previously silently excluded from ``flush_seconds`` entirely).
        ``begin_hook_seconds`` is the pre-upload ``on_batch_begin``
        callback alone (nexus-jb4pp — same silent-exclusion class).
        """
        with self._lock:
            return {
                "flushes": float(self._flush_count),
                "flush_seconds": self._flush_seconds,
                "upload_seconds": self._upload_seconds,
                "settle_seconds": self._settle_seconds,
                "begin_hook_seconds": self._begin_hook_seconds,
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
            # nexus-rhwg5: suppress on_flush for the duration of drain() —
            # see the constructor docstring for the mutual-exclusion
            # rationale. Set under the lock alongside the pending swap so
            # there is no window where a concurrently-dispatched flush
            # observes the old value.
            self._draining = True
        try:
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
        finally:
            with self._lock:
                self._draining = False

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

        Emits ONE ``chunk_flush_complete`` structlog event per completed
        flush (nexus-lde88 G1) with complete per-flush attribution —
        ``begin_hook_s`` (``on_batch_begin``, the pre-upload RUNFENCE
        stamp), ``upload_s`` (the network write alone), ``flush_hook_s``
        (``on_batch_complete`` — taxonomy/manifest/aspect), ``settle_s``
        (the file-settlement bookkeeping block — review round 2), and
        ``file_hook_s`` (the per-file completion callbacks). These five
        SUM to the full flush wall — no attribution gap between them (the
        round-2 fix: settle_s used to be silently excluded from all four
        AND from ``flush_seconds``, see ``test_partition_sums_to_wall``;
        nexus-jb4pp closed the identical gap for begin_hook_s, which
        vw594 F1 introduced by inserting a round trip ahead of ``t0``).
        No event fires on the bisect path (a bisected batch never
        completes as itself; each half fires its own event when IT
        completes).
        """
        import time  # noqa: PLC0415 — leaf util; keep module import surface minimal

        # nexus-vw594 F1: compute the SAME (path, context) shape on_batch_complete
        # uses below, but BEFORE the flush — self._files entries for these
        # paths are stable for the lifetime of this call (add() forbids
        # re-staging an unsettled file), so this read is safe to reuse
        # after the flush too instead of re-acquiring the lock.
        with self._lock:
            file_contexts = [
                (path, self._files[path].context)
                for path in pend.file_counts
                if path in self._files
            ]
        # nexus-jb4pp: TIMED. This callback makes a real round trip
        # (_fence_begin_many) and used to sit outside every timer — see
        # _begin_hook_seconds. Measured before ``t0`` so it is not
        # mis-billed as upload.
        begin_hook_elapsed = 0.0
        if file_contexts:
            _begin_t0 = time.monotonic()
            try:
                self._on_batch_begin(collection, file_contexts)
            except Exception:  # noqa: BLE001 — advisory: the fence begin stamp must never block the upload
                _log.warning(
                    "chunk_batch_begin_callback_failed",
                    collection=collection,
                    chunks=len(pend.ids),
                    exc_info=True,
                )
            begin_hook_elapsed = time.monotonic() - _begin_t0

        error: str | None = None
        t0 = time.monotonic()
        try:
            self._flush(
                collection, pend.ids, pend.documents, pend.metadatas,
                file_contexts,
            )
        except Exception as exc:  # noqa: BLE001 — attribution boundary: convert to per-file failure or bisect
            if len(pend.file_counts) >= 2:
                _log.warning(
                    "chunk_batch_flush_bisect",
                    collection=collection,
                    chunks=len(pend.ids),
                    files=len(pend.file_counts),
                    error=str(exc),
                )
                upload_elapsed = time.monotonic() - t0
                with self._lock:
                    self._flush_count += 1
                    self._upload_seconds += upload_elapsed
                    self._begin_hook_seconds += begin_hook_elapsed
                    self._flush_seconds += upload_elapsed + begin_hook_elapsed
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
        # nexus-lde88 G3: upload_elapsed is the network write ALONE, stopping
        # here — BEFORE the hooks below. This used to be the only number
        # tracked (as "flush_seconds"), under-reporting true flush cost by
        # ~4x. flush_hook_elapsed / file_hook_elapsed close that gap.
        upload_elapsed = time.monotonic() - t0

        flush_hook_elapsed = 0.0
        if error is None:
            _hook_t0 = time.monotonic()
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
            flush_hook_elapsed = time.monotonic() - _hook_t0

        # nexus-lde88 review round 2 (both reviewers; critic-proven: a 0.03s
        # delay injected into this block showed up in NEITHER upload_s,
        # flush_hook_s, NOR file_hook_s — the reported sum read 0.0000s
        # against a real wall of 0.0401s). Timed on its own now so
        # flush_seconds (below) is the genuinely TRUE wall, not a sum with
        # a silent gap in it.
        _settle_t0 = time.monotonic()
        settled: list[_Settled] = []
        with self._lock:
            self._flush_count += 1
            _flush_num = self._flush_count
            _draining = self._draining
            for path, count in pend.file_counts.items():
                state = self._files[path]
                state.outstanding -= count
                if error is not None and state.failed is None:
                    state.failed = error
                self._settle_file_locked(path, settled)
        settle_elapsed = time.monotonic() - _settle_t0

        _file_hook_t0 = time.monotonic()
        self._invoke_callbacks(settled)
        file_hook_elapsed = time.monotonic() - _file_hook_t0

        with self._lock:
            self._upload_seconds += upload_elapsed
            self._settle_seconds += settle_elapsed
            self._begin_hook_seconds += begin_hook_elapsed
            self._flush_seconds += (
                begin_hook_elapsed + upload_elapsed + flush_hook_elapsed
                + settle_elapsed + file_hook_elapsed
            )

        _log.info(
            "chunk_flush_complete",
            collection=collection,
            chunks=len(pend.ids),
            files=len(pend.file_counts),
            begin_hook_s=round(begin_hook_elapsed, 3),
            upload_s=round(upload_elapsed, 3),
            flush_hook_s=round(flush_hook_elapsed, 3),
            settle_s=round(settle_elapsed, 3),
            file_hook_s=round(file_hook_elapsed, 3),
        )

        # nexus-rhwg5: mirrors the log event above exactly (same settled-
        # flush contract, never the bisect parent) but fires only for
        # mid-loop (non-drain) flushes -- see the constructor docstring.
        if self._on_flush is not None and not _draining:
            _total_elapsed = (
                begin_hook_elapsed + upload_elapsed + flush_hook_elapsed
                + settle_elapsed + file_hook_elapsed
            )
            try:
                self._on_flush(
                    _flush_num, len(pend.ids), collection, _total_elapsed, error,
                )
            except Exception:  # noqa: BLE001 — progress reporting is advisory; must never fail the flush
                _log.warning(
                    "chunk_flush_on_flush_callback_failed",
                    collection=collection,
                    chunks=len(pend.ids),
                    exc_info=True,
                )

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
