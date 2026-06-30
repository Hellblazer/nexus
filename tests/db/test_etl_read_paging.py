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

from nexus.db.t2 import T2Database

_TS = "2026-05-15T08:30:00Z"
_COLL = "knowledge__rehearsal__minilm-l6-v2-384__v1"


def _paged_selects(sqls: list[str], table: str) -> list[str]:
    return [
        s for s in sqls
        if table in s and "limit" in s.lower() and "offset" in s.lower()
    ]


def test_telemetry_iter_rows_pages_the_read(tmp_path: Path) -> None:
    from nexus.db.t2.telemetry_etl import _HOOK_FAILURES_COLS, _iter_rows

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
    from nexus.db.t2.telemetry_etl import _HOOK_FAILURES_COLS, _iter_rows

    db = tmp_path / "t.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    assert list(_iter_rows(conn, "no_such_table", _HOOK_FAILURES_COLS, page_size=2)) == []


def test_taxonomy_assignments_read_is_paged(tmp_path: Path) -> None:
    """The 190k-row offender: topic_assignments must be read in LIMIT/OFFSET
    pages, not fetchall. topics stays whole-load (parent-before-child topo-sort)."""
    from nexus.db.t2.taxonomy_etl import migrate_taxonomy_rows

    db = tmp_path / "tax.db"
    T2Database.bootstrap_schema(db)
    conn = sqlite3.connect(str(db))
    # 1 topic so assignments are not orphan-skipped; 5 assignments referencing it.
    conn.execute(
        "INSERT INTO topics (id, label, parent_id, collection, centroid_hash, "
        "doc_count, created_at, review_status, terms) VALUES (?,?,?,?,?,?,?,?,?)",
        (1, "t", None, _COLL, "0" * 32, 0, _TS, "approved", "a b"),
    )
    conn.executemany(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, "
        "source_collection) VALUES (?,?,?,?)",
        [(f"d{i}", 1, "discover", _COLL) for i in range(5)],
    )
    conn.commit()
    conn.close()

    captured: list[str] = []

    class _Store:
        def import_rows_batch(self, _kind, rows):
            return len(rows)

    # Trace the read connection migrate_taxonomy_rows opens internally.
    real_connect = sqlite3.connect

    def _tracing_connect(*a, **k):
        c = real_connect(*a, **k)
        c.set_trace_callback(captured.append)
        return c

    import nexus.db.t2.taxonomy_etl as tax
    orig = tax.sqlite3.connect
    tax.sqlite3.connect = _tracing_connect
    try:
        migrate_taxonomy_rows(db, _Store(), read_page=2)
    finally:
        tax.sqlite3.connect = orig

    assert len(_paged_selects(captured, "topic_assignments")) == 3
