# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Operator-bundle sandbox probe — nexus-nxa-perf.

Exercises ``dispatch_bundle`` against a real ``claude -p`` subprocess and
compares it to the baseline (two isolated ``operator_*`` dispatches).
Emits end-to-end latency + output quality metrics so we can decide
whether wiring into ``plan_run`` is safe.

Requirements:
  * ``claude`` CLI on PATH with valid auth (``claude auth status --json``
    must report ``loggedIn: true``).
  * Network + real API spend — each probe issues ≥ 2 real ``claude -p``
    calls. Typical cost: $0.05-$0.20 per run.

Usage:
    uv run python scripts/bundle_sandbox_probe.py
    uv run python scripts/bundle_sandbox_probe.py --json out.json

Reads a small synthetic input fixture (no real corpus documents are
extracted — the probe's input is intentionally tiny so cost and variance
stay low).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from nexus.plans.bundle import (
    OperatorBundle,
    OperatorBundleStep,
    compose_bundle_prompt,
    dispatch_bundle,
)


# Tiny synthetic corpus — three two-sentence "papers" with authors +
# dates. Real enough for the pipeline to have something to do, small
# enough to keep cost under $0.10 per probe.
SYNTHETIC_INPUTS = [
    {
        "id": "paper-1",
        "text": (
            "Kazerounian & Grossberg (2014). Frontiers in Psychology 5:595. "
            "Introduces cARTWORD, a neural model of sequential word "
            "recognition with laminar adaptive resonance."
        ),
    },
    {
        "id": "paper-2",
        "text": (
            "Grossberg (2021). Frontiers in Systems Neuroscience 15:655. "
            "Defines the six-layer cortical circuit underlying perceptual "
            "resonance and surface-boundary binding."
        ),
    },
    {
        "id": "paper-3",
        "text": (
            "Bradski, Carpenter & Grossberg (1994). Neural Networks 7(6):"
            "1025-1051. Derives STORE 2, a working-memory network for "
            "arbitrary temporal sequences."
        ),
    },
]


def _auth_check() -> bool:
    """Confirm claude CLI is present and authenticated before spending money."""
    if shutil.which("claude") is None:
        print("ERROR: `claude` CLI not on PATH", file=sys.stderr)
        return False
    try:
        r = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        status = json.loads(r.stdout or "{}")
    except Exception as exc:
        print(f"ERROR: auth probe failed: {exc}", file=sys.stderr)
        return False
    ok = bool(status.get("loggedIn"))
    if not ok:
        print(f"ERROR: claude not logged in: {status}", file=sys.stderr)
    return ok


async def _baseline_isolated() -> tuple[float, dict[str, Any]]:
    """Two isolated ``operator_*`` dispatches (extract → summarize)."""
    from nexus.mcp.core import operator_extract, operator_summarize

    start = time.monotonic()

    extract_result = await operator_extract(
        inputs=json.dumps([p["text"] for p in SYNTHETIC_INPUTS]),
        fields="citation,key_contribution",
        timeout=120.0,
    )
    # Thread step1 output into step2 manually, mimicking what plan_run
    # does between isolated steps.
    extractions = extract_result.get("extractions") or []
    intermediate = json.dumps(extractions, indent=2)

    summarize_result = await operator_summarize(
        content=intermediate,
        cited=True,
        timeout=120.0,
    )

    elapsed = time.monotonic() - start
    return elapsed, {
        "extract": extract_result,
        "summarize": summarize_result,
    }


async def _bundled() -> tuple[float, dict[str, Any]]:
    """Single bundled dispatch (extract → summarize as one claude -p call)."""
    bundle = OperatorBundle(steps=(
        OperatorBundleStep(
            plan_index=0,
            tool="extract",
            args={
                "fields": "citation,key_contribution",
                "inputs": json.dumps([p["text"] for p in SYNTHETIC_INPUTS]),
            },
        ),
        OperatorBundleStep(
            plan_index=1,
            tool="summarize",
            args={"cited": True},
        ),
    ))

    start = time.monotonic()
    result = await dispatch_bundle(bundle, timeout=180.0)
    elapsed = time.monotonic() - start
    return elapsed, result


def _assess_quality(isolated: dict, bundled: dict) -> dict[str, Any]:
    """Rudimentary shape checks — did each path produce the expected keys?"""
    iso_summary = (isolated.get("summarize") or {}).get("summary", "")
    bun_summary = bundled.get("summary", "")
    iso_citations = (isolated.get("summarize") or {}).get("citations") or []
    bun_citations = bundled.get("citations") or []
    return {
        "isolated_summary_len": len(iso_summary),
        "bundled_summary_len": len(bun_summary),
        "isolated_citation_count": len(iso_citations),
        "bundled_citation_count": len(bun_citations),
        "both_produced_summary": bool(iso_summary) and bool(bun_summary),
        "both_produced_citations": bool(iso_citations) and bool(bun_citations),
    }


def _stats(samples: list[float]) -> dict[str, float]:
    """Mean, stddev (sample), min, max. Returns zeros for empty input."""
    if not samples:
        return {"n": 0, "mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    mean = statistics.mean(samples)
    sd = statistics.stdev(samples) if len(samples) >= 2 else 0.0
    return {
        "n": len(samples),
        "mean": mean,
        "stddev": sd,
        "min": min(samples),
        "max": max(samples),
    }


async def main_async(args) -> dict[str, Any]:
    # Print the prompts we'd send so a reviewer can eyeball them before
    # we burn money. Skipped when --dry-run was explicit; otherwise
    # proceed after showing them.
    bundle = OperatorBundle(steps=(
        OperatorBundleStep(
            plan_index=0, tool="extract",
            args={"fields": "citation,key_contribution",
                  "inputs": "[three synthetic papers]"},
        ),
        OperatorBundleStep(plan_index=1, tool="summarize", args={"cited": True}),
    ))
    preview_prompt, preview_schema = compose_bundle_prompt(bundle)
    print("=" * 70)
    print("BUNDLE PROMPT PREVIEW (substituted with placeholder inputs):")
    print("=" * 70)
    print(preview_prompt[:800])
    if len(preview_prompt) > 800:
        print(f"... and {len(preview_prompt) - 800} more characters")
    print()
    print("TERMINAL SCHEMA:")
    print(json.dumps(preview_schema, indent=2))
    print()

    if args.dry_run:
        return {"dry_run": True}

    if not _auth_check():
        return {"error": "claude auth not available"}

    iso_samples: list[float] = []
    bun_samples: list[float] = []
    last_iso_result: dict[str, Any] = {}
    last_bun_result: dict[str, Any] = {}

    for run_idx in range(1, args.runs + 1):
        print("=" * 70)
        print(f"RUN {run_idx} of {args.runs}")
        print("=" * 70)

        print(f"  Running BASELINE (two isolated dispatches)...")
        iso_elapsed, iso_result = await _baseline_isolated()
        iso_samples.append(iso_elapsed)
        last_iso_result = iso_result
        print(f"    baseline elapsed: {iso_elapsed:.2f}s")

        print(f"  Running BUNDLED (single dispatch)...")
        bun_elapsed, bun_result = await _bundled()
        bun_samples.append(bun_elapsed)
        last_bun_result = bun_result
        print(f"    bundled elapsed:  {bun_elapsed:.2f}s")

        saved = iso_elapsed - bun_elapsed
        print(f"    saved:            {saved:+.2f}s")
        print()

    iso_stats = _stats(iso_samples)
    bun_stats = _stats(bun_samples)
    saved_mean = iso_stats["mean"] - bun_stats["mean"]
    saved_pct = 100 * saved_mean / max(iso_stats["mean"], 0.001)

    print("=" * 70)
    print(f"AGGREGATE (N={args.runs})")
    print("=" * 70)
    print(f"  baseline:  mean={iso_stats['mean']:6.2f}s  "
          f"stddev={iso_stats['stddev']:5.2f}s  "
          f"range=[{iso_stats['min']:.1f}, {iso_stats['max']:.1f}]")
    print(f"  bundled:   mean={bun_stats['mean']:6.2f}s  "
          f"stddev={bun_stats['stddev']:5.2f}s  "
          f"range=[{bun_stats['min']:.1f}, {bun_stats['max']:.1f}]")
    print(f"  saved:     {saved_mean:+6.2f}s  ({saved_pct:+.1f}%)")
    print()
    print("Isolated summary excerpt (last run):")
    iso_s = (last_iso_result.get("summarize") or {}).get("summary", "")
    print(f"  {iso_s[:200]}{'...' if len(iso_s) > 200 else ''}")
    print()
    print("Bundled summary excerpt (last run):")
    bun_s = last_bun_result.get("summary", "")
    print(f"  {bun_s[:200]}{'...' if len(bun_s) > 200 else ''}")

    quality = _assess_quality(last_iso_result, last_bun_result)
    print()
    print("Shape check (last run):")
    for k, v in quality.items():
        print(f"  {k}: {v}")

    return {
        "runs": args.runs,
        "baseline_samples_s": iso_samples,
        "bundled_samples_s": bun_samples,
        "baseline_stats": iso_stats,
        "bundled_stats": bun_stats,
        "saved_mean_s": saved_mean,
        "saved_mean_pct": saved_pct,
        "last_isolated_result": last_iso_result,
        "last_bundled_result": last_bun_result,
        "quality_last_run": quality,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=None,
                    help="Write the full report as JSON to this path")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the prompts that would be sent, then exit")
    ap.add_argument("--runs", type=int, default=3,
                    help="Number of times to run each configuration "
                         "(default: 3). Higher N → tighter stddev, "
                         "more API spend (~$0.10 per run).")
    args = ap.parse_args()

    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2

    report = asyncio.run(main_async(args))

    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nJSON written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
