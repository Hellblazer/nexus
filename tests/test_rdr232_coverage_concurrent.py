# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 (nexus-jypmj.17, nexus-jypmj.3.10): the coverage assessment runs its calls concurrently.

The 2026-10-28 live canary measured +118 s with the coverage check on: ``CoverageBoundary._assess`` made one
``operator_check`` call per evidence stream serially, the 65-anchor graph call hit the 60 s call timeout, and a
raised call stopped the loop. These tests pin the replacement: the calls of a stage run at most
``MAX_CONCURRENT_ASSESSMENT_CALLS`` at a time inside the stage's share of the deadline, a call that raises or replies
unusably costs only its own facets, a stream over ``MAX_ANCHORS_PER_ASSESSMENT_CALL`` anchors is split into packs,
the ledger and the call log are in plan order whatever order the calls finish in, and the terminal step's
``elapsed_ms`` no longer carries the assessment.

The timing model is the canary at 1/100 scale: a call takes ``BASE_S + PER_ANCHOR_S * anchors`` (0.15 s + 7 ms per
anchor here; 15 s + 0.7 s per anchor in the canary, so 10-15 anchors answer in about 20-25 s and 65 anchors need 60.5 s,
past the 60 s timeout), and a call longer than its ``timeout`` argument is cut the way the dispatcher cuts it.
Real ``asyncio.sleep`` is used; every bound below is a lower bound of the model or a generous multiple of it.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
import structlog

from nexus.operators.llm_api import DEFAULT_MAX_CONCURRENCY
from nexus.operators import llm_types
from nexus.operators.llm_types import DispatchUsage
from nexus.plans.coverage_ledger import (
    ASSESSMENT_CALL_COST_PRIOR_USD,
    MAX_ANCHORS_PER_ASSESSMENT_CALL,
    MAX_CONCURRENT_ASSESSMENT_CALLS,
    UNASSESSED_CAUSES,
    BoundaryView,
    CoverageBoundary,
    CoverageResult,
    _balanced_chunks,
)
from nexus.plans.run_summary import RunSummaryInputs, build_run_summary
from nexus.plans.runner import plan_run
from tests.test_rdr232_coverage_ledger import (
    CODE_COLL,
    PLAN,
    RDR_COLL,
    Dispatcher,
    _cov,
    _h,
    _match,
    _request,
    _reserved,
    _run,
    _structured,
    _tag_lines,
    supports_nothing,
)

_log = structlog.get_logger(__name__)

BASE_S = 0.15
PER_ANCHOR_S = 0.007
SCALE = 100  # one second here is one hundred in the canary
CALL_TIMEOUT_S = 0.6  # ASSESSMENT_CALL_TIMEOUT_S (60 s) at 1/100


def _latency(anchors: int) -> float:
    return BASE_S + PER_ANCHOR_S * anchors


def _stream_of_items(items: str) -> str:
    """The stream a call reads, from its first item's text: the Canary's vector, graph and other items are named for it,
    anything else is the reserved (primary) stream's."""
    head = json.loads(items)[0]["text"].split()[0]
    return head if head in ("vector", "graph", "other") else "primary"


def _reply(items: str, instruction: str, *, role: str = "supports") -> dict:
    first = json.loads(items)[0]["id"]
    return {"ok": True, "evidence": [{"item_id": first, "quote": f"[{t}] a short quote", "role": role} for t in _tag_lines(instruction)]}


class Canary(Dispatcher):
    """The canary's evidence: a reserved primary stream of 12, a vector stream of 12, a graph stream of 65 and 10
    unattributed items, 99 in all; every ``operator_check`` goes through ``self.model`` (an async callable)."""

    def __init__(self, model, *, vector: int = 12, graph: int = 65, other: int = 10) -> None:
        super().__init__(hydrates=True)
        self.model = model
        self.vec = [f"vector hit {i}" for i in range(vector)]
        self.gr = [f"graph hit {i}" for i in range(graph)]
        self.oth = [f"other hit {i}" for i in range(other)]
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "search":
            self.calls.append((tool, dict(args)))
            return _structured(self.vec, RDR_COLL)
        if tool == "traverse":
            self.calls.append((tool, dict(args)))
            return _structured(self.gr, CODE_COLL) | {"traversal": {"policy_applied": True, "widened": None}}
        if tool == "store_get_many":
            self.calls.append((tool, dict(args)))
            return {"contents": [*self.vec, *self.gr, *self.oth]}
        if tool == "operator_check":
            self.calls.append((tool, dict(args)))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                return await self.model(args)
            finally:
                self.in_flight -= 1
        return await super().__call__(tool, args)


async def modelled(args: dict) -> dict:
    """The scaled canary latency model: sleep ``_latency(anchors)``, or run into the call's own timeout."""
    n = len(json.loads(args["items"]))
    need = _latency(n)
    if need > args["timeout"]:
        await asyncio.sleep(args["timeout"])
        raise TimeoutError("the call outran its timeout")
    await asyncio.sleep(need)
    return _reply(args["items"], args["check_instruction"])


def _boundary(**kw: Any) -> CoverageBoundary:
    return CoverageBoundary(request=_request(1), call_timeout_s=CALL_TIMEOUT_S, min_call_s=0.01, **kw)


async def _timed(d: Dispatcher, boundary: CoverageBoundary, **kw: Any) -> tuple[CoverageResult, float]:
    started = time.monotonic()
    result = await _run(d, boundary, reserved_stream=_reserved(n=12), **kw)
    return _cov(result), time.monotonic() - started


# ── the timing model: before and after ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_canary_before_serial_one_pack_per_stream_runs_the_sum_and_the_graph_call_times_out() -> None:
    """The measured shape, reproduced with the new knobs set to the old behaviour (one call at a time, no split):
    four calls in a row, the 65-anchor one cut at the timeout, the stage the sum of its calls."""
    d = Canary(modelled)
    cov, elapsed = await _timed(d, _boundary(max_concurrent_calls=1, max_anchors_per_call=10**6))
    report = cov.ledger.assessment
    assert report.streams == ("primary", "vector", "graph", "other") and report.calls == 4
    assert report.raised_calls == 1 and report.reply_by_stream()["graph"]["raised"] == 1
    floor = _latency(12) + _latency(12) + CALL_TIMEOUT_S + _latency(10)
    assert elapsed >= floor, "serial calls add up"
    assert d.max_in_flight == 1
    _log.info("rdr232_canary_model_before", scaled_s=round(elapsed * SCALE), floor_s=round(floor * SCALE), measured_live_s=118)


@pytest.mark.asyncio
async def test_the_canary_after_runs_in_about_two_call_times_and_the_graph_pack_is_split() -> None:
    d = Canary(modelled)
    serial_floor = _latency(12) + _latency(12) + CALL_TIMEOUT_S + _latency(10)
    cov, elapsed = await _timed(d, _boundary())
    report = cov.ledger.assessment
    assert report.calls == 7, "primary, vector, other and the graph stream in four packs"
    assert report.raised_calls == 0 and report.outcome == "ok", "no call outran its timeout once the graph stream is split"
    assert sorted(report.facets_per_call) == [1] * 7
    assert d.max_in_flight == MAX_CONCURRENT_ASSESSMENT_CALLS
    two_calls = 2 * _latency(MAX_ANCHORS_PER_ASSESSMENT_CALL)
    # Slack for a loaded xdist box: the bounds sit well under what one-at-a-time costs (7 packs in a row: about
    # 1.75 s here, 1.29 s even for the old four-call serial design), so serialising still fails them.
    assert elapsed < two_calls + 0.5, f"7 calls in 2 waves of at most 4: {elapsed:.2f} s against {two_calls:.2f} s"
    assert elapsed < serial_floor * 0.85
    _log.info("rdr232_canary_model_after", scaled_s=round(elapsed * SCALE), serial_floor_s=round(serial_floor * SCALE))


@pytest.mark.asyncio
async def test_n_slow_calls_finish_in_about_one_call_time_not_n() -> None:
    """Four equal slow calls (two streams, two facet groups) at concurrency 4: one call's time. Failing before: they
    ran one after the other and took four."""
    delay = 0.3
    d = Canary(lambda args: _slow(args, delay), vector=5, graph=5, other=0)
    boundary = CoverageBoundary(request=_request(2), max_facets_per_call=1, call_timeout_s=5.0)
    started = time.monotonic()
    result = await _run(d, boundary)
    elapsed = time.monotonic() - started
    report = _cov(result).ledger.assessment
    assert report.calls == 4 and report.streams == ("vector", "graph")
    assert d.max_in_flight == 4
    assert delay <= report.latency_ms / 1000 < 3 * delay, f"4 calls of {delay} s took {report.latency_ms} ms (serial: {4 * delay} s)"
    assert elapsed < 4 * delay


async def _slow(args: dict, delay: float) -> dict:
    await asyncio.sleep(delay)
    return _reply(args["items"], args["check_instruction"])


@pytest.mark.asyncio
async def test_concurrency_is_bounded_by_the_boundary_setting() -> None:
    d = Canary(lambda args: _slow(args, 0.05), vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(4), max_facets_per_call=1, max_concurrent_calls=2, call_timeout_s=5.0)
    cov, _ = await _timed(d, boundary)
    assert cov.ledger.assessment.calls == 12  # primary, vector, graph x 4 facets
    assert d.max_in_flight == 2


@pytest.mark.parametrize(
    ("backend", "gate", "expect"),
    [("anthropic", "2", 2), ("anthropic", "1", 1), ("anthropic", "8", MAX_CONCURRENT_ASSESSMENT_CALLS),
     ("openai_compat", "3", 3), ("cli", "1", MAX_CONCURRENT_ASSESSMENT_CALLS)],
)
@pytest.mark.asyncio
async def test_the_concurrency_bound_reads_the_configured_gate_on_an_api_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, gate: str, expect: int,
) -> None:
    """The API backends gate HTTP attempts at ``NX_LLM_MAX_CONCURRENCY`` and a call past it waits for a permit inside
    its own timeout, so the stage asks for no more than the smaller of its constant and that setting. The ``claude -p``
    backend has no such gate (its 4 concurrent subprocesses are unmeasured), so there the constant alone bounds it.
    Failing before: the stage ran 4 at once whatever the setting."""
    monkeypatch.setenv("NX_LLM_BACKEND", backend)
    monkeypatch.setenv("NX_LLM_MAX_CONCURRENCY", gate)
    d = Canary(lambda args: _slow(args, 0.05), vector=3, graph=3, other=3)
    boundary = CoverageBoundary(request=_request(4), max_facets_per_call=1, call_timeout_s=5.0)
    cov, _ = await _timed(d, boundary)
    assert cov.ledger.assessment.calls == 16 and d.max_in_flight == expect
    assert expect == min(MAX_CONCURRENT_ASSESSMENT_CALLS, int(gate)) or backend == "cli"


def test_the_default_bound_matches_the_dispatchers_default_gate() -> None:
    assert 1 <= MAX_CONCURRENT_ASSESSMENT_CALLS <= DEFAULT_MAX_CONCURRENCY
    assert CoverageBoundary(request=None).max_concurrent_calls == MAX_CONCURRENT_ASSESSMENT_CALLS


@pytest.mark.asyncio
async def test_a_call_that_raises_cancellation_cancels_the_calls_still_running() -> None:
    """A ``CancelledError`` that is not our own ``wait_for``'s (a cancelled caller, a check that cancels itself) leaves
    the stage by cancelling its siblings. Failing before: ``gather`` re-raised it and left the other calls running."""
    started: list[int] = []
    cancelled: list[int] = []

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        n = len(started)
        started.append(n)
        if n == 0:
            await asyncio.sleep(0.02)
            raise asyncio.CancelledError
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(n)
            raise
        return {}

    view = await _direct_view(Dispatcher(), check)
    with pytest.raises(asyncio.CancelledError):
        await CoverageBoundary(request=_request(3), max_facets_per_call=1).evaluate(view)
    assert len(started) >= 3, "non-vacuity: several calls were running"
    assert sorted(cancelled) == list(range(1, len(started))), "every sibling saw its cancellation before the stage left"
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()] == []


# ── the stage never exceeds its share ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_check_that_ignores_its_timeout_is_cut_at_the_stage_share() -> None:
    """The stage may use 0.4 of the deadline left (here 1.0 s, so 0.4 s). A check that sleeps 5 s whatever it is told
    is cut from outside at the stage end, and the calls that never started are skipped, not queued past it."""
    async def ignores_timeout(args: dict) -> dict:  # noqa: ARG001
        await asyncio.sleep(5.0)
        return {}

    d = Canary(ignores_timeout, vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(1), call_timeout_s=10.0, min_call_s=0.01, max_concurrent_calls=2)
    started = time.monotonic()
    result = await _run(d, boundary, reserved_stream=_reserved(n=2), deadline=started + 1.0)
    cov = _cov(result)
    report = cov.ledger.assessment
    share = 0.4
    assert report.latency_ms <= (share + 0.4) * 1000, f"the stage took {report.latency_ms} ms of a {share} s share"
    assert report.raised_calls == 2 and report.calls == 2
    steps = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(steps) == 2 and all(not r.ok for r in steps), "a cut call is still a StepRecord"
    # What is knowable of a cut call's spend: nothing. The CLI reports cost only in its result event, which a killed
    # call never writes, so the record carries "unknown" (None), not a made-up zero.
    assert all(r.cost_usd is None for r in steps)
    # the third stream's call never started: its facet is already unassessed on the first call's cause
    assert [f.state for f in cov.ledger.findings] == ["unassessed"]


@pytest.mark.asyncio
async def test_queued_calls_draw_their_timeout_from_what_is_left_of_the_stage_when_they_start() -> None:
    """Six calls, two at a time, 0.3 s each, a 0.4 s stage: the first wave completes, the second starts at 0.3 s with
    0.1 s left and is cut at the stage end, the third wave is skipped for the deadline."""
    seen: list[float] = []

    async def record_timeout(args: dict) -> dict:
        seen.append(args["timeout"])
        return await _slow(args, 0.3)

    d = Canary(record_timeout, vector=3, graph=3, other=0)
    boundary = CoverageBoundary(request=_request(3), max_facets_per_call=1, call_timeout_s=10.0, min_call_s=0.05, max_concurrent_calls=2)
    started = time.monotonic()
    result = await _run(d, boundary, deadline=started + 1.0)
    report = _cov(result).ledger.assessment
    assert max(seen) <= 0.4 + 1e-3, "no call is given more than the stage allowance"
    assert seen[2] < 0.15 and seen[3] < 0.15, "the second wave drew what was left, not the call cap"
    assert report.latency_ms <= 700, "the third wave would end at 900 ms"
    assert report.outcome == "partial_failure"
    assert report.calls == 4 < 6, "the third wave had no time left and was skipped, not started"
    assert report.raised_calls == 2, "the second wave was cut at the stage end"


# ── one failed call costs only its own facets ───────────────────────────────────────────────────────────────


def _by_stream(**handlers: Any):
    async def model(args: dict) -> Any:
        handler = handlers.get(_stream_of_items(args["items"]), supports_nothing)  # a stream not named reads and finds nothing
        out = handler(args["items"], args["check_instruction"])
        if isinstance(out, BaseException):
            raise out
        return out
    return model


def _reasons(cov: CoverageResult) -> dict[str, str]:
    return {f.facet_id: f.reason for f in cov.ledger.findings if f.state == "unassessed"}


def _summary(cov: CoverageResult) -> dict[str, Any]:
    return build_run_summary(
        RunSummaryInputs(request=None), outcome="ok", plan_id=7, step_count=5, duration_ms=100, cost_usd=None,
        trace=True, coverage=cov, terminal_reached=True,
    )


@pytest.mark.asyncio
async def test_a_raised_call_leaves_only_its_own_facets_unassessed_and_the_others_still_count() -> None:
    """Three facets, two per call: the vector call over f3 raises. Failing before: the first raise stopped the pass,
    so f1/f2 were never read on the graph stream and f3 was not the only unassessed facet."""
    def vector(items: str, instruction: str):
        return RuntimeError("down") if "f3" in _tag_lines(instruction) else supports_nothing(items, instruction)

    d = Canary(_by_stream(vector=vector, graph=lambda i, s: supports_nothing(i, s), other=lambda i, s: supports_nothing(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(3), max_facets_per_call=2))
    n_checks = d.tools.count("operator_check")
    assert n_checks == cov.ledger.assessment.calls == 6, "every planned call was attempted: primary, vector and graph, two groups each"
    assert cov.ledger.assessment.raised_calls == 1
    assert _reasons(cov) == {"f3": "assessment_raised"}
    by = {f.facet_id: f.state for f in cov.ledger.findings}
    assert by["f1"] == by["f2"] == "unsupported", "read on every stream, nothing bears on them"
    s = _summary(cov)["coverage"]["unassessed_by_cause"]
    assert s["raised_call"] == 1 and s["stopped_after_failure"] == 0
    assert cov.ledger.assessment.outcome == "partial_failure", "five calls were read, one raised"


@pytest.mark.asyncio
async def test_a_malformed_reply_does_not_stop_the_other_calls_either() -> None:
    def vector(items: str, instruction: str):  # noqa: ARG001
        return {"ok": True}  # not the check shape

    d = Canary(_by_stream(vector=vector, graph=lambda i, s: supports_nothing(i, s), other=lambda i, s: supports_nothing(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(3), max_facets_per_call=2))
    assert cov.ledger.assessment.calls == 6
    assert _reasons(cov) == {"f1": "assessment_unusable", "f2": "assessment_unusable", "f3": "assessment_unusable"}
    cause = _summary(cov)["coverage"]["unassessed_by_cause"]
    assert cause["unusable_reply"] == 3 and cause["stopped_after_failure"] == 0


@pytest.mark.asyncio
async def test_another_stream_that_supports_covers_the_facet_a_failed_stream_could_not() -> None:
    d = Canary(_by_stream(vector=lambda i, s: RuntimeError("down"), graph=lambda i, s: _reply(i, s), other=lambda i, s: _reply(i, s)),
               vector=3, graph=3, other=0)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(2)))
    assert [f.state for f in cov.ledger.findings] == ["covered", "covered"]
    assert cov.ledger.assessment.outcome == "partial_failure" and cov.ledger.assessment.raised_calls == 1
    assert cov.state == "complete"


def test_stopped_after_failure_is_kept_in_the_enumeration_but_no_assessment_emits_it() -> None:
    """The cause stays a member of the closed list (old envelopes carry it; the wire is additive), and ``other`` stays
    last. A pass no longer stops at a failure, so only a skipped call (budget, deadline) leaves a facet unread, and those
    have their own causes."""
    assert "stopped_after_failure" in UNASSESSED_CAUSES and UNASSESSED_CAUSES[-1] == "other"


# ── deterministic order whatever the completion order ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_ledger_and_the_call_log_are_in_plan_order_whatever_order_the_calls_finish_in() -> None:
    """Earlier calls are the slower ones, so they finish last. The call log, the reply classes by stream and the
    unassessed counts are the plan's order and the same as when the calls finish in plan order."""
    async def run(slow_first: bool) -> tuple[CoverageResult, list[str]]:
        finished: list[str] = []

        async def model(args: dict) -> dict:
            stream = _stream_of_items(args["items"])
            rank = {"primary": 0, "vector": 1, "graph": 2, "other": 3}.get(stream, 0)
            await asyncio.sleep(0.04 * (3 - rank if slow_first else rank))
            finished.append(stream)
            if stream == "vector":
                return _reply(args["items"], args["check_instruction"])
            if stream == "graph":
                return {"ok": False, "evidence": []}
            return supports_nothing(args["items"], args["check_instruction"])

        d = Canary(model, vector=3, graph=3, other=2)
        cov, _ = await _timed(d, CoverageBoundary(request=_request(2), call_timeout_s=5.0))
        return cov, finished

    slow, order_slow = await run(True)
    fast, order_fast = await run(False)
    assert order_slow[0] != order_fast[0], "the two runs really finished in different orders"
    a, b = slow.ledger.assessment, fast.ledger.assessment
    assert [c.stream for c in a.call_log] == ["primary", "vector", "graph", "other"]
    assert a.call_log == b.call_log and a.reply_by_stream() == b.reply_by_stream() and a.reply_classes() == b.reply_classes()
    assert list(a.reply_by_stream()) == ["primary", "vector", "graph", "other"]
    assert slow.ledger.findings == fast.ledger.findings
    assert _summary(slow)["coverage"]["unassessed_by_cause"] == _summary(fast)["coverage"]["unassessed_by_cause"]


# ── the large pack ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_stream_is_split_into_the_fewest_balanced_packs_within_the_cap() -> None:
    assert [len(p) for p in _balanced_chunks(list(range(65)), 20)] == [17, 16, 16, 16]
    assert [len(p) for p in _balanced_chunks(list(range(20)), 20)] == [20]
    assert [len(p) for p in _balanced_chunks(list(range(21)), 20)] == [11, 10]
    assert _balanced_chunks([], 20) == []
    flat = [x for p in _balanced_chunks(list(range(65)), 20) for x in p]
    assert flat == list(range(65)), "order kept, nothing dropped or repeated"


@pytest.mark.asyncio
async def test_no_call_reads_more_than_the_pack_cap_and_every_anchor_is_read_once_per_group() -> None:
    """The canary's 65-anchor graph stream is four calls of 16-17, not one over the timeout. Failing before: one call
    carried all 65."""
    seen: list[list[str]] = []

    async def model(args: dict) -> dict:
        items = json.loads(args["items"])
        seen.append([i["text"] for i in items])
        return _reply(args["items"], args["check_instruction"])

    d = Canary(model)
    await _timed(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert max(len(s) for s in seen) <= MAX_ANCHORS_PER_ASSESSMENT_CALL
    graph = [s for s in seen if s[0].startswith("graph")]
    assert sorted(len(s) for s in graph) == [16, 16, 16, 17]
    assert sorted(t for s in graph for t in s) == sorted(d.gr), "every graph anchor in exactly one pack"
    assert len(d.gr) > MAX_ANCHORS_PER_ASSESSMENT_CALL, "non-vacuity: the stream really is over the cap"


@pytest.mark.asyncio
async def test_a_failed_pack_loses_its_own_anchors_not_the_stream() -> None:
    """The second graph pack raises; the fourth supports f1. f1 is covered by that evidence. With no supporting pack
    f1 is unassessed (a pack of the stream was never read) and the cause is the raised call."""
    calls = {"n": 0}

    async def second_graph_pack_raises(args: dict) -> dict:
        if _stream_of_items(args["items"]) == "graph":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("down")
            if calls["n"] == 4:
                return _reply(args["items"], args["check_instruction"])
        return supports_nothing(args["items"], args["check_instruction"])

    d = Canary(second_graph_pack_raises)
    cov, _ = await _timed(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert [f.state for f in cov.ledger.findings] == ["covered"]
    assert cov.ledger.assessment.raised_calls == 1

    calls["n"] = 0

    async def no_support(args: dict) -> dict:
        if _stream_of_items(args["items"]) == "graph":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("down")
        return supports_nothing(args["items"], args["check_instruction"])

    cov2, _ = await _timed(Canary(no_support), CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    assert [(f.state, f.reason) for f in cov2.ledger.findings] == [("unassessed", "assessment_raised")]


# ── the USD allowance ───────────────────────────────────────────────────────────────────────────────────────


async def _direct_view(first_dispatcher: Dispatcher, check, **kw: Any) -> BoundaryView:
    first = await _run(first_dispatcher, CoverageBoundary(request=_request(1)))
    prior = _cov(first)
    return BoundaryView(
        evidence_texts=[a.content for a in prior.ledger.anchors], observed=True, step_outputs=list(first.steps),
        tools=["search", "traverse", "store_get_many", "generate"], scope_diagnostics=[], resolved_args=[], check=check,
        remaining_seconds=kw.pop("remaining_seconds", lambda: None), remaining_usd=kw.pop("remaining_usd", lambda: None), **kw,
    )


@pytest.mark.asyncio
async def test_a_spent_usd_allowance_starts_no_call() -> None:
    called = 0

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        nonlocal called
        called += 1
        return _reply(args["items"], args["check_instruction"])

    view = await _direct_view(Dispatcher(), check, remaining_usd=lambda: 0.0)
    cov = await CoverageBoundary(request=_request(2)).evaluate(view)
    assert called == 0 and cov.ledger.assessment.outcome == "skipped_budget"
    assert _summary(cov)["coverage"]["unassessed_by_cause"]["budget_skip"] == 2


async def _count_starts(
    remaining_usd: float, *, estimate: float | None, n_facets: int = 4,
) -> tuple[int, CoverageResult, int]:
    """Starts, the result, and the peak number of calls in flight. ``remaining_usd`` and the estimate are fixed: the
    calls spend nothing the view can see, so what this measures is the wave the allowance admits at once."""
    started = 0
    live = peak = 0

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        nonlocal started, live, peak
        started += 1
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
        finally:
            live -= 1
        return supports_nothing(args["items"], args["check_instruction"])

    view = await _direct_view(
        Dispatcher(), check, remaining_usd=lambda: remaining_usd, assessment_cost_estimate=lambda: estimate,
    )
    cov = await CoverageBoundary(request=_request(n_facets), max_facets_per_call=1).evaluate(view)
    return started, cov, peak


@pytest.mark.asyncio
async def test_calls_in_flight_and_the_starting_call_are_counted_against_the_usd_allowance_at_the_mean_known_cost() -> None:
    """2.5 left, 1.0 a call: the first two start (2.5 less 1.0 and less 2.0 for those in flight and itself), the
    third would spend what is not there beside them (2.5 less 3.0) and is held until one ends, so a wave never
    overshoots the allowance: two at a time at most. (The fixture's allowance does not shrink as calls end, so the
    held calls then start one at a time; the shrinking case is ``_metered`` below.)"""
    started, cov, peak = await _count_starts(2.5, estimate=1.0)
    assert peak == 2, "the wave is bounded by the allowance at the measured cost"
    assert started == 8 == cov.ledger.assessment.calls  # 2 evidence streams x 4 facets


@pytest.mark.asyncio
async def test_the_first_wave_has_no_estimate_so_a_conservative_prior_bounds_it() -> None:
    """No call has reported a cost yet, so the mean is unknown. Failing before: every call of the first wave started
    whenever the allowance was above zero (four calls against an allowance for two). The prior is
    ``ASSESSMENT_CALL_COST_PRIOR_USD``: it must be at least the live canary's cost per call (0.0166 USD over 4 calls,
    nexus-jypmj.3.10), and the allowance here is two and a half of them. The calls the prior holds back start one at
    a time once the wave ends (nothing reported a cost and the allowance is still above zero)."""
    prior = ASSESSMENT_CALL_COST_PRIOR_USD
    assert prior >= 0.0166 / 4, "the prior is no smaller than a cost already measured"
    started, cov, peak = await _count_starts(prior * 2.5, estimate=None)
    assert peak == 2, "two calls fit beside each other; the third would be the one past the allowance"
    assert started == 8 == cov.ledger.assessment.calls  # 2 evidence streams x 4 facets


@pytest.mark.asyncio
async def test_a_small_allowance_still_starts_one_call_as_the_serial_loop_did() -> None:
    """Nothing in flight and a positive allowance: the call starts even when one call may cost more than what is left
    (the budget is soft; one call past it is the accepted overshoot). Without this a small ``budget_usd`` would
    assess nothing."""
    started, cov, peak = await _count_starts(ASSESSMENT_CALL_COST_PRIOR_USD / 10, estimate=None)
    assert peak == 1 and started == 8 == cov.ledger.assessment.calls  # one at a time: the prior holds the rest back


async def _metered(
    left: float, *, cost: float | None, n_facets: int = 7, report_cost: bool = True,
) -> tuple[int, CoverageResult, int]:
    """A wave against a live allowance: every call spends *cost*, and ``remaining_usd`` is what the calls have left of
    *left*. With *report_cost* False the call's cost is unknown (a cut call): neither the estimate nor the remaining
    allowance sees it, as in the runner, which derives both from ``StepRecord.cost_usd``. Returns the starts, the
    result and the peak number of calls in flight."""
    started = 0
    live = peak = 0
    reported: list[float] = []
    state = {"left": left}

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        nonlocal started, live, peak
        started += 1
        live += 1
        peak = max(peak, live)
        try:
            await asyncio.sleep(0.05)
        finally:
            live -= 1
        if cost is not None and report_cost:
            state["left"] -= cost
            reported.append(cost)
        return supports_nothing(args["items"], args["check_instruction"])

    view = await _direct_view(
        Dispatcher(), check, remaining_usd=lambda: state["left"],
        assessment_cost_estimate=lambda: sum(reported) / len(reported) if reported else None,
    )
    cov = await CoverageBoundary(request=_request(n_facets), max_facets_per_call=1).evaluate(view)
    return started, cov, peak


@pytest.mark.asyncio
async def test_calls_held_on_the_prior_alone_run_once_the_first_reports_a_cost_that_fits() -> None:
    """The probe (nexus-jypmj.22). 0.03 USD left, 0.004 a call, seven planned calls. Failing before: every call decided
    while the first was in flight was skipped against the 0.02 prior (0.03 less 0.04 is under zero) and never looked at
    again, so one call ran and the rest were left unassessed. Now they are held, the first reports 0.004, and the
    allowance covers as many calls as fit: 0.03 at 0.004 a call is seven (0.028 <= 0.03), out of the 14 planned
    (two evidence streams, seven facets each), and the other seven are skipped on the MEASURED cost."""
    started, cov, _ = await _metered(0.03, cost=0.004)
    a = cov.ledger.assessment
    assert started == a.calls == 7 and a.skipped_budget_calls == 7 and a.outcome == "partial_failure"


@pytest.mark.asyncio
async def test_a_call_that_does_not_fit_beside_those_in_flight_is_held_again_not_skipped_for_good() -> None:
    """0.012 left, 0.004 a call: the first runs and the rest are held. With 0.004 measured, 0.008 is left: one call
    starts, the next would not fit beside it (0.008 less 0.008 is zero but 0.004 is in flight too) and is HELD, not
    cut; when the one in flight ends and 0.004 is left, that call fits alone and runs. Three calls fit (3 x 0.004 =
    0.012) and three run; the rest cannot fit even alone and are skipped on the measured cost. Failing before: the
    call that did not fit beside the one in flight was skipped for good and only two ran."""
    started, cov, peak = await _metered(0.012, cost=0.004)
    a = cov.ledger.assessment
    assert started == a.calls == 3 and a.skipped_budget_calls == 11
    assert peak <= 2, "the wave never holds more calls than the allowance covers"
    assert a.skipped_deadline_calls == 0 and a.outcome == "partial_failure" and "USD" in a.detail
    assert _summary(cov)["coverage"]["unassessed_by_cause"]["budget_skip"] > 0


@pytest.mark.asyncio
async def test_a_call_that_cannot_fit_even_alone_is_skipped_on_the_measured_cost() -> None:
    """0.005 left, 0.004 a call: the first runs (nothing measured, nothing in flight) and spends 0.004; 0.001 is left,
    which a 0.004 call cannot fit even alone, so every other call is skipped. One call, not two: the old rule started
    any call that found nothing in flight whenever the allowance was above zero, past a cost already measured."""
    started, cov, _ = await _metered(0.005, cost=0.004)
    a = cov.ledger.assessment
    assert started == a.calls == 1 and a.skipped_budget_calls == 13
    assert "cannot cover" in a.detail, "no allowance is claimed spent when the cost says a call does not fit"


@pytest.mark.asyncio
async def test_a_budget_too_small_for_one_measured_call_still_skips_the_rest() -> None:
    """0.0015 left (the live row 898's allowance), 0.004 a call: the first call runs, spends past the allowance, and
    the held calls find none left."""
    started, cov, _ = await _metered(0.0015, cost=0.004)
    a = cov.ledger.assessment
    assert started == a.calls == 1 and a.skipped_budget_calls == 13 and a.outcome == "partial_failure"
    assert "was spent" in a.detail


@pytest.mark.asyncio
async def test_calls_held_on_the_prior_run_one_at_a_time_when_no_call_ever_reports_a_cost() -> None:
    """The first call ends without a reported cost (the runner's view of a cut call: neither the estimate nor the
    remaining allowance moves). The allowance is still above zero, so a held call starts when none is in flight, as
    the module docstring says ('one call always starts when none is in flight and the allowance is above zero'), one
    at a time. Failing before: the held calls were skipped for budget though nothing had been spent."""
    started, cov, peak = await _metered(0.03, cost=0.004, report_cost=False)
    a = cov.ledger.assessment
    assert started == a.calls == 14 and a.skipped_budget_calls == 0 and a.skipped_deadline_calls == 0
    assert peak == 1, "the prior still says only one call fits at a time, so they run serially"
    assert a.outcome == "ok" and "USD" not in a.detail


@pytest.mark.asyncio
async def test_an_expired_deadline_still_skips_a_held_call_for_the_deadline() -> None:
    """The deadline path is unchanged: the held calls wake after the first call, find no time left for a call, and
    are skipped for the deadline (the first started with time, then the clock ran out)."""
    clock = {"left": 600.0}
    state = {"left": 0.03}
    reported: list[float] = []

    async def check(args: dict, *, timeout: float) -> dict:  # noqa: ARG001
        await asyncio.sleep(0.05)
        clock["left"] = 0.0  # the stage's share is gone once the first call ends
        state["left"] -= 0.004
        reported.append(0.004)
        return supports_nothing(args["items"], args["check_instruction"])

    view = await _direct_view(
        Dispatcher(), check, remaining_usd=lambda: state["left"], remaining_seconds=lambda: clock["left"],
        assessment_cost_estimate=lambda: sum(reported) / len(reported) if reported else None,
    )
    cov = await CoverageBoundary(request=_request(7), max_facets_per_call=1).evaluate(view)
    a = cov.ledger.assessment
    assert a.calls == 1 and a.skipped_deadline_calls == 13 and a.skipped_budget_calls == 0
    assert _summary(cov)["coverage"]["unassessed_by_cause"]["deadline_skip"] == 7


@pytest.mark.asyncio
async def test_the_cost_estimate_averages_assessment_calls_only_not_the_plans_own_check_steps() -> None:
    """The runner's estimate was the mean of every ``operator_check`` record with a cost, so a plan's own ``check``
    step (a different prompt, here 1.0 USD) inflated what an assessment call was assumed to cost. 1.012 USD is left
    before the plan's check spends 1.0, so 0.012 reaches the assessment, and each assessment call costs 0.004: three
    fit. With the check step in the mean the estimate after the first call is 0.502 and nothing else fits (one
    call). Records are told apart by step index: assessment calls are numbered past the plan's own steps."""
    # A fetch sits between the plan's check and the terminal step: the boundary precedes the plan's TRAILING operator
    # run (nexus-jypmj), so a check directly before the terminal operator would run after it, and the budget the
    # test pins (spent by the check before the assessment) would never be spent.
    plan = {"steps": [
        *PLAN["steps"][:3],
        {"tool": "operator_check", "args": {"items": "$step3.contents", "check_instruction": "plan check"}},
        PLAN["steps"][2],
        PLAN["steps"][3],
    ]}

    async def model(args: dict) -> dict:
        sink = llm_types._ambient_usage_sink.get()
        assert sink is not None
        plan_check = args["check_instruction"] == "plan check"
        await asyncio.sleep(0.05)
        sink.append(DispatchUsage(
            model="haiku", cost_usd=1.0 if plan_check else 0.004, input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1,
        ))
        if plan_check:
            return {"ok": True, "evidence": []}
        return supports_nothing(args["items"], args["check_instruction"])

    d = Canary(model, vector=3, graph=3, other=0)
    result = await _run(d, CoverageBoundary(request=_request(7), max_facets_per_call=1, call_timeout_s=5.0), plan=plan, budget_usd_remaining=1.012)
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert sorted(r.cost_usd for r in checks)[-1] == 1.0, "non-vacuity: the plan's own check step cost 1.0 and is recorded"
    a = _cov(result).ledger.assessment
    assert a.calls == 3 and a.skipped_budget_calls == 11, (a.calls, a.skipped_budget_calls)


# ── the runner: accounting and the terminal step's elapsed ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_assessment_time_is_not_charged_to_the_terminal_step() -> None:
    """Failing before: the terminal step's ``elapsed_ms`` started before the boundary, so a 0.4 s assessment showed as
    0.4 s of generation in ``nx_answer_steps``. The assessment calls carry their own time."""
    async def slow_assessment(args: dict) -> dict:
        return await _slow(args, 0.4)

    d = Canary(slow_assessment, vector=3, graph=3, other=0)
    result = await _run(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    gen = [r for r in result.step_records if r.operator == "generate"]
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(gen) == 1 and checks
    assert gen[0].elapsed_ms < 200, f"generation was charged {gen[0].elapsed_ms} ms"
    assert all(r.elapsed_ms >= 380 for r in checks)
    assert _cov(result).ledger.assessment.latency_ms >= 380, "the run summary's assessment_latency_ms still has it"


@pytest.mark.asyncio
async def test_assessment_time_is_not_charged_to_a_bundled_terminal_step_either() -> None:
    plan = {"steps": [
        PLAN["steps"][0], PLAN["steps"][1], PLAN["steps"][2],
        {"tool": "rank", "args": {"criterion": "c", "items": "$step3.contents"}},
        {"tool": "generate", "args": {"context": "$step4.ranked", "question": "q"}},
    ]}

    async def slow_assessment(args: dict) -> dict:
        return await _slow(args, 0.4)

    d = Canary(slow_assessment, vector=3, graph=3, other=0)

    async def fake_bundle(bundle, **kwargs):  # noqa: ARG001
        return {"text": "bundled answer"}

    class _Wrapper:
        supports_bundling = True

        async def __call__(self, tool: str, args: dict) -> dict:
            return await d(tool, args)

    with patch("nexus.plans.bundle.dispatch_bundle", fake_bundle):
        result = await plan_run(_match(plan), {}, dispatcher=_Wrapper(), coverage_boundary=CoverageBoundary(request=_request(1), call_timeout_s=5.0))
    bundle_records = [r for r in result.step_records if r.operator not in ("operator_check", "search", "traverse", "store_get_many")]
    assert bundle_records and all(r.elapsed_ms < 200 for r in bundle_records), [(r.operator, r.elapsed_ms) for r in bundle_records]


@pytest.mark.asyncio
async def test_concurrent_calls_keep_their_own_usage_and_step_index() -> None:
    """Each concurrent call reads the ambient usage sink of its own task: its StepRecord carries its own cost, and the
    step indices are distinct and numbered past the plan."""
    costs = {"primary": 0.01, "vector": 0.02, "graph": 0.03}

    async def model(args: dict) -> dict:
        stream = _stream_of_items(args["items"])
        sink = llm_types._ambient_usage_sink.get()
        assert sink is not None
        await asyncio.sleep(0.05 if stream == "primary" else 0.02)  # interleave
        sink.append(DispatchUsage(
            model="haiku", cost_usd=costs.get(stream, 0.0), input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
            cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1,
        ))
        return _reply(args["items"], args["check_instruction"])

    d = Canary(model, vector=3, graph=3, other=0)
    result = await _run(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0), reserved_stream=_reserved(n=2))
    checks = [r for r in result.step_records if r.operator == "operator_check"]
    assert len(checks) == 3
    assert sorted(r.cost_usd for r in checks) == [0.01, 0.02, 0.03]
    indices = [r.step_index for r in checks]
    assert len(set(indices)) == 3 and min(indices) >= len(PLAN["steps"])


@pytest.mark.asyncio
async def test_step_records_are_in_step_index_order_whatever_order_the_calls_finish_in() -> None:
    """The first assessment calls are the slowest, so they finish last and are recorded last. The result lists the
    records by step index, so the envelope's step order is the same on every run."""
    finished: list[str] = []

    async def model(args: dict) -> dict:
        stream = _stream_of_items(args["items"])
        rank = {"primary": 0, "vector": 1, "graph": 2, "other": 3}.get(stream, 0)
        await asyncio.sleep(0.05 * (3 - rank))
        finished.append(stream)
        return _reply(args["items"], args["check_instruction"])

    d = Canary(model, vector=3, graph=3, other=2)
    result = await _run(d, CoverageBoundary(request=_request(1), call_timeout_s=5.0), reserved_stream=_reserved(n=2))
    assert finished[0] != "primary", "non-vacuity: the calls finished out of plan order"
    indices = [r.step_index for r in result.step_records]
    assert len([r for r in result.step_records if r.operator == "operator_check"]) == 4
    assert indices == sorted(indices) and len(set(indices)) == len(indices)
