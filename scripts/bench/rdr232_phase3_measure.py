# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement driver (nexus-jypmj.3.10): the golden set with the coverage boundary off and on.

Runs each golden request through ``nx_answer(structured=True, trace=True)`` twice, once with
``NX_ANSWER_COVERAGE=0`` (the baseline: no assessment, no repair) and once with ``=1``, and writes the numbers the
bead lists, from the structured envelopes (repair yield is not in the persisted summary, so it cannot come from a
replay over ``nx_answer_runs``):

* per assessment call, the reply class (support, neutral or contradict only, all rejected, no records, raised), per
  stream and split by single-facet versus multi-facet requests; the share of calls with zero accepted records; the
  neutral-only reply rate;
* the failed assessment outcome split into an unusable reply and a raised call; the unassessed rate by cause;
* how many runs entered repair, and repair yield split by facet origin (a ``whole_question`` facet replays the
  plan's own search, so near-zero yield there is expected and must be read apart);
* the conflicted-facet rate;
* cost and latency, coverage on versus off, and the deadline-skip incidence;
* the assessor's false-negative rate, spurious-contradicts rate and false-skip rate against the ORACLE labels
  (:mod:`bench.rdr232_oracle`), for the requests the oracle labels; ``todo`` requests are counted, not scored;
* for the ART RDR-084 request (G06), the MVV checklist facts: plan, coverage state, ledger, repair, persistence.

**This script spends money and writes telemetry when it runs live.** ``--live`` calls the real ``nx_answer`` (model
calls through ``claude -p``; run rows written to whatever tenant the environment names) and waits on Sam's go.
``--stub FILE`` replays a recorded envelope instead, with no model, no network and no tenant, which is what the unit
tests and a smoke run use. Exactly one of the two is required.

Not measured here (listed in the report's ``not_measured``): the continuation fallback's repay cost (it needs a
cut-and-resume run), ``terminal_completeness`` (needs per-facet addressed/disclosed judgments of the prose), and the
precision of any-support-wins beyond what the oracle labels cover.

Run from the repository root::

    PYTHONPATH=scripts uv run python -m bench.rdr232_phase3_measure --set golden --stub FILE --out OUT.json
    PYTHONPATH=scripts uv run python -m bench.rdr232_phase3_measure --set both --live --engine 0.1.167 --out OUT.json
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

_BENCH_PARENT = _Path(__file__).resolve().parent.parent
if str(_BENCH_PARENT) not in _sys.path:
    _sys.path.insert(0, str(_BENCH_PARENT))

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click

from bench.answer_shape_metrics import Measure, NotMeasurable, compute_measures
from bench.rdr232_envelope import envelope_facts, observation_from_envelope
from bench.rdr232_facet_map import DEFAULT_MAP_PATH, FacetResolution, load_facet_map, resolve_facets
from bench.rdr232_oracle import (
    DEFAULT_ORACLE_PATH,
    GOLDEN_PATH,
    HELDOUT_PATH,
    DocPathOf,
    OracleSet,
    aggregate,
    load_oracle,
    score_run,
)
from bench.schema import Query, load_golden_set

COVERAGE_ENV = "NX_ANSWER_COVERAGE"
ARMS: tuple[str, ...] = ("off", "on")
ARM_VALUE = {"off": "0", "on": "1"}
#: The reply classes, duplicated from ``coverage_ledger.REPLY_CLASSES`` so the module imports nothing from ``nexus``.
REPLY_CLASSES: tuple[str, ...] = ("support", "neutral_or_contradict", "all_rejected", "no_records", "raised")
MVV_QID = "G06-synthesis-art-rdr084"

NOT_MEASURED: tuple[str, ...] = (
    "continuation_fallback_repay_cost: needs a run cut at the terminal reduction and resumed; this driver makes headless calls only",
    "terminal_completeness: needs per-facet addressed or disclosed judgments of the prose, which no label or judge supplies",
    "any_support_wins_precision: only for the facets the oracle labels, in oracle.spurious_contradicts",
    "engine_old_and_cached_capability_rollback_legs: simulated by tests/db/test_rdr232_run_summary_integration.py, not live",
)

#: ``(query, arm, scope, bindings) -> the structured envelope``. The live caller sets the coverage switch itself.
Caller = Callable[..., Awaitable[Mapping[str, Any]]]


@dataclass
class ArmRun:
    qid: str
    arm: str
    wall_ms: int
    facts: dict[str, Any] = field(default_factory=dict)
    envelope: Mapping[str, Any] | None = None
    error: str = ""


# ── what to call ─────────────────────────────────────────────────────────────────────────────────────────


def call_shape(q: Query, owners: Mapping[str, str] | None = None) -> tuple[str, dict[str, Any]]:
    """The ``scope`` and ``bindings`` a caller gives ``nx_answer`` for golden request *q*: its own scope (an ``rdr`` scope
    also carries ``content_type=rdr``, as the recorded ART call did) and the reserved source bindings for its
    ``required_sources``. *owners* maps a project (``ART``) to an owner tumbler (``1.2``); a project with no mapping
    travels as the ``project`` binding. A label no ``SourceRef`` names (a paper title) is not bound."""
    from nexus.plans.evidence import SourceRef, SourceRefError  # noqa: PLC0415 — deferred; keeps module import cheap

    bindings: dict[str, Any] = {"content_type": "rdr"} if q.scope == "rdr" else {}
    sources: list[str] = []
    qualifiers: set[str] = set()
    for label in q.required_sources:
        try:
            ref, project = SourceRef.parse_labeled(label)
        except SourceRefError:
            continue
        sources.append(ref.value)
        if project:
            qualifiers.add(project)
    if sources:
        bindings["source"] = sources[0] if len(sources) == 1 else sources
    if len(qualifiers) == 1:
        (project,) = qualifiers
        owner = (owners or {}).get(project)
        bindings["owner" if owner else "project"] = owner or project
    return q.scope, bindings


def live_caller() -> Caller:
    """The real ``nx_answer``, one arm at a time. Imports ``nexus.mcp.core`` only here."""
    from nexus.mcp import core  # noqa: PLC0415 — heavy; a live run only

    async def call(q: Query, *, arm: str, scope: str, bindings: Mapping[str, Any]) -> Mapping[str, Any]:
        saved = os.environ.get(COVERAGE_ENV)
        os.environ[COVERAGE_ENV] = ARM_VALUE[arm]
        try:
            out = await core.nx_answer(
                question=q.text, scope=scope, bindings=dict(bindings) or None, structured=True, trace=True,
            )
        finally:
            if saved is None:
                os.environ.pop(COVERAGE_ENV, None)
            else:
                os.environ[COVERAGE_ENV] = saved
        if not isinstance(out, Mapping):
            raise TypeError(f"nx_answer(structured=True) returned {type(out).__name__}, not an envelope")
        return out

    return call


def catalog_doc_paths(tumblers: Sequence[str]) -> DocPathOf:
    """A document-path resolver over the catalog (one read-only ``resolve_many``), for the oracle's
    ``doc_path_contains`` labels: an anchor carries a tumbler, not a path. Live runs only."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — a live run only

    with HttpCatalogClient() as cat:
        entries = cat.resolve_many(sorted({str(t) for t in tumblers if t}))
    paths = {str(k): str(getattr(v, "file_path", "") or "") for k, v in entries.items()}
    return lambda anchor: paths.get(str(anchor.get("tumbler") or ""), "")


def stub_caller(path: Path) -> Caller:
    """Replays the recorded envelope at *path* for every request: the ``on`` arm as recorded, the ``off`` arm as the
    same envelope with the coverage object replaced by the switch-off one. No model, no network, no tenant."""
    recorded = json.loads(path.read_text(encoding="utf-8"))

    async def call(q: Query, *, arm: str, scope: str, bindings: Mapping[str, Any]) -> Mapping[str, Any]:  # noqa: ARG001
        env = json.loads(json.dumps(recorded))
        if arm == "off":
            env["coverage"] = {"state": "unassessed", "reason": "switch_off", "skip_generation": False, "facets_required": 0,
                               "facets": {}, "anchors": [], "findings": [], "assessment": {}, "repair": {"outcome": "not_attempted"}}
            env["steps"] = [s for s in env.get("steps") or [] if s.get("operator") != "operator_check"]
        return env

    return call


# ── running ──────────────────────────────────────────────────────────────────────────────────────────────


async def run_arm(
    queries: Sequence[Query], caller: Caller, arm: str, *, owners: Mapping[str, str] | None = None,
    clock: Callable[[], float] = time.monotonic, repeat: int = 1, keep_envelopes: bool = False,
) -> list[ArmRun]:
    """Every query *repeat* times under *arm*, in order. A call that raises is recorded as an error, not hidden."""
    runs: list[ArmRun] = []
    for q in queries:
        scope, bindings = call_shape(q, owners)
        for _ in range(max(1, repeat)):
            started = clock()
            try:
                env = await caller(q, arm=arm, scope=scope, bindings=bindings)
                err = ""
            except Exception as exc:  # noqa: BLE001 — one failed request must not end the sweep
                env, err = None, type(exc).__name__
            wall = int((clock() - started) * 1000)
            runs.append(ArmRun(
                q.qid, arm, wall, facts=envelope_facts(env) if env is not None else {},
                envelope=env if keep_envelopes else None, error=err,
            ))
    return runs


# ── summarising ──────────────────────────────────────────────────────────────────────────────────────────


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))]


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "mean": statistics.fmean(values) if values else None, "p50": _pct(values, 0.5), "p90": _pct(values, 0.9)}


def _share(num: int, den: int) -> float | None:
    return None if den == 0 else num / den


def _call_tables(runs: Sequence[ArmRun]) -> dict[str, Any]:
    """Reply classes over every assessment call: all, per stream, and split by single versus multi-facet request."""
    total: Counter[str] = Counter()
    by_stream: dict[str, Counter[str]] = {}
    by_shape: dict[str, Counter[str]] = {"single_facet_request": Counter(), "multi_facet_request": Counter()}
    for r in runs:
        shape = "single_facet_request" if r.facts.get("facets_required") == 1 else "multi_facet_request"
        for stream, _facets, reply, *_ in r.facts.get("call_log") or []:
            total[reply] += 1
            by_stream.setdefault(stream, Counter())[reply] += 1
            by_shape[shape][reply] += 1

    def table(c: Counter[str]) -> dict[str, Any]:
        calls, answered = sum(c.values()), sum(c[k] for k in REPLY_CLASSES if k != "raised")
        return {
            "calls": calls, **{k: c[k] for k in REPLY_CLASSES},
            "zero_accepted_share": _share(c["all_rejected"] + c["no_records"], answered),
            "neutral_only_share": _share(c["neutral_or_contradict"], answered),
            "raised_share": _share(c["raised"], calls),
        }

    return {
        "all": table(total), "by_stream": {s: table(c) for s, c in by_stream.items()},
        "by_request_shape": {k: table(c) for k, c in by_shape.items()},
    }


def summarize_arm(
    runs: Sequence[ArmRun], *, queries: Mapping[str, Query], resolutions: Mapping[str, FacetResolution],
    oracle: OracleSet | None, doc_path_of: DocPathOf | None = None,
) -> dict[str, Any]:
    ok = [r for r in runs if not r.error]
    assessed = [r for r in ok if r.facts.get("assessment_calls")]
    causes: Counter[str] = Counter()
    for r in ok:
        causes.update(r.facts.get("unassessed_by_cause") or {})
    facet_total = sum(r.facts.get("facets_required") or 0 for r in ok)
    covered = sum((r.facts.get("facets") or {}).get("covered", 0) for r in ok)
    entered = [r for r in ok if r.facts.get("repair_entered")]
    origin: dict[str, Counter[str]] = {}
    for r in ok:
        for name, row in (r.facts.get("repair_by_origin") or {}).items():
            origin.setdefault(name, Counter()).update(row)
    cost = [r.facts["cost_usd"] for r in ok]
    wall = [float(r.wall_ms) for r in ok]
    skip_incidence = sum(
        1 for r in ok
        if (r.facts.get("unassessed_by_cause") or {}).get("deadline_skip") or r.facts.get("assessment_outcome") == "skipped_deadline"
    )
    summary: dict[str, Any] = {
        "runs": len(runs), "errors": Counter(r.error for r in runs if r.error), "assessed_runs": len(assessed),
        "states": Counter(str(r.facts.get("state")) for r in ok), "answer_shapes": Counter(str(r.facts.get("answer_shape")) for r in ok),
        "assessment_outcomes": Counter(str(r.facts.get("assessment_outcome")) for r in assessed),
        "failure_causes": Counter(str(r.facts.get("failure_cause")) for r in assessed),
        "assessment_calls": _call_tables(ok),
        "unassessed": {
            "facets_by_cause": {c: causes.get(c, 0) for c in ("unusable_reply", "deadline_skip", "budget_skip", "raised_call", "other")},
            "facet_share": _share(sum(causes.values()), facet_total),
            "runs_with_unassessed": sum(1 for r in ok if sum((r.facts.get("unassessed_by_cause") or {}).values())),
        },
        "repair": {
            "runs_entered": len(entered), "outcomes": Counter(str(r.facts.get("repair_outcome")) for r in entered),
            "by_facet_origin": {
                name: {"targeted": row["targeted"], "covered_after": row["covered_after"], "yield": _share(row["covered_after"], row["targeted"])}
                for name, row in origin.items()
            },
        },
        "conflicted": {
            "facets_conflicted": sum(r.facts.get("facets_conflicted") or 0 for r in ok), "facets_covered": covered,
            "share_of_covered": _share(sum(r.facts.get("facets_conflicted") or 0 for r in ok), covered),
        },
        "cost_usd": _stats(cost), "assessment_usd": {"total": round(sum(r.facts["assessment_usd"] for r in ok), 6),
                                                       **_stats([r.facts["assessment_usd"] for r in assessed])},
        "latency_ms": _stats(wall), "assessment_latency_ms": _stats([float(r.facts["assessment_latency_ms"]) for r in assessed]),
        "deadline_skip_incidence": {"runs": skip_incidence, "share": _share(skip_incidence, len(ok))},
        "persistence": Counter(str(r.facts.get("persistence")) for r in ok),
    }
    if oracle is not None:
        from bench.rdr232_oracle import RunScore  # noqa: PLC0415 — type only

        scores: list[RunScore] = []
        for r in assessed:
            o = oracle.requests.get(r.qid)
            res = resolutions.get(r.qid)
            if o is None or o.todo or res is None or r.envelope is None:
                continue
            scores.append(score_run(o, r.envelope.get("coverage") or {}, res, doc_path_of))
        summary["oracle"] = {**aggregate(scores), "runs_scored": len(scores), "runs_assessed": len(assessed)}
    return summary


def compare_arms(off: Sequence[ArmRun], on: Sequence[ArmRun]) -> dict[str, Any]:
    """Coverage on versus off, paired by position (the same queries in the same order)."""
    pairs = [(a, b) for a, b in zip(off, on, strict=False) if not a.error and not b.error and a.qid == b.qid]

    def delta(values: Sequence[float]) -> dict[str, Any]:
        return {"n": len(values), "mean": statistics.fmean(values) if values else None, "p50": _pct(values, 0.5), "p90": _pct(values, 0.9)}

    return {
        "pairs": len(pairs),
        "cost_usd_delta": delta([b.facts["cost_usd"] - a.facts["cost_usd"] for a, b in pairs]),
        "latency_ms_delta": delta([float(b.wall_ms - a.wall_ms) for a, b in pairs]),
        "answer_shape_changed": sum(1 for a, b in pairs if a.facts.get("answer_shape") != b.facts.get("answer_shape")),
        "answer_withheld_only_with_coverage_on": sum(
            1 for a, b in pairs if b.facts.get("answer_shape") in ("uncovered", "insufficient_evidence") and a.facts.get("answer_shape") == "answered"
        ),
    }


def mvv_facts(runs: Sequence[ArmRun]) -> dict[str, Any] | None:
    """The ART RDR-084 MVV checklist facts for the ``on`` arm's G06 run(s): what to look at, not a verdict."""
    mine = [r for r in runs if r.qid == MVV_QID and not r.error and r.envelope is not None]
    if not mine:
        return None
    r = mine[0]
    env = r.envelope or {}
    return {
        "plan_id": env.get("plan_id"), "answer_shape": r.facts["answer_shape"], "coverage_state": r.facts["state"],
        "coverage_reason": r.facts["reason"], "findings": [dict(f) for f in (env.get("coverage") or {}).get("findings") or []],
        "residual_gaps": (env.get("coverage") or {}).get("residual_gaps"), "repair_outcome": r.facts["repair_outcome"],
        "code_anchor_share": r.facts["code_anchor_share"], "corpus_counts": (env.get("coverage") or {}).get("corpus_counts"),
        "persistence": r.facts["persistence"], "returned_coverage_state": r.facts["returned_coverage_state"],
        "persisted_coverage_state": r.facts["persisted_coverage_state"], "wall_ms": r.wall_ms,
    }


def config_echo() -> dict[str, Any]:
    """The constants this measurement is a measurement OF, read from the code under test."""
    from nexus.plans import coverage_ledger as cl  # noqa: PLC0415 — deferred; keeps module import cheap
    from nexus.plans import coverage_repair as cr  # noqa: PLC0415

    return {
        "MAX_FACETS_PER_ASSESSMENT_CALL": cl.MAX_FACETS_PER_ASSESSMENT_CALL,
        "ASSESSMENT_STAGE_DEADLINE_SHARE": cl.ASSESSMENT_STAGE_DEADLINE_SHARE, "ASSESSMENT_CALL_TIMEOUT_S": cl.ASSESSMENT_CALL_TIMEOUT_S,
        "MAX_REPAIR_QUERIES": cr.MAX_REPAIR_QUERIES, "MAX_REPAIR_ITEMS": cr.MAX_REPAIR_ITEMS,
        "REPAIR_DEADLINE_SHARE": cr.REPAIR_DEADLINE_SHARE, "REPAIR_ALLOWANCE_CAP_S": cr.REPAIR_ALLOWANCE_CAP_S,
    }


def _measures_json(measures: Mapping[str, Measure]) -> dict[str, Any]:
    return {
        n: {"value": str(m.value) if isinstance(m.value, NotMeasurable) else m.value, "n": m.n, "detail": m.detail}
        for n, m in measures.items()
    }


def build_report(
    queries: Sequence[Query], off: Sequence[ArmRun], on: Sequence[ArmRun], *, resolutions: Mapping[str, FacetResolution],
    oracle: OracleSet | None, doc_path_of: DocPathOf | None = None, meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    by_qid = {q.qid: q for q in queries}
    obs = [
        observation_from_envelope(r.qid, r.envelope, oracle=(oracle.requests.get(r.qid) if oracle else None), doc_path_of=doc_path_of)
        for r in on if r.envelope is not None and not r.error
    ]
    unresolved = {qid: {"unmapped_fids": list(r.unmapped_fids), "unmapped_slugs": list(r.unmapped_slugs)}
                  for qid, r in resolutions.items() if not r.complete}
    return {
        "meta": dict(meta or {}), "config": config_echo(),
        "arms": {
            "off": summarize_arm(off, queries=by_qid, resolutions=resolutions, oracle=None),
            "on": summarize_arm(on, queries=by_qid, resolutions=resolutions, oracle=oracle, doc_path_of=doc_path_of),
        },
        "compare_on_vs_off": compare_arms(off, on),
        "answer_shape_measures_on": _measures_json(compute_measures(list(queries), obs)) if obs else {},
        "facet_map": {"requests_resolved": len(resolutions), "requests_incomplete": len(unresolved), "incomplete": unresolved},
        "oracle_labels": oracle.summary() if oracle else None,
        "mvv_g06": mvv_facts(on),
        "not_measured": list(NOT_MEASURED),
    }


# ── cli ──────────────────────────────────────────────────────────────────────────────────────────────────


def _load_queries(which: str, qids: Sequence[str], limit: int | None) -> list[Query]:
    queries: list[Query] = []
    if which in ("golden", "both"):
        queries += load_golden_set(GOLDEN_PATH)
    if which in ("heldout", "both"):
        queries += load_golden_set(HELDOUT_PATH)
    if qids:
        queries = [q for q in queries if q.qid in set(qids)]
    return queries[:limit] if limit else queries


def _resolutions(all_queries: Sequence[Query], on: Sequence[ArmRun], map_path: Path) -> dict[str, FacetResolution]:
    entries = load_facet_map(map_path, all_queries)
    by_qid = {q.qid: q for q in all_queries}
    out: dict[str, FacetResolution] = {}
    for r in on:
        if r.envelope is None or r.qid in out:
            continue
        out[r.qid] = resolve_facets(by_qid[r.qid], (r.envelope.get("request_coverage") or {}).get("facets") or [], entries.get(r.qid, ()))
    return out


async def _run(args: argparse.Namespace, caller: Caller | None, clock: Callable[[], float], doc_path_of: DocPathOf | None) -> int:
    queries = _load_queries(args.set, args.qid or [], args.limit)
    if not queries:
        click.echo("FAIL: no requests selected; a measurement of nothing proves nothing", err=True)
        return 2
    if caller is None:
        if args.stub:
            caller = stub_caller(args.stub)
        elif args.live:
            click.echo("LIVE: model calls and nx_answer_runs telemetry writes against the configured tenant", err=True)
            caller = live_caller()
        else:
            click.echo("FAIL: pass --live (real nx_answer; waits on Sam's go) or --stub FILE (a recorded envelope)", err=True)
            return 2
    owners = dict(kv.split("=", 1) for kv in args.owner or [])
    arms = {a: await run_arm(queries, caller, a, owners=owners, clock=clock, repeat=args.repeat, keep_envelopes=True) for a in ARMS}
    all_queries = [*load_golden_set(GOLDEN_PATH), *load_golden_set(HELDOUT_PATH)]
    oracle = None if args.no_oracle else load_oracle(args.oracle, all_queries)
    if doc_path_of is None and args.live and not args.stub and oracle is not None:
        doc_path_of = catalog_doc_paths([
            a.get("tumbler") for r in arms["on"] if r.envelope for a in (r.envelope.get("coverage") or {}).get("anchors") or []
        ])
    resolutions = _resolutions(all_queries, arms["on"], args.facet_map)
    report = build_report(
        queries, arms["off"], arms["on"], resolutions=resolutions, oracle=oracle, doc_path_of=doc_path_of,
        meta={"set": args.set, "requests": len(queries), "repeat": args.repeat, "engine": args.engine, "mode": "live" if args.live and not args.stub else "stub",
              "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "owners": owners},
    )
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        click.echo(f"wrote {args.out}: {len(queries)} requests x {len(ARMS)} arms", err=True)
    else:
        click.echo(text)
    if args.envelopes_out:
        args.envelopes_out.write_text(json.dumps({a: [r.envelope for r in runs] for a, runs in arms.items()}, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


def main(
    argv: list[str] | None = None, *, caller: Caller | None = None, clock: Callable[[], float] = time.monotonic,
    doc_path_of: DocPathOf | None = None,
) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", default="golden", choices=("golden", "heldout", "both"))
    ap.add_argument("--live", action="store_true", help="call the real nx_answer (model calls, telemetry writes); waits on Sam's go")
    ap.add_argument("--stub", type=Path, help="replay this recorded envelope (JSON) instead of calling nx_answer")
    ap.add_argument("--qid", action="append", help="only this request (repeatable), e.g. a canary of one")
    ap.add_argument("--limit", type=int, help="only the first N selected requests")
    ap.add_argument("--repeat", type=int, default=1, help="calls per request per arm")
    ap.add_argument("--owner", action="append", help="PROJECT=TUMBLER owner binding, e.g. ART=1.2 (repeatable)")
    ap.add_argument("--engine", default="", help="the engine version the run is against, recorded in the report")
    ap.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE_PATH, help="oracle label file")
    ap.add_argument("--no-oracle", action="store_true", help="skip assessor scoring against the oracle")
    ap.add_argument("--facet-map", type=Path, default=DEFAULT_MAP_PATH)
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--envelopes-out", type=Path, help="also write every envelope (contains answer text) to this file")
    args = ap.parse_args(argv)
    return asyncio.run(_run(args, caller, clock, doc_path_of))


if __name__ == "__main__":
    raise SystemExit(main())
