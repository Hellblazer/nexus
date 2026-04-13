# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for query sanitizer (RDR-071).

Ported from mempalace/tests/test_query_sanitizer.py, adapted for
nexus's str-returning interface (mempalace returns dict).

TDD: these tests are written before the implementation exists.
Run `uv run pytest tests/test_query_sanitizer.py --collect-only` to
verify test discovery. Full run will fail until nexus-xyh.2 implements
sanitize_query in filters.py.
"""
from __future__ import annotations

import pytest

from nexus.filters import (
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    SAFE_QUERY_LENGTH,
    sanitize_query,
)


class TestPassthrough:
    """Step 1: Queries under SAFE_QUERY_LENGTH pass through unchanged."""

    def test_short_query_unchanged(self) -> None:
        query = "What is Rust error handling?"
        assert sanitize_query(query) == query

    def test_empty_query(self) -> None:
        assert sanitize_query("") == ""

    def test_whitespace_only(self) -> None:
        assert sanitize_query("   ") == ""

    def test_exactly_safe_length(self) -> None:
        query = "a" * SAFE_QUERY_LENGTH
        assert sanitize_query(query) == query

    def test_one_over_safe_triggers_sanitization(self) -> None:
        query = "a" * (SAFE_QUERY_LENGTH + 1)
        result = sanitize_query(query)
        assert len(result) <= MAX_QUERY_LENGTH


class TestQuestionExtraction:
    """Step 2: Extract question sentences (ending with ?)."""

    def test_question_at_end_of_long_text(self) -> None:
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is the best way to handle errors in Rust?"
        result = sanitize_query(query)
        assert result != query  # was sanitized
        assert "error" in result.lower() or "Rust" in result
        assert len(result) <= MAX_QUERY_LENGTH

    def test_fullwidth_question_mark(self) -> None:
        """Japanese fullwidth ? is recognized."""
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "Rustのエラーハンドリング方法は？"
        result = sanitize_query(query)
        assert result != query
        assert "Rust" in result or "エラー" in result

    def test_multiple_questions_takes_last(self) -> None:
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is Python?\nHow does Rust handle errors?"
        result = sanitize_query(query)
        assert "Rust" in result or "error" in result.lower()

    def test_system_prompt_question_ignored_when_real_exists(self) -> None:
        system_prompt = "Are you ready to help? " * 30 + "\n"
        real_query = "What databases does nexus support?"
        query = system_prompt + real_query
        result = sanitize_query(query)
        assert "nexus" in result or "database" in result.lower()


class TestTailSentence:
    """Step 3: Extract last meaningful sentence (no question mark)."""

    def test_command_style_query(self) -> None:
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "Show me all error handling patterns"
        result = sanitize_query(query)
        assert result != query
        assert "error" in result.lower() or "pattern" in result.lower()

    def test_keyword_query_after_noise(self) -> None:
        system_prompt = "System configuration loaded. " * 60
        query = system_prompt + "\nChromaDB integration setup"
        result = sanitize_query(query)
        assert result != query
        assert "ChromaDB" in result or "integration" in result


class TestTailTruncation:
    """Step 4: Fallback to last MAX_QUERY_LENGTH characters."""

    def test_no_sentences_falls_to_truncation(self) -> None:
        filler = "\n".join(["ab"] * 200)
        result = sanitize_query(filler)
        assert len(result) <= MAX_QUERY_LENGTH
        assert result != filler

    def test_truncation_preserves_tail(self) -> None:
        filler = "x" * 1000 + "IMPORTANT_QUERY_CONTENT"
        result = sanitize_query(filler)
        assert "IMPORTANT_QUERY_CONTENT" in result


class TestLengthConstraints:
    """Output length invariants."""

    def test_output_never_exceeds_max(self) -> None:
        long_question = "a" * 1000 + "?"
        system_prompt = "Context. " * 100
        query = system_prompt + long_question
        result = sanitize_query(query)
        assert len(result) <= MAX_QUERY_LENGTH

    def test_short_extraction_falls_through(self) -> None:
        """Question mark found but sentence too short (<MIN_QUERY_LENGTH)."""
        system_prompt = "You are helpful. " * 50
        query = system_prompt + "\nOK?"
        result = sanitize_query(query)
        assert result != query  # was sanitized
        assert len(result) >= 1  # not empty

    def test_passthrough_returns_identical(self) -> None:
        query = "short query"
        assert sanitize_query(query) is not None
        assert sanitize_query(query) == query

    def test_contaminated_returns_shorter(self) -> None:
        system_prompt = "You are a helpful assistant. " * 50
        query = system_prompt + "What is PBFT?"
        result = sanitize_query(query)
        assert len(result) < len(query)


# ── SC-4: 5 contamination patterns ──────────────────────────────────────────


class TestContaminationPatterns:
    """SC-4: Test with 5 realistic contamination patterns."""

    def test_system_prompt_prepended(self) -> None:
        """Pattern 1: AI system prompt prepended to query."""
        system = (
            "You are a helpful assistant with access to semantic search tools. "
            "Always be thorough. Check multiple collections. "
        ) * 20
        query = system + "What is Byzantine fault tolerance?"
        result = sanitize_query(query)
        assert len(result) <= MAX_QUERY_LENGTH
        assert len(result) >= MIN_QUERY_LENGTH
        assert "Byzantine" in result or "fault" in result

    def test_chain_of_thought_prepended(self) -> None:
        """Pattern 2: Chain-of-thought reasoning before the actual query."""
        cot = (
            "Let me think step by step. First I need to understand the context. "
            "The user is asking about distributed systems. I should search for "
            "relevant papers. Actually, the key question is: "
        )
        query = cot + "how does PBFT handle view changes?"
        result = sanitize_query(query)
        assert "PBFT" in result or "view" in result

    def test_tool_preamble(self) -> None:
        """Pattern 3: Tool result output prepended to next query."""
        preamble = (
            "<tool_result>Previous search returned 0 results for 'consensus'. "
            "The collection knowledge__delos has 1397 documents.</tool_result>\n"
            "Trying broader query. "
        ) * 3
        query = preamble + "Search for: distributed consensus algorithms"
        result = sanitize_query(query)
        assert "consensus" in result.lower() or "distributed" in result.lower()

    def test_multi_turn_context(self) -> None:
        """Pattern 4: Previous conversation turns prepended."""
        context = (
            "User: What is Raft?\n"
            "Assistant: Raft is a consensus algorithm designed for understandability. "
            "It separates leader election, log replication, and safety.\n"
            "User: How does it compare to Paxos?\n"
            "Assistant: Raft is equivalent to multi-Paxos but easier to implement.\n"
        ) * 3
        query = context + "User: And how does PBFT differ from both?"
        result = sanitize_query(query)
        assert "PBFT" in result or "differ" in result

    def test_empty_and_whitespace(self) -> None:
        """Pattern 5: Empty / whitespace-only queries."""
        assert sanitize_query("") == ""
        assert sanitize_query("   ") == ""
        assert sanitize_query("\n\n") == ""


# ── Real-world scenarios ────────────────────────────────────────────────────


class TestRealWorldScenarios:
    """Realistic contamination from agent workflows."""

    def test_claude_code_context_prepended(self) -> None:
        """Claude Code agent prepends CLAUDE.md context to search."""
        context = (
            "This project uses Python 3.12+, ChromaDB for vector storage, "
            "SQLite for metadata, and Voyage AI for embeddings. "
            "The CLI entry point is nx. "
        ) * 10
        real_query = "How did we decide on the database architecture?"
        query = context + real_query
        result = sanitize_query(query)
        assert len(result) <= MAX_QUERY_LENGTH
        assert len(result) >= MIN_QUERY_LENGTH

    def test_2000_char_system_prompt(self) -> None:
        """The exact scenario from MemPalace Issue #333."""
        system_prompt = "You are an AI assistant with access to tools. " * 45
        real_query = "What is the status of the indexing pipeline?"
        query = system_prompt + real_query
        result = sanitize_query(query)
        assert result != query
        assert len(result) <= MAX_QUERY_LENGTH
        assert "indexing" in result.lower() or "pipeline" in result.lower()
