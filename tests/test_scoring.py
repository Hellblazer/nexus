# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consolidated scoring tests: normalize, hybrid, rerank, interleave, quality, file-size."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.scoring import (
    _calibration_factor_for_model,
    _resolve_calibration_factors,
    apply_hybrid_scoring,
    apply_link_boost,
    apply_quality_boost,
    min_max_normalize,
    quality_score,
    round_robin_interleave,
)
from nexus.types import SearchResult


def _r(coll: str = "code__repo", dist: float = 0.3, frecency: float = 0.5,
       chunks: int = 1, **meta: object) -> SearchResult:
    m = {"frecency_score": frecency, "chunk_count": chunks}
    m.update(meta)
    return SearchResult(id=f"{coll}-d{dist}", content="content",
                        distance=dist, collection=coll, metadata=m)


# ── min_max_normalize ────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,window,expected", [
    (42.0, [42.0], 1.0),               # single element
    (5.0, [5.0, 5.0, 5.0], 0.0),       # identical values
    (0.5, [0.0, 1.0], 0.5),            # typical
    (1.0, [1.0, 3.0, 5.0], 0.0),       # min of range
    (5.0, [1.0, 3.0, 5.0], 1.0),       # max of range
])
def test_min_max_normalize(value, window, expected):
    assert min_max_normalize(value, window) == pytest.approx(expected, abs=1e-6)


def test_min_max_normalize_empty_raises():
    with pytest.raises(ValueError, match="non-empty"):
        min_max_normalize(0.5, [])


# ── apply_hybrid_scoring ─────────────────────────────────────────────────────

def test_hybrid_scoring_empty():
    assert apply_hybrid_scoring([], hybrid=True) == []


def test_hybrid_scoring_no_code_warns():
    r = _r(coll="docs__corpus", dist=0.2)
    results = apply_hybrid_scoring([r], hybrid=True)
    assert len(results) == 1 and results[0].hybrid_score is not None


def test_hybrid_scoring_code_uses_frecency():
    r = _r(coll="code__repo", dist=0.2, frecency=0.8)
    results = apply_hybrid_scoring([r], hybrid=True)
    assert results[0].hybrid_score > 0


def test_hybrid_score_weighted_sum():
    from nexus.scoring import hybrid_score
    assert hybrid_score(0.8, 0.5) == pytest.approx(0.71, abs=1e-6)


# ── round_robin_interleave ───────────────────────────────────────────────────

@pytest.mark.parametrize("groups,expected_dists", [
    ([], []),
    ([[]], []),
    ([[_r(dist=0.1), _r(dist=0.3)], [_r(dist=0.2)]], [0.1, 0.2, 0.3]),
])
def test_round_robin_interleave(groups, expected_dists):
    result = round_robin_interleave(groups)
    assert [r.distance for r in result] == expected_dists


# ── chunk-count blindness (RDR-006 supersession, nexus-0bmhd) ───────────────
#
# RDR-006's file-size scoring penalty (_file_size_factor, _FILE_SIZE_THRESHOLD)
# is removed from scoring entirely — not gated, absent. Domination control
# moved to the render layer: search_engine.apply_file_diversity_cap (see
# tests/test_search_engine.py::TestApplyFileDiversityCap). These tests pin
# that scoring itself no longer reads chunk_count at all.

def test_apply_hybrid_scoring_is_chunk_count_blind():
    """Two code__ results at an IDENTICAL vector distance, one from a
    5-chunk file and one from a 5000-chunk file, must score IDENTICALLY —
    scoring has no opinion on file size any more."""
    small = _r("code__repo", 0.3, chunks=5)
    large = _r("code__repo", 0.3, chunks=5000)
    results = apply_hybrid_scoring([small, large], hybrid=False)
    assert results[0].hybrid_score == pytest.approx(results[1].hybrid_score, abs=1e-9)


def test_new_scoring_fixes_the_vlzz0_ranking_the_old_formula_got_wrong():
    """nexus-vlzz0: src/nexus/corpus.py (chunk_count=145, the measured
    plans/runner.py-scale value) at distance 0.1981 lost to 8-chunk test
    files at distance 0.267-0.32 under RDR-006's original per-file
    penalty (0.1981's factor = min(1, 30/145) ~= 0.207, dragging its score
    below the unpenalized (chunk_count=8 <= threshold=30) test files).

    Reimplements the ORIGINAL (now fully removed from scoring.py) formula
    inline, labelled historical, purely as a FIXTURE-SANITY gate: if the
    historical formula did not actually reproduce the bug on these
    numbers, the "old formula got this wrong" premise would be untested
    and this test would be vacuous. Then asserts the CURRENT
    (chunk-count-blind) scoring ranks the impl file first — the fix.
    """
    IMPL_DIST = 0.1981
    IMPL_CHUNKS = 145
    TEST_DISTANCES = [0.267, 0.29, 0.32]
    HISTORICAL_THRESHOLD = 30

    def _historical_file_size_factor(chunk_count: int) -> float:
        # RDR-006's removed formula, reimplemented here ONLY to prove the
        # fixture reproduces the bug the new scoring fixes.
        return min(1.0, HISTORICAL_THRESHOLD / max(1, chunk_count))

    distances = [IMPL_DIST, *TEST_DISTANCES]
    chunk_counts = [IMPL_CHUNKS, 8, 8, 8]
    lo, hi = min(distances), max(distances)
    old_scores = [
        (1.0 - (d - lo) / (hi - lo + 1e-9)) * _historical_file_size_factor(cc)
        for d, cc in zip(distances, chunk_counts)
    ]
    old_winner = "impl" if old_scores[0] == max(old_scores) else "test"
    assert old_winner != "impl", (
        "fixture sanity gate: the historical formula must reproduce the "
        "nexus-vlzz0 bug (impl demoted below a smaller test file) on "
        "these numbers, or this test proves nothing about the fix"
    )

    impl_r = _r("code__nexus", IMPL_DIST, chunks=IMPL_CHUNKS)
    test_rs = [_r("code__nexus", d, chunks=8) for d in TEST_DISTANCES]
    results = apply_hybrid_scoring([impl_r, *test_rs], hybrid=False)
    assert results[0].distance == pytest.approx(IMPL_DIST), (
        f"chunk-count-blind scoring should rank the impl file first; "
        f"got distance order {[r.distance for r in results]}"
    )


# ── cross-model distance calibration (nexus-tox2m) ──────────────────────────
#
# Raw cosine distance is not comparable across embedding models (a measured
# stable 0.135-0.203 scale gap between voyage-code-3 and voyage-context-3 on
# off-domain queries is a model-scale artefact, not a relevance signal).
# Pooling ONE min-max window over RAW distances let the tighter-scaled,
# larger code__ corpus dominate a merged top-N regardless of the prose
# corpus's actual relevance. The fix rescales each result's distance by a
# per-collection factor from `_resolve_calibration_factors` (keyed on the
# RESOLVED EMBEDDING MODEL, not the collection prefix -- code review
# Critical, second round: a prefix-keyed factor fired even when every
# collection actually shared one local embedder, the LOCAL-MODE DEFAULT
# install path, producing a maximal unjustified reorder on an identical-
# distance tie) BEFORE pooling every result into ONE window -- comparable
# absolute scores, not independently-maximized per-corpus ranks (that
# alternative was tried and rejected too: code review Critical, first
# round, found it mints a fake winner out of each corpus's own local best
# regardless of whether that candidate is actually a good match).

def test_calibration_factor_for_model_baseline_and_scaling(cloud_mode) -> None:  # RDR-109: names voyage-* collections
    assert _calibration_factor_for_model("voyage-code-3") == pytest.approx(1.0)
    assert _calibration_factor_for_model("voyage-context-3") == pytest.approx(0.45 / 0.65)


def test_calibration_factor_for_unrecognized_model_gets_default(cloud_mode) -> None:  # RDR-109: names voyage-* collections
    """An embedding model this table doesn't know (e.g. a local embedder
    token, or any future model) falls to the same "default" bucket
    `search_engine._threshold_for_collection` already uses for an
    unrecognized collection prefix -- provably NOT voyage-code-3's
    factor."""
    factor = _calibration_factor_for_model("bge-base-en-v15-768")
    assert factor == pytest.approx(0.45 / 0.55)
    assert factor != pytest.approx(_calibration_factor_for_model("voyage-code-3"))


def test_resolve_calibration_factors_is_noop_for_single_embedder_result_set():
    """CRITICAL regression pin (code review, second round): local-mode
    installs (the DEFAULT install path) embed every collection with ONE
    local model regardless of content_type -- code__, knowledge__,
    docs__, rdr__ all share the same embedding space, so there is no
    scale gap to correct. Falsified directly against the first version
    of this fix: two results at an IDENTICAL raw distance, one code__
    one knowledge__, scored 0.0 vs 1.0 purely from the collection name.
    Conformant 4-segment local-mode collection names (RDR-103 shape)
    carry the real model token directly, so this MUST resolve to a
    single distinct model and every factor MUST be 1.0."""
    code_r = _r("code__1-1__bge-base-en-v15-768__v1", 0.30, chunks=1)
    know_r = _r("knowledge__1-1__bge-base-en-v15-768__v1", 0.30, chunks=1)
    factors = _resolve_calibration_factors([code_r, know_r])
    assert factors == {
        "code__1-1__bge-base-en-v15-768__v1": 1.0,
        "knowledge__1-1__bge-base-en-v15-768__v1": 1.0,
    }


def test_identical_distance_stays_identical_score_in_single_embedder_mode():
    """End-to-end version of the no-op pin above, through the real
    scoring path: two results at an IDENTICAL raw distance, one code__
    one knowledge__, sharing one local embedder, MUST score identically
    -- not 0.0 vs 1.0 from a prefix-derived reshuffle."""
    code_r = _r("code__1-1__bge-base-en-v15-768__v1", 0.30, chunks=1)
    know_r = _r("knowledge__1-1__bge-base-en-v15-768__v1", 0.30, chunks=1)
    scored = apply_hybrid_scoring([code_r, know_r], hybrid=False)
    assert scored[0].hybrid_score == pytest.approx(scored[1].hybrid_score, abs=1e-9)


def test_resolve_calibration_factors_activates_for_genuine_multi_model_set(cloud_mode) -> None:  # RDR-109: names voyage-* collections
    """Counterpart to the no-op pin: legacy 2-segment names (no embedded
    model token) fall back to the prefix-based Voyage heuristic
    (nexus.corpus.voyage_model_for_collection) -- code__ and rdr__
    resolve to DIFFERENT models there, so calibration DOES activate,
    matching real cloud/service-mode behavior (conformant 4-segment
    names carry voyage-code-3/voyage-context-3 directly and resolve the
    same way)."""
    factors = _resolve_calibration_factors([
        _r("code__repo", 0.30), _r("rdr__proj", 0.30),
    ])
    assert factors["code__repo"] == pytest.approx(1.0)
    assert factors["rdr__proj"] == pytest.approx(0.45 / 0.65)


def test_calibration_thresholds_match_config_defaults(cloud_mode) -> None:  # RDR-109: names voyage-* collections
    """DRIFT GUARD (code review Significant-1): _CALIBRATION_THRESHOLDS_BY_MODEL
    is a hardcoded literal copy of config.py's search.distance_threshold
    defaults, with zero runtime coupling -- chosen over threading live
    config through apply_hybrid_scoring because that would require
    touching apply_ranking_boosts and its search_cmd.py/mcp/core.py call
    sites, well outside this fix's scope (scoring.py + tests only). This
    test is the tradeoff's enforcement: it fails LOUDLY the moment a
    future retune of config.py's thresholds (a real, user-configurable,
    previously-recalibrated-once value -- see config.py's own "Post-
    RDR-059 recalibrated thresholds" comment) diverges from this copy,
    instead of silently reordering every cross-model search."""
    from nexus.config import _DEFAULTS
    from nexus.scoring import (
        _CALIBRATION_DEFAULT_THRESHOLD,
        _CALIBRATION_THRESHOLDS_BY_MODEL,
    )

    cfg_thresholds = _DEFAULTS["search"]["distance_threshold"]
    assert _CALIBRATION_THRESHOLDS_BY_MODEL["voyage-code-3"] == cfg_thresholds["code"]
    assert _CALIBRATION_THRESHOLDS_BY_MODEL["voyage-context-3"] == cfg_thresholds["knowledge"]
    assert _CALIBRATION_THRESHOLDS_BY_MODEL["voyage-context-3"] == cfg_thresholds["docs"]
    assert _CALIBRATION_THRESHOLDS_BY_MODEL["voyage-context-3"] == cfg_thresholds["rdr"]
    assert _CALIBRATION_DEFAULT_THRESHOLD == cfg_thresholds["default"]


def test_apply_hybrid_scoring_calibrates_before_pooling():
    """A genuinely GOOD rdr__ match (well inside its own 0.65 threshold)
    must be able to win the merge against code__ results that are, in raw-
    distance terms, closer -- because raw distance alone is a model-scale
    artefact, not a fair comparison. On raw distance alone, code's best
    (0.36) beats rdr's 0.48 and the rdr result is buried; calibrated
    (0.48 * 0.45/0.65 ~= 0.332), it becomes the pool's best and reaches
    the top. Mirrors the measured live-probe shape (rdr-092 at raw
    distance 0.4827, calibrated ~0.334, beating code's own best surviving
    candidates at 0.38-0.39)."""
    code_results = [
        _r("code__repo", d, chunks=1) for d in (0.36, 0.38, 0.40, 0.42, 0.44)
    ]
    rdr_result = _r("rdr__proj", 0.48, chunks=1)

    scored = apply_hybrid_scoring([*code_results, rdr_result], hybrid=False)
    rdr_rank = next(i for i, r in enumerate(scored) if r.collection == "rdr__proj")
    assert rdr_rank == 0, (
        f"calibrated rdr__ match should top the merge; "
        f"ranked at position {rdr_rank} of {len(scored)}"
    )


def test_calibration_does_not_mint_fake_winners():
    """Code review Critical 2 counter-test: a corpus with NOTHING
    relevant -- every candidate sitting near its own threshold, i.e. a
    weak match -- must NOT beat a genuinely strong match from a
    different corpus. A weak rdr__ match at 0.64 (barely inside its 0.65
    threshold) calibrates to ~0.443, which must NOT beat a strong
    code__ match at 0.05.

    DISCRIMINATION (code-review Important, T2 [23990]): an earlier
    version of this test used ONE item per collection, so under the
    REJECTED per-corpus-window design both collections' sole member
    normalized to 1.0, tied, and the stable sort kept code first — it
    passed against the very design it was named to reject. Two items
    per collection plus a score-MAGNITUDE assertion is what actually
    separates the designs: per-corpus windows pin each group's local
    best to 1.0 regardless of absolute relevance, so the weak rdr__
    best would land at the ceiling alongside the strong code__ best.
    Under the shipped single-pooled-window design it must sit well
    below it."""
    strong_code = _r("code__repo", 0.05, chunks=1)
    other_code = _r("code__repo", 0.20, chunks=1)
    weak_rdr = _r("rdr__proj", 0.64, chunks=1)
    other_rdr = _r("rdr__proj", 0.68, chunks=1)
    scored = apply_hybrid_scoring(
        [strong_code, other_code, weak_rdr, other_rdr], hybrid=False,
    )
    assert scored[0].collection == "code__repo", (
        f"a weak rdr__ match must not outrank a strong code__ match; "
        f"got order {[r.collection for r in scored]}"
    )
    best_code = max(r.hybrid_score for r in scored if r.collection == "code__repo")
    best_rdr = max(r.hybrid_score for r in scored if r.collection == "rdr__proj")
    # Per-corpus windows would put BOTH at the 1.0 ceiling. One pooled
    # window over calibrated distances must leave the irrelevant corpus
    # visibly behind, not merely second.
    assert best_rdr < best_code - 0.15, (
        f"the weak corpus was renormalized close to the strong one — the "
        f"fake-winner failure mode: best_code={best_code:.4f} "
        f"best_rdr={best_rdr:.4f}"
    )


# ── quality_score (RDR-055 E2) ───────────────────────────────────────────────

@pytest.mark.parametrize("count,expected_zero", [
    (0, True), (-1, True),
])
def test_quality_score_zero_for_unenriched(count, expected_zero):
    assert (quality_score(count) == 0.0) == expected_zero


def test_quality_score_monotonic():
    scores = [quality_score(n) for n in (10, 100, 1000)]
    assert scores[0] < scores[1] < scores[2]


def test_quality_score_bounded():
    assert quality_score(100_000) <= 1.0


def test_quality_score_age_decay():
    assert quality_score(100, age_days=30) > quality_score(100, age_days=3000)


def test_quality_score_alpha_ignores_age():
    assert quality_score(100, age_days=365, alpha=1.0) == pytest.approx(
        quality_score(100, age_days=0, alpha=1.0), abs=1e-9)


# ── apply_quality_boost ──────────────────────────────────────────────────────

def _qr(dist: float = 0.3, coll: str = "knowledge__papers",
        bib_count: int = 0, **meta: object) -> SearchResult:
    m = {"bib_citation_count": bib_count}
    m.update(meta)
    return SearchResult(id="r1", content="chunk", distance=dist,
                        collection=coll, metadata=m)


def test_quality_boost_no_enrichment():
    results = [_qr(0.3), _qr(0.5)]
    for r in results:
        r.hybrid_score = 1.0 - r.distance
    orig = [r.hybrid_score for r in results]
    apply_quality_boost(results)
    assert [r.hybrid_score for r in results] == orig


def test_quality_boost_enriched():
    r_high, r_low = _qr(bib_count=500), _qr(bib_count=0)
    r_high.hybrid_score = r_low.hybrid_score = 0.5
    apply_quality_boost([r_high, r_low])
    assert r_high.hybrid_score > r_low.hybrid_score


def test_quality_boost_skips_code():
    r = _qr(coll="code__repo", bib_count=500)
    r.hybrid_score = 0.7
    apply_quality_boost([r])
    assert r.hybrid_score == 0.7


# ── module import + circular dep guards ──────────────────────────────────────

def test_no_circular_imports():
    import importlib, sys
    saved = dict(sys.modules)
    try:
        for mod in [k for k in sys.modules if k.startswith("nexus.")]:
            del sys.modules[mod]
        scoring = importlib.import_module("nexus.scoring")
        formatters = importlib.import_module("nexus.formatters")
        assert not hasattr(scoring, "search_engine")
        assert not hasattr(formatters, "search_engine")
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


# ── formatter spot-checks ────────────────────────────────────────────────────

def test_format_vimgrep():
    from nexus.formatters import format_vimgrep
    r = SearchResult(id="1", content="def foo():", distance=0.1,
                     collection="code__r", metadata={"source_path": "./foo.py", "line_start": 10})
    assert format_vimgrep([r]) == ["./foo.py:10:0:def foo():"]


def test_format_json_no_metadata_shadow():
    import json as _json
    from nexus.formatters import format_json
    r = SearchResult(id="real", content="real", distance=0.3, collection="c",
                     metadata={"id": "EVIL", "content": "EVIL"})
    parsed = _json.loads(format_json([r]))
    assert parsed[0]["id"] == "real" and parsed[0]["content"] == "real"


# ── link boost (RDR-060 E3) ─────────────────────────────────────────────────

class TestLinkBoost:
    """apply_link_boost() scoring tests."""

    def _make_catalog(self, tmp_path):
        # nexus-i711w terminal deletion: the local Catalog died; seed and
        # read through the ACTIVE (service) catalog instead. The subject
        # (apply_link_boost) takes any catalog exposing links_from_batch.
        from tests._catalog_fixture_ops import ActiveCatalog
        return ActiveCatalog()

    def _make_result(self, doc_id="", score=0.5, collection="code__test"):
        # nexus-1qed: link boost keys on doc_id (Phase 1: doc_id == str(tumbler)).
        meta: dict = {}
        if doc_id:
            meta["doc_id"] = doc_id
        return SearchResult(
            id="r1", content="text", distance=0.3, collection=collection,
            metadata=meta, hybrid_score=score,
        )

    def test_implements_link_boosts_score(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements", created_by="test")

        r = self._make_result(doc_id=str(t1), score=0.5)
        apply_link_boost([r], cat)
        assert r.hybrid_score > 0.5  # boosted

    def test_heuristic_link_no_boost(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements-heuristic", created_by="test")

        r = self._make_result(doc_id=str(t1), score=0.5)
        apply_link_boost([r], cat)
        assert r.hybrid_score == 0.5  # unchanged

    def test_no_catalog_returns_unchanged(self):
        r = self._make_result(score=0.5)
        apply_link_boost([r], None)
        assert r.hybrid_score == 0.5

    def test_no_matching_entry_unchanged(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        r = self._make_result(doc_id="nonexistent-tumbler", score=0.5)
        apply_link_boost([r], cat)
        assert r.hybrid_score == 0.5

    def test_chunk_without_doc_id_skipped(self, tmp_path):
        """nexus-1qed: chunks predating the doc_id backfill are
        skipped silently. WITH TEETH: a regression that re-introduces
        the legacy source_path probe would boost the score."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements", created_by="test")

        # Chunk has source_path but no doc_id (pre-backfill shape).
        r = SearchResult(
            id="legacy-chunk", content="x", distance=0.3, collection="code__test",
            metadata={"source_path": "src/foo.py"}, hybrid_score=0.5,
        )
        apply_link_boost([r], cat)
        assert r.hybrid_score == 0.5

    def test_signal_capped_at_one(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        # Create 10 implements links
        for i in range(10):
            t_target = cat.register(owner, f"bar{i}.py", content_type="code", file_path=f"src/bar{i}.py")
            cat.link(t1, t_target, "implements", created_by="test")

        r = self._make_result(doc_id=str(t1), score=0.5)
        apply_link_boost([r], cat, boost_weight=0.15)
        # signal capped at 1.0, so max boost is 0.15
        assert r.hybrid_score == pytest.approx(0.65, abs=0.01)

    def test_relates_link_half_boost(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "relates", created_by="test")

        r = self._make_result(doc_id=str(t1), score=0.5)
        apply_link_boost([r], cat, boost_weight=0.15)
        # relates = 0.5 weight, so boost = 0.15 * 0.5 = 0.075
        assert r.hybrid_score == pytest.approx(0.575, abs=0.01)

    def test_custom_type_weights(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements", created_by="test")

        r = self._make_result(doc_id=str(t1), score=0.5)
        apply_link_boost([r], cat, boost_weight=0.2, type_weights={"implements": 0.5})
        # 0.2 * 0.5 = 0.1
        assert r.hybrid_score == pytest.approx(0.6, abs=0.01)

    def test_links_from_batch_failure_degrades_gracefully(self, tmp_path):
        """nexus-qnp5s: links_from_batch() raising (e.g. transient HTTP failure
        in service mode) must not propagate — apply_link_boost returns the
        original results unchanged and logs a warning instead of crashing."""
        from unittest.mock import MagicMock

        cat = MagicMock()
        cat.links_from_batch.side_effect = RuntimeError("simulated network failure")

        r = self._make_result(doc_id="1.1.1", score=0.7)
        original_score = r.hybrid_score

        # Must not raise; score must be unchanged (unboosted degradation).
        apply_link_boost([r], cat)

        assert r.hybrid_score == original_score
        cat.links_from_batch.assert_called_once()


# ── Topic boost (RDR-070, nexus-aym) ─────────────────────────────────────


class TestTopicBoost:
    """apply_topic_boost() scoring tests.

    Topic boost reduces ``distance`` (lower = better) rather than
    modifying ``hybrid_score``, because ``hybrid_score`` is populated
    later by the reranker and would be overwritten.
    """

    def _make_result(
        self, doc_id="doc-1", distance=0.5, collection="code__test",
    ) -> SearchResult:
        return SearchResult(
            id=doc_id, content="text", distance=distance, collection=collection,
            metadata={}, hybrid_score=0.0,
        )

    def test_same_topic_boost(self) -> None:
        """Results in the same topic as another result get distance reduction."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", distance=0.5)
        r2 = self._make_result(doc_id="doc-b", distance=0.4)
        r3 = self._make_result(doc_id="doc-c", distance=0.3)

        # doc-a and doc-b in same topic, doc-c in a different one
        assignments = {"doc-a": 1, "doc-b": 1, "doc-c": 2}

        apply_topic_boost([r1, r2, r3], assignments)

        # doc-a and doc-b should have lower distance (boosted)
        assert r1.distance < 0.5
        assert r2.distance < 0.4
        # doc-c is alone in its topic — no same-topic partner in results
        assert r3.distance == 0.3

    def test_linked_topic_boost(self) -> None:
        """Results in linked topics get distance reduction."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", distance=0.5)
        r2 = self._make_result(doc_id="doc-b", distance=0.4)

        assignments = {"doc-a": 1, "doc-b": 2}
        topic_links = {(1, 2): 3}  # topics 1 and 2 are linked

        apply_topic_boost([r1, r2], assignments, topic_links=topic_links)

        assert r1.distance < 0.5
        assert r2.distance < 0.4

    def test_no_assignments_unchanged(self) -> None:
        """No topic assignments → distances unchanged."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", distance=0.5)

        apply_topic_boost([r1], {})
        assert r1.distance == 0.5

    def test_single_result_no_boost(self) -> None:
        """A single result has no partner → no boost."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", distance=0.5)
        assignments = {"doc-a": 1}

        apply_topic_boost([r1], assignments)
        assert r1.distance == 0.5

    def test_boost_values(self) -> None:
        """Verify exact boost amounts (distance reduction)."""
        from nexus.scoring import (
            _TOPIC_LINKED_BOOST,
            _TOPIC_SAME_BOOST,
            apply_topic_boost,
        )

        r1 = self._make_result(doc_id="doc-a", distance=0.5)
        r2 = self._make_result(doc_id="doc-b", distance=0.5)

        assignments = {"doc-a": 1, "doc-b": 1}
        apply_topic_boost([r1, r2], assignments)

        assert r1.distance == pytest.approx(0.5 - _TOPIC_SAME_BOOST, abs=0.001)
        assert r2.distance == pytest.approx(0.5 - _TOPIC_SAME_BOOST, abs=0.001)

    def test_combined_same_and_linked_boost(self) -> None:
        """Results get both same-topic and linked-topic distance reduction."""
        from nexus.scoring import (
            _TOPIC_LINKED_BOOST,
            _TOPIC_SAME_BOOST,
            apply_topic_boost,
        )

        r1 = self._make_result(doc_id="doc-a", distance=0.5)
        r2 = self._make_result(doc_id="doc-b", distance=0.5)
        r3 = self._make_result(doc_id="doc-c", distance=0.5)

        # doc-a and doc-b in topic 1, doc-c in topic 2
        assignments = {"doc-a": 1, "doc-b": 1, "doc-c": 2}
        topic_links = {(1, 2): 1}

        apply_topic_boost([r1, r2, r3], assignments, topic_links=topic_links)

        # r1 gets same-topic (with r2) + linked-topic (with r3)
        assert r1.distance == pytest.approx(
            0.5 - _TOPIC_SAME_BOOST - _TOPIC_LINKED_BOOST, abs=0.001,
        )
        # r3 gets linked-topic (with r1 and r2)
        assert r3.distance == pytest.approx(
            0.5 - _TOPIC_LINKED_BOOST, abs=0.001,
        )

    def test_distance_floor_at_zero(self) -> None:
        """Distance never goes below 0.0."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", distance=0.05)
        r2 = self._make_result(doc_id="doc-b", distance=0.05)

        assignments = {"doc-a": 1, "doc-b": 1}
        apply_topic_boost([r1, r2], assignments)

        assert r1.distance == 0.0
        assert r2.distance == 0.0
