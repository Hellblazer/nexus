# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P1: a measurement harness for nx_answer's T2 fan-out.

Design record: T2 nexus/design-nexus-m20mf-single-t2-transport (T2 [24553]).
Bead: nexus-m20mf.

Counts, never timings — T2Database constructions, real httpx.Client
constructions, and GET /version capability-probe requests during ONE (or a
handful of) nx_answer call(s) on a fixed question. Runs against the
self-provisioned engine TEST substrate only — the suite's autouse
``_pin_t2_substrate`` fixture (``tests/conftest.py`` + ``tests/
_engine_substrate.py``), a per-process hermetic PG + service JAR with a
freshly minted per-test tenant. Never production; never a mocked
``_t2_ctx``/``_t2_index_write``, since that transport is exactly what this
file measures.

Only two things are mocked, deliberately narrow and both ABOVE the T2
transport layer this harness counts: ``nexus.plans.matcher.plan_match``
(the ranking algorithm, forced to a deterministic HIT so the inline LLM
planner's ``claude -p`` subprocess is never reached) and ``plan_run``
(Step 4's step EXECUTION, irrelevant to how many T2 contexts Steps 1/2/6
open). This is NOT the fully mocked-I/O harness at
``tests/test_nx_answer.py:2445`` (``TestNxAnswerLatencyProxy``, which also
patches ``_t2_ctx`` itself) — that harness is explicitly out of scope for
counting T2 transport activity, per the design record.

These are regression PINS, not prints, per nexus-m20mf P2: BEFORE the
P2 routing fix, nx_answer's five happy-path contexts (plan match, price
table, run_start, record_run, run_outcome) each opened a fresh
``with _t2_ctx() as db:`` block — one ``T2Database`` (8 ``Http*Store``
constructions, 8 ``httpx.Client`` pools) per context, 5 per call. AFTER
P2 (routed through the shared ``_t2_index_write`` singleton), one call
resolves AT MOST ONE ``T2Database``, and the ``GET /version`` capability
probe (``HttpTelemetryStore._supports_nx_answer_steps``, cached PER STORE
INSTANCE) survives across calls in the same process instead of re-firing
on every one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _instrument_t2_construction(monkeypatch) -> list[int]:
    """Wrap ``T2Database.__init__`` to COUNT constructions without faking
    behavior — every instance is still real, still talks to the real
    per-test-tenant engine substrate. Returns the running tally list."""
    import nexus.db.t2 as _t2_mod

    tally: list[int] = []
    orig_init = _t2_mod.T2Database.__init__

    def _counting_init(self, *args, **kwargs):
        tally.append(1)
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(_t2_mod.T2Database, "__init__", _counting_init)
    return tally


def _instrument_httpx_clients(monkeypatch) -> tuple[list[int], list[tuple[str, str]]]:
    """Wrap ``httpx.Client.__init__``/``.send`` to count constructions and
    record every outbound request's (method, path) — real requests still
    go out over real connections; nothing here is faked."""
    constructions: list[int] = []
    requests: list[tuple[str, str]] = []

    orig_init = httpx.Client.__init__
    orig_send = httpx.Client.send

    def _counting_init(self, *args, **kwargs):
        constructions.append(1)
        return orig_init(self, *args, **kwargs)

    def _recording_send(self, request, *args, **kwargs):
        requests.append((request.method, request.url.path))
        return orig_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    monkeypatch.setattr(httpx.Client, "send", _recording_send)
    return constructions, requests


async def _run_nx_answer_happy_path(question: str) -> str:
    """Drive one real nx_answer() call through the plan_run
    ("needs_operators") happy path — the shape that fires all five
    contexts named in the design record (plan_match, price_table,
    run_start, record_run, run_outcome). Only the plan-match RANKING and
    plan_run's step EXECUTION are mocked; the T2 transport underneath is
    untouched."""
    import nexus.mcp_infra as _infra
    import nexus.plans.runner as _runner
    from nexus.plans.runner import PlanResult, StepRecord
    from tests.test_nx_answer import _make_match

    match = _make_match(confidence=0.9)
    # A non-empty step_records is required to exercise the GET /version
    # capability probe at all (HttpTelemetryStore.record_nx_answer_run
    # skips it entirely when `steps` is falsy) — an earlier version of
    # this harness used PlanResult's step_records=[] default and silently
    # measured nothing for that metric.
    run_result = PlanResult(
        steps=[{"text": "The final answer."}],
        step_records=[
            StepRecord(step_index=0, operator="query", source="sql", cost_usd=0.0),
        ],
    )

    with (
        patch("nexus.plans.matcher.plan_match", return_value=[match]),
        patch.object(_infra, "get_t1_plan_cache",
                     return_value=MagicMock(is_available=False)),
        patch("nexus.mcp.core.scratch", MagicMock()),
        patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
    ):
        from nexus.mcp.core import nx_answer
        return await nx_answer(question)


@pytest.mark.asyncio
async def test_happy_path_constructs_at_most_one_t2_store(monkeypatch) -> None:
    """ONE nx_answer call's five happy-path T2 contexts must resolve AT
    MOST ONE T2Database. Pre-nexus-m20mf-P2 baseline (measured by AST
    census, design record §1): 5 — one fresh construction per
    ``with _t2_ctx()`` context."""
    constructed = _instrument_t2_construction(monkeypatch)

    result = await _run_nx_answer_happy_path("what is projection quality?")

    assert "final answer" in result.lower()
    assert len(constructed) <= 1, (
        f"expected at most one T2Database construction across the happy "
        f"path's five T2 contexts (plan_match, price_table, run_start, "
        f"record_run, run_outcome); got {len(constructed)}"
    )


@pytest.mark.asyncio
async def test_happy_path_opens_at_most_eight_connections(monkeypatch) -> None:
    """ONE nx_answer call must open AT MOST EIGHT httpx.Client pools — the
    eight Http*Store clients of a SINGLE T2Database. Pre-nexus-m20mf-P2
    baseline: 40 (5 contexts x 8 stores each its own pool, discarded at
    every context boundary — the pool-churn cost the design record's §1
    'two costs neither measurement captured' names as the real argument,
    distinct from the retracted 90%-of-a-mocked-harness figure)."""
    constructed, _requests = _instrument_httpx_clients(monkeypatch)

    result = await _run_nx_answer_happy_path("what is projection quality?")

    assert "final answer" in result.lower()
    assert len(constructed) <= 8, (
        f"expected at most 8 httpx.Client constructions (one T2Database's "
        f"worth of Http*Store pools) for one nx_answer call; got "
        f"{len(constructed)} (pre-nexus-m20mf-P2 baseline: 40)"
    )


@pytest.mark.asyncio
async def test_happy_path_issues_at_most_one_version_probe_across_two_calls(
    monkeypatch,
) -> None:
    """TWO nx_answer calls in the same process must together issue AT MOST
    ONE ``GET /version`` capability probe (``HttpTelemetryStore.
    _supports_nx_answer_steps``, cached per store INSTANCE — design
    record §1 'a defeated cache'). A single call cannot discriminate this
    metric: both before and after nexus-m20mf-P2 a fresh process's first
    call always probes once. The FIX is that a SECOND call reuses the
    same telemetry store instance and its already-populated cache;
    pre-P2, the second call's fresh ``with _t2_ctx()`` telemetry store
    re-probes, for 2 total."""
    _client_constructions, requests = _instrument_httpx_clients(monkeypatch)

    r1 = await _run_nx_answer_happy_path("what is projection quality?")
    r2 = await _run_nx_answer_happy_path("what is projection quality, again?")

    assert "final answer" in r1.lower()
    assert "final answer" in r2.lower()

    version_probes = sum(1 for method, path in requests if path == "/version")
    assert version_probes <= 1, (
        f"expected at most one GET /version capability probe across two "
        f"nx_answer calls in the same process (the probe is cached per "
        f"store instance, and after nexus-m20mf-P2 that instance is the "
        f"shared singleton); got {version_probes} (pre-P2 baseline: 2, "
        f"one per fresh telemetry store)"
    )
