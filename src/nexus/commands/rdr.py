# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx rdr`` — RDR authoring helpers.

Currently exposes a single subcommand, ``lint``, that scans RDR markdown
files for frontmatter hazards that break downstream indexing. The first
hazard it covers is ``nexus-u7ek``: YAML flow sequences containing
unquoted ``#``-prefixed refs (``prs: [#381, #382]``) silently parse as an
empty list + comment, scan past the closing ``]``, and run off the end
of the frontmatter.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import click
import yaml


# Matches a flow-sequence opener followed (eventually) by an unquoted
# ``#`` before the closing ``]``. ``[^\]"']*?`` lets us span multiple
# lines (PyYAML's multi-line flow sequences parse silently into empty
# lists when ``#`` introduces comments mid-sequence — a true false
# negative for the single-line regex). The ``"'`` exclusion keeps quoted
# strings from being mis-flagged as the hazard.
_HASH_REF_IN_FLOW_SEQ = re.compile(r":\s*\[[^\]\"']*?#", re.DOTALL)


def _frontmatter_block(text: str) -> str | None:
    """Return the frontmatter block (without delimiters) or None."""
    if not text.startswith("---"):
        return None
    idx = text.find("\n---", 3)
    if idx == -1:
        return None
    return text[3:idx]


def _lint_one(path: Path) -> list[str]:
    """Return a list of human-readable findings for *path* (empty if clean)."""
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: read failed ({type(exc).__name__}: {exc})"]

    fm = _frontmatter_block(text)
    if fm is None:
        return findings

    # _frontmatter_block returns text starting at index 3 (right after the
    # opening ``---``), which is typically the trailing ``\n`` of that line.
    # Strip the leading newline so the first content line maps cleanly to
    # file line 2 (file line 1 is the opening ``---``).
    fm_body = fm.lstrip("\n")

    for m in _HASH_REF_IN_FLOW_SEQ.finditer(fm_body):
        # If the opener line is itself a YAML comment (``# note: [#381]``),
        # the ``: [`` is inside a comment and the whole thing is benign.
        # Find the start of the line containing the match opener and
        # check the first non-whitespace char.
        line_start = fm_body.rfind("\n", 0, m.start()) + 1
        if fm_body[line_start:m.start()].lstrip().startswith("#"):
            continue
        # Line number within fm_body. +2 for the opening ``---`` line.
        line_no = fm_body.count("\n", 0, m.start()) + 2
        snippet = fm_body[m.start():m.end()].replace("\n", " ").strip()
        findings.append(
            f"{path}:{line_no}: unquoted #-ref in YAML flow sequence "
            f"({snippet!r}); quote the refs: "
            f'prs: ["#381", "#382"]'
        )

    try:
        yaml.safe_load(fm)
    except yaml.YAMLError as exc:
        findings.append(f"{path}: frontmatter YAML parse error: {exc}")

    return findings


@click.group()
def rdr() -> None:
    """RDR authoring helpers."""


@rdr.command("lint")
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Scan this directory recursively for *.md (default: docs/rdr/ if it exists).",
)
def lint(paths: tuple[Path, ...], root: Path | None) -> None:
    """Lint RDR frontmatter for parse hazards.

    Checks each *.md for frontmatter that would fail downstream YAML
    parsing — primarily the ``prs: [#NNN]`` flow-sequence hazard
    (nexus-u7ek). Exits non-zero when any finding is reported.
    """
    targets: list[Path] = []
    if paths:
        for p in paths:
            if p.is_dir():
                targets.extend(sorted(p.rglob("*.md")))
            else:
                targets.append(p)
    else:
        scan_root = root or Path("docs/rdr")
        if not scan_root.exists():
            click.echo(
                f"no paths given and {scan_root} not found; nothing to lint",
                err=True,
            )
            sys.exit(2)
        targets = sorted(scan_root.rglob("*.md"))

    all_findings: list[str] = []
    files_with_findings = 0
    for path in targets:
        per_file = _lint_one(path)
        if per_file:
            files_with_findings += 1
            all_findings.extend(per_file)

    if all_findings:
        for f in all_findings:
            click.echo(f, err=True)
        click.echo(
            f"\n{len(all_findings)} finding(s) in {files_with_findings} of "
            f"{len(targets)} file(s)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"clean: {len(targets)} file(s) scanned")
