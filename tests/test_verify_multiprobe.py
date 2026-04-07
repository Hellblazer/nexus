# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for multi-probe verify_collection_deep() — Phase 1b of RDR-056.

Uses EphemeralClient + DefaultEmbeddingFunction — no API keys needed.
"""
from __future__ import annotations

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.t3 import T3Database, VerifyResult, verify_collection_deep


def _make_db() -> T3Database:
    """Fresh T3Database backed by EphemeralClient + ONNX MiniLM."""
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    return T3Database(_client=client, _ef_override=ef, local_mode=True, local_path="/tmp/test_verify")


def _add_docs(db: T3Database, collection: str, docs: list[tuple[str, str]]) -> None:
    """Add (id, text) pairs to a collection via get_or_create_collection."""
    col = db.get_or_create_collection(collection)
    ids = [d[0] for d in docs]
    documents = [d[1] for d in docs]
    metadatas = [{"title": d[0], "source_path": f"/test/{d[0]}.txt", "file_path": f"/test/{d[0]}.txt"} for d in docs]
    col.add(ids=ids, documents=documents, metadatas=metadatas)


# ── VerifyResult dataclass ────────────────────────────────────────────────────

def test_verify_result_has_probe_hit_rate_field() -> None:
    """VerifyResult has probe_hit_rate field with None default."""
    result = VerifyResult(status="healthy", doc_count=10)
    assert result.probe_hit_rate is None


def test_verify_result_probe_hit_rate_can_be_set() -> None:
    """probe_hit_rate can be set to a float."""
    result = VerifyResult(status="healthy", doc_count=10, probe_hit_rate=0.8)
    assert result.probe_hit_rate == pytest.approx(0.8)


# ── Full-hit: 5/5 probes found ────────────────────────────────────────────────

def test_five_of_five_probes_healthy() -> None:
    """5/5 probes found → status='healthy', probe_hit_rate=1.0."""
    db = _make_db()
    docs = [
        ("doc-1", "The quick brown fox jumps over the lazy dog near the river bank"),
        ("doc-2", "Python programming language uses indentation for code blocks syntax"),
        ("doc-3", "Machine learning models require large amounts of training data samples"),
        ("doc-4", "Database indexing improves query performance by creating efficient lookups"),
        ("doc-5", "Network security protocols protect data transmission over internet connections"),
        ("doc-6", "Software testing ensures code quality through automated verification checks"),
    ]
    _add_docs(db, "knowledge__multiprobe_full", docs)

    result = verify_collection_deep(db, "knowledge__multiprobe_full")

    assert result.status == "healthy"
    assert result.probe_hit_rate == pytest.approx(1.0)
    assert result.doc_count == 6
    assert result.probe_doc_id is not None


# ── Partial-hit: degraded ─────────────────────────────────────────────────────

def test_partial_probes_degraded() -> None:
    """partial probes found → status='degraded', probe_hit_rate between 0 and 1."""
    db = _make_db()
    # Add enough documents so that some probes may not be in top-10
    # We mock this by using a collection name and patching db.search
    from unittest.mock import patch

    docs = [
        ("doc-a", "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"),
        ("doc-b", "Red green blue yellow orange purple violet indigo cyan magenta pink"),
        ("doc-c", "Apple banana cherry date elderberry fig grape honeydew kiwi lemon"),
        ("doc-d", "Mountain valley river ocean desert forest jungle tundra savanna"),
        ("doc-e", "Hydrogen helium lithium beryllium boron carbon nitrogen oxygen"),
    ]
    col_name = "knowledge__multiprobe_partial"
    _add_docs(db, col_name, docs)

    # Only return hits for some probe IDs to simulate partial degradation
    original_search = db.search
    call_count = [0]

    def fake_search(query, collection_names, n_results=10, **kwargs):
        call_count[0] += 1
        results = original_search(query=query, collection_names=collection_names, n_results=n_results)
        # On even calls, return empty (simulate miss)
        if call_count[0] % 2 == 0:
            return []
        return results

    with patch.object(db, "search", side_effect=fake_search):
        result = verify_collection_deep(db, col_name)

    assert result.status == "degraded"
    assert 0 < result.probe_hit_rate < 1.0
    assert result.doc_count == 5


# ── Zero-hit: broken ─────────────────────────────────────────────────────────

def test_zero_probes_broken() -> None:
    """0/N probes found → status='broken', probe_hit_rate=0.0."""
    from unittest.mock import patch

    db = _make_db()
    docs = [
        ("doc-x1", "Completely unrelated content about architecture and design patterns"),
        ("doc-x2", "Another document with different topics covering software engineering"),
        ("doc-x3", "Third document about mathematics and theoretical computer science"),
    ]
    col_name = "knowledge__multiprobe_broken"
    _add_docs(db, col_name, docs)

    # Simulate all searches returning empty
    with patch.object(db, "search", return_value=[]):
        result = verify_collection_deep(db, col_name)

    assert result.status == "broken"
    assert result.probe_hit_rate == pytest.approx(0.0)
    assert result.doc_count == 3


# ── Fewer than 5 docs: denominator adjusts ───────────────────────────────────

def test_fewer_than_five_docs_denominator_adjusts() -> None:
    """Collection with <5 docs: peek returns available count, denominator adjusts."""
    db = _make_db()
    docs = [
        ("few-1", "The solar system contains eight planets orbiting around the sun"),
        ("few-2", "Quantum mechanics describes the behavior of subatomic particles energy"),
        ("few-3", "Evolution explains the diversity of life through natural selection"),
    ]
    col_name = "knowledge__multiprobe_few"
    _add_docs(db, col_name, docs)

    result = verify_collection_deep(db, col_name)

    # Should work fine with 3 docs (all probed)
    assert result.status in ("healthy", "degraded", "broken")
    assert result.probe_hit_rate is not None
    assert 0.0 <= result.probe_hit_rate <= 1.0
    assert result.doc_count == 3


# ── <2 docs: skipped ─────────────────────────────────────────────────────────

def test_collection_with_one_doc_skipped() -> None:
    """Collection with <2 docs → status='skipped', probe_hit_rate=None."""
    db = _make_db()
    docs = [
        ("single-1", "Only one document in this collection"),
    ]
    col_name = "knowledge__multiprobe_single"
    _add_docs(db, col_name, docs)

    result = verify_collection_deep(db, col_name)

    assert result.status == "skipped"
    assert result.probe_hit_rate is None
    assert result.doc_count == 1


def test_empty_collection_skipped() -> None:
    """Empty collection → status='skipped', probe_hit_rate=None."""
    db = _make_db()
    col_name = "knowledge__multiprobe_empty"
    db.get_or_create_collection(col_name)  # create but don't add docs

    result = verify_collection_deep(db, col_name)

    assert result.status == "skipped"
    assert result.probe_hit_rate is None
    assert result.doc_count == 0


# ── probe_hit_rate=None on skipped ───────────────────────────────────────────

def test_probe_hit_rate_none_on_skipped() -> None:
    """Skipped result always has probe_hit_rate=None."""
    result = VerifyResult(status="skipped", doc_count=0)
    assert result.probe_hit_rate is None


# ── Local mode metric ─────────────────────────────────────────────────────────

def test_local_mode_uses_l2_metric() -> None:
    """In local_mode, metric defaults to l2 (hnsw:space)."""
    db = _make_db()
    docs = [
        ("metric-1", "Testing local mode metric calculation distance measure"),
        ("metric-2", "Second document for metric verification in local mode test"),
        ("metric-3", "Third document ensuring enough data for probe verification"),
    ]
    col_name = "knowledge__multiprobe_metric"
    _add_docs(db, col_name, docs)

    result = verify_collection_deep(db, col_name)

    if result.status != "skipped":
        assert result.metric == "l2"
