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
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INTEGER NOT NULL DEFAULT 0,
    enqueued_at     TEXT NOT NULL,
    last_attempt_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (collection, source_path)
  );

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
    pass ``content=""`` because chunk-level scope only — those rows
    rely on the worker re-reading ``source_path`` from disk at
    extraction time. The worker prefers ``content`` over file read
    when non-empty.
    """

    collection: str
    source_path: str
    content_hash: str
    content: str
    retry_count: int


# ── AspectExtractionQueue ───────────────────────────────────────────────────


class AspectExtractionQueue:
    """Owns the ``aspect_extraction_queue`` table.

    See module docstring for state transitions and locking contract.
    """

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
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
            self.conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def enqueue(
        self,
        collection: str,
        source_path: str,
        content_hash: str = "",
        content: str = "",
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

        Empty ``collection`` or ``source_path`` raises ``ValueError`` —
        these are caller-side bugs that should fail fast.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        if not source_path:
            raise ValueError("source_path must not be empty")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO aspect_extraction_queue "
                "(collection, source_path, content_hash, content, status, "
                " retry_count, enqueued_at, last_attempt_at, last_error) "
                "VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, NULL)",
                (collection, source_path, content_hash, content, now),
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
        for _ in range(_MAX_CAS_RETRIES):
            with self._lock:
                row = self.conn.execute(
                    "SELECT collection, source_path, content_hash, content, "
                    "       retry_count "
                    "FROM aspect_extraction_queue "
                    "WHERE status = 'pending' "
                    "ORDER BY enqueued_at ASC, source_path ASC "
                    "LIMIT 1"
                ).fetchone()
                if row is None:
                    return None
                collection, source_path, content_hash, content, retry_count = row
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
                    )
                # rowcount == 0 → another process / thread won the
                # CAS; loop and try a different row.
        return None

    def mark_done(self, collection: str, source_path: str) -> int:
        """DELETE the row at ``(collection, source_path)`` — success path.

        Returns deleted row count (0 when the row was already gone —
        idempotent under concurrent worker invocations).
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM aspect_extraction_queue "
                "WHERE collection = ? AND source_path = ?",
                (collection, source_path),
            )
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

    def reclaim_stale(self, timeout_seconds: int = 300) -> int:
        """Reset ``in_progress`` rows whose ``last_attempt_at`` is older
        than the timeout back to ``pending``.

        Handles worker process death: the previous worker claimed a row
        but died before completing extraction. Without reclamation the
        row is stuck in ``in_progress`` forever. Default 5-minute
        timeout matches a generous extraction wall-clock.

        Returns the number of rows reclaimed.
        """
        cutoff_clause = f"datetime('now', '-{int(timeout_seconds)} seconds')"
        with self._lock:
            cur = self.conn.execute(
                "UPDATE aspect_extraction_queue "
                "SET status = 'pending', last_attempt_at = NULL "
                "WHERE status = 'in_progress' "
                f"  AND last_attempt_at < {cutoff_clause}",
            )
            self.conn.commit()
            return cur.rowcount

    def pending_count(self) -> int:
        """Return the number of rows currently in ``pending`` status."""
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM aspect_extraction_queue "
                "WHERE status = 'pending'"
            ).fetchone()
        return row[0] if row else 0

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
