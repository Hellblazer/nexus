# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for retrieval feedback logging (RDR-061 E2, nexus-0l39)."""
import sqlite3
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.feedback import log_feedback, query_feedback_stats


# ── round-trip ──────────────────────────────────────────────────────────────


def test_log_feedback_round_trip(db: T2Database) -> None:
    """Log a feedback entry and retrieve it via stats."""
    log_feedback(
        db,
        doc_id="doc::chunk_0",
        collection="code__nexus",
        query_hash="abc123",
        action="store_put",
    )
    stats = query_feedback_stats(db)
    assert len(stats) == 1
    row = stats[0]
    assert row["doc_id"] == "doc::chunk_0"
    assert row["collection"] == "code__nexus"
    assert row["query_hash"] == "abc123"
    assert row["action"] == "store_put"
    assert row["ts"]  # non-empty timestamp


# ── schema ──────────────────────────────────────────────────────────────────


def test_result_feedback_table_created(db: T2Database) -> None:
    """The result_feedback table exists after T2Database init."""
    tables = {
        r[0]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "result_feedback" in tables


def test_result_feedback_indexes_created(db: T2Database) -> None:
    """Both doc_id and collection indexes exist."""
    indexes = {
        r[0]
        for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "idx_rf_doc" in indexes
    assert "idx_rf_collection" in indexes


# ── migration ───────────────────────────────────────────────────────────────


def test_migration_adds_table_to_existing_db(tmp_path: Path) -> None:
    """Opening an older DB (without result_feedback) adds the table."""
    db_path = tmp_path / "legacy.db"
    # Create a DB with current schema, then drop result_feedback
    db = T2Database(db_path)
    db.conn.execute("DROP TABLE IF EXISTS result_feedback")
    db.conn.commit()
    db.close()

    # Re-open — migration should recreate the table
    db2 = T2Database(db_path)
    tables = {
        r[0]
        for r in db2.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "result_feedback" in tables
    db2.close()


# ── multiple entries & filtering ────────────────────────────────────────────


def test_feedback_stats_filtered_by_collection(db: T2Database) -> None:
    """query_feedback_stats can filter by collection."""
    log_feedback(db, doc_id="d1", collection="code__nexus", query_hash="h1", action="store_put")
    log_feedback(db, doc_id="d2", collection="docs__corpus", query_hash="h2", action="catalog_link")
    log_feedback(db, doc_id="d3", collection="code__nexus", query_hash="h3", action="explicit")

    all_stats = query_feedback_stats(db)
    assert len(all_stats) == 3

    code_stats = query_feedback_stats(db, collection="code__nexus")
    assert len(code_stats) == 2
    assert all(r["collection"] == "code__nexus" for r in code_stats)


def test_feedback_stats_respects_limit(db: T2Database) -> None:
    """query_feedback_stats honours the limit parameter."""
    for i in range(5):
        log_feedback(db, doc_id=f"d{i}", collection="c", query_hash=f"h{i}", action="explicit")
    stats = query_feedback_stats(db, limit=3)
    assert len(stats) == 3


def test_feedback_stats_ordered_newest_first(db: T2Database) -> None:
    """Results are ordered by timestamp descending."""
    log_feedback(db, doc_id="old", collection="c", query_hash="h1", action="explicit")
    log_feedback(db, doc_id="new", collection="c", query_hash="h2", action="explicit")
    stats = query_feedback_stats(db)
    assert stats[0]["doc_id"] == "new"
    assert stats[1]["doc_id"] == "old"


def test_log_feedback_with_session(db: T2Database) -> None:
    """Session ID is stored when provided."""
    log_feedback(
        db,
        doc_id="d1",
        collection="c",
        query_hash="h1",
        action="explicit",
        session="sess-42",
    )
    stats = query_feedback_stats(db)
    assert stats[0]["session"] == "sess-42"
