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


@dataclass(frozen=True)
class RevertReport:
    """:func:`cascade_revert`'s result — per-store outcomes PLUS the
    collapse loss, returned as DATA (reviewer-146xx-8: a WARNING log alone
    cannot reach the CLI operator deciding whether the rollback is clean).

    The CALLER (``rollback_collections``' whole-leg block) MUST refuse to
    clear the leg's map rows unless every store reverted ok — a partial
    revert with the map cleared anyway would erase the one signal that can
    ever detect the unreverted store."""

    stores: list[StoreCascadeResult]
    #: Old ids whose rows the FORWARD cascade's dedupe merged away —
    #: unrestorable (byte-identical siblings; the survivor carries the
    #: sorted-first old id). Empty on legs with no identical-text collapse.
    unrestorable: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.stores)

    def failures(self) -> list[StoreCascadeResult]:
        return [r for r in self.stores if not r.ok]


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


#: store -> (which db, table, chash-bearing column). The READ-ONLY twin of
#: :data:`_STORE_FNS`: a probe only needs to know WHERE old ids live, not the
#: two-phase / scope / merge machinery a rewrite needs. Kept beside _STORE_FNS
#: and drift-guarded against :data:`CASCADE_STORES` by the same tripwire, so a
#: store cannot be added to one and forgotten in the other — that omission
#: class is exactly what RDR-180's Failure Modes records (topic_assignments
#: missed by its original inventory) and what the RDR-185 .13 audit re-found.
_STORE_COLUMNS: dict[str, tuple[str, str, str]] = {
    "document_chunks": ("catalog", "document_chunks", "chash"),
    "chash_index": ("memory", "chash_index", "chash"),
    "topic_assignments": ("memory", "topic_assignments", "doc_id"),
    "frecency": ("memory", "frecency", "chunk_id"),
    "relevance_log": ("memory", "relevance_log", "chunk_id"),
    # Class D (see _cascade_aspect_table): the chash lives in source_path for
    # note-backed rows, NOT in a column named chash. Probing a `chash` column
    # here would be silently blind — and blind in the exact direction that
    # reports "reflected" over an orphaned cascade.
    "document_aspects": ("memory", "document_aspects", "source_path"),
    "aspect_extraction_queue": ("memory", "aspect_extraction_queue", "source_path"),
}


def unreflected_stores(
    map_store: ChashRemapStore,
    *,
    catalog_db: Path,
    memory_db: Path,
) -> list[str]:
    """Stores that STILL hold an old id the map says was re-identified.

    The read-only authority for "has the cascade actually landed?" — the
    question :func:`cascade_remap`'s own return value cannot answer for a
    LATER process (bead nexus-mapbc follow-up, P4.R2 Critical).

    The crash window it closes: ``run_substrate_migration`` writes every leg's
    target rows FIRST and cascades SECOND. A process death in between leaves
    the vector counts matching — so the next run's plan is empty, converge has
    nothing to do, and a count-only verify would record the rung COMPLETE
    FOREVER while the catalog manifest still pointed at legacy chashes, with
    doctor reporting clean. RDR-142 requires verify to re-read the WORLD; this
    is the half of the world a vector count cannot see.

    Empty map -> ``[]`` (nothing was re-identified, nothing to reflect). A
    store that cannot be read is reported as unreflected, never skipped: "I
    could not tell" is never "it is fine".
    """
    view = _global_view(map_store)  # raises AmbiguousRemapError, as the writer does
    if not view:
        return []
    drift = set(CASCADE_STORES).symmetric_difference(_STORE_COLUMNS)
    if drift:
        raise RuntimeError(
            f"remap probe inventory drift: {sorted(drift)!r} — "
            "CASCADE_STORES and _STORE_COLUMNS must cover the same audited set"
        )
    old_ids = list(view)
    unreflected: list[str] = []
    conns: dict[str, sqlite3.Connection] = {}
    try:
        conns["catalog"] = _connect(catalog_db)
        conns["memory"] = _connect(memory_db)
        for store in CASCADE_STORES:
            which, table, column = _STORE_COLUMNS[store]
            conn = conns[which]
            try:
                still = 0
                for chunk in (old_ids[i : i + 500] for i in range(0, len(old_ids), 500)):
                    marks = ",".join("?" * len(chunk))
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})",  # noqa: S608 — table/column are module-internal literals
                        chunk,
                    ).fetchone()
                    still += int(row[0]) if row else 0
                    if still:
                        break
            except sqlite3.Error as exc:
                _log.warning("remap_probe_store_failed", store=store, error=str(exc))
                unreflected.append(store)
                continue
            if still:
                unreflected.append(store)
    finally:
        for conn in conns.values():
            conn.close()
    return unreflected


def _run_stores(
    view: dict[str, str], *, catalog_db: Path, memory_db: Path, event: str
) -> list[StoreCascadeResult]:
    """The shared per-store runner: one transaction PER STORE, per-store
    failures reported not raised, every ``CASCADE_STORES`` entry yields a
    result row (the completeness tripwire). *view* maps
    from-value → to-value — the forward cascade passes old→new, the
    rollback revert passes new→old through the identical machinery."""
    missing = set(CASCADE_STORES).symmetric_difference(_STORE_FNS)
    if missing:
        raise RuntimeError(
            f"remap cascade inventory drift: {sorted(missing)!r} — "
            "CASCADE_STORES and _STORE_FNS must cover the same audited set"
        )
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
                event,
                store=store,
                rewritten=rewritten,
                deduped=deduped,
            )
            results.append(StoreCascadeResult(store, True, rewritten, deduped))
    finally:
        for conn in conns.values():
            conn.close()
    return results


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
    view = _global_view(map_store)  # raises AmbiguousRemapError pre-write
    return _run_stores(
        view, catalog_db=catalog_db, memory_db=memory_db,
        event="remap_cascade_store_done",
    )


def cascade_revert(
    leg_entries: dict[str, tuple[str, str]],
    *,
    catalog_db: Path,
    memory_db: Path,
) -> RevertReport:
    """Point the local stores BACK at one leg's old ids — the rollback's
    un-pointing half (RDR-186 D2 / nexus-146xx.8): a leg is not "rolled
    back" while local stores still reference chashes whose target rows the
    rollback just deleted.

    *leg_entries* is the leg's ``entries_with_targets`` shape
    (``old_id → (new_chash, target_collection)``), read BEFORE the caller
    clears the leg's map rows — the map is the only record of what to
    revert, which is exactly why the clear must come strictly after this.

    LOSSY-INVERSE CONTRACT (stated, never silent): the forward cascade's
    two-phase dedupe DELETED collapse-sibling rows and ``frecency`` merged
    their values with MAX — that information is gone, so an exact inverse
    does not exist. When N old ids collapsed into one new chash, the
    surviving row is restored under the deterministic sorted-first old id
    and the other N-1 are logged loudly as unrestorable
    (``remap_revert_collapse_loss``). This is a choice among byte-identical
    representations, not a semantic guess: collapse siblings carry the SAME
    chunk text, so either old id points at the content the restored source
    serves. Runs through the identical per-store machinery as the forward
    cascade — same stores, same two-phase shapes, same per-store
    transactions, inverted view.

    STRUCTURAL NO-OPS ON REVERT (reviewer-146xx-8, stated so the shared
    machinery's behavior is intentional, not incidental): after a completed
    forward cascade no row exists at any of the leg's OLD ids in these
    stores, so the two-phase DELETE-dedupe step and ``frecency``'s merge
    SQL cannot fire on the revert direction — the restore rides entirely on
    the final rename UPDATE. When a stray row DOES pre-exist at an old id
    (an out-of-band write between cascade and revert), the dedupe step
    handles it exactly as the forward direction would: the stray absorbs
    the reverting row (merge for frecency, delete-then-keep elsewhere) —
    tested, not assumed.

    Returns a :class:`RevertReport`; the caller gates the map-clear on
    ``report.ok``.
    """
    reverse: dict[str, str] = {}
    lost: list[str] = []
    for old_id in sorted(leg_entries):
        new_chash, _target = leg_entries[old_id]
        if new_chash in reverse:
            lost.append(old_id)  # collapse sibling: its row was merged away
        else:
            reverse[new_chash] = old_id
    if lost:
        _log.warning(
            "remap_revert_collapse_loss",
            unrestorable=len(lost),
            old_ids=lost[:20],
            note="forward dedupe merged these siblings' rows; the surviving "
            "row reverts to the sorted-first old id (byte-identical text)",
        )
    results = _run_stores(
        reverse, catalog_db=catalog_db, memory_db=memory_db,
        event="remap_revert_store_done",
    )
    return RevertReport(stores=results, unrestorable=tuple(lost))
