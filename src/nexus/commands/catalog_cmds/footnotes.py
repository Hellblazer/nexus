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

nexus-3ioz2 (critical, code-review round): the source file is read as
STRICT UTF-8 (never ``errors="replace"``, which silently corrupts
non-UTF-8 bytes and then writes the corruption back over the original)
and written atomically (a sibling ``.tmp`` file + ``Path.replace()`` —
the same pattern as ``nexus.commands.t3._save_backfill_state``), so a
crash mid-write can never leave a half-written file, and invalid input
bytes are refused outright rather than silently lossily "fixed".

GH #896's own acceptance criteria: an unresolvable tumbler must not
partially write the file. This command holds that literally — ANY
dangling reference (a raw link, in either conversion direction, that
does not resolve) means NOTHING is written for that file; every failure
is still reported, and the run exits 1. ``--dry-run`` always shows the
full picture (the diff as if the write had happened) regardless.

Exit contract (mirrors ``nx doc validate``'s convention,
``src/nexus/commands/doc.py``):

    0 — converted cleanly (or --check found the file already current,
        or --dry-run/--to-links found nothing to report)
    1 — one or more tumblers did not resolve — nothing was written for
        that file; every failure is reported — or, under --check, the
        file is not in current converted form
    2 — flag misuse (a UsageError — mutually exclusive options), a
        non-UTF-8 source file, a foreign '## Footnotes' section this
        converter did not write, or the catalog service unreachable
"""
from __future__ import annotations

import difflib
import shutil
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


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically: a sibling ``.tmp`` file,
    then ``Path.replace()`` (rename, atomic on POSIX) — mirrors
    ``nexus.commands.t3._save_backfill_state``'s tmp+rename pattern, so
    a crash mid-write never leaves *path* half-written.

    Two follow-up fixes (re-review round):

    * ``Path.replace()`` renames the tmp file's OWN inode into place —
      the tmp file was just created with umask defaults (typically
      0644), so replacing an existing 0600 file silently loosened its
      mode. The original's mode is copied onto the tmp file (when
      *path* already exists) BEFORE the replace, so the replace's
      result carries it forward.
    * Any failure between creating the tmp file and the replace
      succeeding (a write error, a permissions error on the replace
      itself) is caught, the tmp file is removed, and the exception is
      re-raised — a half-finished attempt must never leave a stray
      ``.tmp`` file behind, and must never leave *path* itself touched.
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if path.exists():
            shutil.copymode(path, tmp)
        tmp.replace(path)
    except Exception:  # noqa: BLE001 — cleanup-then-reraise, never swallowed
        tmp.unlink(missing_ok=True)
        raise


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
@click.option(
    "--refresh", "refresh_only", is_flag=True,
    help="Only refresh EXISTING footnote bodies against current catalog "
         "state; add no new markers for links not already converted.",
)
@click.option(
    "--style", type=click.Choice(["long", "short"]), default="long", show_default=True,
    help="Footnote body verbosity: 'long' (title, content type, indexed "
         "date, link, outbound links) or 'short' (title + tumbler id only).",
)
def footnotes_cmd(
    paths: tuple[Path, ...], check: bool, dry_run: bool, to_links: bool,
    refresh_only: bool, style: str,
) -> None:
    """Convert nx://catalog/<tumbler> markdown links to stable GFM
    footnotes, in place — or reverse it with --to-links.

    Re-running on an already-converted file is a no-op when catalog
    state is unchanged; when it has drifted (a title edit, a merge),
    only the footnote BODIES are rewritten — markers already assigned
    in the body never move. A tumbler that no longer resolves means
    NOTHING is written for that file (GH #896's own "never partially
    write" criterion) — every such failure is reported, never silently
    dropped.
    """
    if check and dry_run:
        raise click.UsageError("--check and --dry-run are mutually exclusive.")
    if check and to_links:
        raise click.UsageError(
            "--check only checks the forward (link -> footnote) conversion; "
            "combine --to-links with --dry-run to preview the reverse instead."
        )
    if refresh_only and to_links:
        raise click.UsageError("--refresh applies to the forward conversion only.")

    reader_holder: list[Any] = []
    had_dangling = False
    needs_change = False
    had_hard_error = False

    for path in paths:
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            click.echo(
                f"{path}: not valid UTF-8 ({exc}) — refusing to read or "
                f"write; fix the file's encoding and re-run.", err=True,
            )
            had_hard_error = True
            continue

        try:
            if to_links:
                result = convert_footnotes_to_links(original)
            else:
                result = convert_links_to_footnotes(
                    original, partial(_lazy_reader, reader_holder),
                    base_dir=path.parent, refresh_only=refresh_only, style=style,
                )
        except CatalogLinkResolutionError as exc:
            click.echo(f"{path}: catalog service unreachable — {exc}", err=True)
            had_hard_error = True
            continue
        except FootnoteSectionConflict as exc:
            click.echo(f"{path}: {exc}", err=True)
            had_hard_error = True
            continue

        _report_dangling(path, result, reversed_=to_links)
        if result.dangling:
            had_dangling = True
        if result.changed:
            needs_change = True

        if check:
            continue

        if dry_run:
            # GH #896: --dry-run always shows the FULL picture, dangling
            # references included -- it never withholds the diff just
            # because the real write would be refused.
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                result.text.splitlines(keepends=True),
                fromfile=str(path), tofile=str(path),
            )
            click.echo("".join(diff), nl=False)
            continue

        if result.dangling:
            # GH #896's own acceptance criterion: an unresolvable tumbler
            # must not partially write the file. Nothing is written for
            # THIS path; the dangling report above already named every
            # failure.
            click.echo(f"{path}: not converted — one or more references unresolved.", err=True)
            continue

        if result.changed:
            _atomic_write_text(path, result.text)
            click.echo(f"{path}: converted")
        else:
            click.echo(f"{path}: already up to date")

    if had_hard_error:
        raise click.exceptions.Exit(2)

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
