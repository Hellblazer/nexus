# SPDX-License-Identifier: AGPL-3.0-or-later
"""search_cross_corpus enrichment must not scale with an unbounded pool.

nexus-5ezgn / nexus-w032x. nx_answer's plan 473 searches
``knowledge,code,docs,rdr`` with ``threshold=2.0``. On the live cloud that
pooled 7,805 candidates from 80 collections for a 10-row answer, and the
contradiction check then fetched embeddings for every one of them, one
serial round trip per collection: 80 x 1.3 s = 104 s of a 146 s search
(profiled 2026-09-28), and 310 s in the shakeout's nx_answer run.

Earlier tests used a handful of rows in one or two collections, where
neither the pool size nor the serial loop costs anything. These build the
production shape: 80 collections, 100 rows each, no threshold.
"""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from nexus import call_deadline
from nexus.search_engine import SearchResult, _cap_enrichment_pool, search_cross_corpus

pytestmark = pytest.mark.usefixtures("named_collection_rows")  # RDR-229 P2.9: prefix-named fixtures get a catalog row

N_COLS = 80
ROWS_PER_COL = 100


class _WideT3:
    """80 collections, 100 rows each; records every embedding fetch."""

    def __init__(self) -> None:
        self.embedding_ids: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include"):
        col = collection_names[0]
        i = int(col.rsplit("c", 1)[1])
        return [
            {
                "id": f"{col}-{j}",
                "content": f"row {j}",
                # Interleaved so the 300 best rows span every collection.
                "distance": 0.3 + j * 0.01 + i * 0.0001,
            }
            for j in range(ROWS_PER_COL)
        ]

    def get_embeddings(self, collection_name, ids):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.embedding_ids.extend(ids)
        time.sleep(0.02)
        with self._lock:
            self.active -= 1
        return np.ones((len(ids), 4), dtype=np.float32)


def _cols() -> list[str]:
    return [f"knowledge__c{i}" for i in range(N_COLS)]


def test_embedding_fetch_is_bounded_by_the_page_cap_not_the_pool() -> None:
    t3 = _WideT3()
    results = search_cross_corpus("q", _cols(), n_results=10, t3=t3, sensitivity="include")

    assert len(results) == 300
    assert len(t3.embedding_ids) == 300, (
        f"fetched embeddings for {len(t3.embedding_ids)} rows of a "
        f"{N_COLS * ROWS_PER_COL}-row pool"
    )
    assert "knowledge__c0-0" in {r.id for r in results}, "the nearest row was cut"


def test_embedding_fetch_runs_collections_in_parallel() -> None:
    t3 = _WideT3()
    search_cross_corpus("q", _cols(), n_results=10, t3=t3, sensitivity="include")
    assert t3.max_active > 1, "one embedding round trip at a time"


def _row(rid: str, distance: float, **meta) -> SearchResult:
    return SearchResult(id=rid, content="", distance=distance,
                        collection="knowledge__x", metadata=dict(meta))


def test_cap_keeps_every_lexical_row_and_input_order() -> None:
    rows = [_row("far-lexical", 0.99), _row("a", 0.10), _row("b", 0.20), _row("c", 0.30)]
    kept = _cap_enrichment_pool(rows, {"far-lexical"}, cap=2)
    assert [r.id for r in kept] == ["far-lexical", "a", "b"]


def test_cap_ranks_rerank_scored_rows_ahead_of_unscored() -> None:
    rows = [
        _row("near-unscored", 0.05),
        _row("low-score", 0.40, rerank_score=0.1),
        _row("high-score", 0.50, rerank_score=0.9),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["low-score", "high-score"]


CODE = "code__1-1__voyage-code-3__v1"
DOCS = "docs__1-1__voyage-context-3__v1"


def _in(collection: str, rid: str, distance: float) -> SearchResult:
    return SearchResult(id=rid, content="", distance=distance, collection=collection, metadata={})


def test_cap_ranks_unscored_rows_by_calibrated_distance_across_embedding_models(cloud_mode) -> None:
    """GH #1667 follow-up C: voyage-code-3 distances run tighter than voyage-context-3 (0.45 vs 0.65
    calibration), so a raw-distance cut filled the pool with code and dropped the CHANGELOG row a
    docs search ranks first. On the calibrated scale 0.50 * 0.45/0.65 = 0.346 sits ahead of 0.38."""
    rows = [_in(CODE, "code-a", 0.38), _in(CODE, "code-b", 0.39), _in(DOCS, "changelog", 0.50)]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert "changelog" in [r.id for r in kept]
    assert [r.id for r in kept] == ["code-a", "changelog"], "kept rows stay in input order"


def test_cap_takes_the_model_of_resolver_the_search_built() -> None:
    """The cut must agree with the scoring on each collection's model. Names with no model token resolve
    to one install model by name alone (every factor 1.0, the raw cut); only the row-preferred resolver
    can say they differ, so this fails without *model_of* whatever the install posture."""
    code, prose = "code__legacy", "docs__legacy"
    rows = [_in(code, "code-a", 0.38), _in(code, "code-b", 0.39), _in(prose, "doc", 0.50)]
    # Local models, so the test stays in the default (local) posture the name derivation depends on:
    # bge-base-en-v15-768 calibrates at 1.37, an unlisted model at the default 0.818.
    models = {code: "bge-base-en-v15-768", prose: "unlisted-test-model"}
    assert "doc" not in [r.id for r in _cap_enrichment_pool(rows, set(), cap=2)]
    assert "doc" in [r.id for r in _cap_enrichment_pool(rows, set(), cap=2, model_of=models.get)]


def test_search_cross_corpus_hands_its_own_model_resolver_to_the_cut(cloud_mode, monkeypatch) -> None:
    """The production call site passes the search's row-preferred resolver, not None (review of 4a11ee38c)."""
    import nexus.search_engine as se  # noqa: PLC0415 — the module under test, patched by attribute

    def resolver(name: str) -> str | None:
        return "voyage-context-3"

    monkeypatch.setattr(se, "_collection_model_resolver", lambda t3, collections, rows=None: resolver)
    seen: list[object] = []
    real = se._cap_enrichment_pool

    def spy(results, lexical_ids, cap=None, *, model_of=None, **kwargs):
        seen.append(model_of)
        return real(results, lexical_ids, cap, model_of=model_of, **kwargs)

    monkeypatch.setattr(se, "_cap_enrichment_pool", spy)
    search_cross_corpus("q", _cols(), n_results=10, t3=_WideT3(), sensitivity="include")
    assert seen == [resolver]


def test_cap_within_one_model_is_still_by_raw_distance() -> None:
    rows = [_in(DOCS, "far", 0.60), _in(DOCS, "near", 0.40), _in(DOCS, "mid", 0.50)]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["near", "mid"]


def _scored(collection: str, rid: str, distance: float, score: float) -> SearchResult:
    return SearchResult(id=rid, content="", distance=distance, collection=collection,
                        metadata={"rerank_score": score})


def test_cap_ranks_both_families_by_calibrated_distance_when_one_family_is_unscored() -> None:
    """GH #1667 follow-up C: a degraded rerank scores the code rows and leaves the docs rows unscored.
    Score-first would fill the cut with code and drop the docs row before calibration runs; the render
    side keeps every row in calibrated order for this case (core.py partial_rerank), so the cut does too.
    On the calibrated scale 0.50 * 0.45/0.65 = 0.346 sits ahead of 0.39 whatever the scores say."""
    rows = [
        _scored(CODE, "code-a", 0.38, 0.9),
        _scored(CODE, "code-b", 0.39, 0.8),
        _in(DOCS, "doc", 0.50),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["code-a", "doc"]


def test_cap_one_family_unscored_ignores_scores_even_inside_the_scored_family() -> None:
    """The mixed-pool rule keys EVERY non-lexical row on calibrated distance: a low-scored near code row
    outranks a high-scored far code row."""
    rows = [
        _scored(CODE, "code-near-low-score", 0.38, 0.1),
        _scored(CODE, "code-far-high-score", 0.44, 0.9),
        _in(DOCS, "doc", 0.50),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["code-near-low-score", "doc"]


def test_cap_scores_win_when_every_family_in_a_mixed_pool_is_scored() -> None:
    """Fully scored mixed pool: the scores are the ranking, calibrated distance never enters."""
    rows = [
        _scored(CODE, "code-a", 0.38, 0.9),
        _scored(CODE, "code-b", 0.39, 0.8),
        _scored(DOCS, "doc", 0.50, 0.1),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["code-a", "code-b"]


def test_cap_partly_scored_single_model_pool_keeps_scored_rows_first() -> None:
    """Local mode scores only the first rerank_max_candidates rows, in one model: scored rows still
    sort ahead of every unscored row, however near."""
    rows = [
        _in(DOCS, "near-unscored", 0.30),
        _scored(DOCS, "far-scored", 0.60, 0.5),
        _scored(DOCS, "farther-scored", 0.70, 0.4),
    ]
    kept = _cap_enrichment_pool(rows, set(), cap=2)
    assert [r.id for r in kept] == ["far-scored", "farther-scored"]


def test_cap_lexical_rows_do_not_make_a_family_unscored() -> None:
    """A lexical row is exempt from the cut, so its family does not count toward the mixed-pool test:
    docs rows that are all lexical leave the non-lexical pool one family, and score-first."""
    rows = [
        _scored(CODE, "code-a", 0.38, 0.9),
        _scored(CODE, "code-b", 0.39, 0.8),
        _in(CODE, "code-c", 0.30),
        _in(DOCS, "doc-lexical", 0.90),
    ]
    kept = _cap_enrichment_pool(rows, {"doc-lexical"}, cap=2)
    assert [r.id for r in kept] == ["code-a", "code-b", "doc-lexical"]


def test_cap_ranks_every_row_by_distance_when_one_collection_of_a_scored_family_degraded(cloud_mode) -> None:
    """The family rule misses a collection that degraded inside a family that was otherwise scored: both
    collections are voyage-context-3 (one factor), so every family has a scored row. Core's render treats a
    degraded collection beside scored rows as partial_rerank and keeps every row in calibrated order; the cut
    must agree, or the scored collection fills it and the degraded one's rows are dropped first."""
    scored_col, degraded_col = "docs__1-1__voyage-context-3__v1", "knowledge__x__voyage-context-3__v1"
    rows = [
        _scored(scored_col, "scored-a", 0.60, 0.9),
        _scored(scored_col, "scored-b", 0.61, 0.8),
        _in(degraded_col, "degraded-a", 0.30),
        _in(degraded_col, "degraded-b", 0.31),
    ]
    assert [r.id for r in _cap_enrichment_pool(rows, set(), cap=2)] == ["scored-a", "scored-b"], (
        "no degrade named: score-first, as before"
    )
    kept = _cap_enrichment_pool(rows, set(), cap=2, degraded_collections=frozenset({degraded_col}))
    assert [r.id for r in kept] == ["degraded-a", "degraded-b"]


def test_cap_keeps_score_first_when_the_unscored_rows_are_local_modes_rows_beyond_the_rerank_cap(cloud_mode) -> None:
    """Local mode scores only the first rerank_max_candidates rows of a collection and flags no degrade: its
    unscored rows are not a degraded collection's, so score-first stands. A degrade named for a collection
    with no row in the pool changes nothing either."""
    col = "docs__1-1__voyage-context-3__v1"
    rows = [_in(col, "near-unscored", 0.30), _scored(col, "far-scored", 0.60, 0.5),
            _scored(col, "farther-scored", 0.70, 0.4)]
    assert [r.id for r in _cap_enrichment_pool(rows, set(), cap=2, degraded_collections=frozenset())] == [
        "far-scored", "farther-scored"]
    kept = _cap_enrichment_pool(rows, set(), cap=2, degraded_collections=frozenset({"knowledge__elsewhere"}))
    assert [r.id for r in kept] == ["far-scored", "farther-scored"]


def test_cap_ignores_a_degraded_collection_whose_only_rows_are_lexical(cloud_mode) -> None:
    """A lexical row is exempt from the cut, so its degraded collection does not flip the pool."""
    scored_col, degraded_col = "docs__1-1__voyage-context-3__v1", "knowledge__x__voyage-context-3__v1"
    rows = [
        _scored(scored_col, "scored-a", 0.60, 0.9),
        _scored(scored_col, "scored-b", 0.61, 0.8),
        _scored(scored_col, "scored-c", 0.62, 0.7),
        _in(degraded_col, "lexical", 0.30),
    ]
    kept = _cap_enrichment_pool(rows, {"lexical"}, cap=2, degraded_collections=frozenset({degraded_col}))
    assert [r.id for r in kept] == ["scored-a", "scored-b", "lexical"]


class _RerankT3:
    """A reranking backend whose group holding DEGRADED reports the engine's degraded flag."""

    supports_server_rerank = True

    def __init__(self, degraded: set[str], sensitive: set[str] = frozenset()) -> None:
        self.degraded, self.sensitive = degraded, sensitive

    def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include",
               rerank=False, rerank_meta_out=None, **kw):
        if rerank_meta_out is not None and self.degraded & set(collection_names):
            rerank_meta_out["degraded"] = True
        return [{"id": f"{c}-{j}", "content": "x", "distance": 0.3 + j * 0.01, "collection": c,
                 "sensitive": c in self.sensitive} for c in collection_names for j in range(3)]

    def get_embeddings(self, collection_name, ids):
        return np.ones((len(ids), 4), dtype=np.float32)


def _degraded_seen_by_the_cut(monkeypatch, t3: _RerankT3) -> list[frozenset]:
    import nexus.search_engine as se  # noqa: PLC0415 — the module under test, patched by attribute

    # One model per collection, so each is its own backend call and only one can report a degrade.
    monkeypatch.setattr(se, "_collection_model_resolver", lambda _t3, cols, rows=None: lambda name: f"model-{name}")
    seen: list[frozenset] = []
    real = se._cap_enrichment_pool

    def spy(results, lexical_ids, cap=None, **kwargs):
        seen.append(frozenset(kwargs.get("degraded_collections", ())))
        return real(results, lexical_ids, cap, **kwargs)

    monkeypatch.setattr(se, "_cap_enrichment_pool", spy)
    search_cross_corpus("q", ["knowledge__a", "knowledge__b"], n_results=5, t3=t3, rerank=True,
                        cluster_by=None, sensitivity="include")
    return seen


def test_search_cross_corpus_hands_the_degraded_collections_to_the_cut(monkeypatch) -> None:
    """The production call site names the collections whose rerank degraded, as core's partial_rerank does."""
    assert _degraded_seen_by_the_cut(monkeypatch, _RerankT3({"knowledge__b"})) == [frozenset({"knowledge__b"})]


def test_search_cross_corpus_names_no_degrade_when_none_happened(monkeypatch) -> None:
    assert _degraded_seen_by_the_cut(monkeypatch, _RerankT3(set())) == [frozenset()]


def test_search_cross_corpus_does_not_name_a_benign_held_degrade(monkeypatch) -> None:
    """The engine's "every rerankable row is sensitive" degrade is no failure and, as in core, no degrade."""
    t3 = _RerankT3({"knowledge__b"}, sensitive={"knowledge__b"})
    assert _degraded_seen_by_the_cut(monkeypatch, t3) == [frozenset()]


def test_cap_is_a_no_op_under_the_limit() -> None:
    rows = [_row("a", 0.1), _row("b", 0.2)]
    assert _cap_enrichment_pool(rows, set(), cap=2) is rows


def test_pool_cap_scales_with_the_requested_page() -> None:
    """A caller asking for more than MAX_QUERY_RESULTS (nx search -m 1000)
    must get a pool at least that large (critic finding on 8f31f90f9)."""
    t3 = _WideT3()
    results = search_cross_corpus("q", _cols(), n_results=100, t3=t3, sensitivity="include")
    assert len(results) == 400


def test_search_stops_at_a_stage_boundary_once_its_deadline_passes() -> None:
    """An abandoned search (nx_answer's budget cut) stops instead of making
    its remaining round trips: no embedding fetch runs after the deadline."""
    t3 = _WideT3()
    token = call_deadline.set_deadline(time.monotonic() - 1.0)
    try:
        with pytest.raises(call_deadline.DeadlineExceeded):
            search_cross_corpus("q", _cols(), n_results=10, t3=t3, sensitivity="include")
    finally:
        call_deadline.reset(token)
    assert t3.embedding_ids == []


# --- topic grouping orders on the calibrated scale (GH #1667 follow-up C) -----------------------------------------


def _taxonomy(labels: dict[int, str]):
    from unittest.mock import MagicMock  # noqa: PLC0415 — test-local

    taxonomy = MagicMock()
    taxonomy.get_labels_for_ids.return_value = labels
    return taxonomy


def test_topic_grouping_orders_groups_by_calibrated_distance() -> None:
    """The group holding the docs row at raw 0.50 (calibrated 0.346) leads the group holding the code row at
    raw 0.38; ordered by raw distance it came second."""
    from nexus.search_engine import _apply_topic_grouping  # noqa: PLC0415 — test-local

    rows = [_in(CODE, "code", 0.38), _in(DOCS, "doc", 0.50)]
    out = _apply_topic_grouping(rows, {"code": 1, "doc": 2}, _taxonomy({1: "c", 2: "d"}))
    assert [r.id for r in out] == ["doc", "code"]


def test_topic_grouping_orders_rows_inside_a_group_by_calibrated_distance() -> None:
    from nexus.search_engine import _apply_topic_grouping  # noqa: PLC0415 — test-local

    rows = [_in(CODE, "code", 0.40), _in(DOCS, "doc", 0.50)]
    out = _apply_topic_grouping(rows, {"code": 1, "doc": 1}, _taxonomy({1: "t"}))
    assert [r.id for r in out] == ["doc", "code"]
    assert [r.distance for r in out] == [0.50, 0.40], "the reported distances are not rescaled"


def test_topic_grouping_orders_unassigned_rows_by_calibrated_distance() -> None:
    from nexus.search_engine import _apply_topic_grouping  # noqa: PLC0415 — test-local

    rows = [_in(CODE, "code", 0.38), _in(DOCS, "doc", 0.50), _in(CODE, "assigned", 0.10)]
    out = _apply_topic_grouping(rows, {"assigned": 1}, _taxonomy({1: "t"}))
    assert [r.id for r in out] == ["assigned", "doc", "code"]


def test_topic_grouping_takes_the_model_of_resolver_the_search_built() -> None:
    """Names with no model token resolve to one install model by name alone (every factor 1.0, the raw
    order); only the row-preferred resolver can say they differ."""
    from nexus.search_engine import _apply_topic_grouping  # noqa: PLC0415 — test-local

    code, prose = "code__legacy", "docs__legacy"
    # Local models, so the test stays in the default (local) posture the name derivation depends on:
    # bge-base-en-v15-768 calibrates at 1.37, an unlisted model at the default 0.818.
    models = {code: "bge-base-en-v15-768", prose: "unlisted-test-model"}
    rows = [_in(code, "code", 0.38), _in(prose, "doc", 0.50)]
    assignments = {"code": 1, "doc": 2}
    labels = {1: "c", 2: "d"}
    assert [r.id for r in _apply_topic_grouping(rows, assignments, _taxonomy(labels))] == ["code", "doc"]
    out = _apply_topic_grouping(rows, assignments, _taxonomy(labels), model_of=models.get)
    assert [r.id for r in out] == ["doc", "code"]


def test_topic_grouping_within_one_model_is_still_by_raw_distance() -> None:
    from nexus.search_engine import _apply_topic_grouping  # noqa: PLC0415 — test-local

    rows = [_in(DOCS, "far", 0.60), _in(DOCS, "near", 0.40), _in(DOCS, "mid", 0.50), _in(DOCS, "loose", 0.45)]
    out = _apply_topic_grouping(rows, {"far": 1, "near": 2, "mid": 1}, _taxonomy({1: "a", 2: "b"}))
    assert [r.id for r in out] == ["near", "mid", "far", "loose"]


def test_search_cross_corpus_hands_its_own_model_resolver_to_the_topic_grouping(cloud_mode, monkeypatch) -> None:
    """The production call site passes the search's row-preferred resolver, as it does to the pool cut."""
    import nexus.search_engine as se  # noqa: PLC0415 — the module under test, patched by attribute

    def resolver(name: str) -> str | None:
        return "voyage-context-3"

    class _T3:
        def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include"):
            return [{"id": f"{c}-{j}", "content": "x", "distance": 0.3 + j * 0.01, "collection": c}
                    for c in collection_names for j in range(3)]

        def get_embeddings(self, collection_name, ids):
            return np.ones((len(ids), 4), dtype=np.float32)

    from unittest.mock import MagicMock  # noqa: PLC0415 — test-local

    taxonomy = MagicMock()
    taxonomy.get_assignments_for_docs.side_effect = lambda ids: dict.fromkeys(ids, 1)
    taxonomy.get_topic_link_pairs.return_value = {}
    taxonomy.get_labels_for_ids.return_value = {1: "t"}
    monkeypatch.setattr(se, "_collection_model_resolver", lambda t3, collections, rows=None: resolver)
    seen: list[object] = []
    real = se._apply_topic_grouping

    def spy(results, assignments, tax, **kwargs):
        seen.append(kwargs.get("model_of"))
        return real(results, assignments, tax, **kwargs)

    monkeypatch.setattr(se, "_apply_topic_grouping", spy)
    search_cross_corpus("q", ["knowledge__a", "knowledge__b"], n_results=5, t3=_T3(),
                        cluster_by="semantic", taxonomy=taxonomy, sensitivity="include")
    assert seen == [resolver]


# --- Ward clustering orders on the calibrated scale (GH #1667 follow-up C) -----------------------------------------


def _two_far_clusters() -> np.ndarray:
    return np.array([[1, 0, 0, 0], [1, 0.01, 0, 0], [1, 0, 0.01, 0],
                     [0, 1, 0, 0], [0, 1, 0.01, 0], [0, 1, 0, 0.01]], dtype=np.float32)


def test_clustering_takes_the_model_of_resolver_the_search_built() -> None:
    """Names with no model token resolve to one install model by name alone (every factor 1.0, the raw
    order); only the row-preferred resolver can say the code cluster's 0.38 is farther than the prose 0.50."""
    from nexus.search_engine import _apply_clustering  # noqa: PLC0415 — test-local

    code, prose = "code__legacy", "docs__legacy"
    # Local models, so the test stays in the default (local) posture the name derivation depends on:
    # bge-base-en-v15-768 calibrates at 1.37, an unlisted model at the default 0.818.
    models = {code: "bge-base-en-v15-768", prose: "unlisted-test-model"}
    rows = [_in(code, f"c{i}", d) for i, d in enumerate([0.38, 0.40, 0.42])] + [
        _in(prose, f"d{i}", d) for i, d in enumerate([0.50, 0.52, 0.54])
    ]
    plain = _apply_clustering(rows, _two_far_clusters())
    assert [r.id for r in plain][0] == "c0", "no resolver: factors are 1.0 and the code cluster leads"
    out = _apply_clustering(rows, _two_far_clusters(), model_of=models.get)
    assert [r.id for r in out] == ["d0", "d1", "d2", "c0", "c1", "c2"]
    assert [r.distance for r in out] == [0.50, 0.52, 0.54, 0.38, 0.40, 0.42], "reported distances are raw"


def test_search_cross_corpus_hands_its_own_model_resolver_to_the_clustering(monkeypatch) -> None:
    """The production call site passes the search's row-preferred resolver, as it does to the pool cut and
    the topic grouping."""
    import nexus.search_engine as se  # noqa: PLC0415 — the module under test, patched by attribute

    def resolver(name: str) -> str | None:
        return "bge-base-en-v15-768"

    class _T3:
        def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include"):
            return [{"id": f"{c}-{j}", "content": "x", "distance": 0.3 + j * 0.01, "collection": c}
                    for c in collection_names for j in range(3)]

        def get_embeddings(self, collection_name, ids):
            rng = np.random.default_rng(len(ids))
            return rng.normal(size=(len(ids), 4)).astype(np.float32)

    monkeypatch.setattr(se, "_collection_model_resolver", lambda t3, collections, rows=None: resolver)
    seen: list[object] = []
    real = se._apply_clustering

    def spy(results, embeddings, **kwargs):
        seen.append(kwargs.get("model_of"))
        return real(results, embeddings, **kwargs)

    monkeypatch.setattr(se, "_apply_clustering", spy)
    search_cross_corpus("q", ["knowledge__a", "knowledge__b"], n_results=5, t3=_T3(),
                        cluster_by="semantic", sensitivity="include")
    assert seen == [resolver]
