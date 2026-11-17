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

``--gate-from <taxonomy_coverage report>`` applies RDR-134's 0.9 coverage gate: the collections that
report marks status ``ok`` and below the gate are left out of the topic ranking and searched unscoped
in every ON arm (their OFF rows are reused, no extra search). Without it the run is as it was.
``taxonomy_ab_compare.py`` prints a saved run against a gated one.
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


def arm_off(t3: Any, query: str, colls: list[str], calls: dict[str, int],
            per_coll: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    """``per_coll``, when given, also receives each collection's own rows (the gated run reuses
    them as the unscoped search of a collection the gate leaves unrouted)."""
    rows: list[dict[str, Any]] = []
    for c in colls:
        calls["search"] += 1
        hits = t3.search(query, [c], n_results=FETCH, sensitivity="include")
        own = _rows(hits)
        for r in own:
            r["collection"] = r["collection"] or c
            rows.append(r)
        if per_coll is not None:
            per_coll[c] = own
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
           empty: list[str] | None = None,
           extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """``extra`` is rows already fetched unscoped (the gated collections); they merge with the
    topic-scoped rows by distance and chunk hash."""
    rows: list[dict[str, Any]] = list(extra) if extra else []
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


def load_gate(path: Path) -> set[str]:
    """The collections RDR-134's 0.9 coverage gate leaves unrouted, read from a
    ``taxonomy_coverage.py`` report: status ``ok`` and below the gate. A collection with no
    taxonomy (status ``no_taxonomy``) is never routable and is not listed here. Refuses a
    partial report and a report that gates nothing (a gate that matches no collection would
    make the gated run a copy of the ungated one)."""
    report = json.loads(path.read_text())
    if not report.get("complete"):
        raise ValueError(f"{path}: the coverage report is not complete")
    rows = report.get("collections")
    if not isinstance(rows, list):
        raise ValueError(f"{path}: no 'collections' list")
    gated = {r["collection"] for r in rows if r.get("status") == "ok" and r.get("above_0_9") is False}
    if not gated:
        raise ValueError(f"{path}: no collection is below the gate; nothing to unroute")
    return gated


def measure_query(q: dict[str, Any], sets: dict[str, list[str]], variants: list[tuple[str, float]],
                  t3: Any, catalog: Any, tax: Any, cstore: Any,
                  gated: frozenset[str] | set[str] = frozenset()) -> dict[str, Any]:
    """One question: the OFF arm and every ON variant, on each set.

    A collection in ``gated`` is not routed: it is left out of the topic ranking (so it sets no
    band) and its unscoped OFF rows join every ON arm, the way a collection with no topics is
    searched unscoped in ``taxonomy_ab2``. With ``gated`` empty nothing differs from the
    ungated run."""
    row: dict[str, Any] = {"qid": q["qid"], "text": q["text"], "gt": q["gt"]}
    vec_cache: dict[str, list[float]] = {}
    for sname, colls in sets.items():
        calls = {"search": 0, "embed": 0, "ann": 0, "topic_search": 0}
        unscoped = [c for c in colls if c in gated]
        routed = [c for c in colls if c not in gated]
        per_coll: dict[str, list[dict[str, Any]]] | None = {} if unscoped else None
        t0 = time.monotonic()
        off = score(arm_off(t3, q["text"], colls, calls, per_coll), q["gt"], catalog)
        off["s"] = time.monotonic() - t0
        t0 = time.monotonic()
        ranked = rank_topics(t3, cstore, tax, q["text"], routed, calls, vec_cache)
        rank_s = time.monotonic() - t0
        extra = [r for c in unscoped for r in (per_coll or {}).get(c, [])] or None
        row[sname] = {"OFF": off}
        for mode, p in variants:
            t0 = time.monotonic()
            chosen = select(ranked, mode, p)
            empty: list[str] = []
            on = score(arm_on(t3, q["text"], chosen, calls, empty, extra), q["gt"], catalog)
            on["empty_topics"] = empty
            on["s"] = time.monotonic() - t0 + rank_s
            on["n_topics"] = len(chosen)
            on["n_colls"] = len({c for c, _ in chosen})
            if unscoped:
                on["n_unscoped"] = len(unscoped)
            row[sname][f"{mode}{p}"] = on
        row[sname]["calls"] = calls
    return row


def summarize(results: list[dict[str, Any]], sets: dict[str, list[str]]) -> dict[str, Any]:
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
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml")])
    ap.add_argument("--s1", default="rdr__1-1__voyage-context-3__v1")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--gate-from", type=Path, default=None, metavar="COVERAGE_JSON",
                    help="apply RDR-134's 0.9 coverage gate: the collections this taxonomy_coverage "
                         "report marks status ok and below the gate are searched unscoped in every ON arm "
                         "instead of topic-routed")
    a = ap.parse_args(argv)

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    gate = load_gate(a.gate_from) if a.gate_from else None  # before any engine call: a bad file costs nothing
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
        gated: set[str] = set()
        if gate is not None:
            gated = gate & {c for colls in sets.values() for c in colls}
            if not gated:
                raise SystemExit(f"--gate-from {a.gate_from}: none of its {len(gate)} gated collections is in S1 or S2")
        for q in queries:
            row = measure_query(q, sets, variants, t3, catalog, tax, cstore, gated)
            results.append(row)
            print(q["qid"], {s: round(row[s]["OFF"]["ndcg3"], 2) for s in sets},
                  {s: {k: round(v["ndcg3"], 2) for k, v in row[s].items() if k not in ("OFF", "calls")} for s in sets},
                  file=sys.stderr, flush=True)

    summary = summarize(results, sets)
    out: dict[str, Any] = {"summary": summary, "sets": sets, "results": results}
    if gate is not None:
        out["gate"] = {"from": str(a.gate_from), "unrouted": sorted(gated),
                       "gate_not_in_sets": sorted(gate - gated)}
    a.out.write_text(json.dumps(out, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
