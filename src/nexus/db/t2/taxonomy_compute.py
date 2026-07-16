# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""taxonomy_compute — the backend-neutral, connection-free taxonomy compute core.

RDR-158 P1 (nexus-z3znb). Lifted out of :class:`nexus.db.t2.catalog_taxonomy.
CatalogTaxonomy` so the pure clustering / c-TF-IDF / label-merge logic survives
the SQLite backend's deletion (RDR-158 P4). Nothing here touches a SQLite
connection, ``self._lock``, or a chroma client: every function is a pure
transform over numpy arrays + Python lists, and every return value is JSON
round-trippable so it can cross the daemon RPC boundary.

The SQLite ``CatalogTaxonomy`` and the live ``HttpTaxonomyStore`` both reach
these symbols; ``CatalogTaxonomy`` re-exports them (binding the functions as
static methods and re-importing the NamedTuples / constants) so existing call
sites and the ``CatalogTaxonomy.X`` monkeypatch surface are unchanged.

HARD INVARIANT (RF-158-2 category c): the chroma-coupled statics
(``_create_centroid_collection``, ``_centroid_records_for``, ``_batched_upsert``,
``compute_assignments(chroma_client=...)``, ``compute_cross_links``) do NOT
belong here — they take a chroma client and stay in the store classes (re-homed
in P4). Do not fold them in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, NamedTuple

import numpy as np
import structlog

# RDR-070 (nexus-9k5): scikit-learn>=1.3 is a core dep. HDBSCAN for topic
# discovery with c-TF-IDF labels via CountVectorizer.
from sklearn.cluster import HDBSCAN as SklearnHDBSCAN
from sklearn.feature_extraction.text import CountVectorizer, TfidfTransformer

_log = structlog.get_logger()


# ── Constants ────────────────────────────────────────────────────────────────

# Cosine-similarity floor for cross-topic projection pairs (RDR-075).
PROJECTION_THRESHOLD = 0.85

# Above this doc count, _cluster switches HDBSCAN -> MiniBatchKMeans for O(n).
LARGE_COLLECTION_THRESHOLD = 5000

# RDR-077 Phase 5 PQ-3: default stopword tokens for generic-pattern detection.
# A hub's label that *contains* any of these (case-insensitive substring) is
# flagged. Ops can surface these in `--explain` so operators can accept or
# suppress. Extending this list is a future RDR (PQ-3 open).
DEFAULT_HUB_STOPWORDS: tuple[str, ...] = (
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


# ── Result shapes ────────────────────────────────────────────────────────────


class AssignResult(NamedTuple):
    """Return shape for :meth:`CatalogTaxonomy.assign_single`.

    Carries the nearest ``topic_id`` and the raw cosine ``similarity``
    (``1.0 - distance``). ICF weighting is applied at query time, not here.
    """

    topic_id: int
    similarity: float


class HubRow(NamedTuple):
    """One hub row emitted by :meth:`CatalogTaxonomy.detect_hubs`.

    RDR-077 Phase 5 (nexus-84v).
    """

    topic_id: int
    label: str
    collection: str
    distinct_source_collections: int
    total_chunks: int
    icf: float
    score: float
    matched_stopwords: tuple[str, ...]
    source_collections: tuple[str, ...]
    last_assigned_at: str | None
    # --warn-stale output (populated only when requested; None otherwise)
    max_last_discover_at: str | None
    never_discovered_count: int
    is_stale: bool


class AuditReport(NamedTuple):
    """Summary of projection quality for a single source collection.

    Returned by :meth:`CatalogTaxonomy.audit_collection`. RDR-077 Phase 6
    (nexus-w4k).
    """

    collection: str
    total_assignments: int  # projection rows with source_collection = this
    p10: float | None
    p50: float | None
    p90: float | None
    below_threshold_count: int
    threshold: float
    top_receiving_hubs: list["AuditHub"]
    pattern_pollution: list["AuditHub"]


class AuditHub(NamedTuple):
    """One receiving-topic row inside an :class:`AuditReport`."""

    topic_id: int
    label: str
    chunk_count: int
    icf: float
    matched_stopwords: tuple[str, ...]


# ── Clustering primitives ────────────────────────────────────────────────────


def _cluster(
    embeddings: np.ndarray,
    n: int,
    collection_name: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Cluster embeddings. Returns (labels, centroids).

    Uses HDBSCAN for small collections (density-based, automatic k).
    Switches to MiniBatchKMeans for large collections (O(n) speed).

    Pure (no instance state) so the daemon-routable COMPUTE half
    (:func:`compute_discovered_topics`, RDR-151 Phase 3) can call it
    without a ``CatalogTaxonomy`` instance.
    """
    if n <= LARGE_COLLECTION_THRESHOLD:
        _log.info("clustering_hdbscan", n=n, collection=collection_name)
        clusterer = SklearnHDBSCAN(
            min_cluster_size=5,
            store_centers="centroid",
            copy=True,
        )
        labels = clusterer.fit_predict(embeddings)
        # HDBSCAN centroids indexed by cluster label
        centroids = getattr(clusterer, "centroids_", np.empty((0, embeddings.shape[1])))
        return labels, centroids

    from sklearn.cluster import MiniBatchKMeans  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    k = max(10, int(n ** 0.5 / 3))
    _log.info(
        "clustering_minibatch_kmeans",
        n=n, k=k, collection=collection_name,
    )
    km = MiniBatchKMeans(
        n_clusters=k,
        batch_size=min(1000, n),
        n_init=3,
        random_state=42,
    )
    # macOS Accelerate emits spurious FP-state RuntimeWarnings (divide by
    # zero / overflow / invalid in matmul) from kmeans++ init even on clean
    # unit-norm float32 input — verified 2026-07-15: full norm census of
    # code__1-1 (28,164 x 1024) showed zero NaN/inf/zero-norm rows while the
    # warnings fired, and the resulting topics were valid. Suppress them
    # ONLY when the input is provably finite; genuinely bad input keeps the
    # warnings AND gets a loud structured event (fail-loud discipline).
    if np.isfinite(embeddings).all():
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            labels = km.fit_predict(embeddings)
        # Tripwire (critique): isfinite gates the INPUT only. Degenerate-but-
        # finite data (e.g. many identical points) could raise a genuine 0/0
        # that the suppression would otherwise hide — NaN centroids are the
        # observable symptom, so check the OUTPUT loudly.
        if np.isnan(km.cluster_centers_).any():
            _log.warning(
                "clustering_nan_centroids",
                n=n, k=k, collection=collection_name,
            )
    else:
        bad = int((~np.isfinite(embeddings)).any(axis=1).sum())
        _log.warning(
            "clustering_nonfinite_embeddings",
            n=n, nonfinite_rows=bad, collection=collection_name,
        )
        labels = km.fit_predict(embeddings)
    return labels, km.cluster_centers_


def _merge_labels(
    old_centroids: np.ndarray,
    old_labels: list[str],
    old_review_statuses: list[str],
    new_centroids: np.ndarray,
    *,
    threshold: float = 0.8,
) -> list[dict[str, Any]]:
    """Match new centroids to old centroids, transfer operator labels.

    Returns a list of dicts (one per new centroid) with:
    - ``label``: transferred label or None (caller uses c-TF-IDF)
    - ``review_status``: 'accepted' if matched, 'pending' if new

    N:1 dedup: each old centroid claimed at most once. If two new
    centroids match the same old centroid above threshold, the
    higher-similarity claimant wins.
    """
    from sklearn.metrics.pairwise import cosine_similarity  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    n_new = new_centroids.shape[0]
    result: list[dict[str, Any]] = [
        {"label": None, "review_status": "pending", "old_centroid_idx": -1}
        for _ in range(n_new)
    ]

    if old_centroids.shape[0] == 0:
        return result

    # Dimensionality mismatch guard (model upgrade scenario)
    if old_centroids.shape[1] != new_centroids.shape[1]:
        _log.warning(
            "centroid_dimension_mismatch",
            old_dim=old_centroids.shape[1],
            new_dim=new_centroids.shape[1],
        )
        return result

    # Cosine similarity matrix: (n_new, n_old)
    sims = cosine_similarity(new_centroids, old_centroids)

    # Greedy assignment: highest similarity first, each old used once
    claimed_old: set[int] = set()
    # Build (sim, new_idx, old_idx) sorted descending by sim
    candidates = []
    for new_idx in range(n_new):
        for old_idx in range(old_centroids.shape[0]):
            candidates.append((sims[new_idx, old_idx], new_idx, old_idx))
    candidates.sort(key=lambda x: x[0], reverse=True)

    claimed_new: set[int] = set()
    for sim, new_idx, old_idx in candidates:
        if sim < threshold:
            break  # No more above threshold
        if old_idx in claimed_old or new_idx in claimed_new:
            continue
        result[new_idx] = {
            "label": old_labels[old_idx],
            "review_status": old_review_statuses[old_idx],
            "old_centroid_idx": old_idx,
        }
        claimed_old.add(old_idx)
        claimed_new.add(new_idx)

    return result


# ── Compute halves (chroma-free, T2-write-free) ──────────────────────────────


def compute_split(
    topic_id: int,
    doc_ids: list[str],
    texts: list[str],
    fetched_ids: list[str],
    embeddings: np.ndarray,
    collection_name: str,
    k: int,
) -> dict[str, Any]:
    """Compute the KMeans split (pure CPU + numpy — no T2 writes).

    Returns a serializable dict with child specs and centroid records
    so the caller can route :meth:`persist_split` through the daemon
    and then do the local chroma centroid update.

    ``fetched_ids`` is the subset of ``doc_ids`` for which texts were
    actually retrieved (may differ from ``doc_ids`` when the T3 get
    returned partial results).

    Returns a dict with keys:
    - ``child_specs``: list of dicts ``{label, terms_json, doc_count,
      doc_ids, centroid}`` (one per KMeans cluster that is non-empty)
    - ``collection_name``: passed through for the caller
    - ``topic_id``: passed through for the caller
    """
    from sklearn.cluster import KMeans  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    km = KMeans(n_clusters=k, n_init=10, random_state=42)
    labels = km.fit_predict(embeddings)

    vectorizer = CountVectorizer(stop_words="english")
    tfidf_matrix = TfidfTransformer().fit_transform(
        vectorizer.fit_transform(texts),
    )
    feature_names = vectorizer.get_feature_names_out()

    child_specs: list[dict[str, Any]] = []
    for cid in range(k):
        mask = labels == cid
        if not mask.any():
            continue
        cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
        top_idx = cluster_tfidf.argsort()[-10:][::-1]
        top_terms = [str(feature_names[i]) for i in top_idx]
        label = " ".join(top_terms[:3])
        terms_json = json.dumps(top_terms)
        doc_count = int(mask.sum())
        child_doc_ids = [fetched_ids[i] for i in range(len(fetched_ids)) if mask[i]]
        child_centroid = embeddings[mask].mean(axis=0).tolist()
        child_specs.append({
            "label": label,
            "terms_json": terms_json,
            "doc_count": doc_count,
            "doc_ids": child_doc_ids,
            "centroid": child_centroid,
            "created_at": now,
        })

    return {
        "topic_id": topic_id,
        "collection_name": collection_name,
        "child_specs": child_specs,
    }


def compute_discovered_topics(
    collection_name: str,
    doc_ids: list[str],
    embeddings: np.ndarray,
    texts: list[str],
) -> list[dict[str, Any]]:
    """Cluster + c-TF-IDF — the chroma-free, T2-free COMPUTE half of
    :meth:`discover_topics` (RDR-151 Phase 3, nexus-uzay8).

    Returns a list of serializable topic-spec dicts, one per real
    (non-noise) cluster, in stable cluster order::

        {"label", "terms", "doc_count", "doc_ids", "centroid", "assigned_by"}

    ``terms`` is a JSON string (top-10 c-TF-IDF terms); ``centroid`` is a
    plain ``list[float]`` so the spec survives the daemon RPC. Empty list
    on any no-op condition (``< 5`` docs, all-noise clustering) — the same
    short-circuits the monolithic ``discover_topics`` returned 0 for.
    """
    n = len(doc_ids)
    if n < 5:
        return []

    labels, centroids = _cluster(embeddings, n, collection_name)
    real_labels = sorted(set(int(lbl) for lbl in labels if lbl >= 0))
    if not real_labels:
        _log.warning("cluster_all_noise", n_docs=n, collection=collection_name)
        return []

    vectorizer = CountVectorizer(stop_words="english")
    tfidf_matrix = TfidfTransformer().fit_transform(
        vectorizer.fit_transform(texts),
    )
    feature_names = vectorizer.get_feature_names_out()

    specs: list[dict[str, Any]] = []
    for cid in real_labels:
        mask = labels == cid
        cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
        top_idx = cluster_tfidf.argsort()[-10:][::-1]
        top_terms = [str(feature_names[i]) for i in top_idx]
        specs.append({
            "label": " ".join(top_terms[:3]),
            "terms": json.dumps(top_terms),
            "doc_count": int(mask.sum()),
            "doc_ids": [doc_ids[i] for i in range(n) if mask[i]],
            "centroid": [float(x) for x in centroids[cid].tolist()],
            "assigned_by": "hdbscan",
        })
    specs, _ = _dedup_specs_by_label(specs)
    return specs


def _dedup_specs_by_label(
    specs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    """Collapse discovered specs that share an identical label (nexus-slcn7).

    The c-TF-IDF top-3 labeler can assign the SAME label string to two distinct
    HDBSCAN clusters (their top-3 terms collide). Persisted as-is, each becomes a
    separate root topic with an identical ``(collection, label)``, which surfaces
    as duplicate Knowledge Map rows once the RDR-154 read-side dedup band-aid is
    removed. Merge same-label specs at the source instead: union their ``doc_ids``,
    recompute ``doc_count``, keep the first cluster's centroid/terms/review_status
    and (for the rebuild path) its ``assigned_by`` — order preserved. One root
    topic per label is then the invariant both T2 backends persist, and the
    partial unique index is its structural backstop.

    Returns ``(deduped_specs, index_map)`` where ``index_map`` maps each ORIGINAL
    spec index to its index in ``deduped_specs`` — callers that key data on spec
    position (the rebuild path's ``manual_transfers``) must remap through it.
    """
    label_to_new_idx: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    index_map: dict[int, int] = {}
    for old_idx, spec in enumerate(specs):
        label = spec["label"]
        new_idx = label_to_new_idx.get(label)
        if new_idx is None:
            new_idx = len(out)
            label_to_new_idx[label] = new_idx
            out.append({**spec, "doc_ids": list(spec["doc_ids"])})
        else:
            merged = out[new_idx]
            seen = set(merged["doc_ids"])
            for did in spec["doc_ids"]:
                if did not in seen:
                    merged["doc_ids"].append(did)
                    seen.add(did)
            merged["doc_count"] = len(merged["doc_ids"])
        index_map[old_idx] = new_idx
    return out, index_map


def compute_rebuild_plan(
    collection_name: str,
    doc_ids: list[str],
    embeddings: np.ndarray,
    texts: list[str],
    *,
    old_centroids: np.ndarray,
    old_labels: list[str],
    old_review_statuses: list[str],
    old_centroid_topic_ids: list[int],
    manual_assignments: dict[str, int],
) -> dict[str, Any]:
    """Cluster + merge-labels + manual-transfer resolution — the
    chroma-free, T2-write-free COMPUTE half of :meth:`rebuild_taxonomy`
    (RDR-151 Phase 3, nexus-uzay8).

    ``old_*`` state and ``manual_assignments`` are read by the caller
    (chroma for old centroids, T2 for old labels / manual rows) and passed
    in, so this stays pure. Returns a serializable plan::

        {"specs": [{label, terms, doc_count, doc_ids, centroid,
                    assigned_by, review_status}, ...],
         "manual_transfers": {doc_id: spec_index}}

    ``manual_transfers`` resolves Route 1 (old topic matched to a new one
    via :func:`_merge_labels`) and Route 2 (doc in the current corpus, cosine
    to a new centroid > 0.5) to a SPEC INDEX; the persist half maps that to
    the freshly-generated topic_id. Route 3 (unplaceable) is dropped with a
    warning. Empty ``specs`` on ``< 5`` docs / all-noise.
    """
    n = len(doc_ids)
    if n < 5:
        return {"specs": [], "manual_transfers": {}}

    labels, centroids_arr = _cluster(embeddings, n, collection_name)
    real_labels = sorted(set(int(lbl) for lbl in labels if lbl >= 0))
    if not real_labels:
        _log.warning("rebuild_all_noise", collection=collection_name, n_docs=n)
        return {"specs": [], "manual_transfers": {}}

    vectorizer = CountVectorizer(stop_words="english")
    tfidf_matrix = TfidfTransformer().fit_transform(
        vectorizer.fit_transform(texts),
    )
    feature_names = vectorizer.get_feature_names_out()

    new_centroids_arr = np.array(
        [centroids_arr[cid] for cid in real_labels], dtype=np.float32,
    )
    merged = _merge_labels(
        old_centroids, old_labels, old_review_statuses, new_centroids_arr,
    )

    specs: list[dict[str, Any]] = []
    for idx, cid in enumerate(real_labels):
        mask = labels == cid
        cluster_tfidf = tfidf_matrix[mask].mean(axis=0).A1
        top_idx = cluster_tfidf.argsort()[-10:][::-1]
        top_terms = [str(feature_names[i]) for i in top_idx]
        tfidf_label = " ".join(top_terms[:3])
        merged_info = merged[idx]
        specs.append({
            "label": merged_info["label"] or tfidf_label,
            "terms": json.dumps(top_terms),
            "doc_count": int(mask.sum()),
            "doc_ids": [doc_ids[i] for i in range(n) if mask[i]],
            "centroid": [float(x) for x in new_centroids_arr[idx].tolist()],
            "assigned_by": "auto-matched" if merged_info["label"] else "hdbscan",
            "review_status": merged_info["review_status"],
        })

    # Resolve manual-assignment transfers to spec indices.
    old_to_new_spec: dict[int, int] = {}
    if old_centroid_topic_ids:
        for new_idx, merge_info in enumerate(merged):
            old_idx = merge_info.get("old_centroid_idx", -1)
            if merge_info["label"] is not None and 0 <= old_idx < len(old_centroid_topic_ids):
                old_to_new_spec[old_centroid_topic_ids[old_idx]] = new_idx

    manual_transfers: dict[str, int] = {}
    if manual_assignments:
        doc_id_to_idx = {did: i for i, did in enumerate(doc_ids)}
        for manual_doc, old_topic_id in manual_assignments.items():
            # Route 1: old topic matched to a new topic.
            if old_topic_id in old_to_new_spec:
                manual_transfers[manual_doc] = old_to_new_spec[old_topic_id]
                continue
            # Route 2: doc in the current corpus — cosine to new centroids.
            if manual_doc in doc_id_to_idx:
                from sklearn.metrics.pairwise import cosine_similarity as _cos_sim  # noqa: PLC0415 — heavy/optional dependency deferred to call time
                j = doc_id_to_idx[manual_doc]
                sims = _cos_sim(embeddings[j : j + 1], new_centroids_arr)[0]
                best_idx = int(sims.argmax())
                if float(sims[best_idx]) > 0.5:
                    manual_transfers[manual_doc] = best_idx
                    continue
            # Route 3: unplaceable.
            _log.warning(
                "manual_assignment_lost",
                doc_id=manual_doc,
                old_topic_id=old_topic_id,
                collection=collection_name,
            )

    # nexus-slcn7: dedup same-label specs here too — persist_rebuild_topics
    # inserts root topics with a plain INSERT, so a label collision would hit the
    # partial unique index. Remap manual_transfers (keyed on spec position)
    # through the dedup index map so transfers still point at the right topic.
    specs, index_map = _dedup_specs_by_label(specs)
    manual_transfers = {
        doc: index_map[old_idx] for doc, old_idx in manual_transfers.items()
    }

    return {"specs": specs, "manual_transfers": manual_transfers}
