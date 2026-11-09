# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci``: CI wakes read from the CI board (nexus-1d6pz.11.4)."""
from __future__ import annotations

import math
import re

import click

from nexus import ci_wait

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")


def _store():
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: CLI startup cost
    return HttpTupleStore()


@click.group(name="ci")
def ci_group() -> None:
    """CI results read from the CI board, without polling GitHub."""


@ci_group.command(name="wait")
@click.argument("sha")
@click.option("--timeout", "timeout_min", type=click.FloatRange(min=0, max=24 * 60), default=60.0,
              show_default=True, help="Minutes to wait for the sha's --done row (at most a day).")
@click.option("--fresh", is_flag=True,
              help="Count only a row that arrives after the wait starts: for a wait on a rerun, "
                   "while the earlier attempt's row is still on the topic.")
@click.option("--repo", default="nexus", show_default=True, help="Repository part of the board topic.")
@click.option("--branch", default="develop", show_default=True, help="Branch part of the board topic.")
@click.option("--gh-repo", default=None,
              help="owner/name for the GitHub reads (a row with no conclusions, and the one read at "
                   "the deadline) [default: Hellblazer/<repo>].")
@click.option("--park-s", type=click.IntRange(1, ci_wait.PARK_CAP_S), default=ci_wait.PARK_CAP_S,
              hidden=True, help="Seconds per park (tests).")
def ci_wait_cmd(
    sha: str, timeout_min: float, fresh: bool, repo: str, branch: str, gh_repo: str | None, park_s: int,
) -> None:
    """Wait for SHA's CI runs to complete and print one line with the result.

    Parks on board/ci/<repo>-<branch>--done until the row for SHA (a full sha
    or a prefix of at least 7 hex digits) is there; a row already on the topic
    counts unless --fresh, and when a rerun gave the sha more than one row,
    the newest is used. Meant to run as a background command, so its exit
    wakes the session that pushed. The board wake is best effort, so at the
    deadline a full SHA is looked up on GitHub once: finished runs are
    reported as usual, unfinished ones as a timeout.

    Exit 0 when every run succeeded (neutral and skipped count), 1 on any
    failure (failure, timed_out, startup_failure, action_required), 3 when
    runs were cancelled or stale and none failed, 2 on timeout, 4 when a
    conclusion could not be read, the board's outcome disagrees with the
    conclusions, or the row lists fewer runs than its count (the adapter's
    size cap) and GitHub could not supply the rest, and 5 when the wait could not run (a bad SHA, no tuple store,
    a refused board read, or the board unreachable until the deadline).
    Click's own usage errors (a missing SHA, --timeout out of range) exit 2.
    """
    if not math.isfinite(timeout_min):
        click.echo(f"{sha} unusable: --timeout must be a number of minutes")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    if not _SHA.fullmatch(sha):
        click.echo(f"{sha} unusable: give a commit sha, 7 to 40 hex digits")
        raise SystemExit(ci_wait.EXIT_UNUSABLE)
    sha = sha.lower()
    gh = gh_repo or f"Hellblazer/{repo}"
    subspace = ci_wait.done_subspace(repo, branch)
    try:
        store = _store()
    except Exception as exc:  # noqa: BLE001 — no store, no wait: say so, never exit 1 ("a run failed")
        click.echo(f"{sha[:12]} unusable: no tuple store: {type(exc).__name__}: {exc}")
        raise SystemExit(ci_wait.EXIT_UNUSABLE) from None
    try:
        row = ci_wait.find_done_row(
            store, subspace, sha, timeout_s=timeout_min * 60, fresh=fresh, park_s=park_s,
        )
    except ci_wait.BoardUnreadableError as exc:
        click.echo(f"{sha[:12]} unusable: {subspace} could not be read: {exc}")
        raise SystemExit(ci_wait.EXIT_UNUSABLE) from None
    if row is None:
        if fresh:
            # GitHub cannot tell a rerun that has not started from the earlier
            # attempt it would replace, so a --fresh wait never reads it.
            click.echo(f"{sha[:12]} timeout: no new row on {subspace} after {timeout_min:g} min (--fresh)")
            raise SystemExit(ci_wait.EXIT_TIMEOUT)
        line, code = _at_deadline(sha, gh, branch, subspace, timeout_min)
        click.echo(line)
        raise SystemExit(code)
    line, code = ci_wait.summarize(ci_wait.complete_runs(ci_wait.fill_conclusions(row, gh), gh, branch))
    click.echo(line)
    raise SystemExit(code)


def _at_deadline(sha: str, gh_repo: str, branch: str, subspace: str, timeout_min: float) -> tuple[str, int]:
    """No row by the deadline: one GitHub read of the sha's push runs tells a
    dropped board wake (every run finished) from CI still running."""
    head = f"{sha[:12]} timeout: no row on {subspace} after {timeout_min:g} min"
    if len(sha) != 40:
        return f"{head}; give the full sha to check GitHub at the deadline", ci_wait.EXIT_TIMEOUT
    runs = ci_wait.gh_push_runs(gh_repo, sha, branch)
    if runs is None:
        return f"{head}; GitHub could not be read", ci_wait.EXIT_TIMEOUT
    if not runs:
        return f"{head}; GitHub lists no push runs for it", ci_wait.EXIT_TIMEOUT
    unfinished = [r for r in runs if r.conclusion is None]
    if unfinished:
        return f"{head}; {len(unfinished)} of {len(runs)} runs still running", ci_wait.EXIT_TIMEOUT
    line, code = ci_wait.summarize(
        ci_wait.DoneRow("", sha, tuple(runs), f"https://github.com/{gh_repo}/commit/{sha}"),
    )
    return f"{line} (no board row: the wake was dropped; read from GitHub)", code
