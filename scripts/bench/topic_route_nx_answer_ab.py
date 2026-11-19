# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 9 (nexus-7fku2.2.3): nx_answer with ``search.topic_route_nx_answer`` off against ``top1``, paired.

Each question runs through the real ``nx_answer(structured=True)`` twice, once per arm, in alternating order (off
first on even questions, top1 first on odd ones). The arm is switched in-process by replacing
``nexus.plans.topic_route_scope.configured_route``, which the plan runner reads once per run (Step 7). The
envelope's chunks are resolved to file paths through the catalog and graded with ``taxonomy_ab2.score``
(NDCG@3, gain 2^g-1; recall@10; dedupe by file name; RDR id or normalised basename).

Ordering (see :data:`ORDERING`): the envelope order, which is each retrieval step's own ranking (search and query
sort by ``hybrid_score``) with the steps in plan order. The per-step ``hybrid_scores`` are min-max normalised
within each step's own window, so they are not merged into one cross-step sort; the envelope does not carry them.

SPEND: this calls a language model. ``--cap-usd`` is required and has no default; Sam names it for the run. Every
call gets ``budget_usd`` = what is left of the cap, a question starts only while twice the largest call cost seen
still fits, and an envelope with no ``cost_usd`` stops the run (the cap cannot be enforced on an unknown cost).
Run ``--limit 1`` first to project the total.

The pass rule (:func:`pass_rule`) is the RDR's and is not a parameter.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from bench.paths import _resolve_paths_via_catalog
from bench.taxonomy_ab2 import load_queries, score

ARMS: tuple[str, ...] = ("off", "top1")
ORDERING = (
    "envelope order: each retrieval step's own hybrid_score ranking, steps in plan order "
    "(per-step hybrid_scores are min-max normalised per window, so not merged across steps)"
)
#: The RDR's rule (Step 9). Changing it is Sam's decision.
MIN_MEAN_DELTA_ALL = 0.03
MIN_MEAN_DELTA_GENERIC = -0.02
MIN_GENERIC = 10
_EPS = 1e-9
IN_SAMPLE_KINDS = ("rdr",)
OUT_OF_SAMPLE_KINDS = ("title", "generic")


def pass_rule(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Each paired row is ``{"kind", "off", "top1"}`` with NDCG@3 floats. Returns every component and the verdict."""
    deltas = [r["top1"] - r["off"] for r in rows]
    generic = [r["top1"] - r["off"] for r in rows if r["kind"] == "generic"]
    wins = sum(d > _EPS for d in deltas)
    losses = sum(d < -_EPS for d in deltas)
    mean_all = statistics.mean(deltas) if deltas else 0.0
    mean_generic = statistics.mean(generic) if generic else 0.0
    note = "" if len(generic) >= MIN_GENERIC else f"fewer than {MIN_GENERIC} generic questions scored ({len(generic)})"
    checks = {
        "mean_delta_all": bool(deltas) and mean_all >= MIN_MEAN_DELTA_ALL - _EPS,
        "more_better_than_worse": wins > losses,
        "generic_loss": not note and mean_generic >= MIN_MEAN_DELTA_GENERIC - _EPS,
    }
    return {
        "n": len(rows), "mean_delta_all": mean_all, "wins": wins, "losses": losses, "ties": len(rows) - wins - losses,
        "n_generic": len(generic), "mean_delta_generic": mean_generic, "generic_note": note,
        "checks": checks, "passed": all(checks.values()),
        "rule": {"min_mean_delta_all": MIN_MEAN_DELTA_ALL, "min_mean_delta_generic": MIN_MEAN_DELTA_GENERIC,
                 "min_generic": MIN_GENERIC},
    }


@contextlib.contextmanager
def arm_setting(arm: str) -> Iterator[None]:
    """Make the plan runner read *arm* as ``search.topic_route_nx_answer`` for the calls inside the block."""
    from nexus.plans import topic_route_scope as trs  # noqa: PLC0415

    route = trs.parse_setting(arm)
    saved = trs.configured_route
    trs.configured_route = lambda: dict(route) if route is not None else None  # type: ignore[assignment]
    try:
        yield
    finally:
        trs.configured_route = saved  # type: ignore[assignment]


class _Stop(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def _answer_arm(q: dict[str, Any], arm: str, *, answer_fn: Callable, catalog: Any, scope: str | None,
                budget_usd: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"budget_usd": budget_usd}
    if scope:
        kwargs["scope"] = scope
    with arm_setting(arm):
        try:
            envelope = asyncio.run(answer_fn(question=q["text"], structured=True, trace=False, **kwargs))
        except Exception as e:  # noqa: BLE001 — its cost is unknown, so the run stops and says why
            raise _Stop("answer_error", f"{q['qid']} {arm}: {type(e).__name__}: {e}") from e
    if not isinstance(envelope, dict):
        raise _Stop("answer_error", f"{q['qid']} {arm}: non-envelope {type(envelope).__name__}")
    cost = envelope.get("cost_usd")
    if cost is None:
        raise _Stop("cost_unknown", f"{q['qid']} {arm}: the envelope has no cost_usd")
    chunks = [c for c in envelope.get("chunks") or [] if isinstance(c, dict)]
    rows = [{"chash": c.get("chash", ""), "collection": c.get("collection", "")} for c in chunks]
    out: dict[str, Any] = {"cost_usd": float(cost), "chunk_count": len(rows), "error": None}
    if rows:
        paths = _resolve_paths_via_catalog(catalog, [r["chash"] for r in rows])
        if not any(paths.get(r["chash"]) for r in rows):
            out.update(error="vacuous: every chunk resolved to an empty path", ndcg3=None, recall10=None, top3=[])
            return out
    out.update(score(rows, q["gt"], catalog))
    return out


def run(queries: list[dict[str, Any]], *, answer_fn: Callable, catalog: Any, cap_usd: float,
        scope: str | None) -> dict[str, Any]:
    spent = 0.0
    largest = 0.0
    rows: list[dict[str, Any]] = []
    stopped: str | None = None
    detail = ""
    try:
        for i, q in enumerate(queries):
            if spent + 2 * largest > cap_usd + _EPS:
                raise _Stop("cap", f"spent {spent:.4f} of {cap_usd}; the next question could cost {2 * largest:.4f}")
            arms = ARMS if i % 2 == 0 else tuple(reversed(ARMS))
            row: dict[str, Any] = {"qid": q["qid"], "kind": q["kind"], "order": list(arms)}
            for arm in arms:
                res = _answer_arm(q, arm, answer_fn=answer_fn, catalog=catalog, scope=scope,
                                  budget_usd=max(cap_usd - spent, 0.0))
                spent += res["cost_usd"]
                largest = max(largest, res["cost_usd"])
                row[arm] = res
            row["paired"] = all(row[a]["error"] is None for a in ARMS)
            rows.append(row)
            print(  # noqa: T201 — progress line of a bench script
                q["qid"], q["kind"], {a: row[a].get("ndcg3") for a in ARMS}, f"spent={spent:.4f}",
                file=sys.stderr, flush=True,
            )
    except _Stop as s:
        stopped, detail = s.reason, s.detail
    return {"rows": rows, "spent_usd": spent, "cap_usd": cap_usd, "stopped": stopped, "stop_detail": detail}


def _arm_means(sel: list[dict[str, Any]]) -> dict[str, Any]:
    return {a: {"ndcg3": statistics.mean(r[a]["ndcg3"] for r in sel),
                "recall10": statistics.mean(r[a]["recall10"] for r in sel)} for a in ARMS}


def _split(sel: list[dict[str, Any]]) -> dict[str, Any]:
    if not sel:
        return {"n": 0}
    return {"n": len(sel), **_arm_means(sel),
            "mean_delta_ndcg3": statistics.mean(r["top1"]["ndcg3"] - r["off"]["ndcg3"] for r in sel)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paired = [r for r in rows if r.get("paired")]
    kinds = sorted({r["kind"] for r in paired})
    return {
        "ordering": ORDERING,
        "n": len(rows), "paired": len(paired), "errors": len(rows) - len(paired),
        "by_kind": {k: _split([r for r in paired if r["kind"] == k]) for k in kinds},
        "in_sample": _split([r for r in paired if r["kind"] in IN_SAMPLE_KINDS]),
        "out_of_sample": _split([r for r in paired if r["kind"] in OUT_OF_SAMPLE_KINDS]),
        "pass_rule": pass_rule([{"kind": r["kind"], "off": r["off"]["ndcg3"], "top1": r["top1"]["ndcg3"]}
                                for r in paired]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml"), Path("bench/queries/generic_docs_v1.yaml")])
    ap.add_argument("--cap-usd", type=float, required=True, help="the spend cap Sam named for this run")
    ap.add_argument("--scope", default=None, help="nx_answer scope (default: none)")
    ap.add_argument("--limit", type=int, default=0, help="first N questions only (use 1 to project the cost)")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cap_usd <= 0:
        ap.error("--cap-usd must be > 0")

    from bench.manifest import build_manifest  # noqa: PLC0415
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.mcp.core import nx_answer  # noqa: PLC0415

    queries = load_queries(a.queries)
    if a.limit:
        queries = queries[: a.limit]
    result = run(queries, answer_fn=nx_answer, catalog=make_catalog_reader(), cap_usd=a.cap_usd, scope=a.scope)
    out = {"manifest": build_manifest(Path.cwd()), "queries": [str(p) for p in a.queries], "scope": a.scope,
           "summary": summarize(result["rows"]), **result}
    a.out.write_text(json.dumps(out, indent=1))
    print(json.dumps({k: out[k] for k in ("summary", "spent_usd", "cap_usd", "stopped", "stop_detail")}, indent=1))  # noqa: T201 — the run's readout
    return 0 if result["stopped"] in (None,) else 1


if __name__ == "__main__":
    sys.exit(main())
