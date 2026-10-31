# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-jypmj): the coverage boundary of a plan whose operators do not bundle.

The boundary reads the input of the FIRST step of the plan's trailing operator run: the step that consumes the
retrieved evidence. Bundled, that run is one segment and the boundary sits before it. A dispatcher without
``supports_bundling`` flattens the run into isolated steps, and the boundary used to sit before the LAST of them,
which reads a later operator's input (``summarize`` over ``$step3.aggregates``, model prose as dicts), so the ledger
ended ``unavailable`` / ``no_evidence`` with no anchors although evidence had reached the reduction.

The boundary also reads the same evidence the bundled one does (the union of every run step's evidence args, deferred
refs excluded), reaches the same state and reason, and the repair evidence reaches every step of the run that reads
retrieved evidence directly (the unbundled run and the oversized-bundle per-step fallback), not only the first.

Real ``plan_run``, real default dispatcher, real boundary and ledger; only the ``nexus.mcp.core`` tools that reach
a model or an engine are stubbed. The plans are the builtin templates, read from ``conexus/plans/builtin``, and a few
inline ones.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from nexus.mcp import core as mcp_core
from nexus.plans import runner
from nexus.plans.bundle import BUNDLED_INTERMEDIATE, MAX_BUNDLE_PROMPT_CHARS as _REAL_MAX_BUNDLE_PROMPT_CHARS
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.match import Match
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.runner import PlanResult, plan_run

BUILTIN = pathlib.Path(__file__).parent.parent / "conexus" / "plans" / "builtin"
TEXTS = ["chunk about the chash identity gap", "chunk about the options weighed", "chunk about what was left open"]
#: The second branch of the two-branch probe plan: a search for ``q2`` reads these, so a first-step read and a union
#: read of the run's evidence differ.
TEXTS_B = ["branch B chunk about the later review", "branch B chunk about the closing question"]
#: What only the coverage repair retrieves (a search for the facet's own text), and the only item the repair check supports.
REPAIR_TEXT = "repair hit: the gap it found is stated here"
FACET_TEXT = "the gap it found"
COLL = "rdr__1-1__voyage-context-3__v1"
SKIPPED_MARK = "coverage_skipped"


def _h(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


#: An inline plan (nexus-jypmj, the measured S06 shape): two retrievals, two fetches, then extract and generate.
INLINE_PLANS: dict[str, dict[str, Any]] = {
    "extract-generate": {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "search", "args": {"query": "q2", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "store_get_many", "args": {"ids": "$step2.ids", "collections": "$step2.collections"}},
        {"tool": "extract", "args": {"inputs": ["$step3.contents", "$step4.contents"], "fields": "decision"}},
        {"tool": "generate", "args": {"context": "$step5.extractions", "question": "q"}},
    ]},
    #: The plan's own operator_check directly before the terminal operator: both are the trailing operator run.
    "check-generate": {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "check", "args": {"items": "$step2.contents", "check_instruction": "do the chunks say anything"}},
        {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}},
    ]},
    #: Two retrieval branches, each fetched, then an extract over branch A and a generate over branch B plus the extract:
    #: the evidence of the run is A plus B, the generate's second element is a deferred ref when the run bundles.
    "two-branch-probe": {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
        {"tool": "search", "args": {"query": "q2", "corpus": "rdr"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "$step1.collections"}},
        {"tool": "store_get_many", "args": {"ids": "$step2.ids", "collections": "$step2.collections"}},
        {"tool": "extract", "args": {"inputs": "$step3.contents", "fields": "decision"}},
        {"tool": "generate", "args": {"context": ["$step4.contents", "$step5.extractions"], "question": "q"}},
    ]},
}


def _plan(name: str) -> dict[str, Any]:
    if name in INLINE_PLANS:
        return INLINE_PLANS[name]
    return yaml.safe_load((BUILTIN / f"{name}.yml").read_text())["plan_json"]


def _match(name: str, bindings: dict[str, Any]) -> tuple[Match, dict[str, Any]]:
    return Match(
        plan_id=465, name=name, description="d", confidence=0.9, dimensions={"verb": "query"}, tags="",
        plan_json=json.dumps(_plan(name)), required_bindings=list(bindings), optional_bindings=[],
        default_bindings={}, parent_dims=None,
    ), bindings


def _request() -> RequestCoverage:
    return RequestCoverage(answer_kind="synthesis", facets=(Facet("f1", "the gap it found", "clause", (), ()),))


def _structured(texts: list[str]) -> dict[str, Any]:
    ids = [_h(t) for t in texts]
    return {
        "ids": ids, "tumblers": [f"1.1.{i}" for i in range(len(ids))], "distances": [0.1] * len(ids),
        "collections": [COLL], "chunk_collections": [COLL] * len(ids), "chunk_text_hash": list(ids),
    }


#: Every text a fetch can return, by chash. A fetch answers per id, so two fetch calls return different texts.
KNOWN = {_h(t): t for t in (*TEXTS, *TEXTS_B, REPAIR_TEXT)}


def _retrieve_for(*, repair: bool):
    async def retrieve(**kw: Any) -> dict[str, Any]:
        text = str(kw.get("query") or kw.get("question") or "")
        if repair and text.startswith(FACET_TEXT):
            return _structured([REPAIR_TEXT])
        return _structured(TEXTS_B if text == "q2" else TEXTS)
    return retrieve


async def _traverse(**kw: Any) -> dict[str, Any]:
    return {"ids": [], "tumblers": [], "collections": [], "traversal": {"policy_applied": True, "widened": None}}


def _get_many(**kw: Any) -> dict[str, Any]:
    ids = kw["ids"] if isinstance(kw["ids"], list) else [kw["ids"]]
    return {"contents": [KNOWN.get(str(i), "") for i in ids]}


async def _groupby(**kw: Any) -> dict[str, Any]:
    items = json.loads(kw["items"]) if isinstance(kw.get("items"), str) else kw.get("items")
    return {"groups": [{"key_value": "identity", "items": items}]}


async def _aggregate(**kw: Any) -> dict[str, Any]:
    return {"aggregates": [{"key_value": "identity", "summary": "RDR-086 found a gap in chunk identity."}]}


async def _text(**kw: Any) -> dict[str, Any]:
    return {"text": "the answer"}


async def _extract(**kw: Any) -> dict[str, Any]:
    return {"extractions": [{"decision": "d", "rationale": "r", "rdr": "RDR-086"}]}


async def _rank(**kw: Any) -> dict[str, Any]:
    return {"ranked": json.loads(kw["items"])}


class Calls:
    """What ran: ``ran`` the plan's own operators in order, ``timeline`` those plus the coverage assessments and the
    fused bundle dispatch, ``args`` the keyword arguments each operator was handed."""

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.timeline: list[str] = []
        self.args: dict[str, list[dict[str, Any]]] = {}
        self.bundles: list[Any] = []

    def count(self, name: str, fn: Any):
        async def wrapped(**kw: Any) -> Any:
            self.ran.append(name)
            self.timeline.append(name)
            self.args.setdefault(name, []).append(kw)
            out = fn(**kw)
            return await out if hasattr(out, "__await__") else out
        return wrapped

    def check(self, mode: str):
        """``operator_check``: the coverage assessment (its instruction lists ``[fN]`` facets) or the plan's own check.
        *mode*: ``supports`` every facet, ``neutral`` none, or ``repair`` (only the repair hit supports)."""
        async def check(items: str, check_instruction: str, **kw: Any) -> dict[str, Any]:
            tags = re.findall(r"^\[(f\d+)\]", check_instruction, flags=re.M)
            if not tags:
                self.ran.append("check")
                self.timeline.append("check")
                return {"ok": True, "evidence": []}
            self.timeline.append("assess")
            payload = json.loads(items)
            if mode == "repair":
                held = [p for p in payload if REPAIR_TEXT in p["text"]]
            else:
                held = payload[:1] if mode == "supports" else []
            role = "supports" if held else "neutral"
            pick = held[0] if held else payload[0]
            return {"ok": bool(held), "evidence": [{"item_id": pick["id"], "quote": f"[{t}] q", "role": role} for t in tags]}
        return check

    def bundle(self):
        async def dispatch_bundle(bundle: Any, **kw: Any) -> dict[str, Any]:
            self.timeline.append("bundle")
            self.bundles.append(bundle)
            return {"text": "bundled answer"}
        return dispatch_bundle


async def _run(
    name: str, bindings: dict[str, Any], *, bundled: bool, supports: bool = True, cut_at: int | None = None,
    repair: bool = False, oversized: bool = False,
) -> tuple[PlanResult, Calls]:
    """``supports`` False: the assessment finds nothing supporting. ``repair``: only the repair's own hit supports,
    and the facet's own text retrieves it. ``oversized``: the bundle is too big, so it falls back to per-step dispatch."""
    match, binds = _match(name, bindings)
    calls = Calls()
    mode = "repair" if repair else ("supports" if supports else "neutral")
    retrieve = _retrieve_for(repair=repair)
    with patch.object(mcp_core, "search", retrieve), patch.object(mcp_core, "query", retrieve), \
         patch.object(mcp_core, "traverse", _traverse), patch.object(mcp_core, "store_get_many", _get_many), \
         patch.object(mcp_core, "operator_groupby", calls.count("groupby", _groupby)), \
         patch.object(mcp_core, "operator_aggregate", calls.count("aggregate", _aggregate)), \
         patch.object(mcp_core, "operator_summarize", calls.count("summarize", _text)), \
         patch.object(mcp_core, "operator_extract", calls.count("extract", _extract)), \
         patch.object(mcp_core, "operator_rank", calls.count("rank", _rank)), \
         patch.object(mcp_core, "operator_compare", calls.count("compare", _text)), \
         patch.object(mcp_core, "operator_generate", calls.count("generate", _text)), \
         patch.object(mcp_core, "operator_check", calls.check(mode)), \
         patch.object(runner, "_resolve_non_evidentiary_chashes", lambda *a, **k: set()), \
         patch("nexus.plans.bundle.dispatch_bundle", calls.bundle()), \
         patch("nexus.plans.bundle.MAX_BUNDLE_PROMPT_CHARS", 1 if oversized else _REAL_MAX_BUNDLE_PROMPT_CHARS):
        result = await plan_run(
            match, binds, bundle_operators=bundled, coverage_boundary=CoverageBoundary(request=_request()),
            continuation_cut_at_step=cut_at,
        )
    return result, calls


THEMES = ("abstract-themes", {"intent": "what are the main themes", "corpus": "rdr", "limit": 30})
REVIEW = ("review-default", {"change_set": "the identity change", "depth": 1})
ANALYZE = ("analyze-default", {"area": "the identity gap", "criterion": "relevance", "limit": 30})
HYBRID = ("hybrid-factual-lookup", {
    "question": "what is the identity gap", "corpus": "rdr", "limit": 30, "depth": 1,
    "vector_budget_chunks": 5, "graph_budget_chunks": 5,
})
EXTRACT_GENERATE = ("extract-generate", {})
CHECK_GENERATE = ("check-generate", {})
PROBE = ("two-branch-probe", {})
SHAPES = [THEMES, REVIEW, ANALYZE, HYBRID, EXTRACT_GENERATE, CHECK_GENERATE, PROBE]
SHAPE_IDS = [
    "abstract-themes", "review-default", "analyze-default", "hybrid-factual-lookup", "extract-generate",
    "check-generate", "two-branch-probe",
]


def _anchor_contents(result: PlanResult) -> set[str]:
    assert result.coverage is not None
    return {a.content for a in result.coverage.ledger.anchors}


def _hand_text(kw: dict[str, Any]) -> str:
    """Everything an operator was handed as evidence, as one string."""
    return "\n".join(str(kw[k]) for k in ("items", "inputs", "content", "context") if k in kw)


@pytest.mark.asyncio
async def test_abstract_themes_bundled_and_unbundled_read_the_same_anchors() -> None:
    bundled, _ = await _run(*THEMES, bundled=True)
    unbundled, _ = await _run(*THEMES, bundled=False)
    assert bundled.coverage is not None and unbundled.coverage is not None
    assert _anchor_contents(bundled) == set(TEXTS)
    # Unbundled, the boundary used to read the lone summarize step's dict input: reason no_evidence, no anchors.
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert unbundled.coverage.reason != "no_evidence"
    assert unbundled.coverage.state == bundled.coverage.state


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", SHAPES, ids=SHAPE_IDS)
@pytest.mark.parametrize("supports", [True, False], ids=["covered", "unsupported"])
async def test_every_trailing_shape_unbundled_reaches_the_bundled_anchors_state_and_reason(
    plan: Any, supports: bool,
) -> None:
    name, bindings = plan
    bundled, _ = await _run(name, bindings, bundled=True, supports=supports)
    unbundled, _ = await _run(name, bindings, bundled=False, supports=supports)
    assert bundled.coverage is not None and unbundled.coverage is not None
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert unbundled.coverage.state == bundled.coverage.state
    assert unbundled.coverage.reason == bundled.coverage.reason
    assert unbundled.coverage.skip_generation == bundled.coverage.skip_generation == (not supports)


@pytest.mark.asyncio
async def test_two_branch_probe_unbundled_reads_both_branches_like_the_bundled_run() -> None:
    """``search, search, fetch(A), fetch(B), extract(A), generate([B, $extract])``: the run's evidence is A plus B.
    A first-step read sees only A; the bundle builder reads every step's evidence args."""
    bundled, _ = await _run(*PROBE, bundled=True)
    unbundled, _ = await _run(*PROBE, bundled=False)
    assert _anchor_contents(bundled) == set(TEXTS) | set(TEXTS_B)
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)


@pytest.mark.asyncio
async def test_a_deferred_ref_is_no_anchor_text_bundled_or_unbundled() -> None:
    """``generate(context=[B, $extract])`` bundles with the extract as a deferred ref in the list; the list used to be
    joined with ``str(x)`` into one anchor that ends in the ref's repr."""
    for bundled in (True, False):
        result, _ = await _run(*PROBE, bundled=bundled)
        bad = [c for c in _anchor_contents(result) if "deferred" in c or "{'" in c]
        assert not bad, f"bundled={bundled}: anchor text carries a deferred ref: {bad}"


@pytest.mark.asyncio
async def test_unbundled_run_still_runs_every_step_when_covered() -> None:
    result, calls = await _run(*THEMES, bundled=False)
    assert calls.ran == ["groupby", "aggregate", "summarize"]
    assert len(result.steps) == 4
    assert result.final == {"text": "the answer"}


@pytest.mark.asyncio
async def test_review_default_extract_then_compare_unbundled_reads_the_evidence() -> None:
    """A regression guard only: ``review-default`` reads one fetch in both of its operators, so the first-step read the
    boundary used to make before b59b8d757's fix also saw every chunk. The discriminating pins are the two-branch
    probe above and the repair-parity tests below."""
    bundled, _ = await _run(*REVIEW, bundled=True)
    unbundled, calls = await _run(*REVIEW, bundled=False)
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert set(TEXTS) <= _anchor_contents(unbundled)
    assert unbundled.coverage is not None and unbundled.coverage.reason != "no_evidence"
    assert calls.ran == ["extract", "compare"]


@pytest.mark.asyncio
async def test_extract_then_generate_unbundled_reads_the_same_anchors_as_bundled() -> None:
    """The measured S06 shape: unbundled, the boundary sat before ``generate``, whose input is the extract output,
    and the run reported "covered" over one anchor where the bundled run read every retrieved chunk."""
    bundled, _ = await _run(*EXTRACT_GENERATE, bundled=True)
    unbundled, calls = await _run(*EXTRACT_GENERATE, bundled=False)
    assert _anchor_contents(bundled) == set(TEXTS) | set(TEXTS_B)
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert calls.ran == ["extract", "generate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", SHAPES, ids=SHAPE_IDS)
async def test_unbundled_skip_generation_ends_in_the_skipped_output_and_runs_no_operator(plan: Any) -> None:
    name, bindings = plan
    bundled, _ = await _run(name, bindings, bundled=True, supports=False)
    unbundled, calls = await _run(name, bindings, bundled=False, supports=False)
    assert bundled.coverage is not None and bundled.coverage.skip_generation
    assert unbundled.coverage is not None and unbundled.coverage.skip_generation
    assert unbundled.coverage.state == bundled.coverage.state
    assert unbundled.coverage.reason == bundled.coverage.reason
    assert calls.ran == [], "the operators of the trailing run must not dispatch once the boundary skips"
    # One output per plan step, intermediates as the bundle path leaves them, the skipped output last.
    steps = _plan(name)["steps"]
    assert len(unbundled.steps) == len(bundled.steps) == len(steps)
    assert unbundled.steps[-1][SKIPPED_MARK] is True
    assert unbundled.steps[-1] == bundled.steps[-1]
    assert unbundled.final == bundled.final
    assert unbundled.steps == bundled.steps


@pytest.mark.asyncio
async def test_unbundled_continuation_cut_carries_the_coverage_state() -> None:
    """The cut classifier cuts an unbundled run at its first operator step, the step the boundary now sits before."""
    bundled, _ = await _run(*THEMES, bundled=True, cut_at=1)
    unbundled, calls = await _run(*THEMES, bundled=False, cut_at=1)
    assert unbundled.continuation_cut_applied and bundled.continuation_cut_applied
    assert calls.ran == []
    assert _anchor_contents(unbundled) == _anchor_contents(bundled) == set(TEXTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [THEMES, REVIEW, EXTRACT_GENERATE], ids=["abstract-themes", "review-default", "extract-generate"])
async def test_unbundled_cut_with_skip_generation_carries_the_bundled_coverage_state(plan: Any) -> None:
    """A continuation cut at the trailing run on a plan whose facets nothing supports: the boundary still runs, the
    ledger skips generation, and the unbundled state, reason and steps are the bundled run's."""
    name, bindings = plan
    first_operator = next(i for i, s in enumerate(_plan(name)["steps"]) if s["tool"] in ("groupby", "extract"))
    bundled, _ = await _run(name, bindings, bundled=True, supports=False, cut_at=first_operator)
    unbundled, calls = await _run(name, bindings, bundled=False, supports=False, cut_at=first_operator)
    assert bundled.coverage is not None and unbundled.coverage is not None
    assert unbundled.coverage.state == bundled.coverage.state
    assert unbundled.coverage.reason == bundled.coverage.reason
    assert unbundled.coverage.skip_generation == bundled.coverage.skip_generation
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert calls.ran == []
    assert len(unbundled.steps) == len(bundled.steps)


@pytest.mark.asyncio
async def test_a_plans_own_check_before_the_terminal_operator_does_not_dispatch_on_a_skip() -> None:
    """``search, fetch, check, generate``: the plan's check and the generate are one trailing run, and the boundary
    sits before the check. On a skip neither dispatches, bundled or not."""
    bundled, bcalls = await _run(*CHECK_GENERATE, bundled=True, supports=False)
    unbundled, ucalls = await _run(*CHECK_GENERATE, bundled=False, supports=False)
    assert "check" not in bcalls.ran and "check" not in ucalls.ran
    assert "generate" not in ucalls.ran and "bundle" not in bcalls.timeline
    assert unbundled.coverage is not None and bundled.coverage is not None
    assert unbundled.coverage.skip_generation and bundled.coverage.skip_generation
    assert unbundled.steps == bundled.steps
    assert ucalls.timeline and set(ucalls.timeline) == {"assess"}


@pytest.mark.asyncio
async def test_a_plans_own_check_before_the_terminal_operator_dispatches_after_the_boundary_when_covered() -> None:
    bundled, bcalls = await _run(*CHECK_GENERATE, bundled=True)
    unbundled, ucalls = await _run(*CHECK_GENERATE, bundled=False)
    assert ucalls.ran == ["check", "generate"]
    assert "assess" in ucalls.timeline
    last_assessment = max(i for i, t in enumerate(ucalls.timeline) if t == "assess")
    assert ucalls.timeline.index("check") > last_assessment, "the plan's check runs after the boundary, not before it"
    assert bcalls.timeline.index("bundle") > max(i for i, t in enumerate(bcalls.timeline) if t == "assess")
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert unbundled.coverage is not None and bundled.coverage is not None
    assert unbundled.coverage.state == bundled.coverage.state


# ── the repair evidence reaches the answering step ─────────────────────────────────────────────────────────

#: (plan, the operators that read the retrieved evidence directly, the one that answers)
REPAIR_SHAPES = [
    pytest.param(REVIEW, ["extract", "compare"], "compare", id="review-default"),
    pytest.param(ANALYZE, ["rank", "summarize"], "summarize", id="analyze-default"),
    #: The generate reads the rank's output, which carries the repair hit the rank was handed: no second merge.
    pytest.param(HYBRID, ["rank", "generate"], "generate", id="hybrid-factual-lookup"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan", "readers", "answering"), REPAIR_SHAPES)
async def test_unbundled_repair_evidence_reaches_every_step_that_reads_the_retrieved_evidence(
    plan: Any, readers: list[str], answering: str,
) -> None:
    """A facet only the repair can support: the first assessment finds it unsupported, the repair retrieves its hit, the
    reassessment supports it. The ledger then says "covered" on that hit, so the answering step has to be handed it."""
    name, bindings = plan
    result, calls = await _run(name, bindings, bundled=False, repair=True)
    assert result.coverage is not None and result.coverage.repair.outcome == "completed"
    assert REPAIR_TEXT in _anchor_contents(result)
    assert calls.ran == readers
    for reader in readers:
        assert REPAIR_TEXT in _hand_text(calls.args[reader][0]), f"{reader} was not handed the repair evidence"
    assert REPAIR_TEXT in _hand_text(calls.args[answering][0])


@pytest.mark.asyncio
@pytest.mark.parametrize(("plan", "readers", "answering"), REPAIR_SHAPES)
async def test_oversized_bundle_fallback_hands_the_repair_evidence_to_every_reading_step(
    plan: Any, readers: list[str], answering: str,
) -> None:
    name, bindings = plan
    result, calls = await _run(name, bindings, bundled=True, repair=True, oversized=True)
    assert calls.bundles == [], "the fused dispatch must not run; the oversized bundle falls back to per-step dispatch"
    assert result.coverage is not None and result.coverage.repair.outcome == "completed"
    assert calls.ran == readers
    for reader in readers:
        assert REPAIR_TEXT in _hand_text(calls.args[reader][0]), f"{reader} was not handed the repair evidence"
    assert REPAIR_TEXT in _hand_text(calls.args[answering][0])


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [REVIEW, ANALYZE], ids=["review-default", "analyze-default"])
async def test_the_fused_bundle_merges_the_repair_evidence_at_its_first_step_only(plan: Any) -> None:
    """Unchanged by the unbundled fix: the fused bundle carries the repair evidence at the run's first step, and the
    later operator is handed no second copy (a guard on the bundled path, not a pin of the unbundled behaviour)."""
    name, bindings = plan
    result, calls = await _run(name, bindings, bundled=True, repair=True)
    assert result.coverage is not None and result.coverage.repair.outcome == "completed"
    (bundle,) = calls.bundles
    first, *later = bundle.steps
    assert REPAIR_TEXT in _hand_text(first.args)
    assert later and all(REPAIR_TEXT not in _hand_text(st.args) for st in later)


@pytest.mark.asyncio
async def test_unbundled_repair_leaves_a_downstream_operator_over_model_output_alone() -> None:
    """``abstract-themes``: only the groupby reads the retrieved evidence. The aggregate and the summarize read model
    output, so they are not handed the repair evidence again."""
    result, calls = await _run(*THEMES, bundled=False, repair=True)
    assert result.coverage is not None and result.coverage.repair.outcome == "completed"
    assert REPAIR_TEXT in _hand_text(calls.args["groupby"][0])
    assert REPAIR_TEXT not in _hand_text(calls.args["summarize"][0])
