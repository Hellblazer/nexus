# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx census`` — per-capability tool-use census over transcript JSONL.

nexus-h33x8.1. See :mod:`nexus.census` for the substrate and for why
this reports counts and refuses verdicts.
"""
from __future__ import annotations

import click


@click.group("census")
def census_group() -> None:
    """Measure which capabilities sessions actually invoked."""


@census_group.command("capability")
@click.option(
    "--session",
    default=None,
    help="Census a single session id instead of the whole project corpus.",
)
@click.option(
    "--since",
    default=None,
    help="Only sessions whose latest record is at or after this ISO date "
    "(e.g. 2026-07-01). Compared as a timestamp prefix.",
)
@click.option(
    "--project-dir",
    type=click.Path(path_type=str, file_okay=False),
    default=None,
    help="Transcript directory. Defaults to NX_CENSUS_PROJECT_DIR, else the "
    "~/.claude/projects entry for the current working directory.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON (carries per-tool counts) instead of the human table.",
)
@click.option(
    "--from-store",
    is_flag=True,
    default=False,
    help="Read the durable capability_census engine table (written at every "
    "SessionEnd, nexus-gjv9b PART 1) instead of re-parsing transcripts. "
    "Reuses --session/--since/--json; --project-dir is ignored.",
)
def capability_cmd(
    session: str | None,
    since: str | None,
    project_dir: str | None,
    as_json: bool,
    from_store: bool,
) -> None:
    """Count tool calls per capability, split orchestrator vs subagent.

    Exits non-zero when the run measured *nothing* — an empty, corrupt,
    or tool-call-free scope reports UNMEASURABLE rather than a clean
    zero. A zero row in a measurable run is a real zero.

    ``--from-store`` reads a fundamentally different artifact: the
    already-measured ``capability_census`` engine table (nexus-gjv9b
    PART 1's replacement for ``capability_census.jsonl``), never a fresh
    transcript walk — so it carries no UNMEASURABLE/BLINDSPOT distinction
    of its own; a session absent from the table is reported as absent,
    and a ``blindspot`` row (the transcript WAS unmeasurable at the time
    it was recorded) is surfaced verbatim.
    """
    if from_store:
        _capability_from_store(session=session, since=since, as_json=as_json)
        return

    import pathlib as _pathlib  # noqa: PLC0415 — stdlib deferred to subcommand scope

    from nexus.census import (  # noqa: PLC0415 — deferred; census only needed here
        census_corpus,
        default_project_dir,
        render_text,
        to_json,
    )

    root = _pathlib.Path(project_dir) if project_dir else default_project_dir()
    result = census_corpus(root, session=session, since=since)
    click.echo(to_json(result) if as_json else render_text(result), nl=False)
    if result.exit_code:
        raise SystemExit(result.exit_code)


def _capability_from_store(*, session: str | None, since: str | None, as_json: bool) -> None:
    """The store-backed reader half of ``nx census capability
    --from-store`` (nexus-gjv9b PART 1, S11 doctrine: no writer ships
    without its reader). Normal HttpTelemetryStore construction (full
    resolve/retry mixin) — this is an interactive CLI command, not the
    SessionEnd hot path, so there is no reason to bypass it the way
    ``_print_service_tier_summary``'s single-attempt read does.
    """
    import json as _json  # noqa: PLC0415 — stdlib deferred to subcommand scope

    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred; only needed here

        store = HttpTelemetryStore()
        try:
            rows = store.query_capability_census(session_id=session, since=since, limit=100)
        finally:
            store.close()
    except Exception as exc:  # noqa: BLE001 — boundary catch; degrade to an honest, non-zero-exit message
        click.echo(f"UNAVAILABLE: capability_census read failed: {exc}", nl=True)
        raise SystemExit(1) from exc

    if as_json:
        click.echo(_json.dumps({"rows": rows}, sort_keys=True))
        return

    if not rows:
        click.echo("No capability_census rows found for the given filter.")
        return

    lines = []
    for row in rows:
        header = f"session={row.get('session_id')} ts={row.get('ts')}"
        if row.get("blindspot"):
            lines.append(f"{header} BLINDSPOT reason={row.get('unmeasurable_reason')}")
            continue
        caps = row.get("capabilities") or {}
        cap_str = " ".join(f"{k}={v}" for k, v in caps.items())
        lines.append(
            f"{header} total_calls={row.get('total_calls')} "
            f"dispatches={row.get('dispatches')} {cap_str}"
        )
    click.echo("\n".join(lines))


@census_group.command("dispatches")
@click.option(
    "--session",
    default=None,
    help="Census a single session id instead of the whole project corpus.",
)
@click.option(
    "--project-dir",
    type=click.Path(path_type=str, file_okay=False),
    default=None,
    help="Transcript directory. Defaults to NX_CENSUS_PROJECT_DIR, else the "
    "~/.claude/projects entry for the current working directory.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON (per-dispatch rows, ledger-consumable subagent_type) instead "
    "of the human table.",
)
def dispatches_cmd(
    session: str | None,
    project_dir: str | None,
    as_json: bool,
) -> None:
    """Recognize Agent dispatches (subagent_type + ordinal) from the transcript.

    nexus-h33x8.2: the transcript-based recognizer nexus-nu7fo's
    name-morphology guard could never provide, because the Agent tool
    carries no ``name`` parameter. Reports RECOGNIZED dispatches only —
    it does not verify EXPECT rows were written, does not compute
    undeclared/BLINDSPOT, and does not modify or replace
    ``tests/e2e/lib/expectations.sh``.

    Exits non-zero when the run measured *nothing* — same UNMEASURABLE-
    vs-zero contract as ``nx census capability``.
    """
    import pathlib as _pathlib  # noqa: PLC0415 — stdlib deferred to subcommand scope

    from nexus.census import (  # noqa: PLC0415 — deferred; census only needed here
        census_corpus_dispatches,
        default_project_dir,
        dispatches_to_json,
        render_dispatches_text,
    )

    root = _pathlib.Path(project_dir) if project_dir else default_project_dir()
    result = census_corpus_dispatches(root, session=session)
    click.echo(dispatches_to_json(result) if as_json else render_dispatches_text(result), nl=False)
    if result.exit_code:
        raise SystemExit(result.exit_code)
