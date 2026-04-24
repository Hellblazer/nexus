#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-093 Spike B — operator_aggregate cross-group content-leakage probe.

Bead ``nexus-rojs``. Verifies that operator_aggregate's per-group
reduction does not leak content from one group into another's summary
when item vocabulary overlaps significantly across groups. Operator
itself does not exist yet in core.py; this spike validates the prompt
shape proposed in RDR-093 §Technical Design before Phase 2 impl.

Protocol:
  * One adversarial fixture: 3 groups x 3 items (=9 items). All groups
    share the same evaluation vocabulary (YCSB workload, transactions
    per second, baseline) but each group's items mention a UNIQUE,
    distinguishing baseline-method name. The reducer asks for the most-
    discussed baseline; a leak is any sibling-group method name in a
    group's summary.
  * 10 runs of operator_aggregate prompt via claude_dispatch. Each run
    produces 3 aggregates (one per group).
  * For each run, scan each aggregate's summary for sibling-group
    method names. Leak count is counted both per-group and per-run.
  * Report leakage_rate = runs_with_zero_leak / 10. Target ≥ 90%
    (the bead acceptance criterion is "if leakage > 10%, document
    prompt-framing mitigation before Phase 2 impl").
  * Persist summary JSON to ``scripts/spikes/spike_b_aggregate_summary.json``.

Expected runtime: 10 calls at ~30-45s = 5-10 minutes. Runs serially.

Usage::

    uv run python scripts/spikes/spike_b_aggregate_leakage.py

Append-only results file. Delete it to start fresh.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from nexus.operators.dispatch import claude_dispatch  # noqa: E402

REPEATS: int = 10
RESULTS_PATH = SCRIPT_DIR / "spike_b_aggregate_results.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_b_aggregate_summary.json"

# Three sibling baseline method names; the LLM must keep them
# isolated to the corresponding group. Names are distinctive (no
# overlap with each other or with mainstream method names) so a
# substring match is unambiguous.
DISTINGUISHING_METHODS: dict[str, str] = {
    "GroupAlpha": "PaxosClassic",
    "GroupBravo": "ZyzzyvaBFT",
    "GroupCharlie": "HotStuffMod",
}

# Adversarial fixture: vocabulary overlaps heavily (every item
# mentions YCSB-A, transactions per second, three replicas, the word
# "baseline") so the LLM is tempted to merge cross-group content. The
# distinguishing signal is exactly one method name per group.
FIXTURE: dict = {
    "groups": [
        {
            "key_value": "GroupAlpha",
            "items": [
                {"id": "A1", "quote": "We compare our system against PaxosClassic on the YCSB-A workload at three replicas; PaxosClassic reaches 12k transactions per second."},
                {"id": "A2", "quote": "Following Lamport-style consensus, our PaxosClassic baseline reports 12k transactions per second on YCSB-A."},
                {"id": "A3", "quote": "PaxosClassic remains the dominant baseline for crash-fault-tolerant SMR; we use YCSB-A and three replicas."},
            ],
        },
        {
            "key_value": "GroupBravo",
            "items": [
                {"id": "B1", "quote": "We compare our system against ZyzzyvaBFT on the YCSB-A workload at three replicas; ZyzzyvaBFT reaches 8k transactions per second."},
                {"id": "B2", "quote": "ZyzzyvaBFT achieves 8k transactions per second on YCSB-A under crash-only conditions with three replicas."},
                {"id": "B3", "quote": "Our ZyzzyvaBFT baseline runs YCSB-A across three replicas and is the protocol we measure ourselves against."},
            ],
        },
        {
            "key_value": "GroupCharlie",
            "items": [
                {"id": "C1", "quote": "We compare our system against HotStuffMod on the YCSB-A workload at three replicas; HotStuffMod reaches 15k transactions per second."},
                {"id": "C2", "quote": "HotStuffMod is our baseline for chained BFT consensus, evaluated on YCSB-A with three replicas."},
                {"id": "C3", "quote": "Building on HotStuffMod we use YCSB-A and observe 15k transactions per second across three replicas."},
            ],
        },
    ],
    "reducer": "the most-cited baseline method",
}

# RDR-093 §Technical Design output schema (verbatim).
AGGREGATE_SCHEMA: dict = {
    "type": "object",
    "required": ["aggregates"],
    "properties": {
        "aggregates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["key_value", "summary"],
                "properties": {
                    "key_value": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        },
    },
}


def _build_prompt(groups: list[dict], reducer: str) -> str:
    """Construct the operator_aggregate prompt per RDR-093 §Technical Design."""
    groups_json = json.dumps(groups)
    return (
        f"Reduce each group of items into a per-group summary using "
        f"this reducer instruction: {reducer}\n\n"
        f"Output one aggregate per input group, preserving the group's "
        f"``key_value`` verbatim. Each ``summary`` MUST reference only "
        f"the items in that group's ``items`` array. Do not pull "
        f"content from items in other groups, even when vocabulary "
        f"overlaps across groups. The summary is a short paragraph "
        f"answering the reducer instruction USING ONLY this group's "
        f"items.\n\n"
        f"Groups:\n{groups_json}"
    )


def _detect_leaks(aggregates: list[dict]) -> dict:
    """Count cross-group method-name leaks in each aggregate's summary.

    For each aggregate (keyed by group_key), scan its ``summary`` for
    distinguishing method names belonging to OTHER groups. A hit on a
    sibling group's method name is a leak.

    Returns per-group leak counts, the run-level leak total, and a
    ``run_has_leak`` boolean (any leak across any group).
    """
    per_group: dict[str, dict] = {}
    total_leaks = 0
    run_has_leak = False

    # Build {key_value -> summary} once
    by_key: dict[str, str] = {}
    for agg in aggregates:
        if not isinstance(agg, dict):
            continue
        kv = agg.get("key_value")
        summary = agg.get("summary")
        if isinstance(kv, str) and isinstance(summary, str):
            by_key[kv] = summary

    for kv, own_method in DISTINGUISHING_METHODS.items():
        summary = by_key.get(kv, "")
        # Sibling methods are every distinguishing method NOT owned by
        # this group. Word-boundary match is too strict because the
        # method names are PascalCase compounds; substring match on
        # the full distinct token is unambiguous because the names
        # are unique (no shared substrings across the three).
        sibling_hits: list[str] = []
        for other_kv, other_method in DISTINGUISHING_METHODS.items():
            if other_kv == kv:
                continue
            if other_method in summary:
                sibling_hits.append(other_method)
        own_present = own_method in summary
        per_group[kv] = {
            "summary_chars": len(summary),
            "own_method_present": own_present,
            "sibling_method_hits": sibling_hits,
            "leak_count": len(sibling_hits),
        }
        total_leaks += len(sibling_hits)
        if sibling_hits:
            run_has_leak = True

    return {
        "per_group": per_group,
        "total_leaks": total_leaks,
        "run_has_leak": run_has_leak,
        "groups_present": sorted(by_key.keys()),
    }


async def probe_one(repeat_ix: int) -> dict:
    """Run one operator_aggregate invocation and capture a leakage report."""
    prompt = _build_prompt(FIXTURE["groups"], FIXTURE["reducer"])
    t0 = time.monotonic()
    result: dict | None
    error: str | None
    try:
        result = await claude_dispatch(
            prompt=prompt,
            json_schema=AGGREGATE_SCHEMA,
            timeout=300.0,
        )
        error = None
    except Exception as exc:  # noqa: BLE001
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - t0

    aggregates = (
        result.get("aggregates")
        if isinstance(result, dict)
        else None
    )
    if not isinstance(aggregates, list):
        aggregates = []
    leakage = _detect_leaks(aggregates)
    schema_ok = (
        len(aggregates) == len(FIXTURE["groups"])
        and all(
            isinstance(a, dict)
            and isinstance(a.get("key_value"), str)
            and isinstance(a.get("summary"), str)
            for a in aggregates
        )
    )

    return {
        "repeat_ix": repeat_ix,
        "elapsed_s": round(elapsed, 2),
        "schema_ok": schema_ok,
        "n_aggregates": len(aggregates),
        "leakage": leakage,
        "summaries": [
            {"key_value": a.get("key_value"), "summary": a.get("summary")}
            for a in aggregates if isinstance(a, dict)
        ],
        "error": error,
    }


def _aggregate(records: list[dict]) -> dict:
    runs = len(records)
    runs_with_leak = sum(1 for r in records if r["leakage"]["run_has_leak"])
    runs_zero_leak = runs - runs_with_leak
    schema_ok_runs = sum(1 for r in records if r["schema_ok"])

    # Per-group leak counts across runs.
    per_group_runs_with_leak: dict[str, int] = {
        kv: 0 for kv in DISTINGUISHING_METHODS
    }
    per_group_total_hits: dict[str, int] = {
        kv: 0 for kv in DISTINGUISHING_METHODS
    }
    own_method_present_runs: dict[str, int] = {
        kv: 0 for kv in DISTINGUISHING_METHODS
    }
    for r in records:
        for kv, info in r["leakage"]["per_group"].items():
            if info["leak_count"] > 0:
                per_group_runs_with_leak[kv] += 1
            per_group_total_hits[kv] += info["leak_count"]
            if info["own_method_present"]:
                own_method_present_runs[kv] += 1

    return {
        "runs": runs,
        "schema_ok_runs": schema_ok_runs,
        "schema_ok_rate": round(schema_ok_runs / runs, 3) if runs else None,
        "runs_with_leak": runs_with_leak,
        "runs_zero_leak": runs_zero_leak,
        "leak_free_rate": round(runs_zero_leak / runs, 3) if runs else None,
        "leak_rate": round(runs_with_leak / runs, 3) if runs else None,
        "per_group_runs_with_leak": per_group_runs_with_leak,
        "per_group_total_hits": per_group_total_hits,
        "own_method_present_runs": own_method_present_runs,
        "errored_runs": sum(1 for r in records if r.get("error")),
    }


async def main() -> int:
    records: list[dict] = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_PATH.open("a") as out:
        t_start = time.monotonic()
        for r_ix in range(REPEATS):
            rec = await probe_one(r_ix)
            out.write(json.dumps(rec) + "\n")
            out.flush()
            records.append(rec)
            elapsed_total = time.monotonic() - t_start
            rate = (r_ix + 1) / elapsed_total if elapsed_total else 0
            eta_s = (REPEATS - (r_ix + 1)) / rate if rate > 0 else 0
            err_label = "" if not rec.get("error") else f" err={rec['error'][:60]}"
            leak_label = (
                "leak" if rec["leakage"]["run_has_leak"] else "clean"
            )
            print(
                f"[{r_ix+1}/{REPEATS}] schema={rec['schema_ok']} "
                f"{leak_label} hits={rec['leakage']['total_leaks']} "
                f"{rec['elapsed_s']:.1f}s ETA {eta_s/60:.1f}m{err_label}",
                flush=True,
            )

    summary = _aggregate(records)
    SUMMARY_PATH.write_text(json.dumps(
        {"summary": summary, "fixture": FIXTURE,
         "distinguishing_methods": DISTINGUISHING_METHODS},
        indent=2,
    ))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
