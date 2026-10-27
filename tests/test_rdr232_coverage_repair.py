# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 step 2 (nexus-jypmj.3.2): one targeted retrieval repair and one final reduction.

Tenant-free, like ``tests/test_rdr232_coverage_ledger.py`` (the helpers come from there): ``plan_run`` is
driven with a stub dispatcher and the model call (``operator_check``) is stubbed at the dispatcher, the
boundary the runner uses. The stub's check is content-aware: a facet is covered only by an item that
holds its topic word, so a repair that does not put the missing item in front of the reassessment cannot
turn a gap into coverage.

The five Test Plan scenarios of the bead are the tests named ``test_scenario_N_*``; the rest pin the
bounds (allowance, USD, caps), the merge order, the stable evidence ids and the continuation route.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp import core as mcp_core
from nexus.mcp.core import nx_answer
from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink
from nexus.plans import runner
from nexus.plans.continuation import classify_continuation_cut
from nexus.plans.continuation_envelope import assemble_continuation_envelope
from nexus.plans.coverage_ledger import AssessmentReport, BoundaryView, CoverageBoundary, CoverageResult, build_anchors
from nexus.plans.coverage_repair import (
    MAX_REPAIR_ITEMS,
    MAX_REPAIR_QUERIES,
    MIN_REPAIR_ALLOWANCE_S,
    REPAIR_ALLOWANCE_CAP_S,
    REPAIR_DEADLINE_SHARE,
    REPAIR_RESULTS_PER_QUERY,
    RepairTarget,
    build_repair_queries,
    repair_allowance,
    usd_affordable,
)
from nexus.plans.evidence import ScopeConstraint
from nexus.plans.evidence_diagnostics import capture_reducer_input
from nexus.plans.repair_stream import RepairSupplement, SupplementItem, supplement_scope
from nexus.plans.request_coverage import Facet
from nexus.plans.reserved_stream import reserved_scope
from nexus.plans.runner import plan_run
from tests.test_rdr232_coverage_ledger import (
    CODE_COLL,
    GRAPH_TEXTS,
    PLAN,
    RDR_COLL,
    VECTOR_TEXTS,
    Dispatcher,
    _cov,
    _match,
    _primary,
    _request,
    _reserved,
    _structured,
    _tag_lines,
    _Wrapper,
)

REPAIR_TEXT = "repair hit about topic three: the missing facet is stated here"
OTHER_REPAIR_TEXT = "a second repair hit that mentions nothing asked for"
#: The word each facet needs in an item before the stub's check supports it. f3 is the facet the plan's
#: own retrieval does not reach.
TOPIC = {"f1": "decision", "f2": "gate", "f3": "repair hit"}
FACET_TEXT = "what the document says about topic {n}"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    """The runner's non-evidentiary hygiene asks the catalog; these tests are tenant-free, like the other
    ``plan_run`` tests that patch it, so the lookup answers 'nothing excluded'."""
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


def by_topic(*, contradict: tuple[str, ...] = (), never: tuple[str, ...] = (), topic: dict[str, str] | None = None):
    """A check that supports a listed facet with every item holding its topic word."""
    words = {**TOPIC, **(topic or {})}

    def check(items: str, instruction: str) -> dict:
        payload = json.loads(items)
        evidence = []
        for tag in _tag_lines(instruction):
            if tag in never:
                continue
            for item in payload:
                if words.get(tag, "\0") in item["text"]:
                    role = "contradicts" if tag in contradict else "supports"
                    evidence.append({"item_id": item["id"], "quote": f"[{tag}] quoted", "role": role})
        return {"ok": bool(evidence), "evidence": evidence}

    return check


class RepairDispatcher(Dispatcher):
    """The ledger tests' stub, plus a repair search that answers with ``repair_hits`` and a fetch that
    returns whatever it knows by id."""

    def __init__(self, check=None, *, repair_hits: list[str] | None = None, fail_repair_search: bool = False,
                 usage_cost: float | None = None, **kw: Any) -> None:
        super().__init__(check or by_topic(), **kw)
        self.repair_hits = [REPAIR_TEXT] if repair_hits is None else repair_hits
        self.fail_repair_search = fail_repair_search
        self.usage_cost = usage_cost
        self.known = {_h(t): t for t in self.repair_hits}

    def is_repair_search(self, args: dict) -> bool:
        return args.get("query", args.get("question", "")).startswith("what the document says")

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool in ("search", "query") and self.is_repair_search(args):
            self.calls.append((tool, dict(args)))
            if self.fail_repair_search:
                raise RuntimeError("repair search backend down")
            return _structured(self.repair_hits, RDR_COLL)
        if tool == "store_get_many" and args.get("ids") and {str(i) for i in args["ids"]} <= set(self.known):
            self.calls.append((tool, dict(args)))
            return {"contents": [self.known[str(i)] for i in args["ids"]]}
        if tool == "operator_check" and self.usage_cost is not None:
            _ambient_usage_sink.get().append(DispatchUsage(
                model="m", cost_usd=self.usage_cost, input_tokens=1, output_tokens=1,
                cache_creation_input_tokens=0, cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1))
        return await super().__call__(tool, args)

    def repair_searches(self) -> list[dict]:
        return [a for t, a in self.calls if t in ("search", "query") and self.is_repair_search(a)]


async def _run(d: Dispatcher, boundary: CoverageBoundary | None, *, plan: dict = PLAN, **kw: Any):
    return await plan_run(_match(plan), {}, dispatcher=d, bundle_operators=False, coverage_boundary=boundary, **kw)


def _repair_records(result) -> list:
    return [r for r in result.step_records if r.step_index >= result.total_planned_steps and r.operator != "operator_check"]


# ── scenario 1: one requested facet is absent from initial retrieval ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_1_one_missing_facet_gets_exactly_one_repair_and_the_facet_gets_evidence() -> None:

    d = RepairDispatcher()
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert d.tools.count("traverse") == 1 and d.tools.count("generate") == 1, "the prefix and the reduction each ran once"
    repair_q = d.repair_searches()
    assert len(repair_q) == 1, "exactly one targeted query: one missing facet, one target"
    assert repair_q[0]["query"] == FACET_TEXT.format(n=3) and repair_q[0]["corpus"] == "rdr"
    assert repair_q[0]["limit"] == REPAIR_RESULTS_PER_QUERY
    assert d.tools.count("search") == 2 and d.tools.count("store_get_many") == 2, "the plan's own search and fetch, plus the repair's"
    assert d.tools.count("operator_check") == 3, "vector and graph before repair, one reassessment after"
    last = d.args_of("operator_check")[-1]
    assert _tag_lines(last["check_instruction"]) == ["f3"], "only the unsupported facet is reassessed"
    assert [i["text"] for i in json.loads(last["items"])] == [REPAIR_TEXT], "against the new evidence only"
    assert cov.state == "complete" and cov.reason == "all_covered" and cov.ledger.rounds == 2
    f3 = cov.ledger.finding("f3")
    new = next(a for a in cov.ledger.anchors if a.content == REPAIR_TEXT)
    assert f3 is not None and f3.state == "covered" and f3.evidence_ids == (new.item_id,)
    assert {a.content: a.item_id for a in cov.ledger.anchors if a.content != REPAIR_TEXT} == {
        VECTOR_TEXTS[0]: "e1", VECTOR_TEXTS[1]: "e2", GRAPH_TEXTS[0]: "e3"}, "earlier evidence ids do not move"
    assert new.item_id == "e4" and new.stream == "vector" and new.collection == RDR_COLL
    context = d.args_of("generate")[0]["context"]
    assert isinstance(context, str) and REPAIR_TEXT in context, "generation got the repair evidence"
    assert context.index(REPAIR_TEXT) < context.index(VECTOR_TEXTS[0]), "repair evidence leads the plan's own secondary items"
    assert (cov.repair.outcome, cov.repair.facet_ids, cov.repair.queries, cov.repair.items_merged) == ("completed", ("f3",), 1, 1)


@pytest.mark.asyncio
async def test_scenario_1_the_repair_is_recorded_in_the_existing_step_vocabulary() -> None:
    d = RepairDispatcher()
    result = await _run(d, CoverageBoundary(request=_request(3)))
    records = _repair_records(result)
    assert [r.operator for r in records] == ["search", "store_get_many"]
    assert all(r.source == "sql" and r.ok and r.cost_usd == 0.0 for r in records), "retrieval is a known-zero sql step"
    indices = [r.step_index for r in result.step_records]
    assert len(set(indices)) == len(indices) and min(r.step_index for r in records) >= result.total_planned_steps
    assert [r.operator for r in result.step_records if r.operator == "operator_check"] == ["operator_check"] * 3


@pytest.mark.asyncio
async def test_scenario_1_a_facet_the_repair_cannot_support_stays_a_residual_gap_and_is_not_repaired_again() -> None:
    d = RepairDispatcher(by_topic(topic={"f3": "a word no item holds"}))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "partial" and cov.unresolved_facet_ids == ("f3",)
    assert len(d.repair_searches()) == 1, "one repair, not a loop"
    assert d.tools.count("operator_check") == 3 and d.tools.count("generate") == 1
    assert cov.ledger.rounds == 2 and cov.ledger.finding("f3").state == "unsupported"
    assert cov.repair_facet_ids == ("f3",), "still a repairable gap by kind, but the allowance was one round"


@pytest.mark.asyncio
async def test_a_repair_that_finds_only_what_is_already_held_makes_no_reassessment() -> None:
    d = RepairDispatcher(repair_hits=VECTOR_TEXTS[:1])
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.repair.outcome == "no_new_evidence" and cov.repair.items_merged == 0
    assert d.tools.count("operator_check") == 2, "nothing new to put in front of the model, so no third call"
    assert cov.state == "partial" and cov.ledger.rounds == 1 and d.tools.count("generate") == 1


@pytest.mark.asyncio
async def test_scenario_1_a_repair_that_leaves_nothing_covered_still_skips_generation_afterwards() -> None:
    """Sam's 3.12 ruling skips generation when the ledger covers nothing. The skip is decided on the
    ledger AFTER the one repair, so the repair gets its chance first."""
    d = RepairDispatcher(by_topic(never=("f1", "f2"), topic={"f3": "a word no item holds"}))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert len(d.repair_searches()) >= 1, "the repair ran before the skip was decided"
    assert (cov.state, cov.reason, cov.skip_generation) == ("unavailable", "uncovered", True)
    assert "generate" not in d.tools and result.final.get("coverage_skipped") is True


@pytest.mark.asyncio
async def test_a_skip_that_the_repair_overturns_generates_once() -> None:
    d = RepairDispatcher(by_topic(never=("f1", "f2"), topic={"f3": "repair hit"}))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "partial" and not cov.skip_generation
    assert d.tools.count("generate") == 1 and result.final == {"text": "the answer"}


# ── scenario 2: retrieval evidence contradicts a requested premise ───────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_2_a_contradicted_facet_is_kept_and_repair_does_not_search_for_agreement() -> None:
    d = RepairDispatcher(by_topic(contradict=("f1",)))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    queries = [a["query"] for a in d.repair_searches()]
    assert queries == [FACET_TEXT.format(n=3)], "only the facet with no evidence is searched for; f1 is not"
    for args in d.args_of("operator_check")[2:]:
        assert "f1" not in _tag_lines(args["check_instruction"]), "the contradicted facet is not reassessed"
    f1 = cov.ledger.finding("f1")
    assert f1.state == "contradicted" and f1.evidence_ids, "the contradiction is retained for the reduction"
    assert cov.unresolved_facet_ids == ("f1",) and cov.state == "partial"


@pytest.mark.asyncio
async def test_scenario_2_a_ledger_whose_only_gap_is_a_contradiction_triggers_no_repair() -> None:
    d = RepairDispatcher(by_topic(contradict=("f1",), topic={"f3": "gate"}))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert d.repair_searches() == [] and d.tools.count("search") == 1 and d.tools.count("operator_check") == 2
    assert cov.state == "partial" and cov.repair.outcome == "not_attempted" and cov.ledger.rounds == 1
    assert [r for r in _repair_records(result)] == []


# ── scenario 3: the repair call fails after a successful first assessment ───────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_3_a_failed_reassessment_leaves_the_first_ledger_in_force() -> None:
    base = by_topic()

    def check(items: str, instruction: str) -> dict:
        if REPAIR_TEXT in items:
            return RuntimeError("check backend down")  # type: ignore[return-value]
        return base(items, instruction)

    d = RepairDispatcher(check)
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "partial", "not unassessed: the first assessment succeeded"
    assert [f.state for f in cov.ledger.findings] == ["covered", "covered", "unsupported"]
    assert cov.unresolved_facet_ids == ("f3",) and cov.skip_generation is False
    assert cov.ledger.assessment.outcome == "partial_failure"
    assert cov.repair.outcome == "completed" and d.tools.count("generate") == 1 and result.final == {"text": "the answer"}
    failed = [r for r in result.step_records if r.operator == "operator_check" and not r.ok]
    assert len(failed) == 1


@pytest.mark.asyncio
async def test_scenario_3_a_failed_repair_retrieval_leaves_the_first_ledger_in_force() -> None:
    d = RepairDispatcher(fail_repair_search=True)
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "partial" and cov.unresolved_facet_ids == ("f3",)
    assert cov.repair.outcome == "retrieval_failed" and d.tools.count("operator_check") == 2
    assert d.tools.count("generate") == 1
    failed = [r for r in result.step_records if r.operator == "search" and not r.ok and r.step_index >= result.total_planned_steps]
    assert len(failed) == 1 and failed[0].source == "sql", "the failed repair call is still one accounted step, in the existing vocabulary"


@pytest.mark.asyncio
async def test_a_failed_reassessment_when_everything_else_was_covered_is_partial_not_unassessed() -> None:
    def check(items: str, instruction: str) -> dict:
        if REPAIR_TEXT in items:
            return RuntimeError("down")  # type: ignore[return-value]
        return by_topic()(items, instruction)

    d = RepairDispatcher(check)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3))))
    assert cov.state != "unassessed" and cov.reason == "unresolved_facets"


# ── scenario 4: repair is unaffordable ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_4_a_deadline_with_too_little_left_skips_repair_and_generates_once() -> None:
    d = RepairDispatcher()
    result = await _run(d, CoverageBoundary(request=_request(3)), deadline=time.monotonic() + 40.0)
    cov = _cov(result)
    assert cov.ledger.assessment.calls == 2, "the assessment itself fit"
    assert cov.repair.outcome == "skipped_deadline" and d.repair_searches() == []
    assert d.tools.count("generate") == 1 and cov.state == "partial" and cov.unresolved_facet_ids == ("f3",)


@pytest.mark.asyncio
async def test_a_deadline_with_room_pays_for_the_repair() -> None:
    d = RepairDispatcher()
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3)), deadline=time.monotonic() + 1000.0))
    assert cov.repair.outcome == "completed" and cov.state == "complete"


@pytest.mark.asyncio
async def test_scenario_4_a_usd_allowance_too_small_for_the_reassessment_skips_repair() -> None:
    """The two assessment calls cost 0.5 each; 0.05 is left, and one reassessment call would cost about 0.5."""
    d = RepairDispatcher(usage_cost=0.5)
    result = await _run(d, CoverageBoundary(request=_request(3)), budget_usd_remaining=1.05)
    cov = _cov(result)
    assert cov.repair.outcome == "skipped_budget" and d.repair_searches() == []
    assert d.tools.count("generate") == 1 and cov.state == "partial"


@pytest.mark.asyncio
async def test_a_spent_usd_allowance_skips_repair_and_the_skip_follows_the_state_table() -> None:
    d = RepairDispatcher(by_topic(never=("f1", "f2", "f3")), usage_cost=0.5)
    result = await _run(d, CoverageBoundary(request=_request(3)), budget_usd_remaining=1.0)
    cov = _cov(result)
    assert cov.repair.outcome == "skipped_budget" and d.repair_searches() == []
    assert (cov.state, cov.reason, cov.skip_generation) == ("unavailable", "uncovered", True)
    assert "generate" not in d.tools


@pytest.mark.asyncio
async def test_a_usd_allowance_with_room_pays_for_the_repair_and_the_shared_accumulator_sees_every_call() -> None:
    d = RepairDispatcher(usage_cost=0.1)
    result = await _run(d, CoverageBoundary(request=_request(3)), budget_usd_remaining=5.0)
    cov = _cov(result)
    assert cov.repair.outcome == "completed"
    spent = sum(r.cost_usd for r in result.step_records if r.cost_usd is not None)
    assert spent == pytest.approx(0.3), "prefix + both assessment rounds + repair retrieval, on one accumulator"


def test_the_repair_allowance_is_a_share_of_what_is_left_inside_a_cap_and_none_under_the_floor() -> None:

    assert repair_allowance(None) == REPAIR_ALLOWANCE_CAP_S, "no deadline: the cap alone"
    assert repair_allowance(1000.0) == REPAIR_ALLOWANCE_CAP_S
    assert repair_allowance(100.0) == pytest.approx(100.0 * REPAIR_DEADLINE_SHARE)
    assert repair_allowance(MIN_REPAIR_ALLOWANCE_S / REPAIR_DEADLINE_SHARE - 0.1) == 0.0
    assert repair_allowance(0.0) == 0.0 and repair_allowance(-5.0) == 0.0
    assert REPAIR_DEADLINE_SHARE < 0.5, "generation keeps the larger part of what is left"


def test_usd_affordability_needs_room_for_the_estimated_reassessment() -> None:

    assert usd_affordable(None, None) and usd_affordable(None, 9.9), "no allowance set: nothing to run out of"
    assert not usd_affordable(0.0, None) and not usd_affordable(-1.0, 0.1), "spent is spent"
    assert usd_affordable(0.01, None), "an unknown cost never fabricates a refusal"
    assert not usd_affordable(0.05, 0.5) and usd_affordable(0.5, 0.5)


# ── the queries ──────────────────────────────────────────────────────────────────────────────────────────────


def _facets(*ids: str) -> list[Facet]:
    return [Facet(i, FACET_TEXT.format(n=i[1:])) for i in ids]


def test_queries_target_the_primary_source_first_then_the_plans_own_scope_and_stop_at_the_cap() -> None:

    qs = build_repair_queries(
        _facets("f2", "f3"), primary_collections=[RDR_COLL],
        plan_targets=[RepairTarget("rdr,docs"), RepairTarget("code")], max_queries=5, limit=4,
    )
    assert [(q.origin, q.facet_id, q.args.get("corpus")) for q in qs] == [
        ("primary", "f2", RDR_COLL), ("primary", "f3", RDR_COLL),
        ("plan", "f2", "rdr,docs"), ("plan", "f3", "rdr,docs"), ("plan", "f2", "code"),
    ]
    assert all(q.tool == "search" and q.args["limit"] == 4 and q.args["query"].startswith("what the document") for q in qs)
    assert build_repair_queries(_facets("f2"), primary_collections=[], plan_targets=[], max_queries=5, limit=4) == []
    assert build_repair_queries(_facets("f2"), primary_collections=[RDR_COLL], plan_targets=[], max_queries=0, limit=4) == []


def test_a_subtree_scoped_target_uses_the_query_tool_with_its_subtree_and_duplicate_targets_collapse() -> None:

    qs = build_repair_queries(
        _facets("f3"), primary_collections=[], plan_targets=[RepairTarget("rdr", "1.2"), RepairTarget("rdr", "1.2"), RepairTarget("rdr")],
        max_queries=6, limit=5,
    )
    assert [(q.tool, q.args.get("subtree"), "question" in q.args) for q in qs] == [("query", "1.2", True), ("search", None, False)]


@pytest.mark.asyncio
async def test_scenario_1_with_a_named_source_the_primary_collection_is_asked_before_the_plans_corpus() -> None:
    d = RepairDispatcher(hydrates=True)
    boundary = CoverageBoundary(request=_request(3), primary=_primary())
    await _run(d, boundary, reserved_stream=_reserved())
    corpora = [a["corpus"] for a in d.repair_searches()]
    assert corpora[0] == RDR_COLL and "rdr" in corpora[1:], "the named source's collection first, then the plan's own corpus"


@pytest.mark.asyncio
async def test_a_primary_collection_outside_the_callers_corpus_scope_is_not_queried() -> None:
    d = RepairDispatcher(hydrates=True)
    boundary = CoverageBoundary(request=_request(3), primary=_primary(), scope=ScopeConstraint.parse("docs"))
    await _run(d, boundary, reserved_stream=_reserved())
    assert RDR_COLL not in [a["corpus"] for a in d.repair_searches()], "an explicit scope bounds the repair too"


@pytest.mark.asyncio
async def test_the_number_of_repair_queries_and_merged_items_is_capped() -> None:

    many = [f"{OTHER_REPAIR_TEXT} {i}" for i in range(MAX_REPAIR_ITEMS + 8)]
    d = RepairDispatcher(by_topic(never=tuple(f"f{i}" for i in range(1, 7))), repair_hits=many, hydrates=True)
    result = await _run(d, CoverageBoundary(request=_request(6), primary=_primary()), reserved_stream=_reserved())
    cov = _cov(result)
    assert len(d.repair_searches()) == cov.repair.queries == MAX_REPAIR_QUERIES, "six facets, two targets: twelve candidate queries, six asked"
    assert cov.repair.items_merged == MAX_REPAIR_ITEMS and cov.repair.items_found > MAX_REPAIR_ITEMS


# ── the merge: after the reserved stream, ahead of the plan's own items, inside the same caps ───────────────


def _supplement(*texts: str):

    return RepairSupplement(items=tuple(SupplementItem(text=t, chash=_h(t), collection=RDR_COLL, tumbler="1.1.7") for t in texts))


def _hydrate_generate(secondary: list[str], supp, *, reserved=None) -> list[str]:

    with reserved_scope(reserved), supplement_scope(supp), capture_reducer_input() as captured:
        runner._mark_reserved_step(0, [{"tool": "generate", "args": {"context": list(secondary)}}])
        runner._hydrate_operator_args("generate", {"context": list(secondary), "question": "q"})
    return captured[-1]


def test_supplement_items_lead_the_plans_own_items_and_follow_the_reserved_ones() -> None:
    plain = _hydrate_generate(["s1", "s2"], _supplement("r1", "r2"))
    assert plain == ["r1", "r2", "s1", "s2"]
    stream = _reserved()
    merged = _hydrate_generate(["s1", "s2"], _supplement("r1"), reserved=stream)
    assert [m for m in merged if not m.startswith("[primary")] == ["r1", "s1", "s2"]
    assert merged.index("r1") > max(i for i, m in enumerate(merged) if m.startswith("[primary")), "after every reserved item"


def test_the_positional_cap_cuts_the_plans_tail_before_repair_evidence() -> None:
    secondary = [f"s{i}" for i in range(runner._OPERATOR_MAX_INPUTS + 5)]
    out = _hydrate_generate(secondary, _supplement("r1", "r2"))
    assert len(out) == runner._OPERATOR_MAX_INPUTS and out[:2] == ["r1", "r2"]
    assert out[-1] == f"s{runner._OPERATOR_MAX_INPUTS - 3}", "the same cap, cut from the tail of the plan's own items"


def test_a_plan_item_already_in_the_supplement_is_not_merged_twice() -> None:
    out = _hydrate_generate(["[source: x]\nrepeated", "other"], _supplement("repeated"))
    assert [t.removeprefix("[source: x]\n") for t in out].count("repeated") == 1 and out[0] == "repeated"


def test_the_supplement_merges_once_at_one_step_and_again_on_rehydration_of_that_step() -> None:

    supp = _supplement("r1")
    with supplement_scope(supp):
        steps = [{"tool": "generate", "args": {"context": ["a"]}}, {"tool": "summarize", "args": {"content": ["b"]}}]
        for _ in range(2):  # the same step hydrated twice (the oversized-bundle fallback, the envelope replay)
            runner._mark_reserved_step(0, steps)
            _, first = runner._hydrate_operator_args("generate", {"context": ["a"]})
            assert "r1" in first["context"]
        runner._mark_reserved_step(1, steps)
        _, later = runner._hydrate_operator_args("summarize", {"content": ["b"]})
        assert "r1" not in later["content"], "a later operator is not handed the supplement again"


# ── the ledger: stable ids, keep rules, failure fallback ────────────────────────────────────────────────────


def test_known_texts_keep_their_item_ids_and_new_texts_take_the_next_free_ones() -> None:

    got = build_anchors(
        ["new", "old b", "old a"], step_outputs=[], tools=[], reserved=None, known={"old a": "e1", "old b": "e2"},
    )
    assert [(a.item_id, a.content) for a in got] == [("e3", "new"), ("e2", "old b"), ("e1", "old a")]


def test_build_anchors_attributes_a_supplement_item_to_its_stream_collection_and_document() -> None:

    supp = _supplement("repair text")
    (a,) = build_anchors(["repair text"], step_outputs=[], tools=[], reserved=None, supplement=supp)
    assert (a.stream, a.kind, a.collection, a.tumbler, a.chash) == ("vector", "chunk", RDR_COLL, "1.1.7", _h("repair text"))


async def _first_result(d: RepairDispatcher, n: int = 3) -> tuple[CoverageBoundary, CoverageResult, BoundaryView]:
    """The first ledger of a run with repair switched off, and a view that adds the repair item to its evidence."""
    result = await _run(d, CoverageBoundary(request=_request(n), max_repair_queries=0))
    cov = _cov(result)

    async def call(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await d("operator_check", args)

    view = BoundaryView(
        evidence_texts=[a.content for a in cov.ledger.anchors] + [REPAIR_TEXT], observed=True, step_outputs=list(result.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=call, remaining_seconds=lambda: None, remaining_usd=lambda: None,
    )
    return CoverageBoundary(request=_request(n)), cov, view


@pytest.mark.asyncio
async def test_a_prior_finding_whose_evidence_left_the_input_is_reassessed_not_kept() -> None:
    d = RepairDispatcher()
    boundary, first, view = await _first_result(d)
    assert first.ledger.finding("f1").state == "covered"
    f1_ids = set(first.ledger.finding("f1").evidence_ids)
    dropped = [a.content for a in first.ledger.anchors if a.item_id in f1_ids]
    view.evidence_texts = [t for t in view.evidence_texts if t not in dropped]
    again = await boundary.evaluate(view, prior=first)
    assert again.ledger.finding("f1").state == "unsupported", "a covered finding is never kept on evidence the reducer no longer holds"


@pytest.mark.asyncio
async def test_a_failed_reassessment_keeps_each_prior_finding_that_was_known() -> None:
    def fails_on_repair(items: str, instruction: str) -> dict:
        if REPAIR_TEXT in items:
            return RuntimeError("down")  # type: ignore[return-value]
        return by_topic()(items, instruction)

    boundary, first, view = await _first_result(RepairDispatcher(fails_on_repair))
    again = await boundary.evaluate(view, prior=first)
    assert [f.state for f in again.ledger.findings] == [f.state for f in first.ledger.findings], "the first ledger stands"
    assert again.state == first.state and again.ledger.rounds == 2
    assert again.ledger.assessment.outcome == "partial_failure"


def test_two_rounds_sum_and_a_later_failure_after_an_ok_round_is_a_partial_failure() -> None:

    ok = AssessmentReport(outcome="ok", calls=2, facets_per_call=(3, 3))
    failed = AssessmentReport(outcome="failed", calls=1, facets_per_call=(1,))
    merged = ok.merged_with(failed)
    assert (merged.outcome, merged.calls, merged.facets_per_call) == ("partial_failure", 3, (3, 3, 1))
    assert ok.merged_with(AssessmentReport(outcome="skipped_deadline")).outcome == "partial_failure"
    assert AssessmentReport(outcome="failed").merged_with(AssessmentReport(outcome="ok", calls=1)).outcome == "ok"


@pytest.mark.asyncio
async def test_the_counts_the_run_summary_reads_carry_the_repair_usage() -> None:
    d = RepairDispatcher()
    result = await _run(d, CoverageBoundary(request=_request(3)))
    counts = _cov(result).counts()
    assert counts["repair_outcome"] == "completed" and counts["repair_queries"] == 1
    assert counts["repair_items_merged"] == 1 and counts["repair_facets"] == 1 and counts["rounds"] == 2
    wire = _cov(result).to_wire()
    assert wire["repair"]["outcome"] == "completed" and "text" not in json.dumps(wire["repair"])
    assert FACET_TEXT.format(n=3) not in json.dumps(wire), "no facet text on the wire"


# ── the bundled terminal route ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_5_a_bundled_terminal_gets_the_repair_evidence_and_the_same_coverage_state() -> None:
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}
    seen: list[Any] = []

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        seen.append(bundle)
        return {"text": "bundled answer"}

    d = RepairDispatcher()
    wrapper = _Wrapper(d.__call__)
    wrapper.supports_bundling = True  # type: ignore[attr-defined]
    with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        bundled = await plan_run(_match(plan), {}, dispatcher=wrapper, coverage_boundary=CoverageBoundary(request=_request(3)))
    headless = _cov(await _run(RepairDispatcher(), CoverageBoundary(request=_request(3))))
    cov = _cov(bundled)
    assert len(seen) == 1 and REPAIR_TEXT in seen[0].steps[0].args["items"], "the one fused call carries the repair evidence"
    assert d.tools.count("generate") == 0 and d.tools.count("traverse") == 1
    assert (cov.state, cov.reason, cov.counts()["facets"]) == (headless.state, headless.reason, headless.counts()["facets"])
    assert cov.repair.outcome == headless.repair.outcome == "completed"


# ── scenario 5: the continuation route carries the same coverage state ─────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_5_a_continuation_cut_runs_the_boundary_and_carries_the_same_coverage_state() -> None:
    boundary = CoverageBoundary(request=_request(3))
    headless = await _run(RepairDispatcher(), boundary)
    d = RepairDispatcher()
    cut = await _run(d, boundary, continuation_cut_at_step=3)
    assert cut.continuation_cut_applied is True and "generate" not in d.tools, "the suffix was not dispatched"
    assert cut.coverage is not None, "no revert to unassessed at the hand-off"
    h, c = _cov(headless), _cov(cut)
    assert (c.state, c.reason) == (h.state, h.reason) == ("complete", "all_covered")
    assert [f.to_wire() for f in c.ledger.findings] == [f.to_wire() for f in h.ledger.findings]
    assert c.repair.outcome == "completed" and len(d.repair_searches()) == 1 and d.tools.count("traverse") == 1
    assert cut.repair_supplement is not None and [i.text for i in cut.repair_supplement.items] == [REPAIR_TEXT]


@pytest.mark.asyncio
async def test_scenario_5_a_bundled_terminal_cut_runs_the_boundary_and_dispatches_no_bundle() -> None:
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}

    async def fail_bundle(bundle, **kwargs):  # noqa: ARG001
        raise AssertionError("the suffix must be handed off, not dispatched")

    d = RepairDispatcher()
    wrapper = _Wrapper(d.__call__)
    wrapper.supports_bundling = True  # type: ignore[attr-defined]
    with patch("nexus.plans.bundle.dispatch_bundle", fail_bundle):
        cut = await plan_run(_match(plan), {}, dispatcher=wrapper, coverage_boundary=CoverageBoundary(request=_request(3)),
                             continuation_cut_at_step=3)
    cov = _cov(cut)
    assert cut.continuation_cut_applied is True and (cov.state, cov.repair.outcome) == ("complete", "completed")
    assert cut.repair_supplement is not None and [i.text for i in cut.repair_supplement.items] == [REPAIR_TEXT]


@pytest.mark.asyncio
async def test_a_continuation_cut_that_the_ledger_skips_hands_nothing_off() -> None:
    d = RepairDispatcher(by_topic(never=("f1", "f2", "f3")))
    result = await _run(d, CoverageBoundary(request=_request(3)), continuation_cut_at_step=3)
    assert _cov(result).skip_generation and result.continuation_cut_applied is False
    assert result.final is not None and result.final.get("coverage_skipped") is True, "the caller gets the skipped terminal, not an envelope"


@pytest.mark.asyncio
async def test_a_continuation_run_without_a_boundary_is_unchanged() -> None:
    d = RepairDispatcher()
    result = await _run(d, None, continuation_cut_at_step=3)
    assert result.continuation_cut_applied is True and result.coverage is None and result.repair_supplement is None


@pytest.mark.asyncio
async def test_scenario_5_the_envelope_carries_the_coverage_state_and_the_repair_evidence() -> None:

    steps = [*PLAN["steps"][:3], {"tool": "generate", "args": {"template": "answer", "context": "$step3.contents"}}]
    d = RepairDispatcher()
    cut = await _run(d, CoverageBoundary(request=_request(3)), plan={"steps": steps}, continuation_cut_at_step=3)
    classified = classify_continuation_cut(steps)
    wire = _cov(cut).to_wire()
    with supplement_scope(cut.repair_supplement):
        env = assemble_continuation_envelope(
            cut=classified, steps=steps, bindings={}, step_outputs=cut.steps, plan_id=7, run_id=None, coverage=wire,
        )
    assert env is not None and env["coverage"]["state"] == "complete" and env["coverage"]["repair"]["outcome"] == "completed"
    assert REPAIR_TEXT in env["reduction_spec"]["prompt"], "the hand-off prompt holds what the boundary assessed"
    bare = assemble_continuation_envelope(cut=classified, steps=steps, bindings={}, step_outputs=cut.steps, plan_id=7, run_id=None)
    assert bare is not None and bare["coverage"] is None and REPAIR_TEXT not in bare["reduction_spec"]["prompt"], (
        "without the supplement in scope the evidence is absent: the caller must re-enter it")


# ── nothing changes when no repair is wanted ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_complete_first_ledger_makes_no_repair_call_and_no_extra_step() -> None:
    d = RepairDispatcher(by_topic(topic={"f3": "decision"}))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "complete" and cov.repair.outcome == "not_attempted" and cov.ledger.rounds == 1
    assert d.repair_searches() == [] and _repair_records(result) == []


@pytest.mark.asyncio
async def test_the_switch_off_keeps_the_first_ledger_without_any_repair() -> None:
    d = RepairDispatcher()
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3), max_repair_queries=0)))
    assert d.repair_searches() == [] and cov.state == "partial" and cov.repair.outcome == "not_attempted"


@pytest.mark.asyncio
async def test_a_view_with_no_retrieval_hooks_cannot_repair_and_says_so() -> None:
    d = RepairDispatcher()
    boundary, first, view = await _first_result(d)
    view.evidence_texts = [a.content for a in first.ledger.anchors]
    run = await boundary.run(view)
    assert run.repair.outcome == "unavailable" and run.state == first.state


# ── the ids shape and the real default dispatcher ───────────────────────────────────────────────────────────


def test_the_supplement_merges_in_the_ids_shape_too_ahead_of_the_hydrated_items() -> None:

    def stub_get_many(ids, collections="knowledge", structured=False, **kw):  # noqa: ARG001
        return {"contents": ["s0", "s1", "s2"], "missing": []}

    steps = [{"tool": "generate", "args": {"ids": ["a", "b", "c"], "collections": "knowledge"}}]
    with patch.object(mcp_core, "store_get_many", stub_get_many), supplement_scope(_supplement("r1")), \
            capture_reducer_input() as captured:
        runner._mark_reserved_step(0, steps)
        _, args = runner._hydrate_operator_args("generate", {"ids": ["a", "b", "c"], "collections": "knowledge", "question": "q"})
    assert captured[-1] == ["r1", "s0", "s1", "s2"]
    assert args["context"].startswith("r1\n\n"), "the operator is handed the merged list"


@pytest.mark.asyncio
async def test_scenario_1_under_the_real_default_dispatcher_the_repair_evidence_reaches_generation() -> None:

    texts = {_h(t): t for t in (*VECTOR_TEXTS, REPAIR_TEXT)}
    seen: dict[str, Any] = {"hydrated": [], "generate": [], "prehydrated": [], "search": []}
    real_hydrate = runner._hydrate_operator_args

    def spy(tool: str, args: dict):
        seen["hydrated"].append(tool)
        return real_hydrate(tool, args)

    async def stub_search(**kwargs):
        seen["search"].append(kwargs)
        return _structured([REPAIR_TEXT] if kwargs["query"].startswith("what the document") else VECTOR_TEXTS, RDR_COLL)

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        ids = ids if isinstance(ids, list) else [ids]
        return {"contents": [texts.get(str(i), "") for i in ids], "missing": []}

    async def stub_check(items, check_instruction, timeout=300.0, model=None):  # noqa: ARG001
        return by_topic()(items, check_instruction)

    async def stub_generate(**kwargs):
        seen["prehydrated"].append(runner.operator_args_prehydrated())
        seen["generate"].append(kwargs)
        return {"text": "the answer"}

    plan = {"steps": [PLAN["steps"][0], PLAN["steps"][2] | {"args": {"ids": "$step1.ids", "collections": "rdr"}},
                      {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}}]}
    with patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_check", stub_check), patch.object(mcp_core, "operator_generate", stub_generate), \
         patch.object(runner, "_hydrate_operator_args", spy):
        result = await plan_run(_match(plan), {}, bundle_operators=False, coverage_boundary=CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.state == "complete" and cov.repair.outcome == "completed"
    assert len(seen["generate"]) == 1 and seen["prehydrated"] == [True], "generation ran once, on the runner's own reading"
    assert seen["hydrated"].count("generate") == 2, "read for the boundary, and once more with the repair evidence merged; never by the dispatcher"
    context = seen["generate"][0]["context"]
    assert isinstance(context, str) and context.startswith(REPAIR_TEXT), "the repair evidence leads the plan's own"
    assert [k["query"] for k in seen["search"]].count(FACET_TEXT.format(n=3)) == 1
    assert runner.operator_args_prehydrated() is False


# ── nx_answer: the hand-off carries the coverage state ──────────────────────────────────────────────────────


def _handoff_patches(boundary: CoverageBoundary, check, generated: list):
    """The patch set the continuation tests of ``test_nx_answer.py`` use, plus a ``plan_run`` that adds the
    boundary (``nx_answer`` builds one only from nexus-jypmj.3.3 on)."""


    texts = {_h(t): t for t in (*VECTOR_TEXTS, REPAIR_TEXT)}
    real_plan_run = runner.plan_run

    async def with_boundary(match, bindings=None, **kw):
        return await real_plan_run(match, bindings, coverage_boundary=boundary, **kw)

    async def stub_search(**kwargs):
        return _structured([REPAIR_TEXT] if kwargs["query"].startswith("what the document") else VECTOR_TEXTS, RDR_COLL)

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        ids = ids if isinstance(ids, list) else [ids]
        return {"contents": [texts.get(str(i), "") for i in ids], "missing": []}

    async def stub_check(items, check_instruction, timeout=300.0, model=None):  # noqa: ARG001
        reply = check(items, check_instruction)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def stub_generate(**kwargs):
        generated.append(kwargs)
        return {"text": "the answer"}

    db_stub = MagicMock()
    db_stub.plans.save_plan = MagicMock(return_value=1)
    db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][2] | {"args": {"ids": "$step1.ids", "collections": "rdr"}},
        {"tool": "generate", "args": {"template": "answer", "context": "$step2.contents"}},
    ]}
    return db_stub, [
        patch("nexus.plans.matcher.plan_match", return_value=[_match(plan)]),
        patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)),
        patch("nexus.mcp.core.scratch", return_value="ok"),
        patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None),
        patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True),
        patch("nexus.plans.runner.plan_run", with_boundary),
        patch.object(mcp_core, "search", stub_search),
        patch.object(mcp_core, "store_get_many", stub_get_many),
        patch.object(mcp_core, "operator_check", stub_check),
        patch.object(mcp_core, "operator_generate", stub_generate),
    ]


async def _nx_answer_with_boundary(boundary: CoverageBoundary, check, **kw: Any) -> tuple[Any, list]:


    generated: list = []
    db_stub, patches = _handoff_patches(boundary, check, generated)
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        t2_ctx = stack.enter_context(patch("nexus.mcp.core._t2_ctx"))
        t2_ctx.return_value.__enter__.return_value = db_stub
        result = await nx_answer(question="q", continuation=True, structured=True, **kw)
    return result, generated


@pytest.mark.asyncio
async def test_scenario_5_nx_answer_hands_off_with_the_coverage_state_and_the_repair_evidence() -> None:
    result, generated = await _nx_answer_with_boundary(CoverageBoundary(request=_request(3)), by_topic())
    env = result["continuation"]
    assert env is not None and generated == [], "the suffix was handed off, not run"
    assert env["coverage"]["state"] == "complete" and env["coverage"]["repair"]["outcome"] == "completed"
    assert env["coverage"]["rounds"] == 2 and env["cut_at_step"] == 2
    assert REPAIR_TEXT in env["reduction_spec"]["prompt"], "the caller reduces over the evidence the ledger assessed"
    assert FACET_TEXT.format(n=3) not in json.dumps(env["coverage"]), "no facet text on the wire"


@pytest.mark.asyncio
async def test_scenario_5_nx_answer_hand_off_carries_the_same_state_as_the_headless_run() -> None:
    boundary = CoverageBoundary(request=_request(3))
    handed, _ = await _nx_answer_with_boundary(boundary, by_topic())
    headless = await _run(RepairDispatcher(), boundary)
    wire = handed["continuation"]["coverage"]
    assert (wire["state"], wire["reason"]) == (_cov(headless).state, _cov(headless).reason)
    assert [(f["facet_id"], f["state"]) for f in wire["findings"]] == [(f.facet_id, f.state) for f in _cov(headless).ledger.findings]


@pytest.mark.asyncio
async def test_a_skipped_generation_is_not_handed_off_and_does_not_rerun_the_plan() -> None:
    result, generated = await _nx_answer_with_boundary(CoverageBoundary(request=_request(3)), by_topic(never=("f1", "f2", "f3")))
    assert result["continuation"] is None and generated == [], "no envelope, and the terminal reduction never ran"


@pytest.mark.asyncio
async def test_a_hand_off_without_a_boundary_has_a_null_coverage() -> None:

    generated: list = []

    db_stub, patches = _handoff_patches(CoverageBoundary(request=None), by_topic(), generated)
    with contextlib.ExitStack() as stack:
        for p in patches[:5] + patches[6:]:  # everything but the plan_run wrapper
            stack.enter_context(p)
        t2_ctx = stack.enter_context(patch("nexus.mcp.core._t2_ctx"))
        t2_ctx.return_value.__enter__.return_value = db_stub
        result = await nx_answer(question="q", continuation=True, structured=True)
    assert result["continuation"] is not None and result["continuation"]["coverage"] is None


# ── generation is told which facets are unresolved ──────────────────────────────────────────────────────────


def _note_of(context: str) -> str:
    """The coverage note inside a joined generation context ('' when there is none)."""
    return next((chunk for chunk in context.split("\n\n") if chunk.startswith("[coverage note")), "")


@pytest.mark.asyncio
async def test_scenario_1_generation_is_handed_the_unresolved_facets_after_the_one_repair() -> None:
    d = RepairDispatcher(by_topic(topic={"f4": "a word no item holds"}))
    result = await _run(d, CoverageBoundary(request=_request(4)))
    cov = _cov(result)
    assert cov.unresolved_facet_ids == ("f4",) and d.tools.count("generate") == 1
    context = d.args_of("generate")[0]["context"]
    note = _note_of(context)
    assert "f4" in note and FACET_TEXT.format(n=4) in note, "the unresolved facet is named, with its wording"
    for covered in ("f1", "f2", "f3"):
        assert FACET_TEXT.format(n=int(covered[1:])) not in note, "a covered facet is not listed"
    assert context.index(note) < context.index(REPAIR_TEXT) < context.index(VECTOR_TEXTS[0]), (
        "the note leads the repair evidence, which leads the plan's own")
    assert cov.repair.noted_facets == 1
    assert not any(a.content.startswith("[coverage note") for a in cov.ledger.anchors), "the note is not evidence and not an anchor"


@pytest.mark.asyncio
async def test_a_complete_ledger_adds_no_note() -> None:
    d = RepairDispatcher()
    cov = _cov(await _run(d, CoverageBoundary(request=_request(3))))
    assert cov.state == "complete" and _note_of(d.args_of("generate")[0]["context"]) == "" and cov.repair.noted_facets == 0


@pytest.mark.asyncio
async def test_scenario_2_a_contradicted_facet_is_named_to_generation_as_contradicted() -> None:
    d = RepairDispatcher(by_topic(contradict=("f1",), topic={"f3": "gate"}), hydrates=False)
    result = await _run(d, CoverageBoundary(request=_request(3)))
    note = _note_of(d.args_of("generate")[0]["context"])
    assert "f1" in note and "contradict" in note.lower() and "f2" not in note and "f3" not in note
    assert d.repair_searches() == [], "naming it to generation is not a search for agreement"
    assert _cov(result).repair.noted_facets == 1


@pytest.mark.asyncio
async def test_scenario_4_an_unaffordable_repair_still_tells_generation_what_is_unresolved() -> None:
    d = RepairDispatcher()
    result = await _run(d, CoverageBoundary(request=_request(3)), deadline=time.monotonic() + 40.0)
    note = _note_of(d.args_of("generate")[0]["context"])
    assert _cov(result).repair.outcome == "skipped_deadline" and "f3" in note and REPAIR_TEXT not in d.args_of("generate")[0]["context"]


@pytest.mark.asyncio
async def test_a_skipped_generation_has_nothing_to_tell() -> None:
    d = RepairDispatcher(by_topic(never=("f1", "f2", "f3")))
    result = await _run(d, CoverageBoundary(request=_request(3)))
    assert _cov(result).skip_generation and "generate" not in d.tools and _cov(result).repair.noted_facets == 0


@pytest.mark.asyncio
async def test_the_hand_off_prompt_lists_the_unresolved_facets_too() -> None:
    result, _ = await _nx_answer_with_boundary(CoverageBoundary(request=_request(4)), by_topic())
    env = result["continuation"]
    assert env is not None and env["coverage"]["state"] == "partial" and env["coverage"]["repair"]["noted_facets"] == 1
    assert "[coverage note" in env["reduction_spec"]["prompt"] and FACET_TEXT.format(n=4) in env["reduction_spec"]["prompt"]
    assert FACET_TEXT.format(n=4) not in json.dumps(env["coverage"]), "no facet text on the wire"


def test_a_supplement_note_leads_its_items_and_alone_is_enough_to_merge() -> None:
    supp = RepairSupplement(items=(SupplementItem(text="r1"),), note="N")
    assert supp.merge(["s1"]) == ["N", "r1", "s1"]
    only_note = RepairSupplement(note="N")
    assert only_note.should_merge() and only_note.merge(["s1"]) == ["N", "s1"]
    assert not RepairSupplement().should_merge()
    only_note.install_note("")
    assert not only_note.should_merge(), "a withdrawn note merges nothing"


@pytest.mark.asyncio
async def test_a_ledger_with_no_evidence_gets_no_note_so_the_zero_evidence_path_still_answers() -> None:
    """A note is an item; on a reduction with nothing to read it would defeat the runner's zero-evidence
    short-circuit and send the operator a prompt holding only the note."""
    seen: dict[str, list] = {"generate": []}

    async def stub_search(**kwargs):  # noqa: ARG001
        return _structured(VECTOR_TEXTS, RDR_COLL)

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        ids = ids if isinstance(ids, list) else [ids]
        return {"contents": ["" for _ in ids], "missing": []}

    async def stub_generate(**kwargs):
        seen["generate"].append(kwargs)
        return {"text": "should not run"}

    plan = {"steps": [PLAN["steps"][0], {"tool": "generate", "args": {"ids": "$step1.ids", "collections": "rdr", "template": "t"}}]}
    with patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_generate", stub_generate):
        result = await plan_run(_match(plan), {}, bundle_operators=False, coverage_boundary=CoverageBoundary(request=_request(2)))
    cov = _cov(result)
    assert (cov.state, cov.reason) == ("unavailable", "no_evidence") and cov.repair.noted_facets == 0
    assert seen["generate"] == [], "the short-circuit answered; no operator ran on a prompt of nothing"
    assert str(result.final["text"]).startswith(runner._NO_EVIDENCE_RESULT_PREFIX)


@pytest.mark.asyncio
async def test_a_note_the_reduction_input_does_not_hold_is_withdrawn_and_not_counted() -> None:
    """A plan shape with no list-valued evidence argument cannot carry the note; the ledger must not claim it did."""
    boundary, first, view = await _first_result(RepairDispatcher(by_topic(topic={"f3": "a word no item holds"})))
    view.evidence_texts = [a.content for a in first.ledger.anchors]
    sent: list[str] = []
    view.install_note = sent.append
    view.rehydrate = lambda: (list(view.evidence_texts), True)
    run = await boundary.run(view)
    assert run.state == "partial" and run.repair.noted_facets == 0
    assert len(sent) == 2 and sent[0].startswith("[coverage note") and sent[1] == "", "installed, found missing, withdrawn"


@pytest.mark.asyncio
async def test_a_repair_chunk_the_evidence_hygiene_excludes_is_not_merged() -> None:
    """The fetch applies the same hygiene an operator's own ``ids`` get: a chunk of a non-evidentiary document
    is dropped, so a repair cannot bring back what the plan's hydration would have refused."""
    d = RepairDispatcher()
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value={_h(REPAIR_TEXT)}):
        result = await _run(d, CoverageBoundary(request=_request(3)))
    cov = _cov(result)
    assert cov.repair.outcome == "no_new_evidence" and cov.state == "partial" and REPAIR_TEXT not in str(d.args_of("generate")[0])
