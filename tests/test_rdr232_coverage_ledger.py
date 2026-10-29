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
from nexus.plans.answer_shape import AnswerShape
from nexus.plans.coverage_outcome import decide_outcome
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
    SourceCandidate,
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


def supports_nothing(items: str, instruction: str) -> dict:
    """An assessor that read the items and found nothing bearing on any facet: one neutral record per facet.
    (A reply with NO records is not this: it is an unusable assessment, see ``returns_no_records``.)"""
    first = json.loads(items)[0]["id"]
    return {"ok": False, "evidence": [
        {"item_id": first, "quote": f"[{fid}] only mentions the topic", "role": "neutral"} for fid in _tag_lines(instruction)
    ]}


def returns_no_records(items: str, instruction: str) -> dict:  # noqa: ARG001
    """A reply that parses but carries no evidence record: the assessor said nothing usable."""
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
    assert len(d.args_of("operator_check")) == 3, "a failed call does not stop the others (nexus-jypmj.17): primary, vector and graph each ran"
    assert result.final == {"text": "the answer"}, "the answer is returned, not a notice in its place"
    assert all(f.state == "unassessed" for f in cov.ledger.findings), "no facet defaults to covered"
    assert cov.ledger.assessment.outcome == "failed"
    assert cov.ledger.scope is not None and [(s.source, s.state) for s in cov.ledger.sources] == [("RDR-084", "covered")], (
        "the deterministic checks still ran")
    failed = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(failed) == 3 and not any(r.ok for r in failed)


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


def _ambiguous(ref: str = "RDR-049") -> NamedSourceResolution:
    """The G01 shape: the number is carried by two projects, so resolution returns candidates and picks nothing."""
    def cand(tumbler: str) -> SourceCandidate:
        return SourceCandidate(
            tumbler=tumbler, title=ref, file_path=f"docs/rdr/{ref.lower()}.md", owner=tumbler.rsplit(".", 1)[0],
            content_type="rdr", collection=RDR_COLL)

    return NamedSourceResolution(sources=(ResolvedSource(
        ref=SourceRef.parse(ref), status="candidates", candidates=(cand("1.1.49"), cand("1.2.49")),
        detail="two projects carry this number; pass owner or project to choose"),))


@pytest.mark.asyncio
async def test_an_ambiguous_named_source_does_not_block_the_facet_and_is_disclosed() -> None:
    """nexus-jypmj.3.17: candidates are an unresolved source, not a missing one. The facet is assessed on the evidence
    that was read (here the secondary streams) and the answer is generated; the ambiguity stays in coverage.sources."""
    d = Dispatcher(check=supports_first_item)
    request = _request(1, hints={"f1": ("RDR-049",)})
    result = await _run(d, CoverageBoundary(request=request, named_sources=_ambiguous()))
    cov = _cov(result)
    assert not cov.skip_generation and cov.state == "complete", (cov.state, cov.reason)
    assert d.tools.count("generate") == 1 and d.args_of("operator_check"), "the facet went through the assessment"
    assert [(f.facet_id, f.state) for f in cov.ledger.findings] == [("f1", "covered")]
    [row] = cov.ledger.sources
    assert (row.source, row.state) == ("RDR-049", "unresolved") and "candidates" in row.detail
    wire = cov.to_wire()
    assert wire["primary_source"] == {"state": "unresolved", "sources": 1}
    assert wire["sources"][0]["state"] == "unresolved"


@pytest.mark.asyncio
async def test_an_ambiguous_source_with_no_supporting_evidence_is_repairable_not_source_missing() -> None:
    d = Dispatcher(check=supports_nothing)
    request = _request(1, hints={"f1": ("RDR-049",)})
    cov = _cov(await _run(d, CoverageBoundary(request=request, named_sources=_ambiguous())))
    [finding] = cov.ledger.findings
    assert finding.state == "unsupported" and finding.reason != "source_missing"
    assert cov.repair_facet_ids == ("f1",), "an assessed gap is a repair target; a missing source never is"


@pytest.mark.asyncio
async def test_a_lookup_that_could_not_be_trusted_is_unresolved_and_does_not_block() -> None:
    sources = NamedSourceResolution.failed([SourceRef.parse("RDR-049")], "TimeoutError")
    request = _request(1, hints={"f1": ("RDR-049",)})
    cov = _cov(await _run(Dispatcher(), CoverageBoundary(request=request, named_sources=sources)))
    assert [(s.source, s.state) for s in cov.ledger.sources] == [("RDR-049", "unresolved")]
    assert not cov.skip_generation and cov.ledger.findings[0].state == "covered"


@pytest.mark.asyncio
async def test_an_ambiguous_source_does_not_hide_a_missing_one_beside_it() -> None:
    sources = NamedSourceResolution(sources=(*_ambiguous().sources, *_unresolved("RDR-084").sources))
    request = _request(2, hints={"f1": ("RDR-049",), "f2": ("RDR-084",)})
    cov = _cov(await _run(Dispatcher(), CoverageBoundary(request=request, named_sources=sources)))
    by_id = {f.facet_id: f for f in cov.ledger.findings}
    assert by_id["f1"].state == "covered", "the ambiguous source blocks nothing"
    assert (by_id["f2"].state, by_id["f2"].reason) == ("unsupported", "source_missing")
    assert cov.primary_source_state["state"] == "missing", "missing outranks unresolved in the one-word state"


# ── scenario 7: a returned id outside the input is rejected ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_7_an_id_outside_the_input_is_rejected_and_covers_nothing() -> None:
    """An invented id cannot buy coverage. A call whose every record is rejected is an unusable assessment (the
    facets are unassessed, generation is not skipped); next to an accepted record the rejection changes nothing."""
    def stray(items: str, instruction: str) -> dict:  # noqa: ARG001
        return {"ok": True, "evidence": [{"item_id": "e999", "quote": "[f1] invented", "role": "supports"}]}

    d = Dispatcher(check=stray)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(1))))
    assert [f.state for f in cov.ledger.findings] == ["unassessed"], "never covered, and not known to be unsupported either"
    report = cov.ledger.assessment
    assert report.calls == 2 and report.rejected_records == report.calls, "one invented record per call, each rejected"
    assert not cov.skip_generation and d.tools.count("generate") == 1, "the answer is generated, not withheld"

    def stray_beside_a_neutral(items: str, instruction: str) -> dict:
        reply = supports_nothing(items, instruction)
        return {"ok": True, "evidence": [*reply["evidence"], *stray(items, instruction)["evidence"]]}

    d2 = Dispatcher(check=stray_beside_a_neutral)
    cov2 = _cov(await _run(d2, CoverageBoundary(request=_request(1))))
    assert [f.state for f in cov2.ledger.findings] == ["unsupported"], "an accepted record exists and none supports"
    assert cov2.skip_generation and cov2.ledger.assessment.rejected_records == cov2.ledger.assessment.calls


@pytest.mark.asyncio
async def test_a_facet_tag_outside_the_call_and_an_untagged_quote_are_rejected() -> None:
    def odd(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [
            {"item_id": first, "quote": "[f9] not a facet of this call", "role": "supports"},
            {"item_id": first, "quote": "no tag at all", "role": "supports"},
            {"item_id": first, "quote": "[f1] fine", "role": "maybe"},
        ]}

    d = Dispatcher(check=odd)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(1))))
    report = cov.ledger.assessment
    assert report.rejected_records == 3 * report.calls and [f.state for f in cov.ledger.findings] == ["unassessed"]
    assert not cov.skip_generation and d.tools.count("generate") == 1


# ── an unusable assessor reply is an assessment failure, never "unsupported" ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_reply_with_no_records_is_an_unusable_assessment_not_unsupported() -> None:
    """RDR-232 fix round (code C1, critique C2): 'the assessor returned nothing usable' must not read as 'nothing
    supports the facet'. The facets stay unassessed, generation runs once, no repair is attempted for them."""
    d = Dispatcher(check=returns_no_records)
    result = await _run(d, CoverageBoundary(request=_request(2)))
    cov = _cov(result)
    assert [f.state for f in cov.ledger.findings] == ["unassessed", "unassessed"]
    assert (cov.state, cov.reason) == ("unassessed", "assessment_failed") and not cov.skip_generation
    report = cov.ledger.assessment
    assert report.outcome == "failed" and report.calls == 2 and report.detail.startswith("assessor_unusable")
    assert d.tools.count("generate") == 1 and result.final == {"text": "the answer"}
    assert cov.repair_facet_ids == () and d.tools.count("search") == 1, "no repair retrieval for a facet nobody assessed"
    assert cov.repair.outcome == "not_attempted"


@pytest.mark.asyncio
async def test_an_unusable_call_leaves_only_its_own_facets_unknown_and_the_run_a_partial_failure() -> None:
    """Vector says f1 is supported and f2 only mentioned; graph returns nothing usable. f1 is covered whatever the
    graph call did; f2 cannot be called unsupported while a stream was not read: unassessed."""
    def per_stream(items: str, instruction: str) -> dict:
        if "graph hit" in items:
            return returns_no_records(items, instruction)
        first = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [
            {"item_id": first, "quote": "[f1] it states it", "role": "supports"},
            {"item_id": first, "quote": "[f2] only mentions it", "role": "neutral"},
        ]}

    d = Dispatcher(check=per_stream)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(2))))
    assert [(f.facet_id, f.state) for f in cov.ledger.findings] == [("f1", "covered"), ("f2", "unassessed")]
    assert cov.ledger.assessment.outcome == "partial_failure" and cov.ledger.assessment.calls == 2
    assert cov.state == "partial" and not cov.skip_generation and d.tools.count("generate") == 1
    assert cov.repair_facet_ids == ()


@pytest.mark.asyncio
async def test_a_tag_for_a_facet_another_call_carries_is_rejected_not_accepted() -> None:
    """Seven facets split into a six-facet call and a one-facet call per stream. A record in the first call tagged
    [f7] names a facet that call's prompt never showed the model: it is rejected, never support for f7 (fix-round
    confirmation N1, the tag check must be the call's group, not the whole pass)."""
    def strays_into_f7(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        shown = re.findall(r"^\[(f\d+)\]", instruction, flags=re.M)
        records = [{"item_id": first, "quote": f"[{fid}] mentioned only", "role": "neutral"} for fid in shown]
        if "f7" not in shown:
            records.append({"item_id": first, "quote": "[f7] borrowed from another call", "role": "supports"})
        return {"ok": True, "evidence": records}

    cov = _cov(await _run(Dispatcher(check=strays_into_f7), CoverageBoundary(request=_request(7))))
    f7 = next(f for f in cov.ledger.findings if f.facet_id == "f7")
    assert f7.state == "unsupported", "a record from a call that never showed f7 cannot support f7"
    assert cov.ledger.assessment.rejected_records >= 1


@pytest.mark.asyncio
async def test_the_facet_tag_is_matched_without_regard_to_case() -> None:
    def shouting(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": True, "evidence": [{"item_id": first, "quote": "[F1]: a short quote", "role": "supports"}]}

    cov = _cov(await _run(Dispatcher(check=shouting), CoverageBoundary(request=_request(1))))
    assert [f.state for f in cov.ledger.findings] == ["covered"] and cov.ledger.assessment.rejected_records == 0


def test_the_conftest_guard_matches_a_marker_the_built_instruction_carries() -> None:
    """The unit-test guard refuses a live coverage assessment by a marker string. It must be the ledger module's own
    constant and the built instruction must carry it, so a retuned prompt cannot silently unguard the suite."""
    group = _facets(2)
    assert coverage_ledger.INSTRUCTION_MARKER in coverage_ledger._instruction(group)
    assert coverage_ledger._instruction(group).startswith(coverage_ledger.INSTRUCTION_MARKER)
    guard = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    assert "INSTRUCTION_MARKER" in guard, "conftest matches the exported constant"
    assert "Coverage assessment. For each facet" not in guard, "and keeps no literal copy of the prompt"


# ── mixed evidence on one facet ─────────────────────────────────────────────────────────────────────────────


class _ManyItems(Dispatcher):
    """Eleven vector chunks, so a facet can hold ten supporting items and one contradicting one."""

    TEXTS = [f"supporting chunk {i}" for i in range(10)] + ["an older revision that says otherwise"]

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "search":
            self.calls.append((tool, dict(args)))
            return _structured(self.TEXTS, RDR_COLL)
        if tool == "store_get_many":
            self.calls.append((tool, dict(args)))
            return {"contents": list(self.TEXTS)}
        if tool == "traverse":
            self.calls.append((tool, dict(args)))
            return {"ids": [], "tumblers": [], "traversal": {"policy_applied": True, "widened": None}}
        return await super().__call__(tool, args)


def _ten_support_one_contradicts(items: str, instruction: str) -> dict:
    payload = json.loads(items)
    return {"ok": True, "evidence": [
        {"item_id": it["id"], "quote": "[f1] quoted", "role": "contradicts" if "otherwise" in it["text"] else "supports"}
        for it in payload
    ]}


@pytest.mark.asyncio
async def test_a_facet_with_ten_supports_and_one_contradiction_is_covered_and_marked_conflicted() -> None:
    """Code C2: supports present means covered; the contradiction rides on the finding for the reduction and the notice."""
    d = _ManyItems(check=_ten_support_one_contradicts)
    result = await _run(d, CoverageBoundary(request=_request(1)))
    cov = _cov(result)
    f1 = cov.ledger.finding("f1")
    assert f1 is not None and f1.state == "covered" and f1.reason == "conflicted"
    assert len(f1.evidence_ids) == 11, "the supporting and the contradicting items are both cited"
    assert (cov.state, cov.reason) == ("complete", "all_covered") and not cov.skip_generation
    assert d.tools.count("generate") == 1 and result.final == {"text": "the answer"}
    assert cov.conflicted_facet_ids == ("f1",)
    assert cov.to_wire()["findings"][0]["reason"] == "conflicted", "additive: a reason value, no new key"
    context = d.args_of("generate")[0]["context"]
    assert isinstance(context, str) and "[coverage note, not evidence]" in context and "conflict" in context, (
        "the reduction is told the evidence on f1 conflicts")
    assert cov.repair.noted_facets == 1


@pytest.mark.asyncio
async def test_contradicting_evidence_without_support_is_still_contradicted() -> None:
    def only_the_opposite(items: str, instruction: str) -> dict:
        payload = json.loads(items)
        return {"ok": True, "evidence": [{"item_id": payload[0]["id"], "quote": "[f1] the opposite", "role": "contradicts"}]}

    cov = _cov(await _run(_ManyItems(check=only_the_opposite), CoverageBoundary(request=_request(1))))
    f1 = cov.ledger.finding("f1")
    assert f1 is not None and (f1.state, f1.reason) == ("contradicted", "contradicted") and cov.conflicted_facet_ids == ()


# ── item 9: the all-contradicted rule is one named predicate ─────────────────────────────────────────────────


def test_the_all_contradicted_rule_is_one_named_predicate_both_sides_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sam ruled 2026-10-27 (nexus-jypmj.3.13): a facet with only contradicting evidence counts toward a usable
    answer. Both the pre-generation skip (``state_for``) and the post-generation withhold (``decide_outcome``)
    ask ``counts_toward_a_usable_answer``, so the rule is ``USABLE_ANSWER_FINDING_STATES`` alone."""
    assert coverage_ledger.USABLE_ANSWER_FINDING_STATES == frozenset({"covered", "contradicted"})
    assert coverage_ledger.counts_toward_a_usable_answer("contradicted") and not coverage_ledger.counts_toward_a_usable_answer("unsupported")
    assert state_for(("contradicted",), assessment_outcome="ok", anchors=3, observed=True) == ("partial", "unresolved_facets", False)
    none_settled = CoverageResult("partial", "unresolved_facets", False, coverage_ledger.CoverageLedger(
        findings=(coverage_ledger.FacetFinding("f1", "contradicted", ("e1",), "contradicted"),
                  coverage_ledger.FacetFinding("f2", "unsupported", (), "no_supporting_evidence"))))
    assert decide_outcome(none_settled, AnswerShape.ANSWERED).shape is AnswerShape.INSUFFICIENT_EVIDENCE

    monkeypatch.setattr(coverage_ledger, "USABLE_ANSWER_FINDING_STATES", frozenset({"covered"}))
    assert state_for(("contradicted",), assessment_outcome="ok", anchors=3, observed=True) == ("unavailable", "uncovered", True), \
        "control: the one set is what both sides read"


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


# ── contradicted facets are retained for the reduction and never repaired ────────────────────────────────────


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
async def test_only_contradicted_facets_still_generate_as_a_partial_answer() -> None:
    """Sam's ruling 2026-10-27 (nexus-jypmj.3.13): contradicting evidence is evidence the reduction must state
    (RDR section 4, "contradicted is retained for the reduction"), so a ledger whose only finding is a
    contradiction is partial and generation runs; the notice names the contradiction ahead of the prose."""
    def contradicts(items: str, instruction: str) -> dict:
        first = json.loads(items)[0]["id"]
        return {"ok": False, "evidence": [{"item_id": first, "quote": "[f1] the opposite", "role": "contradicts"}]}

    d = Dispatcher(check=contradicts)
    cov = _cov(await _run(d, CoverageBoundary(request=_request(1))))
    assert cov.ledger.findings[0].state == "contradicted" and cov.ledger.findings[0].evidence_ids
    assert (cov.state, cov.reason) == ("partial", "unresolved_facets") and not cov.skip_generation
    assert d.tools.count("generate") == 1
    assert cov.unresolved_facet_ids == ("f1",)
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


def test_the_stage_allowance_is_a_named_share_of_what_is_left_and_a_call_is_drawn_from_it() -> None:
    boundary = CoverageBoundary(request=_request(1))
    assert coverage_ledger.ASSESSMENT_STAGE_DEADLINE_SHARE == boundary.stage_share == 0.4
    assert boundary.stage_allowance(None) is None, "no deadline, no stage cap (the per-call ceiling still applies)"
    assert boundary.stage_allowance(300.0) == pytest.approx(120.0)
    assert boundary.call_timeout(None) == boundary.call_timeout_s
    assert boundary.call_timeout(0.5) == 0.0, "too little of the stage left for a call"
    assert boundary.call_timeout(1000.0) == boundary.call_timeout_s, "never more than the per-call ceiling"
    assert boundary.call_timeout(12.0) == 12.0, "a call is drawn from what is left of the stage"


@pytest.mark.asyncio
async def test_six_calls_on_a_300_second_deadline_stay_inside_the_stage_share_and_generation_keeps_its_time() -> None:
    """Code S1: three evidence streams and two facet groups are six calls. Each call below runs to the whole of its
    allowance. The stage as a whole may spend 0.4 of the deadline, so the calls that do not fit are skipped (their
    facets unassessed) and at least 0.6 of the deadline is left for repair and the terminal reduction."""
    result = await _run(Dispatcher(hydrates=True), CoverageBoundary(request=_request(7)), reserved_stream=_reserved())
    prior = _cov(result)
    assert {a.stream for a in prior.ledger.anchors} == {"primary", "vector", "graph"}
    clock = {"now": 0.0}
    deadline_s = 300.0
    timeouts: list[float] = []

    async def slow_check(args: dict, *, timeout: float) -> dict:
        timeouts.append(timeout)
        clock["now"] += timeout  # the call runs to the end of its allowance
        return supports_nothing(args["items"], args["check_instruction"])

    view = BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(result.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[], check=slow_check, remaining_usd=lambda: None,
        remaining_seconds=lambda: deadline_s - clock["now"],
    )
    boundary = CoverageBoundary(request=_request(7))
    # outside plan_run the reserved stream is not active, so the primary items arrive as the "other" stream
    cov = await boundary.evaluate(view)
    streams = {a.stream for a in cov.ledger.anchors}
    assert len(streams) == 3 and 2 * len(streams) == 6, "three streams, two facet groups: six planned calls"
    report = cov.ledger.assessment
    share = coverage_ledger.ASSESSMENT_STAGE_DEADLINE_SHARE
    assert sum(timeouts) <= deadline_s * share + 1e-9, "the whole stage stays inside its share of the deadline"
    assert deadline_s - clock["now"] >= deadline_s * (1 - share) - 1e-9, "generation keeps its time"
    assert report.calls == len(timeouts) < 6, "calls that do not fit are skipped, not squeezed in"
    assert report.outcome == "partial_failure" and "deadline" in report.detail
    assert all(t <= boundary.call_timeout_s for t in timeouts)
    assert all(f.state == "unassessed" for f in cov.ledger.findings), "a facet whose streams were not all read is not known"
    assert not cov.skip_generation


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
        (("contradicted",), "ok", 3, ("partial", "unresolved_facets", False)),
        (("contradicted", "unsupported"), "ok", 3, ("partial", "unresolved_facets", False)),
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

    # nexus-jypmj.3.2: a repairable gap goes back to the model against the NEW evidence only (the old items were
    # already read for it), so the re-entry view carries one chunk the first ledger did not hold.
    view = _view(one, Dispatcher(check=second), result)
    view.evidence_texts = [*view.evidence_texts, "a chunk the repair retrieved"]
    two = await boundary.evaluate(view, prior=one)
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
