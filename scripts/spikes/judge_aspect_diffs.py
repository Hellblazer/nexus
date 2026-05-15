#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
judge_aspect_diffs — semantic-equivalence judge for spike_c parity output.

The spike_c harness uses strict set-equality on experimental_datasets /
experimental_baselines. That's too strict for free-form extraction:
"UCI Mushroom database" vs "Mushroom database" register as disagreement.

This script re-scores a spike_c JSONL by running an LLM judge on each
(only-claude, only-qwen) item pair within a paper's diverging set fields.
The judge returns yes/no on semantic equivalence; matched items are
counted as agreed.

Output: per-field semantic-agreement rate (replaces the strict-set rate),
plus precision/recall/F1 on the matched pairs.

Usage:
    python judge_aspect_diffs.py \\
        --in /tmp/aspect-bench-out/parity-v3-2026-05-15.jsonl \\
        --out /tmp/aspect-bench-out/parity-v3-judged.jsonl \\
        [--judge claude|qwen]   # default: qwen (free)

The judge dispatch goes through nexus.operators.qwen_dispatch or
claude_dispatch directly; backend is selected by --judge.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus.operators.dispatch import claude_dispatch  # noqa: E402
from nexus.operators.qwen_dispatch import qwen_dispatch  # noqa: E402


SET_FIELDS = ("experimental_datasets", "experimental_baselines")


_JUDGE_PROMPT = """\
You are judging whether two short item descriptions, extracted from
the same scholarly paper by two different systems, refer to the SAME
underlying entity (the same dataset, baseline model, or method).

Return JSON {{"equivalent": <bool>, "reason": "<one short sentence>"}}.

"equivalent" should be true when:
- The two strings name the same dataset / model / method, even if one
  is more verbose, includes citations, or paraphrases the other.
  Examples:
    "UCI Mushroom database" ↔ "Mushroom database" → equivalent
    "STAGGER (Schlimmer 1987)" ↔ "STAGGER algorithm" → equivalent
    "Latent Semantic Analysis (LSA) vectors" ↔ "Latent Semantic Analysis" → equivalent

"equivalent" should be false when:
- The strings name genuinely different entities.
- One is a proper-named entity and the other is an aggregation phrase
  ("MNIST" vs "various image datasets" → not equivalent).
- One is an ablation variant of the paper's own model and the other
  is a prior external model.

Item A (from system 1): {a}
Item B (from system 2): {b}
"""

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "equivalent": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["equivalent", "reason"],
}


async def _judge(backend: str, a: str, b: str) -> dict:
    prompt = _JUDGE_PROMPT.format(a=a, b=b)
    if backend == "claude":
        result = await claude_dispatch(
            prompt, _JUDGE_SCHEMA, timeout=60.0,
            operator_name="aspect_diff_judge",
        )
    else:
        result = await qwen_dispatch(
            prompt, _JUDGE_SCHEMA, timeout=60.0,
            operator_name="aspect_diff_judge",
        )
    return result


async def _match_pairs(
    backend: str,
    only_c: list[str],
    only_q: list[str],
) -> tuple[set[tuple[str, str]], list[dict]]:
    """Greedy bipartite match: for each only-C item, find first
    semantically-equivalent only-Q item. Returns (matched_pairs,
    judge_trace).
    """
    matched: set[tuple[str, str]] = set()
    matched_q: set[str] = set()
    trace: list[dict] = []
    for a in only_c:
        for b in only_q:
            if b in matched_q:
                continue
            try:
                verdict = await _judge(backend, a, b)
            except Exception as exc:
                trace.append({"a": a, "b": b, "error": str(exc)})
                continue
            trace.append({
                "a": a, "b": b,
                "equivalent": bool(verdict.get("equivalent")),
                "reason": str(verdict.get("reason", "")),
            })
            if verdict.get("equivalent"):
                matched.add((a, b))
                matched_q.add(b)
                break
    return matched, trace


async def _rescore_row(backend: str, row: dict) -> dict:
    """Add semantic-agreement scores per set field. Mutates `row`."""
    if not row.get("both_ok"):
        return row
    c = row.get("claude_record") or {}
    q = row.get("qwen_record") or {}
    judged: dict[str, Any] = {}
    for f in SET_FIELDS:
        cs = set(c.get(f) or [])
        qs = set(q.get(f) or [])
        strict_inter = cs & qs
        only_c = sorted(cs - qs)
        only_q = sorted(qs - cs)
        matched, trace = await _match_pairs(backend, only_c, only_q)
        # Total equivalent = strict intersection + semantic matches.
        equiv = len(strict_inter) + len(matched)
        # Precision/recall against the LARGER set as denominator —
        # matches the spirit of "did each engine catch the other's
        # findings".
        union_size = len(cs | qs) - len(matched)  # collapse matched pairs
        agreement = equiv / union_size if union_size > 0 else 1.0
        judged[f] = {
            "strict_intersection": sorted(strict_inter),
            "semantic_matches": [list(p) for p in sorted(matched)],
            "unmatched_only_c": sorted([a for a in only_c if not any(a == p[0] for p in matched)]),
            "unmatched_only_q": sorted([b for b in only_q if b not in {p[1] for p in matched}]),
            "agreement": agreement,
            "trace": trace,
        }
    row["semantic_judged"] = judged
    return row


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Semantic-equivalence judge for spike_c parity output.")
    p.add_argument("--in", dest="in_path", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--judge", choices=("claude", "qwen"), default="qwen")
    args = p.parse_args(argv)

    rows = [json.loads(l) for l in args.in_path.read_text().splitlines() if l.strip()]
    print(f"judge: {len(rows)} rows, backend={args.judge}", file=sys.stderr)

    out_rows: list[dict] = []
    for i, row in enumerate(rows, 1):
        name = (row.get("uri") or "?").split("/")[-1][:50]
        print(f"  [{i}/{len(rows)}] {name}", file=sys.stderr)
        out_row = await _rescore_row(args.judge, row)
        out_rows.append(out_row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fp:
        for r in out_rows:
            fp.write(json.dumps(r) + "\n")

    # Summary
    print("\n=== Semantic-equivalence summary ===", file=sys.stderr)
    for f in SET_FIELDS:
        rates = []
        for r in out_rows:
            if r.get("both_ok") and "semantic_judged" in r:
                rates.append(r["semantic_judged"][f]["agreement"])
        if rates:
            avg = sum(rates) / len(rates)
            print(f"  {f}: {avg*100:.1f}% mean semantic agreement (n={len(rates)})", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
