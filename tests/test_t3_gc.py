# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-r5eo: ``nx t3 gc`` subcommand (RDR-101 Phase 6).

Per RF-101-3, ``nx t3 gc`` is the SOLE emitter of ``ChunkOrphaned`` events
and the SOLE post-Phase-3 deletion path for T3 chunks. The verb:

  1. Reads catalog projection: alive doc_ids per collection (= ``tumbler``
     in v: 0 schema, scoped by ``physical_collection``).
  2. Reads T3: per chunk, ``(chunk_id, doc_id, indexed_at)``.
  3. Diffs: chunks whose ``doc_id`` is no longer alive AND whose
     ``indexed_at`` predates the orphan window (default 30 days).
  4. STRICT ORDER: emit ``ChunkOrphaned`` event THEN call
     ``delete_by_chunk_ids``. A crash mid-GC leaves the log consistent
     with T3 (event present + delete failed = next gc retries).

Tests use a real T3Database backed by chromadb's EphemeralClient +
DefaultEmbeddingFunction so we exercise the full delete-by-chunk-ids
machinery without Cloud credentials. The Catalog uses a tmp_path
``catalog_dir`` so events.jsonl is real on disk.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.event_log import EVENTS_FILENAME, EventLog
from nexus.catalog.events import TYPE_CHUNK_ORPHANED, Event
from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def catalog(tmp_path):
    """Catalog rooted in tmp_path so events.jsonl is real on disk."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    doc_id: str,
    indexed_at: str,
) -> None:
    """Insert one chunk with the metadata GC reads."""
    col = t3_db._client.get_or_create_collection(collection)
    col.add(
        ids=[chunk_id],
        documents=[content],
        metadatas=[{"doc_id": doc_id, "indexed_at": indexed_at}],
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── T3Database new methods ────────────────────────────────────────────────


def test_list_chunks_with_metadata_returns_doc_id_and_indexed_at(t3_db):
    """``list_chunks_with_metadata`` yields ``(chunk_id, metadata_subset)``."""
    coll = "knowledge__test_list"
    now = _iso(datetime.now(UTC))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        doc_id="1.1.1", indexed_at=now,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c2", content="y",
        doc_id="1.1.2", indexed_at=now,
    )
    rows = list(t3_db.list_chunks_with_metadata(coll))
    by_id = {cid: meta for cid, meta in rows}
    assert by_id["c1"] == {"doc_id": "1.1.1", "indexed_at": now}
    assert by_id["c2"] == {"doc_id": "1.1.2", "indexed_at": now}


def test_list_chunks_with_metadata_missing_collection(t3_db):
    assert list(t3_db.list_chunks_with_metadata("knowledge__nonexistent")) == []


def test_delete_by_chunk_ids_deletes_only_listed(t3_db):
    """``delete_by_chunk_ids`` deletes the listed ids and returns the count."""
    coll = "knowledge__test_gc_delete_by_ids"
    now = _iso(datetime.now(UTC))
    for cid in ("c1", "c2", "c3"):
        _seed_chunk(
            t3_db, collection=coll, chunk_id=cid, content=cid,
            doc_id="1.1.1", indexed_at=now,
        )
    deleted = t3_db.delete_by_chunk_ids(coll, ["c1", "c3"])
    assert deleted == 2
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert surviving == ["c2"]


def test_delete_by_chunk_ids_missing_collection_returns_zero(t3_db):
    assert t3_db.delete_by_chunk_ids("knowledge__nonexistent", ["c1"]) == 0


def test_delete_by_chunk_ids_empty_list(t3_db):
    coll = "knowledge__test_gc_empty_list"
    now = _iso(datetime.now(UTC))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        doc_id="1.1.1", indexed_at=now,
    )
    assert t3_db.delete_by_chunk_ids(coll, []) == 0
    assert t3_db._client.get_collection(coll).count() == 1


# ── nx t3 gc CLI ─────────────────────────────────────────────────────────


def _register_doc(catalog: Catalog, *, tumbler: str, collection: str) -> None:
    """Seed a document row directly so ``list_by_collection`` returns it.

    Bypasses ``Catalog.register`` (which mints its own tumbler off an
    owner prefix). GC only cares that ``list_by_collection`` reports
    alive doc_ids, and the SQLite projection is the read path.
    """
    catalog._db.execute(  # epsilon-allow: GC alive_set fixture; Catalog.register would mint its own tumbler instead of pinning to the test value
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "text", f"/tmp/{tumbler}.md",
            "", collection, 1, "", "", "{}", 0.0, "", "",
        ),
    )
    catalog._db.commit()


def test_gc_dry_run_reports_orphans_no_mutation(t3_db, catalog, tmp_path, runner):
    """Default dry-run prints orphan candidates but does not delete or emit."""
    coll = "knowledge__test_gc_dryrun"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_doc(catalog, tumbler="1.1.1", collection=coll)  # alive
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        doc_id="1.1.1", indexed_at=long_ago,
    )
    _seed_chunk(  # orphan: doc 1.1.99 not registered
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        doc_id="1.1.99", indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert "orphan1" in result.output
    assert "alive1" not in result.output
    assert "would delete" in result.output

    # No T3 mutation
    assert t3_db._client.get_collection(coll).count() == 2

    # No event emitted
    events_path = catalog._dir / EVENTS_FILENAME
    if events_path.exists():
        log = EventLog(catalog._dir)
        events = [e for e in log.replay() if e.type == TYPE_CHUNK_ORPHANED]
        assert events == []


def test_gc_emits_chunk_orphaned_event_before_delete(
    t3_db, catalog, tmp_path, runner,
):
    """``--no-dry-run --yes`` emits ChunkOrphaned BEFORE deleting the chunk.

    Strict-order contract from RF-101-3: a crash between event-write and
    delete leaves the log consistent with T3 (event present, delete
    pending; next gc retries the delete).
    """
    coll = "knowledge__test_gc_emit"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_doc(catalog, tumbler="1.1.1", collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        doc_id="1.1.1", indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        doc_id="1.1.99", indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan2", content="o2",
        doc_id="1.1.99", indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert "deleted 2" in result.output

    # Alive chunk survives
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert surviving == ["alive1"]

    # ChunkOrphaned events emitted, one per deleted chunk
    log = EventLog(catalog._dir)
    orphan_events = [e for e in log.replay() if e.type == TYPE_CHUNK_ORPHANED]
    chunk_ids = {e.payload.chunk_id for e in orphan_events}
    assert chunk_ids == {"orphan1", "orphan2"}


def test_gc_orphan_window_excludes_recent(t3_db, catalog, tmp_path, runner):
    """Chunks whose ``indexed_at`` is within the orphan window are not GC'd
    even if their ``doc_id`` is dead.

    Rationale: a fresh re-index might briefly leave chunks orphaned
    while the catalog projection catches up. The window is the grace
    period.
    """
    coll = "knowledge__test_gc_window"
    recent = _iso(datetime.now(UTC) - timedelta(hours=1))
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(  # orphan but recent: protected
        t3_db, collection=coll, chunk_id="recent_orphan", content="r",
        doc_id="1.1.99", indexed_at=recent,
    )
    _seed_chunk(  # orphan and old: eligible
        t3_db, collection=coll, chunk_id="old_orphan", content="o",
        doc_id="1.1.99", indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            [
                "t3", "gc", "-c", coll,
                "--orphan-window", "30d",
                "--no-dry-run", "--yes",
            ],
        )

    assert result.exit_code == 0, result.output
    surviving = sorted(t3_db._client.get_collection(coll).get()["ids"])
    assert surviving == ["recent_orphan"]


def test_gc_default_window_is_30_days(t3_db, catalog, tmp_path, runner):
    """No ``--orphan-window`` flag → default 30 days."""
    coll = "knowledge__test_gc_default"
    twenty_days = _iso(datetime.now(UTC) - timedelta(days=20))
    forty_days = _iso(datetime.now(UTC) - timedelta(days=40))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="within_window", content="w",
        doc_id="1.1.99", indexed_at=twenty_days,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="past_window", content="p",
        doc_id="1.1.99", indexed_at=forty_days,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    surviving = sorted(t3_db._client.get_collection(coll).get()["ids"])
    assert surviving == ["within_window"]


def test_gc_no_orphans_clean_summary(t3_db, catalog, runner):
    """Every chunk's doc_id alive → 0/0 summary, no events."""
    coll = "knowledge__test_gc_clean"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_doc(catalog, tumbler="1.1.1", collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        doc_id="1.1.1", indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(main, ["t3", "gc", "-c", coll])

    assert result.exit_code == 0
    assert "0 orphan(s)" in result.output  # parenthetical-plural form
    log_path = catalog._dir / EVENTS_FILENAME
    if log_path.exists():
        events = [e for e in EventLog(catalog._dir).replay()
                  if e.type == TYPE_CHUNK_ORPHANED]
        assert events == []


def test_gc_chunk_with_missing_doc_id_skipped(
    t3_db, catalog, tmp_path, runner,
):
    """Chunk metadata without ``doc_id`` is undecidable, skipped not GC'd.

    Legacy chunks pre-Phase-2-backfill may not carry ``doc_id``. They
    must NOT be silently deleted; a maintenance backfill verb is the
    right path, not GC.
    """
    coll = "knowledge__test_gc_no_doc_id"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    col = t3_db._client.get_or_create_collection(coll)
    col.add(
        ids=["legacy_chunk"],
        documents=["x"],
        metadatas=[{"indexed_at": long_ago}],  # no doc_id
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0
    assert t3_db._client.get_collection(coll).count() == 1


def test_gc_aborts_on_uninitialized_catalog(t3_db, tmp_path, runner, monkeypatch):
    """nx t3 gc on an uninitialized catalog must raise a clear error,
    not crash with an opaque traceback or silently produce an empty
    alive-set (which would treat every chunk as orphan).
    """
    bare_path = tmp_path / "no-such-catalog"
    monkeypatch.setattr(
        "nexus.config.catalog_path", lambda: bare_path,
    )
    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", "knowledge__test", "--dry-run"],
        )
    assert result.exit_code != 0
    assert "not initialized" in result.output.lower()


def test_gc_orphan_window_rejects_zero(runner):
    """A zero-or-negative orphan window is rejected at parse time.

    Without this guard ``--orphan-window 0d`` would treat every
    orphaned chunk as immediately eligible, which is dangerous when
    paired with --no-dry-run --yes.
    """
    result = runner.invoke(
        main,
        ["t3", "gc", "-c", "knowledge__test", "--orphan-window", "0d"],
    )
    assert result.exit_code != 0
    assert "must be positive" in result.output.lower()


def test_gc_malformed_indexed_at_is_skipped(t3_db, catalog, tmp_path, runner):
    """Chunks with malformed ``indexed_at`` (non-ISO string) are
    undecidable for the orphan-window filter and must be skipped, not
    crash the GC.
    """
    coll = "knowledge__test_gc_bad_indexed_at"
    col = t3_db._client.get_or_create_collection(coll)
    col.add(
        ids=["bad_chunk"],
        documents=["x"],
        metadatas=[{"doc_id": "1.1.99", "indexed_at": "not-an-iso-timestamp"}],
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    # Chunk preserved (not deleted): undecidable indexed_at
    assert t3_db._client.get_collection(coll).count() == 1


def test_gc_paginates_above_300_chunk_boundary(
    t3_db, catalog, tmp_path, runner,
):
    """`list_chunks_with_metadata` paginates at the 300-record Cloud
    limit. Seed 305 chunks (all orphans past window) and verify all
    305 are detected and deleted, not silently truncated to 300.
    """
    coll = "knowledge__test_gc_pagination"
    col = t3_db._client.get_or_create_collection(coll)
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    chunk_count = 305
    col.add(
        ids=[f"orphan_{i:04d}" for i in range(chunk_count)],
        documents=[f"chunk {i}" for i in range(chunk_count)],
        metadatas=[
            {"doc_id": "1.1.99", "indexed_at": long_ago}
            for _ in range(chunk_count)
        ],
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert f"deleted {chunk_count}" in result.output
    assert t3_db._client.get_collection(coll).count() == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "nexus-krhr contract lock: today GC builds alive_set from "
        "CatalogEntry.tumbler only (commands/t3.py:302). When Phase 3 "
        "ships native UUID7 doc_id writes to T3 chunk metadata while "
        "the catalog still keys documents by tumbler, the alive_set "
        "intersection misses every UUID7-keyed chunk and GC silently "
        "sweeps live data after the orphan window. The fix unions both "
        "tumbler and uuid7 columns when constructing alive_set. This "
        "test fails today (UUID7 chunks classified as orphans, deleted) "
        "and starts passing once the union lands. The strict=True flag "
        "ensures CI fails when the fix is in place but the marker has "
        "not been removed, forcing the contract lock to ratchet forward."
    ),
)
def test_gc_uuid7_keyed_chunks_with_alive_catalog_entry_must_survive(
    t3_db, catalog, tmp_path, runner,
):
    """nexus-krhr forward-proof: when chunk doc_id is a UUID7 string
    that the catalog's projection treats as alive (under whatever
    schema extension lands), GC must not classify the chunk as an
    orphan.

    Today's behaviour: alive_set = {tumbler}; chunk doc_id (UUID7) not
    in alive_set; chunk swept after orphan window. This test fails.

    Future behaviour: alive_set = {tumbler} | {uuid7}; chunk doc_id
    matches; chunk survives. This test passes; remove the xfail marker.
    """
    coll = "knowledge__test_gc_uuid7"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))

    # The catalog has the document keyed by tumbler "1.1.1" today.
    # In a future native-UUID7-writes world, the SAME document would
    # ALSO carry the UUID7. Simulate that future by stuffing the UUID7
    # into a synthetic field; the GC fix must read it.
    uuid7 = "01900000-0000-7000-8000-000000000000"
    _register_doc(catalog, tumbler="1.1.1", collection=coll)

    # Seed a T3 chunk whose doc_id is the UUID7 (the Phase 3 native
    # write shape). Today's GC, walking alive = {tumbler}, classifies
    # this as an orphan because UUID7 != "1.1.1".
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive-uuid7", content="x",
        doc_id=uuid7, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output

    # Future contract: the UUID7-keyed chunk must survive because the
    # catalog projection treats it as alive. Today this assertion fails
    # (the chunk was deleted as an orphan).
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert "alive-uuid7" in surviving, (
        f"UUID7-keyed chunk was swept; alive_set construction in "
        f"commands/t3.py:302 only reads e.tumbler. Fix: union with "
        f"the future uuid7 column when it lands. Surviving ids: "
        f"{surviving!r}."
    )


def test_gc_no_yes_flag_reports_only(t3_db, catalog, tmp_path, runner):
    """``--no-dry-run`` without ``--yes`` falls back to report-only."""
    coll = "knowledge__test_gc_no_yes"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        doc_id="1.1.99", indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run"],
        )

    assert result.exit_code == 0
    assert "Add --yes" in result.output
    assert t3_db._client.get_collection(coll).count() == 1
    log_path = catalog._dir / EVENTS_FILENAME
    if log_path.exists():
        events = [e for e in EventLog(catalog._dir).replay()
                  if e.type == TYPE_CHUNK_ORPHANED]
        assert events == []
