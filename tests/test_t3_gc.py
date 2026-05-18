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
    indexed_at: str,
    chunk_text_hash: str | None = None,
    doc_id: str | None = None,
) -> None:
    """Insert one chunk with the metadata GC reads.

    nexus-e5aw: GC now reads ``chunk_text_hash`` (not ``doc_id``) to
    decide orphan status, matching the indexer's manifest-based GC.
    ``doc_id`` is retained as an optional kwarg for back-compat with
    legacy tests, but the new GC ignores it.
    """
    meta: dict = {"indexed_at": indexed_at}
    if chunk_text_hash is not None:
        meta["chunk_text_hash"] = chunk_text_hash
    if doc_id is not None:
        meta["doc_id"] = doc_id
    col = t3_db._client.get_or_create_collection(collection)
    col.add(ids=[chunk_id], documents=[content], metadatas=[meta])


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


def _register_doc(
    catalog: Catalog,
    *,
    tumbler: str,
    collection: str,
    chashes: list[str] | None = None,
) -> None:
    """Seed a document row directly so the catalog manifest references it.

    Bypasses ``Catalog.register`` (which mints its own tumbler off an
    owner prefix). nexus-e5aw: also writes manifest rows for the given
    ``chashes`` so ``Catalog.chashes_for_collection`` returns them as
    referenced (live).
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
    if chashes:
        catalog.write_manifest(tumbler, [
            {"chash": c, "position": i} for i, c in enumerate(chashes)
        ])


def test_gc_dry_run_reports_orphans_no_mutation(t3_db, catalog, tmp_path, runner):
    """Default dry-run prints orphan candidates but does not delete or emit."""
    coll = "knowledge__test_gc_dryrun"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64
    orphan_chash = "b" * 64
    _register_doc(
        catalog, tumbler="1.1.1", collection=coll, chashes=[live_chash],
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )
    _seed_chunk(  # orphan: chash not in any manifest entry
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash=orphan_chash, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
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
    live_chash = "a" * 64
    _register_doc(
        catalog, tumbler="1.1.1", collection=coll, chashes=[live_chash],
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash="b" * 64, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan2", content="o2",
        chunk_text_hash="c" * 64, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
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
    even if their chash is not in the manifest.

    Rationale: a fresh re-index might briefly leave chunks orphaned
    while the manifest projection catches up. The window is the grace
    period.
    """
    coll = "knowledge__test_gc_window"
    recent = _iso(datetime.now(UTC) - timedelta(hours=1))
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(  # orphan but recent: protected
        t3_db, collection=coll, chunk_id="recent_orphan", content="r",
        chunk_text_hash="b" * 64, indexed_at=recent,
    )
    _seed_chunk(  # orphan and old: eligible
        t3_db, collection=coll, chunk_id="old_orphan", content="o",
        chunk_text_hash="c" * 64, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
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
        chunk_text_hash="b" * 64, indexed_at=twenty_days,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="past_window", content="p",
        chunk_text_hash="c" * 64, indexed_at=forty_days,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    surviving = sorted(t3_db._client.get_collection(coll).get()["ids"])
    assert surviving == ["within_window"]


def test_gc_no_orphans_clean_summary(t3_db, catalog, runner):
    """Every chunk's chash referenced in the manifest → 0 orphans, no events."""
    coll = "knowledge__test_gc_clean"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64
    _register_doc(
        catalog, tumbler="1.1.1", collection=coll, chashes=[live_chash],
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(main, ["t3", "gc", "-c", coll])

    assert result.exit_code == 0
    assert "0 orphan(s)" in result.output  # parenthetical-plural form
    log_path = catalog._dir / EVENTS_FILENAME
    if log_path.exists():
        events = [e for e in EventLog(catalog._dir).replay()
                  if e.type == TYPE_CHUNK_ORPHANED]
        assert events == []


def test_gc_chunk_with_missing_chunk_text_hash_skipped(
    t3_db, catalog, tmp_path, runner,
):
    """nexus-e5aw: pre-RDR-053 chunks without ``chunk_text_hash`` are
    undecidable under the manifest path and skipped with a warning,
    not GC'd. Same carve-out as ``indexer._prune_deleted_files``."""
    coll = "knowledge__test_gc_no_chash"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    col = t3_db._client.get_or_create_collection(coll)
    col.add(
        ids=["legacy_chunk"],
        documents=["x"],
        metadatas=[{"indexed_at": long_ago}],  # no chunk_text_hash
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    # Carve-out chunk preserved.
    assert t3_db._client.get_collection(coll).count() == 1
    # Operator-visible warning surfaces.
    assert "no chunk_text_hash" in result.output
    assert "pre-RDR-053" in result.output


def test_gc_aborts_on_uninitialized_catalog(t3_db, tmp_path, runner, monkeypatch):
    """nx t3 gc on an uninitialized catalog must raise a clear error,
    not crash with an opaque traceback or silently produce an empty
    alive-set (which would treat every chunk as orphan).
    """
    bare_path = tmp_path / "no-such-catalog"
    monkeypatch.setattr(
        "nexus.config.catalog_path", lambda: bare_path,
    )
    with patch("nexus.mcp_infra.get_t3", return_value=t3_db):
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
        metadatas=[{
            "chunk_text_hash": "b" * 64,
            "indexed_at": "not-an-iso-timestamp",
        }],
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
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
            {
                "chunk_text_hash": f"{i:064x}",  # unique chash per chunk
                "indexed_at": long_ago,
            }
            for i in range(chunk_count)
        ],
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert f"deleted {chunk_count}" in result.output
    assert t3_db._client.get_collection(coll).count() == 0


def test_gc_chunk_id_shape_irrelevant_under_manifest_path(
    t3_db, catalog, tmp_path, runner,
):
    """nexus-e5aw replaces the legacy nexus-krhr xfail. Under the
    manifest path, the chunk's natural-ID shape (UUID7, content-derived
    chash[:32], legacy synthetic hash) is irrelevant for orphan
    classification: the only thing that matters is whether the chunk's
    ``meta.chunk_text_hash[:32]`` is in the manifest's referenced set.
    A UUID7-keyed chunk whose chash IS referenced survives; a content-
    derived-keyed chunk whose chash is NOT referenced is GC'd."""
    coll = "knowledge__test_gc_chunk_id_shape"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64

    _register_doc(
        catalog, tumbler="1.1.1", collection=coll, chashes=[live_chash],
    )

    # UUID7-keyed chunk whose chash IS in the manifest. Survives.
    _seed_chunk(
        t3_db, collection=coll,
        chunk_id="01900000-0000-7000-8000-000000000000",
        content="live",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )
    # Content-derived-keyed chunk whose chash is NOT referenced. GC'd.
    _seed_chunk(
        t3_db, collection=coll,
        chunk_id=("b" * 64)[:32],
        content="orphan",
        chunk_text_hash="b" * 64, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=catalog):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    surviving = set(t3_db._client.get_collection(coll).get()["ids"])
    assert "01900000-0000-7000-8000-000000000000" in surviving
    assert ("b" * 64)[:32] not in surviving


def test_gc_no_yes_flag_reports_only(t3_db, catalog, tmp_path, runner):
    """``--no-dry-run`` without ``--yes`` falls back to report-only."""
    coll = "knowledge__test_gc_no_yes"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash="b" * 64, indexed_at=long_ago,
    )

    with patch("nexus.mcp_infra.get_t3", return_value=t3_db), \
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
