# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""PipelineDB — SQLite buffer for streaming PDF pipeline (RDR-048).

Stores extracted pages, chunks, and pipeline state in a WAL-mode SQLite
database with per-thread connections via ``threading.local()``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Stale threshold: a pipeline with no heartbeat for this long is considered crashed.
STALE_THRESHOLD = timedelta(minutes=5)

# Canonical database path (parallel to T2's nexus.db).
PIPELINE_DB_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "nexus"
    / "pipeline.db"
)

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS pdf_pages (
    content_hash  TEXT NOT NULL,
    page_index    INTEGER NOT NULL,
    page_text     TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    created_at    TEXT NOT NULL,
    PRIMARY KEY (content_hash, page_index)
);

CREATE TABLE IF NOT EXISTS pdf_chunks (
    content_hash  TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    chunk_id      TEXT NOT NULL,
    metadata_json TEXT DEFAULT '{}',
    embedding     BLOB DEFAULT NULL,
    uploaded      INTEGER DEFAULT 0,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (content_hash, chunk_index)
);

CREATE TABLE IF NOT EXISTS pdf_pipeline (
    content_hash     TEXT PRIMARY KEY,
    pdf_path         TEXT NOT NULL,
    collection       TEXT NOT NULL,
    total_pages      INTEGER,
    pages_extracted  INTEGER DEFAULT 0,
    chunks_created   INTEGER,   -- NULL = chunking not started; set explicitly by chunker
    chunks_embedded  INTEGER,   -- NULL = embedding not started
    chunks_uploaded  INTEGER DEFAULT 0,
    status           TEXT DEFAULT 'running',
    error            TEXT DEFAULT '',
    started_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PipelineDB:
    """SQLite buffer for the streaming PDF pipeline.

    Each thread gets its own ``sqlite3.Connection`` via ``threading.local()``.
    WAL mode is enabled on every new connection for concurrent reads.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Initialize schema on the creating thread's connection.
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        """Return (or create) the per-thread connection."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            _log.warning("WAL mode not available", actual_mode=mode)

    # ── Pipeline state ───────────────────────────────────────────────────────

    def create_pipeline(
        self, content_hash: str, pdf_path: str, collection: str
    ) -> str:
        """Register a new pipeline run.

        Returns:
            ``'created'`` — new pipeline inserted.
            ``'resuming'`` — existing failed/stale pipeline, reset to running.
            ``'skip'`` — already running (recent heartbeat) or completed.
        """
        conn = self._conn()
        row = conn.execute(
            "SELECT status, updated_at FROM pdf_pipeline WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()

        now = _now()

        if row is None:
            conn.execute(
                "INSERT INTO pdf_pipeline "
                "(content_hash, pdf_path, collection, status, started_at, updated_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (content_hash, pdf_path, collection, now, now),
            )
            conn.commit()
            return "created"

        status = row["status"]

        if status == "completed":
            return "skip"

        if status == "failed":
            conn.execute(
                "UPDATE pdf_pipeline SET status = 'running', updated_at = ? "
                "WHERE content_hash = ?",
                (now, content_hash),
            )
            conn.commit()
            return "resuming"

        # status == 'running' (or 'resuming') — check staleness
        updated_at = datetime.fromisoformat(row["updated_at"])
        if datetime.now(UTC) - updated_at > STALE_THRESHOLD:
            conn.execute(
                "UPDATE pdf_pipeline SET status = 'running', updated_at = ? "
                "WHERE content_hash = ?",
                (now, content_hash),
            )
            conn.commit()
            return "resuming"

        return "skip"

    def get_pipeline_state(self, content_hash: str) -> dict[str, Any] | None:
        """Return pipeline row as a dict, or ``None`` if not found."""
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM pdf_pipeline WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def update_progress(self, content_hash: str, **fields: int) -> None:
        """Update numeric progress counters and refresh the heartbeat.

        Accepted keyword arguments: ``pages_extracted``, ``chunks_created``,
        ``chunks_embedded``, ``chunks_uploaded``.
        """
        allowed = {"total_pages", "pages_extracted", "chunks_created", "chunks_embedded", "chunks_uploaded"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unknown progress fields: {bad}")
        if not fields:
            return

        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values())
        vals.append(_now())
        vals.append(content_hash)

        conn = self._conn()
        conn.execute(
            f"UPDATE pdf_pipeline SET {sets}, updated_at = ? WHERE content_hash = ?",
            vals,
        )
        conn.commit()

    # ── Page CRUD ────────────────────────────────────────────────────────────

    def write_page(
        self,
        content_hash: str,
        page_index: int,
        page_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Write (or replace) a single extracted page."""
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO pdf_pages "
            "(content_hash, page_index, page_text, metadata_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (content_hash, page_index, page_text, json.dumps(metadata or {}), _now()),
        )
        conn.commit()

    def read_pages(self, content_hash: str) -> list[dict[str, Any]]:
        """Return all pages for a content_hash, ordered by page_index."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM pdf_pages WHERE content_hash = ? ORDER BY page_index",
            (content_hash,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Chunk CRUD ───────────────────────────────────────────────────────────

    def write_chunk(
        self,
        content_hash: str,
        chunk_index: int,
        chunk_text: str,
        chunk_id: str,
        metadata: dict[str, Any] | None = None,
        embedding: bytes | None = None,
    ) -> None:
        """Write a chunk, skipping if it already exists (idempotent resume).

        Uses INSERT OR IGNORE so that an existing row (which may already have
        an embedding written by a concurrent embed step) is never overwritten.
        """
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO pdf_chunks "
            "(content_hash, chunk_index, chunk_text, chunk_id, metadata_json, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content_hash, chunk_index, chunk_text, chunk_id, json.dumps(metadata or {}), embedding, _now()),
        )
        conn.commit()

    def read_ready_chunks(self, content_hash: str) -> list[dict[str, Any]]:
        """Return chunks not yet uploaded, ordered by chunk_index."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM pdf_chunks "
            "WHERE content_hash = ? AND uploaded = 0 "
            "ORDER BY chunk_index",
            (content_hash,),
        ).fetchall()
        return [dict(r) for r in rows]

    def read_uploadable_chunks(self, content_hash: str) -> list[dict[str, Any]]:
        """Return chunks with embeddings that are not yet uploaded."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM pdf_chunks "
            "WHERE content_hash = ? AND embedding IS NOT NULL AND uploaded = 0 "
            "ORDER BY chunk_index",
            (content_hash,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_uploaded(self, content_hash: str, chunk_indices: list[int]) -> None:
        """Mark specific chunks as uploaded."""
        if not chunk_indices:
            return
        conn = self._conn()
        placeholders = ",".join("?" for _ in chunk_indices)
        conn.execute(
            f"UPDATE pdf_chunks SET uploaded = 1 "
            f"WHERE content_hash = ? AND chunk_index IN ({placeholders})",
            [content_hash, *chunk_indices],
        )
        conn.commit()

    def mark_completed(self, content_hash: str) -> None:
        """Set pipeline status to ``'completed'``."""
        conn = self._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET status = 'completed', updated_at = ? WHERE content_hash = ?",
            (_now(), content_hash),
        )
        conn.commit()

    def mark_failed(self, content_hash: str, error: str = "") -> None:
        """Set pipeline status to ``'failed'`` with optional error message."""
        conn = self._conn()
        conn.execute(
            "UPDATE pdf_pipeline SET status = 'failed', error = ?, updated_at = ? WHERE content_hash = ?",
            (error, _now(), content_hash),
        )
        conn.commit()

    def count_pipelines(self) -> int:
        """Return the total number of pipeline entries."""
        return self._conn().execute("SELECT COUNT(*) FROM pdf_pipeline").fetchone()[0]

    def count_embedded_chunks(self, content_hash: str) -> int:
        """Return the count of chunks with embeddings (both uploaded and not)."""
        return self._conn().execute(
            "SELECT COUNT(*) FROM pdf_chunks WHERE content_hash = ? AND embedding IS NOT NULL",
            (content_hash,),
        ).fetchone()[0]

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def scan_orphaned_pipelines(self, *, delete: bool = False) -> list[str]:
        """Scan for orphaned pipeline entries.

        An entry is orphaned when:
        1. ``pdf_path`` no longer exists on disk (file moved/deleted).
        2. ``status='running'`` and ``updated_at`` is older than the stale
           threshold (crashed pipeline).

        Content-hash re-verification is intentionally skipped (existence
        check only — re-hashing every PDF is prohibitively expensive,
        matching the ``scan_orphaned_checkpoints`` policy).

        When *delete* is True, removes all data (pages, chunks, pipeline
        row) for each orphan.
        """
        conn = self._conn()
        rows = conn.execute(
            "SELECT content_hash, pdf_path, status, updated_at FROM pdf_pipeline"
        ).fetchall()

        now = datetime.now(UTC)
        orphans: list[str] = []

        for row in rows:
            content_hash = row["content_hash"]
            pdf_path = row["pdf_path"]
            status = row["status"]

            # Case 1: PDF file no longer exists.
            if not Path(pdf_path).exists():
                orphans.append(content_hash)
                if delete:
                    self.delete_pipeline_data(content_hash)
                continue

            # Case 2: Stale running pipeline (crashed).
            # Note: status='failed' with existing PDF is intentionally NOT orphaned —
            # failed pipelines are reset to 'running' on the next create_pipeline() call.
            if status == "running":
                updated_at = datetime.fromisoformat(row["updated_at"])
                if now - updated_at > STALE_THRESHOLD:
                    orphans.append(content_hash)
                    if delete:
                        self.delete_pipeline_data(content_hash)

        return orphans

    def delete_pipeline_data(self, content_hash: str) -> None:
        """Remove all data for a content_hash across all three tables."""
        conn = self._conn()
        conn.execute("DELETE FROM pdf_pages WHERE content_hash = ?", (content_hash,))
        conn.execute("DELETE FROM pdf_chunks WHERE content_hash = ?", (content_hash,))
        conn.execute("DELETE FROM pdf_pipeline WHERE content_hash = ?", (content_hash,))
        conn.commit()
