# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for persistent topic taxonomy (RDR-061 P3-1, nexus-vk8m; RDR-070 nexus-9k5)."""
from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
import pytest

from nexus.db.storage_mode import has_raw_access
from nexus.db.t2 import T2Database
from nexus.db.t3 import T3Database
from nexus.taxonomy import (
    get_topic_docs,
    get_topic_tree,
    get_topics,
)
from tests.conftest import make_vector_test_client


@pytest.fixture()
def chroma_client() -> chromadb.ClientAPI:
    """Ephemeral ChromaDB client for taxonomy centroid tests."""
    return make_vector_test_client()


# ── Substrate-neutral seed/read helpers (RDR-155 P4b P0a') ──────────────────
#
# Seeds route through the fidelity-import surface on the engine substrate
# (import_topic / import_assignment preserve the supplied values verbatim)
# and keep the historical raw INSERTs on the SQLite twin (which dies with
# the twin at the flip). Import src_ids start >= 1e9: import_topic preserves
# ids WITHOUT advancing the engine's topics sequence, so low ids collide
# order-dependently with later engine-side INSERTs (bisected finding, see
# tests/test_context.py). The base is module-distinct (1.1e9 here) because
# the topics PK is global across tenants — two modules restarting the same
# counter in one engine session 500 on /import/topic.

_seed_src_ids = itertools.count(1_100_000_000)


def _seed_topic(
    taxonomy: Any,
    label: str,
    *,
    collection: str,
    doc_count: int = 0,
    parent_id: int | None = None,
    review_status: str = "pending",
    terms: str | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> int:
    """Insert one topics row on either substrate; return its id."""
    if has_raw_access(taxonomy):
        cur = taxonomy.conn.execute(
            "INSERT INTO topics "
            "(label, parent_id, collection, doc_count, created_at, "
            "review_status, terms) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (label, parent_id, collection, doc_count, created_at,
             review_status, terms),
        )
        taxonomy.conn.commit()
        return cur.lastrowid
    return taxonomy.import_topic(
        src_id=next(_seed_src_ids),
        label=label,
        parent_id=parent_id,
        collection=collection,
        centroid_hash=None,
        doc_count=doc_count,
        created_at=created_at,
        review_status=review_status,
        terms=terms,
    )


def _seed_assignment(
    taxonomy: Any,
    doc_id: str,
    topic_id: int,
    *,
    assigned_by: str = "hdbscan",
    similarity: float | None = None,
    assigned_at: str | None = None,
    source_collection: str | None = None,
) -> None:
    """Insert one topic_assignments row on either substrate."""
    if has_raw_access(taxonomy):
        taxonomy.conn.execute(
            "INSERT OR REPLACE INTO topic_assignments "
            "(doc_id, topic_id, assigned_by, similarity, assigned_at, "
            "source_collection) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, topic_id, assigned_by, similarity, assigned_at,
             source_collection),
        )
        taxonomy.conn.commit()
        return
    taxonomy.import_assignment(
        doc_id=doc_id,
        topic_id=topic_id,
        assigned_by=assigned_by,
        similarity=similarity,
        assigned_at=assigned_at,
        source_collection=source_collection,
    )


def _link_pairs(taxonomy: Any, topic_ids: list[int]) -> dict[tuple[int, int], int]:
    """Normalized {(from_id, to_id): link_count} read.

    get_topic_link_pairs returns a dict on the SQLite twin and a list of
    (from, to, count) triples on the Http twin — normalize both shapes.
    """
    raw = taxonomy.get_topic_link_pairs(list(topic_ids))
    if isinstance(raw, dict):
        return dict(raw)
    return {(f, t): c for f, t, c in raw}


def _centroid_state(taxonomy: Any, collection: str, chroma_client: Any) -> dict[str, Any]:
    """Read a collection's centroid state through the public rebuild-state
    surface (substrate-neutral). The centroid_coll arg is required
    positionally by the SQLite oracle and ignored by the Http twin
    (centroids route through the engine's centroid port) — see
    tests/test_taxonomy_e2e.py for the pattern.
    """
    return taxonomy.read_rebuild_old_state(
        collection,
        chroma_client.get_or_create_collection("taxonomy__centroids"),
    )


def _collection_topic_ids(taxonomy: Any, collection: str) -> set[int]:
    return {t["id"] for t in taxonomy.get_topics_for_collection(collection)}


def _collection_assignment_count(taxonomy: Any, collection: str) -> int:
    return sum(
        len(taxonomy.get_all_topic_doc_ids(tid))
        for tid in _collection_topic_ids(taxonomy, collection)
    )


# ── schema ──────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw sqlite_master schema introspection dies at the RDR-155 P4b flip",
)
def test_topics_table_created(db: T2Database) -> None:
    """topics and topic_assignments tables exist after T2Database init."""
    tables = {
        r[0] for r in db.taxonomy.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "topics" in tables
    assert "topic_assignments" in tables


# ── topic CRUD ──────────────────────────────────────────────────────────────


def test_get_topics_empty(db: T2Database) -> None:
    """No topics initially."""
    assert get_topics(db) == []


def test_discover_topics_creates_topics_and_centroids(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """discover_topics persists topics to T2 and upserts centroids to ChromaDB."""
    rng = np.random.default_rng(42)
    # Two well-separated clusters in 384d
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0

    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network gradient {i}" for i in range(30)]
        + [f"database query indexing sql schema {i}" for i in range(30)]
    )

    count = db.taxonomy.discover_topics(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    assert count >= 2

    topics = get_topics(db)
    assert len(topics) >= 2
    # Each topic has a c-TF-IDF label (not empty)
    assert all(t["label"] for t in topics)
    # doc_count populated
    assert all(t["doc_count"] > 0 for t in topics)
    assert sum(t["doc_count"] for t in topics) <= 60

    # Centroids upserted (chroma coll on the SQLite twin, centroid port on
    # the engine) — read through the public rebuild-state surface.
    state = _centroid_state(db.taxonomy, "test__coll", chroma_client)
    assert len(state["old_centroid_topic_ids"]) >= 2
    # Centroid embeddings are 384d
    assert state["old_centroids"].shape[1] == 384
    # Every centroid maps back to a persisted topic id with its label.
    assert set(state["old_centroid_topic_ids"]) <= {t["id"] for t in topics}
    assert all(lbl for lbl in state["old_labels"])


# ── RDR-128 P1 (fkq5q): assign_batch compute/persist split ───────────────────


def _seed_centroids(db: T2Database, chroma_client) -> list[str]:
    """discover topics so taxonomy__centroids exists; return the doc_ids."""
    rng = np.random.default_rng(7)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"sd-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network gradient {i}" for i in range(30)]
        + [f"database query indexing sql schema {i}" for i in range(30)]
    )
    db.taxonomy.discover_topics("split__coll", doc_ids, embeddings, texts, chroma_client)
    return doc_ids


def test_compute_assignments_returns_json_serializable_dicts(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """The COMPUTE half returns plain dicts (no chroma objects) that survive
    JSON — i.e. they can cross the daemon RPC boundary, which is the whole
    point of the split."""
    import json

    _seed_centroids(db, chroma_client)
    new_embs = [
        (np.random.default_rng(1).standard_normal(384).astype(np.float32) * 0.1
         + np.array([3.0] + [0.0] * 383, dtype=np.float32)).tolist()
        for _ in range(3)
    ]
    # Substrate-neutral COMPUTE half: on the SQLite twin db.taxonomy IS
    # CatalogTaxonomy (identical staticmethod, centroids from the passed
    # chroma client); on the Http twin centroids come from the centroid port.
    out = db.taxonomy.compute_assignments(
        "split__coll", ["a", "b", "c"], new_embs, chroma_client,
        cross_collection=False,
    )
    assert out, "expected at least one assignment against seeded centroids"
    # Plain, JSON-round-trippable dicts with the persist contract's keys.
    json.dumps(out)  # must not raise
    for a in out:
        assert set(a) == {"doc_id", "topic_id", "assigned_by", "similarity", "source_collection"}
        assert isinstance(a["topic_id"], int)
        assert a["assigned_by"] == "centroid"


def test_persist_assignments_writes_rows(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """The PERSIST half writes the computed dicts to topic_assignments."""
    _seed_centroids(db, chroma_client)
    new_embs = [
        (np.random.default_rng(2).standard_normal(384).astype(np.float32) * 0.1
         + np.array([3.0] + [0.0] * 383, dtype=np.float32)).tolist()
    ]
    out = db.taxonomy.compute_assignments(
        "split__coll", ["persist-doc"], new_embs, chroma_client,
    )
    assert out, "expected an assignment against seeded centroids"
    n = db.taxonomy.persist_assignments(out)
    assert n == len(out)
    assert "persist-doc" in db.taxonomy.get_assignments_for_docs(["persist-doc"])


def test_assign_batch_still_composes_compute_and_persist(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """Back-compat: assign_batch == compute_assignments + persist_assignments,
    same return + same persisted rows (direct callers unchanged)."""
    _seed_centroids(db, chroma_client)
    new_embs = [
        (np.random.default_rng(3).standard_normal(384).astype(np.float32) * 0.1
         + np.array([3.0] + [0.0] * 383, dtype=np.float32)).tolist()
    ]
    expected = db.taxonomy.compute_assignments(
        "split__coll", ["batch-doc"], new_embs, chroma_client,
    )
    assert expected, "expected an assignment against seeded centroids"
    assigned = db.taxonomy.assign_batch(
        "split__coll", ["batch-doc"], new_embs, chroma_client,
    )
    assert assigned == len(expected)
    # Verify the persisted row matches what compute_assignments produced —
    # not just that "a row exists" (guards against a drift where assign_batch
    # silently computed something different).
    mapping = db.taxonomy.get_assignments_for_docs(["batch-doc"])
    assert mapping.get("batch-doc") == expected[0]["topic_id"]
    if has_raw_access(db.taxonomy):
        row = db.taxonomy.conn.execute(
            "SELECT assigned_by FROM topic_assignments WHERE doc_id='batch-doc'"
        ).fetchone()
        assert row[0] == expected[0]["assigned_by"]


def test_compute_assignments_empty_when_no_centroids(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """No centroids for the collection → empty (the old no-op-returns-0 case)."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    out = CatalogTaxonomy.compute_assignments(
        "never__discovered", ["x"], [[0.1] * 384], chroma_client,
    )
    assert out == []


# ── RDR-151 Phase 3 (uzay8): discover_topics compute/persist split ───────────
#
# Mirrors the RDR-128 P1 assign_batch split above. The discovery COMPUTE half
# (clustering + c-TF-IDF) is pure and chroma-free; the PERSIST half (INSERT
# topics + assignments, return generated topic_ids) is daemon-routable; the
# caller writes the chroma centroids locally from the returned ids. This is what
# lets ``nx index`` stop opening a direct T2 write connection (the live peg's
# external contender).


def _discovery_inputs(seed: int = 11) -> tuple[list[str], np.ndarray, list[str]]:
    """Two well-separated 384d clusters — same shape the discover test uses."""
    rng = np.random.default_rng(seed)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"dd-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network gradient {i}" for i in range(30)]
        + [f"database query indexing sql schema {i}" for i in range(30)]
    )
    return doc_ids, embeddings, texts


def test_compute_discovered_topics_returns_serializable_specs() -> None:
    """The COMPUTE half returns plain JSON-round-trippable spec dicts and
    touches neither T2 nor chroma (so it can run client-side and the result
    can cross the daemon RPC)."""
    import json

    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    doc_ids, embeddings, texts = _discovery_inputs()
    specs = CatalogTaxonomy.compute_discovered_topics(
        "split__disc", doc_ids, embeddings, texts,
    )
    assert len(specs) >= 2, "expected >=2 clusters from two separated blobs"
    json.dumps(specs)  # must not raise — serializable across the RPC
    for s in specs:
        assert set(s) == {
            "label", "terms", "doc_count", "doc_ids", "centroid", "assigned_by",
        }
        assert isinstance(s["label"], str) and s["label"]
        assert isinstance(s["doc_count"], int) and s["doc_count"] > 0
        assert isinstance(s["doc_ids"], list) and s["doc_ids"]
        assert len(s["doc_ids"]) == s["doc_count"]
        assert isinstance(s["centroid"], list) and len(s["centroid"]) == 384
        assert all(isinstance(x, float) for x in s["centroid"])
        assert s["assigned_by"] == "hdbscan"


def test_compute_discovered_topics_empty_short_circuits() -> None:
    """<5 docs returns [] (the old discover_topics no-op-returns-0 case)."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    out = CatalogTaxonomy.compute_discovered_topics(
        "tiny__disc", ["a", "b"], np.zeros((2, 384), dtype=np.float32), ["x", "y"],
    )
    assert out == []


def test_persist_discovered_topics_writes_and_returns_ids(db: T2Database) -> None:
    """The PERSIST half writes topic rows + assignments and returns the
    generated topic_ids aligned to the input spec order — no chroma needed."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    doc_ids, embeddings, texts = _discovery_inputs()
    specs = CatalogTaxonomy.compute_discovered_topics(
        "persist__disc", doc_ids, embeddings, texts,
    )
    topic_ids = db.taxonomy.persist_discovered_topics("persist__disc", specs)
    assert isinstance(topic_ids, list)
    assert len(topic_ids) == len(specs)
    assert all(isinstance(t, int) for t in topic_ids)

    # Topic rows exist, one per spec, with the spec's label + doc_count.
    rows = db.taxonomy.get_topics_for_collection("persist__disc")
    assert len(rows) == len(specs)
    assert {r["id"] for r in rows} == set(topic_ids)
    # Assignments written for every doc in every spec.
    total_assigned = _collection_assignment_count(db.taxonomy, "persist__disc")
    assert total_assigned == sum(s["doc_count"] for s in specs)


def test_persist_discovered_topics_skips_existing(db: T2Database) -> None:
    """Existing-topics guard preserved: a second persist for the same
    collection is a no-op returning [] (matches discover_topics' guard)."""
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    doc_ids, embeddings, texts = _discovery_inputs()
    specs = CatalogTaxonomy.compute_discovered_topics(
        "guard__disc", doc_ids, embeddings, texts,
    )
    first = db.taxonomy.persist_discovered_topics("guard__disc", specs)
    assert len(first) == len(specs)
    second = db.taxonomy.persist_discovered_topics("guard__disc", specs)
    assert second == []


def test_discover_topics_still_composes(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """Back-compat: discover_topics == compute + persist + centroid upsert.
    Same end state (topic rows in T2 AND centroids in chroma) so direct
    callers and the CLI path are unchanged by the split."""
    doc_ids, embeddings, texts = _discovery_inputs(seed=13)
    count = db.taxonomy.discover_topics(
        "compose__disc", doc_ids, embeddings, texts, chroma_client,
    )
    assert count >= 2

    t2_ids = _collection_topic_ids(db.taxonomy, "compose__disc")
    assert len(t2_ids) == count

    state = _centroid_state(db.taxonomy, "compose__disc", chroma_client)
    assert len(state["old_centroid_topic_ids"]) == count
    # Every persisted centroid maps to a real T2 topic id.
    assert {int(t) for t in state["old_centroid_topic_ids"]} == t2_ids


def test_taxonomy_hook_routes_persist_through_t2_index_write(monkeypatch) -> None:
    """The rerouted taxonomy_assign_batch_hook must compute client-side and
    persist via t2_index_write (the daemon path), NOT a direct t2_ctx open.

    Without this the hook's new routing is untested — the pre-existing
    t2_ctx-patching hook tests pass vacuously after the reroute (they don't
    invoke this hook), so a lambda-scoping / wiring regression would slip
    through (test-validator finding, fkq5q gate).
    """
    import nexus.mcp_infra as mi
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    computed = [{
        "doc_id": "d1", "topic_id": 7, "assigned_by": "centroid",
        "similarity": None, "source_collection": None,
    }]

    # Bypass chroma: same-collection returns the known list, cross returns [].
    monkeypatch.setattr(
        CatalogTaxonomy, "compute_assignments",
        staticmethod(lambda *a, **k: [] if k.get("cross_collection") else computed),
    )
    # The hook does `from nexus.config import is_local_mode` at call time,
    # so patch the source module (not mcp_infra).
    import nexus.config as _cfg
    monkeypatch.setattr(_cfg, "is_local_mode", lambda: False)  # skip exclude gate
    monkeypatch.setattr(
        mi, "get_t3", lambda: type("T", (), {"_client": object()})(),
    )

    captured: dict = {}

    class _FakeTaxonomy:
        def persist_assignments(self, assignments):  # noqa: ANN001
            captured["assignments"] = assignments
            return len(assignments)

    class _FakeDb:
        taxonomy = _FakeTaxonomy()

    def _spy_index_write(write_fn):  # noqa: ANN001
        captured["routed"] = True
        write_fn(_FakeDb())

    monkeypatch.setattr(mi, "t2_index_write", _spy_index_write)
    monkeypatch.setattr(
        mi, "t2_ctx",
        lambda: pytest.fail("hook must route via t2_index_write, not t2_ctx"),
    )

    mi.taxonomy_assign_batch_hook(
        doc_ids=["d1"], collection="code__c", contents=["x"],
        embeddings=[[0.1] * 384], metadatas=None,
    )

    assert captured.get("routed") is True, "hook must call t2_index_write"
    assert captured.get("assignments") == computed, "must persist the computed assignments"


def test_assign_topic(db: T2Database) -> None:
    """Assign a doc_id to a topic."""
    # Create a topic first. The Http twin's assign_topic requires an
    # explicit assigned_by, so call the store directly (the deprecated
    # nexus.taxonomy.assign_topic facade omits it).
    topic_id = _seed_topic(db.taxonomy, "test-topic", collection="proj")

    db.taxonomy.assign_topic("doc-123", topic_id, assigned_by="hdbscan")

    assert db.taxonomy.get_assignments_for_docs(["doc-123"]) == {"doc-123": topic_id}


def test_assign_topic_idempotent(db: T2Database) -> None:
    """Assigning same doc to same topic twice doesn't error."""
    topic_id = _seed_topic(db.taxonomy, "test-topic", collection="proj")

    db.taxonomy.assign_topic("doc-123", topic_id, assigned_by="hdbscan")
    db.taxonomy.assign_topic("doc-123", topic_id, assigned_by="hdbscan")  # no error

    assert db.taxonomy.get_all_topic_doc_ids(topic_id) == ["doc-123"]


def test_assign_topic_updates_doc_count_cache(db: T2Database) -> None:
    """nexus-n41p: topics.doc_count must track COUNT(*) topic_assignments.

    Cache-invalidation regression test. Pre-fix, doc_count stayed at its
    register-time default (0) after assign_topic() inserted into
    topic_assignments. Stats consumers (`nx taxonomy stats`, ORDER BY
    doc_count) silently under-reported.
    """
    topic_id = _seed_topic(db.taxonomy, "test-topic", collection="proj")

    def _cached_and_derived() -> tuple[int, int]:
        return (
            db.taxonomy.get_topic_by_id(topic_id)["doc_count"],
            len(db.taxonomy.get_all_topic_doc_ids(topic_id)),
        )

    # HDBSCAN path (default assigned_by)
    db.taxonomy.assign_topic("doc-a", topic_id, assigned_by="hdbscan")
    db.taxonomy.assign_topic("doc-b", topic_id, assigned_by="hdbscan")
    cached, derived = _cached_and_derived()
    assert cached == derived == 2, f"cached={cached} derived={derived}"

    # Projection (UPSERT) path
    db.taxonomy.assign_topic(
        "doc-c",
        topic_id,
        assigned_by="projection",
        similarity=0.9,
        source_collection="proj",
    )
    cached, derived = _cached_and_derived()
    assert cached == derived == 3, f"cached={cached} derived={derived}"

    # Re-assigning same doc via projection UPSERT must not double-count.
    db.taxonomy.assign_topic(
        "doc-c",
        topic_id,
        assigned_by="projection",
        similarity=0.95,
        source_collection="proj",
    )
    cached, derived = _cached_and_derived()
    assert cached == derived == 3, f"cached={cached} derived={derived}"


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: CatalogTaxonomy's assign-time topic_links resync (nexus-zq79 F5; the engine maintains links via refresh/cooccurrence passes, not per-assign) dies at the RDR-155 P4b flip",
)
def test_assign_topic_resyncs_topic_links_link_count(db: T2Database) -> None:
    """nexus-zq79 F5: topic_links.link_count must track co-occurrence
    count after every assign_topic, not stay stale until the next full
    refresh_projection_links / generate_cooccurrence_links rebuild.
    """
    # Two topics in different collections so co-occurrence is meaningful.
    a_id = _seed_topic(db.taxonomy, "topic-a", collection="proj-a")
    b_id = _seed_topic(db.taxonomy, "topic-b", collection="proj-b")
    pair = (min(a_id, b_id), max(a_id, b_id))

    # Doc X in topic A, then X in topic B — co-occurrence 1.
    db.taxonomy.assign_topic("doc-x", a_id, assigned_by="hdbscan")
    db.taxonomy.assign_topic("doc-x", b_id, assigned_by="hdbscan")
    link = _link_pairs(db.taxonomy, [a_id, b_id]).get(pair)
    assert link == 1, (
        f"expected link_count=1 after first co-occurrence, got {link}"
    )

    # Doc Y also in both topics — co-occurrence 2.
    db.taxonomy.assign_topic("doc-y", a_id, assigned_by="hdbscan")
    db.taxonomy.assign_topic("doc-y", b_id, assigned_by="hdbscan")
    link = _link_pairs(db.taxonomy, [a_id, b_id]).get(pair)
    assert link == 2, (
        f"expected link_count=2 after second co-occurrence, got {link}"
    )

    # Re-assigning the same (doc, topic) must not inflate.
    db.taxonomy.assign_topic("doc-y", a_id, assigned_by="hdbscan")
    link = _link_pairs(db.taxonomy, [a_id, b_id]).get(pair)
    assert link == 2, (
        f"INSERT OR IGNORE on duplicate must not increment link_count; "
        f"got {link}"
    )


def test_rebuild_taxonomy_clears_and_rediscovers(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """rebuild_taxonomy deletes old topics, then re-discovers fresh ones."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural network {i}" for i in range(30)]
        + [f"database query sql schema {i}" for i in range(30)]
    )

    count1 = db.taxonomy.rebuild_taxonomy(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    ids_after_first = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__coll"
    }

    count2 = db.taxonomy.rebuild_taxonomy(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    ids_after_second = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__coll"
    }

    assert count1 >= 2
    assert count2 >= 2
    # Topic count should match the second run, not be doubled (no accumulation)
    assert len(ids_after_second) == count2
    # Total assignments should be consistent — not doubled
    total_assignments = _collection_assignment_count(db.taxonomy, "test__coll")
    assert total_assignments <= 60, "rebuild should replace, not accumulate"


def test_rebuild_taxonomy_preserves_manual_assignment(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """RDR-151 Phase 3 regression: rebuild must carry a manually-assigned doc
    onto the rebuilt topic (Route 1 — old topic matched to new via _merge_labels).
    This is the transfer logic the compute/persist split must preserve exactly."""
    doc_ids, embeddings, texts = _discovery_inputs(seed=21)
    db.taxonomy.discover_topics(
        "manual__coll", doc_ids, embeddings, texts, chroma_client,
    )
    # Operator manually assigns a doc to an existing topic.
    first_topic = min(_collection_topic_ids(db.taxonomy, "manual__coll"))
    db.taxonomy.assign_topic("manual-doc", first_topic, assigned_by="manual")

    db.taxonomy.rebuild_taxonomy(
        "manual__coll", doc_ids, embeddings, texts, chroma_client,
    )

    # The manual doc still carries a 'manual' assignment to a live topic —
    # read through the public rebuild-state surface (manual_assignments is
    # exactly the assigned_by='manual' row set joined to live topics).
    manual = _centroid_state(db.taxonomy, "manual__coll", chroma_client)[
        "manual_assignments"
    ]
    assert "manual-doc" in manual, "manual assignment must survive rebuild (Route 1/2)"
    assert manual["manual-doc"] in _collection_topic_ids(db.taxonomy, "manual__coll")


def test_compute_rebuild_plan_is_pure_and_serializable(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """The rebuild COMPUTE half returns a JSON-serializable plan (specs +
    manual-transfer decisions keyed to spec index) and touches no T2 write."""
    import json

    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    doc_ids, embeddings, texts = _discovery_inputs(seed=23)
    plan = CatalogTaxonomy.compute_rebuild_plan(
        "rb__coll", doc_ids, embeddings, texts,
        old_centroids=np.empty((0, 0), dtype=np.float32),
        old_labels=[], old_review_statuses=[], old_centroid_topic_ids=[],
        manual_assignments={},
    )
    json.dumps(plan)
    assert "specs" in plan and "manual_transfers" in plan
    assert len(plan["specs"]) >= 2
    for s in plan["specs"]:
        assert set(s) >= {
            "label", "terms", "doc_count", "doc_ids", "centroid",
            "assigned_by", "review_status",
        }
    # No manual assignments in -> none out.
    assert plan["manual_transfers"] == {}


def test_get_topics_filtered_by_parent(db: T2Database) -> None:
    """get_topics(parent_id=None) returns only root topics."""
    root_id = _seed_topic(db.taxonomy, "root-topic", collection="proj", doc_count=5)
    _seed_topic(
        db.taxonomy, "child-topic", collection="proj", doc_count=2,
        parent_id=root_id,
    )

    roots = get_topics(db, parent_id=None)
    assert len(roots) == 1
    assert roots[0]["label"] == "root-topic"

    children = get_topics(db, parent_id=root_id)
    assert len(children) == 1
    assert children[0]["label"] == "child-topic"


# ── tree + docs ─────────────────────────────────────────────────────────────


def test_get_topic_tree_structure(db: T2Database) -> None:
    """get_topic_tree returns nested dicts with children."""
    root_id = _seed_topic(db.taxonomy, "root", collection="proj", doc_count=10)
    _seed_topic(
        db.taxonomy, "child", collection="proj", doc_count=3, parent_id=root_id,
    )

    tree = get_topic_tree(db, "proj")
    assert len(tree) == 1
    assert tree[0]["label"] == "root"
    assert len(tree[0]["children"]) == 1
    assert tree[0]["children"][0]["label"] == "child"


def test_get_topic_docs_returns_assigned(db: T2Database) -> None:
    """get_topic_docs returns doc_ids assigned to the topic."""
    topic_id = _seed_topic(db.taxonomy, "test", collection="proj", doc_count=2)
    _seed_assignment(db.taxonomy, "doc-a", topic_id)
    _seed_assignment(db.taxonomy, "doc-b", topic_id)

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 2
    assert {d["doc_id"] for d in docs} == {"doc-a", "doc-b"}


def test_discover_topics_all_noise_returns_zero(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """When HDBSCAN assigns all docs to noise (-1), return 0 and skip centroids."""
    rng = np.random.default_rng(42)
    # Too few scattered points — HDBSCAN cannot find clusters
    embeddings = rng.standard_normal((8, 384)).astype(np.float32) * 100
    doc_ids = [f"noise-{i}" for i in range(8)]
    texts = [f"completely unrelated text {i}" for i in range(8)]

    count = db.taxonomy.discover_topics(
        "test__coll", doc_ids, embeddings, texts, chroma_client,
    )
    assert count == 0
    assert db.taxonomy.get_topics() == []


def test_assign_single_returns_nearest_topic(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns the nearest topic_id via centroid ANN lookup."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    db.taxonomy.discover_topics("test__coll", doc_ids, embeddings, texts, chroma_client)

    # New embedding near cluster A (dimension 0 shifted)
    new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
    new_emb[0] += 3.0

    result = db.taxonomy.assign_single("test__coll", new_emb, chroma_client)
    assert result is not None
    # RDR-077: AssignResult(topic_id, similarity)
    assert isinstance(result.topic_id, int)
    assert 0.0 <= result.similarity <= 1.0

    # Verify it's assigned to a real topic in T2
    topics = db.taxonomy.get_topics()
    topic_ids = {t["id"] for t in topics}
    assert result.topic_id in topic_ids


def test_assign_single_no_centroids_returns_none(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns None when no centroids exist for the collection."""
    emb = np.random.default_rng(42).standard_normal(384).astype(np.float32)
    # Use a collection name with no centroids — EphemeralClient shares
    # in-process state, so centroids from other tests may exist.
    result = db.taxonomy.assign_single("nonexistent__coll", emb, chroma_client)
    assert result is None


def test_assign_single_cross_collection_isolation(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns None for collection B when centroids only exist for A."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    # Discover for collection A — creates centroids
    db.taxonomy.discover_topics("coll_A", doc_ids, embeddings, texts, chroma_client)

    # Query for collection B — should return None, not a topic from A
    new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
    new_emb[0] += 3.0  # similar to cluster A's centroid
    result = db.taxonomy.assign_single("coll_B", new_emb, chroma_client)
    assert result is None, "assign_single must not cross collection boundaries"


def test_assign_single_cross_collection_finds_foreign_topic(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single with cross_collection=True returns topics from other collections."""
    rng = np.random.default_rng(42)
    # Create topics in collection A
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = [f"text {i}" for i in range(60)]
    db.taxonomy.discover_topics("coll_A_xc", doc_ids, embeddings, texts, chroma_client)

    # Query from collection B with cross_collection=True — should find A's topics
    new_emb = rng.standard_normal(384).astype(np.float32) * 0.1
    new_emb[0] += 3.0
    result = db.taxonomy.assign_single(
        "coll_B_xc", new_emb, chroma_client, cross_collection=True,
    )
    assert result is not None, "cross_collection=True should find topics from other collections"

    # Confirm default (False) still isolates
    result_isolated = db.taxonomy.assign_single(
        "coll_B_xc", new_emb, chroma_client, cross_collection=False,
    )
    assert result_isolated is None, "cross_collection=False must not cross boundaries"


def test_assign_batch_cross_collection(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_batch with cross_collection=True assigns from foreign centroids."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    db.taxonomy.discover_topics(
        "batch_A_xc",
        [f"doc-{i}" for i in range(60)],
        embeddings,
        [f"text {i}" for i in range(60)],
        chroma_client,
    )

    # New batch from collection B
    new_embs = rng.standard_normal((3, 384)).astype(np.float32) * 0.1
    new_embs[:, 0] += 3.0
    new_ids = ["xc-0", "xc-1", "xc-2"]

    assigned = db.taxonomy.assign_batch(
        "batch_B_xc", new_ids, new_embs.tolist(), chroma_client,
        cross_collection=True,
    )
    assert assigned == 3

    # Default should assign 0 (no centroids for batch_B_xc)
    assigned_isolated = db.taxonomy.assign_batch(
        "batch_B_xc", ["iso-0"], new_embs[:1].tolist(), chroma_client,
        cross_collection=False,
    )
    assert assigned_isolated == 0


def test_assign_batch_assigns_multiple_docs(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_batch assigns multiple new docs to nearest topics."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    db.taxonomy.discover_topics("test__coll", doc_ids, embeddings, texts, chroma_client)

    # New batch: 3 docs near cluster A, 2 near cluster B
    new_embs = rng.standard_normal((5, 384)).astype(np.float32) * 0.1
    new_embs[:3, 0] += 3.0  # near cluster A
    new_embs[3:, 1] += 3.0  # near cluster B
    new_ids = [f"new-doc-{i}" for i in range(5)]

    assigned = db.taxonomy.assign_batch(
        "test__coll", new_ids, new_embs.tolist(), chroma_client,
    )
    assert assigned == 5

    # Verify assignments exist in T2
    mapping = db.taxonomy.get_assignments_for_docs(new_ids)
    assert set(mapping) == set(new_ids)
    if has_raw_access(db.taxonomy):
        rows = db.taxonomy.conn.execute(
            "SELECT assigned_by FROM topic_assignments WHERE doc_id LIKE 'new-doc-%'"
        ).fetchall()
        assert all(r[0] == "centroid" for r in rows)


def test_assign_batch_no_centroids_returns_zero(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_batch returns 0 when no centroids exist."""
    embs = np.random.default_rng(42).standard_normal((3, 384)).astype(np.float32)
    result = db.taxonomy.assign_batch(
        "nonexistent__coll", ["a", "b", "c"], embs.tolist(), chroma_client,
    )
    assert result == 0


def test_assign_single_dimension_mismatch(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_single returns None with warning on embedding dimension mismatch."""
    rng = np.random.default_rng(42)
    # Create centroids with 384d embeddings
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = [f"text {i}" for i in range(60)]
    db.taxonomy.discover_topics("dim__coll", doc_ids, embeddings, texts, chroma_client)

    # Query with 1024d embedding — dimension mismatch
    wrong_dim_emb = rng.standard_normal(1024).astype(np.float32)
    result = db.taxonomy.assign_single("dim__coll", wrong_dim_emb, chroma_client)
    assert result is None


def test_assign_batch_dimension_mismatch(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """assign_batch returns 0 on embedding dimension mismatch."""
    rng = np.random.default_rng(42)
    # Create centroids with 384d
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = [f"text {i}" for i in range(60)]
    db.taxonomy.discover_topics("dimbatch__coll", doc_ids, embeddings, texts, chroma_client)

    # Query with 1024d embeddings — dimension mismatch
    wrong_embs = rng.standard_normal((3, 1024)).astype(np.float32)
    result = db.taxonomy.assign_batch(
        "dimbatch__coll", ["a", "b", "c"], wrong_embs.tolist(), chroma_client,
    )
    assert result == 0


def test_project_against_basic(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """project_against returns matched topics and novel chunks."""
    rng = np.random.default_rng(42)
    # Create source collection with 20 chunks in two clusters
    src_embs = rng.standard_normal((20, 384)).astype(np.float32) * 0.1
    src_embs[:10, 0] += 3.0  # cluster A
    src_embs[10:, 1] += 3.0  # cluster B
    src_ids = [f"src-{i}" for i in range(20)]

    # Create target collection and discover topics (creates centroids)
    tgt_embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    tgt_embs[:30, 0] += 3.0  # similar to source cluster A
    tgt_embs[30:, 1] += 3.0  # similar to source cluster B
    tgt_ids = [f"tgt-{i}" for i in range(60)]
    tgt_texts = [f"text {i}" for i in range(60)]
    db.taxonomy.discover_topics("target__coll", tgt_ids, tgt_embs, tgt_texts, chroma_client)

    # Store source embeddings in a ChromaDB collection
    src_coll = chroma_client.get_or_create_collection(
        "source__coll", embedding_function=None, metadata={"hnsw:space": "cosine"},
    )
    src_coll.upsert(ids=src_ids, embeddings=src_embs.tolist())

    result = db.taxonomy.project_against(
        "source__coll", ["target__coll"], chroma_client, threshold=0.5,
    )

    assert "matched_topics" in result
    assert "novel_chunks" in result
    assert "chunk_assignments" in result
    assert "total_chunks" in result
    assert result["total_chunks"] == 20
    # With threshold=0.5 and clearly separated clusters, most chunks should match
    assert len(result["matched_topics"]) > 0
    # Invariant: novel + covered == total
    covered = result["total_chunks"] - len(result["novel_chunks"])
    assert covered + len(result["novel_chunks"]) == result["total_chunks"]
    # chunk_assignments has entries for covered chunks
    assert len(result["chunk_assignments"]) > 0


def test_project_against_empty_target(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """project_against with no target centroids returns all chunks as novel."""
    rng = np.random.default_rng(42)
    src_embs = rng.standard_normal((5, 384)).astype(np.float32)
    src_ids = [f"src-{i}" for i in range(5)]

    src_coll = chroma_client.get_or_create_collection(
        "empty_src__coll", embedding_function=None, metadata={"hnsw:space": "cosine"},
    )
    src_coll.upsert(ids=src_ids, embeddings=src_embs.tolist())

    result = db.taxonomy.project_against(
        "empty_src__coll", ["nonexistent__coll"], chroma_client,
    )

    assert result["total_chunks"] == 5
    assert len(result["novel_chunks"]) == 5
    assert len(result["matched_topics"]) == 0


def test_project_against_dimension_mismatch(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """project_against raises ValueError on dimension mismatch."""
    rng = np.random.default_rng(42)
    # Create target centroids with 384d
    tgt_embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    tgt_embs[:30, 0] += 3.0
    tgt_embs[30:, 1] += 3.0
    tgt_ids = [f"tgt-{i}" for i in range(60)]
    tgt_texts = [f"text {i}" for i in range(60)]
    db.taxonomy.discover_topics("dimtgt__coll", tgt_ids, tgt_embs, tgt_texts, chroma_client)

    # Source collection with 1024d — dimension mismatch
    src_embs_1024 = rng.standard_normal((5, 1024)).astype(np.float32)
    src_ids = [f"src-{i}" for i in range(5)]
    src_coll = chroma_client.get_or_create_collection(
        "dimsrc__coll", embedding_function=None, metadata={"hnsw:space": "cosine"},
    )
    src_coll.upsert(ids=src_ids, embeddings=src_embs_1024.tolist())

    with pytest.raises(ValueError, match="Dimension mismatch"):
        db.taxonomy.project_against(
            "dimsrc__coll", ["dimtgt__coll"], chroma_client,
        )


def test_assigned_by_column_populated(
    db: T2Database, chroma_client: chromadb.ClientAPI,
) -> None:
    """discover_topics sets assigned_by='hdbscan' on topic_assignment rows."""
    rng = np.random.default_rng(42)
    embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
    embeddings[:30, 0] += 3.0
    embeddings[30:, 1] += 3.0
    doc_ids = [f"doc-{i}" for i in range(60)]
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )

    db.taxonomy.discover_topics("test__coll", doc_ids, embeddings, texts, chroma_client)

    # Assignments landed for the discovered docs on both substrates.
    assert db.taxonomy.get_assignments_for_docs(doc_ids)
    if has_raw_access(db.taxonomy):
        rows = db.taxonomy.conn.execute(
            "SELECT DISTINCT assigned_by FROM topic_assignments"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "hdbscan"
    else:
        # No public assigned_by read on the Http twin; manual_assignments
        # (the assigned_by='manual' slice) being empty pins the provenance
        # is not 'manual'.
        state = _centroid_state(db.taxonomy, "test__coll", chroma_client)
        assert state["manual_assignments"] == {}


def test_get_topic_docs_resolves_title_via_join(db: T2Database) -> None:
    """get_topic_docs JOINs on memory.title to resolve human-readable titles."""
    # Insert a memory entry — title must match doc_id AND project must match collection
    db.put(project="test", title="my-research-note", content="some content")

    topic_id = _seed_topic(db.taxonomy, "topic", collection="test", doc_count=1)
    _seed_assignment(db.taxonomy, "my-research-note", topic_id)

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "my-research-note"
    assert docs[0]["title"] == "my-research-note"


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: SQLite memory-title JOIN known-defect documentation (RDR-063) dies at the RDR-155 P4b flip",
)
def test_get_topic_docs_known_defect_project_collection_mismatch(db: T2Database) -> None:
    """RDR-063 Known Defect: get_topic_docs() JOIN conflates T3 collection with T2 project.

    This test DOCUMENTS the defect: when a topic's ``collection`` is a T3
    collection name (e.g. ``code__myrepo``) and the memory entry's ``project``
    is the T2 project name (e.g. ``myrepo``), the JOIN fails because the
    project ≠ collection comparison is false.

    The assertion uses a doc_id that does NOT match any memory.title, so
    the JOIN can't short-circuit. When the defect is fixed (JOIN semantics
    changed), this test will fail and must be rewritten.
    """
    # Memory entry with a DIFFERENT title from the topic's doc_id — so even
    # if the project/collection match, the title JOIN cannot find a hit.
    # This ensures we're testing the project-vs-collection mismatch, not
    # accidentally matching via title == doc_id.
    db.put(project="myrepo", title="unrelated-memory-title", content="some notes")

    # Topic associated with a T3 collection name (NOT the T2 project name)
    db.taxonomy.conn.execute(
        "INSERT INTO topics (label, collection, doc_count, created_at) VALUES (?, ?, ?, ?)",
        ("code topic", "code__myrepo", 1, "2026-01-01T00:00:00Z"),
    )
    topic_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # doc_id = "t3-chunk-id" — does NOT match any memory.title in the DB
    db.taxonomy.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
        ("t3-chunk-id", topic_id),
    )
    db.taxonomy.conn.commit()

    docs = get_topic_docs(db, topic_id)
    assert len(docs) == 1
    assert docs[0]["doc_id"] == "t3-chunk-id"
    # Because the JOIN fails (project != collection AND title != doc_id),
    # title falls back to doc_id and project falls back to empty string.
    # When the defect is fixed, both fields should carry resolved values
    # that differ from these fallbacks — the test will fail and must be
    # rewritten alongside removing the Known Defect section from RDR-063.
    assert docs[0]["title"] == "t3-chunk-id", (
        "Known defect regression: get_topic_docs() now resolves T3-origin "
        "titles. Update this test to assert the new resolved title value "
        "and remove the Known Defect section from RDR-063."
    )
    assert docs[0]["project"] == "", (
        "Known defect regression: get_topic_docs() now resolves T3-origin "
        "project. Update this test alongside the title assertion."
    )


# ── Cascade on memory delete (v3.8.1) ─────────────────────────────────────


def test_memory_delete_cascades_topic_assignments(db: T2Database) -> None:
    """Deleting a memory entry removes its topic_assignments (v3.8.1 fix).

    Regression pin: pre-v3.8.1, ``nx memory delete`` left dangling
    ``topic_assignments`` rows whose ``doc_id`` referenced the deleted
    entry. Those orphans surfaced in ``nx taxonomy list`` / ``nx
    taxonomy show`` as ghost entries. The fix adds a cascade in the
    facade ``T2Database.delete()`` that calls
    ``CatalogTaxonomy.purge_assignments_for_doc()``.
    """
    # Seed memory entries for a project
    db.put(project="proj", title="doc-a", content="alpha content here")
    db.put(project="proj", title="doc-b", content="beta content here")

    # Seed a topic with both entries assigned
    topic_id = _seed_topic(db.taxonomy, "test-topic", collection="proj", doc_count=2)
    _seed_assignment(db.taxonomy, "doc-a", topic_id)
    _seed_assignment(db.taxonomy, "doc-b", topic_id)

    # Sanity: both assignments present
    assert len(db.taxonomy.get_all_topic_doc_ids(topic_id)) == 2

    # Delete doc-a via the facade — should cascade-purge its assignment
    assert db.delete(project="proj", title="doc-a") is True

    post = sorted(db.taxonomy.get_all_topic_doc_ids(topic_id))
    assert post == ["doc-b"], (
        "cascade should have removed doc-a's assignment but kept doc-b's"
    )

    # Topic still exists because doc-b still references it
    assert len(_collection_topic_ids(db.taxonomy, "proj")) == 1


def test_memory_delete_drops_empty_topics(db: T2Database) -> None:
    """Deleting the last memory entry in a topic also drops the topic."""
    db.put(project="proj", title="solo-doc", content="lonely content")
    topic_id = _seed_topic(db.taxonomy, "solo-topic", collection="proj", doc_count=1)
    _seed_assignment(db.taxonomy, "solo-doc", topic_id)

    assert db.delete(project="proj", title="solo-doc") is True

    # Assignment gone
    assert db.taxonomy.get_all_topic_doc_ids(topic_id) == []

    # Topic also gone (empty after the cascade)
    assert db.taxonomy.get_topic_by_id(topic_id) is None


def test_memory_delete_cascade_scoped_to_project(db: T2Database) -> None:
    """Cascade only touches topics in the deleted entry's project.

    If two projects happen to have a memory entry with the same title,
    deleting one must not cascade-remove the other's topic assignment.
    """
    # Same title under two projects
    db.put(project="proj-a", title="shared-title", content="content under proj-a")
    db.put(project="proj-b", title="shared-title", content="content under proj-b")

    # Two topics, one per project, both assigning the shared title
    topic_a_id = _seed_topic(db.taxonomy, "topic-a", collection="proj-a", doc_count=1)
    topic_b_id = _seed_topic(db.taxonomy, "topic-b", collection="proj-b", doc_count=1)
    _seed_assignment(db.taxonomy, "shared-title", topic_a_id)
    _seed_assignment(db.taxonomy, "shared-title", topic_b_id)

    # Delete only the proj-a entry
    assert db.delete(project="proj-a", title="shared-title") is True

    # topic-a's assignment removed, topic-b's assignment untouched
    assert db.taxonomy.get_all_topic_doc_ids(topic_a_id) == []
    assert db.taxonomy.get_all_topic_doc_ids(topic_b_id) == ["shared-title"]


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: T2Database.delete(id=...) resolves the row under the raw memory._lock (SQLite-only facade branch) and dies at the RDR-155 P4b flip",
)
def test_memory_delete_by_id_cascades(db: T2Database) -> None:
    """Facade resolves project/title from --id before cascading."""
    row_id = db.put(project="proj", title="by-id", content="delete via numeric id")
    topic_id = _seed_topic(db.taxonomy, "id-topic", collection="proj", doc_count=1)
    _seed_assignment(db.taxonomy, "by-id", topic_id)

    assert db.delete(id=row_id) is True

    assert db.taxonomy.get_all_topic_doc_ids(topic_id) == []


def test_cli_taxonomy_list(tmp_path: Path) -> None:
    """CLI taxonomy list outputs topic labels and doc counts."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        _seed_topic(db.taxonomy, "Search Methods", collection="proj", doc_count=5)
        _seed_topic(db.taxonomy, "Database Queries", collection="proj", doc_count=3)

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["list"])

    assert result.exit_code == 0, result.output
    assert "Search Methods" in result.output
    assert "Database Queries" in result.output
    assert "5 docs" in result.output
    assert "3 docs" in result.output


def test_cli_taxonomy_status_warns_on_missing_projection(tmp_path: Path) -> None:
    """status flags collections that have topics but zero projection data.

    GitHub #239 + bead nexus-gwhy: a collection fresh from ``discover``
    has own-collection topics but no cross-collection projection; the
    status default output previously showed it as healthy.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        _seed_topic(db.taxonomy, "t1", collection="docs__alpha", doc_count=10)
        # No topic_assignments with assigned_by='projection' exist.

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "[no projection]" in result.output
    assert "Action:" in result.output
    assert "nx taxonomy project" in result.output


def test_cli_taxonomy_status_missing_projection_count_not_truncated_by_limit(
    tmp_path: Path,
) -> None:
    """The missing-projection Action count reflects the full universe,
    not the ``--limit`` page (code-review finding C-2).

    Previously the count could under-report when the user scoped the
    display with ``-n N`` and the zero-projection collections were
    ranked below the top-N by doc_count.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        # Three collections, descending doc_count. The top-1 has a
        # projection; the other two (below the -n 1 cut-off) do not.
        big_id = _seed_topic(db.taxonomy, "big", collection="docs__big", doc_count=100)
        _seed_topic(db.taxonomy, "medium", collection="docs__medium", doc_count=50)
        _seed_topic(db.taxonomy, "small", collection="docs__small", doc_count=10)
        db.taxonomy.assign_topic(
            "doc-1", big_id, assigned_by="projection",
            similarity=0.9, source_collection="docs__big",
        )

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status", "-n", "1"])

    assert result.exit_code == 0, result.output
    # Two collections (medium + small) are zero-projection; the Action
    # line must name both even though only the top-1 was displayed.
    assert "2 collection(s) have no cross-collection projection" in result.output, (
        result.output
    )


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw-SQLite hook_failures table + migrations (status skips the read in service mode) dies at the RDR-155 P4b flip",
)
def test_cli_taxonomy_status_surfaces_recent_hook_failures(tmp_path: Path) -> None:
    """GH #251: status emits an Action line when hook_failures has recent rows.

    The persist path is dormant until 4.9.10, but the read path is live
    today — the table may exist in DBs upgraded by the migration test
    harness, or once the next release ships.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    import sqlite3

    from nexus.commands.taxonomy_cmd import taxonomy
    from nexus.db.migrations import migrate_hook_failures

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES ('t1', 'docs__alpha', 10, '2026-01-01T00:00:00Z')"
        )
        tid = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label='t1'"
        ).fetchone()[0]
        db.taxonomy.conn.commit()
        db.taxonomy.assign_topic(
            "doc-1", tid, assigned_by="projection",
            similarity=0.9, source_collection="docs__alpha",
        )

    # Apply the dormant 4.9.10 migration manually, then seed recent failures.
    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    conn.execute(
        "INSERT INTO hook_failures (hook_name, collection, error) "
        "VALUES (?, ?, ?)",
        ("taxonomy_assign_hook", "docs__alpha", "centroids missing"),
    )
    conn.execute(
        "INSERT INTO hook_failures (hook_name, collection, error) "
        "VALUES (?, ?, ?)",
        ("taxonomy_assign_hook", "docs__alpha", "chroma timeout"),
    )
    # An old failure (35 days ago) must NOT count toward the 24h window.
    conn.execute(
        "INSERT INTO hook_failures (hook_name, occurred_at) VALUES (?, ?)",
        ("some_old_hook", "2026-03-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "Action:" in result.output
    assert "2 post-store hook failure(s) in the last 24h" in result.output, result.output
    assert "taxonomy_assign_hook=2" in result.output
    # The 35-day-old failure is outside the window, so its hook name must not
    # appear in the Action line.
    assert "some_old_hook" not in result.output


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw-SQLite hook_failures table + migrations (status skips the read in service mode) dies at the RDR-155 P4b flip",
)
def test_cli_taxonomy_status_surfaces_batch_doc_count(tmp_path: Path) -> None:
    """RDR-095: batch-shape hook_failures rows surface their full
    doc-affected count (one row representing N documents) in the
    Action line. Per-hook breakdown still counts rows, but the
    parenthetical 'affecting M document(s)' shows the blast radius
    when M > N.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    import json
    import sqlite3

    from nexus.commands.taxonomy_cmd import taxonomy
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES ('t1', 'docs__alpha', 10, '2026-01-01T00:00:00Z')"
        )
        tid = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label='t1'"
        ).fetchone()[0]
        db.taxonomy.conn.commit()
        db.taxonomy.assign_topic(
            "doc-1", tid, assigned_by="projection",
            similarity=0.9, source_collection="docs__alpha",
        )

    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    migrate_hook_failures_batch_columns(conn)
    # One scalar row + one batch row covering 50 docs + one batch row
    # covering 25 docs. Total rows = 3, total docs affected = 76.
    conn.execute(
        "INSERT INTO hook_failures (hook_name, collection, error) "
        "VALUES (?, ?, ?)",
        ("taxonomy_assign_hook", "docs__alpha", "scalar fail"),
    )
    conn.execute(
        "INSERT INTO hook_failures "
        "(hook_name, collection, error, batch_doc_ids, is_batch) "
        "VALUES (?, ?, ?, ?, 1)",
        ("chash_dual_write_batch_hook", "docs__alpha", "batch fail",
         json.dumps([f"doc-{i}" for i in range(50)])),
    )
    conn.execute(
        "INSERT INTO hook_failures "
        "(hook_name, collection, error, batch_doc_ids, is_batch) "
        "VALUES (?, ?, ?, ?, 1)",
        ("taxonomy_assign_batch_hook", "docs__alpha", "batch fail 2",
         json.dumps([f"doc-{i}" for i in range(25)])),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "3 post-store hook failure(s) affecting 76 document(s)" in result.output
    assert "chash_dual_write_batch_hook=1" in result.output
    assert "taxonomy_assign_batch_hook=1" in result.output
    assert "taxonomy_assign_hook=1" in result.output


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw-SQLite hook_failures table + migrations (status skips the read in service mode) dies at the RDR-155 P4b flip",
)
def test_cli_taxonomy_status_handles_malformed_batch_doc_ids(
    tmp_path: Path,
) -> None:
    """RDR-095: a batch row with malformed JSON in batch_doc_ids must
    not crash the reader. The malformed row falls back to counting as
    one document affected.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    import sqlite3

    from nexus.commands.taxonomy_cmd import taxonomy
    from nexus.db.migrations import (
        migrate_hook_failures,
        migrate_hook_failures_batch_columns,
    )

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES ('t1', 'docs__alpha', 5, '2026-01-01T00:00:00Z')"
        )
        tid = db.taxonomy.conn.execute(
            "SELECT id FROM topics WHERE label='t1'"
        ).fetchone()[0]
        db.taxonomy.conn.commit()
        db.taxonomy.assign_topic(
            "doc-1", tid, assigned_by="projection",
            similarity=0.9, source_collection="docs__alpha",
        )

    conn = sqlite3.connect(str(db_path))
    migrate_hook_failures(conn)
    migrate_hook_failures_batch_columns(conn)
    conn.execute(
        "INSERT INTO hook_failures "
        "(hook_name, collection, error, batch_doc_ids, is_batch) "
        "VALUES (?, ?, ?, ?, 1)",
        ("chash_dual_write_batch_hook", "docs__alpha", "garbage payload",
         "not valid json"),
    )
    conn.commit()
    conn.close()

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    # Row count = 1, fallback doc count = 1, so no "affecting M" branch.
    assert "1 post-store hook failure(s) in the last 24h" in result.output
    assert "chash_dual_write_batch_hook=1" in result.output


def test_cli_taxonomy_status_silent_when_hook_failures_table_missing(
    tmp_path: Path,
) -> None:
    """GH #251: a DB without hook_failures table (pre-4.9.10) must not blow up."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        tid = _seed_topic(db.taxonomy, "t1", collection="docs__alpha", doc_count=10)
        db.taxonomy.assign_topic(
            "doc-1", tid, assigned_by="projection",
            similarity=0.9, source_collection="docs__alpha",
        )

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    # No Action line for hook failures; the base status still prints.
    assert "post-store hook failure" not in result.output
    assert "Total:" in result.output


def test_cli_taxonomy_status_quiet_when_projection_present(tmp_path: Path) -> None:
    """status does NOT emit the projection-missing hint when every
    collection with topics has at least one projection assignment."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        tid = _seed_topic(
            db.taxonomy, "t1", collection="docs__alpha", doc_count=10,
            review_status="accepted",
        )
        db.taxonomy.assign_topic(
            "doc-1", tid, assigned_by="projection",
            similarity=0.8, source_collection="docs__alpha",
        )

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output
    assert "[no projection]" not in result.output


def test_cli_taxonomy_list_shows_collection_at_root(tmp_path: Path) -> None:
    """CLI taxonomy list includes [collection] prefix on root topics.

    GitHub #241 Item 1: without a per-topic collection tag, the flat
    listing across multi-collection setups gives no way to tell which
    topic belongs to which collection.
    """
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.commands.taxonomy_cmd import taxonomy

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        _seed_topic(db.taxonomy, "Alpha topic", collection="docs__alpha", doc_count=10)
        _seed_topic(db.taxonomy, "Beta topic", collection="docs__beta", doc_count=5)

    runner = CliRunner()
    with patch(
        "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
    ):
        result = runner.invoke(taxonomy, ["list"])

    assert result.exit_code == 0, result.output
    # Each root's collection is shown in a [coll] prefix column.
    assert "[docs__alpha]" in result.output
    assert "Alpha topic" in result.output
    assert "[docs__beta]" in result.output
    assert "Beta topic" in result.output


# ── discover_for_collection + CLI (RDR-070, nexus-2dq) ──────────────────────


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw-path discover_for_collection over a raw chroma client + t2_index_write routing (service branch routes _discover_via_service against a service T3) dies at the RDR-155 P4b flip",
)
def test_discover_for_collection(
    db: T2Database, chroma_client: chromadb.ClientAPI, monkeypatch,
) -> None:
    """discover_for_collection fetches texts, embeds with MiniLM, runs discover_topics.

    RDR-151 Phase 3 routing proof: the SPY asserts t2_index_write is called at
    least twice (persist_discovered_topics + record_discover_count), proving the
    wiring rather than just confirming rows land.
    """
    from nexus.commands.taxonomy_cmd import discover_for_collection
    from nexus.db.local_ef import LocalEmbeddingFunction

    # RDR-151 Phase 3: spy on t2_index_write to count routed calls.
    # Pin to the test fixture db (no-daemon fallback opens default_db_path).
    import nexus.mcp_infra as _mi
    t2_write_call_count = 0

    def _spy(fn):
        nonlocal t2_write_call_count
        t2_write_call_count += 1
        return fn(db)

    monkeypatch.setattr(_mi, "t2_index_write", _spy)

    # Seed a ChromaDB collection with documents
    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    texts_a = [f"machine learning neural network gradient {i}" for i in range(30)]
    texts_b = [f"database query indexing sql schema {i}" for i in range(30)]
    texts = texts_a + texts_b
    doc_ids = [f"doc-{i}" for i in range(60)]

    coll = chroma_client.get_or_create_collection(
        "test__discover", embedding_function=None,
    )
    embeddings = ef(texts)
    coll.add(ids=doc_ids, documents=texts, embeddings=embeddings)

    count = discover_for_collection(
        "test__discover", db.taxonomy, chroma_client, force=False,
    )
    assert count >= 2
    topics = db.taxonomy.get_topics()
    assert len(topics) >= 2

    # Routing proof: at minimum persist_discovered_topics + record_discover_count
    # (+ persist_cross_links if cross-links are computed = 3+). Must be >= 2.
    assert t2_write_call_count >= 2, (
        f"expected >= 2 t2_index_write calls (persist_discovered_topics + "
        f"record_discover_count), got {t2_write_call_count}"
    )


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw-path discover_for_collection over a raw chroma client + t2_index_write routing (service branch routes _discover_via_service against a service T3) dies at the RDR-155 P4b flip",
)
def test_discover_for_collection_force(
    db: T2Database, chroma_client: chromadb.ClientAPI, monkeypatch,
) -> None:
    """force=True clears existing topics before re-discovering fresh ones."""
    from nexus.commands.taxonomy_cmd import discover_for_collection
    from nexus.db.local_ef import LocalEmbeddingFunction

    import nexus.mcp_infra as _mi
    monkeypatch.setattr(_mi, "t2_index_write", lambda fn: fn(db))

    ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    texts = (
        [f"machine learning neural {i}" for i in range(30)]
        + [f"database query sql {i}" for i in range(30)]
    )
    doc_ids = [f"doc-{i}" for i in range(60)]

    coll = chroma_client.get_or_create_collection(
        "test__force", embedding_function=None,
    )
    coll.add(ids=doc_ids, documents=texts, embeddings=ef(texts))

    count1 = discover_for_collection(
        "test__force", db.taxonomy, chroma_client, force=False,
    )
    ids_after_first = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__force"
    }

    count2 = discover_for_collection(
        "test__force", db.taxonomy, chroma_client, force=True,
    )
    ids_after_second = {
        t["id"] for t in db.taxonomy.get_topics()
        if t.get("collection") == "test__force"
    }

    assert count1 >= 2
    assert count2 >= 2
    # force=True should replace topics, not accumulate
    assert len(ids_after_second) == count2
    # Assignments should be consistent — not doubled
    total_assignments = _collection_assignment_count(db.taxonomy, "test__force")
    assert total_assignments <= 60, "force rebuild should replace, not accumulate"


def test_discover_cli_invocation() -> None:
    """nx taxonomy discover --collection <name> exits 0 in local mode.

    RDR-155 P4a.2 (nexus-1k8s1): the command's bootstrapping path
    constructs a T3 handle via ``make_t3()`` before reaching the
    mocked ``discover_for_collection``. Post-cutover the real handle is
    service-backed (no raw chroma client) and the command refuses; stub
    ``make_t3`` with a chroma-shaped MagicMock so the CLI plumbing under
    test still runs.
    """
    from unittest.mock import MagicMock, patch

    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    runner = CliRunner()
    with (
        patch("nexus.commands.taxonomy_cmd.discover_for_collection", return_value=3) as mock_fn,
        patch("nexus.db.make_t3", return_value=MagicMock(spec=T3Database)),
    ):
        result = runner.invoke(taxonomy, ["discover", "--collection", "test__coll"])

    assert result.exit_code == 0, result.output
    assert "3 topics" in result.output
    mock_fn.assert_called_once()


def test_rebuild_cli_is_discover_force_alias() -> None:
    """nx taxonomy rebuild --collection <name> delegates to discover --force.

    RDR-120 P6: see test_discover_cli_invocation for the rationale.
    """
    from unittest.mock import MagicMock, patch

    from click.testing import CliRunner
    from nexus.commands.taxonomy_cmd import taxonomy

    runner = CliRunner()
    with (
        patch("nexus.commands.taxonomy_cmd.discover_for_collection", return_value=2) as mock_fn,
        patch("nexus.db.make_t3", return_value=MagicMock(spec=T3Database)),
    ):
        result = runner.invoke(taxonomy, ["rebuild", "--collection", "test__coll"])

    assert result.exit_code == 0, result.output
    mock_fn.assert_called_once()
    _, kwargs = mock_fn.call_args
    assert kwargs.get("force") is True


# ── MiniLM topic quality validation (RDR-070, nexus-7m8) ─────────────────────


class TestMiniLMTopicQuality:
    """Validate HDBSCAN topic quality on code-representative chunks.

    Uses LocalEmbeddingFunction (MiniLM 384d) for real semantic embeddings
    rather than random vectors — validates that the clustering pipeline
    produces coherent topics from identifier-heavy code text.
    """

    @pytest.fixture()
    def ef(self):
        from nexus.db.local_ef import LocalEmbeddingFunction
        return LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")

    @pytest.fixture()
    def chroma(self):
        return make_vector_test_client()

    def test_code_chunk_topic_quality(
        self, db: T2Database, ef, chroma,
    ) -> None:
        """Topics from code-like chunks show recognizable structural patterns."""
        # Three domains of code-like text
        http_chunks = [
            f"def handle_request(request): response = json_response(status={200+i}) return response"
            for i in range(20)
        ] + [
            f"@app.route('/api/v{i}') def endpoint(): return jsonify(data)"
            for i in range(10)
        ]
        db_chunks = [
            f"cursor.execute('SELECT id, name FROM users WHERE age > {i}') rows = cursor.fetchall()"
            for i in range(20)
        ] + [
            f"conn.execute('INSERT INTO logs (event, ts) VALUES (?, ?)', (event_{i}, now()))"
            for i in range(10)
        ]
        test_chunks = [
            f"def test_create_user_{i}(db): user = db.put(name='test') assert user.id is not None"
            for i in range(20)
        ] + [
            f"@pytest.fixture def mock_client_{i}(): return MockClient(timeout={i})"
            for i in range(10)
        ]

        texts = http_chunks + db_chunks + test_chunks
        doc_ids = [f"chunk-{i}" for i in range(len(texts))]
        embeddings = np.array(ef(texts), dtype=np.float32)

        count = db.taxonomy.discover_topics(
            "code__test", doc_ids, embeddings, texts, chroma,
        )

        # Should find at least 2 distinct clusters (ideally 3)
        assert count >= 2, f"Expected >=2 topics from 3 code domains, got {count}"

        topics = db.taxonomy.get_topics()
        # Each topic label should be non-empty (c-TF-IDF produced terms)
        for t in topics:
            assert t["label"].strip(), f"Topic {t['id']} has empty label"
            assert t["doc_count"] > 0

    def test_nearest_centroid_agreement(
        self, db: T2Database, ef, chroma,
    ) -> None:
        """Hold-out agreement: nearest-centroid assigns consistently with batch.

        Hold out 10% of embeddings, run discover on 90%, then check that
        assign_single maps held-out docs to the same cluster HDBSCAN would
        have assigned them to. Reports agreement percentage.
        """
        from sklearn.cluster import HDBSCAN as SklearnHDBSCAN

        # Same code domains as above for reproducibility
        http_chunks = [f"def handle_request(r): return json_response({i})" for i in range(30)]
        db_chunks = [f"cursor.execute('SELECT * FROM table_{i}') rows = cursor.fetchall()" for i in range(30)]
        test_chunks = [f"def test_feature_{i}(db): result = db.get({i}) assert result" for i in range(30)]

        texts = http_chunks + db_chunks + test_chunks
        doc_ids = [f"chunk-{i}" for i in range(len(texts))]
        embeddings = np.array(ef(texts), dtype=np.float32)

        # Hold out last 10% from each domain (3 per domain = 9 total)
        holdout_indices = list(range(27, 30)) + list(range(57, 60)) + list(range(87, 90))
        train_mask = np.ones(len(texts), dtype=bool)
        train_mask[holdout_indices] = False

        train_ids = [doc_ids[i] for i in range(len(doc_ids)) if train_mask[i]]
        train_texts = [texts[i] for i in range(len(texts)) if train_mask[i]]
        train_embs = embeddings[train_mask]

        # Discover on training set
        count = db.taxonomy.discover_topics(
            "code__agreement", train_ids, train_embs, train_texts, chroma,
        )
        assert count >= 2

        # Also run batch HDBSCAN on FULL set to get "ground truth" labels
        min_cs = max(5, len(embeddings) // 15)
        full_labels = SklearnHDBSCAN(
            min_cluster_size=min_cs, store_centers="centroid", copy=True,
        ).fit_predict(embeddings)

        # For each held-out doc, check that assign_single assigns it to a
        # topic whose members come from the SAME domain. The three domains
        # are http (indices 0-29), db (30-59), test (60-89). We check that
        # the held-out doc and the majority of the topic's training docs
        # share the same domain.
        def _domain(i: int) -> str:
            if i < 30:
                return "http"
            if i < 60:
                return "db"
            return "test"

        agreements = 0
        total = 0
        for idx in holdout_indices:
            if full_labels[idx] < 0:
                continue  # skip noise in full batch
            result = db.taxonomy.assign_single(
                "code__agreement", embeddings[idx], chroma,
            )
            if result is None:
                continue
            topic_id = result.topic_id
            total += 1

            # Check domain coherence: get docs assigned to this topic
            # and verify most share the same domain as the held-out doc
            topic_docs = db.taxonomy.get_all_topic_doc_ids(topic_id)
            doc_domain = _domain(idx)
            topic_domains = [
                _domain(int(did.split("-")[1]))
                for did in topic_docs
                if did.startswith("chunk-")
            ]
            if topic_domains:
                majority_domain = max(set(topic_domains), key=topic_domains.count)
                if majority_domain == doc_domain:
                    agreements += 1

        # Agreement threshold: domain coherence should hold for most
        assert total >= len(holdout_indices) // 2, (
            f"Too few held-out docs assigned: {total}/{len(holdout_indices)}"
        )
        if total > 0:
            agreement_rate = agreements / total
            assert agreement_rate >= 0.8, (
                f"Domain coherence too low: {agreements}/{total} = {agreement_rate:.0%}"
            )


# ── sklearn HDBSCAN smoke tests (RDR-070, nexus-86v) ────────────────────────
#
# scikit-learn>=1.3 is a core dep. sklearn.cluster.HDBSCAN for topic
# discovery, c-TF-IDF labels via CountVectorizer+TfidfTransformer,
# incremental assignment via nearest centroid in ChromaDB.
# No torch, no sentence-transformers, no optional extra needed.


class TestSklearnHdbscanSmoke:
    """Verify sklearn HDBSCAN + TF-IDF topic pipeline works on 384d embeddings."""

    def test_hdbscan_finds_clusters(self) -> None:
        """HDBSCAN discovers clusters from well-separated 384d embeddings."""
        from sklearn.cluster import HDBSCAN

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        clusterer = HDBSCAN(min_cluster_size=5, store_centers="centroid", copy=True)
        labels = clusterer.fit_predict(embeddings)

        assert len(labels) == 60
        real_topics = {t for t in labels if t >= 0}
        assert len(real_topics) >= 2, f"Expected >=2 clusters, got {real_topics}"
        assert clusterer.centroids_.shape[1] == 384

    def test_tfidf_topic_labels(self) -> None:
        """c-TF-IDF produces meaningful per-cluster labels from doc text."""
        from sklearn.cluster import HDBSCAN
        from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer

        rng = np.random.default_rng(42)
        docs_a = [f"machine learning neural network gradient {i}" for i in range(30)]
        docs_b = [f"database query indexing sql schema {i}" for i in range(30)]
        docs = docs_a + docs_b

        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        labels = HDBSCAN(min_cluster_size=5, copy=True).fit_predict(embeddings)

        vectorizer = CountVectorizer(stop_words="english")
        tfidf = TfidfTransformer().fit_transform(vectorizer.fit_transform(docs))
        feature_names = vectorizer.get_feature_names_out()

        real_clusters = {t for t in labels if t >= 0}
        for cid in real_clusters:
            mask = labels == cid
            cluster_tfidf = tfidf[mask].mean(axis=0).A1
            top_idx = cluster_tfidf.argsort()[-3:][::-1]
            top_terms = [feature_names[i] for i in top_idx]
            assert len(top_terms) == 3
            # Each cluster's top terms should come from its own domain
            assert any(t in top_terms for t in
                       ["machine", "learning", "neural", "network", "gradient",
                        "database", "query", "indexing", "sql", "schema"])

    def test_incremental_nearest_centroid(self) -> None:
        """New docs assigned to nearest cluster centroid via cosine similarity."""
        from sklearn.cluster import HDBSCAN
        from sklearn.metrics.pairwise import cosine_similarity

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0

        clusterer = HDBSCAN(min_cluster_size=5, store_centers="centroid", copy=True)
        labels = clusterer.fit_predict(embeddings)

        # New embedding near cluster A
        new_emb = rng.standard_normal((1, 384)).astype(np.float32) * 0.1
        new_emb[0, 0] += 3.0

        sims = cosine_similarity(new_emb, clusterer.centroids_)
        assigned = int(sims.argmax())

        # Should assign to same cluster as the first 30 docs
        cluster_a = labels[0]
        assert assigned == cluster_a
        assert sims[0, assigned] > 0.5


# ── Review infrastructure (RDR-070, nexus-lbu) ────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
    reason="dies-roster: raw PRAGMA/schema-default introspection of the SQLite twin dies at the RDR-155 P4b flip",
)
class TestReviewSchema:
    """Schema migrations for review_status and terms columns."""

    def test_review_status_column_exists(self, db: T2Database) -> None:
        """review_status column added to topics table via migration."""
        cols = {
            row[1]
            for row in db.taxonomy.conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "review_status" in cols

    def test_terms_column_exists(self, db: T2Database) -> None:
        """terms column added to topics table via migration."""
        cols = {
            row[1]
            for row in db.taxonomy.conn.execute("PRAGMA table_info(topics)").fetchall()
        }
        assert "terms" in cols

    def test_review_status_default_pending(self, db: T2Database) -> None:
        """New topics default to review_status='pending'."""
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("test", "proj", 1, "2026-01-01T00:00:00Z"),
        )
        db.taxonomy.conn.commit()
        row = db.taxonomy.conn.execute(
            "SELECT review_status FROM topics LIMIT 1"
        ).fetchone()
        assert row[0] == "pending"


class TestReviewMethods:
    """CatalogTaxonomy methods for review workflow."""

    def test_get_unreviewed_topics(self, db: T2Database) -> None:
        """get_unreviewed_topics returns only pending topics."""
        _seed_topic(db.taxonomy, "pending-topic", collection="proj", doc_count=5,
                    review_status="pending")
        _seed_topic(db.taxonomy, "accepted-topic", collection="proj", doc_count=3,
                    review_status="accepted")
        _seed_topic(db.taxonomy, "deleted-topic", collection="proj", doc_count=1,
                    review_status="deleted")

        unreviewed = db.taxonomy.get_unreviewed_topics(collection="proj")
        assert len(unreviewed) == 1
        assert unreviewed[0]["label"] == "pending-topic"

    def test_get_unreviewed_topics_limit(self, db: T2Database) -> None:
        """get_unreviewed_topics respects limit."""
        for i in range(10):
            _seed_topic(db.taxonomy, f"topic-{i}", collection="proj",
                        doc_count=i + 1)

        result = db.taxonomy.get_unreviewed_topics(collection="proj", limit=3)
        assert len(result) == 3

    def test_get_unreviewed_topics_all_collections(self, db: T2Database) -> None:
        """get_unreviewed_topics with empty collection returns all."""
        _seed_topic(db.taxonomy, "topic-a", collection="coll-a", doc_count=5)
        _seed_topic(db.taxonomy, "topic-b", collection="coll-b", doc_count=3)

        result = db.taxonomy.get_unreviewed_topics()
        assert len(result) == 2

    def test_mark_topic_reviewed(self, db: T2Database) -> None:
        """mark_topic_reviewed updates review_status."""
        topic_id = _seed_topic(db.taxonomy, "test", collection="proj", doc_count=5)

        db.taxonomy.mark_topic_reviewed(topic_id, "accepted")

        assert db.taxonomy.get_topic_by_id(topic_id)["review_status"] == "accepted"

    def test_rename_topic(self, db: T2Database) -> None:
        """rename_topic updates label and sets review_status='accepted'."""
        topic_id = _seed_topic(db.taxonomy, "old-label", collection="proj",
                               doc_count=5)

        db.taxonomy.rename_topic(topic_id, "new-label")

        topic = db.taxonomy.get_topic_by_id(topic_id)
        assert topic["label"] == "new-label"
        assert topic["review_status"] == "accepted"

    def test_delete_topic(self, db: T2Database) -> None:
        """delete_topic removes topic and its assignments."""
        topic_id = _seed_topic(db.taxonomy, "doomed", collection="proj", doc_count=1)
        _seed_assignment(db.taxonomy, "doc-1", topic_id)

        db.taxonomy.delete_topic(topic_id)

        assert db.taxonomy.get_topic_by_id(topic_id) is None
        assert db.taxonomy.get_all_topic_doc_ids(topic_id) == []

    def test_merge_topics(self, db: T2Database) -> None:
        """merge_topics moves assignments from source to target, deletes source."""
        source_id = _seed_topic(db.taxonomy, "source", collection="proj", doc_count=2)
        target_id = _seed_topic(db.taxonomy, "target", collection="proj", doc_count=3)
        _seed_assignment(db.taxonomy, "doc-a", source_id)
        _seed_assignment(db.taxonomy, "doc-b", source_id)
        _seed_assignment(db.taxonomy, "doc-c", target_id)

        db.taxonomy.merge_topics(source_id, target_id)

        # Source topic deleted
        assert db.taxonomy.get_topic_by_id(source_id) is None
        # Target doc_count = actual assignment count (3 distinct docs)
        assert db.taxonomy.get_topic_by_id(target_id)["doc_count"] == 3
        # All assignments on target
        assert sorted(db.taxonomy.get_all_topic_doc_ids(target_id)) == [
            "doc-a", "doc-b", "doc-c",
        ]

    def test_merge_topics_dedup(self, db: T2Database) -> None:
        """merge_topics handles docs assigned to both source and target."""
        source_id = _seed_topic(db.taxonomy, "source", collection="proj", doc_count=1)
        target_id = _seed_topic(db.taxonomy, "target", collection="proj", doc_count=1)

        # Same doc assigned to both topics
        _seed_assignment(db.taxonomy, "shared-doc", source_id)
        _seed_assignment(db.taxonomy, "shared-doc", target_id)

        db.taxonomy.merge_topics(source_id, target_id)

        # Only one assignment for the shared doc on target
        assert db.taxonomy.get_all_topic_doc_ids(target_id) == ["shared-doc"]

    # ── RDR-164 P5 (nexus-c6vze): dead Chroma centroid cleanup removed ────────
    # nexus-5kl1b closed obsolete: post-RDR-155 P4a the raw-Chroma
    # taxonomy__centroids path is unreachable (make_t3 -> HttpVectorClient).
    # delete_topic/merge_topics must NO LONGER touch a passed chroma_client.

    def test_delete_topic_ignores_chroma_client(self, db: T2Database) -> None:
        from unittest.mock import MagicMock

        tid = _seed_topic(db.taxonomy, "doomed", collection="proj", doc_count=1)
        mock_chroma = MagicMock()

        db.taxonomy.delete_topic(tid, chroma_client=mock_chroma)

        # Relational delete still happens; the retired Chroma cleanup does not.
        assert db.taxonomy.get_topic_by_id(tid) is None
        mock_chroma.get_collection.assert_not_called()

    def test_merge_topics_ignores_chroma_client(self, db: T2Database) -> None:
        from unittest.mock import MagicMock

        source_id = _seed_topic(db.taxonomy, "source", collection="proj", doc_count=1)
        target_id = _seed_topic(db.taxonomy, "target", collection="proj", doc_count=1)
        mock_chroma = MagicMock()

        db.taxonomy.merge_topics(source_id, target_id, chroma_client=mock_chroma)

        assert db.taxonomy.get_topic_by_id(source_id) is None
        mock_chroma.get_collection.assert_not_called()

    def test_get_topic_by_id(self, db: T2Database) -> None:
        """get_topic_by_id returns a single topic dict or None."""
        topic_id = _seed_topic(db.taxonomy, "my-topic", collection="proj", doc_count=7)

        result = db.taxonomy.get_topic_by_id(topic_id)
        assert result is not None
        assert result["label"] == "my-topic"
        assert result["doc_count"] == 7

        assert db.taxonomy.get_topic_by_id(99999) is None

    def test_get_topic_doc_ids(self, db: T2Database) -> None:
        """get_topic_doc_ids returns limited doc_ids for a topic."""
        topic_id = _seed_topic(db.taxonomy, "test", collection="proj", doc_count=5)
        for i in range(5):
            _seed_assignment(db.taxonomy, f"doc-{i}", topic_id)

        result = db.taxonomy.get_topic_doc_ids(topic_id, limit=3)
        assert len(result) == 3
        assert all(isinstance(d, str) for d in result)


class TestDiscoverStoresTerms:
    """discover_topics stores c-TF-IDF terms in the terms column."""

    @pytest.fixture()
    def chroma_client(self) -> chromadb.ClientAPI:
        return make_vector_test_client()

    def test_terms_stored_as_json(
        self, db: T2Database, chroma_client: chromadb.ClientAPI,
    ) -> None:
        """discover_topics persists top c-TF-IDF terms as JSON."""
        import json

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning neural network gradient {i}" for i in range(30)]
            + [f"database query indexing sql schema {i}" for i in range(30)]
        )

        db.taxonomy.discover_topics(
            "test__coll", doc_ids, embeddings, texts, chroma_client,
        )

        rows = [
            t["terms"] for t in db.taxonomy.get_topics_for_collection("test__coll")
            if t.get("terms")
        ]
        assert len(rows) >= 2
        for raw in rows:
            terms = json.loads(raw)
            assert isinstance(terms, list)
            assert len(terms) >= 3


class TestReviewCLI:
    """CLI tests for nx taxonomy review."""

    def _seed_topics(self, db_path: Path) -> int:
        """Insert test topics and return the first topic_id."""
        import json

        with T2Database(db_path) as db:
            topic_id = _seed_topic(
                db.taxonomy,
                "machine learning",
                collection="proj",
                doc_count=5,
                review_status="pending",
                terms=json.dumps(["neural", "network", "gradient", "loss", "model"]),
            )
            for i in range(3):
                _seed_assignment(db.taxonomy, f"src/model_{i}.py", topic_id)
        return topic_id

    @staticmethod
    def _t2_router(db_path: Path):
        """Return a t2_index_write stub that routes through db_path.

        RDR-151 Phase 3: CLI tests that exercise routed write paths must
        patch nexus.mcp_infra.t2_index_write so the daemon stub uses the
        test's tmp_path db rather than the autouse-isolated default path.
        """
        def _router(fn):
            with T2Database(db_path) as db:
                return fn(db)
        return _router

    def test_review_accept(self, tmp_path: Path) -> None:
        """Accept action marks topic as accepted."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        topic_id = self._seed_topics(db_path)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="a\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.get_topic_by_id(topic_id)["review_status"]
        assert status == "accepted"

    def test_review_skip(self, tmp_path: Path) -> None:
        """Skip action leaves topic as pending."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        topic_id = self._seed_topics(db_path)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="S\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            status = db.taxonomy.get_topic_by_id(topic_id)["review_status"]
        assert status == "pending"

    def test_review_rename(self, tmp_path: Path) -> None:
        """Rename action updates the topic label."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        topic_id = self._seed_topics(db_path)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy,
                ["review", "--collection", "proj"],
                input="r\ndeep learning\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            topic = db.taxonomy.get_topic_by_id(topic_id)
        assert topic["label"] == "deep learning"
        assert topic["review_status"] == "accepted"

    def test_review_delete(self, tmp_path: Path) -> None:
        """Delete action removes topic and assignments."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        topic_id = self._seed_topics(db_path)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"], input="d\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(topic_id) is None
            assert db.taxonomy.get_topics_for_collection("proj") == []

    def test_review_no_unreviewed(self, tmp_path: Path) -> None:
        """Shows 'all done' message when no topics need review."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        # Create DB but don't add any topics
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["review", "--collection", "proj"],
            )

        assert result.exit_code == 0
        assert "no unreviewed topics" in result.output.lower() or "all done" in result.output.lower()

    def test_review_merge(self, tmp_path: Path) -> None:
        """Merge action moves docs to target topic."""
        import json
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            # Source topic (pending)
            source_id = _seed_topic(
                db.taxonomy, "source topic", collection="proj", doc_count=2,
                review_status="pending", terms=json.dumps(["a", "b", "c"]),
            )
            # Target topic (already accepted)
            target_id = _seed_topic(
                db.taxonomy, "target topic", collection="proj", doc_count=3,
                review_status="accepted", terms=json.dumps(["d", "e", "f"]),
            )
            _seed_assignment(db.taxonomy, "doc-a", source_id)
            _seed_assignment(db.taxonomy, "doc-b", source_id)
            _seed_assignment(db.taxonomy, "doc-c", target_id)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            # m = merge, then enter target topic ID
            result = runner.invoke(
                taxonomy,
                ["review", "--collection", "proj"],
                input=f"m\n{target_id}\n",
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            # Source deleted
            assert db.taxonomy.get_topic_by_id(source_id) is None
            # All docs on target
            docs = db.taxonomy.get_all_topic_doc_ids(target_id)
            assert set(docs) == {"doc-a", "doc-b", "doc-c"}


# ── Manual taxonomy operations CLI (RDR-070, nexus-c3w) ───────────────────


class TestResolveLabel:
    """resolve_label looks up topic_id by label."""

    def test_resolve_existing(self, db: T2Database) -> None:
        _seed_topic(db.taxonomy, "my-topic", collection="proj", doc_count=5)
        result = db.taxonomy.resolve_label("my-topic", collection="proj")
        assert result is not None
        assert isinstance(result, int)

    def test_resolve_missing(self, db: T2Database) -> None:
        assert db.taxonomy.resolve_label("nonexistent") is None

    def test_resolve_scoped_by_collection(self, db: T2Database) -> None:
        _seed_topic(db.taxonomy, "shared-label", collection="coll-a", doc_count=3)
        _seed_topic(db.taxonomy, "shared-label", collection="coll-b", doc_count=2)
        result = db.taxonomy.resolve_label("shared-label", collection="coll-b")
        assert result is not None
        topic = db.taxonomy.get_topic_by_id(result)
        assert topic["collection"] == "coll-b"


class TestSplitTopic:
    """split_topic creates child topics via KMeans sub-clustering."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return make_vector_test_client()

    def test_split_creates_children(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """Split a parent topic into k children via KMeans."""
        from nexus.db.local_ef import LocalEmbeddingFunction

        # Create parent topic with mixed docs
        parent_id = _seed_topic(
            db.taxonomy, "mixed-topic", collection="test__split", doc_count=30,
        )

        # Two domains — split should separate them
        texts_a = [f"machine learning gradient descent {i}" for i in range(15)]
        texts_b = [f"database query sql index {i}" for i in range(15)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(30)]

        for did in doc_ids:
            _seed_assignment(db.taxonomy, did, parent_id)

        # Seed the T3 collection with docs
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        coll = chroma.get_or_create_collection(
            "test__split", embedding_function=None,
        )
        emb_list = ef(texts)
        coll.add(ids=doc_ids, documents=texts, embeddings=emb_list)

        # Seed a parent centroid in taxonomy__centroids (raw chroma leg
        # only — on the engine substrate centroids route through the
        # centroid port and split does not need the parent centroid to
        # pre-exist).
        import numpy as _np
        if has_raw_access(db.taxonomy):
            centroid_coll = chroma.get_or_create_collection(
                "taxonomy__centroids",
                embedding_function=None,
                metadata={"hnsw:space": "cosine"},
            )
            parent_centroid = _np.array(emb_list).mean(axis=0).tolist()
            centroid_coll.add(
                ids=[f"test__split:{parent_id}"],
                embeddings=[parent_centroid],
                metadatas=[{
                    "topic_id": parent_id,
                    "label": "mixed-topic",
                    "collection": "test__split",
                    "doc_count": 30,
                }],
            )

        child_count = db.taxonomy.split_topic(
            parent_id, k=2, chroma_client=chroma,
        )
        assert child_count == 2

        # Children exist with parent_id set
        children = db.taxonomy.get_topics(parent_id=parent_id)
        assert len(children) == 2
        assert all(c["doc_count"] > 0 for c in children)

        # Parent has no direct assignments (all moved to children)
        parent_docs = db.taxonomy.get_topic_doc_ids(parent_id)
        assert len(parent_docs) == 0

        # Centroids: parent removed, children added
        state = _centroid_state(db.taxonomy, "test__split", chroma)
        centroid_topic_ids = {int(t) for t in state["old_centroid_topic_ids"]}
        child_ids = {c["id"] for c in children}
        # Parent centroid should be gone, child centroids should exist
        assert parent_id not in centroid_topic_ids
        assert child_ids == centroid_topic_ids

    def test_get_all_topics_returns_roots_and_children(self, db: T2Database) -> None:
        """get_all_topics returns every row; get_topics returns only roots.

        GitHub #243 + bead nexus-kxez. label / relabel pre-check used
        get_topics and therefore couldn't see split sub-topics.
        """
        parent_a = _seed_topic(db.taxonomy, "root-a", collection="c", doc_count=10)
        _seed_topic(db.taxonomy, "root-b", collection="c", doc_count=8)
        _seed_topic(
            db.taxonomy, "child-a1", collection="c", doc_count=4,
            parent_id=parent_a,
        )

        roots = db.taxonomy.get_topics()
        assert {t["label"] for t in roots} == {"root-a", "root-b"}

        everything = db.taxonomy.get_all_topics()
        assert {t["label"] for t in everything} == {"root-a", "root-b", "child-a1"}

    def test_get_all_topics_filters_by_collection(self, db: T2Database) -> None:
        """get_all_topics(collection=...) narrows to one collection."""
        _seed_topic(db.taxonomy, "a-root", collection="c1", doc_count=10)
        _seed_topic(db.taxonomy, "b-root", collection="c2", doc_count=5)
        # Keyword form: the Http twin's get_all_topics takes collection
        # keyword-only.
        assert {t["label"] for t in db.taxonomy.get_all_topics(collection="c1")} == {"a-root"}
        assert {t["label"] for t in db.taxonomy.get_all_topics(collection="c2")} == {"b-root"}

    def test_update_topic_label_preserves_review_status(
        self, db: T2Database,
    ) -> None:
        """update_topic_label changes only the label, not review_status.

        GitHub #241 Item 3: rename_topic sets review_status='accepted'
        as a side effect, which is correct for the interactive review
        path but wrong for batch LLM labeling. update_topic_label is
        the label-only helper.
        """
        tid = _seed_topic(
            db.taxonomy, "old-label", collection="c", doc_count=5,
            review_status="pending",
        )

        db.taxonomy.update_topic_label(tid, "new-label")
        topic = db.taxonomy.get_topic_by_id(tid)
        assert (topic["label"], topic["review_status"]) == ("new-label", "pending")

    def test_rename_topic_still_accepts(self, db: T2Database) -> None:
        """rename_topic continues to transition review_status → accepted.

        Used by the interactive ``nx taxonomy review`` rename path.
        Regression guard: the #241 Item 3 fix does not touch rename_topic.
        """
        tid = _seed_topic(
            db.taxonomy, "old", collection="c", doc_count=5,
            review_status="pending",
        )

        db.taxonomy.rename_topic(tid, "renamed")
        topic = db.taxonomy.get_topic_by_id(tid)
        assert (topic["label"], topic["review_status"]) == ("renamed", "accepted")

    def test_get_projection_counts_by_collection(self, db: T2Database) -> None:
        """get_projection_counts_by_collection groups by source_collection.

        GitHub #239: status uses this to flag collections with topics
        but zero projection assignments.
        """
        # Seed two topics (targets) in different collections
        tgt_a = _seed_topic(db.taxonomy, "tgt-a", collection="c_target_a", doc_count=5)
        tgt_b = _seed_topic(db.taxonomy, "tgt-b", collection="c_target_b", doc_count=3)

        # Projection assignments originate from two different source collections.
        db.taxonomy.assign_topic(
            "doc-1", tgt_a, assigned_by="projection",
            similarity=0.9, source_collection="c_src_1",
        )
        db.taxonomy.assign_topic(
            "doc-2", tgt_a, assigned_by="projection",
            similarity=0.8, source_collection="c_src_1",
        )
        db.taxonomy.assign_topic(
            "doc-3", tgt_b, assigned_by="projection",
            similarity=0.7, source_collection="c_src_2",
        )
        # A non-projection assignment must be ignored by the helper.
        db.taxonomy.assign_topic(
            "doc-4", tgt_a, assigned_by="hdbscan",
        )

        counts = db.taxonomy.get_projection_counts_by_collection()
        assert counts == {"c_src_1": 2, "c_src_2": 1}

    def test_refresh_projection_links_aggregates_per_chunk_pairs(
        self, db: T2Database,
    ) -> None:
        """refresh_projection_links produces (src_topic, tgt_topic) pair counts.

        GitHub #240: project --persist only wrote topic_assignments;
        links view read topic_links which was stale. The refresh helper
        aggregates per-chunk projection rows into topic-pair counts
        and upserts them into topic_links.
        """
        src_id = _seed_topic(db.taxonomy, "src-topic", collection="c_src", doc_count=3)
        tgt_id = _seed_topic(db.taxonomy, "tgt-topic", collection="c_tgt", doc_count=0)

        # Three docs assigned to src-topic via hdbscan, then projected to tgt-topic.
        for doc_id in ("doc-1", "doc-2", "doc-3"):
            db.taxonomy.assign_topic(doc_id, src_id, assigned_by="hdbscan")
            db.taxonomy.assign_topic(
                doc_id, tgt_id, assigned_by="projection",
                similarity=0.8, source_collection="c_src",
            )

        written = db.taxonomy.refresh_projection_links()
        assert written == 1

        pair = (min(src_id, tgt_id), max(src_id, tgt_id))
        count = _link_pairs(db.taxonomy, [src_id, tgt_id]).get(pair)
        assert count == 3  # three per-chunk projection rows
        if has_raw_access(db.taxonomy):
            row = db.taxonomy.conn.execute(
                "SELECT link_types FROM topic_links "
                "WHERE from_topic_id = ? AND to_topic_id = ?",
                pair,
            ).fetchone()
            assert "projection" in row[0]

    def test_refresh_projection_links_merges_existing_types(
        self, db: T2Database,
    ) -> None:
        """Existing link_types (e.g. 'cites') survive the projection refresh."""
        import json as _json

        src_id = _seed_topic(db.taxonomy, "src", collection="c1", doc_count=1)
        tgt_id = _seed_topic(db.taxonomy, "tgt", collection="c2", doc_count=0)
        from_id = min(src_id, tgt_id)
        to_id = max(src_id, tgt_id)

        # Seed an existing link_types entry (simulates prior compute_topic_links)
        db.taxonomy.upsert_topic_links([
            {"from_topic_id": from_id, "to_topic_id": to_id,
             "link_count": 5, "link_types": ["cites"]},
        ])

        # Assign a projection pair
        db.taxonomy.assign_topic("doc-1", src_id, assigned_by="hdbscan")
        db.taxonomy.assign_topic(
            "doc-1", tgt_id, assigned_by="projection",
            similarity=0.9, source_collection="c1",
        )

        db.taxonomy.refresh_projection_links()

        # The pair still exists after the refresh on both substrates.
        assert (from_id, to_id) in _link_pairs(db.taxonomy, [from_id, to_id])
        if has_raw_access(db.taxonomy):
            row = db.taxonomy.conn.execute(
                "SELECT link_types FROM topic_links "
                "WHERE from_topic_id = ? AND to_topic_id = ?",
                (from_id, to_id),
            ).fetchone()
            types = _json.loads(row[0])
            assert set(types) == {"cites", "projection"}

    def test_refresh_projection_links_no_op_when_no_projections(
        self, db: T2Database,
    ) -> None:
        """No projection rows → returns 0 and doesn't crash."""
        _seed_topic(db.taxonomy, "t", collection="c", doc_count=0)
        assert db.taxonomy.refresh_projection_links() == 0

    def test_split_too_few_docs(self, db: T2Database) -> None:
        """Split with fewer docs than k returns 0."""
        parent_id = _seed_topic(db.taxonomy, "tiny", collection="proj", doc_count=2)
        _seed_assignment(db.taxonomy, "doc-0", parent_id)
        _seed_assignment(db.taxonomy, "doc-1", parent_id)

        result = db.taxonomy.split_topic(
            parent_id, k=3, chroma_client=make_vector_test_client(),
        )
        assert result == 0

    def test_compute_split_returns_child_specs(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """compute_split returns serializable child specs (no T2 writes)."""
        import numpy as _np
        from nexus.db.local_ef import LocalEmbeddingFunction
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        texts_a = [f"machine learning neural {i}" for i in range(15)]
        texts_b = [f"database sql query {i}" for i in range(15)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(30)]
        embeddings = _np.array(ef(texts), dtype=_np.float32)

        result = CatalogTaxonomy.compute_split(
            topic_id=99,
            doc_ids=doc_ids,
            texts=texts,
            fetched_ids=doc_ids,
            embeddings=embeddings,
            collection_name="test__compute_split",
            k=2,
        )

        assert result["topic_id"] == 99
        assert result["collection_name"] == "test__compute_split"
        child_specs = result["child_specs"]
        assert len(child_specs) == 2
        for spec in child_specs:
            assert "label" in spec
            assert "terms_json" in spec
            assert "doc_count" in spec
            assert "doc_ids" in spec
            assert "centroid" in spec
            assert spec["doc_count"] > 0
        # Total docs across children == 30 (all docs assigned)
        total = sum(s["doc_count"] for s in child_specs)
        assert total == 30

    def test_persist_split_writes_children(
        self, db: T2Database,
    ) -> None:
        """persist_split writes children to T2 and returns child IDs (no chroma)."""
        import json as _json
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        # Seed parent with assignments
        parent_id = _seed_topic(
            db.taxonomy, "parent", collection="test__persist_split", doc_count=4,
        )
        for i in range(4):
            _seed_assignment(db.taxonomy, f"doc-{i}", parent_id)

        split_result = {
            "topic_id": parent_id,
            "collection_name": "test__persist_split",
            "child_specs": [
                {
                    "label": "child-a",
                    "terms_json": _json.dumps(["alpha", "beta"]),
                    "doc_count": 2,
                    "doc_ids": ["doc-0", "doc-1"],
                    "centroid": [0.1] * 10,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "label": "child-b",
                    "terms_json": _json.dumps(["gamma", "delta"]),
                    "doc_count": 2,
                    "doc_ids": ["doc-2", "doc-3"],
                    "centroid": [0.9] * 10,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
        }

        child_ids = db.taxonomy.persist_split(split_result)
        assert len(child_ids) == 2
        # Parent assignments gone
        assert db.taxonomy.get_all_topic_doc_ids(parent_id) == []
        # Each child has its docs
        for cid in child_ids:
            assert len(db.taxonomy.get_all_topic_doc_ids(cid)) == 2
        # Parent doc_count is 0
        parent = db.taxonomy.get_topic_by_id(parent_id)
        assert parent["doc_count"] == 0

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: raw-SQLite split_cmd t2_index_write routing (service branch persists via the engine, not t2_index_write) dies at the RDR-155 P4b flip",
    )
    def test_split_cmd_routes_persist_via_t2_index_write(
        self, db: T2Database, chroma: chromadb.ClientAPI, monkeypatch,
    ) -> None:
        """split_cmd (Phase C) must route the T2 persist through t2_index_write.

        RDR-151 Phase 3 (nexus-uzay8): the taxonomy CLI 'split' command
        calls compute_split (local, chroma-coupled) then routes persist_split
        through t2_index_write (daemon path).  This test is a RED test until
        the routing is in place.
        """
        import nexus.mcp_infra as _mi
        from nexus.commands.taxonomy_cmd import taxonomy as taxonomy_grp
        from nexus.db.local_ef import LocalEmbeddingFunction
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        routed = {"calls": 0}

        def _spy_t2_index_write(fn):
            routed["calls"] += 1
            return fn(db)

        monkeypatch.setattr(_mi, "t2_index_write", _spy_t2_index_write)

        db_path = db._path
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        texts_a = [f"machine learning {i}" for i in range(15)]
        texts_b = [f"database query {i}" for i in range(15)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(30)]

        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES ('to-split', 'test__split_route', 30, '2026-01-01T00:00:00Z')",
        )
        parent_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        for did in doc_ids:
            db.taxonomy.conn.execute(
                "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                (did, parent_id),
            )
        db.taxonomy.conn.commit()

        # Seed T3 collection
        embs = ef(texts)
        coll = chroma.get_or_create_collection("test__split_route", embedding_function=None)
        coll.add(ids=doc_ids, documents=texts, embeddings=embs)

        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ), patch("nexus.db.make_t3") as mock_t3:
            # Raw-path exception (see test_split_cli): db.taxonomy here is
            # real raw SQLite, so t3 must stay raw-backed or
            # _require_supported_taxonomy_backend refuses.
            mock_t3.return_value = MagicMock(spec=T3Database)
            mock_t3.return_value._client = chroma
            result = CliRunner().invoke(
                taxonomy_grp,
                ["split", "to-split", "--k", "2", "--collection", "test__split_route"],
            )

        assert result.exit_code == 0, result.output
        assert routed["calls"] >= 1, (
            "split_cmd must call t2_index_write at least once (persist_split routed)"
        )


class TestManualOpsCLI:
    """CLI tests for nx taxonomy assign/merge/split/rename commands."""

    @staticmethod
    def _t2_router(db_path: Path):
        """Route t2_index_write through db_path for CLI tests."""
        def _router(fn):
            with T2Database(db_path) as db:
                return fn(db)
        return _router

    def test_assign_cli(self, tmp_path: Path) -> None:
        """nx taxonomy assign sets assigned_by='manual'."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            topic_id = _seed_topic(
                db.taxonomy, "target-topic", collection="proj", doc_count=5,
            )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy,
                ["assign", "my-doc-id", "target-topic", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            # The assignment landed on the topic; 'manual' provenance is
            # only readable on the raw twin (assigned_by='manual' rows are
            # exactly the manual_assignments slice of the rebuild state).
            assert db.taxonomy.get_assignments_for_docs(["my-doc-id"]) == {
                "my-doc-id": topic_id,
            }
            if has_raw_access(db.taxonomy):
                row = db.taxonomy.conn.execute(
                    "SELECT assigned_by FROM topic_assignments "
                    "WHERE doc_id = 'my-doc-id'"
                ).fetchone()
                assert row[0] == "manual"
            else:
                state = db.taxonomy.read_rebuild_old_state("proj")
                assert state["manual_assignments"].get("my-doc-id") == topic_id

    def test_assign_cli_unknown_label(self, tmp_path: Path) -> None:
        """nx taxonomy assign with unknown label prints error."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                taxonomy, ["assign", "doc-x", "nonexistent"],
            )

        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_rename_cli(self, tmp_path: Path) -> None:
        """nx taxonomy rename updates the label."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            topic_id = _seed_topic(
                db.taxonomy, "old-name", collection="proj", doc_count=5,
            )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy,
                ["rename", "old-name", "new-name", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            assert db.taxonomy.get_topic_by_id(topic_id)["label"] == "new-name"

    def test_rename_cli_no_accept_preserves_pending_status(
        self, tmp_path: Path,
    ) -> None:
        """``rename --no-accept`` updates the label without transitioning
        review_status (code-review finding M-1).

        Default behaviour (which still transitions to 'accepted') is
        already exercised by ``test_rename_cli``; this test pins the
        explicit opt-out path for users fixing a typo on a still-pending
        topic.
        """
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            topic_id = _seed_topic(
                db.taxonomy, "old-label", collection="proj", doc_count=5,
                review_status="pending",
            )

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy,
                ["rename", "old-label", "fixed-typo", "-c", "proj", "--no-accept"],
            )

        assert result.exit_code == 0, result.output
        assert "review_status preserved" in result.output
        with T2Database(db_path) as db:
            topic = db.taxonomy.get_topic_by_id(topic_id)
        assert (topic["label"], topic["review_status"]) == ("fixed-typo", "pending")

    def test_merge_cli(self, tmp_path: Path) -> None:
        """nx taxonomy merge moves docs and deletes source."""
        import nexus.mcp_infra as _mi
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            source_id = _seed_topic(
                db.taxonomy, "source", collection="proj", doc_count=2,
            )
            target_id = _seed_topic(
                db.taxonomy, "target", collection="proj", doc_count=1,
            )
            _seed_assignment(db.taxonomy, "doc-a", source_id)
            _seed_assignment(db.taxonomy, "doc-b", source_id)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch.object(_mi, "t2_index_write", self._t2_router(db_path)),
        ):
            result = runner.invoke(
                taxonomy,
                ["merge", "source", "target", "--collection", "proj"],
            )

        assert result.exit_code == 0, result.output
        with T2Database(db_path) as db:
            # Source deleted
            assert db.taxonomy.get_topic_by_id(source_id) is None
            assert db.taxonomy.resolve_label("source", collection="proj") is None
            # Target has the docs
            docs = db.taxonomy.get_all_topic_doc_ids(target_id)
            assert set(docs) == {"doc-a", "doc-b"}

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: raw-path split CLI (CatalogTaxonomy.compute_split patch + t2_index_write routing; service branch bypasses both) dies at the RDR-155 P4b flip",
    )
    def test_split_cli(self, tmp_path: Path) -> None:
        """nx taxonomy split invokes compute_split+persist_split via t2_index_write."""
        import nexus.mcp_infra as _mi
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("big-topic", "test__split_cli", 30, "2026-01-01T00:00:00Z"),
            )
            parent_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for i in range(30):
                db.taxonomy.conn.execute(
                    "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (f"doc-{i}", parent_id),
                )
            db.taxonomy.conn.commit()

        # Stub compute_split to return 2 child specs (no numpy/KMeans needed)
        fake_split_result = {
            "topic_id": parent_id,
            "collection_name": "test__split_cli",
            "child_specs": [
                {
                    "label": "child-0",
                    "terms_json": "[]",
                    "doc_count": 15,
                    "doc_ids": [f"doc-{i}" for i in range(15)],
                    "centroid": [0.1] * 384,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "label": "child-1",
                    "terms_json": "[]",
                    "doc_count": 15,
                    "doc_ids": [f"doc-{i}" for i in range(15, 30)],
                    "centroid": [0.9] * 384,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
        }

        # nx taxonomy split's raw path (taxonomy_cmd.py:1081-1083,
        # `chroma_client = t3._client`) only runs when db.taxonomy has
        # raw access (_has_raw_access) -- true here since this test uses
        # a real T2Database/SQLite taxonomy store. spec=T3Database (not
        # HttpVectorClient) is deliberate: is_service_backed(t3) must be
        # False or _require_supported_taxonomy_backend raises "not
        # supported" against the real raw-SQLite taxonomy.
        mock_t3 = MagicMock(spec=T3Database)
        mock_coll = MagicMock()
        mock_coll.get.return_value = {
            "ids": [f"doc-{i}" for i in range(30)],
            "documents": [f"text {i}" for i in range(30)],
        }
        # _client is an instance-only attribute of T3Database (assigned in
        # __init__), so it is invisible to dir(T3Database) and thus to
        # MagicMock's spec introspection. Set it explicitly (a plain
        # attribute set, unrestricted even under spec=) before chaining
        # into it, so the subsequent GET resolves against the mock's own
        # __dict__ rather than the spec-restricted __getattr__. spec= the
        # child too (chromadb.api.ClientAPI) so a method that real Chroma
        # clients don't have can't hide behind a bare MagicMock.
        mock_t3._client = MagicMock(spec=chromadb.api.ClientAPI)
        mock_t3._client.get_collection.return_value = mock_coll
        mock_t3._client.get_or_create_collection.return_value = MagicMock()

        # persist_split returns list of 2 new child IDs
        fake_persist = MagicMock(return_value=[101, 102])
        import numpy as _np

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch("nexus.db.make_t3", return_value=mock_t3),
            patch(
                "nexus.db.local_ef.LocalEmbeddingFunction.__call__",
                return_value=_np.zeros((30, 384), dtype=_np.float32).tolist(),
            ),
            patch(
                "nexus.db.t2.catalog_taxonomy.CatalogTaxonomy.compute_split",
                return_value=fake_split_result,
            ),
            patch.object(_mi, "t2_index_write", lambda fn: fake_persist(fn)),
        ):
            result = runner.invoke(
                taxonomy,
                ["split", "big-topic", "--k", "2", "--collection", "test__split_cli"],
            )

        assert result.exit_code == 0, result.output
        assert "2 sub-topics" in result.output
        # GH #250: split echoes a next-step hint pointing at `label`.
        assert "Action:" in result.output
        assert "nx taxonomy label" in result.output
        assert "-c test__split_cli" in result.output

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: raw-path split CLI (CatalogTaxonomy.compute_split patch + t2_index_write routing; service branch bypasses both) dies at the RDR-155 P4b flip",
    )
    def test_split_cli_action_hint_without_collection_flag(self, tmp_path: Path) -> None:
        """GH #250: split without --collection still emits a scoped hint.

        The collection scope comes from the parent topic row, so the hint
        reads `nx taxonomy label -c <parent-collection>` even when the
        user did not pass --collection.
        """
        import nexus.mcp_infra as _mi
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("lonely-topic", "test__parent_scope", 10, "2026-01-01T00:00:00Z"),
            )
            parent_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for i in range(10):
                db.taxonomy.conn.execute(
                    "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (f"doc-{i}", parent_id),
                )
            db.taxonomy.conn.commit()

        fake_split_result = {
            "topic_id": parent_id,
            "collection_name": "test__parent_scope",
            "child_specs": [
                {
                    "label": "c0",
                    "terms_json": "[]",
                    "doc_count": 5,
                    "doc_ids": [f"doc-{i}" for i in range(5)],
                    "centroid": [0.1] * 384,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "label": "c1",
                    "terms_json": "[]",
                    "doc_count": 5,
                    "doc_ids": [f"doc-{i}" for i in range(5, 10)],
                    "centroid": [0.9] * 384,
                    "created_at": "2026-01-01T00:00:00Z",
                },
                {
                    "label": "c2",
                    "terms_json": "[]",
                    "doc_count": 0,
                    "doc_ids": [],
                    "centroid": [0.5] * 384,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            ],
        }
        # Same raw-path rationale as test_split_cli above: this test's
        # T2Database taxonomy store is real raw SQLite, so t3 must stay
        # raw-backed (spec=T3Database) or _require_supported_taxonomy_backend
        # raises against is_service_backed(t3)=True + _has_raw_access=True.
        mock_t3 = MagicMock(spec=T3Database)
        mock_coll = MagicMock()
        mock_coll.get.return_value = {
            "ids": [f"doc-{i}" for i in range(10)],
            "documents": [f"text {i}" for i in range(10)],
        }
        mock_t3._client = MagicMock(spec=chromadb.api.ClientAPI)
        mock_t3._client.get_collection.return_value = mock_coll
        mock_t3._client.get_or_create_collection.return_value = MagicMock()
        fake_persist = MagicMock(return_value=[201, 202, 203])
        import numpy as _np

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch("nexus.db.make_t3", return_value=mock_t3),
            patch(
                "nexus.db.local_ef.LocalEmbeddingFunction.__call__",
                return_value=_np.zeros((10, 384), dtype=_np.float32).tolist(),
            ),
            patch(
                "nexus.db.t2.catalog_taxonomy.CatalogTaxonomy.compute_split",
                return_value=fake_split_result,
            ),
            patch.object(_mi, "t2_index_write", lambda fn: fake_persist(fn)),
        ):
            result = runner.invoke(taxonomy, ["split", "lonely-topic", "--k", "3"])

        assert result.exit_code == 0, result.output
        assert "Action:" in result.output
        assert "-c test__parent_scope" in result.output

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: raw-path split CLI (CatalogTaxonomy.compute_split patch + t2_index_write routing; service branch bypasses both) dies at the RDR-155 P4b flip",
    )
    def test_split_cli_no_hint_when_child_count_zero(self, tmp_path: Path) -> None:
        """GH #250: no-op split (child_count=0) must NOT print the action hint."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("noop-topic", "test__noop", 5, "2026-01-01T00:00:00Z"),
            )
            parent_id = db.taxonomy.conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            for i in range(5):
                db.taxonomy.conn.execute(
                    "INSERT INTO topic_assignments (doc_id, topic_id) VALUES (?, ?)",
                    (f"doc-{i}", parent_id),
                )
            db.taxonomy.conn.commit()

        # compute_split returns no child specs -> early return, no Action hint
        fake_split_result = {
            "topic_id": parent_id,
            "collection_name": "test__noop",
            "child_specs": [],
        }
        # Same raw-path rationale as test_split_cli above.
        mock_t3 = MagicMock(spec=T3Database)
        mock_coll = MagicMock()
        mock_coll.get.return_value = {
            "ids": [f"doc-{i}" for i in range(5)],
            "documents": [f"text {i}" for i in range(5)],
        }
        mock_t3._client = MagicMock(spec=chromadb.api.ClientAPI)
        mock_t3._client.get_collection.return_value = mock_coll
        import numpy as _np

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path),
            patch("nexus.db.make_t3", return_value=mock_t3),
            patch(
                "nexus.db.local_ef.LocalEmbeddingFunction.__call__",
                return_value=_np.zeros((5, 384), dtype=_np.float32).tolist(),
            ),
            patch(
                "nexus.db.t2.catalog_taxonomy.CatalogTaxonomy.compute_split",
                return_value=fake_split_result,
            ),
        ):
            result = runner.invoke(taxonomy, ["split", "noop-topic"])

        assert result.exit_code == 0, result.output
        assert "Action:" not in result.output


# ── Rebalance trigger + merge strategy (RDR-070, nexus-1im) ───────────────


class TestRebalanceTrigger:
    """needs_rebalance detects 2x corpus growth."""

    def test_no_prior_discover_needs_rebalance(self, db: T2Database) -> None:
        """First discover always proceeds (no prior count)."""
        assert db.taxonomy.needs_rebalance("test__coll", current_count=100) is True

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: CatalogTaxonomy's 2x rebalance threshold semantics (engine twin uses 5% growth) dies at the RDR-155 P4b flip",
    )
    def test_below_threshold_no_rebalance(self, db: T2Database) -> None:
        """Under 2x growth does not trigger rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=150) is False

    def test_at_2x_triggers_rebalance(self, db: T2Database) -> None:
        """At 2x growth triggers rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=200) is True

    def test_above_2x_triggers_rebalance(self, db: T2Database) -> None:
        """Above 2x growth triggers rebalance."""
        db.taxonomy.record_discover_count("test__coll", 100)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=300) is True

    def test_record_updates_count(self, db: T2Database) -> None:
        """record_discover_count updates the stored count.

        Substrate-neutral assertions: a zero-growth check is False on both
        twins (SQLite's 2x threshold, the engine's 5% growth threshold),
        while an unrecorded collection is True on both — so the False
        answers below prove each record landed.
        """
        db.taxonomy.record_discover_count("test__coll", 50)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=50) is False
        db.taxonomy.record_discover_count("test__coll", 80)
        assert db.taxonomy.needs_rebalance("test__coll", current_count=80) is False
        assert db.taxonomy.needs_rebalance("test__coll", current_count=160) is True


class TestMergeStrategy:
    """_merge_labels transfers operator labels by centroid similarity."""

    def test_high_similarity_transfers_label(self, db: T2Database) -> None:
        """Old label transferred when cosine similarity > 0.8."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        # Near-identical centroids (high cosine similarity)
        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["operator-approved-label"]
        old_review_statuses = ["accepted"]
        new_centroids = np.array([[0.99, 0.1, 0.0]], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 1
        assert merged[0]["label"] == "operator-approved-label"
        assert merged[0]["review_status"] == "accepted"

    def test_low_similarity_uses_new_label(self, db: T2Database) -> None:
        """New c-TF-IDF label used when cosine similarity <= 0.8."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["old-label"]
        old_review_statuses = ["accepted"]
        # Orthogonal -> cosine similarity ~0
        new_centroids = np.array([[0.0, 1.0, 0.0]], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 1
        assert merged[0]["label"] is None  # caller uses c-TF-IDF
        assert merged[0]["review_status"] == "pending"

    def test_n1_dedup_highest_wins(self, db: T2Database) -> None:
        """When two new centroids match the same old centroid, highest similarity wins."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        old_centroids = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        old_labels = ["shared-label"]
        old_review_statuses = ["accepted"]
        # Two new centroids, both close to old but one closer
        new_centroids = np.array([
            [0.95, 0.3, 0.0],  # similarity ~0.95
            [0.85, 0.5, 0.0],  # similarity ~0.86
        ], dtype=np.float32)

        merged = CatalogTaxonomy._merge_labels(
            old_centroids, old_labels, old_review_statuses, new_centroids,
        )
        assert len(merged) == 2
        # Only the higher-similarity centroid gets the label
        labels = [m["label"] for m in merged]
        assert labels.count("shared-label") == 1
        assert labels.count(None) == 1

    def test_no_old_centroids(self, db: T2Database) -> None:
        """With no old centroids, all new centroids get None labels."""
        from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

        new_centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        merged = CatalogTaxonomy._merge_labels(
            np.empty((0, 2), dtype=np.float32), [], [], new_centroids,
        )
        assert len(merged) == 2
        assert all(m["label"] is None for m in merged)


class TestManualPreservation:
    """Manual assignments preserved across re-discovery."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return make_vector_test_client()

    def test_manual_assignments_survive_rebuild(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """Rebuild with merge strategy preserves manual assignments."""
        from nexus.db.local_ef import LocalEmbeddingFunction

        # Initial discovery
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        texts_a = [f"machine learning gradient descent {i}" for i in range(30)]
        texts_b = [f"database query sql index {i}" for i in range(30)]
        texts = texts_a + texts_b
        doc_ids = [f"doc-{i}" for i in range(60)]
        embeddings = np.array(ef(texts), dtype=np.float32)

        count = db.taxonomy.discover_topics(
            "test__preserve", doc_ids, embeddings, texts, chroma,
        )
        assert count >= 2

        # Manually assign a doc and rename a topic
        topics = db.taxonomy.get_topics()
        topic = topics[0]
        db.taxonomy.assign_topic("manual-doc", topic["id"], assigned_by="manual")
        db.taxonomy.rename_topic(topic["id"], "operator-approved")

        # Rebuild (with merge strategy)
        new_count = db.taxonomy.rebuild_taxonomy(
            "test__preserve", doc_ids, embeddings, texts, chroma,
        )
        assert new_count >= 2

        # Check that operator label survived via merge strategy
        new_topics = db.taxonomy.get_topics()
        new_labels = [t["label"] for t in new_topics]
        # The operator-approved label should be transferred to the
        # nearest matching centroid (same data -> high similarity)
        assert "operator-approved" in new_labels

        # Manual assignment should be preserved — read through the public
        # rebuild-state surface (manual_assignments is the assigned_by='manual'
        # row set joined to live topics).
        manual = _centroid_state(db.taxonomy, "test__preserve", chroma)[
            "manual_assignments"
        ]
        assert "manual-doc" in manual


class TestRediscoveryCentroidLifecycle:
    """Centroid lifecycle: clear before re-upsert on --force."""

    @pytest.fixture()
    def chroma(self) -> chromadb.ClientAPI:
        return make_vector_test_client()

    def test_force_clears_old_centroids(
        self, db: T2Database, chroma: chromadb.ClientAPI,
    ) -> None:
        """rebuild_taxonomy clears old centroids before upserting new."""
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning neural {i}" for i in range(30)]
            + [f"database query sql {i}" for i in range(30)]
        )

        # First discovery
        db.taxonomy.discover_topics(
            "test__lifecycle", doc_ids, embeddings, texts, chroma,
        )
        first = _centroid_state(db.taxonomy, "test__lifecycle", chroma)
        assert len(first["old_centroid_ids"]) >= 2

        # Rebuild (clears old, creates new)
        db.taxonomy.rebuild_taxonomy(
            "test__lifecycle", doc_ids, embeddings, texts, chroma,
        )
        second = _centroid_state(db.taxonomy, "test__lifecycle", chroma)
        assert len(second["old_centroid_ids"]) >= 2

        # Old centroids should be replaced (not accumulated): count matches
        # the number of current topics, not 2x.
        current_topics = [
            t for t in db.taxonomy.get_topics()
            if t.get("collection") == "test__lifecycle"
        ]
        assert len(second["old_centroid_ids"]) == len(current_topics)


# ── Topic-aware links (RDR-070, nexus-40f) ────────────────────────────────


class TestComputeTopicLinks:
    """compute_topic_links derives inter-topic relationships from catalog links."""

    def test_basic_topic_link(self, db: T2Database) -> None:
        """Two docs in different topics, linked in catalog, produce a topic link."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        # Set up two topics with docs
        t1_id = _seed_topic(db.taxonomy, "networking", collection="code__proj", doc_count=2)
        t2_id = _seed_topic(db.taxonomy, "database", collection="code__proj", doc_count=2)
        _seed_assignment(db.taxonomy, "src/net/server.py", t1_id)
        _seed_assignment(db.taxonomy, "src/net/client.py", t1_id)
        _seed_assignment(db.taxonomy, "src/db/store.py", t2_id)
        _seed_assignment(db.taxonomy, "src/db/query.py", t2_id)

        # Mock catalog with one link between docs in different topics
        mock_catalog = MagicMock()
        mock_entry_a = MagicMock(
            file_path="src/net/server.py",
            physical_collection="code__proj",
        )
        mock_entry_a.tumbler = MagicMock()
        mock_entry_a.tumbler.__str__ = lambda s: "1.1"
        mock_entry_b = MagicMock(
            file_path="src/db/store.py",
            physical_collection="code__proj",
        )
        mock_entry_b.tumbler = MagicMock()
        mock_entry_b.tumbler.__str__ = lambda s: "1.2"

        mock_link = MagicMock(
            link_type="cites",
        )
        mock_link.from_tumbler = mock_entry_a.tumbler
        mock_link.to_tumbler = mock_entry_b.tumbler

        mock_catalog.link_query.return_value = [mock_link]
        mock_catalog.resolve.side_effect = lambda t: (
            mock_entry_a if str(t) == "1.1" else mock_entry_b
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert len(result) >= 1
        pair = result[0]
        assert {"networking", "database"} == {pair["from_topic"], pair["to_topic"]}
        assert pair["link_count"] == 1
        assert "cites" in pair["link_types"]

    def test_no_links_returns_empty(self, db: T2Database) -> None:
        """No catalog links → empty result."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        mock_catalog = MagicMock()
        mock_catalog.link_query.return_value = []

        assert compute_topic_links(db.taxonomy, mock_catalog) == []

    def test_same_topic_link_excluded(self, db: T2Database) -> None:
        """Links between docs in the same topic are excluded."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        tid = _seed_topic(db.taxonomy, "single-topic", collection="code__proj", doc_count=2)
        _seed_assignment(db.taxonomy, "src/a.py", tid)
        _seed_assignment(db.taxonomy, "src/b.py", tid)

        mock_catalog = MagicMock()
        entry_a = MagicMock(file_path="src/a.py", physical_collection="code__proj")
        entry_a.tumbler.__str__ = lambda s: "1.1"
        entry_b = MagicMock(file_path="src/b.py", physical_collection="code__proj")
        entry_b.tumbler.__str__ = lambda s: "1.2"
        link = MagicMock(link_type="relates")
        link.from_tumbler = entry_a.tumbler
        link.to_tumbler = entry_b.tumbler
        mock_catalog.link_query.return_value = [link]
        mock_catalog.resolve.side_effect = lambda t: (
            entry_a if str(t) == "1.1" else entry_b
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert result == []

    def test_multiple_link_types_aggregated(self, db: T2Database) -> None:
        """Multiple links between same topic pair aggregate counts and types."""
        from unittest.mock import MagicMock

        from nexus.commands.taxonomy_cmd import compute_topic_links

        t1 = _seed_topic(db.taxonomy, "api", collection="code__proj", doc_count=1)
        t2 = _seed_topic(db.taxonomy, "model", collection="code__proj", doc_count=1)
        _seed_assignment(db.taxonomy, "src/api.py", t1)
        _seed_assignment(db.taxonomy, "src/model.py", t2)

        mock_catalog = MagicMock()
        ea = MagicMock(file_path="src/api.py", physical_collection="code__proj")
        ea.tumbler.__str__ = lambda s: "1.1"
        eb = MagicMock(file_path="src/model.py", physical_collection="code__proj")
        eb.tumbler.__str__ = lambda s: "1.2"

        link1 = MagicMock(link_type="cites")
        link1.from_tumbler = ea.tumbler
        link1.to_tumbler = eb.tumbler
        link2 = MagicMock(link_type="implements")
        link2.from_tumbler = ea.tumbler
        link2.to_tumbler = eb.tumbler

        mock_catalog.link_query.return_value = [link1, link2]
        mock_catalog.resolve.side_effect = lambda t: (
            ea if str(t) == "1.1" else eb
        )

        result = compute_topic_links(db.taxonomy, mock_catalog)
        assert len(result) == 1
        assert result[0]["link_count"] == 2
        assert set(result[0]["link_types"]) == {"cites", "implements"}


class TestCooccurrenceLinks:
    """Tests for generate_cooccurrence_links (RDR-075 SC-5)."""

    def test_cross_collection_cooccurrence(self, db: T2Database) -> None:
        """Docs assigned to topics in different collections generate links."""
        # Create topics in two collections
        t1 = _seed_topic(db.taxonomy, "neural-nets", collection="coll_A", doc_count=5)
        t2 = _seed_topic(db.taxonomy, "databases", collection="coll_B", doc_count=5)

        # Assign one doc to topics in both collections
        _seed_assignment(db.taxonomy, "doc-shared", t1, assigned_by="centroid")
        _seed_assignment(db.taxonomy, "doc-shared", t2, assigned_by="projection")

        count = db.taxonomy.generate_cooccurrence_links()
        assert count == 1

        pairs = _link_pairs(db.taxonomy, [t1, t2])
        assert len(pairs) == 1
        assert next(iter(pairs.values())) == 1  # link_count

    def test_same_collection_no_links(self, db: T2Database) -> None:
        """Docs assigned to topics in the SAME collection don't generate links."""
        t1 = _seed_topic(db.taxonomy, "topic-x", collection="same_coll", doc_count=5)
        t2 = _seed_topic(db.taxonomy, "topic-y", collection="same_coll", doc_count=5)
        _seed_assignment(db.taxonomy, "doc-same", t1)
        _seed_assignment(db.taxonomy, "doc-same", t2)

        count = db.taxonomy.generate_cooccurrence_links()
        assert count == 0


class TestTopicLinksCLI:
    """CLI tests for nx taxonomy links."""

    def test_links_no_catalog(self, tmp_path: Path) -> None:
        """Links command gracefully handles missing catalog."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path):
            pass

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(taxonomy, ["links"])

        assert result.exit_code == 0
        assert "no topic links" in result.output.lower() or "catalog" in result.output.lower()

    def test_links_with_data(self, tmp_path: Path) -> None:
        """Links command shows topic relationships from topic_links table."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            api_id = _seed_topic(db.taxonomy, "api", collection="code__test", doc_count=5)
            db_id = _seed_topic(db.taxonomy, "database", collection="code__test", doc_count=5)
            db.taxonomy.upsert_topic_links([
                {"from_topic_id": api_id, "to_topic_id": db_id,
                 "link_count": 5, "link_types": ["cites", "implements"]},
            ])

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path",
            return_value=db_path,
        ):
            result = runner.invoke(taxonomy, ["links"])

        assert result.exit_code == 0
        assert "api" in result.output
        assert "database" in result.output


# ── Coverage gap tests (deep review, nexus-kwx) ──────────────────────────────


class TestQueryMethodCoverage:
    """Direct unit tests for query methods on CatalogTaxonomy."""

    def test_get_doc_ids_for_topic(self, db: T2Database) -> None:
        """get_doc_ids_for_topic resolves label -> doc_ids via JOIN."""
        tid = _seed_topic(db.taxonomy, "search-methods", collection="proj", doc_count=3)
        for did in ("doc-x", "doc-y", "doc-z"):
            _seed_assignment(db.taxonomy, did, tid)

        result = db.taxonomy.get_doc_ids_for_topic("search-methods")
        assert set(result) == {"doc-x", "doc-y", "doc-z"}

    def test_get_doc_ids_for_topic_unknown_label(self, db: T2Database) -> None:
        """get_doc_ids_for_topic returns empty list for unknown label."""
        assert db.taxonomy.get_doc_ids_for_topic("nonexistent") == []

    def test_get_assignments_for_docs(self, db: T2Database) -> None:
        """get_assignments_for_docs returns {doc_id: topic_id} mapping."""
        tid = _seed_topic(db.taxonomy, "topic-a", collection="proj", doc_count=2)
        _seed_assignment(db.taxonomy, "doc-1", tid)
        _seed_assignment(db.taxonomy, "doc-2", tid)

        result = db.taxonomy.get_assignments_for_docs(["doc-1", "doc-2", "doc-3"])
        assert result == {"doc-1": tid, "doc-2": tid}

    def test_get_assignments_for_docs_empty(self, db: T2Database) -> None:
        """get_assignments_for_docs with empty list returns empty dict."""
        assert db.taxonomy.get_assignments_for_docs([]) == {}

    def test_get_labels_for_ids(self, db: T2Database) -> None:
        """get_labels_for_ids returns scoped {id: label} map."""
        all_ids = [
            _seed_topic(db.taxonomy, label, collection="proj", doc_count=1)
            for label in ("alpha", "beta", "gamma")
        ]

        result = db.taxonomy.get_labels_for_ids(all_ids[:2])
        assert len(result) == 2
        assert set(result.values()) <= {"alpha", "beta", "gamma"}

    def test_get_labels_for_ids_empty(self, db: T2Database) -> None:
        """get_labels_for_ids with empty list returns empty dict."""
        assert db.taxonomy.get_labels_for_ids([]) == {}

    def test_get_all_topic_doc_ids(self, db: T2Database) -> None:
        """get_all_topic_doc_ids returns all assigned doc_ids without limit."""
        tid = _seed_topic(db.taxonomy, "big-topic", collection="proj", doc_count=10)
        for i in range(10):
            _seed_assignment(db.taxonomy, f"doc-{i}", tid)

        result = db.taxonomy.get_all_topic_doc_ids(tid)
        assert len(result) == 10


class TestEdgeCases:
    """Edge cases identified in deep review."""

    def test_discover_topics_below_minimum(self, db: T2Database) -> None:
        """discover_topics with n < 5 returns 0 without crashing."""
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((3, 384)).astype(np.float32)
        doc_ids = ["doc-0", "doc-1", "doc-2"]
        texts = ["hello world", "foo bar", "baz qux"]
        chroma = make_vector_test_client()

        result = db.taxonomy.discover_topics(
            "tiny__coll", doc_ids, embeddings, texts, chroma,
        )
        assert result == 0

    def test_rebuild_with_few_docs_clears_and_records(
        self, db: T2Database,
    ) -> None:
        """Rebuild with n < 5 clears old topics and records the count."""
        chroma = make_vector_test_client()

        # Seed existing topics for the collection
        _seed_topic(db.taxonomy, "old-topic", collection="shrunk__coll", doc_count=50)

        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((3, 384)).astype(np.float32)

        result = db.taxonomy.rebuild_taxonomy(
            "shrunk__coll",
            ["doc-0", "doc-1", "doc-2"],
            embeddings,
            ["a", "b", "c"],
            chroma,
        )
        assert result == 0

        # Old topics should be cleared
        assert db.taxonomy.get_topics_for_collection("shrunk__coll") == []

        # Doc count should be recorded: a zero-growth check is False on both
        # twins, while an unrecorded collection would be True on both.
        assert db.taxonomy.needs_rebalance("shrunk__coll", current_count=3) is False

    def test_split_topic_collection_not_found(self, db: T2Database) -> None:
        """split_topic returns 0 when T3 collection doesn't exist."""
        tid = _seed_topic(
            db.taxonomy, "orphan", collection="nonexistent__coll", doc_count=5,
        )
        for i in range(5):
            _seed_assignment(db.taxonomy, f"doc-{i}", tid)

        chroma = make_vector_test_client()
        result = db.taxonomy.split_topic(tid, k=2, chroma_client=chroma)
        assert result == 0

    def test_discover_skip_existing_topics(self, db: T2Database) -> None:
        """discover_topics skips if topics already exist for collection."""
        chroma = make_vector_test_client()
        rng = np.random.default_rng(42)
        embeddings = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        embeddings[:30, 0] += 3.0
        embeddings[30:, 1] += 3.0
        doc_ids = [f"doc-{i}" for i in range(60)]
        texts = (
            [f"machine learning {i}" for i in range(30)]
            + [f"database query {i}" for i in range(30)]
        )

        # First discover succeeds
        count1 = db.taxonomy.discover_topics(
            "dup__coll", doc_ids, embeddings, texts, chroma,
        )
        assert count1 >= 2

        # Second discover skips (returns 0, doesn't duplicate)
        count2 = db.taxonomy.discover_topics(
            "dup__coll", doc_ids, embeddings, texts, chroma,
        )
        assert count2 == 0

        # Still only the original topics
        topics = [
            t for t in db.taxonomy.get_topics()
            if t.get("collection") == "dup__coll"
        ]
        assert len(topics) == count1

    def test_show_cmd(self, tmp_path: Path) -> None:
        """nx taxonomy show <id> displays documents."""
        from unittest.mock import patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        db_path = tmp_path / "memory.db"
        with T2Database(db_path) as db:
            tid = _seed_topic(db.taxonomy, "test-topic", collection="proj", doc_count=2)
            _seed_assignment(db.taxonomy, "doc-a", tid)
            _seed_assignment(db.taxonomy, "doc-b", tid)

        runner = CliRunner()
        with patch(
            "nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path,
        ):
            result = runner.invoke(taxonomy, ["show", str(tid)])

        assert result.exit_code == 0, result.output
        assert "doc-a" in result.output
        assert "doc-b" in result.output


class TestTopicLinksTable:
    """topic_links T2 table for search-time linked-topic boost."""

    @pytest.mark.skipif(
        os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine",
        reason="dies-roster: raw sqlite_master schema introspection dies at the RDR-155 P4b flip",
    )
    def test_topic_links_table_created(self, db: T2Database) -> None:
        """topic_links table exists after init."""
        tables = {
            r[0]
            for r in db.taxonomy.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "topic_links" in tables

    def test_upsert_and_read_topic_links(self, db: T2Database) -> None:
        """upsert_topic_links persists, get_topic_link_pairs reads."""
        # Create topics
        ids = [
            _seed_topic(db.taxonomy, label, collection="proj", doc_count=n)
            for label, n in (("api", 5), ("db", 3), ("test", 2))
        ]

        links = [
            {
                "from_topic_id": ids[0],
                "to_topic_id": ids[1],
                "link_count": 5,
                "link_types": ["cites", "implements"],
            },
            {
                "from_topic_id": ids[1],
                "to_topic_id": ids[2],
                "link_count": 2,
                "link_types": ["relates"],
            },
        ]
        count = db.taxonomy.upsert_topic_links(links)
        assert count == 2

        result = _link_pairs(db.taxonomy, ids)
        assert (ids[0], ids[1]) in result
        assert result[(ids[0], ids[1])] == 5
        assert (ids[1], ids[2]) in result

    def test_get_topic_link_pairs_empty(self, db: T2Database) -> None:
        """Empty topic_links returns an empty mapping (sqlite {} / Http [])."""
        assert _link_pairs(db.taxonomy, [1, 2, 3]) == {}

    def test_get_topic_link_pairs_scoped(self, db: T2Database) -> None:
        """Only returns links where both endpoints are in the requested set."""
        ids = [
            _seed_topic(db.taxonomy, label, collection="proj", doc_count=1)
            for label in ("a", "b", "c")
        ]

        db.taxonomy.upsert_topic_links([
            {"from_topic_id": ids[0], "to_topic_id": ids[1], "link_count": 3, "link_types": ["cites"]},
            {"from_topic_id": ids[1], "to_topic_id": ids[2], "link_count": 1, "link_types": ["relates"]},
        ])

        # Only request ids[0] and ids[1] — should not include the (1,2) link
        result = _link_pairs(db.taxonomy, [ids[0], ids[1]])
        assert (ids[0], ids[1]) in result
        assert (ids[1], ids[2]) not in result


class TestGetTopicsForCollection:
    """get_topics_for_collection returns all topics (root + children)."""

    def test_includes_children(self, db: T2Database) -> None:
        """Returns both root and child topics for a collection."""
        parent_id = _seed_topic(db.taxonomy, "parent", collection="proj", doc_count=10)
        _seed_topic(
            db.taxonomy, "child", collection="proj", doc_count=5,
            parent_id=parent_id,
        )

        result = db.taxonomy.get_topics_for_collection("proj")
        assert len(result) == 2
        labels = {t["label"] for t in result}
        assert labels == {"parent", "child"}

    def test_exclude_id(self, db: T2Database) -> None:
        """exclude_id filters out the specified topic."""
        _seed_topic(db.taxonomy, "keep-a", collection="proj", doc_count=3)
        _seed_topic(db.taxonomy, "keep-b", collection="proj", doc_count=2)
        exclude = _seed_topic(db.taxonomy, "exclude-me", collection="proj", doc_count=1)

        result = db.taxonomy.get_topics_for_collection("proj", exclude_id=exclude)
        labels = {t["label"] for t in result}
        assert "exclude-me" not in labels
        assert len(result) == 2


# ── project CLI tests (RDR-075 SC-2, SC-3, SC-9) ───────────────────────────


class TestProjectCmd:
    """Tests for nx taxonomy project CLI command."""

    def test_project_cmd_output(
        self, db: T2Database, chroma_client: chromadb.ClientAPI, tmp_path: Path,
    ) -> None:
        """project command shows matched topics and novel chunks."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        rng = np.random.default_rng(42)

        # Create target topics
        tgt_embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        tgt_embs[:30, 0] += 3.0
        tgt_embs[30:, 1] += 3.0
        db.taxonomy.discover_topics(
            "tgt__coll",
            [f"t-{i}" for i in range(60)],
            tgt_embs,
            [f"text {i}" for i in range(60)],
            chroma_client,
        )

        # Create source collection
        src_embs = rng.standard_normal((10, 384)).astype(np.float32) * 0.1
        src_embs[:5, 0] += 3.0
        src_coll = chroma_client.get_or_create_collection(
            "src__coll", embedding_function=None, metadata={"hnsw:space": "cosine"},
        )
        src_coll.upsert(ids=[f"s-{i}" for i in range(10)], embeddings=src_embs.tolist())

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.taxonomy_cmd._T2Database", return_value=db),
            patch("nexus.db.make_t3") as mock_t3,
        ):
            # Raw-path exception (see test_split_cli): db.taxonomy here is
            # real raw SQLite, so t3 must stay raw-backed or
            # _require_supported_taxonomy_backend refuses.
            mock_t3.return_value = MagicMock(spec=T3Database)
            mock_t3.return_value._client = chroma_client
            result = runner.invoke(taxonomy, [
                "project", "src__coll", "--against", "tgt__coll", "--threshold", "0.5",
            ])

        assert result.exit_code == 0
        assert "matched topics" in result.output.lower() or "novel chunks" in result.output.lower()

    def test_project_cmd_persist(
        self, db: T2Database, chroma_client: chromadb.ClientAPI, tmp_path: Path,
    ) -> None:
        """--persist writes assignments with assigned_by='projection'."""
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        rng = np.random.default_rng(42)

        tgt_embs = rng.standard_normal((60, 384)).astype(np.float32) * 0.1
        tgt_embs[:30, 0] += 3.0
        tgt_embs[30:, 1] += 3.0
        db.taxonomy.discover_topics(
            "ptgt__coll",
            [f"t-{i}" for i in range(60)],
            tgt_embs,
            [f"text {i}" for i in range(60)],
            chroma_client,
        )

        src_embs = rng.standard_normal((10, 384)).astype(np.float32) * 0.1
        src_embs[:5, 0] += 3.0
        src_coll = chroma_client.get_or_create_collection(
            "psrc__coll", embedding_function=None, metadata={"hnsw:space": "cosine"},
        )
        src_coll.upsert(ids=[f"ps-{i}" for i in range(10)], embeddings=src_embs.tolist())

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.taxonomy_cmd._T2Database", return_value=db),
            patch("nexus.db.make_t3") as mock_t3,
        ):
            # Raw-path exception (see test_split_cli): db.taxonomy here is
            # real raw SQLite, so t3 must stay raw-backed or
            # _require_supported_taxonomy_backend refuses.
            mock_t3.return_value = MagicMock(spec=T3Database)
            mock_t3.return_value._client = chroma_client
            result = runner.invoke(taxonomy, [
                "project", "psrc__coll", "--against", "ptgt__coll",
                "--threshold", "0.5", "--persist",
            ])

        assert result.exit_code == 0
        assert "persisted" in result.output.lower()

    def test_project_single_source_default_matches_backfill_target_set(
        self, db: T2Database, chroma_client: chromadb.ClientAPI, tmp_path: Path,
    ) -> None:
        """project <src> default targets every collection with topics minus src.

        GitHub #238 + bead nexus-gwhy. Before the fix, single-source
        project preferred same-hash siblings (via list_sibling_collections)
        and fell back to all-collections only when siblings was empty.
        For multi-repo families (e.g. three docs__<repo>-<hash> from
        distinct projects), the heuristic silently narrowed the target
        set. After the fix, the default matches backfill: all
        collections with topics minus the source. Explicit --against
        still overrides.
        """
        from unittest.mock import MagicMock, patch

        from click.testing import CliRunner

        from nexus.commands.taxonomy_cmd import taxonomy

        # Seed four collections with topics by direct insertion. The
        # CLI's get_distinct_collections() groups by topics.collection,
        # so one INSERT per collection is enough.
        src = "docs__alpha-11112222"
        others = [
            "docs__beta-33334444",
            "docs__gamma-55556666",
            # Same-hash sibling that the OLD heuristic would have
            # preferred exclusively (non-docs prefix, same hash8).
            # New behaviour includes it alongside the other docs__
            # collections, not instead of them.
            "code__alpha-11112222",
        ]
        for coll in [src, *others]:
            _seed_topic(db.taxonomy, f"t-{coll}", collection=coll, doc_count=3)

        # T3 still has to resolve the source collection for the
        # project_against fetch; create it empty and short-circuit via
        # --threshold so no real similarity math runs.
        chroma_client.get_or_create_collection(src, embedding_function=None)

        runner = CliRunner()
        with (
            patch("nexus.commands.taxonomy_cmd._default_db_path",
                  return_value=tmp_path / "db.sqlite"),
            patch("nexus.commands.taxonomy_cmd._T2Database", return_value=db),
            patch("nexus.db.make_t3") as mock_t3,
        ):
            # Raw-path exception (see test_split_cli): db.taxonomy here is
            # real raw SQLite, so t3 must stay raw-backed or
            # _require_supported_taxonomy_backend refuses.
            mock_t3.return_value = MagicMock(spec=T3Database)
            mock_t3.return_value._client = chroma_client
            result = runner.invoke(taxonomy, ["project", src, "--threshold", "0.5"])

        # The command may fail downstream (empty source has no chunks)
        # but the progress line must show all three other collections
        # as the resolved target set. That is what the unification fix
        # is responsible for; the later numerical result is not.
        assert "against 3 collection(s)" in result.output, result.output


class TestListSiblingCollections:
    """Tests for list_sibling_collections (RDR-075 SC-8)."""

    def test_finds_siblings_by_hash8(self, chroma_client: chromadb.ClientAPI) -> None:
        from nexus.registry import list_sibling_collections

        # Create collections with shared hash suffix
        chroma_client.get_or_create_collection("code__myrepo-abc12345")
        chroma_client.get_or_create_collection("docs__myrepo-abc12345")
        chroma_client.get_or_create_collection("rdr__myrepo-abc12345")
        chroma_client.get_or_create_collection("code__other-def67890")

        siblings = list_sibling_collections("code__myrepo-abc12345", chroma_client)
        assert "docs__myrepo-abc12345" in siblings
        assert "rdr__myrepo-abc12345" in siblings
        assert "code__myrepo-abc12345" not in siblings  # excludes self
        assert "code__other-def67890" not in siblings  # different hash

    def test_excludes_taxonomy_collections(self, chroma_client: chromadb.ClientAPI) -> None:
        from nexus.registry import list_sibling_collections

        chroma_client.get_or_create_collection("code__repo-aaa11111")
        chroma_client.get_or_create_collection("taxonomy__centroids-aaa11111")

        siblings = list_sibling_collections("code__repo-aaa11111", chroma_client)
        assert not any(s.startswith("taxonomy__") for s in siblings)

    def test_no_hash_suffix_returns_empty(self, chroma_client: chromadb.ClientAPI) -> None:
        from nexus.registry import list_sibling_collections

        siblings = list_sibling_collections("knowledge__art", chroma_client)
        assert siblings == []
