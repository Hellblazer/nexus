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

import click

from nexus.commands._helpers import t2_handle
from nexus.commit_review import (
    iter_review_records,
    REVIEW_PROJECT,
    VERDICTS,
    parse_record_verdicts,
    record_title,
    review_commit,
)
from nexus.config import get_commit_review_config


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
def review_commit_cmd(rev: str, repo: Path, quiet: bool) -> None:
    """Review REV (default HEAD) and record findings in T2.

    ALWAYS exits 0. This is what the post-commit hook calls, and a hook
    that can fail a commit is a footgun during a tag-push sequence that
    has to land in tight succession. Failures are reported on stderr and
    in the structured log, never as an exit code.
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
                review_commit(repo=repo, sha=sha, cfg=cfg, put=db.memory.put)
            )
    except Exception as exc:  # noqa: BLE001 - never fail a commit
        click.echo(f"nx review: skipped ({exc})", err=True)
        return

    if quiet:
        return

    if result.skipped_reason is not None:
        click.echo(f"nx review {sha[:12]}: skipped ({result.skipped_reason})", err=True)
        return

    if not result.findings:
        click.echo(f"nx review {sha[:12]}: no findings", err=True)
        return

    counts = {v: sum(1 for f in result.findings if f.verdict == v) for v in VERDICTS}
    summary = ", ".join(f"{v}={counts[v]}" for v in VERDICTS if counts[v])
    click.echo(
        f"nx review {sha[:12]}: {summary} → nx memory get -p {REVIEW_PROJECT} "
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
            f"no review recorded for {sha[:12]} "
            f"(run: nx review commit {sha[:12]} --repo {repo})"
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


def reviews_census(db, *, limit: int | None = None) -> dict[str, int]:
    """Count findings by verdict across stored review records.

    Extracted from the Click command so the census logic is testable
    without a CLI runner, and so the counter is fed :func:`render_record`'s
    OWN output in test rather than a literal that resembles it.
    """
    totals: dict[str, int] = {v: 0 for v in VERDICTS}
    totals_records = 0
    clean = 0
    for row in _iter_review_records(db):
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
