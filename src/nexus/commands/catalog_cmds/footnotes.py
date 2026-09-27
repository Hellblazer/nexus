# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog footnotes <file.md>`` — GH #896 / nexus-sxiay: the
in-place converter GH #896 actually asked for (nexus-sevlu landed only
the render/validate sidecar half — see that bead's substantive-critique
note and ``docs/rdr`` history for why this command exists separately).

Converts ``[label](nx://catalog/<tumbler>)`` markdown links IN PLACE in
the SOURCE file into stable GFM footnote markers, so GitHub/GitLab/VS
Code preview — none of which resolve ``nx://catalog/`` — render a
working, informative citation instead of a dead link. All scanning,
resolution and link-safety logic lives in
:mod:`nexus.doc.catalog_links` / :mod:`nexus.doc.footnote_converter`;
this module is the CLI shell only.

Exit contract (mirrors ``nx doc validate``'s convention,
``src/nexus/commands/doc.py``):

    0 — converted cleanly (or --check found the file already current,
        or --dry-run/--to-links found nothing to report)
    1 — one or more tumblers did not resolve (left as links, reported)
        — or, under --check, the file is not in current converted form
    2 — argument / IO error, a foreign '## Footnotes' section this
        converter did not write, or the catalog service unreachable
"""
from __future__ import annotations

import difflib
from functools import partial
from pathlib import Path
from typing import Any

import click

from nexus.doc.catalog_links import CatalogLinkResolutionError
from nexus.doc.footnote_converter import (
    ConversionResult,
    FootnoteSectionConflict,
    convert_footnotes_to_links,
    convert_links_to_footnotes,
)


def _lazy_reader(holder: list[Any]) -> Any:
    """Command-invocation-scoped catalog reader cache — a multi-path
    invocation pays one client construction, not N. Reuses
    ``nx doc render``/``nx doc validate``'s own opener
    (``nexus.commands.doc._open_catalog_link_reader``) so there is one
    place that knows how to open a catalog reader for
    ``nx://catalog/`` resolution, not two.
    """
    if not holder:
        from nexus.commands.doc import _open_catalog_link_reader  # noqa: PLC0415 — deferred: avoids a hard import at CLI startup
        holder.append(_open_catalog_link_reader())
    return holder[0]


def _report_dangling(path: Path, result: ConversionResult, *, reversed_: bool) -> None:
    label = "unresolved slug" if reversed_ else "unresolved tumbler"
    for d in result.dangling:
        click.echo(f"{path}:{d.lineno}: {label} {d.tumbler}", err=True)


@click.command("footnotes")
@click.argument(
    "paths", nargs=-1, required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--check", is_flag=True,
    help="Exit non-zero if the file is not already in converted, current "
         "form (or has a dangling reference). Writes nothing.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print the diff that would be written; write nothing.",
)
@click.option(
    "--to-links", "to_links", is_flag=True,
    help="Reverse conversion: expand footnote markers back into "
         "nx://catalog/ links, and drop the Footnotes section.",
)
def footnotes_cmd(paths: tuple[Path, ...], check: bool, dry_run: bool, to_links: bool) -> None:
    """Convert nx://catalog/<tumbler> markdown links to stable GFM
    footnotes, in place — or reverse it with --to-links.

    Re-running on an already-converted file is a no-op when catalog
    state is unchanged; when it has drifted (a title edit, a merge),
    only the footnote BODIES are rewritten — markers already assigned
    in the body never move. A tumbler that no longer resolves is left
    as a link (or, for an already-converted file, its footnote body is
    marked unresolved) and reported — never silently dropped.
    """
    if check and dry_run:
        raise click.ClickException("--check and --dry-run are mutually exclusive.")
    if check and to_links:
        raise click.ClickException(
            "--check only checks the forward (link -> footnote) conversion; "
            "combine --to-links with --dry-run to preview the reverse instead."
        )

    reader_holder: list[Any] = []
    had_dangling = False
    needs_change = False

    for path in paths:
        original = path.read_text(errors="replace")
        try:
            if to_links:
                result = convert_footnotes_to_links(original)
            else:
                result = convert_links_to_footnotes(
                    original, partial(_lazy_reader, reader_holder),
                    base_dir=path.parent,
                )
        except CatalogLinkResolutionError as exc:
            click.echo(f"{path}: catalog service unreachable — {exc}", err=True)
            raise click.exceptions.Exit(2)
        except FootnoteSectionConflict as exc:
            click.echo(f"{path}: {exc}", err=True)
            raise click.exceptions.Exit(2)

        _report_dangling(path, result, reversed_=to_links)
        if result.dangling:
            had_dangling = True
        if result.changed:
            needs_change = True

        if check:
            continue

        if dry_run:
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                result.text.splitlines(keepends=True),
                fromfile=str(path), tofile=str(path),
            )
            click.echo("".join(diff), nl=False)
            continue

        if result.changed:
            path.write_text(result.text)
            click.echo(f"{path}: converted")
        else:
            click.echo(f"{path}: already up to date")

    if check:
        if needs_change or had_dangling:
            click.echo(
                "footnotes check failed: one or more files are not in "
                "converted form.", err=True,
            )
            raise click.exceptions.Exit(1)
        click.echo("footnotes check passed: all files already converted.")
        return

    if had_dangling:
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach ``footnotes`` to the shared ``catalog`` group."""
    group.add_command(footnotes_cmd)
