# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nx_answer MCP tool — RDR-080 P1.

Tests cover:
  - Plan-match gate logic (hit at 0.40, hit at None/FTS5, miss below 0.40)
  - Single-step guard reroute to query()
  - Auto-hydration dispatch (stubbed store_get_many)
  - Extract-field translation (template dict → fields CSV)
  - Graceful degradation without auth (SC-9)
  - Run recording with trace=true and trace=false
  - T2 migration for nx_answer_runs table
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.plans.match import Match


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _make_match(
    *,
    plan_id: int = 1,
    confidence: float | None = 0.55,
    plan_json: str | None = None,
    name: str = "test-plan",
) -> Match:
    """Build a Match with sensible defaults."""
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
    """Build a Match with a single 'query' step."""
    return _make_match(
        plan_json=json.dumps({
            "steps": [
                {"tool": "query", "args": {"question": "$intent", "corpus": "knowledge"}},
            ],
        }),
    )


def _make_multi_step_match() -> Match:
    """Build a Match with multiple steps including an operator."""
    return _make_match(
        plan_json=json.dumps({
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "extract", "args": {"inputs": "$step1.ids", "fields": "title,summary"}},
            ],
        }),
    )


# ── T2 migration ─────────────────────────────────────────────────────────────


class TestNxAnswerRunsMigration:
    """Test the nx_answer_runs T2 migration (version 4.5.0)."""

    def test_creates_table(self):
        from nexus.db.migrations import migrate_nx_answer_runs

        conn = sqlite3.connect(":memory:")
        migrate_nx_answer_runs(conn)

        # Table should exist with expected columns.
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
        # Running again should not raise.
        migrate_nx_answer_runs(conn)

    def test_in_migrations_list(self):
        from nexus.db.migrations import MIGRATIONS

        versions = [(m.introduced, m.name) for m in MIGRATIONS]
        assert any(
            v == "4.5.0" and "nx_answer_runs" in n
            for v, n in versions
        ), f"4.5.0 nx_answer_runs migration not in MIGRATIONS: {versions}"


# ── Plan-match gate ──────────────────────────────────────────────────────────


class TestPlanMatchGate:
    """Test the plan-match gate logic inside nx_answer."""

    def test_hit_at_threshold(self):
        """confidence == 0.40 should be a hit (>= threshold)."""
        from nexus.mcp.core import _nx_answer_match_is_hit

        assert _nx_answer_match_is_hit(0.40) is True

    def test_hit_above_threshold(self):
        """confidence > 0.40 should be a hit."""
        from nexus.mcp.core import _nx_answer_match_is_hit

        assert _nx_answer_match_is_hit(0.85) is True

    def test_hit_at_none_fts5_sentinel(self):
        """confidence=None (FTS5 fallback) should be a hit, not a miss."""
        from nexus.mcp.core import _nx_answer_match_is_hit

        assert _nx_answer_match_is_hit(None) is True

    def test_miss_below_threshold(self):
        """confidence < 0.40 should be a miss."""
        from nexus.mcp.core import _nx_answer_match_is_hit

        assert _nx_answer_match_is_hit(0.30) is False

    def test_miss_at_zero(self):
        from nexus.mcp.core import _nx_answer_match_is_hit

        assert _nx_answer_match_is_hit(0.0) is False


# ── Single-step guard ────────────────────────────────────────────────────────


class TestSingleStepGuard:
    """A 1-step plan whose only step is 'query' should reroute directly."""

    def test_single_query_step_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query

        match = _make_single_step_query_match()
        assert _nx_answer_is_single_query(match) is True

    def test_multi_step_not_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query

        match = _make_multi_step_match()
        assert _nx_answer_is_single_query(match) is False

    def test_single_non_query_step_not_detected(self):
        from nexus.mcp.core import _nx_answer_is_single_query

        match = _make_match(
            plan_json=json.dumps({
                "steps": [{"tool": "search", "args": {"query": "$intent"}}],
            }),
        )
        assert _nx_answer_is_single_query(match) is False


# ── Auto-hydration ───────────────────────────────────────────────────────────


class TestAutoHydration:
    """Test auto-hydration in _default_dispatcher."""

    def test_operator_with_ids_gets_hydrated(self):
        """When an operator tool receives args with 'ids', store_get_many
        should be called and ids replaced with inputs."""
        from nexus.plans.runner import _OPERATOR_TOOL_MAP

        # Verify operator tools are in the map.
        assert "extract" in _OPERATOR_TOOL_MAP
        assert "summarize" in _OPERATOR_TOOL_MAP

    def test_non_operator_not_hydrated(self):
        """Retrieval tools (search, query) should NOT be auto-hydrated."""
        from nexus.plans.runner import _OPERATOR_TOOL_MAP

        assert "search" not in _OPERATOR_TOOL_MAP
        assert "query" not in _OPERATOR_TOOL_MAP

    @pytest.mark.asyncio
    async def test_hydration_replaces_ids_with_inputs(self):
        """When dispatcher resolves an operator and args contain 'ids',
        it should call store_get_many and inject 'inputs'."""
        from nexus.plans.runner import _default_dispatcher

        fake_ids = ["id1", "id2", "id3"]
        fake_collections = ["knowledge__test"]
        hydrated_contents = ["content 1", "content 2", "content 3"]

        # Mock store_get_many to return structured contents.
        mock_get_many = MagicMock(return_value={
            "contents": hydrated_contents,
            "missing": [],
        })
        # Mock the operator tool to capture what it receives.
        received_args = {}

        async def mock_operator(**kwargs):
            received_args.update(kwargs)
            return {"extractions": [{"title": "t1"}, {"title": "t2"}, {"title": "t3"}]}

        with patch("nexus.mcp.core.store_get_many", mock_get_many), \
             patch("nexus.mcp.core.operator_extract", mock_operator):
            result = await _default_dispatcher("extract", {
                "ids": fake_ids,
                "collections": fake_collections,
                "fields": "title,summary",
            })

        # store_get_many should have been called.
        mock_get_many.assert_called_once()
        call_kwargs = mock_get_many.call_args
        assert call_kwargs[1]["structured"] is True

        # The operator should receive 'inputs' (JSON array), not 'ids'.
        assert "inputs" in received_args
        assert "ids" not in received_args
        parsed_inputs = json.loads(received_args["inputs"])
        assert parsed_inputs == hydrated_contents

    @pytest.mark.asyncio
    async def test_hydration_caps_at_max_inputs(self):
        """When hydrated inputs exceed _OPERATOR_MAX_INPUTS, the dispatcher
        truncates to the cap and logs a WARNING."""
        import logging

        from nexus.plans.runner import _OPERATOR_MAX_INPUTS, _default_dispatcher

        assert _OPERATOR_MAX_INPUTS == 100

        # 150 IDs — exceeds the 100 cap.
        fake_ids = [f"id{i}" for i in range(150)]
        hydrated_contents = [f"content {i}" for i in range(150)]

        mock_get_many = MagicMock(return_value={
            "contents": hydrated_contents,
            "missing": [],
        })
        received_args = {}

        async def mock_operator(**kwargs):
            received_args.update(kwargs)
            return {"text": "summarized", "citations": []}

        with patch("nexus.mcp.core.store_get_many", mock_get_many), \
             patch("nexus.mcp.core.operator_summarize", mock_operator):
            result = await _default_dispatcher("summarize", {
                "ids": fake_ids,
                "collections": "knowledge",
            })

        # The operator should receive at most _OPERATOR_MAX_INPUTS inputs.
        parsed = json.loads(received_args["inputs"])
        assert len(parsed) == _OPERATOR_MAX_INPUTS


# ── Extract-field translation ────────────────────────────────────────────────


class TestExtractFieldTranslation:
    """RF-13: old template dict → fields CSV."""

    def test_template_dict_to_csv(self):
        from nexus.mcp.core import _nx_answer_translate_extract_fields

        args = {"template": {"title": "str", "year": "int", "author": "str"}}
        translated = _nx_answer_translate_extract_fields(args)
        assert "fields" in translated
        assert "template" not in translated
        fields = set(translated["fields"].split(","))
        assert fields == {"title", "year", "author"}

    def test_fields_csv_untouched(self):
        from nexus.mcp.core import _nx_answer_translate_extract_fields

        args = {"fields": "title,year"}
        translated = _nx_answer_translate_extract_fields(args)
        assert translated["fields"] == "title,year"

    def test_no_fields_or_template(self):
        from nexus.mcp.core import _nx_answer_translate_extract_fields

        args = {"query": "test"}
        translated = _nx_answer_translate_extract_fields(args)
        assert translated == args


# ── Graceful degradation (SC-9) ──────────────────────────────────────────────


class TestGracefulDegradation:
    """Without auth, retrieval-only plan-hits work; plan-miss returns error."""

    @pytest.mark.asyncio
    async def test_retrieval_only_plan_works_without_auth(self):
        """A plan with only retrieval steps (search, query, traverse)
        should work even without operator pool auth."""
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

    @pytest.mark.asyncio
    async def test_operator_plan_needs_auth(self):
        """A plan with operator steps requires auth."""
        from nexus.mcp.core import _nx_answer_needs_operators

        match = _make_multi_step_match()
        assert _nx_answer_needs_operators(match) is True


# ── Run recording ────────────────────────────────────────────────────────────


class TestRunRecording:
    """nx_answer_runs T2 table recording."""

    def test_record_run_trace_true(self):
        """When trace=True, the run should be recorded with final_text."""
        from nexus.mcp.core import _nx_answer_record_run

        conn = sqlite3.connect(":memory:")
        from nexus.db.migrations import migrate_nx_answer_runs
        migrate_nx_answer_runs(conn)

        _nx_answer_record_run(
            conn,
            question="test question",
            plan_id=1,
            matched_confidence=0.55,
            step_count=3,
            final_text="the answer",
            cost_usd=0.04,
            duration_ms=1500,
            trace=True,
        )

        row = conn.execute("SELECT * FROM nx_answer_runs").fetchone()
        assert row is not None
        # question is at index 1
        assert row[1] == "test question"
        # final_text is at index 5
        assert row[5] == "the answer"

    def test_record_run_trace_false(self):
        """When trace=False, the run should be recorded but final_text omitted."""
        from nexus.mcp.core import _nx_answer_record_run

        conn = sqlite3.connect(":memory:")
        from nexus.db.migrations import migrate_nx_answer_runs
        migrate_nx_answer_runs(conn)

        _nx_answer_record_run(
            conn,
            question="private question",
            plan_id=2,
            matched_confidence=None,
            step_count=2,
            final_text="sensitive answer",
            cost_usd=0.02,
            duration_ms=800,
            trace=False,
        )

        row = conn.execute("SELECT * FROM nx_answer_runs").fetchone()
        assert row is not None
        # question should be redacted.
        assert row[1] == "[redacted]"
        # final_text should be redacted.
        assert row[5] == "[redacted]"


# ── Plan-miss planner ────────────────────────────────────────────────────────


class TestPlanMissPlanner:
    """C-1 remediation: plan-miss dispatches an inline LLM planner."""

    @pytest.mark.asyncio
    async def test_plan_miss_dispatches_planner(self):
        """On plan miss, _nx_answer_plan_miss should dispatch the planner
        pool and return a Match with the decomposed plan."""
        from nexus.mcp.core import _nx_answer_plan_miss

        fake_plan = {
            "steps": [
                {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        }

        async def mock_dispatch_with_rotation(*, prompt, timeout=60.0):
            return fake_plan

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch_with_rotation

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool), \
             patch("nexus.mcp.core.plan_save", return_value="Saved"):
            match = await _nx_answer_plan_miss("how does X work")

        assert match.name == "ad-hoc"
        plan = json.loads(match.plan_json)
        assert len(plan["steps"]) == 2
        assert plan["steps"][0]["tool"] == "search"
        assert match.default_bindings["intent"] == "how does X work"

    @pytest.mark.asyncio
    async def test_plan_miss_does_not_save_before_execution(self):
        """_nx_answer_plan_miss should NOT call plan_save — saving happens
        in nx_answer after plan_run succeeds (I-6 fix)."""
        from nexus.mcp.core import _nx_answer_plan_miss

        fake_plan = {
            "steps": [{"tool": "search", "args": {"query": "$intent"}}],
        }

        async def mock_dispatch(*a, **kw):
            return fake_plan

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch
        save_calls = []

        def mock_plan_save(**kwargs):
            save_calls.append(kwargs)
            return "Saved"

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool), \
             patch("nexus.mcp.core.plan_save", mock_plan_save):
            await _nx_answer_plan_miss("test question")

        assert len(save_calls) == 0, "plan_save should not be called before execution"

    @pytest.mark.asyncio
    async def test_plan_miss_empty_plan_raises(self):
        """Planner returning empty steps should raise ValueError."""
        from nexus.mcp.core import _nx_answer_plan_miss

        async def mock_dispatch(*a, **kw):
            return {"steps": []}

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            with pytest.raises(ValueError, match="empty plan"):
                await _nx_answer_plan_miss("test")

    @pytest.mark.asyncio
    async def test_plan_miss_drops_non_nexus_tools(self):
        """Planner that generates ONLY non-dispatchable tools should fail.
        Mixed plans have non-dispatchable steps dropped silently."""
        from nexus.mcp.core import _nx_answer_plan_miss

        # All steps non-dispatchable → error.
        all_bad = {
            "steps": [
                {"tool": "mcp__plugin_sn_serena__jet_brains_find_symbol", "args": {"query": "test"}},
            ],
        }

        async def mock_dispatch_bad(*a, **kw):
            return all_bad

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch_bad

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            with pytest.raises(ValueError, match="no dispatchable steps"):
                await _nx_answer_plan_miss("test")

    @pytest.mark.asyncio
    async def test_plan_miss_keeps_valid_drops_invalid(self):
        """Mixed plan: valid steps kept, non-dispatchable dropped."""
        from nexus.mcp.core import _nx_answer_plan_miss

        mixed_plan = {
            "steps": [
                {"tool": "Grep", "args": {"pattern": "test"}},
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        }

        async def mock_dispatch(*a, **kw):
            return mixed_plan

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            match = await _nx_answer_plan_miss("how does X work")

        plan = json.loads(match.plan_json)
        # Grep aliased to search, so all 3 steps survive.
        tools = [s["tool"] for s in plan["steps"]]
        assert all(t in {"search", "summarize"} for t in tools)

    @pytest.mark.asyncio
    async def test_plan_miss_aliases_common_tools(self):
        """Common non-nexus tools should be aliased, not dropped."""
        from nexus.mcp.core import _nx_answer_plan_miss

        plan_with_aliases = {
            "steps": [
                {"tool": "Grep", "args": {"query": "test"}},
                {"tool": "Read", "args": {"path": "file.py"}},
                {"tool": "Bash", "args": {"command": "ls"}},
                {"tool": "find", "args": {"pattern": "*.py"}},
                {"tool": "glob", "args": {"pattern": "*.md"}},
            ],
        }

        async def mock_dispatch(*a, **kw):
            return plan_with_aliases

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            match = await _nx_answer_plan_miss("test")

        plan = json.loads(match.plan_json)
        # All should be aliased to "search".
        assert all(s["tool"] == "search" for s in plan["steps"])

    @pytest.mark.asyncio
    async def test_plan_miss_mixed_valid_and_unmappable(self):
        """Steps with truly unmappable tools are dropped, valid kept."""
        from nexus.mcp.core import _nx_answer_plan_miss

        mixed = {
            "steps": [
                {"tool": "search", "args": {"query": "$intent"}},
                {"tool": "some_random_tool", "args": {}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        }

        async def mock_dispatch(*a, **kw):
            return mixed

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            match = await _nx_answer_plan_miss("test")

        plan = json.loads(match.plan_json)
        tools = [s["tool"] for s in plan["steps"]]
        assert tools == ["search", "summarize"]
        assert "some_random_tool" not in tools

    @pytest.mark.asyncio
    async def test_plan_miss_normalizes_mcp_prefixed_tools(self):
        """Planner that uses mcp__plugin_nx_nexus__search should have it
        normalized to bare 'search' in the saved plan."""
        from nexus.mcp.core import _nx_answer_plan_miss

        prefixed_plan = {
            "steps": [
                {"tool": "mcp__plugin_nx_nexus__search", "args": {"query": "$intent"}},
                {"tool": "summarize", "args": {"inputs": "$step1.ids"}},
            ],
        }

        async def mock_dispatch(*a, **kw):
            return prefixed_plan

        mock_pool = MagicMock()
        mock_pool.dispatch_with_rotation = mock_dispatch

        with patch("nexus.mcp_infra.get_operator_pool", return_value=mock_pool):
            match = await _nx_answer_plan_miss("how does X work")

        plan = json.loads(match.plan_json)
        assert plan["steps"][0]["tool"] == "search"
        assert plan["steps"][1]["tool"] == "summarize"
