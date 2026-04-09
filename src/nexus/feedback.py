# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Retrieval feedback logging — tracks which search results are actually used.

Pure-function module: all I/O flows through a T2Database parameter.
No global state, no singletons.

See RDR-061 E2 (nexus-0l39) for design rationale.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database

_log = structlog.get_logger()

_FEEDBACK_COLUMNS = ("id", "query_hash", "doc_id", "collection", "action", "session", "ts")


def log_feedback(
    db: T2Database,
    *,
    doc_id: str,
    collection: str,
    query_hash: str,
    action: str,
    session: str | None = None,
) -> int:
    """Record that a search result was used.

    Args:
        db: T2Database instance (provides the sqlite3 connection).
        doc_id: The document/chunk ID that was used.
        collection: The collection the document belongs to.
        query_hash: Hash of the query that produced this result.
        action: How the result was used — ``'store_put'``, ``'catalog_link'``,
            or ``'explicit'``.
        session: Optional session ID for correlation.

    Returns:
        The row ID of the inserted feedback record.
    """
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db._lock:
        cursor = db.conn.execute(
            """
            INSERT INTO result_feedback (query_hash, doc_id, collection, action, session, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (query_hash, doc_id, collection, action, session, ts),
        )
        db.conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def query_feedback_stats(
    db: T2Database,
    *,
    collection: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent feedback entries, newest first.

    Args:
        db: T2Database instance.
        collection: Optional filter — only return entries from this collection.
        limit: Maximum number of rows to return.

    Returns:
        List of dicts with keys matching ``_FEEDBACK_COLUMNS``.
    """
    if collection:
        sql = """
            SELECT id, query_hash, doc_id, collection, action, session, ts
            FROM result_feedback
            WHERE collection = ?
            ORDER BY ts DESC, id DESC
            LIMIT ?
        """
        params: tuple = (collection, limit)
    else:
        sql = """
            SELECT id, query_hash, doc_id, collection, action, session, ts
            FROM result_feedback
            ORDER BY ts DESC, id DESC
            LIMIT ?
        """
        params = (limit,)

    with db._lock:
        rows = db.conn.execute(sql, params).fetchall()
    return [dict(zip(_FEEDBACK_COLUMNS, row)) for row in rows]
