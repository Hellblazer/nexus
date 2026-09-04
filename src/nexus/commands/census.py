# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx census`` — per-capability tool-use census over transcript JSONL.

nexus-h33x8.1. See :mod:`nexus.census` for the substrate and for why
this reports counts and refuses verdicts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from nexus.commit_review import ReviewCoverage


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
def capability_cmd(
    session: str | None,
    since: str | None,
    project_dir: str | None,
    as_json: bool,
) -> None:
    """Count tool calls per capability, split orchestrator vs subagent.

    Exits non-zero when the run measured *nothing* — an empty, corrupt,
    or tool-call-free scope reports UNMEASURABLE rather than a clean
    zero. A zero row in a measurable run is a real zero.
    """
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


@census_group.command("reviews")
@click.option("--as-json", is_flag=True, help="Emit JSON instead of text.")
def reviews_cmd(as_json: bool) -> None:
    """Count per-commit review findings by verdict (bead nexus-jh86x).

    Reads the review records the post-commit hook writes to T2 and
    reports the FIX-NOW / FILE / DROP distribution across them, plus how
    many commits were reviewed and found clean.

    Reviewed-and-clean is reported separately from not-reviewed: a census
    that cannot tell those apart would read an unarmed hook as a clean
    codebase (the nexus-moht0 vacuous-gate doctrine).
    """
    import json as _json  # noqa: PLC0415 — stdlib deferred to subcommand scope

    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — deferred; T2 only needed here
    from nexus.commands.review_cmd import reviews_census  # noqa: PLC0415 — deferred; avoids import cycle at module load
    from nexus.commit_review import VERDICTS  # noqa: PLC0415 — deferred with its siblings

    from pathlib import Path  # noqa: PLC0415 — stdlib deferred to subcommand scope

    from nexus.commands.hooks import hook_stanza_state  # noqa: PLC0415 — deferred; avoids click group import at module load
    from nexus.commands.review_cmd import (  # noqa: PLC0415 — deferred with reviews_census
        _iter_review_records,
        reviews_coverage,
    )

    repo = Path.cwd()
    with t2_handle() as db:
        review_records = _iter_review_records(db)
        totals = reviews_census(db, records=review_records)
    coverage, queued = reviews_coverage(review_records, repo)

    records = totals.pop("_records", 0)
    clean = totals.pop("_clean", 0)

    state = hook_stanza_state(repo)

    if as_json:
        click.echo(_json.dumps(
            {
                "records": records,
                "clean": clean,
                "verdicts": totals,
                "hook_state": state,
                "coverage": None if coverage is None else {
                    "since": coverage.since,
                    "commits": coverage.commits,
                    "patchless": coverage.patchless,
                    "unreviewed": [{"sha": g.sha, "subject": g.subject} for g in coverage.gaps],
                },
                "queued": queued,
            },
            indent=2,
        ))
        return

    remedy = {
        "stale": " (nx hooks update refreshes it)",
        "not installed": " (nx hooks install arms it)",
        "unmanaged": " (a foreign hook; nx hooks install appends the stanza)",
        "unknown": " (not a git repository, or git did not answer)",
    }.get(state, "")
    click.echo(f"Post-commit reviewer in this repo: {state}{remedy}")

    if not records:
        click.echo(
            "No commit reviews recorded (records expire after the configured "
            "ttl). Either the hook was not armed when commits happened, or "
            "nothing has committed since."
        )
        _echo_coverage(coverage, queued)
        return

    click.echo(f"Commit reviews: {records} record(s), {clean} clean")
    for verdict in VERDICTS:
        click.echo(f"  {verdict:<8} {totals.get(verdict, 0)}")
    _echo_coverage(coverage, queued)


def _echo_coverage(coverage: ReviewCoverage | None, queued: int) -> None:
    """The gap half of the census. A commit with no record is named, never
    summed into "clean": the hook's burst guard queued (before 2026-09-04,
    dropped) commits that landed while a reviewer ran, and the record count
    alone cannot show which commits those were."""
    if coverage is None:
        return
    n = len(coverage.gaps)
    patchless = f", {coverage.patchless} without a patch" if coverage.patchless else ""
    click.echo(
        f"Unreviewed since {coverage.since}: {n} of {coverage.commits} commit(s){patchless}"
    )
    for gap in coverage.gaps:
        click.echo(f"  {gap.sha[:12]}  {gap.subject}")
    if n:
        click.echo("  (review one: nx review commit <sha>; a burst drains at the next commit)")
    if queued:
        click.echo(f"Review queue: {queued} waiting (drained by the running reviewer, or the next commit)")
