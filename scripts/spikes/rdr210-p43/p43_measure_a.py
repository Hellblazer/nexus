# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measurement A: the bge calibration factor, by the 210-research-17 method.

Per family (prose: voyage-context-3 vs bge; code: voyage-code-3 vs bge), take
each query's top-10 candidates from each collection through the engine's
``/v1/vectors/search`` (no rerank, no threshold), label a candidate relevant when
its chunk comes from the query's target file, and find the GLOBAL distance
threshold (one per collection family across all 20 queries) that maximises F1.
The factor is ``0.45 / bge_threshold``: bge distances are numerically lower than
Voyage's, so the factor stretches them onto voyage-code-3's scale, where the
relevance boundary is 0.45. A seeded 1000-resample bootstrap over queries gives a
95% interval. PASS when the bge factor's interval overlaps 1.36-1.48.
"""
from __future__ import annotations

import statistics
from collections.abc import Callable

import p43_stats as st
from p43_corpus import Corpus
from p43_queries import Query

TOP_K = 10


def collect_a(
    search: Callable[[str, str, int], list[dict]],
    queries: list[Query],
    corpora: dict[str, Corpus],
    names: dict[tuple[str, str], str],
    *, k: int = TOP_K, log=print,
) -> list[dict]:
    """Raw per-query candidate lists. *search* is ``(query, collection, n) ->
    rows`` (rows carry ``id`` = chunk hash and ``distance``); *names* maps
    ``(set, family)`` to a collection, family being ``bge`` or ``voyage``."""
    raw: list[dict] = []
    for q in queries:
        corpus = corpora[q.set]
        target = corpus.chashes_of(q.target)
        by_chash = corpus.chash_files()
        for family in ("bge", "voyage"):
            name = names.get((q.set, family))
            if name is None:
                continue
            rows = search(q.text, name, k)
            raw.append({
                "set": q.set, "query_id": q.id, "family": family, "collection": name,
                "relevant_in_corpus": len(target),
                "rows": [
                    [r["id"], float(r["distance"]), q.target in by_chash.get(r["id"], set())]
                    for r in rows
                ],
            })
        log(f"[p43] A: {q.id}")
    return raw


def _labelled(raw: list[dict], sets: tuple[str, ...], family: str) -> list[st.LabelledQuery]:
    return [
        st.LabelledQuery(tuple((d, bool(rel)) for _, d, rel in r["rows"]), r["relevant_in_corpus"])
        for r in raw if r["set"] in sets and r["family"] == family
    ]


def _factor_dict(qs: list[st.LabelledQuery], resamples: int, seed: int, fn_basis: str) -> dict:
    res = st.factor_interval(qs, resamples=resamples, seed=seed, fn_basis=fn_basis)
    return {
        "queries": len(qs),
        "threshold": res.threshold, "f1": res.f1,
        "threshold_ci95": list(res.threshold_ci),
        "factor": res.factor, "factor_ci95": list(res.factor_ci),
        "bootstrap_resamples_used": res.resamples_used,
        "overlaps_expected_1.36_1.48": st.intervals_overlap(res.factor_ci, st.EXPECTED_FACTOR_RANGE),
    }


def _distance_stats(qs: list[st.LabelledQuery]) -> dict:
    rel = sorted(d for q in qs for d, r in q.items if r)
    irr_all = [d for q in qs for d, r in q.items if not r]
    irr_min = [min((d for d, r in q.items if not r), default=float("nan")) for q in qs]
    return {
        "relevant_p50": st.percentile(rel, 50), "relevant_p90": st.percentile(rel, 90),
        "relevant_n": len(rel),
        "offtopic_median": statistics.median(irr_all) if irr_all else float("nan"),
        "offtopic_top10_min": min(irr_all) if irr_all else float("nan"),
        "offtopic_best_per_query_median": statistics.median([v for v in irr_min if v == v]) if irr_min else float("nan"),
    }


def analyze_a(raw: list[dict], *, resamples: int = 1000, seed: int = 210) -> dict:
    """Thresholds, factors and intervals per set and pooled, from the raw lists."""
    out: dict = {"method": "labelled global best-F1 distance threshold over top-10 per collection; "
                           "factor = 0.45 / bge threshold; query-level bootstrap",
                 "resamples": resamples, "seed": seed, "expected_factor_range": list(st.EXPECTED_FACTOR_RANGE),
                 "sets": {}}
    groups = {"prose": ("prose",), "code": ("code",), "pooled": ("prose", "code")}
    for label, sets in groups.items():
        entry: dict = {}
        for family in ("bge", "voyage"):
            qs = _labelled(raw, sets, family)
            if not qs:
                continue
            entry[family] = {
                "pool_basis": _factor_dict(qs, resamples, seed, "pool"),
                "corpus_basis": _factor_dict(qs, resamples, seed, "corpus"),
                "distances": _distance_stats(qs),
            }
        out["sets"][label] = entry
    out["verdict"] = verdict_a(out)
    return out


def verdict_a(analysis: dict) -> dict:
    """PASS when the bge factor's 95% interval overlaps 1.36-1.48 (pool-basis F1;
    the corpus-basis figure is reported beside it). Stated per set and pooled."""
    per: dict[str, dict] = {}
    for label, entry in analysis["sets"].items():
        bge = entry.get("bge")
        if bge is None:
            continue
        f = bge["pool_basis"]
        per[label] = {
            "factor": f["factor"], "factor_ci95": f["factor_ci95"],
            "overlaps_1.36_1.48": f["overlaps_expected_1.36_1.48"],
            "result": "PASS" if f["overlaps_expected_1.36_1.48"] else "FAIL",
        }
    pooled = per.get("pooled")
    return {"per_set": per,
            "headline": pooled["result"] if pooled else "NOT RUN",
            "rule": "bge factor 95% bootstrap interval overlaps 1.36-1.48"}
