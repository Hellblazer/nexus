# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-134 Step 5a-ii (nexus-7fku2.1.9): the engine-route arm and the Minimum Viable Validation.

Each question of the production set runs four ways over the same prose collections:

  P0            ``search_cross_corpus`` unrouted: the production path, the latency baseline.
  OFF           the plain per-collection scan (``taxonomy_ab2.search_one``), the exact result 134-research-13
                called OFF; it is also the exact scan the short-row count compares against.
  ENG           ``search_cross_corpus`` with ``topic_route={"mode": "top1"}``: the engine route, at the
                engine's default coverage gate (0.9).
  ENG_gate_off  the same with ``min_coverage`` 0.0: the gate off.

Per routed arm the client's ``search_topic_route`` event is kept: statuses, coverage values, raw row counts.
A routed collection is SHORT when the engine route returned fewer rows than the exact scan did for it, up to
the request's per-collection k (the largest raw count in that request).

The bars are the RDR's, written before any run (changing one is Sam's decision): RDR-question NDCG@3 at least
0.44; no question worse than OFF by more than 0.1; request latency at most 1.5 times the unrouted request
(mean ENG seconds over mean P0 seconds). This is not a generalisation test: the 25 RDR questions are among
leg 1's 26, and only the 8 title questions are out of sample (Steps 8 and 9 are the generalisation test).

Retrieval only; no language model is called. Spend: Voyage query embeddings.

    PYTHONPATH=scripts uv run python scripts/bench/topic_route_engine_arm.py --out bench/results/<file>.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from bench.taxonomy_ab2 import _grading, load_queries, merge, prose_collections, score, search_one
from nexus.topic_route import EVENT

NDCG3_BAR = 0.44
WORSE_BY = 0.1
LATENCY_RATIO = 1.5
N_RESULTS = 30  # the n 134-research-13's P0 used
#: 134-research-13's production-set figures, for the record beside this run's own
RESEARCH_13 = {"off_rdr_ndcg3": 0.335, "spike_rdr_ndcg3": 0.470}
ROUTES: dict[str, dict[str, Any]] = {"ENG": {"mode": "top1"}, "ENG_gate_off": {"mode": "top1", "min_coverage": 0.0}}


class Vacuous(RuntimeError):
    """A routed call logged no ``search_topic_route`` event: the arm measured nothing routed."""


def _rows(hits: list[Any]) -> list[dict[str, Any]]:
    return [{"chash": getattr(h, "id", ""), "distance": float(getattr(h, "distance", 1.0)),
             "collection": getattr(h, "collection", "")} for h in hits]


def _route(event: dict[str, Any], plain: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    statuses = dict(event.get("statuses") or {})
    raw = dict(event.get("raw_counts") or {})
    k = max(raw.values(), default=0)
    short = sorted(c for c, s in statuses.items()
                   if s == "routed" and raw.get(c, 0) < min(k, len(plain.get(c, []))))
    return {"statuses": statuses, "coverage": dict(event.get("coverage") or {}), "raw_counts": raw,
            "counts": {s: event.get(s, 0) for s in ("routed", "no_topics", "fallback", "skipped")},
            "short": short, "errors": dict(event.get("errors") or {})}


def measure(q: dict[str, Any], prod: list[str], t3: Any, catalog: Any, *, search: Callable,
            plain_search: Callable = search_one) -> dict[str, Any]:
    """One question: P0, OFF and the two routed arms, scored the way 134-research-13 scored them."""
    grade = _grading(q)
    row: dict[str, Any] = {"qid": q["qid"], "kind": q["kind"]}
    t0 = time.monotonic()
    p0 = _rows(search(q["text"], prod, N_RESULTS, t3, cluster_by=None, taxonomy=None, sensitivity="include"))
    row["P0"] = {**score(p0, q["gt"], catalog, **grade), "s": time.monotonic() - t0}
    plain = {c: plain_search(t3, q["text"], c) for c in prod}
    row["OFF"] = score(merge([r for rs in plain.values() for r in rs]), q["gt"], catalog, **grade)
    for name, route in ROUTES.items():
        with structlog.testing.capture_logs() as logs:
            t0 = time.monotonic()
            hits = search(q["text"], prod, N_RESULTS, t3, cluster_by=None, taxonomy=None, sensitivity="include",
                          topic_route=dict(route))
            elapsed = time.monotonic() - t0
        events = [e for e in logs if e.get("event") == EVENT]
        if not events:
            raise Vacuous(f"{q['qid']} {name}: the routed call logged no {EVENT} event")
        row[name] = {**score(_rows(hits), q["gt"], catalog, **grade), "s": elapsed, "route": _route(events[-1], plain)}
    return row


def _bar_checks(rows: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rdr = [r for r in rows if r["kind"] == "rdr"]
    ndcg = statistics.mean(r[arm]["ndcg3"] for r in rdr) if rdr else 0.0
    worse = [r["qid"] for r in rows if r[arm]["ndcg3"] < r["OFF"]["ndcg3"] - WORSE_BY - 1e-9]
    ratio = (statistics.mean(r[arm]["s"] for r in rows) / statistics.mean(r["P0"]["s"] for r in rows)
             if rows and statistics.mean(r["P0"]["s"] for r in rows) > 0 else float("inf"))
    checks = {"ndcg3": bool(rdr) and ndcg >= NDCG3_BAR, "no_question_worse_than_off_by_more_than_0.1": not worse,
              "latency": ratio <= LATENCY_RATIO}
    return {"n_rdr": len(rdr), "ndcg3_rdr": ndcg, "worse_than_off": worse, "latency_ratio": ratio,
            "checks": checks, "passed": all(checks.values())}


def bars(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The three bars on ENG (the engine default gate), with the gate-off arm read out beside them."""
    out = _bar_checks(rows, "ENG")
    out["gate_off"] = _bar_checks(rows, "ENG_gate_off")
    out["gate_note"] = ("threshold rule (fixed before the run): if gate-off reaches 0.44 and the 0.9 gate does "
                        "not, lower the threshold to the highest recorded coverage value at which the arm reaches "
                        "0.44 and re-run; if both reach it, keep 0.9; if gate-off also misses, the gate is not the "
                        "cause")
    out["off_rdr_ndcg3_this_run"] = (statistics.mean(r["OFF"]["ndcg3"] for r in rows if r["kind"] == "rdr")
                                     if any(r["kind"] == "rdr" for r in rows) else None)
    out["research_13"] = RESEARCH_13
    return out


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, Any] = {}
    for kind in sorted({r["kind"] for r in rows}):
        sel = [r for r in rows if r["kind"] == kind]
        by_kind[kind] = {a: {"ndcg3": statistics.mean(r[a]["ndcg3"] for r in sel),
                             "recall10": statistics.mean(r[a]["recall10"] for r in sel)}
                         for a in ("P0", "OFF", *ROUTES)} | {"n": len(sel)}
    short = {a: sum(len(r[a]["route"]["short"]) for r in rows) for a in ROUTES}
    coverage = sorted({v for r in rows for v in r["ENG"]["route"]["coverage"].values()})
    return {"n": len(rows), "by_kind": by_kind, "bars": bars(rows), "short_rows": short,
            "coverage_values": coverage,
            "scope_note": "Checks the engine route against the production-set result of 134-research-13; not a "
                          "generalisation test (the 25 RDR questions are among leg 1's 26; only the 8 title "
                          "questions are out of sample)."}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml")])
    ap.add_argument("--limit", type=int, default=0, help="first N questions only")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.search_engine import search_cross_corpus  # noqa: PLC0415

    import logging  # noqa: PLC0415
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    t3 = make_t3()
    if not t3.supports_topic_route():
        print("refused: the engine does not report vector_topic_route_supported", file=sys.stderr)  # noqa: T201
        return 2
    queries = [q for q in load_queries(a.queries) if q["kind"] in ("rdr", "title")]
    if a.limit:
        queries = queries[: a.limit]
    catalog, prod = make_catalog_reader(), prose_collections()
    rows: list[dict[str, Any]] = []
    for q in queries:
        rows.append(measure(q, prod, t3, catalog, search=search_cross_corpus))
        r = rows[-1]
        print(q["qid"], q["kind"], {k: round(r[k]["ndcg3"], 2) for k in ("P0", "OFF", *ROUTES)},  # noqa: T201
              file=sys.stderr, flush=True)
        a.out.write_text(json.dumps({"rows": rows, "summary": summary(rows)}, indent=1, default=str))
    s = summary(rows)
    a.out.write_text(json.dumps({"rows": rows, "summary": s}, indent=1, default=str))
    print(json.dumps(s["bars"], indent=1, default=str))  # noqa: T201
    return 0 if s["bars"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
