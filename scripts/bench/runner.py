# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-090 retrieval-bench runner.

CLI entry. Loads queries from YAML, dispatches each through the three
retrieval paths (A: ``nx search`` CLI; B: ``nx_answer`` plan-routed;
C: ``nx_answer`` force_dynamic), and writes a JSON report to
``scripts/bench/baselines/<date>.json`` (or ``--out``).

Usage::

    uv run python scripts/bench/runner.py bench/queries/spike_5q.yaml
    uv run python scripts/bench/runner.py path/to/q.yaml --out report.json
    uv run python scripts/bench/runner.py q.yaml --paths A          # subset

The report shape is intentionally stable so that diffing two reports
(``git diff baselines/2026-04-28.json baselines/2026-04-29.json``)
shows real regressions instead of timestamp churn.

Out of scope:

  * Warmup runs / repeats — single-call latency is the metric.
  * Confidence intervals — N=5..30 is the operational scale.
  * Statistical significance testing — that's bench/analyze.py work,
    not this scaffold.
"""
from __future__ import annotations

# scripts/bench/runner.py is invokable directly (``python scripts/bench/runner.py``)
# or via the test pythonpath (``pythonpath=scripts``). When invoked
# directly we need to teach Python that ``bench`` is a package on
# sys.path.
import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import datetime as _dt
import json
import time
from pathlib import Path
from typing import Any, Callable

from bench.metrics import multi_hop_precision
from bench.schema import Query, load_queries

PathHandler = Callable[[Query], dict[str, Any]]
DEFAULT_PATHS = ("A", "B", "C")
K = 3


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def run_bench(
    queries: list[Query],
    *,
    handlers: dict[str, PathHandler],
) -> dict[str, Any]:
    """Run each query through each handler and return an aggregated report.

    Handlers may raise; the runner captures the exception into the row's
    ``error`` field so a single broken handler doesn't kill the whole
    sweep. Per-query/per-path rows preserve the order of insertion in
    ``handlers`` (Python 3.7+ dict ordering).
    """
    rows: list[dict[str, Any]] = []
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
    sweep_t0 = time.monotonic()

    for q in queries:
        for label, handler in handlers.items():
            t0 = time.monotonic()
            try:
                row = handler(q)
            except Exception as e:
                row = {
                    "path": label, "qid": q.qid,
                    "elapsed_s": time.monotonic() - t0,
                    "error": f"{type(e).__name__}: {e}",
                    "chunks": [], "grades": [], "ndcg_at_3": 0.0,
                }
            row.setdefault("path", label)
            row.setdefault("qid", q.qid)
            row.setdefault("elapsed_s", 0.0)
            row.setdefault("error", None)
            row.setdefault("chunks", [])
            row.setdefault("grades", [])
            row.setdefault("ndcg_at_3", 0.0)
            # Compute multi-hop precision when GT carries required keys.
            paths = [c.get("source_path", "") for c in row.get("chunks", [])]
            row["multi_hop_precision"] = multi_hop_precision(paths, q.ground_truth)
            rows.append(row)

    sweep_elapsed_s = time.monotonic() - sweep_t0

    # ── Aggregations ────────────────────────────────────────────────
    by_path: dict[str, list[dict[str, Any]]] = {label: [] for label in handlers}
    by_category: dict[str, dict[str, list[float]]] = {}
    qid_to_cat = {q.qid: q.category for q in queries}

    for r in rows:
        by_path[r["path"]].append(r)
        cat = qid_to_cat.get(r["qid"], "unknown")
        by_category.setdefault(cat, {label: [] for label in handlers})
        if r.get("error") is None:
            by_category[cat].setdefault(r["path"], []).append(r["ndcg_at_3"])

    by_path_summary = {}
    for label, rs in by_path.items():
        clean = [r for r in rs if r.get("error") is None]
        ndcg = [r["ndcg_at_3"] for r in clean]
        elapsed = [r["elapsed_s"] for r in rs]
        mhp = [r["multi_hop_precision"] for r in clean
               if r.get("multi_hop_precision") is not None]
        by_path_summary[label] = {
            "mean_ndcg_at_3": _mean(ndcg),
            "mean_elapsed_s": _mean(elapsed),
            "mean_multi_hop_precision": _mean(mhp) if mhp else None,
            "errors": sum(1 for r in rs if r.get("error")),
            "n": len(rs),
        }

    by_category_summary = {
        cat: {label: _mean(vals) for label, vals in cat_data.items()}
        for cat, cat_data in by_category.items()
    }

    return {
        "started_at": started_at,
        "sweep_elapsed_s": round(sweep_elapsed_s, 3),
        "queries": len(queries),
        "k": K,
        "paths": list(handlers.keys()),
        "rows": rows,
        "by_path": by_path_summary,
        "by_category": by_category_summary,
    }


def write_report(report: dict[str, Any], out: Path) -> None:
    """Write the report as stable, sorted, indent=2 JSON."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def _live_handlers(corpus: str, scope_b: str) -> dict[str, PathHandler]:
    """Build handlers that hit the real retrieval surface.

    Imported lazily so unit tests don't pay the cost of touching ``nexus.mcp``
    or starting a T3 client.
    """
    from bench.paths import run_path_a, run_path_b, run_path_c
    from nexus.mcp_infra import get_t3

    t3 = get_t3()

    def _a(q: Query) -> dict[str, Any]:
        return run_path_a(q, corpus=corpus)

    def _b(q: Query) -> dict[str, Any]:
        return run_path_b(q, t3, scope=scope_b)

    def _c(q: Query) -> dict[str, Any]:
        return run_path_c(q, t3, corpus=corpus)

    return {"A": _a, "B": _b, "C": _c}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RDR-090 retrieval bench runner — paths A (nx search), "
                    "B (nx_answer plan-routed), C (nx_answer force_dynamic).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("queries_yaml", type=Path,
                        help="YAML file of queries to bench.")
    parser.add_argument("--corpus", default="rdr__nexus-571b8edd",
                        help="Path-A corpus filter and Path-C explicit scope.")
    parser.add_argument("--scope-b", default="rdr",
                        help="Path-B scope (the cross-project leakage probe).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output JSON path; defaults to "
                             "scripts/bench/baselines/<date>.json.")
    parser.add_argument("--paths", default=",".join(DEFAULT_PATHS),
                        help="Comma-separated subset of A,B,C to run.")
    args = parser.parse_args(argv)

    queries = load_queries(args.queries_yaml)

    selected = {p.strip() for p in args.paths.split(",") if p.strip()}
    if not selected.issubset(set(DEFAULT_PATHS)):
        bad = sorted(selected - set(DEFAULT_PATHS))
        raise SystemExit(f"Unknown path label(s): {bad}; valid={list(DEFAULT_PATHS)}")

    full = _live_handlers(args.corpus, args.scope_b)
    handlers = {label: full[label] for label in DEFAULT_PATHS if label in selected}

    out_path = args.out or (
        Path(__file__).resolve().parent / "baselines"
        / f"{_dt.date.today().isoformat()}.json"
    )

    print(f"=== bench: {len(queries)} queries × {len(handlers)} path(s) ===")
    print(f"corpus={args.corpus!r}  scope_b={args.scope_b!r}")
    report = run_bench(queries, handlers=handlers)
    write_report(report, out_path)

    print("\n=== Summary ===")
    for label, summary in report["by_path"].items():
        line = (
            f"Path {label}: NDCG@3={summary['mean_ndcg_at_3']:.3f} "
            f"t={summary['mean_elapsed_s']:.2f}s  "
            f"errors={summary['errors']}/{summary['n']}"
        )
        if summary["mean_multi_hop_precision"] is not None:
            line += f"  mhp={summary['mean_multi_hop_precision']:.3f}"
        print(line)
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
