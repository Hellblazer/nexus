# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): measure the optional model-backed request extraction.

``NX_ANSWER_STRUCTURED_EXTRACT`` (default on since this bead; this script still sets it itself) lets ``nx_answer`` make at most one schema-closed ``claude -p``
call per request when the deterministic first pass left the request open. This script measures what that call
buys: how many requests it resolves, how often it is wrong against a frozen label, what it costs and how long
it takes, and (for recorded runs) how the eligibility gate's verdicts move when the extracted kinds are used.

**Same code path as ``nx_answer``.** The real extractor calls ``nexus.mcp.core._nx_answer_request_coverage``
itself with the setting turned on, so the deterministic pass, the send condition
(:func:`~nexus.plans.request_coverage.needs_structured_extract`: kind ``unknown`` OR clause structure
uncertain), the model pin, the default timeout and the outcome classification are the production ones. A
request whose kind is known is therefore NOT always unsent: it is sent when its facets are uncertain. Only a
request whose kind is ``unknown`` can have its kind changed (the production rule); the report lists the two
send classes apart (``kind_unknown`` and ``facets_uncertain``).

Three sets, one report each (``--set``): ``golden`` (``bench/queries/golden_v1.yaml``, IN-SAMPLE: the
classifier was tuned on it), ``heldout`` (``bench/queries/golden_heldout_v1.yaml``, written blind, labels
frozen) and ``real`` (recorded ``nx_answer`` runs through :mod:`bench.rdr232_replay`). A fourth, ``compound``
(``bench/queries/golden_compound_lookup_v1.yaml``, nexus-jypmj.13), is the class probe for the lookup boundary:
six lookups that each ask two connected facts of one passage of one source, IN-SAMPLE (written with the
boundary in view). A fifth, ``near_miss`` (``bench/queries/golden_near_miss_synthesis_v1.yaml``), is its control: six
one-source synthesis requests whose parts live in different sections, also IN-SAMPLE.

Bounds: one extraction per DISTINCT question, at most ``MAX_CONCURRENCY`` (2) in flight, and the run stops
sending after more than ``MAX_CONSECUTIVE_FAILURES`` (10) consecutive failed calls (``timeout`` or ``error``);
what was not sent is counted as ``aborted_not_sent``. Read-only against the tenant: nothing here writes, and
``NX_ALLOW_PROD_WRITE`` is refused if present in the environment. Question text is user data: the report
carries qids, run ids, kinds and counts only; ``--show-questions`` prints text to stderr for local reading.

Run as a module from the repo root: ``PYTHONPATH=scripts uv run python -m bench.rdr232_extract_measure --set heldout --out F``.

Exit status: 0 on a report, 2 when the set is empty or a prerequisite fails, 1 on a read failure.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import json
import math
import os
import statistics
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import structlog

from bench import rdr232_replay as replay

_log = structlog.get_logger(__name__)

REPO = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO / "bench" / "queries" / "golden_v1.yaml"
HELDOUT_PATH = REPO / "bench" / "queries" / "golden_heldout_v1.yaml"
COMPOUND_PATH = REPO / "bench" / "queries" / "golden_compound_lookup_v1.yaml"
NEAR_MISS_PATH = REPO / "bench" / "queries" / "golden_near_miss_synthesis_v1.yaml"
#: The labelled sets ``--set`` reads (``real`` is the recorded runs, not a file). ``compound`` is the class probe
#: of nexus-jypmj.13: six lookups that each ask two connected facts of one passage of one source. ``near_miss`` is its
#: control (critique S7): six synthesis requests that each name one source and ask for an account spread over its
#: sections, which a boundary drawn too wide would call lookups.
SET_PATHS: dict[str, Path] = {"golden": GOLDEN_PATH, "heldout": HELDOUT_PATH, "compound": COMPOUND_PATH, "near_miss": NEAR_MISS_PATH}
#: Sets written with the classifier or the lookup boundary in view; their figures are floors, not rates.
IN_SAMPLE_SETS: frozenset[str] = frozenset({"golden", "compound", "near_miss"})

MAX_CONCURRENCY = 2
MAX_CONSECUTIVE_FAILURES = 10

#: Outcomes that count as a failed call for the stop rule. ``invalid`` is a model answer outside the schema
#: (the call itself worked) and does not.
FAILED_OUTCOMES: frozenset[str] = frozenset({"timeout", "error"})

#: Every outcome the report lists (zero included). ``not_sent`` is a request the first pass settled;
#: ``aborted_not_sent`` is a request that needed a call but the stop rule ended the run first.
OUTCOMES: tuple[str, ...] = (
    "ok", "unchanged", "invalid", "timeout", "error", "skipped_deadline", "disabled", "not_needed",
    "aborted_not_sent",
)

#: ``(question text) -> RequestCoverage`` after the (possibly skipped) extraction.
Extractor = Callable[[str], Awaitable[Any]]

_CURRENT_KEY: contextvars.ContextVar[str] = contextvars.ContextVar("rdr232_extract_key", default="")


@dataclass
class Probe:
    """One distinct question through the deterministic pass and the structured extraction."""

    key: str
    det_kind: str
    det_decision: str
    det_facets_certain: bool
    send_class: str  # kind_unknown | facets_uncertain | not_sent
    final_kind: str = ""
    final_decision: str = ""
    facets_certain_after: bool = False
    outcome: str = "not_needed"
    calls: int = 0
    latency_ms: int | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    run_ids: list[Any] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)  # qids carrying this question's text

    def kind_changed(self) -> bool:
        return self.final_kind != self.det_kind


# ── measurement ─────────────────────────────────────────────────────────────────────────────────────────


async def measure(
    questions: Mapping[str, str],
    extractor: Extractor,
    *,
    concurrency: int = MAX_CONCURRENCY,
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    max_calls: int | None = None,
    token_sink: Mapping[str, list[Any]] | None = None,
) -> tuple[dict[str, Probe], dict[str, Any]]:
    """Run the deterministic pass for every distinct question and the extraction for those that need it.

    *questions* maps a key to its text. *extractor* is the production path (or a fake with the same shape);
    it decides for itself whether a call is made. At most *concurrency* (clamped to ``MAX_CONCURRENCY``)
    calls are in flight. After more than *max_consecutive_failures* consecutive failed calls no further
    request that needs a call is sent (it is recorded ``aborted_not_sent``). *max_calls* (a canary bound)
    stops sending the same way once that many calls were made. Returns the probes and the run facts.
    """
    from nexus.plans.request_coverage import derive_request_coverage, needs_structured_extract  # noqa: PLC0415 — deferred; keeps module import cheap

    limit = max(1, min(concurrency, MAX_CONCURRENCY))
    probes: dict[str, Probe] = {}
    pending: list[str] = []
    for key, text in questions.items():
        det = derive_request_coverage(text)
        needs = needs_structured_extract(det)
        send_class = "not_sent" if not needs else ("kind_unknown" if det.answer_kind == "unknown" else "facets_uncertain")
        probes[key] = Probe(
            key=key, det_kind=det.answer_kind, det_decision=det.kind_decision,
            det_facets_certain=det.facets_certain, send_class=send_class,
            final_kind=det.answer_kind, final_decision=det.kind_decision, facets_certain_after=det.facets_certain,
        )
        pending.append(key)
    sem = asyncio.Semaphore(limit)
    state = {"consecutive": 0, "aborted": False, "calls": 0, "in_flight": 0, "max_in_flight": 0, "stop_reason": ""}

    async def one(key: str) -> None:
        probe = probes[key]
        async with sem:
            if probe.send_class != "not_sent":
                if state["aborted"]:
                    probe.outcome = "aborted_not_sent"
                    return
                if max_calls is not None and state["calls"] >= max_calls:
                    probe.outcome = "aborted_not_sent"
                    state["stop_reason"] = state["stop_reason"] or f"max_calls={max_calls}"
                    return
                state["calls"] += 1  # reserved before the await, so the bound holds under concurrency
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            token = _CURRENT_KEY.set(key)
            try:
                cov = await extractor(questions[key])
            finally:
                _CURRENT_KEY.reset(token)
                state["in_flight"] -= 1
            rep = cov.extraction
            probe.outcome, probe.calls = rep.outcome, rep.calls
            probe.latency_ms, probe.cost_usd = rep.latency_ms, rep.cost_usd
            probe.final_kind, probe.final_decision = cov.answer_kind, cov.kind_decision
            probe.facets_certain_after = cov.facets_certain
            if token_sink is not None:
                usage = token_sink.get(key, [])
                ins = [u.input_tokens for u in usage if getattr(u, "input_tokens", None) is not None]
                outs = [u.output_tokens for u in usage if getattr(u, "output_tokens", None) is not None]
                probe.input_tokens = sum(ins) if ins else None
                probe.output_tokens = sum(outs) if outs else None
            if probe.send_class != "not_sent":
                if rep.outcome in FAILED_OUTCOMES:
                    state["consecutive"] += 1
                    if state["consecutive"] > max_consecutive_failures:
                        state["aborted"] = True
                        state["stop_reason"] = f"more than {max_consecutive_failures} consecutive failed calls"
                else:
                    state["consecutive"] = 0

    await asyncio.gather(*(one(k) for k in pending))
    facts = {
        "concurrency_limit": limit, "max_in_flight_observed": state["max_in_flight"],
        "max_consecutive_failures": max_consecutive_failures, "aborted": state["aborted"] or bool(state["stop_reason"]),
        "stop_reason": state["stop_reason"], "extractor_calls_reserved": state["calls"],
    }
    return probes, facts


# ── the production extractor ────────────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def core_extractor(captured: dict[str, list[Any]]) -> Iterator[Extractor]:
    """The extractor ``nx_answer`` itself uses, with the setting on, tapping the dispatch's usage objects.

    Sets ``NX_ANSWER_STRUCTURED_EXTRACT`` for the life of the block (restored on exit) and wraps
    ``nexus.operators.dispatch.claude_dispatch`` so each call's usage objects (token counts) land in
    *captured* under the question key set by :func:`measure`. The wrapper passes everything through
    unchanged; the cost the report carries is the production ``ExtractionReport``'s own.
    """
    from nexus.mcp import core  # noqa: PLC0415 — heavy; deferred to a live extraction only
    from nexus.operators import dispatch as op  # noqa: PLC0415 — as above
    from nexus.plans.evidence import build_evidence_request  # noqa: PLC0415 — as above
    from nexus.plans.request_coverage import EXTRACT_ENV  # noqa: PLC0415 — as above

    real = op.claude_dispatch

    async def tapped(prompt: str, schema: dict[str, Any], *args: Any, usage_sink: list[Any] | None = None, **kw: Any) -> dict[str, Any]:
        sink: list[Any] = usage_sink if usage_sink is not None else []
        try:
            return await real(prompt, schema, *args, usage_sink=sink, **kw)
        finally:
            captured.setdefault(_CURRENT_KEY.get(), []).extend(sink)

    request = build_evidence_request(None, scope="", name_lookup=str)
    saved = os.environ.get(EXTRACT_ENV)

    async def extract(text: str) -> Any:
        return await core._nx_answer_request_coverage(
            question=text, scope="", evidence_request=request, explicit_kind=None, deadline=None, trace=False,
        )

    os.environ[EXTRACT_ENV] = "1"
    op.claude_dispatch = tapped
    try:
        yield extract
    finally:
        op.claude_dispatch = real
        if saved is None:
            os.environ.pop(EXTRACT_ENV, None)
        else:
            os.environ[EXTRACT_ENV] = saved


# ── summaries ───────────────────────────────────────────────────────────────────────────────────────────


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered) / 100))
    return float(ordered[min(rank, len(ordered)) - 1])


def kind_distribution(kinds: Iterable[str]) -> dict[str, Any]:
    c = Counter(kinds)
    n = sum(c.values())
    return {"n": n, "kinds": dict(sorted(c.items())), "unknown": c.get("unknown", 0),
            "unknown_rate": round(c.get("unknown", 0) / n, 4) if n else None}



def label_agreement(
    labelled: Sequence[tuple[str, str, str]],
) -> dict[str, Any]:
    """Agreement of predicted kinds with frozen labels. *labelled* is ``(qid, label_kind, predicted_kind)``.

    ``agree``: predicted equals the label (``unknown`` agrees with an ``unknown`` label).
    ``unknown_instead``: predicted ``unknown`` where the label names a kind.
    ``wrong_kind``: any other disagreement, including a kind predicted where the label is ``unknown``.
    """
    agree: list[str] = []
    unknown_instead: list[dict[str, str]] = []
    wrong: list[dict[str, str]] = []
    for qid, label, pred in labelled:
        if pred == label:
            agree.append(qid)
        elif pred == "unknown":
            unknown_instead.append({"qid": qid, "label": label})
        else:
            wrong.append({"qid": qid, "label": label, "predicted": pred})
    n = len(labelled)

    def rate(x: int) -> float | None:
        return round(x / n, 4) if n else None

    return {
        "n": n, "agree": len(agree), "agree_rate": rate(len(agree)),
        "unknown_instead": len(unknown_instead), "unknown_instead_qids": unknown_instead,
        "wrong_kind": len(wrong), "wrong_kind_qids": wrong,
    }


def extraction_stats(probes: Iterable[Probe]) -> dict[str, Any]:
    """What the extraction did, accounted by outcome (every distinct question lands in exactly one)."""
    ps = list(probes)
    outcomes: Counter[str] = Counter({o: 0 for o in OUTCOMES})
    by_class: dict[str, Counter[str]] = {c: Counter() for c in ("kind_unknown", "facets_uncertain", "not_sent")}
    for p in ps:
        outcomes[p.outcome] += 1
        by_class[p.send_class][p.outcome] += 1
    called = [p for p in ps if p.calls > 0]
    lat = [float(p.latency_ms) for p in called if p.latency_ms is not None]
    cost = [p.cost_usd for p in called if p.cost_usd is not None]
    ins = [p.input_tokens for p in called if p.input_tokens is not None]
    outs = [p.output_tokens for p in called if p.output_tokens is not None]
    resolved = [p for p in ps if p.det_kind == "unknown" and p.final_kind != "unknown"]
    return {
        "distinct_questions": len(ps),
        "needed_a_call": sum(1 for p in ps if p.send_class != "not_sent"),
        "send_classes": {c: sum(1 for p in ps if p.send_class == c) for c in by_class},
        "calls_made": sum(p.calls for p in ps),
        "outcomes": dict(outcomes),
        "outcomes_by_send_class": {c: dict(v) for c, v in by_class.items()},
        "outcomes_sum_equals_distinct": sum(outcomes.values()) == len(ps),
        "latency_ms": {
            "n": len(lat), "p50": percentile(lat, 50), "p95": percentile(lat, 95), "max": max(lat) if lat else None,
            "mean": round(statistics.fmean(lat), 1) if lat else None,
        },
        "cost_usd": {
            "n_reporting": len(cost), "total": round(sum(cost), 6) if cost else None,
            "mean_per_call": round(sum(cost) / len(cost), 6) if cost else None,
        },
        "tokens": {
            "n_reporting": len(ins), "input_total": sum(ins) if ins else None,
            "output_total": sum(outs) if outs else None,
        },
        "kind_unknown_resolved_to_a_kind": len(resolved),
        "kind_changed_without_ok_outcome": sum(1 for p in ps if p.kind_changed() and p.outcome != "ok"),
        "facets_made_certain": sum(1 for p in ps if not p.det_facets_certain and p.facets_certain_after),
    }


def _probe_row(p: Probe, extra: Mapping[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in asdict(p).items() if k not in ("key", "labels")}
    return {**extra, **row}


# ── per-set reports ─────────────────────────────────────────────────────────────────────────────────────


def labelled_report(
    set_name: str, queries: Sequence[Any], probes: Mapping[str, Probe], facts: Mapping[str, Any],
) -> dict[str, Any]:
    """The report for a labelled set (*queries* carry ``qid``, ``text`` and ``answer_kind``)."""
    from nexus.plans.request_coverage import normalize_kind  # noqa: PLC0415 — deferred; keeps module import cheap

    det: list[tuple[str, str, str]] = []
    ext: list[tuple[str, str, str]] = []
    detail: list[dict[str, Any]] = []
    for q in queries:
        label = normalize_kind(q.answer_kind) or q.answer_kind
        p = probes[replay.question_key(q.text)]
        det.append((q.qid, label, p.det_kind))
        ext.append((q.qid, label, p.final_kind))
        detail.append(_probe_row(p, {"qid": q.qid, "label": label}))
    probe_list = [probes[replay.question_key(q.text)] for q in queries]
    # a duplicated question text would share one probe; report each once for the stats
    unique = list({id(p): p for p in probe_list}.values())
    return {
        "set": set_name,
        "in_sample": set_name in IN_SAMPLE_SETS,
        "questions": len(queries), "distinct_questions": len(unique),
        "run": dict(facts),
        "deterministic": {
            "distribution": kind_distribution(k for _, _, k in det), "agreement": label_agreement(det),
        },
        "with_extraction": {
            "distribution": kind_distribution(k for _, _, k in ext), "agreement": label_agreement(ext),
        },
        "extraction": extraction_stats(unique),
        "questions_detail": detail,
    }


def real_report(
    rows: Sequence[Any], plans: Sequence[Mapping[str, Any]], probes: Mapping[str, Probe], facts: Mapping[str, Any],
) -> dict[str, Any]:
    """The report for recorded runs: the gate's verdicts on the deterministic kinds and on the extracted ones."""
    override = {k: p.final_kind for k, p in probes.items() if p.final_kind != p.det_kind}
    before = replay.classify_rows(rows, plans)
    after = replay.classify_rows(rows, plans, kind_override=override)
    b_runs = {d["run_id"]: d for d in before["runs"]}
    a_runs = {d["run_id"]: d for d in after["runs"]}
    flips_to_admit = sorted(r for r, d in a_runs.items() if d["admitted"] and not b_runs[r]["admitted"])
    flips_to_exclude = sorted(r for r, d in a_runs.items() if not d["admitted"] and b_runs[r]["admitted"])

    def p473(rep: Mapping[str, Any]) -> dict[str, Any]:
        runs = [d for d in rep["runs"] if d["plan_id"] == 473]
        adm = [d for d in runs if d["admitted"]]
        return {
            "runs": len(runs), "admitted": len(adm), "excluded": len(runs) - len(adm),
            "excluded_by_reason": dict(Counter(d["reason"] for d in runs if not d["admitted"])),
            "request_kinds": dict(sorted(Counter(d["request_kind"] for d in runs).items())),
        }

    def side(rep: Mapping[str, Any]) -> dict[str, Any]:
        ex = rep["excluded"]
        return {
            "request_kinds": rep["request_kinds"], "unknown_rate": rep["unknown_rate"],
            "excluded_count": ex["count"], "excluded_share": ex["share"], "excluded_by_reason": ex["by_reason"],
            "excluded_by_request_kind": ex["by_request_kind"], "plan_473": p473(rep),
        }

    unique = list(probes.values())
    return {
        "set": "real",
        "rows": before["rows"],
        "run": dict(facts),
        "deterministic": {"distribution": kind_distribution(p.det_kind for p in unique)},
        "with_extraction": {"distribution": kind_distribution(p.final_kind for p in unique)},
        "extraction": extraction_stats(unique),
        "gate": {
            "before": side(before), "after": side(after),
            "runs_flipped_excluded_to_admitted": len(flips_to_admit),
            "runs_flipped_admitted_to_excluded": len(flips_to_exclude),
            "flipped_to_admitted_run_ids": flips_to_admit, "flipped_to_excluded_run_ids": flips_to_exclude,
        },
        "questions_detail": [
            _probe_row(p, {"q": f"q{i:03d}", "run_count": len(p.run_ids)}) for i, p in enumerate(unique, 1)
        ],
    }


# ── input assembly ──────────────────────────────────────────────────────────────────────────────────────


def load_set(path: Path) -> tuple[list[Any], dict[str, str]]:
    from bench.schema import load_golden_set  # noqa: PLC0415 — deferred; keeps module import cheap

    queries = load_golden_set(path)
    questions: dict[str, str] = {}
    for q in queries:
        questions.setdefault(replay.question_key(q.text), q.text)
    return queries, questions


def real_questions(
    rows: Sequence[Any], plans: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, list[Any]]]:
    """The distinct questions of the rows the replay would examine, and the run ids asking each."""
    plans_by_id = {int(p["id"]): p for p in plans if isinstance(p.get("id"), int)}
    questions: dict[str, str] = {}
    runs: dict[str, list[Any]] = {}
    for row in rows:
        if replay.skip_reason(row, plans_by_id) is not None:
            continue
        text = replay._row_text(row)
        key = replay.question_key(text)
        questions.setdefault(key, text)
        runs.setdefault(key, []).append(row.get("id"))
    return questions, runs


def model_report(environ: Mapping[str, str], captured: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
    """Which model the calls asked for (the setting) and which each call's own usage says answered it."""
    from nexus.plans.request_coverage import extraction_model  # noqa: PLC0415 — deferred; keeps module import cheap

    seen = Counter(str(getattr(u, "model", None)) for usage in captured.values() for u in usage)
    return {"requested": extraction_model(environ), "canonical_models": dict(sorted(seen.items()))}


def refuse_prod_write_opt_in(environ: Mapping[str, str]) -> None:
    """The measurement is read-only: an ambient write opt-in is a reason to stop, not to proceed."""
    from nexus.db.service_endpoint import PROD_WRITE_OPT_IN_ENV  # noqa: PLC0415 — the one name of the opt-in

    if environ.get(PROD_WRITE_OPT_IN_ENV):
        raise SystemExit(f"{PROD_WRITE_OPT_IN_ENV} is set; this measurement is read-only. Unset it and rerun.")


async def _run(args: argparse.Namespace) -> int:
    refuse_prod_write_opt_in(os.environ)
    captured: dict[str, list[Any]] = {}
    meta: dict[str, Any] = {}
    if args.set == "real":
        since = None if args.all_time else args.since
        try:
            rows, read_facts = replay._read_rows(since, args.last)
            plans, library_source = replay._read_plans(args.plans_json)
        except Exception as exc:  # noqa: BLE001 — a failed read is reported, not a traceback
            _log.error("rdr232_extract_read_failed", error=type(exc).__name__)
            click.echo(f"read failed: {type(exc).__name__}: {exc}", err=True)
            return 1
        questions, run_ids = real_questions(rows, plans)
        meta = {"since": since, "last": args.last, "library_source": library_source, "library_plans": len(plans),
                "read": read_facts}
        queries: list[Any] = []
    else:
        queries, questions = load_set(SET_PATHS[args.set])
        run_ids, rows, plans = {}, [], []
    if not questions:
        click.echo("FAIL: no questions to measure; a measurement of nothing proves nothing", err=True)
        return 2
    if args.dry_run:
        extractor: Extractor = _deterministic_only
        cm: contextlib.AbstractContextManager[Extractor] = contextlib.nullcontext(extractor)
    else:
        cm = core_extractor(captured)
    with cm as extractor:
        probes, facts = await measure(
            questions, extractor, max_calls=args.max_calls, token_sink=captured,
        )
    for key, ids in run_ids.items():
        probes[key].run_ids = ids
    if args.set == "real":
        report = real_report(rows, plans, probes, facts)
        report["window"] = meta
    else:
        report = labelled_report(args.set, queries, probes, facts)
    report["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["dry_run"] = bool(args.dry_run)
    report["extract_model"] = model_report(os.environ, captured)
    if args.show_questions:
        for key, text in questions.items():
            p = probes[key]
            click.echo(f"{p.det_kind}->{p.final_kind} {p.outcome} {p.latency_ms}ms  {text[:140]!r}", err=True)
    text_out = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text_out + "\n", encoding="utf-8")
        click.echo(
            f"wrote {args.out}: {len(questions)} distinct questions, "
            f"{report['extraction']['calls_made']} calls, aborted={facts['aborted']}", err=True,
        )
    else:
        click.echo(text_out)
    return 0


async def _deterministic_only(text: str) -> Any:
    """The extractor of a dry run: the first pass with the extraction reported ``disabled`` (no call)."""
    from nexus.plans.request_coverage import derive_request_coverage, structured_extract  # noqa: PLC0415 — deferred, as above

    async def never(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("a dry run makes no model call")

    return await structured_extract(
        derive_request_coverage(text), text, dispatch=never, timeout=0.0, enabled=False,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--set", required=True, choices=(*SET_PATHS, "real"))
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--dry-run", action="store_true", help="deterministic pass only; no model call")
    ap.add_argument("--max-calls", type=int, help="canary bound: stop sending after this many calls")
    ap.add_argument("--show-questions", action="store_true", help="print each question to stderr (local only)")
    ap.add_argument("--since", help="real: only runs created at or after this ISO date or datetime")
    ap.add_argument("--all-time", action="store_true", help="real: no time bound")
    ap.add_argument("--last", type=int, help="real: read at most this many runs, newest first")
    ap.add_argument("--plans-json", type=Path, help="real: replay against this saved plan-row list")
    return asyncio.run(_run(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
