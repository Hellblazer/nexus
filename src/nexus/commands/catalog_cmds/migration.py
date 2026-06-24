# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Fallback-collection migration command for the ``nx catalog`` group (nexus-whh61.4).

Carved out of ``commands.catalog``: ``migrate-fallback`` — re-points documents
from a legacy fallback collection (e.g. ``docs__default``) onto per-owner
conformant targets and registers/supersedes the projection rows. Behaviour-
preserving; ``register`` attaches it to the shared ``catalog`` group so
``nx catalog migrate-fallback`` resolves exactly as before.

The shared helpers (``_get_catalog`` / ``_get_catalog_writer``) are reached
through the ``nexus.commands.catalog`` module object inside the body — keeping
this module's imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam. (The original
in-place command used the direct ``_get_catalog()`` form; the carve converts
it to the module-routed form the other carved families use.)
"""
from __future__ import annotations

import click


@click.command("migrate-fallback")
@click.argument("source")
@click.option(
    "--target-model",
    default="",
    help="Override the target embedding model. Default: derived from "
    "the source's content-type prefix (knowledge__/docs__/rdr__ → "
    "voyage-context-3; code__ → voyage-code-3).",
)
@click.option(
    "--target-version",
    default="v1",
    show_default=True,
    help="Target model_version segment for new collections.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Report the migration proposal without writing.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually migrate. Without --yes, the command "
    "falls back to report-only.",
)
def migrate_fallback_cmd(
    source: str,
    target_model: str,
    target_version: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Migrate documents from a fallback collection to per-owner targets.

    \b
    Walks every document in SOURCE (a fallback like ``docs__default``
    or ``knowledge__knowledge``) and proposes a target collection per
    document, computed as
    ``<content_type>__<owner>__<embedding_model>__<version>`` where
    ``content_type`` and ``embedding_model`` come from the source's
    prefix and ``owner`` comes from each document's tumbler.

    \b
    With --yes, re-points each document's physical_collection in the
    catalog and auto-registers the target rows in the collections
    projection. T3 chunks are NOT moved; the catalog-side migration
    is enough to deprecate the fallback over time. Operators
    repopulate the target by re-running ``nx index`` against the
    source files; old chunks become orphans whose ``nx t3 gc`` will
    sweep on the next cycle (catalog now points elsewhere, so the
    chunk's doc_id is no longer alive in the source collection).

    \b
    Source must NOT already be conformant; conformant collections are
    not fallbacks. Source must be registered in the projection (run
    ``nx catalog backfill-collections`` first if needed).

    \b
    When the migration empties the source AND every doc landed in the
    same target, the source row is marked superseded_by that target.
    Multiple targets leave the source NOT superseded; the operator
    deprecates manually.

    \b
    Examples:
      nx catalog migrate-fallback knowledge__knowledge --dry-run
      nx catalog migrate-fallback docs__default --yes
    """
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        is_conformant_collection_name, voyage_model_for_collection,
    )

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()

    src_row = cat.get_collection(source)
    if src_row is None:
        raise click.ClickException(
            f"source {source!r} is not registered in the collections "
            f"projection. Run 'nx catalog backfill-collections' first."
        )
    if is_conformant_collection_name(source):
        raise click.ClickException(
            f"source {source!r} is already conformant; this is not a "
            f"fallback collection."
        )

    if "__" not in source:
        raise click.ClickException(
            f"source {source!r} has no content-type prefix; cannot "
            f"derive a migration target."
        )
    content_type = source.split("__", 1)[0]

    if not target_model:
        target_model = voyage_model_for_collection(source)

    entries = cat.list_by_collection(source)
    rows = [(str(e.tumbler),) for e in sorted(entries, key=lambda e: str(e.tumbler))]

    if not rows:
        click.echo(f"{source}: 0 doc(s) to migrate.")
        return

    from nexus.catalog.collection_name import owner_segment_for_tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.collection_name)

    proposals: list[tuple[str, str]] = []
    for (tumbler,) in rows:
        owner = owner_segment_for_tumbler(tumbler)
        if not owner:
            click.echo(
                f"  WARN: could not derive owner from tumbler {tumbler!r}; "
                f"skipping",
                err=True,
            )
            continue
        target = f"{content_type}__{owner}__{target_model}__{target_version}"
        proposals.append((tumbler, target))

    # nexus-qpet.3: aggregate by target so the operator can scan the
    # mapping at a glance. Per-doc lines below are kept for tests +
    # operators who want the full proposal.
    target_counts: dict[str, int] = {}
    for _, target in proposals:
        target_counts[target] = target_counts.get(target, 0) + 1

    click.echo(
        f"{source}: {len(proposals)} doc(s) -> "
        f"{len(target_counts)} target collection(s)"
    )
    for target in sorted(target_counts):
        click.echo(f"  {target}: {target_counts[target]} doc(s)")
    click.echo("")
    for tumbler, target in proposals:
        click.echo(f"  {tumbler}  ->  {target}")

    if dry_run:
        return
    if not yes:
        click.echo(
            "\n--no-dry-run alone is treated as report-only. "
            "Add --yes to actually migrate."
        )
        return

    # Register every unique target ONCE (each register_collection
    # acquires its own flock; the targets count is small relative to
    # the document count so no batched register is needed yet).
    targets_seen: set[str] = set()
    for _, target in proposals:
        if target in targets_seen:
            continue
        from nexus.corpus import parse_conformant_collection_name  # noqa: PLC0415  — command-local import (nexus.corpus)
        segments = parse_conformant_collection_name(target)
        writer.register_collection(
            target,
            content_type=segments["content_type"],
            owner_id=segments["owner_id"],
            embedding_model=segments["embedding_model"],
            model_version=segments["model_version"],
        )
        targets_seen.add(target)

    # nexus-qpet.3: single flock + single commit for the per-doc
    # re-point loop. Pre-fix shape was N flocks + N commits per
    # update_document_collection call; a 1000-doc fallback paid the
    # SQLite commit overhead 1000 times. Batch keeps the operation
    # deterministic and order-preserving (proposals is already
    # sorted by tumbler).
    writer.update_documents_collection_batch(proposals)

    if len(targets_seen) == 1:
        only_target = next(iter(targets_seen))
        writer.supersede_collection(
            source, only_target, reason="migrate-fallback",
        )
        click.echo(
            f"\nMigrated {len(proposals)} doc(s); source {source!r} "
            f"superseded by {only_target!r}."
        )
    else:
        click.echo(
            f"\nMigrated {len(proposals)} doc(s) across "
            f"{len(targets_seen)} target collection(s). Source "
            f"{source!r} retained (multiple targets); operator "
            f"deprecates manually if appropriate."
        )

    # Split-brain warning: catalog now points at the new collections,
    # but T3 chunks are still in the source. Searches against the new
    # collections return empty for migrated docs until they are
    # re-indexed. This is the trade-off the verb makes per the bead
    # spec ("T3 chunks are NOT moved"); making it explicit at the
    # output prevents an operator from silently missing it.
    click.echo(
        f"\nWARNING: {len(proposals)} document(s) are now SPLIT-BRAIN: "
        f"catalog points at the new target collection(s) but T3 chunks "
        f"remain in {source!r}. Searches against the new collection(s) "
        f"will return empty for these docs until you run:\n"
        f"  nx index repo .   # re-populate the target with new chunks\n"
        f"After re-index completes, the old chunks become orphans and "
        f"'nx t3 gc -c {source} --no-dry-run --yes' will sweep them.",
        err=True,
    )


def register(group: click.Group) -> None:
    """Attach the fallback-migration command to the shared ``catalog`` group."""
    group.add_command(migrate_fallback_cmd)
