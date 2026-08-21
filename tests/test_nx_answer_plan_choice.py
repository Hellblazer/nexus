# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring test for nx_answer's Step-1 predicted-cost plan choice (RDR-196
Phase 3 Step 1, nexus-nyry9.20).

Code-review round 2 Important finding (T2 nyry9.20-code-review-
round2-2026-08-21, "no wiring test"): every prior assertion drove
``choose_within_band`` directly with hand-built ``Match`` lists
(tests/test_plan_cost_estimate.py) -- none exercised nx_answer's REAL
Step-1 code path with ``plan_match`` mocked to return multiple
above-floor candidates, so the feature's activation in the real flow
(``candidate_count > 1`` ever reached, ``plan_choice``/
``nx_answer_plan_choice`` ever populated, the cost-ranking-failure
fail-safe ever exercised) stayed unproven. This file closes that gap.

Reuses the fake-dispatch / T2-context fixture patterns already
established in tests/test_nx_answer.py (read for reference, not edited --
that file is sibling bead .16's; RDR-100 fence keeps matcher.py itself
untouched by both).
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from nexus.plans.match import Match


def _match(plan_id: int, confidence: float, *tools: str, name: str = "") -> Match:
    plan_json = json.dumps({"steps": [{"tool": t, "args": {}} for t in tools]})
    return Match(
        plan_id=plan_id,
        name=name or f"plan-{plan_id}",
        description="test plan",
        confidence=confidence,
        dimensions={},
        tags="",
        plan_json=plan_json,
        required_bindings=[],
        optional_bindings=[],
        default_bindings={},
        parent_dims=None,
    )


def _fake_t2_ctx(tmp_path):
    """Same shape as test_nx_answer.py's helper of the same name: a real
    T2Database at tmp_path/t2.db, so ``get_cached_price_table`` sees a
    real (empty) telemetry store and falls back to the static per-tier
    cost table rather than needing its own mock."""
    from nexus.db.t2 import T2Database

    @contextmanager
    def _ctx():
        with T2Database(tmp_path / "t2.db") as db:
            yield db

    return _ctx


class TestStep1PlanChoiceWiring:
    """Drives the real ``nx_answer`` Step-1 code path (not
    ``choose_within_band`` in isolation) with ``plan_match`` mocked to
    return two above-floor, different-shape candidates."""

    @pytest.mark.asyncio
    async def test_two_candidates_envelope_and_log_name_the_cheaper_plan(self, tmp_path):
        """(a)+(b): plan_choice envelope carries candidate_count == 2 and
        chosen_plan_id == the cheaper candidate; the nx_answer_plan_choice
        structlog event fires with the matching shape."""
        import nexus.mcp_infra as _infra
        import nexus.plans.cost_estimate as _cost
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        _cost.invalidate_price_table_cache()

        # Same in-band, different-shape pairing as
        # test_plan_cost_estimate.py::test_two_in_band_candidates_different_shapes_cheaper_wins:
        # gap 0.75/0.79 <= PLAN_CHOICE_CONFIDENCE_BAND (0.05); expensive
        # is a priced LLM operator step, cheap is sql-fast-path-eligible
        # ($0) -- the cheaper candidate must win.
        expensive = _match(1, 0.75, "search", "operator_generate", name="expensive")
        cheap = _match(2, 0.79, "search", "operator_filter", name="cheap")
        run_result = PlanResult(steps=[{"text": "The final answer."}])

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[expensive, cheap]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
            capture_logs() as logs,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("what is projection quality?", structured=True)

        plan_choice = result["plan_choice"]
        assert plan_choice is not None, "plan_choice envelope field never populated"
        assert plan_choice["candidate_count"] == 2
        assert plan_choice["chosen_plan_id"] == cheap.plan_id
        assert result["plan_id"] == cheap.plan_id

        choice_events = [e for e in logs if e.get("event") == "nx_answer_plan_choice"]
        assert choice_events, "nx_answer_plan_choice log event never fired"
        assert choice_events[0]["candidate_count"] == 2
        assert choice_events[0]["chosen_plan_id"] == cheap.plan_id

    @pytest.mark.asyncio
    async def test_cost_ranking_failure_falls_back_to_top_confidence_match(self, tmp_path, monkeypatch):
        """(c) fail-safe: choose_within_band raising -> nx_answer proceeds
        with matches[0] (the pre-existing top-confidence choice), never
        crashes the caller, and logs nx_answer_plan_cost_ranking_failed."""
        import nexus.mcp_infra as _infra
        import nexus.plans.cost_estimate as _cost
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        top = _match(1, 0.75, "search", "operator_generate", name="top")
        second = _match(2, 0.79, "search", "operator_filter", name="second")
        run_result = PlanResult(steps=[{"text": "The final answer."}])

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        # nx_answer's Step-1 does a deferred `from nexus.plans.cost_estimate
        # import choose_within_band` INSIDE the function body on every
        # call, so patching the module attribute here is picked up at
        # call time -- same mechanism the module's own deferred imports
        # of plan_match/plan_run rely on for the patch() calls above.
        monkeypatch.setattr(_cost, "choose_within_band", _boom)

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[top, second]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
            capture_logs() as logs,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("what is projection quality?", structured=True)

        assert "final answer" in result["final_text"].lower()  # did not crash
        assert result["plan_id"] == top.plan_id  # degraded to matches[0]
        assert result["plan_choice"] is None  # never populated on the failure path

        failed_events = [e for e in logs if e.get("event") == "nx_answer_plan_cost_ranking_failed"]
        assert failed_events, "nx_answer_plan_cost_ranking_failed was never logged"
