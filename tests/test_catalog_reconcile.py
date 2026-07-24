# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1371: ``nx catalog reconcile`` repairs document_chunks manifest gaps
left by a persistently-failed manifest_write_batch_hook.

A gap is: ``documents.chunk_count > 0`` but the document_chunks manifest
has fewer rows than chunk_count (including zero). The command rebuilds the
manifest from T3 chunk metadata, matching a document's chunks by the
whole-file ``content_hash`` stamped in ``documents.metadata`` and in every
chunk's T3 metadata (RDR-108 Phase 3 dropped doc_id/chunk_index from chunk
metadata, but content_hash + the char/line spans survive and are enough
to reconstruct both identity and order).
"""
from __future__ import annotations

import json

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main
from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def t3_db():
    db = T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )
    for raw in list(db._client.list_collections()):
        name = raw if isinstance(raw, str) else getattr(raw, "name", str(raw))
        try:
            db._client.delete_collection(name)
        except Exception:
            pass
    return db


@pytest.fixture()
def catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _seed_doc(
    catalog: Catalog, *, tumbler: str, collection: str, chunk_count: int,
    content_hash: str, file_path: str = "",
) -> None:
    meta = json.dumps({"content_hash": content_hash}) if content_hash else "{}"
    catalog._db.execute(  # epsilon-allow: fixture seeds a documents row with caller-pinned tumbler; Catalog.register mints its own owner-prefixed tumbler
        "INSERT INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", file_path or f"/tmp/{tumbler}.py",
            "", collection, chunk_count, "", "", meta, 0.0, "", "",
        ),
    )
    catalog._db.commit()


def _seed_chunks(t3_db: T3Database, collection: str, content_hash: str, n: int) -> list[str]:
    """Add n chunks sharing content_hash, with distinct char spans so
    ordering can be reconstructed, and return the chunk ids in file order."""
    col = t3_db._client.get_or_create_collection(collection)
    ids = [f"{content_hash}{i:02d}" for i in range(n)]
    metadatas = [
        {
            "content_hash": content_hash,
            "chunk_text_hash": ids[i],
            "chunk_start_char": i * 100,
            "chunk_end_char": (i + 1) * 100,
            "embedding_model": "voyage-code-3",
        }
        for i in range(n)
    ]
    # Insert in REVERSE order to prove the command sorts by span, not by
    # T3 insertion / return order.
    col.add(
        ids=list(reversed(ids)),
        documents=[f"chunk {i}" for i in reversed(range(n))],
        metadatas=list(reversed(metadatas)),
    )
    return ids


def test_reconcile_rebuilds_gapped_manifest(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="abc123",
    )
    _seed_chunks(t3_db, "code__delos", "abc123", 3)

    assert catalog.get_manifest("1.1.1") == []

    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 1 document(s); 0 with chunks LOST" in result.output

    rows = catalog.get_manifest("1.1.1")
    assert len(rows) == 3
    assert [r.position for r in rows] == [0, 1, 2]
    assert [r.chash for r in rows] == ["abc12300", "abc12301", "abc12302"]


def test_reconcile_dry_run_reports_without_writing(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="abc123",
    )
    _seed_chunks(t3_db, "code__delos", "abc123", 3)

    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Would reconcile 1 document(s); 0 with chunks LOST" in result.output
    assert catalog.get_manifest("1.1.1") == []


def test_reconcile_reports_unmatched_when_no_content_hash(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="",
    )
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 1 with chunks LOST" in result.output
    assert "1.1.1" in result.output


def test_reconcile_reports_unmatched_when_no_t3_chunks_found(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="nomatch",
    )
    # Seed unrelated chunks under a different content_hash.
    _seed_chunks(t3_db, "code__delos", "other999", 2)
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 1 with chunks LOST" in result.output


def test_reconcile_skips_documents_already_complete(t3_db, catalog, runner):
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=2, content_hash="complete1",
    )
    catalog.write_manifest("1.1.1", [
        {"chash": "complete100", "position": 0},
        {"chash": "complete101", "position": 1},
    ])
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 0 with chunks LOST" in result.output


def test_reconcile_no_gapped_documents_reports_zero(t3_db, catalog, runner):
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 0 with chunks LOST" in result.output


def patch_reconcile(t3_db, catalog, *, writer=None):
    from unittest.mock import patch as _patch
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(_patch("nexus.db.make_t3", return_value=t3_db))
    stack.enter_context(_patch("nexus.commands.catalog._get_catalog", return_value=catalog))
    stack.enter_context(
        _patch("nexus.commands.catalog._get_catalog_writer", return_value=writer or catalog)
    )
    return stack


class _UnsyncedManifestWriter:
    """Wraps a real ``Catalog`` but mimics
    ``HttpCatalogClient.atomic_manifest_replace``'s contract: writes the
    manifest rows WITHOUT resyncing ``documents.chunk_count`` unless
    ``new_chunk_count=`` is explicitly passed (see
    ``src/nexus/catalog/http_catalog_client.py``).

    The local-mode ``Catalog.atomic_manifest_replace`` re-derives
    ``chunk_count`` from the post-write row count inside the same SQL
    transaction, so the GH #1371 follow-up bug (reconcile never converges)
    cannot reproduce against a bare local ``Catalog`` — it is a real
    behavioral asymmetry between the two backends. This double isolates
    exactly that one asymmetry so the command-level convergence contract
    can be exercised with a real Catalog + real chromadb, without standing
    up an HTTP transport for the Java engine.
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def atomic_manifest_replace(self, doc_id, chunks, *, new_collection=None, new_chunk_count=None):
        self._catalog.write_manifest(doc_id, chunks)
        if new_chunk_count is not None:
            self._catalog.update(Tumbler.parse(doc_id), chunk_count=new_chunk_count)

    def resync_chunk_count_cache(self, doc_id):
        self._catalog.resync_chunk_count_cache(doc_id)

    def close(self):
        pass


def test_reconcile_converges_after_one_pass_service_mode_shaped(t3_db, catalog, runner):
    """GH #1371 follow-up: live-verified defect where ``nx catalog
    reconcile`` reported "Reconciled 36 document(s)" on TWO consecutive
    runs against the deployed cloud catalog, and a dry-run after that
    still reported the same 36. Root cause: ``atomic_manifest_replace``'s
    HTTP/service-mode path only resyncs ``documents.chunk_count`` when
    ``new_chunk_count=`` is passed, which the reconcile call site never
    did — so the gap detector (``len(manifest) < chunk_count``) re-flagged
    the same documents forever.

    Fixture covers both shapes from the live incident: a document whose
    manifest positions collapse onto fewer distinct T3 rows (duplicate
    chunk text, RDR-108) and a document with a stale-high chunk_count
    whose manifest was entirely empty (pure indexing-hook-failure gap).
    """
    # (a) Duplicate-text collapse: claims 3 chunks, only 2 distinct T3 rows.
    _seed_doc(
        catalog, tumbler="1.1.1", collection="code__delos",
        chunk_count=3, content_hash="dup1",
    )
    _seed_chunks(t3_db, "code__delos", "dup1", 2)

    # (b) Stale-high chunk_count, no dedup involved: claims 5, T3 has 5.
    _seed_doc(
        catalog, tumbler="1.1.2", collection="code__delos",
        chunk_count=5, content_hash="stale1",
    )
    _seed_chunks(t3_db, "code__delos", "stale1", 5)

    writer = _UnsyncedManifestWriter(catalog)

    with patch_reconcile(t3_db, catalog, writer=writer):
        first = runner.invoke(main, ["catalog", "reconcile"])
    assert first.exit_code == 0, first.output
    assert "Reconciled 2 document(s)" in first.output
    assert "chunk_count corrected 3 -> 2" in first.output
    assert "0 with chunks LOST" in first.output

    # Manifests + chunk_count both converged after one pass.
    assert len(catalog.get_manifest("1.1.1")) == 2
    assert len(catalog.get_manifest("1.1.2")) == 5
    assert catalog.resolve(Tumbler.parse("1.1.1")).chunk_count == 2
    assert catalog.resolve(Tumbler.parse("1.1.2")).chunk_count == 5

    # The bug: without the resync_chunk_count_cache fix, chunk_count stays
    # stale on this writer shape and the SAME documents get re-flagged.
    with patch_reconcile(t3_db, catalog, writer=writer):
        second = runner.invoke(main, ["catalog", "reconcile"])
    assert second.exit_code == 0, second.output
    assert "Reconciled 0 document(s); 0 with chunks LOST" in second.output

    with patch_reconcile(t3_db, catalog, writer=writer):
        third = runner.invoke(main, ["catalog", "reconcile", "--dry-run"])
    assert third.exit_code == 0, third.output
    assert "Would reconcile 0 document(s); 0 with chunks LOST" in third.output


# ── nexus-94fxl / GH #1397: chunk_count=0 ghost rows ─────────────────────────


def test_reconcile_heals_chunk_count_zero_ghost(t3_db, catalog, runner):
    """GH #1397 Run A: a document registered with chunk_count=0 and an empty
    manifest (the manifest hook dropped the batch for missing doc identity)
    whose T3 chunks DO exist must be healed — the old chunk_count>0 candidate
    filter excluded exactly these rows, making reconcile unable to repair the
    class it was pointed at."""
    _seed_doc(
        catalog, tumbler="1.3.142", collection="rdr__nexus",
        chunk_count=0, content_hash="ghost1",
    )
    _seed_chunks(t3_db, "rdr__nexus", "ghost1", 3)

    assert catalog.get_manifest("1.3.142") == []

    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 1 document(s)" in result.output

    rows = catalog.get_manifest("1.3.142")
    assert len(rows) == 3
    assert [r.position for r in rows] == [0, 1, 2]
    # chunk_count cache resynced from the rebuilt manifest: the ghost is now a
    # first-class document and search/GC no longer treat it as chunkless.
    entry = next(e for e in catalog.all_documents(limit=0) if str(e.tumbler) == "1.3.142")
    assert entry.chunk_count == 3


def test_reconcile_zero_ghost_without_content_hash_is_not_noise(t3_db, catalog, runner):
    """A chunk_count=0 row with NO content_hash is a legitimate ghost/planned
    entry (register-only, never indexed) — it must stay out of the candidate
    set entirely, not spam the unmatched report."""
    _seed_doc(
        catalog, tumbler="1.3.150", collection="rdr__nexus",
        chunk_count=0, content_hash="",
    )
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 0 document(s); 0 with chunks LOST" in result.output


def test_reconcile_zero_ghost_with_hash_but_no_t3_chunks_reported(t3_db, catalog, runner):
    """A chunk_count=0 row WITH a content_hash but no matching T3 chunks is
    anomalous (the file was hashed for indexing but its chunks are gone) —
    reported, not silently skipped. Critique d470eda1 refined the taxonomy:
    chunk_count==0 ghosts land in the never-chunked (expected) bucket — the
    live catalog holds ~8.6k of them (empty __init__.py etc.), and burying a
    REAL chunks-LOST regression under that noise was the defect. Still
    visible in the report, listed with the (empty) prefix."""
    _seed_doc(
        catalog, tumbler="1.3.151", collection="rdr__nexus",
        chunk_count=0, content_hash="vanished",
    )
    with patch_reconcile(t3_db, catalog):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "0 with chunks LOST (real gap), 1 never-chunked" in result.output
    assert "1.3.151" in result.output


def test_reconcile_batches_t3_fetches_per_collection(t3_db, catalog, runner):
    """nexus-8g0ch: the T3 fetch is batched per collection ($in over content
    hashes), never per document. The pre-fix shape issued 2-3 HTTP round
    trips PER gapped doc — 1h42m+ on a real 8.7k-gap service-mode catalog.
    Three gapped docs sharing one collection must cost exactly ONE
    _paginated_get call (3 unique hashes < the 64-hash batch size)."""
    from unittest.mock import patch as _patch  # noqa: PLC0415 — file pattern: deferred imports

    import nexus.indexer as indexer_mod  # noqa: PLC0415 — file pattern: deferred imports

    for i in range(1, 4):
        _seed_doc(
            catalog, tumbler=f"1.1.{i}", collection="code__delos",
            chunk_count=2, content_hash=f"hash{i:03d}",
        )
        _seed_chunks(t3_db, "code__delos", f"hash{i:03d}", 2)

    calls: list[dict] = []
    real = indexer_mod._paginated_get

    def counting(col, **kwargs):
        calls.append(kwargs.get("where") or {})
        return real(col, **kwargs)

    with patch_reconcile(t3_db, catalog), \
            _patch("nexus.indexer._paginated_get", side_effect=counting):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 3 document(s)" in result.output

    assert len(calls) == 1, f"expected ONE batched fetch, got {len(calls)}: {calls}"
    in_list = calls[0]["content_hash"]["$in"]
    assert sorted(in_list) == ["hash001", "hash002", "hash003"]
    # Progress emission (observability half of nexus-8g0ch): per-collection
    # line plus the scan header.
    assert "gapped" in result.output and "code__delos" in result.output


def test_reconcile_batch_fetch_failure_marks_batch_unmatched_and_continues(
    t3_db, catalog, runner,
):
    """Review d470eda1 Medium-2: the batch-failure path — a _paginated_get
    exception marks exactly that batch's docs unmatched (visible in the
    progress line, not just structlog), and docs in OTHER collections still
    reconcile. Parity with the old per-doc failure semantics."""
    from unittest.mock import patch as _patch  # noqa: PLC0415 — file pattern: deferred imports

    import nexus.indexer as indexer_mod  # noqa: PLC0415 — file pattern: deferred imports

    _seed_doc(catalog, tumbler="1.1.1", collection="code__broken",
              chunk_count=2, content_hash="deadhash")
    _seed_chunks(t3_db, "code__broken", "deadhash", 2)
    _seed_doc(catalog, tumbler="1.1.2", collection="code__healthy",
              chunk_count=2, content_hash="goodhash")
    _seed_chunks(t3_db, "code__healthy", "goodhash", 2)

    real = indexer_mod._paginated_get

    def failing_for_broken(col, **kwargs):
        if getattr(col, "name", "") == "code__broken":
            raise RuntimeError("simulated T3 fetch failure")
        return real(col, **kwargs)

    with patch_reconcile(t3_db, catalog), \
            _patch("nexus.indexer._paginated_get", side_effect=failing_for_broken):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    # The healthy collection reconciled; the broken batch's doc is unmatched.
    assert "Reconciled 1 document(s); 1 with chunks LOST" in result.output
    # Medium-1 fix: the failure is VISIBLE in the progress line.
    assert "1 doc(s) unmatched (fetch error)" in result.output
    assert catalog.get_manifest("1.1.2") != []
    assert catalog.get_manifest("1.1.1") == []


def test_reconcile_shared_content_hash_resolves_both_docs_from_one_fetch(
    t3_db, catalog, runner,
):
    """Two docs with IDENTICAL content_hash in one collection (the
    empty-__init__.py shape at scale): both must heal from the shared
    rows_by_hash bucket, with exactly one $in fetch issued."""
    from unittest.mock import patch as _patch  # noqa: PLC0415 — file pattern: deferred imports

    import nexus.indexer as indexer_mod  # noqa: PLC0415 — file pattern: deferred imports

    _seed_doc(catalog, tumbler="1.2.1", collection="code__delos",
              chunk_count=2, content_hash="samehash")
    _seed_doc(catalog, tumbler="1.2.2", collection="code__delos",
              chunk_count=2, content_hash="samehash")
    _seed_chunks(t3_db, "code__delos", "samehash", 2)

    calls: list = []
    real = indexer_mod._paginated_get

    def counting(col, **kwargs):
        calls.append(kwargs.get("where") or {})
        return real(col, **kwargs)

    with patch_reconcile(t3_db, catalog), \
            _patch("nexus.indexer._paginated_get", side_effect=counting):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 2 document(s)" in result.output
    assert len(calls) == 1  # the shared hash dedupes to ONE fetch
    assert [r.chash for r in catalog.get_manifest("1.2.1")] == \
        [r.chash for r in catalog.get_manifest("1.2.2")]


def test_reconcile_over_64_hashes_pages_the_in_predicate(t3_db, catalog, runner):
    """The 64-hash $in boundary: 65 unique hashes in one collection must
    produce exactly TWO batched fetches (64 + 1), never per-doc calls."""
    from unittest.mock import patch as _patch  # noqa: PLC0415 — file pattern: deferred imports

    import nexus.indexer as indexer_mod  # noqa: PLC0415 — file pattern: deferred imports

    for i in range(65):
        _seed_doc(catalog, tumbler=f"1.3.{i + 1}", collection="code__delos",
                  chunk_count=1, content_hash=f"bulk{i:04d}")
        _seed_chunks(t3_db, "code__delos", f"bulk{i:04d}", 1)

    calls: list = []
    real = indexer_mod._paginated_get

    def counting(col, **kwargs):
        calls.append(kwargs.get("where") or {})
        return real(col, **kwargs)

    with patch_reconcile(t3_db, catalog), \
            _patch("nexus.indexer._paginated_get", side_effect=counting):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "Reconciled 65 document(s)" in result.output
    assert len(calls) == 2
    assert len(calls[0]["content_hash"]["$in"]) == 64
    assert len(calls[1]["content_hash"]["$in"]) == 1


def test_reconcile_collection_unavailable_reports_and_continues(
    t3_db, catalog, runner,
):
    """get_collection() raising (collection deleted from the service — 14
    live occurrences on the 2026-07-13 run) emits the UNAVAILABLE progress
    line, marks that collection's docs unmatched, and the pass continues."""
    from unittest.mock import patch as _patch  # noqa: PLC0415 — file pattern: deferred imports

    _seed_doc(catalog, tumbler="1.4.1", collection="code__gone",
              chunk_count=2, content_hash="gonehash")
    _seed_doc(catalog, tumbler="1.4.2", collection="code__alive",
              chunk_count=2, content_hash="alivehash")
    _seed_chunks(t3_db, "code__alive", "alivehash", 2)

    real_get = t3_db.get_collection

    def failing_get(name):
        if name == "code__gone":
            raise ValueError(f"collection '{name}' not found in service")
        return real_get(name)

    with patch_reconcile(t3_db, catalog), \
            _patch.object(t3_db, "get_collection", side_effect=failing_get):
        result = runner.invoke(main, ["catalog", "reconcile"])
    assert result.exit_code == 0, result.output
    assert "code__gone: UNAVAILABLE — 1 doc(s) unmatched" in result.output
    assert "Reconciled 1 document(s)" in result.output
    assert "1 with chunks LOST (real gap)" in result.output
    assert catalog.get_manifest("1.4.2") != []
