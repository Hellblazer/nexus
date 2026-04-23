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


# ── T2 migration ──────────────────────────────────────────────────────────────


class TestNxAnswerRunsMigration:

    def test_creates_table(self):
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)

        cursor = conn.execute("PRAGMA table_info(nx_answer_runs)")
        columns = {row[1] for row in cursor.fetchall()}
        expected = {
            "id", "question", "plan_id", "matched_confidence",
            "step_count", "final_text", "cost_usd", "duration_ms",
            "created_at",
        }
        assert expected <= columns

    def test_idempotent(self):
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)
        migrate_nx_answer_runs(conn)  # must not raise

    def test_in_migrations_list(self):
        from nexus.db.migrations import MIGRATIONS

        versions = [(m.introduced, m.name) for m in MIGRATIONS]
        assert any(
            v == "4.5.0" and "nx_answer_runs" in n
            for v, n in versions
        ), f"4.5.0 nx_answer_runs migration not in MIGRATIONS: {versions}"


# ── Plan-match gate ───────────────────────────────────────────────────────────


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

    def test_record_run_trace_true(self):
        from nexus.mcp.core import _nx_answer_record_run
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)

        _nx_answer_record_run(
            conn, question="test question", plan_id=1,
            matched_confidence=0.55, step_count=3,
            final_text="the answer", cost_usd=0.04,
            duration_ms=1500, trace=True,
        )

        row = conn.execute("SELECT * FROM nx_answer_runs").fetchone()
        assert row is not None
        assert row[1] == "test question"   # question
        assert row[5] == "the answer"      # final_text

    def test_record_run_trace_false_redacts(self):
        from nexus.mcp.core import _nx_answer_record_run
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)

        _nx_answer_record_run(
            conn, question="private question", plan_id=2,
            matched_confidence=None, step_count=2,
            final_text="sensitive answer", cost_usd=0.02,
            duration_ms=800, trace=False,
        )

        row = conn.execute("SELECT * FROM nx_answer_runs").fetchone()
        assert row[1] == "[redacted]"   # question
        assert row[5] == "[redacted]"   # final_text


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

        async def fake_dispatch(prompt, schema, timeout=60.0):
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

        async def fake_dispatch(prompt, schema, timeout=60.0):
            return {"steps": []}

        with patch.object(_dispatch_mod, "claude_dispatch", fake_dispatch):
            with pytest.raises(ValueError, match="empty plan"):
                await _nx_answer_plan_miss("test")

    @pytest.mark.asyncio
    async def test_plan_miss_drops_non_dispatchable_tools(self):
        """All-undispatchable steps → ValueError after normalization."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0):
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

        async def fake_dispatch(prompt, schema, timeout=60.0):
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
        """mcp__plugin_nx_nexus__search → search."""
        from nexus.mcp.core import _nx_answer_plan_miss
        import nexus.operators.dispatch as _dispatch_mod

        async def fake_dispatch(prompt, schema, timeout=60.0):
            return {"steps": [
                {"tool": "mcp__plugin_nx_nexus__search", "args": {"query": "$intent"}},
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

        async def fake_dispatch(prompt, schema, timeout=60.0):
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

        async def fake_dispatch(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
            captured["timeout"] = timeout
            return {"summary": "ok", "actions": []}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_tidy(topic="test")
        assert captured["timeout"] == 600.0, (
            f"nx_tidy default timeout must be 600s; got {captured['timeout']}"
        )


# ── nx_enrich_beads ───────────────────────────────────────────────────────────


class TestNxEnrichBeads:

    @pytest.mark.asyncio
    async def test_returns_enriched_string(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        async def fake(prompt, schema, timeout=60.0):
            return {"enriched_description": "## Enriched\n\nDetails here."}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_enrich_beads(bead_description="Implement feature X")
        assert "Enriched" in result

    @pytest.mark.asyncio
    async def test_prompt_contains_description(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_enrich_beads

        captured = []

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
            captured["timeout"] = timeout
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task")
        assert captured["timeout"] == 300.0, (
            f"nx_enrich_beads default timeout must be 300s; "
            f"got {captured['timeout']}"
        )


# ── nx_plan_audit ─────────────────────────────────────────────────────────────


class TestNxPlanAudit:

    @pytest.mark.asyncio
    async def test_returns_verdict_string(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        async def fake(prompt, schema, timeout=60.0):
            return {"verdict": "pass", "findings": [], "summary": "All good."}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        result = await nx_plan_audit(plan_json='{"steps": []}')
        assert "pass" in result
        assert "All good." in result

    @pytest.mark.asyncio
    async def test_findings_included_in_output(self, monkeypatch):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
            captured["timeout"] = timeout
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}')
        assert captured["timeout"] == 600.0, (
            f"nx_plan_audit default timeout must be 600s; "
            f"got {captured['timeout']}"
        )


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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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


class TestNxAnswerCostStub:
    """cost_usd is hardcoded 0.0 — pin the stub contract explicitly."""

    def test_cost_usd_recorded_as_zero(self, tmp_path):
        """_nx_answer_record_run stores cost_usd=0.0 (P5 stub — not real cost)."""
        import sqlite3
        from nexus.mcp.core import _nx_answer_record_run
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)

        _nx_answer_record_run(
            conn, question="q", plan_id=1, matched_confidence=0.8,
            step_count=2, final_text="answer", cost_usd=0.0,
            duration_ms=500, trace=True,
        )

        row = conn.execute("SELECT cost_usd FROM nx_answer_runs").fetchone()
        assert row is not None
        # SC-TODO P5: cost_usd is a stub (always 0.0). When real cost tracking
        # ships, this test documents the before state and must be updated.
        assert row[0] == 0.0

    def test_budget_usd_parameter_accepted_without_error(self, tmp_path):
        """budget_usd is a no-op parameter — accepted but not enforced (P5 stub)."""
        import inspect
        from nexus.mcp.core import nx_answer

        sig = inspect.signature(nx_answer)
        assert "budget_usd" in sig.parameters, "budget_usd must remain in signature"
        # Default must be present (contract stability)
        assert sig.parameters["budget_usd"].default == 0.25


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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
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

        async def fake(prompt, schema, timeout=60.0):
            captured["timeout"] = timeout
            return {"enriched_description": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_enrich_beads(bead_description="task", timeout=450.0)
        assert captured["timeout"] == 450.0

    @pytest.mark.asyncio
    async def test_plan_audit_logs_warning_on_clamp(self, monkeypatch, capsys):
        import nexus.operators.dispatch as _mod
        from nexus.mcp.core import nx_plan_audit

        async def fake(prompt, schema, timeout=60.0):
            return {"verdict": "pass", "findings": [], "summary": "ok"}

        monkeypatch.setattr(_mod, "claude_dispatch", fake)
        await nx_plan_audit(plan_json='{"steps": []}', timeout=180.0)
        out = capsys.readouterr()
        emitted = out.out + out.err
        assert "subagent_timeout_clamped" in emitted, (
            "expected a structured warning when caller timeout is below floor"
        )
        assert "tool=nx_plan_audit" in emitted
        assert "requested=180" in emitted
        assert "floor=300" in emitted
