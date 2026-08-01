# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 5: salience module + search_engine boost integration.

Tests cover the deterministic surfaces (the salience module wraps the
Phase 4 prototype; the search-engine wiring is feature-flagged so
default-off behavior is the regression bar).

The DocumentAspects.salient_sentences I/O tests that used to live here
targeted the SQLite ``DocumentAspects`` store, deleted in nexus-i711w
Stage 2 sub-stage A3. The accessor contract
(set/get_salient_sentences) now lives on ``HttpDocumentAspectsStore``
and is covered in ``tests/db/test_http_aspects_stores.py``.
"""
from __future__ import annotations

import pytest

from nexus.salience import (
    extract_salient_sentences,
    split_sentences,
    token_overlap_boost,
)
from nexus.types import SearchResult


# ── salience module ──────────────────────────────────────────────────


def test_split_sentences_drops_empty() -> None:
    assert split_sentences("") == []


def test_split_sentences_basic() -> None:
    out = split_sentences("First. Second. Third.")
    assert len(out) == 3


def test_token_overlap_zero_weight_short_circuits() -> None:
    assert token_overlap_boost("query text", ["query"], weight=0.0) == 0.0


def test_token_overlap_partial_match() -> None:
    # query: {alpha, beta, gamma} (3); salient: {alpha, beta} (2 overlap)
    score = token_overlap_boost(
        "alpha beta gamma", ["alpha beta"], weight=0.3,
    )
    assert score == pytest.approx(0.3 * 2 / 3)


class _StubCrossEncoder:
    def __init__(self, scores_by_substring: dict[str, float]) -> None:
        self._scores = scores_by_substring

    def score(self, query: str, documents: list[str]) -> list[float]:
        out: list[float] = []
        for doc in documents:
            best = 0.0
            for substr, val in self._scores.items():
                if substr in doc:
                    best = max(best, val)
            out.append(best)
        return out


def test_extract_salient_returns_top_n() -> None:
    chunk = "Alpha one. Beta two. Gamma three."
    ce = _StubCrossEncoder({"Beta": 5.0, "Gamma": 4.0, "Alpha": 1.0})
    out = extract_salient_sentences(
        chunk, seed_queries=["seed"], top_n=2, cross_encoder=ce,
    )
    assert len(out) == 2
    assert any("Beta" in s for s in out)
    assert any("Gamma" in s for s in out)


# ── DocumentAspects.salient_sentences I/O ────────────────────────────
#
# TOMBSTONE (nexus-i711w Stage 2 A3): the ``aspects_db`` fixture and the
# six set/get_salient_sentences store-accessor tests (round-trip,
# missing doc, empty doc_id, no-row, NULL column, garbage JSON) were
# deleted with their subject — the SQLite ``DocumentAspects`` store
# (src/nexus/db/t2/document_aspects.py). The accessor contract now
# lives on ``HttpDocumentAspectsStore`` and is pinned in
# tests/db/test_http_aspects_stores.py (set_salient_sentences,
# set_salient_sentences_by_key, get_salient_sentences, and the
# 404-returns-empty arm that subsumes the old missing-row case).
# Corrupt-JSON-in-storage is an engine-side concern on the HTTP
# substrate; there is no client-side column to corrupt.


# ── search_engine._apply_salience_boost ──────────────────────────────

_ASPECTS_SEAM = (
    "nexus.db.t2.http_document_aspects_store.HttpDocumentAspectsStore"
)


class _FakeAspectsStore:
    """Stateful stand-in for HttpDocumentAspectsStore — the ONLY aspects
    store after nexus-i711w Stage 2 A3. ``_apply_salience_boost`` imports
    the class function-locally, so patching the module attribute is the
    seam."""

    def __init__(self, sentences: dict[str, list[str]] | None = None) -> None:
        self.sentences = sentences or {}
        self.requested: list[str] = []
        self.closed = 0

    def get_salient_sentences(self, doc_id: str) -> list[str]:
        self.requested.append(doc_id)
        return self.sentences.get(doc_id, [])

    def close(self) -> None:
        self.closed += 1


def _make_result(rid: str, collection: str, doc_id: str, score: float) -> SearchResult:
    return SearchResult(
        id=rid,
        content=f"content for {rid}",
        distance=0.5,
        collection=collection,
        metadata={"doc_id": doc_id},
        hybrid_score=score,
    )


def test_salience_boost_reorders_by_token_overlap(monkeypatch) -> None:
    """Two results, one with strong salient overlap with the query —
    boost moves it to the top. Ported off the deleted SQLite store
    (nexus-i711w A3): salient sentences now come from
    HttpDocumentAspectsStore, faked at the function-local import seam."""
    fake = _FakeAspectsStore({
        "A": ["irrelevant words"],
        "B": ["hybrid retrieval cross-encoder reranking"],
    })
    monkeypatch.setattr(_ASPECTS_SEAM, lambda: fake)

    from nexus.search_engine import _apply_salience_boost
    results = [
        _make_result("a", "knowledge__rag", "A", score=0.50),
        _make_result("b", "knowledge__rag", "B", score=0.45),
    ]
    out = _apply_salience_boost(
        results, query="hybrid retrieval cross-encoder", weight=0.5,
    )
    assert [r.id for r in out] == ["b", "a"]
    assert out[0].hybrid_score > 0.45  # boosted
    assert fake.closed == 1


def test_salience_boost_ignores_code_collections(monkeypatch) -> None:
    """code__ results pass through unchanged even if doc_id matches a
    row with salient_sentences (Phase 4b: code is opt-in via flag,
    boost gated to knowledge__/docs__ only). The fake would hand back
    boost-bait if consulted; it must never be."""
    fake = _FakeAspectsStore({
        "X": ["anything at all"],
        "Y": ["anything at all"],
    })
    monkeypatch.setattr(_ASPECTS_SEAM, lambda: fake)

    from nexus.search_engine import _apply_salience_boost
    results = [
        _make_result("c1", "code__foo", "X", score=0.70),
        _make_result("c2", "code__foo", "Y", score=0.60),
    ]
    out = _apply_salience_boost(results, query="anything", weight=0.5)
    assert [r.id for r in out] == ["c1", "c2"]
    assert out[0].hybrid_score == pytest.approx(0.70)
    assert fake.requested == []  # aspects store never consulted


# TOMBSTONE (nexus-i711w Stage 2 A3): test_salience_boost_no_op_when_db_missing
# deleted. Its premise — a missing local ``memory.db`` file makes
# ``_apply_salience_boost`` early-return — DIED with the SQLite arm.
# The HTTP path has no local-file existence check to no-op on.


def test_salience_boost_no_op_when_no_doc_id(monkeypatch) -> None:
    """Result without doc_id metadata passes through unchanged and the
    aspects store is never queried for it (ported to the http seam,
    nexus-i711w A3)."""
    fake = _FakeAspectsStore({"any": ["boost bait"]})
    monkeypatch.setattr(_ASPECTS_SEAM, lambda: fake)

    from nexus.search_engine import _apply_salience_boost
    r = SearchResult(
        id="r", content="", distance=0.5, collection="knowledge__x",
        metadata={}, hybrid_score=0.5,
    )
    out = _apply_salience_boost([r], query="q", weight=0.5)
    assert out == [r]
    assert fake.requested == []
    assert fake.closed == 1  # store is constructed for knowledge__ results and must be closed


def test_salience_boost_routes_via_http_store(monkeypatch) -> None:
    """nexus-g8r2h fold (sweep [21089] item 8): the boost must read
    salient_sentences through HttpDocumentAspectsStore — the old direct
    DocumentAspects(memory.db) read served STALE frozen pre-migration
    rows. The routing seam is now COLLAPSED (nexus-i711w A3): the http
    store is constructed unconditionally, no storage_backend_for call
    remains, so no service-mode pin is needed. Also pins that the
    store's close() is called (it closes the httpx pool — load-bearing,
    reviewer Low)."""
    calls: dict = {"salient": [], "closed": 0}

    class _FakeHttpAspects:
        def get_salient_sentences(self, doc_id: str) -> list[str]:
            calls["salient"].append(doc_id)
            return ["hybrid retrieval cross-encoder reranking"] if doc_id == "B" else []

        def close(self) -> None:
            calls["closed"] += 1

    monkeypatch.setattr(_ASPECTS_SEAM, lambda: _FakeHttpAspects())

    from nexus.search_engine import _apply_salience_boost
    results = [
        _make_result("a", "knowledge__rag", "A", score=0.50),
        _make_result("b", "knowledge__rag", "B", score=0.45),
    ]
    out = _apply_salience_boost(
        results, query="hybrid retrieval cross-encoder", weight=0.5,
    )
    assert [r.id for r in out] == ["b", "a"]
    assert sorted(calls["salient"]) == ["A", "B"]
    assert calls["closed"] == 1
