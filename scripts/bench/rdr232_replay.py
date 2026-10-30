# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 2 (nexus-jypmj.2.8): replay recorded ``nx_answer`` runs through request extraction
and answer-kind eligibility, and report what the gate would change.

For each ``nx_answer_runs`` row with a question and a selected library plan, this derives the request's
:class:`~nexus.plans.request_coverage.RequestCoverage` (the deterministic first pass only; the structured
extraction stays off), looks the selected plan up in the CURRENT plan library, and judges it exactly as the
gate would with ``ANSWER_KIND_ELIGIBILITY_ENABLED`` on (an enforcing
:class:`~nexus.plans.eligibility.EligibilityFilter`, so the verdicts are the gate's own, not a copy).

Read-only. The two reads are ``GET /v1/telemetry/nx_answer_runs/query`` and ``GET /v1/plans/list``;
nothing here writes, and ``NX_ALLOW_PROD_WRITE`` is not consulted. ``--plans-json`` replays against a saved
library snapshot (a list of plan rows) instead of the live one.

What the stored rows cannot say, and the report says so rather than guessing:

* which matcher branch picked the plan, and the candidate list the matcher saw: a run row stores
  ``plan_id`` and ``matched_confidence`` only, so "an eligible alternative existed" is answered at the
  LIBRARY level (is any other enabled library plan eligible for the request's kind), an upper bound on the
  matcher level, and labeled as such;
* the caller's ``dimensions["answer_kind"]`` hint, so every kind here is the heuristic reading;
* the ``source``/``owner``/``project`` bindings (they do not change the kind).

Question text is user data: the committed report carries ids and kinds only. ``--show-questions`` prints the
text to stderr for local reading and never into the report.

Exit status: 0 on a report, 2 when no row was examined (a replay that examined nothing proves nothing, so it
fails instead of writing an empty report), 1 on a read failure.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)

#: Request kinds the RDR calls broad: a single-fact plan is the wrong shape for them.
BROAD_KINDS: tuple[str, ...] = ("synthesis", "comparison", "audit", "debug")

#: Every reason a row can be skipped; the report lists all of them (zero included) so a missing key is never
#: read as "none happened".
SKIP_REASONS: tuple[str, ...] = (
    "unreadable_row",
    "continuation_report",
    "no_question",
    "no_plan_id",
    "inline_planned",
    "plan_not_in_library",
)

#: Plan provenance buckets, all always reported (zero included).
BUCKETS: tuple[str, ...] = ("curated_builtin", "grown", "undeclared_builtin", "unattested_other")

DEFAULT_WINDOW_DAYS = 30


class NoRowsExamined(RuntimeError):
    """The replay examined zero rows: nothing was measured, so no report is written."""


def _row_text(row: Mapping[str, Any]) -> str:
    q = row.get("question")
    return q.strip() if isinstance(q, str) else ""


def skip_reason(row: Any, plans_by_id: Mapping[int, Mapping[str, Any]]) -> str | None:
    """Why *row* is not replayed, or ``None`` when it is.

    The order is the order of the facts: a report event is no run at all; a run with no question cannot be
    extracted; no plan id, plan id 0 (the inline planner's sentinel) and a plan the library no longer holds
    have no library plan to judge.
    """
    if not isinstance(row, Mapping):
        return "unreadable_row"
    from nexus.commands.answer_runs import _row_is_continuation_report  # noqa: PLC0415 — the canonical marker test; heavy import kept off the module top

    if _row_is_continuation_report(dict(row)):
        return "continuation_report"
    if not _row_text(row):
        return "no_question"
    plan_id = row.get("plan_id")
    if plan_id is None or isinstance(plan_id, bool) or not isinstance(plan_id, int):
        return "no_plan_id"
    if plan_id == 0:
        return "inline_planned"
    if plan_id not in plans_by_id:
        return "plan_not_in_library"
    return None


def _is_enabled(plan: Mapping[str, Any]) -> bool:
    return not plan.get("disabled_at")


def judge(request_kind: str, plan: Mapping[str, Any], named_source_count: int = 0) -> tuple[bool, str | None, Any]:
    """The gate's verdict on *plan* for a request of *request_kind*: ``(admitted, reason, standing)``.

    Runs the real enforcing :class:`EligibilityFilter`, so the matrix, the provenance rules and the fail-closed
    ``judge_error`` are the gate's own. *named_source_count* is 1 for a request that names exactly one source
    (``RequestCoverage.names_one_source``): the gate then refuses citation-traversal for a synthesis or ``unknown``
    request (nexus-jypmj).
    """
    from nexus.plans.eligibility import EligibilityFilter, classify_plan  # noqa: PLC0415 — deferred; keeps module import cheap

    filt = EligibilityFilter(request_kind, "heuristic", enforce=True, named_source_count=named_source_count)
    admitted = filt.admit(plan, "cosine")
    reasons = filt.to_wire()["branches"]["cosine"]["reasons"]
    reason = None if admitted else next(iter(reasons), "unknown")
    standing = classify_plan(plan.get("tags"), plan.get("plan_json"))
    return admitted, reason, standing


def _bucket(standing: Any) -> str:
    """The plan's provenance bucket as the report names it."""
    if standing.provenance == "curated":
        return "curated_builtin"
    if standing.provenance == "grown":
        return "grown"
    return "undeclared_builtin" if standing.flag == "undeclared_builtin" else "unattested_other"


def _alternative_exists(
    request_kind: str, selected_id: int, plans_by_id: Mapping[int, Mapping[str, Any]],
) -> bool:
    """Whether some OTHER enabled library plan is eligible for *request_kind* (library level, not matcher)."""
    for pid, plan in plans_by_id.items():
        if pid == selected_id or not _is_enabled(plan):
            continue
        if judge(request_kind, plan)[0]:
            return True
    return False


def question_key(text: str) -> str:
    """The key a question is deduplicated and overridden by (case-folded text)."""
    return text.casefold()


def classify_rows(
    rows: Sequence[Any], plans: Iterable[Mapping[str, Any]],
    kind_override: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Replay *rows* against *plans* and return the report (no I/O; the unit of the tests).

    *kind_override* maps :func:`question_key` to a request kind that replaces the deterministic reading (the
    kind a structured extraction produced, nexus-jypmj.2.8); such a request is reported with the
    ``model-assisted`` decision. ``None`` (the default) is the deterministic replay and changes nothing.

    Raises :class:`NoRowsExamined` when no row survives the skips.
    """
    from nexus.commands.answer_runs import _row_is_handed_off  # noqa: PLC0415 — the canonical marker test; deferred like its sibling
    from dataclasses import replace as dc_replace  # noqa: PLC0415 — stdlib, branch-local

    from nexus.plans.request_coverage import derive_request_coverage, needs_structured_extract  # noqa: PLC0415 — deferred; keeps module import cheap

    plans_by_id = {int(p["id"]): p for p in plans if isinstance(p.get("id"), int)}
    skipped: Counter[str] = Counter({r: 0 for r in SKIP_REASONS})
    detail: list[dict[str, Any]] = []
    kinds: Counter[str] = Counter()
    decisions: Counter[str] = Counter()
    by_request_kind: dict[str, Counter[str]] = {}
    by_plan: dict[int, dict[str, Any]] = {}
    reasons: Counter[str] = Counter()
    handoffs = 0
    structured_candidates = 0
    narrowed_runs = 0
    distinct: set[str] = set()
    # broad-request reuse, before the gate (every examined run was served) and after (the admitted ones)
    reuse: dict[str, dict[str, int]] = {b: {"before": 0, "after": 0} for b in BUCKETS}
    alt = {"excluded_with_library_alternative": 0, "excluded_without_library_alternative": 0}
    for row in rows:
        why = skip_reason(row, plans_by_id)
        if why is not None:
            skipped[why] += 1
            continue
        if _row_is_handed_off(dict(row)):
            handoffs += 1
        plan = plans_by_id[row["plan_id"]]
        coverage = derive_request_coverage(_row_text(row))
        kind, decision = coverage.answer_kind, coverage.kind_decision
        overridden = kind_override.get(question_key(_row_text(row))) if kind_override else None
        if overridden is not None:
            kind, decision = overridden, "model-assisted"
        # nexus-4xmpv, nexus-jypmj: the one-source rule refuses citation-traversal (the gate, below) and confines the
        # run's secondary retrieval to the document, which a replay cannot see. The replay reads the SAME property
        # nx_answer does, so a count here cannot disagree with a live run's.
        admitted, reason, standing = judge(kind, plan, 1 if coverage.names_one_source else 0)
        narrowed = dc_replace(coverage, answer_kind=kind).narrows_to_one_source
        narrowed_runs += narrowed
        bucket = _bucket(standing)
        distinct.add(question_key(_row_text(row)))
        kinds[kind] += 1
        decisions[decision] += 1
        if needs_structured_extract(coverage):
            structured_candidates += 1
        rk = by_request_kind.setdefault(kind, Counter())
        rk["examined"] += 1
        pl = by_plan.setdefault(row["plan_id"], {
            "plan_id": row["plan_id"], "name": plan.get("name"), "bucket": bucket,
            "plan_kind": standing.kind.value, "examined": 0, "excluded": 0, "reasons": Counter(),
        })
        pl["examined"] += 1
        if kind in BROAD_KINDS:
            r = reuse[bucket]
            r["before"] += 1
            r["after"] += 1 if admitted else 0
        has_alt = None
        if not admitted:
            rk["excluded"] += 1
            pl["excluded"] += 1
            pl["reasons"][reason] += 1
            reasons[reason] += 1
            has_alt = _alternative_exists(kind, row["plan_id"], plans_by_id)
            alt["excluded_with_library_alternative" if has_alt else "excluded_without_library_alternative"] += 1
        detail.append({
            "run_id": row.get("id"), "created_at": row.get("created_at"), "plan_id": row["plan_id"],
            "request_kind": kind, "kind_decision": decision, "plan_bucket": bucket,
            "plan_kind": standing.kind.value, "admitted": admitted, "reason": reason,
            "library_alternative": has_alt, "one_source_narrowing": narrowed,
        })
    examined = len(detail)
    if examined == 0:
        raise NoRowsExamined(
            f"0 of {len(rows)} rows examined (skipped: {dict(skipped)}); a replay that examined nothing "
            "measures nothing"
        )
    excluded = sum(1 for d in detail if not d["admitted"])
    unknown = kinds.get("unknown", 0)
    return {
        "rows": {"read": len(rows), "examined": examined, "distinct_questions_examined": len(distinct), "skipped_total": sum(skipped.values()),
                 "skipped": dict(skipped), "examined_continuation_handoffs": handoffs},
        "request_kinds": dict(sorted(kinds.items())),
        "kind_decisions": dict(sorted(decisions.items())),
        "unknown_rate": round(unknown / examined, 4),
        "unknown_needing_structured_extract": structured_candidates,
        "one_source_narrowing": {
            "runs": narrowed_runs, "of": examined,
            "note": "synthesis or unknown runs that name exactly one source (RequestCoverage.narrows_to_one_source); "
                    "nx_answer confines their secondary retrieval to the document once the source resolves, which a replay cannot see",
        },
        "excluded": {
            "count": excluded, "share": round(excluded / examined, 4),
            "by_reason": dict(sorted(reasons.items())),
            "by_request_kind": {k: dict(v) for k, v in sorted(by_request_kind.items())},
            "by_plan": [
                {**{k: v for k, v in p.items() if k != "reasons"}, "reasons": dict(p["reasons"])}
                for p in sorted(by_plan.values(), key=lambda p: p["plan_id"])
            ],
        },
        "broad_request_reuse": {
            "broad_kinds": list(BROAD_KINDS),
            "examined_broad": sum(v["before"] for v in reuse.values()),
            "by_plan_bucket": {
                b: {**v, "dropped": v["before"] - v["after"]} for b, v in reuse.items()
            },
        },
        "alternatives": {
            "matcher_branch_recorded": False,
            "candidate_list_recorded": False,
            "note": (
                "run rows store plan_id and matched_confidence only, so whether the MATCHER had an eligible "
                "alternative is not recoverable; the counts below are library level: some other enabled "
                "library plan is eligible for the request kind (an upper bound, ignoring bindings, scope "
                "and the confidence floor)"
            ),
            "library_level": alt,
        },
        "not_recoverable": [
            "matcher branch (cosine / category_route / fts) of the selected plan",
            "the candidate list the matcher returned",
            "dimensions answer_kind hint (every kind here is the heuristic reading)",
            "source / owner / project bindings",
        ],
        "runs": detail,
    }


def _read_rows(since: str | None, last: int | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The run rows (newest first) and read facts. One probe for the window's size, then one page for it."""
    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred: service client, only on a live read

    store = HttpTelemetryStore()
    probe = store.query_nx_answer_runs(since=since, limit=1)
    total = int(probe["total"])
    want = total if last is None else min(total, last)
    if want <= 0:
        return [], {"total_in_window": total, "requested": 0, "returned": 0, "truncated_by_server": False}
    page = store.query_nx_answer_runs(since=since, limit=want)
    rows = list(page["rows"])
    return rows, {
        "total_in_window": total, "requested": want, "returned": len(rows),
        "truncated_by_server": len(rows) < want,
        "oldest_created_at_in_window": page.get("oldest_created_at", ""),
        "oldest_created_at_returned": min((str(r.get("created_at") or "") for r in rows if r.get("created_at")), default=""),
    }


def _read_plans(plans_json: Path | None) -> tuple[list[dict[str, Any]], str]:
    if plans_json is not None:
        data = json.loads(plans_json.read_text(encoding="utf-8"))
        return list(data), f"snapshot:{plans_json.name}"
    from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 — deferred: service client, only on a live read

    return HttpPlanLibrary().list_plans(limit=1000, include_disabled=True), "live"


def _show_questions(rows: Sequence[Any], plans: Iterable[Mapping[str, Any]], report: dict[str, Any]) -> None:
    """Local reading aid: one stderr line per examined run with its question (never written to a report)."""
    by_id = {r.get("id"): r for r in rows if isinstance(r, Mapping)}
    for d in report["runs"]:
        q = _row_text(by_id.get(d["run_id"], {}))
        click.echo(
            f"run {d['run_id']} plan {d['plan_id']} {d['request_kind']} "
            f"{'admit' if d['admitted'] else 'EXCLUDE:' + str(d['reason'])}  {q[:160]!r}",
            err=True,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", help="only runs created at or after this ISO date or datetime "
                    f"(default: {DEFAULT_WINDOW_DAYS} days ago)")
    ap.add_argument("--all-time", action="store_true", help="no time bound (overrides --since)")
    ap.add_argument("--last", type=int, help="read at most this many runs, newest first")
    ap.add_argument("--plans-json", type=Path, help="replay against this saved plan-row list, not the live library")
    ap.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    ap.add_argument("--show-questions", action="store_true",
                    help="print each examined run's question to stderr (local reading only; never in the report)")
    args = ap.parse_args(argv)
    since: str | None
    if args.all_time:
        since = None
    elif args.since:
        since = args.since
    else:
        since = (datetime.now(UTC) - timedelta(days=DEFAULT_WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows, read_facts = _read_rows(since, args.last)
        plans, library_source = _read_plans(args.plans_json)
    except Exception as exc:  # noqa: BLE001 — a failed read is reported, not a traceback
        _log.error("rdr232_replay_read_failed", error=type(exc).__name__)
        click.echo(f"read failed: {type(exc).__name__}: {exc}", err=True)
        return 1
    try:
        report = classify_rows(rows, plans)
    except NoRowsExamined as exc:
        click.echo(f"FAIL: {exc}", err=True)
        return 2
    report = {
        "window": {"since": since, "last": args.last, "library_source": library_source,
                   "library_plans": len(plans), "read": read_facts,
                   "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")},
        **report,
    }
    if args.show_questions:
        _show_questions(rows, plans, report)
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        click.echo(f"wrote {args.out}: {report['rows']['examined']} examined of {report['rows']['read']} read", err=True)
    else:
        click.echo(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
