# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DocumentHighlights — T2 store for DEVONthink highlight/mention notes (RDR-139 Layer E).

Owns one SQLite table, ``document_highlights``, holding the markdown blob that
DEVONthink's ``extract_record_highlights`` (and ``extract_record_mentions``)
produce for a record, keyed by the document's catalog tumbler:

    PRIMARY KEY (doc_id)

This is deliberately SEPARATE from ``document_aspects``. Highlights are
user-authored annotations rendered as a single markdown blob, not the
scholarly-paper structured fields the aspect extractor produces. Folding them
into ``document_aspects`` would (a) overload a confidence-gated, scholarly-
shaped table with free-text notes, and (b) contend with the aspect worker's
whole-row ``INSERT OR REPLACE`` (the RDR-128 single-writer hazard) — a Layer E
ingest and an aspect re-extraction would clobber each other. A dedicated table
sidesteps both.

Upsert semantics: COMPLETE IDEMPOTENT OVERWRITE keyed on ``doc_id`` — a fresh
Layer E ingest replaces the prior blob verbatim. A record with neither a
highlights blob nor a mentions blob is a no-op (nothing to store).

The schema is duplicated here as ``CREATE IF NOT EXISTS`` so a fresh store
construction creates the table before any migration runs (mirrors
``DocumentAspects``). Identical shape to the migration entry.

Lock convention (mirrors ``DocumentAspects`` / ``ChashIndex``):
  * Public methods acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import structlog

from nexus.db.t2._tuning import SERVING_BUSY_TIMEOUT_MS

_log = structlog.get_logger()


_DOCUMENT_HIGHLIGHTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS document_highlights (
    doc_id        TEXT PRIMARY KEY,
    source_uri    TEXT,
    collection    TEXT,
    highlights_md TEXT,
    mentions_md   TEXT,
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_highlights_source_uri
    ON document_highlights(source_uri);
"""


@dataclass
class HighlightRecord:
    """A document's DEVONthink-sourced highlight + mention notes (RDR-139 Layer E).

    ``doc_id`` is the catalog tumbler of the source document. ``source_uri`` is
    the ``x-devonthink-item://<uuid>`` identity. ``highlights_md`` /
    ``mentions_md`` are the markdown blobs from ``extract_record_highlights`` /
    ``extract_record_mentions`` (either may be empty).
    """

    doc_id: str
    source_uri: str
    collection: str
    highlights_md: str
    mentions_md: str
    ingested_at: str


def _row_to_record(row: sqlite3.Row | tuple) -> HighlightRecord:
    return HighlightRecord(
        doc_id=row[0],
        source_uri=row[1] or "",
        collection=row[2] or "",
        highlights_md=row[3] or "",
        mentions_md=row[4] or "",
        ingested_at=row[5] or "",
    )


_SELECT = (
    "SELECT doc_id, source_uri, collection, highlights_md, mentions_md, "
    "ingested_at FROM document_highlights"
)


class DocumentHighlights:
    """T2 store for per-document DEVONthink highlight/mention notes (RDR-139 Layer E)."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(f"PRAGMA busy_timeout={SERVING_BUSY_TIMEOUT_MS}")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.executescript(_DOCUMENT_HIGHLIGHTS_SCHEMA_SQL)
            self.conn.commit()

    def upsert(self, record: HighlightRecord) -> bool:
        """Persist *record* — complete overwrite if ``doc_id`` already exists.

        Returns ``True`` when a row was written, ``False`` when the record
        carried neither a highlights blob nor a mentions blob (nothing to
        store). Raises ``ValueError`` on an empty ``doc_id``.
        """
        if not record.doc_id:
            raise ValueError("doc_id must not be empty")
        if not record.ingested_at:
            raise ValueError("ingested_at must not be empty")
        if not (record.highlights_md or record.mentions_md):
            return False
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO document_highlights "
                "(doc_id, source_uri, collection, highlights_md, mentions_md, "
                " ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.doc_id,
                    record.source_uri,
                    record.collection,
                    record.highlights_md,
                    record.mentions_md,
                    record.ingested_at,
                ),
            )
            self.conn.commit()
        return True

    def get(self, doc_id: str) -> HighlightRecord | None:
        with self._lock:
            row = self.conn.execute(
                f"{_SELECT} WHERE doc_id=?", (doc_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_by_source_uri(self, source_uri: str) -> HighlightRecord | None:
        with self._lock:
            row = self.conn.execute(
                f"{_SELECT} WHERE source_uri=? LIMIT 1", (source_uri,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list(self, *, limit: int = 50, offset: int = 0) -> list[HighlightRecord]:
        with self._lock:
            rows = self.conn.execute(
                f"{_SELECT} ORDER BY ingested_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM document_highlights WHERE doc_id=?", (doc_id,)
            )
            self.conn.commit()
            return cur.rowcount > 0
