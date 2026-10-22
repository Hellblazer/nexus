# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measurement B: is the cross-encoder's score comparable across requests?

210-research-16 showed Voyage rerank scores are comparable across separate
requests, so merging per-model groups by ``rerank_score`` is sound on a
Voyage-profile engine. The design infers the same of the local cross-encoder on a
bge-profile engine (raw logits per document, no per-batch normalisation) and
leaves it for this step to measure.

Per query: a pool of 60 chunks (up to 8 from the target file, the rest the
nearest non-target chunks by bge distance). The pool is built as collections:
X holds all 60; A and B hold 30 each (a seeded split of the same chunk texts);
X' holds the same 60 inserted in a shuffled order. Each is searched through
``/v1/vectors/search`` with ``rerank=true`` and a limit at or above the pool, so
every chunk is scored:

* single  = X
* split   = A and B reranked in separate requests, merged by ``rerank_score``
* noise   = X against X' (the single request with the pool order shuffled), and
            X against X again (run-to-run noise)

PASS when the split-merge Kendall tau is within 0.01 of the noise floor.
"""
from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass

import p43_stats as st
from p43_corpus import Chunk, Corpus
from p43_queries import Query

POOL_SIZE = 60
POOL_TARGETS = 8
TAU_TOLERANCE = 0.01
PAGE = 10


# ── pure parts ──────────────────────────────────────────────────────────────

def build_pool(
    target_hits: Sequence[str], target_all: Sequence[str], others_by_distance: Sequence[str],
    *, size: int = POOL_SIZE, max_targets: int = POOL_TARGETS,
) -> list[str]:
    """Chunk hashes of one pool, in a deterministic order.

    Up to *max_targets* from the target file, those the search already returned
    first (nearest first) and then the file's remaining chunks in position order;
    the rest of *size* filled with the nearest other chunks. Hashes are unique.
    Fewer than *size* are returned only when the sources run out."""
    chosen: list[str] = []
    seen: set[str] = set()
    for h in [*target_hits, *target_all]:
        if len(chosen) >= max_targets:
            break
        if h not in seen:
            seen.add(h)
            chosen.append(h)
    for h in others_by_distance:
        if len(chosen) >= size:
            break
        if h not in seen:
            seen.add(h)
            chosen.append(h)
    return chosen


def split_pool(chashes: Sequence[str], seed: int) -> tuple[list[str], list[str]]:
    """A seeded random split of the pool into two halves (A gets the extra one)."""
    order = sorted(chashes)
    random.Random(seed).shuffle(order)
    half = (len(order) + 1) // 2
    return order[:half], order[half:]


def shuffled(chashes: Sequence[str], seed: int) -> list[str]:
    order = list(chashes)
    random.Random(seed ^ 0x5EED).shuffle(order)
    return order


def merge_by_score(*groups: dict[str, float]) -> dict[str, float]:
    """Union of per-request score maps (disjoint chunk sets)."""
    out: dict[str, float] = {}
    for g in groups:
        for k, v in g.items():
            if k in out:
                raise ValueError(f"chunk {k[:12]} scored in two groups")
            out[k] = v
    return out


@dataclass(frozen=True)
class Agreement:
    tau: float
    top1_same: bool
    top10_overlap: float
    mean_abs_diff: float
    max_abs_diff: float


def _ranked(scores: dict[str, float]) -> list[str]:
    return [k for k, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def compare_scores(a: dict[str, float], b: dict[str, float]) -> Agreement:
    """How two score maps over the same chunks agree: Kendall tau-b of the scores,
    whether the top-ranked chunk is the same, top-10 overlap, and the absolute
    score differences. Raises when the maps cover different chunks."""
    if set(a) != set(b):
        raise ValueError("score maps cover different chunks")
    keys = sorted(a)
    diffs = [abs(a[k] - b[k]) for k in keys]
    ra, rb = _ranked(a), _ranked(b)
    return Agreement(
        tau=st.kendall_tau_b([a[k] for k in keys], [b[k] for k in keys]),
        top1_same=bool(ra and rb and ra[0] == rb[0]),
        top10_overlap=st.top_k_overlap(ra, rb, min(PAGE, len(keys))) if keys else 1.0,
        mean_abs_diff=st.mean(diffs),
        max_abs_diff=max(diffs) if diffs else 0.0,
    )


def summarize(agreements: Sequence[Agreement]) -> dict:
    taus = [a.tau for a in agreements]
    return {
        "queries": len(agreements),
        "tau_mean": st.mean(taus), "tau_min": min(taus) if taus else float("nan"),
        "top1_same": sum(a.top1_same for a in agreements),
        "top10_overlap_mean": st.mean([a.top10_overlap for a in agreements]),
        "mean_abs_score_diff": st.mean([a.mean_abs_diff for a in agreements]),
        "max_abs_score_diff": max((a.max_abs_diff for a in agreements), default=float("nan")),
    }


def verdict_b(split: dict, noise: dict) -> dict:
    gap = noise["tau_mean"] - split["tau_mean"]
    return {
        "rule": f"split-merge mean tau within {TAU_TOLERANCE} of the noise floor",
        "split_tau_mean": split["tau_mean"], "noise_tau_mean": noise["tau_mean"],
        "gap": gap,
        "result": "PASS" if gap <= TAU_TOLERANCE else "FAIL",
    }


def pool_tag(corpus_hash: str, qid: str, chashes: Sequence[str]) -> str:
    """A short id for one query's pool: changes when the corpus or the pool changes,
    so a reused collection always holds exactly this pool."""
    h = hashlib.sha256((corpus_hash + qid + "".join(sorted(chashes))).encode()).hexdigest()
    return h[:8]


# ── impure driver ───────────────────────────────────────────────────────────

class RerankInvalid(RuntimeError):
    """A rerank answer that cannot be measured (degraded, or rows left unscored)."""


def scores_of(rows: list[dict], env: dict, expect: int, what: str) -> dict[str, float]:
    if env.get("rerank_degraded"):
        raise RerankInvalid(f"{what}: rerank degraded ({env.get('rerank_error')})")
    out = {r["id"]: float(r["rerank_score"]) for r in rows if "rerank_score" in r}
    if len(out) != expect:
        raise RerankInvalid(f"{what}: {len(out)} of {expect} chunks carry a rerank_score")
    return out


def run_b(engine, ingest_fn, spec_for, queries: list[Query], corpora: dict[str, Corpus],
          bge_names: dict[str, str], *, seed: int = 210, shuffled_collection: bool = True,
          log=print) -> dict:
    """Build the pools, run the rerank requests, return raw per-query records and the
    aggregate. *ingest_fn(spec, chunks_by_file)* writes one pool collection and
    *spec_for(set, owner_id)* builds its :class:`CollectionSpec`; both are passed in
    so this module stays free of the ingest import cycle."""
    per_query: list[dict] = []
    split_ag: list[Agreement] = []
    noise_shuf: list[Agreement] = []
    noise_rep: list[Agreement] = []
    models: set[str] = set()
    for qi, q in enumerate(queries):
        corpus = corpora[q.set]
        text_of = {c.chash: c for c in corpus.all_chunks()}
        hits, _ = engine.search(q.text, [bge_names[q.set]], 100)
        by_chash = corpus.chash_files()
        target_hits = [h["id"] for h in hits if q.target in by_chash.get(h["id"], set())]
        others = [h["id"] for h in hits if q.target not in by_chash.get(h["id"], set())]
        pool = build_pool(target_hits, [c.chash for c in corpus.chunks_by_file[q.target]], others)
        if len(pool) != POOL_SIZE:
            raise RuntimeError(f"{q.id}: could only build a pool of {len(pool)} chunks")
        half_a, half_b = split_pool(pool, seed + qi)
        tag = pool_tag(corpus.corpus_hash, q.id, pool)
        parts = {"x": pool, "a": half_a, "b": half_b}
        if shuffled_collection:
            parts["s"] = shuffled(pool, seed + qi)
        names: dict[str, str] = {}
        for part, members in parts.items():
            spec = spec_for(q.set, f"p43b-{tag}-{q.id.replace('-', '')}-{part}")
            chunks = [Chunk(f"pool-{part}", i, text_of[h].text, h) for i, h in enumerate(members)]
            ingest_fn(spec, {f"pool-{part}": chunks})
            names[part] = spec.name
        x1_rows, x1_env = engine.search(q.text, [names["x"]], POOL_SIZE + 40, rerank=True)
        x2_rows, x2_env = engine.search(q.text, [names["x"]], POOL_SIZE + 40, rerank=True)
        a_rows, a_env = engine.search(q.text, [names["a"]], POOL_SIZE + 40, rerank=True)
        b_rows, b_env = engine.search(q.text, [names["b"]], POOL_SIZE + 40, rerank=True)
        for env in (x1_env, x2_env, a_env, b_env):
            if env.get("rerank_model"):
                models.add(str(env["rerank_model"]))
        x1 = scores_of(x1_rows, x1_env, POOL_SIZE, f"{q.id} single")
        x2 = scores_of(x2_rows, x2_env, POOL_SIZE, f"{q.id} single repeat")
        merged = merge_by_score(
            scores_of(a_rows, a_env, len(half_a), f"{q.id} A"),
            scores_of(b_rows, b_env, len(half_b), f"{q.id} B"))
        rec: dict = {"query_id": q.id, "pool_tag": tag, "collections": names,
                     "split": vars(compare_scores(x1, merged))}
        split_ag.append(compare_scores(x1, merged))
        noise_rep.append(compare_scores(x1, x2))
        rec["noise_repeat"] = vars(noise_rep[-1])
        if shuffled_collection:
            s_rows, s_env = engine.search(q.text, [names["s"]], POOL_SIZE + 40, rerank=True)
            s = scores_of(s_rows, s_env, POOL_SIZE, f"{q.id} shuffled")
            noise_shuf.append(compare_scores(x1, s))
            rec["noise_shuffled"] = vars(noise_shuf[-1])
        per_query.append(rec)
        log(f"[p43] B: {q.id} tau split={split_ag[-1].tau:.4f} repeat={noise_rep[-1].tau:.4f}")
    split = summarize(split_ag)
    noise_primary = summarize(noise_shuf) if shuffled_collection else summarize(noise_rep)
    return {
        "pool_size": POOL_SIZE, "rerank_models_seen": sorted(models),
        "split_merge": split,
        "noise_floor_shuffled_pool": summarize(noise_shuf) if shuffled_collection else None,
        "noise_floor_repeat": summarize(noise_rep),
        "verdict": verdict_b(split, noise_primary),
        "per_query": per_query,
    }
