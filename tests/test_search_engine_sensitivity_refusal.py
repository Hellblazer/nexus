# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F5 (nexus-rfvyy.7.11): a refused ``exclude`` read is never a per-collection failure.

``SensitivityExcludeUnavailable`` is a ``VectorServiceError``, and the search fan-out catches that class per
collection (and per lexical leg) to isolate one bad collection from the rest. A refusal is not that: it says the
engine cannot be shown to filter sensitive chunks, so it holds for every collection, and a probe that flaps
mid-search must not leave a partial result that looks complete. These tests drive ``search_cross_corpus`` with a
T3 stub that refuses and require the refusal itself to come out, not partial rows, not "all collections failed",
and not ``LexicalLegUnavailableError`` with its "retry without --lexical" advice.
"""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import SensitivityExcludeUnavailable
from nexus.search_engine import LexicalLegUnavailableError, search_cross_corpus

_REFUSAL = "sensitivity='exclude' read refused: the engine does not report vector_sensitivity_exclude_supported"
_ROW = {"id": "a" * 64, "content": "plain text", "distance": 0.1, "metadata": {}}


class _T3:
    """Serves ``knowledge__ok``, refuses an exclude read of any batch that names ``knowledge__flaky``."""

    supports_hybrid_search = True

    def __init__(self, *, refuse_search: bool = True, refuse_lexical: bool = False) -> None:
        self.refuse_search, self.refuse_lexical = refuse_search, refuse_lexical
        self.search_calls: list[list[str]] = []

    def search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include", **_kw):
        self.search_calls.append(list(collection_names))
        if self.refuse_search and "knowledge__flaky" in collection_names:
            raise SensitivityExcludeUnavailable(_REFUSAL)
        return [dict(_ROW, collection=c) for c in collection_names if c == "knowledge__ok"]

    def hybrid_search(self, query, collection_names, n_results=10, where=None, *, sensitivity="include", **_kw):
        if self.refuse_lexical:
            raise SensitivityExcludeUnavailable(_REFUSAL)
        return []


def test_a_single_refused_collection_surfaces_the_refusal():
    with pytest.raises(SensitivityExcludeUnavailable, match="read refused"):
        search_cross_corpus("q", ["knowledge__flaky"], 5, _T3(), threshold_override=float("inf"), sensitivity="exclude")


def test_a_refusal_in_one_collection_of_a_batch_leaves_no_partial_result():
    t3 = _T3()
    with pytest.raises(SensitivityExcludeUnavailable):
        search_cross_corpus(
            "q", ["knowledge__ok", "knowledge__flaky"], 5, t3, threshold_override=float("inf"), sensitivity="exclude",
        )
    assert any("knowledge__flaky" in c for c in t3.search_calls), "the refusing batch was reached"


def test_the_control_a_healthy_collection_still_answers():
    rows = search_cross_corpus("q", ["knowledge__ok"], 5, _T3(), threshold_override=float("inf"), sensitivity="exclude")
    assert rows, "the stub answers for knowledge__ok: the tests above are not vacuous"


def test_a_refused_lexical_leg_is_the_refusal_not_a_retry_without_lexical_hint():
    t3 = _T3(refuse_search=False, refuse_lexical=True)
    with pytest.raises(SensitivityExcludeUnavailable) as excinfo:
        search_cross_corpus(
            "q", ["knowledge__ok"], 5, t3, threshold_override=float("inf"), lexical=True, sensitivity="exclude",
        )
    assert not isinstance(excinfo.value, LexicalLegUnavailableError)
    assert "retry without" not in str(excinfo.value)
