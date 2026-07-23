# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres catalog ETL (bead nexus-bdaxz, RDR-152).

COPY-NOT-MOVE: reads all rows from the SQLite catalog tables (owners,
documents, links, collections, document_chunks, _meta) and writes them
through the validated HTTP seam (``HttpCatalogClient`` import endpoints) so
every write flows via Java -> jOOQ -> Postgres under RLS with tenant
stamping.  The SQLite source is NEVER modified.

IDEMPOTENT: each import route uses server-side upsert / DO NOTHING:

- ``owners``:         ON CONFLICT (tenant_id, tumbler_prefix) DO UPDATE
                       (all fields from EXCLUDED) via upsertOwner
- ``documents``:      ON CONFLICT (tenant_id, tumbler) DO UPDATE
                       (GREATEST source_mtime, all other fields EXCLUDED)
- ``collections``:    ON CONFLICT (tenant_id, name) DO UPDATE, conditional —
                       only UPGRADES a pre-existing stub row (one whose
                       embedding_model/content_type/owner_id are still empty);
                       an already-populated row is left untouched (effective
                       no-op). Insert-or-preserve, never delete, so the landed
                       count is always >= the written count (see
                       orchestrator._VERIFY_TABLES_DEDUP).
- ``document_chunks``:ON CONFLICT (tenant_id, doc_id, position) DO UPDATE
                       (chash + all data cols from EXCLUDED; nexus-9wz72)
                       Convergent: re-index with changed content updates the
                       manifest; idempotent: same values are a no-op in effect.
- ``links``:          ON CONFLICT (tenant_id, from_tumbler, to_tumbler,
                       link_type) DO NOTHING

FIDELITY-PRESERVING:
- source_mtime uses GREATEST (monotonic high-water mark on re-run).
- links use DO NOTHING (event data, never overwritten).
- chunks use DO UPDATE for convergence (nexus-9wz72): a re-index with
  changed content updates chash + positional fields; same values are a
  no-op in effect (idempotency preserved).
- metadata JSON is copied verbatim.
- SQLite ``_meta`` table is migrated into ``catalog_meta`` verbatim
  (key-value store for consistency markers).

FK INSERTION ORDER (critical):
  owners -> documents -> collections -> document_chunks -> links

  The cross-store FK constraint (fk-001-catalog-cross-store.xml) enforces
  ``catalog_document_chunks(tenant_id, doc_id) REFERENCES
  catalog_documents(tenant_id, tumbler) ON DELETE CASCADE``.  Documents
  must be committed before chunks; owners before documents; collections are
  independent of documents so they can go anywhere after owners.  Links
  reference documents via soft FKs in production (the hard FK is only on
  the cross-store tables topic_assignments / document_aspects etc.), so
  links come last to be safe.

NEXT_SEQ RECONCILIATION:
  The SQLite ``.catalog.db`` this ETL reads does NOT store next_seq -- in the
  SQLite-backed catalog that counter lives in ``owners.jsonl`` (see
  catalog.py: "next_seq is JSONL-only state"), which the ETL never opens.  The
  authoritative post-migration value is therefore DERIVED from the migrated data:
  ``next_seq = max(document_sequence_for_owner)``, computed by parsing the document
  tumbler strings.  ``_reconcile_next_seq`` re-POSTs each owner with that floor; the
  service GREATEST-merges next_seq on conflict (importOwner in CatalogRepository), so
  the pass is idempotent and never downgrades a counter the live service has already
  advanced.  ``registerDocument`` then assigns ``prefix.{next_seq+1}``, which cannot
  collide with any migrated tumbler.  (The owner payload also carries ``next_seq``
  verbatim for forward-compat with any future source that does persist it; from
  SQLite that value is always 0 and the derived floor wins via GREATEST.)

BATCH SIZE:
  Each table is imported row-by-row through the ``POST /v1/catalog/import/*``
  routes (which accept either a single object or ``{"rows": [...]}``) to keep
  memory pressure low and make per-row error logging actionable.  A future
  optimisation can batch N rows per request; the current shape is identical
  to the other ETL modules in this package.

FIELD MAPPING:

owners (SQLite) -> POST /v1/catalog/import/owner:
  tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash, next_seq

documents (SQLite) -> POST /v1/catalog/import/document:
  tumbler, title, author, year, content_type, file_path, corpus,
  physical_collection, chunk_count, head_hash, indexed_at,
  metadata (JSON column -> dict), source_mtime, alias_of, source_uri,
  bib_year, bib_authors, bib_venue, bib_citation_count,
  bib_semantic_scholar_id, bib_openalex_id, bib_doi, bib_enriched_at

links (SQLite) -> POST /v1/catalog/import/link:
  from_tumbler, to_tumbler, link_type, from_span, to_span,
  created_by, created_at, metadata (JSON)

document_chunks (SQLite) -> POST /v1/catalog/import/chunk:
  Grouped by doc_id; each group posted as {"doc_id": ..., "rows": [...]}.
  Columns per row: position, chash, chunk_index, line_start, line_end,
  char_start, char_end

collections (SQLite) -> POST /v1/catalog/import/collection:
  name, content_type, owner_id, embedding_model, model_version,
  display_name, legacy_grandfathered, superseded_by, superseded_at,
  created_at

_meta (SQLite ``_meta``) -> POST /v1/catalog/import/meta (catalog_meta):
  key, value
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

_log = structlog.get_logger(__name__)

# Tables read from the SQLite catalog (read-only, copy-not-move).
_CATALOG_DB_FILENAME = ".catalog.db"

# Result keys that represent a genuine per-table import (read N rows, wrote M).
# Used for total_read/total_written; excludes bookkeeping entries (catalog_meta,
# next_seq_reconcile) whose read/written do not pair up. Consumed by the CLI too.
IMPORT_TABLE_KEYS = ("owners", "documents", "collections", "document_chunks", "links")

# ── column lists aligned to _SCHEMA_SQL in nexus/db/t2/catalog.py ────────────

_OWNER_COLUMNS = (
    "tumbler_prefix",
    "name",
    "owner_type",
    "repo_hash",
    "description",
    "repo_root",
    "head_hash",
)

_DOC_COLUMNS = (
    "tumbler",
    "title",
    "author",
    "year",
    "content_type",
    "file_path",
    "corpus",
    "physical_collection",
    "chunk_count",
    "head_hash",
    "indexed_at",
    "metadata",
    "source_mtime",
    "alias_of",
    "source_uri",
    "bib_year",
    "bib_authors",
    "bib_venue",
    "bib_citation_count",
    "bib_semantic_scholar_id",
    "bib_openalex_id",
    "bib_doi",
    "bib_enriched_at",
)

_LINK_COLUMNS = (
    "id",  # SQLite AUTOINCREMENT PK — fetched by SELECT * but intentionally
           # OMITTED from the import payload (_transform_link); PG uses BIGSERIAL.
    "from_tumbler",
    "to_tumbler",
    "link_type",
    "from_span",
    "to_span",
    "created_by",
    "created_at",
    "metadata",
)

_CHUNK_COLUMNS = (
    "doc_id",
    "position",
    "chash",
    "chunk_index",
    "line_start",
    "line_end",
    "char_start",
    "char_end",
)

_COLLECTION_COLUMNS = (
    "name",
    "content_type",
    "owner_id",
    "embedding_model",
    "model_version",
    "display_name",
    "legacy_grandfathered",
    "superseded_by",
    "superseded_at",
    "created_at",
)


def _open_ro(catalog_db_path: Path) -> sqlite3.Connection:
    """Open the SQLite catalog DB read-only via URI mode."""
    uri = f"file:{catalog_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite catalog for reading: {catalog_db_path}: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if ``table`` exists in the SQLite catalog."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _fetch_all(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """Fetch all rows from ``table`` as dicts, or [] if the table is absent."""
    if not _table_exists(conn, table):
        _log.warning("catalog_etl.table_missing", table=table)
        return []
    cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 — source is read-only
    return [dict(r) for r in cursor.fetchall()]


def _transform_owner(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite ``owners`` row to the import payload dict."""
    return {
        "tumbler_prefix": row["tumbler_prefix"],
        "name":           row["name"],
        "owner_type":     row["owner_type"],
        "repo_hash":      row.get("repo_hash") or "",
        "description":    row.get("description") or "",
        "repo_root":      row.get("repo_root") or "",
        "head_hash":      row.get("head_hash") or "",
        # next_seq is NOT in the SQLite .catalog.db (it lives in owners.jsonl), so this
        # is 0 from a real source. The authoritative value is derived from the migrated
        # document tumblers in _reconcile_next_seq's second pass; this field is forward-
        # compat transport for any future source that does persist next_seq. The service
        # GREATEST-merges on conflict, so the derived floor always wins.
        "next_seq":       int(row.get("next_seq") or 0),
    }


def _transform_document(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite ``documents`` row to the import payload dict.

    ``metadata`` is stored as a JSON string in SQLite (column type JSON);
    the service expects a dict or null.
    """
    raw_meta = row.get("metadata")
    if raw_meta and isinstance(raw_meta, str):
        try:
            metadata = json.loads(raw_meta)
        except (json.JSONDecodeError, ValueError):
            metadata = None
    elif isinstance(raw_meta, dict):
        metadata = raw_meta
    else:
        metadata = None

    return {
        "tumbler":                row["tumbler"],
        "title":                  row["title"],
        "author":                 row.get("author") or "",
        "year":                   row.get("year") or 0,
        "content_type":           row.get("content_type") or "",
        "file_path":              row.get("file_path") or "",
        "corpus":                 row.get("corpus") or "",
        "physical_collection":    row.get("physical_collection") or "",
        "chunk_count":            row.get("chunk_count") or 0,
        "head_hash":              row.get("head_hash") or "",
        "indexed_at":             row.get("indexed_at") or "",
        "metadata":               metadata,
        "source_mtime":           row.get("source_mtime") or 0.0,
        "alias_of":               row.get("alias_of") or "",
        "source_uri":             row.get("source_uri") or "",
        "bib_year":               row.get("bib_year") or 0,
        "bib_authors":            row.get("bib_authors") or "",
        "bib_venue":              row.get("bib_venue") or "",
        "bib_citation_count":     row.get("bib_citation_count") or 0,
        "bib_semantic_scholar_id": row.get("bib_semantic_scholar_id") or "",
        "bib_openalex_id":        row.get("bib_openalex_id") or "",
        "bib_doi":                row.get("bib_doi") or "",
        "bib_enriched_at":        row.get("bib_enriched_at") or "",
    }


def _transform_link(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite ``links`` row to the import payload dict.

    ``id`` is the SQLite AUTOINCREMENT PK — NOT imported (PG uses BIGSERIAL).
    ``metadata`` is a JSON string or None.
    """
    raw_meta = row.get("metadata")
    if raw_meta and isinstance(raw_meta, str):
        try:
            metadata = json.loads(raw_meta)
        except (json.JSONDecodeError, ValueError):
            metadata = None
    elif isinstance(raw_meta, dict):
        metadata = raw_meta
    else:
        metadata = None

    return {
        "from_tumbler": row["from_tumbler"],
        "to_tumbler":   row["to_tumbler"],
        "link_type":    row["link_type"],
        "from_span":    row.get("from_span") or "",
        "to_span":      row.get("to_span") or "",
        "created_by":   row.get("created_by") or "user",
        "created_at":   row.get("created_at") or "",
        "metadata":     metadata,
    }


def _transform_chunk_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map one row from SQLite ``document_chunks`` to the per-row payload.

    ``doc_id`` is excluded here — it is passed as the top-level ``doc_id``
    field in the ``{"doc_id": ..., "rows": [...]}`` envelope.
    """
    return {
        "position":    row["position"],
        "chash":       row["chash"],
        "chunk_index": row.get("chunk_index"),
        "line_start":  row.get("line_start"),
        "line_end":    row.get("line_end"),
        "char_start":  row.get("char_start"),
        "char_end":    row.get("char_end"),
    }


def _transform_collection(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite ``collections`` row to the import payload dict."""
    return {
        "name":                 row["name"],
        "content_type":         row.get("content_type") or "",
        "owner_id":             row.get("owner_id") or "",
        "embedding_model":      row.get("embedding_model") or "",
        "model_version":        row.get("model_version") or "",
        "display_name":         row.get("display_name") or "",
        "legacy_grandfathered": row.get("legacy_grandfathered") or 0,
        "superseded_by":        row.get("superseded_by") or "",
        "superseded_at":        row.get("superseded_at") or "",
        "created_at":           row.get("created_at") or "",
    }


def live_source_uri_losers(doc_rows: list[dict[str, Any]]) -> set[str]:
    """Tumblers that LOSE the per-``source_uri`` uniqueness dedup (nexus-78n33).

    The catalog-016 partial unique index (one LIVE document per
    ``(tenant_id, source_uri)``) exists on the target engine BEFORE this
    import runs, so a source carrying duplicate live source_uris — the
    evidenced 201-uri class from the 2026-06-11 audit (T2
    ``nexus_rdr/156-P0-source-uri-audit``); no dedup sweep ever ran on the
    SQLite side — would land persistent batch failures. Instead the import
    stream is made conflict-free HERE, deterministically, with the SAME
    winner rule as the server-side catalog-016-0 backfill: greatest
    ``chunk_count``, then earliest non-empty ``indexed_at``, then lowest
    ``tumbler``. Losers (and, in the sibling migrate functions, their
    ``document_chunks`` / ``links`` child rows) are skipped and RECORDED in
    the migration report — never silently dropped. The immutable SQLite
    source keeps every row as the rollback record.

    Pure function of the source rows so each migrate_* step can recompute
    it independently and reach the identical verdict (no cross-step state).
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in doc_rows:
        su = r.get("source_uri") or ""
        if su:
            groups.setdefault(su, []).append(r)
    losers: set[str] = set()
    for rows in groups.values():
        if len(rows) < 2:
            continue
        def _rank(r: dict[str, Any]) -> tuple:
            indexed_at = r.get("indexed_at") or ""
            return (
                -(r.get("chunk_count") or 0),
                (indexed_at == "", indexed_at),  # empty sorts last
                str(r["tumbler"]),
            )
        ordered = sorted(rows, key=_rank)
        losers.update(str(r["tumbler"]) for r in ordered[1:])
    return losers


def _record_uri_dedup_skips(
    collector: Any, table: str, sample_ids: list[str],
) -> None:
    if collector is None or not sample_ids:
        return
    for sid in sample_ids:
        collector.record(
            "catalog", table,
            issue_class="identity_mismatch",
            constraint="ux_catalog_documents_live_source_uri",
            reason=(
                "duplicate live source_uri in the migration source (the "
                "201-uri class, audit 2026-06-11): the catalog-016 unique "
                "index on the target refuses a second live row, so the "
                "dedup LOSER (fewest chunks / latest indexed_at / highest "
                "tumbler) is skipped; the winner carries the identity. "
                "Source row preserved in the immutable SQLite rollback copy."
            ),
            action="skipped",
            sample_id=sid,
        )


def _max_seq_for_owner(tumbler_prefix: str, doc_tumblers: list[str]) -> int:
    """Return the highest document-sequence number for an owner prefix.

    For owner prefix ``1.2``, a document tumbler ``1.2.37`` has seq=37.
    Returns 0 when no documents are found for this owner.

    ``registerDocument`` reads ``next_seq`` and assigns tumbler
    ``ownerPrefix + "." + (seq + 1)``, then stores ``next_seq = seq + 1``.
    So to ensure the next assigned tumbler is ``prefix.{max_seq+1}``, we
    must set ``next_seq = max_seq`` after ETL.
    """
    prefix_dot = tumbler_prefix + "."
    max_seq = 0
    for t in doc_tumblers:
        if t.startswith(prefix_dot):
            rest = t[len(prefix_dot):]
            # Only consider direct children (no more dots in remainder)
            if "." not in rest and rest.isdigit():
                seq = int(rest)
                if seq > max_seq:
                    max_seq = seq
    return max_seq


def count_source_rows(catalog_db_path: Path) -> dict[str, int]:
    """Return row counts for each catalog table (read-only).

    Used by the ``--dry-run`` CLI path to report table sizes without
    writing.  Opens the source in ``uri=True mode=ro``.

    Returns a dict ``{table_name: row_count}`` for the 6 relevant tables.
    """
    conn = _open_ro(catalog_db_path)
    counts: dict[str, int] = {}
    try:
        for table in ("owners", "documents", "links", "collections",
                      "document_chunks", "_meta"):
            if _table_exists(conn, table):
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
                counts[table] = row[0] if row else 0
            else:
                counts[table] = 0
    finally:
        conn.close()
    return counts


# ── Per-table entry points (RDR-180 nexus-jxizy.10.7 split) ────────────────────
#
# Each function migrates exactly ONE SQLite catalog table (or, for links, one
# table plus a read-only reference read of documents for the soft-dangler
# check) and opens its own read-only connection (copy-not-move; mirrors
# aspects_etl.py's per-table shape), self-recording collector counts so it
# is usable standalone.
#
# document_chunks is the CHASH-BEARING manifest (RDR-180): the guided
# land-then-transform path lands + promotes it server-side, so
# migrate_catalog_without_chunks() composes the other four (plus the meta
# skip-note and next_seq reconcile, neither of which touches chunks) and
# deliberately excludes it.


def migrate_owners(
    catalog_db_path: Path, client: Any, *, batch_log_every: int = 50,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY the owners table."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        owners_rows = _fetch_all(conn, "owners")
    finally:
        conn.close()
    result = _import_table(
        table="owners", rows=owners_rows, transform=_transform_owner,
        import_fn=lambda rows: client._post("/import/owner", {"rows": rows}),
        batch_log_every=batch_log_every, collector=collector, breaker=breaker,
    )
    if collector is not None:
        collector.count_read("catalog", "owners", result["read"])
        collector.count_written("catalog", "owners", result["written"])
    return result


def migrate_documents(
    catalog_db_path: Path, client: Any, *, batch_log_every: int = 50,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY the documents table."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        docs_rows = _fetch_all(conn, "documents")
    finally:
        conn.close()
    # nexus-78n33: make the stream conflict-free against the target's
    # catalog-016 unique index (see live_source_uri_losers).
    losers = live_source_uri_losers(docs_rows)
    if losers:
        _log.warning(
            "catalog_etl.source_uri_dedup",
            table="documents", skipped=len(losers),
        )
        _record_uri_dedup_skips(collector, "documents", sorted(losers))
        docs_rows = [r for r in docs_rows if str(r["tumbler"]) not in losers]
    result = _import_table(
        table="documents", rows=docs_rows, transform=_transform_document,
        import_fn=lambda rows: client._post("/import/document", {"rows": rows}),
        batch_log_every=batch_log_every, collector=collector, breaker=breaker,
    )
    if collector is not None:
        collector.count_read("catalog", "documents", result["read"])
        collector.count_written("catalog", "documents", result["written"])
    return result


def migrate_collections(
    catalog_db_path: Path, client: Any, *, batch_log_every: int = 50,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY the collections table."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        collections_rows = _fetch_all(conn, "collections")
    finally:
        conn.close()
    result = _import_table(
        table="collections", rows=collections_rows, transform=_transform_collection,
        import_fn=lambda rows: client._post("/import/collection", {"rows": rows}),
        batch_log_every=batch_log_every, collector=collector, breaker=breaker,
    )
    if collector is not None:
        collector.count_read("catalog", "collections", result["read"])
        collector.count_written("catalog", "collections", result["written"])
    return result


def migrate_document_chunks(
    catalog_db_path: Path, client: Any, *, batch_log_every: int = 50,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY document_chunks — the CHASH-BEARING manifest import.

    RDR-180 (nexus-jxizy.10.7): this is the table the guided
    land-then-transform path lands + promotes separately (chunks carry
    chash identity); the guided ``catalog`` slot excludes exactly this
    function (see :func:`migrate_catalog_without_chunks`).
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        chunks_rows = _fetch_all(conn, "document_chunks")
        docs_rows = _fetch_all(conn, "documents")
    finally:
        conn.close()

    # nexus-78n33: chunks carry the HARD FK to documents — manifest rows of
    # source_uri-dedup LOSERS (whose parent doc is skipped by
    # migrate_documents) would fail the import. Skip + record them; the
    # winner document's own manifest carries the content.
    losers = live_source_uri_losers(docs_rows)

    chunk_read = 0
    chunk_written = 0
    # Group chunks by doc_id for the envelope format {"doc_id": ..., "rows": [...]}
    chunks_by_doc: dict[str, list[dict[str, Any]]] = {}
    skipped_loser_docs: list[str] = []
    for crow in chunks_rows:
        doc_id = crow["doc_id"]
        if str(doc_id) in losers:
            chunk_read += 1
            if not skipped_loser_docs or skipped_loser_docs[-1] != str(doc_id):
                skipped_loser_docs.append(str(doc_id))
            continue
        chunks_by_doc.setdefault(doc_id, []).append(crow)
    if skipped_loser_docs:
        _log.warning(
            "catalog_etl.source_uri_dedup",
            table="document_chunks", skipped_docs=len(skipped_loser_docs),
        )
        _record_uri_dedup_skips(
            collector, "document_chunks", sorted(set(skipped_loser_docs)),
        )

    # RDR-176 P3 (Gap 1): the /import/chunk envelope is doc-scoped ({doc_id,
    # rows}); split each doc's chunks into <= quota-sized sub-batches so a doc
    # with >300 chunks does not exceed MAX_RECORDS_PER_WRITE in one POST. (Cross-
    # doc batching is not possible under the doc-scoped envelope; a doc with few
    # chunks is still one POST — the server-side jOOQ batch is the further
    # optimisation, bead nexus-1qpni.)
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — branch-local; quota constant
    _chunk_cap = QUOTAS.MAX_RECORDS_PER_WRITE

    for doc_id, doc_chunks in chunks_by_doc.items():
        chunk_rows_payload = [_transform_chunk_row(c) for c in doc_chunks]
        chunk_read += len(doc_chunks)
        try:
            for i in range(0, len(chunk_rows_payload), _chunk_cap):
                sub = chunk_rows_payload[i: i + _chunk_cap]
                # RDR-178 Gap 3 (nexus-ob4vc): this call used to POST
                # directly with NO retry wrapper at all — the genuine
                # bypassed call site behind the 270-row catalog manifest
                # loss on 2026-07-01 (unlike chash_etl, which DID route
                # through _etl_with_retry but was blocked by the classifier
                # gap fixed in nexus.retry._RETRYABLE_ETL_HTTP_STATUSES).
                _etl_batch_with_breaker(
                    client._post, "/import/chunk", {"doc_id": doc_id, "rows": sub},
                    breaker=breaker,
                )
            chunk_written += len(doc_chunks)
        except Exception as exc:  # noqa: BLE001 — per-doc resilience; logged, one bad group must not abort ETL
            _log.error(
                "catalog_etl.chunk_group_failed",
                doc_id=doc_id,
                count=len(doc_chunks),
                error=str(exc),
            )
            if collector is not None:
                # One failed record per chunk in the rejected group —
                # total_failed is the gate predicate and counts rows.
                for _ in doc_chunks:
                    collector.record_event(
                        "catalog", "document_chunks",
                        issue_class="unexpected",
                        constraint="document_chunks(doc_id,position)",
                        reason=f"chunk group rejected during import "
                               f"(doc {doc_id}): {exc}",
                        action="failed",
                    )
        if chunk_read % (batch_log_every * 5) == 0 and chunk_read > 0:
            _log.info(
                "catalog_etl.chunks_progress",
                read=chunk_read,
                written=chunk_written,
            )
    _log.info("catalog_etl.table_done", table="document_chunks",
              read=chunk_read, written=chunk_written)
    if collector is not None:
        collector.count_read("catalog", "document_chunks", chunk_read)
        collector.count_written("catalog", "document_chunks", chunk_written)
    return {"read": chunk_read, "written": chunk_written}


def migrate_links(
    catalog_db_path: Path, client: Any, *, batch_log_every: int = 50,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY the links table.

    RDR-153 soft-dangler policy: links carry NO enforced endpoint FK — rows
    referencing missing documents IMPORT (the graph edge is event data) and
    each dangling edge is recorded as a flagged advisory. documents are
    read (read-only) ONLY to build that advisory check — they are NOT
    written here; see :func:`migrate_documents`.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        links_rows = _fetch_all(conn, "links")
        docs_rows = _fetch_all(conn, "documents")
    finally:
        conn.close()

    if collector is not None:
        live_tumblers = {r.get("tumbler") for r in docs_rows}
        # nexus-78n33: links whose endpoint is a source_uri-dedup LOSER
        # import as event data (soft-FK policy, same as danglers) but the
        # endpoint document is skipped by migrate_documents — the edge is
        # invisible to live traversal on the target. Flag them so the
        # report names the orphaned edges instead of leaving them silent.
        dedup_losers = live_source_uri_losers(docs_rows)
        for r in links_rows:
            f, t = r.get("from_tumbler"), r.get("to_tumbler")
            if str(f) in dedup_losers or str(t) in dedup_losers:
                collector.record(
                    "catalog", "links",
                    issue_class="identity_mismatch",
                    constraint="ux_catalog_documents_live_source_uri",
                    reason="link endpoint is a source_uri-dedup loser (its "
                           "document is skipped; the winner carries the "
                           "identity) — edge imports but is orphaned from "
                           "live traversal; sample ids are <from>:<to>",
                    action="flagged",
                    sample_id=f"{f}:{t}",
                )
            elif f not in live_tumblers or t not in live_tumblers:
                collector.record(
                    "catalog", "links",
                    issue_class="soft_dangler",
                    constraint="links.(from|to)_tumbler -> documents.tumbler "
                               "(not enforced)",
                    reason="link endpoint references a missing document; row "
                           "imports; sample ids are <from_tumbler>:<to_tumbler>",
                    action="flagged",
                    sample_id=f"{f}:{t}",
                )

    result = _import_table(
        table="links", rows=links_rows, transform=_transform_link,
        import_fn=lambda rows: client._post("/import/link", {"rows": rows}),
        batch_log_every=batch_log_every, collector=collector, breaker=breaker,
    )
    if collector is not None:
        collector.count_read("catalog", "links", result["read"])
        collector.count_written("catalog", "links", result["written"])
    return result


def _catalog_meta_and_next_seq(
    catalog_db_path: Path, client: Any, results: dict[str, dict[str, int]],
    *, collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, dict[str, int]]:
    """Append the _meta skip-note + next_seq reconcile bookkeeping entries
    (shared by :func:`migrate_catalog` and :func:`migrate_catalog_without_chunks`
    — neither touches document_chunks) into *results*, then log totals.

    Mutates and returns *results*.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(catalog_db_path)
    try:
        owners_rows = _fetch_all(conn, "owners")
        docs_rows   = _fetch_all(conn, "documents")
        meta_rows   = _fetch_all(conn, "_meta")
    finally:
        conn.close()

    # ── _meta — SKIPPED intentionally ──────────────────────────────────────
    # The SQLite ``_meta`` table stores SQLite-projection consistency markers
    # (``last_applied_event_offset``, etc.).  These are SQLite rebuild artifacts
    # and have no meaning in Postgres (Postgres is always consistent).  The PG
    # ``catalog_meta`` table is reserved for future service-side markers.
    _log.info(
        "catalog_etl.meta_skipped",
        rows=len(meta_rows),
        reason="SQLite projection markers not applicable to PG",
    )
    results["catalog_meta"] = {"read": 0, "written": 0, "skipped": len(meta_rows)}
    if collector is not None:
        collector.count_read("catalog", "catalog_meta", 0)
        collector.count_written("catalog", "catalog_meta", 0)

    # ── Reconcile next_seq on owners ────────────────────────────────────────
    # Floor each owner's next_seq so future server-side tumbler allocation cannot
    # collide with -- or REUSE -- a migrated tumbler. The authoritative high-water
    # mark is owners.jsonl (never decremented on delete); max(surviving doc seq) is
    # only a lower bound and would reuse deleted slots on a compacted catalog.
    # Independent of document_chunks — safe to run whether or not chunks migrated.
    doc_tumblers = [r["tumbler"] for r in docs_rows]
    high_water = _read_owner_high_water(catalog_db_path)
    reconcile = _reconcile_next_seq(client, owners_rows, doc_tumblers, high_water, breaker=breaker)
    results["next_seq_reconcile"] = {
        "read": 0,
        "written": reconcile["reconciled"],
        "failed": reconcile["failed"],
    }
    if collector is not None:
        collector.count_read("catalog", "next_seq_reconcile", 0)
        collector.count_written("catalog", "next_seq_reconcile", reconcile["reconciled"])

    # Totals cover the genuine per-table imports only. catalog_meta (intentional
    # skip) and next_seq_reconcile (second-pass owner re-imports with no source
    # "read") are bookkeeping entries; including them would make total_written
    # exceed total_read and corrupt the CLI's skipped-rows calculation.
    total_read    = sum(results[k]["read"]    for k in IMPORT_TABLE_KEYS if k in results)
    total_written = sum(results[k]["written"] for k in IMPORT_TABLE_KEYS if k in results)
    _log.info(
        "catalog_etl.complete",
        source=str(catalog_db_path),
        total_read=total_read,
        total_written=total_written,
        by_table={k: v for k, v in results.items()},
    )
    return results


# ── Composition entry points ────────────────────────────────────────────────


def migrate_catalog_without_chunks(
    catalog_db_path: Path,
    client: Any,
    *,
    batch_log_every: int = 50,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, dict[str, int]]:
    """owners + documents + collections + links + catalog_meta skip-note +
    next_seq reconcile — NOT document_chunks.

    RDR-180 (nexus-jxizy.10.7): document_chunks is the chash-bearing
    manifest, landed + promoted server-side by the guided land-then-transform
    path; running it here too would double-write a stale, unresolved
    legacy-id copy. The guided ``catalog`` store slot calls this instead of
    :func:`migrate_catalog`. FK insertion order (owners -> documents ->
    collections -> links) is preserved; next_seq reconcile depends only on
    documents, never on chunks.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    results: dict[str, dict[str, int]] = {
        "owners": migrate_owners(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "documents": migrate_documents(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "collections": migrate_collections(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "links": migrate_links(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
    }
    return _catalog_meta_and_next_seq(
        catalog_db_path, client, results, collector=collector, breaker=breaker,
    )


def migrate_catalog(
    catalog_db_path: Path,
    client: Any,
    *,
    batch_log_every: int = 50,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, dict[str, int]]:
    """Copy all rows from a SQLite catalog into Postgres via *client*.

    Thin composition of the five per-table entry points (RDR-180
    nexus-jxizy.10.7 split), in the exact order the prior monolithic
    implementation used. Uses ``HttpCatalogClient`` import endpoints:
      - POST /v1/catalog/import/owner
      - POST /v1/catalog/import/document
      - POST /v1/catalog/import/collection
      - POST /v1/catalog/import/chunk
      - POST /v1/catalog/import/link

    Insertion order respects FK constraints:
      owners -> documents -> collections -> document_chunks -> links

    After importing all documents, reconciles ``next_seq`` on each owner by
    re-POSTing ``/v1/catalog/import/owner`` with ``next_seq`` floored at the max
    migrated document sequence.  The service GREATEST-merges ``next_seq`` on
    conflict, so this is idempotent and never downgrades a live-advanced counter.
    See ``_reconcile_next_seq``.

    Args:
        catalog_db_path: Path to the SQLite ``.catalog.db`` file.
        client:          An ``HttpCatalogClient`` (or duck-typed) instance
                         connected to the Postgres service.
        batch_log_every: Emit a progress log line every N rows.
        breaker:         Shared :class:`~nexus.retry.EtlCircuitBreaker` for
                         this whole catalog leg (RDR-178 Gap 3) — ONE
                         instance spans owners/documents/collections/
                         document_chunks/links/next_seq_reconcile so "N
                         consecutive" reflects the leg's health, not a
                         single table. Defaults to a fresh instance.

    Returns:
        ``{"owners": {"read": N, "written": M}, "documents": {...}, ...}``
        — always ``read == written`` for a healthy run.

    Copy-not-move guarantee: ``_open_ro`` opens the file with ``?mode=ro``
    so the SQLite source is read-only at the OS level.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    results: dict[str, dict[str, int]] = {
        "owners": migrate_owners(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "documents": migrate_documents(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "collections": migrate_collections(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "document_chunks": migrate_document_chunks(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "links": migrate_links(
            catalog_db_path, client, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
    }
    return _catalog_meta_and_next_seq(
        catalog_db_path, client, results, collector=collector, breaker=breaker,
    )


def _import_table(
    *,
    table: str,
    rows: list[dict[str, Any]],
    transform: Any,
    import_fn: Any,
    batch_log_every: int,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Import one table's rows via ``import_fn(rows)`` (a batched array POST).

    RDR-176 P3 (Gap 1, bead nexus-t9rmg.18): the ``/v1/catalog/import/*``
    endpoints accept a ``{"rows": [...]}`` array; ``import_fn`` POSTs the batch,
    so the transfer count is ceil(N/quota), not N (the per-row shape the dogfood
    hit). Each row is still TRANSFORMED per-row (a corrupt row is recorded +
    excluded); only the NETWORK is batched. A server-side batch rejection is
    recorded at batch granularity; the import is idempotent so a re-run lands it.

    RDR-178 Gap 3 (nexus-ob4vc): *breaker* (defaults to a fresh instance when
    unset) paces retries on a sustained transient outage instead of dropping
    the batch after one bounded ``_etl_with_retry`` cycle.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — branch-local; quota constant
    bsize = QUOTAS.MAX_RECORDS_PER_WRITE

    read_count = 0
    written_count = 0
    total = len(rows)
    batch: list[dict[str, Any]] = []
    keys: list[str] = []

    _log.info("catalog_etl.table_start", table=table, total_rows=total)

    def _key(row: dict[str, Any], n: int) -> str:
        return str(
            row.get("tumbler_prefix") or row.get("tumbler") or row.get("name")
            or row.get("from_tumbler") or f"row-{n}"
        )

    def _flush() -> None:
        nonlocal written_count, batch, keys
        if not batch:
            return
        try:
            _etl_batch_with_breaker(import_fn, batch, breaker=breaker)
            written_count += len(batch)
        except Exception as exc:  # noqa: BLE001 — batch failure logged + recorded; migration continues (idempotent re-run)
            _log.error("catalog_etl.batch_failed", table=table, count=len(batch), error=str(exc))
            if collector is not None:
                for key in keys:
                    collector.record(
                        "catalog", table,
                        issue_class="unexpected", constraint=table,
                        reason=f"batch import rejected: {exc}",
                        action="failed", sample_id=key,
                    )
        batch = []
        keys = []

    for row in rows:
        read_count += 1
        try:
            # Transform INSIDE the try (RDR-153 P2 critic): a corrupt row
            # must become a recorded failed issue, never abort the table.
            payload = transform(row)
        except Exception as exc:  # noqa: BLE001 — per-row transform failure logged + recorded; continue
            key_hint = _key(row, read_count)
            _log.error("catalog_etl.row_failed", table=table, key=key_hint, error=str(exc))
            if collector is not None:
                collector.record(
                    "catalog", table,
                    issue_class="unexpected", constraint=table,
                    reason=f"row rejected during transform: {exc}",
                    action="failed", sample_id=str(key_hint),
                )
            continue

        batch.append(payload)
        keys.append(_key(row, read_count))
        if len(batch) >= bsize:
            _flush()

        if read_count % batch_log_every == 0:
            _log.info(
                "catalog_etl.progress",
                table=table, read=read_count, written=written_count, total=total,
            )

    _flush()
    _log.info("catalog_etl.table_done", table=table, read=read_count, written=written_count)
    return {"read": read_count, "written": written_count}


def _read_owner_high_water(catalog_db_path: Path) -> dict[str, int]:
    """Return ``{owner_prefix: next_seq}`` from the catalog's ``owners.jsonl``.

    ``owners.jsonl`` (sibling of ``.catalog.db``) holds the authoritative tumbler
    high-water mark: ``OwnerRecord.next_seq`` is the *next* document number to assign
    and is NEVER decremented on delete/compact (catalog.py: "prevents tumbler reuse
    after delete+compact").  Deriving next_seq from surviving document tumblers alone
    under-counts on a compacted catalog and would reuse deleted tumbler slots.

    Returns an empty dict (with a warning) when ``owners.jsonl`` is absent so callers
    fall back to the surviving-doc floor; that fallback is correct only for catalogs
    that have never had a document deleted.
    """
    jsonl_path = catalog_db_path.parent / "owners.jsonl"
    if not jsonl_path.exists():
        _log.warning(
            "catalog_etl.owners_jsonl_absent",
            path=str(jsonl_path),
            impact="next_seq floor falls back to max surviving doc seq; safe only "
                   "if no documents were ever deleted from this catalog",
        )
        return {}
    from nexus.catalog.tumbler import read_owners  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.tumbler)

    records = read_owners(jsonl_path)
    return {owner: rec.next_seq for owner, rec in records.items()}


def _reconcile_next_seq(
    client: Any,
    owners_rows: list[dict[str, Any]],
    doc_tumblers: list[str],
    high_water: dict[str, int],
    *,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Reconcile ``next_seq`` on each owner so post-cutover tumbler allocation can
    neither collide with NOR reuse a migrated tumbler.

    The SQLite ``.catalog.db`` has no next_seq column; the authoritative high-water
    mark lives in ``owners.jsonl`` (``high_water``).  JSONL ``next_seq`` is the *next*
    number to assign, whereas the Java service stores the *last assigned* and allocates
    ``last + 1`` -- so the correct service floor is ``jsonl_next_seq - 1``.  Where the
    JSONL value is unavailable we fall back to ``max(surviving doc sequence)`` (a lower
    bound, correct only when nothing was deleted).  We take the max of the two so a
    stale JSONL can never drop us below an actually-present document.

    The service GREATEST-merges next_seq on conflict, so this is idempotent and never
    downgrades a counter the live service has already advanced past the floor.

    ``registerDocument`` reads ``seq = next_seq`` then assigns
    ``tumbler = ownerPrefix + "." + (seq + 1)``.  With ``next_seq = floor`` the next
    assigned tumbler is ``prefix.{floor+1}``, which is ``>= jsonl_next_seq`` and so
    cannot collide with or reuse any migrated/deleted tumbler.

    Returns ``{"reconciled": N, "failed": M}``.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    reconciled = 0
    failed = 0
    for owner in owners_rows:
        prefix = owner["tumbler_prefix"]
        max_doc_seq = _max_seq_for_owner(prefix, doc_tumblers)
        # jsonl next_seq is "next to assign"; the service stores "last assigned",
        # so the service-side floor is jsonl_next_seq - 1.
        jsonl_floor = high_water.get(prefix, 0) - 1
        floor = max(max_doc_seq, jsonl_floor)
        if floor <= 0:
            _log.debug("catalog_etl.next_seq_no_docs", owner=prefix)
            continue
        if jsonl_floor < max_doc_seq and prefix in high_water:
            # owners.jsonl high-water is below a surviving document — the source
            # catalog's own counter is corrupt; the doc floor protects us.
            _log.warning(
                "catalog_etl.next_seq_jsonl_below_docs",
                owner=prefix,
                jsonl_next_seq=high_water.get(prefix),
                max_doc_seq=max_doc_seq,
            )
        payload = _transform_owner(owner)
        payload["next_seq"] = floor
        try:
            # RDR-178 Gap 3: was an unwrapped client._post — same bypass
            # class as the document_chunks loop above, fixed the same way.
            _etl_batch_with_breaker(client._post, "/import/owner", payload, breaker=breaker)
            reconciled += 1
            _log.info(
                "catalog_etl.next_seq_reconciled",
                owner=prefix,
                next_seq=floor,
                source="jsonl" if jsonl_floor >= max_doc_seq else "max_doc_seq",
            )
        except Exception as exc:  # noqa: BLE001 — per-owner resilience; logged, one failure must not abort reconcile
            failed += 1
            _log.error(
                "catalog_etl.next_seq_reconcile_failed",
                owner=prefix,
                next_seq=floor,
                error=str(exc),
            )
    return {"reconciled": reconciled, "failed": failed}
