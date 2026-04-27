# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DocumentAspects — T2 store for structured per-document aspects (RDR-089).

Owns one SQLite table, ``document_aspects``, holding the canonical
extracted aspects for each document. Each row is a *complete* snapshot
of the latest extraction:

    PRIMARY KEY (collection, source_path)

Per-chunk doc_id is intentionally not in the schema — multiple chunks
of the same source document map to a single aspect row.

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
from dataclasses import dataclass, field
from pathlib import Path

import structlog

_log = structlog.get_logger()


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

    # ── Public API ────────────────────────────────────────────────────────

    def upsert(self, record: AspectRecord) -> None:
        """Persist *record* — complete overwrite if (collection, source_path)
        already present.

        Uses ``INSERT OR REPLACE``. JSON fields serialize with
        ``json.dumps`` (no separator overrides — matches the rest of
        the project).
        """
        if not record.collection:
            raise ValueError("collection must not be empty")
        if not record.source_path:
            raise ValueError("source_path must not be empty")
        if not record.extracted_at:
            raise ValueError("extracted_at must not be empty")
        if not record.model_version:
            raise ValueError("model_version must not be empty")
        if not record.extractor_name:
            raise ValueError("extractor_name must not be empty")

        datasets_json = json.dumps(list(record.experimental_datasets))
        baselines_json = json.dumps(list(record.experimental_baselines))
        extras_json = json.dumps(dict(record.extras))

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

    def get(self, collection: str, source_path: str) -> AspectRecord | None:
        """Return the row matching ``(collection, source_path)``, or None."""
        with self._lock:
            row = self.conn.execute(
                "SELECT collection, source_path, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri "
                "FROM document_aspects "
                "WHERE collection = ? AND source_path = ?",
                (collection, source_path),
            ).fetchone()
        if row is None:
            return None
        return _row_to_record(row)

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
        sql = (
            "SELECT collection, source_path, problem_formulation, "
            "       proposed_method, experimental_datasets, "
            "       experimental_baselines, experimental_results, "
            "       extras, confidence, extracted_at, "
            "       model_version, extractor_name, source_uri "
            "FROM document_aspects "
            "WHERE collection = ? "
            "ORDER BY source_path ASC"
        )
        params: tuple = (collection,)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (collection, limit, offset)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def delete(self, collection: str, source_path: str) -> int:
        """Drop the row at ``(collection, source_path)``. Returns deleted
        row count (0 when absent — idempotent).
        """
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM document_aspects "
                "WHERE collection = ? AND source_path = ?",
                (collection, source_path),
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
        with self._lock:
            rows = self.conn.execute(
                "SELECT collection, source_path, problem_formulation, "
                "       proposed_method, experimental_datasets, "
                "       experimental_baselines, experimental_results, "
                "       extras, confidence, extracted_at, "
                "       model_version, extractor_name, source_uri "
                "FROM document_aspects "
                "WHERE extractor_name = ? AND model_version < ? "
                "ORDER BY collection, source_path",
                (extractor_name, max_version),
            ).fetchall()
        return [_row_to_record(r) for r in rows]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _row_to_record(row: tuple) -> AspectRecord:
    """Inflate a SELECT-result tuple into an ``AspectRecord``.

    JSON columns deserialize on read; missing or NULL JSON columns
    yield empty list/dict so callers never need a None-check.
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
