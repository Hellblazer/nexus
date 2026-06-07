# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for taxonomy_etl (bead nexus-gmiaf.14, RDR-152 P2.4).

Tests:
  - _transform_topic: required field validation + normalization
  - _transform_assignment: field mapping + normalization
  - _transform_link: field mapping + normalization
  - _transform_meta: field mapping + normalization
  - count_source_rows: read-only row counts
  - migrate_taxonomy_rows: happy path, ordering (topics before assignments),
    skip-on-failure, idempotent re-run
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from nexus.db.t2.taxonomy_etl import (
    _transform_assignment,
    _transform_link,
    _transform_meta,
    _transform_topic,
    count_source_rows,
    migrate_taxonomy_rows,
)

# ── Schema helpers ─────────────────────────────────────────────────────────────

_TOPICS_SQL = """\
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    parent_id     INTEGER,
    collection    TEXT NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    terms         TEXT
)"""

_ASSIGNMENTS_SQL = """\
CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id            TEXT NOT NULL,
    topic_id          INTEGER NOT NULL,
    assigned_by       TEXT NOT NULL DEFAULT 'hdbscan',
    similarity        REAL,
    assigned_at       TEXT,
    source_collection TEXT,
    PRIMARY KEY (doc_id, topic_id)
)"""

_LINKS_SQL = """\
CREATE TABLE IF NOT EXISTS topic_links (
    from_topic_id INTEGER NOT NULL,
    to_topic_id   INTEGER NOT NULL,
    link_count    INTEGER NOT NULL DEFAULT 0,
    link_types    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (from_topic_id, to_topic_id)
)"""

_META_SQL = """\
CREATE TABLE IF NOT EXISTS taxonomy_meta (
    collection              TEXT PRIMARY KEY,
    last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
    last_discover_at        TEXT
)"""


def _make_taxonomy_db(path: Path, *, topics=None, assignments=None, links=None, meta=None) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(_TOPICS_SQL)
    conn.execute(_ASSIGNMENTS_SQL)
    conn.execute(_LINKS_SQL)
    conn.execute(_META_SQL)

    for t in (topics or []):
        conn.execute(
            "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, doc_count, created_at, review_status, terms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t["label"], t.get("parent_id"), t["collection"],
             t.get("centroid_hash"), t.get("doc_count", 0), t["created_at"],
             t.get("review_status", "pending"), t.get("terms")),
        )

    for a in (assignments or []):
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, similarity, assigned_at, source_collection) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (a["doc_id"], a["topic_id"], a.get("assigned_by", "hdbscan"),
             a.get("similarity"), a.get("assigned_at"), a.get("source_collection")),
        )

    for lk in (links or []):
        conn.execute(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) VALUES (?, ?, ?, ?)",
            (lk["from_topic_id"], lk["to_topic_id"], lk.get("link_count", 0), lk.get("link_types", "[]")),
        )

    for m in (meta or []):
        conn.execute(
            "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, last_discover_at) VALUES (?, ?, ?)",
            (m["collection"], m.get("last_discover_doc_count", 0), m.get("last_discover_at")),
        )

    conn.commit()
    conn.close()


# ── transform tests ────────────────────────────────────────────────────────────


class TestTransformTopic:
    def test_full_row(self) -> None:
        row = {
            "id": 42,
            "label": "machine-learning",
            "parent_id": None,
            "collection": "knowledge__papers",
            "centroid_hash": "abc",
            "doc_count": 10,
            "created_at": "2026-01-01T00:00:00Z",
            "review_status": "accepted",
            "terms": '["ai", "ml"]',
        }
        r = _transform_topic(row)
        assert r["src_id"] == 42
        assert r["label"] == "machine-learning"
        assert r["parent_id"] is None
        assert r["doc_count"] == 10
        assert r["centroid_hash"] == "abc"
        assert r["review_status"] == "accepted"
        assert r["terms"] == '["ai", "ml"]'

    def test_with_parent_id(self) -> None:
        row = {
            "id": 2, "label": "child", "parent_id": 1,
            "collection": "c", "doc_count": 1,
            "created_at": "2026-01-01T00:00:00Z",
        }
        r = _transform_topic(row)
        assert r["parent_id"] == 1

    def test_missing_id_raises(self) -> None:
        with pytest.raises(ValueError, match="missing id"):
            _transform_topic({"label": "x", "collection": "c", "created_at": "ts"})

    def test_missing_label_raises(self) -> None:
        with pytest.raises(ValueError, match="missing label"):
            _transform_topic({"id": 1, "label": "", "collection": "c", "created_at": "ts"})

    def test_missing_collection_raises(self) -> None:
        with pytest.raises(ValueError, match="missing collection"):
            _transform_topic({"id": 1, "label": "x", "collection": "", "created_at": "ts"})

    def test_missing_created_at_raises(self) -> None:
        with pytest.raises(ValueError, match="missing created_at"):
            _transform_topic({"id": 1, "label": "x", "collection": "c", "created_at": ""})

    def test_none_review_status_defaults_pending(self) -> None:
        row = {
            "id": 1, "label": "t", "collection": "c",
            "created_at": "ts", "doc_count": 0,
        }
        r = _transform_topic(row)
        assert r["review_status"] == "pending"

    def test_none_doc_count_defaults_zero(self) -> None:
        row = {
            "id": 1, "label": "t", "collection": "c",
            "created_at": "ts", "doc_count": None,
        }
        r = _transform_topic(row)
        assert r["doc_count"] == 0


class TestTransformAssignment:
    def test_full_row(self) -> None:
        row = {
            "doc_id": "d1",
            "topic_id": 5,
            "assigned_by": "projection",
            "similarity": 0.9,
            "assigned_at": "2026-01-01T00:00:00Z",
            "source_collection": "knowledge__papers",
        }
        r = _transform_assignment(row)
        assert r["doc_id"] == "d1"
        assert r["topic_id"] == 5
        assert r["assigned_by"] == "projection"
        assert r["similarity"] == pytest.approx(0.9)
        assert r["assigned_at"] == "2026-01-01T00:00:00Z"
        assert r["source_collection"] == "knowledge__papers"

    def test_none_similarity_stays_none(self) -> None:
        row = {"doc_id": "d", "topic_id": 1, "assigned_by": "hdbscan", "similarity": None}
        r = _transform_assignment(row)
        assert r["similarity"] is None

    def test_none_assigned_by_defaults_hdbscan(self) -> None:
        row = {"doc_id": "d", "topic_id": 1}
        r = _transform_assignment(row)
        assert r["assigned_by"] == "hdbscan"

    def test_empty_assigned_at_normalizes_none(self) -> None:
        row = {"doc_id": "d", "topic_id": 1, "assigned_by": "hdbscan", "assigned_at": ""}
        r = _transform_assignment(row)
        assert r["assigned_at"] is None


class TestTransformLink:
    def test_full_row(self) -> None:
        row = {
            "from_topic_id": 1,
            "to_topic_id": 2,
            "link_count": 7,
            "link_types": '["co-occurrence"]',
        }
        r = _transform_link(row)
        assert r["from_topic_id"] == 1
        assert r["to_topic_id"] == 2
        assert r["link_count"] == 7
        assert r["link_types"] == '["co-occurrence"]'

    def test_none_link_count_defaults_zero(self) -> None:
        r = _transform_link({"from_topic_id": 1, "to_topic_id": 2, "link_count": None})
        assert r["link_count"] == 0

    def test_none_link_types_defaults_empty_json(self) -> None:
        r = _transform_link({"from_topic_id": 1, "to_topic_id": 2})
        assert r["link_types"] == "[]"


class TestTransformMeta:
    def test_full_row(self) -> None:
        row = {
            "collection": "knowledge__papers",
            "last_discover_doc_count": 100,
            "last_discover_at": "2026-01-01T00:00:00Z",
        }
        r = _transform_meta(row)
        assert r["collection"] == "knowledge__papers"
        assert r["last_discover_doc_count"] == 100
        assert r["last_discover_at"] == "2026-01-01T00:00:00Z"

    def test_none_count_defaults_zero(self) -> None:
        r = _transform_meta({"collection": "c", "last_discover_doc_count": None})
        assert r["last_discover_doc_count"] == 0

    def test_empty_discover_at_normalizes_none(self) -> None:
        r = _transform_meta({"collection": "c", "last_discover_doc_count": 0, "last_discover_at": ""})
        assert r["last_discover_at"] is None


# ── count_source_rows ──────────────────────────────────────────────────────────


class TestCountSourceRows:
    def test_counts_all_four_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
            assignments=[{"doc_id": "d1", "topic_id": 1}],
            links=[{"from_topic_id": 1, "to_topic_id": 1}],
            meta=[{"collection": "c", "last_discover_doc_count": 5}],
        )
        counts = count_source_rows(db)
        assert counts == {"topics": 1, "assignments": 1, "links": 1, "meta": 1}

    def test_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(db)
        counts = count_source_rows(db)
        assert all(v == 0 for v in counts.values())

    def test_missing_db_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="Cannot open"):
            count_source_rows(tmp_path / "nonexistent.db")


# ── migrate_taxonomy_rows ──────────────────────────────────────────────────────


class TestMigrateTaxonomyRows:
    def _make_store(self) -> MagicMock:
        store = MagicMock()
        store.import_topic.return_value = 1
        store.import_assignment.return_value = None
        store.import_topic_link.return_value = None
        store.import_taxonomy_meta.return_value = None
        return store

    def test_happy_path(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 5}],
            assignments=[{"doc_id": "d1", "topic_id": 1, "assigned_by": "hdbscan"}],
            links=[{"from_topic_id": 1, "to_topic_id": 1, "link_count": 3}],
            meta=[{"collection": "c", "last_discover_doc_count": 10}],
        )
        store = self._make_store()
        result = migrate_taxonomy_rows(db, store)

        assert result["topics"]["read"] == 1
        assert result["topics"]["written"] == 1
        assert result["assignments"]["read"] == 1
        assert result["assignments"]["written"] == 1
        assert result["links"]["read"] == 1
        assert result["links"]["written"] == 1
        assert result["meta"]["read"] == 1
        assert result["meta"]["written"] == 1

    def test_topics_migrated_before_assignments(self, tmp_path: Path) -> None:
        """Topics must be written first since assignments reference topic.id."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
            assignments=[{"doc_id": "d1", "topic_id": 1}],
        )
        store = self._make_store()
        call_order = []

        def record_topic(**kwargs: Any) -> int:
            call_order.append("topic")
            return kwargs["src_id"]

        def record_assignment(**kwargs: Any) -> None:
            call_order.append("assignment")

        store.import_topic.side_effect = record_topic
        store.import_assignment.side_effect = record_assignment

        migrate_taxonomy_rows(db, store)
        assert call_order[0] == "topic"
        assert call_order[1] == "assignment"

    def test_row_failure_continues(self, tmp_path: Path) -> None:
        """A single failing row should not abort the migration."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[
                {"id": 1, "label": "t1", "collection": "c", "created_at": "ts", "doc_count": 1},
                {"id": 2, "label": "t2", "collection": "c", "created_at": "ts", "doc_count": 2},
            ],
        )
        store = self._make_store()
        calls = [0]

        def fail_first(**kwargs: Any) -> int:
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("simulated write failure")
            return kwargs["src_id"]

        store.import_topic.side_effect = fail_first

        result = migrate_taxonomy_rows(db, store)
        assert result["topics"]["read"] == 2
        assert result["topics"]["written"] == 1  # one failed, one succeeded

    def test_idempotent_rerun(self, tmp_path: Path) -> None:
        """Running migrate twice should call import_* twice per row (idempotency
        is enforced by the server's upsert logic, not the ETL)."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
        )
        store = self._make_store()
        migrate_taxonomy_rows(db, store)
        migrate_taxonomy_rows(db, store)
        assert store.import_topic.call_count == 2

    def test_fidelity_topic_id_preserved(self, tmp_path: Path) -> None:
        """The original SQLite id must be passed as src_id verbatim."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 9999, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
        )
        store = self._make_store()
        migrate_taxonomy_rows(db, store)
        call_kwargs = store.import_topic.call_args[1]
        assert call_kwargs["src_id"] == 9999

    def test_fidelity_doc_count_preserved(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 42}],
        )
        store = self._make_store()
        migrate_taxonomy_rows(db, store)
        call_kwargs = store.import_topic.call_args[1]
        assert call_kwargs["doc_count"] == 42

    def test_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(db)
        store = self._make_store()
        result = migrate_taxonomy_rows(db, store)
        assert all(v["read"] == 0 for v in result.values())
        store.import_topic.assert_not_called()
        store.import_assignment.assert_not_called()

    def test_source_not_modified(self, tmp_path: Path) -> None:
        """Source DB must be opened read-only and never modified."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
        )
        import stat
        db.chmod(stat.S_IRUSR | stat.S_IRGRP)  # read-only
        store = self._make_store()
        # Should succeed (read-only is fine for the ETL)
        result = migrate_taxonomy_rows(db, store)
        assert result["topics"]["read"] == 1
