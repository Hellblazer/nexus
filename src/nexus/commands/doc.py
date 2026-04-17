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

from nexus.commands._helpers import default_db_path
from nexus.config import load_config
from nexus.db.t2 import T2Database
from nexus.doc.citations import (
    extensions_report,
    grounding_report,
    scan_citations,
)
from nexus.doc.render import RenderError, render_file
from nexus.doc.resolvers import BeadResolver, RdrResolver, ResolverRegistry
from nexus.doc.resolvers_corpus import AnchorResolver


@click.group("doc")
def doc() -> None:
    """Doc-build token rendering (RDR-082)."""


def _default_registry(project_root: Path) -> ResolverRegistry:
    """Build the default Resolver registry: bead + RDR system-of-record
    resolvers (RDR-082) plus corpus-evidence resolvers (RDR-083) when
    T2 is reachable.

    If T2 can't be opened (fresh install, no db) we silently degrade
    to the bead + RDR pair — a project that has no projection data
    has nothing for ``nx-anchor`` to render anyway.
    """
    cfg = {}
    try:
        cfg = load_config(project_root)
    except Exception:
        cfg = {}
    rdr_paths = (cfg.get("indexing") or {}).get("rdr_paths") or ["docs/rdr"]
    rdr_dir = project_root / rdr_paths[0]

    registry = ResolverRegistry({
        "bd": BeadResolver(),
        "rdr": RdrResolver(rdr_dir=rdr_dir),
    })

    # RDR-083: corpus-evidence resolvers.  Register lazily — if T2 is
    # unreachable we skip rather than crash the render.
    try:
        db = T2Database(default_db_path())
        registry.register("nx-anchor", AnchorResolver(taxonomy=db.taxonomy))
    except Exception:
        pass

    return registry


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


# ── RDR-083 validators ───────────────────────────────────────────────────────


@doc.command("check-grounding")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--fail-under", type=float, default=None,
    help="Exit non-zero when chash-coverage ratio falls under this value (0.0-1.0).",
)
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]),
    default="table", help="Report format.",
)
def check_grounding_cmd(
    paths: tuple[Path, ...], fail_under: float | None, output_format: str,
) -> None:
    """Report citation-coverage per markdown file.

    Coverage = chash citations / (chash + prose + bracket). Prose and
    bracketed citations are not machine-verifiable — the ratio tells you
    how much of this doc's grounding is upgradeable.
    """
    import json
    results = []
    any_fail = False
    for path in paths:
        text = path.read_text(errors="replace")
        cites = scan_citations(text)
        report = grounding_report(cites)
        results.append({
            "path": str(path),
            "total": report.total,
            "chash": report.chash_count,
            "prose": report.prose_count,
            "bracket": report.bracket_count,
            "coverage": round(report.coverage, 4),
        })
        if (
            fail_under is not None
            and report.total > 0
            and report.coverage < fail_under
        ):
            any_fail = True

    if output_format == "json":
        click.echo(json.dumps(results, indent=2))
    else:
        click.echo(f"{'file':<40}{'total':>6}{'chash':>7}{'prose':>7}{'bracket':>9}{'cov':>7}")
        for r in results:
            click.echo(
                f"{r['path'][:38]:<40}"
                f"{r['total']:>6}{r['chash']:>7}"
                f"{r['prose']:>7}{r['bracket']:>9}"
                f"{r['coverage']:>7.2f}"
            )
    if any_fail:
        sys.exit(1)


@doc.command("check-extensions")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--primary-source", required=True,
    help="Collection whose projection defines 'grounded' "
         "(e.g. docs__art-grossberg-papers).",
)
@click.option(
    "--threshold", type=float, default=0.70,
    help="Projection-similarity cutoff. Docs at-or-above are grounded; "
         "below → author-extension candidates.",
)
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]),
    default="table",
)
def check_extensions_cmd(
    paths: tuple[Path, ...],
    primary_source: str,
    threshold: float,
    output_format: str,
) -> None:
    """Flag doc chunks that don't project into a primary source.

    The doc's own chunks are keyed by the ``chash:`` citations it
    contains — absence of projection into ``--primary-source`` is
    treated as an author-extension candidate.
    """
    import json
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    results = []
    any_candidate = False
    try:
        db = T2Database(default_db_path())
    except Exception as exc:
        click.echo(f"cannot open T2: {exc}", err=True)
        sys.exit(2)

    for path in paths:
        text = path.read_text(errors="replace")
        cites = scan_citations(text)
        # v1: collect the unique chash values as proxy doc ids; when
        # chash-to-doc resolution lands, swap this out for the resolved
        # document ids.
        doc_ids = sorted({c.chash for c in cites if c.chash})
        report = extensions_report(
            doc_ids,
            primary_source=primary_source,
            threshold=threshold,
            taxonomy=db.taxonomy,
        )
        results.append({
            "path": str(path),
            "checked": report.checked,
            "candidates": [
                {"doc_id": d, "similarity": s} for d, s in report.candidates
            ],
            "no_data": list(report.no_data),
        })
        if report.candidates:
            any_candidate = True

    if output_format == "json":
        click.echo(json.dumps(results, indent=2))
    else:
        click.echo(
            f"{'file':<36}{'checked':>8}{'candidates':>12}{'no-data':>9}"
        )
        for r in results:
            click.echo(
                f"{r['path'][:34]:<36}{r['checked']:>8}"
                f"{len(r['candidates']):>12}{len(r['no_data']):>9}"
            )
            for c in r["candidates"]:
                click.echo(
                    f"  candidate: {c['doc_id'][:40]} "
                    f"(similarity={c['similarity']:.3f})"
                )
    if any_candidate:
        sys.exit(1)
