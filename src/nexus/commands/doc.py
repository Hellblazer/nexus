# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-082: ``nx doc`` — doc-build token rendering + validation.

Two subcommands share one engine:

* ``nx doc render <path>...`` — parse, resolve, emit
  ``<stem>.rendered.md`` sibling.
* ``nx doc validate <path>...`` — same parse + resolve, no emit; exits
  non-zero on any unresolved token.

Exit contract::

    0  — all tokens resolved (or ``--allow-unresolved`` and nothing
         structural went wrong)
    1  — one or more tokens unresolved (validate mode or strict render)
    2  — argument / IO error (no file, bad flag, etc.)
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

from nexus.config import load_config
from nexus.doc.render import RenderError, render_file
from nexus.doc.resolvers import BeadResolver, RdrResolver, ResolverRegistry


@click.group("doc")
def doc() -> None:
    """Doc-build token rendering (RDR-082)."""


def _default_registry(project_root: Path) -> ResolverRegistry:
    """Build the v1 Resolver registry: bead + RDR, nothing else.

    RDR-083 extends this at its own registration time; 082 stays on
    system-of-record token families only.
    """
    cfg = {}
    try:
        cfg = load_config(project_root)
    except Exception:
        cfg = {}
    rdr_paths = (cfg.get("indexing") or {}).get("rdr_paths") or ["docs/rdr"]
    rdr_dir = project_root / rdr_paths[0]
    return ResolverRegistry({
        "bd": BeadResolver(),
        "rdr": RdrResolver(rdr_dir=rdr_dir),
    })


@doc.command("render")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--out-dir", type=click.Path(file_okay=False, path_type=Path),
    help="Write rendered files into this directory (mirrors source names). "
         "Default: sibling '<stem>.rendered.md' next to each source file.",
)
@click.option(
    "--allow-unresolved", is_flag=True,
    help="Preserve unresolved tokens as literal text instead of failing.",
)
@click.option(
    "--project-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root for resolver context (bead DB, rdr_paths). "
         "Default: current working directory.",
)
def render_cmd(
    paths: tuple[Path, ...],
    out_dir: Path | None,
    allow_unresolved: bool,
    project_root: Path | None,
) -> None:
    """Render markdown tokens into resolved sidecar files."""
    root = project_root or Path.cwd()
    registry = _default_registry(root)

    total_resolved = 0
    total_misses = 0
    try:
        for path in paths:
            result = render_file(
                path,
                registry,
                out_dir=out_dir,
                allow_unresolved=allow_unresolved,
                emit=True,
            )
            total_resolved += result.resolved
            for tok, reason in result.unresolved:
                click.echo(
                    f"{path}:{tok.lineno}:{tok.col}: unresolved {tok.raw} — {reason}",
                    err=True,
                )
                total_misses += 1
    except RenderError as exc:
        click.echo(f"render error: {exc}", err=True)
        sys.exit(1)
    except OSError as exc:
        click.echo(f"io error: {exc}", err=True)
        sys.exit(2)

    click.echo(f"rendered {total_resolved} tokens across {len(paths)} file(s)")
    if total_misses:
        click.echo(f"note: {total_misses} unresolved (preserved verbatim)", err=True)


@doc.command("validate")
@click.argument("paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--project-root", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project root for resolver context. Default: cwd.",
)
def validate_cmd(paths: tuple[Path, ...], project_root: Path | None) -> None:
    """Parse + resolve without emitting. Exits non-zero on any miss."""
    root = project_root or Path.cwd()
    registry = _default_registry(root)

    total_misses = 0
    total_ok = 0
    for path in paths:
        try:
            result = render_file(
                path, registry, allow_unresolved=True, emit=False,
            )
        except OSError as exc:
            click.echo(f"io error: {exc}", err=True)
            sys.exit(2)
        for tok, reason in result.unresolved:
            click.echo(
                f"{path}:{tok.lineno}:{tok.col}: {tok.raw} — {reason}",
                err=True,
            )
            total_misses += 1
        total_ok += result.resolved

    click.echo(
        f"validated {total_ok} tokens across {len(paths)} file(s); "
        f"{total_misses} unresolved"
    )
    if total_misses:
        sys.exit(1)
