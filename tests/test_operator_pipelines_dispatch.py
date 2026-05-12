# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke-level dispatch tests for the §D.4 operator pipeline (nexus-8g79.27).

The full integration suites
(``tests/integration/test_rdr_088_operator_pipelines.py``,
``test_rdr_093_groupby_aggregate_pipelines.py``,
``test_nx_answer_equivalence.py``) are excluded from default CI by
``addopts='-m not integration and not slow'``. As a result the
LLM-dispatch wiring for every §D.4 operator had zero CI coverage and
regressions surfaced only via manual ``pytest -m integration`` runs.

These tests exercise the dispatch path of each operator at the
SQL-miss boundary: mock ``claude_dispatch`` (the ``claude -p``
boundary) to return a canned envelope, then assert the operator
builds the prompt + schema correctly and routes the result back
unchanged. SQL fast paths are exercised at the unit level elsewhere
(``tests/test_aspect_sql.py``); these are dispatch-only smokes.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

# All operators under test live on the MCP module.
from nexus.mcp import core as mcp_core


pytestmark = pytest.mark.asyncio


# ── Helpers ──────────────────────────────────────────────────────────────────


def _items_json(*records: dict) -> str:
    """JSON-encode a list of records the way operators expect."""
    return json.dumps(list(records))


# ── operator_groupby ─────────────────────────────────────────────────────────


class TestOperatorGroupbyDispatch:
    """operator_groupby tries the SQL fast path then falls back to
    ``claude_dispatch``. With SQL forced to miss (``source='llm'``),
    the dispatch boundary is the only path."""

    async def test_llm_path_builds_prompt_with_key_and_items(self):
        """The LLM prompt embeds the partition key and the verbatim items
        JSON. The schema requires a ``groups`` array. Dispatch return is
        returned unchanged."""
        items = _items_json(
            {"id": "a", "year": 2024},
            {"id": "b", "year": 2024},
            {"id": "c", "year": 2023},
        )
        canned = {
            "groups": [
                {"key_value": "2024", "items": [
                    {"id": "a", "year": 2024},
                    {"id": "b", "year": 2024},
                ]},
                {"key_value": "2023", "items": [{"id": "c", "year": 2023}]},
            ],
        }
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_groupby(
                items=items, key="publication_year", source="llm",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        schema = mock_dispatch.call_args.args[1]
        assert "publication_year" in prompt
        assert "Partition the following items" in prompt
        assert items in prompt
        # Schema enforces the groups envelope downstream operators expect.
        assert schema["required"] == ["groups"]
        assert "key_value" in schema["properties"]["groups"]["items"]["required"]


# ── operator_aggregate ───────────────────────────────────────────────────────


class TestOperatorAggregateDispatch:
    """operator_aggregate runs after groupby. Mock the LLM and assert the
    aggregate prompt carries the per-group reducer."""

    async def test_llm_path_builds_prompt_with_reducer_and_groups(self):
        groups = json.dumps([
            {"key_value": "2024", "items": [
                {"id": "a"}, {"id": "b"},
            ]},
            {"key_value": "2023", "items": [{"id": "c"}]},
        ])
        canned = {
            "results": [
                {"key_value": "2024", "value": 2},
                {"key_value": "2023", "value": 1},
            ],
        }
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_aggregate(
                groups=groups, reducer="count", source="llm",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "count" in prompt
        assert groups in prompt


# ── operator_filter ──────────────────────────────────────────────────────────


class TestOperatorFilterDispatch:
    """operator_filter SQL miss falls through to LLM dispatch."""

    async def test_llm_path_builds_prompt_with_predicate(self):
        items = _items_json(
            {"id": "a", "title": "alpha"},
            {"id": "b", "title": "beta"},
        )
        canned = {"items": [{"id": "a", "title": "alpha"}]}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_filter(
                items=items, criterion="title starts with 'a'",
                source="llm",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "title starts with 'a'" in prompt
        assert items in prompt


# ── operator_extract ─────────────────────────────────────────────────────────


class TestOperatorExtractDispatch:
    async def test_dispatch_carries_fields_to_prompt(self):
        inputs = _items_json({"id": "a", "abstract": "blah"})
        canned = {"items": [{"id": "a", "method": "x", "dataset": "y"}]}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_extract(
                inputs=inputs, fields="method, dataset",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "method, dataset" in prompt


# ── operator_rank ────────────────────────────────────────────────────────────


class TestOperatorRankDispatch:
    async def test_dispatch_carries_criterion(self):
        items = _items_json({"id": "a"}, {"id": "b"})
        canned = {"ranked": [{"id": "b"}, {"id": "a"}]}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_rank(
                items=items, criterion="most recent",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "most recent" in prompt


# ── operator_check ───────────────────────────────────────────────────────────


class TestOperatorCheckDispatch:
    async def test_llm_path_carries_invariant(self):
        items = _items_json({"id": "a", "year": 2024})
        canned = {"results": [{"id": "a", "ok": True, "reason": ""}]}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_check(
                items=items, check_instruction="year is after 2020",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "year is after 2020" in prompt


# ── operator_verify ──────────────────────────────────────────────────────────


class TestOperatorVerifyDispatch:
    async def test_dispatch_carries_claim_and_evidence(self):
        canned = {"verdict": "supported", "confidence": 0.8}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_verify(
                claim="dataset X has 1000 samples",
                evidence="Table 2 lists |X|=1000.",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "dataset X has 1000 samples" in prompt
        assert "Table 2 lists" in prompt


# ── operator_compare ─────────────────────────────────────────────────────────


class TestOperatorCompareDispatch:
    async def test_dispatch_carries_items_and_focus(self):
        canned = {"comparison": [{"axis": "speed", "winner": "A"}]}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_compare(
                items=_items_json({"id": "A"}, {"id": "B"}),
                focus="speed and accuracy",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "speed and accuracy" in prompt


# ── operator_summarize ───────────────────────────────────────────────────────


class TestOperatorSummarizeDispatch:
    async def test_dispatch_carries_content(self):
        canned = {"summary": "Two papers on X.", "key_points": []}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_summarize(
                content="Paper A discusses methodology X. Paper B uses Y.",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "methodology X" in prompt


# ── operator_generate ────────────────────────────────────────────────────────


class TestOperatorGenerateDispatch:
    async def test_dispatch_carries_template_and_context(self):
        canned = {"output": "synthesized text"}
        with patch(
            "nexus.operators.dispatch.claude_dispatch",
            new=AsyncMock(return_value=canned),
        ) as mock_dispatch:
            result = await mcp_core.operator_generate(
                template="Write a 3-sentence summary of: {context}",
                context="Paper details here.",
            )

        assert result == canned
        prompt = mock_dispatch.call_args.args[0]
        assert "3-sentence summary" in prompt
        assert "Paper details here." in prompt
