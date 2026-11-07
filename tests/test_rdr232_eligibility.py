# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 (nexus-jypmj.2.3): answer-kind eligibility on every matcher branch, the planner's
few-shots, and the inline planner.

Tenant-free. The matcher runs over fake plan libraries and caches (the pattern of
``test_plan_match_category_route.py``); the ``nx_answer`` tests drive the real matcher through the
``nexus.plans.matcher.plan_match`` patch point, so the wiring is exercised and not stubbed away.

The gate is ON by default since nexus-jypmj.2.8 (module constant ``ANSWER_KIND_ELIGIBILITY_ENABLED`` is the
default, ``NX_ANSWER_KIND_ELIGIBILITY=0`` the kill switch). The scenario tests use the ``enabled`` fixture (kept
from when the default was off; harmless now); the shadow tests set the kill switch (``shadow`` fixture) and pin
that selection does not move. Defaults, kill switches and the library precondition are in
``tests/test_rdr232_gate_defaults.py``.
"""
from __future__ import annotations

import json
from itertools import product
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans import eligibility as el
from nexus.plans.answer_kind import ANSWER_KIND_KEY, PlanAnswerKind, declaration_payload
from nexus.plans.matcher import plan_match as real_plan_match
from nexus.plans.match import Match

K = PlanAnswerKind
L, S, D, C, A, B, U = (
    K.LOOKUP, K.SYNTHESIS, K.DISCOVERY_LISTING, K.COMPARISON, K.AUDIT, K.DEBUG, K.UNKNOWN,
)

G06 = (
    "Synthesize RDR-084, LIST PARSE Canonical Consolidation. State the locked architecture and numerical "
    "conventions, the evidence, the gate history, the implementation state, and what is open by design."
)
LOOKUP_Q = "Which paper reported the value 0.82 for the cosine floor?"


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(el, "ANSWER_KIND_ELIGIBILITY_ENABLED", True)


@pytest.fixture
def shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(el.ANSWER_KIND_ELIGIBILITY_ENV, "0")


async def _extraction_that_works(prompt, schema, **kw):  # noqa: ARG001
    """A structured extraction that answers inside the schema and adds nothing (outcome ``unchanged``)."""
    return {"answer_kind": "unknown", "facets": [], "constraints": []}


@pytest.fixture
def working_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite-wide guard makes every extraction call fail, and a request whose extraction failed runs its gate in
    shadow (nexus-x3eoc). A test that needs the gate ENFORCING on a request the first pass leaves open asks for this."""
    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _extraction_that_works)


# ── the matrix, as a table over every cell ───────────────────────────────────────────────────────────────

#: RDR-232 section 1, "Request kind -> Eligible declared plan kinds", written out here independently of the
#: module under test. A curated plan, or a grown plan that recorded its route kind, is looked up in it.
_MATRIX: dict[K, frozenset[K]] = {
    # nexus-jypmj.11, decided by Sam 2026-10-30: a lookup admits lookup plans only (no synthesis plan)
    L: frozenset({L, U}),
    S: frozenset({S, U}),
    D: frozenset({D, U}),
    C: frozenset({C, S, U}),
    A: frozenset({A, S, U}),
    B: frozenset({B, S, U}),
}


def _expected(request: K, plan: K, provenance: str) -> bool:
    """The RDR's rule for one cell, from its prose, not from the module."""
    if request is U:
        # "curated synthesis, curated unknown only; otherwise inline planning"
        return provenance == "curated" and plan in (S, U)
    if provenance == "unattested":
        plan = U  # nothing was declared, so nothing known: it is an unknown plan
    if provenance == "saved":
        # nexus-0vh80 (Sam, 2026-11-03): a saved plan serves only its OWN declared kind, and no unknown-plan row
        return plan is not U and plan is request
    if plan is not U:
        return plan in _MATRIX[request]
    if provenance == "curated":
        return True  # "Unknown-kind builtin/curated plans remain eligible as in the compatibility matrix"
    # "Unknown-kind grown plans ... eligible only for lookup or discovery/listing requests"
    if provenance == "unattested":
        return False  # nexus-rflvp (Sam, 2026-11-03): a plan that declares nothing serves neither of them either
    return request in (L, D)


_ALL_KINDS = list(K)
_PROVENANCES = ("curated", "grown", "unattested", "saved")


@pytest.mark.parametrize(
    ("request_kind", "plan_kind", "provenance"), list(product(_ALL_KINDS, _ALL_KINDS, _PROVENANCES)),
    ids=lambda v: v.value if isinstance(v, K) else v,
)
def test_the_matrix_cell(request_kind: K, plan_kind: K, provenance: str) -> None:
    got = el.eligibility(request_kind, plan_kind, provenance)
    assert got.eligible is _expected(request_kind, plan_kind, provenance), (request_kind, plan_kind, provenance)
    assert (got.reason is None) is got.eligible, "a reason is given exactly when a plan is refused"


def test_the_table_has_every_cell_and_the_expected_side_is_not_all_one_way() -> None:
    cells = list(product(_ALL_KINDS, _ALL_KINDS, _PROVENANCES))
    assert len(cells) == 7 * 7 * 4 == 196
    admitted = sum(_expected(*c) for c in cells)
    # 42 admitted before nexus-rflvp (an unattested plan served lookups and listings): 28 after it, 14 cells fewer;
    # the saved column (nexus-0vh80) adds the six known kinds on their own row: 34
    assert admitted == 34, f"{admitted} of 196 admitted: a matrix cell changed"


def test_a_refusal_names_why_in_the_closed_vocabulary() -> None:
    reasons = {
        el.eligibility(r, p, v).reason for r, p, v in product(_ALL_KINDS, _ALL_KINDS, _PROVENANCES)
    } - {None}
    assert reasons == {
        "kind_mismatch", "unknown_request_untrusted_plan", "unknown_request_narrow_plan",
        "untrusted_unknown_plan", "unattested_plan", "saved_unknown_plan",
    }
    assert reasons <= el.REASONS


def test_the_two_named_rdr_cells() -> None:
    # plan 473: a curated lookup plan; the incident request is a synthesis
    assert not el.eligibility(S, L, "curated").eligible
    # the other direction is closed since nexus-jypmj.11 (Sam, 2026-10-30): a lookup runs a lookup plan
    assert not el.eligibility(L, S, "curated").eligible
    assert el.eligibility(L, L, "curated").eligible
    # an unknown request never reaches a narrow curated plan nor any grown plan
    assert not el.eligibility(U, L, "curated").eligible
    assert not el.eligibility(U, U, "grown").eligible


# ── provenance: decided from the stored row, failing closed ──────────────────────────────────────────────


def _pj(
    kind: K | None = None, provenance: str = "curated", route: str | None = None, steps: list | None = None,
) -> str:
    plan: dict = {"steps": steps if steps is not None else [{"tool": "search", "args": {"query": "$intent"}}]}
    if kind is not None:
        plan[ANSWER_KIND_KEY] = declaration_payload(kind, provenance, route)
    return json.dumps(plan)


@pytest.mark.parametrize(
    ("tags", "plan_json", "kind", "provenance", "flag"),
    [
        pytest.param("builtin-template,rdr-097", _pj(L), L, "curated", None, id="builtin-with-a-curated-declaration"),
        pytest.param("builtin-template", _pj(), U, "unattested", "undeclared_builtin",
                     id="legacy-builtin-seeded-before-2.1-declares-nothing"),
        pytest.param("", _pj(L), U, "grown", "untrusted_curated_claim",
                     id="a-curated-declaration-without-the-builtin-tag-is-not-trusted"),
        pytest.param("ad-hoc", _pj(S), U, "grown", "untrusted_curated_claim", id="a-curated-claim-on-an-adhoc-row"),
        # nexus-3ihve: a grown TAG alone is not attestation. Without a valid grown declaration the row is
        # unattested (it was read as grown before; plan 517 is such a row), whatever else it carries.
        pytest.param("builtin-template,grown", _pj(L), U, "unattested", None,
                     id="a-grown-tag-over-a-curated-declaration-is-unattested"),
        pytest.param("ad-hoc,grown", _pj(), U, "unattested", None, id="grown-tag-no-declaration-is-unattested"),
        pytest.param("grown", _pj(), U, "unattested", None, id="the-bare-grown-tag-is-unattested"),
        pytest.param("builtin-template,grown", _pj(), U, "unattested", None,
                     id="grown-and-builtin-tags-no-declaration-is-unattested-not-undeclared-builtin"),
        pytest.param("ad-hoc,grown", json.dumps({"steps": [], ANSWER_KIND_KEY: {"version": 1, "kind": "lookup"}}),
                     U, "unattested", None, id="a-grown-tag-with-an-invalid-declaration-is-unattested"),
        pytest.param("ad-hoc,grown", "{not json", U, "unattested", None, id="a-grown-tag-with-malformed-json"),
        pytest.param("ad-hoc,grown", _pj(U, "grown"), U, "grown", None, id="grown-unknown"),
        pytest.param("ad-hoc,grown", _pj(S, "grown", "explicit"), U, "grown", "unreviewed_route",
                     id="grown-with-an-explicit-route-reads-as-unknown-no-route-is-trusted-yet"),
        pytest.param("ad-hoc,grown", _pj(S, "grown", "heuristic"), U, "grown", "unreviewed_route",
                     id="grown-with-a-heuristic-route-reads-as-unknown"),
        pytest.param("ad-hoc,grown", _pj(S, "grown", "model-assisted"), U, "grown", "unreviewed_route",
                     id="grown-with-a-model-assisted-route-reads-as-unknown"),
        pytest.param("", _pj(S, "grown", "explicit"), U, "grown", "unreviewed_route",
                     id="the-declaration-alone-says-grown"),
        pytest.param("builtin-template", _pj(S, "grown", "heuristic"), U, "grown", "unreviewed_route",
                     id="builtin-tag-but-a-grown-declaration-is-grown"),
        pytest.param("", _pj(), U, "unattested", None, id="a-plan-save-row-declares-nothing"),
        pytest.param(None, None, U, "unattested", None, id="no-tags-no-json"),
        pytest.param("builtin-template", "{not json", U, "unattested", "undeclared_builtin", id="malformed-json"),
        pytest.param("builtin-template",
                     json.dumps({"steps": [], ANSWER_KIND_KEY: {"version": 1, "kind": "lookup"}}), U, "unattested",
                     "undeclared_builtin", id="an-invalid-declaration-is-no-declaration"),
        # nexus-0vh80 (Sam, 2026-11-03): a saved declaration is its own provenance, whatever tags ride with it.
        pytest.param("", _pj(L, "saved"), L, "saved", None, id="a-saved-declaration-reads-saved-with-its-kind"),
        pytest.param("research,notes", _pj(D, "saved"), D, "saved", None, id="saved-with-ordinary-tags"),
        pytest.param("builtin-template", _pj(S, "saved"), S, "saved", None,
                     id="a-project-tier-builtin-tagged-plan-reads-saved-never-curated"),
        pytest.param("", _pj(U, "saved"), U, "saved", None, id="saved-unknown"),
        pytest.param("grown", _pj(L, "saved"), U, "unattested", None, id="a-grown-tag-over-a-saved-declaration-attests-nothing"),
        pytest.param("", json.dumps({"steps": [], ANSWER_KIND_KEY: {"version": 1, "kind": "lookup", "provenance": "saved",
                                                                   "route": "explicit"}}),
                     U, "unattested", None, id="a-saved-declaration-with-a-route-is-invalid"),
    ],
)
def test_provenance_is_decided_from_the_row_and_fails_closed(tags, plan_json, kind, provenance, flag) -> None:
    standing = el.classify_plan(tags, plan_json)
    assert (standing.kind, standing.provenance, standing.flag) == (kind, provenance, flag)


@pytest.mark.parametrize(
    ("route", "honoured"),
    [("explicit", False), ("heuristic", False), ("model-assisted", False)],
    ids=["explicit", "heuristic", "model-assisted"],
)
def test_a_grown_plans_recorded_kind_counts_on_no_route_until_one_is_trusted(route, honoured) -> None:
    """RDR-232: recorded kind provenance relaxes the unknown-kind restriction "after review". No review has
    happened (nexus-jypmj.2.8), so ``TRUSTED_ROUTES`` ships empty and EVERY grown plan reads as unknown-kind,
    the caller's ``explicit`` hint included: it labels the request, and nothing checks it against the plan's
    steps. (A row recording a kind with no route is not a valid declaration: it decodes as none, and since
    nexus-3ihve a grown tag over it reads unattested, pinned in the provenance table above.)"""
    plan_json = _pj(S, "grown", route)
    standing = el.classify_plan("ad-hoc,grown", plan_json)
    assert not honoured and el.TRUSTED_ROUTES == frozenset()
    assert standing.provenance == "grown"
    assert standing.kind is U
    assert standing.flag == "unreviewed_route"
    assert not el.eligibility(S, standing.kind, standing.provenance).eligible
    # either way the stored route is reported, so the envelope can say why
    assert standing.route == route


@pytest.mark.parametrize("route", ["explicit", "heuristic", "model-assisted"])
def test_adding_a_route_to_the_trusted_set_makes_that_route_count(route, monkeypatch) -> None:
    """The mechanism a reviewed decision will use: a route in ``TRUSTED_ROUTES`` keeps its recorded kind, the
    others stay unknown."""
    monkeypatch.setattr(el, "TRUSTED_ROUTES", frozenset({route}))
    for other in ("explicit", "heuristic", "model-assisted"):
        standing = el.classify_plan("ad-hoc,grown", _pj(S, "grown", other))
        if other == route:
            assert (standing.kind, standing.flag, standing.route) == (S, None, other)
            assert el.eligibility(S, standing.kind, standing.provenance).eligible
        else:
            assert (standing.kind, standing.flag) == (U, "unreviewed_route")


@pytest.mark.parametrize("route", ["explicit", "heuristic", "model-assisted"])
def test_a_discounted_route_is_refused_with_its_own_reason_and_still_serves_listing(route) -> None:
    """A lookup is no longer served by it: the recorded synthesis kind narrows (nexus-indyo)."""
    row = _row(910, "grown-synth", kind=S, tags="ad-hoc,grown", provenance="grown", route=route)
    out, filt = _match([row], [(910, 0.05)], S)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unreviewed_route": 1}
    out, _ = _match([row], [(910, 0.05)], D)
    assert _ids(out) == [910]
    out, filt = _match([row], [(910, 0.05)], L)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_not_lookup": 1}


@pytest.mark.parametrize("tags", [42, ["builtin-template"], MagicMock()], ids=["int", "list", "mock"])
def test_a_row_whose_tags_are_not_text_is_unattested_and_never_raises(tags) -> None:
    standing = el.classify_plan(tags, MagicMock())
    assert (standing.kind, standing.provenance, standing.flag) == (U, "unattested", None)


def test_session_cache_metadata_is_not_consulted() -> None:
    """The cache's ``answer_provenance`` metadata is stale until a refresh. The classifier takes the stored row's
    tags and plan_json and nothing else, so a row that changed under the cache is read as it now stands."""
    import inspect

    params = list(inspect.signature(el.classify_plan).parameters)
    assert params == ["tags", "plan_json"]


# ── the matcher: fake library and cache ──────────────────────────────────────────────────────────────────


class _Cache:
    is_available = True

    def __init__(self, hits):
        self.hits = hits
        self.asked: list[int] = []

    def query(self, intent, n):  # noqa: ARG002
        self.asked.append(n)
        return self.hits[:n]

    def remove(self, plan_id):  # noqa: ARG002
        pass


class _Library:
    def __init__(self, rows, fts=()):
        self.rows = {r["id"]: r for r in rows}
        self.fts = list(fts)
        self.metrics: list = []
        self.fts_limits: list[int] = []

    def get_plan(self, plan_id):
        return self.rows.get(plan_id)

    def increment_match_metrics(self, plan_id, confidence=None):
        self.metrics.append((plan_id, confidence))

    def search_plans(self, intent, limit=10, project=""):  # noqa: ARG002
        self.fts_limits.append(limit)
        return [self.rows[i] for i in self.fts]


def _row(
    pid, name, *, kind=None, tags="builtin-template", provenance="curated", route=None, verb="research",
    strategy=None, steps=None, project="nexus",
):
    dims = {"scope": "global", "verb": verb}
    if strategy is not None:
        dims["strategy"] = strategy
    return {
        "id": pid, "name": name, "query": f"Description for {name}.", "plan_json": _pj(kind, provenance, route, steps),
        "tags": tags, "verb": verb, "scope": "global",
        "dimensions": json.dumps(dims), "scope_tags": "", "project": project,
        "success_count": 1, "failure_count": 0,
    }


#: Plan 473 as the incident selected it: a builtin lookup plan.
LOOKUP_473 = _row(473, "hybrid-factual-lookup", kind=L, tags="builtin-template,rdr-097", verb="lookup")
SYNTH_12 = _row(12, "research-default", kind=S, tags="builtin-template,rdr-097", verb="research")
DISC_13 = _row(13, "document-discovery", kind=D, tags="builtin-template", verb="discovery")
#: A grown plan whose route was unknown: lookup-shaped history, declared unknown.
GROWN_UNKNOWN = _row(901, "grown-lookup", kind=U, tags="ad-hoc,grown", provenance="grown", verb="lookup")
GROWN_LOOKUP = _row(902, "grown-lookup-recorded", kind=L, tags="ad-hoc,grown", provenance="grown", route="explicit",
                    verb="lookup")
#: The same plan stamped by the heuristic route: the request's guess, discounted until reviewed.
GROWN_LOOKUP_HEURISTIC = _row(903, "grown-lookup-heuristic", kind=L, tags="ad-hoc,grown", provenance="grown",
                              route="heuristic", verb="lookup")
LEGACY_BUILTIN = _row(14, "legacy-default", kind=None, tags="builtin-template", verb="research")


def _filter(request: K, *, enforce: bool, decision: str = "heuristic", project: str = "") -> el.EligibilityFilter:
    return el.EligibilityFilter(request_kind=request, decision=decision, enforce=enforce, project=project)


def _ids(matches) -> list[int]:
    return [m.plan_id for m in matches]


def _match(
    rows, hits, request: K, *, enforce=True, fts=(), cache=True, category_verb=None, n=5, request_project="", **kw,
):
    lib = _Library(rows, fts)
    filt = _filter(request, enforce=enforce, project=request_project)
    out = real_plan_match(
        "q", library=lib, cache=_Cache(hits) if cache else None, eligibility=filt, category_verb=category_verb, n=n,
        **kw,
    )
    return out, filt


# Scenario 1: the observed multi-facet RDR-084 synthesis -----------------------------------------------------


def test_scenario_1_plan_473_is_ineligible_for_the_synthesis_request() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S)
    assert _ids(out) == [12], "473 is the better cosine, and it is still refused"
    assert filt.to_wire()["branches"]["cosine"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert filt.excluded_plan_ids == [473]


# Scenario 2: lookup plans keep matching ---------------------------------------------------------------------


def test_scenario_2_an_eligible_lookup_plan_still_matches() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], L)
    assert _ids(out) == [473], "a lookup runs a lookup plan only (nexus-jypmj.11); the synthesis plan is refused"
    assert filt.excluded_plan_ids == [12]
    assert filt.to_wire()["branches"]["cosine"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}


#: Plan 467 as the live H09 run selected it: a builtin synthesis plan (citation-traversal) that scored better
#: than the lookup plan, ran 1.24 USD against 0.14 for 473, and carried unrelated code files.
CITATION_467 = _row(
    467, "citation-traversal", kind=S, tags="builtin-template,rdr-097", verb="research", strategy="citation-traversal",
)


def test_a_lookup_that_a_synthesis_plan_outscores_still_gets_the_lookup_plan() -> None:
    out, _ = _match([CITATION_467, LOOKUP_473], [(467, 0.05), (473, 0.3)], L)
    assert _ids(out) == [473], "H09 (nexus-jypmj.11): the better-scoring synthesis plan is not offered"


def test_a_lookup_no_lookup_plan_matches_selects_nothing_so_inline_planning_follows() -> None:
    out, filt = _match([CITATION_467, SYNTH_12], [(467, 0.05), (12, 0.3)], L, fts=[467, 12])
    assert out == [], "nothing is forced; the caller plans inline (cost not measured, see the RDR)"
    assert filt.excluded_total >= 2


# nexus-4xmpv: a synthesis request that names exactly one source does not admit citation-traversal -------------


def _single_source_requests() -> list[str]:
    """G06 and the six near-miss requests (bench/queries), each of which names exactly one RDR."""
    import yaml

    from tests._rdr232_golden import REPO

    near_miss = yaml.safe_load((REPO / "bench" / "queries" / "golden_near_miss_synthesis_v1.yaml").read_text())
    return [G06] + [q["text"] for q in near_miss["queries"]]


def _named(text: str) -> int:
    from nexus.plans.request_coverage import derive_request_coverage

    return len({(s.kind, s.value) for s in derive_request_coverage(text).named_sources})


def test_the_request_set_names_one_source_each_and_is_not_empty() -> None:
    texts = _single_source_requests()
    assert len(texts) == 7, "G06 + S01..S06"
    assert [_named(t) for t in texts] == [1] * 7


@pytest.mark.parametrize("text", _single_source_requests())
def test_a_one_source_synthesis_request_is_not_offered_citation_traversal(text: str) -> None:
    # S01/S03/S05/S06 read as `unknown` on the first pass and become synthesis only through the model-assisted
    # extraction (rows 919, 921, 923, 924); the kind is passed as the extraction's reading, the count is derived.
    n = _named(text)
    filt = el.EligibilityFilter(S, "model-assisted", enforce=True, named_source_count=n)
    out = real_plan_match("q", library=_Library([CITATION_467, SYNTH_12]), cache=_Cache([(467, 0.05), (12, 0.4)]),
                          eligibility=filt, n=5)
    assert _ids(out) == [12], "the better-scoring citation-traversal plan is refused; the other synthesis plan stays"
    assert filt.excluded_plan_ids == [467]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"single_source_synthesis": 1}


def test_a_one_source_synthesis_with_no_other_plan_selects_nothing_so_inline_planning_follows() -> None:
    filt = el.EligibilityFilter(S, "model-assisted", enforce=True, named_source_count=1)
    out = real_plan_match("q", library=_Library([CITATION_467], fts=[467]), cache=_Cache([(467, 0.05)]),
                          eligibility=filt, n=5)
    assert out == []
    assert filt.to_wire()["branches"]["fts"]["reasons"] == {"single_source_synthesis": 1}
    assert el.fallback_reason(force_dynamic=False, matches=out, hit=False, filt=filt) == "all_candidates_ineligible"


@pytest.mark.parametrize("named", [0, 2, 3])
def test_a_synthesis_request_naming_no_source_or_several_may_still_use_citation_traversal(named: int) -> None:
    filt = el.EligibilityFilter(S, "heuristic", enforce=True, named_source_count=named)
    out = real_plan_match("q", library=_Library([CITATION_467, SYNTH_12]), cache=_Cache([(467, 0.05), (12, 0.4)]),
                          eligibility=filt, n=5)
    assert _ids(out)[0] == 467 and filt.excluded_plan_ids == []


def test_two_sources_named_in_the_question_count_as_two() -> None:
    assert _named("Compare how RDR-084 and RDR-180 handle the gate history.") == 2
    assert _named("Synthesize RDR-084, then RDR-084 again, as a whole.") == 1, "one source named twice is one source"


def test_a_lookup_is_unaffected_by_the_single_source_rule() -> None:
    # a lookup never admitted a synthesis plan; the reason stays kind_mismatch and its plan is still selected
    filt = el.EligibilityFilter(L, "heuristic", enforce=True, named_source_count=1)
    out = real_plan_match("q", library=_Library([CITATION_467, LOOKUP_473]),
                          cache=_Cache([(467, 0.05), (473, 0.3)]), eligibility=filt, n=5)
    assert _ids(out) == [473]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"kind_mismatch": 1}


@pytest.mark.parametrize("request_kind", [C, A, B])
def test_the_other_kinds_that_admit_synthesis_plans_keep_citation_traversal(request_kind: K) -> None:
    filt = el.EligibilityFilter(request_kind, "heuristic", enforce=True, named_source_count=1)
    out = real_plan_match("q", library=_Library([CITATION_467]), cache=_Cache([(467, 0.05)]), eligibility=filt, n=5)
    assert _ids(out) == [467], "nexus-4xmpv is decided for a synthesis request only"


def test_the_rule_reads_the_declared_strategy_not_the_plan_id_or_name() -> None:
    renumbered = _row(9001, "citation-traversal-v2", kind=S, tags="builtin-template", verb="research",
                      strategy="citation-traversal")
    namesake = _row(467, "citation-traversal", kind=S, tags="builtin-template", verb="research")  # no strategy
    for row, excluded in ((renumbered, True), (namesake, False)):
        filt = el.EligibilityFilter(S, "model-assisted", enforce=True, named_source_count=1)
        out = real_plan_match("q", library=_Library([row]), cache=_Cache([(row["id"], 0.05)]), eligibility=filt, n=5)
        assert (out == []) is excluded, row["name"]


def test_a_grown_plan_cannot_claim_the_strategy_to_change_its_standing() -> None:
    # a grown plan is judged by provenance first (unknown kind, refused for synthesis); the strategy rule is for
    # curated rows, so it never turns an eligible grown plan into a different reason
    grown = _row(905, "grown-cite", kind=U, tags="ad-hoc,grown", provenance="grown", verb="research",
                 strategy="citation-traversal")
    filt = el.EligibilityFilter(S, "model-assisted", enforce=True, named_source_count=1)
    real_plan_match("q", library=_Library([grown]), cache=_Cache([(905, 0.05)]), eligibility=filt, n=5)
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"untrusted_unknown_plan": 1}


def test_the_planner_few_shots_leave_citation_traversal_out_for_a_one_source_synthesis() -> None:
    def match(row) -> Match:
        return Match(plan_id=row["id"], name=row["name"], description="d", confidence=0.5,
                     dimensions=json.loads(row["dimensions"]), tags=row["tags"], plan_json=row["plan_json"],
                     required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None)

    both = [match(CITATION_467), match(SYNTH_12)]
    assert [m.plan_id for m in el.eligible_matches(S, both, 1)] == [12]
    assert [m.plan_id for m in el.eligible_matches(S, both, 2)] == [467, 12]
    assert [m.plan_id for m in el.eligible_matches(S, both)] == [467, 12]


def test_nx_answer_wires_the_named_source_count_g06_gets_the_other_plan_and_a_bare_467_goes_inline() -> None:
    env, miss, seen = _drive(G06, [CITATION_467, SYNTH_12], [(467, 0.05), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True and wire["request_kind"] == "synthesis"
    assert wire["excluded_plan_ids"] == [467] and wire["selected"]["plan_id"] == 12
    assert wire["branches"]["cosine"]["reasons"] == {"single_source_synthesis": 1}
    miss.assert_not_awaited()
    env, miss, _ = _drive(G06, [CITATION_467], [(467, 0.05)], fts=[467])
    assert env["plan_eligibility"]["fallback_reason"] == "all_candidates_ineligible"
    miss.assert_awaited()


# nexus-jypmj (Sam, 2026-10-30): the deny-list holds citation-traversal only; abstract-themes stays allowed, and the
# rule covers an `unknown` request too -------------------------------------------------------------------------

#: Plan 465 as seeded: a builtin synthesis plan, ``verb: query``, ``strategy: abstract-themes``.
ABSTRACT_465 = _row(
    465, "abstract-themes", kind=S, tags="builtin-template,rdr-098", verb="query", strategy="abstract-themes",
)


def test_the_deny_list_holds_citation_traversal_only_and_exactly_one_shipped_template_declares_it() -> None:
    import yaml

    from tests._rdr232_golden import BUILTIN_PLANS

    assert el.SINGLE_SOURCE_EXCLUDED_STRATEGIES == frozenset({"citation-traversal"})
    declared = {}
    dims_of = {}
    for path in sorted(BUILTIN_PLANS.glob("*.yml")):
        dims = (yaml.safe_load(path.read_text()) or {}).get("dimensions") or {}
        dims_of[path.stem] = dims
        declared[path.stem] = dims.get("strategy")
    assert len(declared) >= 10, f"non-vacuity: {len(declared)} templates read"
    hit = sorted(n for n, strategy in declared.items() if strategy in el.SINGLE_SOURCE_EXCLUDED_STRATEGIES)
    assert hit == ["citation-traversal"], "no plan that should stay eligible shares a strategy value"
    # the fixture row carries the dimensions the shipped template declares
    shipped = json.loads(ABSTRACT_465["dimensions"])
    assert {k: shipped[k] for k in ("verb", "strategy")} == {k: dims_of["abstract-themes"][k] for k in ("verb", "strategy")}


@pytest.mark.parametrize("text", _single_source_requests())
@pytest.mark.parametrize("kind", [S, U], ids=["synthesis", "unknown"])
def test_a_one_source_request_is_refused_citation_traversal_and_keeps_abstract_themes(text: str, kind: K) -> None:
    # G06, S02 and S05 ran 467 after the deny-list was removed; S01, S03, S05, S06 read as unknown when extraction fails.
    filt = el.EligibilityFilter(kind, "model-assisted", enforce=True, named_source_count=_named(text))
    out = real_plan_match("q", library=_Library([CITATION_467, ABSTRACT_465, SYNTH_12]),
                          cache=_Cache([(467, 0.05), (465, 0.1), (12, 0.4)]), eligibility=filt, n=5)
    assert _ids(out) == [465, 12], "citation-traversal is refused though it outscores; abstract-themes stays offered"
    assert filt.excluded_plan_ids == [467]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"single_source_synthesis": 1}


@pytest.mark.parametrize("kind", [S, U], ids=["synthesis", "unknown"])
def test_a_one_source_request_with_only_citation_traversal_goes_inline(kind: K) -> None:
    filt = el.EligibilityFilter(kind, "model-assisted", enforce=True, named_source_count=1)
    out = real_plan_match("q", library=_Library([CITATION_467], fts=[467]), cache=_Cache([(467, 0.05)]),
                          eligibility=filt, n=5)
    assert out == []
    assert filt.to_wire()["branches"]["fts"]["reasons"] == {"single_source_synthesis": 1}
    assert el.fallback_reason(force_dynamic=False, matches=out, hit=False, filt=filt) == "all_candidates_ineligible"


@pytest.mark.parametrize("named", [0, 1, 2, 3])
@pytest.mark.parametrize("kind", [S, U], ids=["synthesis", "unknown"])
def test_abstract_themes_is_admitted_whatever_the_named_source_count(kind: K, named: int) -> None:
    filt = el.EligibilityFilter(kind, "heuristic", enforce=True, named_source_count=named)
    out = real_plan_match("q", library=_Library([ABSTRACT_465, SYNTH_12]), cache=_Cache([(465, 0.05), (12, 0.4)]),
                          eligibility=filt, n=5)
    assert _ids(out)[0] == 465 and filt.excluded_plan_ids == []


@pytest.mark.parametrize("named", [0, 2, 3])
@pytest.mark.parametrize("kind", [S, U], ids=["synthesis", "unknown"])
def test_a_request_naming_no_source_or_several_still_admits_citation_traversal(kind: K, named: int) -> None:
    filt = el.EligibilityFilter(kind, "heuristic", enforce=True, named_source_count=named)
    out = real_plan_match("q", library=_Library([CITATION_467, SYNTH_12]), cache=_Cache([(467, 0.05), (12, 0.4)]),
                          eligibility=filt, n=5)
    assert _ids(out)[0] == 467 and filt.excluded_plan_ids == []


def test_an_unknown_request_still_admits_a_curated_synthesis_plan_when_it_names_one_source() -> None:
    filt = el.EligibilityFilter(U, "model-assisted", enforce=True, named_source_count=1)
    out = real_plan_match("q", library=_Library([SYNTH_12]), cache=_Cache([(12, 0.4)]), eligibility=filt, n=5)
    assert _ids(out) == [12] and filt.excluded_plan_ids == []


def test_the_planner_few_shots_leave_citation_traversal_out_for_a_one_source_unknown_request() -> None:
    def match(row) -> Match:
        return Match(plan_id=row["id"], name=row["name"], description="d", confidence=0.5,
                     dimensions=json.loads(row["dimensions"]), tags=row["tags"], plan_json=row["plan_json"],
                     required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None)

    rows = [match(CITATION_467), match(ABSTRACT_465), match(SYNTH_12)]
    for kind in (S, U):
        assert [m.plan_id for m in el.eligible_matches(kind, rows, 1)] == [465, 12], kind
        assert [m.plan_id for m in el.eligible_matches(kind, rows, 2)] == [467, 465, 12], kind


def test_nx_answer_keeps_abstract_themes_for_a_one_source_request(enabled) -> None:
    env, miss, _ = _drive(G06, [ABSTRACT_465, SYNTH_12], [(465, 0.05), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["excluded_plan_ids"] == [] and wire["selected"]["plan_id"] == 465
    miss.assert_not_awaited()


# Scenario 3: no plan has a compatible shape ------------------------------------------------------------------


def test_scenario_3_no_compatible_plan_returns_nothing_and_never_a_known_mismatch() -> None:
    out, filt = _match([LOOKUP_473], [(473, 0.05)], S, fts=[473])
    assert out == [], "inline planning follows; the lookup plan is not forced"
    wire = filt.to_wire()
    assert wire["branches"]["cosine"]["excluded"] == 1 and wire["branches"]["fts"]["excluded"] == 1
    assert wire["excluded_total"] == 2


# Scenario 4: unknown-kind grown plan with lookup-shaped history and a synthesis request ----------------------


def test_scenario_4_an_unknown_grown_plan_is_not_selected_for_a_synthesis_request() -> None:
    out, filt = _match([GROWN_UNKNOWN, SYNTH_12], [(901, 0.05), (12, 0.45)], S)
    assert _ids(out) == [12]
    assert filt.excluded_plan_ids == [901]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"untrusted_unknown_plan": 1}


def test_scenario_4_the_same_grown_plan_still_serves_a_lookup_and_a_listing() -> None:
    for request in (L, D):
        out, _ = _match([GROWN_UNKNOWN], [(901, 0.05)], request)
        assert _ids(out) == [901], request


@pytest.mark.parametrize("request_kind", [S, C, A, B])
def test_scenario_4_it_is_refused_for_every_broad_kind(request_kind: K) -> None:
    out, _ = _match([GROWN_UNKNOWN], [(901, 0.05)], request_kind)
    assert out == []


def test_a_grown_plan_that_recorded_an_explicit_route_kind_is_unknown_until_the_route_is_trusted(monkeypatch) -> None:
    out, _ = _match([GROWN_LOOKUP], [(902, 0.05)], L)
    assert _ids(out) == [902]
    out, filt = _match([GROWN_LOOKUP], [(902, 0.05)], S)
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"unreviewed_route": 1}
    # once a reviewed decision trusts the route, the recorded kind is judged by the matrix
    monkeypatch.setattr(el, "TRUSTED_ROUTES", frozenset({"explicit"}))
    out, filt = _match([GROWN_LOOKUP], [(902, 0.05)], S)
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"kind_mismatch": 1}


# nexus-3ihve: a grown TAG with no grown declaration is unattested, refused for lookup and listing ---------------

#: Plan 517 as it lives: tagged grown on 2026-10-22, before declarations existed, so it declares nothing.
TAG_ONLY_GROWN = _row(517, "grown-tag-only", kind=None, tags="ad-hoc,grown", verb="lookup")
#: A tag-only claim over a curated declaration (not producible by a writer, but a stored row can say it).
TAG_OVER_CURATED = _row(518, "grown-tag-over-curated", kind=L, tags="builtin-template,grown", verb="lookup")


@pytest.mark.parametrize("request_kind", [L, D])
@pytest.mark.parametrize("row", [TAG_ONLY_GROWN, TAG_OVER_CURATED], ids=["tag-only", "tag-over-curated"])
def test_a_tag_only_grown_row_is_refused_for_lookup_and_listing_as_unattested(row, request_kind: K) -> None:
    out, filt = _match([row], [(row["id"], 0.05)], request_kind)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unattested_plan": 1}


def test_the_declared_grown_row_beside_a_tag_only_one_still_serves_lookup_and_listing() -> None:
    for request in (L, D):
        out, filt = _match(
            [TAG_ONLY_GROWN, GROWN_UNKNOWN], [(517, 0.04), (901, 0.05)], request,
        )
        assert _ids(out) == [901], request
        assert filt.excluded_plan_ids == [517]


# Scenario 5: unknown request with unknown-grown lookup history ------------------------------------------------


def test_scenario_5_an_unknown_request_never_selects_a_grown_plan() -> None:
    out, filt = _match(
        [GROWN_UNKNOWN, GROWN_LOOKUP, GROWN_LOOKUP_HEURISTIC], [(901, 0.05), (902, 0.06), (903, 0.07)], U,
    )
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {
        "unknown_request_untrusted_plan": 1, "unreviewed_route": 2,
    }


def test_scenario_5_an_unknown_request_keeps_the_curated_synthesis_and_drops_the_curated_lookup() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.45)], U)
    assert _ids(out) == [12]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unknown_request_narrow_plan": 1}


# Scenario 6 (matcher half): unknown request and a high-confidence lookup match --------------------------------


def test_scenario_6_an_unknown_request_with_a_high_confidence_lookup_match_selects_nothing() -> None:
    out, _ = _match([LOOKUP_473], [(473, 0.02)], U)
    assert out == []


# Scenario 7: category-route (below floor) and FTS candidates are judged the same way ------------------------


def test_scenario_7_the_category_route_skips_an_ineligible_pool_member() -> None:
    lookup = dict(LOOKUP_473, dimensions=json.dumps({"scope": "global", "verb": "research"}), verb="research")
    # both sit below the 0.40 floor; raw cosine prefers the lookup plan
    hits = [(473, 0.80), (12, 0.90)]
    out, filt = _match([lookup, SYNTH_12], hits, S, category_verb="research")
    assert _ids(out) == [12] and out[0].confidence is None, "routed by dimension: the sentinel confidence"
    wire = filt.to_wire()["branches"]
    assert wire["category_route"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert wire["cosine"]["examined"] == 0


def test_scenario_7_the_category_route_with_nothing_eligible_falls_through_to_fts() -> None:
    lookup = dict(LOOKUP_473, dimensions=json.dumps({"scope": "global", "verb": "research"}), verb="research")
    out, filt = _match([lookup], [(473, 0.80)], S, category_verb="research", fts=[473])
    assert out == []
    wire = filt.to_wire()["branches"]
    assert wire["category_route"]["excluded"] == 1 and wire["fts"]["excluded"] == 1


def test_scenario_7_fts_candidates_are_judged() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12, GROWN_UNKNOWN], [], S, fts=[473, 901, 12], cache=False)
    assert _ids(out) == [12]
    assert filt.to_wire()["branches"]["fts"] == {
        "examined": 3, "excluded": 2, "reasons": {"kind_mismatch": 1, "untrusted_unknown_plan": 1},
    }


def test_scenario_7_a_cosine_miss_that_reaches_fts_is_filtered_there_too() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.95)], S, fts=[473, 12])
    assert _ids(out) == [12]
    wire = filt.to_wire()["branches"]
    assert wire["fts"]["excluded"] == 1


# the legacy builtin ------------------------------------------------------------------------------------


def test_a_legacy_builtin_without_a_declaration_is_not_curated_and_says_so() -> None:
    """A builtin seeded before 2.1 has the tag and no declaration until a reseed. With the gate on it is an
    untrusted unknown plan, and the exclusion names the cause."""
    out, filt = _match([LEGACY_BUILTIN], [(14, 0.2)], S)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"undeclared_builtin": 1}
    # nexus-rflvp (Sam, 2026-11-03): an undeclared plan no longer serves a lookup; the exclusion names the
    # builtin's own defect, and the library precondition keeps the gate in shadow until the reseed (gate_defaults).
    out, filt = _match([LEGACY_BUILTIN], [(14, 0.2)], L)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"undeclared_builtin": 1}


# Scenario 8: with the constant off, matching is unchanged ----------------------------------------------------

_BATTERY = [
    pytest.param([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S, {}, id="synthesis-with-473-leading"),
    pytest.param([GROWN_UNKNOWN, SYNTH_12], [(901, 0.05), (12, 0.45)], U, {}, id="unknown-with-a-grown-lead"),
    pytest.param([LOOKUP_473], [(473, 0.8)], S, {"category_verb": "research"}, id="category-route"),
    pytest.param([LOOKUP_473, SYNTH_12], [], S, {"fts": [473, 12], "cache": False}, id="fts"),
    pytest.param([LEGACY_BUILTIN, SYNTH_12], [(14, 0.2), (12, 0.3)], B, {}, id="legacy-builtin"),
]


@pytest.mark.parametrize(("rows", "hits", "request_kind", "extra"), _BATTERY)
def test_scenario_8_with_the_gate_off_the_returned_matches_are_those_of_no_filter(rows, hits, request_kind, extra) -> None:
    kwargs = {"category_verb": extra.get("category_verb")}
    fts = extra.get("fts", ())
    use_cache = extra.get("cache", True)

    def run(filt):
        lib = _Library(rows, fts)
        cache = _Cache(hits) if use_cache else None
        out = real_plan_match("q", library=lib, cache=cache, eligibility=filt, **kwargs)
        return out, lib.metrics, (cache.asked if cache else []), lib.fts_limits

    base = run(None)
    shadow_filter = _filter(request_kind, enforce=False)
    shadow = run(shadow_filter)
    assert shadow == base, (
        "matches, the metrics bumped, and the over-fetch asked of the cache and of FTS are all identical"
    )


def test_scenario_8_the_shadow_filter_still_reports_what_the_gate_would_exclude() -> None:
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S, enforce=False)
    assert _ids(out) == [473, 12], "selection unchanged: 473 still leads"
    wire = filt.to_wire()
    assert wire["enforced"] is False
    assert wire["branches"]["cosine"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert filt.excluded_plan_ids == [473]


def test_scenario_8_the_constant_ships_on() -> None:
    # nexus-jypmj.2.8: this pinned "ships off".
    assert el.ANSWER_KIND_ELIGIBILITY_ENABLED is True


def test_enforcing_widens_the_cosine_over_fetch_so_excluded_plans_do_not_starve_the_pool() -> None:
    def asked(enforce: bool) -> list[int]:
        cache = _Cache([(473, 0.1)])
        real_plan_match("q", library=_Library([LOOKUP_473]), cache=cache, eligibility=_filter(S, enforce=enforce))
        return cache.asked

    assert asked(True)[0] == asked(False)[0] + 5


def test_enforcing_widens_the_fts_over_fetch_too() -> None:
    def asked(enforce: bool) -> int:
        lib = _Library([LOOKUP_473], fts=[473])
        real_plan_match("q", library=lib, cache=None, eligibility=_filter(S, enforce=enforce))
        return lib.fts_limits[0]

    assert asked(True) == asked(False) + 5


def test_no_filter_at_all_is_the_existing_call() -> None:
    lib = _Library([LOOKUP_473, SYNTH_12])
    out = real_plan_match("q", library=lib, cache=_Cache([(473, 0.1), (12, 0.4)]))
    assert _ids(out) == [473, 12]


# order: eligibility is asked last ------------------------------------------------------------------------------


def _requiring(row: dict, *names: str) -> dict:
    plan = json.loads(row["plan_json"])
    plan["required_bindings"] = list(names)
    return {**row, "plan_json": json.dumps(plan)}


def test_eligibility_runs_after_the_binding_gate_so_an_unrunnable_wrong_shape_plan_is_counted_once(
    monkeypatch,
) -> None:
    """A lookup plan that needs a ``content_type`` nobody supplied is unrunnable AND the wrong shape for a
    synthesis request. It is the binding gate's drop: it never reaches the filter, so it is not ``examined``
    and not ``excluded`` there. Asked FIRST instead, the filter would count it and the binding log would not."""
    from nexus.plans import matcher

    dropped: list[tuple[int, str]] = []

    def spy(drops):
        dropped.extend(drops)
        drops.clear()

    monkeypatch.setattr(matcher, "_log_binding_drops", spy)
    unrunnable = dict(_requiring(LOOKUP_473, "content_type"), id=480, name="needs-a-type")
    out, filt = _match(
        [unrunnable, LOOKUP_473, SYNTH_12], [(480, 0.05), (473, 0.1), (12, 0.4)], S,
        available_bindings=frozenset({"intent"}),
    )
    assert _ids(out) == [12]
    assert filt.to_wire()["branches"]["cosine"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert filt.excluded_plan_ids == [473], "480 was dropped for its binding, not for its shape"
    assert dropped == [(480, "content_type")]


def test_eligibility_runs_after_the_binding_gate_on_the_fts_branch_too() -> None:
    """Same order on the FTS fallback: the unrunnable plan (480) is the binding gate's, only 473 is judged."""
    unrunnable = dict(_requiring(LOOKUP_473, "content_type"), id=480, name="needs-a-type")
    _, filt = _match(
        [unrunnable, LOOKUP_473], [], S, fts=[480, 473], cache=False, available_bindings=frozenset({"intent"}),
    )
    assert filt.to_wire()["branches"]["fts"] == {"examined": 1, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert filt.excluded_plan_ids == [473]


# a judging fault ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("enforce", [False, True], ids=["shadow", "enforcing"])
def test_a_judging_fault_never_fails_plan_match(monkeypatch, enforce) -> None:
    """Shadow mode runs the judge on the production path with the gate off. An exception in it must not
    become a ``plan_match`` failure: the candidate is counted under ``judge_error`` and, with the gate off, kept."""

    def boom(*_a, **_k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(el, "_judge", boom)
    out, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S, enforce=enforce)
    branch = filt.to_wire()["branches"]["cosine"]
    assert branch == {"examined": 2, "excluded": 2, "reasons": {"judge_error": 2}}
    assert _ids(out) == ([] if enforce else [473, 12]), "kept when not enforcing, dropped (fail closed) when enforcing"
    assert "judge_error" in el.REASONS


def test_a_judging_fault_does_not_fail_the_returned_summary_either(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("judge down")

    monkeypatch.setattr(el, "_judge", boom)
    filt = _filter(S, enforce=False)
    m = SimpleNamespace(plan_id=473)
    filt.record_returned([m], selected=m)
    assert filt.to_wire()["selected"]["reason"] == "judge_error"
    assert el.eligible_matches(S, [m]) == []


# odd stored rows ------------------------------------------------------------------------------------------------

_DEEP: dict = {}
_cursor = _DEEP
for _ in range(400):
    _cursor["k"] = {}
    _cursor = _cursor["k"]


@pytest.mark.parametrize(
    "plan_json",
    [
        None, "", "null", "[]", "42", '"text"', "{not json", json.dumps(_DEEP), [], 7, 1.5, object(),
        {"answer_kind": None}, {"answer_kind": []}, {"answer_kind": 7}, {"answer_kind": "lookup"},
        {"answer_kind": {"version": 1, "kind": ["lookup"], "provenance": "curated"}},
        {"answer_kind": {"version": 1, "kind": {"k": 1}, "provenance": "grown", "route": "explicit"}},
        {"answer_kind": {"version": 1, "kind": "lookup", "provenance": ["curated"]}},
        {"answer_kind": {"version": 1, "kind": "lookup", "provenance": "grown", "route": ["explicit"]}},
        {"answer_kind": {"version": [1], "kind": "lookup", "provenance": "curated"}},
        {"answer_kind": {"version": 1, "kind": "lookup", "provenance": "curated", "extra": {"a": [1]}}},
        {"steps": "x", "answer_kind": {}},
    ],
    ids=lambda v: type(v).__name__ if not isinstance(v, (str, dict)) else str(v)[:40],
)
@pytest.mark.parametrize("tags", [None, "", "builtin-template", "grown", "builtin-template,grown", 5, ["grown"]])
def test_classify_plan_never_raises_and_never_trusts_an_odd_row(plan_json, tags) -> None:
    standing = el.classify_plan(tags, plan_json)
    assert isinstance(standing.kind, K) and standing.provenance in ("curated", "grown", "unattested")
    if standing.provenance == "curated":
        assert isinstance(tags, str) and "builtin-template" in tags
    for request in K:
        el.eligibility(request, standing.kind, standing.provenance)


@pytest.mark.parametrize("raw", [None, [], 3, {"a": 1}, ["lookup"], float("nan"), b"lookup", object()])
def test_coerce_kind_of_an_unhashable_or_odd_value_is_unknown(raw) -> None:
    assert el.coerce_kind(raw) is U


# the filter ---------------------------------------------------------------------------------------------------


def test_the_filter_wire_carries_no_question_text_and_no_plan_text() -> None:
    _, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S)
    text = json.dumps(filt.to_wire())
    assert "Description for" not in text and "hybrid-factual-lookup" not in text and "search" not in text


def test_the_filter_wire_shape() -> None:
    _, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S)
    wire = filt.to_wire()
    assert set(wire) == {
        "request_kind", "kind_decision", "enforced", "shadow_reason", "library_check", "builtins_checked",
        "undeclared_builtins", "undeclared_builtin_names", "branches",
        "excluded_total", "excluded_plan_ids", "returned", "selected", "fallback_reason",
        "enforced_reasons", "enforced_excluded_total",  # additive: the exclusions that stay enforced in a shadow
        "refused_recorded_kinds",  # additive (nexus-q7dpt)
    }
    assert wire["refused_recorded_kinds"] == {}
    assert wire["enforced_reasons"] == [] and wire["enforced_excluded_total"] == wire["excluded_total"] == 1
    assert wire["shadow_reason"] is None and wire["library_check"] is None and wire["builtins_checked"] is None
    assert wire["undeclared_builtins"] is None and wire["undeclared_builtin_names"] == []
    assert wire["request_kind"] == "synthesis" and wire["kind_decision"] == "heuristic"
    assert set(wire["branches"]) == {"cosine", "category_route", "fts"}
    assert wire["branches"]["fts"] == {"examined": 0, "excluded": 0, "reasons": {}}


def test_fallback_reason_is_a_pure_function_of_what_happened() -> None:
    filt = _filter(S, enforce=True)
    assert el.fallback_reason(force_dynamic=True, matches=[], hit=False, filt=filt) == "force_dynamic"
    assert el.fallback_reason(force_dynamic=False, matches=[], hit=False, filt=filt) == "no_plan_candidates"
    filt.admit(LOOKUP_473, "cosine")
    assert el.fallback_reason(force_dynamic=False, matches=[], hit=False, filt=filt) == "all_candidates_ineligible"
    assert el.fallback_reason(force_dynamic=False, matches=[object()], hit=False, filt=filt) == "below_confidence_floor"
    assert el.fallback_reason(force_dynamic=False, matches=[object()], hit=True, filt=filt) is None
    shadow = _filter(S, enforce=False)
    shadow.admit(LOOKUP_473, "cosine")
    assert el.fallback_reason(force_dynamic=False, matches=[], hit=False, filt=shadow) == "no_plan_candidates", (
        "shadow mode never refused a plan, so exclusion is not why nothing matched"
    )


def test_returned_summary_counts_what_the_gate_would_leave() -> None:
    _, filt = _match([LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)], S, enforce=False)
    m473 = SimpleNamespace(plan_id=473)
    m12 = SimpleNamespace(plan_id=12)
    filt.record_returned([m473, m12], selected=m473)
    wire = filt.to_wire()
    assert wire["returned"] == {"total": 2, "eligible": 1}
    assert wire["selected"] == {
        "plan_id": 473, "eligible": False, "kind": "lookup", "provenance": "curated", "route": None,
        "reason": "kind_mismatch",
    }
    filt.record_returned([m12], selected=m12)
    assert filt.to_wire()["selected"]["eligible"] is True


def test_the_synthetic_inline_plan_has_no_verdict() -> None:
    _, filt = _match([LOOKUP_473], [(473, 0.1)], S)
    filt.record_returned([], selected=SimpleNamespace(plan_id=0))
    assert filt.to_wire()["selected"] is None


# few-shots ----------------------------------------------------------------------------------------------------


def _as_match(row: dict, confidence: float = 0.35) -> Match:
    return Match.from_plan_row(row, confidence=confidence)


def test_eligible_matches_drops_the_wrong_shape_plans() -> None:
    ms = [_as_match(LOOKUP_473), _as_match(SYNTH_12), _as_match(GROWN_UNKNOWN)]
    assert _ids(el.eligible_matches(S, ms)) == [12]
    assert _ids(el.eligible_matches(L, ms)) == [473, 901], "a lookup is offered no synthesis plan (nexus-jypmj.11)"
    assert el.eligible_matches(S, None) == [] and el.eligible_matches(S, []) == []


@pytest.mark.asyncio
async def test_scenario_6_the_wrong_shape_plan_is_absent_from_the_planners_prompt() -> None:
    import nexus.mcp.core as core
    from nexus.plans.request_coverage import RequestCoverage

    captured: dict = {}

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        captured["prompt"] = prompt
        return {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    near_misses = [
        _as_match(dict(LOOKUP_473, query="LOOKUP-SHAPED-EXAMPLE how to look up a value")),
        _as_match(dict(SYNTH_12, query="SYNTHESIS-SHAPED-EXAMPLE how to summarize several aspects")),
    ]
    coverage = RequestCoverage(answer_kind="synthesis", kind_decision="heuristic")
    with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch), \
         patch("nexus.mcp_infra.get_collection_names", lambda: []):
        await core._nx_answer_plan_miss(
            "q", few_shot_matches=near_misses, coverage=coverage, enforce_eligibility=True,
        )
    assert "SYNTHESIS-SHAPED-EXAMPLE" in captured["prompt"]
    assert "LOOKUP-SHAPED-EXAMPLE" not in captured["prompt"]


@pytest.mark.asyncio
async def test_an_unknown_request_gives_the_planner_no_narrow_or_grown_example() -> None:
    import nexus.mcp.core as core
    from nexus.plans.request_coverage import RequestCoverage

    captured: dict = {}

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        captured["prompt"] = prompt
        return {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    near_misses = [
        _as_match(dict(LOOKUP_473, query="LOOKUP-SHAPED-EXAMPLE")),
        _as_match(dict(GROWN_UNKNOWN, query="GROWN-EXAMPLE")),
    ]
    with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch), \
         patch("nexus.mcp_infra.get_collection_names", lambda: []):
        await core._nx_answer_plan_miss(
            "q", few_shot_matches=near_misses, coverage=RequestCoverage(), enforce_eligibility=True,
        )
    assert "LOOKUP-SHAPED-EXAMPLE" not in captured["prompt"] and "GROWN-EXAMPLE" not in captured["prompt"]


@pytest.mark.asyncio
async def test_with_the_gate_off_the_planner_prompt_is_unchanged() -> None:
    import nexus.mcp.core as core
    from nexus.plans.request_coverage import RequestCoverage

    prompts: list[str] = []

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        prompts.append(prompt)
        return {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    near_misses = [_as_match(dict(LOOKUP_473, query="LOOKUP-SHAPED-EXAMPLE"))]
    with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch), \
         patch("nexus.mcp_infra.get_collection_names", lambda: []):
        await core._nx_answer_plan_miss("q", few_shot_matches=near_misses)
        await core._nx_answer_plan_miss(
            "q", few_shot_matches=near_misses, coverage=RequestCoverage(),
        )
    assert prompts[0] == prompts[1] and "LOOKUP-SHAPED-EXAMPLE" in prompts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(("constant", "enforce", "filtered"), [(True, False, False), (False, True, True)])
async def test_the_planner_follows_the_callers_enforce_and_never_rereads_the_constant(
    monkeypatch, constant, enforce, filtered,
) -> None:
    """nexus-jypmj.2.3 S6: the gate is read once, into the request's filter. The planner takes that decision as
    an argument, so the matcher and the few-shots cannot disagree within one call."""
    import nexus.mcp.core as core
    from nexus.plans.request_coverage import RequestCoverage

    monkeypatch.setattr(el, "ANSWER_KIND_ELIGIBILITY_ENABLED", constant)
    prompts: list[str] = []

    async def fake_dispatch(prompt, schema, timeout=300.0, model=None, **kw):  # noqa: ARG001
        prompts.append(prompt)
        return {"steps": [{"tool": "search", "args": {"query": "x"}}]}

    near_misses = [_as_match(dict(LOOKUP_473, query="LOOKUP-SHAPED-EXAMPLE"))]
    with patch("nexus.operators.dispatch.claude_dispatch", fake_dispatch), \
         patch("nexus.mcp_infra.get_collection_names", lambda: []):
        await core._nx_answer_plan_miss(
            "q", few_shot_matches=near_misses, coverage=RequestCoverage(answer_kind="synthesis"),
            enforce_eligibility=enforce,
        )
    assert ("LOOKUP-SHAPED-EXAMPLE" not in prompts[0]) is filtered


# nx_answer: the wiring ----------------------------------------------------------------------------------------


def _ad_hoc(steps=None) -> Match:
    steps = steps or [
        {"tool": "search", "args": {"query": "$intent"}},
        {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
    ]
    return Match(
        plan_id=0, name="ad-hoc", description="inline", confidence=None, dimensions={}, tags="ad-hoc",
        plan_json=json.dumps({"steps": steps}), required_bindings=["intent"], optional_bindings=[],
        default_bindings={"intent": "q"}, parent_dims=None,
    )


def _drive(question, rows, hits, *, plan_miss=None, fts=(), db=None, matcher_floor=None, miss_error=None, **kw):
    """Run ``nx_answer`` with the REAL matcher over a fake library; the planner and the runner are stubs."""
    from tests.test_rdr232_caller_scope import _MULTI_PLAN, _drive_nx_answer, _plan_run_result

    lib = _Library(rows, fts)
    cache = _Cache(hits)
    seen: list[dict] = []

    def matcher(q, **kwargs):
        seen.append(kwargs)
        kwargs = {k: v for k, v in kwargs.items() if k not in ("library", "cache")}
        if matcher_floor is not None:
            kwargs["min_confidence"] = matcher_floor  # the matcher admits what nx_answer's own gate then refuses
        return real_plan_match(q, library=lib, cache=cache, **kwargs)

    miss = AsyncMock(side_effect=miss_error) if miss_error else AsyncMock(return_value=plan_miss or _ad_hoc())
    steps = [{"ids": ["c1"], "tumblers": ["1.1.1"], "chunk_collections": ["rdr__1-1__x__v1"]},
             {"text": "The decision is X [1]. It was accepted on 2026-10-01 after review of the evidence."}]
    kw.setdefault("scope", "")
    kw.setdefault("structured", True)
    with patch("nexus.mcp.core._nx_answer_plan_miss", miss), \
         patch("nexus.mcp.core._generalize_grown_match_description", AsyncMock(return_value=None)):
        env, _ = _drive_nx_answer(
            _MULTI_PLAN, question=question, plan_match=matcher, db=db,
            plan_run_result=_plan_run_result(steps, []), **kw,
        )
    return env, miss, seen


def test_the_envelope_reports_eligibility_even_with_the_gate_off(shadow) -> None:
    # nexus-jypmj.2.8: the gate is on by default; the kill switch restores the old shadow behaviour.
    env, miss, seen = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["shadow_reason"] == "disabled_by_setting"
    assert wire["enforced"] is False and wire["request_kind"] == "synthesis" and wire["kind_decision"] == "heuristic"
    assert wire["branches"]["cosine"] == {"examined": 2, "excluded": 1, "reasons": {"kind_mismatch": 1}}
    assert wire["selected"]["plan_id"] == 473 and wire["selected"]["eligible"] is False, (
        "shadow: 473 is still selected, and the report says the gate would refuse it"
    )
    assert env["plan_id"] == 473
    assert wire["fallback_reason"] is None
    assert not miss.called


def test_with_the_gate_on_plan_473_is_refused_and_the_next_eligible_plan_runs(enabled) -> None:
    env, miss, _ = _drive(G06, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    wire = env["plan_eligibility"]
    assert wire["enforced"] is True
    assert env["plan_id"] == 12
    assert wire["selected"] == {
        "plan_id": 12, "eligible": True, "kind": "synthesis", "provenance": "curated", "route": None, "reason": None,
    }
    assert wire["excluded_plan_ids"] == [473] and not miss.called


def test_scenario_3_nothing_eligible_means_the_inline_planner_and_says_why(enabled) -> None:
    env, miss, _ = _drive(G06, [LOOKUP_473], [(473, 0.05)], fts=[473])
    wire = env["plan_eligibility"]
    assert miss.called, "no known mismatch is forced"
    assert wire["fallback_reason"] == "all_candidates_ineligible"
    assert wire["excluded_total"] == 2, "the cosine candidate, then the FTS candidate of the same plan"
    assert wire["selected"] is None


def test_scenario_2_through_nx_answer_a_lookup_request_keeps_the_lookup_plan(enabled) -> None:
    env, miss, _ = _drive(LOOKUP_Q, [LOOKUP_473, SYNTH_12], [(473, 0.1), (12, 0.4)])
    assert env["request_coverage"]["answer_kind"] == "lookup"
    assert env["plan_id"] == 473 and not miss.called
    assert env["plan_eligibility"]["selected"]["eligible"] is True


def test_scenario_6_an_unknown_request_with_a_high_confidence_lookup_match_plans_inline(enabled, working_extraction) -> None:
    question = "Tell me about plan matching."
    env, miss, _ = _drive(question, [LOOKUP_473], [(473, 0.02)])
    assert env["request_coverage"]["answer_kind"] == "unknown"
    assert miss.called
    assert miss.call_args.kwargs["few_shot_matches"] == [], "the lookup plan is not offered as an example"
    assert env["plan_eligibility"]["fallback_reason"] == "all_candidates_ineligible"


def test_scenario_5_through_nx_answer_an_unknown_request_does_not_select_the_grown_plan(enabled, working_extraction) -> None:
    env, miss, _ = _drive("Tell me about plan matching.", [GROWN_UNKNOWN], [(901, 0.05)])
    assert miss.called and env["plan_id"] != 901


def test_no_candidates_and_a_low_confidence_match_have_their_own_fallback_reasons() -> None:
    env, miss, _ = _drive(G06, [], [])
    assert miss.called and env["plan_eligibility"]["fallback_reason"] == "no_plan_candidates"
    low = _row(77, "low", kind=S, tags="builtin-template", verb="research")
    env, miss, _ = _drive(G06, [low], [(77, 0.7)], matcher_floor=0.0)
    assert miss.called and env["plan_eligibility"]["fallback_reason"] == "below_confidence_floor"


def test_the_planner_is_handed_the_coverage_and_the_filters_enforce(monkeypatch) -> None:
    # nexus-jypmj.2.8: enforcing is the default; the kill switch gives False (the old test had the two reversed).
    env, miss, _ = _drive(G06, [], [])
    cov = miss.call_args.kwargs["coverage"]
    assert cov.answer_kind == "synthesis" and cov.kind_decision == "heuristic"
    assert miss.call_args.kwargs["enforce_eligibility"] is True
    monkeypatch.setenv(el.ANSWER_KIND_ELIGIBILITY_ENV, "0")
    _, miss, _ = _drive(G06, [], [])
    assert miss.call_args.kwargs["enforce_eligibility"] is False


def test_the_matcher_is_handed_a_filter_built_from_the_coverage_and_the_setting() -> None:
    _, _, seen = _drive(G06, [LOOKUP_473], [(473, 0.1)])
    filt = seen[0]["eligibility"]
    assert (filt.request_kind, filt.decision, filt.enforce) == (S, "heuristic", True)


def test_a_caller_hint_is_an_explicit_decision_in_the_report() -> None:
    env, _, _ = _drive("Tell me about plan matching.", [], [], dimensions={"answer_kind": "synthesis"})
    wire = env["plan_eligibility"]
    assert (wire["request_kind"], wire["kind_decision"]) == ("synthesis", "explicit")


def test_a_matcher_error_still_carries_the_eligibility_key() -> None:
    from tests.test_rdr232_caller_scope import _MULTI_PLAN, _drive_nx_answer

    def boom(*_a, **_k):
        raise RuntimeError("matcher down")

    env, _ = _drive_nx_answer(_MULTI_PLAN, scope="", structured=True, plan_match=boom, question=G06)
    assert env["plan_eligibility"]["fallback_reason"] is None and env["plan_eligibility"]["branches"]["fts"]["examined"] == 0


def test_the_planner_failure_envelope_says_why_the_planner_was_asked(enabled) -> None:
    """The fallback reason is set before the inline planner runs, so the error envelope carries it too."""
    env, miss, _ = _drive(G06, [LOOKUP_473], [(473, 0.05)], fts=[473], miss_error=RuntimeError("planner down"))
    assert miss.called
    assert env["plan_eligibility"]["fallback_reason"] == "all_candidates_ineligible"
    assert env["plan_eligibility"]["enforced"] is True and env["plan_eligibility"]["excluded_total"] == 2
    assert env["plan_eligibility"]["selected"] is None, "no plan was selected: the planner failed"
    assert "planner" in json.dumps(env).lower()


# the grow site records the route kind only when the route was known ----------------------------------------


def _grown_save(question, **kw):
    db = MagicMock(plans=MagicMock())
    db.plans.save_plan = MagicMock(return_value=999)
    db.plans.get_plan = MagicMock(return_value={"id": 999})
    _drive(question, [], [], db=db, **kw)
    assert db.plans.save_plan.called, "the inline plan must be grown for this test to mean anything"
    return json.loads(db.plans.save_plan.call_args.kwargs["plan_json"])[ANSWER_KIND_KEY]


def test_a_grown_plan_records_the_kind_and_route_of_the_request_that_produced_it() -> None:
    assert _grown_save(G06) == {"version": 1, "kind": "synthesis", "provenance": "grown", "route": "heuristic"}


def test_an_explicit_hint_is_recorded_as_an_explicit_route() -> None:
    declared = _grown_save("Tell me about plan matching.", dimensions={"answer_kind": "comparison"})
    assert declared == {"version": 1, "kind": "comparison", "provenance": "grown", "route": "explicit"}


def test_an_unknown_route_records_unknown_and_never_a_guessed_kind() -> None:
    declared = _grown_save("Tell me about plan matching.")
    assert declared == {"version": 1, "kind": "unknown", "provenance": "grown"}


def test_a_model_assisted_route_is_recorded_as_such() -> None:
    from nexus.plans.request_coverage import RequestCoverage

    cov = RequestCoverage(answer_kind="audit", kind_decision="model-assisted")
    with patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=cov)):
        declared = _grown_save("Tell me about plan matching.")
    assert declared == {"version": 1, "kind": "audit", "provenance": "grown", "route": "model-assisted"}


def test_a_known_kind_with_no_decision_is_not_recorded() -> None:
    from nexus.plans.request_coverage import RequestCoverage

    cov = RequestCoverage(answer_kind="audit", kind_decision="unknown")
    with patch("nexus.mcp.core._nx_answer_request_coverage", AsyncMock(return_value=cov)):
        declared = _grown_save("Tell me about plan matching.")
    assert declared == {"version": 1, "kind": "unknown", "provenance": "grown"}


def test_a_verbatim_repeat_does_not_bypass_eligibility() -> None:
    """The verbatim-repeat override lifts the confidence floors and nothing else (nexus-7g0rg). A grown plan of
    unknown kind answering its own question word for word is refused for a synthesis request all the same."""
    row = dict(GROWN_UNKNOWN, query="q")  # the matcher's intent below is "q"
    out, filt = _match([row], [(901, 0.9)], S)
    assert out == [] and filt.excluded_plan_ids == [901]
    out, _ = _match([row], [(901, 0.9)], L)
    assert _ids(out) == [901] and out[0].confidence == 1.0, "the override did fire for the lookup request"


def test_the_tag_words_are_the_matchers() -> None:
    from nexus.plans import matcher

    assert el.BUILTIN_TAG == matcher._CATEGORY_PLAN_TAG
    assert matcher._is_grown_plan_tags(el.GROWN_TAG) and not matcher._is_grown_plan_tags(el.BUILTIN_TAG)


# ── nexus-indyo: a grown plan's recorded kind narrows, it never widens ────────────────────────────────────────────
#: Grown plans that recorded a non-lookup kind: the request that grew them was not a lookup, so their steps were
#: planned for something else (a synthesis plan answering a lookup is the H09 cost shape, nexus-jypmj.11).
GROWN_SYNTHESIS = _row(904, "grown-synthesis", kind=S, tags="ad-hoc,grown", provenance="grown", route="model-assisted")
GROWN_COMPARISON = _row(905, "grown-comparison", kind=C, tags="ad-hoc,grown", provenance="grown", route="heuristic")
GROWN_DISCOVERY = _row(906, "grown-discovery", kind=D, tags="ad-hoc,grown", provenance="grown", route="explicit")


@pytest.mark.parametrize("row", [GROWN_SYNTHESIS, GROWN_COMPARISON, GROWN_DISCOVERY], ids=lambda r: r["name"])
def test_a_grown_plan_that_recorded_another_kind_does_not_serve_a_lookup(row) -> None:
    out, filt = _match([row], [(row["id"], 0.05)], L)
    assert out == [] and filt.excluded_plan_ids == [row["id"]]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_not_lookup": 1}


@pytest.mark.parametrize("row", [GROWN_UNKNOWN, GROWN_LOOKUP, GROWN_LOOKUP_HEURISTIC], ids=lambda r: r["name"])
def test_a_grown_plan_that_recorded_lookup_or_nothing_still_serves_a_lookup(row) -> None:
    out, _ = _match([row], [(row["id"], 0.05)], L)
    assert _ids(out) == [row["id"]]


def test_the_recorded_kind_does_not_widen_a_grown_plan() -> None:
    """The narrowing reads the recorded kind; it does not trust it. A grown plan that recorded synthesis is still
    refused for a synthesis request (unreviewed route), as before nexus-indyo."""
    out, filt = _match([GROWN_SYNTHESIS], [(904, 0.05)], S)
    assert out == [] and filt.excluded_plan_ids == [904]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unreviewed_route": 1}


def test_discovery_requests_are_unchanged_by_the_lookup_narrowing() -> None:
    out, _ = _match([GROWN_SYNTHESIS], [(904, 0.05)], D)
    assert _ids(out) == [904]


def test_the_lookup_narrowing_runs_in_shadow_like_the_matrix() -> None:
    out, filt = _match([GROWN_SYNTHESIS], [(904, 0.05)], L, enforce=False)
    assert _ids(out) == [904]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"grown_route_not_lookup": 1}


@pytest.mark.parametrize("enforce", [True, False], ids=["enforced", "shadow"])
def test_a_lookup_refusal_reports_the_recorded_kind_that_caused_it(enforce) -> None:
    # nexus-q7dpt: the reason alone does not say which recorded kind the grown plan carried.
    _, filt = _match([GROWN_SYNTHESIS, GROWN_COMPARISON], [(904, 0.05), (905, 0.06)], L, enforce=enforce)
    assert filt.to_wire()["refused_recorded_kinds"] == {"comparison": 1, "synthesis": 1}


def test_a_matrix_refusal_reports_no_recorded_kind() -> None:
    _, filt = _match([LOOKUP_473], [(473, 0.1)], S)
    assert filt.to_wire()["refused_recorded_kinds"] == {}


def test_the_lookup_narrowing_reason_is_in_the_closed_vocabulary() -> None:
    assert "grown_route_not_lookup" in el.REASONS


def test_a_grown_declaration_with_no_route_is_no_declaration_so_the_row_is_unattested_for_a_lookup() -> None:
    # nexus-3ihve changed this contract: a kind with no route is not a valid declaration (it decodes as none),
    # so the grown TAG stands alone and attests nothing. It served lookups before (recorded_kind None, grown).
    row = _row(907, "grown-routeless", tags="ad-hoc,grown", verb="lookup")
    row["plan_json"] = json.dumps(
        {"steps": [], ANSWER_KIND_KEY: {"version": 1, "kind": "synthesis", "provenance": "grown"}},
    )
    standing = el.classify_plan(row["tags"], row["plan_json"])
    assert (standing.provenance, standing.recorded_kind) == ("unattested", None)
    out, filt = _match([row], [(907, 0.05)], L)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unattested_plan": 1}


def test_a_trusted_route_is_judged_by_the_matrix_not_the_lookup_narrowing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a route is trusted the recorded kind IS the plan's kind, so a synthesis plan is refused for a lookup by
    the matrix (``kind_mismatch``), and the narrowing, which reads only discounted kinds, never fires."""
    monkeypatch.setattr(el, "TRUSTED_ROUTES", frozenset({"model-assisted"}))
    standing = el.classify_plan(GROWN_SYNTHESIS["tags"], GROWN_SYNTHESIS["plan_json"])
    assert (standing.kind, standing.recorded_kind) == (S, None)
    out, filt = _match([GROWN_SYNTHESIS], [(904, 0.05)], L)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"kind_mismatch": 1}


# ── nexus-rflvp (Sam, 2026-11-03): an unattested plan serves neither a lookup nor a listing ─────────────────────
#: A plan-save row: no builtin tag, no grown tag, no declaration. Plans 598 and 599 were this (user-saved beads
#: plans) and won 31 of 77 lookups since the gate was enabled.
USER_SAVED = _row(910, "user-saved-plan", tags="", verb="lookup")
USER_SAVED["plan_json"] = json.dumps({"steps": [{"tool": "search", "args": {"query": "$intent"}}]})


@pytest.mark.parametrize("request_kind", [L, D])
def test_an_unattested_plan_is_refused_for_a_lookup_and_a_listing_with_its_own_reason(request_kind: K) -> None:
    assert el.classify_plan(USER_SAVED["tags"], USER_SAVED["plan_json"]).provenance == "unattested"
    out, filt = _match([USER_SAVED], [(910, 0.05)], request_kind)
    assert out == [] and filt.excluded_plan_ids == [910]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unattested_plan": 1}


def test_a_lookup_goes_to_the_curated_plan_when_a_user_saved_plan_outscores_it() -> None:
    out, _ = _match([USER_SAVED, LOOKUP_473], [(910, 0.05), (473, 0.3)], L)
    assert _ids(out) == [473]


@pytest.mark.parametrize("request_kind", [S, C, A, B, U])
def test_the_other_request_kinds_keep_their_unattested_reasons(request_kind: K) -> None:
    out, filt = _match([USER_SAVED], [(910, 0.05)], request_kind)
    assert out == []
    reason = "unknown_request_untrusted_plan" if request_kind is U else "untrusted_unknown_plan"
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {reason: 1}


@pytest.mark.parametrize("request_kind", [L, D])
def test_the_unattested_refusal_runs_in_shadow_like_the_matrix(request_kind: K) -> None:
    out, filt = _match([USER_SAVED], [(910, 0.05)], request_kind, enforce=False)
    assert _ids(out) == [910], "shadow keeps the candidate"
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unattested_plan": 1}
    assert filt.enforced_excluded_total == 0


def test_the_unattested_refusal_holds_on_every_branch() -> None:
    out, filt = _match([USER_SAVED], [], L, fts=[910], cache=False)
    assert out == []
    assert filt.to_wire()["branches"]["fts"]["reasons"] == {"unattested_plan": 1}


@pytest.mark.parametrize("request_kind", [L, D])
def test_grown_and_curated_plans_keep_serving_lookups_and_listings(request_kind: K) -> None:
    curated = LOOKUP_473 if request_kind is L else DISC_13
    out, _ = _match([GROWN_UNKNOWN, curated], [(901, 0.05), (curated["id"], 0.3)], request_kind)
    assert _ids(out) == [901, curated["id"]]


def test_an_undeclared_builtin_is_refused_under_the_builtins_own_reason_not_unattested_plan() -> None:
    standing = el.classify_plan(LEGACY_BUILTIN["tags"], LEGACY_BUILTIN["plan_json"])
    assert (standing.provenance, standing.flag) == ("unattested", "undeclared_builtin")
    verdict = el._judge(L, LEGACY_BUILTIN["tags"], LEGACY_BUILTIN["plan_json"])[1]
    assert (verdict.eligible, verdict.reason) == (False, "undeclared_builtin")


def test_the_unattested_reason_is_in_the_closed_vocabulary() -> None:
    assert "unattested_plan" in el.REASONS


# ── nexus-0vh80 (Sam, 2026-11-03): a saved plan declares a kind and is honoured narrowly ─────────────────────
#: The terminal step of a plan that ends in an operator: it puts an answer together, so any known kind may claim it.
_OPERATOR_END = [{"tool": "search", "args": {"query": "$intent"}}, {"tool": "summarize", "args": {"inputs": "$step1.ids"}}]
_RETRIEVAL_END = [{"tool": "memory_get", "args": {}}, {"tool": "search", "args": {"query": "$intent"}}]
_KNOWN = [L, S, D, C, A, B]


def _saved(pid: int, kind: K, *, project: str = "nexus", steps: list | None = None, tags: str = "") -> dict:
    return _row(
        pid, f"saved-{kind.value}", kind=kind, tags=tags, provenance="saved", project=project,
        steps=steps if steps is not None else _OPERATOR_END,
    )


@pytest.mark.parametrize("plan_kind", _KNOWN, ids=lambda k: k.value)
@pytest.mark.parametrize("request_kind", _KNOWN, ids=lambda k: k.value)
def test_a_saved_plan_serves_only_its_own_kinds_request_in_its_own_project(request_kind: K, plan_kind: K) -> None:
    row = _saved(920, plan_kind)
    out, filt = _match([row], [(920, 0.05)], request_kind, request_project="nexus")
    if request_kind is plan_kind:
        assert _ids(out) == [920]
    else:
        assert out == []
        assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"kind_mismatch": 1}


@pytest.mark.parametrize("request_kind", [C, A, B])
def test_a_saved_synthesis_plan_gets_none_of_the_compatibility_rows_curated_synthesis_has(request_kind: K) -> None:
    out, _ = _match([_saved(920, S)], [(920, 0.05)], request_kind, request_project="nexus")
    assert out == []
    out, _ = _match([SYNTH_12], [(12, 0.05)], request_kind, request_project="nexus")
    assert _ids(out) == [12], "the curated synthesis plan still serves them"


@pytest.mark.parametrize("branch", ["cosine", "fts"])
def test_a_saved_plan_is_refused_in_another_project_and_says_so(branch: str) -> None:
    row = _saved(921, L, project="ART")
    kw = {"fts": [921], "cache": False} if branch == "fts" else {}
    hits = [] if branch == "fts" else [(921, 0.05)]
    out, filt = _match([row], hits, L, request_project="nexus", **kw)
    assert out == [] and filt.excluded_plan_ids == [921]
    assert filt.to_wire()["branches"][branch]["reasons"] == {"saved_other_project": 1}
    out, _ = _match([row], hits, L, request_project="ART", **kw)
    assert _ids(out) == [921], "the same plan serves a request from the project that saved it"


@pytest.mark.parametrize(("plan_project", "request_project"), [("", "nexus"), ("nexus", ""), ("", "")])
def test_a_saved_plan_with_no_saving_project_or_a_request_with_none_serves_nothing(plan_project, request_project) -> None:
    out, filt = _match([_saved(922, L, project=plan_project)], [(922, 0.05)], L, request_project=request_project)
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"saved_other_project": 1}


def test_the_project_comparison_is_exact_not_a_prefix_or_case_fold() -> None:
    for saved_in in ("nexus-wt", "Nexus", "nex"):
        out, _ = _match([_saved(923, L, project=saved_in)], [(923, 0.05)], L, request_project="nexus")
        assert out == [], saved_in


def test_a_saved_plan_is_refused_for_the_unknown_request_in_its_own_project() -> None:
    for kind in _KNOWN:
        out, filt = _match([_saved(924, kind)], [(924, 0.05)], U, request_project="nexus")
        assert out == [], kind
        assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"unknown_request_untrusted_plan": 1}


@pytest.mark.parametrize("request_kind", [L, S, D, C, A, B, U], ids=lambda k: k.value)
def test_a_saved_plan_of_unknown_kind_serves_nothing(request_kind: K) -> None:
    out, filt = _match([_saved(925, U)], [(925, 0.05)], request_kind, request_project="nexus")
    assert out == []
    reason = "unknown_request_untrusted_plan" if request_kind is U else "saved_unknown_plan"
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {reason: 1}


@pytest.mark.parametrize("kind", [L, S, C, A, B], ids=lambda k: k.value)
def test_the_shape_veto_refuses_a_retrieval_only_plan_declared_with_an_answering_kind(kind: K) -> None:
    row = _saved(926, kind, steps=_RETRIEVAL_END)
    out, filt = _match([row], [(926, 0.05)], kind, request_project="nexus")
    assert out == []
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"saved_retrieval_only": 1}


def test_the_shape_veto_does_not_touch_a_listing_or_a_plan_that_ends_in_an_operator() -> None:
    out, _ = _match([_saved(927, D, steps=_RETRIEVAL_END)], [(927, 0.05)], D, request_project="nexus")
    assert _ids(out) == [927], "discovery/listing may end in retrieval"
    out, _ = _match([_saved(928, L, steps=_OPERATOR_END)], [(928, 0.05)], L, request_project="nexus")
    assert _ids(out) == [928]


@pytest.mark.parametrize("body", [{"steps": []}, {"steps": ["x"]}, {"steps": [{"args": {}}]}, {}], ids=["empty", "not-a-step", "no-tool", "no-steps"])
def test_a_saved_plan_whose_last_step_cannot_be_read_is_vetoed_not_trusted(body: dict) -> None:
    row = _saved(929, L)
    row["plan_json"] = json.dumps({**body, ANSWER_KIND_KEY: declaration_payload(L, "saved")})
    out, filt = _match([row], [(929, 0.05)], L, request_project="nexus")
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"saved_retrieval_only": 1}


def test_the_operator_terminals_the_veto_reads_are_the_runners_operators() -> None:
    from nexus.plans.answer_kind import OPERATOR_TERMINALS
    from nexus.plans.runner import _OPERATOR_TOOL_MAP

    assert OPERATOR_TERMINALS == frozenset(_OPERATOR_TOOL_MAP) | frozenset(_OPERATOR_TOOL_MAP.values())


def test_a_saved_row_with_the_builtin_tag_is_still_judged_saved_and_narrow() -> None:
    """A project-tier plan the seed loader wrote: tag `builtin-template`, declaration `saved`."""
    row = _saved(930, L, tags="builtin-template")
    out, _ = _match([row], [(930, 0.05)], S, request_project="nexus")
    assert out == []
    out, _ = _match([row], [(930, 0.05)], L, request_project="ART")
    assert out == []
    out, _ = _match([row], [(930, 0.05)], L, request_project="nexus")
    assert _ids(out) == [930]


def test_a_saved_refusal_runs_in_shadow_like_the_matrix() -> None:
    out, filt = _match([_saved(931, L, project="ART")], [(931, 0.05)], L, enforce=False, request_project="nexus")
    assert _ids(out) == [931]
    assert filt.to_wire()["branches"]["cosine"]["reasons"] == {"saved_other_project": 1}
    assert filt.enforced_excluded_total == 0


def test_the_saved_reasons_are_in_the_closed_vocabulary() -> None:
    assert {"saved_unknown_plan", "saved_other_project", "saved_retrieval_only"} <= el.REASONS


def test_a_saved_lookup_plan_does_not_outscore_its_way_past_the_project_check_in_the_planners_few_shots() -> None:
    ms = [_as_match(_saved(932, L, project="ART")), _as_match(_saved(933, L, project="nexus")), _as_match(LOOKUP_473)]
    assert _ids(el.eligible_matches(L, ms, request_project="nexus")) == [933, 473]
    assert _ids(el.eligible_matches(L, ms)) == [473], "with no project known, no saved plan is offered"


def test_a_match_carries_the_project_of_its_row() -> None:
    assert _as_match(_saved(934, L, project="ART")).project == "ART"
    assert _as_match({**LOOKUP_473, "project": None}).project == ""


def test_nx_answer_hands_the_matcher_the_sessions_project_the_grown_plans_are_filed_under() -> None:
    from pathlib import Path

    _, _, seen = _drive(LOOKUP_Q, [LOOKUP_473], [(473, 0.1)])
    assert Path.cwd().name in seen[0]["eligibility"].request_projects


def test_through_nx_answer_a_saved_lookup_plan_serves_its_own_project_and_no_other(monkeypatch) -> None:
    from pathlib import Path

    here = Path.cwd().name
    mine, theirs = _saved(940, L, project=here), _saved(941, L, project="not-" + here)
    env, miss, _ = _drive(LOOKUP_Q, [theirs, mine], [(941, 0.05), (940, 0.3)], matcher_floor=0.0)
    wire = env["plan_eligibility"]
    assert wire["branches"]["cosine"]["reasons"] == {"saved_other_project": 1}
    assert env["plan_id"] == 940 and not miss.called


# ── nexus-0vh80 review round 1: the request's project is the cwd name or the primary checkout's ────────────────


def _match_as(rows, hits, request: K, cwd_name: str, aliases=(), **kw):
    lib = _Library(rows, kw.pop("fts", ()))
    filt = el.EligibilityFilter(request_kind=request, decision="heuristic", enforce=True, project=cwd_name,
                                project_aliases=aliases)
    out = real_plan_match("q", library=lib, cache=_Cache(hits), eligibility=filt, n=5, **kw)
    return out, filt


def test_a_saved_plan_filed_under_the_repo_name_serves_a_session_in_a_worktree_of_it() -> None:
    row = _saved(950, L, project="nexus")
    out, _ = _match_as([row], [(950, 0.05)], L, "elsinore-0vh80", aliases=("nexus",))
    assert _ids(out) == [950]
    out, _ = _match_as([row], [(950, 0.05)], L, "elsinore-0vh80", aliases=("nexus",))
    assert _ids(out) == [950]


def test_a_worktree_session_still_refuses_another_projects_saved_plan_and_a_plan_with_no_alias_match() -> None:
    row = _saved(951, L, project="ART")
    out, filt = _match_as([row], [(951, 0.05)], L, "elsinore-0vh80", aliases=("nexus",))
    assert out == [] and filt.to_wire()["branches"]["cosine"]["reasons"] == {"saved_other_project": 1}
    out, _ = _match_as([_saved(952, L, project="nexus")], [(952, 0.05)], L, "elsinore-0vh80")
    assert out == [], "with no primary name known only the cwd name counts"


@pytest.mark.parametrize(("plan_project", "request_project"), [(" nexus ", "nexus"), ("nexus", " nexus\n"), ("\tnexus", " nexus ")])
def test_the_project_comparison_strips_whitespace_on_both_sides(plan_project: str, request_project: str) -> None:
    out, _ = _match([_saved(953, L, project=plan_project)], [(953, 0.05)], L, request_project=request_project)
    assert _ids(out) == [953]


def test_a_whitespace_only_project_is_no_project() -> None:
    out, _ = _match([_saved(954, L, project="  ")], [(954, 0.05)], L, request_project="  ")
    assert out == []
    out, _ = _match([_saved(955, L, project="nexus")], [(955, 0.05)], L, request_project="   ")
    assert out == []


def test_the_few_shot_filter_takes_the_same_names() -> None:
    ms = [_as_match(_saved(956, L, project="nexus")), _as_match(_saved(957, L, project="ART"))]
    assert _ids(el.eligible_matches(L, ms, request_project=("elsinore-0vh80", "nexus"))) == [956]


def test_the_primary_checkout_name_is_read_from_the_git_common_dir_of_a_worktree(tmp_path) -> None:
    import subprocess

    from nexus.plans.active_project import primary_checkout_name

    repo = tmp_path / "myrepo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": __import__("os").environ["PATH"], "HOME": str(tmp_path)}
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "x"],
                 ["worktree", "add", "-q", str(tmp_path / "wt-feature"), "-b", "feat"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    sub = tmp_path / "wt-feature" / "deep"
    sub.mkdir()
    assert primary_checkout_name(str(repo)) == "myrepo"
    assert primary_checkout_name(str(tmp_path / "wt-feature")) == "myrepo"
    assert primary_checkout_name(str(sub)) == "myrepo", "from a subdirectory too"
    assert primary_checkout_name(str(tmp_path)) == "", "not a git checkout: no name, never raises"
    assert primary_checkout_name(str(tmp_path / "missing")) == ""


def test_nx_answer_hands_the_filter_the_cwd_name_and_the_primary_checkouts_name(monkeypatch) -> None:
    monkeypatch.setattr("nexus.plans.active_project.primary_checkout_name", lambda cwd: "nexus")
    from pathlib import Path

    _, _, seen = _drive(LOOKUP_Q, [LOOKUP_473], [(473, 0.1)])
    filt = seen[0]["eligibility"]
    assert filt.request_projects == frozenset({Path.cwd().name, "nexus"})
