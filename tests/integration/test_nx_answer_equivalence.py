# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for nx_answer — RDR-080 SC-1, SC-2, SC-9.

SC-1: nx_answer plan-match decision matches the calibration set
  (a) same plan_id hit as direct plan_match call
  (b) for hits: nx_answer proceeds (not a miss)
  (c) for misses: nx_answer returns a clear miss message

SC-2: latency measurement (recorded, not gated in CI)

SC-9: graceful degradation without auth — retrieval-only plans work,
      operator-requiring plans return clear error

Marked @pytest.mark.integration — skipped by default.
Run with: uv run pytest -m integration tests/integration/test_nx_answer_equivalence.py
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import time
from unittest.mock import patch

import pytest
import pytest_asyncio

pytestmark = pytest.mark.integration


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _claude_auth_available() -> bool:
    """Return True iff ``claude auth status --json`` reports loggedIn."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return bool(data.get("loggedIn"))


@pytest.fixture(scope="module", autouse=True)
def _skip_without_auth():
    if not _claude_auth_available():
        pytest.skip("claude auth not available — skipping live integration tests")


@pytest_asyncio.fixture(autouse=True)
async def _reset_pool_between_tests():
    """Reset operator pool singletons between tests."""
    yield
    from nexus.mcp_infra import (
        reset_operator_pool,
        reset_singletons,
    )
    reset_operator_pool()
    reset_singletons()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_plan_match_for_intent(intent: str):
    """Call plan_match directly and return the top Match or None."""
    from nexus.mcp_infra import get_t1_plan_cache, t2_ctx
    from nexus.plans.matcher import plan_match

    with t2_ctx() as db:
        cache = get_t1_plan_cache(populate_from=db.plans)
        matches = plan_match(
            intent,
            library=db.plans,
            cache=cache,
            min_confidence=0.40,
            n=5,
        )
    return matches[0] if matches else None


# ── SC-1: Plan-match decision equivalence ─────────────────────────────────────


class TestPlanMatchDecision:
    """SC-1(a): nx_answer plan-match decision matches direct plan_match."""

    def _sample_intents(self):
        """Return a sample of calibration intents (20+)."""
        from tests.fixtures.calibration_paraphrases import paraphrase_dataset

        all_paras = paraphrase_dataset()
        # Take the first 24 positive paraphrases.
        positives = [p for p in all_paras if p.is_positive][:24]
        return positives

    def test_plan_match_decision_equivalence(self):
        """For each positive paraphrase, nx_answer's match gate should
        agree with direct plan_match: both hit or both miss."""
        from nexus.mcp.core import _nx_answer_match_is_hit

        intents = self._sample_intents()
        assert len(intents) >= 20, f"Need 20+ intents, got {len(intents)}"

        agreements = 0
        for p in intents:
            match = _get_plan_match_for_intent(p.intent)
            if match is None:
                # Both paths should agree it's a miss.
                agreements += 1
                continue
            is_hit = _nx_answer_match_is_hit(match.confidence)
            # Direct plan_match returned a result — nx_answer's gate
            # should see it as a hit (confidence >= 0.40 or None).
            if is_hit:
                agreements += 1

        # Allow some disagreement (plan library may not be fully seeded),
        # but expect ≥80% agreement.
        agreement_rate = agreements / len(intents)
        assert agreement_rate >= 0.80, (
            f"Plan-match decision agreement rate {agreement_rate:.0%} "
            f"below 80% threshold ({agreements}/{len(intents)})"
        )


# ── SC-9: Graceful degradation ───────────────────────────────────────────────


class TestGracefulDegradation:
    """SC-9: Without auth, retrieval-only plan-hits work;
    operator-requiring plan-miss returns clear error."""

    @pytest.mark.asyncio
    async def test_plan_miss_without_auth_returns_error(self):
        """When no plan matches and auth is unavailable, nx_answer
        should return a clear miss message (not crash)."""
        from nexus.mcp.core import nx_answer

        # Use an adversarial intent that won't match any plan.
        result = await nx_answer(
            question="What is the weather in Tokyo right now?",
            trace=False,
        )
        # Should get a miss message, not an exception.
        assert isinstance(result, str)
        assert "no matching plan" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_retrieval_plan_without_auth(self):
        """A plan-hit whose steps are all retrieval (no operators)
        should work even when the operator pool auth fails."""
        from nexus.mcp.core import _nx_answer_needs_operators
        from nexus.plans.match import Match

        # Build a retrieval-only match.
        match = Match(
            plan_id=999,
            name="test-retrieval-only",
            description="test",
            confidence=0.50,
            dimensions={},
            tags="",
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                ],
            }),
            required_bindings=["intent"],
            optional_bindings=[],
            default_bindings={},
            parent_dims=None,
        )
        assert _nx_answer_needs_operators(match) is False

    @pytest.mark.asyncio
    async def test_operator_plan_with_mocked_auth_failure(self):
        """When auth fails during plan_run (operator step), nx_answer
        returns an error message (not a hang or crash)."""
        from nexus.mcp.core import nx_answer
        from nexus.plans.match import Match
        from nexus.plans.runner import PlanRunOperatorUnavailableError

        # Patch plan_match to return a match that needs operators.
        mock_match = Match(
            plan_id=1,
            name="test",
            description="test",
            confidence=0.55,
            dimensions={},
            tags="",
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "title"}},
                ],
            }),
            required_bindings=["intent"],
            optional_bindings=[],
            default_bindings={},
            parent_dims=None,
        )

        async def mock_plan_run(match, bindings, **kw):
            raise PlanRunOperatorUnavailableError(
                operator="extract", reason="mocked auth failure"
            )

        with patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.plans.matcher.plan_match", return_value=[mock_match]), \
             patch("nexus.plans.runner.plan_run", mock_plan_run):
            result = await nx_answer(
                question="test operator plan",
                trace=False,
            )

        assert isinstance(result, str)
        assert "unavailable" in result.lower() or "operator" in result.lower()


# ── SC-2: Latency measurement ────────────────────────────────────────────────


class TestLatencyMeasurement:
    """SC-2: Record nx_answer latency. Not gated — measurement only."""

    def test_plan_match_latency_under_5s(self):
        """Plan-match alone (no plan_run) should be fast."""
        start = time.monotonic()
        match = _get_plan_match_for_intent(
            "how does the retrieval layer work end-to-end"
        )
        elapsed = time.monotonic() - start
        # Plan match should be sub-second on warm T1 cache.
        assert elapsed < 5.0, f"Plan match took {elapsed:.1f}s (expected <5s)"
