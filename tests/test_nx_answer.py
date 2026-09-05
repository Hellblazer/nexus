# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nx_answer, nx_tidy, nx_enrich_beads, nx_plan_audit — RDR-080.

Tests cover:
  - Plan-match gate logic (hit at 0.40, hit at None/FTS5, miss below 0.40)
  - Single-step guard reroute to query()
  - Plan-miss inline planner via claude_dispatch (no pool)
  - Run recording with trace=true and trace=false
  - T2 migration for nx_answer_runs table
  - nx_tidy / nx_enrich_beads / nx_plan_audit dispatch contracts
"""
from __future__ import annotations

import json
import re
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans.match import Match


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_match(
    *,
    plan_id: int = 1,
    confidence: float | None = 0.55,
    plan_json: str | None = None,
    name: str = "test-plan",
) -> Match:
    if plan_json is None:
        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        })
    return Match(
        plan_id=plan_id,
        name=name,
        description="test plan",
        confidence=confidence,
        dimensions={},
        tags="",
        plan_json=plan_json,
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "test question"},
        parent_dims=None,
    )


def _make_single_step_query_match() -> Match:
    return _make_match(
        plan_json=json.dumps({
            "steps": [
                {"tool": "query", "args": {"question": "$intent", "corpus": "knowledge"}},
            ],
        }),
    )


def _make_multi_step_match() -> Match:
    return _make_match(
        plan_json=json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "title,summary"}},
            ],
        }),
    )


# ── T2 migration tests DELETED (RDR-158 P4 Stage 4, nexus-i711w): the
# migrate_nx_answer_runs chain died with nexus/db/migrations.py; the
# nx_answer_runs table is engine-owned (Liquibase). ─────────────────────────

class TestPlanMatchGate:

    def test_hit_at_threshold(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        assert _nx_answer_match_is_hit(0.40) is True

    def test_hit_above_threshold(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        assert _nx_answer_match_is_hit(0.85) is True

    def test_hit_at_none_fts5_sentinel(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        assert _nx_answer_match_is_hit(None) is True

    def test_miss_below_threshold(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        assert _nx_answer_match_is_hit(0.30) is False

    def test_miss_at_zero(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        assert _nx_answer_match_is_hit(0.0) is False


# ── min_confidence override (RDR-092 Phase 2, Option A) ──────────────────────


class TestMinConfidenceOverride:
    """Per-call ``min_confidence`` override on ``nx_answer`` / the hit
    helper. RDR-092 Phase 2 Option A: the global
    ``_PLAN_MATCH_MIN_CONFIDENCE`` stays at 0.40 (RDR-079 calibration);
    verb skills that validated a stricter floor (0.50 per R9) opt in
    by passing it explicitly.
    """

    def test_hit_helper_accepts_threshold_arg(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        # Default threshold unchanged (0.40).
        assert _nx_answer_match_is_hit(0.40) is True
        assert _nx_answer_match_is_hit(0.45) is True
        # Caller can pin 0.50.
        assert _nx_answer_match_is_hit(0.45, threshold=0.50) is False
        assert _nx_answer_match_is_hit(0.50, threshold=0.50) is True

    def test_fts5_sentinel_ignores_threshold(self):
        from nexus.mcp.core import _nx_answer_match_is_hit
        # ``None`` sentinel is a hit at any threshold (RF-11).
        assert _nx_answer_match_is_hit(None, threshold=0.99) is True

    @pytest.mark.asyncio
    async def test_nx_answer_accepts_min_confidence_kwarg(self):
        """Override flows into plan_match *and* governs the hit check."""
        from nexus.mcp.core import nx_answer

        captured: dict = {}

        def fake_match(question, **kwargs):
            captured.update(kwargs)
            # Return a confidence just above 0.40 but below the caller's
            # override — the hit check must reject it and the planner
            # miss path must kick in.
            return [_make_match(plan_id=1, confidence=0.45)]

        async def fake_miss(question, scope="", max_steps=6):
            return _make_match(plan_id=0, confidence=None)

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=fake_miss)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q", min_confidence=0.50)

        # plan_match saw the caller-supplied floor.
        assert captured.get("min_confidence") == 0.50

    @pytest.mark.asyncio
    async def test_nx_answer_default_threshold_unchanged(self):
        """With no override, the 0.40 floor still matches RDR-079."""
        from nexus.mcp.core import nx_answer, _PLAN_MATCH_MIN_CONFIDENCE

        assert _PLAN_MATCH_MIN_CONFIDENCE == 0.40

        captured: dict = {}

        def fake_match(question, **kwargs):
            captured.update(kwargs)
            return []

        async def fake_miss(question, scope="", max_steps=6):
            return _make_match(plan_id=0, confidence=None)

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=fake_miss)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q")

        assert captured.get("min_confidence") == 0.40

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_value", [-0.01, -1.0, 1.01, 2.0])
    async def test_nx_answer_rejects_out_of_range_min_confidence(
        self, bad_value: float,
    ):
        """RDR-092 code-review S-4: values outside [0, 1] must fail
        loudly rather than silently admitting (negative) or rejecting
        (> 1.0) every match.
        """
        from nexus.mcp.core import nx_answer

        match_called = MagicMock()

        def fake_match(question, **kwargs):
            match_called()
            return []

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = MagicMock()
            result = await nx_answer(
                question="q", min_confidence=bad_value,
            )

        assert "min_confidence must be in [0.0, 1.0]" in result
        assert str(bad_value) in result
        assert not match_called.called, (
            "plan_match must never be reached with an invalid floor"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ok_value", [0.0, 0.25, 0.5, 1.0])
    async def test_nx_answer_accepts_boundary_min_confidence(
        self, ok_value: float,
    ):
        """The validator accepts both endpoints (0.0 and 1.0) plus
        anything in between so verb skills can pin the most permissive
        and most restrictive floors without hitting the guard.
        """
        from nexus.mcp.core import nx_answer

        captured: dict = {}

        def fake_match(question, **kwargs):
            captured.update(kwargs)
            return []

        async def fake_miss(question, scope="", max_steps=6):
            return _make_match(plan_id=0, confidence=None)

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=fake_miss)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="q", min_confidence=ok_value)

        assert captured.get("min_confidence") == ok_value


# ── Single-step guard ─────────────────────────────────────────────────────────


class TestSingleStepGuard:

    def test_single_query_step_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query
        assert _nx_answer_is_single_query(_make_single_step_query_match()) is True

    def test_multi_step_not_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query
        assert _nx_answer_is_single_query(_make_multi_step_match()) is False

    def test_single_non_query_step_not_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query
        match = _make_match(
            plan_json=json.dumps({
                "steps": [{"tool": "search", "args": {"query": "$intent"}}],
            }),
        )
        assert _nx_answer_is_single_query(match) is False


# ── Shared step-binding resolution helper (review-fix) ─────────────────────────


class TestSharedStepBindingResolutionHelper:
    """nexus-nyry9.5 review-fix (code-review SIGNIFICANT, T2
    nyry9.5-code-review-2026-08-20): core.py's single_query fast path
    and runner.py's plan_run must resolve step bindings through the
    SAME shared precedence formula (``nexus.plans.runner.merge_bindings``)
    rather than two independently hand-maintained copies with no test
    cross-checking they stayed in sync."""

    @pytest.mark.asyncio
    async def test_single_query_fast_path_calls_shared_merge_bindings(self, tmp_path):
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner

        match = _make_single_step_query_match()

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query", return_value="ok"),
            patch.object(_runner, "merge_bindings", wraps=_runner.merge_bindings) as spy,
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q")

        assert spy.called, (
            "single_query fast path must resolve bindings via the "
            "shared merge_bindings helper, not a hand-replicated copy"
        )

    @pytest.mark.asyncio
    async def test_single_query_fast_path_uses_plan_step_default_corpus(self, tmp_path):
        """nexus-rl59s (code review [24061] Critical): the single-step
        reroute bypasses plan_run, so it must apply the runner's
        fall-through corpus itself. A corpus-agnostic single_query plan
        must NOT land on query()'s bare "knowledge" default (which omits
        rdr__ -- the Phase 1b degenerate class A listings)."""
        import nexus.mcp_infra as _infra
        from nexus.plans.runner import _PLAN_STEP_DEFAULT_CORPUS

        match = _make_match(
            plan_json=json.dumps({
                "steps": [{"tool": "query", "args": {"question": "$intent"}}],
            }),
        )
        assert "corpus" not in json.loads(match.plan_json)["steps"][0]["args"]

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query", return_value="ok") as q,
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q")

        assert q.called
        assert q.call_args.kwargs.get("corpus") == _PLAN_STEP_DEFAULT_CORPUS, q.call_args

    @pytest.mark.asyncio
    async def test_plan_run_calls_shared_merge_bindings(self):
        import nexus.plans.runner as _runner
        from nexus.plans.runner import plan_run

        match = _make_match(
            plan_json=json.dumps({
                "steps": [{"tool": "search", "args": {"query": "$intent"}}],
            }),
        )

        async def fake_dispatch(tool, args):
            return {"text": "ok", "ids": []}

        with patch.object(_runner, "merge_bindings", wraps=_runner.merge_bindings) as spy:
            await plan_run(match, {"intent": "q"}, dispatcher=fake_dispatch)

        assert spy.called, (
            "plan_run must resolve bindings via the shared "
            "merge_bindings helper"
        )


# ── Single-query typed-binding refusal (review-fix) ─────────────────────────────


class TestSingleQueryPlanBindingUnsatisfiable:
    """nexus-nyry9.5 review-fix (code-review IMPORTANT #1, T2
    nyry9.5-code-review-2026-08-20): a typed required binding
    (``TYPED_FILTER_BINDINGS`` -- e.g. ``content_type``) the single_query
    fast path cannot derive from the question text must be handled the
    same explicit way Step 4 handles it: step_count=0, a logged
    ``nx_answer_plan_binding_unsatisfiable`` event, NOT the generic
    ``except Exception`` (which used to record step_count=1 with no
    telemetry event, wrongly implying one step actually ran)."""

    @pytest.mark.asyncio
    async def test_typed_binding_unsatisfiable_records_step_count_zero(self, tmp_path):
        import nexus.mcp_infra as _infra
        from nexus.plans.match import Match

        match = Match(
            plan_id=1,
            name="test-plan-typed-binding",
            description="test",
            confidence=0.75,
            dimensions={},
            tags="",
            plan_json=json.dumps({
                "steps": [{
                    "tool": "query",
                    "args": {"question": "$intent", "content_type": "$content_type"},
                }],
            }),
            required_bindings=["content_type"],
            optional_bindings=[],
            default_bindings={},
            parent_dims=None,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core._nx_answer_record_run") as record_run_spy,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q")

        assert "content_type" in result, (
            f"expected the typed-binding refusal message naming the "
            f"unsatisfiable binding, got: {result!r}"
        )
        assert record_run_spy.called, "the refusal must still be recorded"
        assert record_run_spy.call_args.kwargs.get("step_count") == 0, (
            "single_query's typed-binding refusal must record step_count=0 "
            "(zero steps ran), matching Step 4's dedicated handling -- not "
            "step_count=1 from the generic except Exception fallthrough"
        )


# ── Single-query unresolved $var (nexus-pucte, undefended-sibling-path fix) ─────


class TestSingleQueryUnresolvedVar:
    """nexus-pucte review-fix (critic Significant #3): the single_query
    fast path bypasses plan_run (and its pre-dispatch _validate_var_refs
    check) entirely by design -- resolve_step_bindings now runs the
    identical check itself, so a $var with no default and no
    caller-supplied value (and NOT covered by _autoalias_bindings, which
    only fills names in required_bindings) is refused loudly instead of
    reaching query() as the literal token string -- exactly the
    nexus-nyry9.5 bug class, on its second entry point.

    Uses ``question`` (review round 2, critic cosmetic item): the fast
    path's ``query()`` call only ever consumes ``question``/``corpus``/
    ``limit`` from ``step_args`` (core.py's Step 2 body) -- ``question``
    is the field a real exploit of this bug would ride, matching the
    ORIGINAL nexus-nyry9.5 failure mode exactly (a literal ``$question``
    reaching ``query()`` as a garbage filter value, returning "No
    results." with nothing surfaced). An earlier draft of this test used
    an arbitrary ``topic`` field the fast path never reads at all, which
    proved the validation MECHANISM fires but not a concretely reachable
    scenario for this tool."""

    @pytest.mark.asyncio
    async def test_unaliased_question_refused_not_queried_as_literal(self, tmp_path):
        import nexus.mcp_infra as _infra
        from nexus.plans.match import Match

        match = Match(
            plan_id=1,
            name="test-plan-unaliased-question",
            description="test",
            confidence=0.75,
            dimensions={},
            tags="",
            plan_json=json.dumps({
                "steps": [{
                    "tool": "query",
                    # "question" is neither in required_bindings (so
                    # _autoalias_bindings never touches it) nor in
                    # default_bindings -- nothing supplies it.
                    "args": {"question": "$question", "corpus": "knowledge"},
                }],
            }),
            required_bindings=[],
            optional_bindings=[],
            default_bindings={},
            parent_dims=None,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query") as query_spy,
            patch("nexus.mcp.core._nx_answer_record_run") as record_run_spy,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q")

        assert "question" in result, (
            f"expected the unresolved-var refusal naming the var, got: {result!r}"
        )
        assert not query_spy.called, (
            "query() must never be called with the unresolved var reaching "
            "it as the literal token '$question' -- the whole point of the "
            "fix (and the exact nexus-nyry9.5 failure mode this closes on "
            "the fast path's second entry point)"
        )
        assert record_run_spy.called, "the refusal must still be recorded"
        assert record_run_spy.call_args.kwargs.get("step_count") == 0, (
            "zero steps ran -- matches the sibling "
            "PlanBindingUnsatisfiableError handling's step_count=0 convention"
        )


# ── Single-query limit clamp (review-fix) ───────────────────────────────────────


class TestSingleQueryLimitClamp:
    """nexus-nyry9.5 review-fix (code-review IMPORTANT #2, T2
    nyry9.5-code-review-2026-08-20): a plan- or caller-influenced
    ``limit`` reaching the single_query fast path's ``query()`` call
    must be clamped to ``QUOTAS.MAX_QUERY_RESULTS`` like every other
    paging/query-result path in this codebase (AGENTS.md § External
    service limits) -- this path skipping ``plan_run`` must not also
    mean it skips the ceiling."""

    @pytest.mark.asyncio
    async def test_plan_limit_over_ceiling_clamped_to_max_query_results(self, tmp_path):
        import nexus.mcp_infra as _infra
        from nexus.db.limits import MAX_QUERY_RESULTS

        match = _make_match(
            plan_json=json.dumps({
                "steps": [{
                    "tool": "query",
                    "args": {"question": "$intent", "corpus": "knowledge", "limit": 1000},
                }],
            }),
        )
        query_calls: list[dict] = []

        def fake_query(**kwargs):
            query_calls.append(kwargs)
            return "ok"

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query", side_effect=fake_query),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q")

        assert query_calls, "query() was never called"
        assert query_calls[0].get("limit") == MAX_QUERY_RESULTS == 300, (
            f"a plan limit of 1000 must clamp to MAX_QUERY_RESULTS "
            f"(300), got {query_calls[0].get('limit')!r}"
        )

    @pytest.mark.asyncio
    async def test_missing_plan_limit_uses_querys_own_default(self, tmp_path):
        import inspect
        import nexus.mcp_infra as _infra
        from nexus.mcp.core import query as _query_tool

        default_limit = inspect.signature(_query_tool).parameters["limit"].default
        assert default_limit == 10  # sanity: query()'s own documented default

        match = _make_single_step_query_match()  # no "limit" key in args
        query_calls: list[dict] = []

        def fake_query(**kwargs):
            query_calls.append(kwargs)
            return "ok"

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query", side_effect=fake_query),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q")

        assert query_calls, "query() was never called"
        assert query_calls[0].get("limit") == default_limit == 10, (
            f"a plan with no 'limit' arg must fall back to query()'s own "
            f"default (10), got {query_calls[0].get('limit')!r}"
        )


# ── Graceful degradation (SC-9) ───────────────────────────────────────────────


class TestGracefulDegradation:

    def test_retrieval_only_plan_does_not_need_operators(self):
        from nexus.mcp.core import _nx_answer_needs_operators
        match = _make_match(
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "query", "args": {"question": "$intent"}},
                ],
            }),
        )
        assert _nx_answer_needs_operators(match) is False

    def test_operator_plan_needs_operators(self):
        from nexus.mcp.core import _nx_answer_needs_operators
        assert _nx_answer_needs_operators(_make_multi_step_match()) is True


# ── Run recording ─────────────────────────────────────────────────────────────


class TestRunRecording:

    @staticmethod
    def _record_and_capture(db, **kwargs) -> dict:
        """Drive ``_nx_answer_record_run`` against the REAL telemetry store on
        either substrate, spying on the store-call kwargs (post-redaction) and
        asserting the write was not silently dropped.

        ``_nx_answer_record_run`` swallows store errors via
        ``_warn_telemetry_drop`` (best-effort telemetry), so
        ``warn.assert_not_called()`` is the backend-blind proof that the row
        landed (SQLite INSERT committed / service POST returned 2xx). Raw-row
        content assertions died with the raw SQLite store — the Http store
        exposes no row-level read surface for nx_answer_runs."""
        from nexus.mcp import core as _core

        telemetry = db.telemetry
        seen: dict = {}
        real = telemetry.record_nx_answer_run

        def _spy(**call_kwargs):
            seen.update(call_kwargs)
            return real(**call_kwargs)

        with patch.object(telemetry, "record_nx_answer_run", side_effect=_spy), \
             patch.object(_core, "_warn_telemetry_drop") as warn:
            _core._nx_answer_record_run(telemetry, **kwargs)
        warn.assert_not_called()
        return seen

    def test_record_run_trace_true(self, tmp_path):
        from nexus.db.t2 import T2Database
        from nexus.plans.runner import StepRecord

        with T2Database(tmp_path / "mem.db") as db:
            seen = self._record_and_capture(
                db, question="test question", plan_id=1,
                matched_confidence=0.55, step_count=3,
                final_text="the answer",
                step_records=[StepRecord(step_index=0, operator="op", source="llm", cost_usd=0.04)],
                duration_ms=1500, trace=True,
            )
            assert seen["question"] == "test question"
            assert seen["final_text"] == "the answer"

    def test_record_run_trace_false_redacts(self, tmp_path):
        from nexus.db.t2 import T2Database
        from nexus.plans.runner import StepRecord

        with T2Database(tmp_path / "mem.db") as db:
            seen = self._record_and_capture(
                db, question="private question", plan_id=2,
                matched_confidence=None, step_count=2,
                final_text="sensitive answer",
                step_records=[StepRecord(step_index=0, operator="op", source="llm", cost_usd=0.02)],
                duration_ms=800, trace=False,
            )
            # Redaction happens caller-side, BEFORE the store boundary —
            # identical on both substrates.
            assert seen["question"] == "[redacted]"
            assert seen["final_text"] == "[redacted]"

    def test_record_run_lands_via_t2_telemetry(self, tmp_path):
        """Regression for nexus-598n + nexus-pyzk7: the MCP call sites write
        through the telemetry *store* (``db.telemetry``), not a raw ``.conn``.

        nexus-598n: after the RDR-063 T2Database split removed the facade-level
        ``.conn``, the old ``db.conn`` writes raised ``AttributeError`` under
        ``except Exception: pass`` — silently dropping every run record.
        nexus-pyzk7: passing a raw ``.conn`` broke again in service mode (an
        ``Http*Store`` has none). Routing through ``db.telemetry`` (which owns
        the INSERT and dispatches SQLite-raw vs service-POST) locks the
        backend-blind contract — asserted here on BOTH substrates via the
        no-silent-drop spy in ``_record_and_capture``.
        """
        from nexus.db.t2 import T2Database

        path = tmp_path / "mem.db"
        with T2Database(path) as db:
            assert hasattr(db, "telemetry")
            seen = self._record_and_capture(
                db, question="integration-probe",
                plan_id=7, matched_confidence=0.8, step_count=2,
                final_text="ok", step_records=[], duration_ms=42,
                trace=True,
            )
            assert seen["question"] == "integration-probe"
            assert seen["plan_id"] == 7
            assert seen["step_count"] == 2

    def test_step_write_failure_does_not_fail_the_answer(self):
        """RDR-196 .p1d DO 5: a telemetry write failure — including one
        raised from INSIDE the ``steps`` write-through — must not
        propagate out of ``_nx_answer_record_run``. Pre-existing
        ``_warn_telemetry_drop`` contract (nexus-pyzk7), pinned here
        specifically for the new ``step_records`` path so a bug in
        ``_step_record_to_wire`` (e.g. a StepRecord field typo) cannot
        turn a best-effort telemetry write into a caller-visible crash."""
        from nexus.mcp import core as _core
        from nexus.plans.runner import StepRecord

        telemetry = MagicMock()
        telemetry.record_nx_answer_run = MagicMock(
            side_effect=RuntimeError("simulated wire failure mid steps[] write")
        )

        with patch.object(_core, "_warn_telemetry_drop") as warn:
            # Must not raise.
            _core._nx_answer_record_run(
                telemetry, question="q", plan_id=1, matched_confidence=0.8,
                step_count=1, final_text="answer",
                step_records=[StepRecord(step_index=0, operator="op", source="llm", cost_usd=0.01)],
                duration_ms=100, trace=True,
            )
        warn.assert_called_once()
        assert warn.call_args.args[0] == "nx_answer_runs"


# ── Zero-evidence fallback provenance (nexus-ivv4d) ────────────────────────


class TestZeroEvidenceFallbackProvenance:
    """nexus-ivv4d: the zero-evidence fallback ("No matching evidence
    found ... retrieval steps returned zero results") is the ONE outcome
    where the matched plan's own parameters (plan_id, each retrieval
    step's tool/corpus/query) ARE the finding — RDR-200 Phase 1c found it
    recorded none of them, unlike every other degenerate shape which
    leaks at least step vocabulary or collections. Both the returned
    text and the telemetry row's ``final_text`` share the same string
    (``no_match`` in the guard), so asserting on the returned text also
    covers the telemetry row.
    """

    @pytest.mark.asyncio
    async def test_zero_evidence_message_names_plan_id_and_step_basis(self):
        from nexus.mcp.core import nx_answer

        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "widget frobnication", "corpus": "rdr"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        })

        def fake_match(question, **kwargs):
            return [_make_match(plan_id=42, confidence=0.60, plan_json=plan_json)]

        plan_run_result = MagicMock()
        plan_run_result.steps = [
            {"ids": [], "collections": [], "distances": []},
            {"text": "nothing to summarize"},
        ]
        plan_run_result.step_records = []

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="what is widget frobnication")

        assert "No matching evidence found" in result
        assert "plan_id=42" in result, result
        assert "search" in result and "rdr" in result and "widget frobnication" in result

    @pytest.mark.asyncio
    async def test_zero_evidence_provenance_lands_in_telemetry_row(self):
        """The recorded ``final_text`` for the telemetry row must carry the
        same provenance as the returned text — not just the user-facing
        string."""
        from nexus.mcp.core import nx_answer

        two_step_plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "distributed consensus", "corpus": "docs"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        })

        def fake_match_multi(question, **kwargs):
            return [_make_match(plan_id=99, confidence=0.60, plan_json=two_step_plan_json)]

        plan_run_result = MagicMock()
        plan_run_result.steps = [
            {"ids": [], "collections": []},
            {"text": "nothing"},
        ]
        plan_run_result.step_records = []

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        seen: dict = {}

        def _spy(*args, **kwargs):
            seen.update(kwargs)

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match_multi), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.mcp.core._nx_answer_record_run", side_effect=_spy):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="tell me about distributed consensus")

        assert "99" in seen.get("final_text", "")
        assert "distributed consensus" in seen.get("final_text", "")
        assert "docs" in seen.get("final_text", "")

    @pytest.mark.asyncio
    async def test_unset_template_corpus_renders_the_runtime_resolved_value(
        self,
    ):
        """Code review follow-up (T2 [24198]): a plan template that
        leaves ``corpus`` unset does not mean the step searched nothing
        -- the runner fills it in at dispatch time (plan scope -> caller
        scope -> ``_PLAN_STEP_DEFAULT_CORPUS``). The static template
        alone renders ``corpus=''`` for exactly the corpus-scoping-
        failure class this diagnostic exists to expose; the fallback
        message must instead show what the runner's
        ``PlanResult.resolved_step_args`` says actually ran."""
        from nexus.mcp.core import nx_answer

        # Template deliberately leaves corpus unset on the search step.
        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "widget frobnication"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        })

        def fake_match(question, **kwargs):
            return [_make_match(plan_id=55, confidence=0.60, plan_json=plan_json)]

        plan_run_result = MagicMock()
        plan_run_result.steps = [
            {"ids": [], "collections": [], "distances": []},
            {"text": "nothing to summarize"},
        ]
        plan_run_result.step_records = []
        # The runner's own resolved-at-dispatch corpus -- what the
        # step-shape fall-through actually filled in.
        plan_run_result.resolved_step_args = [
            {"step_index": 0, "tool": "search",
             "corpus": "knowledge,code,docs,rdr", "query": "widget frobnication"},
        ]

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="what is widget frobnication")

        assert "plan_id=55" in result, result
        assert "knowledge,code,docs,rdr" in result, result
        # The template's own empty corpus must NOT appear as corpus=''.
        assert "corpus=''" not in result, result

    @pytest.mark.asyncio
    async def test_missing_resolved_step_args_falls_back_to_static_template(
        self,
    ):
        """A test double / older PlanResult with no resolved_step_args
        attribute at all (a bare, unconfigured MagicMock) must fall back
        to the static-template renderer cleanly, never raise."""
        from nexus.mcp.core import nx_answer

        plan_json = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "widget frobnication", "corpus": "rdr"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        })

        def fake_match(question, **kwargs):
            return [_make_match(plan_id=42, confidence=0.60, plan_json=plan_json)]

        plan_run_result = MagicMock()
        plan_run_result.steps = [
            {"ids": [], "collections": [], "distances": []},
            {"text": "nothing to summarize"},
        ]
        plan_run_result.step_records = []
        # NOT set: plan_run_result.resolved_step_args stays an
        # unconfigured MagicMock attribute (not a list).

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="what is widget frobnication")

        assert "plan_id=42" in result, result
        assert "rdr" in result and "widget frobnication" in result


# ── Plan-run use_count / success_count / failure_count telemetry ──────────────


class TestPlanRunTelemetry:
    """nexus-use1: plan execution bumps use_count + success/failure counts.

    The counters live on ``plans`` rows; prior to this fix, nothing in the
    codebase called ``increment_run_started`` or ``increment_run_outcome``,
    so every plan in production had ``use_count == 0`` regardless of how
    many times nx_answer actually invoked it.
    """

    def test_record_outcome_noop_for_plan_id_zero(self):
        """Synthetic inline-planner Match (plan_id=0) must not touch T2."""
        from nexus.mcp.core import _nx_answer_record_outcome
        # No _t2_ctx patch — a real T2 call would fail loudly if reached.
        _nx_answer_record_outcome(0, success=True)
        _nx_answer_record_outcome(0, success=False)

    def test_record_outcome_bumps_success_for_library_plan(self):
        """plan_id > 0 increments success_count on the real library row.

        nexus-m20mf P2: ``_nx_answer_record_outcome`` now routes its
        ``db.plans.increment_run_outcome`` call through
        ``_t2_index_write`` (the shared T2 singleton), not ``_t2_ctx``.
        """
        from nexus.mcp import core as _core

        library = MagicMock()
        db_stub = MagicMock(plans=library)

        with patch.object(_core, "_t2_index_write", lambda fn, **_kw: fn(db_stub)):
            _core._nx_answer_record_outcome(42, success=True)

        library.increment_run_outcome.assert_called_once_with(42, success=True)

    def test_record_outcome_bumps_failure_for_library_plan(self):
        """plan_id > 0 increments failure_count on the real library row.

        nexus-m20mf P2: same routing note as the success case above.
        """
        from nexus.mcp import core as _core

        library = MagicMock()
        db_stub = MagicMock(plans=library)

        with patch.object(_core, "_t2_index_write", lambda fn, **_kw: fn(db_stub)):
            _core._nx_answer_record_outcome(42, success=False)

        library.increment_run_outcome.assert_called_once_with(42, success=False)

    def test_record_outcome_swallows_library_errors(self):
        """Telemetry must never break the user-facing flow.

        nexus-m20mf P2: routed through ``_t2_index_write`` — see the
        success/failure tests above for why ``_t2_ctx`` no longer
        reaches this call.
        """
        from nexus.mcp import core as _core

        library = MagicMock()
        library.increment_run_outcome.side_effect = RuntimeError("db gone")
        db_stub = MagicMock(plans=library)

        with patch.object(_core, "_t2_index_write", lambda fn, **_kw: fn(db_stub)):
            _core._nx_answer_record_outcome(42, success=True)  # no raise

    @pytest.mark.asyncio
    async def test_nx_answer_bumps_use_count_and_success_for_library_plan(self):
        """End-to-end integration: a library-matched plan that runs cleanly
        must call ``increment_run_started`` once (use_count++ + last_used
        timestamp) AND ``increment_run_outcome`` with success=True once.

        This is the critical regression guard the substantive-critic flagged
        — a refactor that moves ``increment_run_started`` under the wrong
        conditional (e.g. inside the plan_id==0 branch) would pass every
        other test while zeroing out use_count again.
        """
        from nexus.mcp.core import nx_answer

        # Match returns a library plan (plan_id=1, confidence above 0.40).
        def fake_match(question, **kwargs):
            return [_make_match(plan_id=1, confidence=0.55)]

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="does this wire telemetry?")

        library.increment_run_started.assert_called_once_with(1)
        library.increment_run_outcome.assert_called_once_with(1, success=True)

    @pytest.mark.asyncio
    async def test_nx_answer_skips_telemetry_for_synthetic_match(self):
        """Inline-planner fallthrough produces a synthetic Match with
        ``plan_id=0``. Library increments MUST NOT fire — there's no
        library row to update."""
        from nexus.mcp.core import nx_answer

        # Match returns empty → triggers inline planner → synthetic match.
        def fake_match(question, **kwargs):
            return []

        async def fake_miss(question, scope="", max_steps=6):
            return _make_match(plan_id=0, confidence=None)

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._nx_answer_plan_miss",
                   AsyncMock(side_effect=fake_miss)), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(return_value=plan_run_result)), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="something the library can't match")

        # nexus-m20mf P2 round 2 (code review [24602]): patching
        # _t2_index_write to the SAME db_stub means these two asserts are
        # now proven by the plan_id==0 guard itself, not merely left
        # unfalsifiable by an unpatched real singleton -- a future
        # weakening of that guard (e.g. run_start moved outside the
        # `if best.plan_id:` check) would make library.increment_run_
        # started fire against THIS mock and fail the test, instead of
        # silently landing on the real substrate and passing regardless.
        library.increment_run_started.assert_not_called()
        library.increment_run_outcome.assert_not_called()

    @pytest.mark.asyncio
    async def test_nx_answer_records_failure_outcome_on_plan_run_exception(self):
        """plan_run raising mid-execution must still fire ``increment_run_outcome``
        with ``success=False`` — otherwise failure telemetry is lost and
        success_count/failure_count drift from reality over time."""
        from nexus.mcp.core import nx_answer

        def fake_match(question, **kwargs):
            return [_make_match(plan_id=7, confidence=0.60)]

        library = MagicMock()
        db_stub = MagicMock(plans=library)
        db_stub.conn = MagicMock()

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.plans.runner.plan_run",
                   AsyncMock(side_effect=RuntimeError("exec blew up"))), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="will fail")

        # use_count bumped (the plan was STARTED, even if it didn't finish).
        library.increment_run_started.assert_called_once_with(7)
        # Failure recorded.
        library.increment_run_outcome.assert_called_once_with(7, success=False)
        # User-facing flow still returns an error string, not a raise.
        assert "Error" in str(result)


class TestPlanLibraryMetrics:
    """Direct tests for the library-level metric increments.

    These methods existed before nexus-use1 but had zero callers. We now
    pin their behavior so the wiring cannot regress silently.
    """

    def _fresh_library(self):
        """Return a fresh plan library + seed one plan.

        Ported (nexus-i711w Stage 2 sub-stage A3): the SQLite PlanLibrary is
        deleted; HttpPlanLibrary on the suite's hermetic engine substrate
        (autouse ``_pin_t2_substrate``, per-test tenant) is the only plan
        library — the fresh tenant makes it a fresh library.
        """
        from nexus.db.t2.http_plan_library import HttpPlanLibrary

        lib = HttpPlanLibrary()
        plan_id = lib.save_plan(
            query="anchor probe",
            plan_json=json.dumps({"steps": []}),
            tags="test",
            verb="query",
        )
        return lib, plan_id

    def test_increment_run_started_bumps_use_and_stamps_last_used(self):
        lib, plan_id = self._fresh_library()
        before = lib.get_plan(plan_id)
        assert before["use_count"] == 0
        assert before["last_used"] in (None, "")

        lib.increment_run_started(plan_id)

        after = lib.get_plan(plan_id)
        assert after["use_count"] == 1
        assert after["last_used"]  # ISO timestamp, non-empty

    def test_increment_run_outcome_success(self):
        lib, plan_id = self._fresh_library()
        lib.increment_run_outcome(plan_id, success=True)
        row = lib.get_plan(plan_id)
        assert row["success_count"] == 1
        assert row["failure_count"] == 0

    def test_increment_run_outcome_failure(self):
        lib, plan_id = self._fresh_library()
        lib.increment_run_outcome(plan_id, success=False)
        row = lib.get_plan(plan_id)
        assert row["success_count"] == 0
        assert row["failure_count"] == 1


# ── Plan-miss planner (uses claude_dispatch, no pool) ─────────────────────────


class TestPlanMissPlanner:

    @pytest.mark.asyncio
    async def test_plan_miss_dispatches_via_claude_dispatch(self):
        """_nx_answer_plan_miss uses claude_dispatch (no pool)."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        fake_plan = {
            "steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        }

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return fake_plan

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            match = await _nx_answer_plan_miss("how does X work")

        assert match.name == "ad-hoc"
        plan = json.loads(match.plan_json)
        assert len(plan["steps"]) == 2
        assert plan["steps"][0]["tool"] == "search"
        assert match.default_bindings["intent"] == "how does X work"

    @pytest.mark.asyncio
    async def test_plan_miss_empty_plan_raises(self):
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return {"steps": []}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            with pytest.raises(ValueError, match="empty plan"):
                await _nx_answer_plan_miss("test")

    @pytest.mark.asyncio
    async def test_plan_miss_drops_non_dispatchable_tools(self):
        """All-undispatchable steps → ValueError after normalization."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return {"steps": [
                {"tool": "mcp__plugin_sn_serena__jet_brains_find_symbol", "args": {}},
            ]}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            # Search review I-5: the message surfaces the dropped tool
            # names so callers can report "why" instead of a generic
            # "planner failed". Match the new message shape.
            with pytest.raises(ValueError, match="non-dispatchable tools"):
                await _nx_answer_plan_miss("test")

    @pytest.mark.asyncio
    async def test_plan_miss_aliases_common_tools(self):
        """grep/read/bash/find/glob → search."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return {"steps": [
                {"tool": "Grep", "args": {}},
                {"tool": "Read", "args": {}},
                {"tool": "Bash", "args": {}},
            ]}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            match = await _nx_answer_plan_miss("test")

        plan = json.loads(match.plan_json)
        assert all(s["tool"] == "search" for s in plan["steps"])

    @pytest.mark.asyncio
    async def test_plan_miss_normalizes_mcp_prefix(self):
        """mcp__plugin_conexus_nexus__search → search."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return {"steps": [
                {"tool": "mcp__plugin_conexus_nexus__search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ]}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            match = await _nx_answer_plan_miss("how does X work")

        plan = json.loads(match.plan_json)
        assert plan["steps"][0]["tool"] == "search"

    @pytest.mark.asyncio
    async def test_plan_miss_drops_invalid_keeps_valid(self):
        """Mixed plan: valid steps kept, unmappable dropped."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            return {"steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "totally_unknown_tool", "args": {}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ]}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            match = await _nx_answer_plan_miss("test")

        plan = json.loads(match.plan_json)
        tools = [s["tool"] for s in plan["steps"]]
        assert tools == ["search", "summarize"]

    @pytest.mark.asyncio
    async def test_plan_miss_does_not_call_pool(self):
        """_nx_answer_plan_miss must use claude_dispatch, not get_operator_pool.

        The pool was retired. We verify by patching claude_dispatch and
        ensuring the planner calls it — no pool involvement.
        """
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        dispatch_calls = []

        async def fake_dispatch(prompt, schema, timeout=60.0, model=None, **kw):
            dispatch_calls.append(prompt)
            return {"steps": [{"tool": "search", "args": {"query": "$intent"}}]}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            await _nx_answer_plan_miss("test")

        assert dispatch_calls, "claude_dispatch must be called (not pool)"
        assert "nexus.mcp_infra" not in str(dispatch_calls), "pool path must not be taken"


# ── Grown plan dimensional columns (RDR-092 Phase 0b) ─────────────────────────


def _ad_hoc_match_for_grow(plan_json_steps: list[dict]) -> Match:
    """Build an ad-hoc Match whose plan_json has the given steps shape."""
    return Match(
        plan_id=0,
        name="ad-hoc",
        description="what is the meaning of life",
        confidence=None,
        dimensions={},
        tags="ad-hoc",
        plan_json=json.dumps({"steps": plan_json_steps}),
        required_bindings=["intent"],
        optional_bindings=[],
        default_bindings={"intent": "what is the meaning of life"},
        parent_dims=None,
    )


def _plan_run_ok():
    result = MagicMock()
    result.steps = [{"text": "the answer is 42"}]
    return result


class TestGrownPlanDimensionalColumns:
    """RDR-092 Phase 0b: grown plans pass verb/name/dimensions on save_plan.

    The R6 three-tier cascade resolves verb:
      1. caller-supplied ``dimensions["verb"]``
      2. inferred from ``plan_json.steps`` operator shape
      3. ``"research"`` fallback
    """

    @pytest.mark.asyncio
    async def test_grown_plan_has_dimensional_columns(self):
        """save_plan on an ad-hoc grow path receives verb, name, dimensions."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match_for_grow([
            {"tool": "search", "args": {"query": "$intent"}},
            {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
        ])
        save_mock = MagicMock(return_value=999)
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock
        db_stub.plans.get_plan = MagicMock(return_value={"id": 999})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_ok())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="what is the meaning of life")

        assert save_mock.called, "save_plan must be called on ad-hoc success"
        kwargs = save_mock.call_args.kwargs
        assert kwargs.get("verb"), "grown plan must carry a verb"
        assert kwargs.get("name"), "grown plan must carry a name"
        assert kwargs.get("dimensions"), "grown plan must carry canonical dimensions"
        # dimensions string is canonical JSON: sorted keys, lowercased strings
        parsed = json.loads(kwargs["dimensions"])
        assert parsed["verb"] == kwargs["verb"]
        assert parsed["scope"] == "personal"
        # name is kebab-case; strategy mirrors it so each grown plan is unique
        assert "-" in kwargs["name"] or kwargs["name"].isalpha()
        assert parsed.get("strategy") == kwargs["name"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "steps,expected_verb",
        [
            # Tier 2.1: compare step → analyze
            (
                [
                    {"tool": "search", "args": {}},
                    {"tool": "compare", "args": {}},
                ],
                "analyze",
            ),
            # Tier 2.2: extract + rank → analyze
            (
                [
                    {"tool": "search", "args": {}},
                    {"tool": "extract", "args": {}},
                    {"tool": "rank", "args": {}},
                ],
                "analyze",
            ),
            # Tier 2.3: traverse + search + summarize → research
            (
                [
                    {"tool": "search", "args": {}},
                    {"tool": "traverse", "args": {}},
                    {"tool": "summarize", "args": {}},
                ],
                "research",
            ),
            # Tier 3: flat shape falls back to research
            (
                [
                    {"tool": "search", "args": {}},
                    {"tool": "summarize", "args": {}},
                ],
                "research",
            ),
        ],
        ids=["compare→analyze", "extract+rank→analyze",
             "traverse+search+summarize→research", "flat→research"],
    )
    async def test_grown_plan_verb_inference_from_plan_json(
        self, steps, expected_verb,
    ):
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match_for_grow(steps)
        save_mock = MagicMock(return_value=1)
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_ok())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="how does X behave")

        assert save_mock.call_args.kwargs.get("verb") == expected_verb

    @pytest.mark.asyncio
    async def test_caller_dimensions_verb_wins(self):
        """Tier 1: caller-supplied dimensions['verb'] overrides inference."""
        from nexus.mcp.core import nx_answer

        # Plan shape would otherwise infer as "analyze" (has compare step),
        # but the caller pinned verb:debug.
        match = _ad_hoc_match_for_grow([
            {"tool": "search", "args": {}},
            {"tool": "compare", "args": {}},
        ])
        save_mock = MagicMock(return_value=1)
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_ok())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(
                question="test q",
                dimensions={"verb": "debug"},
            )

        assert save_mock.call_args.kwargs.get("verb") == "debug"

    @pytest.mark.asyncio
    async def test_name_is_kebab_case_from_content_words(self):
        """Name skips stop-words and kebab-cases 3-5 content tokens."""
        from nexus.mcp.core import nx_answer

        match = _ad_hoc_match_for_grow([
            {"tool": "search", "args": {}},
            {"tool": "summarize", "args": {}},
        ])
        save_mock = MagicMock(return_value=1)
        db_stub = MagicMock()
        db_stub.plans.save_plan = save_mock
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match", return_value=[]), \
             patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
             patch("nexus.plans.runner.plan_run", AsyncMock(return_value=_plan_run_ok())), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer(question="How does the chroma cache evict?")

        name = save_mock.call_args.kwargs.get("name") or ""
        # Stop-words ('how', 'does', 'the') are dropped; content words remain.
        assert "how" not in name.split("-")
        assert "does" not in name.split("-")
        assert "chroma" in name or "cache" in name
        # Kebab-case: lowercase, no spaces
        assert name == name.lower()
        assert " " not in name

    def test_infer_grown_plan_verb_helper_exposed(self):
        """The verb-inference helper is importable for direct unit tests
        and docs examples.
        """
        from nexus.mcp.core import _infer_grown_plan_verb

        plan = json.dumps({"steps": [
            {"tool": "search"}, {"tool": "compare"},
        ]})
        assert _infer_grown_plan_verb(
            caller_dimensions=None, plan_json=plan,
        ) == "analyze"
        # Caller override wins.
        assert _infer_grown_plan_verb(
            caller_dimensions={"verb": "review"}, plan_json=plan,
        ) == "review"
        # Unparseable plan → fallback.
        assert _infer_grown_plan_verb(
            caller_dimensions=None, plan_json="not-json",
        ) == "research"

    def test_infer_grown_plan_name_helper_exposed(self):
        from nexus.mcp.core import _infer_grown_plan_name

        # Drops common stop-words, keeps content tokens, joins with '-'.
        name = _infer_grown_plan_name("How does the chroma cache evict entries?")
        parts = name.split("-")
        assert "how" not in parts and "does" not in parts
        assert any(p in parts for p in ("chroma", "cache", "evict"))
        # Max 5 content words.
        long_q = "one two three four five six seven eight nine ten"
        long_name = _infer_grown_plan_name(long_q)
        assert len(long_name.split("-")) <= 5
        # Empty / whitespace-only falls back to a sentinel.
        assert _infer_grown_plan_name("") == "grown-plan"


# ── nx_tidy ───────────────────────────────────────────────────────────────────


class TestNxTidy:

    @pytest.mark.asyncio
    async def test_returns_summary_string(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_tidy

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            return {"summary": "Consolidated.", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_tidy(topic="chromadb quotas")
        assert isinstance(result, str)
        assert "Consolidated." in result

    @pytest.mark.asyncio
    async def test_prompt_contains_topic(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_tidy

        captured = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured.append(prompt)
            return {"summary": "ok", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_tidy(topic="voyage embeddings")
        assert "voyage embeddings" in captured[0]

    @pytest.mark.asyncio
    async def test_calls_claude_dispatch(self, monkeypatch):
        """nx_tidy must route through claude_dispatch, not a pool."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_tidy

        dispatch_calls = []

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            dispatch_calls.append(prompt)
            return {"summary": "ok", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_tidy(topic="test")
        assert dispatch_calls, "claude_dispatch must be called"

    @pytest.mark.asyncio
    async def test_default_timeout_is_600s(self, monkeypatch):
        """nx_tidy default timeout is 600s (10 min).

        Consolidation on a large corpus hits the old 120s ceiling
        routinely. Default raised 2026-04-17; caller can override.
        """
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_tidy

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"summary": "ok", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_tidy(topic="test")
        assert captured["timeout"] == 600.0, (
            f"nx_tidy default timeout must be 600s; got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_prefetches_entries_and_stays_tool_free(self, monkeypatch):
        """nexus-mawqw / Fix A: nx_tidy retrieves entries server-side
        (the MCP server holds direct T3 access), inlines them into the
        prompt, and dispatches a TOOL-FREE claude -p. This makes nx_tidy
        immune to CC permission posture forever — the child never calls a
        tool, so the post-2.1.162 server-approval gate can't break it."""
        import nexus.operators.dispatch as _mod
        import nexus.mcp.core as _core
        from nexus.mcp.core import nx_tidy

        # Pre-fetch surface: search returns structured ids, store_get_many
        # hydrates the bodies. Both run in-process on the server side.
        monkeypatch.setattr(_core, "search", lambda **kw: {
            "ids": ["id1", "id2"],
            "chunk_collections": ["knowledge__x", "knowledge__x"],
            "collections": ["knowledge__x"],
        })
        monkeypatch.setattr(_core, "store_get_many", lambda *a, **kw: {
            "contents": [
                "ENTRY-BODY-SENTINEL-ONE about chromadb quotas",
                "ENTRY-BODY-SENTINEL-TWO duplicate of one",
            ],
            "missing": [],
        })

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"summary": "ok", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_tidy(topic="chromadb quotas", collection="knowledge__x")

        # Hydrated bodies inlined into the prompt.
        assert "ENTRY-BODY-SENTINEL-ONE" in captured["prompt"]
        assert "ENTRY-BODY-SENTINEL-TWO" in captured["prompt"]
        # Tool-free: no MCP/tool grant passed to dispatch.
        assert not captured["kwargs"].get("mcp_servers"), (
            "nx_tidy must stay tool-free (Fix A pre-fetches in-process)"
        )
        assert not captured["kwargs"].get("allowed_tools")

    @pytest.mark.asyncio
    async def test_cap_saturation_is_surfaced_not_silent(self, monkeypatch):
        """nexus-mawqw / no-silent-caps: when retrieval saturates the
        _TIDY_MAX_ENTRIES cap, the returned summary must say so. A silent
        cap would let a tidy 'consolidate' a 200-entry collection from 30
        chunks and suggest deletions for entries it never saw."""
        import nexus.operators.dispatch as _mod
        import nexus.mcp.core as _core
        from nexus.mcp.core import nx_tidy, _TIDY_MAX_ENTRIES

        n = _TIDY_MAX_ENTRIES
        monkeypatch.setattr(_core, "search", lambda **kw: {
            "ids": [f"id{i}" for i in range(n)],
            "chunk_collections": ["knowledge__x"] * n,
            "collections": ["knowledge__x"],
        })
        monkeypatch.setattr(_core, "store_get_many", lambda *a, **kw: {
            "contents": [f"body {i}" for i in range(n)],
            "missing": [],
        })

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            return {"summary": "done", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_tidy(topic="t", collection="knowledge__x")
        assert str(_TIDY_MAX_ENTRIES) in result
        assert "capped" in result.lower()

    @pytest.mark.asyncio
    async def test_prefetch_failure_degrades_gracefully(self, monkeypatch):
        """If server-side retrieval errors, nx_tidy still dispatches
        (with no inlined entries) rather than raising — a tidy on a
        missing collection should report 'nothing found', not crash."""
        import nexus.operators.dispatch as _mod
        import nexus.mcp.core as _core
        from nexus.mcp.core import nx_tidy

        def boom(**kw):
            raise RuntimeError("t3 down")

        monkeypatch.setattr(_core, "search", boom)

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["prompt"] = prompt
            return {"summary": "nothing to consolidate", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_tidy(topic="ghost topic")
        assert "ghost topic" in captured["prompt"]
        assert isinstance(result, str)


# ── nx_enrich_beads ───────────────────────────────────────────────────────────


class TestNxEnrichBeads:

    @pytest.mark.asyncio
    async def test_returns_enriched_string(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            return {"enriched_description": "## Enriched\n\nDetails here."}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_enrich_beads(bead_description="Implement feature X")
        assert "Enriched" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_description(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = []

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured.append(prompt)
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="Sentinel bead abc123")
        assert "Sentinel bead abc123" in captured[0]

    @pytest.mark.asyncio
    async def test_prompt_includes_context(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = []

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured.append(prompt)
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task", context="extra ctx sentinel")
        assert "extra ctx sentinel" in captured[0]

    @pytest.mark.asyncio
    async def test_default_timeout_is_300s(self, monkeypatch):
        """nx_enrich_beads default timeout is 300s (5 min).

        Codebase enrichment with file:line verification is
        multi-step; 120s was a frequent false-timeout.
        """
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task")
        assert captured["timeout"] == 300.0, (
            f"nx_enrich_beads default timeout must be 300s; "
            f"got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_grants_mcp_and_tool_access(self, monkeypatch):
        """nexus-mawqw / Fix B: enrich does open-ended codebase
        exploration, so its claude -p child must be granted MCP + file
        tools. Without the grant the child sees the conexus server as
        unapproved (post-CC-2.1.162) and every tool call is denied."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured.update(kwargs)
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task")
        assert "nexus" in (captured.get("mcp_servers") or {}), (
            "enrich must inject the conexus MCP server inline"
        )
        assert "nexus_catalog" in (captured.get("mcp_servers") or {})
        allowed = captured.get("allowed_tools") or []
        assert "mcp__nexus" in allowed
        assert "Read" in allowed and "Grep" in allowed


# ── nx_plan_audit ─────────────────────────────────────────────────────────────


class TestNxPlanAudit:

    @pytest.mark.asyncio
    async def test_returns_verdict_string(self, monkeypatch):
        """The verdict is COMPUTED from classified findings (nexus-ll7zm),
        never taken from the model — the stub's own "pass" must not
        appear; zero findings resolve to READY."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit
        from nexus.plans.audit_rounds import READY

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            return {"verdict": "pass", "findings": [], "summary": "All good."}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_plan_audit(plan_json='{"steps": []}')
        assert READY in result
        assert "pass" not in result
        assert "All good." in result

    @pytest.mark.asyncio
    async def test_findings_included_in_output(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            return {
                "verdict": "warn",
                "findings": [{"severity": "important", "title": "Missing file"}],
                "summary": "One warning.",
            }

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_plan_audit(plan_json='{"steps": []}')
        assert "important" in result
        assert "Missing file" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_plan(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        captured = []

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured.append(prompt)
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        sentinel_plan = '{"steps": [{"tool": "search_sentinel_xyz"}]}'
        await nx_plan_audit(plan_json=sentinel_plan)
        assert "search_sentinel_xyz" in captured[0]

    @pytest.mark.asyncio
    async def test_default_timeout_is_600s(self, monkeypatch):
        """nx_plan_audit default timeout is 600s (10 min).

        A real plan audit verifies file:line pointers across the
        codebase and cross-references research findings; 120s was
        routinely hitting the timeout on non-trivial plans
        (observed on RDR-086's 11-bead plan, 2026-04-17).
        """
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}')
        assert captured["timeout"] == 600.0, (
            f"nx_plan_audit default timeout must be 600s; "
            f"got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_grants_mcp_and_tool_access(self, monkeypatch):
        """nexus-mawqw / Fix B: plan audit verifies file:line pointers
        across the codebase, so its claude -p child must be granted MCP +
        file tools."""
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured.update(kwargs)
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}')
        assert "nexus" in (captured.get("mcp_servers") or {})
        assert "nexus_catalog" in (captured.get("mcp_servers") or {})
        allowed = captured.get("allowed_tools") or []
        assert "mcp__nexus" in allowed
        assert "Read" in allowed


# ── Operator timeout defaults (all raised to 300s 2026-04-17) ────────────────


class TestOperatorTimeoutDefaults:
    """The 5 operator_* MCP tools default to 300s. 120s was too tight
    on real input (long documents, large item lists, complex criteria).
    Callers can still override lower when they know the scope is small.
    """

    @pytest.mark.asyncio
    async def test_operator_summarize_default_is_300s(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_summarize

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_summarize(content="text")
        assert captured["timeout"] == 300.0

    @pytest.mark.asyncio
    async def test_operator_extract_default_is_300s(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_extract

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"extractions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_extract(inputs="x", fields="a,b")
        assert captured["timeout"] == 300.0

    @pytest.mark.asyncio
    async def test_operator_rank_default_is_300s(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_rank

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"ranked": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_rank(items="a", criterion="x")
        assert captured["timeout"] == 300.0

    @pytest.mark.asyncio
    async def test_operator_compare_default_is_300s(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_compare

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"comparison": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_compare(items="a")
        assert captured["timeout"] == 300.0

    @pytest.mark.asyncio
    async def test_operator_generate_default_is_300s(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import operator_generate

        captured = {}

        async def fake(prompt, schema, timeout=60.0, model=None, **kw):
            captured["timeout"] = timeout
            return {"output": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await operator_generate(template="x", context="y")
        assert captured["timeout"] == 300.0

    def test_claude_dispatch_default_is_300s(self):
        """The dispatch substrate default should not regress below 300s —
        it's the floor every direct caller inherits."""
        import inspect
        from nexus.operators.dispatch import claude_dispatch

        sig = inspect.signature(claude_dispatch)
        assert sig.parameters["timeout"].default == 300.0


# ── nx_answer end-to-end orchestration (trunk tests, no API keys) ─────────────


def _fake_t2_ctx(tmp_path):
    """Return a factory that yields a real T2Database at tmp_path/t2.db."""
    from contextlib import contextmanager
    from nexus.db.t2 import T2Database

    @contextmanager
    def _ctx():
        with T2Database(tmp_path / "t2.db") as db:
            yield db

    return _ctx



#: structlog's ConsoleRenderer emits ANSI colour when FORCE_COLOR is set, which
#: interleaves escape codes inside `key=value` pairs. Log-content assertions
#: strip it so they measure the log's CONTENT in every environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class TestNxAnswerEndToEnd:
    """nx_answer() orchestration wiring with fully mocked sub-calls.

    No live API keys needed. Verifies match→classify→run→record trunk.
    """

    @pytest.mark.asyncio
    async def test_hit_path_calls_plan_run_and_returns_text(self, tmp_path):
        """Hit (cosine ≥ 0.40) → plan_run called → final text returned."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"text": "The final answer."}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("what is projection quality?")

        assert "final answer" in result.lower()

    @pytest.mark.asyncio
    async def test_dimensions_forwarded_to_plan_match(self, tmp_path):
        """Verb skills pass dimensions={verb: ...} → forwarded to plan_match.

        Verifies the signature extension that lets verb skills route through
        nx_answer (unifying the trunk + picking up the record step) instead
        of hand-rolling plan_match + plan_run.
        """
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"text": "Research answer."}])
        pm_calls = []

        def _spy(*args, **kwargs):
            pm_calls.append(kwargs)
            return [match]

        with (
            patch("nexus.plans.matcher.plan_match", side_effect=_spy),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer(
                "how does projection quality work?",
                dimensions={"verb": "research"},
            )

        assert pm_calls, "plan_match was never called"
        assert pm_calls[0].get("dimensions") == {"verb": "research"}, (
            f"plan_match dimensions not forwarded: {pm_calls[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_miss_path_calls_plan_miss_planner(self, tmp_path):
        """No matches (plan miss) → _nx_answer_plan_miss dispatched."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        import nexus.mcp.core as _core
        from nexus.plans.runner import PlanResult

        ad_hoc = _make_match(plan_id=0, confidence=None)
        run_result = PlanResult(steps=[{"text": "Inline answer."}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_core, "_nx_answer_plan_miss",
                         AsyncMock(return_value=ad_hoc)),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("novel question with no plan")

        assert "inline answer" in result.lower()

    @pytest.mark.asyncio
    async def test_planner_fail_returns_user_readable_error(self, tmp_path):
        """When inline planner raises, nx_answer returns a readable error string."""
        import nexus.mcp_infra as _infra
        import nexus.mcp.core as _core

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch.object(_core, "_nx_answer_plan_miss",
                         AsyncMock(side_effect=ValueError("dispatch failed"))),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("unanswerable question")

        assert isinstance(result, str)
        assert "planner" in result.lower() or "search" in result.lower()


class TestNxAnswerBindingAlias:
    """Library plans declare ``required_bindings`` like ``[concept]``,
    ``[area, criterion]``, ``[topic]`` etc., but ``nx_answer`` only
    auto-supplies ``intent``. Without aliasing, plans with these names
    fail at dispatch with ``missing required bindings: [...]`` even
    though ``$intent`` carries the equivalent value.

    The fix: when the matched plan declares a required binding the
    caller didn't pre-supply (and that has no default), nx_answer
    aliases the question text into it before calling plan_run.
    Mirrors what the inline-planner fallback already does.
    """

    @pytest.mark.asyncio
    async def test_concept_binding_aliased_from_question(self, tmp_path):
        """``required_bindings: [concept]`` (research-default shape) gets
        ``concept = question`` filled in by nx_answer."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        concept_plan = Match(
            plan_id=1, name="research-default", description="research plan",
            confidence=0.75, dimensions={"verb": "research"}, tags="",
            plan_json=json.dumps({
                "steps": [{"tool": "search",
                           "args": {"query": "$concept", "corpus": "knowledge"}}],
            }),
            required_bindings=["concept"], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )
        run_result = PlanResult(steps=[{"text": "ok"}])
        captured: list[dict] = []

        async def _spy(match, bindings, **kwargs):
            captured.append(dict(bindings))
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[concept_plan]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("how does projection quality work?")

        assert captured, "plan_run was never called"
        assert captured[0].get("intent") == "how does projection quality work?"
        assert captured[0].get("concept") == "how does projection quality work?", (
            f"concept binding not aliased from question: {captured[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_multiple_required_bindings_all_aliased(self, tmp_path):
        """``required_bindings: [area, criterion]`` (analyze-default
        shape) gets both filled. Imperfect but consistent with inline-
        planner behavior; better than dispatch failure."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        analyze_plan = Match(
            plan_id=2, name="analyze-default", description="analyze",
            confidence=0.65, dimensions={"verb": "analyze"}, tags="",
            plan_json=json.dumps({"steps": [{"tool": "search",
                                              "args": {"query": "$area"}}]}),
            required_bindings=["area", "criterion"], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )
        run_result = PlanResult(steps=[{"text": "ok"}])
        captured: list[dict] = []

        async def _spy(match, bindings, **kwargs):
            captured.append(dict(bindings))
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[analyze_plan]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("compare projection strategies")

        assert captured[0].get("area") == "compare projection strategies"
        assert captured[0].get("criterion") == "compare projection strategies"

    @pytest.mark.asyncio
    async def test_default_bindings_not_overwritten(self, tmp_path):
        """A required binding with a default value is left alone — only
        unsupplied AND defaultless bindings get the question text."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        plan_with_default = Match(
            plan_id=3, name="with-default", description="x",
            confidence=0.75, dimensions={}, tags="",
            plan_json=json.dumps({"steps": [{"tool": "search",
                                              "args": {"query": "$concept"}}]}),
            required_bindings=["concept"], optional_bindings=[],
            default_bindings={"concept": "hardcoded default"},
            parent_dims=None,
        )
        run_result = PlanResult(steps=[{"text": "ok"}])
        captured: list[dict] = []

        async def _spy(match, bindings, **kwargs):
            captured.append(dict(bindings))
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[plan_with_default]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("the user's question")

        # nx_answer must NOT clobber the default — plan_run merges
        # defaults with caller bindings; nx_answer's auto-alias fires
        # only when neither side supplied the binding.
        assert "concept" not in captured[0], (
            f"nx_answer overwrote a defaulted binding: {captured[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_intent_required_binding_unchanged(self, tmp_path):
        """``required_bindings: [intent]`` (the abstract-themes shape)
        already gets ``intent`` from nx_answer's standard population —
        the alias loop is a no-op for it."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        intent_plan = Match(
            plan_id=4, name="abstract-themes", description="x",
            confidence=0.55, dimensions={}, tags="",
            plan_json=json.dumps({"steps": [{"tool": "search",
                                              "args": {"query": "$intent"}}]}),
            required_bindings=["intent"], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )
        run_result = PlanResult(steps=[{"text": "ok"}])
        captured: list[dict] = []

        async def _spy(match, bindings, **kwargs):
            captured.append(dict(bindings))
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[intent_plan]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("what are the main themes?")

        assert captured[0].get("intent") == "what are the main themes?"


class TestNxAnswerFTS5HitPath:
    """FTS5 sentinel (confidence=None) is treated as a hit, not a miss."""

    @pytest.mark.asyncio
    async def test_fts5_sentinel_routes_to_plan_run(self, tmp_path):
        """confidence=None match → plan_run called (not inline planner)."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        fts5_match = _make_match(confidence=None)
        run_result = PlanResult(steps=[{"text": "FTS5 answer."}])

        plan_miss_calls = []

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[fts5_match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
            patch("nexus.mcp.core._nx_answer_plan_miss",
                  AsyncMock(side_effect=lambda *a, **k: plan_miss_calls.append(1))),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("fts5 question")

        assert not plan_miss_calls, "FTS5 hit must not fall through to inline planner"
        assert "FTS5 answer" in result


class TestNxAnswerTimeoutHandling:
    """claude_dispatch timeout raises OperatorTimeoutError → user-readable error."""

    @pytest.mark.asyncio
    async def test_plan_miss_timeout_returns_readable_string(self, tmp_path):
        """OperatorTimeoutError from inline planner → graceful error return."""
        import nexus.mcp_infra as _infra
        import nexus.mcp.core as _core
        from nexus.operators.dispatch import OperatorTimeoutError

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch.object(_core, "_nx_answer_plan_miss",
                         AsyncMock(side_effect=OperatorTimeoutError("timed out"))),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("slow question")

        assert isinstance(result, str)
        # Must not re-raise — must surface as a readable message
        assert "planner" in result.lower() or "search" in result.lower() or "error" in result.lower()


class TestNxAnswerCostAccounting:
    """RDR-196 .p1d (nexus-nyry9.10): ``cost_usd`` is no longer a hardcoded
    0.0 stub — it is the SUM of the run's ``StepRecord.cost_usd`` values
    that are not ``None``, or ``None`` (never a fabricated 0.0) when no
    step reports a known cost. Supersedes the old ``TestNxAnswerCostStub``
    class, which pinned the P5-stub-0.0 contract this bead removes."""

    def test_cost_usd_sums_known_step_costs(self, tmp_path):
        from nexus.db.t2 import T2Database
        from nexus.plans.runner import StepRecord

        steps = [
            StepRecord(step_index=0, operator="query", source="sql", cost_usd=None),
            StepRecord(step_index=1, operator="operator_generate", source="llm", cost_usd=0.02),
            StepRecord(step_index=2, operator="operator_summarize", source="llm", cost_usd=0.015),
        ]
        with T2Database(tmp_path / "mem.db") as db:
            seen = TestRunRecording._record_and_capture(
                db, question="q", plan_id=1, matched_confidence=0.8,
                step_count=3, final_text="answer", step_records=steps,
                duration_ms=500, trace=True,
            )
            assert seen["cost_usd"] == pytest.approx(0.035)

    def test_cost_usd_none_when_no_step_reports_a_known_cost(self, tmp_path):
        """An isolated/bundle-fallback 'llm' StepRecord with cost_usd=None
        (the dispatch layer genuinely could not observe it, per StepRecord's
        own docstring) must never coerce to a fabricated 0.0."""
        from nexus.db.t2 import T2Database
        from nexus.plans.runner import StepRecord

        steps = [StepRecord(step_index=0, operator="claude_dispatch", source="llm", cost_usd=None)]
        with T2Database(tmp_path / "mem.db") as db:
            seen = TestRunRecording._record_and_capture(
                db, question="q", plan_id=1, matched_confidence=0.8,
                step_count=1, final_text="answer", step_records=steps,
                duration_ms=500, trace=True,
            )
            assert seen["cost_usd"] is None

    def test_cost_usd_none_when_no_steps_at_all(self, tmp_path):
        """The call sites that never produce a StepRecord at all (planner
        failure before any dispatch, a binding refusal before plan_run,
        the single-step fast path which bypasses the runner) get None —
        sum-of-nothing is honestly 'unknown', not zero."""
        from nexus.db.t2 import T2Database

        with T2Database(tmp_path / "mem.db") as db:
            seen = TestRunRecording._record_and_capture(
                db, question="q", plan_id=1, matched_confidence=0.8,
                step_count=0, final_text="answer", step_records=[],
                duration_ms=500, trace=True,
            )
            assert seen["cost_usd"] is None

    def test_budget_usd_parameter_accepted_without_error(self, tmp_path):
        """budget_usd is accepted. RDR-196 .p3a (nexus-nyry9.19): the
        unmeasured 0.25 literal is gone; the default is None = "resolve
        to budget_default.DERIVED_BUDGET_USD". RDR-196 .p3c
        (nexus-nyry9.21): enforcement is now ON -- see TestNxAnswerBudgetUsd
        for the enforcement behavior itself."""
        import inspect
        from nexus.mcp.core import nx_answer
        from nexus.plans.budget_default import BUDGET_ENFORCEMENT_ENABLED

        sig = inspect.signature(nx_answer)
        assert "budget_usd" in sig.parameters, "budget_usd must remain in signature"
        assert sig.parameters["budget_usd"].default is None
        assert BUDGET_ENFORCEMENT_ENABLED is True


class TestNxAnswerLatencyProxy:
    """nx_answer with mocked sub-calls completes in <1s (no blocking calls)."""

    @pytest.mark.asyncio
    async def test_orchestration_has_no_blocking_calls(self, tmp_path):
        """With all I/O mocked, nx_answer completes in under 1 second.

        A >1s wall time indicates an inadvertent blocking sleep or subprocess.
        """
        import time
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.9)
        run_result = PlanResult(steps=[{"text": "Fast answer."}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            t0 = time.monotonic()
            await nx_answer("fast question")
            elapsed = time.monotonic() - t0

        assert elapsed < 1.0, (
            f"nx_answer with mocked I/O took {elapsed:.2f}s — "
            "possible blocking call reintroduced"
        )


# ── Subagent timeout floor (nexus-7sbf) ──────────────────────────────────────


class TestSubagentTimeoutFloor:
    """Agents (e.g. strategic-planner) occasionally pass explicit low
    timeouts to ``nx_plan_audit`` / ``nx_enrich_beads`` (seen: 180s,
    300s), bypassing the v4.5.3 raised defaults and producing
    false-positive timeouts on multi-phase plans. The tool body
    clamps the requested timeout to a floor so agent overrides can
    only raise, not lower, the effective timeout.
    """

    SUBAGENT_TIMEOUT_FLOOR = 300.0

    @pytest.mark.asyncio
    async def test_plan_audit_clamps_below_floor(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}', timeout=60.0)
        assert captured["timeout"] == self.SUBAGENT_TIMEOUT_FLOOR, (
            f"nx_plan_audit with timeout=60 must clamp to "
            f"{self.SUBAGENT_TIMEOUT_FLOOR}; got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_plan_audit_honours_above_floor(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}', timeout=900.0)
        assert captured["timeout"] == 900.0, (
            "timeouts above the floor must be honoured verbatim"
        )

    @pytest.mark.asyncio
    async def test_enrich_beads_clamps_below_floor(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task", timeout=120.0)
        assert captured["timeout"] == self.SUBAGENT_TIMEOUT_FLOOR, (
            f"nx_enrich_beads with timeout=120 must clamp to "
            f"{self.SUBAGENT_TIMEOUT_FLOOR}; got {captured['timeout']}"
        )

    @pytest.mark.asyncio
    async def test_enrich_beads_honours_above_floor(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = {}

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            captured["timeout"] = timeout
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task", timeout=450.0)
        assert captured["timeout"] == 450.0

    @pytest.mark.asyncio
    async def test_plan_audit_logs_warning_on_clamp(self, monkeypatch, capsys):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        async def fake(prompt, schema, timeout=60.0, **kwargs):
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}', timeout=180.0)
        out = capsys.readouterr()
        # Strip ANSI: structlog's ConsoleRenderer colorizes whenever FORCE_COLOR
        # is set in the environment (independent of isatty, so capsys does not
        # disable it), which splits `tool=nx_plan_audit` into
        # `\x1b[36mtool\x1b[0m=\x1b[35mnx_plan_audit\x1b[0m` and breaks every
        # plain substring assertion below. CI has no FORCE_COLOR, so this pin
        # was green there and red in any developer terminal that sets it.
        emitted = _ANSI_RE.sub("", out.out + out.err)
        assert "subagent_timeout_clamped" in emitted, (
            "expected a structured warning when caller timeout is below floor"
        )
        assert "tool=nx_plan_audit" in emitted
        assert "requested=180" in emitted
        assert "floor=300" in emitted


class TestNxAnswerPlannerRetry:
    """nexus-wr5o: 1-shot retry on OperatorOutputError from claude_dispatch.

    Transient JSON parse failures (model output drift, partial stream, null
    structured_output on first attempt) are retried once with a halved
    timeout. OperatorError (subprocess non-zero) and OperatorTimeoutError
    are NOT retried — those failure modes are not transient.
    """

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self):
        """First call raises OperatorOutputError, second returns valid payload."""
        from nexus.mcp.core import _nx_answer_plan_miss
        from nexus.operators.dispatch import OperatorOutputError

        valid_payload = {
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
            ],
        }
        mock_dispatch = AsyncMock(side_effect=[
            OperatorOutputError("transient JSON parse drift"),
            valid_payload,
        ])

        with patch(
            "nexus.operators.dispatch.claude_dispatch", mock_dispatch,
        ), patch(
            "nexus.mcp_infra.get_collection_names", return_value=["knowledge"],
        ):
            match = await _nx_answer_plan_miss("how does X work?")

        # Synthetic match for inline-planner result.
        assert match.plan_id == 0
        assert match.name == "ad-hoc"
        # Two attempts: first failed, second succeeded.
        assert mock_dispatch.call_count == 2
        # Verify the timeouts: 300 then 150.
        assert mock_dispatch.call_args_list[0].kwargs["timeout"] == 300.0
        assert mock_dispatch.call_args_list[1].kwargs["timeout"] == 150.0

    @pytest.mark.asyncio
    async def test_retry_exhausted_raises_second_error(self):
        """Both attempts raise OperatorOutputError → raise the second one."""
        from nexus.mcp.core import _nx_answer_plan_miss
        from nexus.operators.dispatch import OperatorOutputError

        mock_dispatch = AsyncMock(side_effect=[
            OperatorOutputError("first attempt drift"),
            OperatorOutputError("second attempt drift, this is the actionable one"),
        ])

        with patch(
            "nexus.operators.dispatch.claude_dispatch", mock_dispatch,
        ), patch(
            "nexus.mcp_infra.get_collection_names", return_value=["knowledge"],
        ):
            with pytest.raises(OperatorOutputError) as excinfo:
                await _nx_answer_plan_miss("how does X work?")

        assert mock_dispatch.call_count == 2
        # Caller gets the most recent (most actionable) error.
        assert "second attempt" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_no_retry_for_operator_error(self):
        """OperatorError (subprocess non-zero) raises immediately, no retry."""
        from nexus.mcp.core import _nx_answer_plan_miss
        from nexus.operators.dispatch import OperatorError

        mock_dispatch = AsyncMock(
            side_effect=OperatorError("claude -p exited 1: oops"),
        )

        with patch(
            "nexus.operators.dispatch.claude_dispatch", mock_dispatch,
        ), patch(
            "nexus.mcp_infra.get_collection_names", return_value=["knowledge"],
        ):
            with pytest.raises(OperatorError):
                await _nx_answer_plan_miss("how does X work?")

        assert mock_dispatch.call_count == 1, (
            "OperatorError must NOT trigger retry — non-zero exit is "
            "not transient"
        )

    @pytest.mark.asyncio
    async def test_no_retry_for_timeout_error(self):
        """OperatorTimeoutError raises immediately — hangs are not transient."""
        from nexus.mcp.core import _nx_answer_plan_miss
        from nexus.operators.dispatch import OperatorTimeoutError

        mock_dispatch = AsyncMock(
            side_effect=OperatorTimeoutError("claude -p timed out after 300s"),
        )

        with patch(
            "nexus.operators.dispatch.claude_dispatch", mock_dispatch,
        ), patch(
            "nexus.mcp_infra.get_collection_names", return_value=["knowledge"],
        ):
            with pytest.raises(OperatorTimeoutError):
                await _nx_answer_plan_miss("how does X work?")

        assert mock_dispatch.call_count == 1

    @pytest.mark.asyncio
    async def test_first_attempt_success_no_retry(self):
        """If first call succeeds, no second call is made."""
        from nexus.mcp.core import _nx_answer_plan_miss

        valid_payload = {
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
            ],
        }
        mock_dispatch = AsyncMock(return_value=valid_payload)

        with patch(
            "nexus.operators.dispatch.claude_dispatch", mock_dispatch,
        ), patch(
            "nexus.mcp_infra.get_collection_names", return_value=["knowledge"],
        ):
            match = await _nx_answer_plan_miss("how does X work?")

        assert match.plan_id == 0
        assert mock_dispatch.call_count == 1
        # First-attempt path uses the full 300s timeout.
        assert mock_dispatch.call_args.kwargs["timeout"] == 300.0


# ── GH #555: final_text envelope unwrap ─────────────────────────────────────


def test_maybe_unwrap_output_envelope_passthrough() -> None:
    """Plain prose passes through unchanged. Empty string short-circuits."""
    from nexus.mcp.core import _maybe_unwrap_output_envelope

    assert _maybe_unwrap_output_envelope("plain prose") == "plain prose"
    assert _maybe_unwrap_output_envelope("") == ""


def test_maybe_unwrap_output_envelope_single_layer() -> None:
    """One-layer ``{"output": "..."}`` envelope unwraps to the inner
    string. The ``operator_generate`` schema produces this shape;
    pre-fix the json.dumps fallback in nx_answer's text-key search
    surfaced it as final_text verbatim.
    """
    import json
    from nexus.mcp.core import _maybe_unwrap_output_envelope

    payload = json.dumps({"output": "actual prose"})
    assert _maybe_unwrap_output_envelope(payload) == "actual prose"


def test_maybe_unwrap_output_envelope_double_wrap() -> None:
    """GH #555: the extract -> generate bundle path can produce a
    doubly-wrapped envelope when claude -p treats the prior step's
    envelope as raw input. The unwrap descends into nested
    string-valued ``output`` fields.
    """
    import json
    from nexus.mcp.core import _maybe_unwrap_output_envelope

    inner = json.dumps({"output": "deep prose"})
    outer = json.dumps({"output": inner})
    assert _maybe_unwrap_output_envelope(outer) == "deep prose"


def test_maybe_unwrap_output_envelope_strict_shape_only() -> None:
    """Multi-key dicts and non-string output values are NOT unwrapped.
    Only the strict ``{"output": <str>}`` single-key shape triggers
    descent so legitimate JSON payloads pass through intact.
    """
    import json
    from nexus.mcp.core import _maybe_unwrap_output_envelope

    # Multi-key dict: not unwrapped.
    multi = json.dumps({"output": "x", "citations": []})
    assert _maybe_unwrap_output_envelope(multi) == multi

    # Non-string output: not unwrapped.
    list_val = json.dumps({"output": [1, 2, 3]})
    assert _maybe_unwrap_output_envelope(list_val) == list_val

    # Different key: not unwrapped.
    other = json.dumps({"text": "x"})
    assert _maybe_unwrap_output_envelope(other) == other


def test_maybe_unwrap_output_envelope_max_depth_bounded() -> None:
    """A malformed deeply-recursive payload terminates after max_depth
    iterations. Chosen so a 3-or-more-deep wrap (unobserved in the
    wild) returns the partially-unwrapped state rather than looping.
    """
    import json
    from nexus.mcp.core import _maybe_unwrap_output_envelope

    # Build a 4-deep wrap; default max_depth is 3.
    text = "core"
    for _ in range(4):
        text = json.dumps({"output": text})
    out = _maybe_unwrap_output_envelope(text, max_depth=3)
    # 3 unwraps from a 4-layer payload leaves one layer remaining.
    assert "core" in out and out.startswith('{')


# ── RDR-137 followup (nexus-n1908): scope normalization + empty-retrieval guard


class TestScopeNormalization:
    """nx_answer must normalize a malformed comma-list scope to broad
    search (with a warning) instead of filtering retrieval to nothing."""

    @pytest.mark.parametrize(
        "raw,expected_norm,warning_present,warning_substr",
        [
            pytest.param("rdr,code,docs", "", True, "comma-list", id="comma_list_normalizes_to_empty_with_warning"),
            pytest.param("knowledge", "knowledge", False, None, id="single_corpus_unchanged_no_warning"),
            pytest.param("1.2", "1.2", False, None, id="subtree_scope_unchanged"),
            pytest.param("   ", "", False, None, id="whitespace_only_normalizes_to_empty"),
            pytest.param("  knowledge  ", "knowledge", False, None, id="surrounding_whitespace_stripped"),
        ],
    )
    def test_normalize_scope(self, raw, expected_norm, warning_present, warning_substr):
        from nexus.mcp.core import _nx_answer_normalize_scope
        norm, warning = _nx_answer_normalize_scope(raw)
        assert norm == expected_norm
        if warning_present:
            assert warning is not None
            assert warning_substr in warning
        else:
            assert warning is None


class TestEmptyRetrievalGuard:
    """nx_answer must return an explicit no-match when retrieval steps
    yield zero evidence, rather than letting the operator synthesize a
    confident off-topic answer from ambient SessionStart context."""

    @pytest.mark.parametrize(
        "steps,expected",
        [
            pytest.param(
                [{"ids": [], "distances": []}, {"output": "synthesized prose"}],
                True, id="fires_when_retrieval_step_empty",
            ),
            pytest.param(
                [{"ids": ["c1", "c2"]}, {"output": "prose"}],
                False, id="does_not_fire_when_evidence_present",
            ),
            pytest.param(
                # No step exposes ids/tumblers -> not retrieval-bearing -> exempt.
                [{"output": "a synthesized answer with no retrieval"}],
                False, id="exempts_pure_generate_plan_no_retrieval",
            ),
            pytest.param(
                [{"tumblers": []}, {"summary": "prose"}],
                True, id="fires_on_empty_tumbler_traversal",
            ),
            pytest.param(
                # One empty search step + one non-empty traversal -> evidence exists.
                [{"ids": []}, {"tumblers": ["1.2.3"]}, {"output": "prose"}],
                False, id="evidence_in_any_step_suppresses_guard",
            ),
            pytest.param(
                ["not a dict", None, {"ids": []}],
                True, id="non_dict_steps_ignored_alongside_empty_ids",
            ),
            pytest.param(
                ["just", "strings"],
                False, id="all_non_dict_steps_not_retrieval_bearing",
            ),
        ],
    )
    def test_empty_retrieval_guard(self, steps, expected):
        from nexus.mcp.core import _nx_answer_is_empty_retrieval
        assert _nx_answer_is_empty_retrieval(steps) is expected


# ── nexus-h33x8.6 a4: hard time budget + partial results ────────────────────
#
# Contract (T2 nexus/nx-answer-capability-analysis-2026-08-19, converged
# with RDR-196 §Approach): nx_answer accepts an OPTIONAL wall-clock
# ``budget_seconds``. Default unset ("generous/off") reproduces existing
# behavior exactly. When plan_run reports a budget cutoff
# (``PlanResult.budget_exhausted_at_step``), nx_answer returns the
# retrieved results plus any reconstructed partial operator text instead
# of the normal final-step extraction, marked with a leading
# ``[budget exhausted after step N of M — partial answer]`` line in text
# mode and a ``budget_exhausted_at_step`` top-level field in structured
# mode.


class TestNxAnswerBudgetSeconds:

    def test_default_is_off(self):
        """The parameter defaults to None (no budget enforced) — the
        module-level constant documents the default explicitly."""
        import inspect
        from nexus.mcp.core import nx_answer, _NX_ANSWER_DEFAULT_BUDGET_SECONDS

        assert _NX_ANSWER_DEFAULT_BUDGET_SECONDS is None
        sig = inspect.signature(nx_answer)
        assert "budget_seconds" in sig.parameters
        assert sig.parameters["budget_seconds"].default is None

    @pytest.mark.asyncio
    async def test_unset_budget_does_not_pass_a_deadline_to_plan_run(self, tmp_path):
        """Regression: omitting budget_seconds must leave plan_run's
        deadline kwarg at None — proves the default truly changes
        nothing for existing callers."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"text": "answer"}])
        captured: dict = {}

        async def _spy(match, bindings, **kwargs):
            captured.update(kwargs)
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q")

        assert captured.get("deadline") is None

    @pytest.mark.asyncio
    async def test_explicit_budget_passes_a_deadline_to_plan_run(self, tmp_path):
        """budget_seconds=N must translate to a real monotonic deadline
        passed through to plan_run for an operator-bearing plan."""
        import time
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)  # search + operator_summarize
        run_result = PlanResult(steps=[{"text": "answer"}])
        captured: dict = {}

        async def _spy(match, bindings, **kwargs):
            captured.update(kwargs)
            return run_result

        before = time.monotonic()
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q", budget_seconds=30.0)
        after = time.monotonic()

        deadline = captured.get("deadline")
        assert deadline is not None
        assert before + 30.0 <= deadline <= after + 30.0

    @pytest.mark.asyncio
    async def test_multi_step_no_operator_plan_now_gets_budget_deadline(self, tmp_path):
        """nexus-nyry9.5 (RDR-196 .r5 review-fix, critic CRITICAL, T2
        review-nexus-nyry9.5): PINS THE NEW CONTRACT. A multi-step plan
        with zero operator steps used to be classified into a deleted
        third bucket and exempted from the budget deadline entirely --
        a census of all 17 real shipped builtin plans found ZERO that
        ever classified that way via a real plan_match(), so the
        exemption protected no real plan. The bucket and its exemption
        are deleted; this same plan shape must now receive a real
        deadline like any other non-single_query plan. Formerly
        ``test_retrieval_only_plan_exempt_from_budget_deadline``, which
        asserted the opposite (``deadline is None``) under the deleted
        contract."""
        import time
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        no_operator_match = _make_match(
            confidence=0.75,
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "query", "args": {"question": "$intent"}},
                ],
            }),
        )
        run_result = PlanResult(steps=[{"text": "answer"}])
        captured: dict = {}

        async def _spy(match, bindings, **kwargs):
            captured.update(kwargs)
            return run_result

        before = time.monotonic()
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[no_operator_match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q", budget_seconds=5.0)
        after = time.monotonic()

        deadline = captured.get("deadline")
        assert deadline is not None, (
            "a multi-step, zero-operator plan must now receive the same "
            "budget deadline as any other non-single_query plan -- the "
            "deleted bucket's exemption must not resurface"
        )
        assert before + 5.0 <= deadline <= after + 5.0

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_marker_text_not_raw_error(self, tmp_path):
        """A budget-exhausted PlanResult must produce the documented
        marker line, not the normal final-step extraction and not a
        raised exception."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)  # 2-step plan: search, operator_summarize
        run_result = PlanResult(
            steps=[{"ids": ["a", "b"], "tumblers": [], "distances": [0.1, 0.2],
                    "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            total_planned_steps=2,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=20.0)

        assert isinstance(result, str)
        assert "[budget exhausted (time) after step 2 of 2 — partial answer]" in result
        assert "a" in result and "b" in result  # retrieved chunk ids surfaced

    @pytest.mark.asyncio
    async def test_budget_exhausted_includes_partial_operator_text(self, tmp_path):
        """The reconstructed OperatorTimeoutError.partial_text (captured
        by plan_run into the terminal sentinel) must reach the final
        answer text, not be silently dropped."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[
                {"ids": ["a"], "tumblers": [], "distances": [0.1], "collections": ["knowledge"]},
                {"status": "timeout", "partial_text": "reconstructed partial synthesis",
                 "event_count": 5, "text": "reconstructed partial synthesis",
                 "summary": "", "aggregates": [], "error": "timed out",
                 "tool": "operator_summarize", "step_index": 1},
            ],
            budget_exhausted_at_step=2,
            total_planned_steps=2,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=20.0)

        assert "reconstructed partial synthesis" in result

    @pytest.mark.asyncio
    async def test_budget_exhausted_structured_envelope_carries_marker_field(self, tmp_path):
        """structured=True must surface budget_exhausted_at_step as a
        top-level envelope field per the RDR-196 marker convention."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1], "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            total_planned_steps=2,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=20.0, structured=True)

        assert isinstance(result, dict)
        assert result["budget_exhausted_at_step"] == 2
        assert "[budget exhausted (time) after step 2 of 2" in result["final_text"]

    @pytest.mark.asyncio
    async def test_budget_exhausted_marker_uses_plan_result_total_planned_steps_field(
        self, tmp_path,
    ):
        """code-review-expert Important (T2 code-review-nexus-h33x8.6-
        a4-a2-2026-08-19): the marker's 'of M' must come from
        ``PlanResult.total_planned_steps`` -- the field a4 added
        specifically so callers don't re-parse ``best.plan_json`` --
        not from a fresh re-parse. Deliberately diverges the two
        sources (2-step plan_json vs total_planned_steps=5) so a
        re-parse would produce the WRONG number and this test would
        catch it."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)  # plan_json describes 2 steps
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1], "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            total_planned_steps=5,  # deliberately NOT what plan_json would yield
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=20.0)

        assert "[budget exhausted (time) after step 2 of 5 — partial answer]" in result, (
            f"expected total_planned_steps=5 (from PlanResult) in the marker, "
            f"got: {result!r}"
        )

    @pytest.mark.asyncio
    async def test_planner_phase_exhausts_budget_returns_marker_not_plan_run(
        self, tmp_path,
    ):
        """nexus-nyry9.2 (RDR-196 .r2), RED-FIRST: a plan-match MISS that
        forces the inline planner must have that planner phase charged
        against budget_seconds. A budget smaller than the planner's own
        elapsed time must return the exhaustion marker WITHOUT ever
        calling plan_run — not silently run the (now-late) plan anyway.
        Pre-fix this goes red: plan_run() IS called and a plain answer
        with no marker comes back, because deadline was never consulted
        between the plan-match gate/inline planner and plan execution.
        """
        import asyncio
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner

        match = _make_match(confidence=0.75)  # 2-step: search + summarize (needs_operators)

        async def _slow_plan_miss(question, scope="", max_steps=6, **kwargs):
            await asyncio.sleep(0.05)
            return match

        plan_run_mock = AsyncMock()

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),  # forced miss
            patch("nexus.mcp.core._nx_answer_plan_miss",
                  AsyncMock(side_effect=_slow_plan_miss)),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", plan_run_mock),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=0.01)

        # code-review-nexus-nyry9.2-2026-08-20 (optional nit): this
        # assertion runs BEFORE the sentinel import below so the
        # pre-fix red is the actual behavioral failure (plan_run WAS
        # called) rather than an ImportError on a constant that
        # doesn't exist yet.
        plan_run_mock.assert_not_called()
        from nexus.mcp.core import _NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN
        assert isinstance(result, str)
        assert (
            f"[budget exhausted (time) after step {_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN} "
            "of 2 — partial answer]"
        ) in result

    @pytest.mark.asyncio
    async def test_planner_phase_exhaustion_structured_envelope_carries_pre_plan_sentinel(
        self, tmp_path,
    ):
        """Both marker shapes must agree: structured=True must ALSO
        carry the pre-Step-2 sentinel as the top-level
        ``budget_exhausted_at_step`` field. Asserting only the text-mode
        leading line (the test above) would miss a silent-truncation
        class where the structured envelope's field disagrees with, or
        omits, what the text says — the exact class RDR-196 calls out.
        """
        import asyncio
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner

        match = _make_match(confidence=0.75)

        async def _slow_plan_miss(question, scope="", max_steps=6, **kwargs):
            await asyncio.sleep(0.05)
            return match

        plan_run_mock = AsyncMock()

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),
            patch("nexus.mcp.core._nx_answer_plan_miss",
                  AsyncMock(side_effect=_slow_plan_miss)),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", plan_run_mock),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=0.01, structured=True)

        # See the sibling text-mode test above for why this import is
        # deferred past the behavioral assertion.
        plan_run_mock.assert_not_called()
        from nexus.mcp.core import _NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN
        assert isinstance(result, dict)
        assert result["budget_exhausted_at_step"] == _NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN
        assert (
            f"[budget exhausted (time) after step {_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN} "
            "of 2"
        ) in result["final_text"]

    @pytest.mark.asyncio
    async def test_pre_plan_check_survives_malformed_plan_json(self, tmp_path):
        """code-review Important (T2 nyry9.2-code-review-2026-08-20): a
        corrupted plan_json row reaching the pre-Step-2 budget check
        with an already-exhausted budget must still return the
        exhaustion marker, not raise out of the MCP tool -- mirrors
        ``_nx_answer_classify_plan``'s own JSONDecodeError/TypeError
        guard on the same field.
        """
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.mcp.core import _NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN

        malformed_match = _make_match(confidence=0.75, plan_json="{not valid json")
        plan_run_mock = AsyncMock()

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[malformed_match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", plan_run_mock),
        ):
            from nexus.mcp.core import nx_answer
            # budget_seconds=0.0 (not None -- deadline = start + 0.0 =
            # start) guarantees the pre-Step-2 check's own
            # ``time.monotonic() >= deadline`` is already true by the
            # time it runs, without a real sleep.
            result = await nx_answer("q", budget_seconds=0.0)

        plan_run_mock.assert_not_called()
        assert isinstance(result, str)
        assert (
            f"[budget exhausted (time) after step {_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN} "
            "of 0 — partial answer]"
        ) in result

    @pytest.mark.asyncio
    async def test_budget_seconds_applies_to_miss_plus_grown_plan_combo(
        self, tmp_path,
    ):
        """PIN TEST, UPDATED by nexus-nyry9.5 (RDR-196 .r5 review-fix,
        critic CRITICAL, T2 review-nexus-nyry9.5) for the NEW contract.
        Originally ``test_budget_seconds_silently_bypassed_by_miss_plus_
        retrieval_only_combo`` (substantive-critic SIGNIFICANT #1, T2
        substantive-critique-nexus-h33x8.6-a4-a2-2026-08-19; UPDATED by
        nexus-nyry9.2 / RDR-196 .r2), which pinned that a plan-miss
        combined with a grown plan shaped like the (now-deleted) third
        classify_plan bucket ran completely unbounded by
        ``budget_seconds`` -- deliberate at the time, because that
        bucket carried its own deadline exemption. The bucket and its
        exemption are deleted (census: zero of 17 real shipped builtin
        plans ever classified that way via a real plan_match()), so
        this exact combo must now behave like any other miss-plus-
        matched-plan run: a real deadline reaches plan_run. This test's
        mocked planner still returns instantly (no sleep) and the mock
        plan_run still returns success text with no budget already
        exhausted at the pre-Step-4 check, so the run still completes
        with plain success text and no marker -- what changed is
        whether plan_run's ``deadline`` kwarg is ``None``.
        """
        import time
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        grown_match = _make_match(
            plan_id=0, confidence=None,
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "query", "args": {"question": "$intent"}},
                ],
            }),
        )
        run_result = PlanResult(steps=[{"text": "a normal completed answer"}])
        captured: dict = {}

        async def _spy(match, bindings, **kwargs):
            captured.update(kwargs)
            return run_result

        async def fake_miss(question, scope="", max_steps=6, **kwargs):
            return grown_match

        before = time.monotonic()
        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),  # miss
            patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(side_effect=fake_miss)),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", budget_seconds=1.0)
        after = time.monotonic()

        assert isinstance(result, str)
        assert "[budget exhausted" not in result, (
            "the mocked planner/plan_run still complete well inside the "
            "1s budget in this test, so no marker is expected -- this "
            "test is about the deadline KWARG plan_run receives, not "
            "about the budget actually running out"
        )
        deadline = captured.get("deadline")
        assert deadline is not None, (
            "a miss-path-grown plan shaped like the deleted bucket must "
            "now receive a real deadline like any other plan -- the "
            "deleted exemption must not resurface for this combo"
        )
        assert before + 1.0 <= deadline <= after + 1.0

    @pytest.mark.asyncio
    async def test_non_budget_run_structured_envelope_field_is_none(self, tmp_path):
        """A normal (non-budget) structured run must carry the new key
        with value None — envelope shape is stable and discoverable
        without special-casing 'is the key present'."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"output": "normal answer"}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", structured=True)

        assert result["budget_exhausted_at_step"] is None

    @pytest.mark.asyncio
    async def test_budget_exhausted_run_records_outcome_as_failure(self, tmp_path):
        """A budget-exhausted run did not complete — per the nexus-yg49g
        doctrine (binary success/failure counters) it must record as a
        failure, not a success, so a chronically-timing-out plan does
        not accrue a false success rate."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75, plan_id=42)
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1], "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            total_planned_steps=2,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
            patch("nexus.mcp.core._nx_answer_record_outcome") as record_outcome,
        ):
            from nexus.mcp.core import nx_answer
            await nx_answer("q", budget_seconds=20.0)

        record_outcome.assert_called_once_with(42, success=False)


# ── RDR-196 .p3c (nexus-nyry9.21): USD budget enforcement ───────────────────
#
# D1: BUDGET_ENFORCEMENT_ENABLED (nexus.plans.budget_default) gates ALL of
# the below -- an explicit caller budget_usd and the derived default alike.
# D2: mid-run stop is checked BEFORE dispatching each segment (a stop-line,
# not a hard ceiling -- see plan_run's own docstring). D3: the USD budget
# REUSES the exact marker budget_seconds already produces, with "(cost)" in
# place of "(time)" -- one emitter, never a second shape. D4: the pre-flight
# PRICE CHECK on an over-estimate is a WARNING (round 2, Sam's decision on
# the critic's CRITICAL, T2 p3c-critique-2026-08-21: the step-shape
# estimator has no per-plan discriminating power in the live population, so
# a hard refusal was a step function that refused real runs deterministically
# and wrongly); a genuinely unpriceable plan ALSO warns and runs, never
# refuses -- both kinds share ONE emitter (_emit_budget_warning). D5: the
# inline planner's own dispatch cost is seeded into the running spend before
# Step 4. Round 2 also added an "unknown-cost" warning kind through the same
# emitter (critic Significant 1): a step with no captured cost_usd is a
# blind spot for the mid-run stop-line and must say so, once per run.


class TestNxAnswerBudgetUsdEnforcement:

    @pytest.mark.asyncio
    async def test_over_cap_warns_and_runs_zero_planner_spend(self, tmp_path):
        """A plan whose predicted cost exceeds the cap must WARN and
        still RUN (never refuse) -- both numbers (estimate, cap) in
        the warning text, both shapes, and plan_run genuinely called."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)  # search + operator_summarize
        run_result = PlanResult(steps=[{"text": "the real answer"}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            # operator_summarize prices at the $0.23 static fallback
            # (empty telemetry in this fixture's fresh T2Database) --
            # comfortably above a $0.05 cap. Zero planner spend (a
            # plan-match HIT never dispatches the inline planner), so
            # remaining == the full cap here -- the nonzero-spend
            # arithmetic case is covered separately below.
            text_result = await nx_answer("q", budget_usd=0.05)
            struct_result = await nx_answer("q", budget_usd=0.05, structured=True)

        assert isinstance(text_result, str)
        assert "Refused" not in text_result, "over-cap must WARN, never refuse"
        assert "budget warning (over-cap)" in text_result
        assert "0.2300" in text_result, f"estimate not in warning text: {text_result!r}"
        assert "0.0500" in text_result, f"cap/remaining not in warning text: {text_result!r}"
        assert "the real answer" in text_result, (
            "the plan must still RUN and its real answer must still "
            "surface -- a warning is not a refusal"
        )

        assert isinstance(struct_result, dict)
        over_cap_entries = [
            w for w in struct_result["budget_warnings"] if w["kind"] == "over-cap"
        ]
        assert len(over_cap_entries) == 1
        assert "the real answer" in struct_result["final_text"]

    @pytest.mark.asyncio
    async def test_over_cap_warning_names_remaining_cap_and_spent_when_nonzero(
        self, tmp_path,
    ):
        """code-review Medium 1 (T2 p3c-code-review-2026-08-21): the
        warning must name the REMAINING budget (the actual comparison
        operand), the FULL cap, and the already-spent amount -- not
        label the full cap as what was compared against, which reads
        as numerically false whenever D5's planner spend is nonzero.
        Uses the plan-miss path so the inline planner's own dispatch
        cost (captured via the ambient sink) is genuinely nonzero."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.operators.dispatch import DispatchUsage
        from nexus.plans.runner import PlanResult

        # 3 NON-contiguous operator steps (interleaved with retrieval
        # steps so they never bundle) -> estimate = 3 x $0.23 = $0.69,
        # via the same empty-telemetry static-fallback pricing every
        # other test in this class relies on.
        grown_match = _make_match(
            plan_id=0, confidence=None,
            plan_json=json.dumps({"steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "extract", "args": {"fields": "a", "inputs": "[]"}},
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "rank", "args": {"items": "[]", "criterion": "x"}},
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"content": "x"}},
            ]}),
        )
        run_result = PlanResult(steps=[{"text": "the real answer"}])

        async def fake_plan_miss_direct(question, scope="", max_steps=6, **kwargs):
            import nexus.operators.dispatch as _dispatch_mod

            sink = _dispatch_mod._ambient_usage_sink.get()
            assert sink is not None
            sink.append(DispatchUsage(
                model="claude-opus-5", cost_usd=0.50, input_tokens=100,
                output_tokens=50, cache_creation_input_tokens=0,
                cache_read_input_tokens=0, duration_ms=1000,
                duration_api_ms=900, num_turns=1,
            ))
            return grown_match

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),  # forced miss
            patch("nexus.mcp.core._nx_answer_plan_miss",
                  AsyncMock(side_effect=fake_plan_miss_direct)),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            # cap $1.0530 (derived default), planner spend $0.50 ->
            # remaining $0.5530; estimate $0.69 > remaining, but
            # $0.69 < the full cap $1.0530 -- exactly the scenario the
            # prior text got numerically backwards.
            result = await nx_answer("q")

        assert isinstance(result, str)
        assert "Refused" not in result
        assert "the real answer" in result, "must still run, not refuse"
        assert "0.6900" in result, f"estimate missing: {result!r}"
        assert "0.5530" in result, f"remaining (the real comparison operand) missing: {result!r}"
        assert "1.0530" in result, f"full cap missing: {result!r}"
        assert "0.5000" in result, f"already-spent amount missing: {result!r}"

    @pytest.mark.asyncio
    async def test_mid_run_stop_emits_both_shapes_cost_kind(self, tmp_path):
        """A PlanResult reporting a cost-axis exhaustion must produce
        the SAME marker budget_seconds produces, with (cost) in place
        of (time) -- text-mode leading line AND the structured
        budget_exhausted_at_step field, per D3's one-emitter rule."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1],
                    "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            budget_exhausted_kind="cost",
            total_planned_steps=2,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q")
            struct_result = await nx_answer("q", structured=True)

        assert isinstance(text_result, str)
        assert "[budget exhausted (cost) after step 2 of 2 — partial answer]" in text_result

        assert isinstance(struct_result, dict)
        assert struct_result["budget_exhausted_at_step"] == 2
        assert "[budget exhausted (cost) after step 2 of 2" in struct_result["final_text"]

    @pytest.mark.asyncio
    async def test_under_budget_completion_emits_no_marker(self, tmp_path):
        """False-positive guard: a normal completion well under the
        cap must carry no exhaustion marker of any kind."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"text": "a normal completed answer"}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q")

        assert isinstance(result, str)
        assert "[budget exhausted" not in result
        assert "Refused" not in result
        assert "[budget warning" not in result, (
            "a normal completion well under the cap, fully priced and "
            "fully measured, must carry no warning of any kind either"
        )

    @pytest.mark.asyncio
    async def test_no_estimate_path_warns_and_runs(self, tmp_path):
        """A plan naming a tool the price table has never heard of is
        UNPRICEABLE -- estimate.usd is None -- and must WARN, never
        refuse: the run still completes with the normal answer text
        plus a leading warning line / structured field."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        unpriceable_match = _make_match(
            confidence=0.75,
            plan_json=json.dumps({
                "steps": [{"tool": "totally_unknown_tool_xyz", "args": {}}],
            }),
        )
        run_result = PlanResult(steps=[{"text": "an actual answer"}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[unpriceable_match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q")
            struct_result = await nx_answer("q", structured=True)

        assert isinstance(text_result, str)
        assert "budget warning (no-estimate)" in text_result
        assert "an actual answer" in text_result, (
            "the plan must still RUN and its real answer must still "
            "surface -- a warning is not a refusal"
        )

        assert isinstance(struct_result, dict)
        no_estimate_entries = [
            w for w in struct_result["budget_warnings"] if w["kind"] == "no-estimate"
        ]
        assert len(no_estimate_entries) == 1
        assert "an actual answer" in struct_result["final_text"]

    @pytest.mark.asyncio
    async def test_inline_planner_cost_included_in_running_sum(self, tmp_path):
        """The inline planner's own claude_dispatch cost, captured via
        the ambient usage sink, must be seeded into the running spend
        BEFORE Step 4 -- a planner dispatch alone big enough to exhaust
        the whole cap must stop the run via the pre-plan cost sentinel,
        WITHOUT plan_run ever being called, mirroring the shipped
        budget_seconds pre-plan-exhaustion contract on the cost axis."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.operators.dispatch import DispatchUsage

        grown_match = _make_match(
            plan_id=0, confidence=None,
            plan_json=json.dumps({"steps": [{"tool": "search", "args": {"query": "$intent"}}]}),
        )

        # Simulate a real claude_dispatch call landing its usage in
        # whatever ambient sink nx_answer wraps around the plan-miss
        # dispatch -- exactly what the real inline planner's own
        # claude_dispatch call does internally.
        async def fake_plan_miss_direct(question, scope="", max_steps=6, **kwargs):
            import nexus.operators.dispatch as _dispatch_mod

            sink = _dispatch_mod._ambient_usage_sink.get()
            assert sink is not None, (
                "nx_answer must wrap the plan-miss dispatch in "
                "ambient_usage_sink for D5 to have anything to capture"
            )
            sink.append(DispatchUsage(
                model="claude-opus-5", cost_usd=2.00, input_tokens=100,
                output_tokens=50, cache_creation_input_tokens=0,
                cache_read_input_tokens=0, duration_ms=1000,
                duration_api_ms=900, num_turns=1,
            ))
            return grown_match

        plan_run_mock = AsyncMock()

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[]),  # forced miss
            patch("nexus.mcp.core._nx_answer_plan_miss",
                  AsyncMock(side_effect=fake_plan_miss_direct)),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", plan_run_mock),
        ):
            from nexus.mcp.core import nx_answer
            # $2.00 planner spend alone exceeds the derived $1.0530 cap.
            result = await nx_answer("q")

        plan_run_mock.assert_not_called()
        assert isinstance(result, str)
        from nexus.mcp.core import _NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN
        assert (
            f"[budget exhausted (cost) after step {_NX_ANSWER_BUDGET_EXHAUSTED_PRE_PLAN} "
            "of 1"
        ) in result

    @pytest.mark.asyncio
    async def test_budget_usd_non_positive_is_loud_error(self, tmp_path):
        """budget_usd <= 0 must be a loud bounds error, before any
        dispatch -- 0 must never mean 'unlimited'."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner

        match = _make_match(confidence=0.75)
        plan_match_mock = MagicMock(return_value=[match])
        plan_run_mock = AsyncMock()

        with (
            patch("nexus.plans.matcher.plan_match", plan_match_mock),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", plan_run_mock),
        ):
            from nexus.mcp.core import nx_answer
            zero_result = await nx_answer("q", budget_usd=0)
            negative_result = await nx_answer("q", budget_usd=-1.5)

        plan_match_mock.assert_not_called()
        plan_run_mock.assert_not_called()
        assert "budget_usd must be > 0" in zero_result
        assert "budget_usd must be > 0" in negative_result

    @pytest.mark.asyncio
    async def test_enforcement_off_restores_pre_p3c_behavior(self, tmp_path, monkeypatch):
        """Flipping BUDGET_ENFORCEMENT_ENABLED back False must restore
        pre-.p3c behavior on every path this class exercises: a
        non-positive budget_usd no longer errors, an over-estimate
        no longer refuses, and plan_run never receives
        budget_usd_remaining."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        import nexus.plans.budget_default as _bd
        from nexus.plans.runner import PlanResult

        monkeypatch.setattr(_bd, "BUDGET_ENFORCEMENT_ENABLED", False)

        match = _make_match(confidence=0.75)  # would price at $0.23
        run_result = PlanResult(steps=[{"text": "a normal completed answer"}])
        captured: dict = {}

        async def _spy(match, bindings, **kwargs):
            captured.update(kwargs)
            return run_result

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=_spy)),
        ):
            from nexus.mcp.core import nx_answer
            # A cap far below the plan's real static-fallback estimate
            # ($0.23) AND non-positive -- neither must matter when
            # enforcement is off.
            zero_result = await nx_answer("q", budget_usd=0)
            low_result = await nx_answer("q", budget_usd=0.001)

        assert "budget_usd must be > 0" not in zero_result
        assert "Refused" not in low_result
        assert "budget_usd_remaining" not in captured, (
            "plan_run must not receive budget_usd_remaining when "
            "enforcement is off"
        )
        assert "a normal completed answer" in low_result

    @pytest.mark.asyncio
    async def test_unknown_cost_steps_surface_the_blind_spot_warning(self, tmp_path):
        """critic Significant 1 (T2 p3c-critique-2026-08-21): a run
        whose executed steps carry no cost_usd is a blind spot for the
        mid-run stop-line -- ``test_budget_usd_remaining_unknown_cost_
        steps_never_trip`` (tests/test_plan_run.py) already proves the
        blind spot EXISTS at the runner level (dispatch continues,
        cost_usd stays None, never fabricated to 0); this test proves
        the SIGNAL now fires at the nx_answer level, once per run, in
        both shapes, without turning into a refusal or an exhaustion
        marker.

        code-review Medium (round 3, T2 p3c-code-review round 3): a
        SINGLE unknown-cost record among two cannot distinguish a
        correct single-emit implementation from a hypothetical
        per-step-loop regression -- both would produce the identical
        "fires once" observation. Uses THREE unknown-cost records among
        FIVE so a per-step-loop bug (which would fire 3 times, or name
        the wrong count) is genuinely falsifiable: asserts the warning
        substring's COUNT is exactly 1 (never once per step) AND that
        it names the real "3 of 5", not "1 of 5" or "1 of 2"."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult, StepRecord

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"text": "an answer despite unknown cost"}],
            step_records=[
                StepRecord(step_index=0, operator="search", source="sql",
                           cost_usd=0.0, ok=True),
                StepRecord(step_index=1, operator="extract", source="llm",
                           cost_usd=None, ok=True),
                StepRecord(step_index=2, operator="rank", source="llm",
                           cost_usd=None, ok=True),
                StepRecord(step_index=3, operator="filter", source="sql",
                           cost_usd=0.0, ok=True),
                StepRecord(step_index=4, operator="summarize", source="llm",
                           cost_usd=None, ok=True),
            ],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q")
            struct_result = await nx_answer("q", structured=True)

        assert isinstance(text_result, str)
        assert text_result.count("budget warning (unknown-cost)") == 1, (
            f"must fire exactly ONCE per run, never once per unknown-cost "
            f"step: {text_result!r}"
        )
        assert "3 of 5" in text_result, (
            f"expected the real per-run count of unknown-cost steps "
            f"(3 of the 5 executed): {text_result!r}"
        )
        assert "an answer despite unknown cost" in text_result
        assert "[budget exhausted" not in text_result, (
            "an unknown-cost step is a coverage gap, not itself an "
            "exhaustion -- must not fabricate a stop"
        )
        assert "Refused" not in text_result

        assert isinstance(struct_result, dict)
        assert struct_result["budget_exhausted_at_step"] is None
        assert "an answer despite unknown cost" in struct_result["final_text"]

        # ITEM B (round 3, critic Significant) / round 4 (the
        # machine-readable field is the ONLY structured shape now --
        # the redundant joined-prose `budget_estimate_warning` field
        # was deleted, never having shipped). Exactly one
        # "unknown-cost" entry, with the real per-run count in its
        # detail text -- derived from the SAME accumulator as the
        # human leading text line asserted above, not a second shape.
        unknown_cost_entries = [
            w for w in struct_result["budget_warnings"] if w["kind"] == "unknown-cost"
        ]
        assert len(unknown_cost_entries) == 1
        assert "3 of 5" in unknown_cost_entries[0]["detail"]

    @pytest.mark.asyncio
    async def test_fully_measured_steps_never_trigger_unknown_cost_warning(
        self, tmp_path,
    ):
        """False-positive guard for the new signal: every step
        carrying a real (even zero) cost_usd must never emit the
        unknown-cost warning."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult, StepRecord

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"text": "a fully measured answer"}],
            step_records=[
                StepRecord(step_index=0, operator="search", source="sql",
                           cost_usd=0.0, ok=True),
                StepRecord(step_index=1, operator="summarize", source="llm",
                           cost_usd=0.05, ok=True),
            ],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q")

        assert "budget warning (unknown-cost)" not in result

    @pytest.mark.asyncio
    async def test_warning_and_exhaustion_marker_co_occur_marker_leads(
        self, tmp_path,
    ):
        """code review Medium (round 3, T2 p3c-code-review round 3):
        a warning co-occurring with an exhaustion marker is materially
        more likely now that a warned run PROCEEDS to execute instead
        of being refused. Construct a run carrying BOTH an unknown-cost
        step AND a set budget_exhausted_at_step; both the warning line
        and the exhaustion marker must reach a structured=False caller
        in the SAME output, with the marker still LEADING -- this is
        load-bearing, not cosmetic: commands/answer_runs.py's
        `_row_is_failed` keys on
        ``final_text.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_
        PREFIX)``, which an unconditional warning-first prepend would
        silently break the moment the two co-occur."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult, StepRecord

        match = _make_match(confidence=0.75)  # 2-step plan: search, operator_summarize
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1],
                    "collections": ["knowledge"]}],
            budget_exhausted_at_step=2,
            budget_exhausted_kind="cost",
            total_planned_steps=2,
            step_records=[
                StepRecord(step_index=0, operator="search", source="sql",
                           cost_usd=0.0, ok=True),
                StepRecord(step_index=1, operator="summarize", source="llm",
                           cost_usd=None, ok=False),
            ],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX, nx_answer
            result = await nx_answer("q")

        assert isinstance(result, str)
        marker_idx = result.find("[budget exhausted (cost) after step 2 of 2")
        warning_idx = result.find("[budget warning (unknown-cost)")
        assert marker_idx != -1, f"exhaustion marker missing: {result!r}"
        assert warning_idx != -1, f"unknown-cost warning missing: {result!r}"
        assert marker_idx < warning_idx, (
            f"the exhaustion marker must LEAD the warning, not the other "
            f"way around: {result!r}"
        )
        assert result.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX), (
            "commands/answer_runs.py's _row_is_failed depends on this "
            "startswith contract -- a warning co-occurring with an "
            "exhaustion marker must never break it"
        )

    @pytest.mark.asyncio
    async def test_exhausted_warned_and_oversized_keeps_warning_and_marker(
        self, tmp_path, monkeypatch,
    ):
        """nexus-2xjge fold-in (critique [23267] Significant 2): with the
        result-size cap in play, the triple co-occurrence (exhaustion
        marker + warning + oversized final text) must keep BOTH signals:
        the cap is applied to the raw text BEFORE warning composition,
        so the end-appended warning can never be sliced off. The marker
        still leads; the cap marker sits inside, before the warning."""
        import nexus.mcp.core as _core
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult, StepRecord

        monkeypatch.setattr(_core, "_TEXT_RESULT_CAP_CHARS", 2_000)
        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"ids": ["a"], "tumblers": [], "distances": [0.1],
                    "collections": ["knowledge"]},
                   {"partial_text": "x" * 10_000}],
            budget_exhausted_at_step=2,
            budget_exhausted_kind="cost",
            total_planned_steps=2,
            step_records=[
                StepRecord(step_index=0, operator="search", source="sql",
                           cost_usd=0.0, ok=True),
                StepRecord(step_index=1, operator="summarize", source="llm",
                           cost_usd=None, ok=False),
            ],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX, nx_answer
            result = await nx_answer("q")

        assert isinstance(result, str)
        assert result.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX)
        warning_idx = result.find("[budget warning (unknown-cost)")
        assert warning_idx != -1, (
            f"warning sliced off by the result-size cap: ...{result[-200:]!r}"
        )
        cap_idx = result.find("result capped at")
        assert cap_idx != -1, f"cap marker missing: ...{result[-200:]!r}"
        assert cap_idx < warning_idx, (
            "cap must be applied to the raw text BEFORE the warning is "
            "composed, so the warning survives capping"
        )


class TestNxAnswerDroppedReduceStepsEnvelope:
    """nexus-4h0oh follow-up (code-review T2 [24199]):
    ``PlanResult.dropped_reduce_steps`` never reached the ``nx_answer``
    caller. Thread it into the structured envelope the same way
    ``budget_warnings`` is threaded (a closure-scoped accumulator
    ``_result`` reads directly, never a per-call-site parameter) --
    always present, ``[]`` when empty -- and into the text-mode return
    as a one-line notice when non-empty."""

    @pytest.mark.asyncio
    async def test_dropped_reduce_steps_surfaces_in_structured_envelope(
        self, tmp_path,
    ):
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"text": "final answer"}],
            dropped_reduce_steps=[2, 4],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q")
            struct_result = await nx_answer("q", structured=True)

        assert isinstance(struct_result, dict)
        assert struct_result["dropped_reduce_steps"] == [2, 4]
        assert isinstance(text_result, str)
        assert "2" in text_result and "4" in text_result
        assert "final answer" in text_result, (
            "the notice must not replace the real answer, only precede it"
        )

    @pytest.mark.asyncio
    async def test_no_dropped_reduce_steps_yields_empty_list_and_no_notice(
        self, tmp_path,
    ):
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(steps=[{"text": "final answer"}])

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q")
            struct_result = await nx_answer("q", structured=True)

        assert struct_result["dropped_reduce_steps"] == []
        assert "dropped reduce" not in text_result.lower()
        assert text_result == "final answer"

    @pytest.mark.asyncio
    async def test_dropped_reduce_steps_coexists_with_budget_warning(
        self, tmp_path,
    ):
        """Both notices must survive together -- the marker-convergence
        rule that governs budget_warnings/exhaustion co-occurrence must
        extend to this new line, not silently drop one when both fire."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult

        match = _make_match(confidence=0.75)
        run_result = PlanResult(
            steps=[{"text": "the real answer"}],
            dropped_reduce_steps=[2],
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            text_result = await nx_answer("q", budget_usd=0.05)
            struct_result = await nx_answer(
                "q", budget_usd=0.05, structured=True,
            )

        assert "budget warning (over-cap)" in text_result
        assert "2" in text_result
        assert "the real answer" in text_result
        assert struct_result["dropped_reduce_steps"] == [2]
        over_cap_entries = [
            w for w in struct_result["budget_warnings"] if w["kind"] == "over-cap"
        ]
        assert len(over_cap_entries) == 1


class TestNxAnswerClassifyPlanPrefixedOperatorNames:
    """nexus-h33x8.6 a4 fold-in (found in passing, dev notes
    nx-answer-a3-a1-dev-notes-2026-08-19): ``_nx_answer_classify_plan``
    used a bare-name-only operator set (``_OPERATOR_TOOL_MAP`` keys),
    narrower than the authoritative ``nexus.plans.bundle.is_operator_tool``
    (bare AND ``operator_``-prefixed forms both accepted, per that
    module's own docstring: 'plan YAMLs use either'). A plan step written
    as ``operator_summarize`` (rather than bare ``summarize``) was
    misclassified into the deleted retrieval-only bucket (see
    nexus-nyry9.5, RDR-196 .r5) even though it genuinely needs a
    claude -p dispatch."""

    def test_prefixed_operator_tool_name_classifies_as_needs_operators(self):
        from nexus.mcp.core import _nx_answer_classify_plan
        match = _make_match(
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "operator_summarize", "args": {"content": "$step1.ids"}},
                ],
            }),
        )
        assert _nx_answer_classify_plan(match) == "needs_operators"

    def test_bare_operator_tool_name_still_classifies_as_needs_operators(self):
        """Regression: the bare-name path must keep working."""
        from nexus.mcp.core import _nx_answer_classify_plan
        assert _nx_answer_classify_plan(_make_multi_step_match()) == "needs_operators"

    def test_multi_step_plan_with_zero_operator_steps_classifies_as_needs_operators(self):
        """nexus-nyry9.5 (RDR-196 .r5 review-fix): the third
        classify_plan bucket a plan like this used to land in (no
        operator steps, exempt from the budget deadline) was deleted --
        a census of all 17 shipped builtin plans found zero that ever
        classified that way via a real plan_match(). A multi-step plan
        with zero operator steps (bare OR prefixed tool names) now
        classifies the same as any other non-single_query plan:
        needs_operators, budget-bound like everything else. (Whether it
        structurally contains an operator step is a SEPARATE question
        answered by ``_nx_answer_needs_operators``, which this diff
        decoupled from ``_nx_answer_classify_plan`` precisely so this
        distinction stays testable without resurrecting the deleted
        deadline exemption.)"""
        from nexus.mcp.core import _nx_answer_classify_plan
        match = _make_match(
            plan_json=json.dumps({
                "steps": [
                    {"tool": "search", "args": {"query": "$intent"}},
                    {"tool": "query", "args": {"question": "$intent"}},
                ],
            }),
        )
        assert _nx_answer_classify_plan(match) == "needs_operators"


# ── RDR-196 .p1e: structured-envelope steps/cost_usd ────────────────────────


class TestStructuredEnvelopeStepBreakdown:
    """nexus-nyry9.11 (RDR-196 .p1e): ``structured=True`` must surface a
    ``steps`` list (same field names as the wire — ``_step_record_to_wire``)
    and a ``cost_usd`` field (sum of known step costs, or None) alongside
    the existing envelope fields."""

    @pytest.mark.asyncio
    async def test_success_envelope_carries_steps_and_summed_cost(self, tmp_path):
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanResult, StepRecord

        match = _make_match(confidence=0.75)
        records = [
            StepRecord(
                step_index=0, operator="search", source="sql",
                model=None, input_tokens=0, output_tokens=0,
                cost_usd=0.0, elapsed_ms=50, ok=True,
            ),
            StepRecord(
                step_index=1, operator="summarize", source="llm",
                model="claude-sonnet-5-20260101", input_tokens=100,
                output_tokens=50, cost_usd=0.02, elapsed_ms=1200, ok=True,
            ),
        ]
        run_result = PlanResult(
            steps=[{"text": "The final answer."}],
            step_records=records,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(return_value=run_result)),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", structured=True)

        assert result["cost_usd"] == pytest.approx(0.02)
        assert len(result["steps"]) == 2
        assert result["steps"][0] == {
            "step_index": 0, "operator": "search", "source": "sql",
            "model": None, "input_tokens": 0, "output_tokens": 0,
            # nexus-ndoke: a "sql" step runs no prompt, so it has no cache
            # dimension at all. None, never 0 — a 0 here would be a claim that
            # this step used no cached input, which it never measured.
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "cost_usd": 0.0, "elapsed_ms": 50, "ok": True, "bundled_steps": [],
        }
        assert result["steps"][1]["operator"] == "summarize"
        assert result["steps"][1]["model"] == "claude-sonnet-5-20260101"

    @pytest.mark.asyncio
    async def test_no_steps_executed_envelope_carries_empty_list_and_none_cost(
        self, tmp_path,
    ):
        """The single-step fast path (and any pre-plan_run error/miss)
        bypasses plan_run entirely — no StepRecord is ever produced.
        ``steps`` must be ``[]`` and ``cost_usd`` must be ``None`` (never
        a fabricated ``0.0``), matching ``_nx_answer_record_run``'s own
        blanket rule."""
        import nexus.mcp_infra as _infra
        from nexus.plans.match import Match

        match = Match(
            plan_id=1, name="single-query-plan", description="test",
            confidence=0.75, dimensions={}, tags="",
            plan_json=json.dumps({
                "steps": [{"tool": "query", "args": {"question": "$intent", "corpus": "knowledge"}}],
            }),
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch("nexus.mcp.core.query", return_value={
                "ids": [], "collections": [], "chunk_text_hash": [], "distances": [],
            }),
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", structured=True)

        assert result["steps"] == []
        assert result["cost_usd"] is None

    @pytest.mark.asyncio
    async def test_plan_run_failure_records_and_returns_partial_step_records(
        self, tmp_path,
    ):
        """RDR-196 .p1d critique fold (T2 [23092]): a plan_run exception
        that carries partial step_records (runner.py's outer try/except)
        must be both (a) recorded to telemetry with the real step data
        (not a hardcoded []), and (b) reflected in the structured
        envelope's own ``steps``/``cost_usd`` fields for this same
        (failed) call."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner
        from nexus.plans.runner import PlanRunStepRefError, StepRecord

        match = _make_match(confidence=0.75)
        partial_record = StepRecord(
            step_index=0, operator="search", source="sql",
            model=None, input_tokens=0, output_tokens=0,
            cost_usd=0.0, elapsed_ms=75, ok=True,
        )
        exc = PlanRunStepRefError(ref="step2", reason="boom")
        exc.step_records = [partial_record]

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run", AsyncMock(side_effect=exc)),
            patch("nexus.mcp.core._nx_answer_record_run") as record_run_spy,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", structured=True)

        assert record_run_spy.called
        recorded = record_run_spy.call_args.kwargs.get("step_records")
        assert recorded == [partial_record]
        assert record_run_spy.call_args.kwargs.get("step_count") == 1

        assert result["cost_usd"] == pytest.approx(0.0)
        assert len(result["steps"]) == 1
        assert result["steps"][0]["operator"] == "search"

    @pytest.mark.asyncio
    async def test_plan_run_failure_without_step_records_attribute_degrades_to_empty(
        self, tmp_path,
    ):
        """A plan_run exception that never went through runner.py's
        attach point (or a test double raising a bare exception) must
        still degrade to an empty list, not crash on ``getattr``."""
        import nexus.mcp_infra as _infra
        import nexus.plans.runner as _runner

        match = _make_match(confidence=0.75)

        with (
            patch("nexus.plans.matcher.plan_match", return_value=[match]),
            patch.object(_infra, "get_t1_plan_cache",
                         return_value=MagicMock(is_available=False)),
            patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx(tmp_path)),
            patch("nexus.mcp.core.scratch", MagicMock()),
            patch.object(_runner, "plan_run",
                         AsyncMock(side_effect=RuntimeError("no attribute here"))),
            patch("nexus.mcp.core._nx_answer_record_run") as record_run_spy,
        ):
            from nexus.mcp.core import nx_answer
            result = await nx_answer("q", structured=True)

        assert record_run_spy.call_args.kwargs.get("step_records") == []
        assert record_run_spy.call_args.kwargs.get("step_count") == 0
        assert result["steps"] == []
        assert result["cost_usd"] is None


class TestNxAnswerLatencyDocstringPinned:
    """nexus-nyry9.11 (RDR-196 .p1e DO item 4): the docstring's measured
    latency figures were 45x-wrong for months (32% under 5s, when the
    real executed-only figure was 0.7%) because nothing pinned them
    against drift.

    SOURCE (review-fix S4, substantive-critic T2 [23111]): T2 [22886]
    (bead nexus-h33x8.6, measured 2026-08-19), n=142 executed-only rows:
    p50 80.1s, p95 217.1s, p99 316.7s, mean 97.7s, 0.7% under 5s, 88.7%
    >= 30s, 33.8% >= 2min — unchanged from the docstring already on
    develop; verified against that recorded measurement, not re-measured
    here.

    TRANSCRIPTION PIN, NOT A REGIME PIN: this test only catches the
    docstring drifting away from the 2026-08-19 pre-Phase-2 (all-strong-
    model) measurement above — it does NOT re-derive the distribution and
    cannot detect the distribution itself going stale. RDR-196 Phase 2
    (per-operator model tier routing, nexus-nyry9.15/.16/.17) will change
    the cost/latency profile these figures describe; when that ships, the
    n=142 numbers become systematically stale while this test keeps
    passing (it only checks the docstring matches ITSELF, not reality).
    Re-derivation-on-Phase-2-flip is tracked as a forward-pointer on
    nexus-nyry9.17 (`bd comments add nexus-nyry9.17`, RDR-196 .p2d, sets
    the FLIP/HOLD/UNDECIDED per-operator gate) — that bead is where a
    default actually flips, so it is the natural trigger to re-run this
    measurement and update both the docstring and this pin together.
    """

    def test_docstring_pins_the_measured_executed_only_figures(self):
        from nexus.mcp.core import nx_answer

        # The docstring line-wraps at ~79 chars, so a figure can straddle
        # a "\n    " boundary (e.g. "p95\n    217.1s") — normalize
        # whitespace before matching rather than pinning to the exact
        # wrap points, which would make this test as brittle as the
        # drift it exists to catch.
        doc = " ".join((nx_answer.__doc__ or "").split())
        for needle in (
            "n=142", "p50 80.1s", "p95 217.1s", "p99 316.7s", "mean 97.7s",
            "0.7% finish under 5s", "88.7% take >= 30s", "33.8% take >= 2min",
        ):
            assert needle in doc, (
                f"nx_answer docstring drifted -- expected {needle!r} in the "
                "latency paragraph; re-derive from `nx answer-runs --json` "
                "(executed-only rows) and update both the docstring and "
                "this pin together"
            )


# ── RDR-200 Phase 1a: continuation parameter (nexus-4e75w.3) ────────────────


class TestContinuationParameter:
    """``continuation: bool | None`` is threaded and validated, but in
    Phase 1a it changes NOTHING about the return value: None/False/True
    all fall through to the SAME headless plan_run call. True additionally
    classifies the continuation cut and logs the decision via structlog —
    a side effect, never a behaviour change (nexus-4e75w.3 acceptance
    criteria)."""

    @staticmethod
    def _patches(plan_run_result):
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
        return (
            patch("nexus.plans.matcher.plan_match",
                  return_value=[_make_multi_step_match()]),
            patch("nexus.plans.runner.plan_run",
                  AsyncMock(return_value=plan_run_result)),
            patch("nexus.mcp.core._t2_ctx"),
            patch("nexus.mcp.core.scratch", return_value="ok"),
            patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None),
            # nexus-m20mf P2 (code review [24580] audit): _make_multi_step_
            # match() carries plan_id=1 (truthy), so run_start/run_outcome/
            # record_run -- all now routed through _t2_index_write -- fire
            # for real on every caller of this helper. Route them to the
            # SAME db_stub so this helper's isolation is not silently
            # bypassed for the sites nexus-m20mf converted.
            patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)),
        ), db_stub

    @pytest.mark.asyncio
    @pytest.mark.parametrize("continuation_value", [None, False, True])
    async def test_return_value_byte_identical_across_continuation_values(
        self, continuation_value,
    ):
        """Phase 1a builds no envelope — the returned text/steps must not
        depend on ``continuation`` at all.

        Go-live pinned False explicitly (CR-Important-3, T2 [23951],
        code-review-expert falsified this test live): with
        `_CONTINUATION_GO_LIVE` True by default post-nexus-4e75w.5 and
        `plan_run` mocked to return the SAME `plan_run_result` regardless
        of kwargs, the `True` parametrization would otherwise silently
        take the FULL engaged+fallback path (assembly raises on the
        missing `ids` field, caught, `plan_run` called a SECOND time,
        Step 5 extracts "ok" from the same mock) and pass only because
        the mock ignores call count — proving nothing about Phase 1a's
        actual "no envelope, no behavior change" claim. Pinning the gate
        False here restores that claim; the engaged path has its own
        dedicated tests in TestContinuationGoLive."""
        from nexus.mcp.core import nx_answer

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]
        plan_run_result.step_records = []
        plan_run_result.budget_exhausted_at_step = None

        patches, db_stub = self._patches(plan_run_result)
        with patches[0], patches[1], patches[2] as t2_ctx, patches[3], patches[4], \
             patches[5], \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", False):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(
                question="q", continuation=continuation_value,
            )

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_rejects_non_bool_continuation(self):
        """Same fail-loud-before-dispatch discipline as min_confidence's
        bounds check immediately above it."""
        from nexus.mcp.core import nx_answer

        match_called = MagicMock()

        def fake_match(question, **kwargs):
            match_called()
            return []

        with patch("nexus.plans.matcher.plan_match", side_effect=fake_match), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
            t2_ctx.return_value.__enter__.return_value = MagicMock()
            result = await nx_answer(question="q", continuation="yes")  # type: ignore[arg-type]

        assert "continuation must be a bool or None" in result
        assert not match_called.called, (
            "plan_match must never be reached with an invalid continuation value"
        )

    @pytest.mark.asyncio
    async def test_continuation_true_logs_cut_decision_when_go_live_off(self):
        """The classifier fires and logs exactly once, with the shape and
        cut index, when the caller opts in — and (go-live explicitly
        pinned False here) the plan_run call underneath is untouched:
        Phase 1a is cut-selection only. RDR-200 (nexus-4e75w.5) makes
        this go-live-DEPENDENT — see TestContinuationGoLive below for the
        engaged (go-live True) behaviour, which legitimately calls
        plan_run a second time on a fallback."""
        import logging

        import structlog
        from structlog.testing import capture_logs

        from nexus.mcp.core import nx_answer

        plan_run_result = MagicMock()
        plan_run_result.steps = [{"text": "ok"}]
        plan_run_result.step_records = []
        plan_run_result.budget_exhausted_at_step = None

        # conftest.py's pytest_configure pins the ambient level to
        # WARNING; the decision log is .info(), so raise the filtering
        # level for this capture the same way
        # test_nx_answer_plan_choice.py's established wiring test does.
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

        patches, db_stub = self._patches(plan_run_result)
        with patches[0], patches[1] as plan_run_mock, patches[2] as t2_ctx, \
             patches[3], patches[4], patches[5], \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", False):
            t2_ctx.return_value.__enter__.return_value = db_stub
            with capture_logs() as cap:
                result = await nx_answer(question="q", continuation=True)

        assert result == "ok"
        assert plan_run_mock.await_count == 1, (
            "Phase 1a must still call plan_run exactly once — no envelope, "
            "no short-circuit"
        )
        cut_events = [e for e in cap if e.get("event") == "nx_answer_continuation_cut"]
        assert len(cut_events) == 1
        # _make_multi_step_match(): search -> extract($step1.ids) — a
        # single trailing operator, Shape B.
        assert cut_events[0]["shape"] == "shape_b"
        assert cut_events[0]["cut_at_step"] == 1
        assert cut_events[0]["operators"] == ["extract"]

    @pytest.mark.asyncio
    async def test_continuation_false_and_none_do_not_log_cut_decision(self):
        """No side effect at all when the caller has not opted in —
        None (policy default) and False (explicit force-headless) are
        indistinguishable in Phase 1a."""
        import logging

        import structlog
        from structlog.testing import capture_logs

        from nexus.mcp.core import nx_answer

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

        for continuation_value in (None, False):
            plan_run_result = MagicMock()
            plan_run_result.steps = [{"text": "ok"}]
            plan_run_result.step_records = []
            plan_run_result.budget_exhausted_at_step = None

            patches, db_stub = self._patches(plan_run_result)
            with patches[0], patches[1], patches[2] as t2_ctx, patches[3], patches[4], \
                 patches[5]:
                t2_ctx.return_value.__enter__.return_value = db_stub
                with capture_logs() as cap:
                    await nx_answer(question="q", continuation=continuation_value)

            cut_events = [
                e for e in cap if e.get("event") == "nx_answer_continuation_cut"
            ]
            assert cut_events == [], (
                f"continuation={continuation_value!r} must not classify or log"
            )


class TestContinuationGoLive:
    """RDR-200 (nexus-4e75w.5) — the engaged (go-live True) path, run
    end to end against the REAL ``plan_run`` (never mocked here) so the
    stop-before-cut mechanism and envelope assembly interact for real,
    not through a stand-in that would hide the interaction. Only the
    underlying ``operator_*``/retrieval MCP tools are stubbed."""

    @pytest.mark.asyncio
    async def test_handoff_row_written_before_envelope_returns(self):
        """The ordering rule (RDR-200 R2): the handoff row is recorded
        BEFORE the envelope is returned, never after. Proven here by
        capturing the telemetry write's call and cross-checking its
        content against what the (already-returned) response carries --
        if the write had not already happened, there would be nothing
        to cross-check against."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import NX_ANSWER_CONTINUATION_MARKER_PREFIX, nx_answer

        extract_calls: list = []

        async def stub_search(**kwargs):
            return {
                "ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                "collections": ["knowledge"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["knowledge"],
            }

        async def stub_extract(**kwargs):
            extract_calls.append(kwargs)
            return {"extractions": []}

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
        recorded_calls: list = []
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded_calls.append(kw)
        )

        with patch("nexus.plans.matcher.plan_match",
                    return_value=[_make_multi_step_match()]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_extract", stub_extract):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q", continuation=True)

        assert extract_calls == [], (
            "the cut suffix (extract) must never dispatch server-side "
            "when it is being handed off"
        )
        assert len(recorded_calls) == 1, (
            "exactly ONE handoff row -- and it must already have been "
            "written by the time nx_answer returns"
        )
        recorded = recorded_calls[0]
        assert recorded["final_text"].startswith(NX_ANSWER_CONTINUATION_MARKER_PREFIX)
        assert recorded["step_count"] == 1, (
            "step_count covers only the server-side-executed prefix "
            "(the search step) -- never the planned total (2)"
        )
        assert recorded["cost_usd"] == pytest.approx(0.0), (
            "an all-SQL prefix is a TRUE, honest zero"
        )
        # Cross-check: the continuation_id embedded in the ALREADY-
        # WRITTEN row is the SAME id the returned instruction names --
        # both derive from the one envelope this call assembled.
        assert isinstance(result, str)
        assert "nx_answer_report" in result
        cid = recorded["final_text"].split("continuation_id=")[1].rstrip("]")
        assert cid in result

    @pytest.mark.asyncio
    async def test_structured_mode_carries_the_continuation_envelope(self):
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import nx_answer

        async def stub_search(**kwargs):
            return {
                "ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                "collections": ["knowledge"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["knowledge"],
            }

        async def stub_extract(**kwargs):
            return {"extractions": []}

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        with patch("nexus.plans.matcher.plan_match",
                    return_value=[_make_multi_step_match()]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_extract", stub_extract):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q", continuation=True, structured=True)

        assert isinstance(result, dict)
        assert result["continuation"] is not None
        assert result["continuation"]["spec_version"] == 1
        assert result["continuation"]["cut_at_step"] == 1
        assert result["step_count"] == 1

    @pytest.mark.asyncio
    async def test_sql_would_hit_falls_back_to_a_normal_headless_answer(self):
        """When the terminal operator's real dispatch would have taken
        the SQL fast path, envelope assembly returns None -- the caller
        must get the SAME normal answer headless always would, via the
        fallback re-run (search dispatches twice; the operator's own
        real SQL path never pays for a claude -p subprocess either
        time)."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import nx_answer
        from nexus.plans.match import Match

        plan = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "filter", "args": {
                    "items": "[{\"id\": \"1\"}]", "criterion": "keep",
                }},
            ],
        })
        match = Match(
            plan_id=1, name="test", description="test", confidence=0.9,
            dimensions={}, tags="", plan_json=plan,
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        search_calls: list = []

        async def stub_search(**kwargs):
            search_calls.append(kwargs)
            return {
                "ids": ["a"], "tumblers": ["1.1"], "distances": [0.1],
                "collections": ["knowledge"], "chunk_text_hash": ["h1"],
                "chunk_collections": ["knowledge"],
            }

        claude_dispatch_calls: list = []

        async def spy_claude_dispatch(prompt, schema, **kwargs):
            claude_dispatch_calls.append(prompt)
            return {"items": [], "rationale": []}

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        import nexus.operators.dispatch as _dispatch_mod

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(_dispatch_mod, "claude_dispatch", spy_claude_dispatch), \
             patch("nexus.operators.aspect_sql.try_filter",
                   lambda *a, **kw: {"items": [], "rationale": []}):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q", continuation=True, structured=True)

        assert search_calls, "the prefix retrieval step must have run"
        assert len(search_calls) == 2, (
            "search re-runs once for the prefix probe and once for the "
            "fallback full run -- the accepted cost of a rare fallback"
        )
        assert claude_dispatch_calls == [], (
            "the SQL-would-hit path never dispatches an LLM subprocess, "
            "in the prefix probe OR the fallback"
        )
        assert result["continuation"] is None, (
            "a fallback is a normal headless answer -- no envelope"
        )

    @pytest.mark.asyncio
    async def test_interleaved_prefix_llm_step_redispatches_on_fallback(self):
        """CR-Important-1 / critic-F1 (T2 [23951]/[23952], both
        reproduced live): an INTERLEAVED prefix (search->extract->search
        ->summarize, the RDR's own worked example) whose `extract` step
        is a REAL, already-paid LLM dispatch. Forcing the fallback path
        (via zero-evidence retrieval) must re-dispatch that SAME extract
        step a second time -- measured directly by counting the tool's
        own dispatches, not assumed -- and the empty-evidence fallback
        log event must report fallback_may_redispatch=True with the
        correct step count, so the contamination is observable post-hoc
        even though it is not prevented (resume-from-prefix is Phase
        2). `extract`/`summarize` are stubbed directly (rather than at
        the subprocess seam) so extract's own dispatch count is never
        conflated with summarize's -- the terminal step only ever
        dispatches on the fallback's full run, a THIRD, unrelated
        dispatch this test must not miscount as a repeated `extract`."""
        import logging

        import structlog
        from structlog.testing import capture_logs

        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import nx_answer
        from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink
        from nexus.plans.match import Match

        plan = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "a"}},
                {"tool": "search", "args": {"query": "y", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "analyze the results"}},
            ],
        })
        match = Match(
            plan_id=1, name="test", description="test", confidence=0.9,
            dimensions={}, tags="", plan_json=plan,
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        # Every search returns zero evidence -- the empty-evidence guard
        # sums evidence ACROSS THE WHOLE PREFIX (proven by
        # test_partial_evidence_across_multiple_retrieval_steps_is_not_
        # flagged in test_continuation_envelope.py: one non-empty step
        # alongside an empty one does NOT trip it), so both retrieval
        # steps must come back empty for this test's fallback to
        # actually fire.
        async def stub_search(**kwargs):
            return {"ids": [], "tumblers": [], "distances": [], "collections": []}

        extract_dispatch_count = {"n": 0}

        async def stub_extract(**kwargs):
            extract_dispatch_count["n"] += 1
            # Manually append a real DispatchUsage onto the ambient sink
            # the runner scopes around this dispatch -- exactly what the
            # real claude_dispatch call this stub replaces would do, so
            # the resulting StepRecord carries a genuine non-None
            # cost_usd/source="llm" (the signal
            # _prefix_may_redispatch/_prefix_redispatch_step_count key
            # on) rather than the "no usage captured" default an
            # ordinary mock would leave.
            sink = _ambient_usage_sink.get()
            if sink is not None:
                sink.append(DispatchUsage(
                    model="claude-sonnet-5-20260101", cost_usd=0.03,
                    input_tokens=5, output_tokens=2,
                    cache_creation_input_tokens=0, cache_read_input_tokens=0,
                    duration_ms=100, duration_api_ms=90, num_turns=1,
                ))
            return {"extractions": []}

        summarize_dispatch_count = {"n": 0}

        async def stub_summarize(**kwargs):
            summarize_dispatch_count["n"] += 1
            return {"summary": "a normal fallback answer"}

        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})

        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_extract", stub_extract), \
             patch.object(mcp_core, "operator_summarize", stub_summarize):
            t2_ctx.return_value.__enter__.return_value = db_stub
            with capture_logs() as cap:
                result = await nx_answer(question="q", continuation=True, structured=True)

        assert extract_dispatch_count["n"] == 2, (
            "extract is a real, already-paid LLM step in the prefix -- "
            "the fallback re-run pays for it a SECOND time; this IS the "
            "double-cost the fold documents, not fixed here"
        )
        assert summarize_dispatch_count["n"] == 1, (
            "the terminal step only ever dispatches on the fallback's "
            "full run -- never on the prefix-only attempt (stop-before-"
            "cut) and never twice"
        )
        assert result["continuation"] is None, "the fallback is a normal answer"

        events = [
            e for e in cap
            if e.get("event") == "continuation_empty_evidence_fallback_to_headless"
        ]
        assert len(events) == 1
        assert events[0]["fallback_may_redispatch"] is True, (
            "the prefix's real extract StepRecord must be visible to "
            "the fallback-decision event"
        )
        assert events[0]["fallback_redispatch_step_count"] == 1


class TestContinuationGoLiveMidPrefixFailure:
    """CR-Important-2 (T2 [23951]): a prefix that never finishes -- cut
    short by budget exhaustion, deadline exhaustion, or a raised
    exception -- must never hand off. Each of these returns BEFORE
    nx_answer's `_continuation_engaged` block is ever reached (verified
    by direct code tracing in the review; these tests pin that
    ordering as a REGRESSION test rather than relying on the trace
    alone). continuation=True and go-live True in every case -- the
    scenario the review named as unlocked by no test."""

    @pytest.mark.asyncio
    async def test_budget_exhausted_mid_prefix_never_hands_off(self):
        """extract (real cost 1.0) dispatches; the SECOND prefix step
        (search, still before the cut at step index 2) must never
        dispatch because the running spend already exceeds budget_usd
        -- normal budget-exhausted behaviour, unchanged by continuation
        being requested."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import (
            NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX,
            NX_ANSWER_CONTINUATION_MARKER_PREFIX,
            nx_answer,
        )
        from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink
        from nexus.plans.match import Match

        plan = json.dumps({
            "steps": [
                {"tool": "extract", "args": {"inputs": "[]", "fields": "a"}},
                {"tool": "search", "args": {"query": "y", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "analyze the results"}},
            ],
        })
        match = Match(
            plan_id=1, name="test", description="test", confidence=0.9,
            dimensions={}, tags="", plan_json=plan,
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        async def stub_extract(**kwargs):
            sink = _ambient_usage_sink.get()
            if sink is not None:
                sink.append(DispatchUsage(
                    model="claude-sonnet-5-20260101", cost_usd=1.0,
                    input_tokens=5, output_tokens=2,
                    cache_creation_input_tokens=0, cache_read_input_tokens=0,
                    duration_ms=100, duration_api_ms=90, num_turns=1,
                ))
            return {"extractions": []}

        search_calls: list = []

        async def stub_search(**kwargs):
            search_calls.append(kwargs)
            return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"]}

        recorded_calls: list = []
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded_calls.append(kw)
        )

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "operator_extract", stub_extract), \
             patch.object(mcp_core, "search", stub_search):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(
                question="q", continuation=True, structured=True, budget_usd=0.5,
            )

        assert search_calls == [], (
            "the second prefix step must never dispatch -- budget "
            "exhausted before it could"
        )
        assert result["final_text"].startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX)
        assert result["continuation"] is None, "a budget-exhausted prefix hands off nothing"
        assert len(recorded_calls) == 1, "exactly one telemetry row -- the exhaustion marker"
        assert recorded_calls[0]["final_text"].startswith(
            NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX,
        )
        assert NX_ANSWER_CONTINUATION_MARKER_PREFIX not in recorded_calls[0]["final_text"]

    @pytest.mark.asyncio
    async def test_deadline_exhausted_mid_prefix_never_hands_off(self):
        """The first prefix step's stub sleeps past a tiny
        budget_seconds; the SECOND prefix step (still before the cut)
        must never dispatch -- normal deadline behaviour, unchanged by
        continuation being requested."""
        import asyncio

        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import (
            NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX,
            NX_ANSWER_CONTINUATION_MARKER_PREFIX,
            nx_answer,
        )
        from nexus.plans.match import Match

        plan = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "search", "args": {"query": "y", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "analyze the results"}},
            ],
        })
        match = Match(
            plan_id=1, name="test", description="test", confidence=0.9,
            dimensions={}, tags="", plan_json=plan,
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        search_calls: list = []

        async def stub_search(**kwargs):
            search_calls.append(kwargs)
            if len(search_calls) == 1:
                await asyncio.sleep(0.15)
            return {"ids": ["a"], "tumblers": ["1.1"], "distances": [0.1], "collections": ["knowledge"]}

        recorded_calls: list = []
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded_calls.append(kw)
        )

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(
                question="q", continuation=True, structured=True, budget_seconds=0.05,
            )

        assert len(search_calls) == 1, (
            "the second prefix step must never dispatch -- the deadline "
            "was already past before its pre-segment check"
        )
        assert result["final_text"].startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX)
        assert result["continuation"] is None, "a deadline-exhausted prefix hands off nothing"
        assert len(recorded_calls) == 1
        assert recorded_calls[0]["final_text"].startswith(
            NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX,
        )
        assert NX_ANSWER_CONTINUATION_MARKER_PREFIX not in recorded_calls[0]["final_text"]

    @pytest.mark.asyncio
    async def test_prefix_step_raising_never_hands_off(self):
        """A genuine (non-OperatorError) exception from a prefix step
        must surface as the normal plan-execution error -- never a
        handoff, never a fallback re-run (the exception propagates out
        of the ONE plan_run call nx_answer made; there is no result to
        assemble an envelope from)."""
        from nexus.mcp import core as mcp_core
        from nexus.mcp.core import NX_ANSWER_CONTINUATION_MARKER_PREFIX, nx_answer
        from nexus.plans.match import Match

        plan = json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"content": "analyze the results"}},
            ],
        })
        match = Match(
            plan_id=1, name="test", description="test", confidence=0.9,
            dimensions={}, tags="", plan_json=plan,
            required_bindings=[], optional_bindings=[],
            default_bindings={}, parent_dims=None,
        )

        async def stub_search(**kwargs):
            raise RuntimeError("boom")

        summarize_calls: list = []

        async def stub_summarize(**kwargs):
            summarize_calls.append(kwargs)
            return {"summary": "unreachable"}

        recorded_calls: list = []
        db_stub = MagicMock()
        db_stub.plans.save_plan = MagicMock(return_value=1)
        db_stub.plans.get_plan = MagicMock(return_value={"id": 1})
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded_calls.append(kw)
        )

        with patch("nexus.plans.matcher.plan_match", return_value=[match]), \
             patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
             patch("nexus.mcp.core.scratch", return_value="ok"), \
             patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
             patch("nexus.plans.continuation_envelope._CONTINUATION_GO_LIVE", True), \
             patch.object(mcp_core, "search", stub_search), \
             patch.object(mcp_core, "operator_summarize", stub_summarize):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer(question="q", continuation=True, structured=True)

        assert summarize_calls == [], "the plan never reached the terminal step"
        assert result["final_text"].startswith("Error during plan execution:")
        assert result["continuation"] is None, "a crashed prefix hands off nothing"
        assert len(recorded_calls) == 1
        assert recorded_calls[0]["final_text"].startswith("Error:")
        assert NX_ANSWER_CONTINUATION_MARKER_PREFIX not in recorded_calls[0]["final_text"]


class TestNxAnswerReport:
    """``nx_answer_report`` (RDR-200 §Telemetry, nexus-4e75w.5) — the
    SECOND, independent append that closes the handoff/report pairing.
    Zero engine change: writes through the SAME
    ``telemetry.record_nx_answer_run`` route ``nx_answer`` itself uses."""

    @pytest.mark.asyncio
    async def test_writes_exactly_one_report_row(self):
        from nexus.mcp.core import (
            NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX,
            nx_answer_report,
        )

        db_stub = MagicMock()
        recorded: list = []
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded.append(kw)
        )

        with patch("nexus.mcp.core._t2_ctx") as t2_ctx:
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer_report(
                continuation_id="cid-abc", ok=True, final_text_excerpt="a short excerpt",
            )

        assert result == {"ok": True, "recorded": True, "continuation_id": "cid-abc"}
        assert len(recorded) == 1
        row = recorded[0]
        assert row["final_text"].startswith(NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX)
        assert "continuation_id=cid-abc" in row["final_text"]
        assert "ok=True" in row["final_text"]
        assert "a short excerpt" in row["final_text"]
        assert row["step_count"] == 0, "a report event is not a run"
        assert row["cost_usd"] is None
        assert row["plan_id"] is None

    @pytest.mark.asyncio
    async def test_ok_false_is_carried_in_the_marker(self):
        from nexus.mcp.core import nx_answer_report

        db_stub = MagicMock()
        recorded: list = []
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded.append(kw)
        )

        with patch("nexus.mcp.core._t2_ctx") as t2_ctx:
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer_report(continuation_id="cid-fail", ok=False)

        assert "ok=False" in recorded[0]["final_text"]

    @pytest.mark.asyncio
    async def test_telemetry_failure_degrades_to_a_reported_false(self):
        """Best-effort, matching every other telemetry write site in
        this file: a T2 failure must never raise out to the caller."""
        from nexus.mcp.core import nx_answer_report

        db_stub = MagicMock()
        db_stub.telemetry.record_nx_answer_run.side_effect = RuntimeError("t2 down")

        with patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
             patch("nexus.mcp.core._warn_telemetry_drop"):
            t2_ctx.return_value.__enter__.return_value = db_stub
            result = await nx_answer_report(continuation_id="cid-x", ok=True)

        assert result["ok"] is False
        assert result["recorded"] is False
        assert result["continuation_id"] == "cid-x"

    @pytest.mark.asyncio
    async def test_excerpt_is_capped(self):
        from nexus.mcp.core import nx_answer_report

        db_stub = MagicMock()
        recorded: list = []
        db_stub.telemetry.record_nx_answer_run.side_effect = (
            lambda **kw: recorded.append(kw)
        )

        with patch("nexus.mcp.core._t2_ctx") as t2_ctx:
            t2_ctx.return_value.__enter__.return_value = db_stub
            await nx_answer_report(
                continuation_id="cid-long", ok=True,
                final_text_excerpt="x" * 5000,
            )

        # 500-char excerpt cap, plus the marker prefix/suffix text.
        assert len(recorded[0]["final_text"]) < 600
