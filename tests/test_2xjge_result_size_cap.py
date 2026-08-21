# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2xjge — explicit server-side capping of assembled MCP text results.

The bead's audit (see the bead notes / T2 write-back) found that none of
this module's paginated text tools clamp a caller-supplied ``limit``, and
that the backing pgvector engine does not either: ``PgVectorRepository
#searchWithTokens``'s own javadoc states the Chroma-era result-size caps
"fall away with pgvector". So a caller-supplied ``limit`` can scale an
assembled text response arbitrarily. House doctrine forbids the harness-
side ``_meta.maxResultSizeChars`` route (SILENT truncation, no marker) --
this module caps explicitly instead, via ``_cap_text_result``, with a
visible trailing marker.

This file pins the pure helper's contract, then ``nx_answer``'s use of it
(the one call site whose structured envelope also carries a
``truncated_chars`` field). The other seven call sites (search, query,
store_list x2, memory_search, plan_search, scratch x2) are integration-
tested in ``tests/test_mcp_server.py`` alongside their existing pagination
tests, reusing that file's real T1/T2/T3 fixtures.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from nexus.mcp.core import _TEXT_RESULT_CAP_CHARS, _cap_text_result


class TestCapTextResult:
    def test_under_cap_returned_unchanged(self) -> None:
        text = "short result"
        assert _cap_text_result(text, "search", cap=1000) == text

    def test_exactly_at_cap_returned_unchanged(self) -> None:
        text = "x" * 500
        assert _cap_text_result(text, "search", cap=500) == text

    def test_over_cap_truncated_with_trailing_marker(self) -> None:
        text = "x" * 600
        out = _cap_text_result(text, "search", cap=500)
        assert out.startswith("x" * 500)
        assert out.endswith(
            "[search: result capped at 500 chars; 100 chars dropped — "
            "narrow the query, lower limit, or page with offset=...]"
        )

    def test_marker_never_leads_the_string(self) -> None:
        # House doctrine: the marker must never be mistaken for the start
        # of the result, and must never disturb a leading-prefix contract
        # elsewhere in this module (e.g. nx_answer's budget-exhaustion
        # marker, which callers key on via str.startswith).
        out = _cap_text_result("y" * 600, "search", cap=500)
        assert not out.startswith("[")
        assert out.startswith("y")

    def test_dropped_count_is_exact(self) -> None:
        out = _cap_text_result("z" * 1234, "query", cap=1000)
        assert "234 chars dropped" in out

    def test_default_cap_is_the_module_constant(self) -> None:
        # No explicit cap passed -- reads _TEXT_RESULT_CAP_CHARS at call
        # time (not bound as a stale default), so the same helper works
        # under production settings AND under a test's monkeypatched cap.
        text = "a" * (_TEXT_RESULT_CAP_CHARS + 10)
        out = _cap_text_result(text, "search")
        assert out.endswith(
            f"[search: result capped at {_TEXT_RESULT_CAP_CHARS} chars; "
            "10 chars dropped — narrow the query, lower limit, or page "
            "with offset=...]"
        )

    def test_monkeypatched_module_cap_takes_effect(self, monkeypatch) -> None:
        from nexus.mcp import core

        monkeypatch.setattr(core, "_TEXT_RESULT_CAP_CHARS", 50)
        out = core._cap_text_result("b" * 100, "store_list")
        assert "50 chars dropped" in out

    def test_tool_name_appears_in_marker(self) -> None:
        out = _cap_text_result("c" * 20, "plan_search", cap=10)
        assert "[plan_search: result capped" in out


# ── nx_answer: defense-in-depth cap on final_text ───────────────────────────


def _match(plan_id: int, confidence: float, *tools: str) -> "object":
    from nexus.plans.match import Match

    plan_json = json.dumps({"steps": [{"tool": t, "args": {}} for t in tools]})
    return Match(
        plan_id=plan_id, name=f"plan-{plan_id}", description="test plan",
        confidence=confidence, dimensions={}, tags="", plan_json=plan_json,
        required_bindings=[], optional_bindings=[], default_bindings={},
        parent_dims=None,
    )


def _fake_t2_ctx(tmp_path):
    from nexus.db.t2 import T2Database

    @contextmanager
    def _ctx():
        with T2Database(tmp_path / "t2.db") as db:
            yield db

    return _ctx


class TestNxAnswerResultCap:
    """Mirrors tests/test_nx_answer_plan_choice.py's scaffold: a single
    above-floor match, plan_run mocked to return an oversized final step
    text, so nx_answer's own _result() closure is driven end-to-end."""

    @pytest.mark.asyncio
    async def test_oversized_final_text_capped_with_marker(self, tmp_path) -> None:
        import nexus.mcp_infra as _infra
        import nexus.plans.cost_estimate as _cost
        import nexus.plans.runner as _runner
        from nexus.mcp.core import nx_answer
        from nexus.plans.runner import PlanResult

        _cost.invalidate_price_table_cache()
        match = _match(1, 0.9, "operator_generate")
        oversized = "q" * (_TEXT_RESULT_CAP_CHARS + 500)
        run_result = PlanResult(steps=[{"text": oversized}])

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            result = await nx_answer("what does this do?", structured=True)

        assert len(result["final_text"]) < len(oversized), "final_text must be capped, not passed through raw"
        assert result["final_text"].endswith(
            "chars dropped — narrow the query, lower limit, or page with offset=...]"
        )
        assert result["truncated_chars"] == 500

    @pytest.mark.asyncio
    async def test_normal_final_text_not_truncated_and_field_is_none(self, tmp_path) -> None:
        import nexus.mcp_infra as _infra
        import nexus.plans.cost_estimate as _cost
        import nexus.plans.runner as _runner
        from nexus.mcp.core import nx_answer
        from nexus.plans.runner import PlanResult

        _cost.invalidate_price_table_cache()
        match = _match(1, 0.9, "operator_generate")
        run_result = PlanResult(steps=[{"text": "The final answer."}])

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            result = await nx_answer("what does this do?", structured=True)

        assert result["final_text"] == "The final answer."
        assert result["truncated_chars"] is None
