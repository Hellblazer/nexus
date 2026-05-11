#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-109 Phase 4 calibration harness.

Sweeps the salience boost weight over a candidate set, runs each
held-out QA item through baseline + boosted retrieval, and reports
per-weight top-K hit rate. Output is a per-weight table to stdout plus
a JSON record at ``data/calibration/rdr-109/results.json``.

Usage:

    python scripts/rdr-109-calibrate.py \\
        --content-type knowledge \\
        --collection knowledge__rag-papers \\
        --weights 0.0,0.025,0.05,0.075,0.10,0.15 \\
        --top-k 5

The harness deliberately scopes one content_type per invocation so the
operator can iterate per corpus and inspect intermediate results.

Pareto non-regression: a winning weight is one whose hit rate is
strictly higher than baseline at ``weight=0.0`` AND that does not
demote any query that the baseline ranked in top-K to outside top-K
(no item-level regressions).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "calibration" / "rdr-109"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from rdr_109_salience import (  # noqa: E402
    extract_salient_sentences,
    load_seed_queries,
    token_overlap_boost,
)


@dataclass
class QAItem:
    question: str
    expected_chunk_chash: str
    content_type: str


def load_qa(content_type: str) -> list[QAItem]:
    path = DATA_DIR / f"qa_{content_type}.jsonl"
    items: list[QAItem] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rec = json.loads(line)
        items.append(
            QAItem(
                question=rec["question"],
                expected_chunk_chash=rec["expected_chunk_chash"],
                content_type=rec.get("content_type", content_type),
            )
        )
    return items


@dataclass
class RankedHit:
    chash: str
    base_score: float
    boosted_score: float


def _build_salience_cache(
    chunks: list[dict],
    seed_queries: list[str],
    *,
    top_n_per_chunk: int,
) -> dict[str, list[str]]:
    """Pre-extract salient sentences for every chunk so the boost
    application during the sweep is a cheap dict lookup."""
    cache: dict[str, list[str]] = {}
    for chunk in chunks:
        chash = chunk["chash"]
        text = chunk["text"]
        cache[chash] = extract_salient_sentences(
            text, seed_queries, top_n=top_n_per_chunk,
        )
    return cache


def _baseline_rank(
    qa: QAItem, collection: str, top_k: int,
) -> list[RankedHit]:
    """Baseline retrieval for *qa*: pull top-K chunks by hybrid score
    via ``nx search`` (read-only; mirrors the live search path)."""
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.search_engine import search_one  # noqa: PLC0415

    t3 = make_t3()
    results = search_one(
        t3,
        query=qa.question,
        collection=collection,
        top_k=top_k * 3,  # over-fetch so boost can re-rank within window
    )
    ranked: list[RankedHit] = []
    for r in results:
        chash = (r.metadata or {}).get("chunk_text_hash") or r.id
        ranked.append(
            RankedHit(chash=chash, base_score=r.hybrid_score, boosted_score=r.hybrid_score)
        )
    return ranked


def _apply_boost(
    ranked: list[RankedHit],
    qa: QAItem,
    salience_cache: dict[str, list[str]],
    weight: float,
) -> list[RankedHit]:
    """Apply token-overlap boost and re-sort."""
    boosted: list[RankedHit] = []
    for hit in ranked:
        salients = salience_cache.get(hit.chash, [])
        boost = token_overlap_boost(qa.question, salients, weight=weight)
        boosted.append(
            RankedHit(
                chash=hit.chash,
                base_score=hit.base_score,
                boosted_score=hit.base_score + boost,
            )
        )
    boosted.sort(key=lambda r: r.boosted_score, reverse=True)
    return boosted


def _hit_in_top_k(ranked: list[RankedHit], expected: str, top_k: int) -> bool:
    return any(r.chash == expected for r in ranked[:top_k])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--content-type", required=True,
                    choices=["knowledge", "code", "docs", "rdr"])
    ap.add_argument("--collection", required=True,
                    help="T3 collection name (e.g. knowledge__rag-papers)")
    ap.add_argument("--weights", default="0.0,0.025,0.05,0.075,0.10,0.15",
                    help="Comma-separated boost-weight sweep.")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--salient-per-chunk", type=int, default=3,
                    help="Number of salient sentences kept per chunk.")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "results.json")
    args = ap.parse_args()

    weights = [float(w) for w in args.weights.split(",")]
    qa_items = load_qa(args.content_type)
    seed_queries = load_seed_queries(DATA_DIR, args.content_type)
    if not qa_items:
        print(f"no QA items for {args.content_type}", file=sys.stderr)
        return 2
    print(
        f"[calibrate] content_type={args.content_type} "
        f"collection={args.collection} qa_items={len(qa_items)} "
        f"weights={weights} top_k={args.top_k}",
        flush=True,
    )

    # ── Build per-chunk salience cache, one shot ─────────────────────
    # The cache is reused across every (qa, weight) pair so the
    # cross-encoder cost is paid once per chunk-in-window per run.
    from nexus.db import make_t3  # noqa: PLC0415
    t3 = make_t3()
    coll = t3.get_or_create_collection(args.collection)
    raw = coll.get(include=["documents", "metadatas"])
    chunks = []
    for cid, doc, meta in zip(raw["ids"], raw["documents"], raw["metadatas"]):
        chash = (meta or {}).get("chunk_text_hash") or cid
        chunks.append({"chash": chash, "text": doc or ""})
    t0 = time.time()
    print(f"[calibrate] building salience cache over {len(chunks)} chunks ...", flush=True)
    salience_cache = _build_salience_cache(
        chunks, seed_queries, top_n_per_chunk=args.salient_per_chunk,
    )
    print(f"[calibrate] salience cache built in {time.time() - t0:.1f}s", flush=True)

    # ── Sweep ────────────────────────────────────────────────────────
    summary: dict[float, dict[str, int]] = {}
    per_query: list[dict] = []
    baseline_hits: set[int] = set()
    for qi, qa in enumerate(qa_items):
        ranked_base = _baseline_rank(qa, args.collection, args.top_k)
        base_hit = _hit_in_top_k(ranked_base, qa.expected_chunk_chash, args.top_k)
        if base_hit:
            baseline_hits.add(qi)
        for w in weights:
            ranked = _apply_boost(ranked_base, qa, salience_cache, w) if w > 0 else ranked_base
            hit = _hit_in_top_k(ranked, qa.expected_chunk_chash, args.top_k)
            summary.setdefault(w, Counter())[("hit" if hit else "miss")] += 1
            per_query.append({
                "qa_index": qi, "weight": w, "hit": hit,
                "question": qa.question[:80],
            })

    # ── Pareto non-regression check ──────────────────────────────────
    regressions: dict[float, list[int]] = {}
    for w in weights:
        if w == 0:
            continue
        # A regression: a baseline-hit query that misses at this weight.
        per_w_hits = {r["qa_index"] for r in per_query
                      if r["weight"] == w and r["hit"]}
        missing = sorted(baseline_hits - per_w_hits)
        if missing:
            regressions[w] = missing

    # ── Report ───────────────────────────────────────────────────────
    print()
    print(f"{'weight':>8}  {'hits':>5}  {'total':>5}  {'rate':>6}  {'pareto-ok':>10}")
    for w in weights:
        c = summary[w]
        total = c["hit"] + c["miss"]
        rate = c["hit"] / total if total else 0.0
        pareto_ok = "yes" if (w == 0 or not regressions.get(w)) else f"no ({len(regressions[w])})"
        print(f"{w:>8.4f}  {c['hit']:>5d}  {total:>5d}  {rate:>6.3f}  {pareto_ok:>10}")

    # ── Winner ───────────────────────────────────────────────────────
    baseline_rate = summary[0.0]["hit"] / max(1, sum(summary[0.0].values()))
    winner: float | None = None
    winner_rate = baseline_rate
    for w in weights:
        if w == 0:
            continue
        if regressions.get(w):
            continue
        rate = summary[w]["hit"] / max(1, sum(summary[w].values()))
        if rate > winner_rate:
            winner = w
            winner_rate = rate

    if winner is None:
        print(f"\n[calibrate] NO weight passed Pareto non-regression with rate > baseline {baseline_rate:.3f}.")
        print("[calibrate] surface to RDR-109 revisions per lines 290-293 (Phase-5 deferral or split).")
    else:
        print(f"\n[calibrate] winning weight: {winner} (rate {winner_rate:.3f} vs baseline {baseline_rate:.3f})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "content_type": args.content_type,
        "collection": args.collection,
        "weights": weights,
        "top_k": args.top_k,
        "salient_per_chunk": args.salient_per_chunk,
        "qa_count": len(qa_items),
        "summary": {str(w): dict(summary[w]) for w in weights},
        "regressions": {str(w): regressions.get(w, []) for w in weights},
        "baseline_rate": baseline_rate,
        "winner_weight": winner,
        "winner_rate": winner_rate,
    }, indent=2, sort_keys=True))
    print(f"[calibrate] results written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
