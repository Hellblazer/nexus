# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx review — per-commit automated review (bead nexus-jh86x).

Invoked by the post-commit git hook stanza (``nx hooks install``), and by
hand for a one-off. See :mod:`nexus.commit_review` for why this exists and
what it deliberately does not do.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from nexus.commands._helpers import t2_handle
from nexus.commit_review import (
    REVIEW_PROJECT,
    VERDICTS,
    CommitReviewError,
    ReviewCoverage,
    ReviewResult,
    iter_review_records,
    parse_record_verdicts,
    pop_review_queue,
    record_title,
    review_commit,
    review_coverage,
    review_queue_depth,
)
from nexus.config import CommitReviewConfig, get_commit_review_config

if TYPE_CHECKING:
    from nexus.db.t2 import T2Database


def _resolve_sha(repo: Path, rev: str) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", rev],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise click.ClickException(f"cannot resolve {rev!r} in {repo}: {exc}") from exc


@click.group("review")
def review_group() -> None:
    """Automated review of committed work."""


@review_group.command("commit")
@click.argument("rev", default="HEAD")
@click.option(
    "--repo",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    help="Repository to review in (default: current directory).",
)
@click.option(
    "--quiet",
    is_flag=True,
    help="Suppress the one-line verdict notice; the T2 record is still written.",
)
@click.option(
    "--drain",
    is_flag=True,
    help=(
        "After REV, review every sha the post-commit hook queued while a "
        "reviewer was running, until the queue is empty. The hook passes this."
    ),
)
def review_commit_cmd(rev: str, repo: Path, quiet: bool, drain: bool) -> None:
    """Review REV (default HEAD) and record findings in T2.

    ALWAYS exits 0. This is what the post-commit hook calls, and a hook
    that can fail a commit is a footgun during a tag-push sequence that
    has to land in tight succession. Failures are reported on stderr and
    in the structured log, never as an exit code.

    With ``--drain``, the queue the hook appends to on a burst is popped
    and reviewed after REV, repeatedly, until a pop returns nothing: a
    commit that landed while this reviewer ran is reviewed by this
    reviewer instead of being dropped (2026-09-04, session nexus-65).
    """
    repo = repo.resolve()
    try:
        sha = _resolve_sha(repo, rev)
    except click.ClickException as exc:
        click.echo(f"nx review: {exc.message}", err=True)
        return

    cfg = get_commit_review_config(repo_root=repo)

    try:
        with t2_handle() as db:
            result = asyncio.run(
                review_commit(repo=repo, sha=sha, cfg=cfg, put=db.memory.put, memory=db.memory)
            )
            _report(sha, result, quiet=quiet)
            if drain:
                _drain_queue(repo, cfg, db, quiet=quiet)
    except Exception as exc:  # noqa: BLE001 - never fail a commit
        click.echo(f"nx review: skipped ({exc})", err=True)
        return


def _drain_queue(repo: Path, cfg: CommitReviewConfig, db: T2Database, *, quiet: bool) -> None:
    """Pop and review queued shas until the queue is empty. A sha that
    cannot be reviewed is reported and dropped, never re-queued: a
    poison entry must not pin the drainer forever."""
    while queued := pop_review_queue(repo):
        for queued_sha in queued:
            try:
                result = asyncio.run(
                    review_commit(
                        repo=repo, sha=queued_sha, cfg=cfg, put=db.memory.put, memory=db.memory
                    )
                )
            except Exception as exc:  # noqa: BLE001 - never fail, never re-queue
                click.echo(f"nx review {queued_sha[:12]} (queued): skipped ({exc})", err=True)
                continue
            _report(queued_sha, result, quiet=quiet, queued=True)


def _report(sha: str, result: ReviewResult, *, quiet: bool, queued: bool = False) -> None:
    if quiet:
        return
    tag = " (queued)" if queued else ""

    if result.skipped_reason is not None:
        click.echo(f"nx review {sha[:12]}{tag}: skipped ({result.skipped_reason})", err=True)
        return

    if not result.findings:
        click.echo(f"nx review {sha[:12]}{tag}: no findings", err=True)
        return

    counts = {v: sum(1 for f in result.findings if f.verdict == v) for v in VERDICTS}
    summary = ", ".join(f"{v}={counts[v]}" for v in VERDICTS if counts[v])
    click.echo(
        f"nx review {sha[:12]}{tag}: {summary} → nx memory get -p {REVIEW_PROJECT} "
        f"-t {record_title(sha)}",
        err=True,
    )


@review_group.command("show")
@click.argument("rev", default="HEAD")
@click.option(
    "--repo",
    type=click.Path(file_okay=False, path_type=Path),
    default=".",
    help="Repository the commit belongs to (default: current directory).",
)
def review_show_cmd(rev: str, repo: Path) -> None:
    """Print the stored review record for REV, if one exists."""
    repo = repo.resolve()
    sha = _resolve_sha(repo, rev)
    with t2_handle() as db:
        entry = db.memory.get(project=REVIEW_PROJECT, title=record_title(sha))
    if not entry:
        raise click.ClickException(
            f"no review recorded for {sha[:12]}; an amended or rebased commit is "
            f"recorded under the sha it was first reviewed as "
            f"(run: nx review commit {sha[:12]} --repo {repo}, which names it)"
        )
    content = entry.get("content") if isinstance(entry, dict) else str(entry)
    click.echo(content)
    verdicts = parse_record_verdicts(content or "")
    if verdicts:
        click.echo("", err=True)
        click.echo(
            "verdicts: " + ", ".join(f"{k}={v}" for k, v in verdicts.items()), err=True
        )


def _iter_review_records(db) -> list[dict]:
    """Review records only: listed by the reviewer's ``agent`` attribution,
    then confirmed by the record's own first line (``iter_review_records``).

    The records share the ``nexus`` project with thousands of unrelated
    entries (Sam's ruling, 2026-09-02). A title-prefix sieve over a
    whole-project download counted 401 human notes as reviews on
    2026-09-04; the agent filter is server-side and a foreign writer
    cannot produce it by accident.
    """
    try:
        return iter_review_records(db.memory)
    except Exception as exc:  # noqa: BLE001 - a census must report, not crash
        click.echo(f"nx census reviews: T2 unreachable ({exc})", err=True)
        sys.exit(1)


def reviews_census(
    db, *, limit: int | None = None, records: list[dict] | None = None
) -> dict[str, int]:
    """Count findings by verdict across stored review records.

    Extracted from the Click command so the census logic is testable
    without a CLI runner, and so the counter is fed :func:`render_record`'s
    OWN output in test rather than a literal that resembles it. *records*
    lets the command fetch the list once and share it with
    :func:`reviews_coverage`.
    """
    totals: dict[str, int] = {v: 0 for v in VERDICTS}
    totals_records = 0
    clean = 0
    for row in _iter_review_records(db) if records is None else records:
        content = row.get("content", "") if isinstance(row, dict) else ""
        totals_records += 1
        counts = parse_record_verdicts(content)
        if not counts:
            clean += 1
        for verdict, n in counts.items():
            totals[verdict] = totals.get(verdict, 0) + n
        if limit is not None and totals_records >= limit:
            break
    totals["_records"] = totals_records
    totals["_clean"] = clean
    return totals


def reviews_coverage(records: list[dict], repo: Path) -> tuple[ReviewCoverage | None, int]:
    """``(coverage, queued)`` for *repo*: the commits since the newest
    reachable tag with no review record among *records*, and the
    burst-queue depth. ``(None, 0)`` when *repo* is not a git repository,
    so the census still prints its record counts from anywhere."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"], cwd=repo, capture_output=True, check=True
        )
    except (subprocess.CalledProcessError, OSError):
        return None, 0
    cfg = get_commit_review_config(repo_root=repo)
    try:
        coverage = review_coverage(repo, records, max_diff_bytes=cfg.max_diff_bytes)
        depth = review_queue_depth(repo)
    except (subprocess.CalledProcessError, OSError, CommitReviewError) as exc:
        # The record counts above are still worth printing; say why the gap
        # half is missing rather than tracebacking the whole census.
        click.echo(f"nx census reviews: coverage unavailable ({exc})", err=True)
        return None, 0
    return coverage, depth
