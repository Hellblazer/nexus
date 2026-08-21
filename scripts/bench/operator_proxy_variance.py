# SPDX-License-Identifier: AGPL-3.0-or-later
"""Variance-measurement driver for RDR-196 .p2a (ship-blocker fix,
2026-08-21 code-review round; round-2 review-fix, same day): dispatches
multiple independent strong-tier runs per in-scope operator and reports
pairwise agreement (mean/min/max) instead of a single n=1 point estimate.

Per-operator plan (methodologically deliberate, not uniform — see T2
``nexus_rdr/196-phase2-quality-proxy`` "VARIANCE MEASUREMENT" for the full
reasoning):

  * filter, rank: 3 FRESH dispatches each, all 3 pairwise scores reported
    (C(3,2)=3 pairs). filter had no prior numeric score at all (only a
    pytest pass/fail); rank's prompt CHANGED in this same review round
    (the "preserve id tag" instruction was removed), so its prior 0.976
    score was measured under different code and is not valid to reuse.
  * groupby, extract, check: 2 FRESH dispatches each (1 new pair) PLUS
    the ALREADY-RECORDED positive-control score from the first .p2a run
    (valid to reuse: their prompt builders and scoring metrics are
    unchanged by this review round) = 2 pairs total.
  * verify: 2 FRESH dispatches (1 new pair) PLUS the persisted rerun
    transcript's output RE-SCORED under the current (stopword-filtered)
    citation_tokens metric = 2 pairs total.

Round-2 review fix: the report now persists RAW per-dispatch outputs +
DispatchUsage (not just scalar scores) under a repo-relative, gitignored
``bench/out/`` directory (was a hardcoded absolute session-scratch path
that would not exist for the script's next reader) — see ``--out``.

``--topup OPERATOR`` dispatches exactly ONE more strong-tier run for
*OPERATOR*, loads the existing report's raw outputs for it, computes every
NEW pairwise score against the previously stored outputs, and updates the
report in place — used to add a 3rd data point to an operator that
started at n=2 without re-paying for the pairs it already has.

Run with:
  uv run python scripts/bench/operator_proxy_variance.py
  uv run python scripts/bench/operator_proxy_variance.py --topup operator_verify
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import asyncio
import itertools
import json
from dataclasses import asdict

from bench.operator_proxy import BUILDERS, score

_REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
_DEFAULT_OUT = _REPO_ROOT / "bench" / "out" / "operator_proxy_variance_report.json"

# Historical, code-path-valid reuse (see module docstring).
_HISTORICAL_PAIR_SCORE = {
    "operator_groupby": 1.000,
    "operator_extract": 1.000,
    "operator_check": 1.000,
    "operator_verify": 1.000,  # re-scored under the current citation_tokens metric
}

_THREE_FRESH = ("operator_filter", "operator_rank")
_TWO_FRESH_PLUS_HISTORICAL = ("operator_groupby", "operator_extract", "operator_check", "operator_verify")


async def _dispatch_n(operator_name: str, n: int, *, model: str) -> tuple[list[dict], list]:
    from nexus.operators.dispatch import DispatchUsage, claude_dispatch
    from nexus.operators.model_tiers import resolve_model_for_tier

    prompt, schema = BUILDERS[operator_name]()
    resolved_model = resolve_model_for_tier(model) if model in ("cheap", "strong") else model
    usage: list[DispatchUsage] = []
    outputs = []
    for _ in range(n):
        out = await claude_dispatch(
            prompt, schema, timeout=90.0, model=resolved_model,
            operator=operator_name, usage_sink=usage,
        )
        outputs.append(out)
    return outputs, usage


def _summarize(operator_name: str, pair_scores: list[float], raw_outputs: list[dict],
               usage: list, source: str) -> dict:
    return {
        "pairs": pair_scores,
        "n_pairs": len(pair_scores),
        "mean": sum(pair_scores) / len(pair_scores),
        "min": min(pair_scores),
        "max": max(pair_scores),
        "dispatch_count": len(usage),
        "cost_usd": sum(u.cost_usd for u in usage if u.cost_usd is not None),
        "canonical_models": [u.model for u in usage if u.model],
        "raw_outputs": raw_outputs,
        "raw_usage": [asdict(u) for u in usage],
        "source": source,
    }


async def measure_variance() -> dict:
    report: dict = {}
    total_dispatches = 0
    total_cost = 0.0

    for operator_name in _THREE_FRESH:
        outputs, usage = await _dispatch_n(operator_name, 3, model="strong")
        pair_scores = [
            score(operator_name, a, b)
            for a, b in itertools.combinations(outputs, 2)
        ]
        total_dispatches += len(usage)
        total_cost += sum(u.cost_usd for u in usage if u.cost_usd is not None)
        report[operator_name] = _summarize(
            operator_name, pair_scores, outputs, usage,
            "3 fresh dispatches, all pairwise",
        )

    for operator_name in _TWO_FRESH_PLUS_HISTORICAL:
        outputs, usage = await _dispatch_n(operator_name, 2, model="strong")
        new_pair_score = score(operator_name, outputs[0], outputs[1])
        historical_score = _HISTORICAL_PAIR_SCORE[operator_name]
        pair_scores = [historical_score, new_pair_score]
        total_dispatches += len(usage)
        total_cost += sum(u.cost_usd for u in usage if u.cost_usd is not None)
        report[operator_name] = _summarize(
            operator_name, pair_scores, outputs, usage,
            "1 historical (code-path-valid reuse, raw output NOT available "
            "for the historical half -- only the fresh dispatches this run "
            "carry a raw_outputs entry) + 1 fresh pair",
        )

    report["_totals"] = {"dispatch_count": total_dispatches, "cost_usd": total_cost}
    return report


async def topup_one(operator_name: str, out_path: _Path) -> dict:
    """Dispatch exactly ONE more strong-tier run for *operator_name*,
    pairing it against every raw output already on file, and update the
    report in place. Requires an existing report with a non-empty
    ``raw_outputs`` for *operator_name* (i.e. run the full measurement
    first)."""
    if not out_path.exists():
        raise SystemExit(f"no existing report at {out_path} -- run the full measurement first")
    report = json.loads(out_path.read_text())
    if operator_name not in report or not report[operator_name].get("raw_outputs"):
        raise SystemExit(f"{operator_name}: no prior raw_outputs in {out_path} to top up against")

    prior_outputs = report[operator_name]["raw_outputs"]
    new_outputs, usage = await _dispatch_n(operator_name, 1, model="strong")
    new_output = new_outputs[0]

    new_pair_scores = [score(operator_name, prior, new_output) for prior in prior_outputs]
    all_pair_scores = list(report[operator_name]["pairs"]) + new_pair_scores
    all_outputs = prior_outputs + [new_output]

    cost = sum(u.cost_usd for u in usage if u.cost_usd is not None)
    report[operator_name] = {
        "pairs": all_pair_scores,
        "n_pairs": len(all_pair_scores),
        "mean": sum(all_pair_scores) / len(all_pair_scores),
        "min": min(all_pair_scores),
        "max": max(all_pair_scores),
        "dispatch_count": report[operator_name]["dispatch_count"] + len(usage),
        "cost_usd": report[operator_name]["cost_usd"] + cost,
        "canonical_models": report[operator_name]["canonical_models"] + [u.model for u in usage if u.model],
        "raw_outputs": all_outputs,
        "raw_usage": report[operator_name].get("raw_usage", []) + [asdict(u) for u in usage],
        "source": report[operator_name]["source"] + f" + 1 TOPUP dispatch ({len(usage)} new call, ${cost:.3f})",
    }
    report["_totals"]["dispatch_count"] += len(usage)
    report["_totals"]["cost_usd"] += cost
    return report


def _print_summary(report: dict) -> None:
    for name, data in report.items():
        if name == "_totals":
            continue
        print(
            f"{name:20s} n={data['n_pairs']} mean={data['mean']:.4f} "
            f"min={data['min']:.4f} max={data['max']:.4f} "
            f"dispatches={data['dispatch_count']} cost=${data['cost_usd']:.3f} "
            f"src={data['source']}"
        )
    totals = report["_totals"]
    print(f"\nTOTAL dispatches (cumulative): {totals['dispatch_count']}, TOTAL spend (cumulative): ${totals['cost_usd']:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=str, default=str(_DEFAULT_OUT), help=f"report path (default: {_DEFAULT_OUT}, repo-relative + gitignored)")
    parser.add_argument("--topup", type=str, default="", help="operator name: dispatch ONE more run and pair it against the existing report's raw outputs")
    args = parser.parse_args()

    out_path = _Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.topup:
        report = asyncio.run(topup_one(args.topup, out_path))
    else:
        report = asyncio.run(measure_variance())

    _print_summary(report)
    out_path.write_text(json.dumps(report, indent=2))
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
