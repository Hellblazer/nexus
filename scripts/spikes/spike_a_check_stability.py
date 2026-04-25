#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-088 Spike A — operator_check verdict-stability probe.

Bead ``nexus-ac40.7``. Measures how often the boolean ``ok`` verdict
agrees across N independent ``claude -p`` invocations for the same
``(items, check_instruction)`` input. The output feeds into the
Phase 2 review artefact ``088-spike-a-check-stability`` in nx memory.

Protocol:
  * Load 20 consistency-question fixtures from
    ``scripts/spikes/spike_a_fixtures.py``.
  * Run each fixture ``REPEATS`` times against the real
    ``operator_check`` MCP tool (dispatches to claude -p).
  * Log each run as a JSONL record to ``scripts/spikes/spike_a_results.jsonl``.
  * Aggregate per-fixture stability (% runs agreeing with modal verdict)
    and aggregate stability across fixtures.
  * Persist summary to nx memory + nx T3 store per bead acceptance
    criteria.

Expected runtime: 100 operator_check calls at ~30-60s each = 50-100
minutes. Runs serially to respect ChromaDB Cloud + Voyage concurrency
limits and keep traces interpretable.

Usage::

    uv run python scripts/spikes/spike_a_check_stability.py

Or invoke via a background Bash run in Claude Code for long-running
execution. Results file is append-only; re-running appends without
rotating. Delete ``scripts/spikes/spike_a_results.jsonl`` to start
fresh.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
# Ensure ``scripts/spikes`` is importable as a package-free module dir.
sys.path.insert(0, str(SCRIPT_DIR))

from spike_a_fixtures import FIXTURES  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "src"))
from nexus.mcp.core import operator_check  # noqa: E402

REPEATS: int = 5
RESULTS_PATH = SCRIPT_DIR / "spike_a_results.jsonl"
SUMMARY_PATH = SCRIPT_DIR / "spike_a_summary.json"


async def probe_one(fixture: dict, repeat_ix: int) -> dict:
    """Run one operator_check invocation and capture a compact record."""
    items_json = json.dumps(fixture["items_inline"])
    t0 = time.monotonic()
    result: dict | None
    error: str | None
    try:
        result = await operator_check(
            items=items_json,
            check_instruction=fixture["check_instruction"],
            timeout=300.0,
        )
        error = None
    except Exception as exc:  # noqa: BLE001 - record everything
        result = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - t0

    ok_value: bool | None
    evidence_roles: list[str]
    evidence_count: int
    if isinstance(result, dict):
        ok_raw = result.get("ok")
        ok_value = bool(ok_raw) if isinstance(ok_raw, bool) else None
        evidence = result.get("evidence") or []
        evidence_count = len(evidence) if isinstance(evidence, list) else 0
        evidence_roles = [
            e.get("role") for e in evidence
            if isinstance(e, dict) and isinstance(e.get("role"), str)
        ] if isinstance(evidence, list) else []
    else:
        ok_value = None
        evidence_count = 0
        evidence_roles = []

    return {
        "fixture_id": fixture["id"],
        "topic": fixture.get("topic", ""),
        "repeat_ix": repeat_ix,
        "elapsed_s": round(elapsed, 2),
        "ok": ok_value,
        "evidence_count": evidence_count,
        "evidence_roles": evidence_roles,
        "expected_verdict_hint": fixture.get("expected_verdict_hint"),
        "error": error,
    }


def _aggregate(records: list[dict]) -> dict:
    """Compute per-fixture modal verdict + stability rate + aggregates."""
    by_fid: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_fid[r["fixture_id"]].append(r)

    per_fixture: list[dict] = []
    stable_count = 0
    for fid, recs in by_fid.items():
        verdicts = [r["ok"] for r in recs if r["ok"] is not None]
        if not verdicts:
            per_fixture.append({
                "fixture_id": fid,
                "topic": recs[0]["topic"],
                "runs_with_ok_verdict": 0,
                "runs_total": len(recs),
                "modal_verdict": None,
                "stability": None,
                "note": "all runs errored",
            })
            continue
        counts = Counter(verdicts)
        modal, agreeing = counts.most_common(1)[0]
        stability = agreeing / len(verdicts)
        if agreeing == len(verdicts):
            stable_count += 1
        per_fixture.append({
            "fixture_id": fid,
            "topic": recs[0]["topic"],
            "modal_verdict": modal,
            "agreeing": agreeing,
            "runs_with_ok_verdict": len(verdicts),
            "runs_total": len(recs),
            "stability": round(stability, 3),
            "expected_verdict_hint": recs[0].get("expected_verdict_hint"),
        })

    total_runs = sum(f["runs_total"] for f in per_fixture)
    runs_with_verdict = sum(f["runs_with_ok_verdict"] for f in per_fixture)
    all_stable_rate = (
        stable_count / len(per_fixture) if per_fixture else None
    )
    micro_stability = (
        sum(f.get("agreeing", 0) for f in per_fixture) / runs_with_verdict
        if runs_with_verdict
        else None
    )

    return {
        "per_fixture": per_fixture,
        "aggregate": {
            "fixtures_total": len(per_fixture),
            "fixtures_fully_stable": stable_count,
            "fully_stable_rate": (
                round(all_stable_rate, 3) if all_stable_rate is not None
                else None
            ),
            "micro_stability_rate": (
                round(micro_stability, 3) if micro_stability is not None
                else None
            ),
            "total_runs": total_runs,
            "runs_with_ok_verdict": runs_with_verdict,
            "error_rate": round(
                (total_runs - runs_with_verdict) / total_runs, 3,
            ) if total_runs else None,
        },
    }


async def main() -> int:
    records: list[dict] = []
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Append mode; delete file to restart from scratch.
    with RESULTS_PATH.open("a") as out:
        total = len(FIXTURES) * REPEATS
        idx = 0
        t_start = time.monotonic()
        for fixture in FIXTURES:
            for r_ix in range(REPEATS):
                idx += 1
                rec = await probe_one(fixture, r_ix)
                out.write(json.dumps(rec) + "\n")
                out.flush()
                records.append(rec)
                elapsed_total = time.monotonic() - t_start
                rate = idx / elapsed_total if elapsed_total else 0
                eta_s = (total - idx) / rate if rate > 0 else 0
                print(
                    f"[{idx}/{total}] {rec['fixture_id']} r{r_ix} "
                    f"ok={rec['ok']} {rec['elapsed_s']:.1f}s "
                    f"ETA {eta_s/60:.1f}m",
                    flush=True,
                )

    summary = _aggregate(records)
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary["aggregate"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
