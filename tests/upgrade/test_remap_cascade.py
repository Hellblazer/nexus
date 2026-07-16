# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.3 (nexus-n7u38.16): the in-DB remap-cascade primitive.

Applies the persisted old→new map across EVERY local store the P2.0 audit
enumerated (the genuine RDR-180 SUBSET — no binary value type, no
chash_alias). Fixtures use the stores' real DDL (copied verbatim from the
owning modules; .21 integration validates against the real stores).

Design pins from the audit:
- B2 stores (chash IN the PK: chash_index, topic_assignments, frecency)
  use catalog-013-0's two-phase dedupe-then-rewrite — a blind UPDATE
  PK-collides under identical-text collapse.
- frecency collision-merge follows telemetry_etl's GREATEST-on-reimport.
- topic_assignments has no collection scope → the GLOBAL map view drives
  it, and a same-old-id-different-new ambiguity across collections fails
  LOUD (never a silent guess).
- A store named in CASCADE_STORES without an implementation fails the
  run at entry (inventory-completeness tripwire).
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

import nexus.migration.remap_cascade as mod
from nexus.migration.remap_cascade import (
    CASCADE_STORES,
    AmbiguousRemapError,
    cascade_remap,
)
from nexus.migration.wire_reid import ChashRemapStore, RemapEntry

NEW_A = "a" * 32
NEW_B = "b" * 32

_CATALOG_DDL = """
CREATE TABLE document_chunks (
    doc_id      TEXT NOT NULL,
    position    INTEGER NOT NULL,
    chash       TEXT NOT NULL,
    chunk_index INTEGER,
    PRIMARY KEY (doc_id, position)
);
"""

_MEMORY_DDL = """
CREATE TABLE chash_index (
    chash                TEXT NOT NULL,
    physical_collection  TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    PRIMARY KEY (chash, physical_collection)
);
CREATE TABLE topic_assignments (
    doc_id      TEXT NOT NULL,
    topic_id    INTEGER NOT NULL,
    assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
    PRIMARY KEY (doc_id, topic_id)
);
CREATE TABLE frecency (
    chunk_id        TEXT PRIMARY KEY,
    embedded_at     TEXT NOT NULL DEFAULT '',
    ttl_days        INTEGER NOT NULL DEFAULT 0,
    frecency_score  REAL NOT NULL DEFAULT 0,
    miss_count      INTEGER NOT NULL DEFAULT 0,
    last_hit_at     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE relevance_log (
    id         INTEGER PRIMARY KEY,
    query      TEXT NOT NULL,
    chunk_id   TEXT NOT NULL,
    collection TEXT,
    action     TEXT NOT NULL,
    session_id TEXT,
    timestamp  TEXT NOT NULL
);
CREATE TABLE document_aspects (
    collection   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    source_uri   TEXT,
    extracted_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (collection, source_path)
);
CREATE TABLE aspect_extraction_queue (
    collection   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending',
    enqueued_at  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (collection, source_path)
);
"""


@pytest.fixture
def dbs(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    catalog_db = tmp_path / "catalog.db"
    memory_db = tmp_path / "memory.db"
    for path, ddl in ((catalog_db, _CATALOG_DDL), (memory_db, _MEMORY_DDL)):
        conn = sqlite3.connect(path)
        conn.executescript(ddl)
        conn.commit()
        conn.close()
    return catalog_db, memory_db


@pytest.fixture
def map_store(tmp_path: pathlib.Path) -> ChashRemapStore:
    with ChashRemapStore(tmp_path / "chash_remap.db") as s:
        yield s


def _seed(db: pathlib.Path, sql: str, rows: list[tuple]) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(sql, rows)
    conn.commit()
    conn.close()


def _q(db: pathlib.Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _map(map_store: ChashRemapStore, *pairs: tuple[str, str], coll: str = "src") -> None:
    map_store.record_batch(
        [RemapEntry("", coll, old, new, "dst", "test") for old, new in pairs]
    )


# ── completeness tripwire ────────────────────────────────────────────────────


def test_cascade_covers_the_audited_store_set() -> None:
    """The .13 inventory's Class B/D cascade set, verbatim — a store added
    to the audit without landing here fails this pin."""
    assert set(CASCADE_STORES) == {
        "document_chunks",
        "chash_index",
        "topic_assignments",
        "frecency",
        "relevance_log",
        "document_aspects",
        "aspect_extraction_queue",
    }


def test_every_named_store_has_an_implementation(
    dbs: tuple[pathlib.Path, pathlib.Path], map_store: ChashRemapStore
) -> None:
    """Every CASCADE_STORES entry produces a result row even on an empty
    map — an unimplemented store cannot silently vanish from the report."""
    catalog_db, memory_db = dbs
    results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    assert [r.store for r in results] == list(CASCADE_STORES)
    assert all(r.ok and r.rewritten == 0 for r in results)


# ── per-store cascades ───────────────────────────────────────────────────────


def test_manifest_rows_follow_the_map(dbs, map_store: ChashRemapStore) -> None:
    catalog_db, memory_db = dbs
    _seed(
        catalog_db,
        "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?,?,?)",
        [("1.2.3", 0, "legacy-old-1"), ("1.2.3", 1, "legacy-old-1"), ("1.2.3", 2, "legacy-old-2")],
    )
    _map(map_store, ("legacy-old-1", NEW_A), ("legacy-old-2", NEW_B))
    results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    by = {r.store: r for r in results}
    assert by["document_chunks"].rewritten == 3
    # Positions preserved; the same new chash legitimately appears at
    # multiple (doc_id, position) rows (RDR-108 manifest contract).
    assert _q(catalog_db, "SELECT position, chash FROM document_chunks ORDER BY position") == [
        (0, NEW_A), (1, NEW_A), (2, NEW_B),
    ]


def test_chash_index_two_phase_collapse(dbs, map_store: ChashRemapStore) -> None:
    """Two old ids collapsing to one new chash in the SAME collection must
    end as ONE row — the blind-UPDATE PK collision the critic flagged."""
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO chash_index (chash, physical_collection, created_at) VALUES (?,?,?)",
        [("legacy-old-1", "coll", "t"), ("legacy-old-2", "coll", "t"), ("legacy-old-3", "other", "t")],
    )
    _map(map_store, ("legacy-old-1", NEW_A), ("legacy-old-2", NEW_A), ("legacy-old-3", NEW_B))
    cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    rows = _q(memory_db, "SELECT chash, physical_collection FROM chash_index ORDER BY chash, physical_collection")
    assert rows == [(NEW_A, "coll"), (NEW_B, "other")]  # one survivor, no PK violation


def test_topic_assignments_two_phase_collapse(dbs, map_store: ChashRemapStore) -> None:
    """The .16 acceptance fixture the critic specified: a duplicate-content
    pair BOTH assigned to the same topic — zero PK violations, one
    surviving row per (new_chash, topic)."""
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?,?)",
        [("legacy-old-1", 7), ("legacy-old-2", 7), ("legacy-old-2", 9)],
    )
    _map(map_store, ("legacy-old-1", NEW_A), ("legacy-old-2", NEW_A))
    results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    assert {r.store: r.ok for r in results}["topic_assignments"] is True
    rows = _q(memory_db, "SELECT doc_id, topic_id FROM topic_assignments ORDER BY topic_id")
    assert rows == [(NEW_A, 7), (NEW_A, 9)]


def test_frecency_collision_merges_greatest(dbs, map_store: ChashRemapStore) -> None:
    """telemetry_etl's GREATEST-on-reimport convention: on collapse the
    surviving row keeps the max score/hit/miss values."""
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO frecency (chunk_id, frecency_score, miss_count, last_hit_at) VALUES (?,?,?,?)",
        [("legacy-old-1", 5.0, 2, "2026-01-01"), ("legacy-old-2", 9.0, 1, "2026-06-01")],
    )
    _map(map_store, ("legacy-old-1", NEW_A), ("legacy-old-2", NEW_A))
    cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    rows = _q(memory_db, "SELECT chunk_id, frecency_score, miss_count, last_hit_at FROM frecency")
    assert rows == [(NEW_A, 9.0, 2, "2026-06-01")]


def test_relevance_log_plain_update(dbs, map_store: ChashRemapStore) -> None:
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO relevance_log (query, chunk_id, action, timestamp) VALUES (?,?,?,?)",
        [("q", "legacy-old-1", "hit", "t1"), ("q", "conformant-untouched", "hit", "t2")],
    )
    _map(map_store, ("legacy-old-1", NEW_A))
    cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    rows = _q(memory_db, "SELECT chunk_id FROM relevance_log ORDER BY id")
    assert rows == [(NEW_A,), ("conformant-untouched",)]


def test_aspect_rows_rewrite_path_and_uri(dbs, map_store: ChashRemapStore) -> None:
    """Class D: note-backed rows keyed by chash-as-source_path rewrite both
    the key and the chroma:// URI; file-backed rows are untouched."""
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO document_aspects (collection, source_path, source_uri) VALUES (?,?,?)",
        [
            ("knowledge__notes", "legacy-old-1", "chroma://knowledge__notes/legacy-old-1"),
            ("code__repo", "src/file.py", "file:///src/file.py"),
        ],
    )
    _seed(
        memory_db,
        "INSERT INTO aspect_extraction_queue (collection, source_path) VALUES (?,?)",
        [("knowledge__notes", "legacy-old-1"), ("code__repo", "src/file.py")],
    )
    _map(map_store, ("legacy-old-1", NEW_A))
    cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    aspects = _q(memory_db, "SELECT source_path, source_uri FROM document_aspects ORDER BY collection")
    assert aspects == [
        ("src/file.py", "file:///src/file.py"),
        (NEW_A, f"chroma://knowledge__notes/{NEW_A}"),
    ]
    queue = _q(memory_db, "SELECT source_path FROM aspect_extraction_queue ORDER BY collection")
    assert queue == [("src/file.py",), (NEW_A,)]  # code__repo sorts before knowledge__notes


# ── safety semantics ─────────────────────────────────────────────────────────


def test_ambiguous_global_map_fails_loud(dbs, map_store: ChashRemapStore) -> None:
    """Same old id in two source collections mapping to DIFFERENT new
    chashes: unscoped stores (topic_assignments) cannot disambiguate —
    fail loud, never guess."""
    catalog_db, memory_db = dbs
    _map(map_store, ("legacy-old-1", NEW_A), coll="src-1")
    _map(map_store, ("legacy-old-1", NEW_B), coll="src-2")
    with pytest.raises(AmbiguousRemapError, match="legacy-old-1"):
        cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)


def test_same_mapping_from_two_collections_is_not_ambiguous(
    dbs, map_store: ChashRemapStore
) -> None:
    """Identical text indexed into two collections maps the same old id to
    the SAME new chash — consistent, not ambiguous."""
    catalog_db, memory_db = dbs
    _map(map_store, ("legacy-old-1", NEW_A), coll="src-1")
    _map(map_store, ("legacy-old-1", NEW_A), coll="src-2")
    results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    assert all(r.ok for r in results)


def test_cascade_is_idempotent(dbs, map_store: ChashRemapStore) -> None:
    catalog_db, memory_db = dbs
    _seed(
        memory_db,
        "INSERT INTO chash_index (chash, physical_collection, created_at) VALUES (?,?,?)",
        [("legacy-old-1", "coll", "t")],
    )
    _map(map_store, ("legacy-old-1", NEW_A))
    first = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    second = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    assert {r.store: r.rewritten for r in first}["chash_index"] == 1
    assert all(r.rewritten == 0 for r in second)  # old ids are gone: no-op
    assert _q(memory_db, "SELECT chash FROM chash_index") == [(NEW_A,)]


def test_missing_table_degrades_per_store_not_run(
    tmp_path: pathlib.Path, map_store: ChashRemapStore
) -> None:
    """A store whose table doesn't exist (older install shape) reports
    not-ok for THAT store; the rest of the cascade still runs."""
    catalog_db = tmp_path / "catalog.db"
    memory_db = tmp_path / "memory.db"
    sqlite3.connect(catalog_db).close()
    conn = sqlite3.connect(memory_db)
    conn.executescript(_MEMORY_DDL)
    conn.commit()
    conn.close()
    _map(map_store, ("legacy-old-1", NEW_A))
    results = cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
    by = {r.store: r for r in results}
    assert by["document_chunks"].ok is False  # no table in the empty catalog db
    assert "no such table" in by["document_chunks"].reason
    assert by["chash_index"].ok is True  # the rest still ran


def test_inventory_drift_guard_fires(
    dbs: tuple[pathlib.Path, pathlib.Path],
    map_store: ChashRemapStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-vacuity for the runtime drift guard (P2 validator note): a store
    named in CASCADE_STORES without an implementation refuses the run."""
    monkeypatch.setattr(mod, "CASCADE_STORES", (*CASCADE_STORES, "phantom-store"))
    catalog_db, memory_db = dbs
    with pytest.raises(RuntimeError, match="inventory drift"):
        cascade_remap(map_store, catalog_db=catalog_db, memory_db=memory_db)
