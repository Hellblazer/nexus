# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DocumentAspects — T2 store for structured per-document aspects (RDR-089).

Owns one SQLite table, ``document_aspects``, holding the canonical
extracted aspects for each document. Each row is a *complete* snapshot
of the latest extraction:

    PRIMARY KEY (collection, source_path)

Per-chunk doc_id is intentionally not in the schema — multiple chunks
of the same source document map to a single aspect row.

**RDR-096 deprecation window** (P2.1 → P5.1, two minor releases):
``source_uri`` is the persistent URI identity column added at 4.16.0
(P2.1). Operator SQL fast paths (``operators/aspect_sql.py``) read
identity via ``COALESCE(source_uri, 'file://' || source_path) AS
source_identity`` for the entire window — this is the dual-read that
keeps any pre-migration row whose source_uri escaped backfill (empty
source_path edge case from research-2) addressable. Two minor
releases after 4.16.0, P5.1 stops new ingest paths from writing
``source_path``, and the column becomes read-only. The
``source_path`` column itself is dropped in a later migration once
all consumer code has migrated to URI-only identity. The exact
target version is set when P5.1 ships, not pre-pinned in source.

Upsert semantics: COMPLETE IDEMPOTENT OVERWRITE. The latest extraction
replaces any previous one verbatim. No diff/merge, no per-field
stability check, no deviation log. Phase 1 callers are aspect
extractors that re-run as a whole or not at all.

Schema is locked by the RDR — column names, types, and nullability
must match the migration entry exactly. Drift is caught by
``test_schema_columns_match_rdr_lock``.

Lock convention (mirrors ``ChashIndex``):
  * Public methods acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``.

The schema is duplicated here as ``CREATE IF NOT EXISTS`` so a fresh
``DocumentAspects`` construction creates the table even before
``apply_pending`` runs. Identical shape to the migration —
idempotent across construction + migration.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()


# nexus-17wf: minimum confidence for an aspect record to be persisted.
# Records with confidence None or below this floor are dropped at
# upsert time with a structured warning, since they signal extractor
# failures (LLM returned malformed JSON, retry-loop exhausted, etc.).
# 2026-05-08 prod probe: 125 of 753 rows (16.6%) had NULL or zero
# confidence; downstream consumers treated them as authoritative.
# 0.3 is conservative: the same probe showed avg=0.823 across
# non-NULL rows, so a real extraction clears the floor comfortably.
_MIN_CONFIDENCE: float = 0.3


# ── Schema SQL ──────────────────────────────────────────────────────────────

_DOCUMENT_ASPECTS_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS document_aspects (
    collection             TEXT NOT NULL,
    source_path            TEXT NOT NULL,
    problem_formulation    TEXT,
    proposed_method        TEXT,
    experimental_datasets  TEXT,
    experimental_baselines TEXT,
    experimental_results   TEXT,
    extras                 TEXT,
    confidence             REAL,
    extracted_at           TEXT NOT NULL,
    model_version          TEXT NOT NULL,
    extractor_name         TEXT NOT NULL,
    -- RDR-096 P2.1: persistent URI identity.
    -- For new writes (Phase 1+), populated by callers as
    -- ``chroma://<collection>/<source_path>`` (or ``file://`` for
    -- legacy filesystem-backed collections). Read paths use
    -- ``COALESCE(source_uri, 'file://' || source_path)`` during
    -- the deprecation window (P2.3).
    source_uri             TEXT,
    -- RDR-109 Phase 5: salient sentences produced by attention-guided-v1
    -- extractor. JSON-encoded array of strings; NULL on rows written
    -- before Phase 5 ships. Read paths must tolerate NULL.
    salient_sentences      TEXT,
    PRIMARY KEY (collection, source_path)
);

CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor
    ON document_aspects(extractor_name, model_version);
"""


# ── Record dataclass ────────────────────────────────────────────────────────


@dataclass
class AspectRecord:
    """A single document's extracted aspects.

    JSON-shaped fields (``experimental_datasets``,
    ``experimental_baselines``, ``extras``) are typed as Python
    list / dict here; the store handles serialization on write and
    deserialization on read.

    ``doc_id`` (RDR-108 Phase 1c): catalog tumbler identity for the
    source document.  After the PK migration, this is the primary key;
    ``collection`` and ``source_path`` are retained as denorm cache
    columns.  Empty string on legacy rows written before the migration.
    """

    collection: str
    source_path: str
    problem_formulation: str | None
    proposed_method: str | None
    experimental_datasets: list[str] = field(default_factory=list)
    experimental_baselines: list[str] = field(default_factory=list)
    experimental_results: str | None = None
    extras: dict = field(default_factory=dict)
    confidence: float | None = None
    extracted_at: str = ""
    model_version: str = ""
    extractor_name: str = ""
    # RDR-096 P2.1: persistent URI identity. ``None`` on legacy rows
    # written before P2.1 ships; populated for all writes after.
    source_uri: str | None = None
    # RDR-108 Phase 1c: catalog tumbler identity. Empty string on legacy
    # rows written before the PK migration.
    doc_id: str = ""
    # RDR-109 Phase 5: salient sentences (attention-guided-v1 extractor).
    # Empty list when the extractor was not run or returned no candidates.
    salient_sentences: list[str] = field(default_factory=list)


def _resolve_doc_id(record: AspectRecord) -> str:
    """Derive a stable ``doc_id`` for a record that arrived empty.

    Resolution order:
      1. Catalog lookup (``physical_collection`` + ``file_path`` / ``title``).
         Returns the document's tumbler if found.
      2. ``source_uri`` if non-empty. RDR-096 canonical identity is
         unique per document and stable across runs.
      3. ``legacy:{collection}:{source_path}`` synthetic. Last-resort
         deterministic key so ``INSERT OR REPLACE`` preserves
         uniqueness per real document.

    Returns ``""`` only when collection AND source_path AND source_uri
    are all empty.

    Lazy catalog import to avoid circular dependency with
    ``nexus.catalog`` (which imports ``nexus.db.t2`` for ``_sanitize_fts5``).
    """
    # nexus-8g79.10 (V6): use Catalog.lookup_doc_id_by_collection_and_path
    # (the public probe API) instead of cracking open the raw SQL here.
    # The lazy import remains because db→catalog is still a direction
    # we don't want at module-import time (Catalog itself imports db
    # primitives for FTS5 sanitisation), but the *behaviour* is no
    # longer reaching into Catalog internals.
    try:
        from nexus.catalog import Catalog, open_cached
        from nexus.config import catalog_path
        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            cat = open_cached(cat_path)
            resolved = cat.lookup_doc_id_by_collection_and_path(
                record.collection, record.source_path,
            )
            if resolved:
                return resolved
    except Exception:
        pass
    if record.source_uri:
        return record.source_uri
    if record.collection and record.source_path:
        return f"legacy:{record.collection}:{record.source_path}"
    return ""


# ── DocumentAspects ─────────────────────────────────────────────────────────


class DocumentAspects:
    """Owns the ``document_aspects`` table.

    See module docstring for locking, schema-duplication, and
    upsert-semantics contracts.
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
            self.conn.executescript(_DOCUMENT_ASPECTS_SCHEMA_SQL)
            self.conn.commit()

    # ── Schema detection ──────────────────────────────────────────────────

    def _has_doc_id_pk(self) -> bool:
        """Return True iff ``document_aspects`` is using ``doc_id`` as its PK.

        Used by ``upsert`` and ``get`` to route to the right SQL form
        without callers needing to know which schema version is live.
        Cached per-instance after first call (schema cannot change while
        this connection is open without explicit migration).
        """
        with self._lock:
            if not hasattr(self, "_doc_id_pk_cache"):
                pk_cols = {
                    r[1] for r in self.conn.execute(
                        "PRAGMA table_info(document_aspects)"
                    ).fetchall()
                    if r[5] == 1
                }
                self._doc_id_pk_cache: bool = pk_cols == {"doc_id"}
        return self._doc_id_pk_cache

    def _source_path_select(self) -> str:
        """SQL fragment for the ``source_path`` column in SELECT lists.

        Pre-drop returns ``"source_path"``; post-drop returns
        ``"'' AS source_path"`` so the row-tuple shape consumed by
        ``_row_to_record`` is preserved without per-method branching.
        """
        return "source_path" if self._has_source_path_column() else "'' AS source_path"

    def _has_source_path_column(self) -> bool:
        """Return True iff ``document_aspects.source_path`` still exists.

        nexus-6xp2: ``ocu9.11`` (4.31.0) drops the column once je0b has
        run and every row has source_uri populated. Cached per-instance
        like ``_has_doc_id_pk``; schema cannot change under a live
        connection without an explicit migration.
        """
        with self._lock:
            if not hasattr(self, "_source_path_cache"):
                cols = {
                    r[1] for r in self.conn.execute(
                        "PRAGMA table_info(document_aspects)"
                    ).fetchall()
                }
                self._source_path_cache: bool = "source_path" in cols
        return self._source_path_cache

    # ── RDR-109 Phase 5: salient_sentences I/O ────────────────────────────

    def _has_salient_sentences_column(self) -> bool:
        with self._lock:
            if not hasattr(self, "_salient_cache"):
                cols = {
                    r[1] for r in self.conn.execute(
                        "PRAGMA table_info(document_aspects)"
                    ).fetchall()
                }
                self._salient_cache: bool = "salient_sentences" in cols
        return self._salient_cache

    def set_salient_sentences(
        self, doc_id: str, sentences: list[str],
    ) -> bool:
        """Write the ``salient_sentences`` column for *doc_id*.

        Narrow API independent of :meth:`upsert` so the
        ``attention-guided-v1`` extractor can populate the column
        without re-running the LLM-backed aspect pipeline. Returns
        True on update; False when no row matches.

        Falls back to ``(collection, source_path)``-keyed update on
        installs where je0b's PK switch has not yet run (catalog
        absent or MCP-worker block deferred the migration). The
        fallback looks up the matching row via the legacy ``doc_id``
        column if present, otherwise raises False.

        No-op (returns False) if the ``salient_sentences`` column does
        not exist on the running schema.
        """
        if not self._has_salient_sentences_column():
            return False
        if not doc_id:
            return False
        encoded = json.dumps(sentences, separators=(",", ":")) if sentences else "[]"
        with self._lock:
            cols = {
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            if "doc_id" in cols:
                cur = self.conn.execute(
                    "UPDATE document_aspects "
                    "SET salient_sentences = ? WHERE doc_id = ?",
                    (encoded, doc_id),
                )
            else:
                # Pre-je0b legacy schema: ``doc_id`` column does not
                # exist. Caller must use ``set_salient_sentences_by_key``
                # on this branch; report False so it falls through.
                return False
            self.conn.commit()
            return cur.rowcount > 0

    def set_salient_sentences_by_key(
        self,
        collection: str,
        source_path: str,
        sentences: list[str],
    ) -> bool:
        """Pre-PK-migration fallback: target rows by
        ``(collection, source_path)``. Returns False when the
        ``salient_sentences`` column doesn't exist or no row matches.
        """
        if not self._has_salient_sentences_column():
            return False
        if not self._has_source_path_column():
            return False
        encoded = json.dumps(sentences, separators=(",", ":")) if sentences else "[]"
        with self._lock:
            cur = self.conn.execute(
                "UPDATE document_aspects "
                "SET salient_sentences = ? "
                "WHERE collection = ? AND source_path = ?",
                (encoded, collection, source_path),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def get_salient_sentences(self, doc_id: str) -> list[str]:
        """Return the salient sentences for *doc_id*, or ``[]`` when
        the column is absent / NULL / the row is missing."""
        if not self._has_salient_sentences_column():
            return []
        if not doc_id:
            return []
        with self._lock:
            row = self.conn.execute(
                "SELECT salient_sentences FROM document_aspects "
                "WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if not row or row[0] is None:
            return []
        try:
            value = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(value, list):
            return []
        return [str(s) for s in value if s]

    # ── Public API ────────────────────────────────────────────────────────

    def upsert(self, record: AspectRecord) -> bool:
        """Persist *record* — complete overwrite if the PK key already exists.

        Pre-migration: keyed on ``(collection, source_path)``.
        Post-migration (RDR-108 Phase 1c): keyed on ``doc_id``.

        Both forms require ``extracted_at``, ``model_version``, and
        ``extractor_name``. Post-migration writes should supply ``doc_id``.

        Uses ``INSERT OR REPLACE``. JSON fields serialize with
        ``json.dumps`` (no separator overrides — matches the rest of
        the project).

        nexus-17wf: silently DROPS records with ``confidence is None``
        or ``confidence < _MIN_CONFIDENCE`` (no row written, structured
        warning logged). The 2026-05-08 prod probe found 125 of 753
        rows (16.6%) committed with NULL or zero confidence, signaling
        extractor failures that downstream consumers (``nx aspects
        show``, retrieval ranking, telemetry) treated as authoritative.
        Per the project's no-silent-fallback principle for
        data-correctness problems, the right shape is reject + log so
        the data store stays clean. Returns True if the row was
        written, False if dropped on the confidence floor; callers
        that need to mark a queue row done either way ignore the
        return value.
        """
        if not record.extracted_at:
            raise ValueError("extracted_at must not be empty")
        if not record.model_version:
            raise ValueError("model_version must not be empty")
        if not record.extractor_name:
            raise ValueError("extractor_name must not be empty")

        # nexus-17wf: drop low-quality extractions before they pollute
        # the aspects table. The threshold is conservative; a real
        # extraction should clear it comfortably (2026-05-08 probe:
        # min=0, max=1.0, avg=0.823 across non-NULL rows).
        if record.confidence is None or record.confidence < _MIN_CONFIDENCE:
            _log.warning(
                "document_aspects_upsert_dropped_low_confidence",
                doc_id=record.doc_id,
                collection=record.collection,
                source_path=record.source_path,
                extractor_name=record.extractor_name,
                model_version=record.model_version,
                confidence=record.confidence,
                threshold=_MIN_CONFIDENCE,
            )
            return False

        # nexus-6xp2: source_uri is the post-drop addressing key. Auto-
        # derive it from (collection, source_path) when the writer didn't
        # supply one so post-drop reads can recover source_path via the
        # canonical URI shape.
        if not record.source_uri and record.collection and record.source_path:
            from nexus.aspect_readers import uri_for  # noqa: PLC0415
            derived_uri = uri_for(record.collection, record.source_path)
            if derived_uri:
                record = replace(record, source_uri=derived_uri)

        datasets_json = json.dumps(list(record.experimental_datasets))
        baselines_json = json.dumps(list(record.experimental_baselines))
        extras_json = json.dumps(dict(record.extras))

        if self._has_doc_id_pk():
            # Post-migration schema: doc_id is PK; collection + source_path are denorm.
            doc_id = record.doc_id or _resolve_doc_id(record)
            if not doc_id:
                raise ValueError(
                    "doc_id must not be empty on a migrated document_aspects "
                    "table and could not be derived from source_uri or "
                    "collection+source_path"
                )
            if doc_id != record.doc_id:
                _log.warning(
                    "document_aspects_upsert_synthesized_doc_id",
                    derived_doc_id=doc_id,
                    collection=record.collection,
                    source_path=record.source_path,
                    source_uri=record.source_uri,
                    extractor_name=record.extractor_name,
                )
                record = replace(record, doc_id=doc_id)
            # nexus-6xp2: omit source_path from INSERT once ocu9.11 drops it.
            has_sp = self._has_source_path_column()
            with self._lock:
                if has_sp:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO document_aspects "
                        "(doc_id, collection, source_path, problem_formulation, "
                        " proposed_method, experimental_datasets, "
                        " experimental_baselines, experimental_results, "
                        " extras, confidence, extracted_at, "
                        " model_version, extractor_name, source_uri) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.doc_id,
                            record.collection,
                            record.source_path,
                            record.problem_formulation,
                            record.proposed_method,
                            datasets_json,
                            baselines_json,
                            record.experimental_results,
                            extras_json,
                            record.confidence,
                            record.extracted_at,
                            record.model_version,
                            record.extractor_name,
                            record.source_uri,
                        ),
                    )
                else:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO document_aspects "
                        "(doc_id, collection, problem_formulation, "
                        " proposed_method, experimental_datasets, "
                        " experimental_baselines, experimental_results, "
                        " extras, confidence, extracted_at, "
                        " model_version, extractor_name, source_uri) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record.doc_id,
                            record.collection,
                            record.problem_formulation,
                            record.proposed_method,
                            datasets_json,
                            baselines_json,
                            record.experimental_results,
                            extras_json,
                            record.confidence,
                            record.extracted_at,
                            record.model_version,
                            record.extractor_name,
                            record.source_uri,
                        ),
                    )
                self.conn.commit()
        else:
            # Pre-migration schema: (collection, source_path) is PK.
            if not record.collection:
                raise ValueError("collection must not be empty")
            if not record.source_path:
                raise ValueError("source_path must not be empty")
            with self._lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO document_aspects "
                    "(collection, source_path, problem_formulation, "
                    " proposed_method, experimental_datasets, "
                    " experimental_baselines, experimental_results, "
                    " extras, confidence, extracted_at, "
                    " model_version, extractor_name, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.collection,
                        record.source_path,
                        record.problem_formulation,
                        record.proposed_method,
                        datasets_json,
                        baselines_json,
                        record.experimental_results,
                        extras_json,
                        record.confidence,
                        record.extracted_at,
                        record.model_version,
                        record.extractor_name,
                        record.source_uri,
                    ),
                )
                self.conn.commit()
        return True

    def get(self, collection: str, source_path: str) -> AspectRecord | None:
        """Return the row matching ``(collection, source_path)``, or None.

        Pre-drop: queries by ``(collection, source_path)`` directly.
        Post-drop (nexus-6xp2 / ocu9.11): re-derives the canonical
        ``source_uri`` via :func:`nexus.aspect_readers.uri_for` and
        queries by ``source_uri`` (always populated post-deprecation).
        Caller can switch to ``get_by_doc_id`` for tumbler-keyed reads.
        """
        sp_col = self._source_path_select()
        if self._has_source_path_column():
            sql = (
                f"SELECT collection, {sp_col}, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri "
                "FROM document_aspects "
                "WHERE collection = ? AND source_path = ?"
            )
            params: tuple = (collection, source_path)
        else:
            from nexus.aspect_readers import uri_for  # noqa: PLC0415
            uri = uri_for(collection, source_path)
            if not uri:
                return None
            sql = (
                f"SELECT collection, {sp_col}, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri "
                "FROM document_aspects "
                "WHERE collection = ? AND source_uri = ?"
            )
            params = (collection, uri)
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        record = _row_to_record(row)
        if not self._has_source_path_column():
            record = replace(record, source_path=source_path)
        return record

    def get_by_doc_id(self, doc_id: str) -> AspectRecord | None:
        """Return the row matching ``doc_id``, or None.

        Only valid on post-migration schema (RDR-108 Phase 1c) where
        ``doc_id`` is the primary key.  Returns None if the table has
        not been migrated or if the doc_id is not present.
        """
        if not self._has_doc_id_pk():
            return None
        sp_col = self._source_path_select()
        with self._lock:
            row = self.conn.execute(
                f"SELECT collection, {sp_col}, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri, doc_id "
                "FROM document_aspects "
                "WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        record = _row_to_record_with_doc_id(row)
        if not self._has_source_path_column():
            record = replace(
                record,
                source_path=_source_path_from_uri(record.source_uri, record.collection),
            )
        return record

    def list_by_collection(
        self,
        collection: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AspectRecord]:
        """Return all rows in ``collection``, paginated.

        ``limit=None`` returns every row; otherwise capped at ``limit``
        and starting at ``offset``. Order is ``source_path ASC`` for
        deterministic pagination across calls.
        """
        sp_col = self._source_path_select()
        order = "source_path ASC" if self._has_source_path_column() else "source_uri ASC"
        sql = (
            f"SELECT collection, {sp_col}, problem_formulation, "
            "       proposed_method, experimental_datasets, "
            "       experimental_baselines, experimental_results, "
            "       extras, confidence, extracted_at, "
            "       model_version, extractor_name, source_uri "
            "FROM document_aspects "
            "WHERE collection = ? "
            f"ORDER BY {order}"
        )
        params: tuple = (collection,)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (collection, limit, offset)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        records = [_row_to_record(r) for r in rows]
        # nexus-6xp2: post-drop, source_path projects to "". Recover it
        # from source_uri so renderers (aspects-list / info) still show
        # the path; falls back to "" when source_uri can't be parsed.
        if not self._has_source_path_column():
            records = [
                replace(r, source_path=_source_path_from_uri(r.source_uri, r.collection))
                for r in records
            ]
        return records

    def delete(self, collection: str, source_path: str) -> int:
        """Drop the row at ``(collection, source_path)``. Returns deleted
        row count (0 when absent — idempotent). Post-drop (nexus-6xp2)
        re-derives source_uri via :func:`uri_for` and deletes by URI.
        """
        if self._has_source_path_column():
            sql = (
                "DELETE FROM document_aspects "
                "WHERE collection = ? AND source_path = ?"
            )
            params: tuple = (collection, source_path)
        else:
            from nexus.aspect_readers import uri_for  # noqa: PLC0415
            uri = uri_for(collection, source_path)
            if not uri:
                return 0
            sql = (
                "DELETE FROM document_aspects "
                "WHERE collection = ? AND source_uri = ?"
            )
            params = (collection, uri)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def delete_orphans(
        self,
        catalog_db_path: Path | None,
        *,
        dry_run: bool = True,
    ) -> tuple[int, int]:
        """Delete document_aspects rows whose ``source_uri`` no longer
        appears in the catalog ``documents`` table (RDR-108 nexus-urj4).

        Aspects are extracted asynchronously after a document is
        registered. When the document is later deleted (via
        ``cat.delete_document``, source-file removal, etc.) the
        corresponding aspect rows are NOT cascaded today (the catalog
        and T2 live in separate SQLite files; cross-DB FK CASCADE is
        not supported by SQLite). This method is the periodic-sweep
        cleanup for the orphans that accumulate between extraction
        and deletion lifecycle events.

        Behavior:
          - Aspects whose ``source_uri`` is empty are NOT classified
            as orphans (legacy / pre-RDR-096 P2.1 rows; the operator
            must use the ``rename_collection`` or ``delete`` paths
            to address those).
          - Aspects whose ``source_uri`` matches at least one row in
            ``catalog.documents.source_uri`` are LIVE; not touched.
          - Everything else (non-empty source_uri, no match in catalog)
            is orphan; deleted unless ``dry_run=True``.

        Returns ``(orphan_count, total_examined)``. ``orphan_count`` is
        the number of orphans found (and deleted, when ``dry_run=False``);
        ``total_examined`` is the total non-empty-source_uri row count
        considered.

        Catalog-absent (``catalog_db_path is None`` or path does not
        exist) returns ``(0, 0)`` -- no orphans can be detected without
        the catalog as the live-set source of truth.
        """
        if catalog_db_path is None or not Path(catalog_db_path).exists():
            return (0, 0)

        with self._lock:
            self.conn.execute(f"ATTACH DATABASE ? AS cat", (str(catalog_db_path),))
            try:
                total = self.conn.execute(
                    "SELECT COUNT(*) FROM document_aspects "
                    "WHERE source_uri != ''"
                ).fetchone()[0]
                orphans = self.conn.execute(
                    "SELECT COUNT(*) FROM document_aspects da "
                    "WHERE da.source_uri != '' "
                    "  AND NOT EXISTS (SELECT 1 FROM cat.documents d "
                    "                  WHERE d.source_uri = da.source_uri)"
                ).fetchone()[0]
                if not dry_run and orphans > 0:
                    self.conn.execute(
                        "DELETE FROM document_aspects "
                        "WHERE source_uri != '' "
                        "  AND NOT EXISTS (SELECT 1 FROM cat.documents d "
                        "                  WHERE d.source_uri = document_aspects.source_uri)"
                    )
                    self.conn.commit()
            finally:
                self.conn.execute("DETACH DATABASE cat")
        return (int(orphans), int(total))

    def rename_collection(self, *, old: str, new: str) -> int:
        """Re-point every row's denorm ``collection`` cache from ``old`` to ``new``.

        nexus-gp20 / RDR-108 Phase 1d: ``collection`` is a denorm cache
        column (the primary key is ``doc_id`` post-migration, or
        ``(collection, source_path)`` on legacy tables). Updating the
        cache column does NOT affect the primary key either way -- no row
        identity changes, no row recreation.

        Collision defense (nexus-nhyh / K4): on legacy-PK tables where
        the PK is ``(collection, source_path)``, a pre-existing
        ``(new, source_path)`` row would collide with the UPDATE.
        Mirror chash_index's strategy: DELETE any conflicting new-side
        rows whose ``source_path`` values overlap with old-side rows,
        then UPDATE. This is conservative but correct: the rename is an
        atomic re-home, so preserving a stale ``new``-side row would
        silently drop the ``old``-side data.

        Returns the count of rows updated (0 when no rows match -- safe
        no-op). Idempotent: a second call with the same ``old`` name
        (now no rows match) returns 0 without error.
        """
        # nexus-6xp2: post-drop, dedupe by source_uri instead of source_path.
        if self._has_source_path_column():
            collide_sql = (
                "DELETE FROM document_aspects "
                "WHERE collection = ? "
                "  AND source_path IN ("
                "    SELECT source_path FROM document_aspects WHERE collection = ?"
                "  )"
            )
        else:
            collide_sql = (
                "DELETE FROM document_aspects "
                "WHERE collection = ? "
                "  AND source_uri IN ("
                "    SELECT source_uri FROM document_aspects WHERE collection = ?"
                "  )"
            )
        with self._lock:
            self.conn.execute(collide_sql, (new, old))
            cur = self.conn.execute(
                "UPDATE document_aspects SET collection = ? WHERE collection = ?",
                (new, old),
            )
            self.conn.commit()
            return cur.rowcount

    def list_by_extractor_version(
        self,
        extractor_name: str,
        max_version: str,
    ) -> list[AspectRecord]:
        """Return rows whose ``extractor_name`` matches and
        ``model_version`` is STRICTLY less than ``max_version``.

        Used by re-extraction logic to find documents whose aspects
        were captured by an older model and should be re-run.
        Strict ``<`` (not ``<=``) so the threshold version is not
        repeatedly re-extracted.

        Comparison is **lexicographic TEXT** ordering in SQLite — the
        caller is responsible for using version strings that sort
        lexicographically in the same order as their semantic order.
        The ``claude-haiku-4-5-20251001`` slug format used by
        ``scholarly-paper-v1`` satisfies this (the numeric tail is
        zero-padded enough for the date suffix to dominate). A future
        slug like ``claude-haiku-4-10`` would NOT — ``"4-10"`` sorts
        BEFORE ``"4-5"`` lexicographically. New extractor configs
        must either keep the constraint or factor a semver-aware
        comparator before relying on this method for re-extraction
        triage.
        """
        sp_col = self._source_path_select()
        order_tail = "source_path" if self._has_source_path_column() else "source_uri"
        with self._lock:
            rows = self.conn.execute(
                f"SELECT collection, {sp_col}, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri "
                "FROM document_aspects "
                "WHERE extractor_name = ? AND model_version < ? "
                f"ORDER BY collection, {order_tail}",
                (extractor_name, max_version),
            ).fetchall()
        records = [_row_to_record(r) for r in rows]
        if not self._has_source_path_column():
            records = [
                replace(r, source_path=_source_path_from_uri(r.source_uri, r.collection))
                for r in records
            ]
        return records

    # ── schema-evolution helpers (RDR-112 P0-gate, nexus-mz2c) ──────────────
    #
    # Used by ``nexus.aspect_promotion`` to graduate JSON ``extras`` keys
    # into their own typed columns. All four methods encapsulate
    # operations the aspect_promotion module previously inlined via
    # ``db.document_aspects.conn.execute(...)``.

    def alter_add_column_if_missing(
        self, *, field_name: str, sql_type: str,
    ) -> bool:
        """``ALTER TABLE document_aspects ADD COLUMN <name> <type>``.

        Idempotent — returns True iff the column was added (False if it
        already existed). The caller is responsible for validating
        ``field_name`` and ``sql_type`` against an allowlist; this method
        does not re-validate because it composes raw SQL.
        """
        with self._lock:
            cols = {
                r[1] for r in self.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            if field_name in cols:
                return False
            self.conn.execute(
                f"ALTER TABLE document_aspects "
                f"ADD COLUMN {field_name} {sql_type}"
            )
            self.conn.commit()
        return True

    def backfill_extras_column(self, *, field_name: str) -> int:
        """Copy ``extras['$.<field>']`` into the typed ``<field>``
        column for rows where the typed column is currently NULL.

        Returns the affected row count. Caller validates ``field_name``.
        """
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE document_aspects "
                f"SET {field_name} = json_extract(extras, ?) "
                f"WHERE {field_name} IS NULL "
                f"  AND json_extract(extras, ?) IS NOT NULL",
                (f"$.{field_name}", f"$.{field_name}"),
            )
            self.conn.commit()
        return cur.rowcount or 0

    def prune_extras_key(self, *, field_name: str) -> int:
        """Remove ``<field>`` from the ``extras`` JSON blob across all rows.

        Returns the affected row count. Caller decides whether prune
        is safe (every reader must already consume the typed column).
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE document_aspects "
                "SET extras = json_remove(extras, ?) "
                "WHERE json_extract(extras, ?) IS NOT NULL",
                (f"$.{field_name}", f"$.{field_name}"),
            )
            self.conn.commit()
        return cur.rowcount or 0

    def _ensure_promotion_log_table(self) -> None:
        """Create ``aspect_promotion_log`` lazily (idempotent)."""
        with self._lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS aspect_promotion_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    field_name      TEXT NOT NULL,
                    sql_type        TEXT NOT NULL,
                    column_added    INTEGER NOT NULL,
                    rows_backfilled INTEGER NOT NULL DEFAULT 0,
                    rows_pruned     INTEGER NOT NULL DEFAULT 0,
                    pruned          INTEGER NOT NULL DEFAULT 0,
                    promoted_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_aspect_promotion_log_field
                    ON aspect_promotion_log(field_name);
            """)
            self.conn.commit()

    def record_promotion_audit(
        self,
        *,
        field_name: str,
        sql_type: str,
        column_added: bool,
        rows_backfilled: int,
        rows_pruned: int,
        pruned: bool,
        promoted_at: str,
    ) -> None:
        """Insert one row into ``aspect_promotion_log`` (best-effort)."""
        self._ensure_promotion_log_table()
        with self._lock:
            self.conn.execute(
                "INSERT INTO aspect_promotion_log "
                "(field_name, sql_type, column_added, "
                " rows_backfilled, rows_pruned, pruned, promoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    field_name, sql_type,
                    1 if column_added else 0,
                    rows_backfilled, rows_pruned,
                    1 if pruned else 0,
                    promoted_at,
                ),
            )
            self.conn.commit()

    def list_promotion_audit(self) -> list[dict]:
        """Full ``aspect_promotion_log`` history, oldest first."""
        self._ensure_promotion_log_table()
        with self._lock:
            rows = self.conn.execute(
                "SELECT field_name, sql_type, column_added, rows_backfilled, "
                "       rows_pruned, pruned, promoted_at "
                "FROM aspect_promotion_log "
                "ORDER BY promoted_at ASC, id ASC"
            ).fetchall()
        return [
            {
                "field_name": r[0],
                "sql_type": r[1],
                "column_added": bool(r[2]),
                "rows_backfilled": r[3],
                "rows_pruned": r[4],
                "pruned": bool(r[5]),
                "promoted_at": r[6],
            }
            for r in rows
        ]

    # ── paginated operator helpers (RDR-112 P0.5, nexus-xcji) ───────────────
    #
    # Each method below replaces a ``db.document_aspects.conn.execute``
    # reach-through from ``src/nexus/operators/aspect_sql.py``. They
    # share the 300-uri batching to stay under SQLite's 999-param cap.

    def filter_uris_by_predicate(
        self,
        uris: list[str],
        *,
        where_sql: str,
        where_params: tuple | list,
        batch_size: int = 300,
    ) -> set[str]:
        """Return the subset of ``uris`` whose row matches ``where_sql``.

        Executes ``SELECT source_uri FROM document_aspects WHERE
        source_uri IN (<placeholders>) AND <where_sql>`` in batches of
        ``batch_size``. ``where_sql`` and ``where_params`` are passed
        through unchanged; the caller is responsible for validating
        column names (they typically come from a fixed allowlist).
        """
        matched: set[str] = set()
        if not uris:
            return matched
        with self._lock:
            for chunk_start in range(0, len(uris), batch_size):
                batch = uris[chunk_start:chunk_start + batch_size]
                placeholders = ",".join("?" * len(batch))
                sql = (
                    f"SELECT source_uri FROM document_aspects "
                    f"WHERE source_uri IN ({placeholders}) AND {where_sql}"
                )
                params = list(batch) + list(where_params)
                for (uri,) in self.conn.execute(sql, params).fetchall():
                    matched.add(uri)
        return matched

    def select_field_by_uris(
        self,
        uris: list[str],
        *,
        select_expr: str,
        select_params: tuple | list = (),
        batch_size: int = 300,
    ) -> dict[str, Any]:
        """Return ``{source_uri: value}`` for each uri whose row exists.

        ``select_expr`` may be a column name or a SQL expression such
        as ``json_extract(extras, ?)``; ``select_params`` supplies its
        parameters. The caller validates ``select_expr`` from a fixed
        allowlist.
        """
        out: dict[str, Any] = {}
        if not uris:
            return out
        with self._lock:
            for chunk_start in range(0, len(uris), batch_size):
                batch = uris[chunk_start:chunk_start + batch_size]
                placeholders = ",".join("?" * len(batch))
                sql = (
                    f"SELECT source_uri, {select_expr} "
                    f"FROM document_aspects "
                    f"WHERE source_uri IN ({placeholders})"
                )
                params = list(select_params) + list(batch)
                for uri, value in self.conn.execute(sql, params).fetchall():
                    out[uri] = value
        return out

    def fold_confidence_by_uris(
        self,
        uris: list[str],
        *,
        batch_size: int = 300,
    ) -> tuple[float, float | None, float | None, int]:
        """Single-pass fold over ``confidence`` for rows whose
        ``source_uri`` is in ``uris``.

        Returns ``(sum, min, max, count)``. ``min`` and ``max`` are
        ``None`` when ``count == 0``. NULL confidences are skipped.
        Callers compute the appropriate scalar reducer from the tuple.
        """
        sum_acc = 0.0
        count_acc = 0
        min_acc: float | None = None
        max_acc: float | None = None
        if not uris:
            return (0.0, None, None, 0)
        with self._lock:
            for chunk_start in range(0, len(uris), batch_size):
                batch = uris[chunk_start:chunk_start + batch_size]
                placeholders = ",".join("?" * len(batch))
                sql = (
                    f"SELECT confidence FROM document_aspects "
                    f"WHERE source_uri IN ({placeholders}) "
                    f"AND confidence IS NOT NULL"
                )
                for (value,) in self.conn.execute(sql, list(batch)).fetchall():
                    if value is None:
                        continue
                    v = float(value)
                    sum_acc += v
                    count_acc += 1
                    min_acc = v if min_acc is None else min(min_acc, v)
                    max_acc = v if max_acc is None else max(max_acc, v)
        return (sum_acc, min_acc, max_acc, count_acc)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _source_path_from_uri(uri: str | None, collection: str) -> str:
    """Recover the source_path embedded in a canonical aspect source_uri.

    Inverse of :func:`nexus.aspect_readers.uri_for`.
      - ``file://<abs>`` → ``<abs>``
      - ``chroma://<coll>/<rest>`` → ``<rest>``
      - anything else (or empty) → ``""`` so renderers don't crash.
    """
    if not uri:
        return ""
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if uri.startswith("chroma://"):
        rest = uri[len("chroma://"):]
        prefix = collection + "/"
        if rest.startswith(prefix):
            return rest[len(prefix):]
        # ``chroma://<rest>`` without explicit collection prefix.
        return rest
    return ""


def _row_to_record(row: tuple) -> AspectRecord:
    """Inflate a 13-column SELECT-result tuple into an ``AspectRecord``.

    JSON columns deserialize on read; missing or NULL JSON columns
    yield empty list/dict so callers never need a None-check.

    Column order must match the SELECT in ``get()`` and
    ``list_by_collection()``:
      collection, source_path, problem_formulation, proposed_method,
      experimental_datasets, experimental_baselines, experimental_results,
      extras, confidence, extracted_at, model_version, extractor_name,
      source_uri
    """
    (
        collection,
        source_path,
        problem_formulation,
        proposed_method,
        datasets_json,
        baselines_json,
        experimental_results,
        extras_json,
        confidence,
        extracted_at,
        model_version,
        extractor_name,
        source_uri,
    ) = row
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=problem_formulation,
        proposed_method=proposed_method,
        experimental_datasets=_safe_json_list(datasets_json),
        experimental_baselines=_safe_json_list(baselines_json),
        experimental_results=experimental_results,
        extras=_safe_json_dict(extras_json),
        confidence=confidence,
        extracted_at=extracted_at,
        model_version=model_version,
        extractor_name=extractor_name,
        source_uri=source_uri,
    )


def _row_to_record_with_doc_id(row: tuple) -> AspectRecord:
    """Inflate a 14-column SELECT-result tuple (includes doc_id) into an
    ``AspectRecord``.

    Used by ``get_by_doc_id()`` which SELECTs the ``doc_id`` column as the
    14th positional column.
    """
    (
        collection,
        source_path,
        problem_formulation,
        proposed_method,
        datasets_json,
        baselines_json,
        experimental_results,
        extras_json,
        confidence,
        extracted_at,
        model_version,
        extractor_name,
        source_uri,
        doc_id,
    ) = row
    return AspectRecord(
        collection=collection,
        source_path=source_path,
        problem_formulation=problem_formulation,
        proposed_method=proposed_method,
        experimental_datasets=_safe_json_list(datasets_json),
        experimental_baselines=_safe_json_list(baselines_json),
        experimental_results=experimental_results,
        extras=_safe_json_dict(extras_json),
        confidence=confidence,
        extracted_at=extracted_at,
        model_version=model_version,
        extractor_name=extractor_name,
        source_uri=source_uri,
        doc_id=doc_id or "",
    )


def _safe_json_list(s: str | None) -> list:
    if not s:
        return []
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return []
    return v if isinstance(v, list) else []


def _safe_json_dict(s: str | None) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
    except (ValueError, TypeError):
        return {}
    return v if isinstance(v, dict) else {}
