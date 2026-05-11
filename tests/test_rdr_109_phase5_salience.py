# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 5: salience module + DocumentAspects.salient_sentences
I/O + search_engine boost integration.

Tests cover the deterministic surfaces (the salience module wraps the
Phase 4 prototype; the DocumentAspects column adds three narrow
methods; the search-engine wiring is feature-flagged so default-off
behavior is the regression bar).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.db.t2.document_aspects import AspectRecord, DocumentAspects
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


@pytest.fixture
def aspects_db(tmp_path: Path, monkeypatch):
    """Run T2 migrations against a fresh tmp DB so document_aspects has
    the post-migration schema (doc_id PK + salient_sentences column).
    Pre-initialises the catalog at the path the autouse
    ``_isolate_catalog`` fixture configured so je0b runs.
    """
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)
    # ``_isolate_catalog`` (tests/conftest.py) sets NEXUS_CATALOG_PATH
    # to ``tmp_path / "test-catalog"``; init the catalog there so the
    # je0b migration sees the expected file.
    from nexus.catalog.catalog import Catalog
    # je0b's _catalog_db_path_from_conn infers
    # <memory_db_parent>/catalog/.catalog.db regardless of
    # NEXUS_CATALOG_PATH; init there so the migration runs.
    Catalog.init(tmp_path / "catalog")
    from nexus.db.t2 import T2Database
    db = T2Database(tmp_path / "memory.db")
    try:
        yield db.document_aspects
    finally:
        db.close()


def test_set_salient_sentences_round_trip(aspects_db) -> None:
    """Write + read via the legacy-keyed API; verify the JSON round-trips
    through the new ``salient_sentences`` column regardless of whether
    je0b's doc_id PK switch has run."""
    record = AspectRecord(
        collection="knowledge__test",
        source_path="doc.md",
        problem_formulation="p",
        proposed_method="m",
        extracted_at="2026-05-11T00:00:00Z",
        model_version="v1",
        extractor_name="scholarly-paper-v1",
        confidence=0.9,
        doc_id="1.1.42",
        source_uri="file:///tmp/doc.md",
    )
    aspects_db.upsert(record)
    # je0b ran (catalog is initialised in the fixture) so doc_id PK is
    # active and we can use the doc_id-keyed setter directly.
    ok = aspects_db.set_salient_sentences(
        "1.1.42", ["alpha beta", "gamma delta"],
    )
    assert ok is True
    assert aspects_db.get_salient_sentences("1.1.42") == [
        "alpha beta", "gamma delta",
    ]


def test_get_salient_sentences_returns_empty_when_missing(aspects_db) -> None:
    assert aspects_db.get_salient_sentences("never-existed") == []


def test_set_salient_sentences_empty_doc_id_returns_false(aspects_db) -> None:
    assert aspects_db.set_salient_sentences("", ["x"]) is False


def test_set_salient_sentences_targets_no_row_returns_false(aspects_db) -> None:
    assert aspects_db.set_salient_sentences("missing-id", ["x"]) is False


def test_get_salient_sentences_handles_null_column(aspects_db) -> None:
    record = AspectRecord(
        collection="docs__test",
        source_path="x.md",
        problem_formulation=None,
        proposed_method=None,
        extracted_at="2026-05-11T00:00:00Z",
        model_version="v1",
        extractor_name="rdr-frontmatter-v1",
        confidence=0.9,
        doc_id="2.2.42",
        source_uri="file:///tmp/x.md",
    )
    aspects_db.upsert(record)
    # salient_sentences was not set; expect [].
    assert aspects_db.get_salient_sentences("2.2.42") == []


def test_get_salient_sentences_handles_garbage_json(tmp_path: Path) -> None:
    """If a future writer corrupts the JSON, get returns [] not a raise."""
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    db.executescript(
        """
        CREATE TABLE document_aspects (
            doc_id TEXT PRIMARY KEY,
            collection TEXT NOT NULL,
            source_path TEXT,
            extracted_at TEXT NOT NULL,
            model_version TEXT NOT NULL,
            extractor_name TEXT NOT NULL,
            salient_sentences TEXT
        );
        INSERT INTO document_aspects(
            doc_id, collection, source_path, extracted_at,
            model_version, extractor_name, salient_sentences
        )
        VALUES ('x', 'k__t', 's', '2026-05-11', 'v1', 'e1', '{not-json}');
        """
    )
    db.commit()
    db.close()
    da = DocumentAspects(tmp_path / "memory.db")
    try:
        assert da.get_salient_sentences("x") == []
    finally:
        da.close()


# ── search_engine._apply_salience_boost ──────────────────────────────


def _make_result(rid: str, collection: str, doc_id: str, score: float) -> SearchResult:
    return SearchResult(
        id=rid,
        content=f"content for {rid}",
        distance=0.5,
        collection=collection,
        metadata={"doc_id": doc_id},
        hybrid_score=score,
    )


def test_salience_boost_reorders_by_token_overlap(tmp_path: Path, monkeypatch) -> None:
    """Two results, one with strong salient overlap with the query —
    boost moves it to the top."""
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path,
    )
    from nexus.catalog.catalog import Catalog
    # je0b's _catalog_db_path_from_conn infers
    # <memory_db_parent>/catalog/.catalog.db regardless of
    # NEXUS_CATALOG_PATH; init there so the migration runs.
    Catalog.init(tmp_path / "catalog")
    from nexus.db.t2 import T2Database
    db = T2Database(tmp_path / "memory.db")
    da = db.document_aspects
    da.upsert(AspectRecord(
        collection="knowledge__rag",
        source_path="a.md",
        problem_formulation=None, proposed_method=None,
        extracted_at="2026-05-11T00:00:00Z",
        model_version="v1", extractor_name="t", confidence=0.9,
        doc_id="A", source_uri="file:///a",
    ))
    da.upsert(AspectRecord(
        collection="knowledge__rag",
        source_path="b.md",
        problem_formulation=None, proposed_method=None,
        extracted_at="2026-05-11T00:00:00Z",
        model_version="v1", extractor_name="t", confidence=0.9,
        doc_id="B", source_uri="file:///b",
    ))
    da.set_salient_sentences("A", ["irrelevant words"])
    da.set_salient_sentences("B", ["hybrid retrieval cross-encoder reranking"])
    db.close()

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


def test_salience_boost_ignores_code_collections(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path,
    )
    # code__ results pass through unchanged even if doc_id matches a
    # row with salient_sentences (Phase 4b: code is opt-in via flag,
    # boost gated to knowledge__/docs__ only).
    from nexus.search_engine import _apply_salience_boost
    results = [
        _make_result("c1", "code__foo", "X", score=0.70),
        _make_result("c2", "code__foo", "Y", score=0.60),
    ]
    out = _apply_salience_boost(results, query="anything", weight=0.5)
    assert [r.id for r in out] == ["c1", "c2"]
    assert out[0].hybrid_score == pytest.approx(0.70)


def test_salience_boost_no_op_when_db_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path / "absent",
    )
    from nexus.search_engine import _apply_salience_boost
    results = [_make_result("r", "knowledge__x", "doc-1", score=0.50)]
    out = _apply_salience_boost(results, query="q", weight=0.5)
    assert out == results


def test_salience_boost_no_op_when_no_doc_id(tmp_path: Path, monkeypatch) -> None:
    """Result without doc_id metadata passes through unchanged."""
    monkeypatch.setattr(
        "nexus.config.nexus_config_dir", lambda: tmp_path,
    )
    from nexus.db.t2 import T2Database
    db = T2Database(tmp_path / "memory.db")
    db.close()
    from nexus.search_engine import _apply_salience_boost
    r = SearchResult(
        id="r", content="", distance=0.5, collection="knowledge__x",
        metadata={}, hybrid_score=0.5,
    )
    out = _apply_salience_boost([r], query="q", weight=0.5)
    assert out == [r]
