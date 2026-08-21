# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx prose`` : lint markdown prose against ``docs/writing-style.md``."""
from __future__ import annotations

import sys
from pathlib import Path

import click

from nexus.prose_lint import (
    check_against_baseline,
    lint_file,
    load_baseline,
    write_baseline,
)


@click.group()
def prose() -> None:
    """Prose style tools (see docs/writing-style.md)."""


def _collect(paths: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out.extend(sorted(q for q in p.rglob("*.md") if q.is_file()))
        else:
            out.append(p)
    seen: set[Path] = set()
    unique: list[Path] = []
    for q in out:
        r = q.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(q)
    return unique


@prose.command("lint")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--baseline",
    type=click.Path(path_type=Path),
    default=None,
    help="JSON ratchet file: {relative path: allowed finding count}.",
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Root that baseline paths are relative to (default: cwd).",
)
@click.option(
    "--write-baseline",
    "write_baseline_flag",
    is_flag=True,
    help="Record current counts into --baseline instead of checking.",
)
def lint(
    paths: tuple[Path, ...],
    baseline: Path | None,
    root: Path | None,
    write_baseline_flag: bool,
) -> None:
    """Lint markdown files for LLM-register patterns.

    Exit 0 clean, 1 on findings (or baseline violations), 2 when there
    was nothing to lint.
    """
    if write_baseline_flag and baseline is None:
        raise click.UsageError("--write-baseline requires --baseline FILE")
    targets = _collect(paths)
    if not targets:
        click.echo("nothing to lint", err=True)
        sys.exit(2)

    base_root = (root or Path.cwd()).resolve()

    def _rel(p: Path) -> str:
        rp = p.resolve()
        try:
            return rp.relative_to(base_root).as_posix()
        except ValueError:
            return rp.as_posix()

    counts: dict[str, int] = {}
    total = 0
    for path in targets:
        findings = lint_file(path)
        counts[_rel(path)] = len(findings)
        total += len(findings)
        if baseline is None or write_baseline_flag:
            for f in findings:
                click.echo(f.render(path), err=True)

    if baseline is not None and write_baseline_flag:
        write_baseline(baseline, counts)
        click.echo(f"baseline written: {baseline} ({sum(1 for v in counts.values() if v)} files with findings)")
        return

    if baseline is not None:
        problems = check_against_baseline(counts, load_baseline(baseline))
        if problems:
            for p in problems:
                click.echo(p, err=True)
            click.echo(f"\n{len(problems)} baseline violation(s)", err=True)
            sys.exit(1)
        click.echo(f"{len(targets)} file(s) within baseline ({total} known findings)")
        return

    if total:
        click.echo(f"\n{total} finding(s) in {sum(1 for v in counts.values() if v)} of {len(targets)} file(s)", err=True)
        sys.exit(1)
    click.echo(f"{len(targets)} file(s) clean")
