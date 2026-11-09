# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx ci``: CI wakes read from the CI board (nexus-1d6pz.11.4)."""
from __future__ import annotations

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
@click.option("--timeout", "timeout_min", type=float, default=60.0, show_default=True,
              help="Minutes to wait for the sha's --done row.")
@click.option("--repo", default="nexus", show_default=True, help="Repository part of the board topic.")
@click.option("--branch", default="develop", show_default=True, help="Branch part of the board topic.")
@click.option("--gh-repo", default=None,
              help="owner/name for the one GitHub read of a row that carries no conclusions "
                   "[default: Hellblazer/<repo>].")
@click.option("--park-s", type=click.IntRange(1, ci_wait.PARK_CAP_S), default=ci_wait.PARK_CAP_S,
              hidden=True, help="Seconds per park (tests).")
def ci_wait_cmd(sha: str, timeout_min: float, repo: str, branch: str, gh_repo: str | None, park_s: int) -> None:
    """Wait for SHA's CI runs to complete and print one line with the result.

    Parks on board/ci/<repo>-<branch>--done until the row for SHA (a full sha
    or a prefix of at least 7 hex digits) is there; a row already on the topic
    counts, and when a rerun gave the sha more than one row, the newest is
    used. Meant to run as a background command, so its exit wakes the session
    that pushed. Exit 0 when every run succeeded (neutral and skipped count),
    1 on any failure (failure, timed_out, startup_failure, action_required),
    3 when runs were cancelled or stale and none failed, 2 on timeout, and
    4 when a conclusion could not be read.
    """
    if not _SHA.fullmatch(sha):
        raise click.BadParameter("give a commit sha: 7 to 40 hex digits", param_hint="SHA")
    subspace = ci_wait.done_subspace(repo, branch)
    row = ci_wait.find_done_row(_store(), subspace, sha, timeout_s=timeout_min * 60, park_s=park_s)
    if row is None:
        click.echo(f"{sha[:12]} timeout: no row on {subspace} after {timeout_min:g} min")
        raise SystemExit(ci_wait.EXIT_TIMEOUT)
    row = ci_wait.fill_conclusions(row, gh_repo or f"Hellblazer/{repo}")
    line, code = ci_wait.summarize(row)
    click.echo(line)
    raise SystemExit(code)
