#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-089 Phase C benchmark — SQL fast path vs LLM path.

Proves the RDR's stated value (O(1) SQL reads) by comparing
``operator_filter`` (SQL fast path) wall-clock against
``operator_filter`` (LLM dispatch) on identical inputs. Same for
``operator_groupby`` and ``operator_aggregate``.

This benchmark uses an in-process tmp T2 populated with a
synthetic 100-paper corpus so it requires no API keys and no
real cloud state. The LLM path is a mock that simulates
``claude_dispatch`` with a constant 1.5s per-call latency
(approximating the Spike-A measured Claude wall-clock for a
~5KB-prompt operator dispatch). The SQL path runs against the
real ``aspect_sql.try_*`` functions with a real SQLite query.

Output:
  scripts/spikes/bench_rdr089_results.json   — wall-clock numbers
  scripts/spikes/bench_rdr089_run.log        — per-trial log

The benchmark is deterministic — fixed corpus, fixed mock
latency, no network. Run as part of the release-sandbox
shakedown if you want signal that the SQL fast path has not
regressed; not in the standard pytest cycle (it would slow it
without adding correctness coverage).

Usage:
  uv run python scripts/spikes/bench_rdr089_sql_fast_path.py
  uv run python scripts/spikes/bench_rdr089_sql_fast_path.py --papers 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nexus.aspect_extractor import AspectRecord  # noqa: E402
from nexus.db.t2 import T2Database  # noqa: E402
from nexus.operators import aspect_sql  # noqa: E402


# Simulated LLM latency — chosen to approximate the Claude CLI's
# wall-clock for a moderate-size operator prompt. Real measurements
# vary 1-3s; 1.5s is the median of that range.
_LLM_SIMULATED_LATENCY_S = 1.5


def _populate_corpus(db_path: Path, n_papers: int) -> None:
    venues = ["VLDB", "OSDI", "SIGMOD", "NSDI", "SOSP"]
    methods = [
        "Hybrid Paxos with batched leader appends",
        "Raft variant with single-leader writes",
        "BFT consensus with HotStuff backbone",
        "MultiPaxos with leader leases",
        "Tendermint variant with rotating proposers",
    ]
    datasets_pool = [
        ["TPC-C", "YCSB"],
        ["YCSB"],
        ["TPC-C"],
        ["TATP", "YCSB"],
        ["SmallBank"],
    ]
    baselines_pool = [
        ["raft", "paxos"],
        ["pbft"],
        ["raft"],
        ["paxos"],
        ["tendermint"],
    ]
    with T2Database(db_path) as db:
        for i in range(n_papers):
            db.document_aspects.upsert(AspectRecord(
                collection="bench__corpus",
                source_path=f"/papers/paper-{i:04d}.pdf",
                problem_formulation=f"Problem statement {i}",
                proposed_method=methods[i % len(methods)],
                experimental_datasets=datasets_pool[i % len(datasets_pool)],
                experimental_baselines=baselines_pool[i % len(baselines_pool)],
                experimental_results=f"Result {i}: 30% improvement",
                extras={
                    "venue": venues[i % len(venues)],
                    "year": 2020 + (i % 5),
                },
                confidence=0.5 + ((i % 5) * 0.1),
                extracted_at=datetime.now(UTC).isoformat(),
                model_version="claude-haiku-4-5-20251001",
                extractor_name="scholarly-paper-v1",
            ))


def _items_payload(n_papers: int) -> str:
    return json.dumps([
        {
            "id": f"/papers/paper-{i:04d}.pdf",
            "collection": "bench__corpus",
            "source_path": f"/papers/paper-{i:04d}.pdf",
        }
        for i in range(n_papers)
    ])


async def _mock_claude_dispatch(prompt: str, schema: dict, timeout: float):
    """Simulate a real LLM dispatch with constant latency."""
    await asyncio.sleep(_LLM_SIMULATED_LATENCY_S)
    return {"items": [], "rationale": [], "groups": [], "aggregates": []}


def _time_call(fn, *args, **kwargs) -> float:
    start = time.monotonic()
    fn(*args, **kwargs)
    return (time.monotonic() - start) * 1000.0  # ms


async def _time_async_call(coro_fn, *args, **kwargs) -> float:
    start = time.monotonic()
    await coro_fn(*args, **kwargs)
    return (time.monotonic() - start) * 1000.0  # ms


async def _run(n_papers: int, trials: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "bench.db"
        # Patch default_db_path so try_filter/groupby/aggregate hit the
        # benchmark's tmp DB.
        import nexus.commands._helpers as h
        original = h.default_db_path
        h.default_db_path = lambda: db_path
        try:
            _populate_corpus(db_path, n_papers)
            items = _items_payload(n_papers)

            # ── operator_filter ────────────────────────────────
            filter_sql_ms = []
            for _ in range(trials):
                ms = _time_call(
                    aspect_sql.try_filter,
                    items, "paxos",
                    source="aspects", aspect_field="proposed_method",
                )
                filter_sql_ms.append(ms)

            # LLM path: mock dispatch.
            from nexus.mcp.core import operator_filter
            filter_llm_ms = []
            with patch(
                "nexus.operators.dispatch.claude_dispatch",
                _mock_claude_dispatch,
            ):
                for _ in range(trials):
                    ms = await _time_async_call(
                        operator_filter,
                        items, "paxos",
                        source="llm",
                    )
                    filter_llm_ms.append(ms)

            # ── operator_groupby ───────────────────────────────
            groupby_sql_ms = []
            for _ in range(trials):
                ms = _time_call(
                    aspect_sql.try_groupby,
                    items, "venue",
                    source="aspects", aspect_field="extras.venue",
                )
                groupby_sql_ms.append(ms)

            from nexus.mcp.core import operator_groupby
            groupby_llm_ms = []
            with patch(
                "nexus.operators.dispatch.claude_dispatch",
                _mock_claude_dispatch,
            ):
                for _ in range(trials):
                    ms = await _time_async_call(
                        operator_groupby,
                        items, "venue",
                        source="llm",
                    )
                    groupby_llm_ms.append(ms)

            # ── operator_aggregate ─────────────────────────────
            # Build a groups payload from a previous groupby call.
            groupby_result = aspect_sql.try_groupby(
                items, "venue",
                source="aspects", aspect_field="extras.venue",
            )
            groups_payload = json.dumps(groupby_result["groups"])

            aggregate_sql_ms = []
            for _ in range(trials):
                ms = _time_call(
                    aspect_sql.try_aggregate,
                    groups_payload, "count",
                    source="aspects", aspect_field="",
                )
                aggregate_sql_ms.append(ms)

            from nexus.mcp.core import operator_aggregate
            aggregate_llm_ms = []
            with patch(
                "nexus.operators.dispatch.claude_dispatch",
                _mock_claude_dispatch,
            ):
                for _ in range(trials):
                    ms = await _time_async_call(
                        operator_aggregate,
                        groups_payload, "count",
                        source="llm",
                    )
                    aggregate_llm_ms.append(ms)
        finally:
            h.default_db_path = original

    return {
        "operator_filter": _summarise(filter_sql_ms, filter_llm_ms),
        "operator_groupby": _summarise(groupby_sql_ms, groupby_llm_ms),
        "operator_aggregate": _summarise(aggregate_sql_ms, aggregate_llm_ms),
    }


def _summarise(sql_ms: list[float], llm_ms: list[float]) -> dict:
    sql_median = statistics.median(sql_ms)
    llm_median = statistics.median(llm_ms)
    speedup = llm_median / sql_median if sql_median > 0 else float("inf")
    return {
        "sql_median_ms": round(sql_median, 3),
        "sql_p95_ms": round(_p95(sql_ms), 3),
        "llm_median_ms": round(llm_median, 3),
        "llm_p95_ms": round(_p95(llm_ms), 3),
        "speedup_x": round(speedup, 1),
        "trials": len(sql_ms),
    }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    return sorted_vals[max(0, int(0.95 * len(sorted_vals)) - 1)]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--papers", type=int, default=100)
    p.add_argument("--trials", type=int, default=10)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent,
    )
    args = p.parse_args()

    print(f"Benchmarking SQL fast path vs LLM path on "
          f"{args.papers} papers, {args.trials} trials each operator...")
    started = datetime.now(UTC).isoformat()

    summary: dict = {
        "rdr": "rdr-089",
        "phase": "C",
        "started_at": started,
        "papers": args.papers,
        "trials": args.trials,
        "llm_simulated_latency_s": _LLM_SIMULATED_LATENCY_S,
        "results": asyncio.run(_run(args.papers, args.trials)),
        "finished_at": datetime.now(UTC).isoformat(),
    }

    out = args.output_dir / "bench_rdr089_results.json"
    out.write_text(json.dumps(summary, indent=2))

    print()
    print(f"{'operator':24s} {'SQL median':>12s} {'LLM median':>12s} {'speedup':>10s}")
    for op, r in summary["results"].items():
        print(f"{op:24s} {r['sql_median_ms']:>9.2f} ms "
              f"{r['llm_median_ms']:>9.2f} ms "
              f"{r['speedup_x']:>8.1f}x")
    print()
    print(f"Summary -> {out}")
    print()

    # Verdict: SQL must be at least 10x faster than LLM at 100 papers
    # to claim "O(1) SQL reads". A 10x bound is conservative; the
    # actual ratio at this corpus size should be 100x+.
    verdict_pass = all(
        r["speedup_x"] >= 10.0
        for r in summary["results"].values()
    )
    summary["verdict_pass"] = verdict_pass
    out.write_text(json.dumps(summary, indent=2))
    return 0 if verdict_pass else 1


if __name__ == "__main__":
    sys.exit(main())
