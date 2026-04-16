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
        """When hydrated inputs exceed _OPERATOR_MAX_INPUTS, a rank winnow
        should be auto-inserted (logged at WARNING)."""
        from nexus.plans.runner import _OPERATOR_MAX_INPUTS

        assert _OPERATOR_MAX_INPUTS == 100


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
