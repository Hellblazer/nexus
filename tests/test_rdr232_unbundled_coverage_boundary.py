# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 (nexus-jypmj): the coverage boundary of a plan whose operators do not bundle.

The boundary reads the input of the FIRST step of the plan's trailing operator run: the step that consumes the
retrieved evidence. Bundled, that run is one segment and the boundary sits before it. A dispatcher without
``supports_bundling`` flattens the run into isolated steps, and the boundary used to sit before the LAST of them,
which reads a later operator's input (``summarize`` over ``$step3.aggregates``, model prose as dicts), so the ledger
ended ``unavailable`` / ``no_evidence`` with no anchors although evidence had reached the reduction.

Real ``plan_run``, real default dispatcher, real boundary and ledger; only the ``nexus.mcp.core`` tools that reach
a model or an engine are stubbed. The plans are the builtin templates, read from ``conexus/plans/builtin``.
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
from nexus.plans.bundle import BUNDLED_INTERMEDIATE
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.match import Match
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.runner import PlanResult, plan_run

BUILTIN = pathlib.Path(__file__).parent.parent / "conexus" / "plans" / "builtin"
TEXTS = ["chunk about the chash identity gap", "chunk about the options weighed", "chunk about what was left open"]
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


def _structured() -> dict[str, Any]:
    ids = [_h(t) for t in TEXTS]
    return {
        "ids": ids, "tumblers": [f"1.1.{i}" for i in range(len(ids))], "distances": [0.1] * len(ids),
        "collections": [COLL], "chunk_collections": [COLL] * len(ids), "chunk_text_hash": list(ids),
    }


async def _retrieve(**kw: Any) -> dict[str, Any]:
    return _structured()


async def _traverse(**kw: Any) -> dict[str, Any]:
    return {"ids": [], "tumblers": [], "collections": [], "traversal": {"policy_applied": True, "widened": None}}


def _get_many(**kw: Any) -> dict[str, Any]:
    return {"contents": list(TEXTS)}


async def _groupby(**kw: Any) -> dict[str, Any]:
    items = json.loads(kw["items"]) if isinstance(kw.get("items"), str) else kw.get("items")
    return {"groups": [{"key_value": "identity", "items": items}]}


async def _aggregate(**kw: Any) -> dict[str, Any]:
    return {"aggregates": [{"key_value": "identity", "summary": "RDR-086 found a gap in chunk identity."}]}


async def _text(**kw: Any) -> dict[str, Any]:
    return {"text": "the answer"}


async def _extract(**kw: Any) -> dict[str, Any]:
    return {"extractions": [{"decision": "d", "rationale": "r", "rdr": "RDR-086"}]}


def _check(supports: bool):
    async def check(items: str, check_instruction: str, **kw: Any) -> dict[str, Any]:
        first = json.loads(items)[0]["id"]
        tags = re.findall(r"^\[(f\d+)\]", check_instruction, flags=re.M)
        role = "supports" if supports else "neutral"
        return {"ok": supports, "evidence": [{"item_id": first, "quote": f"[{t}] q", "role": role} for t in tags]}
    return check


async def _bundle(bundle: Any, **kw: Any) -> dict[str, Any]:
    return {"text": "bundled answer"}


class Calls:
    """Which operator tools actually ran, in order."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def count(self, name: str, fn: Any):
        async def wrapped(**kw: Any) -> Any:
            self.ran.append(name)
            out = fn(**kw)
            return await out if hasattr(out, "__await__") else out
        return wrapped


async def _run(
    name: str, bindings: dict[str, Any], *, bundled: bool, supports: bool = True, cut_at: int | None = None,
) -> tuple[PlanResult, Calls]:
    match, binds = _match(name, bindings)
    calls = Calls()
    with patch.object(mcp_core, "search", _retrieve), patch.object(mcp_core, "query", _retrieve), \
         patch.object(mcp_core, "traverse", _traverse), patch.object(mcp_core, "store_get_many", _get_many), \
         patch.object(mcp_core, "operator_groupby", calls.count("groupby", _groupby)), \
         patch.object(mcp_core, "operator_aggregate", calls.count("aggregate", _aggregate)), \
         patch.object(mcp_core, "operator_summarize", calls.count("summarize", _text)), \
         patch.object(mcp_core, "operator_extract", calls.count("extract", _extract)), \
         patch.object(mcp_core, "operator_compare", calls.count("compare", _text)), \
         patch.object(mcp_core, "operator_generate", calls.count("generate", _text)), \
         patch.object(mcp_core, "operator_check", _check(supports)), \
         patch.object(runner, "_resolve_non_evidentiary_chashes", lambda *a, **k: set()), \
         patch("nexus.plans.bundle.dispatch_bundle", _bundle):
        result = await plan_run(
            match, binds, bundle_operators=bundled, coverage_boundary=CoverageBoundary(request=_request()),
            continuation_cut_at_step=cut_at,
        )
    return result, calls


THEMES = ("abstract-themes", {"intent": "what are the main themes", "corpus": "rdr", "limit": 30})
REVIEW = ("review-default", {"change_set": "the identity change", "depth": 1})
EXTRACT_GENERATE = ("extract-generate", {})


def _anchor_contents(result: PlanResult) -> set[str]:
    assert result.coverage is not None
    return {a.content for a in result.coverage.ledger.anchors}


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
async def test_unbundled_run_still_runs_every_step_when_covered() -> None:
    result, calls = await _run(*THEMES, bundled=False)
    assert calls.ran == ["groupby", "aggregate", "summarize"]
    assert len(result.steps) == 4
    assert result.final == {"text": "the answer"}


@pytest.mark.asyncio
async def test_review_default_extract_then_compare_unbundled_reads_the_evidence() -> None:
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
    assert _anchor_contents(bundled) == set(TEXTS)
    assert _anchor_contents(unbundled) == _anchor_contents(bundled)
    assert calls.ran == ["extract", "generate"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan", [THEMES, REVIEW, EXTRACT_GENERATE], ids=["abstract-themes", "review-default", "extract-generate"],
)
async def test_unbundled_skip_generation_ends_in_the_skipped_output_and_runs_no_operator(plan: Any) -> None:
    name, bindings = plan
    bundled, _ = await _run(name, bindings, bundled=True, supports=False)
    unbundled, calls = await _run(name, bindings, bundled=False, supports=False)
    assert bundled.coverage is not None and bundled.coverage.skip_generation
    assert unbundled.coverage is not None and unbundled.coverage.skip_generation
    assert calls.ran == [], "the operators of the trailing run must not dispatch once the boundary skips"
    # One output per plan step, intermediates as the bundle path leaves them, the skipped output last.
    assert len(unbundled.steps) == len(bundled.steps) == len(_plan(name)["steps"])
    assert unbundled.steps[-1][SKIPPED_MARK] is True
    assert unbundled.steps[-1] == bundled.steps[-1]
    assert unbundled.final == bundled.final
    n_ops = len(_plan(name)["steps"]) - next(
        i for i, s in enumerate(_plan(name)["steps"]) if s["tool"] in ("groupby", "extract")
    )
    assert unbundled.steps[-n_ops:-1] == [dict(BUNDLED_INTERMEDIATE)] * (n_ops - 1)


@pytest.mark.asyncio
async def test_unbundled_continuation_cut_carries_the_coverage_state() -> None:
    """The cut classifier cuts an unbundled run at its first operator step, the step the boundary now sits before."""
    bundled, _ = await _run(*THEMES, bundled=True, cut_at=1)
    unbundled, calls = await _run(*THEMES, bundled=False, cut_at=1)
    assert unbundled.continuation_cut_applied and bundled.continuation_cut_applied
    assert calls.ran == []
    assert _anchor_contents(unbundled) == _anchor_contents(bundled) == set(TEXTS)
