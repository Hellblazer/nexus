# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres memory ETL (bead nexus-gmiaf.8, RDR-152 Phase 1.8).

COPY-NOT-MOVE: reads all rows from the SQLite ``memory`` table and writes
them through the validated HTTP seam (``HttpMemoryStore.put``) so every
write flows via Java -> jOOQ -> Postgres under RLS with tenant stamping.
The SQLite source is NEVER modified.

IDEMPOTENT: relies on the upsert ``ON CONFLICT (tenant_id, project, title)
DO UPDATE`` that the Java service enforces via ``MemoryRepository.doUpsert``.
Re-running the ETL produces the same row count; the last-write wins for
content/tags/ttl.

TENANT STAMPING: the ``HttpMemoryStore`` is constructed with
``tenant=DEFAULT_TENANT`` (``"default"``); the service stamps
``tenant_id`` from the ``X-Nexus-Tenant`` request header, so every
migrated row lands under the correct RLS principal without the ETL
touching ``tenant_id`` directly.

FIELD MAPPING (SQLite -> put() kwargs):
  project       -> project      (TEXT NOT NULL)
  title         -> title        (TEXT NOT NULL)
  content       -> content      (TEXT NOT NULL)
  tags          -> tags         (TEXT; '' preserved, NULL -> '')
  ttl           -> ttl          (INTEGER nullable)
  session       -> session      (TEXT nullable; None -> not passed)
  agent         -> agent        (TEXT nullable; None -> not passed)
  id            NOT copied      (PG uses its own BIGSERIAL)
  timestamp     NOT passed      (doUpsert sets timestamp=now on every upsert)
  access_count  NOT passed      (doUpsert sets access_count=0 on new insert,
                                 preserves existing on conflict-update)
  last_accessed NOT passed      (service-managed; NULL on new insert)

The ``_transform_row`` function documents the full mapping logic and is
exposed for unit-testing.  ``migrate_memory_rows`` is the public entry
point used by the CLI and integration tests.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Column order matches ``_COLUMNS`` in memory_store.py — used when reading
# rows via ``SELECT *``.
_COLUMNS = (
    "id",
    "project",
    "title",
    "session",
    "agent",
    "content",
    "tags",
    "timestamp",
    "ttl",
    "access_count",
    "last_accessed",
)


def _transform_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite memory row dict to ``HttpMemoryStore.put()`` kwargs.

    Rules:
    - ``id`` is NOT included (PG uses its own BIGSERIAL).
    - ``tags``: ``None`` normalised to ``""``; empty string preserved.
    - ``last_accessed``: empty string ``""`` normalised to ``None``
      (PG stores this as TIMESTAMPTZ nullable; ``''`` is the SQLite
      column default but has no meaning as a timestamp).
    - ``session``, ``agent``: ``None`` is passed through as ``None``
      (``put()`` only adds them to the payload when non-None).
    - ``timestamp``: NOT included — ``doUpsert`` sets ``timestamp=now``
      on every write (the service owns the authoritative timestamp).
    - ``access_count``: NOT included — ``doUpsert`` manages access_count
      server-side (0 on new insert; the on-conflict UPDATE does not touch
      access_count, preserving whatever the server has accumulated).

    Returns a dict suitable for unpacking into ``store.put(**kwargs)``.
    """
    tags = row.get("tags") or ""
    last_accessed_raw = row.get("last_accessed", "")
    last_accessed: str | None = last_accessed_raw if last_accessed_raw else None

    return {
        "project":      row["project"],
        "title":        row["title"],
        "content":      row["content"],
        "tags":         tags,
        "ttl":          row.get("ttl"),
        "session":      row.get("session"),
        "agent":        row.get("agent"),
        # last_accessed is included in the transform output for documentation /
        # future bulk-import endpoints; migrate_memory_rows does NOT pass it to
        # the current put() API which has no such parameter.
        "last_accessed": last_accessed,
    }


def migrate_memory_rows(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
) -> dict[str, int]:
    """Copy all rows from a SQLite memory table into Postgres via *store*.

    Args:
        source_db_path: Path to the SQLite T2 database file.
        store: An ``HttpMemoryStore`` (or compatible duck-typed) instance
               connected to the Postgres service.  Receives one
               ``put(project, title, content, tags, ttl, agent, session)``
               call per source row.
        batch_log_every: Emit a progress log line every N rows.

    Returns:
        ``{"read": N, "written": M}`` — always ``read == written`` for
        a healthy run; differences indicate per-row failures (logged).

    Copy-not-move guarantee: the SQLite connection is opened in
    ``uri=True`` mode with ``?mode=ro`` so the file is opened read-only
    at the OS level; even a bug in this function cannot modify the source.
    """
    read_count = 0
    written_count = 0

    # Open SQLite read-only via the URI file name syntax (mode=ro prevents
    # any writes even if code is accidentally changed later).
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM memory ORDER BY id ASC")
        rows = cursor.fetchall()
    finally:
        conn.close()

    total = len(rows)
    _log.info(
        "memory_etl.start",
        source=str(source_db_path),
        total_rows=total,
    )

    for row_dict in (dict(r) for r in rows):
        read_count += 1
        transformed = _transform_row(row_dict)

        # Build put() kwargs — exclude last_accessed which put() doesn't accept.
        put_kwargs: dict[str, Any] = {
            "project": transformed["project"],
            "title":   transformed["title"],
            "content": transformed["content"],
            "tags":    transformed["tags"],
            "ttl":     transformed["ttl"],
        }
        if transformed["session"] is not None:
            put_kwargs["session"] = transformed["session"]
        if transformed["agent"] is not None:
            put_kwargs["agent"] = transformed["agent"]

        try:
            store.put(**put_kwargs)
            written_count += 1
        except Exception as exc:
            _log.error(
                "memory_etl.row_failed",
                project=transformed["project"],
                title=transformed["title"],
                error=str(exc),
            )
            # Continue processing remaining rows so a single failure
            # doesn't abort the whole migration.

        if read_count % batch_log_every == 0:
            _log.info(
                "memory_etl.progress",
                read=read_count,
                written=written_count,
                total=total,
            )

    _log.info(
        "memory_etl.complete",
        read=read_count,
        written=written_count,
        total=total,
    )
    return {"read": read_count, "written": written_count}
