# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 step 1 (nexus-jypmj.3.1): the runner-owned pre-reduction boundary and the coverage ledger.

Tenant-free: ``plan_run`` is driven with a stub dispatcher, the way ``tests/test_rdr232_caller_scope.py``
and ``tests/test_rdr232_evidence_diagnostics.py`` drive it, and the model call (``operator_check``) is
stubbed at the dispatcher, which is the boundary the runner uses. Every test names the claim it pins.

The seven Test Plan scenarios of the bead are the tests named ``test_scenario_N_*``; the rest pin the
state table, the facet-call cap, the step accounting and the re-entry the repair step (nexus-jypmj.3.2)
relies on.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from nexus.mcp import core as mcp_core
from nexus.plans import coverage_ledger, runner
from nexus.plans.coverage_ledger import (
    MAX_FACETS_PER_ASSESSMENT_CALL,
    BoundaryView,
    CoverageBoundary,
    CoverageResult,
    state_for,
)
from nexus.plans.evidence import ScopeConstraint, SourceRef
from nexus.plans.evidence_diagnostics import (
    ReducerInputRecorder,
    capture_reducer_input,
    note_reducer_input,
    reducer_input_scope,
)
from nexus.plans.match import Match
from nexus.plans.primary_evidence import (
    DocumentCoverage,
    NamedSourceResolution,
    PrimaryEvidence,
    ResolvedSource,
    SelectedChunk,
)
from nexus.plans.request_coverage import Facet, RequestCoverage, _MAX_FACETS
from nexus.plans.reserved_stream import ReservedStream
from nexus.plans.runner import plan_run

RDR_COLL = "rdr__1-1__bge-base-en-v15-768__v1"
CODE_COLL = "code__1-1__bge-base-en-v15-768__v1"
DOC = "1.1.9"

VECTOR_TEXTS = ["vector hit about the decision", "vector hit about the gate"]
GRAPH_TEXTS = ["graph hit about an implementing module"]


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _facets(n: int, *, hints: dict[str, tuple[str, ...]] | None = None) -> tuple[Facet, ...]:
    hints = hints or {}
    return tuple(
        Facet(f"f{i}", f"what the document says about topic {i}", "clause", (), hints.get(f"f{i}", ()))
        for i in range(1, n + 1)
    )


def _request(n: int = 2, **kw: Any) -> RequestCoverage:
    return RequestCoverage(answer_kind="synthesis", facets=_facets(n, hints=kw.pop("hints", None)), **kw)


def _match(plan: dict) -> Match:
    return Match(
        plan_id=7, name="fixture", description="fixture", confidence=0.9, dimensions={"verb": "test"}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[], default_bindings={}, parent_dims=None,
    )


#: search (vector stream), traverse (graph stream), one fetch of both, then the terminal reduction.
PLAN = {"steps": [
    {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
    {"tool": "traverse", "args": {"seeds": "$step1.tumblers"}},
    {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
    {"tool": "generate", "args": {"context": "$step3.contents", "question": "q"}},
]}


def _structured(texts: list[str], collection: str) -> dict:
    ids = [_h(t) for t in texts]
    return {
        "ids": ids, "tumblers": [f"1.1.{i}" for i in range(len(ids))], "distances": [0.1] * len(ids),
        "collections": [collection], "chunk_collections": [collection] * len(ids), "chunk_text_hash": list(ids),
    }


def _tag_lines(instruction: str) -> list[str]:
    return re.findall(r"^\[(f\d+)\]", instruction, flags=re.M)


def supports_first_item(items: str, instruction: str) -> dict:
    """A check reply that says the first item supports every facet the call lists."""
    first = json.loads(items)[0]["id"]
    return {"ok": True, "evidence": [
        {"item_id": first, "quote": f"[{fid}] a short quote", "role": "supports"} for fid in _tag_lines(instruction)
    ]}


def supports_nothing(items: str, instruction: str) -> dict:  # noqa: ARG001
    return {"ok": False, "evidence": []}


class Dispatcher:
    """Stub dispatcher: records every call; answers retrieval, fetch, check and generate."""

    def __init__(self, check=supports_first_item, *, hydrates: bool = False, answer: str = "the answer") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.check = check
        self.answer = answer
        if hydrates:
            self.hydrates_operator_args = True  # opt in to the runner's pre-hydration (see the runner)

    @property
    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    def args_of(self, tool: str) -> list[dict]:
        return [a for t, a in self.calls if t == tool]

    async def __call__(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        if tool == "search":
            return _structured(VECTOR_TEXTS, RDR_COLL)
        if tool == "traverse":
            return _structured(GRAPH_TEXTS, CODE_COLL) | {"traversal": {"policy_applied": True, "widened": None}}
        if tool == "store_get_many":
            return {"contents": [*VECTOR_TEXTS, *GRAPH_TEXTS]}
        if tool == "operator_check":
            reply = self.check(args["items"], args["check_instruction"])
            if isinstance(reply, BaseException):
                raise reply
            return reply
        if tool == "generate":
            return {"text": self.answer}
        raise AssertionError(f"unexpected tool {tool!r}")


def _primary(source: str = "RDR-084", doc: str = DOC, n: int = 2) -> PrimaryEvidence:
    items = tuple(
        SelectedChunk(
            source=source, doc_id=doc, position=i, chash=_h(f"{source} TEXT {i}"), collection=RDR_COLL,
            label=f"Section {i}", label_source="content", text=f"{source} TEXT {i}",
        )
        for i in range(n)
    )
    return PrimaryEvidence(items=items, documents=(DocumentCoverage(
        source=source, doc_id=doc, title=source, status="ok", manifest_rows=n, selected_rows=n, partial=False),))


def _reserved(primary: PrimaryEvidence | None = None, n: int = 2) -> ReservedStream:
    stream = ReservedStream.from_evidence(primary or _primary(n=n))
    assert stream is not None
    return stream


def _unresolved(ref: str = "RDR-084") -> NamedSourceResolution:
    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse(ref), status="unresolved", detail="no such document"),))


async def _run(dispatcher: Dispatcher, boundary: CoverageBoundary | None, *, plan: dict = PLAN, **kw: Any):
    return await plan_run(_match(plan), {}, dispatcher=dispatcher, bundle_operators=False, coverage_boundary=boundary, **kw)


def _cov(result) -> CoverageResult:
    assert result.coverage is not None, "the boundary must report a result"
    return result.coverage


# ── scenario 1: check immediately before generate ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_1_coverage_is_evaluated_at_the_runner_boundary_and_each_side_runs_once() -> None:
    d = Dispatcher()
    result = await _run(d, CoverageBoundary(request=_request(2)))
    prefix = [t for t in d.tools if t in ("search", "traverse", "store_get_many")]
    assert prefix == ["search", "traverse", "store_get_many"], "the plan prefix ran exactly once"
    assert d.tools.count("generate") == 1, "the terminal reduction ran exactly once"
    first_check, generate_at = d.tools.index("operator_check"), d.tools.index("generate")
    assert first_check < generate_at, "coverage was assessed before the terminal reduction, at the runner boundary"
    assert "nx_answer" not in d.tools, "the assessment never calls nx_answer"
    assert result.final == {"text": "the answer"}
    cov = _cov(result)
    assert cov.state == "complete" and cov.reason == "all_covered" and not cov.skip_generation
    assert [a.content for a in cov.ledger.anchors] == [*VECTOR_TEXTS, *GRAPH_TEXTS], "the anchors are what the reducer was handed"


@pytest.mark.asyncio
async def test_scenario_1_bundled_terminal_is_assessed_before_the_bundle_is_dispatched() -> None:
    """``rank -> generate`` fuse into one ``claude -p`` call whose intermediate is invisible to the host; the
    boundary sits before that call, on the hydrated input of the bundle's first step."""
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}
    d = Dispatcher()
    order: list[str] = []

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        order.append("bundle")
        return {"text": "bundled answer"}

    async def logged(tool: str, args: dict) -> dict:
        order.append(tool)
        return await d(tool, args)

    wrapper = _Wrapper(logged)
    wrapper.supports_bundling = True  # type: ignore[attr-defined]
    with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, dispatcher=wrapper, coverage_boundary=CoverageBoundary(request=_request(1)))
    assert order.count("bundle") == 1 and order.index("operator_check") < order.index("bundle")
    assert order.count("search") == 1 and order.count("store_get_many") == 1
    cov = _cov(result)
    assert cov.state == "complete"
    assert [a.content for a in cov.ledger.anchors] == [*VECTOR_TEXTS, *GRAPH_TEXTS]


class _Wrapper:
    def __init__(self, fn) -> None:
        self._fn = fn

    async def __call__(self, tool: str, args: dict) -> dict:
        return await self._fn(tool, args)


# ── scenario 2: a six-facet request over several evidence streams ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_2_six_facets_over_three_streams_are_grouped_into_one_call_per_stream() -> None:
    d = Dispatcher(hydrates=True)
    result = await _run(d, CoverageBoundary(request=_request(6)), reserved_stream=_reserved())
    checks = d.args_of("operator_check")
    assert len(checks) == 3, "primary, vector and graph: one call per stream, not six calls for six facets"
    for args in checks:
        assert _tag_lines(args["check_instruction"]) == [f"f{i}" for i in range(1, 7)], "all six facets ride one call"
    report = _cov(result).ledger.assessment
    assert report.calls == 3 and report.facets_per_call == (6, 6, 6)
    assert report.streams == ("primary", "vector", "graph")
    assert 6 <= MAX_FACETS_PER_ASSESSMENT_CALL, "the motivating six-facet request fits one call per stream"


@pytest.mark.asyncio
async def test_a_request_over_the_cap_is_split_and_no_call_exceeds_it() -> None:
    d = Dispatcher()
    n = MAX_FACETS_PER_ASSESSMENT_CALL + 1
    result = await _run(d, CoverageBoundary(request=_request(n)))
    sizes = [len(_tag_lines(a["check_instruction"])) for a in d.args_of("operator_check")]
    assert max(sizes) <= MAX_FACETS_PER_ASSESSMENT_CALL
    assert sorted(sizes) == [1, 1, MAX_FACETS_PER_ASSESSMENT_CALL, MAX_FACETS_PER_ASSESSMENT_CALL], "two streams, two groups each"
    assert _cov(result).ledger.assessment.facets_per_call == tuple(sizes)


# ── scenario 3: the first assessment call fails ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_3_a_failed_first_assessment_call_leaves_the_state_unassessed_and_returns_the_answer() -> None:
    d = Dispatcher(check=lambda items, instruction: RuntimeError("check backend down"), hydrates=True)
    boundary = CoverageBoundary(request=_request(2), named_sources=_resolved_source(), primary=_primary())
    result = await _run(d, boundary, reserved_stream=_reserved())
    cov = _cov(result)
    assert cov.state == "unassessed" and cov.reason == "assessment_failed" and not cov.skip_generation
    assert len(d.args_of("operator_check")) == 1, "the first failure stops further assessment calls"
    assert result.final == {"text": "the answer"}, "the answer is returned, not a notice in its place"
    assert all(f.state == "unassessed" for f in cov.ledger.findings), "no facet defaults to covered"
    assert cov.ledger.assessment.outcome == "failed"
    assert cov.ledger.scope is not None and [(s.source, s.state) for s in cov.ledger.sources] == [("RDR-084", "covered")], (
        "the deterministic checks still ran")
    failed = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(failed) == 1 and not failed[0].ok


def _resolved_source() -> NamedSourceResolution:
    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse("RDR-084"), status="resolved", tumbler=DOC, title="RDR-084", qualified=True, rdr_number=84),))


# ── scenario 4: a run that reaches the boundary without a RequestCoverage ───────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_4_no_request_coverage_is_unassessed_and_the_answer_is_returned_as_before() -> None:
    d = Dispatcher()
    result = await _run(d, CoverageBoundary(request=None))
    cov = _cov(result)
    assert cov.state == "unassessed" and cov.reason == "no_request_coverage" and not cov.skip_generation
    assert d.args_of("operator_check") == [], "nothing to assess, so no assessment call"
    assert d.tools.count("generate") == 1 and result.final == {"text": "the answer"}
    assert all(f.state == "unassessed" for f in cov.ledger.findings) and cov.ledger.findings == ()


# ── scenario 5: the ledger covers nothing before generation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_5_nothing_covered_skips_generation_and_is_unavailable_uncovered() -> None:
    d = Dispatcher(check=supports_nothing)
    result = await _run(d, CoverageBoundary(request=_request(2)))
    cov = _cov(result)
    assert cov.state == "unavailable" and cov.reason == "uncovered" and cov.skip_generation
    assert "generate" not in d.tools, "terminal generation was not called"
    assert result.final is not None and result.final.get("coverage_skipped") is True
    assert not any(r.operator == "generate" for r in result.step_records)
    assert [f.state for f in cov.ledger.findings] == ["unsupported", "unsupported"]
    assert cov.repair_facet_ids == ("f1", "f2"), "the repair step (3.2) targets these"


@pytest.mark.asyncio
async def test_a_switch_off_keeps_generation_when_nothing_is_covered() -> None:
    d = Dispatcher(check=supports_nothing)
    result = await _run(d, CoverageBoundary(request=_request(1), skip_generation_when_uncovered=False))
    assert _cov(result).state == "unavailable" and not _cov(result).skip_generation
    assert d.tools.count("generate") == 1


@pytest.mark.asyncio
async def test_skipping_a_bundled_terminal_leaves_sentinels_and_dispatches_no_bundle() -> None:
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}
    d = Dispatcher(check=supports_nothing)
    wrapper = _Wrapper(d.__call__)
    wrapper.supports_bundling = True  # type: ignore[attr-defined]

    async def fail_bundle(bundle, **kwargs):  # noqa: ARG001
        raise AssertionError("the bundle must not be dispatched when generation is skipped")

    with patch("nexus.plans.bundle.dispatch_bundle", fail_bundle):
        result = await plan_run(_match(plan), {}, dispatcher=wrapper, coverage_boundary=CoverageBoundary(request=_request(1)))
    assert len(result.steps) == 5, "one output per plan step, so a later $stepN reference still resolves"
    assert result.final is not None and result.final.get("coverage_skipped") is True
    assert _cov(result).skip_generation


# ── scenario 6: a missing named source outranks a failed assessment ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_6_a_missing_source_with_a_failed_assessment_follows_the_deterministic_result() -> None:
    d = Dispatcher(check=lambda items, instruction: RuntimeError("check backend down"))
    request = _request(1, hints={"f1": ("RDR-084",)})
    result = await _run(d, CoverageBoundary(request=request, named_sources=_unresolved()))
    cov = _cov(result)
    assert cov.state == "unavailable" and cov.reason == "uncovered", "not unassessed"
    assert [(f.facet_id, f.state, f.reason) for f in cov.ledger.findings] == [("f1", "unsupported", "source_missing")]
    assert cov.skip_generation and "generate" not in d.tools
    assert d.args_of("operator_check") == [], "a facet the source check already decided is not sent to the model"
    assert [(s.source, s.state) for s in cov.ledger.sources] == [("RDR-084", "missing")]


@pytest.mark.asyncio
async def test_scenario_6_mixed_one_facet_blocked_one_unassessed_is_partial_not_unassessed() -> None:
    sources = NamedSourceResolution(sources=(
        ResolvedSource(ref=SourceRef.parse("RDR-084"), status="unresolved", detail="no such document"),
        ResolvedSource(ref=SourceRef.parse("RDR-090"), status="resolved", tumbler="1.1.3", title="RDR-090", qualified=True, rdr_number=90),
    ))
    primary = _primary("RDR-090", "1.1.3")
    request = _request(2, hints={"f1": ("RDR-084",), "f2": ("RDR-090",)})
    d = Dispatcher(check=lambda items, instruction: RuntimeError("check backend down"), hydrates=True)
    result = await _run(d, CoverageBoundary(request=request, named_sources=sources, primary=primary), reserved_stream=_reserved(primary))
    cov = _cov(result)
    assert cov.state == "partial" and not cov.skip_generation
    by_id = {f.facet_id: f for f in cov.ledger.findings}
    assert by_id["f1"].state == "unsupported" and by_id["f1"].reason == "source_missing"
    assert by_id["f2"].state == "unassessed", "no facet is defaulted to covered"
    assert result.final == {"text": "the answer"}


# ── scenario 7: a returned id outside the input is rejected ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_7_an_id_outside_the_input_is_rejected_and_covers_nothing() -> None:
    def stray(items: str, instruction: str) -> dict:  # noqa: ARG001
        return {"ok": True, "evidence": [{"item_id": "e999", "quote": "[f1] invented", "role": "supports"}]}

    d = Dispatcher(check=stray)
    result = await _run(d, CoverageBoundary(request=_request(1)))
    cov = _cov(result)
    assert [f.state for f in cov.ledger.findings] == ["unsupported"]
    report = cov.ledger.assessment
    assert report.calls == 2 and report.rejected_records == report.calls, "one invented record per call, each rejected"
    assert cov.skip_generation, "an invented id must not buy coverage"


@pytest.mark.asyncio
async def test_a_facet_tag_outside_the_call_and_an_untagged_quote_are_rejected() -> None:
    def odd(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [
            {"item_id": first, "quote": "[f9] not a facet of this call", "role": "supports"},
            {"item_id": first, "quote": "no tag at all", "role": "supports"},
            {"item_id": first, "quote": "[f1] fine", "role": "maybe"},
        ]}

    result = await _run(Dispatcher(check=odd), CoverageBoundary(request=_request(1)))
    cov = _cov(result)
    report = cov.ledger.assessment
    assert report.rejected_records == 3 * report.calls and [f.state for f in cov.ledger.findings] == ["unsupported"]


# ── check.ok is not coverage ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_ok_is_never_read_as_coverage() -> None:
    def ok_but_neutral(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [{"item_id": first, "quote": "[f1] mentions it", "role": "neutral"}]}

    def not_ok_but_supports(items: str, instruction: str) -> dict:
        reply = supports_first_item(items, instruction)
        return reply | {"ok": False}

    neutral = _cov(await _run(Dispatcher(check=ok_but_neutral), CoverageBoundary(request=_request(1))))
    assert neutral.ledger.findings[0].state == "unsupported" and neutral.skip_generation
    supports = _cov(await _run(Dispatcher(check=not_ok_but_supports), CoverageBoundary(request=_request(1))))
    assert supports.ledger.findings[0].state == "covered" and supports.state == "complete"


# ── contradicted facets are retained, not repaired; a ledger with nothing covered still skips ────────────────


@pytest.mark.asyncio
async def test_a_contradicted_facet_beside_a_covered_one_is_retained_for_the_reduction() -> None:
    def mixed(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": False, "evidence": [
            {"item_id": first, "quote": "[f1] supports it", "role": "supports"},
            {"item_id": first, "quote": "[f2] the opposite", "role": "contradicts"},
        ]}

    d = Dispatcher(check=mixed)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(2))))
    assert [f.state for f in cov.ledger.findings] == ["covered", "contradicted"]
    assert cov.state == "partial" and not cov.skip_generation and d.tools.count("generate") == 1
    assert cov.repair_facet_ids == (), "repair does not search until it finds agreement"


@pytest.mark.asyncio
async def test_only_contradicted_facets_cover_nothing_and_skip_generation() -> None:
    """RDR-232 section 4: a usable answer needs at least one covered facet; Sam's 3.12 ruling skips
    generation when the ledger covers no facet. Contradicted is not covered."""
    def contradicts(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": False, "evidence": [{"item_id": first, "quote": "[f1] the opposite", "role": "contradicts"}]}

    d = Dispatcher(check=contradicts)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(1))))
    assert cov.ledger.findings[0].state == "contradicted" and cov.ledger.findings[0].evidence_ids
    assert (cov.state, cov.reason) == ("unavailable", "uncovered") and cov.skip_generation
    assert d.tools.count("generate") == 0
    assert cov.repair_facet_ids == (), "repair does not search until it finds agreement"


# ── step accounting: the assessment rides the existing step vocabulary without colliding with a plan step ────


@pytest.mark.asyncio
async def test_assessment_calls_are_step_records_past_the_plan_indices() -> None:
    d = Dispatcher()
    result = await _run(d, CoverageBoundary(request=_request(2)))
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(checks) == _cov(result).ledger.assessment.calls == 2
    assert all(r.source == "llm" and r.ok for r in checks), "existing step vocabulary, no new source"
    indices = [r.step_index for r in result.step_records]
    assert len(set(indices)) == len(indices), "(run_id, step_index) is the primary key of nx_answer_steps"
    assert min(r.step_index for r in checks) >= result.total_planned_steps, "past every plan index"


# ── deadline and budget ─────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_too_little_deadline_left_skips_the_assessment_and_still_returns_the_answer() -> None:
    """The terminal reduction must not be starved by its own pre-check: with a few seconds left the
    assessment is not started, the state is unassessed, and generation still runs."""
    d = Dispatcher()
    result = await plan_run(_match(PLAN), {}, dispatcher=d, bundle_operators=False, deadline=time.monotonic() + 3.0,
                            coverage_boundary=CoverageBoundary(request=_request(2)))
    cov = _cov(result)
    assert cov.state == "unassessed" and cov.reason == "assessment_skipped_deadline"
    assert cov.ledger.assessment.outcome == "skipped_deadline" and cov.ledger.assessment.calls == 0
    assert "operator_check" not in d.tools and d.tools.count("generate") == 1


@pytest.mark.asyncio
async def test_a_spent_usd_allowance_skips_the_assessment() -> None:
    d = Dispatcher()
    boundary = CoverageBoundary(request=_request(1))
    result = await _run(d, boundary)  # a complete first ledger to take the anchors from
    view = _view(_cov(result), Dispatcher(), result, remaining_usd=lambda: 0.0)
    cov = await boundary.evaluate(view)
    assert cov.state == "unassessed" and cov.reason == "assessment_skipped_budget"
    assert cov.ledger.assessment.outcome == "skipped_budget" and cov.ledger.assessment.calls == 0


def test_the_per_call_timeout_is_bounded_by_a_share_of_what_is_left() -> None:
    boundary = CoverageBoundary(request=_request(1))
    assert boundary.assessment_timeout(None) == boundary.call_timeout_s
    assert boundary.assessment_timeout(0.5) == 0.0, "too little of the deadline left for a call"
    assert 0 < boundary.assessment_timeout(1000.0) <= boundary.call_timeout_s


# ── no boundary, no change ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_without_a_boundary_the_run_is_unchanged() -> None:
    d = Dispatcher()
    result = await _run(d, None)
    assert result.coverage is None and "operator_check" not in d.tools and d.tools.count("generate") == 1


@pytest.mark.asyncio
async def test_a_plan_whose_last_step_is_retrieval_has_no_reduction_to_guard() -> None:
    d = Dispatcher()
    plan = {"steps": [PLAN["steps"][0]]}
    result = await _run(d, CoverageBoundary(request=_request(1)), plan=plan)
    assert result.coverage is None and d.tools == ["search"]


# ── single hydration ────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_terminal_step_is_hydrated_once_under_the_real_default_dispatcher() -> None:
    """With the real dispatcher the terminal operator's args are hydrated by the runner for the boundary and
    NOT again by the dispatcher (a second hydration would fetch twice and merge the reserved stream twice)."""
    seen: dict[str, Any] = {"hydrated": [], "prehydrated": [], "generate": []}
    real_hydrate = runner._hydrate_operator_args

    def spy(tool: str, args: dict):
        seen["hydrated"].append(tool)
        return real_hydrate(tool, args)

    async def stub_search(**kwargs):  # noqa: ARG001
        return _structured(VECTOR_TEXTS, RDR_COLL)

    def stub_get_many(ids, collections=None, structured=False):  # noqa: ARG001
        return {"contents": list(VECTOR_TEXTS)}

    async def stub_check(items, check_instruction, timeout=300.0, model=None):  # noqa: ARG001
        return supports_first_item(items, check_instruction)

    async def stub_generate(**kwargs):
        seen["prehydrated"].append(runner.operator_args_prehydrated())
        seen["generate"].append(kwargs)
        return {"text": "the answer"}

    plan = {"steps": [PLAN["steps"][0], PLAN["steps"][2] | {"args": {"ids": "$step1.ids", "collections": "rdr"}},
                      {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}}]}
    stream = _reserved()
    with patch.object(mcp_core, "search", stub_search), patch.object(mcp_core, "store_get_many", stub_get_many), \
         patch.object(mcp_core, "operator_check", stub_check), patch.object(mcp_core, "operator_generate", stub_generate), \
         patch.object(runner, "_hydrate_operator_args", spy):
        result = await plan_run(_match(plan), {}, bundle_operators=False, reserved_stream=stream,
                                coverage_boundary=CoverageBoundary(request=_request(1)))
    assert seen["hydrated"].count("generate") == 1, "hydrated once for the boundary; the dispatcher did not do it again"
    assert seen["prehydrated"] == [True] and runner.operator_args_prehydrated() is False
    context = seen["generate"][0]["context"]
    assert isinstance(context, str) and context.count("RDR-084 TEXT 0") == 1, "the reserved stream merged once"
    assert stream.report()["deduped_secondary"] == 0
    assert _cov(result).state == "complete" and result.final == {"text": "the answer"}


# ── the state table, pure ───────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("states", "outcome", "anchors", "expected"),
    [
        (("covered", "covered"), "ok", 3, ("complete", "all_covered", False)),
        (("covered", "unsupported"), "ok", 3, ("partial", "unresolved_facets", False)),
        (("covered", "contradicted"), "ok", 3, ("partial", "unresolved_facets", False)),
        (("contradicted",), "ok", 3, ("unavailable", "uncovered", True)),
        (("contradicted", "unsupported"), "ok", 3, ("unavailable", "uncovered", True)),
        (("unsupported", "unsupported"), "ok", 3, ("unavailable", "uncovered", True)),
        (("unassessed", "unassessed"), "failed", 3, ("unassessed", "assessment_failed", False)),
        (("unassessed", "unassessed"), "skipped_deadline", 3, ("unassessed", "assessment_skipped_deadline", False)),
        (("unsupported", "unassessed"), "failed", 3, ("partial", "unresolved_facets", False)),
        (("covered", "unassessed"), "partial_failure", 3, ("partial", "unresolved_facets", False)),
        (("unsupported",), "ok", 0, ("unavailable", "no_evidence", False)),
    ],
)
def test_the_state_table(states, outcome, anchors, expected) -> None:
    assert state_for(states, assessment_outcome=outcome, anchors=anchors, observed=True) == expected


def test_no_facets_and_unobserved_evidence_are_unassessed() -> None:
    assert state_for((), assessment_outcome="not_attempted", anchors=3, observed=True) == ("unassessed", "no_facets", False)
    assert state_for(("unassessed",), assessment_outcome="not_attempted", anchors=0, observed=False) == (
        "unassessed", "evidence_not_observed", False)


def test_the_cap_is_a_documented_module_constant_fitted_to_the_labelled_facet_counts() -> None:
    """The docstring's basis, recomputed: the cap covers the largest labelled request in one call per stream
    and the first-pass facet cap in at most two."""
    repo = Path(__file__).resolve().parents[1]
    counts = [
        len(q.get("required_facets") or [])
        for name in ("golden_v1.yaml", "golden_heldout_v1.yaml")
        for q in yaml.safe_load((repo / "bench" / "queries" / name).read_text())["queries"]
    ]
    assert len(counts) >= 50, "the labelled sets were read"
    assert MAX_FACETS_PER_ASSESSMENT_CALL == max(counts) == 6
    assert MAX_FACETS_PER_ASSESSMENT_CALL * 2 >= _MAX_FACETS, "the first-pass cap costs at most two calls per stream"
    doc = coverage_ledger.__doc__ or ""
    assert "cap is 6" in doc and "golden" in doc and "NOT from a measured" in doc


# ── re-entry (what the repair step relies on) ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_second_evaluation_reassesses_only_the_unresolved_facets_and_keeps_the_first_ledger() -> None:
    boundary = CoverageBoundary(request=_request(3))
    first_calls: list[str] = []

    def first(items: str, instruction: str) -> dict:
        first_calls.append(instruction)
        ids = _tag_lines(instruction)
        node = json.loads(items)[0]["id"]
        return {"ok": False, "evidence": [{"item_id": node, "quote": f"[{ids[0]}] q", "role": "supports"}]}

    d = Dispatcher(check=first)
    result = await _run(d, boundary)
    one = _cov(result)
    assert [f.state for f in one.ledger.findings] == ["covered", "unsupported", "unsupported"]
    assert one.repair_facet_ids == ("f2", "f3")

    seen: list[list[str]] = []

    def second(items: str, instruction: str) -> dict:
        seen.append(_tag_lines(instruction))
        node = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [{"item_id": node, "quote": "[f2] now there", "role": "supports"}]}

    two = await _reevaluate(boundary, one, Dispatcher(check=second), result)
    assert seen and all(tags == ["f2", "f3"] for tags in seen), "only the unresolved facets go back to the model"
    assert [f.state for f in two.ledger.findings] == ["covered", "covered", "unsupported"]
    assert two.ledger.rounds == 2 and two.ledger.assessment.calls == one.ledger.assessment.calls + len(seen)


async def _reevaluate(boundary: CoverageBoundary, prior: CoverageResult, dispatcher: Dispatcher, result) -> CoverageResult:
    return await boundary.evaluate(_view(prior, dispatcher, result), prior=prior)


def _view(prior: CoverageResult, dispatcher: Dispatcher, result, *, remaining_usd=lambda: None):
    async def call(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        return await dispatcher("operator_check", args)

    return BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(result.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[],
        check=call, remaining_seconds=lambda: None, remaining_usd=remaining_usd,
    )


# ── zero evidence stays the existing path's ────────────────────────────────────────────────────────────────


class _NoContent(Dispatcher):
    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "store_get_many":
            self.calls.append((tool, dict(args)))
            return {"contents": []}
        return await super().__call__(tool, args)


@pytest.mark.asyncio
async def test_no_evidence_is_unavailable_but_never_skips_the_existing_zero_evidence_path() -> None:
    d = _NoContent()
    result = await _run(d, CoverageBoundary(request=_request(2)))
    cov = _cov(result)
    assert cov.state == "unavailable" and cov.reason == "no_evidence" and not cov.skip_generation
    assert d.args_of("operator_check") == [] and d.tools.count("generate") == 1
    assert [(f.state, f.reason) for f in cov.ledger.findings] == [("unsupported", "no_evidence")] * 2
    assert cov.repair_facet_ids == (), "nothing was assessed, so there is no assessed-and-absent facet to repair"


@pytest.mark.asyncio
async def test_a_scope_conflict_that_removed_all_evidence_is_named() -> None:
    plan = {"steps": [
        {"tool": "search", "args": {"query": "q", "corpus": "code"}},
        {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "code"}},
        {"tool": "generate", "args": {"context": "$step2.contents", "question": "q"}},
    ]}
    d = _NoContent()
    result = await plan_run(
        _match(plan), {"_nx_scope": "rdr"}, dispatcher=d, bundle_operators=False,
        coverage_boundary=CoverageBoundary(request=_request(1), scope=ScopeConstraint.parse("rdr")),
    )
    cov = _cov(result)
    assert cov.ledger.scope is not None and cov.ledger.scope.state == "conflict" and cov.ledger.scope.conflict_steps == (0,)
    assert cov.state == "unavailable" and cov.reason == "scope_conflict" and not cov.skip_generation
    assert "search" not in d.tools, "the conflicting step was skipped, not run unscoped"


# ── the seams ───────────────────────────────────────────────────────────────────────────────────────────────


def test_capture_reducer_input_sees_the_list_beside_an_active_recorder_and_nests_cleanly() -> None:
    rec = ReducerInputRecorder()
    note_reducer_input(["outside any scope"])  # no recorder, no capture: a no-op
    with reducer_input_scope(rec):
        rec.set_step(3)
        with capture_reducer_input() as outer:
            note_reducer_input(["a", "b"])
            rec.set_step(4)  # a later operator: not the first consumer
            with capture_reducer_input() as inner:
                note_reducer_input(["c"])
            note_reducer_input(["d"])
        note_reducer_input(["after"])
    assert outer == [["a", "b"], ["d"]] and inner == [["c"]]
    assert rec.items == ("a", "b"), "the recorder keeps only the first consumer's input, as before"


@pytest.mark.parametrize(
    ("args", "captured", "expected"),
    [
        ({"context": ["a", "", "b"]}, [], (["a", "b"], True)),
        ({"items": json.dumps([{"id": "1", "text": "x"}, "y"])}, [], (["x", "y"], True)),
        ({"context": "one condensed output"}, [], (["one condensed output"], True)),
        ({"context": {"__deferred_step_ref__": 2}}, [], ([], False)),
        ({"question": "q"}, [], ([], False)),
        ({"context": "joined text"}, [["from", "the", "list"]], (["from", "the", "list"], True)),
        ({"context": ""}, [[]], ([], True)),
    ],
)
def test_the_evidence_a_terminal_step_is_handed(args, captured, expected) -> None:
    assert runner._boundary_evidence(args, captured) == expected


# ── anchors ─────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anchors_carry_the_typed_fields_per_stream() -> None:
    d = Dispatcher(hydrates=True)
    result = await _run(d, CoverageBoundary(request=_request(1)), reserved_stream=_reserved(n=1))
    anchors = _cov(result).ledger.anchors
    by_stream = {a.stream: a for a in anchors}
    assert set(by_stream) == {"primary", "vector", "graph"}
    v = by_stream["vector"]
    assert v.chash == _h(v.content) and v.collection == RDR_COLL and v.tumbler.startswith("1.1.") and v.kind == "chunk"
    g = by_stream["graph"]
    assert g.collection == CODE_COLL
    p = by_stream["primary"]
    assert p.collection == RDR_COLL and p.tumbler == DOC and p.chash == _h("RDR-084 TEXT 0")
    assert [a.item_id for a in anchors] == [f"e{i}" for i in range(1, len(anchors) + 1)], "request-local join keys"
    wire = _cov(result).to_wire()
    assert "content" not in json.dumps(wire), "the wire form carries ids and counts, never evidence text"
