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
"""
from __future__ import annotations

import json as _json
from datetime import UTC, datetime

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
    help="Max rows in the listed page (does not affect the aggregates).",
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human table.",
)
def answer_runs_cmd(
    since: str | None,
    limit: int,
    json_out: bool,
) -> None:
    """Read nx_answer_runs: recent runs plus exact aggregates (nexus-eho3u).

    Reports total run count, plan-match hit rate vs. inline-planner
    fallback, average duration/cost, and a fixed-edge latency histogram —
    the §4.5 shakedown-playbook baseline block — plus the last N rows.
    """
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        store = HttpTelemetryStore()
        result = store.query_nx_answer_runs(since=since, limit=limit)
    except Exception as exc:  # noqa: BLE001 — degrade to the honest failure-shaped message, never a silent 0
        _log.debug("answer_runs_service_read_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import nx_answer_runs_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        msg = nx_answer_runs_read_failure_message(exc)
        if json_out:
            click.echo(_json.dumps({"service_backed": True, "message": msg}, indent=2))
        else:
            click.echo(msg)
        return
    _emit_report(result, since=since, limit=limit, json_out=json_out)


def _emit_report(
    result: dict,
    *,
    since: str | None,
    limit: int,
    json_out: bool,
) -> None:
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
        payload = {
            "since": since,
            "limit": limit,
            "captured_at": datetime.now(UTC).isoformat(),
            **result,
        }
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
    if avg_dur is not None:
        click.echo(f"  avg duration_ms: {avg_dur:.0f}")
    if avg_cost is not None:
        click.echo(f"  avg cost_usd: {avg_cost:.6f}")

    buckets = result.get("latency_buckets") or {}
    if buckets:
        click.echo("  latency buckets:")
        for key in _BUCKET_ORDER:
            n = buckets.get(key, 0)
            if n:
                click.echo(f"    {_BUCKET_LABEL[key]:<10} {n}")

    rows = result.get("rows") or []
    if rows:
        click.echo()
        click.echo(f"  last {len(rows)} run(s):")
        for r in rows:
            plan_id = r.get("plan_id")
            # plan_id 0 is the ad-hoc Match sentinel every successful
            # inline-planner run carries (core.py::_nx_answer_plan_miss) —
            # NOT a real plan (plans.id is BIGSERIAL, so 0 can never be
            # one). `not plan_id` is True for both None and 0, so a
            # fallback row renders "fallback", never the misleading
            # "plan=0" (nexus-eho3u review fix).
            is_fallback = not plan_id
            tag = "fallback" if is_fallback else f"plan={plan_id}"
            question = str(r.get("question", ""))[:60]
            click.echo(
                f"    {r.get('created_at', ''):<21} {tag:<10} "
                f"{r.get('duration_ms', 0):>7}ms  {question}"
            )
