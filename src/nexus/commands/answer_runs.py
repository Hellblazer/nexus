# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx answer-runs`` — read the nx_answer_runs telemetry table (nexus-eho3u).

Every ``nx_answer`` MCP call writes a row to ``nx_answer_runs`` via
``POST /v1/telemetry/nx_answer_runs/record`` — but until engine-service
carried the ``GET /v1/telemetry/nx_answer_runs/query`` route, nothing ever
read one back. This is that read surface, shaped after ``nx tier-status``
(``src/nexus/commands/tier_status.py``): same capability-honest degrade on a
pre-route engine (404 -> "deploy a newer engine", not a silent 0), same
--json / human-table split, same "no writes" vs "unreachable" distinction.

SERVICE-MODE ONLY. ``nx_answer_runs`` has no SQLite twin left to fall back
to (RDR-158 P3 retired the local stores); this verb is the sole reader, and
it is also the capture point the shakedown playbook's §4.5 telemetry
baseline snapshot names: total row count, latency-bucket histogram (fixed
edges — "SAME QUERIES, SAME BUCKETS, EVERY TIME"), and plan-match hit rate
versus inline-planner fallback.

RDR-196 .p1e (nexus-nyry9.11) extends this same surface — never a second
command — with a per-step breakdown (``--steps``) and a three-way
executed-ok / executed-failed / degenerate split of the listed rows. See
``answer_runs_cmd``'s own docstring (what ``--help`` actually renders) for
the default-population statement.
"""
from __future__ import annotations

import json as _json
import statistics as _statistics
from datetime import UTC, datetime
from typing import Any

import click
import structlog

_log = structlog.get_logger(__name__)

#: Fixed latency bucket order for human-table rendering — matches the
#: TelemetryRepository.queryNxAnswerRuns bucket keys exactly (never
#: re-derived; see that method's docstring for the edges).
_BUCKET_ORDER = ("under_5s", "5s_to_30s", "30s_to_2min", "2min_to_5min", "over_5min")
_BUCKET_LABEL = {
    "under_5s":    "<5s",
    "5s_to_30s":   "5s-30s",
    "30s_to_2min": "30s-2min",
    "2min_to_5min": "2min-5min",
    "over_5min":   ">5min",
}
#: Upper edge (exclusive), in ms, for each bucket except the last — mirrors
#: TelemetryRepository's SQL CASE edges (5s/30s/2min/5min) exactly, so a
#: client-side re-bucketing (RDR-196 .p1e review-fix S2 — see
#: ``_bucketize_durations``) produces the identical histogram shape the
#: engine would for the same population.
_BUCKET_EDGES_MS = (
    ("under_5s", 5_000),
    ("5s_to_30s", 30_000),
    ("30s_to_2min", 120_000),
    ("2min_to_5min", 300_000),
)

#: RDR-196 .p1c critique (T2 [23073], lme1s dev-notes): a literal
#: to-the-cent tolerance is too loose for typical $0.0001-$0.05 per-call
#: costs. Use a RELATIVE epsilon with a tiny absolute floor for the
#: near-zero case instead.
_COST_EPSILON_REL = 0.005
_COST_EPSILON_ABS_FLOOR = 1e-6


def _costs_agree(run_cost: "float | None", step_sum: "float | None") -> bool:
    """Whether a row's own ``cost_usd`` and the sum of its step costs
    agree within the relative/sub-cent epsilon. Both ``None`` (unknown vs
    unknown) trivially agree; exactly one ``None`` never agrees.
    """
    if run_cost is None and step_sum is None:
        return True
    if run_cost is None or step_sum is None:
        return False
    epsilon = max(
        _COST_EPSILON_ABS_FLOOR,
        _COST_EPSILON_REL * max(abs(run_cost), abs(step_sum)),
    )
    return abs(run_cost - step_sum) <= epsilon


def _row_step_cost_sum(row: dict) -> "float | None":
    """Sum of a row's known (non-``None``) step costs, or ``None`` when no
    step reports a known cost — never a fabricated zero (mirrors
    ``_nx_answer_record_run``'s own blanket rule, core.py)."""
    steps = row.get("steps") or []
    known = [s.get("cost_usd") for s in steps if s.get("cost_usd") is not None]
    return sum(known) if known else None


def _bucketize_durations(durations_ms: "list[int]") -> dict[str, int]:
    """Client-side re-bucketing of a duration list into the SAME
    fixed-edge histogram shape ``TelemetryRepository`` computes
    server-side (see ``_BUCKET_EDGES_MS``). RDR-196 .p1e review-fix S2:
    lets the page-scoped executed-ok population get its own, honestly
    page-scoped, latency-bucket view rather than borrowing the engine's
    whole-set one."""
    buckets = {k: 0 for k in _BUCKET_ORDER}
    for d in durations_ms:
        dur = int(d or 0)
        for key, edge in _BUCKET_EDGES_MS:
            if dur < edge:
                buckets[key] += 1
                break
        else:
            buckets["over_5min"] += 1
    return buckets


def _classify_degenerate_row(row: dict) -> str:
    """Sub-classify a ``step_count == 0`` row from fields visible on the
    row itself.

    NOT the per-id source disposition (dead code / live-fixed / harness
    artifact) the nx-answer-degenerate-row-taxonomy census recorded (T2
    nexus/nx-answer-degenerate-row-taxonomy-2026-08-20) — that requires
    reading the CODE at the time of the run, not the telemetry row, and
    changes as code is fixed. This is a read-time heuristic over the row's
    own ``question``/``final_text``, stable across code changes.

    That census found 24 of 39 degenerate rows (62%) were the BENIGN
    ``trace=False`` redaction class, not errors — so "degenerate" must
    never be rendered as a synonym for "broken"; the ``redacted`` class
    exists specifically so this command doesn't imply that.
    """
    final_text = str(row.get("final_text") or "")
    question = str(row.get("question") or "")
    if final_text == "[redacted]" or question == "[redacted]":
        return "redacted"
    if final_text.startswith("Planner error:"):
        return "planner_error"
    if final_text.startswith("Error:"):
        return "error"
    return "other"


def _row_is_failed(row: dict) -> bool:
    """Whether an EXECUTED row (``step_count > 0``) is a failed run, not a
    success. RDR-196 .p1e review-fix (Important, code-review-expert T2
    [23110]): before this fix, a run that completed >=1 real steps and
    then failed landed in the same "executed" bucket as a genuine
    success — reproducing exactly the population-mislabeling class this
    bead exists to prevent (the 45x-wrong docstring counted degenerate
    rows as successes; this would have counted FAILED-but-partially-
    executed rows as successes instead, a different flavor of the same
    mistake).

    THREE independently-sufficient signals, checked in this order:

    1. ``final_text`` starts with ``"Error:"`` (core.py's plan-execution
       exception-path prefix, the same string
       :func:`_classify_degenerate_row` already keys on for the
       step_count==0 case).
    2. ``final_text`` starts with the budget-exhausted partial-answer
       marker's prefix (RDR-196 .p1e review-fix round 2, code-review-
       expert T2 [23115]): a budget-exhausted run is a NAMED failure
       class (nexus-yg49g doctrine explicitly records it as
       ``success=False``) whose ``step_count`` can be > 0 and whose
       ``final_text`` is NEVER "Error:"-prefixed — before this signal,
       such a row misclassified as executed-ok in the tool's DEFAULT
       (no ``--steps``) invocation, silently polluting
       ``executed_ok_avg_duration_ms``/``executed_ok_latency_buckets``
       with exactly the near-deadline durations those fields exist to
       exclude. Imports ``NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX``
       from ``nexus.mcp.core`` (the single emitter of this text) rather
       than retyping the literal, so the two can never drift apart.
    3. Only checkable when ``--steps`` fetched the row's ``steps``: the
       LAST recorded step's ``ok`` is ``False`` (a plan that ran to
       completion but whose terminal dispatch itself failed).

    RULE when NONE of the three signals is observable (no known
    text-marker prefix matched, AND ``--steps`` was not requested so
    there is no per-step ``ok`` to check): the row is classified
    executed-OK by default. This is a stated, accepted blind spot, not
    a silent one — a failure whose ONLY signal is a per-step
    ``ok=False`` is invisible to the default (no ``--steps``) view;
    pass ``--steps`` to see it. Every failure class this arc has
    produced so far (plan-execution exception, budget exhaustion) DOES
    carry a ``final_text`` marker and is caught by signals 1-2 without
    ``--steps``; only a hypothetical FUTURE failure class with neither
    a text marker nor an "Error:"/budget prefix would fall into this
    blind spot.
    """
    final_text = str(row.get("final_text") or "")
    if final_text.startswith("Error:"):
        return True
    # RDR-196 .p1e review-fix round 2 (code-review-expert T2 [23115]):
    # deferred import (heavy nexus.mcp.core module; matches this
    # module's existing PLC0415 convention, and doc.py's own precedent
    # of deferred-importing a single name from nexus.mcp.core) --
    # imports the SAME constant core.py's single emitter uses, never a
    # retyped literal.
    from nexus.mcp.core import NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

    if final_text.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX):
        return True
    steps = row.get("steps")
    if isinstance(steps, list) and steps:
        last = steps[-1]
        if isinstance(last, dict) and last.get("ok") is False:
            return True
    return False


def _split_three_way(
    rows: "list[dict]",
) -> "tuple[list[dict], list[dict], dict[str, list[dict]]]":
    """Split *rows* (a fetched PAGE, not the whole filtered set) into
    executed-ok, executed-failed, and degenerate (``step_count == 0``,
    itself grouped by :func:`_classify_degenerate_row`).

    Returns ``(executed_ok, executed_failed, degenerate_by_class)``.
    """
    executed_ok: list[dict] = []
    executed_failed: list[dict] = []
    degenerate: dict[str, list[dict]] = {}
    for r in rows:
        if int(r.get("step_count") or 0) > 0:
            (executed_failed if _row_is_failed(r) else executed_ok).append(r)
        else:
            cls = _classify_degenerate_row(r)
            degenerate.setdefault(cls, []).append(r)
    return executed_ok, executed_failed, degenerate


def _step_breakdown(rows: "list[dict]") -> dict[str, Any]:
    """Per-operator / per-source cost+elapsed aggregates (count, sum,
    median), per-plan cost/elapsed medians, and run-vs-sum(steps)
    cost-consistency violations — computed ONLY from *rows* carrying a
    ``steps`` list (i.e. ``--steps`` was requested and the engine
    supports it; a row with no steps, or an empty ``steps`` list,
    contributes nothing here).

    *rows* is the caller-chosen population: executed-ok only by default,
    or executed-ok + executed-failed when ``--include-failed`` is passed
    (RDR-196 .p1e review-fix, Important) — this function itself is
    population-agnostic, it only ever sees what the caller hands it.

    Unknown (``None``) per-step costs are excluded from sums/medians —
    their count is still surfaced (``unknown_cost_count``), never folded
    in as a silent zero.
    """
    # RDR-196 .p1e review-fix (S1, substantive-critic T2 [23111]):
    # by_operator/by_source now compute median_cost_usd/median_elapsed_ms
    # too, matching by_plan's existing shape — accumulate raw lists here,
    # finalize (sum + median) once at the end via _finalize_agg below.
    by_operator_raw: dict[str, dict[str, Any]] = {}
    by_source_raw: dict[str, dict[str, Any]] = {}
    by_plan_costs: dict[str, list[float]] = {}
    by_plan_elapsed: dict[str, list[int]] = {}
    violations: list[dict[str, Any]] = []

    def _bump(bucket: dict[str, dict[str, Any]], key: str, *, cost: "float | None", elapsed_ms: int) -> None:
        entry = bucket.setdefault(key, {
            "count": 0, "known_costs": [], "unknown_cost_count": 0, "elapsed_list": [],
        })
        entry["count"] += 1
        entry["elapsed_list"].append(elapsed_ms)
        if cost is not None:
            entry["known_costs"].append(cost)
        else:
            entry["unknown_cost_count"] += 1

    def _finalize_agg(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in raw.items():
            costs = entry["known_costs"]
            elapsed = entry["elapsed_list"]
            out[key] = {
                "count": entry["count"],
                "known_cost_count": len(costs),
                "unknown_cost_count": entry["unknown_cost_count"],
                "total_cost_usd": sum(costs) if costs else 0.0,
                "median_cost_usd": _statistics.median(costs) if costs else None,
                "total_elapsed_ms": sum(elapsed),
                "median_elapsed_ms": _statistics.median(elapsed) if elapsed else None,
            }
        return out

    for row in rows:
        steps = row.get("steps") or []
        if not steps:
            continue
        plan_id = row.get("plan_id")
        # RDR-196 .p1e review-fix (S3, substantive-critic T2 [23111]):
        # `if plan_id else "fallback"` used Python truthiness, so
        # plan_id=0 -- the documented ad-hoc inline-planner sentinel
        # (core.py::_nx_answer_plan_miss's `Match(plan_id=0, ...)`;
        # plans.id is BIGSERIAL, 0 can never be a real plan) -- collapsed
        # into the SAME bucket as plan_id=None (a genuine planner-error
        # miss, no plan at all). Two different populations conflated;
        # `is not None` distinguishes "a real plan_id, possibly 0" from
        # "no plan_id".
        plan_key = str(plan_id) if plan_id is not None else "fallback"
        run_cost = row.get("cost_usd")
        step_sum = _row_step_cost_sum(row)
        if not _costs_agree(run_cost, step_sum):
            violations.append({
                "id": row.get("id"), "run_cost_usd": run_cost,
                "step_cost_sum_usd": step_sum,
            })
        for s in steps:
            op = str(s.get("operator") or "")
            src = str(s.get("source") or "")
            cost = s.get("cost_usd")
            elapsed = int(s.get("elapsed_ms") or 0)
            _bump(by_operator_raw, op, cost=cost, elapsed_ms=elapsed)
            _bump(by_source_raw, src, cost=cost, elapsed_ms=elapsed)
        if step_sum is not None:
            by_plan_costs.setdefault(plan_key, []).append(step_sum)
        by_plan_elapsed.setdefault(plan_key, []).append(
            sum(int(s.get("elapsed_ms") or 0) for s in steps)
        )

    by_plan: dict[str, dict[str, Any]] = {}
    for plan_key in set(by_plan_costs) | set(by_plan_elapsed):
        costs = by_plan_costs.get(plan_key) or []
        elapsed = by_plan_elapsed.get(plan_key) or []
        by_plan[plan_key] = {
            "run_count": len(elapsed),
            "median_cost_usd": _statistics.median(costs) if costs else None,
            "median_elapsed_ms": _statistics.median(elapsed) if elapsed else None,
        }

    return {
        "by_operator": _finalize_agg(by_operator_raw),
        "by_source": _finalize_agg(by_source_raw),
        "by_plan": by_plan,
        "cost_consistency_violations": violations,
    }


@click.command("answer-runs")
@click.option(
    "--since", "since", default=None,
    help=(
        "ISO 8601 timestamp; only count/list runs at or after this moment. "
        "created_at is SERVER-stamped (the engine's clock, not this "
        "machine's) — a run written moments ago may not appear for a "
        "sub-second-precision --since value if the two clocks have drifted "
        "by even a few hundred ms; prefer a cutoff with real separation "
        "(minutes+) from any write you expect to see."
    ),
)
@click.option(
    "--limit", "limit", type=int, default=20,
    help="Max rows in the listed page (does not affect the whole-set aggregates).",
)
@click.option(
    "--steps", "want_steps", is_flag=True, default=False,
    help=(
        "Also fetch and render the per-step breakdown (by operator, by "
        "source llm|sql|bundle — count/sum/median cost+elapsed each — plus "
        "per-plan cost/elapsed medians) for the listed rows, plus a "
        "run-vs-sum(steps) cost-consistency check. Requires an engine "
        "carrying the nx_answer_steps read route (nexus-lme1s / RDR-196 "
        ".p1c-b) — an older engine silently ignores the request "
        "server-side, so this command probes the same capability signal "
        "the write side uses and says so explicitly rather than rendering "
        "an empty breakdown indistinguishable from 'no steps were ever "
        "recorded'."
    ),
)
@click.option(
    "--include-failed", "include_failed", is_flag=True, default=False,
    help=(
        "Fold executed-but-FAILED rows into the --steps breakdown "
        "population alongside executed-ok rows (default: executed-ok "
        "only). A failed run still ran real, billable steps before it "
        "failed; --include-failed answers 'what did failures cost', "
        "the default answers 'what does a working run cost'."
    ),
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human table.",
)
def answer_runs_cmd(
    since: str | None,
    limit: int,
    want_steps: bool,
    include_failed: bool,
    json_out: bool,
) -> None:
    """Read nx_answer_runs: recent runs plus exact aggregates (nexus-eho3u).

    Reports total run count, plan-match hit rate vs. inline-planner
    fallback, average duration/cost, and a fixed-edge latency histogram —
    the §4.5 shakedown-playbook baseline block, computed over the WHOLE
    ``--since``-filtered set (including degenerate rows: a degenerate run
    still spent real wall-clock time, and its now-nullable ``cost_usd`` is
    correctly excluded by the engine's own AVG-ignores-NULL semantics) —
    plus the last N rows.

    RDR-196 .p1e (nexus-nyry9.11) adds a page-scoped (of the ``--limit``
    rows actually listed, NOT the whole-set block above — the two are
    DIFFERENT populations, printed and labeled separately so neither is
    mistaken for the other) three-way split: executed-ok, executed-failed
    (completed >=1 step but the run itself failed), and degenerate
    (``step_count == 0``, itself named by class: redacted / planner_error
    / error / other). **Default population for every ``--steps``
    aggregate (by-operator/by-source/by-plan) and for the page-scoped
    executed-ok latency block: executed-ok rows only** — the failure
    population that produced this arc's original 45x-wrong latency figure
    must never silently re-enter an aggregate meant to answer "what does a
    normal run cost/take". Pass ``--include-failed`` to fold
    executed-failed rows into the ``--steps`` breakdown too.
    """
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        store = HttpTelemetryStore()
        result = store.query_nx_answer_runs(since=since, limit=limit, include_steps=want_steps)
    except Exception as exc:  # noqa: BLE001 — degrade to the honest failure-shaped message, never a silent 0
        _log.debug("answer_runs_service_read_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import nx_answer_runs_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        msg = nx_answer_runs_read_failure_message(exc)
        if json_out:
            click.echo(_json.dumps({"service_backed": True, "message": msg}, indent=2))
        else:
            click.echo(msg)
        return
    _emit_report(
        result, since=since, limit=limit, json_out=json_out,
        want_steps=want_steps, include_failed=include_failed,
    )


def _emit_report(
    result: dict,
    *,
    since: str | None,
    limit: int,
    json_out: bool,
    want_steps: bool = False,
    include_failed: bool = False,
) -> None:
    rows = result.get("rows") or []
    executed_ok_rows, executed_failed_rows, degenerate_by_class = _split_three_way(rows)
    degenerate_count = sum(len(v) for v in degenerate_by_class.values())
    degenerate_breakdown = {k: len(v) for k, v in degenerate_by_class.items()}

    # RDR-196 .p1e review-fix (Important): executed-ok only by default;
    # --include-failed folds executed-failed rows in too. Never
    # degenerate rows -- those are a structurally different population
    # (zero steps ran at all) with no meaningful per-step cost/elapsed
    # to aggregate.
    breakdown_rows = (
        executed_ok_rows + executed_failed_rows if include_failed else executed_ok_rows
    )

    step_data: dict[str, Any] | None = None
    if want_steps:
        steps_supported = result.get("steps_supported")
        if steps_supported is False:
            step_data = {"steps_supported": False}
        else:
            step_data = {
                "steps_supported": steps_supported,
                **_step_breakdown(breakdown_rows),
            }

    # RDR-196 .p1e review-fix (S2, substantive-critic T2 [23111]): a
    # page-scoped latency view of the SAME executed-ok population the
    # --steps breakdown uses, computed client-side from duration_ms —
    # distinct from (and explicitly labeled apart from) the engine's
    # whole-set `avg_duration_ms`/`latency_buckets` below, which still
    # blends degenerate + executed + failed rows by design (that block
    # answers a different question: "the whole recorded population").
    executed_ok_durations = [r.get("duration_ms", 0) for r in executed_ok_rows]
    executed_ok_avg_duration_ms = (
        sum(executed_ok_durations) / len(executed_ok_durations)
        if executed_ok_durations else None
    )
    executed_ok_latency_buckets = _bucketize_durations(executed_ok_durations)

    if json_out:
        # nexus-eho3u review fix (critic S1): echo the query envelope
        # (since/limit/captured_at) alongside the store result, matching
        # `nx tier-status --json` (tier_status.py's `_emit_report` wraps
        # scope/session_id/last_n/since around the store rows the same
        # way) — a caller diffing successive §4.5 baseline snapshots needs
        # to know what window and page size produced each one, not just
        # the numbers. `captured_at` is THIS process's wall clock, stamped
        # at render time — display metadata for a human reading the
        # snapshot later, not a value compared against any server
        # timestamp (see the clock-skew note in --since's help text).
        payload: dict[str, Any] = {
            "since": since,
            "limit": limit,
            "captured_at": datetime.now(UTC).isoformat(),
            **result,
            # RDR-196 .p1e: PAGE-scoped (of the listed rows, not the
            # whole --since-filtered set the `latency_buckets`/
            # `avg_duration_ms` keys above cover — see answer_runs_cmd's
            # own docstring for the population distinction).
            "executed_ok_count": len(executed_ok_rows),
            "executed_failed_count": len(executed_failed_rows),
            "degenerate_count": degenerate_count,
            "degenerate_breakdown": degenerate_breakdown,
            "executed_ok_avg_duration_ms": executed_ok_avg_duration_ms,
            "executed_ok_latency_buckets": executed_ok_latency_buckets,
        }
        if step_data is not None:
            payload["step_breakdown"] = step_data
        click.echo(_json.dumps(payload, indent=2))
        return

    total = result.get("total", 0)
    scope_label = f"since {since}" if since else "all time"
    click.echo(f"nx_answer_runs ({scope_label}):")
    if total == 0:
        click.echo("  (no runs)")
        return

    hit = result.get("hit_count", 0)
    fallback = result.get("fallback_count", 0)
    avg_dur = result.get("avg_duration_ms")
    avg_cost = result.get("avg_cost_usd")
    oldest = result.get("oldest_created_at") or "<unknown>"

    click.echo(f"  total: {total}")
    click.echo(f"  oldest: {oldest}")
    click.echo(
        f"  plan-match hit: {hit}  inline-planner fallback: {fallback}"
    )
    # RDR-196 .p1e review-fix (S2): explicitly labeled — this is the
    # engine's WHOLE `--since`-filtered set, degenerate + executed-ok +
    # executed-failed all blended, unchanged from pre-.p1e behavior. An
    # unlabeled reading of this block is exactly the 45x-wrong-docstring
    # mistake this arc exists to prevent.
    if avg_dur is not None:
        click.echo(
            f"  avg duration_ms: {avg_dur:.0f}  "
            "(engine aggregate: ALL rows incl. degenerate + failed, whole "
            "--since-filtered set)"
        )
    if avg_cost is not None:
        click.echo(f"  avg cost_usd: {avg_cost:.6f}")

    buckets = result.get("latency_buckets") or {}
    if buckets:
        click.echo(
            "  latency buckets (engine aggregate: ALL rows incl. "
            "degenerate + failed, whole --since-filtered set):"
        )
        for key in _BUCKET_ORDER:
            n = buckets.get(key, 0)
            if n:
                click.echo(f"    {_BUCKET_LABEL[key]:<10} {n}")

    # RDR-196 .p1e: page-scoped three-way split — explicitly labeled by
    # its own scope (of the N rows LISTED below), never mixed into the
    # whole-set aggregates printed above.
    click.echo(
        f"  of the {len(rows)} row(s) shown: executed-ok {len(executed_ok_rows)}, "
        f"executed-failed {len(executed_failed_rows)}, degenerate {degenerate_count}"
    )
    for cls in sorted(degenerate_breakdown):
        click.echo(f"    degenerate/{cls}: {degenerate_breakdown[cls]}")

    if executed_ok_rows:
        click.echo(
            "  executed-ok latency (page-scoped, of the executed-ok rows "
            "shown only):"
        )
        if executed_ok_avg_duration_ms is not None:
            click.echo(f"    avg duration_ms: {executed_ok_avg_duration_ms:.0f}")
        for key in _BUCKET_ORDER:
            n = executed_ok_latency_buckets.get(key, 0)
            if n:
                click.echo(f"    {_BUCKET_LABEL[key]:<10} {n}")

    if step_data is not None:
        if step_data.get("steps_supported") is False:
            click.echo(
                "  --steps requested but this engine does not support "
                "include_steps (steps_supported=false) — run/aggregate "
                "data only; deploy an engine carrying nexus-lme1s to see "
                "the per-step breakdown."
            )
        else:
            pop_label = "executed-ok+failed" if include_failed else "executed-ok"
            click.echo(f"  by operator ({pop_label}):")
            for op, agg in sorted(step_data["by_operator"].items()):
                mc = agg["median_cost_usd"]
                me = agg["median_elapsed_ms"]
                mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                me_s = f"{me:.0f}ms" if me is not None else "unknown"
                click.echo(
                    f"    {op:<20} n={agg['count']:<4} "
                    f"cost_usd={agg['total_cost_usd']:.6f} "
                    f"(unknown={agg['unknown_cost_count']}) "
                    f"median_cost_usd={mc_s} "
                    f"elapsed_ms={agg['total_elapsed_ms']} median_elapsed_ms={me_s}"
                )
            click.echo(f"  by source ({pop_label}):")
            for src, agg in sorted(step_data["by_source"].items()):
                mc = agg["median_cost_usd"]
                me = agg["median_elapsed_ms"]
                mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                me_s = f"{me:.0f}ms" if me is not None else "unknown"
                click.echo(
                    f"    {src:<20} n={agg['count']:<4} "
                    f"cost_usd={agg['total_cost_usd']:.6f} "
                    f"(unknown={agg['unknown_cost_count']}) "
                    f"median_cost_usd={mc_s} "
                    f"elapsed_ms={agg['total_elapsed_ms']} median_elapsed_ms={me_s}"
                )
            by_plan = step_data["by_plan"]
            if by_plan:
                click.echo(f"  by plan ({pop_label}, median cost / median elapsed):")
                for plan_key, agg in sorted(by_plan.items()):
                    mc = agg["median_cost_usd"]
                    me = agg["median_elapsed_ms"]
                    mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                    me_s = f"{me:.0f}ms" if me is not None else "unknown"
                    click.echo(
                        f"    plan={plan_key:<8} runs={agg['run_count']:<4} "
                        f"median_cost_usd={mc_s} median_elapsed_ms={me_s}"
                    )
            violations = step_data["cost_consistency_violations"]
            if violations:
                click.echo(
                    f"  cost-consistency violations "
                    f"(run.cost_usd vs sum(steps.cost_usd), epsilon "
                    f"{_COST_EPSILON_REL:.1%} relative / "
                    f"{_COST_EPSILON_ABS_FLOOR} absolute floor):"
                )
                for v in violations:
                    click.echo(
                        f"    id={v['id']} run={v['run_cost_usd']!r} "
                        f"sum(steps)={v['step_cost_sum_usd']!r}"
                    )

    rows_page = result.get("rows") or []
    if rows_page:
        click.echo()
        click.echo(f"  last {len(rows_page)} run(s):")
        for r in rows_page:
            plan_id = r.get("plan_id")
            # plan_id 0 is the ad-hoc Match sentinel every successful
            # inline-planner run carries (core.py::_nx_answer_plan_miss) —
            # NOT a real plan (plans.id is BIGSERIAL, so 0 can never be
            # one). `not plan_id` is True for both None and 0, so a
            # fallback row renders "fallback", never the misleading
            # "plan=0" (nexus-eho3u review fix). This per-row display
            # convention is UNCHANGED by the .p1e review-fix (S3) above —
            # that fix is scoped to _step_breakdown's by_plan GROUPING key
            # only, where conflating plan_id=0 with plan_id=None hid a
            # real population (see _step_breakdown's own comment).
            is_fallback = not plan_id
            tag = "fallback" if is_fallback else f"plan={plan_id}"
            question = str(r.get("question", ""))[:60]
            click.echo(
                f"    {r.get('created_at', ''):<21} {tag:<10} "
                f"{r.get('duration_ms', 0):>7}ms  {question}"
            )
