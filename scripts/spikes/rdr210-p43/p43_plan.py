# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dry ``--plan``: what a run would build, ask and spend. Pure; it reads the
local corpus files and nothing else, and calls no engine and no service.

Voyage spend is ESTIMATED (characters / 3.3):

* embeds: every character of the prose corpus (voyage-context-3) and the code
  corpus (voyage-code-3) written to its Voyage collection;
* query embeds: one per Voyage-collection search (measurement A, and the three
  surfaces of C);
* rerank (only when the engine's reranker is a Voyage model, i.e. a Voyage
  PROFILE engine; a bge-profile engine reranks locally even when keyed): measurement B's
  rerank requests and surface 1's candidate page.

The cap check covers embeds + query embeds + the rerank figure when
``assume_voyage_rerank`` is set.
"""
from __future__ import annotations

from dataclasses import dataclass

import p43_budget as budget
import p43_engine as eng
from p43_corpus import Corpus
from p43_ingest import CollectionSpec
from p43_measure_b import POOL_SIZE
from p43_queries import Query

#: Per query, rerank requests of measurement B: X twice, A, B, and X' (shuffled) = 60+60+30+30+60.
B_RERANKED_DOCS = 240
#: Surface 1 reranks at most this many candidates per page row (per_collection_k = n * 4).
C_RERANK_MULTIPLIER = 4
C_PAGE = 20
#: Voyage-collection searches per query: A (1) and C's three surfaces (1 each).
VOYAGE_SEARCHES_PER_QUERY = 4


@dataclass(frozen=True)
class Plan:
    mode: str
    corpora: dict
    queries: dict
    collections: list[str]
    pool_collections: int
    estimates: dict
    total_tokens: int
    cap: int
    refused: bool


def collections_for(corpora: dict[str, Corpus], keyless: bool) -> list[str]:
    out = []
    for kind in ("prose", "code"):
        c = corpora[kind]
        out.append(CollectionSpec.for_corpus(c, eng.BGE_MODEL).name)
        if not keyless:
            vm = eng.CONTEXT_MODEL if kind == "prose" else eng.CODE_MODEL
            out.append(CollectionSpec.for_corpus(c, vm).name)
    return out


def make_plan(corpora: dict[str, Corpus], queries: list[Query], *, keyless: bool, cap: int,
              assume_voyage_rerank: bool = False) -> Plan:
    n_q = len(queries)
    avg_chunk_chars = (sum(c.n_chars for c in corpora.values())
                       / max(1, sum(c.n_chunks for c in corpora.values())))
    embed_chars = 0 if keyless else sum(c.n_chars for c in corpora.values())
    embed_tokens = budget.chars_to_tokens(embed_chars)
    avg_q = sum(len(q.text) for q in queries) / max(1, n_q)
    query_tokens = 0 if keyless else n_q * VOYAGE_SEARCHES_PER_QUERY * budget.chars_to_tokens(int(avg_q))
    per_doc = budget.chars_to_tokens(int(avg_q + avg_chunk_chars))
    rerank_b = n_q * B_RERANKED_DOCS * per_doc
    rerank_c = 0 if keyless else n_q * C_PAGE * C_RERANK_MULTIPLIER * per_doc
    rerank_total = rerank_b + rerank_c
    counted = embed_tokens + query_tokens + (rerank_total if assume_voyage_rerank else 0)
    pools_per_query = 4
    plan = Plan(
        mode="keyless (measurement B only)" if keyless else "full (A, B, C)",
        corpora={k: {"files": len(c.files), "chunks": c.n_chunks, "chars": c.n_chars,
                     "corpus_hash": c.short_hash} for k, c in corpora.items()},
        queries={k: sum(1 for q in queries if q.set == k) for k in ("prose", "code")},
        collections=collections_for(corpora, keyless),
        pool_collections=n_q * pools_per_query,
        estimates={
            "embed_chars_to_voyage": embed_chars,
            "embed_tokens": embed_tokens,
            "voyage_query_embed_tokens": query_tokens,
            "voyage_rerank_tokens_if_voyage_reranker": 0 if keyless else rerank_total,
            "voyage_rerank_tokens_counted": rerank_total if assume_voyage_rerank else 0,
            "pool_size": POOL_SIZE,
            "chars_per_token": budget.CHARS_PER_TOKEN,
        },
        total_tokens=counted, cap=cap, refused=counted > cap,
    )
    return plan


def format_plan(p: Plan) -> str:
    lines = [f"RDR-210 P4.3 harness plan ({p.mode}); nothing is called", ""]
    lines.append("Corpora")
    for k, v in p.corpora.items():
        lines.append(f"  {k:5s} {v['files']:3d} files {v['chunks']:5d} chunks {v['chars']:9,d} chars  hash {v['corpus_hash']}")
    lines.append("Queries   " + ", ".join(f"{k} {n}" for k, n in p.queries.items()))
    lines.append("Collections (corpus)")
    lines.extend(f"  {c}" for c in p.collections)
    lines.append(f"Pool collections for measurement B: {p.pool_collections} "
                 f"(per query: single, two halves, shuffled single; {p.estimates['pool_size']} chunks in the pool; bge only)")
    e = p.estimates
    lines.append("Voyage spend estimate (characters / 3.3)")
    lines.append(f"  corpus embeds         {e['embed_chars_to_voyage']:>12,d} chars -> {e['embed_tokens']:>10,d} tokens")
    lines.append(f"  query embeds                                 {e['voyage_query_embed_tokens']:>10,d} tokens")
    lines.append(f"  rerank, ONLY if the engine's reranker is Voyage (a Voyage-profile engine; "
                 f"a bge-profile engine reranks locally): {e['voyage_rerank_tokens_if_voyage_reranker']:,d} tokens")
    lines.append(f"  counted against the cap                      {p.total_tokens:>10,d} tokens (cap {p.cap:,d})")
    lines.append("REFUSED: the estimate is over the cap; raise it with --cap-tokens only if that spend is intended"
                 if p.refused else "within the cap")
    return "\n".join(lines)
