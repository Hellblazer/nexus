# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w8d30 (RDR-250): the pool filter's assessment calls ask for the pool filter's own model, and no other call does.

The 2026-11-17 stability probe (T2 nexus_rdr/250-stability-probe-2026-11-17) found the filter's keep/reject verdict varied on
identical input on the cheap tier; Sam chose a stronger model for the filter (2026-11-18). The fixtures are
``tests/test_rdr250_pool_key_scan.py``'s."""
from __future__ import annotations

import json
from typing import Any

import pytest

from nexus.operators import model_tiers
from nexus.plans import coverage_pool
from tests.test_rdr232_coverage_ledger import _cov
from tests.test_rdr232_coverage_repair import _run
from tests.test_rdr250_pool_key_scan import (  # noqa: F401 — the autouse fixtures must be in this module's namespace
    POOL_PLAN,
    PoolDispatcher,
    _entity_config,
    _fixture,
    _no_catalog_lookup,
    pool_off,
    pool_on,
)

pytestmark = pytest.mark.usefixtures("rowless_collection_cache")  # RDR-229 P2.9: a tenant-free test takes item 1's NoRow outcome without a listing


class _Recording(PoolDispatcher):
    """Records the args of every assessment call, split into pool-filter calls (items ``p<n>``) and the rest."""

    def __init__(self, *a: Any, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.checks: list[tuple[bool, dict]] = []

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "operator_check":
            pool = any(i["id"].startswith(coverage_pool._POOL_ID_PREFIX) for i in json.loads(args["items"]))
            self.checks.append((pool, dict(args)))
        return await super().__call__(tool, args)


def _recording() -> _Recording:
    base = _fixture()
    return _Recording(base.anchor_texts, base.pool_texts)


def test_the_pool_filter_model_is_not_the_check_tiers() -> None:
    """A guard on the decision (Sam 2026-11-18), not coverage: re-pointing the filter at the check tier's alias is red."""
    assert model_tiers.POOL_FILTER_ALIAS == "sonnet"
    assert model_tiers.POOL_FILTER_ALIAS != model_tiers._TIER_ALIASES[model_tiers.OPERATOR_MODEL_TIER["operator_check"]]


@pytest.mark.asyncio
async def test_with_the_switch_on_only_the_pool_filter_calls_carry_the_pool_filter_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_OPERATOR_MODEL_TIERING", raising=False)
    d = _recording()
    cov = _cov(await _run(d, pool_on(), plan=POOL_PLAN))
    pool_calls = [a for p, a in d.checks if p]
    other_calls = [a for p, a in d.checks if not p]
    assert pool_calls and other_calls, "non-vacuity: the run made both kinds of assessment call"
    assert cov.repair.pool_filter_calls == len(pool_calls)
    assert {a.get("model") for a in pool_calls} == {model_tiers.POOL_FILTER_ALIAS}
    assert not [a for a in other_calls if "model" in a], "the boundary's own assessment keeps the check tier's routing"
    assert not [a for _, a in d.checks if coverage_pool.POOL_FILTER_KEY in a], "the runner's marker never reaches the operator"


@pytest.mark.asyncio
async def test_the_marker_changes_no_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub ignores ``model``, so the kept and rejected keys are those of a run with the tier kill switch on (no model
    sent): the marker and the routing move no verdict. (The payload hash is built from the items and instruction only,
    so it is not pinned here.)"""
    monkeypatch.delenv("NX_OPERATOR_MODEL_TIERING", raising=False)
    routed = _cov(await _run(_recording(), pool_on(), plan=POOL_PLAN)).repair
    monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "0")
    bare = _cov(await _run(_recording(), pool_on(), plan=POOL_PLAN)).repair
    assert routed.pool_keys_kept and (routed.pool_keys_kept, routed.pool_keys_rejected) == (bare.pool_keys_kept, bare.pool_keys_rejected)


@pytest.mark.asyncio
async def test_the_tier_kill_switch_sends_the_pool_filter_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_OPERATOR_MODEL_TIERING", "0")
    d = _recording()
    _cov(await _run(d, pool_on(), plan=POOL_PLAN))
    assert [a for p, a in d.checks if p], "non-vacuity: the filter ran"
    assert not [a for _, a in d.checks if "model" in a or coverage_pool.POOL_FILTER_KEY in a]


@pytest.mark.asyncio
async def test_with_the_switch_off_no_assessment_call_carries_a_model_or_the_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_OPERATOR_MODEL_TIERING", raising=False)
    d = _recording()
    _cov(await _run(d, pool_off(), plan=POOL_PLAN))
    assert d.checks and not [p for p, _ in d.checks if p], "non-vacuity: assessments ran, none of them the pool's"
    assert not [a for _, a in d.checks if "model" in a or coverage_pool.POOL_FILTER_KEY in a]


class _PricedDispatcher(_Recording):
    """Reports a ``DispatchUsage`` on every assessment call, as ``claude_dispatch`` does: the pool filter's calls cost
    100 times the boundary's own, as a stronger model's would."""

    async def __call__(self, tool: str, args: dict) -> dict:
        if tool == "operator_check":
            from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink  # noqa: PLC0415

            pool = any(i["id"].startswith(coverage_pool._POOL_ID_PREFIX) for i in json.loads(args["items"]))
            _ambient_usage_sink.get().append(DispatchUsage(
                model="m", cost_usd=1.0 if pool else 0.01, input_tokens=1, output_tokens=1,
                cache_creation_input_tokens=0, cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1))
        return await super().__call__(tool, args)


@pytest.mark.asyncio
async def test_the_pool_filters_calls_stay_out_of_the_cost_mean_the_boundarys_calls_are_admitted_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mean the boundary admits its own calls on is the check tier's price after the filter ran, while the filter's
    cost is still counted in what the run spent."""
    from nexus.plans.coverage_ledger import CoverageBoundary  # noqa: PLC0415

    seen: list[tuple[bool, float | None, float | None]] = []
    real = CoverageBoundary._assess

    async def spy(self, facets, anchors, view, *a: Any, **kw: Any):  # noqa: ANN001, ANN202
        out = await real(self, facets, anchors, view, *a, **kw)
        pool = any(x.item_id.startswith(coverage_pool._POOL_ID_PREFIX) for x in anchors)
        seen.append((pool, view.assessment_cost_estimate(), view.assessment_cost_spent()))
        return out

    monkeypatch.setattr(CoverageBoundary, "_assess", spy)
    base = _fixture()
    cov = _cov(await _run(_PricedDispatcher(base.anchor_texts, base.pool_texts), pool_on(), plan=POOL_PLAN))
    after_pool = [s for s in seen if s[0]]
    assert after_pool and cov.repair.pool_filter_calls, "non-vacuity: the filter ran and was priced"
    _, estimate, spent = after_pool[-1]
    assert estimate == pytest.approx(0.01), "the filter's 1.0 calls do not move the check-tier mean"
    assert spent is not None and spent >= 1.0, "the filter's cost is still spent"
    assert cov.repair.pool_filter_cost_usd == pytest.approx(1.0 * cov.repair.pool_filter_calls)
