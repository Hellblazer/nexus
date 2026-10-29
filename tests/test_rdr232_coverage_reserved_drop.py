# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.3.14: turning the coverage check on must not change what the reserved stream reports.

The 7.88.0 shakedown saw ``reserved_stream.dropped_before_terminal = {step: 5, missing: N, of: N}`` on
every coverage-on run of plan 473 (``... rank -> generate``) and on no coverage-off run. The coverage
assessment's own ``operator_check`` calls went through the dispatcher's operator hydration while the
reserved stream still pointed at the terminal step (fed by a rank), so the stream observed the
assessment payload as "the operator after the rank" and recorded every reserved item dropped. The
repair's second ledger then read that state, marked the named source missing, blocked every facet
``source_missing`` and skipped generation.

These tests drive ``plan_run`` with the REAL default dispatcher (it hydrates operator args, as
production does; the stub dispatchers of the ledger and repair suites do not) and stub the tool
functions it calls, so no model call is made. Coverage on and off are run on the same plan and
evidence and must agree.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from nexus.mcp import core as mcp_core
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.runner import plan_run
from tests.test_rdr232_coverage_ledger import (
    RDR_COLL,
    VECTOR_TEXTS,
    _h,
    _match,
    _primary,
    _reserved,
    _resolved_source,
    _structured,
    _tag_lines,
)

REPAIR_TEXT = "repair hit: the second facet is answered here"
FACET_TEXT = "what the document says about topic {n}"
#: f1 is answered by the named source's reserved chunks; f2 only by the repair hit, so the first ledger
#: has a repairable gap and the repair's second ledger runs (the shakedown's route).
WORDS = {"f1": "RDR-084 TEXT", "f2": "repair hit"}

#: Plan 473's shape: retrieval, an explicit fetch, then ``rank -> generate`` (bundled in production).
PLAN = {"steps": [
    {"tool": "search", "args": {"query": "q", "corpus": "rdr"}},
    {"tool": "store_get_many", "args": {"ids": "$step1.ids", "collections": "rdr"}},
    {"tool": "rank", "args": {"criterion": "c", "inputs": "$step2.contents"}},
    {"tool": "generate", "args": {"context": "$step3.ranked", "question": "q"}},
]}


@pytest.fixture(autouse=True)
def _no_catalog_lookup():
    with patch("nexus.plans.runner._resolve_non_evidentiary_chashes", return_value=set()):
        yield


def _request() -> RequestCoverage:
    return RequestCoverage(answer_kind="synthesis", facets=tuple(
        Facet(f"f{i}", FACET_TEXT.format(n=i), "clause", (), ()) for i in (1, 2)
    ))


class _Tools:
    """Stubs for the ``mcp_core`` functions the default dispatcher calls; records what the reductions got."""

    def __init__(self) -> None:
        self.checks: list[str] = []
        self.generate_context: list[str] = []
        self.bundle_items: list[str] = []
        self.known = {_h(t): t for t in [*VECTOR_TEXTS, REPAIR_TEXT]}

    async def search(self, **kwargs: Any) -> dict:
        query = str(kwargs.get("query", kwargs.get("question", "")))
        texts = [REPAIR_TEXT] if query.startswith("what the document says") else list(VECTOR_TEXTS)
        return _structured(texts, RDR_COLL)

    def store_get_many(self, ids, collections=None, structured=False, **kwargs: Any) -> dict:  # noqa: ARG002
        return {"contents": [self.known.get(str(i), "") for i in ids]}

    async def rank(self, items: str, criterion: str, timeout: float = 300.0, model: str | None = None) -> dict:  # noqa: ARG002
        parsed = json.loads(items) if isinstance(items, str) and items.startswith("[") else [items]
        return {"ranked": parsed}  # keeps every item: nothing is dropped

    async def check(self, items: str, check_instruction: str, timeout: float = 300.0, model: str | None = None) -> dict:  # noqa: ARG002
        self.checks.append(check_instruction)
        payload = json.loads(items)
        evidence = [
            {"item_id": it["id"], "quote": f"[{tag}] quoted", "role": "supports"}
            for tag in _tag_lines(check_instruction) for it in payload if WORDS[tag] in it["text"]
        ]
        if not evidence and payload:
            tag = _tag_lines(check_instruction)[0]
            evidence = [{"item_id": payload[0]["id"], "quote": f"[{tag}] only mentions the topic", "role": "neutral"}]
        return {"ok": bool(evidence), "evidence": evidence}

    async def generate(self, **kwargs: Any) -> dict:
        self.generate_context.append(str(kwargs.get("context", "")))
        return {"text": "the answer"}

    async def bundle(self, bundle, **kwargs: Any) -> dict:  # noqa: ARG002
        self.bundle_items.append(str(bundle.steps[0].args.get("items", "")))
        return {"text": "the bundled answer"}


async def _run(*, coverage: bool, bundled: bool):
    tools = _Tools()
    stream = _reserved()
    boundary = CoverageBoundary(request=_request(), named_sources=_resolved_source(), primary=_primary()) if coverage else None
    with patch.object(mcp_core, "search", tools.search), patch.object(mcp_core, "store_get_many", tools.store_get_many), \
         patch.object(mcp_core, "operator_rank", tools.rank), patch.object(mcp_core, "operator_check", tools.check), \
         patch.object(mcp_core, "operator_generate", tools.generate), patch("nexus.plans.bundle.dispatch_bundle", tools.bundle):
        result = await plan_run(_match(PLAN), {}, bundle_operators=bundled, reserved_stream=stream, coverage_boundary=boundary)
    terminal = (tools.bundle_items if bundled else tools.generate_context)
    reached = {i.token for i in stream.items if any(i.token in t for t in terminal)}
    return result, stream, tools, reached


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [True, False], ids=["bundled_rank_generate", "isolated_rank_then_generate"])
async def test_coverage_on_and_off_agree_on_the_reserved_items_reaching_the_terminal_reduction(bundled: bool) -> None:
    _, off_stream, _, off_reached = await _run(coverage=False, bundled=bundled)
    on_result, on_stream, on_tools, on_reached = await _run(coverage=True, bundled=bundled)
    assert len(on_tools.checks) >= 2, "the assessment and the repair's reassessment both ran (the shakedown's route)"
    assert off_reached == {i.token for i in off_stream.items}, "coverage off: every reserved item reaches the terminal reduction"
    assert on_reached == off_reached, "coverage on hands the terminal reduction the same reserved items"
    assert on_stream.report()["dropped_before_terminal"] == off_stream.report()["dropped_before_terminal"] is None, (
        "the assessment's operator_check is not a plan operator and must not be observed as one"
    )
    assert on_stream.last_missing_tokens == off_stream.last_missing_tokens == ()
    assert not on_stream.disclosures(), "no 'did not reach the final reduction' disclosure"


@pytest.mark.asyncio
@pytest.mark.parametrize("bundled", [True, False], ids=["bundled_rank_generate", "isolated_rank_then_generate"])
async def test_the_repair_ledger_does_not_mark_a_named_source_missing_when_its_reserved_chunks_were_present(bundled: bool) -> None:
    result, _, tools, _ = await _run(coverage=True, bundled=bundled)
    cov = result.coverage
    assert cov is not None and cov.ledger.rounds == 2, "the repair's second ledger ran"
    (row,) = cov.ledger.sources
    assert row.state != "missing", f"RDR-084's reserved chunks reached the reducer; ledger says {row.state}: {row.detail}"
    findings = {f.facet_id: (f.state, f.reason) for f in cov.ledger.findings}
    assert findings["f1"][0] == "covered", findings
    assert all(reason != "source_missing" for _, reason in findings.values()), findings
    assert (cov.state, cov.skip_generation) == ("complete", False)
    assert (tools.bundle_items if bundled else tools.generate_context), "the terminal reduction ran"
