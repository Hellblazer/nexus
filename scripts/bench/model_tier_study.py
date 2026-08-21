# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three-arm model-tier study: Fable (box default, no override) vs Sonnet
vs Haiku on every .p2a proxy-covered operator (nexus-ztunv, RDR-196).

Pre-registered design (arms, operators, n, metrics, thresholds, decision
rule, budget ceiling): T2 ``nexus_rdr/196-model-tier-study-REGISTERED-
2026-08-21`` (frozen). Results: ``nexus_rdr/196-model-tier-study``.

Real money. Dispatches ``N_PER_ARM`` ``claude -p`` calls per arm per
operator through :func:`nexus.operators.dispatch.claude_dispatch`, using
the SAME builders and scorers as .p2a (``bench.operator_proxy``). Arms are
interleaved within each round. Raw outputs and usage are persisted after
EVERY dispatch (``bench/out/model_tier_study.json``, gitignored) so a
crash never discards paid-for data, and the driver aborts before the
dispatch that would cross :data:`BUDGET_CEILING_USD`.

Run:
  uv run python scripts/bench/model_tier_study.py
  uv run python scripts/bench/model_tier_study.py --operators operator_filter --n 1   # pilot
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
import time
from dataclasses import asdict, dataclass
from typing import Any, Final

from bench.operator_proxy import BUILDERS, score
from bench.operator_proxy_metrics import THRESHOLDS

_REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT: Final[_Path] = _REPO_ROOT / "bench" / "out" / "model_tier_study.json"

#: Hard cumulative ceiling, pre-registered. The guard runs BEFORE each
#: dispatch, so the most the run can overshoot is one dispatch.
BUDGET_CEILING_USD: Final[float] = 30.0
N_PER_ARM_DEFAULT: Final[int] = 3
DEFAULT_OPERATORS: Final[tuple[str, ...]] = (
    "operator_filter", "operator_groupby", "operator_rank",
    "operator_extract", "operator_check", "operator_verify",
)


@dataclass(frozen=True)
class Arm:
    name: str
    model: str | None  # None = no --model flag = the production HOLD path
    family: str        # substring the recorded canonical id must carry


ARMS: Final[tuple[Arm, ...]] = (
    Arm("fable", None, "fable"),
    Arm("sonnet", "sonnet", "sonnet"),
    Arm("haiku", "haiku", "haiku"),
)


class BudgetExceeded(RuntimeError):
    pass


def guard_budget(*, spent: float) -> None:
    if spent >= BUDGET_CEILING_USD:
        raise BudgetExceeded(f"cumulative spend {spent:.4f} USD reached the ceiling {BUDGET_CEILING_USD}")


def dispatch_order(operators: list[str], *, n: int) -> list[tuple[str, int, str]]:
    """(operator, round, arm) in pre-registered order: per operator, per
    round, arms interleaved."""
    return [(op, i, arm.name) for op in operators for i in range(n) for arm in ARMS]


def within_arm_pairs(n: int) -> list[tuple[int, int]]:
    return list(itertools.combinations(range(n), 2))


def cross_arm_pairs(n_a: int, n_b: int) -> list[tuple[int, int]]:
    return [(i, j) for i in range(n_a) for j in range(n_b)]


def summarize_scores(scores: list[float | None]) -> dict[str, Any]:
    real = [s for s in scores if s is not None]
    if not real:
        return {"n": 0, "n_none": len(scores), "mean": None, "min": None, "max": None}
    return {
        "n": len(real), "n_none": len(scores) - len(real),
        "mean": sum(real) / len(real), "min": min(real), "max": max(real),
    }


def arm_verdict(*, min_vs_fable: float | None, threshold: float, failures: int,
                fable_noise_min: float | None, fable_void: bool = False) -> str:
    """Pre-registered decision rule. Report only; never flips a tier.

    ``fable_void``: the registration voids the fable arm when ANY of its
    dispatches recorded a canonical id outside the fable family (box-
    default drift); with no valid reference arm every verdict is VOID.
    """
    if fable_void:
        return "VOID"
    if fable_noise_min is None or fable_noise_min < threshold:
        return "NOISE_LIMITED"
    if failures > 0 or min_vs_fable is None or min_vs_fable < threshold:
        return "REFUTED"
    return "NOT_REFUTED"


def _usage_dict(u: Any) -> dict[str, Any]:
    return asdict(u) if u is not None else {}


async def _dispatch_one(operator: str, arm: Arm, *, timeout: float) -> dict[str, Any]:
    from nexus.operators.dispatch import DispatchUsage, claude_dispatch

    prompt, schema = BUILDERS[operator]()
    sink: list[DispatchUsage] = []
    t0 = time.monotonic()
    try:
        output = await claude_dispatch(
            prompt, schema, timeout=timeout, model=arm.model, operator=operator,
            usage_sink=sink,
        )
        error = None
    except Exception as exc:  # noqa: BLE001 — a plumbing failure is DATA here, counted per arm
        output, error = None, f"{type(exc).__name__}: {exc}"
    usage = _usage_dict(sink[0]) if sink else {}
    canonical = usage.get("model")
    return {
        "operator": operator, "arm": arm.name, "requested_model": arm.model,
        "canonical_model": canonical,
        "family_ok": bool(canonical) and arm.family in str(canonical).lower(),
        "elapsed_s": time.monotonic() - t0,
        "cost_usd": usage.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "output": output, "error": error,
    }


def analyze(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: per operator x arm cost summary, within-arm noise, cross-arm
    agreement, and the pre-registered verdict."""
    out: dict[str, Any] = {"operators": {}, "total_cost_usd": 0.0, "n_dispatches": len(records)}
    costs_known = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    out["total_cost_usd"] = sum(costs_known)
    out["n_cost_unknown"] = len(records) - len(costs_known)
    by_op: dict[str, dict[str, list[dict]]] = {}
    for r in records:
        by_op.setdefault(r["operator"], {}).setdefault(r["arm"], []).append(r)
    for op, arms in by_op.items():
        threshold = THRESHOLDS[op]
        entry: dict[str, Any] = {"threshold": threshold, "arms": {}, "cross": {}, "verdicts": {}}
        outputs = {
            a: [r["output"] for r in rs if r["output"] is not None] for a, rs in arms.items()
        }
        for a, rs in arms.items():
            costs = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
            entry["arms"][a] = {
                "n": len(rs),
                "failures": sum(1 for r in rs if r["error"] is not None),
                "family_violations": sum(1 for r in rs if not r["family_ok"]),
                "canonical_models": sorted({str(r["canonical_model"]) for r in rs}),
                "cost": summarize_scores(costs),
                "elapsed_s_mean": sum(r["elapsed_s"] for r in rs) / len(rs),
                "noise": summarize_scores([
                    score(op, outputs[a][i], outputs[a][j])
                    for i, j in within_arm_pairs(len(outputs[a]))
                ]),
            }
        fable_cost = entry["arms"].get("fable", {}).get("cost", {}).get("mean")
        for a in entry["arms"]:
            c = entry["arms"][a]["cost"]["mean"]
            entry["arms"][a]["cost_ratio_vs_fable"] = (
                c / fable_cost if (c is not None and fable_cost) else None
            )
        for a, b in itertools.combinations(sorted(arms), 2):
            entry["cross"][f"{a}_vs_{b}"] = summarize_scores([
                score(op, outputs[a][i], outputs[b][j])
                for i, j in cross_arm_pairs(len(outputs[a]), len(outputs[b]))
            ])
        fable_noise_min = entry["arms"].get("fable", {}).get("noise", {}).get("min")
        fable_arm = entry["arms"].get("fable")
        fable_void = fable_arm is None or fable_arm["family_violations"] > 0
        entry["fable_void"] = fable_void
        for a in arms:
            if a == "fable":
                continue
            key = "_vs_".join(sorted(["fable", a]))
            entry["verdicts"][a] = arm_verdict(
                min_vs_fable=entry["cross"].get(key, {}).get("min"),
                threshold=threshold,
                failures=entry["arms"][a]["failures"] + entry["arms"][a]["family_violations"],
                fable_noise_min=fable_noise_min,
                fable_void=fable_void,
            )
        out["operators"][op] = entry
    return out


async def run(operators: list[str], *, n: int, out_path: _Path, timeout: float) -> dict[str, Any]:
    state: dict[str, Any] = (
        json.loads(out_path.read_text()) if out_path.exists()
        else {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "records": []}
    )
    records: list[dict[str, Any]] = state["records"]
    done = {(r["operator"], r["round"], r["arm"]) for r in records}
    arms_by_name = {a.name: a for a in ARMS}
    for op, i, arm_name in dispatch_order(operators, n=n):
        if (op, i, arm_name) in done:
            continue
        spent = sum(r["cost_usd"] for r in records if r.get("cost_usd") is not None)
        guard_budget(spent=spent)
        rec = await _dispatch_one(op, arms_by_name[arm_name], timeout=timeout)
        rec["round"] = i
        records.append(rec)
        print(
            f"[{op}/{arm_name}][{i}] cost={rec['cost_usd']} model={rec['canonical_model']} "
            f"elapsed={rec['elapsed_s']:.1f}s error={rec['error']} spent={spent + (rec['cost_usd'] or 0):.2f}"
        )
        state["analysis"] = analyze(records)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(state, indent=2, default=str))
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state["analysis"] = analyze(records)
    out_path.write_text(json.dumps(state, indent=2, default=str))
    return state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--operators", default=",".join(DEFAULT_OPERATORS))
    p.add_argument("--n", type=int, default=N_PER_ARM_DEFAULT)
    p.add_argument("--out", type=_Path, default=DEFAULT_OUT)
    p.add_argument("--timeout", type=float, default=120.0)
    args = p.parse_args(argv)
    ops = [o for o in args.operators.split(",") if o]
    unknown = [o for o in ops if o not in BUILDERS]
    if unknown:
        p.error(f"unknown operators {unknown}; known: {sorted(BUILDERS)}")
    try:
        state = asyncio.run(run(ops, n=args.n, out_path=args.out, timeout=args.timeout))
    except BudgetExceeded as exc:
        print(f"ABORTED: {exc}")
        return 2
    a = state["analysis"]
    print(f"\n=== {a['n_dispatches']} dispatches, {a['total_cost_usd']:.2f} USD "
          f"({a['n_cost_unknown']} unknown-cost) -> {args.out}")
    for op, e in a["operators"].items():
        print(f"{op} (thr {e['threshold']}):")
        for arm, s in e["arms"].items():
            print(f"  {arm:6} cost mean {s['cost']['mean']} x{s['cost_ratio_vs_fable']} "
                  f"noise min {s['noise']['min']} fails {s['failures']} models {s['canonical_models']}")
        for k, s in e["cross"].items():
            print(f"  {k}: mean {s['mean']} min {s['min']}")
        print(f"  verdicts: {e['verdicts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
