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
from pathlib import Path

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


def test_cluster_caps_giant_cluster() -> None:
    """nexus-9b9oi regression pin: HDBSCAN's eom selection root-grabs on real
    embedding corpora (knowledge__delos 2026-08-14: one cluster held 94% of
    2101 chunks), which flattens topic grouping and starves retrieval
    diversity. The fixture is a 400-vector subsample of that exact corpus
    (seed-42 choice, float16 — the roundtrip preserves the failure): without
    the max_cluster_size cap it clusters as [359, 23], with the cap it yields
    16 fine clusters. Synthetic blobs do NOT reproduce this (verified: they
    either separate cleanly or go all-noise), so the pin must ride real
    geometry. See tests/fixtures/PROVENANCE-hdbscan-rootgrab.md."""
    fixture = (
        Path(__file__).parent.parent
        / "fixtures"
        / "hdbscan_rootgrab_400x1024_f16.npz"
    )
    embeddings = np.load(fixture)["emb"].astype(np.float32)
    n = len(embeddings)
    assert n == 400
    labels, _ = tc._cluster(embeddings, n, "c__rootgrab")
    cap = tc._max_cluster_size(n)
    sizes = [int((labels == cid).sum()) for cid in set(labels) if cid >= 0]
    assert len(sizes) >= 4, f"expected fine-grained clusters, got {len(sizes)}"
    assert all(s <= cap for s in sizes), (
        f"cluster sizes {sorted(sizes, reverse=True)[:3]} exceed cap {cap}"
    )


def test_cluster_cap_leaves_healthy_blobs_alone() -> None:
    """The cap must not degrade well-separated fine-grained clusters: each
    blob is far below the 25% ceiling, so discovery output is unchanged."""
    rng = np.random.default_rng(13)
    blobs = []
    for axis in range(8):
        blob = rng.standard_normal((40, 64)).astype(np.float32) * 0.05
        blob[:, axis] += 3.0
        blobs.append(blob)
    embeddings = np.vstack(blobs)
    n = len(embeddings)
    labels, _ = tc._cluster(embeddings, n, "c__blobs")
    real = {int(x) for x in labels if x >= 0}
    assert len(real) >= 6, f"expected >= 6 of 8 blobs recovered, got {len(real)}"
    cap = tc._max_cluster_size(n)
    assert all(int((labels == cid).sum()) <= cap for cid in real)


def test_max_cluster_size_floor() -> None:
    """The floor (10x min_cluster_size) rules below n=200; the 25% fraction
    rules above. A higher floor is NOT safe: 100 no-ops across n=100-200
    (nexus-9h7nz)."""
    assert tc.MAX_CLUSTER_SIZE_FLOOR == 50
    assert tc._max_cluster_size(20) == 50
    assert tc._max_cluster_size(60) == 50
    assert tc._max_cluster_size(150) == 50
    assert tc._max_cluster_size(2000) == 500


def test_cluster_caps_midwindow_collection() -> None:
    """nexus-9h7nz regression pin: the first cut of the nexus-9b9oi fix used
    floor=100, which is a no-op for n in [100, 200) whenever the grabbed
    cluster is under 100 docs. This 150-vector subsample of the real-corpus
    fixture (seed-3 choice) root-grabs 98-of-150 uncapped; the floor must
    bind there."""
    fixture = (
        Path(__file__).parent.parent
        / "fixtures"
        / "hdbscan_rootgrab_400x1024_f16.npz"
    )
    emb = np.load(fixture)["emb"].astype(np.float32)
    idx = np.random.default_rng(3).choice(400, 150, replace=False)
    sub = emb[idx]
    labels, _ = tc._cluster(sub, len(sub), "c__midwindow")
    cap = tc._max_cluster_size(len(sub))
    assert cap == tc.MAX_CLUSTER_SIZE_FLOOR, "floor must rule at n=150"
    sizes = [int((labels == cid).sum()) for cid in set(labels) if cid >= 0]
    assert len(sizes) >= 4, f"expected fine-grained clusters, got {len(sizes)}"
    assert all(s <= cap for s in sizes), (
        f"cluster sizes {sorted(sizes, reverse=True)[:3]} exceed cap {cap}"
    )


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


# test_catalog_taxonomy_reexports_same_objects stood here. It asserted that
# reaching the compute core THROUGH catalog_taxonomy resolved to the very
# objects in taxonomy_compute — a behaviour-preservation guard for the
# RDR-158 P1 move. The re-exporting module is deleted (nexus-i711w Stage 2
# sub-stage C), so there is no second path left to agree with the first.
# Every surviving caller imports from taxonomy_compute directly.


class TestDedupSpecsByLabel:
    """nexus-slcn7: same-label discovered specs are merged at the source."""

    def test_merges_same_label_unions_docs(self) -> None:
        specs = [
            {"label": "a b c", "terms": "[]", "doc_count": 2,
             "doc_ids": ["d1", "d2"], "centroid": [0.1], "assigned_by": "hdbscan"},
            {"label": "x y z", "terms": "[]", "doc_count": 1,
             "doc_ids": ["d3"], "centroid": [0.2], "assigned_by": "hdbscan"},
            {"label": "a b c", "terms": "[]", "doc_count": 2,
             "doc_ids": ["d2", "d4"], "centroid": [0.9], "assigned_by": "hdbscan"},
        ]
        out, index_map = tc._dedup_specs_by_label(specs)
        # Two unique labels, original order preserved.
        assert [s["label"] for s in out] == ["a b c", "x y z"]
        merged = out[0]
        # Union of doc_ids (d2 not double-counted); doc_count recomputed.
        assert merged["doc_ids"] == ["d1", "d2", "d4"]
        assert merged["doc_count"] == 3
        # First cluster's centroid/terms kept.
        assert merged["centroid"] == [0.1]
        # index_map: original idx 0 and 2 collapse to new idx 0; idx 1 → 1.
        assert index_map == {0: 0, 1: 1, 2: 0}

    def test_no_duplicates_is_identity(self) -> None:
        specs = [
            {"label": "p", "terms": "[]", "doc_count": 1, "doc_ids": ["d1"],
             "centroid": [0.0], "assigned_by": "hdbscan"},
            {"label": "q", "terms": "[]", "doc_count": 1, "doc_ids": ["d2"],
             "centroid": [0.0], "assigned_by": "hdbscan"},
        ]
        out, index_map = tc._dedup_specs_by_label(specs)
        assert [s["label"] for s in out] == ["p", "q"]
        assert out[0]["doc_ids"] == ["d1"]
        assert index_map == {0: 0, 1: 1}
