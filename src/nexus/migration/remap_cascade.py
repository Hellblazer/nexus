# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P2.3: the in-DB remap cascade — apply the chash_remap map across
every LOCAL store the P2.0 audit enumerated.

This is the genuine SUBSET RDR-180 will reuse (in-DB rewrite via a
persisted map); the 32-byte binary value type and the ``chash_alias``
permanent table for out-of-DB refs are NOT built here.

WHY LOCAL-ONLY: PG's chunk tables, manifest, and chash_index carry
``length(chash)=32`` CHECKs (catalog-002/013) — legacy ids structurally
cannot exist there. The legacy population lives in the LOCAL SQLite
stores (catalog.db manifest + memory.db domain tables), and the ladder
orders this cascade BEFORE the T2→PG ETLs so PG only ever receives
conformant rows (the .13 inventory §2 ordering; the ETL-layer hazard).

Store semantics (the .13 inventory, r3):

- Class B1 (chash NOT in PK) — plain UPDATE: ``document_chunks`` manifest
  (positions preserved; the same new chash legitimately lands at multiple
  rows), ``relevance_log``.
- Class B2 (chash IN the PK) — two-phase dedupe-then-rewrite, the
  catalog-013-0 pattern (a blind UPDATE PK-collides under RDR-108
  identical-text collapse): ``chash_index``, ``topic_assignments``, and
  ``frecency`` (whose collision-merge keeps GREATEST values, the
  telemetry_etl reimport convention).
- Class D (conditional; note-backed rows) — ``document_aspects`` /
  ``aspect_extraction_queue``: rewrite chash-valued ``source_path`` and
  the embedded ``chroma://<coll>/<chash>`` URI.

Unscoped stores (``topic_assignments`` has no collection column) are
driven by the GLOBAL map view; a same-old-id→different-new ambiguity
across source collections raises :class:`AmbiguousRemapError` — loud,
never a guess. Idempotent by construction: rewritten old ids no longer
match, so a re-run is a universal no-op.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from nexus.migration.wire_reid import ChashRemapStore

_log = structlog.get_logger(__name__)

#: The audited cascade set (inventory r3 Classes B+D). The completeness
#: tripwire: every entry MUST have an implementation in ``_STORE_FNS`` —
#: ``cascade_remap`` refuses to run otherwise — and the test suite pins
#: this tuple against the audit's literal store list.
CASCADE_STORES: tuple[str, ...] = (
    "document_chunks",
    "chash_index",
    "topic_assignments",
    "frecency",
    "relevance_log",
    "document_aspects",
    "aspect_extraction_queue",
)


class AmbiguousRemapError(RuntimeError):
    """One old id maps to different new chashes in different source
    collections — unscoped stores cannot disambiguate; the operator must."""


@dataclass(frozen=True)
class StoreCascadeResult:
    store: str
    ok: bool
    rewritten: int = 0
    deduped: int = 0
    reason: str = ""


def _global_view(map_store: ChashRemapStore) -> dict[str, str]:
    """Collapse the per-collection map to old_id → new_chash, failing loud
    on cross-collection ambiguity."""
    view: dict[str, str] = {}
    conflicts: list[str] = []
    for old_id, new_chash in map_store.all_pairs():
        seen = view.get(old_id)
        if seen is None:
            view[old_id] = new_chash
        elif seen != new_chash:
            conflicts.append(old_id)
    if conflicts:
        raise AmbiguousRemapError(
            "old id(s) map to different new chashes across source collections "
            f"(unscoped stores cannot disambiguate): {sorted(set(conflicts))!r}"
        )
    return view


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), isolation_level=None)  # epsilon-allow: remap cascade is migration machinery over the LOCAL catalog.db/memory.db BEFORE any daemon serves them mid-migration (RDR-185 P2.3; same class as the migration source reads in storage_cmd/orchestrator)


def _rewrite_plain(
    conn: sqlite3.Connection, table: str, column: str, view: dict[str, str]
) -> tuple[int, int]:
    rewritten = 0
    for old_id, new_chash in view.items():
        cur = conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # noqa: S608 — table/column are module-internal literals
            (new_chash, old_id),
        )
        rewritten += cur.rowcount
    return rewritten, 0


def _rewrite_two_phase(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    view: dict[str, str],
    *,
    scope_column: str | None,
    merge_sql: str | None = None,
) -> tuple[int, int]:
    """catalog-013-0's dedupe-then-rewrite, per (old→new) pair sequentially:
    when the new id already occupies the PK slot (earlier collapse sibling
    or pre-existing conformant row), optionally MERGE into it, DELETE the
    old row, else UPDATE in place."""
    rewritten = 0
    deduped = 0
    scope_match = (
        f"AND t2.{scope_column} = {table}.{scope_column}" if scope_column else ""
    )
    for old_id, new_chash in view.items():
        if merge_sql is not None:
            conn.execute(merge_sql, {"old": old_id, "new": new_chash})
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {column} = ? AND EXISTS ("  # noqa: S608 — internal literals
            f"  SELECT 1 FROM {table} t2 WHERE t2.{column} = ? {scope_match})",
            (old_id, new_chash),
        )
        deduped += cur.rowcount
        cur = conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE {column} = ?",  # noqa: S608 — internal literals
            (new_chash, old_id),
        )
        rewritten += cur.rowcount
    return rewritten, deduped


_FRECENCY_MERGE_SQL = """
UPDATE frecency SET
    frecency_score = MAX(frecency_score, (SELECT frecency_score FROM frecency WHERE chunk_id = :old)),
    miss_count     = MAX(miss_count,     (SELECT miss_count     FROM frecency WHERE chunk_id = :old)),
    last_hit_at    = MAX(last_hit_at,    (SELECT last_hit_at    FROM frecency WHERE chunk_id = :old)),
    embedded_at    = MAX(embedded_at,    (SELECT embedded_at    FROM frecency WHERE chunk_id = :old)),
    ttl_days       = MAX(ttl_days,       (SELECT ttl_days       FROM frecency WHERE chunk_id = :old))
WHERE chunk_id = :new
  AND EXISTS (SELECT 1 FROM frecency WHERE chunk_id = :old)
"""


def _cascade_aspect_table(
    conn: sqlite3.Connection, table: str, view: dict[str, str], *, has_uri: bool
) -> tuple[int, int]:
    """Class D: chash-valued source_path (note-backed rows) + embedded
    chroma:// URI rewrite. Two-phase on the (collection, source_path) PK."""
    rewritten = 0
    deduped = 0
    for old_id, new_chash in view.items():
        cur = conn.execute(
            f"DELETE FROM {table} WHERE source_path = ? AND EXISTS ("  # noqa: S608 — internal literals
            f"  SELECT 1 FROM {table} t2 WHERE t2.source_path = ? "
            f"  AND t2.collection = {table}.collection)",
            (old_id, new_chash),
        )
        deduped += cur.rowcount
        if has_uri:
            cur = conn.execute(
                f"UPDATE {table} SET source_path = ?, "  # noqa: S608 — internal literals
                "source_uri = CASE WHEN source_uri LIKE '%/' || ? "
                "  THEN replace(source_uri, '/' || ?, '/' || ?) ELSE source_uri END "
                "WHERE source_path = ?",
                (new_chash, old_id, old_id, new_chash, old_id),
            )
        else:
            cur = conn.execute(
                f"UPDATE {table} SET source_path = ? WHERE source_path = ?",  # noqa: S608 — internal literals
                (new_chash, old_id),
            )
        rewritten += cur.rowcount
    return rewritten, deduped


#: store name → (which db, cascade fn). The completeness tripwire pins
#: ``keys == CASCADE_STORES`` at run entry and in tests.
_STORE_FNS: dict[str, tuple[str, Callable[[sqlite3.Connection, dict[str, str]], tuple[int, int]]]] = {
    "document_chunks": ("catalog", lambda c, v: _rewrite_plain(c, "document_chunks", "chash", v)),
    "chash_index": (
        "memory",
        lambda c, v: _rewrite_two_phase(
            c, "chash_index", "chash", v, scope_column="physical_collection"
        ),
    ),
    "topic_assignments": (
        "memory",
        lambda c, v: _rewrite_two_phase(
            c, "topic_assignments", "doc_id", v, scope_column="topic_id"
        ),
    ),
    "frecency": (
        "memory",
        lambda c, v: _rewrite_two_phase(
            c, "frecency", "chunk_id", v, scope_column=None, merge_sql=_FRECENCY_MERGE_SQL
        ),
    ),
    "relevance_log": ("memory", lambda c, v: _rewrite_plain(c, "relevance_log", "chunk_id", v)),
    "document_aspects": (
        "memory",
        lambda c, v: _cascade_aspect_table(c, "document_aspects", v, has_uri=True),
    ),
    "aspect_extraction_queue": (
        "memory",
        lambda c, v: _cascade_aspect_table(c, "aspect_extraction_queue", v, has_uri=False),
    ),
}


def cascade_remap(
    map_store: ChashRemapStore,
    *,
    catalog_db: Path,
    memory_db: Path,
) -> list[StoreCascadeResult]:
    """Apply the persisted map across every audited local store.

    One transaction PER STORE (a crash resumes idempotently: rewritten
    old ids no longer match). Per-store failures are reported, not
    raised — except :class:`AmbiguousRemapError`, which aborts the run
    before any write. Every ``CASCADE_STORES`` entry yields a result row
    (the completeness tripwire).
    """
    missing = set(CASCADE_STORES).symmetric_difference(_STORE_FNS)
    if missing:
        raise RuntimeError(
            f"remap cascade inventory drift: {sorted(missing)!r} — "
            "CASCADE_STORES and _STORE_FNS must cover the same audited set"
        )
    view = _global_view(map_store)  # raises AmbiguousRemapError pre-write
    results: list[StoreCascadeResult] = []
    conns: dict[str, sqlite3.Connection] = {}
    try:
        conns["catalog"] = _connect(catalog_db)
        conns["memory"] = _connect(memory_db)
        for store in CASCADE_STORES:
            which, fn = _STORE_FNS[store]
            conn = conns[which]
            if not view:
                results.append(StoreCascadeResult(store, True))
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                rewritten, deduped = fn(conn, view)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                _log.warning(
                    "remap_cascade_store_failed", store=store, error=str(exc)
                )
                results.append(StoreCascadeResult(store, False, reason=str(exc)))
                continue
            _log.info(
                "remap_cascade_store_done",
                store=store,
                rewritten=rewritten,
                deduped=deduped,
            )
            results.append(StoreCascadeResult(store, True, rewritten, deduped))
    finally:
        for conn in conns.values():
            conn.close()
    return results
