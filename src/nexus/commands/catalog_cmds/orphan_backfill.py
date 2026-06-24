# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``nx catalog orphan-backfill`` subgroup (nexus-whh61.4).

Carved out of ``commands.catalog``: the ``orphan-backfill`` group and its
subcommands (``dt-link`` / ``synthetic`` / ``dump-csv`` / ``apply-csv`` /
``link-existing``) that register catalog Documents for T3 chunks lacking a
catalog entry, plus the group-local ``_get_owner_for`` helper. Behaviour-
preserving; ``register`` attaches the whole subgroup to the shared ``catalog``
group, so ``nx catalog orphan-backfill <sub>`` resolves exactly as before.

The unrelated ``consolidate`` / ``backfill`` verbs and the ``_backfill_*`` /
``_make_t3`` / ``_make_registry`` helpers stay in ``commands.catalog`` — they
are shared with ``setup`` and are a distinct concern. ``_get_catalog`` is
reached through the ``nexus.commands.catalog`` module object inside each
subcommand body, keeping imports acyclic and preserving the test seam.
"""
from __future__ import annotations

from pathlib import Path

import click


@click.group("orphan-backfill")
def orphan_backfill_group() -> None:
    """Backfill catalog Documents for T3 chunks that have no catalog entry.

    \b
    Three modes:
      dt-link    Search DEVONthink, register Documents with
                 source_uri=x-devonthink-item://<UUID> for high-precision
                 fuzzy matches (score >= 0.75 by default).
      synthetic  Register Documents with nx-orphan-backfill:// URIs for
                 chunks DT-link can't claim.
      dump-csv   Dump matched / low-confidence / unmatched titles to CSV
                 for operator triage.
      apply-csv  Read an operator-curated CSV and register the verified
                 UUID assignments.

    \b
    Complementary to ``nx catalog`` ``backfill-collections`` (which
    syncs the collections projection) and to the existing
    ``manifest_backfill`` module (which writes manifest rows when
    Documents already exist).
    """


def _get_owner_for(collection: str) -> str:
    """Resolve owner-tumbler for ``collection`` from the default map.

    Raises ``click.ClickException`` if unknown so operators see the
    actionable error rather than a Python traceback.
    """
    from nexus.catalog.orphan_backfill import (  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
        DEFAULT_COLLECTION_OWNER,
    )
    owner_prefix = DEFAULT_COLLECTION_OWNER.get(collection)
    if not owner_prefix:
        raise click.ClickException(
            f"Unknown owner for collection {collection!r}. "
            f"Add it to DEFAULT_COLLECTION_OWNER in "
            f"src/nexus/catalog/orphan_backfill.py, or pass --owner "
            f"explicitly."
        )
    return owner_prefix


@orphan_backfill_group.command("dt-link")
@click.argument("collection")
@click.option(
    "--min-score", default=0.75, type=float,
    help="High-precision threshold (default 0.75).",
)
@click.option(
    "--owner", default="",
    help="Owner tumbler prefix (e.g. '1.9'). Default: looked up by collection.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). --no-dry-run writes catalog Documents.",
)
def dt_link_cmd(
    collection: str, min_score: float, owner: str, dry_run: bool,
) -> None:
    """High-precision DEVONthink linkage for orphan T3 chunks.

    Walks T3 chunks for COLLECTION, groups by title, queries DEVONthink
    via osascript, and registers a Document per high-confidence match.
    Requires DEVONthink to be running (macOS only).
    """
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    t3 = make_t3()

    click.echo(f"Gathering chunks from T3 {collection}...")
    groups = ob.gather_titled_chunks(t3, collection)
    total_chunks = sum(len(g.chunks) for g in groups)
    click.echo(
        f"  {len(groups)} distinct titles, {total_chunks} chunks total"
    )

    click.echo(f"Classifying via DEVONthink (min_score={min_score})...")
    matched, low, unmatched = ob.classify_groups(
        groups, min_score=min_score, low_conf_floor=ob.LOW_CONF_FLOOR,
    )
    click.echo(
        f"  matched: {len(matched)} ({sum(len(m.chunks) for m in matched)} chunks)"
    )
    click.echo(
        f"  low_confidence: {len(low)} "
        f"({sum(len(m.chunks) for m in low)} chunks) -- run dump-csv for triage"
    )
    click.echo(
        f"  unmatched: {len(unmatched)} "
        f"({sum(len(g.chunks) for g in unmatched)} chunks) -- "
        f"run synthetic mode or dump-csv"
    )

    if dry_run:
        click.echo("\n(dry-run) --no-dry-run to register Documents.")
        return

    docs, links = ob.register_dt_linked(
        cat, owner_tumbler, collection, matched,
    )
    click.echo(
        f"\nRegistered {docs} Documents, linked {links} chunks via DT URIs."
    )


@orphan_backfill_group.command("synthetic")
@click.argument("collection")
@click.option(
    "--owner", default="",
    help="Owner tumbler prefix. Default: looked up by collection.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default).",
)
def synthetic_cmd(
    collection: str, owner: str, dry_run: bool,
) -> None:
    """Register synthetic Documents for orphan chunks DT-link can't claim.

    Synthesizes ``nx-orphan-backfill://`` URIs so the catalog manifest
    is populated without claiming false provenance. For chunks lacking
    title metadata, falls back to per-chash singleton Documents.
    """
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    t3 = make_t3()

    click.echo(f"Gathering chunks from T3 {collection}...")
    groups = ob.gather_titled_chunks(t3, collection)
    total_chunks = sum(len(g.chunks) for g in groups)
    titled = [g for g in groups if g.title]
    untitled = [g for g in groups if not g.title]
    untitled_chunks = sum(len(g.chunks) for g in untitled)
    click.echo(
        f"  {len(titled)} titled groups + {len(untitled)} untitled "
        f"groups ({untitled_chunks} chunks via chash fallback)"
    )
    click.echo(f"  Total chunks: {total_chunks}")

    if dry_run:
        click.echo("\n(dry-run) --no-dry-run to register Documents.")
        return

    docs, links = ob.register_synthetic(
        cat, owner_tumbler, collection, groups,
    )
    click.echo(
        f"\nRegistered {docs} synthetic Documents, linked {links} chunks."
    )


@orphan_backfill_group.command("dump-csv")
@click.argument("collection")
@click.option(
    "--out-dir", default="",
    help="Output directory (default: $NEXUS_CONFIG_DIR/backfill-queue).",
)
@click.option(
    "--min-score", default=0.75, type=float,
    help="High-precision threshold (default 0.75).",
)
def dump_csv_cmd(
    collection: str, out_dir: str, min_score: float,
) -> None:
    """Dump matched / low-confidence / unmatched titles to CSV files.

    Operators review ``low_confidence.csv`` and ``unmatched.csv``,
    fill in the right DT UUID where applicable, then feed back via
    ``apply-csv``.
    """
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.config import nexus_config_dir  # noqa: PLC0415  — command-local import (nexus.config)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    out_path = (
        Path(out_dir) if out_dir
        else Path(nexus_config_dir()) / "backfill-queue"
    )
    out_path.mkdir(parents=True, exist_ok=True)

    t3 = make_t3()
    click.echo(f"Gathering chunks from T3 {collection}...")
    groups = ob.gather_titled_chunks(t3, collection)
    click.echo(f"  {len(groups)} distinct titles")

    click.echo(f"Classifying via DEVONthink (min_score={min_score})...")
    matched, low, unmatched = ob.classify_groups(
        groups, min_score=min_score,
    )
    m_path, l_path, u_path = ob.dump_csvs(
        out_path, collection, matched, low, unmatched,
    )
    click.echo(
        f"\nWrote:\n"
        f"  {m_path}  ({len(matched)} matched)\n"
        f"  {l_path}  ({len(low)} low-confidence; edit operator_decision)\n"
        f"  {u_path}  ({len(unmatched)} unmatched; edit operator_dt_uuid)"
    )


@orphan_backfill_group.command("apply-csv")
@click.argument("collection")
@click.argument("csv_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--owner", default="",
    help="Owner tumbler prefix. Default: looked up by collection.",
)
def apply_csv_cmd(
    collection: str, csv_path: str, owner: str,
) -> None:
    """Apply an operator-curated CSV (from ``dump-csv``).

    Reads ``operator_dt_uuid`` (unmatched.csv) or ``operator_decision``
    (low_confidence.csv) per row; registers Documents with the verified
    UUIDs.
    """
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    t3 = make_t3()

    click.echo(f"Re-gathering T3 chunks for {collection} (chunk_lookup)...")
    groups = ob.gather_titled_chunks(t3, collection)
    chunk_lookup = {g.title: g.chunks for g in groups if g.title}

    click.echo(f"Applying {csv_path}...")
    docs, links = ob.apply_csv(
        cat, owner_tumbler, collection,
        Path(csv_path),
        chunk_lookup=chunk_lookup,
    )
    click.echo(
        f"\nRegistered {docs} Documents, linked {links} chunks "
        f"from operator-curated CSV."
    )


@orphan_backfill_group.command("link-existing")
@click.argument("collection")
@click.option(
    "--by", "match_by",
    type=click.Choice(["title", "content_hash"]),
    default="title",
    help="Match T3 chunks to existing catalog Documents by this field.",
)
@click.option(
    "--also-synthetic/--no-also-synthetic", default=False,
    help="After linking, register synthetic Documents for unlinked chunks.",
)
@click.option(
    "--owner", default="",
    help="Owner for synthetic fallback. Required if --also-synthetic.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default).",
)
def link_existing_cmd(
    collection: str, match_by: str, also_synthetic: bool,
    owner: str, dry_run: bool,
) -> None:
    """Link T3 chunks to EXISTING catalog Documents in a collection.

    \b
    Two strategies:
      --by title          Match T3 chunk's ``title`` metadata to
                          catalog ``documents.title`` in the collection.
                          Use when chunks carry MCP-style title metadata
                          (e.g. knowledge__knowledge).
      --by content_hash   Match T3 chunk's ``content_hash`` metadata to
                          catalog ``documents.head_hash`` in the
                          collection. Use when chunks are PDF-shaped
                          with no title (e.g. docs__default).

    Writes ``document_chunks`` manifest rows but does NOT create new
    Documents. With ``--also-synthetic``, unlinked chunks fall through
    to synthetic-mode registration.
    """
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible
    cat = _cat_cmd._get_catalog()
    t3 = make_t3()

    if dry_run:
        # nexus-xnz0o: use list_by_collection() to count matching docs (uniform API).
        # Avoids direct _db access for this diagnostic-only count.
        coll_docs = cat.list_by_collection(collection)
        if match_by == "title":
            count = sum(1 for e in coll_docs if e.title)
        else:
            count = sum(1 for e in coll_docs if e.head_hash)
        click.echo(
            f"Existing catalog Documents with {match_by}: {count}"
        )
        col = t3._client.get_collection(name=collection)
        click.echo(f"T3 chunks in {collection}: {col.count()}")
        click.echo("\n(dry-run) --no-dry-run to write manifest rows.")
        return

    if match_by == "title":
        click.echo(f"Gathering T3 chunks for {collection}...")
        groups = ob.gather_titled_chunks(t3, collection)
        click.echo(f"  {len(groups)} title groups")
        linked_chunks, linked_docs, unlinked = ob.link_by_title(
            cat, collection, groups,
        )
        click.echo(
            f"Linked {linked_chunks} chunks across {linked_docs} "
            f"existing Documents."
        )
        unlinked_total = sum(len(g.chunks) for g in unlinked)
        click.echo(f"Unlinked: {len(unlinked)} groups, {unlinked_total} chunks")
        if also_synthetic and unlinked:
            if not owner:
                owner_str = _get_owner_for(collection)
            else:
                owner_str = owner
            owner_t = Tumbler.parse(owner_str)
            sdocs, slinks = ob.register_synthetic(
                cat, owner_t, collection, unlinked,
            )
            click.echo(
                f"Synthetic fallback: registered {sdocs} Documents, "
                f"linked {slinks} chunks."
            )
    else:  # content_hash
        click.echo(
            f"Linking by content_hash → head_hash for {collection}..."
        )
        linked_chunks, linked_docs, unmatched = ob.link_by_content_hash(
            cat, t3, collection,
        )
        click.echo(
            f"Linked {linked_chunks} chunks across {linked_docs} "
            f"existing Documents."
        )
        click.echo(f"Unmatched chunks (no head_hash match): {unmatched}")


def register(group: click.Group) -> None:
    """Attach the orphan-backfill subgroup to the shared ``catalog`` group."""
    group.add_command(orphan_backfill_group)
