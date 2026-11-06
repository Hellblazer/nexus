# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-147 Step 4 branch (c) (nexus-irndi): entity-listing questions reach a plan with a coverage boundary.

The 2026-11-06 MVV lost a HERB company question because the request-kind model call read it as
``discovery/listing``: the answer-kind gate then left only the single-query listing templates, and
``document-discovery`` (plan 7) answered with no coverage boundary (T2 ``nexus_rdr`` 147-research-15).
``conexus/plans/builtin/entity-resolution.yml`` is the fix: a curated ``discovery/listing`` template
with the step body of ``hybrid-factual-lookup``.

Same hermetic harness as ``test_plan_match_category_route_probes.py``: bundled ONNX MiniLM, a fake
library built from the YAML on disk, the real ``plan_match``, and a real ``EligibilityFilter`` per
request kind (enforcing) plus the shadow regime (not enforcing, extraction failed). No model call, no
service, no network.

The pins: for each request kind the 7 HERB company questions and some paraphrases reach a plan that
``_nx_answer_classify_plan`` does not call ``single_query`` (that classification is what skips the
coverage boundary); a ``lookup`` reading goes to ``hybrid-factual-lookup``; a ``discovery/listing``
reading and the shadow regime go to ``entity-resolution``; the golden discovery requests keep the plans
they had before the template existed; and with the YAML removed the plan-7 outcome comes back, which
shows the harness can tell the two trees apart.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from nexus.plans.answer_kind import PlanAnswerKind, decode_declaration
from nexus.plans.eligibility import EligibilityFilter
from nexus.plans.matcher import plan_match
from nexus.plans.seed_loader import desired_row_for_template
from nexus.plans.verb_infer import infer_verb
from tests.test_plan_match_category_route_probes import (
    _BUILTIN_DIR,
    _Library,
    _RealEmbedCache,
    _nx_answer_bindings,
    _templates,
)

_YAML = "entity-resolution.yml"

#: The first seven questions of the 2026-11-06 MVV run on HERB (the rows that run measured), verbatim.
_HERB_QUESTIONS = [
    "Find the names of companies were affected by issues in the AI Model & Search Algorithm in SearchFlow that caused decreased search accuracy and disrupted workflow?",
    "Find the names of companies were affected by issues in the backend search engine of SearchFlow that prevented retrieval of important historical messages, risking loss of critical information and user dissatisfaction?",
    "Find the names of companies were affected by issues in the SearchFlow user interface that caused frustration due to unresponsive interactions during search result loading?",
    "Find the names of companies were affected by issues in the Search API of SearchFlow that posed security risks and potential data breaches?",
    "Find the names of companies were affected by issues in the database management of SearchFlow that caused slow search performance and impacted productivity?",
    "Find the name of company that has the maximum number of resolved bugs in SearchFlow",
    "Find the name of company that has the maximum number of unresolved bugs in SearchFlow?",
]

#: Paraphrases of the same request shape (an entity listing tied to an issue), written for this test. Each infers
#: the `research` verb: a wording with "crash" infers `debug` ("Name the customers who reported crashes in the
#: reporting module." matches no plan at all under the lookup, unknown and synthesis readings), which is the verb
#: table's call and outside this template.
_PARAPHRASES = [
    "Find the names of customers affected by outages in the billing service of OrderHub",
    "Which companies were impacted by the login failures in the mobile app?",
    "Which customers were affected by the outage in the payments module?",
]

_QUESTIONS = _HERB_QUESTIONS + _PARAPHRASES

#: (label, request kind, enforcing, shadow reason). The labels name the five regimes of the brief; the shadow
#: regime is the one the MVV's no-claude runs were in: the extraction failed, so the request is on its first-pass
#: kind (`unknown`) and the gate judges and counts but excludes nothing.
_KIND_LOOKUP = ("lookup", "lookup", True, None)
_KIND_DISCOVERY = ("discovery", "discovery/listing", True, None)
_KIND_UNKNOWN = ("unknown", "unknown", True, None)
_KIND_SYNTHESIS = ("synthesis", "synthesis", True, None)
_KIND_SHADOW = ("shadow", "unknown", False, "extraction_failed")
_KINDS = [_KIND_LOOKUP, _KIND_DISCOVERY, _KIND_UNKNOWN, _KIND_SYNTHESIS, _KIND_SHADOW]


@pytest.fixture(scope="module")
def rows():
    return _templates()


@pytest.fixture(scope="module")
def cache(rows):
    return _RealEmbedCache(rows)


@pytest.fixture(scope="module")
def rows_without_the_template():
    """The library as it was before this template: every builtin on disk except entity-resolution.yml."""
    kept = [r for r in _templates() if r["_file"] != _YAML]
    return kept


@pytest.fixture(scope="module")
def cache_without_the_template(rows_without_the_template):
    return _RealEmbedCache(rows_without_the_template)


def _classify(match) -> str:
    from nexus.mcp.core import _nx_answer_classify_plan  # noqa: PLC0415 — deferred: the MCP core import is heavy

    return _nx_answer_classify_plan(match)


def _route(rows, cache, question: str, kind: str, enforce: bool, shadow_reason: str | None):
    """What ``nx_answer`` would match for *question* under this request kind: ``(name, verb, class)`` or None."""
    gate = EligibilityFilter(
        kind, "model-assisted" if enforce else "unknown", enforce=enforce, shadow_reason=shadow_reason,
    )
    matches = plan_match(
        question, library=_Library(rows), cache=cache, category_verb=infer_verb(question), n=5,
        available_bindings=_nx_answer_bindings(question), eligibility=gate,
    )
    if not matches:
        return None
    row = next(r for r in rows if r["id"] == matches[0].plan_id)
    return (row["name"], row["verb"], _classify(matches[0]))


# ── the template itself ─────────────────────────────────────────────────────


def _template() -> dict:
    return yaml.safe_load((_BUILTIN_DIR / _YAML).read_text())


def test_the_template_is_the_curated_discovery_listing_builtin_the_rdr_names() -> None:
    template = _template()
    assert template["dimensions"] == {"verb": "research", "scope": "global", "strategy": "entity-resolution"}
    assert "builtin-template" in template["tags"].split(",")
    row = desired_row_for_template(template)
    decl = decode_declaration(row.plan_json)
    assert (decl.kind, decl.provenance) == (PlanAnswerKind.DISCOVERY_LISTING, "curated")


def test_the_step_body_is_hybrid_factual_lookups() -> None:
    hybrid = yaml.safe_load((_BUILTIN_DIR / "hybrid-factual-lookup.yml").read_text())
    assert _template()["plan_json"] == hybrid["plan_json"]
    assert _template()["default_bindings"] == hybrid["default_bindings"]


def test_the_plan_has_a_coverage_boundary_shape_and_is_not_a_single_query() -> None:
    steps = _template()["plan_json"]["steps"]
    assert len(steps) > 1 and [s["tool"] for s in steps][-1] == "generate"


# ── the questions reach a plan with a coverage boundary ─────────────────────
#
# Which pins the delete-the-YAML check turns red: the discovery/listing and shadow regimes (the two tests that
# expect `entity-resolution`, and the single-query pins for them). The lookup, unknown and synthesis pins pass
# with or without the YAML: they guard against the template stealing those readings, not against its absence.
# The control tests at the end of the file run the library without the YAML to show the contrast.


@pytest.mark.parametrize("question", _QUESTIONS, ids=[q[:48] for q in _QUESTIONS])
@pytest.mark.parametrize("regime", _KINDS, ids=[k[0] for k in _KINDS])
def test_no_company_question_reaches_a_single_query_plan(rows, cache, question, regime) -> None:
    _, kind, enforce, shadow_reason = regime
    got = _route(rows, cache, question, kind, enforce, shadow_reason)
    assert got is not None, f"{regime[0]}: {question!r} matched nothing"
    assert got[2] != "single_query", f"{regime[0]}: {question!r} routed to {got}, a single query"


@pytest.mark.parametrize("question", _QUESTIONS, ids=[q[:48] for q in _QUESTIONS])
def test_a_lookup_reading_goes_to_hybrid_factual_lookup(rows, cache, question) -> None:
    _, kind, enforce, shadow_reason = _KIND_LOOKUP
    got = _route(rows, cache, question, kind, enforce, shadow_reason)
    assert got == ("hybrid-factual-lookup", "lookup", "needs_operators")


@pytest.mark.parametrize("question", _QUESTIONS, ids=[q[:48] for q in _QUESTIONS])
@pytest.mark.parametrize("regime", [_KIND_DISCOVERY, _KIND_SHADOW], ids=["discovery", "shadow"])
def test_a_discovery_reading_and_the_shadow_regime_go_to_entity_resolution(rows, cache, question, regime) -> None:
    _, kind, enforce, shadow_reason = regime
    got = _route(rows, cache, question, kind, enforce, shadow_reason)
    assert got == ("entity-resolution", "research", "needs_operators"), f"{regime[0]}: {question!r} -> {got}"


@pytest.mark.parametrize("question", _QUESTIONS, ids=[q[:48] for q in _QUESTIONS])
def test_the_cosine_path_reaches_the_template_above_the_floor(rows, cache, question) -> None:
    """The match text carries the questions on its own: raw cosine clears the 0.40 floor, so the category route
    is not what saves them. Measured at authoring (bundled MiniLM): the 7 HERB questions 0.43 to 0.55, the three
    paraphrases 0.41 to 0.54; the floor is the RDR-079 value. The paraphrase at 0.41 is close to the floor."""
    scores = {pid: 1.0 - dist for pid, dist in cache.query(question, len(rows))}
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    best = next(r for r in rows if r["id"] == ranked[0])
    assert best["name"] == "entity-resolution"
    assert scores[ranked[0]] >= 0.40, scores[ranked[0]]


# ── with the template removed, plan 7 comes back ────────────────────────────


def test_without_the_template_a_discovery_reading_lands_on_the_single_query_plan(
    rows_without_the_template, cache_without_the_template,
) -> None:
    """The failure this template exists for, reproduced on the tree without it: all 7 HERB questions under the
    discovery/listing reading are answered by document-discovery (plan 7), a single query with no coverage
    boundary. This is the control that makes the pins above mean something."""
    _, kind, enforce, shadow_reason = _KIND_DISCOVERY
    got = [
        _route(rows_without_the_template, cache_without_the_template, q, kind, enforce, shadow_reason)
        for q in _HERB_QUESTIONS
    ]
    assert got == [("document-discovery", "research", "single_query")] * len(_HERB_QUESTIONS)


def test_without_the_template_the_shadow_regime_mostly_lands_on_the_single_query_plan(
    rows_without_the_template, cache_without_the_template,
) -> None:
    """Shadow (extraction failed): raw cosine decides, and document-discovery beats hybrid-factual-lookup on 6 of the 7."""
    _, kind, enforce, shadow_reason = _KIND_SHADOW
    got = [
        _route(rows_without_the_template, cache_without_the_template, q, kind, enforce, shadow_reason)
        for q in _HERB_QUESTIONS
    ]
    assert sum(1 for g in got if g and g[2] == "single_query") == 6, got


# ── the golden discovery requests keep their plans ──────────────────────────

#: The seven golden discovery requests (tests/_rdr232_golden.py's golden_v1 and the held-out set, by qid prefix).
_GOLDEN_DISCOVERY = ("G10", "G11", "G12", "H03", "H11", "H18", "H25")

#: What each request reached BEFORE this template existed, measured on the 12-builtin tree (develop cf2765076):
#: under the discovery/listing reading, and in the shadow regime. (name, verb) or None for no match. H18 infers verb
#: `document`, so no research-verb plan is in its pool.
_DDISC = ("document-discovery", "research")
_BEFORE_DISCOVERY = {
    "G10": _DDISC, "G11": _DDISC, "G12": _DDISC, "H03": _DDISC, "H11": _DDISC, "H18": None, "H25": _DDISC,
}
_BEFORE_SHADOW = {
    "G10": _DDISC, "G11": ("abstract-themes", "query"), "G12": _DDISC, "H03": _DDISC, "H11": _DDISC,
    "H18": ("default", "document"), "H25": ("abstract-themes", "query"),
}


def _golden_requests() -> dict[str, str]:
    from bench.schema import load_golden_set  # noqa: PLC0415 — deferred with the golden fixture module
    from tests import _rdr232_golden as g  # noqa: PLC0415

    found = {}
    for path in (g.GOLDEN_V1_PATH, g.HELDOUT_PATH):
        for q in load_golden_set(path):
            prefix = q.qid.split("-")[0]
            if prefix in _GOLDEN_DISCOVERY:
                assert q.answer_kind == "discovery", q.qid
                found[prefix] = q.text
    return found


def test_the_golden_discovery_set_is_all_seven_requests() -> None:
    """Non-vacuity: the loop below examines every request it names."""
    assert sorted(_golden_requests()) == sorted(_GOLDEN_DISCOVERY)


@pytest.mark.parametrize(
    ("regime", "before"),
    [(_KIND_DISCOVERY, _BEFORE_DISCOVERY), (_KIND_SHADOW, _BEFORE_SHADOW)],
    ids=["discovery", "shadow"],
)
@pytest.mark.parametrize("qid", _GOLDEN_DISCOVERY)
def test_a_golden_discovery_request_keeps_its_plan(rows, cache, qid, regime, before) -> None:
    _, kind, enforce, shadow_reason = regime
    question = _golden_requests()[qid]
    got = _route(rows, cache, question, kind, enforce, shadow_reason)
    assert (got[:2] if got else None) == before[qid], f"{qid} {question!r} moved: {got}"


def test_the_json_round_trip_of_the_rows_keeps_the_template_declared(rows) -> None:
    """Fidelity: the row the matcher judges is the one the seed loader would store, declaration included."""
    row = next(r for r in rows if r["_file"] == _YAML)
    stored = json.loads(row["plan_json"])
    assert stored["answer_kind"]["kind"] == "discovery/listing"
