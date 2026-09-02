# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx tier-status`` — audit tier-write activity per session.

Phase 1B of the tier-discipline restoration initiative (nexus-a52i,
follow-up to Phase 1A's ``tier_writes`` telemetry table at nexus-kren).

Reads the ``tier_writes`` T2 table populated by ``_record_tier_write``
in ``src/nexus/mcp/core.py``. By default reports the current session
(via ``NX_SESSION_ID`` env or ``read_claude_session_id()``); other
modes select by id, last-N, or time window.
"""
from __future__ import annotations

import json as _json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import structlog

from nexus.session import read_claude_session_id

_log = structlog.get_logger(__name__)


# Tier semantics — the order callers care about (T1 sibling-bus first,
# then persistent T2/T3, then plan-library writes which sit in T2 but
# carry distinct semantics for cold-start cost analysis).
_TIER_ORDER = ("T1", "T2", "T3", "plan")
_TOOL_TIER = {
    "scratch_put": "T1",
    "memory_put": "T2",
    "store_put": "T3",
    "plan_save": "plan",
}


def _resolve_session(session_arg: str | None) -> str | None:
    """Return the session_id to query, or None for 'no specific session'."""
    if session_arg:
        return session_arg
    env_sid = os.environ.get("NX_SESSION_ID", "").strip()
    if env_sid:
        return env_sid
    return read_claude_session_id()


def _resolve_target_session_or_exit(
    session_arg: str | None,
    *,
    last_n: int | None,
    since: str | None,
    json_out: bool,
) -> str | None:
    """Session-scope resolution shared by the service and local branches
    (critique: was copy-pasted verbatim — a change to resolution semantics
    applied to one branch would silently diverge the backends).

    Returns ``None`` when ``--last``/``--since`` scope the query instead;
    exits 1 when the default current-session mode cannot resolve a session.
    """
    if last_n or since:
        return None
    target_session = _resolve_session(session_arg)
    if target_session is None and not session_arg:
        if json_out:
            click.echo(_json.dumps({"error": "no current session resolvable"}))
        else:
            click.echo(
                "No current session resolvable "
                "(NX_SESSION_ID env unset, no claude session file). "
                "Use --session, --last, or --since.",
                err=True,
            )
        raise click.exceptions.Exit(1)
    return target_session


def _summarize(rows: list[tuple]) -> dict[str, int]:
    """Aggregate query rows into per-tier counts."""
    summary: dict[str, int] = {tier: 0 for tier in _TIER_ORDER}
    for _tool, tier, _agent, _project, n in rows:
        if tier in summary:
            summary[tier] += n
        else:
            summary.setdefault("other", 0)
            summary["other"] += n
    return summary


@click.command("tier-status")
@click.option(
    "--session", "session_arg", default=None,
    help="Specific session_id (default: current session via NX_SESSION_ID).",
)
@click.option(
    "--last", "last_n", type=int, default=None,
    help="Aggregate the last N sessions (most recent by ts).",
)
@click.option(
    "--since", "since", default=None,
    help="ISO 8601 timestamp; only count writes at or after this moment.",
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human table.",
)
def tier_status_cmd(
    session_arg: str | None,
    last_n: int | None,
    since: str | None,
    json_out: bool,
) -> None:
    """Audit tier-write activity. Phase 1B (nexus-a52i).

    Default mode reports the current session. Override with --session,
    --last, or --since (mutually exclusive — pick one).
    """
    if sum(1 for x in (session_arg, last_n, since) if x) > 1:
        raise click.UsageError(
            "--session, --last, and --since are mutually exclusive"
        )
    # nexus-spbay: validate/normalize --since up front — same defect class
    # as `nx answer-runs --since` (the engine's old parser turned any
    # unparseable value into `since now()`, a guaranteed zero); fail as a
    # usage error naming the value, and send old engines a form they parse.
    if since:
        from nexus.db.t2.http_telemetry_store import normalize_since_filter  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        try:
            since = normalize_since_filter(since)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--since") from exc

    # nexus-59wjj: read parity — query the service-backed telemetry store
    # through GET /v1/telemetry/tier_writes/query. On any failure fall
    # back to an honest, failure-shaped message (404 = engine predates
    # the route; other HTTP = live engine error; else unreachable) rather
    # than silently reporting 0 writes. (The local-SQLite tier_writes
    # reader died with the =sqlite opt-out, RDR-158 P3 nexus-7bomn.)
    target_session = _resolve_target_session_or_exit(
        session_arg, last_n=last_n, since=since, json_out=json_out,
    )
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        store = HttpTelemetryStore()
        rows = store.query_tier_writes(
            session_id=target_session, since=since, last_n=last_n,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to the honest failure-shaped message, never a silent 0
        _log.debug("tier_status_service_read_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import tier_writes_read_failure_message  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        msg = tier_writes_read_failure_message(exc)
        if json_out:
            click.echo(_json.dumps({"service_backed": True, "message": msg}, indent=2))
        else:
            click.echo(msg)
        return
    _emit_report(
        rows, target_session=target_session, last_n=last_n,
        since=since, json_out=json_out,
    )


def _emit_report(
    rows: list[tuple[str, str, str | None, str | None, int]],
    *,
    target_session: str | None,
    last_n: int | None,
    since: str | None,
    json_out: bool,
) -> None:
    """Output tail (nexus-59wjj: the service rows carry the same
    ``(tool, tier, agent, project, count)`` shape the local SQLite
    ``_query`` produced before it died with the =sqlite opt-out)."""
    summary = _summarize(rows)
    total = sum(summary.values())

    if json_out:
        payload = {
            "scope": (
                "last_n" if last_n
                else "since" if since
                else "session"
            ),
            "session_id": target_session,
            "last_n": last_n,
            "since": since,
            "total_writes": total,
            "by_tier": summary,
            "rows": [
                {"tool": t, "tier": ti, "agent": a, "project": p, "count": n}
                for t, ti, a, p, n in rows
            ],
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    # Human table.
    scope_label = (
        f"last {last_n} session(s)" if last_n
        else f"since {since}" if since
        else f"session {target_session}"
    )
    click.echo(f"tier-write activity ({scope_label}):")
    if total == 0:
        click.echo("  (no writes)")
        return
    click.echo(f"  total: {total}")
    for tier in _TIER_ORDER:
        n = summary.get(tier, 0)
        if n:
            click.echo(f"    {tier:<6} {n}")
    if rows:
        click.echo()
        click.echo(f"  {'tool':<14} {'tier':<6} {'agent':<14} {'project':<14} count")
        click.echo(f"  {'-'*14} {'-'*6} {'-'*14} {'-'*14} -----")
        for tool, tier, agent, project, n in rows:
            click.echo(
                f"  {tool:<14} {tier:<6} "
                f"{(agent or '<none>'):<14} {(project or '<none>'):<14} {n}"
            )
