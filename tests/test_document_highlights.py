# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-139 Layer E — DocumentHighlights T2 store.

A dedicated tumbler-keyed table for DEVONthink-sourced highlight/mention
markdown blobs. Separate from ``document_aspects`` by design: highlights are
user-authored notes (a markdown blob, not structured fields), and must not
contend with the confidence-gated, INSERT-OR-REPLACE, aspect-worker-owned
aspects row.
"""
from __future__ import annotations

import pytest

from nexus.db.t2.document_highlights import DocumentHighlights, HighlightRecord


@pytest.fixture
def store(tmp_path) -> DocumentHighlights:
    return DocumentHighlights(tmp_path / "memory.db")


def _rec(**kw) -> HighlightRecord:
    base = dict(
        doc_id="1.14.4",
        source_uri="x-devonthink-item://ABC",
        collection="knowledge__dt-papers__voyage-context-3__v1",
        highlights_md="## Highlights\n- a point\n- another",
        mentions_md="",
        ingested_at="2026-05-30T00:00:00Z",
    )
    base.update(kw)
    return HighlightRecord(**base)


def test_table_self_created_on_init(store) -> None:
    # _init_schema ran in __init__ (fresh DBs/tests need no migration).
    cols = {r[1] for r in store.conn.execute(
        "PRAGMA table_info(document_highlights)"
    ).fetchall()}
    assert {"doc_id", "source_uri", "collection", "highlights_md",
            "mentions_md", "ingested_at"} <= cols


def test_upsert_then_get_roundtrips(store) -> None:
    assert store.upsert(_rec()) is True
    got = store.get("1.14.4")
    assert got is not None
    assert got.highlights_md.startswith("## Highlights")
    assert got.source_uri == "x-devonthink-item://ABC"
    assert got.collection.startswith("knowledge__")


def test_upsert_is_complete_overwrite_by_doc_id(store) -> None:
    store.upsert(_rec(highlights_md="old"))
    store.upsert(_rec(highlights_md="new", mentions_md="@someone"))
    got = store.get("1.14.4")
    assert got.highlights_md == "new"
    assert got.mentions_md == "@someone"
    # still exactly one row for the doc_id
    n = store.conn.execute(
        "SELECT COUNT(*) FROM document_highlights WHERE doc_id=?", ("1.14.4",)
    ).fetchone()[0]
    assert n == 1


def test_empty_record_is_rejected(store) -> None:
    # nothing to store when both blobs are empty
    assert store.upsert(_rec(highlights_md="", mentions_md="")) is False
    assert store.get("1.14.4") is None


def test_empty_doc_id_raises(store) -> None:
    with pytest.raises(ValueError, match="doc_id"):
        store.upsert(_rec(doc_id=""))


def test_get_by_source_uri(store) -> None:
    store.upsert(_rec())
    got = store.get_by_source_uri("x-devonthink-item://ABC")
    assert got is not None and got.doc_id == "1.14.4"
    assert store.get_by_source_uri("x-devonthink-item://NOPE") is None


def test_get_missing_returns_none(store) -> None:
    assert store.get("9.9.9") is None


def test_close_releases_connection(store) -> None:
    store.upsert(_rec())
    store.close()
    import sqlite3
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")


def test_rename_cascade_updates_highlights_collection(tmp_path) -> None:
    """HIGH-2: rename_collection_cascade must carry document_highlights rows."""
    from nexus.db.t2 import T2Database

    db = T2Database(tmp_path / "memory.db")
    try:
        db.document_highlights.upsert(HighlightRecord(
            doc_id="1.2.3", source_uri="x-devonthink-item://A",
            collection="docs__old__voyage-context-3__v1",
            highlights_md="## h", mentions_md="",
            ingested_at="2026-05-30T00:00:00Z",
        ))
        counts = db.rename_collection_cascade(
            old="docs__old__voyage-context-3__v1",
            new="docs__new__voyage-context-3__v1",
        )
        assert counts["highlights"] == 1
        rec = db.document_highlights.get("1.2.3")
        assert rec.collection == "docs__new__voyage-context-3__v1"
    finally:
        db.close()


def test_list_and_delete(store) -> None:
    store.upsert(_rec(doc_id="1.1.1", source_uri="x-devonthink-item://A"))
    store.upsert(_rec(doc_id="1.1.2", source_uri="x-devonthink-item://B"))
    rows = store.list()
    assert {r.doc_id for r in rows} == {"1.1.1", "1.1.2"}
    assert store.delete("1.1.1") is True
    assert store.get("1.1.1") is None
    assert store.delete("1.1.1") is False  # already gone
