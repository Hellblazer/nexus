# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for corpus-specific over-fetch multipliers (RDR-056 Phase 2a)."""
from __future__ import annotations

from nexus.search_engine import _overfetch_multiplier, search_cross_corpus


# ── _overfetch_multiplier unit tests ─────────────────────────────────────────

class TestOverfetchMultiplier:
    def test_knowledge_returns_4x(self) -> None:
        assert _overfetch_multiplier("knowledge__papers") == 4

    def test_docs_returns_4x(self) -> None:
        assert _overfetch_multiplier("docs__corpus") == 4

    def test_rdr_returns_4x(self) -> None:
        assert _overfetch_multiplier("rdr__reviews") == 4

    def test_code_returns_2x(self) -> None:
        assert _overfetch_multiplier("code__nexus") == 2

    def test_unknown_prefix_returns_2x(self) -> None:
        """Unrecognised prefix falls through to the 2x default."""
        assert _overfetch_multiplier("custom__stuff") == 2

    def test_empty_name_returns_2x(self) -> None:
        assert _overfetch_multiplier("") == 2


# ── Tracking mock ─────────────────────────────────────────────────────────────

class _RecordingT3:
    """T3 stub that records the n_results argument passed per collection."""

    # No _voyage_client → thresholds are skipped (local/test mode).

    def __init__(self, results: list[dict] | None = None) -> None:
        self.calls: dict[str, int] = {}  # collection → n_results used
        self._results = results or []

    def search(self, query, collection_names, n_results=10, where=None):
        col = collection_names[0]
        self.calls[col] = n_results
        return self._results


# ── Per-collection fetch-size tests ──────────────────────────────────────────

class TestOverfetchInSearchCrossCorpus:
    def test_knowledge_gets_4x_fetch(self) -> None:
        """knowledge__ collection is fetched with n_results * 4."""
        t3 = _RecordingT3()
        search_cross_corpus("q", ["knowledge__papers"], n_results=10, t3=t3)
        assert t3.calls["knowledge__papers"] == 40

    def test_code_gets_2x_fetch(self) -> None:
        """code__ collection is fetched with n_results * 2."""
        t3 = _RecordingT3()
        search_cross_corpus("q", ["code__nexus"], n_results=10, t3=t3)
        assert t3.calls["code__nexus"] == 20

    def test_docs_gets_4x_fetch(self) -> None:
        t3 = _RecordingT3()
        search_cross_corpus("q", ["docs__api"], n_results=10, t3=t3)
        assert t3.calls["docs__api"] == 40

    def test_rdr_gets_4x_fetch(self) -> None:
        t3 = _RecordingT3()
        search_cross_corpus("q", ["rdr__reviews"], n_results=10, t3=t3)
        assert t3.calls["rdr__reviews"] == 40

    def test_unknown_prefix_gets_2x_fetch(self) -> None:
        t3 = _RecordingT3()
        search_cross_corpus("q", ["custom__stuff"], n_results=10, t3=t3)
        assert t3.calls["custom__stuff"] == 20

    def test_per_k_not_divided_by_num_corpora(self) -> None:
        """Each corpus gets full n_results * mult, NOT divided by num corpora."""
        t3 = _RecordingT3()
        search_cross_corpus(
            "q",
            ["knowledge__a", "knowledge__b", "code__c"],
            n_results=10,
            t3=t3,
        )
        # knowledge → 4x; code → 2x; no division by 3
        assert t3.calls["knowledge__a"] == 40
        assert t3.calls["knowledge__b"] == 40
        assert t3.calls["code__c"] == 20

    def test_small_n_results_uses_floor_of_5(self) -> None:
        """max(5, n_results * mult) floor applies when product is tiny."""
        t3 = _RecordingT3()
        # n_results=1, code 2x → 2, but floor → 5
        search_cross_corpus("q", ["code__nexus"], n_results=1, t3=t3)
        assert t3.calls["code__nexus"] == 5

    def test_small_n_results_knowledge_floor(self) -> None:
        """knowledge 4x of n_results=1 → 4, floored to 5."""
        t3 = _RecordingT3()
        search_cross_corpus("q", ["knowledge__x"], n_results=1, t3=t3)
        assert t3.calls["knowledge__x"] == 5

    def test_normal_n_results_no_floor_needed(self) -> None:
        """n_results=5 with 2x → 10, well above floor."""
        t3 = _RecordingT3()
        search_cross_corpus("q", ["code__nexus"], n_results=5, t3=t3)
        assert t3.calls["code__nexus"] == 10
