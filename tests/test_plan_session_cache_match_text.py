# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-092 Phase 1 match-text synthesis in PlanSessionCache.

The T1 plan cache now embeds a hybrid ``match_text`` shape instead of
the raw ``query`` column, so the cosine lane benefits from the same
dimensional signal the FTS lane gets (R10). Shape:

    <description>. <verb> <name> scope <scope>

When any of ``verb``, ``name``, ``scope`` is absent the synthesiser
falls back to the raw description — a legacy NULL-dimension row still
embeds cleanly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import chromadb
import pytest

from nexus.plans.session_cache import (
    PlanSessionCache,
    _synthesize_match_text,
)


# ── Synthesiser unit tests ──────────────────────────────────────────────────


class TestSynthesizeMatchText:
    def test_full_dimensional_row_hybrid_form(self) -> None:
        row = {
            "query": "Find documents attributed to a specific author.",
            "verb": "research",
            "name": "find-by-author",
            "scope": "global",
        }
        out = _synthesize_match_text(row)
        assert out == (
            "Find documents attributed to a specific author. "
            "research find-by-author scope global"
        )

    def test_missing_verb_falls_back_to_description(self) -> None:
        row = {
            "query": "Legacy plan with no dimensions",
            "verb": None,
            "name": "find-by-author",
            "scope": "global",
        }
        assert _synthesize_match_text(row) == "Legacy plan with no dimensions"

    def test_missing_name_falls_back_to_description(self) -> None:
        row = {
            "query": "Legacy plan",
            "verb": "research",
            "name": None,
            "scope": "global",
        }
        assert _synthesize_match_text(row) == "Legacy plan"

    def test_missing_scope_still_synthesises_verb_and_name(self) -> None:
        """Scope-less rows retain the verb+name suffix; scope is optional."""
        row = {
            "query": "Some plan",
            "verb": "analyze",
            "name": "default",
            "scope": None,
        }
        assert _synthesize_match_text(row) == "Some plan. analyze default"

    def test_empty_row_returns_empty(self) -> None:
        assert _synthesize_match_text({}) == ""

    def test_whitespace_only_description_with_dimensions(self) -> None:
        """Description is stripped; dimensional suffix still ships."""
        row = {
            "query": "   ",
            "verb": "research",
            "name": "default",
            "scope": "global",
        }
        assert _synthesize_match_text(row) == "research default scope global"


# ── _upsert_row integration ─────────────────────────────────────────────────


@pytest.fixture()
def cache() -> PlanSessionCache:
    """Real EphemeralClient-backed cache (no network, no API keys)."""
    client = chromadb.EphemeralClient()
    return PlanSessionCache(client=client, session_id="test-session")


class TestUpsertEmbedsMatchText:
    """_upsert_row now embeds the synthesised match_text, not raw query."""

    def test_upsert_embeds_match_text_on_dimensional_row(
        self, cache: PlanSessionCache,
    ) -> None:
        """A cosine query against the dimensional suffix hits the row."""
        cache.upsert({
            "id": 1,
            "query": "Find documents attributed to a specific author.",
            "verb": "research",
            "name": "find-by-author",
            "scope": "global",
            "tags": "builtin-template,rdr-092",
            "project": "",
        })
        # Query that only overlaps the synthesised dimensional suffix
        # (the description says "documents attributed", not "research
        # find-by-author") — if match_text shipped, this still hits.
        hits = cache.query("research find-by-author", n=3)
        assert hits, "dimensional suffix must be part of the embedding"
        assert hits[0][0] == 1

    def test_upsert_legacy_row_still_embeds_raw_description(
        self, cache: PlanSessionCache,
    ) -> None:
        """Rows missing dimensions retain their raw-description embedding."""
        cache.upsert({
            "id": 2,
            "query": "legacy plan about chunk chunking pipeline",
            "verb": None,
            "name": None,
            "scope": None,
            "tags": "builtin-template",
            "project": "",
        })
        hits = cache.query("chunk pipeline", n=3)
        assert hits
        assert hits[0][0] == 2

    def test_upsert_called_with_match_text_as_document(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Chroma ``upsert(documents=...)`` argument is the synthesised
        match_text — a regression gate against accidentally reverting to
        raw description.
        """
        client = chromadb.EphemeralClient()
        cache = PlanSessionCache(client=client, session_id="sess")

        spy = MagicMock(wraps=cache._col.upsert)
        monkeypatch.setattr(cache._col, "upsert", spy)

        cache.upsert({
            "id": 42,
            "query": "Analyze lineage across prose and code.",
            "verb": "analyze",
            "name": "default",
            "scope": "global",
        })

        assert spy.called
        docs = spy.call_args.kwargs.get("documents") or spy.call_args.args[1]
        assert isinstance(docs, list) and len(docs) == 1
        assert "analyze default scope global" in docs[0]
