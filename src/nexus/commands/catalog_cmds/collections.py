# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Collection-management commands for the ``nx catalog`` group (nexus-whh61.4).

Carved verbatim out of ``commands.catalog``: ``backfill-collections`` /
``collection-name`` / ``rename-collection`` / ``collection-gc`` — the verbs
that operate on the collections projection and T3 collection lifecycle.
Behaviour-preserving; ``register`` attaches all four to the shared ``catalog``
group so ``nx catalog backfill-collections`` (etc.) resolve exactly as before.

Shared helpers (``_get_catalog`` / ``_get_catalog_writer``) stay in
``commands.catalog`` and are reached through the module object inside each
body — keeping this module's imports acyclic and preserving the
``patch("nexus.commands.catalog._get_catalog", …)`` test seam.
"""
from __future__ import annotations

from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


@click.command("backfill-collections")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    help="Report-only (default). Use --no-dry-run to actually register. "
    "Matches the safe-default convention of the other Phase 6 verbs "
    "(rename-collection, migrate-fallback, t3 gc).",
)
def backfill_collections_cmd(dry_run: bool) -> None:
    """Populate the Phase 6 collections projection from existing state.

    \b
    Walks both T3 (live ChromaDB collections) and the catalog
    documents.physical_collection column, unions the two sets, and
    registers each name not already in the projection. The projector's
    is_conformant_collection_name regex decides each row's
    legacy_grandfathered flag automatically.

    \b
    Idempotent: re-running adds only the names that appeared since the
    last run. The conventional first-time invocation is dry-run, then
    --no-dry-run after operator review.

    \b
    Examples:
      nx catalog backfill-collections --dry-run
      nx catalog backfill-collections
    """
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()

    try:
        t3_db = make_t3()
        # nexus-o6aa.14: skip bypass-schema collections (``taxonomy__*``)
        # to keep the projection in sync with the drift check at
        # ``_run_collections_drift`` which excludes the same prefixes.
        # Pre-fix the projection registered taxonomy rows whose T3
        # collection is invisible to drift, producing a permanent
        # "projection row whose T3 collection is gone" report that no
        # backfill could clear.
        t3_names = {
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:
        # Refusing to do a "partial" backfill that registers only the
        # catalog-side names: an operator running this during a T3
        # outage would get a green exit code and half the projection
        # missing, then think the verb is idempotent (it is, but the
        # second run after T3 recovers would silently fix it). Better
        # to fail loud here so the operator knows to retry.
        raise click.ClickException(
            f"Failed to list T3 collections: {exc}. Aborting to avoid a "
            f"partial backfill. Re-run after T3 is reachable."
        ) from exc

    catalog_names = set(cat.distinct_doc_collections())

    candidate_names = sorted(t3_names | catalog_names)
    already = {r["name"] for r in cat.list_collections()}
    to_register = [n for n in candidate_names if n not in already]

    if not to_register:
        click.echo(
            f"Nothing to backfill: {len(already)} collection(s) already registered, "
            f"0 new."
        )
        return

    verb = "would register" if dry_run else "registering"
    click.echo(f"{verb} {len(to_register)} new collection(s):")
    for name in to_register:
        click.echo(f"  {name}")

    if dry_run:
        return

    # RDR-137 followup SIG-7 (nexus-43qgm.7): pass content_type +
    # owner_id when the collection name is conformant so the OQ-5
    # reader inference cannot silently shadow other owners' docs
    # selections with anonymous knowledge__ rows. Legacy 2-segment
    # names (e.g. ``knowledge__delos``) fall through to the bare
    # register_collection call — their owner_id remains empty and
    # the reader's owner_id JOIN excludes them from owner-scoped
    # lookups, which is the desired behaviour.
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    for name in to_register:
        if is_conformant_collection_name(name):
            try:
                parsed = parse_conformant_collection_name(name)
                writer.register_collection(
                    name,
                    content_type=parsed["content_type"],
                    owner_id=parsed["owner_id"],
                    embedding_model=parsed["embedding_model"],
                    model_version=parsed["model_version"],
                )
                continue
            except (KeyError, ValueError) as exc:
                _log.warning(
                    "backfill_collections_conformant_parse_failed",
                    name=name, error=str(exc),
                )
        # Non-conformant fallback (legacy 2-segment names).
        writer.register_collection(name)

    click.echo(
        f"\nDone: {len(to_register)} new, "
        f"{len(already)} already registered."
    )


@click.command("collection-name")
@click.option(
    "--content-type",
    required=True,
    type=click.Choice(["code", "docs", "rdr", "knowledge"]),
    help="Content type to resolve (code | docs | rdr | knowledge).",
)
@click.option(
    "--repo",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=None,
    help="Repository root (default: current working directory).",
)
def collection_name_cmd(content_type: str, repo: Path | None) -> None:
    """Resolve and emit the conformant T3 collection name for ``--content-type`` in ``--repo``.

    \b
    Plugin-layer call sites (rdr-close SKILL.md post-mortem archival,
    rdr_hook status reporting) use this to look up the canonical
    ``CollectionName`` without constructing the legacy 2-segment shape
    themselves. The catalog must be initialized AND the repo must have
    a registered owner (typically populated by the indexer's
    ``_catalog_hook`` on first index).

    Output is a single line: the rendered ``CollectionName``. Operators
    can capture it via shell substitution:

        nx store put --collection "$(nx catalog collection-name --content-type knowledge)" ...
    """
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    target_repo = repo if repo is not None else Path.cwd()
    try:
        name = cat.collection_for_repo(target_repo, content_type)
    except LookupError as exc:
        raise click.ClickException(
            f"{exc}\n\n"
            f"Run 'nx index repo {target_repo}' first; the indexer's "
            f"_catalog_hook registers the owner row that this command needs."
        ) from exc
    click.echo(name.render())


@click.command("rename-collection")
@click.argument("old")
@click.argument("new")
@click.option(
    "--dry-run/--no-dry-run",
    default=False,
    help="Report the rename plan without writing.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Required to actually rename. Without --yes (and without "
    "--dry-run), the command falls back to report-only.",
)
@click.option(
    "--allow-legacy",
    is_flag=True,
    default=False,
    help="Skip the is_conformant_collection_name gate on the new name. "
    "The renamed collection still gets a row in the projection but is "
    "flagged legacy_grandfathered=True. Use only when migrating between "
    "two grandfathered names (rare).",
)
def rename_collection_cmd(
    old: str, new: str, dry_run: bool, yes: bool, allow_legacy: bool,
) -> None:
    """Rename a collection with full RDR-101 Phase 6 lifecycle.

    \b
    Combines the data-plane rename (T3 native modify + T2 cascade +
    catalog documents re-point) with the Phase 6 control-plane work
    (collections-projection update + CollectionSuperseded event
    emission). Operators wanting only the data plane can use
    ``nx collection rename`` instead; this verb is the canonical
    Phase 6 path that keeps the event log and projection in sync.

    \b
    Validation gates fire BEFORE any side effect:
      - new name must be conformant (or pass --allow-legacy)
      - old name must be in the collections projection
      - old name must not already be superseded
      - new name must not already exist in T3

    \b
    Examples:
      nx catalog rename-collection knowledge__delos \\
          knowledge__1-1__voyage-context-3__v1 --yes
      nx catalog rename-collection docs__nexus-571b8edd ... --dry-run
    """
    from nexus.commands.collection import (  # noqa: PLC0415  — command-local import (nexus.commands.collection)
        rename_collection_data_plane,
    )
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    writer = _cat_cmd._get_catalog_writer()
    t3_db = make_t3()

    if not is_conformant_collection_name(new) and not allow_legacy:
        raise click.ClickException(
            f"new name {new!r} is not conformant. Expected "
            f"<content_type>__<owner_id>__<embedding_model>__v<n>. "
            f"Pass --allow-legacy to rename to a grandfathered name "
            f"(rare; only when migrating between two legacy names)."
        )

    old_row = cat.get_collection(old)
    if old_row is None:
        raise click.ClickException(
            f"old name {old!r} is not registered in the collections "
            f"projection. Run 'nx catalog backfill-collections' if this "
            f"is an existing collection that has not been registered."
        )
    if old_row.get("superseded_by"):
        raise click.ClickException(
            f"old name {old!r} is already superseded by "
            f"{old_row['superseded_by']!r}. Refusing to rename a stale entry."
        )

    # Source-exists check fires BEFORE collision check so an operator
    # who typoes the old name gets "old not found" rather than "new
    # already exists" (the latter is misleading when old never existed).
    if not t3_db.collection_exists(old):
        raise click.ClickException(f"old name {old!r} does not exist in T3.")
    if old == new:
        raise click.ClickException(
            f"old and new names are identical ({old!r}); rename is a no-op."
        )
    if t3_db.collection_exists(new):
        raise click.ClickException(
            f"new name {new!r} already exists in T3. "
            f"Refusing to rename {old!r} on top of an existing collection."
        )

    if dry_run:
        click.echo(f"would rename: {old} -> {new}")
        return
    if not yes:
        click.echo(
            f"would rename: {old} -> {new}\n"
            f"--no-dry-run alone is treated as report-only. "
            f"Add --yes to actually rename."
        )
        return

    counts = rename_collection_data_plane(
        old, new, t3_db=t3_db, catalog=cat,
        on_warn=lambda msg: click.echo(msg, err=True),
    )

    # T3 has been renamed by this point. If projection writes raise,
    # we end up with T3 ahead of the projection. Catch and surface so
    # the operator knows what state the system is in - a half-applied
    # rename is recoverable but only if the operator has the recovery
    # plan in front of them.
    try:
        if is_conformant_collection_name(new):
            segments = parse_conformant_collection_name(new)
            writer.register_collection(
                new,
                content_type=segments["content_type"],
                owner_id=segments["owner_id"],
                embedding_model=segments["embedding_model"],
                model_version=segments["model_version"],
            )
        else:
            writer.register_collection(new)
        writer.supersede_collection(old, new, reason="rename-collection")
    except Exception as exc:
        click.echo(
            f"WARN: T3 was renamed {old!r} -> {new!r} but the projection "
            f"update failed: {exc}. Recover by running:\n"
            f"  nx catalog backfill-collections --no-dry-run  "
            f"# registers {new!r}\n"
            f"  python -c \"from nexus.catalog.catalog import Catalog; "
            f"from nexus.config import catalog_path; "
            f"p=catalog_path(); "
            f"Catalog(p, p / '.catalog.db').supersede_collection("
            f"{old!r}, {new!r})\"",
            err=True,
        )
        raise click.exceptions.Exit(2) from exc

    parts: list[str] = []
    if counts["tax_topics"] or counts["tax_assignments"] or counts["tax_meta"]:
        parts.append(
            f"{counts['tax_topics']} topics, "
            f"{counts['tax_assignments']} assignments, "
            f"{counts['tax_meta']} meta"
        )
    if counts["chash"]:
        parts.append(f"{counts['chash']} chash rows")
    if counts["catalog_docs"]:
        parts.append(f"{counts['catalog_docs']} catalog docs")
    suffix = f" ({'; '.join(parts)})" if parts else ""
    click.echo(f"Renamed: {old} -> {new}{suffix}")
    click.echo(f"Emitted CollectionSuperseded({old} -> {new})")


@click.command("collection-gc")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete zombie T3 collections. Without this flag the "
    "command is a dry-run report only.",
)
def collection_gc_cmd(apply: bool) -> None:
    """Sweep zombie T3 collections (0 chunks, no catalog projection,
    no document references).

    \b
    Targets the junkyard pattern flagged by
    ``nx catalog doctor --collections-drift``: T3 collections that
    accumulate from interrupted indexes, deleted worktrees, or any
    indexer path that pre-creates a collection name (via
    ``get_or_create_collection``) and then never writes a chunk.
    Each one shows up in the doctor's "T3 collections without
    projection rows" list and never gets cleaned up because no verb
    targets them.

    \b
    Conservative deletion criteria. A T3 collection must satisfy ALL
    of:

      * 0 chunks (``col.count() == 0``);
      * NOT registered in the catalog ``collections`` projection;
      * NOT referenced by any ``documents.physical_collection`` row;
      * NOT bypass-schema (``taxonomy__*`` is operator-managed and
        out of scope).

    \b
    Default is dry-run: reports per-candidate deletion plan without
    writing. Pass ``--apply`` to actually delete.

    \b
    Stale projection rows (catalog has the row, T3 collection is
    gone) are NOT handled here: the catalog event log is
    append-only, so removing a projection row requires
    ``supersede_collection(<old>, <target>)`` against an explicit
    operator-chosen target. Use the recipe printed by
    ``nx catalog doctor --collections-drift`` for those cases.

    \b
    Examples:
      nx catalog collection-gc          # dry-run report
      nx catalog collection-gc --apply  # actually delete

    \b
    Filed under nexus-ks40 (catalog/T3 hygiene).
    """
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    cat = _cat_cmd._get_catalog()
    try:
        t3_db = make_t3()
        t3_collections = t3_db.list_collections()
    except Exception as exc:  # noqa: BLE001 — re-raises after cleanup/translation
        click.echo(f"Failed to list T3 collections: {exc}", err=True)
        raise SystemExit(1)

    # nexus-pz24 (RDR-108 Phase 4 review CR-M2): mirror the T3 path's
    # error-handling shape so a SQLite failure (locked DB, schema
    # mismatch, FS issue) surfaces a clean operator message rather
    # than a raw Python traceback.
    try:
        projection_names = {r["name"] for r in cat.list_collections()}
        # nexus-xnz0o: use distinct_doc_collections() (uniform API).
        doc_collection_names = set(cat.distinct_doc_collections())
    except Exception as exc:  # noqa: BLE001 — re-raises after cleanup/translation
        click.echo(f"Failed to query catalog: {exc}", err=True)
        raise SystemExit(1)

    candidates: list[tuple[str, int]] = []
    skipped_referenced = 0
    skipped_nonempty = 0
    skipped_bypass = 0

    for c in t3_collections:
        name = c["name"]
        count = c.get("count", 0)
        if name.startswith(_BYPASS_SCHEMA_PREFIXES):
            skipped_bypass += 1
            continue
        if name in projection_names or name in doc_collection_names:
            skipped_referenced += 1
            continue
        if count > 0:
            skipped_nonempty += 1
            continue
        candidates.append((name, count))

    candidates.sort()

    click.echo(
        f"T3 collections: {len(t3_collections)} total"
    )
    click.echo(
        f"  catalog projection: {len(projection_names)} entries"
    )
    click.echo(
        f"  documents.physical_collection: {len(doc_collection_names)} entries"
    )
    click.echo(
        f"  bypass-schema (excluded): {skipped_bypass}"
    )
    click.echo(
        f"  referenced by catalog (kept): {skipped_referenced}"
    )
    click.echo(
        f"  unreferenced but non-empty (kept, needs operator review): "
        f"{skipped_nonempty}"
    )
    click.echo(
        f"  unreferenced AND empty (zombie candidates): {len(candidates)}"
    )

    if not candidates:
        click.echo("\nNothing to gc.")
        return

    verb = "would delete" if not apply else "deleted"
    click.echo(f"\nZombie collections ({verb}):")
    # Cap at 30 to keep output sane; the operator gets the count above.
    for name, count in candidates[:30]:
        click.echo(f"  {verb} {name} ({count} chunks)")
    if len(candidates) > 30:
        click.echo(f"  ... and {len(candidates) - 30} more")

    if apply:
        actually_deleted = 0
        failures: list[tuple[str, str]] = []
        for name, _ in candidates:
            try:
                t3_db.delete_collection(name)
                actually_deleted += 1
            except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
                failures.append((name, str(exc)))
        click.echo(
            f"\nSummary: deleted {actually_deleted} zombie collection(s) "
            f"of {len(candidates)} candidate(s)."
        )
        if failures:
            click.echo(f"  {len(failures)} delete(s) failed:")
            for name, err in failures[:10]:
                click.echo(f"    {name}: {err}")
            raise SystemExit(1)
    else:
        click.echo(
            f"\nSummary: would delete {len(candidates)} zombie collection(s). "
            "Re-run with --apply to actually delete."
        )


def register(group: click.Group) -> None:
    """Attach the collection-management commands to the shared ``catalog`` group."""
    group.add_command(backfill_collections_cmd)
    group.add_command(collection_name_cmd)
    group.add_command(rename_collection_cmd)
    group.add_command(collection_gc_cmd)
