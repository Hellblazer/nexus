#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-088 Spike B — plan_match LLM rerank precision/recall measurement.

Bead ``nexus-ac40.8``. Runs the 20 ambiguous-band queries from
``spike_b_queries.py`` against the live 50-row plan library in TWO
configurations and reports per-config precision + recall plus deltas
against the pre-agreed dual threshold:

  * precision delta >= 0.05 absolute AND
  * recall delta > -0.15 absolute

Missing either threshold closes Gap 4 as "already addressed by
RDR-092" per the RDR's Phase 3 framing.

Configs:
  * A (rerank-off): standard plan_match with min_confidence=0.50.
  * B (rerank-on):  plan_match + spike_b_rerank_prototype applied to the
                     (0.50, 0.65] band.

Records per-query classification (TP / FP / FN / TN) for both configs
plus a confusion matrix summary. Raw results in
``scripts/spikes/spike_b_results.json``; human summary printed to
stdout.

Usage::

    uv run python scripts/spikes/spike_b_run.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

from spike_b_queries import QUERIES  # noqa: E402
from spike_b_rerank_prototype import apply_rerank  # noqa: E402

from nexus.mcp_infra import get_t1_plan_cache, t2_ctx  # noqa: E402
from nexus.plans.matcher import plan_match  # noqa: E402

RESULTS_PATH = SCRIPT_DIR / "spike_b_results.json"
VERB_MIN_CONFIDENCE: float = 0.50  # verb-skill-typical, per RDR-092 P2


def _classify(expected_id: int | None, predicted_id: int | None) -> str:
    """Return TP / FP / FN / TN for the (expected, predicted) pair."""
    if expected_id is not None and predicted_id == expected_id:
        return "TP"
    if expected_id is not None and predicted_id is None:
        return "FN"
    if expected_id is None and predicted_id is None:
        return "TN"
    # expected_id is None OR predicted != expected, and predicted is not None
    return "FP"


def _metrics(classifications: list[str]) -> dict[str, float | int]:
    """Compute precision + recall from a list of TP/FP/FN/TN strings."""
    counts = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    for c in classifications:
        counts[c] = counts.get(c, 0) + 1
    tp, fp, fn, tn = counts["TP"], counts["FP"], counts["FN"], counts["TN"]
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / (tp + fp + fn + tn) if classifications else 0.0
    return {
        "counts": counts,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "accuracy": round(accuracy, 4),
        "total": len(classifications),
    }


async def run_config(
    config_label: str,
    *,
    apply_llm_rerank: bool,
) -> dict:
    """Run all queries under one config; return per-query + aggregate."""
    results: list[dict] = []

    with t2_ctx() as db:
        cache = get_t1_plan_cache(populate_from=db.plans)
        for q in QUERIES:
            t0 = time.monotonic()
            matches = plan_match(
                q["intent"],
                library=db.plans,
                cache=cache,
                min_confidence=VERB_MIN_CONFIDENCE,
                n=5,
            )
            # Under config B, apply the rerank wrapper.
            rerank_applied = False
            if apply_llm_rerank and matches:
                top = matches[0]
                if (
                    top.confidence is not None
                    and VERB_MIN_CONFIDENCE < float(top.confidence) <= 0.65
                ):
                    matches = await apply_rerank(
                        q["intent"],
                        matches,
                        library=db.plans,
                        effective_floor=VERB_MIN_CONFIDENCE,
                    )
                    rerank_applied = True
            elapsed = time.monotonic() - t0

            top = matches[0] if matches else None
            predicted_id = top.plan_id if top else None
            predicted_name = top.name if top else None
            top_conf = (
                float(top.confidence)
                if top and top.confidence is not None
                else None
            )
            classification = _classify(q["expected_plan_id"], predicted_id)
            results.append({
                "id": q["id"],
                "intent": q["intent"],
                "category": q["category"],
                "expected_plan_id": q["expected_plan_id"],
                "expected_plan_name": q.get("expected_plan_name"),
                "predicted_plan_id": predicted_id,
                "predicted_plan_name": predicted_name,
                "top_confidence": top_conf,
                "rerank_applied": rerank_applied,
                "classification": classification,
                "elapsed_s": round(elapsed, 2),
            })
            print(
                f"[{config_label}] {q['id']} -> {classification} "
                f"pred={predicted_name} conf={top_conf} "
                f"rerank={rerank_applied} {elapsed:.1f}s",
                flush=True,
            )

    classifications = [r["classification"] for r in results]
    return {
        "config": config_label,
        "apply_llm_rerank": apply_llm_rerank,
        "per_query": results,
        "metrics": _metrics(classifications),
    }


async def main() -> int:
    t_start = time.monotonic()
    print(f"=== SPIKE B: {len(QUERIES)} queries x 2 configs ===")

    print("\n--- Config A (rerank-off, baseline) ---")
    config_a = await run_config("A", apply_llm_rerank=False)

    print("\n--- Config B (rerank-on, prototype) ---")
    config_b = await run_config("B", apply_llm_rerank=True)

    precision_delta = (
        config_b["metrics"]["precision"] - config_a["metrics"]["precision"]
    )
    recall_delta = (
        config_b["metrics"]["recall"] - config_a["metrics"]["recall"]
    )
    precision_threshold_met = precision_delta >= 0.05
    recall_threshold_met = recall_delta > -0.15
    both_thresholds_met = precision_threshold_met and recall_threshold_met

    summary = {
        "date": "2026-04-24",
        "library_size": 50,
        "verb_min_confidence": VERB_MIN_CONFIDENCE,
        "query_count": len(QUERIES),
        "config_a": config_a,
        "config_b": config_b,
        "precision_delta": round(precision_delta, 4),
        "recall_delta": round(recall_delta, 4),
        "thresholds": {
            "precision_delta_met": precision_threshold_met,
            "recall_delta_met": recall_threshold_met,
            "both_met": both_thresholds_met,
        },
        "recommendation": (
            "LAND Phase 3 rerank"
            if both_thresholds_met
            else "CLOSE Gap 4 as addressed by RDR-092"
        ),
        "elapsed_total_s": round(time.monotonic() - t_start, 2),
    }

    RESULTS_PATH.write_text(json.dumps(summary, indent=2))

    print("\n=== RESULTS ===")
    print(f"Config A metrics: {config_a['metrics']}")
    print(f"Config B metrics: {config_b['metrics']}")
    print(f"Precision delta: {summary['precision_delta']}")
    print(f"Recall delta:    {summary['recall_delta']}")
    print(f"Both thresholds met: {both_thresholds_met}")
    print(f"Recommendation: {summary['recommendation']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
