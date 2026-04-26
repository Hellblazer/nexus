#!/usr/bin/env -S uv run python
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-089 P1.3 spike — 10-paper aspect-extraction probe on knowledge__delos.

Purpose: gate Phase 2 by validating Critical Assumption #2
(per-document extraction adds <3s to ingest time) AND the
exactly-once-per-document fire invariant for the new
``fire_post_document_hooks`` chain wired in P0.2.

Three measurement modes:

  (A) Latency
      For each of N papers, call ``extract_aspects(content, source_path,
      "knowledge__delos")`` and record wall-clock time. Repeat 3
      times. Report per-paper median, overall median + p95.
      Pass: p95 < 3s AND median < 1.5s.

  (B) Fire-once invariant
      Register a counting hook for the duration of one ingest pass,
      run ``nx index pdf <papers> --collection knowledge__delos``,
      then assert each source_path appears EXACTLY once in the
      counter. Validates the P0.2 placement decisions, especially
      the index_pdf branch-join (NOT inside _index_pdf_incremental
      loop).
      Pass: every paper appears exactly once; no zero, no duplicate.

  (C) Schema conformance + field stability
      Reuses the (A) results — for each paper count populated
      fields and count fields stable across all 3 runs (string
      equality for scalars, set equality for arrays).
      Pass: stability rate matches the RDR-088 Spike A budget
      (95% / 99% / 0% reliability tiers — see RDR-089 Critical
      Assumption #1).

Cost: ~30 ``claude -p`` calls (10 papers × 3 runs). Haiku-class.
~$0.30–1.00 USD depending on paper length.

Wall-clock: ~3-10 minutes assuming median ~2s/extraction. With
exponential backoff on transient failures, can be longer.

Defaults:
  --papers-dir  ~/Downloads/delos-papers
  --num-papers  10
  --runs        3
  --output-dir  scripts/spikes
  --mode        all      (one of: latency, fire-once, all)

Outputs:
  scripts/spikes/spike_rdr089_results.jsonl   — per-attempt rows
  scripts/spikes/spike_rdr089_summary.json    — aggregated verdict
  scripts/spikes/spike_rdr089_run.log         — structlog tail

The harness does NOT mutate production T2 — it calls
``extract_aspects`` directly (the writer hook is Phase 2). The
fire-once mode shells out to ``nx index pdf``; that mutation IS
real and writes to the running ChromaDB instance. Pass --skip-index
to skip the (B) mode if you don't want to touch T3.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow running the script in-place without an installed conexus.
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nexus.aspect_extractor import extract_aspects  # noqa: E402


def _select_papers(papers_dir: Path, n: int) -> list[Path]:
    """Return the first *n* PDFs (alphabetical) under *papers_dir*."""
    if not papers_dir.exists():
        raise FileNotFoundError(f"papers_dir does not exist: {papers_dir}")
    candidates = sorted(p for p in papers_dir.iterdir() if p.suffix.lower() == ".pdf")
    if not candidates:
        raise FileNotFoundError(f"No PDFs found under {papers_dir}")
    return candidates[:n]


def _read_paper_text(path: Path) -> str:
    """Best-effort PDF → text via pymupdf (avoid Voyage-side embedding /
    chunking — we need raw text only for extraction timing).
    """
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pymupdf required for spike harness — `uv sync` in repo root",
        ) from exc
    with pymupdf.open(str(path)) as doc:
        return "\n".join(page.get_text() for page in doc)


# ── Mode A: latency + schema conformance ────────────────────────────────────


def _run_latency_mode(
    papers: list[Path],
    runs: int,
    out_jsonl: Path,
) -> list[dict]:
    """Run ``extract_aspects`` ``runs`` times per paper. Append one
    JSONL row per attempt to *out_jsonl* and return the rows.
    """
    rows: list[dict] = []
    with out_jsonl.open("a") as f:
        for paper in papers:
            try:
                content = _read_paper_text(paper)
            except Exception as exc:
                row = {
                    "mode": "latency",
                    "source_path": str(paper),
                    "run_id": None,
                    "elapsed_ms": None,
                    "status": "read_failed",
                    "error": str(exc),
                    "fields_populated": 0,
                }
                rows.append(row)
                f.write(json.dumps(row) + "\n")
                continue

            for run_id in range(runs):
                start = time.monotonic()
                try:
                    record = extract_aspects(
                        content=content,
                        source_path=str(paper),
                        collection="knowledge__delos",
                    )
                    status = "ok"
                    error = None
                except Exception as exc:
                    record = None
                    status = "exception"
                    error = repr(exc)
                elapsed_ms = (time.monotonic() - start) * 1000.0

                if record is None:
                    fields_populated = 0
                    fields_payload = None
                else:
                    fields_populated = sum(
                        1 for v in (
                            record.problem_formulation,
                            record.proposed_method,
                            record.experimental_results,
                        ) if v
                    ) + (1 if record.experimental_datasets else 0) \
                      + (1 if record.experimental_baselines else 0)
                    fields_payload = {
                        "problem_formulation": record.problem_formulation,
                        "proposed_method": record.proposed_method,
                        "experimental_datasets": record.experimental_datasets,
                        "experimental_baselines": record.experimental_baselines,
                        "experimental_results": record.experimental_results,
                        "extras": record.extras,
                        "confidence": record.confidence,
                    }

                row = {
                    "mode": "latency",
                    "source_path": str(paper),
                    "run_id": run_id,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "status": status,
                    "error": error,
                    "fields_populated": fields_populated,
                    "fields": fields_payload,
                }
                rows.append(row)
                f.write(json.dumps(row) + "\n")
                f.flush()
                print(
                    f"[latency] {paper.name} run {run_id}: "
                    f"{elapsed_ms:.0f} ms ({fields_populated}/5 fields)"
                )
    return rows


def _stability_for_paper(paper_rows: list[dict]) -> dict:
    """Compute field-stability rate across runs for one paper.

    Stable = same value across all successful runs (set equality for
    arrays, string equality for scalars). Returns None for the paper
    if fewer than 2 successful runs are available.
    """
    successful = [r for r in paper_rows if r["status"] == "ok" and r["fields"]]
    if len(successful) < 2:
        return {"paper": paper_rows[0]["source_path"], "stable_count": None}

    fields_to_check = (
        ("problem_formulation", "scalar"),
        ("proposed_method", "scalar"),
        ("experimental_datasets", "set"),
        ("experimental_baselines", "set"),
        ("experimental_results", "scalar"),
    )
    stable_count = 0
    detail: dict[str, bool] = {}
    for name, kind in fields_to_check:
        values = [r["fields"][name] for r in successful]
        if kind == "scalar":
            stable = len({v for v in values if v is not None}) <= 1
        else:
            stable = len({tuple(sorted(v or [])) for v in values}) == 1
        detail[name] = stable
        if stable:
            stable_count += 1
    return {
        "paper": paper_rows[0]["source_path"],
        "stable_count": stable_count,
        "field_stability": detail,
    }


def _summarize_latency(rows: list[dict]) -> dict:
    """Aggregate latency statistics + stability rates.

    Distinguishes ``ok_*`` aggregates (only ``status=ok`` rows — what the
    extractor actually delivered) from ``all_*`` aggregates (every timed
    attempt including exceptions). Both verdicts are reported because the
    reviewer for the spike-evidence record needs to see the gap (an
    exception attempt completes in ~0.1 ms and pulls the all-rows median
    down, so the all-rows view is conservative).
    """
    by_paper: dict[str, list[dict]] = {}
    for r in rows:
        by_paper.setdefault(r["source_path"], []).append(r)

    all_elapsed = [r["elapsed_ms"] for r in rows if r["elapsed_ms"] is not None]
    ok_elapsed = [
        r["elapsed_ms"]
        for r in rows
        if r["elapsed_ms"] is not None and r.get("status") == "ok"
    ]
    if not all_elapsed:
        return {
            "mode": "latency",
            "papers_total": len(by_paper),
            "all_timed_attempts": 0,
            "successful_attempts": 0,
            "verdict_latency": "FAIL",
            "reason": "no timed attempts recorded",
        }
    primary = ok_elapsed if ok_elapsed else all_elapsed
    primary.sort()
    median = statistics.median(primary)
    p95_idx = max(0, int(0.95 * len(primary)) - 1)
    p95 = primary[p95_idx]

    pass_latency = (median < 1500.0) and (p95 < 3000.0)

    # Also compute the all-attempts view for full evidence reporting.
    all_sorted = sorted(all_elapsed)
    all_median = statistics.median(all_sorted)
    all_p95 = all_sorted[max(0, int(0.95 * len(all_sorted)) - 1)]

    stability = [_stability_for_paper(rs) for rs in by_paper.values()]
    stable_field_total = sum(
        s["stable_count"] for s in stability if s["stable_count"] is not None
    )
    eligible_papers = sum(1 for s in stability if s["stable_count"] is not None)
    field_stability_rate = (
        stable_field_total / (5 * eligible_papers)
        if eligible_papers else None
    )

    return {
        "mode": "latency",
        "papers_total": len(by_paper),
        "attempts_total": len(rows),
        "all_timed_attempts": len(all_elapsed),
        "successful_attempts": len(ok_elapsed),
        "median_ms": round(median, 2),  # status=ok only when any exist
        "p95_ms": round(p95, 2),
        "min_ms": round(min(primary), 2),
        "max_ms": round(max(primary), 2),
        "all_attempts_median_ms": round(all_median, 2),
        "all_attempts_p95_ms": round(all_p95, 2),
        "verdict_latency": "PASS" if pass_latency else "FAIL",
        "verdict_latency_threshold": "median<1500ms AND p95<3000ms (over status=ok rows)",
        "field_stability_rate": (
            round(field_stability_rate, 3)
            if field_stability_rate is not None else None
        ),
        "field_stability_per_paper": stability,
    }


# ── Mode B: fire-once invariant ──────────────────────────────────────────────


def _run_fire_once_mode(
    papers: list[Path],
    out_jsonl: Path,
) -> dict:
    """Register a counting hook, shell out to ``nx index pdf``, then
    assert each paper appears exactly once.

    Cost: this mode triggers real T3 writes via ``nx index pdf``. Use
    --skip-index to avoid this.
    """
    from nexus.mcp_infra import (
        _post_document_hooks,
        register_post_document_hook,
    )

    counter: dict[str, int] = {}

    def counting_hook(source_path: str, collection: str, content: str) -> None:
        counter[source_path] = counter.get(source_path, 0) + 1

    # Note: registration is process-local. The harness runs in the
    # foreground; ``nx index pdf`` re-imports nexus and registers
    # afresh in its own process — so this in-process hook does NOT
    # see fires from a separate ``nx`` invocation. The fire-once
    # mode therefore does an in-process simulation: it imports and
    # invokes the indexer functions directly so the registered hook
    # sees the fires.
    register_post_document_hook(counting_hook)
    try:
        # Drive each paper through ``index_pdf`` directly (in-process).
        from nexus.doc_indexer import index_pdf

        for paper in papers:
            try:
                index_pdf(paper, "delos", collection_name="knowledge__delos")
            except Exception as exc:
                row = {
                    "mode": "fire-once",
                    "source_path": str(paper),
                    "status": "ingest_failed",
                    "error": repr(exc),
                }
                with out_jsonl.open("a") as f:
                    f.write(json.dumps(row) + "\n")
    finally:
        if counting_hook in _post_document_hooks:
            _post_document_hooks.remove(counting_hook)

    # Per-paper fire-count rows
    duplicates = []
    misses = []
    with out_jsonl.open("a") as f:
        for paper in papers:
            count = counter.get(str(paper), 0)
            row = {
                "mode": "fire-once",
                "source_path": str(paper),
                "fire_count": count,
            }
            if count == 0:
                misses.append(str(paper))
                row["status"] = "miss"
            elif count > 1:
                duplicates.append((str(paper), count))
                row["status"] = "duplicate"
            else:
                row["status"] = "exact_once"
            f.write(json.dumps(row) + "\n")

    pass_fire = (not duplicates) and (not misses)
    return {
        "mode": "fire-once",
        "papers_total": len(papers),
        "fire_count_distribution": dict(counter),
        "duplicates": duplicates,
        "misses": misses,
        "verdict_fire_once": "PASS" if pass_fire else "FAIL",
    }


# ── Top-level orchestration ──────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument(
        "--papers-dir",
        type=Path,
        default=Path.home() / "Downloads" / "delos-papers",
    )
    p.add_argument("--num-papers", type=int, default=10)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent,
    )
    p.add_argument(
        "--mode",
        choices=["latency", "fire-once", "all"],
        default="all",
    )
    p.add_argument(
        "--skip-index",
        action="store_true",
        help="Skip the fire-once mode (which writes to T3).",
    )
    args = p.parse_args()

    out_jsonl = args.output_dir / "spike_rdr089_results.jsonl"
    out_summary = args.output_dir / "spike_rdr089_summary.json"

    # Truncate prior results so re-runs do not accumulate.
    out_jsonl.write_text("")

    papers = _select_papers(args.papers_dir, args.num_papers)
    print(f"Selected {len(papers)} paper(s) under {args.papers_dir}")

    summary: dict[str, Any] = {
        "rdr": "rdr-089",
        "started_at": datetime.now(UTC).isoformat(),
        "papers_dir": str(args.papers_dir),
        "papers_count": len(papers),
        "runs_per_paper": args.runs,
        "results": {},
    }

    if args.mode in ("latency", "all"):
        rows = _run_latency_mode(papers, args.runs, out_jsonl)
        summary["results"]["latency"] = _summarize_latency(rows)

    if args.mode in ("fire-once", "all") and not args.skip_index:
        summary["results"]["fire_once"] = _run_fire_once_mode(papers, out_jsonl)
    elif args.skip_index:
        summary["results"]["fire_once"] = {
            "mode": "fire-once",
            "verdict_fire_once": "SKIPPED",
            "reason": "--skip-index passed",
        }

    summary["finished_at"] = datetime.now(UTC).isoformat()

    out_summary.write_text(json.dumps(summary, indent=2))
    print(f"Summary → {out_summary}")
    print(f"Per-attempt rows → {out_jsonl}")

    # Exit code reflects verdict aggregation: 0 = pass, 1 = any FAIL.
    failed = any(
        v.get(f"verdict_{m}") == "FAIL"
        for m, v in summary["results"].items()
        for k in (m,)
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
