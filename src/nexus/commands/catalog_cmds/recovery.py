# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Recovery-bundle command family for the ``nx catalog`` group
(nexus-xn3fr, GH #1419 Issue 9).

``nx catalog export FILE`` / ``nx catalog import FILE`` — one paired
recovery verb over :mod:`nexus.catalog.recovery_bundle` carrying the two
things a reinstall cannot regenerate: the catalog link graph and
store_put-origin knowledge content. Thin wrappers only; the format, the
identity contract, and the fail-loud semantics live in the core module
(design of record: T2 ``nexus/design-xn3fr-recovery-bundle.md``).

Shared helpers (``_get_catalog`` / ``_get_catalog_writer``) stay in
``commands.catalog`` and are reached through the module object inside
each command body — the ``links.py`` precedent, keeping imports acyclic
and the ``patch("nexus.commands.catalog._get_catalog", ...)`` test seam.
"""
from __future__ import annotations

from pathlib import Path

import json
import urllib.error

import click


#: How many unrecovered note titles ``nx catalog export`` prints before summarising the rest.
_UNRECOVERED_TITLES_SHOWN: int = 20


@click.command("export")
@click.argument("output", type=click.Path(dir_okay=False, writable=True, path_type=Path))
def export_cmd(output: Path) -> None:
    """Export the recovery bundle: catalog links + store_put-origin knowledge.

    Writes a human-inspectable JSONL bundle keyed on source_uri identity
    (never tumblers — they are not stable across reindex). Carries NO
    embeddings: import re-embeds through the note writer store_put uses, so the
    bundle is portable across embedding modes. Run this BEFORE a
    reinstall; pair with 'nx store export COLLECTION' (.nxexp) when you also
    want an embedding-preserving per-collection backup.
    """
    from nexus.catalog.recovery_bundle import export_bundle  # noqa: PLC0415 — command-local import
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import
    from nexus.db.http_vector_client import VectorServiceError  # noqa: PLC0415 — command-local import

    reader = _cat_cmd._get_catalog()
    try:
        summary = export_bundle(reader, make_t3(), output)
    except (VectorServiceError, urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        # nexus-6pfgb.36: a note piece the store could not return is not a piece that is gone; stop
        # rather than write a bundle that silently lacks it. The bundle file is written last. The
        # transport errors are what a local endpoint raises unwrapped; a failure writing the output
        # file is none of these and keeps its own traceback.
        click.echo(
            f"Export failed: the vector store could not be read ({exc}). No bundle was written; "
            "run the export again.",
            err=True,
        )
        raise click.exceptions.Exit(1) from exc
    click.echo(f"Exported recovery bundle: {output}")
    click.echo(
        f"  knowledge docs: {summary.docs_exported}   links: {summary.links_exported}"
    )
    if summary.ghosts_skipped:
        click.echo(
            f"  ghosts skipped (rows with no live content): {summary.ghosts_skipped}"
        )
    if summary.notes_incomplete:
        click.echo(
            f"  WARNING: {summary.notes_incomplete} note(s) written as several pieces have a piece "
            "missing from the store or a gap in their manifest and were left out of the bundle "
            "rather than exported truncated:"
        )
        incomplete = summary.notes_incomplete_notes
        for collection, title in incomplete[:_UNRECOVERED_TITLES_SHOWN]:
            click.echo(f"    {collection}: {json.dumps(title) if title else '(untitled)'}")
        if len(incomplete) > _UNRECOVERED_TITLES_SHOWN:
            click.echo(f"    ... and {len(incomplete) - _UNRECOVERED_TITLES_SHOWN} more")
    if summary.note_keys_unrecovered:
        click.echo(
            f"  note keys not recovered: {summary.note_keys_unrecovered} note(s) whose own tags, "
            "category and ttl_days are not in the catalog (stored by nx 7.87.0 or earlier, or "
            "brought in by an .nxexp import) and whose chunk row cannot be shown to be their own "
            "write; they export with some or all of those empty (ttl_days permanent; tags and "
            "category are still taken when the note is the chunk's only owner and no note stamped "
            "the row). To keep a note's values, re-put it with its tags, category and TTL using a "
            "current client, then export again; take the values from your own records, since the "
            "chunk row may hold another note's:"
        )
        notes = summary.note_keys_unrecovered_notes
        for collection, title in notes[:_UNRECOVERED_TITLES_SHOWN]:
            # json.dumps quotes the title and escapes control characters: an .nxexp import's title
            # is untrusted text and must not reach the terminal raw.
            click.echo(f"    {collection}: {json.dumps(title) if title else '(untitled)'}")
        if len(notes) > _UNRECOVERED_TITLES_SHOWN:
            click.echo(f"    ... and {len(notes) - _UNRECOVERED_TITLES_SHOWN} more")
    if summary.unresolvable_link_endpoints:
        click.echo(
            f"  WARNING: {summary.unresolvable_link_endpoints} link endpoint(s) "
            "could not be resolved to a source_uri and were omitted"
        )
    if summary.notes_incomplete:
        # nexus-6pfgb.37 (Sam): the bundle is written for inspection, but it does not hold every note,
        # so a scripted export-then-reinstall must stop here. Ghosts have nothing left to save and
        # do not change the exit status.
        click.echo(
            f"  {summary.notes_incomplete} note(s) are not in the bundle (listed above); "
            "do not reinstall from it until they are dealt with",
            err=True,
        )
        raise click.exceptions.Exit(1)


@click.command("import")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def import_cmd(bundle: Path) -> None:
    """Import a recovery bundle written by 'nx catalog export'.

    Knowledge docs are written through the note writer MCP store_put uses
    (re-embedding, reconciling onto existing rows per the sdp0u identity
    contract; chunks carry source_agent "recovery-import"); links resolve
    endpoints by source_uri. Idempotent: a second import of
    the same bundle merges rather than duplicates. Each note is written in
    one request with its catalog owner rows. Partial failures are REPORTED
    per note, never silently dropped — and never abort the rest. Exits
    non-zero when any note did not verify (failed, uncertain, or written
    but not stamped complete).
    """
    from nexus.catalog.recovery_bundle import import_bundle  # noqa: PLC0415 — command-local import
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import

    reader = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    from nexus.errors import NoteUriEngineTooOldError  # noqa: PLC0415 — command-local import

    try:
        summary = import_bundle(reader, writer, make_t3(), bundle)
    except NoteUriEngineTooOldError as exc:
        # The first note hit it and so would every other: one clear error, not N per-note failures.
        raise click.ClickException(f"recovery import aborted at the first note: {exc}") from exc
    click.echo(f"Imported recovery bundle: {bundle}")
    click.echo(
        f"  docs imported: {summary.docs_imported}   "
        f"links created: {summary.links_created}   merged: {summary.links_merged}"
    )
    if summary.links_missing_span:
        click.echo(
            f"  spans stripped (chunk no longer exists on this install): "
            f"{summary.links_missing_span}"
        )
    if summary.docs_failed:
        click.echo(f"  DOC FAILURES: {summary.docs_failed}")
        for f in summary.doc_failures:
            click.echo(f"    {f['title']!r}: {f['error']}")
    if summary.docs_uncertain:
        click.echo(f"  DOCS NOT CONFIRMED (may have landed): {summary.docs_uncertain}")
    if summary.docs_stamp_refused:
        click.echo(
            f"  DOCS WRITTEN BUT NOT STAMPED COMPLETE (left 'indexing'): {summary.docs_stamp_refused}"
        )
    for u in summary.doc_unverified:
        click.echo(f"    {u['title']!r}: {u['error']}")
    if summary.unresolvable_links:
        click.echo(f"  UNRESOLVABLE LINKS: {len(summary.unresolvable_links)}")
        for link in summary.unresolvable_links:
            click.echo(
                f"    {link['from_source_uri']} -[{link['link_type']}]-> "
                f"{link['to_source_uri']} (missing: {link['missing']})"
            )
    if summary.docs_unverified:
        click.echo(
            f"  {summary.docs_unverified} note(s) did not verify; "
            f"{summary.docs_imported} verified",
            err=True,
        )
        raise click.exceptions.Exit(1)


def register(group: click.Group) -> None:
    """Attach the recovery command pair to the shared ``catalog`` group."""
    group.add_command(export_cmd)
    group.add_command(import_cmd)
