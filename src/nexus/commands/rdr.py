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


# Matches a flow-sequence opener followed by an unquoted ``#``-token.
# ``\[`` opens the sequence; optional whitespace; then ``#`` directly
# (i.e. *not* preceded by a quote). We rely on the regex being narrow
# enough that legitimate ``#``-inside-a-quoted-string ("[\"#381\"]")
# does not match.
_HASH_REF_IN_FLOW_SEQ = re.compile(r":\s*\[\s*#")


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

    for lineno, line in enumerate(fm.splitlines(), start=2):  # +1 for opening ---
        if _HASH_REF_IN_FLOW_SEQ.search(line):
            findings.append(
                f"{path}:{lineno}: unquoted #-ref in YAML flow sequence "
                f"({line.strip()!r}); quote the refs: "
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
    for path in targets:
        all_findings.extend(_lint_one(path))

    if all_findings:
        for f in all_findings:
            click.echo(f, err=True)
        click.echo(
            f"\n{len(all_findings)} finding(s) in {len(targets)} file(s)", err=True,
        )
        sys.exit(1)

    click.echo(f"clean: {len(targets)} file(s) scanned")
