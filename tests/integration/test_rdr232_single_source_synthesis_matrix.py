# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 section 1, the single-source synthesis rule (nexus-4xmpv; Sam, 2026-10-30).

A synthesis (or unknown) request that names exactly one source does not admit citation-traversal; abstract-themes stays allowed (Sam, 2026-10-30). This is the matrix method
of ``test_rdr232_lookup_only_matrix.py`` for the one rule that moved: the builtin plan templates are seeded
into the test tenant and the matcher runs over the library the way ``nx_answer`` runs it, with the filter built
for kind ``synthesis`` and the named-source count the first-pass derivation reads from the question. The control
arm gives the filter a count of 0, the rule absent. No model call.

What the offline library can and cannot show. With the bundled MiniLM embedder the seeded library does NOT
offer citation-traversal to G06 or to any of the six near-miss requests (it offers ``research:default``,
``analyze:default`` or nothing); the live runs that selected plan 467 (T2
``nexus/rdr232-close-measurements-2026-10-30``, rows 920, 923, 931) ran on Voyage embeddings over the live
library. So the first test pins that the seven requests are never offered it under the rule and that the rule
moves nothing else there, and the second uses a citation-shaped request that names one source, which the
offline library does offer it to, as the discriminating case: offered without the rule, removed with it. The
selection over a library that does offer plan 467 to the seven requests is pinned in
``tests/test_rdr232_eligibility.py`` (fake library, every request's real named-source count).

The request kind is passed as the extraction's reading (``synthesis``): four of the six near-miss requests read
as ``unknown`` on the first pass and become synthesis through the model-assisted extraction.
"""
from __future__ import annotations

import pytest

from bench.schema import load_golden_set
from tests import _rdr232_golden as g

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

NEAR_MISS_PATH = g.REPO / "bench" / "queries" / "golden_near_miss_synthesis_v1.yaml"
CITATION = "research:citation-traversal"
#: Names exactly one source and asks for the reference neighborhood the plan's own description offers.
CITATION_SHAPED = "What does RDR-084 cite, and what cites it? Trace the citation chain around RDR-084."


def _named(text: str) -> int:
    from nexus.plans.request_coverage import derive_request_coverage

    return len({(s.kind, s.value) for s in derive_request_coverage(text).named_sources})


def _select(library, text: str, count: int, scope: str = "", bindings=None):
    from nexus.mcp_infra import reset_plan_cache_for_tests
    from nexus.plans import eligibility as el

    reset_plan_cache_for_tests()
    filt = el.EligibilityFilter("synthesis", "model-assisted", enforce=True, named_source_count=count)
    return g.observe_plan_selection(library, text, scope=scope, bindings=bindings, eligibility=filt), filt


def test_the_seven_one_source_requests_are_never_offered_citation_traversal_and_nothing_else_moves(t2_service_env) -> None:
    from nexus.mcp_infra import reset_plan_cache_for_tests

    g06 = [q for q in load_golden_set(g.GOLDEN_V1_PATH) if q.qid.startswith("G06")]
    queries = g06 + list(load_golden_set(NEAR_MISS_PATH))
    assert [q.qid[:3] for q in queries] == ["G06", "S01", "S02", "S03", "S04", "S05", "S06"], "non-vacuity: 7 requests"
    counts = {q.qid: _named(q.text) for q in queries}
    assert set(counts.values()) == {1}, counts
    library = g.seed_builtin_plans()
    try:
        for q in queries:
            sc, bd = g.request_call_shape(q)
            control, _ = _select(library, q.text, 0, sc, bd)
            rule, _ = _select(library, q.text, counts[q.qid], sc, bd)
            assert CITATION not in rule.candidates and rule.selected != CITATION, q.qid
            assert set(control.candidates) - {CITATION} <= set(rule.candidates), q.qid
    finally:
        reset_plan_cache_for_tests()


def test_a_one_source_citation_shaped_synthesis_loses_citation_traversal_and_a_two_source_one_keeps_it(t2_service_env) -> None:
    from nexus.mcp_infra import reset_plan_cache_for_tests

    assert _named(CITATION_SHAPED) == 1
    library = g.seed_builtin_plans()
    try:
        control, _ = _select(library, CITATION_SHAPED, 0)
        assert CITATION in control.candidates, (
            "non-vacuity: without the rule the offline library offers citation-traversal here", control.candidates,
        )
        rule, filt = _select(library, CITATION_SHAPED, 1)
        assert CITATION not in rule.candidates and rule.selected != CITATION, rule.candidates
        # the request still gets a plan: the freed slot goes to the next eligible plan, never to nothing
        assert rule.candidates and set(control.candidates) - {CITATION} <= set(rule.candidates), rule.candidates
        wire = filt.to_wire()
        assert sum(b["reasons"].get("single_source_synthesis", 0) for b in wire["branches"].values()) >= 1, wire
        two, _ = _select(library, CITATION_SHAPED, 2)
        assert two.candidates == control.candidates, "a request naming two sources keeps the plan"
    finally:
        reset_plan_cache_for_tests()


ABSTRACT = "query:abstract-themes"
#: Names exactly one source and asks for the themes of the whole, the shape abstract-themes' description offers.
THEME_SHAPED = "What are the main themes and key findings across RDR-084? Give an overview of the whole document."


@pytest.mark.parametrize("kind", ["synthesis", "unknown"])
def test_a_one_source_theme_shaped_request_keeps_abstract_themes(t2_service_env, kind) -> None:
    # nexus-jypmj (Sam, 2026-10-30): the deny-list holds citation-traversal only; abstract-themes stays allowed for
    # a synthesis request and for an unknown one, whatever the named-source count.
    from nexus.mcp_infra import reset_plan_cache_for_tests
    from nexus.plans import eligibility as el

    assert _named(THEME_SHAPED) == 1
    library = g.seed_builtin_plans()

    def select(count: int):
        reset_plan_cache_for_tests()
        filt = el.EligibilityFilter(kind, "model-assisted", enforce=True, named_source_count=count)
        return g.observe_plan_selection(library, THEME_SHAPED, scope="", bindings=None, eligibility=filt), filt

    try:
        control, _ = select(0)
        assert ABSTRACT in control.candidates, (
            "non-vacuity: the offline library offers abstract-themes here", control.candidates,
        )
        one, _filt = select(1)
        assert ABSTRACT in one.candidates and one.candidates == control.candidates, (
            "one named source keeps abstract-themes and every other plan the control offered", one.candidates)
    finally:
        reset_plan_cache_for_tests()
