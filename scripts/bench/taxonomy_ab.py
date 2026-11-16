# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 measure-first spike: taxonomy-routed retrieval on/off (RDR-179 Phase 4.2 shape).

Leg 1 only: a retrieval-level paired A/B, no LLM calls. Spend is the engine's
query embeddings (Voyage on a cloud engine).

  OFF  per-collection ``search`` over the set, merged by distance.
  ON   embed the query once per collection model, rank that collection's topic
       centroids (``/v1/taxonomy/centroids/query``), keep topics by a band below
       the best similarity across the set (or top-k per collection), run
       ``search_topic_scoped`` per kept topic, merge by distance. A collection
       with no kept topic is skipped.

Ground truth: bench/queries spike_5q (``ground_truth``) and golden sets
(``required_sources`` RDR-NNN mapped to ``rdr-NNN-``: 3). The pass rule was
declared before the run in T2 nexus_rdr/134-research-9.

    uv run python scripts/bench/taxonomy_ab.py --out /tmp/ab.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from bench.metrics import dedupe_by_doc, grade_for_path, ndcg_at_k
from bench.paths import _resolve_paths_via_catalog

K = 3
RECALL_K = 10
FETCH = 30  # per collection, over-fetched so per-doc dedupe still leaves RECALL_K docs
_RDR = re.compile(r"^RDR-(\d{3})$")


def load_queries(paths: list[Path]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in paths:
        data = yaml.safe_load(p.read_text())
        for q in data.get("queries", []):
            gt = dict(q.get("ground_truth") or {})
            if not gt:
                for src in q.get("required_sources") or []:
                    m = _RDR.match(str(src))
                    if m:
                        gt[f"rdr-{m.group(1)}-"] = 3
            if not gt or q["text"] in out:
                continue
            out[q["text"]] = {"qid": q["qid"], "text": q["text"], "gt": gt, "src": p.name}
    return list(out.values())


def _rows(hits: Any) -> list[dict[str, Any]]:
    if isinstance(hits, dict):
        hits = hits.get("results") or hits.get("rows") or []
    rows = []
    for h in hits or []:
        chash = h.get("chash") or h.get("chunk_text_hash") or h.get("id") or ""
        rows.append({"chash": chash, "distance": float(h.get("distance", 1.0)),
                     "collection": h.get("collection", "")})
    return rows


def arm_off(t3: Any, query: str, colls: list[str], calls: dict[str, int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in colls:
        calls["search"] += 1
        hits = t3.search(query, [c], n_results=FETCH, sensitivity="include")
        for r in _rows(hits):
            r["collection"] = r["collection"] or c
            rows.append(r)
    return sorted(rows, key=lambda r: r["distance"])


def rank_topics(t3: Any, cstore: Any, tax: Any, query: str, colls: list[str],
                calls: dict[str, int], vec_cache: dict[str, list[float]]) -> dict[str, list[tuple[str, float]]]:
    """Per collection: [(topic label, similarity)] best first, top 10."""
    out: dict[str, list[tuple[str, float]]] = {}
    for c in colls:
        model = c.split("__")[2] if c.count("__") >= 3 else c
        if model not in vec_cache:
            calls["embed"] += 1
            vec_cache[model] = t3.embed_for_collection(c, [query])[0]
        calls["ann"] += 1
        res = cstore.ann_query(vec_cache[model], c, n_results=10)
        ranked = []
        for a in res:
            t = tax.get_topic_by_id(a.topic_id)
            if t and t.get("label"):
                ranked.append((t["label"], float(a.similarity)))
        out[c] = ranked
    return out


def select(ranked: dict[str, list[tuple[str, float]]], mode: str, param: float) -> list[tuple[str, str]]:
    best = max((s for r in ranked.values() for _, s in r), default=None)
    if best is None:
        return []
    chosen = []
    for c, r in ranked.items():
        if mode == "band":  # one band below the best similarity across the whole set
            chosen += [(c, lab) for lab, s in r if s >= best - param]
        elif mode == "cband":  # a band below each collection's own best
            if r:
                chosen += [(c, lab) for lab, s in r if s >= r[0][1] - param]
        else:  # topk per collection
            chosen += [(c, lab) for lab, _ in r[: int(param)]]
    return chosen


def arm_on(t3: Any, query: str, chosen: list[tuple[str, str]], calls: dict[str, int],
           empty: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c, label in chosen:
        calls["topic_search"] += 1
        hits = _rows(t3.search_topic_scoped(query, label, c, n_results=FETCH, sensitivity="include"))
        if not hits and empty is not None:
            empty.append(f"{c}::{label}")
        for r in hits:
            r["collection"] = r["collection"] or c
            rows.append(r)
    seen: set[str] = set()
    uniq = []
    for r in sorted(rows, key=lambda r: r["distance"]):
        if r["chash"] in seen:
            continue
        seen.add(r["chash"])
        uniq.append(r)
    return uniq


def score(rows: list[dict[str, Any]], gt: dict[str, int], catalog: Any) -> dict[str, Any]:
    paths = _resolve_paths_via_catalog(catalog, [r["chash"] for r in rows[: FETCH * 2]])
    graded = [{"source_path": paths.get(r["chash"], ""), **r} for r in rows[: FETCH * 2]]
    resolved = [g for g in graded if g["source_path"]]
    docs = dedupe_by_doc(resolved)
    grades = [grade_for_path(d["source_path"], gt) for d in docs]
    targets = [k for k, v in gt.items() if v >= 3]
    top = [Path(d["source_path"]).name for d in docs[:RECALL_K]]
    hit = sum(1 for t in targets if any(t in n for n in top))
    return {"ndcg3": ndcg_at_k(grades[:K], gt, k=K),
            "recall10": hit / len(targets) if targets else 0.0,
            "top3": top[:3], "unresolved": len(graded) - len(resolved)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml")])
    ap.add_argument("--s1", default="rdr__1-1__voyage-context-3__v1")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    queries = load_queries(a.queries)
    if a.limit:
        queries = queries[: a.limit]
    t3 = make_t3()
    catalog = make_catalog_reader()
    variants = [("band", 0.03), ("band", 0.06), ("cband", 0.03), ("topk", 1)]
    results: list[dict[str, Any]] = []
    with T2Database(default_db_path()) as t2:
        tax = t2.taxonomy
        cstore = tax._centroid
        with_topics = {c for c in tax.get_distinct_collections()}
        allc = [c for c in t3.list_collections() if isinstance(c, str)] if hasattr(t3, "list_collections") else []
        s2 = sorted(c for c in with_topics
                    if c.split("__")[0] in ("rdr", "docs", "knowledge") and (not allc or c in allc))
        sets = {"S1": [a.s1], "S2": s2}
        for q in queries:
            row: dict[str, Any] = {"qid": q["qid"], "text": q["text"], "gt": q["gt"]}
            vec_cache: dict[str, list[float]] = {}
            for sname, colls in sets.items():
                calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
                t0 = time.monotonic()
                off = score(arm_off(t3, q["text"], colls, calls), q["gt"], catalog)
                off["s"] = time.monotonic() - t0
                t0 = time.monotonic()
                ranked = rank_topics(t3, cstore, tax, q["text"], colls, calls, vec_cache)
                rank_s = time.monotonic() - t0
                row[sname] = {"OFF": off}
                for mode, p in variants:
                    t0 = time.monotonic()
                    chosen = select(ranked, mode, p)
                    empty: list[str] = []
                    on = score(arm_on(t3, q["text"], chosen, calls, empty), q["gt"], catalog)
                    on["empty_topics"] = empty
                    on["s"] = time.monotonic() - t0 + rank_s
                    on["n_topics"] = len(chosen)
                    on["n_colls"] = len({c for c, _ in chosen})
                    row[sname][f"{mode}{p}"] = on
                row[sname]["calls"] = calls
            results.append(row)
            print(q["qid"], {s: round(row[s]["OFF"]["ndcg3"], 2) for s in sets},
                  {s: {k: round(v["ndcg3"], 2) for k, v in row[s].items() if k not in ("OFF", "calls")} for s in sets},
                  file=sys.stderr, flush=True)

    summary: dict[str, Any] = {"n": len(results), "S2_collections": len(sets["S2"])}
    for sname in sets:
        arms = [k for k in results[0][sname] if k != "calls"]
        summary[sname] = {}
        for arm in arms:
            n3 = [r[sname][arm]["ndcg3"] for r in results]
            rc = [r[sname][arm]["recall10"] for r in results]
            d = {"ndcg3": statistics.mean(n3), "recall10": statistics.mean(rc),
                 "s": statistics.mean(r[sname][arm]["s"] for r in results)}
            if arm != "OFF":
                base = [r[sname]["OFF"]["ndcg3"] for r in results]
                d["wins"] = sum(1 for x, b in zip(n3, base) if x > b + 1e-9)
                d["losses"] = sum(1 for x, b in zip(n3, base) if x < b - 1e-9)
                d["d_ndcg3"] = d["ndcg3"] - statistics.mean(base)
                d["d_recall10"] = d["recall10"] - statistics.mean(r[sname]["OFF"]["recall10"] for r in results)
                d["topics"] = statistics.mean(r[sname][arm]["n_topics"] for r in results)
            summary[sname][arm] = d
    a.out.write_text(json.dumps({"summary": summary, "sets": sets, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
