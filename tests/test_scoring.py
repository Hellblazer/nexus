# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consolidated scoring tests: normalize, hybrid, rerank, interleave, quality, file-size."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus.scoring import (
    _file_size_factor,
    apply_hybrid_scoring,
    apply_link_boost,
    apply_quality_boost,
    min_max_normalize,
    quality_score,
    rerank_results,
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


# ── rerank_results ───────────────────────────────────────────────────────────

def test_rerank_empty():
    assert rerank_results([], "query") == []


@pytest.mark.parametrize("exc", [Exception("API error"), RuntimeError("timeout")])
def test_rerank_degrades_on_error(exc):
    mock_client = MagicMock()
    mock_client.rerank.side_effect = exc
    with patch("nexus.scoring._voyage_client", return_value=mock_client):
        results = rerank_results([_r()], "query", top_k=1)
    assert len(results) == 1


# ── round_robin_interleave ───────────────────────────────────────────────────

@pytest.mark.parametrize("groups,expected_dists", [
    ([], []),
    ([[]], []),
    ([[_r(dist=0.1), _r(dist=0.3)], [_r(dist=0.2)]], [0.1, 0.2, 0.3]),
])
def test_round_robin_interleave(groups, expected_dists):
    result = round_robin_interleave(groups)
    assert [r.distance for r in result] == expected_dists


# ── _file_size_factor ────────────────────────────────────────────────────────

@pytest.mark.parametrize("chunks,expected", [
    (30, 1.0),       # at threshold
    (37, 30 / 37),   # above threshold
    (10, 1.0),       # below threshold
    (0, 1.0),        # zero → max(1, 0)=1
])
def test_file_size_factor(chunks, expected):
    assert _file_size_factor(chunks) == pytest.approx(expected, abs=0.001)


def test_size_penalty_applied_to_code():
    r_a = _r("code__repo", 0.0, chunks=5)
    r_b = _r("code__repo", 0.5, chunks=60)
    r_c = _r("code__repo", 1.0, chunks=5)
    results = apply_hybrid_scoring([r_a, r_b, r_c], hybrid=False)
    score_map = {r.distance: r.hybrid_score for r in results}
    assert score_map[0.5] == pytest.approx(0.25, abs=1e-6)


@pytest.mark.parametrize("coll", ["docs__corpus", "knowledge__notes"])
def test_size_penalty_not_applied_to_non_code(coll):
    r_a = _r(coll, 0.0, chunks=5)
    r_b = _r(coll, 0.5, chunks=60)
    r_c = _r(coll, 1.0, chunks=5)
    results = apply_hybrid_scoring([r_a, r_b, r_c], hybrid=False)
    score_map = {r.distance: r.hybrid_score for r in results}
    assert score_map[0.5] == pytest.approx(0.5, abs=1e-6)


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
        from nexus.catalog.catalog import Catalog
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def _make_result(self, source_path="src/foo.py", score=0.5, collection="code__test"):
        return SearchResult(
            id="r1", content="text", distance=0.3, collection=collection,
            metadata={"source_path": source_path}, hybrid_score=score,
        )

    def test_implements_link_boosts_score(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements", created_by="test")

        r = self._make_result(source_path="src/foo.py", score=0.5)
        apply_link_boost([r], cat)
        assert r.hybrid_score > 0.5  # boosted

    def test_heuristic_link_no_boost(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements-heuristic", created_by="test")

        r = self._make_result(source_path="src/foo.py", score=0.5)
        apply_link_boost([r], cat)
        assert r.hybrid_score == 0.5  # unchanged

    def test_no_catalog_returns_unchanged(self):
        r = self._make_result(score=0.5)
        apply_link_boost([r], None)
        assert r.hybrid_score == 0.5

    def test_no_matching_entry_unchanged(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        r = self._make_result(source_path="nonexistent.py", score=0.5)
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

        r = self._make_result(source_path="src/foo.py", score=0.5)
        apply_link_boost([r], cat, boost_weight=0.15)
        # signal capped at 1.0, so max boost is 0.15
        assert r.hybrid_score == pytest.approx(0.65, abs=0.01)

    def test_relates_link_half_boost(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "relates", created_by="test")

        r = self._make_result(source_path="src/foo.py", score=0.5)
        apply_link_boost([r], cat, boost_weight=0.15)
        # relates = 0.5 weight, so boost = 0.15 * 0.5 = 0.075
        assert r.hybrid_score == pytest.approx(0.575, abs=0.01)

    def test_custom_type_weights(self, tmp_path):
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc12345", repo_root=str(tmp_path))
        t1 = cat.register(owner, "foo.py", content_type="code", file_path="src/foo.py")
        t2 = cat.register(owner, "bar.py", content_type="code", file_path="src/bar.py")
        cat.link(t1, t2, "implements", created_by="test")

        r = self._make_result(source_path="src/foo.py", score=0.5)
        apply_link_boost([r], cat, boost_weight=0.2, type_weights={"implements": 0.5})
        # 0.2 * 0.5 = 0.1
        assert r.hybrid_score == pytest.approx(0.6, abs=0.01)


# ── Topic boost (RDR-070, nexus-aym) ─────────────────────────────────────


class TestTopicBoost:
    """apply_topic_boost() scoring tests."""

    def _make_result(
        self, doc_id="doc-1", score=0.5, collection="code__test",
    ) -> SearchResult:
        return SearchResult(
            id=doc_id, content="text", distance=0.3, collection=collection,
            metadata={}, hybrid_score=score,
        )

    def test_same_topic_boost(self) -> None:
        """Results in the same topic as another result get +0.1 boost."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", score=0.5)
        r2 = self._make_result(doc_id="doc-b", score=0.4)
        r3 = self._make_result(doc_id="doc-c", score=0.3)

        # doc-a and doc-b in same topic, doc-c in a different one
        assignments = {"doc-a": 1, "doc-b": 1, "doc-c": 2}

        apply_topic_boost([r1, r2, r3], assignments)

        # doc-a and doc-b should be boosted (same topic pair)
        assert r1.hybrid_score > 0.5
        assert r2.hybrid_score > 0.4
        # doc-c is alone in its topic — no same-topic partner in results
        assert r3.hybrid_score == 0.3

    def test_linked_topic_boost(self) -> None:
        """Results in linked topics get +0.05 boost."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", score=0.5)
        r2 = self._make_result(doc_id="doc-b", score=0.4)

        assignments = {"doc-a": 1, "doc-b": 2}
        topic_links = {(1, 2): 3}  # topics 1 and 2 are linked

        apply_topic_boost([r1, r2], assignments, topic_links=topic_links)

        assert r1.hybrid_score > 0.5
        assert r2.hybrid_score > 0.4

    def test_no_assignments_unchanged(self) -> None:
        """No topic assignments → scores unchanged."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", score=0.5)

        apply_topic_boost([r1], {})
        assert r1.hybrid_score == 0.5

    def test_single_result_no_boost(self) -> None:
        """A single result has no partner → no boost."""
        from nexus.scoring import apply_topic_boost

        r1 = self._make_result(doc_id="doc-a", score=0.5)
        assignments = {"doc-a": 1}

        apply_topic_boost([r1], assignments)
        assert r1.hybrid_score == 0.5

    def test_boost_values(self) -> None:
        """Verify exact boost amounts."""
        from nexus.scoring import (
            _TOPIC_LINKED_BOOST,
            _TOPIC_SAME_BOOST,
            apply_topic_boost,
        )

        r1 = self._make_result(doc_id="doc-a", score=0.5)
        r2 = self._make_result(doc_id="doc-b", score=0.5)

        assignments = {"doc-a": 1, "doc-b": 1}
        apply_topic_boost([r1, r2], assignments)

        assert r1.hybrid_score == pytest.approx(0.5 + _TOPIC_SAME_BOOST, abs=0.001)
        assert r2.hybrid_score == pytest.approx(0.5 + _TOPIC_SAME_BOOST, abs=0.001)

    def test_combined_same_and_linked_boost(self) -> None:
        """Results get both same-topic and linked-topic boost."""
        from nexus.scoring import (
            _TOPIC_LINKED_BOOST,
            _TOPIC_SAME_BOOST,
            apply_topic_boost,
        )

        r1 = self._make_result(doc_id="doc-a", score=0.5)
        r2 = self._make_result(doc_id="doc-b", score=0.5)
        r3 = self._make_result(doc_id="doc-c", score=0.5)

        # doc-a and doc-b in topic 1, doc-c in topic 2
        assignments = {"doc-a": 1, "doc-b": 1, "doc-c": 2}
        topic_links = {(1, 2): 1}

        apply_topic_boost([r1, r2, r3], assignments, topic_links=topic_links)

        # r1 gets same-topic (with r2) + linked-topic (with r3)
        assert r1.hybrid_score == pytest.approx(
            0.5 + _TOPIC_SAME_BOOST + _TOPIC_LINKED_BOOST, abs=0.001,
        )
        # r3 gets linked-topic (with r1 and r2)
        assert r3.hybrid_score == pytest.approx(
            0.5 + _TOPIC_LINKED_BOOST, abs=0.001,
        )
