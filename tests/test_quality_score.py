# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-055 E2: quality_score and apply_quality_boost."""
from __future__ import annotations

import pytest

from nexus.scoring import quality_score, apply_quality_boost
from nexus.types import SearchResult


def _qr(dist: float = 0.3, coll: str = "knowledge__papers",
        bib_count: int = 0, **meta: object) -> SearchResult:
    m = {"bib_citation_count": bib_count}
    m.update(meta)
    return SearchResult(id="r1", content="chunk", distance=dist,
                        collection=coll, metadata=m)


@pytest.mark.parametrize("count", [0, -1])
def test_quality_score_zero_for_unenriched(count):
    assert quality_score(count) == 0.0


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
