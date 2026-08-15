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
machinery without Cloud credentials.

CATALOG SUBSTRATE (nexus-i711w terminal deletion). The GC verb reads its
alive-set through ``make_catalog_reader()``, so the tests seed through the
SAME factory (``ActiveCatalog``) and let the command resolve the catalog for
itself — no ``_make_catalog`` patch, hence no seed-here / read-there split.
The two tests that were PINNED to the local SQLite catalog (the events.jsonl
audit trail and the uninitialized-catalog abort) RETIRED with it; tombstones
at their former sites carry the coverage warnings.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database
from tests._catalog_fixture_ops import ActiveCatalog, seed_manifest_chunks
from tests.conftest import make_vector_test_client


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed through whichever catalog is live (nexus-i711w Stage 2).

    Deliberately NOT the local ``Catalog`` this file used to build: the verb
    under test resolves its catalog via ``make_catalog_reader()``, so seeding
    anywhere else means the test writes one catalog while the command reads
    another and every alive-set comes back empty.
    """
    return ActiveCatalog()


# local_catalog fixture RETIRED with the local catalog (nexus-i711w
# terminal deletion); its two consumers (the events.jsonl audit-trail test
# and the uninitialized-catalog abort test) retired with it — see the
# tombstones at their former sites.


def _chunk_orphaned_events(catalog: Any) -> list | None:
    """``ChunkOrphaned`` events from *catalog*'s LOCAL event log.

    ``None`` means "there is nothing to read", which covers both cases the
    ``if events_path.exists()`` guard used to cover on its own plus the one it
    could not: a service-backed catalog has no ``_dir`` at all, so the old
    ``catalog._dir / EVENTS_FILENAME`` raised AttributeError BEFORE reaching
    ``.exists()``. Mirrors production, which resolves the log the same way
    (``commands/t3.py``: ``cat_dir = getattr(cat, "_dir", None)``) and skips
    emission entirely when it is absent.
    """
    cat_dir = getattr(catalog, "_dir", None)
    if cat_dir is None:
        return None
    # nexus-i711w terminal deletion: nexus.catalog.event_log / .events are
    # gone, so a catalog handle exposing a ``_dir`` would be a reintroduction
    # of the local event log — fail loud rather than pretend to read it.
    raise AssertionError(
        f"catalog handle unexpectedly exposes _dir={cat_dir!r}; the local "
        f"event log was deleted with the local catalog (nexus-i711w)"
    )


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


def _register_doc_active(
    catalog: Any,
    *,
    collection: str,
    chashes: list[str] | None = None,
) -> str:
    """Register a document through the ACTIVE catalog and point its manifest
    at *chashes*, so ``chashes_for_collection(collection)`` returns them as
    referenced (live). Returns the minted tumbler.

    The local-only ``_register_doc_local`` below pinned the literal tumbler
    ``"1.1.1"`` with raw SQL because ``register`` mints its own off an owner
    prefix. Nothing in this file ever asserted on the tumbler VALUE — the
    requirement is only that a document exists in *collection* whose manifest
    references the chashes — so the minted tumbler is used as-is and returned
    for any caller that wants it.
    """
    owner = catalog.register_owner("t3-gc-test", "curator")
    tumbler = catalog.register(
        owner,
        f"doc-{collection}",
        content_type="text",
        file_path=f"/tmp/{collection}.md",
        physical_collection=collection,
        chunk_count=len(chashes or []),
    )
    if chashes:
        # nexus-dbzxb (RDR-191 Phase 5 Python collateral): fk_catalog_
        # chunks_chunk requires a real nexus.chunks row per manifest chash;
        # seed one (idiom 1) — the "live" chash here represents a chunk
        # that IS present in T3 (in production; this fixture's T3 side is
        # a separate fake client), so a real bookkeeping stub is faithful
        # to the fixture's own "alive" semantics, unlike orphan_chash
        # (never referenced by the manifest, so never write_manifest'd).
        seed_manifest_chunks(collection, chashes)
        catalog.write_manifest(str(tumbler), [
            {"chash": c, "position": i} for i, c in enumerate(chashes)
        ], collection=collection)
    return str(tumbler)


# _register_doc_local RETIRED with the local catalog (nexus-i711w terminal
# deletion); its sole caller was the pinned event-log test, retired below.


def test_gc_dry_run_reports_orphans_no_mutation(
    t3_db, active_catalog, tmp_path, runner,
):
    """Default dry-run prints orphan candidates but does not delete or emit."""
    coll = "knowledge__test-gc-dryrun__bge-base-en-v15-768__v1"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64
    orphan_chash = "b" * 64
    _register_doc_active(
        active_catalog, collection=coll, chashes=[live_chash],
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )
    _seed_chunk(  # orphan: chash not in any manifest entry
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash=orphan_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--dry-run"],
        )

    assert result.exit_code == 0, result.output
    assert "orphan1" in result.output
    # Non-vacuous: alive1 is absent only because the command found the manifest
    # row seeded above. A seed that landed in a different catalog would report
    # alive1 as an orphan candidate and fail here.
    assert "alive1" not in result.output
    assert "would delete" in result.output

    # No T3 mutation
    assert t3_db._client.get_collection(coll).count() == 2

    # No event emitted (skipped when there is no local event log to read)
    events = _chunk_orphaned_events(active_catalog)
    if events is not None:
        assert events == []


# test_gc_emits_chunk_orphaned_event_before_delete RETIRED with the local
# catalog (nexus-i711w terminal deletion): ``events.jsonl`` WAS the subject
# (RF-101-3 strict order: emit ChunkOrphaned BEFORE delete). ⚠️ THE
# STRICT-ORDER CONTRACT IS UNCOVERED ON THE SERVICE ARM because on that arm
# it does not exist — the delete happens with no local audit record at all.


def test_gc_orphan_window_excludes_recent(t3_db, tmp_path, runner):
    """Chunks whose ``indexed_at`` is within the orphan window are not GC'd
    even if their chash is not in the manifest.

    Rationale: a fresh re-index might briefly leave chunks orphaned
    while the manifest projection catches up. The window is the grace
    period.

    nexus-i711w: no catalog fixture — nothing is seeded, so the active
    catalog's alive-set for this (per-test-unique) collection is empty and
    both chunks are orphans by chash. The window is what separates them.

    nexus-jqrtp: an empty alive-set now refuses by default (the guard this
    file's own xfail predicted), so this deliberately-empty-manifest
    scenario needs the explicit override.
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

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            [
                "t3", "gc", "-c", coll,
                "--orphan-window", "30d",
                "--no-dry-run", "--yes", "--allow-empty-manifest-set",
            ],
        )

    assert result.exit_code == 0, result.output
    surviving = sorted(t3_db._client.get_collection(coll).get()["ids"])
    assert surviving == ["recent_orphan"]


# ── nexus-39upx hazard 2 (RDR-145): manifest-less notes ─────────────────
#
# chashes_for_collection only sees chashes with a manifest row. A
# store_put / nx store put note never gets one (RDR-145 defers
# manifest-backed identity for notes; catalog-003-soft-delete.xml's
# live_chunks view treats a manifest-less chunk as live BY DESIGN), so
# without the fix a note's chash reads identically to a genuine re-index
# orphan: both are simply "not referenced".


def _register_note_active(
    catalog: Any, *, collection: str, chash: str, title: str,
) -> str:
    """Register a REAL RDR-145-shaped note through the ACTIVE catalog:
    store_put origin (content_type="knowledge", no file_path), the
    single-chunk natural id stamped into meta["doc_id"] — exactly what
    catalog/store_hook.py::single_chunk_manifest_metadata +
    catalog_store_hook_tracked write at store_put time, and what
    ``nx catalog doctor --store-put-integrity`` already reads back.
    Deliberately writes NO manifest row.
    """
    owner = catalog.register_owner(f"t3-gc-note-{collection}", "curator")
    tumbler = catalog.register(
        owner, title, content_type="knowledge", file_path="",
        physical_collection=collection, meta={"doc_id": chash},
    )
    return str(tumbler)


def test_gc_protects_manifest_less_note_from_deletion(
    t3_db, active_catalog, runner,
):
    """The load-bearing case: a note whose chash is not referenced by
    ANY manifest (not even a live one elsewhere) must still survive —
    chashes_for_collection alone cannot tell it apart from a genuine
    orphan."""
    coll = "knowledge__test_gc_note_protect"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    note_chash = "d" * 64
    _register_note_active(
        active_catalog, collection=coll, chash=note_chash, title="my note",
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="note1", content="a real note",
        chunk_text_hash=note_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    # The note alone makes `referenced` non-empty (its chash is now
    # protected), so this must succeed WITHOUT --allow-empty-manifest-set.
    assert result.exit_code == 0, result.output
    assert "protecting 1 manifest-less note chunk(s)" in result.output
    assert "note1" not in result.output
    assert t3_db._client.get_collection(coll).get()["ids"] == ["note1"]


def test_gc_deletes_genuine_orphan_while_protecting_a_note_in_the_same_collection(
    t3_db, active_catalog, runner,
):
    """Non-vacuous in both directions: the note guard must not become a
    blanket refusal that also hides a genuine orphan sharing the run."""
    coll = "knowledge__test_gc_note_and_orphan"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    note_chash = "e" * 64
    orphan_chash = "f" * 64
    _register_note_active(
        active_catalog, collection=coll, chash=note_chash, title="my other note",
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="note1", content="a real note",
        chunk_text_hash=note_chash, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="stale",
        chunk_text_hash=orphan_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert surviving == ["note1"]


def test_gc_dry_run_never_lists_a_protected_note_as_a_candidate(
    t3_db, active_catalog, runner,
):
    coll = "knowledge__test_gc_note_dryrun"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    note_chash = "1" * 64
    _register_note_active(
        active_catalog, collection=coll, chash=note_chash, title="dry run note",
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="note1", content="a real note",
        chunk_text_hash=note_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "gc", "-c", coll, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "0 orphan chunk(s) eligible" in result.output
    assert "note1" not in result.output


def test_gc_refuses_when_note_lookup_fails(t3_db, active_catalog, runner):
    """FAIL LOUD, not fail-open: this is the operator-driven --yes path,
    so an unverifiable note-set must refuse the run rather than risk
    deleting live notes."""
    coll = "knowledge__test_gc_note_lookup_fails"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="stale",
        chunk_text_hash="2" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
            patch(
                "nexus.indexer_utils.catalog_documents_for_collection",
                side_effect=RuntimeError("engine down"),
            ):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code != 0
    assert "Failed to verify manifest-less note protection" in result.output
    # Refused BEFORE any delete.
    assert t3_db._client.get_collection(coll).get()["ids"] == ["orphan1"]


# ── nexus-g6k6b (nexus-39upx round 2 CRITICAL): RUNFENCE index_state
# precondition ──────────────────────────────────────────────────────────
#
# Bead nexus-39upx's own comment thread (Hal, 2026-08-02 11:23), binding:
# "The corpus-wide sweep (b) MUST filter on index_state = 'complete'.
# Sweeping a document that is mid-index would delete chunks an in-flight
# run has already written but has not yet manifested." A T3 chunk carries
# no doc_id (post-RDR-108), so an orphan candidate cannot be attributed to
# the specific document that most recently owned it — nx t3 gc therefore
# refuses the WHOLE collection's destructive run when it contains ANY
# document not index_state='complete', rather than guessing per-candidate.


def _register_indexing_doc_active(
    catalog: Any, *, collection: str, run_id: str = "test-run",
) -> str:
    """Register a document and stamp index_state='indexing' via the real
    RUNFENCE begin_index_run write op — the shape a genuinely in-flight
    reindex leaves behind."""
    owner = catalog.register_owner(f"t3-gc-indexing-{collection}", "curator")
    tumbler = catalog.register(
        owner, f"doc-{collection}", content_type="text",
        file_path=f"/tmp/{collection}.md", physical_collection=collection,
    )
    catalog.begin_index_run(str(tumbler), "content-hash-1", run_id, collection)
    return str(tumbler)


def _register_failed_doc_active(catalog: Any, *, collection: str) -> str:
    """Register a document and stamp index_state='failed' via the real
    RUNFENCE fail_index_run write op."""
    owner = catalog.register_owner(f"t3-gc-failed-{collection}", "curator")
    tumbler = catalog.register(
        owner, f"doc-{collection}", content_type="text",
        file_path=f"/tmp/{collection}.md", physical_collection=collection,
    )
    catalog.fail_index_run(str(tumbler), "extraction error")
    return str(tumbler)


def test_gc_refuses_when_a_document_is_actively_indexing(
    t3_db, active_catalog, runner,
):
    """The core hazard Hal named: a live in-flight run. A chunk that
    would otherwise classify as a genuine orphan must be protected
    (never physically deleted) while ANY document in the collection is
    mid-index."""
    coll = "knowledge__test_gc_g6k6b_indexing"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_indexing_doc_active(active_catalog, collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="would_be_orphan", content="stale",
        chunk_text_hash="3" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        # --allow-empty-manifest-set: the fixture writes no manifest, so
        # without it the PRE-EXISTING nexus-jqrtp empty-manifest refusal
        # fires first and this test passes without the g6k6b breaker ever
        # running (critique T2 [21515] round 2 falsified exactly that).
        # Bypassing jqrtp leaves g6k6b's refusal as the ONLY non-zero path.
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
                   "--allow-empty-manifest-set"],
        )

    assert result.exit_code != 0
    assert "REFUSING to delete" in result.output
    assert "not index_state='complete'" in result.output
    assert "--allow-incomplete-index-state" in result.output
    # Refused BEFORE any delete — the chunk survives.
    assert t3_db._client.get_collection(coll).get()["ids"] == ["would_be_orphan"]


def test_gc_refuses_when_a_document_is_fence_failed(t3_db, active_catalog, runner):
    """A fenced failure — its manifest may be a partial artifact of the
    first-batch atomic_manifest_replace, same hazard shape as indexing."""
    coll = "knowledge__test_gc_g6k6b_failed"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_failed_doc_active(active_catalog, collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="would_be_orphan", content="stale",
        chunk_text_hash="4" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        # --allow-empty-manifest-set for the same reason as the
        # actively-indexing test above: keep the jqrtp guard from masking
        # the g6k6b refusal under test.
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
                   "--allow-empty-manifest-set"],
        )

    assert result.exit_code != 0
    assert "REFUSING to delete" in result.output
    assert "not index_state='complete'" in result.output
    assert t3_db._client.get_collection(coll).get()["ids"] == ["would_be_orphan"]


def test_gc_override_flag_allows_deletion_despite_incomplete_index_state(
    t3_db, active_catalog, runner,
):
    """The escape hatch, mirroring --allow-empty-manifest-set: once the
    operator has confirmed no reindex is concurrently running, the
    override lets the (otherwise-genuine) orphan get deleted."""
    coll = "knowledge__test_gc_g6k6b_override"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_indexing_doc_active(active_catalog, collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="genuine_orphan", content="stale",
        chunk_text_hash="5" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
                   "--allow-incomplete-index-state", "--allow-empty-manifest-set"],
        )

    assert result.exit_code == 0, result.output
    assert t3_db._client.get_collection(coll).get()["ids"] == []


def test_gc_dry_run_reports_the_non_complete_document_count_loudly(
    t3_db, active_catalog, runner,
):
    """Capability-honest, non-silent: even a report-only run must NAME
    the blocker so an operator planning a real run sees it coming."""
    coll = "knowledge__test_gc_g6k6b_dryrun"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _register_indexing_doc_active(active_catalog, collection=coll)
    _seed_chunk(
        t3_db, collection=coll, chunk_id="would_be_orphan", content="stale",
        chunk_text_hash="6" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "gc", "-c", coll, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 document(s)" in result.output
    assert "not index_state='complete'" in result.output
    assert "'indexing'" in result.output


def test_gc_complete_document_is_never_a_blocker(t3_db, active_catalog, runner):
    """Non-vacuous companion: a document that HAS completed its index
    run must not spuriously block the collection.

    The RUNFENCE completion verify (``complete_index_run``) checks T3
    presence against the SAME Postgres the engine's catalog service
    reads — a datastore this unit test's local ``t3_db`` fixture does
    not write to, so a genuine begin/complete round-trip cannot succeed
    here (it would 409 with ``missing=1``). Patches the collection-documents
    fetch to return a real-shaped ``index_state='complete'`` entry instead,
    isolating THIS test to gc_cmd's classification wiring — the fence's
    OWN verify-then-stamp correctness is the RUNFENCE arc's own test
    suite's job, not this bead's.
    """
    from types import SimpleNamespace

    coll = "knowledge__test-gc-g6k6b-complete__bge-base-en-v15-768__v1"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "7" * 64
    orphan_chash = "8" * 64
    _register_doc_active(active_catalog, collection=coll, chashes=[live_chash])
    _seed_chunk(
        t3_db, collection=coll, chunk_id="alive1", content="a",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash=orphan_chash, indexed_at=long_ago,
    )
    complete_entry = SimpleNamespace(
        title=f"doc-{coll}", file_path=f"/tmp/{coll}.md", meta={},
        index_state="complete", index_state_reported=True,
    )

    with patch("nexus.db.make_t3", return_value=t3_db), \
            patch("nexus.indexer_utils.catalog_documents_for_collection",
                  return_value=[complete_entry]):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert t3_db._client.get_collection(coll).get()["ids"] == ["alive1"]


def test_gc_note_shaped_document_never_blocks_despite_none_index_state(
    t3_db, active_catalog, runner,
):
    """Hal, verbatim: store_put-origin documents never carry index_state
    and 'their chunks are never swept: over-retention, not deletion...
    is correct' — that must be their ONLY protection (via
    live_note_chashes), not an additional collection-wide refusal that
    would make every knowledge collection holding even one note
    permanently block gc."""
    coll = "knowledge__test_gc_g6k6b_note_no_block"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    note_chash = "9" * 64
    orphan_chash = "0" * 64
    _register_note_active(active_catalog, collection=coll, chash=note_chash, title="a note")
    _seed_chunk(
        t3_db, collection=coll, chunk_id="note1", content="a real note",
        chunk_text_hash=note_chash, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash=orphan_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert surviving == ["note1"]


def test_gc_default_window_is_30_days(t3_db, tmp_path, runner):
    """No ``--orphan-window`` flag → default 30 days.

    nexus-i711w: no catalog fixture — nothing seeded, so both chunks are
    orphans by chash and only the default window separates them.

    nexus-jqrtp: an empty alive-set now refuses by default, so this
    deliberately-empty-manifest scenario needs the explicit override.
    """
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

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
             "--allow-empty-manifest-set"],
        )

    assert result.exit_code == 0, result.output
    surviving = sorted(t3_db._client.get_collection(coll).get()["ids"])
    assert surviving == ["within_window"]


def test_gc_no_orphans_clean_summary(t3_db, active_catalog, runner):
    """Every chunk's chash referenced in the manifest → 0 orphans, no events."""
    coll = "knowledge__test-gc-clean__bge-base-en-v15-768__v1"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64
    _register_doc_active(
        active_catalog, collection=coll, chashes=[live_chash],
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        chunk_text_hash=live_chash, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(main, ["t3", "gc", "-c", coll])

    assert result.exit_code == 0
    # Non-vacuous: "0 orphan(s)" holds only because the command resolved the
    # manifest row seeded above. A seed the command could not see would report
    # 1 orphan.
    assert "0 orphan(s)" in result.output  # parenthetical-plural form
    events = _chunk_orphaned_events(active_catalog)
    if events is not None:
        assert events == []


def test_gc_chunk_with_missing_chunk_text_hash_skipped(
    t3_db, tmp_path, runner,
):
    """nexus-e5aw: pre-RDR-053 chunks without ``chunk_text_hash`` are
    undecidable under the manifest path and skipped with a warning,
    not GC'd. Same carve-out as ``indexer._prune_deleted_files``.

    nexus-i711w: no catalog fixture — the carve-out fires before the manifest
    is consulted at all, so an empty alive-set is the strictest premise
    (nothing protects the chunk except the carve-out itself).
    """
    coll = "knowledge__test_gc_no_chash"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    col = t3_db._client.get_or_create_collection(coll)
    col.add(
        ids=["legacy_chunk"],
        documents=["x"],
        metadatas=[{"indexed_at": long_ago}],  # no chunk_text_hash
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
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


# test_gc_aborts_on_uninitialized_catalog RETIRED with the local catalog
# (nexus-i711w terminal deletion): its premise (make_catalog_reader() is
# None) is UNREPRESENTABLE service-side — the factory always returns a
# handle. ⚠️ The hazard it guarded remains UNGUARDED on the service arm,
# filed as nexus-jqrtp (P1): an empty ``chashes_for_collection()`` return
# (fresh or mis-scoped tenant) makes every chunk an orphan candidate; the
# remaining defence layers are --dry-run default, --yes, --orphan-window.


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


def test_gc_malformed_indexed_at_is_skipped(t3_db, tmp_path, runner):
    """Chunks with malformed ``indexed_at`` (non-ISO string) are
    undecidable for the orphan-window filter and must be skipped, not
    crash the GC.

    nexus-i711w: no catalog fixture — the empty alive-set makes the chunk an
    orphan by chash, so only the malformed-timestamp skip preserves it.
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

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    # Chunk preserved (not deleted): undecidable indexed_at
    assert t3_db._client.get_collection(coll).count() == 1


def test_gc_paginates_above_300_chunk_boundary(
    t3_db, tmp_path, runner,
):
    """`list_chunks_with_metadata` paginates at the 300-record Cloud
    limit. Seed 305 chunks (all orphans past window) and verify all
    305 are detected and deleted, not silently truncated to 300.

    nexus-i711w: no catalog fixture — every chunk must be an orphan for the
    305-vs-300 count to mean anything, which an empty alive-set guarantees.

    nexus-jqrtp: an empty alive-set now refuses by default, so this
    deliberately-empty-manifest scenario needs the explicit override.
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

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
             "--allow-empty-manifest-set"],
        )

    assert result.exit_code == 0, result.output
    assert f"deleted {chunk_count}" in result.output
    assert t3_db._client.get_collection(coll).count() == 0


def test_gc_chunk_id_shape_irrelevant_under_manifest_path(
    t3_db, active_catalog, tmp_path, runner,
):
    """nexus-e5aw replaces the legacy nexus-krhr xfail. Under the
    manifest path, the chunk's natural-ID shape (UUID7, content-derived
    chash[:32], legacy synthetic hash) is irrelevant for orphan
    classification: the only thing that matters is whether the chunk's
    ``meta.chunk_text_hash[:32]`` is in the manifest's referenced set.
    A UUID7-keyed chunk whose chash IS referenced survives; a content-
    derived-keyed chunk whose chash is NOT referenced is GC'd."""
    coll = "knowledge__test-gc-chunk-id-shape__bge-base-en-v15-768__v1"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    live_chash = "a" * 64

    _register_doc_active(
        active_catalog, collection=coll, chashes=[live_chash],
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

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            # nexus-g6k6b: _register_doc_active never stamps the RUNFENCE
            # fence, so this document's index_state reads None (unknown) —
            # this test's own subject is chunk-ID-shape irrelevance, not
            # RUNFENCE, so the override is the correct, narrow way to keep
            # it independent of that (separately and thoroughly tested)
            # concern.
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
             "--allow-incomplete-index-state"],
        )

    assert result.exit_code == 0, result.output
    surviving = set(t3_db._client.get_collection(coll).get()["ids"])
    assert "01900000-0000-7000-8000-000000000000" in surviving
    assert ("b" * 64)[:32] not in surviving


def test_gc_no_yes_flag_reports_only(t3_db, active_catalog, tmp_path, runner):
    """``--no-dry-run`` without ``--yes`` falls back to report-only."""
    coll = "knowledge__test_gc_no_yes"
    long_ago = _iso(datetime.now(UTC) - timedelta(days=60))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="orphan1", content="o",
        chunk_text_hash="b" * 64, indexed_at=long_ago,
    )

    with patch("nexus.db.make_t3", return_value=t3_db):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run"],
        )

    assert result.exit_code == 0
    assert "Add --yes" in result.output
    assert t3_db._client.get_collection(coll).count() == 1
    events = _chunk_orphaned_events(active_catalog)
    if events is not None:
        assert events == []


class TestGetEmbeddingsRequestOrder:
    """nexus-pebfx.7 critic: Chroma's col.get(ids=...) returns rows in
    INTERNAL insertion order, not request order — positional consumption
    misattributed embeddings whenever the orders differed (false
    contradiction flags, wrong cluster geometry). T3Database.get_embeddings
    must reorder to request order, exactly like the service path."""

    def test_rows_follow_request_order_not_insertion_order(self):
        import numpy as np
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        from nexus.db.t3 import T3Database

        client = make_vector_test_client()
        t3 = T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())
        col = t3.get_or_create_collection("knowledge__ordertest", strict=False)
        # Insert in REVERSE alphabetical order so insertion order != request
        # order for the ["id-a", "id-b"] request below.
        col.add(
            ids=["id-b", "id-a"],
            documents=["text for b", "text for a"],
            metadatas=[{"k": "b"}, {"k": "a"}],
        )
        direct = col.get(ids=["id-a", "id-b"], include=["embeddings"])
        by_id = dict(zip(direct["ids"], direct["embeddings"]))

        result = t3.get_embeddings("knowledge__ordertest", ["id-a", "id-b"])
        assert result.shape[0] == 2
        assert np.allclose(result[0], np.array(by_id["id-a"], dtype=np.float32))
        assert np.allclose(result[1], np.array(by_id["id-b"], dtype=np.float32))

    def test_missing_ids_dropped(self):
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        from nexus.db.t3 import T3Database

        client = make_vector_test_client()
        t3 = T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())
        col = t3.get_or_create_collection("knowledge__ordertest2", strict=False)
        col.add(ids=["only"], documents=["text"], metadatas=[{"k": "v"}])
        result = t3.get_embeddings("knowledge__ordertest2", ["only", "absent"])
        assert result.shape[0] == 1


def test_gc_refuses_when_manifest_references_nothing_but_chunks_exist(
    runner: CliRunner, t3_db
) -> None:
    """nexus-jqrtp: the empty-alive-set guard, finally reachable.

    _make_catalog()'s init gate was written to stop exactly this catastrophe —
    an empty alive-set makes EVERY chunk an orphan, which with --no-dry-run
    --yes deletes the whole collection. But it tested ``cat is None``, a
    condition only a SQLite opt-out install could produce; in service mode the
    factory always returns a handle, so the guard could never fire and the
    hazard was unguarded on every shipping configuration.

    A first attempt at guarding the CONDITION (chunks present, manifest
    referencing none) was reverted 2026-07-31: it also refused the
    LEGITIMATE fully-orphaned collection several other gc tests construct on
    purpose, and separating the two needs a per-collection document count
    the client has no read for. The shipped fix is the other option that
    reason left open: an explicit opt-out (--allow-empty-manifest-set) that
    changes the destructive verb's default. Without it, GC must refuse and
    name the repair.
    """
    from unittest.mock import MagicMock

    coll = "code__jqrtp-guard__stub-code-1024__v1"
    long_ago = "2020-01-01T00:00:00Z"
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        chunk_text_hash="a" * 64, indexed_at=long_ago,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c2", content="y",
        chunk_text_hash="b" * 64, indexed_at=long_ago,
    )

    # A catalog whose manifest knows nothing about this collection.
    empty_cat = MagicMock()
    empty_cat.chashes_for_collection.return_value = set()

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=empty_cat):
        result = runner.invoke(main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"])

    assert result.exit_code == 1, result.output
    assert "REFUSING" in result.output
    assert "nx catalog reconcile" in result.output
    # THE POINT: nothing was deleted despite --no-dry-run --yes.
    assert t3_db._client.get_collection(coll).count() == 2


def test_gc_allow_empty_manifest_set_overrides_the_refusal(
    runner: CliRunner, t3_db
) -> None:
    """nexus-jqrtp: the explicit override proceeds despite the empty
    alive-set — the operator's own confirmation that this collection really
    is fully orphaned."""
    from unittest.mock import MagicMock

    coll = "code__jqrtp-guard-override__stub-code-1024__v1"
    long_ago = "2020-01-01T00:00:00Z"
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        chunk_text_hash="a" * 64, indexed_at=long_ago,
    )

    empty_cat = MagicMock()
    empty_cat.chashes_for_collection.return_value = set()

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=empty_cat):
        result = runner.invoke(
            main,
            ["t3", "gc", "-c", coll, "--no-dry-run", "--yes",
             "--allow-empty-manifest-set"],
        )

    assert result.exit_code == 0, result.output
    assert "REFUSING" not in result.output
    assert t3_db._client.get_collection(coll).count() == 0


def test_gc_empty_collection_and_empty_alive_set_is_a_clean_noop(
    runner: CliRunner, t3_db
) -> None:
    """An empty COLLECTION (no chunks at all) with an empty alive-set is not
    the nexus-jqrtp hazard — there is nothing to mistakenly orphan. Must
    stay a clean "0 orphan(s)" no-op, never the refusal."""
    from unittest.mock import MagicMock

    coll = "code__jqrtp-guard-empty__stub-code-1024__v1"

    empty_cat = MagicMock()
    empty_cat.chashes_for_collection.return_value = set()

    with patch("nexus.db.make_t3", return_value=t3_db), \
         patch("nexus.commands.t3._make_catalog", return_value=empty_cat):
        result = runner.invoke(
            main, ["t3", "gc", "-c", coll, "--no-dry-run", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert "REFUSING" not in result.output
    assert "0 orphan(s)" in result.output
