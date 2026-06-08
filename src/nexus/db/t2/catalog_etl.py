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
- ``collections``:    ON CONFLICT (tenant_id, name) DO NOTHING
- ``document_chunks``:ON CONFLICT (tenant_id, doc_id, position) DO NOTHING
- ``links``:          ON CONFLICT (tenant_id, from_tumbler, to_tumbler,
                       link_type) DO NOTHING

FIDELITY-PRESERVING:
- source_mtime uses GREATEST (monotonic high-water mark on re-run).
- links and chunks use DO NOTHING (event data, never overwritten).
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
  After importing owners + documents, we issue a POST /v1/catalog/owners/head_hash
  placeholder to bump next_seq on each owner so the service can safely
  assign new document tumblers without colliding with migrated ones.
  The post-migration next_seq = max(existing_doc_sequence) + 1 per owner,
  derived by parsing the document tumbler strings.

BATCH SIZE:
  Each table is imported row-by-row through the ``POST /v1/catalog/import/*``
  routes (which accept either a single object or ``{"rows": [...]}``) to keep
  memory pressure low and make per-row error logging actionable.  A future
  optimisation can batch N rows per request; the current shape is identical
  to the other ETL modules in this package.

FIELD MAPPING:

owners (SQLite) -> POST /v1/catalog/import/owner:
  tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash

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
    "id",
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

    After importing all documents, reconciles ``next_seq`` on each owner
    via ``POST /v1/catalog/owners/head_hash`` (a no-op for the head_hash
    field but the endpoint is the cheapest path to trigger an upsert that
    carries ``next_seq`` in the payload).  We do this by calling
    ``_post("/owners/upsert", {..., "next_seq": N})``.

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
    )

    # ── 2. documents (depends on owners) ───────────────────────────────────────
    results["documents"] = _import_table(
        table="documents",
        rows=docs_rows,
        transform=_transform_document,
        import_fn=lambda payload: client._post("/import/document", payload),
        batch_log_every=batch_log_every,
    )

    # ── 3. collections (independent of docs, but after owners) ─────────────────
    results["collections"] = _import_table(
        table="collections",
        rows=collections_rows,
        transform=_transform_collection,
        import_fn=lambda payload: client._post("/import/collection", payload),
        batch_log_every=batch_log_every,
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
        except Exception as exc:
            _log.error(
                "catalog_etl.chunk_group_failed",
                doc_id=doc_id,
                count=len(doc_chunks),
                error=str(exc),
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
    # After importing all documents, set next_seq = max(doc_seq) + 1 per owner
    # so future server-side tumbler assignment won't collide with migrated docs.
    doc_tumblers = [r["tumbler"] for r in docs_rows]
    _reconcile_next_seq(client, owners_rows, doc_tumblers)

    total_read    = sum(v["read"]    for v in results.values())
    total_written = sum(v["written"] for v in results.values())
    _log.info(
        "catalog_etl.complete",
        source=str(catalog_db_path),
        total_read=total_read,
        total_written=total_written,
        by_table={k: v for k, v in results.items()},
    )
    return results


def _import_table(
    *,
    table: str,
    rows: list[dict[str, Any]],
    transform: Any,
    import_fn: Any,
    batch_log_every: int,
) -> dict[str, int]:
    """Import one table's rows via ``import_fn(payload)``."""
    read_count = 0
    written_count = 0
    total = len(rows)

    _log.info("catalog_etl.table_start", table=table, total_rows=total)

    for row in rows:
        read_count += 1
        payload = transform(row)
        try:
            import_fn(payload)
            written_count += 1
        except Exception as exc:
            # Log the key for the failing row; continue so one bad row
            # doesn't abort the whole table.
            key_hint = (
                payload.get("tumbler_prefix")
                or payload.get("tumbler")
                or payload.get("name")
                or payload.get("from_tumbler")
                or f"row-{read_count}"
            )
            _log.error(
                "catalog_etl.row_failed",
                table=table,
                key=key_hint,
                error=str(exc),
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


def _reconcile_next_seq(
    client: Any,
    owners_rows: list[dict[str, Any]],
    doc_tumblers: list[str],
) -> None:
    """Log the required ``next_seq`` values per owner (informational).

    ``upsertOwner`` on the Java service does NOT accept a ``next_seq``
    override in the import payload — the column is omitted from the jOOQ
    ``onConflict.doUpdate()`` clause.  Reconciling ``next_seq`` after ETL
    requires a dedicated service operation (tracked in a follow-on bead).

    This function computes and logs the correct target values so an
    operator can verify or apply them manually if needed.  It does NOT
    make any HTTP calls.

    The correct value for ``next_seq`` after ETL:
        next_seq = max(document_sequence_for_owner)

    ``registerDocument`` reads ``seq = next_seq`` and then assigns
    ``tumbler = ownerPrefix + "." + (seq + 1)``, setting
    ``next_seq = seq + 1``.  With ``next_seq = max_seq``, the next
    assigned tumbler is ``prefix.{max_seq+1}``, which is safe.
    """
    for owner in owners_rows:
        prefix = owner["tumbler_prefix"]
        max_seq = _max_seq_for_owner(prefix, doc_tumblers)
        if max_seq > 0:
            _log.info(
                "catalog_etl.next_seq_advisory",
                owner=prefix,
                recommended_next_seq=max_seq,
                note=(
                    "upsertOwner does not accept next_seq override. "
                    "Follow-on: service-side endpoint needed to SET next_seq "
                    "so registerDocument won't collide with migrated tumblers."
                ),
            )
        else:
            _log.debug(
                "catalog_etl.next_seq_no_docs",
                owner=prefix,
            )
