# SPDX-License-Identifier: AGPL-3.0-or-later
"""NDCG retrieval benchmark.

Tests NDCG math functions, then runs a retrieval benchmark against a
synthetic corpus using EphemeralClient + ONNX MiniLM. No API keys needed.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import chromadb
import pytest
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

# ── NDCG math ────────────────────────────────────────────────────────────────

_BENCHMARK_DIR = Path(__file__).parent


def dcg_at_k(relevances: list[int], k: int) -> float:
    """Discounted Cumulative Gain at position k."""
    return sum(
        (2**rel - 1) / math.log2(i + 2)
        for i, rel in enumerate(relevances[:k])
    )


def ndcg_at_k(relevances: list[int], ideal: list[int], k: int) -> float:
    """Normalized DCG at position k."""
    dcg = dcg_at_k(relevances, k)
    idcg = dcg_at_k(sorted(ideal, reverse=True), k)
    return dcg / idcg if idcg > 0 else 0.0


# ── NDCG math tests ──────────────────────────────────────────────────────────


def test_dcg_known_values() -> None:
    """Verify DCG against hand-calculated values.

    For relevances [3, 2, 1] at k=3:
      pos 0: (2^3 - 1) / log2(2) = 7 / 1 = 7.0
      pos 1: (2^2 - 1) / log2(3) = 3 / 1.585 ≈ 1.893
      pos 2: (2^1 - 1) / log2(4) = 1 / 2 = 0.5
      total ≈ 9.393
    """
    result = dcg_at_k([3, 2, 1], k=3)
    expected = 7.0 + 3.0 / math.log2(3) + 1.0 / math.log2(4)
    assert abs(result - expected) < 1e-9


def test_dcg_single_highly_relevant() -> None:
    """Single highly-relevant doc at position 0 yields 7.0."""
    assert dcg_at_k([3], k=1) == pytest.approx(7.0)


def test_dcg_irrelevant_docs() -> None:
    """All-zero relevances yield DCG of 0.0."""
    assert dcg_at_k([0, 0, 0], k=3) == pytest.approx(0.0)


def test_ndcg_perfect_ranking() -> None:
    """Perfect ranking: relevances == ideal → NDCG = 1.0."""
    assert ndcg_at_k([3, 2, 1], [3, 2, 1], k=3) == pytest.approx(1.0)


def test_ndcg_reversed_ranking() -> None:
    """Reversed ranking: worst possible order → NDCG < 1.0."""
    score = ndcg_at_k([1, 2, 3], [3, 2, 1], k=3)
    assert 0.0 < score < 1.0


def test_ndcg_no_relevant() -> None:
    """All-zero relevances (retrieved irrelevant docs) → NDCG = 0.0."""
    assert ndcg_at_k([0, 0, 0], [0, 0, 0], k=3) == pytest.approx(0.0)


def test_ndcg_no_relevant_but_ideal_has_relevant() -> None:
    """Retrieved all zeros but ideal has relevant docs → NDCG = 0.0."""
    assert ndcg_at_k([0, 0, 0], [3, 2, 1], k=3) == pytest.approx(0.0)


def test_ndcg_k_larger_than_results() -> None:
    """k larger than result list is handled gracefully (no IndexError)."""
    score = ndcg_at_k([3, 2], [3, 2, 1], k=10)
    assert 0.0 <= score <= 1.0


def test_ndcg_k_one() -> None:
    """NDCG@1 is 1.0 when the top result is the best possible."""
    assert ndcg_at_k([3], [3], k=1) == pytest.approx(1.0)


def test_ndcg_k_one_suboptimal() -> None:
    """NDCG@1 < 1.0 when top result is not the best."""
    score = ndcg_at_k([2], [3], k=1)
    assert 0.0 < score < 1.0


# ── Benchmark test ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def benchmark_collection():
    """Create an EphemeralClient collection with corpus documents, once per module."""
    corpus_path = _BENCHMARK_DIR / "corpus.json"
    corpus: list[dict] = json.loads(corpus_path.read_text())

    ef = ONNXMiniLM_L6_V2()
    client = chromadb.EphemeralClient()
    collection = client.create_collection(
        name="benchmark",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    ids = [doc["id"] for doc in corpus]
    documents = [doc["content"] for doc in corpus]
    metadatas = [{"type": doc["type"]} for doc in corpus]
    collection.add(ids=ids, documents=documents, metadatas=metadatas)

    return collection


def test_retrieval_ndcg_at_5(benchmark_collection) -> None:
    """Benchmark: mean NDCG@5 across all synthetic queries.

    Uses EphemeralClient + ONNX MiniLM (384d) — no API keys required.
    Threshold is calibrated: actual - 0.05 margin guards against regressions.
    """
    queries_path = _BENCHMARK_DIR / "queries.json"
    queries: list[dict] = json.loads(queries_path.read_text())

    k = 5
    ndcg_scores: list[float] = []

    for item in queries:
        query_text: str = item["query"]
        expected: list[dict] = item["expected"]

        # Build doc_id → relevance lookup (default 0 for unreferenced docs)
        relevance_map: dict[str, int] = {
            e["doc_id"]: e["relevance"] for e in expected
        }
        ideal_grades: list[int] = [e["relevance"] for e in expected]

        # Query the collection directly (option b — no T3 wrapper coupling)
        results = benchmark_collection.query(
            query_texts=[query_text],
            n_results=k,
        )
        returned_ids: list[str] = results["ids"][0]

        # Map returned doc IDs to relevance grades
        retrieved_relevances: list[int] = [
            relevance_map.get(doc_id, 0) for doc_id in returned_ids
        ]

        score = ndcg_at_k(retrieved_relevances, ideal_grades, k)
        ndcg_scores.append(score)

    mean_ndcg = sum(ndcg_scores) / len(ndcg_scores)

    # Calibrated: actual=0.94, threshold=0.94 - 0.05 = 0.89
    # (Calibrated on 2026-03-29 using ONNXMiniLM_L6_V2 + chromadb EphemeralClient)
    calibrated_threshold = 0.89

    assert mean_ndcg >= calibrated_threshold, (
        f"NDCG@5 regression: {mean_ndcg:.4f} < {calibrated_threshold:.4f} "
        f"(scores per query min={min(ndcg_scores):.4f} max={max(ndcg_scores):.4f})"
    )
