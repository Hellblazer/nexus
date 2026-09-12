# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-169 Phase B fix round 1, CRITICAL (T2 review-nexus-zw2em-rdr169-
phase-b-2026-09-11): a reference-only chunk (RDR-169 G1, chunk_text=NULL)
is genuinely reachable through ``POST /v1/vectors/search`` once
``REFERENCE_ONLY_WRITES_ENABLED=true`` landed (bead nexus-zw2em). Plain
vector search does NOT exclude such a row -- only ``hybrid_search``'s
FTS/trigram gate does -- so its ``content: null`` reaches
``search_engine.py``'s ``SearchResult`` construction and, from there,
every ``.content[`` slice in ``mcp/core.py``'s render code. Pre-fix this
raised ``TypeError`` (``'NoneType' object is not subscriptable``), caught
by the tool's outer ``try/except`` and surfaced as an opaque error for the
WHOLE page of results, not just the reference-only row.

These tests must fail against the pre-fix code (an ``AssertionError`` on
``content == ""`` for the first, an ``Error:``-prefixed string swallowing
the render ``TypeError`` for the other two) and pass once ``content=None``
is coerced to ``""`` at the ``SearchResult`` construction boundary
(``search_engine.py``'s per-row list-comprehension building the raw
engine dict into a ``SearchResult``).

No T2/engine substrate needed -- ``_t2_ctx``/``_get_catalog`` are
monkeypatched to lightweight in-memory stand-ins so this file runs
identically under ``NX_TEST_T2_SUBSTRATE=none``.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nexus.mcp_infra import inject_t3
from nexus.types import SearchResult

_NO_CONTRADICTION_CFG = {"search": {"contradiction_check": False}}


@pytest.fixture(autouse=True)
def _reset_t3_singleton():
    """nexus-gtl01/jovc9: a MagicMock left in the process-wide
    ``mcp_infra._t3_instance`` singleton silently swallows every later
    chunk write in this worker. Reset unconditionally after each test in
    this file, whether or not it injected one."""
    yield
    inject_t3(None)


def _mock_t3_for_core(collections: list[dict]) -> MagicMock:
    """Minimal re-implementation of test_mcp_server.py's ``_mock_t3``
    helper (kept local so this file has zero dependency on that module's
    heavier, engine-backed fixtures)."""
    filled = []
    for c in collections:
        row = dict(c)
        name = row.get("name", "")
        row.setdefault("content_type", name.partition("__")[0] if "__" in name else "")
        row.setdefault("owner_id", name.partition("__")[2] if "__" in name else name)
        row.setdefault("embedding_model", "test-model")
        row.setdefault("lifecycle_state", "live")
        filled.append(row)
    mock = MagicMock()
    mock.list_collections.return_value = filled
    inject_t3(mock)
    return mock


@contextmanager
def _fake_t2_ctx():
    """Stand-in for ``_t2_ctx()`` carrying only the two attributes
    ``search_cross_corpus`` reads (``taxonomy``, ``telemetry``) -- both
    tolerate ``None`` (best-effort paths, exception-guarded internally)."""
    yield SimpleNamespace(taxonomy=None, telemetry=None)


class TestSearchCrossCorpusNoneContentBoundary:
    """The real parsing boundary: ``search_engine.py``'s ``SearchResult``
    construction from the raw engine-shaped dict must never propagate a
    ``None`` content. This is what makes every downstream consumer
    (formatters, mcp/core render code) safe without each having to
    re-guard defensively."""

    def test_search_cross_corpus_coerces_none_content_to_empty_string(self):
        from nexus.search_engine import search_cross_corpus

        class _RefOnlyFakeT3:
            _voyage_client = None

            def search(self, query, collection_names, n_results=10, where=None):
                return [{
                    "id": "refonly-chash", "content": None, "distance": 0.05,
                    "collection": collection_names[0],
                    "retention": "reference-only",
                }]

            def get_embeddings(self, collection, ids):
                # Only reached if a future default flips contradiction_check
                # or clustering back on for this call; the per-collection
                # try/except in _fetch_embeddings_for_results treats any
                # exception here as an isolated per-collection failure
                # (logged, indices marked failed, search still returns).
                raise NotImplementedError("fake T3 carries no embeddings")

        results = search_cross_corpus(
            "q", ["knowledge__refonly"], 10, _RefOnlyFakeT3(),
            threshold_override=float("inf"), cluster_by=None,
        )
        assert len(results) == 1
        assert results[0].content == ""
        assert results[0].content is not None


class TestMcpRenderNoneContentSafety:
    """Drives the ACTUAL mcp/core.py render code (not a hand-rolled
    reimplementation of it) with genuine None-content SearchResult rows,
    proving each named slice site tolerates them."""

    def test_search_tool_renders_reference_only_row_without_crashing(self):
        """mcp/core.py's search-tool per-row snippet render
        (``r.content[:200]``)."""
        from nexus.mcp.core import _search_render

        _mock_t3_for_core([{"name": "knowledge__refonly", "count": 1}])
        refonly = SearchResult(
            id="refonly-chash", content=None, distance=0.05,
            collection="knowledge__refonly",
            metadata={"retention": "reference-only"},
        )
        with patch("nexus.search_engine.search_cross_corpus", lambda *a, **kw: [refonly]), \
             patch("nexus.config.load_config", return_value=_NO_CONTRADICTION_CFG), \
             patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx), \
             patch("nexus.mcp.core._get_catalog", return_value=None):
            out = _search_render(query="anything", corpus="knowledge__refonly")
        assert isinstance(out, str)
        assert not out.startswith("Error:")
        # id used as the label fallback (no title/source metadata)
        assert "refonly-chash" in out

    def test_query_tool_renders_reference_only_rows_without_crashing(self):
        """mcp/core.py's doc-grouping render for BOTH the new-document
        snippet (first row seen for a doc_key, ~line 4020) and the
        "better matching chunk" snippet update (a later row with a
        higher hybrid_score for the SAME doc_key, ~line 4060) -- two
        distinct ``r.content[:300]`` slice sites, both fed a None
        content."""
        from nexus.mcp.core import query

        _mock_t3_for_core([{"name": "code__refonly", "count": 2}])
        row1 = SearchResult(
            id="refonly-1", content=None, distance=0.20,
            collection="code__refonly", metadata={"title": "shared-doc"},
            hybrid_score=0.5,
        )
        row2 = SearchResult(
            id="refonly-2", content=None, distance=0.05,
            collection="code__refonly", metadata={"title": "shared-doc"},
            hybrid_score=0.9,  # higher than row1 -> "better matching chunk" branch
        )
        with patch("nexus.search_engine.search_cross_corpus", lambda *a, **kw: [row1, row2]), \
             patch("nexus.config.load_config", return_value=_NO_CONTRADICTION_CFG), \
             patch("nexus.mcp.core._t2_ctx", _fake_t2_ctx), \
             patch("nexus.mcp.core._get_catalog", return_value=None):
            out = query(question="anything", corpus="code__refonly")
        assert isinstance(out, str)
        assert not out.startswith("Error:")
        assert "shared-doc" in out
