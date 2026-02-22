# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: hybrid scoring, cross-corpus reranking, answer mode, formatters."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_RERANK_MODEL = "rerank-2.5"

# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    id: str
    content: str
    distance: float
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    hybrid_score: float = 0.0


# ── Normalization & scoring ────────────────────────────────────────────────────

_EPSILON = 1e-9


def min_max_normalize(value: float, window: list[float]) -> float:
    """Normalize *value* into [0, 1] using the min/max of *window*.

    Computed over the combined result window (not per-corpus). Returns 0.0
    when all values are identical (denominator collapses to ε).
    """
    lo = min(window)
    hi = max(window)
    return (value - lo) / (hi - lo + _EPSILON)


def hybrid_score(vector_norm: float, frecency_norm: float) -> float:
    """Weighted combination: 0.7 * vector_norm + 0.3 * frecency_norm."""
    return 0.7 * vector_norm + 0.3 * frecency_norm


def apply_hybrid_scoring(
    results: list[SearchResult],
    hybrid: bool,
) -> list[SearchResult]:
    """Compute hybrid scores for *results*.

    For code__ corpora: score = 0.7 * vector_norm + 0.3 * frecency_norm.
    For docs__/knowledge__: score = 1.0 * vector_norm (frecency_score absent).

    If *hybrid* is True but no code__ collections appear in results, a warning
    is printed and all results use 1.0 * vector_norm.
    """
    if not results:
        return results

    has_code = any(r.collection.startswith("code__") for r in results)

    if hybrid and not has_code:
        print(
            "Warning: --hybrid has no effect — no code corpus in scope.",
            file=sys.stderr,
        )

    distances = [r.distance for r in results]
    frecencies = [
        r.metadata.get("frecency_score", 0.0)
        for r in results
        if r.collection.startswith("code__")
    ]

    for r in results:
        v_norm = min_max_normalize(r.distance, distances)
        if hybrid and r.collection.startswith("code__"):
            f_score = r.metadata.get("frecency_score", 0.0)
            f_norm = min_max_normalize(f_score, frecencies) if frecencies else 0.0
            r.hybrid_score = hybrid_score(v_norm, f_norm)
        else:
            r.hybrid_score = v_norm

    return sorted(results, key=lambda r: r.hybrid_score, reverse=True)


# ── Reranking ─────────────────────────────────────────────────────────────────

def _voyage_client():
    """Return a voyageai.Client instance."""
    import voyageai
    from nexus.config import get_credential
    return voyageai.Client(api_key=get_credential("voyage_api_key"))


def rerank_results(
    results: list[SearchResult],
    query: str,
    model: str = _RERANK_MODEL,
    top_k: int | None = None,
) -> list[SearchResult]:
    """Rerank *results* using Voyage AI reranker.

    Returns results sorted by relevance_score descending.
    """
    if not results:
        return results

    n = top_k or len(results)
    documents = [r.content for r in results]
    client = _voyage_client()
    rerank_response = client.rerank(
        query=query,
        documents=documents,
        model=model,
        top_k=n,
    )
    reranked: list[SearchResult] = []
    for item in rerank_response.results:
        r = results[item.index]
        r.hybrid_score = float(item.relevance_score)
        reranked.append(r)
    return reranked


def round_robin_interleave(
    grouped: list[list[SearchResult]],
) -> list[SearchResult]:
    """Interleave multiple result lists in round-robin order."""
    merged: list[SearchResult] = []
    iterators = [iter(g) for g in grouped]
    while iterators:
        next_iters = []
        for it in iterators:
            try:
                merged.append(next(it))
                next_iters.append(it)
            except StopIteration:
                pass
        iterators = next_iters
    return merged


# ── Cross-corpus search ───────────────────────────────────────────────────────

def _t3_for_search():
    """Create a T3Database from credentials."""
    from nexus.config import get_credential
    from nexus.db.t3 import T3Database
    return T3Database(
        tenant=get_credential("chroma_tenant"),
        database=get_credential("chroma_database"),
        api_key=get_credential("chroma_api_key"),
        voyage_api_key=get_credential("voyage_api_key"),
    )


def search_cross_corpus(
    query: str,
    collections: list[str],
    n_results: int,
    t3: Any,
) -> list[SearchResult]:
    """Query each collection independently, returning combined raw results.

    Per-corpus over-fetch: max(5, (n_results // num_corpora) * 2).
    """
    num = len(collections) or 1
    per_k = max(5, (n_results // num) * 2)

    all_results: list[SearchResult] = []
    for col in collections:
        raw = t3.search(query, [col], n_results=per_k)
        for r in raw:
            all_results.append(SearchResult(
                id=r["id"],
                content=r["content"],
                distance=r["distance"],
                collection=col,
                metadata={k: v for k, v in r.items()
                          if k not in {"id", "content", "distance"}},
            ))
    return all_results


# ── Mixedbread fan-out ────────────────────────────────────────────────────────

def _mxbai_client(api_key: str):
    """Return a Mixedbread client."""
    from mixedbread import Mixedbread
    return Mixedbread(api_key=api_key)


def fetch_mxbai_results(
    query: str,
    stores: list[str],
    per_k: int,
) -> list[SearchResult]:
    """Fan-out to Mixedbread stores. Returns [] with a warning if key is unset."""
    from nexus.config import get_credential
    api_key = get_credential("mxbai_api_key")
    if not api_key:
        print("Warning: MXBAI_API_KEY not set — skipping Mixedbread fan-out")
        return []

    client = _mxbai_client(api_key)
    results: list[SearchResult] = []
    for store_id in stores:
        response = client.stores.search(store_id=store_id, query=query, top_k=per_k)
        for chunk in response.chunks:
            _digest = hashlib.sha256(chunk.content.text.encode()).hexdigest()[:16]
            results.append(SearchResult(
                id=f"mxbai__{store_id}__{_digest}",
                content=chunk.content.text,
                distance=1.0 - float(chunk.score),
                collection=f"mxbai__{store_id}",
                metadata={"mxbai_store": store_id, "mxbai_score": float(chunk.score)},
            ))
    return results


# ── Agentic mode ──────────────────────────────────────────────────────────────

def _haiku_refine(query: str, results: list[SearchResult]) -> dict:
    """Ask Haiku whether to refine the query. Returns {"done": True} or {"query": "..."}."""
    import json as _json
    import anthropic

    snippets = "\n".join(
        f"{i}: {r.content[:200]}" for i, r in enumerate(results[:10])
    )
    prompt = (
        f"Query: {query}\n\nTop results:\n{snippets}\n\n"
        "Are these results sufficient? Respond ONLY with valid JSON:\n"
        '{"done": true}  — if results are sufficient\n'
        '{"query": "<refined query>"}  — if results need improvement'
    )
    from nexus.config import get_credential
    client = anthropic.Anthropic(api_key=get_credential("anthropic_api_key"))
    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    if not msg.content:
        return {"done": True}
    text = msg.content[0].text.strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        return {"done": True}


def agentic_search(
    initial_query: str,
    retrieve_fn: Callable[[str], list[SearchResult]],
    max_iterations: int = 3,
) -> list[SearchResult]:
    """Multi-step query refinement loop powered by Haiku.

    1. Initial query → retrieve top results.
    2. Haiku responds {"done": true} or {"query": "<refined>"}.
    3. Repeat up to *max_iterations* total retrievals.
    4. Deduplicate by ID across all iterations.
    """
    seen_ids: set[str] = set()
    combined: list[SearchResult] = []
    query = initial_query

    for _iteration in range(max_iterations):
        new_results = retrieve_fn(query)
        for r in new_results:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                combined.append(r)

        decision = _haiku_refine(query, combined)
        if decision.get("done"):
            break
        refined = decision.get("query", "").strip()
        if refined:
            query = refined
        else:
            break

    return combined


# ── Answer mode ───────────────────────────────────────────────────────────────

def _haiku_answer(query: str, results: list[SearchResult]) -> str:
    """Synthesize an answer using Haiku with <cite i="N"> references."""
    import anthropic

    snippets = "\n".join(
        f"[{i}] {r.metadata.get('source_path', 'unknown')}:"
        f"{r.metadata.get('start_line', '?')}\n{r.content[:400]}"
        for i, r in enumerate(results)
    )
    prompt = (
        f"Answer the question: {query}\n\n"
        f"Use these sources (cite with <cite i=\"N\"> inline):\n{snippets}\n\n"
        "Cite each source by index number. Use <cite i=\"N\"> for single source, "
        "<cite i=\"N-M\"> for a range of consecutive sources."
    )
    from nexus.config import get_credential
    client = anthropic.Anthropic(api_key=get_credential("anthropic_api_key"))
    msg = client.messages.create(
        model=_HAIKU_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def answer_mode(query: str, results: list[SearchResult]) -> str:
    """Synthesize a cited answer for *query* using Haiku.

    Returns: synthesis text with <cite i="N"> inline + numbered citation footer.
    """
    synthesis = _haiku_answer(query, results)

    # Build citation footer
    footer_lines: list[str] = [""]
    for i, r in enumerate(results):
        source_path = r.metadata.get("source_path", "?")
        start_line = r.metadata.get("start_line", "?")
        end_line = r.metadata.get("end_line", "?")
        match_pct = (1.0 - r.distance) * 100
        line_ref = f"{start_line}-{end_line}" if end_line != "?" else str(start_line)
        footer_lines.append(f"{i}: {source_path}:{line_ref} ({match_pct:.1f}% match)")

    return synthesis + "\n".join(footer_lines)


# ── Output formatters ─────────────────────────────────────────────────────────

def format_vimgrep(results: list[SearchResult]) -> list[str]:
    """Format results as ``path:line:0:content`` for editor integration."""
    lines: list[str] = []
    for r in results:
        source_path = r.metadata.get("source_path", "")
        start_line = r.metadata.get("start_line", 0)
        first_line = r.content.splitlines()[0] if r.content else ""
        lines.append(f"{source_path}:{start_line}:0:{first_line}")
    return lines


def format_json(results: list[SearchResult]) -> str:
    """Format results as a JSON array with id, content, distance, and metadata."""
    items: list[dict[str, Any]] = []
    for r in results:
        item: dict[str, Any] = {
            "id": r.id,
            "content": r.content,
            "distance": r.distance,
            "collection": r.collection,
        }
        item.update(r.metadata)
        items.append(item)
    return json.dumps(items, indent=2, default=str)


def format_plain(results: list[SearchResult]) -> list[str]:
    """Default plain-text format: ./path/to/file.py:42:    content."""
    lines: list[str] = []
    for r in results:
        source_path = r.metadata.get("source_path", "")
        start_line = r.metadata.get("start_line", 0)
        for i, content_line in enumerate(r.content.splitlines()):
            line_no = int(start_line) + i if start_line else 0
            lines.append(f"{source_path}:{line_no}:{content_line}")
    return lines
