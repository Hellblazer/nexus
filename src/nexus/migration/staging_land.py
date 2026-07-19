# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-180 LAND-THEN-TRANSFORM, client landing half (nexus-jxizy.10.6).

The guided migration's client role collapses to: CENSUS the source (every
column, mechanically), LAND it verbatim over ``/v1/staging/*``, drive
``embed_fill -> promote`` per collection and ``finalize`` per wave, verify
counts, clear. The per-leg in-flight rewrite class this replaces produced
eight missed-leg bugs (see tests/test_no_chash_truncation.py's history).

Pieces:

- :class:`HttpStagingStore` — the ``/v1/staging`` wire client (the f2qvx
  house mixin: token/endpoint resolution + 401 self-healing).
- :func:`source_census` — the PRE-LAND every-column census over the SQLite
  sources (Hal directive): ``sqlite_master`` + ``PRAGMA table_info``
  enumeration, value-shape classification, fail LOUD on any chash-bearing
  column the landing manifest does not claim; non-vacuous (must rediscover
  the known inventory or the census itself fails).
- :func:`validate_timestamp_fields` — pre-land ISO-or-empty guard
  (reviewer-p1 Medium: one malformed staged timestamp otherwise aborts the
  whole tenant finalize, repeatedly; surface it BEFORE landing).
- ``land_*`` legs — pointer stores from SQLite (incl. the topics JOIN
  projection: topic identity crosses stores as (label, collection), the
  legacy integer id is audit-only — critic-p1 Critical), chunks from the
  immutable Chroma source with land-time classification (the honest target
  name; nexus-nb7hr measured-dim override preserved) and the reuse-legality
  decision (a vector is staged only when the source model IS the target
  model; everything else embed-fills server-side).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

#: Wire batch cap — mirrors the engine's MAX_ROWS_PER_LOAD.
LOAD_BATCH = 300

#: Chash-shaped value classes (the legacy eras + canonical).
_CHASH_SHAPE = re.compile(r"^([0-9a-f]{16}|[0-9a-f]{32}|[0-9a-f]{64})$")

#: Sample size + prevalence for column classification.
_CENSUS_SAMPLE = 200
_CENSUS_PREVALENCE = 0.8
_CENSUS_MIN_HITS = 3


class StagingCensusError(RuntimeError):
    """A chash-bearing source column the landing manifest does not claim —
    landing would silently lose it. FAIL BEFORE LANDING."""


class StagedTimestampError(ValueError):
    """A staged timestamp value that will not parse as ISO-8601 — it would
    abort the whole tenant finalize server-side; surface it pre-land."""


#: The landing manifest: (source_db, source_table, column) triples the
#: landing legs CLAIM. The census fails on any chash-bearing column outside
#: this set + the justified exclusions below.
LANDING_MANIFEST: frozenset[tuple[str, str, str]] = frozenset({
    ("catalog", "document_chunks", "chash"),
    ("memory", "chash_index", "chash"),
    ("memory", "topic_assignments", "doc_id"),
    ("memory", "frecency", "chunk_id"),
    ("memory", "relevance_log", "chunk_id"),
})

#: Justified census exclusions: chash-shaped content that is NOT a chunk
#: pointer. Each entry must exist in the scanned source or the census fails
#: (the pruned-allowlist discipline).
CENSUS_EXCLUSIONS: frozenset[tuple[str, str, str, str]] = frozenset({
    ("memory", "aspect_extraction_queue", "content_hash",
     "sha256 of source CONTENT (document identity, not a chunk pointer)"),
    ("memory", "document_aspects", "source_path",
     "paths/URIs; chash appears only embedded in URI strings, rewritten at read"),
})


@dataclass(frozen=True)
class CensusFinding:
    db: str
    table: str
    column: str
    sampled: int
    chash_shaped: int


@dataclass
class CensusReport:
    findings: list[CensusFinding] = field(default_factory=list)
    unclaimed: list[CensusFinding] = field(default_factory=list)


def _columns(conn: sqlite3.Connection) -> Iterable[tuple[str, str]]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        for col in conn.execute(f"PRAGMA table_info({t})"):
            yield t, col[1]


def source_census(conns: dict[str, sqlite3.Connection]) -> CensusReport:
    """The pre-land every-column census (Hal directive, schema-derived).

    Scans EVERY column of EVERY table in each source connection, classifies
    chash-bearing columns by value shape (prevalence over a sample), and
    raises :class:`StagingCensusError` when one is neither claimed by
    :data:`LANDING_MANIFEST` nor excluded with justification.

    NON-VACUITY: when a source contains a known-inventory table, the census
    must classify its chash column as chash-bearing — a census that cannot
    rediscover the inventory it exists to protect fails ITSELF.
    """
    report = CensusReport()
    claimed = {(db, t, c) for db, t, c in LANDING_MANIFEST}
    excluded = {(db, t, c) for db, t, c, _ in CENSUS_EXCLUSIONS}
    for db, conn in conns.items():
        for table, column in _columns(conn):
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND "{column}" <> \'\' '
                f"LIMIT {_CENSUS_SAMPLE}").fetchall()
            values = [str(r[0]) for r in rows]
            if not values:
                continue
            hits = sum(1 for v in values if _CHASH_SHAPE.match(v))
            if hits >= _CENSUS_MIN_HITS and hits / len(values) >= _CENSUS_PREVALENCE:
                finding = CensusFinding(db, table, column, len(values), hits)
                report.findings.append(finding)
                if (db, table, column) not in claimed and (db, table, column) not in excluded:
                    report.unclaimed.append(finding)
    if report.unclaimed:
        raise StagingCensusError(
            "chash-bearing source column(s) the landing manifest does not "
            "claim — landing would silently lose them: "
            + ", ".join(f"{f.db}.{f.table}.{f.column} ({f.chash_shaped}/{f.sampled})"
                        for f in report.unclaimed)
            + " — add a landing leg (LANDING_MANIFEST) or a justified "
            "CENSUS_EXCLUSIONS entry")
    # Non-vacuity: rediscover the known inventory where its tables exist
    # with data.
    for db, table, column in LANDING_MANIFEST:
        conn = conns.get(db)
        if conn is None:
            continue
        present = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name = ?",
            (table,)).fetchone()[0]
        if not present:
            continue
        populated = conn.execute(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
            f'AND "{column}" <> \'\'').fetchone()[0]
        if populated >= _CENSUS_MIN_HITS and not any(
                f.db == db and f.table == table and f.column == column
                for f in report.findings):
            raise StagingCensusError(
                f"census failed to rediscover known inventory column "
                f"{db}.{table}.{column} ({populated} populated rows) — the "
                "scanner is broken; a clean report from it proves nothing")
    return report


_TS_FIELDS = ("created_at", "embedded_at", "last_hit_at", "ts", "extracted_at",
              "enqueued_at", "last_attempt_at")


def validate_timestamp_fields(store: str, rows: list[dict[str, Any]]) -> None:
    """ISO-or-empty pre-land guard for every ``*_at``/``ts`` staged field."""
    for i, row in enumerate(rows):
        for f in _TS_FIELDS:
            v = row.get(f)
            if v in (None, ""):
                continue
            try:
                datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError as exc:
                raise StagedTimestampError(
                    f"{store} row {i}: field {f}={v!r} is not ISO-8601 — "
                    "fix or blank it in the source before landing (a malformed "
                    "value aborts the whole tenant finalize server-side)"
                ) from exc


class HttpStagingStore(RefreshableHttpStoreMixin):
    """The ``/v1/staging`` wire client. Construct with no arguments in
    production (endpoint/token via the f2qvx resolution chain)."""

    def load(self, store: str, rows: list[dict[str, Any]]) -> int:
        """Land *rows* verbatim into staging *store*, batched at the wire
        cap. Timestamps are pre-validated. Returns rows landed."""
        validate_timestamp_fields(store, rows)
        landed = 0
        for i in range(0, len(rows), LOAD_BATCH):
            batch = rows[i:i + LOAD_BATCH]
            out = self._post(f"/v1/staging/load/{store}", {"rows": batch})
            landed += int((out or {}).get("landed", 0))
        return landed

    def embed_fill(self, collection: str) -> dict[str, Any]:
        return self._post("/v1/staging/embed_fill", {"collection": collection})

    def promote(self, collection: str) -> dict[str, Any]:
        return self._post("/v1/staging/promote", {"collection": collection})

    def finalize(self, orphan_policy: str = "drop") -> dict[str, Any]:
        return self._post("/v1/staging/finalize", {"orphan_policy": orphan_policy})

    def clear(self) -> dict[str, Any]:
        return self._post("/v1/staging/clear", {})

    def counts(self) -> dict[str, int]:
        return self._get("/v1/staging/counts")


# ── Pointer-store landing legs (SQLite → staging, verbatim) ──────────────────

def topic_assignment_rows(memory_conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The topics JOIN projection (critic-p1 Critical): topic identity
    crosses stores as (label, collection); the legacy integer id rides along
    audit-only. An assignment whose topic row is GONE (orphaned FK) is
    dropped here, loudly counted, rather than landed unresolvable."""
    rows = [
        {"doc_id": r[0], "topic_id": r[1], "topic_label": r[2], "topic_collection": r[3]}
        for r in memory_conn.execute(
            "SELECT ta.doc_id, ta.topic_id, t.label, t.collection "
            "FROM topic_assignments ta JOIN topics t ON t.id = ta.topic_id")
    ]
    orphaned = topic_assignment_orphans(memory_conn)
    if orphaned:
        _log.warning("staging_land_topic_orphans_skipped", count=orphaned)
    return rows


def topic_assignment_orphans(memory_conn: sqlite3.Connection) -> int:
    """Source assignments whose topic row is GONE (orphaned FK) — skipped by
    the landing; surfaced in the driver's landed summary so the operator
    SEES the count (reviewer-p2 Medium: a structlog line is not 'loudly
    counted')."""
    return memory_conn.execute(
        "SELECT count(*) FROM topic_assignments ta "
        "WHERE NOT EXISTS (SELECT 1 FROM topics t WHERE t.id = ta.topic_id)"
    ).fetchone()[0]


def pointer_store_rows(
    store: str,
    catalog_conn: sqlite3.Connection | None,
    memory_conn: sqlite3.Connection | None,
) -> list[dict[str, Any]]:
    """Verbatim wire rows for one pointer store, per the engine's
    ``StagingHandler.STORES`` column contract."""
    if store == "document_chunks":
        assert catalog_conn is not None
        return [
            {"doc_id": r[0], "position": r[1], "chash": r[2], "chunk_index": r[3],
             "line_start": r[4], "line_end": r[5], "char_start": r[6], "char_end": r[7]}
            for r in catalog_conn.execute(
                "SELECT doc_id, position, chash, chunk_index, line_start, line_end, "
                "char_start, char_end FROM document_chunks")
        ]
    assert memory_conn is not None
    if store == "chash_index":
        return [
            {"chash": r[0], "physical_collection": r[1], "created_at": r[2]}
            for r in memory_conn.execute(
                "SELECT chash, physical_collection, created_at FROM chash_index")
        ]
    if store == "topic_assignments":
        return topic_assignment_rows(memory_conn)
    if store == "frecency":
        return [
            {"chunk_id": r[0], "embedded_at": r[1], "ttl_days": r[2],
             "frecency_score": r[3], "miss_count": r[4], "last_hit_at": r[5]}
            for r in memory_conn.execute(
                "SELECT chunk_id, embedded_at, ttl_days, frecency_score, miss_count, "
                "last_hit_at FROM frecency")
        ]
    if store == "relevance_log":
        return [
            {"id": r[0], "query": r[1], "chunk_id": r[2], "collection": r[3],
             "action": r[4], "session_id": r[5], "ts": r[6]}
            for r in memory_conn.execute(
                "SELECT rowid, query, chunk_id, collection, action, session_id, "
                "timestamp FROM relevance_log")
        ]
    if store == "document_aspects":
        # RDR-096 P5.2 (dev-driver-rewire catch): migrated T2 stores DROP
        # the source_path column (migrate_drop_source_path_column, 4.31.0)
        # — the staging wire's source_path (the engine PK's third leg) then
        # derives from the surviving identity: source_uri, else doc_id
        # (never '' — an all-empty PK leg would collide across rows).
        cols = {r[1] for r in memory_conn.execute("PRAGMA table_info(document_aspects)")}
        sp_expr = ("source_path" if "source_path" in cols
                   else "COALESCE(NULLIF(source_uri, ''), doc_id)")
        return [
            {"doc_id": r[0], "collection": r[1], "source_path": r[2],
             "problem_formulation": r[3], "proposed_method": r[4],
             "experimental_datasets": r[5], "experimental_baselines": r[6],
             "experimental_results": r[7], "extras": r[8], "confidence": r[9],
             "extracted_at": r[10], "model_version": r[11], "extractor_name": r[12],
             "source_uri": r[13]}
            for r in memory_conn.execute(
                f"SELECT COALESCE(doc_id, ''), collection, {sp_expr}, "
                "problem_formulation, proposed_method, experimental_datasets, "
                "experimental_baselines, experimental_results, extras, confidence, "
                "extracted_at, model_version, extractor_name, source_uri "
                "FROM document_aspects")
        ]
    if store == "aspect_extraction_queue":
        return [
            {"collection": r[0], "source_path": r[1], "doc_id": r[2],
             "content_hash": r[3], "content": r[4], "status": r[5],
             "retry_count": r[6], "enqueued_at": r[7], "last_attempt_at": r[8],
             "last_error": r[9]}
            for r in memory_conn.execute(
                "SELECT collection, source_path, COALESCE(doc_id, ''), content_hash, "
                "content, status, retry_count, enqueued_at, last_attempt_at, "
                "last_error FROM aspect_extraction_queue")
        ]
    raise ValueError(f"unknown pointer store {store!r}")


# ── Chunk landing leg (Chroma → staging, verbatim + classified) ──────────────

def chunk_rows(
    source_collection: Any,
    *,
    target_name: str,
    target_model: str,
    target_dim: int,
    source_model: str | None,
    page: int = LOAD_BATCH,
) -> Iterable[list[dict[str, Any]]]:
    """Yield wire batches for one Chroma source collection.

    ``target_name`` is the LAND-TIME-CLASSIFIED honest name (the existing
    detection/auto-remap incl. the nexus-nb7hr measured-dim override —
    reconciliation H1: promote asserts name-implied dim == staged dim, so
    the classification MUST happen here, never after landing). Reuse
    legality is decided per the design: the stored vector is staged ONLY
    when the source model IS the target model; otherwise the row lands
    vector-less and ``embed_fill`` covers it server-side.
    """
    reuse_legal = source_model == target_model
    offset = 0
    while True:
        got = source_collection.get(
            limit=page, offset=offset,
            include=["documents", "embeddings", "metadatas"])
        ids = got.get("ids") or []
        if not ids:
            return
        docs = got.get("documents") or []
        embs = got.get("embeddings")
        metas = got.get("metadatas") or []
        batch: list[dict[str, Any]] = []
        for i, cid in enumerate(ids):
            emb = None
            if reuse_legal and embs is not None and i < len(embs) and embs[i] is not None:
                emb = [float(x) for x in embs[i]]
            row: dict[str, Any] = {
                "collection": target_name,
                "dim": target_dim,
                "legacy_ref": cid,
                "chunk_text": (docs[i] if i < len(docs) else "") or "",
                "model": target_model,
                "chunk_meta": (metas[i] if i < len(metas) else None) or None,
            }
            if emb is not None:
                row["embedding"] = emb
            batch.append(row)
        yield batch
        if len(ids) < page:
            return
        offset += page
