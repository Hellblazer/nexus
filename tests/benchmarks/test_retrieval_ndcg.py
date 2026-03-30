# SPDX-License-Identifier: AGPL-3.0-or-later
"""NDCG math tests and retrieval smoke test.

The NDCG math functions (dcg_at_k, ndcg_at_k) are tested with known
input/output pairs. The retrieval smoke test verifies the embedding +
search pipeline runs end-to-end against a synthetic corpus using ONNX
MiniLM — it does NOT test production retrieval quality (production uses
Voyage AI models with different dimensionality and training data).
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

slow = pytest.mark.slow


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


# ── Retrieval smoke test ────────────────────────────────────────────────────
#
# This is a SMOKE TEST, not a quality benchmark.
#
# It verifies: EphemeralClient + ONNX MiniLM + synthetic corpus → search
# pipeline runs end-to-end and returns topically relevant results.
#
# It does NOT test production retrieval quality because:
# 1. Production uses Voyage AI (voyage-context-3, voyage-code-3), not ONNX MiniLM
# 2. The synthetic corpus and ground-truth were authored together
# 3. The threshold was calibrated from the first run (not independently set)
#
# For production quality measurement, use real queries with independently
# assessed relevance grades and Voyage AI embeddings (requires API keys).


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


@slow
def test_retrieval_smoke(benchmark_collection) -> None:
    """Smoke test: ONNX MiniLM retrieval pipeline runs end-to-end.

    Verifies that the search pipeline returns topically plausible results
    for a synthetic corpus. This is NOT a proxy for Voyage AI production
    quality — see module docstring.
    """
    queries_path = _BENCHMARK_DIR / "queries.json"
    queries: list[dict] = json.loads(queries_path.read_text())

    k = 5
    ndcg_scores: list[float] = []

    for item in queries:
        query_text: str = item["query"]
        expected: list[dict] = item["expected"]

        relevance_map: dict[str, int] = {
            e["doc_id"]: e["relevance"] for e in expected
        }
        ideal_grades: list[int] = [e["relevance"] for e in expected]

        results = benchmark_collection.query(
            query_texts=[query_text],
            n_results=k,
        )
        returned_ids: list[str] = results["ids"][0]

        retrieved_relevances: list[int] = [
            relevance_map.get(doc_id, 0) for doc_id in returned_ids
        ]

        score = ndcg_at_k(retrieved_relevances, ideal_grades, k)
        ndcg_scores.append(score)

    mean_ndcg = sum(ndcg_scores) / len(ndcg_scores)

    # Smoke threshold: pipeline should retrieve topically relevant results.
    # 0.50 is generous — a random ranking on this corpus scores ~0.3.
    # This catches pipeline breakage, not quality regressions.
    assert mean_ndcg >= 0.50, (
        f"Retrieval smoke test failed: mean NDCG@5={mean_ndcg:.4f} < 0.50 "
        f"(min={min(ndcg_scores):.4f} max={max(ndcg_scores):.4f})"
    )
