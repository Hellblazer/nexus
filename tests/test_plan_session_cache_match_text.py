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


# ── S-2 parity contract regression guard ────────────────────────────────────


class TestSynthesizerParityWithPlanLibrary:
    """RDR-092 code-review S-2: the ``session_cache`` synthesiser and
    the ``plan_library`` synthesiser MUST produce byte-identical
    strings for the same inputs. Drift between the two means the T1
    cosine embedding decorrelates from the T2 FTS payload for the
    same plan.

    When the ``nexus-w98c`` follow-up collapses the two into a single
    implementation, this test becomes trivially true and can either
    stay as a smoke-test or be deleted.
    """

    @pytest.mark.parametrize("row", [
        # Full dimensional row.
        {
            "query": "Find documents attributed to a specific author.",
            "verb": "research", "name": "find-by-author",
            "scope": "global",
        },
        # Missing scope.
        {
            "query": "Some plan",
            "verb": "analyze", "name": "default", "scope": None,
        },
        # Missing verb (falls back to description).
        {
            "query": "Legacy plan",
            "verb": None, "name": "default", "scope": "global",
        },
        # Missing name (falls back to description).
        {
            "query": "Legacy plan",
            "verb": "research", "name": None, "scope": "global",
        },
        # Trailing period on description.
        {
            "query": "Plan ends with a period.",
            "verb": "research", "name": "default", "scope": "global",
        },
        # Empty description with populated dimensions.
        {
            "query": "", "verb": "research",
            "name": "default", "scope": "global",
        },
        # Whitespace-only description.
        {
            "query": "   ", "verb": "analyze",
            "name": "default", "scope": "global",
        },
        # Completely empty row.
        {},
    ])
    def test_session_cache_and_plan_library_synthesisers_agree(
        self, row: dict,
    ) -> None:
        from nexus.db.t2.plan_library import (
            _synthesize_match_text as _lib_synth,
        )
        from nexus.plans.session_cache import (
            _synthesize_match_text as _cache_synth,
        )

        cache_out = _cache_synth(row)
        lib_out = _lib_synth(
            description=row.get("query"),
            verb=row.get("verb"),
            name=row.get("name"),
            scope=row.get("scope"),
        )
        assert cache_out == lib_out, (
            f"synthesiser drift on row={row!r}: "
            f"session_cache→{cache_out!r}, plan_library→{lib_out!r}"
        )
