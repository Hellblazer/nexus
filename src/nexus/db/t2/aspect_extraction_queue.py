# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""AspectExtractionQueue — durable WAL buffer feeding the async aspect
worker (RDR-089 follow-up nexus-qeo8).

P1.3 spike on ``knowledge__delos`` invalidated Critical Assumption #2
(per-document extraction <3 s) — measured median 26.5 s / p95 38.1 s.
The synchronous-inline shape from Phase 2 is therefore off the table
and replaced by an async pattern (RDR-048 ``pipeline_buffer`` reuse):

  hook fires → enqueue (microseconds) → ingest path returns
  worker thread polls queue → calls extract_aspects → upserts
    document_aspects → deletes queue row

The queue is durable: rows survive process restarts. A worker that
dies mid-extraction leaves its row in ``in_progress`` state; the next
worker run reclaims it via ``reclaim_stale`` after a timeout.

Schema (locked):

  CREATE TABLE aspect_extraction_queue (
    collection      TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    content_hash    TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    enqueued_at     TEXT NOT NULL,
    last_attempt_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (collection, source_path)
  );

The ``content`` column carries the full document body (or its
fallback path) on the MCP single-doc path where the ingest pathway
does not have on-disk content for the worker to re-fetch. CLI batch
ingest paths leave it empty; the worker re-reads from disk via
``source_path``.

State transitions:

  * INSERT (enqueue) → ``pending``
  * claim_next       → ``in_progress`` (sets last_attempt_at)
  * mark_done        → DELETE (success path)
  * mark_failed      → ``failed`` (terminal until re-enqueued)
  * mark_retry       → ``pending`` (next claim picks it up)
  * reclaim_stale    → ``pending`` for in_progress > timeout

PRIMARY KEY ``(collection, source_path)`` mirrors
``document_aspects``. Re-enqueue at the same key replaces the row in
place and resets ``status='pending'`` / ``retry_count=0`` (use case:
new content_hash from a re-indexed file, OR a re-extraction sweep
running after an extractor recipe upgrade).

Lock convention (mirrors ``ChashIndex``, ``DocumentAspects``):
  * Public methods acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``.

Schema duplicated here as ``CREATE IF NOT EXISTS`` so a fresh
construction creates the table even before ``apply_pending`` runs.
Identical shape to the migration — idempotent across construction +
migration.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

_log = structlog.get_logger()


# ── Schema SQL ──────────────────────────────────────────────────────────────

_ASPECT_QUEUE_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS aspect_extraction_queue (
    collection      TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    doc_id          TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL DEFAULT '',
    content         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    enqueued_at     TEXT NOT NULL,
    last_attempt_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (collection, source_path)
);

CREATE INDEX IF NOT EXISTS idx_aspect_queue_status
    ON aspect_extraction_queue(status);
"""


# Bounded retry budget for the SELECT-then-CAS-UPDATE loop in
# ``claim_next``. Larger than the practical concurrent-worker count
# in real deployments (one MCP server + one CLI process = 2 racers,
# 4 covers the test-suite ThreadPoolExecutor as well). On the rare
# extreme contention case where every retry races, ``None`` is
# returned and the worker's next poll loop tick re-tries naturally.
_MAX_CAS_RETRIES = 8


# ── Row dataclass ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueueRow:
    """A claimed queue row passed to the worker. Frozen because the
    worker holds it across the extract → upsert → mark_done sequence
    without mutation.

    ``content`` is the document text captured at enqueue time. The
    MCP ``store_put`` path passes ``content=<full text>`` (the only
    moment the text is in scope before T3 commits); CLI ingest paths
    pass ``content=""`` because chunk-level scope only; those rows
    rely on the worker re-reading ``source_path`` from disk at
    extraction time. The worker prefers ``content`` over file read
    when non-empty.

    ``doc_id`` (nexus-tdgc / RDR-101 Phase 4) is the catalog identity
    of the source document. Captured at enqueue time so the worker
    can build a ``doc_id_lookup`` for the chroma reader without a
    second catalog round-trip. Empty string for legacy rows
    enqueued before the column was added; the worker treats empty
    ``doc_id`` as "fall back to source_path".
    """

    collection: str
    source_path: str
    content_hash: str
    content: str
    retry_count: int
    doc_id: str = ""


# ── AspectExtractionQueue ───────────────────────────────────────────────────


class AspectExtractionQueue:
    """Owns the ``aspect_extraction_queue`` table.

    See module docstring for state transitions and locking contract.

    Locking hierarchy (RDR-138 T1.1, nexus-tgzvt):

    ``rename_lock`` (process-wide ``threading.RLock`` owned by
    ``T2Database``) is the OUTERMOST lock — it must be acquired BEFORE any
    ``self._lock`` region, NEVER while ``self._lock`` is already held.
    Ordering: ``rename_lock`` -> ``self._lock``.

    ``rename_lock`` is an ``RLock`` (not a plain ``Lock``) because
    ``claim_batch`` calls ``claim_next`` in a loop. When T1.2 wraps both
    with ``rename_lock``, a non-reentrant lock would self-deadlock on the
    inner ``claim_next`` call. The ``RLock`` allows the same thread to
    acquire it again safely. An alternative is an unlocked
    ``_claim_next_locked()`` helper (callable while lock held), but
    ``RLock`` is the simpler shape here given the bounded call depth.
    """

    def __init__(
        self,
        path: Path,
        *,
        rename_lock: "threading.RLock | None" = None,
    ) -> None:
        self._lock = threading.Lock()
        # RDR-138 T1.1 (nexus-tgzvt): process-wide outermost lock shared
        # with T2Database and the rename_collection_cascade path. Injected
        # by T2Database at construction so all three paths share the SAME
        # lock instance. Falls back to a new RLock when constructed
        # stand-alone (tests / direct construction outside T2Database).
        # T1.2 will wrap every queue mutator body with this lock.
        self.rename_lock: threading.RLock = (
            rename_lock if rename_lock is not None else threading.RLock()
        )
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        # nexus-v4m7y: bumped from 5s to 30s. Nine T2 stores hold their own
        # sqlite3.Connection against memory.db. Under WAL mode, only one
        # writer is allowed across all connections; busy_timeout governs
        # how long this connection waits before raising ERROR_BUSY. 5s was
        # tight enough that a long memory_put (or any other store's write)
        # could push reclaim_stale past the limit and surface "database is
        # locked" warnings. 30s costs nothing on quiet systems and absorbs
        # the realistic intra-daemon contention window.
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()

    def close(self) -> None:
        """Close the dedicated connection (idempotent under ``self._lock``)."""
        with self._lock:
            self.conn.close()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.executescript(_ASPECT_QUEUE_SCHEMA_SQL)
            # nexus-tdgc: in-place migration for tables created before
            # the doc_id column was added. ADD COLUMN with a NOT NULL
            # DEFAULT '' fills legacy rows with the empty string; the
            # worker treats empty doc_id as "fall back to source_path".
            # The PRAGMA-then-ALTER pattern races under concurrent
            # T2Database construction: thread A reads cols (no doc_id),
            # thread B reads cols (no doc_id), A commits ALTER, B's
            # ALTER raises "duplicate column name". Catching the
            # specific error keeps construction idempotent across
            # threads. Same SQLite catch pattern used in nexus.db.migrations.
            cols = {
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(aspect_extraction_queue)"
                ).fetchall()
            }
            if "doc_id" not in cols:
                try:
                    self.conn.execute(
                        "ALTER TABLE aspect_extraction_queue "
                        "ADD COLUMN doc_id TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    # Another constructor raced ahead of us and added
                    # the column first; verify it really exists before
                    # swallowing the error so a genuinely broken ALTER
                    # still surfaces.
                    if "duplicate column" not in str(exc).lower():
                        raise
                    cols_after = {
                        r[1] for r in self.conn.execute(
                            "PRAGMA table_info(aspect_extraction_queue)"
                        ).fetchall()
                    }
                    if "doc_id" not in cols_after:
                        raise
            # Same in-place migration shape for the ``content`` column
            # added after the original schema. INSERT statements include
            # ``content`` so legacy DBs that only have CREATE TABLE
            # IF NOT EXISTS without the migration would raise
            # ``no such column: content`` at runtime.
            cols = {
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(aspect_extraction_queue)"
                ).fetchall()
            }
            if "content" not in cols:
                try:
                    self.conn.execute(
                        "ALTER TABLE aspect_extraction_queue "
                        "ADD COLUMN content TEXT NOT NULL DEFAULT ''"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                    cols_after = {
                        r[1] for r in self.conn.execute(
                            "PRAGMA table_info(aspect_extraction_queue)"
                        ).fetchall()
                    }
                    if "content" not in cols_after:
                        raise
            self.conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def enqueue(
        self,
        collection: str,
        source_path: str,
        content_hash: str = "",
        content: str = "",
        *,
        doc_id: str = "",
    ) -> None:
        """Persist a new pending row for ``(collection, source_path)``.

        ``INSERT OR REPLACE`` semantics: re-enqueue at the same key
        resets ``status='pending'`` and ``retry_count=0``. Used for both
        first-time enqueue (from ``aspect_extraction_enqueue_hook``) and
        re-extraction triggers (``nx enrich aspects --re-extract``).

        ``content`` is the document text captured at enqueue time. The
        MCP path passes the full text (only moment it is in scope);
        CLI paths pass ``""`` and the worker re-reads ``source_path``
        from disk. Both shapes are valid.

        ``doc_id`` (nexus-tdgc) is the catalog identity. Caller passes
        ``ctx.doc_id_resolver(path)`` (or equivalent) when known; empty
        string is acceptable and routes the worker through the legacy
        source_path path. Stored on the row so the worker can build a
        chroma reader ``doc_id_lookup`` without a catalog round-trip.

        Empty ``collection`` or ``source_path`` raises ``ValueError``;
        these are caller-side bugs that should fail fast.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not source_path:
            raise ValueError("source_path must not be empty")
        now = datetime.now(UTC).isoformat()
        with self.rename_lock:
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO aspect_extraction_queue "
                    "(collection, source_path, doc_id, content_hash, content, "
                    " status, retry_count, enqueued_at, last_attempt_at, last_error) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL)",
                    (collection, source_path, doc_id, content_hash, content, now),
                )
                self.conn.commit()

    def claim_next(self) -> QueueRow | None:
        """Atomically claim the oldest pending row.

        Returns the row as a ``QueueRow`` after flipping its status to
        ``in_progress`` and setting ``last_attempt_at``. Returns ``None``
        when no pending row exists.

        Cross-process atomicity (compare-and-swap pattern): the UPDATE
        WHERE clause includes ``status = 'pending'``. Two processes
        racing the same row will both see it in the SELECT but only
        one UPDATE matches a row whose status is still ``'pending'`` —
        the other UPDATE matches zero rows (because the first commit
        already flipped status to ``'in_progress'``). When this
        process loses the race, ``cursor.rowcount == 0``, we re-run
        the SELECT to find the next pending row. Bounded retry: at
        most ``_MAX_CAS_RETRIES`` iterations before giving up and
        returning ``None`` (queue depth has been racing-claimed by
        peers; the caller's next poll will see fresh work).

        The Python ``threading.Lock`` still serialises within-process
        callers; the SQL CAS adds the across-process guarantee that
        WAL row-locking alone does not provide for SELECT-then-UPDATE
        sequences across separate connections.
        """
        now = datetime.now(UTC).isoformat()
        with self.rename_lock:
            for _ in range(_MAX_CAS_RETRIES):
                with self._lock:
                    row = self.conn.execute(
                        "SELECT collection, source_path, content_hash, content, "
                        "       retry_count, doc_id "
                        "FROM aspect_extraction_queue "
                        "WHERE status = 'pending' "
                        "ORDER BY enqueued_at ASC, source_path ASC "
                        "LIMIT 1"
                    ).fetchone()
                    if row is None:
                        return None
                    collection, source_path, content_hash, content, retry_count, doc_id = row
                    cur = self.conn.execute(
                        "UPDATE aspect_extraction_queue "
                        "SET status = 'in_progress', last_attempt_at = ? "
                        "WHERE collection = ? "
                        "  AND source_path = ? "
                        "  AND status = 'pending'",
                        (now, collection, source_path),
                    )
                    self.conn.commit()
                    if cur.rowcount == 1:
                        return QueueRow(
                            collection=collection,
                            source_path=source_path,
                            content_hash=content_hash,
                            content=content,
                            retry_count=retry_count,
                            doc_id=doc_id or "",
                        )
                    # rowcount == 0 → another process / thread won the
                    # CAS; loop and try a different row.
            return None

    def claim_batch(self, limit: int) -> list[QueueRow]:
        """Claim up to ``limit`` pending rows in FIFO order.

        Each individual claim runs the same atomic CAS as
        ``claim_next``; this method is a bounded loop that stops when
        the queue runs dry or when ``limit`` rows are claimed. Returns
        the rows in the order they were claimed (FIFO).

        Used by the async worker to amortise Claude CLI cost via the
        batch extraction path (RDR-089 Phase D). The worker's
        ``batch_size`` knob caps the per-iteration claim size.
        """
        if limit <= 0:
            return []
        out: list[QueueRow] = []
        with self.rename_lock:
            for _ in range(limit):
                row = self.claim_next()
                if row is None:
                    break
                out.append(row)
        return out

    def mark_done(
        self,
        collection: str = "",
        source_path: str = "",
        *,
        doc_id: str = "",
    ) -> int:
        """DELETE the row at ``(doc_id)`` or ``(collection, source_path)`` — success path.

        After the RDR-108 Phase 1c PK migration the table uses ``doc_id``
        as primary key; pass ``doc_id=<tumbler>`` to delete by the new key.
        The legacy ``(collection, source_path)`` pair is retained as a
        backward-compatible shim for callers that have not yet been updated;
        it deletes matching rows via the denorm cache columns.

        Returns deleted row count (0 when the row was already gone —
        idempotent under concurrent worker invocations).
        """
        with self.rename_lock:
            with self._lock:
                if doc_id:
                    cur = self.conn.execute(
                        "DELETE FROM aspect_extraction_queue WHERE doc_id = ?",
                        (doc_id,),
                    )
                elif collection or source_path:
                    cur = self.conn.execute(
                        "DELETE FROM aspect_extraction_queue "
                        "WHERE collection = ? AND source_path = ?",
                        (collection, source_path),
                    )
                else:
                    return 0
                self.conn.commit()
                return cur.rowcount

    def mark_failed(
        self,
        collection: str,
        source_path: str,
        error: str,
    ) -> None:
        """Mark the row as ``failed`` (terminal until re-enqueued).

        Increments ``retry_count``, records the error text (truncated
        to 2000 chars to match ``hook_failures``), and does NOT delete
        the row — re-extraction triggers can find failed rows and
        re-enqueue them.
        """
        with self.rename_lock:
            with self._lock:
                self.conn.execute(
                    "UPDATE aspect_extraction_queue "
                    "SET status = 'failed', "
                    "    retry_count = retry_count + 1, "
                    "    last_error = ? "
                    "WHERE collection = ? AND source_path = ?",
                    (error[:2000], collection, source_path),
                )
                self.conn.commit()

    def mark_retry(self, collection: str, source_path: str) -> None:
        """Reset the row to ``pending`` and increment ``retry_count``.

        Used by the worker when a single attempt failed transiently
        and the retry budget has not been exhausted. The next
        ``claim_next`` call will pick it up.
        """
        with self.rename_lock:
            with self._lock:
                self.conn.execute(
                    "UPDATE aspect_extraction_queue "
                    "SET status = 'pending', "
                    "    retry_count = retry_count + 1, "
                    "    last_attempt_at = NULL "
                    "WHERE collection = ? AND source_path = ?",
                    (collection, source_path),
                )
                self.conn.commit()

    # nexus-v4m7y: retry-with-backoff for reclaim_stale. Three attempts
    # with two inter-attempt sleeps (0.1s, 0.5s) on top of the 30s
    # per-attempt SQLite busy_timeout. Absorbs transient writer-slot
    # contention that would otherwise leak as ERROR_BUSY.
    # Total worst-case wait if every attempt hits a locked writer:
    # 30 + 0.1 + 30 + 0.5 + 30 = ~90.6 s. Acceptable for a background
    # reclaim that runs every N polls. The final attempt still raises
    # if it cannot acquire, so the existing aspect_worker_claim_failed
    # warning surfaces in the truly-stuck case.
    _RECLAIM_RETRY_SLEEPS_BETWEEN: tuple[float, ...] = (0.1, 0.5)

    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        """Reset ``in_progress`` rows whose ``last_attempt_at`` is older
        than the timeout back to ``pending``.

        Handles worker process death: the previous worker claimed a row
        but died before completing extraction. Without reclamation the
        row is stuck in ``in_progress`` forever. Default 5-minute
        timeout matches a generous extraction wall-clock.

        Returns the number of rows reclaimed.

        Retries up to 3 attempts on ``database is locked`` to absorb
        transient WAL writer-slot contention with other T2 stores in
        the same daemon process. See ``_RECLAIM_RETRY_BACKOFF_SECONDS``.

        ``last_attempt_at`` is wrapped in ``datetime()`` to normalize
        the comparison: production writes ISO 8601 with ``T`` separator
        and ``+00:00`` suffix (``datetime.now(UTC).isoformat()``), while
        SQLite's ``datetime('now', ...)`` returns space-separated, no-tz
        format. Without normalization, lexicographic compare fails
        (``'T' (0x54) > ' ' (0x20)``) and reclaim matches zero rows
        regardless of staleness.
        """
        cutoff_clause = f"datetime('now', '-{int(timeout_seconds)} seconds')"
        sleeps_between = self._RECLAIM_RETRY_SLEEPS_BETWEEN
        max_attempts = len(sleeps_between) + 1
        for attempt in range(1, max_attempts + 1):
            try:
                with self.rename_lock:
                    with self._lock:
                        cur = self.conn.execute(
                            "UPDATE aspect_extraction_queue "
                            "SET status = 'pending', last_attempt_at = NULL "
                            "WHERE status = 'in_progress' "
                            f"  AND datetime(last_attempt_at) < {cutoff_clause}",
                        )
                        self.conn.commit()
                        if attempt > 1:
                            _log.debug(
                                "aspect_queue_reclaim_stale_recovered",
                                attempt=attempt,
                                rowcount=cur.rowcount,
                            )
                        return cur.rowcount
            except sqlite3.OperationalError as exc:
                # Only retry on writer-slot contention. Anything else
                # (schema corruption, FK violation, etc.) propagates
                # immediately.
                if "locked" not in str(exc).lower():
                    raise
                if attempt == max_attempts:
                    raise
                sleep_seconds = sleeps_between[attempt - 1]
                _log.debug(
                    "aspect_queue_reclaim_stale_retry",
                    attempt=attempt,
                    next_sleep_seconds=sleep_seconds,
                    exc=str(exc),
                )
                time.sleep(sleep_seconds)
        raise RuntimeError(  # pragma: no cover
            "reclaim_stale exhausted retries unexpectedly"
        )

    def pending_count(self) -> int:
        """Return the number of rows currently in ``pending`` status."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue "
                "WHERE status = 'pending'"
            ).fetchone()
        return row[0] if row else 0

    def is_drained(self) -> bool:
        """Return True iff no actionable rows remain in the queue.

        A queue is considered drained when the count of rows with
        ``status != 'failed'`` is zero.  Failed rows are terminal and
        do not block a PK migration — they stay in the table as audit
        records and must be explicitly re-enqueued to be retried.

        Used by ``drain_worker`` (RDR-108 Phase 1 S1) and by
        ``nexus-je0b`` as a precondition guard before the PK swap.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue "
                "WHERE status != 'failed'"
            ).fetchone()
        return (row[0] if row else 0) == 0

    def list_pending(self, limit: int | None = None) -> list[QueueRow]:
        """Return pending rows in claim order (FIFO by ``enqueued_at``)."""
        sql = (
            "SELECT collection, source_path, content_hash, content, retry_count "
            "FROM aspect_extraction_queue "
            "WHERE status = 'pending' "
            "ORDER BY enqueued_at ASC, source_path ASC"
        )
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [
            QueueRow(
                collection=c, source_path=sp, content_hash=ch,
                content=co, retry_count=rc,
            )
            for c, sp, ch, co, rc in rows
        ]

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's denorm ``collection`` cache from ``old`` to ``new``.

        nexus-gp20 / RDR-108 Phase 1d: ``collection`` is a denorm cache.
        On migrated tables (PK=doc_id, post-RDR-108 Phase 1c), this
        updates only the denorm cache; PK is unaffected. On legacy tables
        (PK=(collection, source_path)), this also re-keys the PK, which
        is safe because source_path values are unique within a collection.

        Collision defense (nexus-nhyh / K4): on legacy-PK tables, a
        pre-existing ``(new, source_path)`` row would cause a UNIQUE
        constraint violation on UPDATE. Mirror chash_index's strategy:
        DELETE conflicting new-side rows first, then UPDATE. The rename
        is an atomic re-home, so dropping a stale new-side row is safe.

        Returns row count updated (0 when no rows match -- safe no-op).
        Idempotent: a second call with the same ``old`` name returns 0
        without error.
        """
        with self._lock:
            # Drop any pre-existing new-collection rows that would collide
            # with the rename (same source_path). Mirrors ChashIndex pattern.
            self.conn.execute(
                "DELETE FROM aspect_extraction_queue "
                "WHERE collection = ? "
                "  AND source_path IN ("
                "    SELECT source_path FROM aspect_extraction_queue"
                "    WHERE collection = ?"
                "  )",
                (new, old),
            )
            cur = self.conn.execute(
                "UPDATE aspect_extraction_queue "
                "SET collection = ? WHERE collection = ?",
                (new, old),
            )
            self.conn.commit()
            return cur.rowcount
