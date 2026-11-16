# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 gate round 1 fixes: the taxonomy A/B on the production collection set.

Extends ``taxonomy_ab`` (leg 1) for the round-1 Significants:

  S7  PROD set: every live rdr/docs/knowledge/paper collection, with or without
      topics, as nx_answer's prose domain fans out. A collection with no topics
      is searched unscoped in every arm. Plus P0, the production path itself:
      ``search_cross_corpus`` over the same collections (thresholds, per-collection
      k), the search nx_answer's prose steps run.
  S2  Fallback variants for top-1 routing: F0 falls back to the unscoped search
      only when the kept topic returns zero rows; Fk falls back when it returns
      fewer than K_PROD rows.
  S6  Questions whose required sources are paper or note titles, graded by
      normalised title; NDCG capped at 1 and documents deduplicated by name.

Retrieval only; no language model is called.

    PYTHONPATH=scripts uv run python scripts/bench/taxonomy_ab2.py --out /tmp/ab2.json
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from bench.paths import _resolve_paths_via_catalog
from bench.taxonomy_ab import FETCH, _rows, rank_topics

K = 3
RECALL_K = 10
K_PROD = 20  # a production per-collection k for n_results=10 (max(5, n*mult), mult 2)
_RDR = re.compile(r"^RDR-(\d{3})$")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_queries(paths: list[Path]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in paths:
        for q in yaml.safe_load(p.read_text()).get("queries", []):
            gt: dict[str, int] = {}
            kind = "rdr"
            if q.get("ground_truth"):
                gt = {_norm(k): v for k, v in q["ground_truth"].items()}
            else:
                for src in q.get("required_sources") or []:
                    s = str(src)
                    m = _RDR.match(s)
                    if m:
                        gt[_norm(f"rdr-{m.group(1)}-")] = 3
                    elif "@" in s:
                        continue  # another project's RDR: not gradable by name here
                    else:
                        gt[_norm(s)[:30]] = 3
                        kind = "title"
            if not gt or q["text"] in out:
                continue
            out[q["text"]] = {"qid": q["qid"], "text": q["text"], "gt": gt, "kind": kind}
    return list(out.values())


def _grade(name: str, gt: dict[str, int]) -> int:
    n = _norm(name)
    return max((v for k, v in gt.items() if k and k in n), default=0)


def _ndcg(grades: list[int], gt: dict[str, int]) -> float:
    import math
    dcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(grades[:K]))
    ideal = sorted(gt.values(), reverse=True)[:K]
    idcg = sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return min(1.0, dcg / idcg) if idcg else 0.0


def score(rows: list[dict[str, Any]], gt: dict[str, int], catalog: Any) -> dict[str, Any]:
    rows = rows[: FETCH * 3]
    paths = _resolve_paths_via_catalog(catalog, [r["chash"] for r in rows])
    names: list[str] = []
    for r in rows:
        p = paths.get(r["chash"], "")
        n = Path(p).name if p else ""
        if n and n not in names:
            names.append(n)
    grades = [_grade(n, gt) for n in names]
    targets = [k for k, v in gt.items() if v >= 3]
    top = [_norm(n) for n in names[:RECALL_K]]
    hit = sum(1 for t in targets if any(t in n for n in top))
    return {"ndcg3": _ndcg(grades, gt), "recall10": hit / len(targets) if targets else 0.0,
            "top3": names[:3]}


def search_one(t3: Any, query: str, c: str) -> list[dict[str, Any]]:
    rows = _rows(t3.search(query, [c], n_results=FETCH, sensitivity="include"))
    for r in rows:
        r["collection"] = r["collection"] or c
    return rows


def topic_one(t3: Any, query: str, c: str, label: str) -> list[dict[str, Any]]:
    rows = _rows(t3.search_topic_scoped(query, label, c, n_results=FETCH, sensitivity="include"))
    for r in rows:
        r["collection"] = r["collection"] or c
    return rows


def merge(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for r in sorted(rows, key=lambda r: r["distance"]):
        if r["chash"] not in seen:
            seen.add(r["chash"])
            out.append(r)
    return out


def prose_collections() -> list[str]:
    out = subprocess.run(["nx", "collection", "list"], capture_output=True, text=True, check=True).stdout
    cols = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[0].split("__")[0] in ("rdr", "docs", "knowledge", "paper"):
            try:
                if int(parts[1]) > 0:
                    cols.append(parts[0])
            except ValueError:
                pass
    return sorted(cols)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml")])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415
    from nexus.search_engine import search_cross_corpus  # noqa: PLC0415

    queries = load_queries(a.queries)
    if a.limit:
        queries = queries[: a.limit]
    t3 = make_t3()
    catalog = make_catalog_reader()
    prod = prose_collections()
    results: list[dict[str, Any]] = []
    with T2Database(default_db_path()) as t2:
        tax = t2.taxonomy
        cstore = tax._centroid
        with_topics = set(tax.get_distinct_collections())
        routed = [c for c in prod if c in with_topics]
        for q in queries:
            row: dict[str, Any] = {"qid": q["qid"], "kind": q["kind"], "gt": q["gt"]}
            t0 = time.monotonic()
            p0 = search_cross_corpus(q["text"], prod, 30, t3, cluster_by=None,
                                     taxonomy=None, sensitivity="include")
            p0_rows = [{"chash": getattr(r, "id", ""), "distance": float(getattr(r, "distance", 1.0)),
                        "collection": getattr(r, "collection", "")} for r in p0]
            row["P0"] = {**score(p0_rows, q["gt"], catalog), "s": time.monotonic() - t0}
            row["P0s"] = score(merge(p0_rows), q["gt"], catalog)  # same rows, ordered by distance

            t0 = time.monotonic()
            plain = {c: search_one(t3, q["text"], c) for c in prod}
            off_s = time.monotonic() - t0
            row["OFF"] = {**score(merge([r for rs in plain.values() for r in rs]), q["gt"], catalog), "s": off_s}

            calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
            ranked = rank_topics(t3, cstore, tax, q["text"], routed, calls, {})
            top1 = {c: topic_one(t3, q["text"], c, r[0][0]) for c, r in ranked.items() if r}
            stats = {"zero": 0, "short": 0, "routed": 0, "no_topics": len(prod) - len(top1)}
            for arm, rule in (("top1_F0", lambda n: n == 0), ("top1_Fk", lambda n: n < K_PROD)):
                rows: list[dict[str, Any]] = []
                for c in prod:
                    tr = top1.get(c)
                    if tr is None or rule(len(tr)):
                        rows += plain[c]
                    else:
                        rows += tr
                row[arm] = score(merge(rows), q["gt"], catalog)
            for tr in top1.values():
                stats["routed"] += 1
                if not tr:
                    stats["zero"] += 1
                elif len(tr) < K_PROD:
                    stats["short"] += 1
            row["stats"] = stats
            results.append(row)
            print(q["qid"], q["kind"], {k: round(row[k]["ndcg3"], 2) for k in ("P0", "OFF", "top1_F0", "top1_Fk")},
                  stats, file=sys.stderr, flush=True)

    summary: dict[str, Any] = {"n": len(results), "prod_collections": len(prod), "routed_collections": len(routed)}
    for kind in ("rdr", "title", "all"):
        sel = [r for r in results if kind == "all" or r["kind"] == kind]
        if not sel:
            continue
        d: dict[str, Any] = {"n": len(sel)}
        for arm in ("P0", "P0s", "OFF", "top1_F0", "top1_Fk"):
            n3 = [r[arm]["ndcg3"] for r in sel]
            d[arm] = {"ndcg3": statistics.mean(n3), "recall10": statistics.mean(r[arm]["recall10"] for r in sel)}
            if arm.startswith("top1"):
                base = [r["OFF"]["ndcg3"] for r in sel]
                d[arm]["wins"] = sum(x > b + 1e-9 for x, b in zip(n3, base))
                d[arm]["losses"] = sum(x < b - 1e-9 for x, b in zip(n3, base))
        summary[kind] = d
    summary["fallback"] = {k: statistics.mean(r["stats"][k] for r in results) for k in ("routed", "zero", "short", "no_topics")}
    a.out.write_text(json.dumps({"summary": summary, "prod": prod, "results": results}, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
