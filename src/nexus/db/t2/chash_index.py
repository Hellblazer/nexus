# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""ChashIndex: global chash -> physical_collection membership table (RDR-086 Phase 1).

Answers "given this ``chash:<hex>`` citation, which physical collections
hold the chunk?" in ~50 microseconds via a SQLite JOIN, replacing the
~13-min serial-ChromaDB-filter alternative measured in RF-6.

The table originally carried a third column ``chunk_chroma_id`` (the
Chroma-natural-ID for the chunk; renamed from ``doc_id`` per Phase 0
nexus-o6aa.3). RDR-108 D1 (nexus-kmb6) standardizes the chunk natural ID
on ``chunk_text_hash[:32]``, making ``chunk_chroma_id`` a pure function
of ``chash``. RDR-108 Phase 4a (nexus-mmf5) drops the column; callers
that previously read it now derive the chunk natural ID directly from
the chash via ``chash[:32]``.

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

from nexus.db.t2._tuning import SERVING_BUSY_TIMEOUT_MS

_log = structlog.get_logger()


# ── Schema SQL ──────────────────────────────────────────────────────────────

_CHASH_INDEX_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS chash_index (
    chash                TEXT NOT NULL,
    physical_collection  TEXT NOT NULL,
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
        self.conn.execute(f"PRAGMA busy_timeout={SERVING_BUSY_TIMEOUT_MS}")
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
            # RDR-108 Phase 4a (nexus-mmf5): probe-and-drop the legacy
            # ``chunk_chroma_id`` column. CREATE TABLE IF NOT EXISTS
            # above is a no-op on tables left over from
            # ``migrate_chash_index`` + ``migrate_chash_index_rename_doc_id``
            # (which produce the legacy 4-column shape with the NOT NULL
            # ``chunk_chroma_id``); the version-tracked drop migration
            # (``_drop_chash_index_chunk_chroma_id``, introduced at
            # 4.30.0) only fires once the package version crosses that
            # threshold. This in-place drop keeps writes correct in
            # mixed-state dev environments and on first open after the
            # upgrade lands.
            #
            # Concurrent ``T2Database`` constructions race here (each
            # gets its own ``ChashIndex`` connection); the
            # ``OperationalError`` catch covers the lost-race where
            # another connection dropped the column between this
            # connection's PRAGMA probe and the ALTER attempt.
            cols = {
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(chash_index)"
                ).fetchall()
            }
            if "chunk_chroma_id" in cols:
                try:
                    self.conn.execute(
                        "ALTER TABLE chash_index DROP COLUMN chunk_chroma_id"
                    )
                except sqlite3.OperationalError as exc:
                    # Narrow guard: only swallow the lost-race
                    # ("no such column") signature. Other
                    # OperationalError causes (locked DB, syntax,
                    # missing table, I/O) must propagate so a real
                    # failure is not silently masked.
                    if "no such column" not in str(exc):
                        raise
            self.conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def upsert(self, *, chash: str, collection: str) -> None:
        """Register ``chash`` as living in ``collection``.

        ``INSERT OR REPLACE`` semantics: re-indexing the same chunk
        refreshes ``created_at`` rather than erroring. Compound PK
        ``(chash, collection)`` lets the same chunk text register in
        multiple collections without conflict.

        Raises ``ValueError`` if either identifier is empty (empty
        values indicate caller-side bugs).

        RDR-108 Phase 4a (nexus-mmf5) dropped the ``chunk_chroma_id``
        column. Under D1 the chunk natural ID is ``chash[:32]`` (a pure
        function of ``chash``), so callers compute it on demand rather
        than storing a denormalized copy.
        """
        if not chash:
            raise ValueError("chash must not be empty")
        if not collection:
            raise ValueError("collection must not be empty")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO chash_index "
                "(chash, physical_collection, created_at) "
                "VALUES (?, ?, ?)",
                (chash, collection, now),
            )
            self.conn.commit()

    def upsert_many(self, *, chashes: list[str], collection: str) -> None:
        """Register many ``chashes`` in one ``collection`` in a single
        statement + commit.

        RDR-128 P1 (kg8sj): the indexer's per-chunk dual-write previously
        looped :meth:`upsert` (one statement+commit per chunk). Batching
        collapses a chunk-batch to one ``executemany`` — and, crucially,
        to one daemon RPC when routed through ``T2Client``, instead of one
        round-trip per chunk.

        ``INSERT OR REPLACE`` semantics per :meth:`upsert`. Blank/whitespace
        ``chashes`` entries are skipped (the dual-write helper already
        filters, but the batch API is defensive). Raises ``ValueError`` on
        an empty ``collection`` (a caller-side bug); an empty ``chashes``
        list is a silent no-op.

        All-or-nothing: the batch is one ``executemany`` + ``commit``, so a
        failure rolls back the whole batch (unlike the prior per-row loop,
        where one bad row was logged and the rest continued). Callers that
        need best-effort partial progress should pre-validate the batch;
        ``dual_write_chash_index`` accepts this since it is best-effort and
        wrapped at the hook level.
        """
        if not collection:
            raise ValueError("collection must not be empty")
        now = datetime.now(UTC).isoformat()
        rows = [
            (c, collection, now)
            for c in chashes
            if isinstance(c, str) and c.strip()
        ]
        if not rows:
            return
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO chash_index "
                "(chash, physical_collection, created_at) "
                "VALUES (?, ?, ?)",
                rows,
            )
            self.conn.commit()

    def lookup(self, chash: str) -> list[dict[str, Any]]:
        """Return all (collection, created_at) rows for ``chash``.

        Phase 2's ``Catalog.resolve_chash`` tie-breaks multi-match by
        newest ``created_at``. Returns ``[]`` when ``chash`` is unknown.
        Callers that need the chunk natural ID derive it as
        ``chash[:32]`` (RDR-108 D1).
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT physical_collection, created_at "
                "FROM chash_index WHERE chash = ?",
                (chash,),
            ).fetchall()
        return [
            {"collection": coll, "created_at": ts}
            for coll, ts in rows
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

    def distinct_collections(self) -> set[str]:
        """Return every distinct ``physical_collection`` value in the
        index (RDR-108 Phase 5 / nexus-w9vq).

        Used by ``nx catalog chash-reconcile`` to identify ghost
        collections (rows whose collection no longer exists in T3).
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT physical_collection FROM chash_index"
            ).fetchall()
        return {r[0] for r in rows}

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row from ``old`` → ``new``. Returns row count updated.

        nexus-1ccq: `nx collection rename` cascade. Safe because
        ``(chash, physical_collection)`` is the PK: when a chash existed
        in both ``old`` and ``new`` simultaneously (should be rare,
        but possible from interleaved indexes), the UPDATE would
        collide on the new PK. We defend by deleting any
        ``(chash, new)`` row that already exists before the UPDATE —
        rename is an atomic re-home so preserving the ``new``-side row
        would drop the rename's intended membership silently.
        """
        with self._lock:
            # Drop any pre-existing new-collection rows that would collide
            # with the rename. This is conservative — most real renames
            # have an empty destination — but guarantees the UPDATE below
            # can never raise UNIQUE.
            self.conn.execute(
                "DELETE FROM chash_index "
                "WHERE physical_collection = ? "
                "  AND chash IN (SELECT chash FROM chash_index WHERE physical_collection = ?)",
                (new, old),
            )
            cur = self.conn.execute(
                "UPDATE chash_index SET physical_collection = ? "
                "WHERE physical_collection = ?",
                (new, old),
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

    def count_for_collection(self, collection: str) -> int:
        """Return the row count for ``collection``.

        Review remediation (Reviewer B/I-1): public locked alternative to
        ``with idx._lock: idx.conn.execute("SELECT COUNT(*) …")`` so
        external callers (``collection_audit.compute_chash_coverage``)
        don't have to reach into ``_lock`` directly. Returns 0 for an
        unknown collection.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM chash_index WHERE physical_collection = ?",
                (collection,),
            ).fetchone()
        return int(row[0]) if row else 0

    def registered_chashes_for_collection(self, collection: str) -> set[str]:
        """Return the set of chash[:32] values currently registered in
        the chash_index routing table for *collection* (RDR-108 Phase 4
        / nexus-z1mu).

        Disambiguated from ``Catalog.chashes_for_collection``
        (nexus-v7mn): this reader returns the may-be-stale routing
        snapshot the chash_index dual-write hook last recorded, while
        the catalog reader returns the manifest-authoritative chash set
        derived from ``document_chunks``. The two diverge whenever
        backfill-hash, reidentify, chash-reconcile, or any other
        out-of-band mutation runs on T3 between dual-write hook calls.

        The chash_index stores the full ``chunk_text_hash`` plus its
        Chroma natural ID; under RDR-108 D1 the natural ID is
        ``chash[:32]`` so the truncated set composes directly with T3
        chunk IDs. Truncating in SQL via ``substr(chash, 1, 32)`` keeps
        the helper consistent with ``Catalog.chashes_for_collection``
        (chroma-id-shape) and tolerates any 64-char chashes that older
        indexer versions may have stored.

        Replaces ``chunk_chroma_ids_present_in_collection`` (removed in
        the same change). The audit's missing-sample probe now does a
        direct set-difference with this single set rather than a per-
        page IN-list query.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT substr(chash, 1, 32) FROM chash_index "
                "WHERE physical_collection = ?",
                (collection,),
            ).fetchall()
        return {r[0] for r in rows}


# ── Dual-write helper (RDR-086 Phase 1.2) ────────────────────────────────────


def dual_write_chash_index(
    chash_index: "ChashIndex | None",
    collection: str,
    ids: list[str],
    metadatas: list[dict],
) -> None:
    """Best-effort dual-write to ``chash_index`` after a T3 upsert.

    Called at each indexing write site immediately after
    ``T3Database.upsert_chunks_with_embeddings(...)``. Iterates
    ``metadatas``, extracts ``chunk_text_hash``, and registers
    ``(chash, collection)`` in T2.

    ``ids`` is retained on the signature for symmetry with the T3
    write call (and to permit short-circuiting an empty batch) but is
    no longer stored: under RDR-108 D1 the chunk natural ID is a pure
    function of ``chash`` (``chash[:32]``) so the prior denormalized
    ``chunk_chroma_id`` column was dropped in nexus-mmf5.

    Best-effort: every insert is wrapped in a try/except that logs at
    warning level but does NOT re-raise. A T2 failure must never abort
    a successful T3 write; the resolver at Phase 2 falls back to the
    per-collection ``resolve_span`` scan if a chash is missing from
    the index. Missing rows are a performance hit, not a correctness
    hit.

    No-op when ``chash_index`` is ``None``, ``ids`` is empty, or
    ``metadatas`` is empty. The pre-Phase-4a signature consumed both
    lists via ``zip(ids, metadatas)`` which implicitly bounded
    iteration to the shorter; the new helper iterates only
    ``metadatas`` so a length mismatch (caller bug) was previously
    diagnosed by zip truncation. The combined guard preserves that
    fail-cheap behavior. Empty ``chunk_text_hash`` metadata entries
    are skipped silently inside the loop.
    """
    if chash_index is None or not ids or not metadatas:
        return
    chashes = [
        meta.get("chunk_text_hash", "")
        for meta in metadatas
        if isinstance(meta, dict)
    ]
    chashes = [c for c in chashes if c]
    if not chashes:
        return
    # RDR-128 P1 (kg8sj): one batch call (one daemon RPC when routed via
    # T2Client) instead of a per-chunk upsert loop. Still best-effort — a
    # T2 failure logs but never aborts the successful T3 write.
    try:
        chash_index.upsert_many(chashes=chashes, collection=collection)
    except Exception as exc:
        _log.warning(
            "chash_index_dual_write_failed",
            collection=collection,
            count=len(chashes),
            error=str(exc),
        )
