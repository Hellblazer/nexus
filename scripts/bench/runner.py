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
    uv run python scripts/bench/runner.py q.yaml --paths A,C --pairs 5   # paired A/B timing
    uv run python scripts/bench/runner.py --compare old.json new.json   # refuses across manifests

Every report carries a ``manifest`` (``bench.manifest``: git sha, host class, engine build), and
``--compare`` refuses two reports measured on different hosts or engines (nexus-3ceda.11).
``--pairs N`` times exactly two paths on each query in N alternating pairs and reports medians
with every sample kept (``bench.paired``).

The report shape is intentionally stable so that diffing two reports
(``git diff baselines/2026-04-28.json baselines/2026-04-29.json``)
shows real regressions instead of timestamp churn.

Out of scope:

  * Warmup runs — a sweep's single-call latency is its metric; for an A/B, ``--pairs``.
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
import statistics
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from bench.manifest import ManifestMismatch, build_manifest, comparable
from bench.metrics import dedupe_by_doc, grade_for_path, multi_hop_precision, ndcg_at_k
from bench.paired import Clock, run_paired, summarize
from bench.schema import GOLDEN_REQUIRED_FIELDS, Query, load_golden_set, load_queries

PathHandler = Callable[[Query], dict[str, Any]]
DEFAULT_PATHS = ("A", "B", "C")
K = 3


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


#: Request keys only a golden set carries (the RDR-232 labels). A file with one of them and no version
#: stamp is a golden set that lost its stamp, not an RDR-090 file.
_GOLDEN_ONLY_KEYS = frozenset(GOLDEN_REQUIRED_FIELDS)


def load_query_file(path: Path) -> list[Query]:
    """Load a bench query file: strictly as an RDR-232 golden set when it declares a
    ``golden_set_version`` (so a missing label or an unsupported version is refused here, not scored),
    leniently otherwise (the RDR-090 files such as ``spike_5q.yaml``).

    A file that carries golden-only request fields (``answer_kind``, ``unacceptable_plans``, ...) but no
    ``golden_set_version`` is refused: stripping the stamp must not switch off the strict loader.
    """
    with open(path, encoding="utf-8") as f:
        head = yaml.safe_load(f) or {}
    if isinstance(head, dict) and "golden_set_version" in head:
        return load_golden_set(path)
    raw = head.get("queries") if isinstance(head, dict) else None
    if isinstance(raw, list):
        found = sorted({k for q in raw if isinstance(q, dict) for k in q.keys() & _GOLDEN_ONLY_KEYS})
        if found:
            raise ValueError(
                f"{path}: carries golden-set fields {found} but no golden_set_version; "
                "a golden set declares its version"
            )
    return load_queries(path)


def _score_ndcg(row: dict[str, Any], q: Query) -> None:
    """Score NDCG@K from the row's retrieved chunks against the request's ``ground_truth``.

    Only a request with ground truth is scored: its ideal ranking is non-empty, so a 0.0 means the
    retrieval missed. A request without it has an empty ideal ranking, ``ndcg_at_k`` returns 0.0 by
    convention, and that zero must not be averaged in as a failure, so its row is marked unscored
    and keeps whatever the handler reported. An errored row is not scored either. The runner scores
    from ``chunks`` itself rather than trusting a handler's own ``ndcg_at_3``, so every path is graded
    by the same arithmetic.
    """
    row["ndcg_scored"] = False
    if not q.ground_truth or row.get("error") is not None:
        return
    deduped = dedupe_by_doc(row.get("chunks", []))[:K]
    grades = [grade_for_path(c.get("source_path", ""), q.ground_truth) for c in deduped]
    row["grades"] = grades
    row["ndcg_at_3"] = ndcg_at_k(grades, q.ground_truth, k=K)
    row["ndcg_scored"] = True


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
            _score_ndcg(row, q)
            rows.append(row)

    sweep_elapsed_s = time.monotonic() - sweep_t0

    # ── Aggregations ────────────────────────────────────────────────
    by_path: dict[str, list[dict[str, Any]]] = {label: [] for label in handlers}
    by_category: dict[str, dict[str, list[float]]] = {}
    by_category_scored: dict[str, dict[str, list[float]]] = {}
    qid_to_cat = {q.qid: q.category for q in queries}

    for r in rows:
        by_path[r["path"]].append(r)
        cat = qid_to_cat.get(r["qid"], "unknown")
        by_category.setdefault(cat, {label: [] for label in handlers})
        if r.get("error") is None:
            by_category[cat].setdefault(r["path"], []).append(r["ndcg_at_3"])
            by_category_scored.setdefault(cat, {label: [] for label in handlers})
            if r.get("ndcg_scored"):
                by_category_scored[cat].setdefault(r["path"], []).append(r["ndcg_at_3"])

    by_path_summary = {}
    for label, rs in by_path.items():
        clean = [r for r in rs if r.get("error") is None]
        ndcg = [r["ndcg_at_3"] for r in clean]
        elapsed = [r["elapsed_s"] for r in rs]
        mhp = [r["multi_hop_precision"] for r in clean
               if r.get("multi_hop_precision") is not None]
        scored = [r["ndcg_at_3"] for r in clean if r.get("ndcg_scored")]
        by_path_summary[label] = {
            "mean_ndcg_at_3": _mean(ndcg),
            # NDCG over the requests that carry ground_truth only (the RDR-232 golden set: 5 of 24);
            # the all-request mean above counts the others as 0.0.
            "mean_ndcg_at_3_scored": _mean(scored) if scored else None,
            "ndcg_scored_n": len(scored),
            "mean_elapsed_s": _mean(elapsed),
            "mean_multi_hop_precision": _mean(mhp) if mhp else None,
            "errors": sum(1 for r in rs if r.get("error")),
            "n": len(rs),
        }

    by_category_summary = {
        cat: {label: _mean(vals) for label, vals in cat_data.items()}
        for cat, cat_data in by_category.items()
    }
    # The same cut over the requests that carry ground_truth only; None where a category has none.
    by_category_scored_summary = {
        cat: {label: (_mean(vals) if vals else None) for label, vals in cat_data.items()}
        for cat, cat_data in by_category_scored.items()
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
        "by_category_scored": by_category_scored_summary,
    }


def run_paired_bench(
    queries: list[Query], *, handlers: dict[str, PathHandler], pairs: int, clock: Clock = time.perf_counter,
) -> dict[str, Any]:
    """Time two paths on each query in *pairs* alternating pairs (``bench.paired``).

    A failed retrieval fails the A/B instead of entering as a sample: a fast failure would read
    as a fast path. The live handlers report a failure as a row whose ``error`` is set, not by
    raising (``bench.paths``), so a run that returns one (even with an empty error) raises here, naming the path, the
    query and the error; a handler that raises propagates as it is.
    """
    if len(handlers) != 2:
        raise ValueError(f"a paired A/B takes exactly two paths, got {list(handlers)}")
    (label_a, fn_a), (label_b, fn_b) = handlers.items()

    def arm(label: str, fn: PathHandler, q: Query) -> Callable[[], object]:
        def run() -> None:
            row = fn(q)
            # ``is not None``: path A's error is nx's stderr, which can be empty on a failure.
            if isinstance(row, dict) and row.get("error") is not None:
                raise RuntimeError(f"path {label} failed on query {q.qid}: {row['error'] or '(no error text)'}")
        return run

    per_query = {
        q.qid: summarize(run_paired(arm(label_a, fn_a, q), arm(label_b, fn_b, q), pairs, clock=clock))
        for q in queries
    }
    diffs = [s["median_paired_diff_s"] for s in per_query.values()]
    return {
        "a": label_a,
        "b": label_b,
        "pairs": pairs,
        "queries": len(queries),
        "median_of_query_paired_diffs_s": statistics.median(diffs) if diffs else None,
        "b_faster_queries": sum(1 for d in diffs if d < 0),
        "per_query": per_query,
    }


def compare_reports(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Per-path change from *old* to *new*, refusing two reports measured on different hosts
    or engines (:func:`bench.manifest.comparable` raises :class:`ManifestMismatch`)."""
    comparable(old, new)
    for name, report in (("first", old), ("second", new)):
        if "by_path" not in report:
            raise ManifestMismatch(f"the {name} report is not a sweep report (a --pairs report has no by_path)")
    out: dict[str, Any] = {"old_sha": old["manifest"].get("git_sha"), "new_sha": new["manifest"].get("git_sha"),
                           "paths": {}}
    for label in sorted(set(old.get("by_path", {})) & set(new.get("by_path", {}))):
        o, n = old["by_path"][label], new["by_path"][label]
        out["paths"][label] = {
            key: {"old": o.get(key), "new": n.get(key)}
            for key in ("mean_ndcg_at_3", "mean_ndcg_at_3_scored", "mean_elapsed_s", "errors")
        }
    return out


def write_report(report: dict[str, Any], out: Path) -> None:
    """Write the report as stable, sorted, indent=2 JSON."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def _live_handlers(corpus: str, scope_b: str) -> dict[str, PathHandler]:
    """Build handlers that hit the real retrieval surface.

    Imported lazily so unit tests don't pay the cost of touching ``nexus.mcp``
    or building a catalog reader.
    """
    from bench.paths import run_path_a, run_path_b, run_path_c
    from nexus.catalog.factory import make_catalog_reader

    # nexus-hmu02: Path B/C resolve chash -> path through the catalog
    # manifest (the chromadb-era T3 lookup is gone).
    catalog = make_catalog_reader()

    def _a(q: Query) -> dict[str, Any]:
        return run_path_a(q, corpus=corpus)

    def _b(q: Query) -> dict[str, Any]:
        return run_path_b(q, catalog, scope=scope_b)

    def _c(q: Query) -> dict[str, Any]:
        return run_path_c(q, catalog, corpus=corpus)

    return {"A": _a, "B": _b, "C": _c}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="RDR-090 retrieval bench runner — paths A (nx search), "
                    "B (nx_answer plan-routed), C (nx_answer force_dynamic).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("queries_yaml", type=Path, nargs="?",
                        help="YAML file of queries to bench (not with --compare).")
    parser.add_argument("--pairs", type=int, default=None,
                        help="Time exactly two --paths on each query in this many alternating "
                             "pairs (an even number, so each path leads half of them) and "
                             "report medians, instead of a sweep.")
    parser.add_argument("--compare", type=Path, nargs=2, metavar=("OLD", "NEW"),
                        help="Compare two reports; refuses two measured on different hosts or engines.")
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

    if args.compare:
        if args.queries_yaml is not None or args.pairs is not None:
            parser.error("--compare takes two reports and nothing else")
        old, new = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        try:
            diff = compare_reports(old, new)
        except ManifestMismatch as exc:
            print(f"refused: {exc}", file=_sys.stderr)
            return 2
        print(json.dumps(diff, indent=2, sort_keys=True))
        return 0
    if args.queries_yaml is None:
        parser.error("a queries YAML is required unless --compare is given")

    queries = load_query_file(args.queries_yaml)

    order = [p.strip() for p in args.paths.split(",") if p.strip()]
    selected = set(order)
    if not selected.issubset(set(DEFAULT_PATHS)):
        bad = sorted(selected - set(DEFAULT_PATHS))
        raise SystemExit(f"Unknown path label(s): {bad}; valid={list(DEFAULT_PATHS)}")
    if args.pairs is not None:
        if args.pairs < 2 or args.pairs % 2:
            parser.error(f"--pairs takes an even number of at least 2, got {args.pairs}")
        if len(selected) != 2:
            parser.error(f"--pairs needs exactly two --paths, got {sorted(selected)}")

    full = _live_handlers(args.corpus, args.scope_b)
    # The order given: with --pairs the first path is arm A.
    handlers = {label: full[label] for label in dict.fromkeys(order)}

    # A paired report gets its own default name, so it never replaces the day's sweep baseline.
    stem = _dt.date.today().isoformat() + (f"-pairs-{'-'.join(handlers)}" if args.pairs is not None else "")
    out_path = args.out or (Path(__file__).resolve().parent / "baselines" / f"{stem}.json")

    manifest = build_manifest(Path(__file__).resolve().parents[2])
    print(f"=== bench: {len(queries)} queries × {len(handlers)} path(s) ===")
    print(f"corpus={args.corpus!r}  scope_b={args.scope_b!r}  host={manifest['host_class']!r}  "
          f"engine={manifest['engine_build']!r}")
    if args.pairs is not None:
        paired = run_paired_bench(queries, handlers=handlers, pairs=args.pairs)
        paired["manifest"] = manifest
        write_report(paired, out_path)
        print(f"\nPaired {paired['a']} vs {paired['b']}, {args.pairs} pairs per query: median of the "
              f"per-query paired differences {paired['median_of_query_paired_diffs_s']:+.3f}s "
              f"({paired['b']} faster on {paired['b_faster_queries']}/{paired['queries']} queries)")
        print(f"\nReport: {out_path}")
        return 0
    report = run_bench(queries, handlers=handlers)
    report["manifest"] = manifest
    write_report(report, out_path)

    print("\n=== Summary ===")
    for label, summary in report["by_path"].items():
        line = (
            f"Path {label}: NDCG@3={summary['mean_ndcg_at_3']:.3f} "
            f"t={summary['mean_elapsed_s']:.2f}s  "
            f"errors={summary['errors']}/{summary['n']}"
        )
        if summary["mean_ndcg_at_3_scored"] is not None:
            line += (f"  scored NDCG@3={summary['mean_ndcg_at_3_scored']:.3f} "
                     f"(n={summary['ndcg_scored_n']})")
        if summary["mean_multi_hop_precision"] is not None:
            line += f"  mhp={summary['mean_multi_hop_precision']:.3f}"
        print(line)
    print(f"\nReport: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
