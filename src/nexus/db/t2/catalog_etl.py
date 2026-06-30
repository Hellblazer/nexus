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


def migrate_catalog(
    catalog_db_path: Path,
    client: Any,
    *,
    batch_log_every: int = 50,
    collector: Any = None,
) -> dict[str, dict[str, int]]:
    """Copy all rows from a SQLite catalog into Postgres via *client*.

    Uses ``HttpCatalogClient`` import endpoints:
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

    Returns:
        ``{"owners": {"read": N, "written": M}, "documents": {...}, ...}``
        — always ``read == written`` for a healthy run.

    Copy-not-move guarantee: ``_open_ro`` opens the file with ``?mode=ro``
    so the SQLite source is read-only at the OS level.
    """
    conn = _open_ro(catalog_db_path)
    try:
        owners_rows    = _fetch_all(conn, "owners")
        docs_rows      = _fetch_all(conn, "documents")
        links_rows     = _fetch_all(conn, "links")
        collections_rows = _fetch_all(conn, "collections")
        chunks_rows    = _fetch_all(conn, "document_chunks")
        meta_rows      = _fetch_all(conn, "_meta")
    finally:
        conn.close()

    results: dict[str, dict[str, int]] = {}

    # RDR-153 soft-dangler policy: links carry NO enforced endpoint FK —
    # rows referencing missing documents IMPORT (the graph edge is event
    # data) and each dangling edge is recorded as a flagged advisory.
    if collector is not None:
        live_tumblers = {r.get("tumbler") for r in docs_rows}
        for r in links_rows:
            if (
                r.get("from_tumbler") not in live_tumblers
                or r.get("to_tumbler") not in live_tumblers
            ):
                collector.record(
                    "catalog", "links",
                    issue_class="soft_dangler",
                    constraint="links.(from|to)_tumbler -> documents.tumbler "
                               "(not enforced)",
                    reason="link endpoint references a missing document; row "
                           "imports; sample ids are <from_tumbler>:<to_tumbler>",
                    action="flagged",
                    sample_id=f"{r.get('from_tumbler')}:{r.get('to_tumbler')}",
                )

    _log.info(
        "catalog_etl.start",
        source=str(catalog_db_path),
        owners=len(owners_rows),
        documents=len(docs_rows),
        links=len(links_rows),
        collections=len(collections_rows),
        chunks=len(chunks_rows),
        meta=len(meta_rows),
    )

    # ── 1. owners ──────────────────────────────────────────────────────────────
    results["owners"] = _import_table(
        table="owners",
        rows=owners_rows,
        transform=_transform_owner,
        import_fn=lambda payload: client._post("/import/owner", payload),
        batch_log_every=batch_log_every,
        collector=collector,
    )

    # ── 2. documents (depends on owners) ───────────────────────────────────────
    results["documents"] = _import_table(
        table="documents",
        rows=docs_rows,
        transform=_transform_document,
        import_fn=lambda payload: client._post("/import/document", payload),
        batch_log_every=batch_log_every,
        collector=collector,
    )

    # ── 3. collections (independent of docs, but after owners) ─────────────────
    results["collections"] = _import_table(
        table="collections",
        rows=collections_rows,
        transform=_transform_collection,
        import_fn=lambda payload: client._post("/import/collection", payload),
        batch_log_every=batch_log_every,
        collector=collector,
    )

    # ── 4. document_chunks (depends on documents) ──────────────────────────────
    chunk_read = 0
    chunk_written = 0
    # Group chunks by doc_id for the envelope format {"doc_id": ..., "rows": [...]}
    chunks_by_doc: dict[str, list[dict[str, Any]]] = {}
    for crow in chunks_rows:
        doc_id = crow["doc_id"]
        chunks_by_doc.setdefault(doc_id, []).append(crow)

    for doc_id, doc_chunks in chunks_by_doc.items():
        chunk_rows_payload = [_transform_chunk_row(c) for c in doc_chunks]
        chunk_read += len(doc_chunks)
        try:
            client._post("/import/chunk", {"doc_id": doc_id, "rows": chunk_rows_payload})
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
    results["document_chunks"] = {"read": chunk_read, "written": chunk_written}
    _log.info("catalog_etl.table_done", table="document_chunks",
              read=chunk_read, written=chunk_written)

    # ── 5. links (last: soft-FK on both endpoints) ─────────────────────────────
    results["links"] = _import_table(
        table="links",
        rows=links_rows,
        transform=_transform_link,
        import_fn=lambda payload: client._post("/import/link", payload),
        batch_log_every=batch_log_every,
        collector=collector,
    )

    # ── 6. _meta — SKIPPED intentionally ──────────────────────────────────────
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

    # ── 7. Reconcile next_seq on owners ────────────────────────────────────────
    # Floor each owner's next_seq so future server-side tumbler allocation cannot
    # collide with -- or REUSE -- a migrated tumbler. The authoritative high-water
    # mark is owners.jsonl (never decremented on delete); max(surviving doc seq) is
    # only a lower bound and would reuse deleted slots on a compacted catalog.
    doc_tumblers = [r["tumbler"] for r in docs_rows]
    high_water = _read_owner_high_water(catalog_db_path)
    reconcile = _reconcile_next_seq(client, owners_rows, doc_tumblers, high_water)
    results["next_seq_reconcile"] = {
        "read": 0,
        "written": reconcile["reconciled"],
        "failed": reconcile["failed"],
    }

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
    if collector is not None:
        for table, counts in results.items():
            collector.count_read("catalog", table, counts.get("read", 0))
            collector.count_written("catalog", table, counts.get("written", 0))

    return results


def _import_table(
    *,
    table: str,
    rows: list[dict[str, Any]],
    transform: Any,
    import_fn: Any,
    batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    """Import one table's rows via ``import_fn(payload)``."""
    read_count = 0
    written_count = 0
    total = len(rows)

    _log.info("catalog_etl.table_start", table=table, total_rows=total)

    for row in rows:
        read_count += 1
        try:
            # Transform INSIDE the try (RDR-153 P2 critic): a corrupt row
            # must become a recorded failed issue, never abort the table.
            payload = transform(row)
            import_fn(payload)
            written_count += 1
        except Exception as exc:  # noqa: BLE001 — per-row resilience; logged, one bad row must not abort table
            # Log the key for the failing row; continue so one bad row
            # doesn't abort the whole table.
            key_hint = (
                row.get("tumbler_prefix")
                or row.get("tumbler")
                or row.get("name")
                or row.get("from_tumbler")
                or f"row-{read_count}"
            )
            _log.error(
                "catalog_etl.row_failed",
                table=table,
                key=key_hint,
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "catalog", table,
                    issue_class="unexpected",
                    constraint=table,
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=str(key_hint),
                )

        if read_count % batch_log_every == 0:
            _log.info(
                "catalog_etl.progress",
                table=table,
                read=read_count,
                written=written_count,
                total=total,
            )

    _log.info(
        "catalog_etl.table_done",
        table=table,
        read=read_count,
        written=written_count,
    )
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
            client._post("/import/owner", payload)
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
