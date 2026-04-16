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
            with pytest.raises(ValueError, match="no dispatchable steps"):
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
