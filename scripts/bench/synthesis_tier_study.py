# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthesis-operator three-arm study: Fable (box default) vs Sonnet vs
Haiku on summarize / generate / compare / aggregate (nexus-rv9xp, RDR-196).

The four synthesis operators carry most of ``nx_answer``'s spend (every
bundle) and have no .p2a proxy, so nexus-ztunv could not cover them. This
driver calls the PRODUCTION operator functions in ``nexus.mcp.core``
verbatim, with ``nexus.operators.dispatch.claude_dispatch`` wrapped to
inject the arm's ``model=`` and capture usage, on real-corpus inputs
frozen in ``bench/out/synthesis_inputs.json`` (sha256 recorded in the
pre-registration, T2 ``nexus_rdr/196-synthesis-tier-study-REGISTERED-
2026-08-21``).

Quality = mechanical grounding + a blind, position-swapped pairwise
preference judge (sonnet, the non-incumbent; fable as a second judge on
``generate`` only to measure judge agreement; v2 registration
``nexus_rdr/196-synthesis-tier-study-REGISTERED-v2-2026-08-21``). Decision rule per operator x arm vs fable is
pre-registered in :func:`cell_verdict`. Report only; no tier flip here.

Run:
  uv run python scripts/bench/synthesis_tier_study.py            # full
  uv run python scripts/bench/synthesis_tier_study.py --operators summarize --inputs delos   # pilot
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import asyncio
import contextlib
import hashlib
import json
import random
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Final

_REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT: Final[_Path] = _REPO_ROOT / "bench" / "out" / "synthesis_tier_study.json"
DEFAULT_INPUTS: Final[_Path] = _REPO_ROOT / "bench" / "out" / "synthesis_inputs.json"

BUDGET_CEILING_USD: Final[float] = 80.0  # v2 registration: pilot priced judging at 0.57 (sonnet) / 1.3-1.7 (fable) per call
OPERATORS: Final[tuple[str, ...]] = ("summarize", "generate", "compare", "aggregate")
#: v2 registration (pilot): primary judge is sonnet, NOT the incumbent
#: (removes fable self-preference from the decision rule, 3x cheaper);
#: fable judges the generate operator as a second judge to measure
#: agreement between judges. Only pairs involving fable are judged: the
#: decision rule is arm-vs-incumbent, sonnet-vs-haiku never enters it.
JUDGE_MODEL: Final[str | None] = "sonnet"
JUDGE_NAME: Final[str] = "sonnet"
SECOND_JUDGE_MODEL: Final[str | None] = None   # fable: box default
SECOND_JUDGE_NAME: Final[str] = "fable"
SECOND_JUDGE_OPERATOR: Final[str] = "generate"
INCUMBENT_PREFERRED_MAX: Final[float] = 0.60   # arm survives if fable wins <= 60%
GROUNDING_SLACK: Final[float] = 0.10
SWAP_UNSTABLE_AT: Final[float] = 0.50
_ID_RE = re.compile(r"[0-9a-f]{8}")


@dataclass(frozen=True)
class Arm:
    name: str
    model: str | None
    family: str


ARMS: Final[tuple[Arm, ...]] = (
    Arm("fable", None, "fable"),
    Arm("sonnet", "sonnet", "sonnet"),
    Arm("haiku", "haiku", "haiku"),
)


class BudgetExceeded(RuntimeError):
    pass


# ── pure parts ──────────────────────────────────────────────────────────────

def grounding(operator: str, output: dict | None, tags: list[str],
              *, expected_keys: list[str] | None) -> float:
    """Mechanical grounding in [0, 1]; a missing/invalid output is 0.0."""
    if not isinstance(output, dict):
        return 0.0
    # v2 registration (pilot): models cite the 8-hex id with or without
    # the "src:" prefix; match the id suffix, not the literal tag.
    ids = {t.split(":", 1)[-1] for t in tags}
    if operator in ("summarize", "generate"):
        cites = output.get("citations") or []
        if not cites:
            return 0.0
        hits = sum(1 for c in cites if any(i in str(c) for i in ids))
        return hits / len(cites)
    if operator == "compare":
        text = str(output.get("comparison") or "")
        found = {m for m in _ID_RE.findall(text) if m in ids}
        return 1.0 if text.strip() and len(found) >= 2 else 0.0
    if operator == "aggregate":
        aggs = output.get("aggregates") or []
        got = {str(a.get("key_value")) for a in aggs if isinstance(a, dict)}
        exp = set(expected_keys or [])
        jac = len(got & exp) / len(got | exp) if (got | exp) else 0.0
        nonempty = (
            sum(1 for a in aggs if isinstance(a, dict) and str(a.get("summary") or "").strip())
            / len(aggs) if aggs else 0.0
        )
        return jac * nonempty
    raise ValueError(operator)


def judge_pairs(arms: list[str], *, incumbent: str = "fable") -> list[tuple[str, str]]:
    """Ordered pairs involving the incumbent, both positions (v2: the
    decision rule is arm-vs-incumbent only)."""
    return [(a, b) for a in arms for b in arms if a != b and incumbent in (a, b)]


def tally_judgments(judgments: list[tuple[str, str, str, str]], *, incumbent: str, arm: str) -> dict[str, Any]:
    """judgments: (left_arm, right_arm, winner A|B|tie, input_name)."""
    rel = [j for j in judgments if {j[0], j[1]} == {incumbent, arm}]
    inc = armw = ties = 0
    by_input: dict[str, list[str]] = {}
    for left, right, winner, name in rel:
        if winner == "tie":
            ties += 1
            by_input.setdefault(name, []).append("tie")
            continue
        won = left if winner == "A" else right
        if won == incumbent:
            inc += 1
        else:
            armw += 1
        by_input.setdefault(name, []).append(won)
    n = len(rel)
    swaps = [v for v in by_input.values() if len(v) == 2]
    disagree = sum(1 for v in swaps if v[0] != v[1])
    return {
        "n": n, "incumbent_preferred": inc, "arm_preferred": armw, "ties": ties,
        "incumbent_preferred_rate": (inc / n) if n else None,
        "swap_disagreement_rate": (disagree / len(swaps)) if swaps else None,
    }


def cell_verdict(*, incumbent_rate: float | None, arm_grounding: float | None,
                 incumbent_grounding: float | None, failures: int,
                 swap_disagreement: float | None) -> str:
    if incumbent_rate is None:
        # No completed judgment reached this cell (e.g. every judge call
        # failed on a spend limit). That is missing DATA, not a verdict.
        return "NO_DATA"
    if failures > 0 or arm_grounding is None or incumbent_grounding is None:
        return "REFUTED"
    if incumbent_rate > INCUMBENT_PREFERRED_MAX:
        return "REFUTED"
    if arm_grounding < incumbent_grounding - GROUNDING_SLACK:
        return "REFUTED"
    verdict = "NOT_REFUTED"
    if swap_disagreement is not None and swap_disagreement >= SWAP_UNSTABLE_AT:
        verdict += "/JUDGE_UNSTABLE"
    return verdict


# ── inputs ──────────────────────────────────────────────────────────────────

def load_inputs(path: _Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    blob = json.dumps({"inputs": data["inputs"]}, indent=2, sort_keys=True)
    data["sha256_check"] = hashlib.sha256(blob.encode()).hexdigest()
    return data


def _context_text(inp: dict[str, Any]) -> str:
    return "\n\n".join(f"[{c['tag']}] {c['title']}\n{c['text']}" for c in inp["chunks"])


def _groups(inp: dict[str, Any]) -> tuple[str, list[str]]:
    by_title: dict[str, list[dict]] = {}
    for c in inp["chunks"]:
        key = (c["title"] or c["tag"])[:80]
        by_title.setdefault(key, []).append({"id": c["tag"], "text": c["text"]})
    groups = [{"key_value": k, "items": v} for k, v in by_title.items()]
    return json.dumps(groups), [g["key_value"] for g in groups]


def operator_call(operator: str, inp: dict[str, Any]) -> tuple[str, dict[str, Any], list[str] | None]:
    """(core function name, kwargs, expected aggregate keys)."""
    q = inp["question"]
    ctx = _context_text(inp)
    if operator == "summarize":
        return "operator_summarize", {"content": f"Question: {q}\n\n{ctx}", "cited": True}, None
    if operator == "generate":
        return "operator_generate", {
            "template": f"structured research brief answering: {q}",
            "context": ctx, "cited": True,
        }, None
    if operator == "compare":
        items = [{"id": c["tag"], "title": c["title"], "text": c["text"]} for c in inp["chunks"]]
        return "operator_compare", {"items": json.dumps(items), "focus": q}, None
    if operator == "aggregate":
        groups, keys = _groups(inp)
        return "operator_aggregate", {
            "groups": groups, "reducer": f"the key claims relevant to: {q}", "source": "llm",
        }, keys
    raise ValueError(operator)


# ── dispatch plumbing ────────────────────────────────────────────────────────

@contextlib.contextmanager
def _inject_model(model: str | None, sink: list):
    """Wrap the production dispatcher so operator functions (which import
    claude_dispatch at call time) run with this arm's model."""
    import nexus.operators.dispatch as dispatch_mod

    real = dispatch_mod.claude_dispatch

    async def wrapped(prompt, schema, timeout=300.0, **kw):
        kw.pop("model", None)
        kw.pop("usage_sink", None)
        return await real(prompt, schema, timeout=timeout, model=model, usage_sink=sink, **kw)

    dispatch_mod.claude_dispatch = wrapped
    try:
        yield
    finally:
        dispatch_mod.claude_dispatch = real


async def _run_operator(operator: str, arm: Arm, inp: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    import nexus.mcp.core as core

    fn_name, kwargs, expected_keys = operator_call(operator, inp)
    fn = getattr(core, fn_name)
    sink: list = []
    t0 = time.monotonic()
    with _inject_model(arm.model, sink):
        try:
            output = await fn(**kwargs, timeout=timeout)
            error = None
        except Exception as exc:  # noqa: BLE001 — a plumbing failure is DATA, counted per arm
            output, error = None, f"{type(exc).__name__}: {exc}"
    usage = asdict(sink[0]) if sink else {}
    canonical = usage.get("model")
    tags = [c["tag"] for c in inp["chunks"]]
    return {
        "kind": "generation", "operator": operator, "arm": arm.name, "input": inp["name"],
        "requested_model": arm.model, "canonical_model": canonical,
        "family_ok": bool(canonical) and arm.family in str(canonical).lower(),
        "elapsed_s": time.monotonic() - t0, "cost_usd": usage.get("cost_usd"),
        "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
        "output": output, "error": error,
        "grounding": grounding(operator, output, tags, expected_keys=expected_keys),
    }


_JUDGE_SCHEMA: Final[dict] = {
    "type": "object", "required": ["winner", "reason"],
    "properties": {"winner": {"type": "string", "enum": ["A", "B", "tie"]},
                   "reason": {"type": "string"}},
}


def _judge_prompt(operator: str, inp: dict[str, Any], out_a: Any, out_b: Any) -> str:
    return (
        "You are judging two candidate outputs of the same analytical operator on the same "
        "source context. Judge ONLY on: faithfulness to the provided context (no claims the "
        "context does not support), coverage of the question, specificity, and absence of "
        "fabrication. Ignore length and style. Answer 'tie' when neither is better on those "
        f"criteria.\n\nOperator: {operator}\nQuestion: {inp['question']}\n\n"
        f"CONTEXT:\n{_context_text(inp)}\n\n"
        f"CANDIDATE A:\n{json.dumps(out_a, indent=1, default=str)[:12000]}\n\n"
        f"CANDIDATE B:\n{json.dumps(out_b, indent=1, default=str)[:12000]}\n"
    )


async def _judge(operator: str, inp: dict[str, Any], left: dict, right: dict, *,
                 judge_model: str | None, judge_name: str, timeout: float) -> dict[str, Any]:
    from nexus.operators.dispatch import claude_dispatch

    sink: list = []
    t0 = time.monotonic()
    try:
        res = await claude_dispatch(
            _judge_prompt(operator, inp, left["output"], right["output"]), _JUDGE_SCHEMA,
            timeout=timeout, model=judge_model, operator="judge", usage_sink=sink,
        )
        winner, reason, error = res.get("winner"), res.get("reason"), None
    except Exception as exc:  # noqa: BLE001 — judge failure is recorded, never silently dropped
        winner, reason, error = None, None, f"{type(exc).__name__}: {exc}"
    usage = asdict(sink[0]) if sink else {}
    return {
        "kind": "judgment", "operator": operator, "input": inp["name"],
        "left": left["arm"], "right": right["arm"], "judge": judge_name,
        "judge_canonical_model": usage.get("model"), "winner": winner, "reason": reason,
        "error": error, "cost_usd": usage.get("cost_usd"), "elapsed_s": time.monotonic() - t0,
    }


# ── analysis ─────────────────────────────────────────────────────────────────

def analyze(records: list[dict[str, Any]]) -> dict[str, Any]:
    gens = [r for r in records if r["kind"] == "generation"]
    judg = [r for r in records if r["kind"] == "judgment"]
    costs = [r["cost_usd"] for r in records if r.get("cost_usd") is not None]
    out: dict[str, Any] = {
        "n_generations": len(gens), "n_judgments": len(judg),
        "total_cost_usd": sum(costs), "n_cost_unknown": len(records) - len(costs),
        "generation_cost_usd": sum(r["cost_usd"] for r in gens if r.get("cost_usd") is not None),
        "judge_cost_usd": sum(r["cost_usd"] for r in judg if r.get("cost_usd") is not None),
        "operators": {},
    }
    for op in OPERATORS:
        g = [r for r in gens if r["operator"] == op]
        if not g:
            continue
        entry: dict[str, Any] = {"arms": {}, "cells": {}, "judge_agreement": None}
        for arm in ARMS:
            rs = [r for r in g if r["arm"] == arm.name]
            if not rs:
                continue
            c = [r["cost_usd"] for r in rs if r["cost_usd"] is not None]
            entry["arms"][arm.name] = {
                "n": len(rs), "failures": sum(1 for r in rs if r["error"]),
                "family_violations": sum(1 for r in rs if not r["family_ok"]),
                "canonical_models": sorted({str(r["canonical_model"]) for r in rs}),
                "cost_mean": (sum(c) / len(c)) if c else None,
                "output_tokens_mean": (sum(r["output_tokens"] or 0 for r in rs) / len(rs)),
                "grounding_mean": sum(r["grounding"] for r in rs) / len(rs),
            }
        fable = entry["arms"].get("fable")
        fable_void = fable is None or fable["family_violations"] > 0
        entry["fable_void"] = fable_void
        primary = [
            (r["left"], r["right"], r["winner"], r["input"])
            for r in judg if r["operator"] == op and r["judge"] == JUDGE_NAME and r["winner"]
        ]
        for arm in ARMS:
            if arm.name == "fable" or arm.name not in entry["arms"]:
                continue
            t = tally_judgments(primary, incumbent="fable", arm=arm.name)
            a = entry["arms"][arm.name]
            a["cost_ratio_vs_fable"] = (
                a["cost_mean"] / fable["cost_mean"] if (fable and a["cost_mean"] and fable["cost_mean"]) else None
            )
            verdict = "VOID" if fable_void else cell_verdict(
                incumbent_rate=t["incumbent_preferred_rate"],
                arm_grounding=a["grounding_mean"],
                incumbent_grounding=fable["grounding_mean"] if fable else None,
                failures=a["failures"] + a["family_violations"],
                swap_disagreement=t["swap_disagreement_rate"],
            )
            entry["cells"][arm.name] = {**t, "verdict": verdict}
        second = [r for r in judg if r["operator"] == op and r["judge"] == SECOND_JUDGE_NAME and r["winner"]]
        if second:
            prim_by = {(r["left"], r["right"], r["input"]): r["winner"] for r in judg
                       if r["operator"] == op and r["judge"] == JUDGE_NAME and r["winner"]}
            agree = [prim_by.get((r["left"], r["right"], r["input"])) == r["winner"] for r in second]
            entry["judge_agreement"] = {"n": len(agree), "rate": sum(agree) / len(agree)}
        out["operators"][op] = entry
    return out


# ── run ──────────────────────────────────────────────────────────────────────

def _spent(records: list[dict[str, Any]]) -> float:
    return sum(r["cost_usd"] for r in records if r.get("cost_usd") is not None)


def _guard(records: list[dict[str, Any]]) -> None:
    if _spent(records) >= BUDGET_CEILING_USD:
        raise BudgetExceeded(f"spend {_spent(records):.2f} reached ceiling {BUDGET_CEILING_USD}")


async def run(operators: list[str], input_names: list[str] | None, *, out_path: _Path,
              inputs_path: _Path, timeout: float, seed: int = 196) -> dict[str, Any]:
    inputs = load_inputs(inputs_path)
    chosen = [i for i in inputs["inputs"] if input_names is None or i["name"] in input_names]
    state: dict[str, Any] = (
        json.loads(out_path.read_text()) if out_path.exists()
        else {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "inputs_sha256": inputs.get("sha256"), "records": []}
    )
    records: list[dict[str, Any]] = state["records"]
    rng = random.Random(seed)

    def _save() -> None:
        state["analysis"] = analyze(records)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(state, indent=2, default=str))

    # generations: per operator, per input, arms interleaved
    for op in operators:
        for inp in chosen:
            for arm in ARMS:
                # Same resume rule as judgments: only a COMPLETED generation
                # (output present) counts as done; a failed one is retried.
                if any(r["kind"] == "generation" and r.get("output") is not None
                       and (r["operator"], r["input"], r["arm"]) == (op, inp["name"], arm.name)
                       for r in records):
                    continue
                _guard(records)
                rec = await _run_operator(op, arm, inp, timeout=timeout)
                records.append(rec)
                print(f"[gen {op}/{arm.name}/{inp['name']}] cost={rec['cost_usd']} model={rec['canonical_model']} "
                      f"grounding={rec['grounding']:.2f} err={rec['error']} spent={_spent(records):.2f}")
                _save()
    # judgments: both positions per unordered pair, randomised order within each input
    for op in operators:
        for inp in chosen:
            gens = {r["arm"]: r for r in records
                    if r["kind"] == "generation" and r["operator"] == op and r["input"] == inp["name"]
                    and r["output"] is not None}
            pairs = [p for p in judge_pairs([a.name for a in ARMS]) if p[0] in gens and p[1] in gens]
            rng.shuffle(pairs)
            judges: list[tuple[str | None, str]] = [(JUDGE_MODEL, JUDGE_NAME)]
            if op == SECOND_JUDGE_OPERATOR:
                judges.append((SECOND_JUDGE_MODEL, SECOND_JUDGE_NAME))
            for judge_model, jname in judges:
                for left, right in pairs:
                    # A FAILED judgment (winner None, e.g. a spend-limit
                    # error) must not block its own retry on resume; only a
                    # completed one counts as done. The failed record stays
                    # on file for the audit trail.
                    if any(r["kind"] == "judgment" and r.get("winner")
                           and (r["operator"], r["input"], r["left"], r["right"], r["judge"])
                           == (op, inp["name"], left, right, jname) for r in records):
                        continue
                    _guard(records)
                    rec = await _judge(op, inp, gens[left], gens[right], judge_model=judge_model,
                                       judge_name=jname, timeout=timeout)
                    records.append(rec)
                    print(f"[judge {jname} {op}/{inp['name']} {left} vs {right}] winner={rec['winner']} "
                          f"cost={rec['cost_usd']} err={rec['error']} spent={_spent(records):.2f}")
                    _save()
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save()
    return state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--operators", default=",".join(OPERATORS))
    p.add_argument("--inputs", default=None, help="comma-separated input names (default: all)")
    p.add_argument("--out", type=_Path, default=DEFAULT_OUT)
    p.add_argument("--inputs-file", type=_Path, default=DEFAULT_INPUTS)
    p.add_argument("--timeout", type=float, default=240.0)
    args = p.parse_args(argv)
    ops = [o for o in args.operators.split(",") if o]
    bad = [o for o in ops if o not in OPERATORS]
    if bad:
        p.error(f"unknown operators {bad}; known: {OPERATORS}")
    names = [n for n in args.inputs.split(",") if n] if args.inputs else None
    try:
        state = asyncio.run(run(ops, names, out_path=args.out, inputs_path=args.inputs_file, timeout=args.timeout))
    except BudgetExceeded as exc:
        print(f"ABORTED: {exc}")
        return 2
    a = state["analysis"]
    print(f"\n=== gens {a['n_generations']} judgments {a['n_judgments']} total {a['total_cost_usd']:.2f} USD "
          f"(gen {a['generation_cost_usd']:.2f}, judge {a['judge_cost_usd']:.2f}) -> {args.out}")
    for op, e in a["operators"].items():
        print(f"{op}: fable_void={e['fable_void']} judge_agreement={e['judge_agreement']}")
        for arm, s in e["arms"].items():
            print(f"  {arm:6} cost {s['cost_mean']} x{s.get('cost_ratio_vs_fable')} grounding {s['grounding_mean']:.2f} "
                  f"out_tok {s['output_tokens_mean']:.0f} fails {s['failures']} models {s['canonical_models']}")
        for arm, c in e["cells"].items():
            print(f"  vs fable {arm}: fable-preferred {c['incumbent_preferred']}/{c['n']} ties {c['ties']} "
                  f"swap-disagree {c['swap_disagreement_rate']} -> {c['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
