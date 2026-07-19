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
        store.import_rows_batch.side_effect = lambda kind, rows: len(rows)
        return store

    @staticmethod
    def _batch_rows(store: MagicMock, kind: str) -> list:
        """The rows the ETL batched for *kind* (first such call)."""
        for c in store.import_rows_batch.call_args_list:
            k = c.args[0] if c.args else c.kwargs.get("kind")
            rows = c.args[1] if len(c.args) > 1 else c.kwargs.get("rows")
            if k == kind:
                return rows
        raise AssertionError(f"no batch call for kind {kind!r}")

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

    def test_assignment_skipped_when_import_returns_false(self, tmp_path: Path) -> None:
        """Generic skip-accounting: _migrate_table counts a row as skipped (not written,
        not failed) when import_fn returns False. NOTE: no real taxonomy import_fn returns
        False today (import_assignment always applies — fk_ta_catalog_doc was never
        registered, nexus-sa14p); this test pins the generic skip hook via a mock for any
        future import_fn that needs it."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts"}],
            assignments=[
                {"doc_id": "present", "topic_id": 1, "assigned_by": "hdbscan"},
                {"doc_id": "orphan",  "topic_id": 1, "assigned_by": "hdbscan"},
            ],
        )
        store = self._make_store()
        result = migrate_taxonomy_rows(db, store)

        # RDR-176 P3: the batched ETL has no per-row skip hook — both assignments
        # ship in one batch and apply (server upsert). skipped is always 0.
        assert result["assignments"]["read"] == 2
        assert result["assignments"]["written"] == 2
        assert result["assignments"]["skipped"] == 0

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

        def record_batch(kind: str, rows: list) -> int:
            call_order.append(kind)
            return len(rows)

        store.import_rows_batch.side_effect = record_batch

        migrate_taxonomy_rows(db, store)
        assert call_order[0] == "topic"
        assert call_order[1] == "assignment"

    def test_row_failure_continues(self, tmp_path: Path) -> None:
        """A single failing row should not abort the migration."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[
                {"id": 1, "label": "", "collection": "c", "created_at": "ts", "doc_count": 1},
                {"id": 2, "label": "t2", "collection": "c", "created_at": "ts", "doc_count": 2},
            ],
        )
        store = self._make_store()

        # RDR-176 P3: a corrupt row (empty label) fails per-row TRANSFORM and is
        # recorded + excluded; the good row still batches.
        result = migrate_taxonomy_rows(db, store)
        assert result["topics"]["read"] == 2
        assert result["topics"]["written"] == 1

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
        assert store.import_rows_batch.call_count == 2  # one topic batch per run

    def test_fidelity_topic_id_preserved(self, tmp_path: Path) -> None:
        """The original SQLite id must be passed as src_id verbatim."""
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 9999, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 1}],
        )
        store = self._make_store()
        migrate_taxonomy_rows(db, store)
        assert self._batch_rows(store, "topic")[0]["src_id"] == 9999

    def test_fidelity_doc_count_preserved(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts", "doc_count": 42}],
        )
        store = self._make_store()
        migrate_taxonomy_rows(db, store)
        assert self._batch_rows(store, "topic")[0]["doc_count"] == 42

    def test_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "t.db"
        _make_taxonomy_db(db)
        store = self._make_store()
        result = migrate_taxonomy_rows(db, store)
        assert all(v["read"] == 0 for v in result.values())
        store.import_rows_batch.assert_not_called()

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


# ── Per-table split (RDR-180 nexus-jxizy.10.7) ─────────────────────────────────
# Each new public entry point migrates ONLY its own table(s); the other
# tables' rows must never reach the store, even when the source DB has all
# four tables populated (proven via the store's per-kind batch calls).


class TestPerTableSplit:
    def _make_store(self) -> MagicMock:
        store = MagicMock()
        store.import_rows_batch.side_effect = lambda kind, rows: len(rows)
        return store

    def _seeded_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "t.db"
        _make_taxonomy_db(
            db,
            topics=[{"id": 1, "label": "ml", "collection": "c", "created_at": "ts"}],
            assignments=[{"doc_id": "d1", "topic_id": 1}],
            links=[{"from_topic_id": 1, "to_topic_id": 1}],
            meta=[{"collection": "c", "last_discover_doc_count": 1}],
        )
        return db

    def test_migrate_topics_writes_only_topics(self, tmp_path: Path) -> None:
        from nexus.db.t2.taxonomy_etl import migrate_topics

        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_topics(db, store)

        assert result["read"] == 1
        assert result["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert kinds == {"topic"}, f"only 'topic' batches must be sent, got {kinds}"

    def test_migrate_topic_assignments_writes_only_assignments(self, tmp_path: Path) -> None:
        from nexus.db.t2.taxonomy_etl import migrate_topic_assignments

        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_topic_assignments(db, store)

        assert result["read"] == 1
        assert result["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert kinds == {"assignment"}, (
            f"topics must be read for orphan-filtering but never WRITTEN, got {kinds}"
        )

    def test_migrate_topic_links_writes_only_links(self, tmp_path: Path) -> None:
        from nexus.db.t2.taxonomy_etl import migrate_topic_links

        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_topic_links(db, store)

        assert result["read"] == 1
        assert result["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert kinds == {"link"}, (
            f"topics must be read for orphan-filtering but never WRITTEN, got {kinds}"
        )

    def test_migrate_taxonomy_meta_writes_only_meta(self, tmp_path: Path) -> None:
        from nexus.db.t2.taxonomy_etl import migrate_taxonomy_meta

        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_taxonomy_meta(db, store)

        assert result["read"] == 1
        assert result["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert kinds == {"meta"}

    def test_migrate_taxonomy_without_assignments_excludes_assignments(self, tmp_path: Path) -> None:
        """The guided-path entry point: topics + links + meta land; the
        chash-bearing topic_assignments table is NEVER written."""
        from nexus.db.t2.taxonomy_etl import migrate_taxonomy_without_assignments

        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_taxonomy_without_assignments(db, store)

        assert set(result) == {"topics", "links", "meta"}
        assert result["topics"]["written"] == 1
        assert result["links"]["written"] == 1
        assert result["meta"]["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert "assignment" not in kinds, (
            f"topic_assignments must NEVER be written by the guided-path "
            f"non-chash entry point, got batch kinds {kinds}"
        )
        assert kinds == {"topic", "link", "meta"}

    def test_migrate_taxonomy_rows_composition_matches_monolithic_result(self, tmp_path: Path) -> None:
        """The thin composition must still migrate all four tables (byte-
        identical behavior for existing callers)."""
        db = self._seeded_db(tmp_path)
        store = self._make_store()
        result = migrate_taxonomy_rows(db, store)

        assert set(result) == {"topics", "assignments", "links", "meta"}
        for table in result.values():
            assert table["read"] == 1
            assert table["written"] == 1
        kinds = {c.args[0] for c in store.import_rows_batch.call_args_list}
        assert kinds == {"topic", "assignment", "link", "meta"}
