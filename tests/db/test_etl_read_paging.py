# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-176 follow-up (bead nexus-lbolo) — the taxonomy + telemetry migration
ETLs must PAGE the SQLite read (LIMIT/OFFSET), not ``fetchall`` the whole table.

P3 batched the HTTP TRANSFER (ceil(N/300) POSTs) but the READ still materialized
the entire source table into a Python list before the batch loop — O(N) memory
for the 190k-row ``topic_assignments`` case. The memory/plans/aspects/queue
exemplars already page with LIMIT/OFFSET; these two diverged. Paging caps peak
memory at one read-page regardless of table size.

These tests use ``sqlite3``'s ``set_trace_callback`` to observe that the data
read is issued as bounded LIMIT/OFFSET pages, not one unbounded SELECT.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import nexus.db.t2.taxonomy_etl as tax
import nexus.db.t2.telemetry_etl as tel
from nexus.db.t2 import T2Database
from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows
from nexus.db.t2.telemetry_etl import (
    _HOOK_FAILURES_COLS,
    _iter_rows,
    migrate_telemetry_rows,
)
from tests.db._issue_collector import IssueCollector

_TS = "2026-05-15T08:30:00Z"
_COLL = "knowledge__rehearsal__minilm-l6-v2-384__v1"


def _paged_selects(sqls: list[str], table: str) -> list[str]:
    return [
        s for s in sqls
        if table in s and "limit" in s.lower() and "offset" in s.lower()
    ]


def test_telemetry_iter_rows_pages_the_read(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO hook_failures (doc_id, collection, hook_name, error, "
        "occurred_at, batch_doc_ids, is_batch, chain) VALUES (?,?,?,?,?,?,?,?)",
        [(f"d{i}", _COLL, "post_store", "e", _TS, None, 0, "single") for i in range(5)],
    )
    conn.commit()

    sqls: list[str] = []
    conn.set_trace_callback(sqls.append)
    rows = list(_iter_rows(conn, "hook_failures", _HOOK_FAILURES_COLS, page_size=2))

    assert len(rows) == 5
    assert all(isinstance(r, dict) for r in rows)          # projected dicts
    assert rows[0]["doc_id"] == "d0"                       # column projection works
    # ceil(5/2) == 3 pages; the generator early-stops on the short final page,
    # so exactly 3 paged SELECTs — never one unbounded fetchall.
    assert len(_paged_selects(sqls, "hook_failures")) == 3


def test_telemetry_iter_rows_absent_table_yields_nothing(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    assert list(_iter_rows(conn, "no_such_table", _HOOK_FAILURES_COLS, page_size=2)) == []


def _seed_taxonomy_db(tmp_path: Path) -> Path:
    """2 topics; 5 assignments, 5 links, 5 meta rows — all non-orphan."""
    db = tmp_path / "tax.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
        "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
        [(i, f"t{i}", None, _COLL, f"{i:032x}", 0, _TS, "approved", "a b")
         for i in range(1, 7)],
    )
    conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, "
        "source_collection) VALUES (?,?,?,?)",
        [(f"d{i}", 1, "discover", _COLL) for i in range(5)],
    )
    # distinct (from,to) pairs (UNIQUE constraint), all referencing existing
    # topics 1..6 → non-orphan.
    conn.executemany(
        "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
        "VALUES (?,?,?,?)",
        [(1, i + 2, 1, "relates") for i in range(5)],
    )
    conn.executemany(
        "INSERT INTO taxonomy_meta (collection, last_discover_doc_count, "
        "last_discover_at) VALUES (?,?,?)",
        [(f"coll{i}", 0, _TS) for i in range(5)],
    )
    conn.commit()
    conn.close()
    return db


def _trace_migrate_taxonomy(db: Path, store, read_page: int) -> list[str]:
    """Run migrate_taxonomy_rows with the read connection traced; return SQLs."""
    captured: list[str] = []
    real_connect = sqlite3.connect

    def _tracing_connect(*a, **k):
        c = real_connect(*a, **k)
        c.set_trace_callback(captured.append)
        return c

    orig = tax.sqlite3.connect
    tax.sqlite3.connect = _tracing_connect
    try:
        migrate_taxonomy_rows(db, store, read_page=read_page)
    finally:
        tax.sqlite3.connect = orig
    return captured


def test_taxonomy_assignment_link_meta_reads_are_paged(tmp_path: Path) -> None:
    """topic_assignments (the 190k offender), topic_links, and taxonomy_meta
    must each be read in LIMIT/OFFSET pages, not fetchall. topics stays
    whole-load (parent-before-child topo-sort). A revert of ANY of the three
    streamed reads to fetchall drops its paged-SELECT count to 0."""
    db = _seed_taxonomy_db(tmp_path)

    class _Store:
        def import_rows_batch(self, _kind, rows):
            return len(rows)

    captured = _trace_migrate_taxonomy(db, _Store(), read_page=2)

    # 5 rows / page 2 → ceil(5/2) == 3 paged SELECTs per streamed table.
    assert len(_paged_selects(captured, "topic_assignments")) == 3
    assert len(_paged_selects(captured, "topic_links")) == 3
    assert len(_paged_selects(captured, "taxonomy_meta")) == 3


def test_taxonomy_all_orphans_read_equals_source_via_collector(tmp_path: Path) -> None:
    """All assignments orphaned (topic_id absent): every row is skipped-and-
    recorded, written == 0, and the collector's read still == source cardinality
    (the orphan count is added after the stream drains)."""
    db = tmp_path / "tax.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
        "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
        (1, "t", None, _COLL, "0" * 32, 0, _TS, "approved", "a b"),
    )
    conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, "
        "source_collection) VALUES (?,?,?,?)",
        [(f"d{i}", 999, "discover", _COLL) for i in range(4)],  # topic 999 absent
    )
    conn.commit()
    conn.close()

    collector = IssueCollector()

    class _Store:
        def import_rows_batch(self, _kind, rows):
            return len(rows)

    res = migrate_taxonomy_rows(db, _Store(), collector=collector, read_page=2)
    assert res["assignments"]["written"] == 0
    # read (collector) == source cardinality even though all were orphan-skipped
    assert collector.table_counts("taxonomy", "topic_assignments")["read"] == 4
    # The collector dedups orphan issues by (issue_class, constraint, reason),
    # so all 4 orphans collapse to ONE recorded skipped issue; the cardinality
    # is carried by the read count asserted above, not the issue count.
    skipped = [
        i for i in collector.issues_for("taxonomy", "topic_assignments")
        if i.action == "skipped"
    ]
    assert len(skipped) >= 1


def test_telemetry_migrate_pages_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """migrate_telemetry_rows must drive the PAGED read end-to-end (not just the
    _iter_rows helper in isolation): a revert of the wrappers to fetchall would
    drop relevance_log's paged-SELECT count to 0."""
    monkeypatch.setattr(tel, "_READ_PAGE", 2)  # call-site resolves this at runtime

    db = tmp_path / "tel.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO relevance_log (query, chunk_id, action, timestamp) VALUES (?,?,?,?)",
        [(f"q{i}", f"c{i}", "click", _TS) for i in range(5)],
    )
    conn.commit()
    conn.close()

    captured: list[str] = []
    real_connect = sqlite3.connect

    def _tracing_connect(*a, **k):
        c = real_connect(*a, **k)
        c.set_trace_callback(captured.append)
        return c

    monkeypatch.setattr(tel.sqlite3, "connect", _tracing_connect)

    class _Store:
        def import_rows_batch(self, _table, rows):
            return len(rows)

    migrate_telemetry_rows(db, _Store())
    assert len(_paged_selects(captured, "relevance_log")) == 3


def test_iter_rows_exact_page_multiple_reads_all(tmp_path: Path) -> None:
    """Exact multiple of page_size: the loop must issue a trailing empty-page
    query to terminate, never stop after the last full page and drop rows."""
    db = tmp_path / "t.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO hook_failures (doc_id, collection, hook_name, error, "
        "occurred_at, batch_doc_ids, is_batch, chain) VALUES (?,?,?,?,?,?,?,?)",
        [(f"d{i}", _COLL, "post_store", "e", _TS, None, 0, "single") for i in range(4)],
    )
    conn.commit()
    sqls: list[str] = []
    conn.set_trace_callback(sqls.append)
    rows = list(_iter_rows(conn, "hook_failures", _HOOK_FAILURES_COLS, page_size=2))
    assert len(rows) == 4  # all rows read, none dropped at the page boundary
    # 4/2 == 2 full pages + 1 terminating empty query == 3 paged SELECTs.
    assert len(_paged_selects(sqls, "hook_failures")) == 3
