# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-055 E2: quality_score reranking from bibliographic metadata."""
from __future__ import annotations

import math

import pytest

from nexus.scoring import quality_score, apply_quality_boost
from nexus.types import SearchResult


def _sr(
    distance: float = 0.3,
    collection: str = "knowledge__papers",
    bib_citation_count: int = 0,
    bib_year: str = "",
    **extra_meta: object,
) -> SearchResult:
    meta: dict = {"bib_citation_count": bib_citation_count}
    if bib_year:
        meta["bib_year"] = bib_year
    meta.update(extra_meta)
    return SearchResult(
        id="r1", content="chunk", distance=distance,
        collection=collection, metadata=meta,
    )


# ── quality_score function ────���───────────────────────────────────────────────


class TestQualityScore:
    """Unit tests for the quality_score() formula."""

    def test_zero_citations_returns_zero(self):
        """Unenriched chunks (count=0) produce score=0 — no bias."""
        assert quality_score(0) == 0.0

    def test_positive_citations(self):
        """Enriched chunk with citations produces positive score."""
        s = quality_score(100)
        assert s > 0.0

    def test_monotonic_in_citations(self):
        """More citations → higher score."""
        s1 = quality_score(10)
        s2 = quality_score(100)
        s3 = quality_score(1000)
        assert s1 < s2 < s3

    def test_bounded_at_one(self):
        """Even extreme citation counts stay at or below 1.0."""
        s = quality_score(100_000)
        assert s <= 1.0

    def test_age_decay(self):
        """Older papers score lower than recent papers with same citations."""
        recent = quality_score(100, age_days=30)
        old = quality_score(100, age_days=3000)
        assert recent > old

    def test_age_zero_no_decay(self):
        """age_days=0 means no decay applied."""
        s_no_age = quality_score(100, age_days=0)
        s_with_age = quality_score(100, age_days=365)
        assert s_no_age >= s_with_age

    def test_alpha_weight(self):
        """alpha=1.0 ignores age; alpha=0.0 ignores citations."""
        citation_only = quality_score(100, age_days=365, alpha=1.0)
        # With alpha=1.0, age decay shouldn't matter
        assert citation_only == pytest.approx(
            quality_score(100, age_days=0, alpha=1.0), abs=1e-9
        )
        # With alpha=0.0, citation count weight is zero — only age matters
        age_heavy = quality_score(100, age_days=365, alpha=0.0)
        age_light = quality_score(100, age_days=30, alpha=0.0)
        assert age_light > age_heavy

    def test_zero_count_always_zero(self):
        """count=0 returns 0 regardless of alpha (spec: skip unenriched)."""
        assert quality_score(0, age_days=365, alpha=0.0) == 0.0
        assert quality_score(0, age_days=365, alpha=1.0) == 0.0


# ── apply_quality_boost integration ──────────────────────────────────────────


class TestApplyQualityBoost:
    """Integration tests for apply_quality_boost on SearchResult lists."""

    def test_no_enrichment_no_change(self):
        """When no results have bib_citation_count, order unchanged."""
        results = [
            _sr(distance=0.3, bib_citation_count=0),
            _sr(distance=0.5, bib_citation_count=0),
        ]
        for r in results:
            r.hybrid_score = 1.0 - r.distance
        original_scores = [r.hybrid_score for r in results]
        boosted = apply_quality_boost(results)
        assert [r.hybrid_score for r in boosted] == original_scores

    def test_enriched_result_boosted(self):
        """Result with high citation count gets score boost."""
        r_high = _sr(distance=0.5, bib_citation_count=500)
        r_low = _sr(distance=0.5, bib_citation_count=0)
        r_high.hybrid_score = 0.5
        r_low.hybrid_score = 0.5
        boosted = apply_quality_boost([r_high, r_low])
        assert boosted[0].hybrid_score > boosted[1].hybrid_score

    def test_code_collections_skipped(self):
        """quality_boost only applies to knowledge/docs/rdr, not code."""
        r = _sr(distance=0.3, collection="code__repo", bib_citation_count=500)
        r.hybrid_score = 0.7
        original = r.hybrid_score
        boosted = apply_quality_boost([r])
        assert boosted[0].hybrid_score == original

    def test_boost_weight_parameter(self):
        """boost_weight controls strength of quality signal."""
        r = _sr(bib_citation_count=100)
        r.hybrid_score = 0.5
        small = apply_quality_boost([_sr(bib_citation_count=100)], boost_weight=0.05)
        # Assign hybrid_score before calling
        r2 = _sr(bib_citation_count=100)
        r2.hybrid_score = 0.5
        large = apply_quality_boost([r2], boost_weight=0.2)
        # Larger weight means bigger boost (result had citations)
        assert small[0].hybrid_score < large[0].hybrid_score
