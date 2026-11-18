# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 coverage baseline (nexus-7fku2.1.7; ``scripts/bench/taxonomy_coverage.py``) against the
real engine substrate: the script's measure path reads ``/v1/vectors/stats`` through
``HttpVectorClient`` and pages ``/v1/taxonomy/assignments/unassigned`` through
``HttpTaxonomyStore``, and the rows it computes are checked against what the test seeded.

The clients are built inside the test, after ``t2_service_env`` has pointed the process at the
minted substrate tenant, and handed to the script's functions; the script itself reads no
environment on this path.

One test, three sections, one tenant: the storage-touch ceiling counts tests that mint a tenant, and
the three checks seed collections of their own, so they share one without touching each other."""
from __future__ import annotations

import hashlib
import itertools

from nexus.db.http_vector_client import get_http_vector_client
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct

import bench.taxonomy_coverage as cov
from bench.taxonomy_coverage import build_report, measure_collections, rebuild_set

_PARTIAL = "knowledge__cov-partial__bge-base-en-v15-768__v1"
_FULL = "knowledge__cov-full__bge-base-en-v15-768__v1"
_BARE = "knowledge__cov-bare__bge-base-en-v15-768__v1"
_PAGED = "knowledge__cov-paged__bge-base-en-v15-768__v1"
_EMPTY = "knowledge__cov-empty__bge-base-en-v15-768__v1"
#: topics.id is one global sequence in the substrate database, so each seeded topic needs its own id.
_TOPIC_IDS = itertools.count(94001)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _seed(collection: str, n: int, *, topic: bool, assigned: int = 0) -> list[str]:
    """*n* manifest-backed chunks in *collection*; when *topic*, one topic whose centroid is the
    first chunk's own vector, and the first *assigned* chunks assigned to it."""
    client = get_http_vector_client()
    docs = [f"{collection} chunk {i} about lighthouses and tides" for i in range(n)]
    ids = [_chash(d) for d in docs]
    seed_chunks_direct(collection=collection, ids=ids, documents=docs, embed=True,
                       metadatas=[{} for _ in ids])
    cat = ActiveCatalog()
    owner = cat.register_owner("cov-owner", "curator")
    tumbler = str(cat.register(owner, f"{collection}-doc", content_type="knowledge",
                               physical_collection=collection, chunk_count=n))
    cat.write_manifest(tumbler, [{"chash": c, "position": i} for i, c in enumerate(ids)],
                       collection=collection)
    if not topic:
        return ids
    tax = HttpTaxonomyStore()
    topic_id = tax.import_topic(
        src_id=next(_TOPIC_IDS), label=f"{collection}-topic", parent_id=None, collection=collection,
        centroid_hash=None, doc_count=0, created_at="2026-11-17T00:00:00Z",
        review_status="pending", terms=None)
    vec = client.get_embeddings(collection, ids[:1])[0].tolist()
    tax._centroid.upsert([{"collection": collection, "topic_id": topic_id, "embedding": vec,
                           "label": f"{collection}-topic", "doc_count": 0}])
    for chash in ids[:assigned]:
        tax.assign_topic(chash, topic_id, "manual", source_collection=collection)
    return ids


def _measure_path_reads_back_what_was_seeded() -> None:
    ids = _seed(_PARTIAL, 10, topic=True, assigned=6)
    _seed(_FULL, 5, topic=True, assigned=5)
    _seed(_BARE, 3, topic=False)
    # a chunk no manifest references: stored, but neither live nor listed as unassigned
    orphan = "orphan chunk that no document owns"
    seed_chunks_direct(collection=_PARTIAL, ids=[_chash(orphan)], documents=[orphan], embed=True,
                       metadatas=[{}])

    vector_client = get_http_vector_client()
    taxonomy = HttpTaxonomyStore()
    names = [_PARTIAL, _FULL, _BARE, _EMPTY]

    # the candidate set is rebuilt from the engine's own topic list, as the script's main does
    assert rebuild_set(names, taxonomy) == [_PARTIAL, _FULL]

    rows = {r["collection"]: r for r in measure_collections(
        names, vector_client=vector_client, taxonomy=taxonomy)}

    partial = rows[_PARTIAL]
    assert (partial["live_chunks"], partial["unassigned"], partial["status"]) == (len(ids), 4, "ok")
    assert partial["coverage"] == 0.6
    assert partial["above_0_9"] is False
    assert partial["has_taxonomy"] is True
    assert partial["dims"] == [768]
    assert partial["non_live_chunks"] == 1  # the orphan: stored, not live
    assert partial["coverage_upper"] == 0.7
    assert partial["gate_uncertain"] is False

    full = rows[_FULL]
    assert (full["live_chunks"], full["unassigned"], full["status"]) == (5, 0, "ok")
    assert full["coverage"] == 1.0
    assert full["above_0_9"] is True

    bare = rows[_BARE]
    assert (bare["live_chunks"], bare["unassigned"], bare["status"]) == (3, None, "no_taxonomy")
    assert bare["coverage"] is None and bare["above_0_9"] is False
    assert bare["has_taxonomy"] is False

    empty = rows[_EMPTY]
    assert (empty["live_chunks"], empty["status"]) == (0, "no_chunks")
    assert empty["coverage"] is None and empty["above_0_9"] is False

    report = build_report(list(rows.values()), engine="substrate", date="2026-11-17", rebuild={})
    assert report["measured"] == 4
    assert report["above_0_9"] == 1
    assert report["not_routed_by_gate_names"] == [_PARTIAL, _BARE, _EMPTY]
    assert report["coverage_below_0_9"] == 1


def _paging_follows_next_after_across_more_than_one_page(monkeypatch) -> None:
    """The real engine pages at the 1000 cap; seeding that many embedded chunks is slow, so this
    drives the same loop against the real route at a smaller page size (the route takes any
    limit from 1 to 1000) and checks the total against the seeded truth."""
    _seed(_PAGED, 7, topic=True, assigned=2)
    taxonomy = HttpTaxonomyStore()
    monkeypatch.setattr(cov, "PAGE_SIZE", 3)
    result = cov.count_unassigned(taxonomy, _PAGED)
    assert result.unassigned == 5
    assert result.pages == 2  # 3, then 2 (a short page ends the cursor)


_TOMB = "knowledge__cov-tomb__bge-base-en-v15-768__v1"


def _a_chunk_whose_only_owner_is_tombstoned_is_listed_unassigned_but_not_live() -> None:
    """The two engine predicates differ here (the unassigned route needs a manifest reference, the
    stats count needs a live owner), so a row can read more unassigned than live. The script flags
    that row instead of clamping it."""
    ids = _seed(_TOMB, 4, topic=True, assigned=0)
    survivors = [f"{_TOMB} survivor {i}" for i in range(2)]
    seed_chunks_direct(collection=_TOMB, ids=[_chash(t) for t in survivors], documents=survivors,
                       embed=True, metadatas=[{}, {}])
    cat = ActiveCatalog()
    owner = cat.register_owner("cov-owner-2", "curator")
    live_doc = cat.register(owner, f"{_TOMB}-live", content_type="knowledge",
                            physical_collection=_TOMB, chunk_count=2)
    cat.write_manifest(str(live_doc), [{"chash": _chash(t), "position": i} for i, t in enumerate(survivors)],
                       collection=_TOMB)
    # tombstone the document that owns the first four chunks
    docs = [d for d in cat.list_by_collection(_TOMB) if d.title == f"{_TOMB}-doc"]
    assert len(docs) == 1
    cat.delete_document(docs[0].tumbler)

    stats = get_http_vector_client().collection_stats()
    row = measure_collections([_TOMB], vector_client=get_http_vector_client(), taxonomy=HttpTaxonomyStore())[0]
    stats_row = next(r for r in stats if r["name"] == _TOMB)
    assert (stats_row["count"], stats_row["stored_count"]) == (2, 6)  # the four tombstoned-owned chunks are stored, not live
    assert row["live_chunks"] == 2
    assert row["unassigned"] == 6  # the route still lists the tombstoned-owned chunks
    assert row["anomaly"] == "unassigned_exceeds_live"
    assert row["non_live_chunks"] == len(ids)
    assert row["coverage_upper"] == 0.0  # 6 listed less 4 non-live = 2 unassigned of 2 live: exact here
    assert row["above_0_9"] is False and row["gate_uncertain"] is False


def test_the_measure_path_against_the_real_engine(t2_service_env, monkeypatch) -> None:
    _measure_path_reads_back_what_was_seeded()
    _paging_follows_next_after_across_more_than_one_page(monkeypatch)
    _a_chunk_whose_only_owner_is_tombstoned_is_listed_unassigned_but_not_live()
