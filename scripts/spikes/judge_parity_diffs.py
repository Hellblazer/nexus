#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
judge_parity_diffs — semantic-equivalence judge for spike-C *and* spike-D parity output.

The spike_c (aspect_extractor) and spike_d (tier-B tool-use) harnesses
use strict set-equality / Jaccard on canonical fields. That's too
strict for free-form extraction and generated content: paraphrased
constraints, dataset-name variants, etc. register as full
disagreement.

This script re-scores either spike-C or spike-D JSONL output by
running an LLM judge on each (only-claude, only-qwen) item pair within
diverging set fields. Schema auto-detected from row shape.

  spike-C: row has ``claude_record`` + ``qwen_record``; set fields are
           ``experimental_datasets`` and ``experimental_baselines``.

  spike-D: row has ``claude_agent.payload`` + ``qwen_agent.payload``;
           set fields dispatch on ``row["tool"]``:
             - nx_enrich_beads: key_files, test_commands, constraints
             - nx_tidy:         actions (dict items)
             - nx_plan_audit:   findings (dict items)

Dict-shaped set items are canonicalised via ``json.dumps(item,
sort_keys=True)`` before being passed to the judge, mirroring spike-D's
own structural-Jaccard helper.

Usage:
    python judge_parity_diffs.py \\
        --in /tmp/spike-d-out/parity-tier-b-full-2026-05-15.jsonl \\
        --out /tmp/spike-d-out/parity-tier-b-judged.jsonl \\
        [--judge claude|qwen]    # default: qwen (free at margin)
        [--schema spike-c|spike-d|auto]   # default: auto
        [--prose]                # also judge diverging prose fields
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


# ── Schema config ────────────────────────────────────────────────────────────

# spike-C: shared across all rows.
SPIKE_C_SET_FIELDS: tuple[str, ...] = (
    "experimental_datasets",
    "experimental_baselines",
)
SPIKE_C_PROSE_FIELDS: tuple[str, ...] = ()

# spike-D: per-tool. Mirrors STRUCTURAL_FIELDS / PROSE_FIELDS in
# scripts/spikes/spike_d_tier_b_parity.py.
SPIKE_D_SET_FIELDS: dict[str, tuple[str, ...]] = {
    "nx_enrich_beads": ("key_files", "test_commands", "constraints"),
    "nx_tidy": ("actions",),
    "nx_plan_audit": ("findings",),
}
SPIKE_D_PROSE_FIELDS: dict[str, tuple[str, ...]] = {
    "nx_enrich_beads": ("enriched_description",),
    "nx_tidy": ("summary",),
    "nx_plan_audit": ("verdict", "summary"),
}

PROSE_LEN_TOL: float = 0.5
PROSE_MAX_CHARS: int = 4000


# ── Schema detection ─────────────────────────────────────────────────────────


def detect_schema(row: dict) -> str:
    """Return 'spike-c' or 'spike-d'; raise on unknown shape."""
    if "claude_record" in row and "qwen_record" in row:
        return "spike-c"
    ca = row.get("claude_agent")
    qa = row.get("qwen_agent")
    if isinstance(ca, dict) and isinstance(qa, dict) and (
        "payload" in ca or "payload" in qa
    ):
        return "spike-d"
    raise ValueError(
        "cannot detect schema: row lacks both (claude_record, qwen_record) "
        "and (claude_agent.payload, qwen_agent.payload) — got keys: "
        f"{sorted(row.keys())}"
    )


def _payloads_for(row: dict, schema: str) -> tuple[dict, dict]:
    if schema == "spike-c":
        return (row.get("claude_record") or {}, row.get("qwen_record") or {})
    # spike-d
    ca = row.get("claude_agent") or {}
    qa = row.get("qwen_agent") or {}
    return (ca.get("payload") or {}, qa.get("payload") or {})


def _both_ok(row: dict, schema: str) -> bool:
    if schema == "spike-c":
        return bool(row.get("both_ok"))
    return bool(row.get("diff", {}).get("both_ok"))


def _set_fields_for(row: dict, schema: str) -> tuple[str, ...]:
    if schema == "spike-c":
        return SPIKE_C_SET_FIELDS
    tool = row.get("tool")
    return SPIKE_D_SET_FIELDS.get(tool or "", ())


def _prose_fields_for(row: dict, schema: str) -> tuple[str, ...]:
    if schema == "spike-c":
        return SPIKE_C_PROSE_FIELDS
    tool = row.get("tool")
    return SPIKE_D_PROSE_FIELDS.get(tool or "", ())


# ── Item canonicalisation ────────────────────────────────────────────────────


def _canon(item: Any) -> str:
    """Canonicalise a set-item for hashing + prompting. Dict → sorted-key
    JSON; str passthrough; other → repr. Mirrors spike-D `_to_hashable`."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return json.dumps(item, sort_keys=True, ensure_ascii=False)
    return repr(item)


# ── Judge prompts + dispatch ─────────────────────────────────────────────────


_JUDGE_PROMPT = """\
You are judging whether two short item descriptions, extracted by two
different systems for the same task, refer to the SAME underlying
entity / make the same claim (same dataset, baseline, file path,
constraint, action, finding, etc).

Return JSON {{"equivalent": <bool>, "reason": "<one short sentence>"}}.

"equivalent" should be true when:
- The two strings name the same dataset / model / file / claim, even
  if one is more verbose, includes citations, or paraphrases the other.
  Examples:
    "UCI Mushroom database" ↔ "Mushroom database" → equivalent
    "Default posture is claude (cautious) — call-site names are not added to QWEN_OPERATORS_DEFAULT"
      ↔ "Call-site names are not in QWEN_OPERATORS_DEFAULT, so auto-mode routes unknown call sites to claude"
      → equivalent

"equivalent" should be false when:
- The strings name genuinely different entities or claims.
- One is a proper-named entity and the other is an aggregation phrase
  ("MNIST" vs "various image datasets" → not equivalent).

Item A (from system 1): {a}
Item B (from system 2): {b}
"""


_PROSE_JUDGE_PROMPT = """\
You are judging whether two free-form prose passages, written for the
same prompt by two different systems, convey semantically equivalent
content (same key facts, recommendations, file paths, constraints,
even if worded differently).

Return JSON {{"equivalent": <bool>, "reason": "<one short sentence>"}}.

Passage A:
{a}

Passage B:
{b}
"""


_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "equivalent": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["equivalent", "reason"],
}


async def _judge(backend: str, a: str, b: str, *, prose: bool = False) -> dict:
    template = _PROSE_JUDGE_PROMPT if prose else _JUDGE_PROMPT
    prompt = template.format(a=a, b=b)
    if backend == "claude":
        return await claude_dispatch(
            prompt, _JUDGE_SCHEMA, timeout=90.0 if prose else 60.0,
            operator_name="parity_diff_judge",
        )
    return await qwen_dispatch(
        prompt, _JUDGE_SCHEMA, timeout=90.0 if prose else 60.0,
        operator_name="parity_diff_judge",
    )


# ── Match logic ──────────────────────────────────────────────────────────────


async def _match_pairs(
    backend: str,
    only_c: list[str],
    only_q: list[str],
) -> tuple[set[tuple[str, str]], list[dict]]:
    """Greedy bipartite match: for each only-C item, find first
    semantically-equivalent only-Q item. Items must already be
    canonicalised strings."""
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


# ── Prose judging ────────────────────────────────────────────────────────────


def _prose_diverges(a: Any, b: Any, tol: float = PROSE_LEN_TOL) -> bool:
    """Mirror spike-D's `_prose_agree` length-ratio check, inverted.
    Returns True when the pair *should* go to the judge."""
    if a is None and b is None:
        return False
    if not a and not b:
        return False
    if not a or not b:
        return True
    la, lb = len(a), len(b)
    if max(la, lb) == 0:
        return False
    return (min(la, lb) / max(la, lb)) < tol


async def _judge_prose_field(
    backend: str, a: str, b: str,
) -> dict:
    a_trunc = a[:PROSE_MAX_CHARS]
    b_trunc = b[:PROSE_MAX_CHARS]
    try:
        verdict = await _judge(backend, a_trunc, b_trunc, prose=True)
        return {
            "equivalent": bool(verdict.get("equivalent")),
            "reason": str(verdict.get("reason", "")),
            "truncated_a": len(a) > PROSE_MAX_CHARS,
            "truncated_b": len(b) > PROSE_MAX_CHARS,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ── Rescore one row ──────────────────────────────────────────────────────────


async def _rescore_row(
    backend: str,
    row: dict,
    schema: str,
    *,
    prose: bool,
) -> dict:
    """Add a ``semantic_judged`` block to the row. Mutates + returns it."""
    if not _both_ok(row, schema):
        return row

    c_payload, q_payload = _payloads_for(row, schema)
    set_fields = _set_fields_for(row, schema)
    prose_fields = _prose_fields_for(row, schema) if prose else ()

    judged: dict[str, Any] = {}

    for f in set_fields:
        c_raw = c_payload.get(f) or []
        q_raw = q_payload.get(f) or []
        cs = {_canon(x) for x in c_raw}
        qs = {_canon(x) for x in q_raw}
        strict_inter = cs & qs
        only_c = sorted(cs - qs)
        only_q = sorted(qs - cs)
        matched, trace = await _match_pairs(backend, only_c, only_q)
        equiv = len(strict_inter) + len(matched)
        union_size = len(cs | qs) - len(matched)  # collapse matched pairs
        agreement = equiv / union_size if union_size > 0 else 1.0
        judged[f] = {
            "kind": "set",
            "strict_intersection": sorted(strict_inter),
            "semantic_matches": [list(p) for p in sorted(matched)],
            "unmatched_only_c": sorted(
                [a for a in only_c if not any(a == p[0] for p in matched)]
            ),
            "unmatched_only_q": sorted(
                [b for b in only_q if b not in {p[1] for p in matched}]
            ),
            "agreement": agreement,
            "trace": trace,
        }

    for f in prose_fields:
        a = c_payload.get(f) or ""
        b = q_payload.get(f) or ""
        if not _prose_diverges(a, b):
            judged[f] = {"kind": "prose", "skipped": True,
                         "reason": "length-ratio within tolerance"}
            continue
        verdict = await _judge_prose_field(backend, a, b)
        judged[f] = {"kind": "prose", **verdict}

    row["semantic_judged"] = judged
    return row


# ── Main ─────────────────────────────────────────────────────────────────────


async def main_async(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Semantic-equivalence judge for spike-C / spike-D parity output.",
    )
    p.add_argument("--in", dest="in_path", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--judge", choices=("claude", "qwen"), default="qwen")
    p.add_argument("--schema", choices=("spike-c", "spike-d", "auto"),
                   default="auto")
    p.add_argument("--prose", action="store_true",
                   help="Also judge diverging prose fields (slower).")
    args = p.parse_args(argv)

    rows = [json.loads(l) for l in args.in_path.read_text().splitlines() if l.strip()]
    if not rows:
        print("judge: no rows in input", file=sys.stderr)
        return 1

    if args.schema == "auto":
        schema = detect_schema(rows[0])
    else:
        schema = args.schema

    print(
        f"judge: {len(rows)} rows, backend={args.judge}, schema={schema}, "
        f"prose={args.prose}",
        file=sys.stderr,
    )

    out_rows: list[dict] = []
    for i, row in enumerate(rows, 1):
        if schema == "spike-c":
            name = (row.get("uri") or "?").split("/")[-1][:50]
        else:
            name = f"{row.get('tool', '?')}/{row.get('name', '?')}"
        print(f"  [{i}/{len(rows)}] {name}", file=sys.stderr)
        out_row = await _rescore_row(args.judge, row, schema, prose=args.prose)
        out_rows.append(out_row)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fp:
        for r in out_rows:
            fp.write(json.dumps(r) + "\n")

    # Summary — group by tool for spike-D, flat for spike-C.
    print("\n=== Semantic-equivalence summary ===", file=sys.stderr)
    if schema == "spike-c":
        _print_field_summary(out_rows, SPIKE_C_SET_FIELDS, ())
    else:
        by_tool: dict[str, list[dict]] = {}
        for r in out_rows:
            by_tool.setdefault(r.get("tool", "?"), []).append(r)
        for tool, rs in sorted(by_tool.items()):
            print(f"  [{tool}] n={len(rs)}", file=sys.stderr)
            _print_field_summary(
                rs,
                SPIKE_D_SET_FIELDS.get(tool, ()),
                SPIKE_D_PROSE_FIELDS.get(tool, ()) if args.prose else (),
                indent="    ",
            )

    return 0


def _print_field_summary(
    rows: list[dict],
    set_fields: tuple[str, ...],
    prose_fields: tuple[str, ...],
    *,
    indent: str = "  ",
) -> None:
    for f in set_fields:
        rates = []
        for r in rows:
            if "semantic_judged" in r and f in r["semantic_judged"]:
                rates.append(r["semantic_judged"][f].get("agreement", 0.0))
        if rates:
            avg = sum(rates) / len(rates)
            print(
                f"{indent}{f}: {avg*100:.1f}% mean semantic agreement "
                f"(n={len(rates)})",
                file=sys.stderr,
            )
    for f in prose_fields:
        eq = 0
        n = 0
        for r in rows:
            block = r.get("semantic_judged", {}).get(f)
            if not isinstance(block, dict):
                continue
            if block.get("skipped"):
                continue
            if "equivalent" in block:
                n += 1
                if block["equivalent"]:
                    eq += 1
        if n:
            print(
                f"{indent}{f} (prose): {eq}/{n} equivalent",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
