# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 9 (nexus-7fku2.2.3): nx_answer with ``search.topic_route_nx_answer`` off against ``top1``, paired.

Run from the repo root: ``PYTHONPATH=scripts uv run python scripts/bench/topic_route_nx_answer_ab.py --cap-usd <cap>
--out bench/results/<name>.json`` (``--limit 1`` first, to project the cost).

Each question runs through the real ``nx_answer(structured=True)`` twice, once per arm, in alternating order (off
first on even questions, top1 first on odd ones); the generic-answer questions run first, so a stop still leaves the
class the rule's third clause needs. The arm is switched in-process by replacing
``nexus.plans.topic_route_scope.configured_route``, which the plan runner reads once per run (Step 7). A routed call
takes the per-collection route by itself (no ``NX_SEARCH_PER_COLLECTION`` needed); the off arm takes the default
path, so the pair compares what ships.

Ordering (pre-declared, 2026-11-19, before any run): the PRIMARY statistic, the one the pass rule reads, sorts the
envelope's chunks by their carried ``distance`` (ascending, chunks with none last): one absolute scale across steps,
and the ordering 134-research-13 used. The SECONDARY statistic is the envelope order (each step's own ranking, steps
in plan order); it is printed, never decided on. Each chunk is resolved to a file path through the catalog and graded
with ``taxonomy_ab2.score``: NDCG@3 (gain 2^g-1), recall@10, dedupe by file name, a generic question graded on its
owner's collections and the exact basename.

Non-vacuity: before any paid call the engine must report ``vector_topic_route_supported``. Every arm's
``search_topic_route`` events are captured, and a run whose top1 arms routed no collection at all is VACUOUS: no
verdict. Per arm the plan, step count, budget marker and elapsed time are kept, and a pair whose two arms ran
different plans is flagged.

SPEND: ``--cap-usd`` is required and has no default. Every call gets the same ``--call-budget-usd``; a question starts
only while twice the larger of that budget and the largest call cost seen still fits under the cap; an envelope with
no ``cost_usd``, or a step with none, stops the run (the cap cannot be enforced on an unknown cost). The results file
is rewritten after every question, and ``--resume`` continues from it without re-paying finished questions.

The pass rule (:func:`pass_rule`) is the RDR's and is not a parameter. A verdict needs a complete run: no stop, no
errored question, not vacuous.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import statistics
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import structlog

from bench.paths import _resolve_paths_via_catalog
from bench.taxonomy_ab2 import load_queries, score

ARMS: tuple[str, ...] = ("off", "top1")
PRIMARY = "distance"
SECONDARY = "envelope"
ORDERINGS = {
    PRIMARY: "envelope chunks sorted by carried distance, ascending, none last (the ordering of 134-research-13)",
    SECONDARY: "envelope order: each retrieval step's own ranking, steps in plan order (printed, not decided on)",
}
#: The RDR's rule (Step 9). Changing it is Sam's decision.
MIN_MEAN_DELTA_ALL = 0.03
MIN_MEAN_DELTA_GENERIC = -0.02
MIN_GENERIC = 10
_EPS = 1e-9
IN_SAMPLE_KINDS = ("rdr",)
OUT_OF_SAMPLE_KINDS = ("title", "generic")
ROUTE_EVENT = "search_topic_route"
ROUTE_COUNTS = ("routed", "no_topics", "fallback", "skipped", "unreported")
#: Pre-declared 2026-11-19, before any run: a FAIL from a run in which fewer questions than this had a routed top1
#: arm is reported as inconclusive (diluted), since it says little about routing. A pass is not affected.
MIN_ROUTED_QUESTIONS = 10
#: Stop early, before spending the whole cap, when this many questions in a row routed no collection at the start.
EARLY_VACUITY_QUESTIONS = 3
BOOTSTRAP_SEED = 1340
BOOTSTRAP_N = 2000
SCOPE_NOTES = (
    "A pass licenses the default for nx_answer's prose-scoped plan steps, on this corpus, judged on retrieved "
    "chunks; it says nothing about answer quality.",
    "The 25 RDR questions are in-sample (all among leg 1's 26). 'Out-of-sample' (title + generic) means not used to "
    "choose the design; the title questions were measured before (134-research-13), so they are not unseen.",
    "The off arm can grow a plan that the top1 arm then matches (nx_answer saves ad-hoc plans); pairs that ran "
    "different plans are flagged. The run writes those plans to the T2 plan library.",
)


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
    def __init__(self, reason: str, detail: str = "", *, cost_usd: float = 0.0) -> None:
        super().__init__(reason)
        #: what the arm in progress had already spent when the run stopped (it counts against the cap)
        self.reason, self.detail, self.cost_usd = reason, detail, cost_usd


def _route_counts(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the ``search_topic_route`` events one arm logged."""
    out: dict[str, Any] = {k: sum(int(e.get(k) or 0) for e in events) for k in ROUTE_COUNTS}
    out["events"] = len(events)
    out["unrouted_reasons"] = sorted({e["unrouted_reason"] for e in events if e.get("unrouted_reason")})
    return out


def distance_order(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ascending by ``distance``; chunks with none keep their envelope order after the rest."""
    keyed = [(c.get("distance") is None, c.get("distance") or 0.0, i) for i, c in enumerate(chunks)]
    return [chunks[i] for _none, _d, i in sorted(keyed)]


def _answer_arm(q: dict[str, Any], arm: str, *, answer_fn: Callable, catalog: Any, scope: str | None,
                call_budget_usd: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"budget_usd": call_budget_usd}
    if scope:
        kwargs["scope"] = scope
    t0 = time.monotonic()
    with arm_setting(arm), structlog.testing.capture_logs() as logs:
        try:
            envelope = asyncio.run(answer_fn(question=q["text"], structured=True, trace=False, **kwargs))
        except Exception as e:  # noqa: BLE001 — its cost is unknown, so the run stops and says why
            raise _Stop("answer_error", f"{q['qid']} {arm}: {type(e).__name__}: {e}") from e
    elapsed = time.monotonic() - t0
    if not isinstance(envelope, dict):
        raise _Stop("answer_error", f"{q['qid']} {arm}: non-envelope {type(envelope).__name__}")
    cost = envelope.get("cost_usd")
    steps = [s for s in envelope.get("steps") or [] if isinstance(s, dict)]
    if cost is None or any(s.get("cost_usd") is None for s in steps):
        raise _Stop("cost_unknown", f"{q['qid']} {arm}: the envelope or one of its steps has no cost_usd",
                    cost_usd=float(cost) if cost is not None else 0.0)
    try:
        return _score_arm(q, envelope, float(cost), elapsed, logs, catalog)
    except Exception as e:  # noqa: BLE001 — the call was paid for: stop, and count it
        raise _Stop("driver_error", f"{q['qid']} {arm}: {type(e).__name__}: {e}", cost_usd=float(cost)) from e


def _score_arm(q: dict[str, Any], envelope: dict[str, Any], cost: float, elapsed: float, logs: list[dict[str, Any]],
               catalog: Any) -> dict[str, Any]:
    chunks = [c for c in envelope.get("chunks") or [] if isinstance(c, dict)]
    out: dict[str, Any] = {
        "cost_usd": cost, "chunk_count": len(chunks), "error": None, "elapsed_s": elapsed,
        "plan_id": envelope.get("plan_id"), "step_count": envelope.get("step_count"),
        "budget_exhausted_at_step": envelope.get("budget_exhausted_at_step"),
        "route": _route_counts([e for e in logs if e.get("event") == ROUTE_EVENT]),
    }
    if out["budget_exhausted_at_step"] is not None:
        out["error"] = f"budget exhausted at step {out['budget_exhausted_at_step']}: a partial run is not scored"
        return out
    if not chunks:
        out["error"] = "no chunks: the plan retrieved nothing (a failed run is not scored as a loss)"
        return out
    rows = [{"chash": c.get("chash", ""), "collection": c.get("collection", ""), "distance": c.get("distance")}
            for c in chunks]
    if rows:
        paths = _resolve_paths_via_catalog(catalog, [r["chash"] for r in rows])
        if not any(paths.get(r["chash"]) for r in rows):
            out["error"] = "vacuous: every chunk resolved to an empty path"
            return out
    grade = {"owner": q.get("owner"), "exact": bool(q.get("exact"))}
    out[PRIMARY] = score(distance_order(rows), q["gt"], catalog, **grade)
    out[SECONDARY] = score(rows, q["gt"], catalog, **grade)
    out["ndcg3"], out["recall10"] = out[PRIMARY]["ndcg3"], out[PRIMARY]["recall10"]
    return out


def ordered(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The generic-answer questions first, every class otherwise in file order."""
    return [q for q in queries if q["kind"] == "generic"] + [q for q in queries if q["kind"] != "generic"]


def run(queries: list[dict[str, Any]], *, answer_fn: Callable, catalog: Any, cap_usd: float, scope: str | None,
        call_budget_usd: float = 1.0, done: list[dict[str, Any]] | None = None, spent_usd: float = 0.0,
        checkpoint: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    # a resumed run keeps its paired rows and runs an unpaired question again
    rows: list[dict[str, Any]] = [r for r in (done or []) if r.get("paired")]
    finished = {r["qid"] for r in rows}
    spent = spent_usd
    largest = max((r[a]["cost_usd"] for r in rows for a in ARMS), default=0.0)
    stopped: str | None = None
    detail = ""

    def state() -> dict[str, Any]:
        return {"rows": rows, "spent_usd": spent, "cap_usd": cap_usd, "call_budget_usd": call_budget_usd,
                "stopped": stopped, "stop_detail": detail}

    try:
        for i, q in enumerate(ordered(queries)):
            if q["qid"] in finished:
                continue
            need = 2 * max(largest, call_budget_usd)
            if spent + need > cap_usd + _EPS:
                raise _Stop("cap", f"spent {spent:.4f} of {cap_usd}; the next question could cost {need:.4f}")
            arms = ARMS if i % 2 == 0 else tuple(reversed(ARMS))
            row: dict[str, Any] = {"qid": q["qid"], "kind": q["kind"], "order": list(arms)}
            for arm in arms:
                res = _answer_arm(q, arm, answer_fn=answer_fn, catalog=catalog, scope=scope,
                                  call_budget_usd=call_budget_usd)
                spent += res["cost_usd"]
                largest = max(largest, res["cost_usd"])
                row[arm] = res
            row["paired"] = all(row[a]["error"] is None for a in ARMS)
            row["same_plan"] = row["off"]["plan_id"] == row["top1"]["plan_id"]
            rows.append(row)
            if (len(rows) == EARLY_VACUITY_QUESTIONS and not done
                    and all(r["top1"]["route"]["routed"] == 0 for r in rows)):
                raise _Stop("vacuous_early", f"the first {len(rows)} questions' top1 arms routed no collection")
            if checkpoint is not None:
                checkpoint(state())
            print(  # noqa: T201 — progress line of a bench script
                q["qid"], q["kind"], {a: row[a].get("ndcg3") for a in ARMS},
                f"routed={row['top1']['route']['routed']}", f"spent={spent:.4f}", file=sys.stderr, flush=True,
            )
    except _Stop as s:
        stopped, detail = s.reason, s.detail
        spent += s.cost_usd
    except Exception as e:  # noqa: BLE001 — keep what was paid for; the run stops and says why
        stopped, detail = "driver_error", f"{type(e).__name__}: {e}"
    return state()


def bootstrap_ci(deltas: list[float], *, seed: int = BOOTSTRAP_SEED, n: int = BOOTSTRAP_N) -> list[float] | None:
    """A 95% percentile interval of the mean, seeded."""
    if len(deltas) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(n))
    return [means[int(0.025 * n)], means[int(0.975 * n) - 1]]


def _split(sel: list[dict[str, Any]], key: str = PRIMARY) -> dict[str, Any]:
    if not sel:
        return {"n": 0}
    d = [r["top1"][key]["ndcg3"] - r["off"][key]["ndcg3"] for r in sel]
    return {
        "n": len(sel),
        **{a: {"ndcg3": statistics.mean(r[a][key]["ndcg3"] for r in sel),
               "recall10": statistics.mean(r[a][key]["recall10"] for r in sel)} for a in ARMS},
        "mean_delta_ndcg3": statistics.mean(d), "ci95_mean_delta": bootstrap_ci(d),
        "wins": sum(x > _EPS for x in d), "losses": sum(x < -_EPS for x in d),
    }


def _rule_rows(sel: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return [{"kind": r["kind"], "off": r["off"][key]["ndcg3"], "top1": r["top1"][key]["ndcg3"]} for r in sel]


def summarize(rows: list[dict[str, Any]], *, stopped: str | None = None) -> dict[str, Any]:
    paired = [r for r in rows if r.get("paired")]
    routed = [r for r in paired if r["top1"]["route"]["routed"] > 0]
    total_routed = sum(r["top1"]["route"]["routed"] for r in rows if "route" in r.get("top1", {}))
    vacuous = bool(rows) and total_routed == 0
    rule = pass_rule(_rule_rows(paired, PRIMARY))
    errors = [r["qid"] for r in rows if not r.get("paired")]
    complete = stopped is None and not errors and not vacuous
    if vacuous:
        verdict = "vacuous: the top1 arms routed no collection"
    elif not complete:
        verdict = "incomplete: no verdict"
    elif rule["passed"]:
        verdict = "pass: default may become top1"
    elif len(routed) < MIN_ROUTED_QUESTIONS:
        verdict = f"inconclusive (diluted: {len(routed)} of {len(paired)} questions routed): default stays off"
    else:
        verdict = "fail: default stays off"
    return {
        "orderings": ORDERINGS, "primary": PRIMARY,
        "n": len(rows), "paired": len(paired), "errors": len(errors), "error_qids": errors,
        "n_routed_questions": len(routed), "top1_routed_collections": total_routed, "vacuous": vacuous,
        "plan_mismatch_qids": [r["qid"] for r in paired if not r.get("same_plan", True)],
        "by_kind": {k: _split([r for r in paired if r["kind"] == k]) for k in sorted({r["kind"] for r in paired})},
        "in_sample": _split([r for r in paired if r["kind"] in IN_SAMPLE_KINDS]),
        "out_of_sample": _split([r for r in paired if r["kind"] in OUT_OF_SAMPLE_KINDS]),
        "pass_rule": rule,
        "routed_fraction": f"{len(routed)} of {len(paired)}", "min_routed_questions": MIN_ROUTED_QUESTIONS,
        "verdict": verdict,
        "informational": {
            "secondary_ordering_rule": pass_rule(_rule_rows(paired, SECONDARY)),
            "routed_questions_only_rule": pass_rule(_rule_rows(routed, PRIMARY)),
            "elapsed_s_mean": {a: statistics.mean(r[a]["elapsed_s"] for r in paired) for a in ARMS} if paired else {},
        },
        "scope_notes": list(SCOPE_NOTES),
    }


def resume_mismatch(prior: dict[str, Any], head: dict[str, Any]) -> str:
    """Why *prior* is not the same measurement as one about to start with *head*, or ``""``."""
    if prior.get("queries") != head["queries"]:
        return "the question files differ from the recorded run (names or sha256)"
    if prior.get("scope") != head["scope"]:
        return "the scope differs from the recorded run"
    pm, hm = prior.get("manifest") or {}, head["manifest"]
    for key in ("git_sha", "host_class", "engine_build"):
        if key in hm and pm.get(key) != hm.get(key):
            return f"the manifest's {key} differs ({pm.get(key)!r} recorded, {hm.get(key)!r} now)"
    return ""


def _write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1))
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--queries", nargs="+", type=Path, default=[
        Path("bench/queries/spike_5q.yaml"), Path("bench/queries/golden_v1.yaml"),
        Path("bench/queries/golden_heldout_v1.yaml"), Path("bench/queries/generic_docs_v2.yaml")])
    ap.add_argument("--cap-usd", type=float, required=True, help="the spend cap for this run")
    ap.add_argument("--call-budget-usd", type=float, default=1.0, help="budget_usd every nx_answer call gets")
    ap.add_argument("--scope", default=None, help="nx_answer scope (default: none)")
    ap.add_argument("--limit", type=int, default=0, help="first N questions only (use 1 to project the cost)")
    ap.add_argument("--resume", action="store_true", help="continue the run recorded in --out")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cap_usd <= 0 or a.call_budget_usd <= 0:
        ap.error("--cap-usd and --call-budget-usd must be > 0")

    from bench.manifest import build_manifest  # noqa: PLC0415
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.mcp.core import nx_answer  # noqa: PLC0415

    # the search_topic_route event is info-level; capture_logs sees only what the level filter lets through
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    if not make_t3().supports_topic_route():
        print("refused before any paid call: the engine does not report vector_topic_route_supported",  # noqa: T201
              file=sys.stderr)
        return 2
    queries = ordered(load_queries(a.queries))
    if a.limit:
        queries = queries[: a.limit]
    done: list[dict[str, Any]] = []
    spent = 0.0
    head = {
        "manifest": build_manifest(Path.cwd()), "scope": a.scope,
        "queries": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a.queries},
        "per_collection_env": os.environ.get("NX_SEARCH_PER_COLLECTION"),
    }
    if a.resume:
        prior = json.loads(a.out.read_text())
        why = resume_mismatch(prior, head)
        if why:
            print(f"refused to resume: {why}", file=sys.stderr)  # noqa: T201
            return 2
        done, spent = prior["rows"], float(prior["spent_usd"])

    def checkpoint(state: dict[str, Any]) -> None:
        _write(a.out, {**head, **state, "summary": summarize(state["rows"], stopped="running")})

    result = run(queries, answer_fn=nx_answer, catalog=make_catalog_reader(), cap_usd=a.cap_usd, scope=a.scope,
                 call_budget_usd=a.call_budget_usd, done=done, spent_usd=spent, checkpoint=checkpoint)
    summary = summarize(result["rows"], stopped=result["stopped"])
    _write(a.out, {**head, **result, "summary": summary})
    print(json.dumps({"summary": summary, **{k: result[k] for k in ("spent_usd", "cap_usd", "stopped",  # noqa: T201
                                                                    "stop_detail")}}, indent=1))
    return 0 if summary["verdict"].startswith(("pass", "fail")) else 1


if __name__ == "__main__":
    sys.exit(main())
