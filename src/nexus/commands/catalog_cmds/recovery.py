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

import click


@click.command("export")
@click.argument("output", type=click.Path(dir_okay=False, writable=True, path_type=Path))
def export_cmd(output: Path) -> None:
    """Export the recovery bundle: catalog links + store_put-origin knowledge.

    Writes a human-inspectable JSONL bundle keyed on source_uri identity
    (never tumblers — they are not stable across reindex). Carries NO
    embeddings: import re-embeds through the real store_put chain, so the
    bundle is portable across embedding modes. Run this BEFORE a
    reinstall; pair with 'nx export COLLECTION' (.nxexp) when you also
    want an embedding-preserving per-collection backup.
    """
    from nexus.catalog.recovery_bundle import export_bundle  # noqa: PLC0415 — command-local import
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import

    reader = _cat_cmd._get_catalog()
    summary = export_bundle(reader, make_t3(), output)
    click.echo(f"Exported recovery bundle: {output}")
    click.echo(
        f"  knowledge docs: {summary.docs_exported}   links: {summary.links_exported}"
    )
    if summary.ghosts_skipped:
        click.echo(
            f"  ghosts skipped (rows with no live content): {summary.ghosts_skipped}"
        )
    if summary.unresolvable_link_endpoints:
        click.echo(
            f"  WARNING: {summary.unresolvable_link_endpoints} link endpoint(s) "
            "could not be resolved to a source_uri and were omitted"
        )


@click.command("import")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def import_cmd(bundle: Path) -> None:
    """Import a recovery bundle written by 'nx catalog export'.

    Knowledge docs re-run the real store_put chain (re-embedding,
    reconciling onto existing rows per the sdp0u identity contract);
    links resolve endpoints by source_uri. Idempotent: a second import of
    the same bundle merges rather than duplicates. Partial failures are
    REPORTED, never silently dropped — and never abort the rest.
    """
    from nexus.catalog.recovery_bundle import import_bundle  # noqa: PLC0415 — command-local import
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    from nexus.db import make_t3  # noqa: PLC0415 — command-local import

    reader = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    summary = import_bundle(reader, writer, make_t3(), bundle)
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
    if summary.unresolvable_links:
        click.echo(f"  UNRESOLVABLE LINKS: {len(summary.unresolvable_links)}")
        for link in summary.unresolvable_links:
            click.echo(
                f"    {link['from_source_uri']} -[{link['link_type']}]-> "
                f"{link['to_source_uri']} (missing: {link['missing']})"
            )


def register(group: click.Group) -> None:
    """Attach the recovery command pair to the shared ``catalog`` group."""
    group.add_command(export_cmd)
    group.add_command(import_cmd)
