# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-092 replay harness — Tier 1 empirical validation.

Replays every live plan's stored anchor through the new composer and
matcher. Reports self-match rank, attractor distribution, confidence
histogram, and noise-floor rejection rate.

Operates on a throwaway clone of the user's ``memory.db`` so production
``match_count`` is not mutated. Builds a fresh T1 ``plans__session``
cache from an ``EphemeralClient`` so no external ChromaDB is required.

Usage:
    uv run python scripts/rdr092_replay.py
    uv run python scripts/rdr092_replay.py --db /path/to/memory.db
    uv run python scripts/rdr092_replay.py --json report.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import chromadb

from nexus.commands._helpers import default_db_path
from nexus.db.t2.plan_library import PlanLibrary
from nexus.plans.matcher import plan_match
from nexus.plans.session_cache import PlanSessionCache


# Noise probes — synthetic prompts unrelated to the nexus project. Each
# should ideally land with confidence < 0.40 against every plan. Hits
# above the floor are surfaced as attractor evidence.
NOISE_PROBES: list[str] = [
    "What is the weather in Paris right now",
    "How do I brew espresso with a moka pot",
    "Give me today's stock prices for TSLA",
    "Write a haiku about autumn leaves",
    "How to replace a kitchen faucet washer",
    "What time is sunset in Reykjavik",
    "Recommend a beginner knitting pattern",
    "Translate 'good morning' into Japanese",
    "xyzzy qwerty foo bar baz quux",
    "Tell me a joke about penguins",
]

# Confidence-histogram buckets.
CONF_BUCKETS: list[tuple[float, float]] = [
    (0.00, 0.20),
    (0.20, 0.40),
    (0.40, 0.50),
    (0.50, 0.60),
    (0.60, 0.70),
    (0.70, 0.85),
    (0.85, 1.01),
]

# A plan appearing in > this many probes' top-5 (across legit + noise)
# is flagged as a potential attractor.
ATTRACTOR_THRESHOLD: int = 10


def clone_db(source: Path) -> Path:
    """Copy *source* to a temp file and return the clone path."""
    tmp = Path(tempfile.mkdtemp(prefix="rdr092-replay-")) / "memory.db"
    shutil.copy2(source, tmp)
    return tmp


def bucket(conf: float | None) -> str:
    """Return a bucket label for a confidence value. ``None`` → ``fts5``."""
    if conf is None:
        return "fts5"
    for lo, hi in CONF_BUCKETS:
        if lo <= conf < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return "?"


def replay(db_path: Path) -> dict[str, Any]:
    """Run the full replay. Returns a report dict."""
    lib = PlanLibrary(db_path)
    client = chromadb.EphemeralClient()
    cache = PlanSessionCache(client=client, session_id="replay")

    loaded = cache.populate(lib)
    plans = lib.list_active_plans()
    if not plans:
        raise SystemExit("No active plans in library.")

    by_id: dict[int, dict[str, Any]] = {int(p["id"]): p for p in plans}

    def verb_of(pid: int) -> str:
        return (by_id.get(pid, {}).get("verb") or "")

    self_ranks: list[int | None] = []            # rank of own plan (1..5) or None
    self_confidences: list[float] = []           # conf of own plan when at rank ≤5
    rank1_confidences: list[float | None] = []   # conf of whichever plan was rank-1
    conf_histogram: Counter[str] = Counter()
    attractor_counts: Counter[int] = Counter()   # plan_id → top-5 appearances
    never_self_match: list[dict[str, Any]] = []
    fts5_fallbacks: int = 0

    # ── Legit probes: each plan's own anchor ──
    probe_records: list[dict[str, Any]] = []
    for plan in plans:
        intent = (plan.get("query") or "").strip()
        if not intent:
            continue
        pid = int(plan["id"])
        matches = plan_match(intent, library=lib, cache=cache, n=5)
        top5 = [
            {
                "plan_id": m.plan_id,
                "confidence": m.confidence,
                "verb": verb_of(m.plan_id),
                "name": m.name,
                "scope_tags": m.scope_tags,
            }
            for m in matches
        ]
        self_rank: int | None = None
        self_conf: float | None = None
        for i, m in enumerate(matches, start=1):
            attractor_counts[m.plan_id] += 1
            conf_histogram[bucket(m.confidence)] += 1
            if m.plan_id == pid:
                self_rank = i
                self_conf = m.confidence
            if m.confidence is None:
                fts5_fallbacks += 1
        self_ranks.append(self_rank)
        if self_conf is not None:
            self_confidences.append(self_conf)
        if matches:
            rank1_confidences.append(matches[0].confidence)
        if self_rank is None:
            never_self_match.append({
                "plan_id": pid,
                "anchor": intent[:90],
                "verb": plan.get("verb") or "",
                "name": plan.get("name") or "",
                "top1_plan_id": matches[0].plan_id if matches else None,
                "top1_conf": matches[0].confidence if matches else None,
            })
        probe_records.append({
            "plan_id": pid,
            "anchor": intent[:120],
            "self_rank": self_rank,
            "self_conf": self_conf,
            "top5": top5,
        })

    # ── Noise sweep ──
    noise_records: list[dict[str, Any]] = []
    noise_above_floor: list[dict[str, Any]] = []
    for probe in NOISE_PROBES:
        matches = plan_match(probe, library=lib, cache=cache, n=5)
        top5 = [
            {
                "plan_id": m.plan_id,
                "confidence": m.confidence,
                "verb": verb_of(m.plan_id),
                "name": m.name,
            }
            for m in matches
        ]
        noise_records.append({"probe": probe, "top5": top5})
        for m in matches:
            if m.confidence is not None and m.confidence >= 0.40:
                noise_above_floor.append({
                    "probe": probe,
                    "plan_id": m.plan_id,
                    "verb": verb_of(m.plan_id),
                    "name": m.name,
                    "confidence": m.confidence,
                })

    # ── Aggregate ──
    ranked = [r for r in self_ranks if r is not None]
    rank_dist = Counter(ranked)
    unranked = sum(1 for r in self_ranks if r is None)

    attractors = [
        {
            "plan_id": pid,
            "verb": by_id[pid].get("verb") or "",
            "name": by_id[pid].get("name") or "",
            "scope_tags": by_id[pid].get("scope_tags") or "",
            "top5_appearances": count,
        }
        for pid, count in attractor_counts.most_common()
        if count >= ATTRACTOR_THRESHOLD
    ]

    rank1_cos = [c for c in rank1_confidences if c is not None]

    return {
        "db_path": str(db_path),
        "plans_loaded_to_t1": loaded,
        "plans_probed": len(probe_records),
        "fts5_fallback_hits": fts5_fallbacks,
        "self_match": {
            "rank_distribution": dict(sorted(rank_dist.items())),
            "rank1_count": rank_dist.get(1, 0),
            "unranked_count": unranked,
            "rank1_pct": round(100 * rank_dist.get(1, 0) / max(len(self_ranks), 1), 1),
            "self_conf_median": round(statistics.median(self_confidences), 3)
                                if self_confidences else None,
            "self_conf_min": round(min(self_confidences), 3) if self_confidences else None,
            "self_conf_max": round(max(self_confidences), 3) if self_confidences else None,
        },
        "rank1_confidence": {
            "median": round(statistics.median(rank1_cos), 3) if rank1_cos else None,
            "min": round(min(rank1_cos), 3) if rank1_cos else None,
            "max": round(max(rank1_cos), 3) if rank1_cos else None,
        },
        "confidence_histogram": dict(conf_histogram.most_common()),
        "attractors_flagged": attractors,
        "never_self_match": never_self_match,
        "noise_sweep": {
            "probe_count": len(NOISE_PROBES),
            "above_floor_count": len(noise_above_floor),
            "above_floor_hits": noise_above_floor,
        },
        "probes": probe_records,
        "noise_probes": noise_records,
    }


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# RDR-092 Replay Report")
    lines.append("")
    lines.append(f"DB clone: {report['db_path']}")
    lines.append(f"Plans loaded to T1: {report['plans_loaded_to_t1']}")
    lines.append(f"Plans probed: {report['plans_probed']}")
    lines.append(f"FTS5 fallback hits: {report['fts5_fallback_hits']}")
    lines.append("")

    sm = report["self_match"]
    lines.append("## Self-match")
    lines.append(f"  rank-1: {sm['rank1_count']} ({sm['rank1_pct']}%)")
    lines.append(f"  rank distribution: {sm['rank_distribution']}")
    lines.append(f"  unranked in top-5: {sm['unranked_count']}")
    lines.append(f"  self-conf median/min/max: "
                 f"{sm['self_conf_median']} / {sm['self_conf_min']} / {sm['self_conf_max']}")
    lines.append("")

    r1 = report["rank1_confidence"]
    lines.append("## Rank-1 confidence (whichever plan landed at rank-1)")
    lines.append(f"  median/min/max: {r1['median']} / {r1['min']} / {r1['max']}")
    lines.append("")

    lines.append("## Confidence histogram (all top-5 appearances)")
    for b, count in report["confidence_histogram"].items():
        lines.append(f"  {b}: {count}")
    lines.append("")

    attractors = report["attractors_flagged"]
    lines.append(f"## Attractors (appearing in ≥ {ATTRACTOR_THRESHOLD} probes' top-5)")
    if not attractors:
        lines.append("  (none)")
    else:
        for a in attractors:
            lines.append(f"  plan#{a['plan_id']} verb={a['verb']!r} "
                         f"name={a['name']!r} scope={a['scope_tags']!r} "
                         f"appearances={a['top5_appearances']}")
    lines.append("")

    nsm = report["never_self_match"]
    lines.append(f"## Never self-match (plans whose own anchor missed top-5): {len(nsm)}")
    for e in nsm[:15]:
        conf = (f"{e['top1_conf']:.3f}" if isinstance(e['top1_conf'], float)
                else e['top1_conf'])
        lines.append(f"  plan#{e['plan_id']} {e['verb']}/{e['name']} → "
                     f"top1=plan#{e['top1_plan_id']} ({conf}) "
                     f"anchor={e['anchor']!r}")
    if len(nsm) > 15:
        lines.append(f"  … and {len(nsm) - 15} more")
    lines.append("")

    noise = report["noise_sweep"]
    lines.append("## Noise sweep")
    lines.append(f"  probes: {noise['probe_count']}")
    lines.append(f"  hits above 0.40 floor: {noise['above_floor_count']}")
    for h in noise['above_floor_hits']:
        lines.append(f"    [{h['confidence']:.3f}] probe={h['probe']!r} → "
                     f"plan#{h['plan_id']} {h['verb']}/{h['name']}")
    lines.append("")

    # Per-probe noise top-5 (for eyeballing what's close even if below floor)
    lines.append("## Noise top-1 by probe")
    for nr in report["noise_probes"]:
        top1 = nr["top5"][0] if nr["top5"] else None
        if top1 is None:
            lines.append(f"  {nr['probe']!r}: (no hits)")
        else:
            conf = top1["confidence"]
            conf_s = f"{conf:.3f}" if isinstance(conf, float) else str(conf)
            lines.append(f"  [{conf_s}] {nr['probe']!r} → "
                         f"plan#{top1['plan_id']} {top1['verb']}/{top1['name']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None,
                    help="Path to memory.db (default: user config dir)")
    ap.add_argument("--json", type=Path, default=None,
                    help="Write full JSON report to this path")
    ap.add_argument("--keep-clone", action="store_true",
                    help="Do not delete the temp clone on exit")
    args = ap.parse_args()

    source = args.db or default_db_path()
    if not source.exists():
        print(f"memory.db not found: {source}", file=sys.stderr)
        return 2

    clone = clone_db(source)
    try:
        report = replay(clone)
    finally:
        if not args.keep_clone:
            shutil.rmtree(clone.parent, ignore_errors=True)

    print(render(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nJSON written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
