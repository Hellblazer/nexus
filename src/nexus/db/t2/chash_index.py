# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""ChashIndex — global chunk-hash → (collection, doc_id) lookup table (RDR-086 Phase 1).

Answers "given this ``chash:<hex>`` citation, which physical collection
and doc_id hold the chunk?" in ~50 µs via a SQLite JOIN, replacing the
~13-min serial-ChromaDB-filter alternative measured in RF-6.

Every indexing write site in ``code_indexer``, ``prose_indexer``,
``doc_indexer``, and ``pipeline_stages`` dual-writes into this table
alongside its T3 ChromaDB upsert. The dual write is best-effort — T2
failure logs and does not abort the T3 write.

Compound PK ``(chash, physical_collection)`` rationale (RF-10 Issue 1):
the same chunk text (same SHA-256) can legitimately live in multiple
collections. Example: ``knowledge__delos`` and
``knowledge__delos_docling`` both ingest the same paper, so every
chunk's SHA-256 is identical. A single-column chash PK would
FK-violate on the second write.

Secondary index on ``physical_collection`` (created by the Phase 1.1
migration) supports the Phase 1.4 delete cascade — ``DELETE FROM
chash_index WHERE physical_collection = ?`` runs as an index seek,
not a table scan.

Lock convention (mirrors other T2 domain stores):
  * Public methods acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``.

The schema is duplicated here as ``CREATE IF NOT EXISTS`` so fresh
``ChashIndex`` constructions get the table even before
``apply_pending`` runs. Identical shape to the Phase 1.1 migration —
idempotent across construction + migration.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()


# ── Schema SQL ──────────────────────────────────────────────────────────────

_CHASH_INDEX_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS chash_index (
    chash                TEXT NOT NULL,
    physical_collection  TEXT NOT NULL,
    doc_id               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (chash, physical_collection)
);

CREATE INDEX IF NOT EXISTS idx_chash_index_collection
    ON chash_index(physical_collection);
"""


# ── ChashIndex ──────────────────────────────────────────────────────────────


class ChashIndex:
    """Owns the ``chash_index`` table.

    See module docstring for the locking, schema-duplication, and
    dual-write contract.
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
            self.conn.executescript(_CHASH_INDEX_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def upsert(self, *, chash: str, collection: str, doc_id: str) -> None:
        """Register ``chash`` as living in ``collection`` at ``doc_id``.

        ``INSERT OR REPLACE`` semantics: re-indexing the same file
        overwrites the existing row (updates ``doc_id`` and
        ``created_at``) rather than erroring. Compound PK
        ``(chash, collection)`` lets the same chunk text register in
        multiple collections without conflict.

        Raises ``ValueError`` if any of the three identifiers is empty —
        empty values indicate caller-side bugs and are cheaper to fail
        fast than to silently accept.
        """
        if not chash:
            raise ValueError("chash must not be empty")
        if not collection:
            raise ValueError("collection must not be empty")
        if not doc_id:
            raise ValueError("doc_id must not be empty")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO chash_index "
                "(chash, physical_collection, doc_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chash, collection, doc_id, now),
            )
            self.conn.commit()

    def lookup(self, chash: str) -> list[dict[str, Any]]:
        """Return all (collection, doc_id, created_at) rows for ``chash``.

        Phase 2's ``Catalog.resolve_chash`` tie-breaks multi-match by
        newest ``created_at``. Returns ``[]`` when ``chash`` is unknown.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT physical_collection, doc_id, created_at "
                "FROM chash_index WHERE chash = ?",
                (chash,),
            ).fetchall()
        return [
            {"collection": coll, "doc_id": did, "created_at": ts}
            for coll, did, ts in rows
        ]

    def delete_collection(self, collection: str) -> int:
        """Drop all rows for ``collection``. Returns deleted row count.

        Called by Phase 1.4's ``nx collection delete`` cascade. Uses
        the ``idx_chash_index_collection`` index (created at migration
        time) so the DELETE is an index seek, not a table scan.
        Idempotent: absent collection yields 0.
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM chash_index WHERE physical_collection = ?",
                (collection,),
            )
            self.conn.commit()
            return cur.rowcount

    def delete_stale(self, *, chash: str, collection: str) -> int:
        """Drop the single row identified by the compound PK ``(chash, collection)``.

        Used by Phase 2's self-healing read in ``Catalog.resolve_chash``:
        when a T2 row points at a collection that no longer exists in T3,
        the stale row is removed on access. Must acquire ``self._lock``
        so concurrent ``upsert`` / ``delete_collection`` calls on the
        same store don't race against the same SQLite connection.

        Returns the deleted row count (0 when the PK was already absent —
        idempotent under concurrent self-heal invocations).
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM chash_index "
                "WHERE chash = ? AND physical_collection = ?",
                (chash, collection),
            )
            self.conn.commit()
            return cur.rowcount

    def is_empty(self) -> bool:
        """True when no rows exist — the "fresh install" guard.

        Used by Phase 5's ``nx doc cite`` short-circuit so a caller
        hitting the command before any backfill gets an actionable
        error instead of a 30-second fallback timeout. Acquires
        ``self._lock`` to keep the contract that every connection
        access goes through the lock.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM chash_index LIMIT 1"
            ).fetchone()
        return row is None


# ── Dual-write helper (RDR-086 Phase 1.2) ────────────────────────────────────


def dual_write_chash_index(
    chash_index: "ChashIndex | None",
    collection: str,
    ids: list[str],
    metadatas: list[dict],
) -> None:
    """Best-effort dual-write to ``chash_index`` after a T3 upsert.

    Called at each of the six indexing write sites (code, prose, doc,
    streaming pipeline) immediately after
    ``T3Database.upsert_chunks_with_embeddings(...)``. Iterates the
    parallel ``ids`` and ``metadatas`` lists, extracts
    ``chunk_text_hash`` from each metadata dict, and registers the
    ``(chash, collection, doc_id)`` tuple in T2.

    Best-effort: every insert is wrapped in a try/except that logs at
    warning level but does NOT re-raise. A T2 failure must never abort
    a successful T3 write — the resolver at Phase 2 falls back to the
    per-collection ``resolve_span`` scan if a chash is missing from
    the index. Missing rows are a performance hit, not a correctness
    hit.

    No-op when ``chash_index is None`` (e.g. tests that don't want to
    assert on T2 state, or pre-Phase-1.2 call sites that haven't been
    plumbed yet).

    Empty ``chunk_text_hash`` metadata entries are skipped silently —
    some test-only metadata paths may legitimately omit it.
    """
    if chash_index is None:
        return
    for doc_id, meta in zip(ids, metadatas):
        chash = meta.get("chunk_text_hash", "") if isinstance(meta, dict) else ""
        if not chash or not doc_id:
            continue
        try:
            chash_index.upsert(chash=chash, collection=collection, doc_id=doc_id)
        except Exception as exc:
            _log.warning(
                "chash_index_dual_write_failed",
                collection=collection,
                doc_id=doc_id,
                chash_prefix=chash[:16],
                error=str(exc),
            )
