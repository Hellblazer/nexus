# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-158 P1 (nexus-z3znb): isolation tests for the extracted, connection-free
``nexus.db.t2.taxonomy_compute`` module.

The compute core was lifted out of ``CatalogTaxonomy`` so it survives the
SQLite-backend deletion (RDR-158 P4). These tests exercise the module *directly*
— no ``CatalogTaxonomy`` instance, no DB connection, no chroma — to prove the
extraction is both behaviour-preserving and genuinely standalone. Deterministic:
fixed seeds and ``random_state=42`` inside the compute functions.
"""

from __future__ import annotations

import importlib
import json

import numpy as np

from nexus.db.t2 import taxonomy_compute as tc


def _discovery_inputs(seed: int = 11) -> tuple[list[str], np.ndarray, list[str]]:
    """Two well-separated 384d clusters (mirrors tests/test_taxonomy.py)."""
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


# ── Connection-free invariant (the load-bearing reason the module exists) ────


def test_module_is_connection_free() -> None:
    """The module must not import sqlite3 or any DB-connection surface — it is
    the part of the taxonomy stack that outlives the SQLite backend (RDR-158)."""
    import inspect

    source = inspect.getsource(tc)
    import_lines = [
        ln.strip() for ln in source.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    # The whole nexus.db subtree is the connection-bearing surface — guard the
    # prefix so any future coupling path (catalog_taxonomy, memory_store,
    # service_endpoint, chroma_quotas, ...) trips this, not just a fixed list.
    # Docstring references are excluded (filtered to import lines above).
    assert not any("nexus.db" in ln for ln in import_lines)
    # Belt-and-suspenders for the stdlib / vendor connection surfaces that do
    # not live under nexus.db.
    assert not any("sqlite3" in ln for ln in import_lines)
    assert not any("chromadb" in ln for ln in import_lines)


def test_module_imports_standalone() -> None:
    """Importing the module in isolation must not pull in catalog_taxonomy."""
    mod = importlib.import_module("nexus.db.t2.taxonomy_compute")
    assert mod is tc


# ── Constants ────────────────────────────────────────────────────────────────


def test_projection_threshold_value() -> None:
    assert tc.PROJECTION_THRESHOLD == 0.85


def test_large_collection_threshold_value() -> None:
    assert tc.LARGE_COLLECTION_THRESHOLD == 5000


def test_default_hub_stopwords_exact() -> None:
    assert tc.DEFAULT_HUB_STOPWORDS == (
        "assert",
        "junit",
        "builder",
        "class",
        "import",
        "exception",
        "getter",
        "setter",
        "variable",
        "declaration",
        "operator",
    )


# ── NamedTuples ──────────────────────────────────────────────────────────────


def test_assign_result_shape() -> None:
    r = tc.AssignResult(topic_id=7, similarity=0.9)
    assert (r.topic_id, r.similarity) == (7, 0.9)
    assert r._fields == ("topic_id", "similarity")


def test_hub_row_fields() -> None:
    assert tc.HubRow._fields == (
        "topic_id",
        "label",
        "collection",
        "distinct_source_collections",
        "total_chunks",
        "icf",
        "score",
        "matched_stopwords",
        "source_collections",
        "last_assigned_at",
        "max_last_discover_at",
        "never_discovered_count",
        "is_stale",
    )


def test_audit_report_and_hub_fields() -> None:
    assert tc.AuditReport._fields == (
        "collection",
        "total_assignments",
        "p10",
        "p50",
        "p90",
        "below_threshold_count",
        "threshold",
        "top_receiving_hubs",
        "pattern_pollution",
    )
    assert tc.AuditHub._fields == (
        "topic_id",
        "label",
        "chunk_count",
        "icf",
        "matched_stopwords",
    )


# ── _cluster ─────────────────────────────────────────────────────────────────


def test_cluster_returns_labels_and_centroids() -> None:
    _, embeddings, _ = _discovery_inputs()
    labels, centroids = tc._cluster(embeddings, len(embeddings), "c__cluster")
    assert labels.shape == (60,)
    real = sorted({int(x) for x in labels if x >= 0})
    assert len(real) >= 2, "two separated blobs must yield >= 2 clusters"
    assert centroids.shape[1] == 384


# ── _merge_labels ────────────────────────────────────────────────────────────


def test_merge_labels_transfers_on_match() -> None:
    old_centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    new_centroids = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    merged = tc._merge_labels(
        old_centroids, ["alpha", "beta"], ["accepted", "accepted"], new_centroids,
    )
    assert merged[0]["label"] == "alpha"
    assert merged[1]["label"] == "beta"
    assert all(m["review_status"] == "accepted" for m in merged)


def test_merge_labels_no_old_centroids_all_pending() -> None:
    new_centroids = np.array([[1.0, 0.0]], dtype=np.float32)
    merged = tc._merge_labels(
        np.empty((0, 2), dtype=np.float32), [], [], new_centroids,
    )
    assert merged == [{"label": None, "review_status": "pending", "old_centroid_idx": -1}]


def test_merge_labels_dimension_mismatch_returns_pending() -> None:
    old = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)  # 3d
    new = np.array([[1.0, 0.0]], dtype=np.float32)  # 2d
    merged = tc._merge_labels(old, ["x"], ["accepted"], new)
    assert merged[0]["label"] is None


# ── compute_discovered_topics ────────────────────────────────────────────────


def test_compute_discovered_topics_serializable_specs() -> None:
    doc_ids, embeddings, texts = _discovery_inputs()
    specs = tc.compute_discovered_topics("d__disc", doc_ids, embeddings, texts)
    assert len(specs) >= 2
    json.dumps(specs)  # must round-trip across the daemon RPC
    for s in specs:
        assert set(s) == {
            "label", "terms", "doc_count", "doc_ids", "centroid", "assigned_by",
        }
        assert isinstance(s["label"], str) and s["label"]
        assert isinstance(s["doc_count"], int) and s["doc_count"] > 0
        assert len(s["doc_ids"]) == s["doc_count"]
        assert len(s["centroid"]) == 384
        assert all(isinstance(x, float) for x in s["centroid"])
        assert s["assigned_by"] == "hdbscan"


def test_compute_discovered_topics_short_circuits() -> None:
    out = tc.compute_discovered_topics(
        "tiny", ["a", "b"], np.zeros((2, 384), dtype=np.float32), ["x", "y"],
    )
    assert out == []


# ── compute_split ────────────────────────────────────────────────────────────


def test_compute_split_returns_child_specs() -> None:
    doc_ids, embeddings, texts = _discovery_inputs()
    result = tc.compute_split(
        topic_id=42,
        doc_ids=doc_ids,
        texts=texts,
        fetched_ids=doc_ids,
        embeddings=embeddings,
        collection_name="c__split",
        k=2,
    )
    assert result["topic_id"] == 42
    assert result["collection_name"] == "c__split"
    specs = result["child_specs"]
    assert len(specs) == 2
    json.dumps(specs)
    for s in specs:
        assert set(s) == {
            "label", "terms_json", "doc_count", "doc_ids", "centroid", "created_at",
        }
        assert s["doc_count"] == len(s["doc_ids"])
        assert len(s["centroid"]) == 384


def test_compute_split_is_deterministic() -> None:
    doc_ids, embeddings, texts = _discovery_inputs()
    kw = dict(
        topic_id=1, doc_ids=doc_ids, texts=texts, fetched_ids=doc_ids,
        embeddings=embeddings, collection_name="c__det", k=2,
    )
    a = tc.compute_split(**kw)
    b = tc.compute_split(**kw)
    assert [s["label"] for s in a["child_specs"]] == [s["label"] for s in b["child_specs"]]
    assert [s["doc_count"] for s in a["child_specs"]] == [s["doc_count"] for s in b["child_specs"]]


# ── compute_rebuild_plan ─────────────────────────────────────────────────────


def test_compute_rebuild_plan_pure_and_serializable() -> None:
    doc_ids, embeddings, texts = _discovery_inputs()
    plan = tc.compute_rebuild_plan(
        "c__rebuild",
        doc_ids,
        embeddings,
        texts,
        old_centroids=np.empty((0, 384), dtype=np.float32),
        old_labels=[],
        old_review_statuses=[],
        old_centroid_topic_ids=[],
        manual_assignments={},
    )
    assert set(plan) == {"specs", "manual_transfers"}
    json.dumps(plan)
    assert len(plan["specs"]) >= 2
    for s in plan["specs"]:
        assert set(s) == {
            "label", "terms", "doc_count", "doc_ids", "centroid",
            "assigned_by", "review_status",
        }
        assert s["review_status"] == "pending"  # no old centroids => all new


def test_compute_rebuild_plan_short_circuits() -> None:
    plan = tc.compute_rebuild_plan(
        "tiny",
        ["a", "b"],
        np.zeros((2, 384), dtype=np.float32),
        ["x", "y"],
        old_centroids=np.empty((0, 384), dtype=np.float32),
        old_labels=[],
        old_review_statuses=[],
        old_centroid_topic_ids=[],
        manual_assignments={},
    )
    assert plan == {"specs": [], "manual_transfers": {}}


# ── Re-export parity: CatalogTaxonomy still exposes the same objects ──────────


def test_catalog_taxonomy_reexports_same_objects() -> None:
    """Behaviour-preserving: existing call sites that reach the compute core via
    ``CatalogTaxonomy`` (and ``from catalog_taxonomy import AssignResult``) must
    resolve to the very objects now living in taxonomy_compute."""
    from nexus.db.t2 import catalog_taxonomy as ct
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

    assert ct.AssignResult is tc.AssignResult
    assert ct.HubRow is tc.HubRow
    assert ct.AuditReport is tc.AuditReport
    assert ct.AuditHub is tc.AuditHub
    assert ct.DEFAULT_HUB_STOPWORDS is tc.DEFAULT_HUB_STOPWORDS
    assert CatalogTaxonomy._PROJECTION_THRESHOLD == tc.PROJECTION_THRESHOLD
    assert CatalogTaxonomy._LARGE_COLLECTION_THRESHOLD == tc.LARGE_COLLECTION_THRESHOLD
    # The compute statics resolve through the class to the moved functions.
    assert CatalogTaxonomy.compute_split is tc.compute_split
    assert CatalogTaxonomy.compute_discovered_topics is tc.compute_discovered_topics
    assert CatalogTaxonomy.compute_rebuild_plan is tc.compute_rebuild_plan
    assert CatalogTaxonomy._merge_labels is tc._merge_labels
    assert CatalogTaxonomy._cluster is tc._cluster
