#!/usr/bin/env python3
"""ColBERT late-interaction recall spike — measurement scaffold only.

Per ``docs/proposals/m3docrag-application.md`` §6. This script compares
recall@10 and MRR@10 between two retrieval pipelines on the same T3
chunk sample:

  Baseline   — Voyage ``voyage-context-3`` single-vector embeddings
               (already present in T3), scored by cosine similarity.
  Candidate  — ColBERT late-interaction multi-vector embeddings via
               ``pylate`` (CPU-compatible), scored by MaxSim.

Decision rule from the proposal: if the candidate beats the baseline by
≥5 % absolute recall@10 on a target collection (RDR or code), file a
bead for an opt-in late-interaction parallel retriever. Otherwise reject
as not worth the index-size cost.

This script does NOT modify production state. It reads from T3, builds
an in-memory ColBERT index, runs queries, prints metrics. No T3 writes,
no migrations, no MCP side effects.

Usage:
  uv run python scripts/colbert_recall_spike.py \\
      --collection rdr__nexus-571b8edd__voyage-context-3__v1 \\
      --sample-size 200 \\
      --n-queries 50

Optional deps (install before running the candidate path):
  pip install pylate sentence-transformers

If ``pylate`` is missing the script reports baseline metrics only and
exits 0 so it can be wired into CI as a doc-only check until the
optional dep lands.

Synthetic query generation: extract salient n-grams from chunk section
titles. Each query's "relevant" chunk is the source from which the
n-gram was extracted (single-relevant ground truth, recall@10 is a
hit/miss). This is a weak ground truth — query terms appear verbatim in
the relevant chunk — but the bias applies equally to both pipelines so
the relative comparison is informative for the +5 % threshold.

Limitations:
  - Single-relevant ground truth biases toward exact-match. A richer
    eval would use human-labelled relevance judgments.
  - Sample size of 200 is intentionally small for a CPU spike. Scale up
    if results are within noise margin (<2 % gap).
  - ColBERT model defaults to ``colbert-ir/colbertv2.0``. Other models
    (e.g., ``answerdotai/answerai-colbert-small-v1``) may give different
    results; the script reports the model name in the output.
"""
from __future__ import annotations

import argparse
import math
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass
class RetrievalResult:
    """One pipeline's metrics on the same query set."""

    pipeline: str
    n_queries: int
    recall_at_10: float
    mrr_at_10: float
    avg_query_latency_ms: float
    index_size_floats: int
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pipeline": self.pipeline,
            "n_queries": self.n_queries,
            "recall@10": round(self.recall_at_10, 4),
            "mrr@10": round(self.mrr_at_10, 4),
            "avg_query_latency_ms": round(self.avg_query_latency_ms, 2),
            "index_size_floats": self.index_size_floats,
            "notes": self.notes,
        }


# ── T3 sampling ──────────────────────────────────────────────────────────────


def load_chunks(
    collection: str,
    sample_size: int,
) -> tuple[list[str], list[str], list[list[float]]]:
    """Return (chunk_ids, chunk_texts, voyage_embeddings) from T3.

    The Voyage embedding is fetched alongside the text so we don't
    re-embed for the baseline path. ``include=["embeddings",
    "documents", "metadatas"]`` keeps the round-trip to one call per
    300-chunk page.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

    t3 = make_t3()
    coll = t3.get_collection(collection)
    page_limit = min(QUOTAS.MAX_QUERY_RESULTS, sample_size)

    ids: list[str] = []
    texts: list[str] = []
    embeddings: list[list[float]] = []
    offset = 0
    while len(ids) < sample_size:
        page = coll.get(
            limit=page_limit,
            offset=offset,
            include=["embeddings", "documents", "metadatas"],
        )
        page_ids = page.get("ids") or []
        page_docs = page.get("documents") or []
        page_emb = page.get("embeddings")
        if page_emb is None:
            raise RuntimeError(
                f"collection {collection!r} returned no embeddings — was "
                "it indexed with a Voyage embedder? Local-ONNX collections "
                "are not supported by this spike."
            )
        if not page_ids:
            break
        ids.extend(page_ids)
        texts.extend(page_docs)
        embeddings.extend([list(e) for e in page_emb])
        offset += len(page_ids)
        if len(page_ids) < page_limit:
            break
    if len(ids) < sample_size:
        print(
            f"warning: requested {sample_size} chunks but collection only "
            f"yielded {len(ids)}; continuing with the smaller sample."
        )
    return ids[:sample_size], texts[:sample_size], embeddings[:sample_size]


# ── Synthetic query generation ───────────────────────────────────────────────

_WORD_RE = re.compile(r"\b\w+\b")
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with",
    "by", "is", "are", "was", "were", "be", "been", "being", "as", "at",
    "from", "that", "this", "it", "its", "into", "but", "not", "no",
}


def _salient_ngrams(text: str, n: int = 3, max_per_doc: int = 1) -> list[str]:
    """Pick up to ``max_per_doc`` salient n-grams from ``text``.

    Salience heuristic: ignore stopwords, prefer n-grams whose tokens
    are all >3 chars. Picks from the first 200 tokens (favours section
    titles and intro prose; section titles in RDR/code corpora are
    information-dense).
    """
    tokens = [t.lower() for t in _WORD_RE.findall(text)][:200]
    out: list[str] = []
    for i in range(len(tokens) - n + 1):
        gram = tokens[i : i + n]
        if any(t in _STOPWORDS or len(t) <= 3 for t in gram):
            continue
        out.append(" ".join(gram))
        if len(out) >= max_per_doc:
            break
    return out


def build_query_set(
    chunk_ids: list[str],
    chunk_texts: list[str],
    n_queries: int,
) -> list[tuple[str, str]]:
    """Return ``[(query, relevant_chunk_id), ...]`` of length ≤ n_queries."""
    pool: list[tuple[str, str]] = []
    for cid, text in zip(chunk_ids, chunk_texts):
        for gram in _salient_ngrams(text):
            pool.append((gram, cid))
    # Dedupe queries — if the same n-gram came from two chunks, drop
    # both (ambiguous relevance) to keep the ground truth single-target.
    seen: dict[str, int] = {}
    for q, _ in pool:
        seen[q] = seen.get(q, 0) + 1
    return [(q, cid) for q, cid in pool if seen[q] == 1][:n_queries]


# ── Baseline: Voyage cosine ──────────────────────────────────────────────────


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def run_voyage_baseline(
    chunk_ids: list[str],
    chunk_embeddings: list[list[float]],
    queries: list[tuple[str, str]],
) -> RetrievalResult:
    """Score every query against every chunk via cosine on the existing
    Voyage embeddings. Embeds queries on the fly via the same Voyage
    model the collection was indexed with.
    """
    import os  # noqa: PLC0415

    import voyageai  # noqa: PLC0415

    api_key = os.environ.get("VOYAGE_API_KEY") or os.environ.get("VOYAGE_API_KEY_2")
    if not api_key:
        raise RuntimeError(
            "VOYAGE_API_KEY (or VOYAGE_API_KEY_2) must be set to embed "
            "queries with the baseline pipeline."
        )
    client = voyageai.Client(api_key=api_key, max_retries=2)

    query_strs = [q for q, _ in queries]
    relevant_ids = [cid for _, cid in queries]
    t0 = time.perf_counter()
    # voyage-context-3 uses contextualized chunk embeddings; for query
    # embedding we call .embed with model=voyage-context-3 and
    # input_type="query". Matches the indexer's contract.
    resp = client.embed(
        query_strs, model="voyage-context-3", input_type="query",
    )
    query_embeds = resp.embeddings
    embed_elapsed = time.perf_counter() - t0

    hits = 0
    mrr_sum = 0.0
    score_elapsed = 0.0
    for q_emb, relevant_id in zip(query_embeds, relevant_ids):
        t1 = time.perf_counter()
        scores = [(cid, _cosine(q_emb, c_emb))
                  for cid, c_emb in zip(chunk_ids, chunk_embeddings)]
        scores.sort(key=lambda x: x[1], reverse=True)
        score_elapsed += time.perf_counter() - t1
        top10 = [cid for cid, _ in scores[:10]]
        if relevant_id in top10:
            hits += 1
            rank = top10.index(relevant_id) + 1
            mrr_sum += 1.0 / rank

    n = len(queries)
    avg_lat = (embed_elapsed + score_elapsed) / max(n, 1) * 1000.0
    index_floats = len(chunk_embeddings) * (len(chunk_embeddings[0]) if chunk_embeddings else 0)
    return RetrievalResult(
        pipeline="voyage-context-3 (single-vector cosine)",
        n_queries=n,
        recall_at_10=hits / max(n, 1),
        mrr_at_10=mrr_sum / max(n, 1),
        avg_query_latency_ms=avg_lat,
        index_size_floats=index_floats,
        notes=f"embed+score per query; existing T3 embeddings",
    )


# ── Candidate: ColBERT late interaction via pylate ───────────────────────────


def run_colbert_candidate(
    chunk_ids: list[str],
    chunk_texts: list[str],
    queries: list[tuple[str, str]],
    model_name: str = "colbert-ir/colbertv2.0",
) -> RetrievalResult | None:
    """Build an in-memory ColBERT index over ``chunk_texts`` and score
    each query via MaxSim. Returns ``None`` when ``pylate`` is missing
    so the caller can downgrade to baseline-only metrics.
    """
    try:
        from pylate import indexes, models, retrieve  # noqa: PLC0415
    except ImportError:
        return None

    model = models.ColBERT(model_name_or_path=model_name)
    index = indexes.PLAID(
        index_folder="/tmp/colbert-spike-index",
        index_name="spike",
        override=True,
    )

    t0 = time.perf_counter()
    doc_embeds = model.encode(chunk_texts, batch_size=8, is_query=False, show_progress_bar=False)
    index.add_documents(documents_ids=chunk_ids, documents_embeddings=doc_embeds)
    index_elapsed = time.perf_counter() - t0

    retriever = retrieve.ColBERT(index=index)
    query_strs = [q for q, _ in queries]
    relevant_ids = [cid for _, cid in queries]

    t1 = time.perf_counter()
    q_embeds = model.encode(query_strs, batch_size=8, is_query=True, show_progress_bar=False)
    embed_elapsed = time.perf_counter() - t1

    t2 = time.perf_counter()
    scores = retriever.retrieve(queries_embeddings=q_embeds, k=10)
    retrieve_elapsed = time.perf_counter() - t2

    hits = 0
    mrr_sum = 0.0
    for relevant_id, query_scores in zip(relevant_ids, scores):
        top10 = [r["id"] for r in query_scores]
        if relevant_id in top10:
            hits += 1
            rank = top10.index(relevant_id) + 1
            mrr_sum += 1.0 / rank

    n = len(queries)
    avg_lat = (embed_elapsed + retrieve_elapsed) / max(n, 1) * 1000.0

    # Index size estimate: pylate stores per-token 128-dim vectors.
    # Approximate token count as len(text) / 4 (rough English heuristic).
    approx_tokens = sum(len(t) // 4 for t in chunk_texts)
    index_floats = approx_tokens * 128

    return RetrievalResult(
        pipeline=f"colbert ({model_name})",
        n_queries=n,
        recall_at_10=hits / max(n, 1),
        mrr_at_10=mrr_sum / max(n, 1),
        avg_query_latency_ms=avg_lat,
        index_size_floats=index_floats,
        notes=f"index built in {index_elapsed:.1f}s; per-token MaxSim",
    )


# ── Driver ──────────────────────────────────────────────────────────────────


def _print_result(r: RetrievalResult) -> None:
    print(f"  pipeline           : {r.pipeline}")
    print(f"  n_queries          : {r.n_queries}")
    print(f"  recall@10          : {r.recall_at_10:.4f}")
    print(f"  mrr@10             : {r.mrr_at_10:.4f}")
    print(f"  avg query latency  : {r.avg_query_latency_ms:.2f} ms")
    print(f"  index size (floats): {r.index_size_floats:,}")
    if r.notes:
        print(f"  notes              : {r.notes}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--collection",
        required=True,
        help="T3 collection name (e.g. rdr__nexus-571b8edd__voyage-context-3__v1).",
    )
    parser.add_argument(
        "--sample-size", type=int, default=200,
        help="How many chunks to sample (default 200).",
    )
    parser.add_argument(
        "--n-queries", type=int, default=50,
        help="How many synthetic queries to generate (default 50).",
    )
    parser.add_argument(
        "--colbert-model",
        default="colbert-ir/colbertv2.0",
        help="Hugging Face model name for the ColBERT candidate.",
    )
    args = parser.parse_args()

    print(f"\n=== ColBERT recall spike — {args.collection} ===\n")

    print(f"loading up to {args.sample_size} chunks from T3...")
    chunk_ids, chunk_texts, embeddings = load_chunks(args.collection, args.sample_size)
    print(f"  loaded {len(chunk_ids)} chunks "
          f"({len(embeddings[0]) if embeddings else 0}-dim Voyage embeddings)")

    print(f"\nbuilding synthetic query set (target {args.n_queries})...")
    queries = build_query_set(chunk_ids, chunk_texts, args.n_queries)
    print(f"  generated {len(queries)} queries with single-target ground truth")

    print("\n--- baseline: voyage-context-3 cosine ---")
    baseline = run_voyage_baseline(chunk_ids, embeddings, queries)
    _print_result(baseline)

    print("\n--- candidate: ColBERT late-interaction ---")
    candidate = run_colbert_candidate(
        chunk_ids, chunk_texts, queries, model_name=args.colbert_model,
    )
    if candidate is None:
        print("  pylate not installed — skipping candidate path.")
        print("  install with: pip install pylate sentence-transformers")
        print("\n--- decision ---")
        print("  candidate not measured. Baseline recall@10 = "
              f"{baseline.recall_at_10:.4f}. Re-run after install.")
        return 0
    _print_result(candidate)

    print("\n--- decision ---")
    delta = candidate.recall_at_10 - baseline.recall_at_10
    print(f"  recall@10 lift     : {delta:+.4f} ({delta * 100:+.1f} pp)")
    if delta >= 0.05:
        print("  threshold met (≥5 pp). File a bead for an opt-in "
              "late-interaction parallel retriever.")
    else:
        print("  threshold NOT met. Reject adoption per proposal §6.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
