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
        on_batch_complete: "Callable[[str, list[str], list[str], list[dict]], None] | None" = None,
        max_chunks: "int | Callable[[str], int]" = DEFAULT_MAX_CHUNKS,
        max_bytes: int | None = None,
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
        self._on_batch_complete = on_batch_complete or (lambda _c, _i, _d, _m: None)
        self._max_chunks = max_chunks
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._files: dict[str, _FileState] = {}
        self._failed_files: dict[str, str] = {}
        self._flush_count = 0
        self._flush_seconds = 0.0

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
            self._flush_batch(coll, batch)
        return True

    def drain(self) -> None:
        """Flush every non-empty pending buffer (end-of-run)."""
        with self._lock:
            to_flush = [
                (coll, pend) for coll, pend in self._pending.items() if pend.ids
            ]
            self._pending = {}
        for coll, batch in to_flush:
            self._flush_batch(coll, batch)

    # ── internals ────────────────────────────────────────────────────────

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
            try:
                self._on_batch_complete(
                    collection, pend.ids, pend.documents, pend.metadatas
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
