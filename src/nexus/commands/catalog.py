# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
from pathlib import Path

import click

import structlog

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)


def _resolve_plugin_root(repo_root: Path) -> Path:
    """Resolve the ``plugin_root`` for :func:`load_all_tiers`.

    Returns the directory whose ``plans/builtin`` subtree contains
    the shipped YAML plan templates. Tries three resolvers in order
    and returns the first whose ``plans/builtin`` is a directory:

    1. **Package-data resource** — ``importlib.resources.files
       ("nexus") / "_resources"``. Resolves inside
       ``<site-packages>/nexus/_resources`` for wheel installs (via
       the ``[tool.hatch.build.targets.wheel.force-include]``
       mapping in pyproject.toml) and inside
       ``src/nexus/_resources`` for editable installs (via the
       symlink back into ``nx/plans``).
    2. **Repo-root relative** — ``<repo_root>/nx``. Works when the
       caller runs ``nx catalog setup`` from a nexus checkout.
    3. **Legacy ``__file__``-relative walk** — four levels up from
       this module plus ``/nx``. Retained for unusual install
       layouts that neither of the above covers.

    If none of the three resolves, returns the resource candidate so
    the caller's fail-loud guard surfaces a helpful error naming the
    package-data location.
    """
    from importlib.resources import as_file, files

    candidates: list[Path] = []
    try:
        resource = files("nexus") / "_resources"
        with as_file(resource) as resolved:
            candidates.append(Path(resolved))
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        # Importlib resources can raise for exotic loaders (e.g. zip
        # imports without extraction). Fall through to the filesystem
        # resolvers.
        pass
    candidates.extend([
        repo_root / "nx",
        Path(__file__).resolve().parent.parent.parent.parent / "nx",
    ])
    for candidate in candidates:
        if (candidate / "plans" / "builtin").is_dir():
            return candidate
    return candidates[0]


def _seed_plan_templates() -> int:
    """Seed pre-built plan templates into T2. Idempotent — skips existing.

    All templates are shipped as YAML files under ``nx/plans/builtin/``
    and loaded via the four-tier loader, which deduplicates on the
    ``UNIQUE (project, dimensions)`` partial index (nexus-05i.6).

    The legacy :data:`_PLAN_TEMPLATES` array retired in RDR-092 Phase 0a:
    three of its entries migrated to dimensional YAML builtins
    (``find-by-author``, ``citation-traversal``, ``type-scoped-search``);
    the other two were retired as redundant with existing defaults
    (``research-default`` covers the provenance shape,
    ``analyze-default`` covers the multi-corpus compare shape).

    Returns the total number of newly-inserted rows.
    """
    from pathlib import Path

    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    seeded = 0
    with T2Database(default_db_path()) as db:
        from nexus.indexer_utils import find_repo_root
        from nexus.plans.loader import load_all_tiers

        repo_root = find_repo_root(Path.cwd()) or Path.cwd()
        # Plugin root resolution (RDR-092 nexus-b9f3). The nx/ plan
        # YAMLs ship as wheel package data via hatch force-include
        # (see pyproject.toml), landing at
        # ``<site-packages>/nexus/_resources/plans/builtin/*.yml`` on
        # installed builds. Editable installs get the same path via
        # a ``src/nexus/_resources/plans`` symlink back into
        # ``nx/plans``. Either way ``importlib.resources.files`` is
        # the correct resolver; it handles both cases and MultiplexedPath
        # from namespace packages transparently.
        #
        # The tiered fallback (repo root, legacy __file__ walk) is
        # kept as a belt-and-braces measure for unusual install
        # layouts, but real-world CLIs should always hit the
        # ``importlib.resources`` branch.
        plugin_root = _resolve_plugin_root(repo_root)
        tier_results = load_all_tiers(
            plugin_root=plugin_root,
            repo_root=repo_root,
            library=db.plans,
        )

        # RDR-092 Phase 0c.1: fail loud on an empty global tier. A
        # missing or empty ``nx/plans/builtin`` is a deployment gap
        # (plugin root misrouted, YAMLs deleted) that silently leaves
        # the library without dimensional seeds. The loader normally
        # logs this via ``_log.info("seed_directory_missing")``; the
        # setup CLI needs a user-visible failure, not a structured
        # info.
        global_result = tier_results.get("global")
        global_scanned = (
            global_result.total_scanned if global_result is not None else 0
        )
        if global_scanned == 0:
            raise click.ClickException(
                "Plan library seed failed: global tier is empty (no "
                f"YAML builtins found at {plugin_root / 'plans' / 'builtin'}). "
                "This typically means the plugin root is misconfigured "
                "or the shipped builtin YAMLs were removed. Re-install "
                "the nx plugin or run 'nx doctor --check-plan-library' "
                "for diagnostics."
            )

        for scope, result in tier_results.items():
            for source, error in result.errors:
                # Structured log for machine consumption.
                _log.warning(
                    "rdr078_seed_load_error",
                    scope=scope, source=source, error=error,
                )
                # User-visible echo so setup output distinguishes
                # 'files found but some malformed' from the quiet
                # healthy case. Stays on stderr to preserve stdout
                # for the success count.
                click.echo(
                    f"  warning: seed load error in {scope} tier "
                    f"({source}): {error}",
                    err=True,
                )
            seeded += len(result.inserted)

    return seeded


def _get_catalog() -> Catalog:
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' to create and populate it."
        )
    return Catalog(path, path / ".catalog.db")


def _resolve_tumbler(cat: Catalog, value: str) -> Tumbler:
    """Resolve a tumbler string OR title/filename. Raises ClickException on failure."""
    from nexus.catalog import resolve_tumbler
    t, err = resolve_tumbler(cat, value)
    if err:
        raise click.ClickException(err)
    return t



@click.group()
def catalog() -> None:
    """Document catalog — tracks every indexed document and the links between them.

    The catalog knows what you've indexed (repos, PDFs, papers, RDRs), where
    each document lives in T3, and how documents relate to each other (citations,
    implementations, supersedes). Think of it as the index card system for your
    knowledge base — search by metadata, browse by relationship, trace provenance.

    \b
    First time? Run setup:
      nx catalog setup              # one command: init + populate + link

    \b
    Find documents:
      nx catalog search auth        # search by title, author, file path
      nx catalog show "auth module" # full entry with all links
      nx catalog list               # browse all entries

    \b
    Explore relationships:
      nx catalog links "paper X"            # what links to/from this?
      nx catalog links --type cites         # all citation links
      nx catalog links --created-by bib_enricher  # links by creator

    \b
    Agents use the catalog via MCP tools (catalog_search, catalog_links,
    catalog_link). Use /nx:query for multi-step citation and provenance queries.
    """


@catalog.command("init")
@click.option("--remote", default="", help="Optional git remote URL")
def init_cmd(remote: str) -> None:
    """Initialize catalog git repository."""
    from nexus.config import catalog_path

    path = catalog_path()
    Catalog.init(path, remote=remote or None)
    click.echo(f"Catalog initialized at {path}")


@catalog.command("setup")
@click.option("--remote", default="", help="Optional git remote URL")
def setup_cmd(remote: str) -> None:
    """Get the catalog running in one step.

    Creates the catalog, populates it from your existing T3 collections and
    repos, then generates citation and code-RDR links from metadata. After
    this, 'nx catalog search' and 'nx catalog links' work immediately.
    """
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        Catalog.init(path, remote=remote or None)
        click.echo(f"Catalog initialized at {path}")
    else:
        click.echo(f"Catalog already initialized at {path}")

    cat = Catalog(path, path / ".catalog.db")

    try:
        registry = _make_registry()
        t3 = _make_t3()

        import signal

        def _timeout_handler(signum, frame):
            raise TimeoutError("T3 cloud call timed out — try again later or check connectivity")

        repo_count = paper_count = knowledge_count = 0

        click.echo("Populating from repos...")
        repo_count, repo_collections = _backfill_repos(cat, registry, dry_run=False)
        click.echo(f"  {repo_count} repo entries")

        # Paper and knowledge backfill query T3 cloud — timeout after 60s each
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        try:
            click.echo("Populating from paper collections...")
            signal.alarm(60)
            paper_count = _backfill_papers(cat, t3, dry_run=False, repo_collections=repo_collections)
            signal.alarm(0)
            click.echo(f"  {paper_count} paper entries")

            click.echo("Populating from knowledge collections...")
            signal.alarm(30)
            knowledge_count = _backfill_knowledge(cat, t3, dry_run=False)
            signal.alarm(0)
            click.echo(f"  {knowledge_count} knowledge entries")

            click.echo("Populating from RDR collections...")
            signal.alarm(30)
            rdr_count = _backfill_rdrs(cat, t3, dry_run=False)
            signal.alarm(0)
            click.echo(f"  {rdr_count} RDR entries")
        except TimeoutError as exc:
            signal.alarm(0)
            click.echo(f"  Timed out ({exc}). Partial results saved — rerun setup to continue.")
        finally:
            signal.signal(signal.SIGALRM, old_handler)
    except Exception as exc:
        click.echo(f"  Backfill incomplete ({type(exc).__name__}: {exc})")

    click.echo("Backfilling chunk_text_hash...")
    from nexus.commands.collection import _backfill_chunk_text_hash
    hash_updated = 0
    try:
        for col_info in t3.list_collections():
            col = t3._client.get_collection(col_info["name"])
            updated, _, _ = _backfill_chunk_text_hash(col)
            hash_updated += updated
    except Exception as exc:
        click.echo(f"  Hash backfill partial ({type(exc).__name__}: {exc})")
    click.echo(f"  {hash_updated} chunks updated")

    click.echo("Generating links...")
    from nexus.catalog.link_generator import generate_citation_links
    cites = generate_citation_links(cat)
    click.echo(f"  Citations: {cites}")

    click.echo("Seeding plan templates...")
    seeded = _seed_plan_templates()
    click.echo(f"  {seeded} templates seeded")

    # Check if a remote is configured for durability
    import subprocess
    result = subprocess.run(
        ["git", "remote"], cwd=str(path), capture_output=True, text=True, timeout=5,
    )
    if not result.stdout.strip():
        click.echo(
            "\nSetup complete. Catalog is local-only — add a git remote for durability:\n"
            f"  cd {path} && git remote add origin <your-repo-url>\n"
            "  nx catalog sync"
        )
    else:
        click.echo("Setup complete.")


@catalog.command("backfill-collections")
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
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415

    cat = _get_catalog()

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

    rows = cat._db.execute(
        "SELECT DISTINCT physical_collection FROM documents "
        "WHERE physical_collection != ''"
    ).fetchall()
    catalog_names = {r[0] for r in rows if r[0]}

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

    for name in to_register:
        cat.register_collection(name)

    click.echo(
        f"\nDone: {len(to_register)} new, "
        f"{len(already)} already registered."
    )


@catalog.command("migrate-fallback")
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
    from nexus.corpus import (  # noqa: PLC0415
        is_conformant_collection_name, voyage_model_for_collection,
    )

    cat = _get_catalog()

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

    rows = cat._db.execute(
        "SELECT tumbler FROM documents WHERE physical_collection = ? "
        "ORDER BY tumbler",
        (source,),
    ).fetchall()

    if not rows:
        click.echo(f"{source}: 0 doc(s) to migrate.")
        return

    from nexus.catalog.collection_name import owner_segment_for_tumbler  # noqa: PLC0415

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
        from nexus.corpus import parse_conformant_collection_name  # noqa: PLC0415
        segments = parse_conformant_collection_name(target)
        cat.register_collection(
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
    cat.update_documents_collection_batch(proposals)

    if len(targets_seen) == 1:
        only_target = next(iter(targets_seen))
        cat.supersede_collection(
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


@catalog.command("collection-name")
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
    cat = _get_catalog()
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


@catalog.command("rename-collection")
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
    from nexus.commands.collection import (  # noqa: PLC0415
        rename_collection_data_plane,
    )
    from nexus.corpus import (  # noqa: PLC0415
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    from nexus.db import make_t3  # noqa: PLC0415

    cat = _get_catalog()
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

    will_run = yes and not dry_run
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
            cat.register_collection(
                new,
                content_type=segments["content_type"],
                owner_id=segments["owner_id"],
                embedding_model=segments["embedding_model"],
                model_version=segments["model_version"],
            )
        else:
            cat.register_collection(new)
        cat.supersede_collection(old, new, reason="rename-collection")
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


@catalog.command("list")
@click.option("--owner", default="")
@click.option("--type", "content_type", default="")
@click.option("--limit", "-n", default=50)
@click.option("--offset", default=0)
@click.option("--json", "as_json", is_flag=True)
def list_cmd(owner: str, content_type: str, limit: int, offset: int, as_json: bool) -> None:
    """List catalog entries."""
    cat = _get_catalog()
    if owner:
        # Resolve --owner to a Tumbler. Try the dotted-tumbler form first
        # (the canonical input), then fall back to looking up by owner
        # name (#537 / nexus-1lx7) so operators can paste back the
        # human-readable name they see in catalog output. Without this
        # fallback, non-dotted strings leaked the raw int() ValueError
        # from Tumbler.parse as a stack trace.
        owner_tumbler: Tumbler | None
        try:
            owner_tumbler = Tumbler.parse(owner)
        except (ValueError, TypeError):
            matches = cat.owner_tumblers_by_name(owner)
            if not matches:
                raise click.ClickException(
                    f"--owner {owner!r}: not a dotted tumbler "
                    f"(e.g. '1.2') and no owner has this name. "
                    f"Use `nx catalog stats` to list known owners."
                )
            if len(matches) > 1:
                candidates = ", ".join(str(t) for t in matches)
                raise click.ClickException(
                    f"--owner {owner!r}: ambiguous; "
                    f"{len(matches)} owners share this name across "
                    f"types ({candidates}). Pass the dotted tumbler "
                    f"directly to disambiguate."
                )
            owner_tumbler = matches[0]
        entries = cat.by_owner(owner_tumbler)
        # GH #568: --owner path is by-owner-then-Python-filter (the
        # owner cardinality is naturally small so the post-filter is
        # safe). Apply the type filter here too if provided.
        if content_type:
            entries = [e for e in entries if e.content_type == content_type]
    else:
        # GH #568: push --type into SQL so pagination on a small-
        # cardinality content_type doesn't return empty. Pre-fix the
        # filter ran Python-side AFTER LIMIT/OFFSET; a 15K-entry
        # catalog with only 2 rdr rows had ``--type rdr -n 3`` return
        # 0. Mirrors PR #533's fix for the MCP catalog_list surface.
        entries = cat.all_documents(
            limit=limit + offset + 1, content_type=content_type,
        )
    total = len(entries)
    entries = entries[offset:offset + limit]

    if as_json:
        click.echo(json.dumps([e.to_dict() for e in entries], indent=2))
    else:
        for e in entries:
            click.echo(f"{str(e.tumbler):<12} {e.content_type:<10} {e.title}")
        if offset + limit < total:
            click.echo(f"\n  Next page: --offset {offset + limit}")


@catalog.command("show")
@click.argument("tumbler_or_title")
@click.option("--json", "as_json", is_flag=True)
def show_cmd(tumbler_or_title: str, as_json: bool) -> None:
    """Show everything about a document: metadata, collection, and all links.

    Accepts a tumbler (1.9.14) or a title/filename. Use --json for machine-readable output.
    """
    cat = _get_catalog()
    t = _resolve_tumbler(cat, tumbler_or_title)
    entry = cat.resolve(t)
    if entry is None:
        raise click.ClickException(f"Not found: {tumbler_or_title}")

    if as_json:
        d = entry.to_dict()
        d["links_from"] = [l.to_dict() for l in cat.links_from(entry.tumbler)]
        d["links_to"] = [l.to_dict() for l in cat.links_to(entry.tumbler)]
        click.echo(json.dumps(d, indent=2))
    else:
        click.echo(f"Tumbler:    {entry.tumbler}")
        click.echo(f"Title:      {entry.title}")
        click.echo(f"Author:     {entry.author}")
        click.echo(f"Year:       {entry.year}")
        click.echo(f"Type:       {entry.content_type}")
        click.echo(f"File:       {entry.file_path}")
        if entry.source_uri:
            click.echo(f"URI:        {entry.source_uri}")
        click.echo(f"Corpus:     {entry.corpus}")
        click.echo(f"Collection: {entry.physical_collection}")
        click.echo(f"Chunks:     {entry.chunk_count}")
        click.echo(f"Hash:       {entry.head_hash}")
        click.echo(f"Indexed:    {entry.indexed_at}")
        out_links = cat.links_from(entry.tumbler)
        in_links = cat.links_to(entry.tumbler)
        if out_links:
            click.echo("Links out:")
            for lnk in out_links:
                span_note = f" [{lnk.to_span}]" if lnk.to_span else ""
                click.echo(f"  → {lnk.to_tumbler} ({lnk.link_type}){span_note}")
                if lnk.to_span:
                    text = cat.resolve_span_text(lnk.to_tumbler, lnk.to_span)
                    if text:
                        preview = text[:120].replace("\n", " ")
                        click.echo(f"    \"{preview}{'...' if len(text) > 120 else ''}\"")
        if in_links:
            click.echo("Links in:")
            for lnk in in_links:
                span_note = f" [{lnk.from_span}]" if lnk.from_span else ""
                click.echo(f"  ← {lnk.from_tumbler} ({lnk.link_type}){span_note}")
                if lnk.from_span:
                    text = cat.resolve_span_text(lnk.from_tumbler, lnk.from_span)
                    if text:
                        preview = text[:120].replace("\n", " ")
                        click.echo(f"    \"{preview}{'...' if len(text) > 120 else ''}\"")



@catalog.command("search")
@click.argument("query")
@click.option("--limit", "-n", default=20)
@click.option("--offset", default=0)
@click.option("--json", "as_json", is_flag=True)
def search_cmd(query: str, limit: int, offset: int, as_json: bool) -> None:
    """Find documents by title, author, corpus, or file path.

    Uses full-text search across document metadata. Faster than T3 semantic
    search for exact metadata lookups. Returns tumbler, type, and title.
    """
    cat = _get_catalog()
    all_results = cat.find(query)
    total = len(all_results)
    results = all_results[offset:offset + limit]
    if as_json:
        click.echo(json.dumps([e.to_dict() for e in results], indent=2))
    else:
        if not results:
            click.echo("No results.")
            return
        for e in results:
            click.echo(f"{str(e.tumbler):<12} {e.content_type:<10} {e.title}")
        if offset + limit < total:
            click.echo(f"\n  Next page: --offset {offset + limit}")


@catalog.command("register", hidden=True)
@click.option("--title", "-t", required=True)
@click.option("--owner", "-o", required=True)
@click.option("--author", default="")
@click.option("--year", default=0, type=int)
@click.option("--type", "content_type", default="paper")
@click.option("--file-path", default="")
@click.option(
    "--source-uri", default="",
    help=(
        "Persistent URI identity (RDR-096 P3.1). Omit to derive "
        "'file://<abspath>' from --file-path automatically. Pass an "
        "explicit URI (chroma://, https://, nx-scratch://, "
        "x-devonthink-item://) to store verbatim. Malformed URIs "
        "are rejected at register-time."
    ),
)
@click.option("--corpus", default="")
def register_cmd(
    title: str, owner: str, author: str, year: int,
    content_type: str, file_path: str, source_uri: str, corpus: str,
) -> None:
    """Register a document in the catalog."""
    from nexus.catalog.catalog import make_relative

    cat = _get_catalog()
    # Relativize absolute file_path if under a known repo (RDR-060)
    fp = file_path
    if fp and Path(fp).is_absolute():
        from nexus.catalog.catalog import _default_registry_path
        from nexus.registry import RepoRegistry

        reg_path = _default_registry_path()
        if reg_path.exists():
            for repo_path_str in RepoRegistry(reg_path).all_info():
                rel = make_relative(fp, Path(repo_path_str))
                if rel != fp:
                    fp = rel
                    break

    try:
        tumbler = cat.register(
            Tumbler.parse(owner), title,
            content_type=content_type, file_path=fp,
            corpus=corpus, author=author, year=year,
            source_uri=source_uri,
        )
    except ValueError as exc:
        # P3.1 register-boundary validation surfaced a malformed URI.
        # Hard error rather than silent persistence.
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Registered: {tumbler}")


@catalog.command("update")
@click.argument("tumbler", default="")
@click.option("--title", default="")
@click.option("--author", default="")
@click.option("--year", default=0, type=int)
@click.option("--corpus", default="")
@click.option("--meta", default="", help="JSON string of additional metadata")
@click.option(
    "--source-uri",
    "source_uri",
    default="",
    help="Catalog source identity URI (e.g. x-devonthink-item://<UUID>). "
         "Recovery path for entries whose DT-URI stamp failed during "
         "nx dt index, or for manual reassignment of catalog identity.",
)
@click.option("--owner", default="", help="Batch: update all entries for this owner")
@click.option("--search", "search_query", default="", help="Batch: update all entries matching this search")
def update_cmd(
    tumbler: str, title: str, author: str, year: int, corpus: str, meta: str,
    source_uri: str, owner: str, search_query: str,
) -> None:
    """Update catalog entry metadata. TUMBLER can be a tumbler or title.

    Batch mode: use --owner or --search to update multiple entries at once.
    Example: nx catalog update --owner 1.9 --corpus schema-evolution

    --source-uri sets or replaces the catalog identity URI. Use this to
    recover an entry whose DT-URI stamp failed during 'nx dt index'
    (the entry will carry source_uri=file://… instead of x-devonthink-
    item://<UUID>). The URI is validated against the same scheme allowlist
    as register-time.
    """
    cat = _get_catalog()
    fields: dict = {}
    if title:
        fields["title"] = title
    if author:
        fields["author"] = author
    if year:
        fields["year"] = year
    if corpus:
        fields["corpus"] = corpus
    if meta:
        fields["meta"] = json.loads(meta)
    if source_uri:
        fields["source_uri"] = source_uri
    if not fields:
        raise click.ClickException("No fields to update")

    # Batch mode
    if owner or search_query:
        entries = []
        if owner:
            entries = cat.by_owner(Tumbler.parse(owner))
        elif search_query:
            entries = cat.find(search_query)
        if not entries:
            raise click.ClickException("No entries matched")
        try:
            for entry in entries:
                cat.update(entry.tumbler, **fields)
        except ValueError as exc:
            # nexus-fb6x: source_uri / file_path validation can raise
            # ValueError (unknown scheme, malformed URI, owner-root
            # mismatch). Re-raise as ClickException so the operator
            # sees a clean error line instead of a stack trace.
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated {len(entries)} entries")
        return

    # Single entry mode
    if not tumbler:
        raise click.ClickException("Provide a tumbler/title or use --owner/--search for batch")
    t = _resolve_tumbler(cat, tumbler)
    try:
        cat.update(t, **fields)
    except ValueError as exc:
        # nexus-fb6x: same UX-cleanup as the batch path.
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Updated: {t}")


@catalog.command("delete")
@click.argument("tumbler_or_title")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def delete_cmd(tumbler_or_title: str, yes: bool) -> None:
    """Remove a document from the catalog. Links to it are preserved as orphans.

    Accepts a tumbler or title. Prompts for confirmation unless -y is passed.
    The document is removed from SQLite and tombstoned in JSONL, but existing
    links remain — use 'nx catalog links --type ...' to find orphaned links.
    """
    cat = _get_catalog()
    t = _resolve_tumbler(cat, tumbler_or_title)
    entry = cat.resolve(t)
    if entry is None:
        raise click.ClickException(f"Not found: {tumbler_or_title}")
    if not yes:
        click.confirm(
            f"Delete '{entry.title}' ({t})? Links will be preserved.",
            abort=True,
        )

    # Backup snapshot before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_documents
    backup_path = snapshot_documents(
        cat, [str(t)], verb="delete",
        reason=f"single-document delete: {entry.title}",
    )
    if backup_path:
        click.echo(
            f"Backup snapshot: {backup_path.name}"
            f"  (restore: nx catalog undelete {backup_path.name})"
        )

    deleted = cat.delete_document(t)
    if deleted:
        click.echo(f"Deleted: {t} ({entry.title}). Links preserved.")
    else:
        click.echo(f"Not found: {t}")


@catalog.command("link")
@click.argument("from_tumbler")
@click.argument("to_tumbler")
@click.option(
    "--type", "link_type", required=True,
    help="Link type (e.g. cites, implements, supersedes, relates, quotes, comments, or any custom type)",
)
@click.option("--from-span", default="", help="Span: 'line-line', 'chunk:char-char', 'chash:<hex>', or 'chash:<hex>:<start>-<end>'")
@click.option("--to-span", default="", help="Span: 'line-line', 'chunk:char-char', 'chash:<hex>', or 'chash:<hex>:<start>-<end>'")
def link_cmd(
    from_tumbler: str, to_tumbler: str, link_type: str,
    from_span: str, to_span: str,
) -> None:
    """Create a typed link between two documents.

    Both FROM and TO accept tumblers (1.1.5) or titles. Built-in link types:
    cites, implements, implements-heuristic, supersedes, relates, quotes,
    comments, formalizes. Custom types are also accepted. Duplicate links
    are merged.

    \b
    Spans (optional) identify the specific passage being referenced:
      --from-span "42-57"              lines 42-57 of the source document
      --to-span "3:100-250"            chars 100-250 of chunk 3 in T3
      --from-span "chash:<sha256hex>"  content-addressed chunk (preferred)
      --from-span "chash:<sha256hex>:100-250"  character range within a chunk
    Content-hash spans survive re-indexing. Use 'nx catalog show' to see resolved span text.
    """
    cat = _get_catalog()
    ft = _resolve_tumbler(cat, from_tumbler)
    tt = _resolve_tumbler(cat, to_tumbler)
    cat.link(ft, tt, link_type, created_by="user", from_span=from_span, to_span=to_span)
    click.echo(f"Linked: {ft} → {tt} ({link_type})")


@catalog.command("unlink")
@click.argument("from_tumbler")
@click.argument("to_tumbler")
@click.option("--type", "link_type", default="")
def unlink_cmd(from_tumbler: str, to_tumbler: str, link_type: str) -> None:
    """Remove link(s) between two documents.

    Both FROM and TO accept tumblers or titles. Omit --type to remove all
    link types between the pair.
    """
    cat = _get_catalog()
    ft = _resolve_tumbler(cat, from_tumbler)
    tt = _resolve_tumbler(cat, to_tumbler)
    removed = cat.unlink(ft, tt, link_type)
    click.echo(f"Removed {removed} link(s)")


def _endpoint_label(cat: Any, tumbler: Any) -> str:
    """Render a tumbler as ``'<title-or-path> (<tumbler>)'`` for human output.

    Used by ``nx catalog links --resolve``. Prefers ``title`` for
    documents that have one, falls back to ``file_path`` for code /
    prose entries that carry a path, and finally to the bare tumbler
    for entries without either. bead nexus-iojz (formerly nexus-i63n).
    """
    try:
        entry = cat.resolve(tumbler)
    except Exception:
        return str(tumbler)
    if entry is None:
        return str(tumbler)
    label = entry.title or entry.file_path or str(tumbler)
    if label == str(tumbler):
        return str(tumbler)
    return f"{label} ({tumbler})"


def _unique_edges_by_target(cat: Any, edges: list) -> list:
    """Return a stable de-duplicated edge list keyed by ``(from_tumbler,
    link_type, target.file_path or target.tumbler)``.

    Re-indexing the same file under multiple owner tumblers leaves many
    edges that point at the same source file via different tumblers.
    The natural emission order surfaces the duplicate run first, so
    the first copy wins and later aliases drop. Fails open: if a
    target tumbler does not resolve, its bare string is the dedup key,
    so orphan rows are preserved. bead nexus-iojz (formerly nexus-x6eu).
    """
    seen: set[tuple[str, str, str]] = set()
    out: list = []
    for edge in edges:
        try:
            target = cat.resolve(edge.to_tumbler)
            target_key = (
                target.file_path if target and target.file_path else str(edge.to_tumbler)
            )
        except Exception:
            target_key = str(edge.to_tumbler)
        key = (str(edge.from_tumbler), edge.link_type, target_key)
        if key in seen:
            continue
        seen.add(key)
        out.append(edge)
    return out


@catalog.command("links")
@click.argument("tumbler", default="")
@click.option("--from", "from_t", default="", help="Filter by source tumbler or title")
@click.option("--to", "to_t", default="", help="Filter by target tumbler or title")
@click.option("--direction", default="both", type=click.Choice(["in", "out", "both"]))
@click.option("--type", "link_type", default="")
@click.option("--created-by", default="", help="Filter by creator (e.g. bib_enricher)")
@click.option("--depth", default=1, type=int, help="BFS depth for graph traversal")
@click.option("--limit", "-n", default=50, type=int)
@click.option("--offset", default=0, type=int)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--resolve", "resolve_labels", is_flag=True,
    help=(
        "Render each endpoint as '<title-or-path> (<tumbler>)' "
        "instead of bare tumbler arrows."
    ),
)
@click.option(
    "--unique-targets", "unique_targets", is_flag=True,
    help=(
        "Collapse rows that point at the same file_path via "
        "different owner tumblers (e.g. after re-indexing). Keeps "
        "the first seen edge per target."
    ),
)
def links_cmd(
    tumbler: str, from_t: str, to_t: str, direction: str,
    link_type: str, created_by: str, depth: int,
    limit: int, offset: int, as_json: bool,
    resolve_labels: bool, unique_targets: bool,
) -> None:
    """Show or query links. TUMBLER (optional) accepts a tumbler or title.

    \b
    Examples:
      nx catalog links 1.1.5                    # links for document
      nx catalog links "auth module" --type cites
      nx catalog links --created-by bib_enricher # all links by creator
      nx catalog links --type cites --json       # all citation links
      nx catalog links 1.17.14 --resolve         # inline titles / file paths
      nx catalog links 1.17.14 --type implements --unique-targets
    """
    cat = _get_catalog()

    def _render_edge(edge: Any) -> str:
        if resolve_labels:
            src = _endpoint_label(cat, edge.from_tumbler)
            dst = _endpoint_label(cat, edge.to_tumbler)
            return f"{src} → {dst} ({edge.link_type}) by {edge.created_by}"
        return (
            f"{edge.from_tumbler} → {edge.to_tumbler} "
            f"({edge.link_type}) by {edge.created_by}"
        )

    # If a positional tumbler is given, use graph traversal (the common case)
    if tumbler:
        t = _resolve_tumbler(cat, tumbler)
        result = cat.graph(t, depth=depth, direction=direction, link_type=link_type)
        edges = result["edges"]
        if unique_targets:
            edges = _unique_edges_by_target(cat, edges)
        if as_json:
            click.echo(json.dumps({
                "nodes": [n.to_dict() for n in result["nodes"]],
                "edges": [e.to_dict() for e in edges],
            }, indent=2))
        else:
            if not edges:
                click.echo("No links found.")
                return
            for edge in edges:
                click.echo(_render_edge(edge))
        return

    # No positional tumbler — flat filter query
    resolved_from = str(_resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_resolve_tumbler(cat, to_t)) if to_t else ""
    links = cat.link_query(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        limit=limit + 1, offset=offset,
    )
    has_more = len(links) > limit
    links = links[:limit]
    if unique_targets:
        links = _unique_edges_by_target(cat, links)
    if as_json:
        click.echo(json.dumps([lnk.to_dict() for lnk in links], indent=2))
    else:
        if not links:
            click.echo("No links found.")
            return
        for edge in links:
            click.echo(_render_edge(edge))
        if has_more:
            click.echo(f"\n  Next page: --offset {offset + limit}")


@catalog.command("link-bulk-delete", hidden=True)
@click.option("--from", "from_t", default="", help="From tumbler or title")
@click.option("--to", "to_t", default="", help="To tumbler or title")
@click.option("--type", "link_type", default="")
@click.option("--created-by", default="")
@click.option("--created-at-before", default="", help="ISO timestamp cutoff")
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete links.",
)
def link_bulk_delete_cmd(
    from_t: str, to_t: str, link_type: str, created_by: str,
    created_at_before: str, dry_run: bool, confirm: bool,
) -> None:
    """Bulk delete links matching filters.

    nexus-9nim: 4.29.1 inverted the default from "delete unless --dry-run"
    to "report unless --no-dry-run --confirm" + writes a backup snapshot
    before the actual delete. Recoverable via ``nx catalog undelete``.
    """
    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete links."
        )

    cat = _get_catalog()
    resolved_from = str(_resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_resolve_tumbler(cat, to_t)) if to_t else ""

    # First pass: dry-run to enumerate the matching links for the
    # backup snapshot. The cat.bulk_unlink dry_run path returns the
    # count; we need the actual rows for the snapshot, so query directly.
    matching_links = cat.link_query(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        created_at_before=created_at_before,
        limit=0,
    )
    count = len(matching_links)

    if not will_delete:
        click.echo(f"Would remove {count} link(s)")
        return

    # Backup snapshot before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_links
    backup_path = snapshot_links(
        cat,
        [
            {
                "from": str(lnk.from_tumbler),
                "to": str(lnk.to_tumbler),
                "link_type": lnk.link_type,
                "from_span": lnk.from_span,
                "to_span": lnk.to_span,
                "created_by": lnk.created_by,
                "created_at": lnk.created_at,
                "meta": lnk.meta,
            }
            for lnk in matching_links
        ],
        verb="link-bulk-delete",
        reason="bulk-unlink filters",
        args={
            "from_t": resolved_from, "to_t": resolved_to,
            "link_type": link_type, "created_by": created_by,
            "created_at_before": created_at_before,
        },
    )
    if backup_path:
        click.echo(
            f"Backup snapshot written: {backup_path}\n"
            f"  Restore with: nx catalog undelete {backup_path.name}"
        )

    actual = cat.bulk_unlink(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        created_at_before=created_at_before, dry_run=False,
    )
    click.echo(f"Removed {actual} link(s)")


@catalog.command("link-audit", hidden=True)
@click.option("--json", "as_json", is_flag=True)
def link_audit_cmd(as_json: bool) -> None:
    """Audit the link graph: stats, orphans, duplicates."""
    cat = _get_catalog()
    result = cat.link_audit()
    if as_json:
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"Total links:     {result['total']}")
        click.echo(f"Orphaned:        {result['orphaned_count']}")
        click.echo(f"Duplicates:      {result['duplicate_count']}")
        if result["by_type"]:
            click.echo("By type:")
            for t, c in sorted(result["by_type"].items()):
                click.echo(f"  {t:<12} {c}")
        if result["by_creator"]:
            click.echo("By creator:")
            for c, n in sorted(result["by_creator"].items()):
                click.echo(f"  {c:<20} {n}")
        if result["orphaned"]:
            click.echo("Orphaned links:")
            for o in result["orphaned"]:
                click.echo(f"  {o['from']} → {o['to']} ({o['type']})")


@catalog.command("owners")
@click.option("--json", "as_json", is_flag=True)
def owners_cmd(as_json: bool) -> None:
    """List registered owners."""
    cat = _get_catalog()
    rows = cat._db.execute(
        "SELECT tumbler_prefix, name, owner_type, repo_hash, description FROM owners"
    ).fetchall()
    if as_json:
        data = [
            {"tumbler": r[0], "name": r[1], "type": r[2], "repo_hash": r[3], "description": r[4]}
            for r in rows
        ]
        click.echo(json.dumps(data, indent=2))
    else:
        for r in rows:
            click.echo(f"{r[0]:<8} {r[2]:<10} {r[1]}")


@catalog.command("sync")
@click.option("--message", "-m", default="catalog update")
def sync_cmd(message: str) -> None:
    """Commit and push catalog changes."""
    cat = _get_catalog()
    cat.sync(message)
    click.echo("Catalog synced.")


@catalog.command("dedupe-owners")
@click.option("--apply", is_flag=True, default=False,
              help="Commit the plan. Default is dry-run.")
@click.option("--json", "as_json", is_flag=True,
              help="Emit the plan as JSON instead of a human summary.")
def dedupe_owners_cmd(apply: bool, as_json: bool) -> None:
    """Consolidate orphan owners (nexus-tmbh, part of nexus-b34f).

    Classifies each curator owner as:

    \b
      • alias   — synthetic ``<repo>-<hash>`` names map to a canonical
                  repo owner. Each doc is aliased via documents.alias_of
                  to its canonical equivalent (matched by file_path).
                  Rows stay so external references keep resolving.
      • remove  — ``int-cce-*`` / ``int-prov-*`` / ``pdf-e2e-*`` test
                  leakage predating RDR-060's autouse fixture. Documents,
                  links, and the owner row are all deleted with JSONL
                  tombstones.
      • skip    — everything else (papers, knowledge, standalone-docs …).

    Dry-run by default. Use ``--apply`` to commit, then ``nx catalog
    sync`` to push the audit trail.
    """
    cat = _get_catalog()
    from nexus.catalog import dedupe as _dedupe

    plan = _dedupe.plan_dedupe(cat)
    summary = plan.summary()

    if as_json:
        payload = {
            "dry_run": not apply,
            "summary": summary,
            "alias": [op.to_dict() for op in plan.alias],
            "remove": [op.to_dict() for op in plan.remove],
            "skip": [op.to_dict() for op in plan.skip],
        }
        if apply:
            payload["applied"] = _dedupe.apply_plan(cat, plan)
        click.echo(json.dumps(payload, indent=2))
        return

    label = "Would apply" if not apply else "Applying"
    click.echo(f"{label} dedupe plan:")
    click.echo(f"  alias:  {summary['alias']} owners, {summary['alias_docs']} docs")
    click.echo(f"  remove: {summary['remove']} owners, {summary['remove_docs']} docs")
    click.echo(f"  skip:   {summary['skip']} owners, {summary['skip_docs']} docs")

    def _section(title: str, items: list, show_canonical: bool = False) -> None:
        if not items:
            return
        click.echo(f"\n{title}:")
        for op in items:
            if show_canonical:
                click.echo(
                    f"  {op.orphan_prefix:<8} {op.orphan_name} "
                    f"({op.doc_count} docs) → {op.canonical_prefix} {op.canonical_name}"
                )
            else:
                click.echo(
                    f"  {op.orphan_prefix:<8} {op.orphan_name} "
                    f"({op.doc_count} docs)  — {op.reason}"
                )

    _section("Alias consolidation", plan.alias, show_canonical=True)
    _section("Unconditional removal", plan.remove)
    _section("Skipped (manual review)", plan.skip)

    if not apply:
        click.echo("\nDry-run only. Re-run with --apply to commit, "
                   "then `nx catalog sync` to push the audit trail.")
        return

    totals = _dedupe.apply_plan(cat, plan)
    click.echo("\nApplied:")
    click.echo(f"  orphans aliased:  {totals['orphans_aliased']} "
               f"({totals['aliased_docs']} docs, {totals['unmatched_docs']} unmatched)")
    click.echo(f"  orphans removed:  {totals['orphans_removed']} "
               f"({totals['removed_docs']} docs, {totals['removed_links']} links)")
    click.echo("\nRun `nx catalog sync` to commit and push the audit trail.")


@catalog.command("pull")
def pull_cmd() -> None:
    """Pull catalog from remote and rebuild SQLite."""
    cat = _get_catalog()
    cat.pull()
    click.echo("Catalog pulled and rebuilt.")


def _taxonomy_stats() -> dict | None:
    """Return a taxonomy stats block for ``nx catalog stats`` or ``None``.

    The catalog is three layers: nexus-catalog (owners / documents /
    links), ChromaDB (the physical rows), and CatalogTaxonomy (topics
    and projection assignments). ``stats`` previously enumerated only
    the first layer; this helper adds the third. Returns ``None`` when
    T2 is absent or carries no topic rows. Skips the block silently
    for users who have not run discover / project yet. bead nexus-iojz
    (formerly nexus-1n0t).
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    try:
        db_path = default_db_path()
    except Exception:
        return None
    if not db_path.exists():
        return None

    try:
        with T2Database(db_path) as db:
            conn = db.taxonomy.conn
            with db.taxonomy._lock:
                topic_total = conn.execute(
                    "SELECT count(*) FROM topics"
                ).fetchone()[0]
                if not topic_total:
                    return None
                assignment_total = conn.execute(
                    "SELECT count(*) FROM topic_assignments"
                ).fetchone()[0]
                distinct_topics = conn.execute(
                    "SELECT count(DISTINCT topic_id) FROM topic_assignments"
                ).fetchone()[0]
                by_source_rows = conn.execute(
                    "SELECT source_collection, count(*) "
                    "FROM topic_assignments "
                    "WHERE assigned_by = 'projection' "
                    "AND source_collection IS NOT NULL "
                    "AND source_collection != '' "
                    "GROUP BY source_collection "
                    "ORDER BY count(*) DESC"
                ).fetchall()
    except Exception:
        return None

    return {
        "topics": int(topic_total),
        "assignments": int(assignment_total),
        "distinct_topics_assigned": int(distinct_topics),
        "projection_by_source": {row[0]: int(row[1]) for row in by_source_rows},
    }


@catalog.command("stats")
@click.option("--json", "as_json", is_flag=True)
def stats_cmd(as_json: bool) -> None:
    """Show catalog statistics."""
    cat = _get_catalog()
    db = cat._db
    owner_count = db.execute("SELECT count(*) FROM owners").fetchone()[0]
    doc_count = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    link_count = db.execute("SELECT count(*) FROM links").fetchone()[0]
    type_counts = dict(
        db.execute(
            "SELECT content_type, count(*) FROM documents GROUP BY content_type"
        ).fetchall()
    )
    link_type_counts = dict(
        db.execute(
            "SELECT link_type, count(*) FROM links GROUP BY link_type"
        ).fetchall()
    )
    tax = _taxonomy_stats()
    if as_json:
        payload: dict = {
            "owners": owner_count,
            "documents": doc_count,
            "links": link_count,
            "by_type": type_counts,
            "by_link_type": link_type_counts,
        }
        if tax is not None:
            payload["taxonomy"] = tax
        click.echo(json.dumps(payload, indent=2))
    else:
        click.echo(f"Owners:    {owner_count}")
        click.echo(f"Documents: {doc_count}")
        click.echo(f"Links:     {link_count}")
        if type_counts:
            click.echo("By type:")
            for t, c in sorted(type_counts.items()):
                click.echo(f"  {t:<12} {c}")
        if link_type_counts:
            click.echo("By link type:")
            for t, c in sorted(link_type_counts.items()):
                click.echo(f"  {t:<12} {c}")
        if tax is not None:
            click.echo(
                f"Topics:    {tax['topics']} topics, "
                f"{tax['assignments']} assignments "
                f"({tax['distinct_topics_assigned']} distinct topics assigned)"
            )
            by_source = tax["projection_by_source"]
            if by_source:
                click.echo("Projection by source:")
                for src, count in sorted(
                    by_source.items(), key=lambda kv: -kv[1],
                ):
                    click.echo(f"  {src:<40} {count}")


@catalog.command("compact", hidden=True)
def compact_cmd() -> None:
    """Rewrite JSONL files to remove tombstones and duplicate overwrites."""
    cat = _get_catalog()
    removed = cat.compact()
    total = 0
    for filename, count in removed.items():
        click.echo(f"  {filename}: {count} lines removed")
        total += count
    click.echo(f"Compaction complete ({total} lines removed).")
    if total > 0:
        click.echo("Run 'nx catalog sync' to commit the compacted files.")


@catalog.command("audit-membership")
@click.argument("collection", required=False)
@click.option(
    "--all-collections",
    is_flag=True,
    help=(
        "Sweep every physical_collection in the catalog and emit a "
        "single summary report. nexus-3e4s Phase 3 — the post-fix "
        "health check. Incompatible with --purge-non-canonical and "
        "--canonical-home (per-collection contexts)."
    ),
)
@click.option(
    "--purge-non-canonical",
    is_flag=True,
    help=(
        "Delete catalog entries whose source_uri does not match the "
        "canonical home for COLLECTION. Default canonical = the home "
        "with the most entries; override with --canonical-home. "
        "Use with --dry-run to preview. Asks for confirmation unless "
        "--yes is passed."
    ),
)
@click.option(
    "--canonical-home",
    default="",
    help=(
        "Override the dominant-home calculation by specifying a "
        "substring that the canonical home must contain (e.g., "
        "'/git/ART'). Use when the contaminating entries outnumber "
        "the legitimate ones, so dominance is a misleading heuristic."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be deleted without writing.",
)
@click.option(
    "--yes", "-y", is_flag=True, help="Skip confirmation prompt for --purge-non-canonical.",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit per-home counts as JSON instead of human-readable lines.",
)
def audit_membership_cmd(
    collection: str | None,
    all_collections: bool,
    purge_non_canonical: bool,
    canonical_home: str,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Detect cross-project source_uri contamination in COLLECTION.

    Originated from ART-lhk1 (nexus-ow9f): 140 of 245 catalog rows
    in ``rdr__ART-8c2e74c0`` had ``source_uri`` rooted in
    ``/Users/.../nexus/`` rather than the project's expected
    ``/Users/.../ART/`` root. The collection's chunks live under one
    project's identity, so every contaminated entry was a guaranteed
    skip in ``nx enrich aspects`` (no chunks would match).

    The audit groups entries by source_uri "home" (the first 4 path
    segments for ``file://`` URIs, ``scheme://netloc`` otherwise),
    surfaces per-home counts, and identifies the dominant home.
    With ``--purge-non-canonical`` the non-dominant entries are
    soft-deleted (tombstoned in JSONL, removed from SQLite) by the
    standard ``delete_document`` path.

    With ``--all-collections`` the audit runs across every
    physical_collection in the catalog and emits one summary report
    (nexus-3e4s Phase 3). Read-only — purge is per-collection only.
    """
    if all_collections:
        if purge_non_canonical:
            raise click.UsageError(
                "--all-collections is read-only; --purge-non-canonical "
                "must be invoked per-collection so the canonical-home "
                "decision can be reviewed before deletion.",
            )
        if canonical_home:
            raise click.UsageError(
                "--canonical-home is per-collection by definition; "
                "use it with a single COLLECTION argument.",
            )
        if collection:
            raise click.UsageError(
                "Pass either COLLECTION or --all-collections, not both.",
            )
        _audit_membership_all(as_json=as_json)
        return
    if not collection:
        raise click.UsageError(
            "Specify a COLLECTION or use --all-collections.",
        )
    cat = _get_catalog()
    rows = cat._db.execute(
        "SELECT tumbler, source_uri FROM documents "
        "WHERE physical_collection = ?",
        (collection,),
    ).fetchall()

    if not rows:
        if as_json:
            click.echo(json.dumps({
                "collection": collection,
                "total_entries": 0,
                "distinct_homes": 0,
                "by_home": {},
                "dominant_home": None,
            }))
        else:
            click.echo(f"No entries in '{collection}'.")
        return

    by_home: dict[str, list[str]] = {}
    for tumbler_str, source_uri in rows:
        host = _source_uri_home_key(source_uri or "")
        by_home.setdefault(host, []).append(tumbler_str)

    home_counts = {k: len(v) for k, v in by_home.items()}
    dominant_home = max(home_counts.items(), key=lambda kv: kv[1])[0]
    distinct = len(home_counts)

    # Resolve canonical home: explicit substring match wins over the
    # numerical dominant. ART-lhk1 needs this when the contamination
    # exceeds 50% — the user knows /git/ART is canonical even though
    # the leaked /git/nexus entries outnumber the legitimate ones.
    if canonical_home:
        matches = [h for h in home_counts if canonical_home in h]
        if not matches:
            raise click.ClickException(
                f"--canonical-home substring {canonical_home!r} matches "
                f"no home in {sorted(home_counts.keys())!r}"
            )
        if len(matches) > 1:
            raise click.ClickException(
                f"--canonical-home substring {canonical_home!r} is "
                f"ambiguous; matches {matches!r}. Tighten the substring."
            )
        resolved_canonical = matches[0]
    else:
        resolved_canonical = dominant_home

    if as_json:
        click.echo(json.dumps({
            "collection": collection,
            "total_entries": len(rows),
            "distinct_homes": distinct,
            "by_home": home_counts,
            "dominant_home": dominant_home,
            "canonical_home": resolved_canonical,
        }, indent=2))
    else:
        click.echo(
            f"Collection '{collection}': {len(rows)} entries, "
            f"{distinct} distinct source_uri home(s)."
        )
        for home, count in sorted(home_counts.items(), key=lambda kv: -kv[1]):
            tags = []
            if home == dominant_home:
                tags.append("dominant")
            if home == resolved_canonical:
                tags.append("canonical")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            click.echo(f"  {count:5d}  {home or '(empty source_uri)'}{tag_str}")
        if distinct == 1:
            click.echo(
                "Single source_uri home; no contamination detected."
            )

    if not purge_non_canonical:
        return
    if distinct < 2:
        return  # Nothing to purge.

    purge_targets: list[str] = []
    for home, tumblers in by_home.items():
        if home != resolved_canonical:
            purge_targets.extend(tumblers)

    if dry_run:
        click.echo(
            f"\n[dry-run] Would delete {len(purge_targets)} entries "
            f"whose source_uri home differs from {resolved_canonical!r}."
        )
        return

    if not yes:
        click.confirm(
            f"\nDelete {len(purge_targets)} catalog entries whose "
            f"source_uri home differs from {resolved_canonical!r}? "
            f"Links will be preserved (orphaned).",
            abort=True,
        )

    deleted = 0
    for t_str in purge_targets:
        try:
            t = Tumbler.parse(t_str)
        except Exception as e:
            click.echo(f"  skip {t_str}: parse error {e}")
            continue
        if cat.delete_document(t):
            deleted += 1
    click.echo(f"\nDeleted {deleted} of {len(purge_targets)} non-canonical entries.")


def _audit_membership_all(*, as_json: bool) -> None:
    """Sweep every physical_collection and emit a contamination summary.

    nexus-3e4s Phase 3 + critique-followup C2. Reads the catalog in a
    single pass and groups rows by (collection, home). Owner context is
    layered on top so single-home collections whose dominant home does
    not match the owning ``repo``'s ``repo_root`` are flagged as 100%
    contaminated rather than silently passing as "clean" — the failure
    mode that masked ~4,200 wrong-home rows in ``code__ART-...``
    pre-fix.
    """
    cat = _get_catalog()
    rows = cat._db.execute(
        "SELECT physical_collection, source_uri, tumbler FROM documents "
        "WHERE physical_collection != ''",
    ).fetchall()

    owner_rows = cat._db.execute(
        "SELECT tumbler_prefix, owner_type, repo_root FROM owners",
    ).fetchall()
    owners_by_prefix: dict[str, dict[str, str]] = {
        row[0]: {"owner_type": row[1] or "", "repo_root": row[2] or ""}
        for row in owner_rows
    }

    by_collection: dict[str, dict[str, int]] = {}
    collection_owners: dict[str, set[str]] = {}
    for collection, source_uri, tumbler_str in rows:
        home = _source_uri_home_key(source_uri or "")
        bucket = by_collection.setdefault(collection, {})
        bucket[home] = bucket.get(home, 0) + 1
        try:
            owner_prefix = str(Tumbler.parse(tumbler_str).owner_address())
            collection_owners.setdefault(collection, set()).add(owner_prefix)
        except Exception:
            pass

    records: list[dict] = []
    for collection, home_counts in by_collection.items():
        total = sum(home_counts.values())
        dominant = max(home_counts.items(), key=lambda kv: kv[1])[0]
        contaminated = total - home_counts[dominant]

        # Owner-aware overlay (nexus-3e4s C2): when the collection is
        # owned by exactly one ``repo`` owner with a known ``repo_root``,
        # check that the dominant home matches the owner's tree. A
        # mismatch flips the count to 100% — single-home + wrong-home
        # is the worst failure mode and otherwise reads as "clean".
        expected_root = ""
        wrong_home = False
        owner_prefixes = collection_owners.get(collection, set())
        if len(owner_prefixes) == 1:
            owner_info = owners_by_prefix.get(next(iter(owner_prefixes)))
            if (
                owner_info
                and owner_info["owner_type"] == "repo"
                and owner_info["repo_root"]
            ):
                expected_root = owner_info["repo_root"]
                if not _home_matches_root(dominant, expected_root):
                    contaminated = total
                    wrong_home = True

        records.append({
            "collection": collection,
            "total_entries": total,
            "distinct_homes": len(home_counts),
            "by_home": dict(home_counts),
            "dominant_home": dominant,
            "contaminated_entries": contaminated,
            "expected_home": expected_root,
            "wrong_home": wrong_home,
        })

    # Sort: contaminated count desc, then total desc, then name asc so
    # the worst offenders surface first and the order is stable.
    records.sort(key=lambda r: (
        -r["contaminated_entries"], -r["total_entries"], r["collection"],
    ))

    contaminated_count = sum(1 for r in records if r["contaminated_entries"] > 0)
    clean_count = len(records) - contaminated_count

    if as_json:
        click.echo(json.dumps({
            "total_collections": len(records),
            "contaminated_count": contaminated_count,
            "clean_count": clean_count,
            "collections": records,
        }, indent=2))
        return

    if not records:
        click.echo("0 collections in catalog.")
        return

    click.echo(
        f"Audited {len(records)} collections: "
        f"{contaminated_count} contaminated, {clean_count} clean.",
    )
    if contaminated_count == 0:
        click.echo("No contamination detected.")
        return

    click.echo()
    click.echo(f"Contaminated collections ({contaminated_count}):")
    for r in records:
        if r["contaminated_entries"] == 0:
            continue
        wrong_tag = " [wrong-home]" if r["wrong_home"] else ""
        expected_tag = (
            f"  expected={r['expected_home']}" if r["expected_home"] else ""
        )
        click.echo(
            f"  {r['contaminated_entries']:6d} of {r['total_entries']:6d}  "
            f"{r['collection']:40s}  "
            f"{r['distinct_homes']} homes  "
            f"dominant={r['dominant_home']}{expected_tag}{wrong_tag}",
        )

    if clean_count:
        click.echo()
        click.echo(f"Clean collections ({clean_count}):")
        for r in records:
            if r["contaminated_entries"] != 0:
                continue
            click.echo(
                f"  {r['total_entries']:6d}  {r['collection']}  "
                f"({r['dominant_home']})",
            )


def _home_matches_root(home: str, repo_root: str) -> bool:
    """Return True when ``home`` corresponds to the same project as ``repo_root``.

    ``home`` is the 4-segment prefix returned by ``_source_uri_home_key``
    for ``file://`` URIs; ``repo_root`` is the absolute path stored on
    the owner. They match when one is a prefix of the other (the home
    may be shallower than the root for nested repos, or deeper when the
    root itself sits at a non-standard depth).
    """
    if not home or not repo_root:
        return False
    real_home = os.path.realpath(home)
    real_root = os.path.realpath(repo_root)
    return real_home.startswith(real_root) or real_root.startswith(real_home)


_EMPTY_HOME_KEY = ""
_DEVONTHINK_HOME_KEY = "x-devonthink-item://"


def _source_uri_home_key(uri: str) -> str:
    """Stable grouping key for source_uri "home" detection.

    For ``file://`` URIs, returns the first four path segments
    (e.g. ``/Users/hal.hildebrand/git/ART``) so two entries from the
    same repo cluster regardless of the file inside that repo.

    For ``x-devonthink-item://`` URIs (RDR-099 DEVONthink integration),
    returns a fixed sentinel ``x-devonthink-item://`` so every UUID-
    netlocked DEVONthink reference collapses to ONE bucket. Pre-fix
    this returned ``<scheme>://<uuid>`` per chunk, making every
    DEVONthink import look like its own home. ``knowledge__art-
    grossberg-papers`` reported 110+ homes when it has at most 4
    logical roots; the audit's contamination signal was unreadable.

    Other schemes return ``<scheme>://<netloc>``.

    Empty URIs return :data:`_EMPTY_HOME_KEY` so the audit can
    distinguish "no source_uri" rows from real "single home" rows;
    callers that want a non-empty home count must filter the empty
    bucket explicitly (the contamination signal is "≥2 distinct
    NON-EMPTY homes", not just "≥2 distinct buckets"; a single self-
    marker row was previously enough to flip a small clean
    collection to "contaminated").

    Constants ``_EMPTY_HOME_KEY`` and ``_DEVONTHINK_HOME_KEY`` are
    exposed so consumers (audit-membership, doctor checks, tests)
    can pattern-match on them without re-implementing the literals.
    """
    from urllib.parse import urlparse

    if not uri:
        return _EMPTY_HOME_KEY
    p = urlparse(uri)
    if p.scheme == "file":
        # path = "/Users/hal.hildebrand/git/ART/docs/rdr/X.md"
        # parts = ["", "Users", "hal.hildebrand", "git", "ART", ...]
        # Take through the 5th component (the project root).
        parts = p.path.split("/")
        return "/".join(parts[:5]) if len(parts) >= 5 else p.path
    if p.scheme == "x-devonthink-item":
        # nexus-n3md: collapse all DEVONthink items to one bucket.
        # Per RDR-099 the UUID netloc is an opaque doc handle, not a
        # repo / namespace identifier; treating each as a distinct
        # home produced 110+ false-positive homes per collection.
        return _DEVONTHINK_HOME_KEY
    return f"{p.scheme}://{p.netloc}"


@catalog.command("orphans")
@click.option("--no-links", "no_links", is_flag=True, help="Show entries with zero incoming and outgoing links")
def orphans_cmd(no_links: bool) -> None:
    """Find catalog entries that are not connected to anything.

    \b
    Examples:
      nx catalog orphans --no-links    # entries with no links at all
    """
    if not no_links:
        raise click.UsageError("Specify a mode: --no-links")

    cat = _get_catalog()
    db = cat._db
    rows = db.execute(
        """
        SELECT tumbler, title, content_type, file_path
        FROM documents
        WHERE tumbler NOT IN (SELECT from_tumbler FROM links)
          AND tumbler NOT IN (SELECT to_tumbler FROM links)
        ORDER BY content_type, tumbler
        """
    ).fetchall()

    if not rows:
        click.echo("No orphan entries (all documents have at least one link).")
        return

    click.echo(f"Orphan entries ({len(rows)} with no links):")
    for tumbler, title, content_type, file_path in rows:
        loc = f"  [{file_path}]" if file_path else ""
        click.echo(f"  {tumbler:<12} {content_type:<10} {title}{loc}")


@catalog.command("verify")
@click.option(
    "--heal",
    is_flag=True,
    default=False,
    help="For each ghost, prompt to drop the tumbler or print the "
         "`nx store put` invocation that would repopulate it.",
)
@click.option(
    "--collection",
    "-c",
    default="",
    help="Restrict verification to a single physical_collection name.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON: {collection: [{tumbler, title, doc_id}]}.",
)
def verify_cmd(heal: bool, collection: str, json_out: bool) -> None:
    """Reconcile catalog tumblers against their T3 collection.

    Reports *ghost* tumblers — entries in the catalog with no matching
    row in ChromaDB. Ghosts most commonly survive from 4.9.7 / 4.9.8
    installs where an oversize `store_put` silently truncated before
    #244's guard landed. Fresh 4.9.9+ writes can no longer create new
    ghosts.

    The sweep is cheap: one `col.get(ids=[...], include=[])` per 300-id
    page — no ANN, no payload. Collections missing from T3 (deleted,
    renamed) are treated the same as missing ids.

    \b
    Examples:
      nx catalog verify                                  # full sweep
      nx catalog verify --collection knowledge__foo      # one collection
      nx catalog verify --heal                           # interactive fix
      nx catalog verify --json                           # CI-friendly output
    """
    import json as _json

    cat = _get_catalog()
    db = cat._db

    sql = (
        "SELECT tumbler, title, physical_collection, "
        "json_extract(metadata, '$.doc_id') AS doc_id "
        "FROM documents "
        "WHERE alias_of = '' "
        "  AND physical_collection IS NOT NULL "
        "  AND physical_collection != '' "
        "  AND doc_id IS NOT NULL "
    )
    params: tuple = ()
    if collection:
        sql += "  AND physical_collection = ? "
        params = (collection,)
    sql += "ORDER BY physical_collection, tumbler"
    rows = db.execute(sql, params).fetchall()

    if not rows:
        if collection:
            click.echo(f"No catalog tumblers with doc_id in {collection}.")
        else:
            click.echo("No catalog tumblers with doc_id metadata — nothing to verify.")
        return

    # Group by physical_collection → list[(tumbler, title, doc_id)]
    by_collection: dict[str, list[tuple[str, str, str]]] = {}
    for tumbler_str, title, coll, doc_id in rows:
        by_collection.setdefault(coll, []).append((tumbler_str, title, doc_id))

    total_tumblers = sum(len(v) for v in by_collection.values())
    if not json_out:
        click.echo(
            f"Verifying {total_tumblers} catalog tumbler(s) across "
            f"{len(by_collection)} collection(s)..."
        )

    t3 = _make_t3()
    ghosts_by_collection: dict[str, list[dict]] = {}
    for coll, tumblers in sorted(by_collection.items()):
        expected_ids = [doc_id for _, _, doc_id in tumblers]
        present = t3.existing_ids(coll, expected_ids)
        ghosts = [
            {"tumbler": t, "title": title, "doc_id": doc_id}
            for t, title, doc_id in tumblers
            if doc_id not in present
        ]
        if ghosts:
            ghosts_by_collection[coll] = ghosts

    if json_out:
        click.echo(_json.dumps(ghosts_by_collection, indent=2))
        return

    total_ghosts = sum(len(v) for v in ghosts_by_collection.values())
    if not ghosts_by_collection:
        click.echo(f"Summary: 0 ghosts / {total_tumblers} tumblers. All good.")
        return

    for coll, ghosts in sorted(ghosts_by_collection.items()):
        click.echo(f"  {coll}: {len(ghosts)} ghost(s) found")
        for g in ghosts:
            click.echo(f"    {g['tumbler']:<12} {g['title']}  (doc_id {g['doc_id']})")

    pct = (total_ghosts * 100.0) / max(total_tumblers, 1)
    click.echo(
        f"Summary: {total_ghosts} ghosts / {total_tumblers} tumblers ({pct:.1f}%)."
    )
    if not heal:
        click.echo("Run with --heal for remediation options.")
        return

    _heal_ghosts(cat, ghosts_by_collection)


def _heal_ghosts(
    cat: Catalog,
    ghosts_by_collection: dict[str, list[dict]],
) -> None:
    """Interactive heal loop for `nx catalog verify --heal`.

    Per ghost, prompt for one of:
      d  drop the tumbler (catalog.delete_document)
      p  print the `nx store put` invocation that would repopulate it
      s  skip
      q  quit the heal loop
    """
    dropped = 0
    for coll, ghosts in sorted(ghosts_by_collection.items()):
        click.echo(f"\nHealing {coll}:")
        for g in ghosts:
            click.echo(f"  {g['tumbler']} — {g['title']} (doc_id {g['doc_id']})")
            choice = click.prompt(
                "    [d]rop tumbler / [p]rint put cmd / [s]kip / [q]uit",
                default="s",
                show_default=False,
            ).strip().lower()
            if choice == "q":
                click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")
                return
            if choice == "d":
                if cat.delete_document(Tumbler.parse(g["tumbler"])):
                    dropped += 1
                    click.echo("    dropped.")
                else:
                    click.echo("    already gone.")
            elif choice == "p":
                # The put command needs the original content. We emit a
                # template so the user can paste their source material.
                click.echo(
                    f"    nx store put --collection {coll} "
                    f"--title {g['title']!r} < source.md"
                )
            # anything else = skip
    click.echo(f"\nHealed: {dropped} tumbler(s) dropped.")


@catalog.command("links-for-file")
@click.argument("file_path")
def links_for_file_cmd(file_path: str) -> None:
    """Show catalog entries linked to a specific file.

    \b
    Examples:
      nx catalog links-for-file src/nexus/catalog/catalog.py
      nx catalog links-for-file docs/rdr/rdr-060.md
    """
    cat = _get_catalog()
    db = cat._db

    row = db.execute(
        "SELECT tumbler, title, content_type FROM documents WHERE file_path = ?",
        (file_path,),
    ).fetchone()
    if not row:
        click.echo(f"No catalog entry for: {file_path}")
        return

    tumbler_str, title, content_type = row
    click.echo(f"{tumbler_str} {content_type}: {title}")

    link_rows = db.execute(
        """SELECT d.tumbler, d.title, d.content_type, l.link_type,
                  CASE WHEN l.from_tumbler = ? THEN 'outgoing' ELSE 'incoming' END as direction
           FROM links l
           JOIN documents d ON (d.tumbler = l.to_tumbler AND l.from_tumbler = ?)
                            OR (d.tumbler = l.from_tumbler AND l.to_tumbler = ?)
           ORDER BY l.link_type, d.content_type""",
        (tumbler_str, tumbler_str, tumbler_str),
    ).fetchall()

    if not link_rows:
        click.echo("  No links.")
        return

    for t, t_title, t_type, l_type, direction in link_rows:
        arrow = "→" if direction == "outgoing" else "←"
        click.echo(f"  {arrow} [{l_type}] {t} {t_type}: {t_title}")


@catalog.command("session-summary")
@click.option("--since", default=24, type=int, help="Hours to look back for git changes")
def session_summary_cmd(since: int) -> None:
    """Show link graph summary for recently modified files.

    \b
    Examples:
      nx catalog session-summary            # files modified in last 24 hours
      nx catalog session-summary --since 48 # last 48 hours
    """
    import subprocess

    try:
        cat = _get_catalog()
    except click.ClickException:
        click.echo("Catalog not initialized — skipping session summary.")
        return

    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--since={since} hours ago",
                "--name-only",
                "--pretty=format:",
                "--diff-filter=ACMR",
            ],
            capture_output=True, text=True, timeout=5,
        )
        files = {f.strip() for f in result.stdout.splitlines() if f.strip()}
    except Exception:
        click.echo("Could not determine recent file changes.")
        return

    if not files:
        click.echo(f"No files modified in the last {since} hours.")
    else:
        db = cat._db
        found_any = False
        for fp in sorted(files):
            row = db.execute(
                "SELECT tumbler FROM documents WHERE file_path = ?", (fp,)
            ).fetchone()
            if not row:
                continue
            tumbler_str = row[0]
            link_rows = db.execute(
                """SELECT DISTINCT d.title FROM links l
                   JOIN documents d ON (d.tumbler = l.to_tumbler AND l.from_tumbler = ?)
                                    OR (d.tumbler = l.from_tumbler AND l.to_tumbler = ?)
                   WHERE d.content_type = 'rdr'""",
                (tumbler_str, tumbler_str),
            ).fetchall()
            if link_rows:
                rdrs = ", ".join(r[0] for r in link_rows)
                click.echo(f"  {fp} — {len(link_rows)} RDR(s): {rdrs}")
                found_any = True

        if not found_any:
            click.echo("No linked RDRs found for recently modified files.")

    total = cat._db.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    click.echo(f"\nLink graph: {total} links active.")


@catalog.command("gc")
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete catalog rows.",
)
def gc_cmd(dry_run: bool, confirm: bool) -> None:
    """Remove orphan catalog entries that have miss_count >= 2.

    \b
    Orphans are entries that were absent in two or more consecutive index runs.
    Default is read-only (--dry-run is on). To actually delete:
      nx catalog gc --no-dry-run --confirm

    \b
    Examples:
      nx catalog gc                          # report (read-only)
      nx catalog gc --no-dry-run --confirm  # actually delete

    nexus-tnz3: 4.29.1 inverted the default from "delete unless --dry-run"
    to "report unless --no-dry-run --confirm" so a forgotten flag no longer
    silently destroys orphan entries.
    """
    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete catalog rows."
        )

    cat = _get_catalog()

    rows = cat._db.execute(
        "SELECT tumbler, title, file_path, metadata FROM documents"
    ).fetchall()

    orphans: list[tuple[str, str, str]] = []
    for tumbler_str, title, file_path, meta_json in rows:
        meta = json.loads(meta_json) if meta_json else {}
        if int(meta.get("miss_count", 0)) >= 2:
            orphans.append((tumbler_str, title or "", file_path or ""))

    if not orphans:
        click.echo("No orphan entries found.")
        return

    click.echo(
        f"Found {len(orphans)} orphan "
        f"{'entry' if len(orphans) == 1 else 'entries'} (miss_count >= 2):"
    )
    for tumbler_str, title, file_path in orphans[:20]:
        loc = f" ({file_path})" if file_path else ""
        click.echo(f"  {tumbler_str}: {title}{loc}")
    if len(orphans) > 20:
        click.echo(f"  ... ({len(orphans) - 20} more)")

    if not will_delete:
        click.echo(
            f"\n{len(orphans)} {'entry' if len(orphans) == 1 else 'entries'} "
            f"would be deleted. Run with --no-dry-run --confirm to apply."
        )
        return

    # Backup before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_documents
    backup_path = snapshot_documents(
        cat,
        [t for t, _, _ in orphans],
        verb="gc",
        reason="miss_count >= 2",
    )
    if backup_path:
        click.echo(
            f"\nBackup snapshot written: {backup_path}"
            f"\n  Restore with: nx catalog undelete {backup_path.name}"
        )

    n_deleted = 0
    for tumbler_str, title, file_path in orphans:
        if cat.delete_document(Tumbler.parse(tumbler_str)):
            n_deleted += 1

    click.echo(
        f"\nDeleted {n_deleted} orphan "
        f"{'entry' if n_deleted == 1 else 'entries'}."
    )


@catalog.command("coverage")
@click.option("--owner", "owner_prefix", default="", help="Filter by tumbler prefix (e.g. '1.1')")
def coverage_cmd(owner_prefix: str) -> None:
    """Show what percentage of catalog entries have at least one link, by content type.

    \b
    Examples:
      nx catalog coverage                # all types
      nx catalog coverage --owner 1.1   # only entries under owner 1.1
    """
    cat = _get_catalog()
    db = cat._db

    # Fetch all distinct content types under filter
    if owner_prefix:
        like_pat = owner_prefix.rstrip(".") + ".%"
        type_rows = db.execute(
            "SELECT DISTINCT content_type FROM documents WHERE tumbler LIKE ? OR tumbler = ?",
            (like_pat, owner_prefix),
        ).fetchall()
    else:
        type_rows = db.execute("SELECT DISTINCT content_type FROM documents").fetchall()

    content_types = [r[0] for r in type_rows]
    if not content_types:
        click.echo("No documents in catalog.")
        return

    click.echo("Link coverage by content type:")
    for ct in sorted(content_types):
        if owner_prefix:
            like_pat = owner_prefix.rstrip(".") + ".%"
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE content_type = ? AND (tumbler LIKE ? OR tumbler = ?)",
                (ct, like_pat, owner_prefix),
            ).fetchone()[0]
            linked = db.execute(
                """
                SELECT COUNT(DISTINCT d.tumbler)
                FROM documents d
                JOIN links l ON d.tumbler = l.from_tumbler OR d.tumbler = l.to_tumbler
                WHERE d.content_type = ?
                  AND (d.tumbler LIKE ? OR d.tumbler = ?)
                """,
                (ct, like_pat, owner_prefix),
            ).fetchone()[0]
        else:
            total = db.execute(
                "SELECT COUNT(*) FROM documents WHERE content_type = ?",
                (ct,),
            ).fetchone()[0]
            linked = db.execute(
                """
                SELECT COUNT(DISTINCT d.tumbler)
                FROM documents d
                JOIN links l ON d.tumbler = l.from_tumbler OR d.tumbler = l.to_tumbler
                WHERE d.content_type = ?
                """,
                (ct,),
            ).fetchone()[0]

        pct = (linked / total * 100) if total else 0.0
        click.echo(f"  {ct:<12} {linked:>4}/{total:<4} = {pct:5.1f}%")


@catalog.command("link-density")
@click.option(
    "--by-collection/--no-by-collection",
    "by_collection",
    default=True,
    help="Aggregate by physical_collection (default).",
)
@click.option(
    "--sample",
    default=50,
    type=int,
    help="Max seeds per collection to BFS (capped to keep latency bounded).",
)
@click.option("--depth", default=2, type=int, help="BFS depth (default 2).")
@click.option(
    "--threshold",
    default=3,
    type=int,
    help="Frontier-p50 below this flags the collection as low-density.",
)
def link_density_cmd(
    by_collection: bool, sample: int, depth: int, threshold: int
) -> None:
    """Measure catalog link-graph density per collection.

    For each ``physical_collection``, samples up to ``--sample`` seed
    tumblers, runs a depth-``--depth`` BFS from each, and reports the
    frontier-size distribution (p50, p90) plus the link types that
    fired during traversal.

    \b
    Use this before deciding whether a hybrid retrieval plan
    (RDR-097) makes sense for a given collection. A collection with
    median frontier <``--threshold`` nodes is flagged as a poor
    candidate — graph traversal will add latency with little recall
    gain. Vector-only retrieval is the better choice there.

    \b
    The frontier count for one seed is the number of nodes reachable
    within ``--depth`` hops, excluding the seed itself.
    """
    import statistics  # noqa: PLC0415

    cat = _get_catalog()
    db = cat._db

    # By design the bead specifies physical_collection grouping; the
    # ``--no-by-collection`` flag is a placeholder for a future
    # global-density rollup so the option signature stays stable.
    if not by_collection:
        click.echo("Global rollup not yet implemented — use --by-collection.")
        return

    rows = db.execute(
        "SELECT physical_collection, COUNT(*) FROM documents "
        "WHERE physical_collection IS NOT NULL "
        "  AND physical_collection != '' "
        "GROUP BY physical_collection "
        "ORDER BY physical_collection"
    ).fetchall()

    if not rows:
        click.echo("No collections registered in catalog.")
        return

    click.echo(
        f"Link-graph density (depth={depth}, sample={sample} per collection):"
    )
    header = (
        f"  {'collection':<40} {'seeds':>5} {'p50':>5} {'p90':>5}  "
        f"{'flag':<8}  link_types"
    )
    click.echo(header)
    click.echo(f"  {'-' * 38:<40} {'-' * 5:>5} {'-' * 5:>5} {'-' * 5:>5}  "
               f"{'-' * 6:<8}  ----------")

    for coll, total in rows:
        seed_rows = db.execute(
            "SELECT tumbler FROM documents "
            "WHERE physical_collection = ? "
            "LIMIT ?",
            (coll, sample),
        ).fetchall()

        seeds: list[Tumbler] = []
        for r in seed_rows:
            try:
                seeds.append(Tumbler.parse(r[0]))
            except Exception:
                continue

        if not seeds:
            click.echo(
                f"  {coll:<40} {0:>5} {0:>5} {0:>5}  "
                f"{'no-seed':<8}  -"
            )
            continue

        frontier_counts: list[int] = []
        link_types_seen: set[str] = set()
        for seed in seeds:
            try:
                result = cat.graph(seed, depth=depth, direction="both")
            except Exception:
                continue
            nodes = result.get("nodes") or []
            edges = result.get("edges") or []
            frontier_counts.append(max(0, len(nodes) - 1))
            for e in edges:
                if getattr(e, "link_type", None):
                    link_types_seen.add(e.link_type)

        if not frontier_counts:
            click.echo(
                f"  {coll:<40} {len(seeds):>5} {0:>5} {0:>5}  "
                f"{'bfs-err':<8}  -"
            )
            continue

        sorted_counts = sorted(frontier_counts)
        p50_val = statistics.median(sorted_counts)
        # Positional p90: floor(0.9 * (n-1)). For n=1, that's index 0.
        p90_idx = max(0, int(0.9 * (len(sorted_counts) - 1) + 0.5))
        p90_val = sorted_counts[p90_idx]

        flag = "low" if p50_val < threshold else "ok"
        types_str = ",".join(sorted(link_types_seen)) or "-"
        click.echo(
            f"  {coll:<40} {len(seeds):>5} {p50_val:>5.1f} {p90_val:>5}  "
            f"{flag:<8}  {types_str}"
        )

    click.echo()
    click.echo(
        f"Flag legend: 'low' = frontier-p50 < {threshold} (consider "
        f"vector-only retrieval); 'ok' = sufficient density for hybrid."
    )


@catalog.command("suggest-links")
@click.option("--limit", "-n", default=50, type=int, help="Max suggestions to show")
@click.option("--threshold", default=0.0, type=float, help="Reserved for future similarity threshold (unused)")
def suggest_links_cmd(limit: int, threshold: float) -> None:
    """Suggest unlinked code-RDR pairs by module-name overlap.

    Finds code entries whose filename stem appears in an RDR title, where no
    link yet exists between the pair. Same heuristic as 'generate-links' but
    read-only — shows what would be created.

    \b
    Examples:
      nx catalog suggest-links
      nx catalog suggest-links --limit 20
    """
    from pathlib import Path as _Path

    cat = _get_catalog()
    entries = cat.all_documents(limit=10_000)
    rdr_entries = [e for e in entries if e.content_type == "rdr"]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]

    if not rdr_entries or not code_entries:
        click.echo("0 suggestions (no code or RDR entries to match).")
        return

    # Pre-normalize RDR titles
    rdr_normalized = [
        (rdr, rdr.title.lower().replace("-", "").replace(" ", "").replace("_", ""))
        for rdr in rdr_entries
    ]

    suggestions: list[tuple[str, str, str, str]] = []  # (code_t, rdr_t, module, rdr_title)
    for code in code_entries:
        module_name = _Path(code.file_path).stem.replace("_", "").lower()
        if len(module_name) <= 3:
            continue
        for rdr, rdr_title_norm in rdr_normalized:
            if module_name not in rdr_title_norm:
                continue
            # Check if link already exists in either direction
            existing = cat.link_query(
                from_t=str(code.tumbler), to_t=str(rdr.tumbler),
                link_type="", limit=1,
            ) or cat.link_query(
                from_t=str(rdr.tumbler), to_t=str(code.tumbler),
                link_type="", limit=1,
            )
            if not existing:
                suggestions.append((
                    str(code.tumbler), str(rdr.tumbler),
                    module_name, rdr.title,
                ))
        if len(suggestions) >= limit:
            break

    suggestions = suggestions[:limit]
    if not suggestions:
        click.echo("0 suggestions (all matching pairs are already linked).")
        return

    click.echo(f"{len(suggestions)} suggestion(s):")
    for code_t, rdr_t, module, rdr_title in suggestions:
        click.echo(f"  {code_t} → {rdr_t}  [{module}] {rdr_title}")


# ── Backfill helpers ──────────────────────────────────────────────────────────


def _owner_by_name(cat: Catalog, name: str) -> Tumbler | None:
    """Look up a CURATOR owner by name.

    Filters on ``owner_type = 'curator'`` so a same-named REPO owner
    (e.g. a repo whose root path basename happens to be ``knowledge``
    or ``papers``) cannot silently shadow the intended curator. The
    namespaces are separate; repo owners are reachable only via
    ``Catalog.owner_for_repo(repo_hash)``.
    """
    row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = ? "
        "AND owner_type = 'curator'",
        (name,),
    ).fetchone()
    return Tumbler.parse(row[0]) if row else None


def _get_or_create_curator(cat: Catalog, name: str) -> Tumbler:
    """Get or create a curator owner by name."""
    owner = _owner_by_name(cat, name)
    if owner is None:
        owner = cat.register_owner(name, "curator")
    return owner


def _backfill_repos(
    cat: Catalog, registry: object, dry_run: bool
) -> tuple[int, set[str]]:
    """Create owner per repo from registry.

    Returns (count, claimed_collections) — claimed_collections is the set of
    docs__* collection names owned by repos, so Pass 2 can exclude them.
    """
    from hashlib import sha256
    from pathlib import Path

    count = 0
    claimed: set[str] = set()
    skipped = 0

    # First pass: collect ALL repo-owned collections regardless of status
    # so Pass 2 never mistakes repo prose for standalone papers
    for info in registry.all_info().values():
        for key in ("code_collection", "docs_collection", "collection"):
            col = info.get(key, "")
            if col:
                claimed.add(col)

    # Second pass: register only healthy repos
    for repo_path_str, info in registry.all_info().items():
        repo_path = Path(repo_path_str)
        status = info.get("status", "")

        if status not in ("ready", "indexing"):
            skipped += 1
            continue
        if not repo_path.exists():
            skipped += 1
            continue

        repo_name = info.get("name", repo_path.name)
        path_hash = sha256(str(repo_path).encode()).hexdigest()[:8]
        code_col = info.get("code_collection", "")
        docs_col = info.get("docs_collection", "")
        head_hash = info.get("head_hash", "")

        if dry_run:
            click.echo(f"  [dry-run] Would register owner: {repo_name} ({path_hash})")
            if code_col:
                click.echo(f"  [dry-run]   code: {code_col}")
                count += 1
            if docs_col:
                click.echo(f"  [dry-run]   docs: {docs_col}")
                count += 1
            continue

        owner = cat.owner_for_repo(path_hash)
        if owner is None:
            owner = cat.register_owner(
                repo_name, "repo", repo_hash=path_hash,
                repo_root=str(repo_path),
                description=f"Git repository: {repo_name}",
            )

        for col_name, content_type in [(code_col, "code"), (docs_col, "prose")]:
            if not col_name:
                continue
            existing = [
                e for e in cat.by_owner(owner) if e.physical_collection == col_name
            ]
            if not existing:
                cat.register(
                    owner=owner, title=f"{repo_name} ({content_type})",
                    content_type=content_type,
                    physical_collection=col_name,
                    head_hash=head_hash,
                )
                count += 1

    if skipped:
        click.echo(f"  ({skipped} stale/missing repos skipped)")
    return count, claimed


def _backfill_knowledge(cat: Catalog, t3: object, dry_run: bool) -> int:
    """Register knowledge__* collections in catalog."""
    collections = t3.list_collections()
    knowledge_cols = [c for c in collections if c["name"].startswith("knowledge__")]
    count = 0
    total = len(knowledge_cols)

    for i, col_info in enumerate(knowledge_cols, 1):
        col_name = col_info["name"]
        # Derive a title from the collection name
        title = col_name.replace("knowledge__", "").replace("_", " ").title()

        if dry_run:
            click.echo(f"  [dry-run] Would register knowledge: {title} → {col_name}")
            count += 1
            continue

        curator = _get_or_create_curator(cat, "knowledge")
        # Idempotent: check by physical_collection
        existing = [e for e in cat.by_owner(curator) if e.physical_collection == col_name]
        if not existing:
            cat.register(
                owner=curator, title=title, content_type="knowledge",
                physical_collection=col_name,
            )
        count += 1

    return count


def _backfill_rdrs(cat: Catalog, t3: object, dry_run: bool) -> int:
    """Register rdr__* collections in catalog with per-document titles from T3 metadata."""
    collections = t3.list_collections()
    rdr_cols = [c for c in collections if c["name"].startswith("rdr__") and c["count"] > 0]
    count = 0

    for col_info in rdr_cols:
        col_name = col_info["name"]

        try:
            col = t3.get_or_create_collection(col_name)
            # Paginate to discover ALL unique documents.
            # nexus-7b5n: when chunks carry doc_id (post t3-backfill-doc-id),
            # dedup on doc_id so two entries with the same source_path under
            # different owners stay distinct. Falls back to source_path for
            # collections that haven't been backfilled yet (legacy walks).
            seen_paths: dict[str, str] = {}  # path → title
            seen_doc_ids: set[str] = set()
            use_doc_id: bool | None = None
            offset = 0
            while True:
                result = col.get(include=["metadatas"], limit=200, offset=offset)
                metas = result.get("metadatas", [])
                if metas and use_doc_id is None:
                    use_doc_id = any(
                        isinstance(m, dict) and m.get("doc_id", "")
                        for m in metas
                    )
                for meta in metas:
                    if not isinstance(meta, dict):
                        continue
                    path = meta.get("source_path", "")
                    if not path:
                        continue
                    if use_doc_id:
                        did = meta.get("doc_id", "")
                        # Chunks predating t3-backfill-doc-id have empty
                        # doc_id; fall through to source_path dedup so they
                        # still land in the catalog.
                        if did and did in seen_doc_ids:
                            continue
                        if did:
                            seen_doc_ids.add(did)
                    if path in seen_paths:
                        continue
                    title = meta.get("title", "") or Path(path).stem
                    seen_paths[path] = title
                if len(metas) < 200:
                    break
                offset += 200

            # nexus-3e4s S1: prefer the repo owner whose hash matches
            # the collection suffix, so backfill goes through the same
            # register-time guard as live indexing. Fall back to a
            # curator only when no registered repo owns this collection.
            # Pre-fix this branch unconditionally created a curator,
            # which made the guard skip and let backfill silently
            # re-introduce the contamination class on disaster recovery.
            repo_root: Path | None = None
            owner: Tumbler | None = None
            try:
                import hashlib

                from nexus.catalog.catalog import (
                    _default_registry_path,
                    make_relative,
                )
                from nexus.registry import RepoRegistry

                reg_path = _default_registry_path()
                if reg_path.exists():
                    for repo_path_str in RepoRegistry(reg_path).all_info():
                        h = hashlib.sha256(
                            repo_path_str.encode(),
                        ).hexdigest()[:8]
                        if col_name.endswith(h):
                            repo_root = Path(repo_path_str)
                            owner = cat.owner_for_repo(h)
                            break
            except Exception:
                _log.warning(
                    "backfill_rdrs_repo_lookup_failed",
                    col=col_name, exc_info=True,
                )

            if owner is None:
                # Either no registry, no matching repo, or the owner
                # has not yet been registered (backfill_repos runs first
                # but the registry may have stale entries). Curator is
                # the legitimate fallback for orphan rdr__* collections.
                owner = _get_or_create_curator(
                    cat, col_name.replace("rdr__", ""),
                )

            for path, title in seen_paths.items():
                if dry_run:
                    click.echo(f"  [dry-run] {title} → {col_name}")
                    count += 1
                    continue
                fp = make_relative(path, repo_root) if repo_root else path
                existing = [
                    e for e in cat.by_owner(owner)
                    if e.file_path in (path, fp)
                ]
                if not existing:
                    cat.register(
                        owner=owner, title=title, content_type="rdr",
                        file_path=fp, physical_collection=col_name,
                    )
                    count += 1
        except Exception as exc:
            click.echo(f"  warning: {col_name} — {exc}")
            _log.debug("backfill_rdrs_error", col=col_name, exc_info=True)

    return count


def _backfill_papers(
    cat: Catalog, t3: object, dry_run: bool, repo_collections: set[str] | None = None,
) -> int:
    """Register docs__* paper collections, excluding repo-owned collections."""
    collections = t3.list_collections()
    repo_cols = repo_collections or set()
    paper_cols = [
        c for c in collections
        if c["name"].startswith("docs__")
        and c["count"] > 0
        and c["name"] not in repo_cols
    ]
    count = 0

    total = len(paper_cols)
    for i, col_info in enumerate(paper_cols, 1):
        col_name = col_info["name"]

        # Try to extract metadata from first chunk (cloud call — may be slow)
        title = col_name.replace("docs__", "")
        author = ""
        year = 0
        try:
            col = t3.get_or_create_collection(col_name)
            result = col.get(limit=1, include=["metadatas"])
            if result.get("ids") and result.get("metadatas"):
                meta = result["metadatas"][0]
                title = meta.get("title", "") or title
                author = meta.get("bib_authors", "") or meta.get("author", "")
                year = int(meta.get("bib_year", 0) or 0)
        except Exception:
            _log.debug("backfill_papers_metadata_error", col=col_name, exc_info=True)

        if dry_run:
            click.echo(f"  [dry-run] Would register paper: {title} → {col_name}")
            count += 1
            continue

        curator = _get_or_create_curator(cat, "papers")
        existing = [e for e in cat.by_owner(curator) if e.physical_collection == col_name]
        if not existing:
            cat.register(
                owner=curator, title=title, content_type="paper",
                author=author, year=year,
                physical_collection=col_name,
            )
        count += 1
        # Progress — papers are the slow path (one cloud call per collection)
        if total > 5 and i % 10 == 0:
            click.echo(f"  [{i}/{total}] {title[:50]}")

    return count


@catalog.command("consolidate", hidden=True)
@click.argument("corpus")
@click.option("--dry-run", is_flag=True, help="Show what would be merged without writing")
def consolidate_cmd(corpus: str, dry_run: bool) -> None:
    """Merge per-paper collections into a corpus-level collection."""
    cat = _get_catalog()
    from nexus.catalog.consolidation import merge_corpus

    if dry_run:
        result = merge_corpus(cat, None, corpus, dry_run=True)
        entries = cat.by_corpus(corpus)
        if not entries:
            raise click.ClickException(f"No entries with corpus={corpus!r}")
        # RDR-103 Phase 5: mirror the conformant target shape that
        # ``merge_corpus`` will use when run for real so the dry-run
        # message reports the same name.
        from nexus.corpus import canonical_embedding_model  # noqa: PLC0415

        owner_segment = corpus.replace("_", "-")
        target = (
            f"docs__{owner_segment}__{canonical_embedding_model('docs')}__v1"
        )
        click.echo(f"[dry-run] Would merge {result['would_merge']} collections into {target}:")
        for e in entries:
            click.echo(f"  {e.physical_collection} ({e.chunk_count} chunks) → {target}")
        return

    t3 = _make_t3()
    result = merge_corpus(cat, t3, corpus)

    if result["errors"]:
        for err in result["errors"]:
            click.echo(f"  ERROR: {err}", err=True)
    click.echo(f"Consolidation complete: {result['merged']} merged, {len(result['errors'])} errors")


@catalog.command("generate-links")
@click.option("--citations/--no-citations", default=True, help="Generate citation links from bib metadata")
@click.option("--filepath/--no-filepath", default=True, help="Generate RDR filepath links")
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
def generate_links_cmd(citations: bool, filepath: bool, dry_run: bool) -> None:
    """Auto-generate typed links from metadata cross-matching."""
    cat = _get_catalog()
    from nexus.catalog.link_generator import (
        generate_citation_links,
        generate_rdr_filepath_links,
    )

    total = 0
    if citations:
        if dry_run:
            click.echo("Would generate citation links (dry-run mode not yet supported for link preview)")
        else:
            count = generate_citation_links(cat)
            click.echo(f"Citation links created: {count}")
            total += count

    if filepath:
        if dry_run:
            click.echo("Would generate RDR filepath links (dry-run mode not yet supported for link preview)")
        else:
            count = generate_rdr_filepath_links(cat)
            click.echo(f"RDR filepath links created: {count}")
            total += count

    if not dry_run:
        click.echo(f"Total links generated: {total}")


@catalog.command("link-generate", hidden=True)
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be done without writing")
@click.pass_context
def link_generate_cmd(ctx: click.Context, dry_run: bool) -> None:
    """Deprecated alias for ``nx catalog generate-links`` (nexus-2297).

    Pre-fix this verb generated only the RDR filepath links and emitted
    a different message. To converge on the verb-noun convention used
    by ``nx catalog link`` / ``unlink`` / ``links``, the canonical name
    is now ``generate-links`` -- this alias emits a deprecation warning
    and delegates to it. The alias will be removed in a future release.
    """
    click.echo(
        "WARNING: 'nx catalog link-generate' is deprecated and will be "
        "removed in a future release. Use 'nx catalog generate-links' "
        "instead.",
        err=True,
    )
    # Preserve the historical behaviour: filepath links only,
    # no citations. Operators who want both should switch to the
    # canonical ``generate-links`` verb.
    ctx.invoke(
        generate_links_cmd,
        citations=False,
        filepath=True,
        dry_run=dry_run,
    )


def _make_t3():
    from nexus.db import make_t3
    return make_t3()


def _make_registry():
    from nexus.config import nexus_config_dir
    from nexus.registry import RepoRegistry

    return RepoRegistry(nexus_config_dir() / "repos.json")


def _backfill_per_file_from_t3(
    cat: Catalog,
    t3: object,
    collection: str,
    *,
    dry_run: bool,
) -> int:
    """nexus-p03z Issue 2: per-file recovery from T3 chunk metadata.

    For ``docs__<repo>`` and ``code__<repo>`` collections, reconstruct
    one catalog row per unique ``source_path`` discovered in T3 chunk
    metadata. The repo owner is resolved via the ``-<8hex>`` suffix in
    the collection name; an unregistered repo raises so the operator
    knows to register the owner first (or run the standard ``backfill``
    repos pass).

    Idempotent: ``cat.register`` deduplicates on ``(owner, file_path)``,
    so re-running registers nothing new. The register-time anchor guard
    (nexus-3e4s) automatically rejects chunks whose ``source_path``
    lives outside the owner's ``repo_root`` — wrong-project chunks that
    leaked into this collection won't recreate the contamination.

    Returns the number of newly-registered catalog rows. ``dry_run`` skips
    writes and returns the count that *would* register (subject to the
    same dedup logic against existing rows).
    """
    # Parse the trailing -<8hex> as the repo hash. ``rsplit`` keeps
    # repo names containing dashes intact (e.g. ``code__nexus-mini0-4ada4577``).
    # Splitting on `__` first removes the prefix, then `-` splits name
    # vs hash.
    if "__" not in collection:
        raise click.ClickException(
            f"collection {collection!r} has no double-underscore prefix; "
            "per-file recovery only supports docs__<repo>-<hash> and "
            "code__<repo>-<hash> shapes."
        )
    suffix = collection.split("__", 1)[1]
    if "-" not in suffix:
        raise click.ClickException(
            f"collection {collection!r} suffix {suffix!r} has no -<hash> tail; "
            "per-file recovery requires a repo-owned collection."
        )
    _, repo_hash = suffix.rsplit("-", 1)
    if len(repo_hash) != 8 or not all(c in "0123456789abcdef" for c in repo_hash):
        raise click.ClickException(
            f"collection {collection!r} suffix has malformed repo hash "
            f"{repo_hash!r}; expected 8 hex chars."
        )

    owner = cat.owner_for_repo(repo_hash)
    if owner is None:
        raise click.ClickException(
            f"no repo owner registered for hash {repo_hash!r} "
            f"(collection {collection!r}). Run 'nx catalog backfill' "
            "first to register repo owners, or 'nx repo register'."
        )

    # Look up the owner's repo_root so we can anchor file_paths
    # relative to it (matches the post-RDR-060 catalog convention).
    repo_root_row = cat._db.execute(
        "SELECT repo_root FROM owners WHERE tumbler_prefix = ?",
        (str(owner),),
    ).fetchone()
    repo_root = (repo_root_row[0] or "") if repo_root_row else ""

    # Determine content_type from prefix.
    if collection.startswith("code__"):
        content_type = "code"
    elif collection.startswith("docs__"):
        content_type = "prose"
    else:
        raise click.ClickException(
            f"collection {collection!r} prefix is not docs__ or code__; "
            "per-file recovery only supports those two."
        )

    # Paginate T3 chunks at 300 (Cloud cap) and dedupe by source_path.
    # nexus-7b5n: when chunks carry doc_id (post t3-backfill-doc-id),
    # also dedup on doc_id so two distinct documents that happen to
    # share a source_path stay distinct. Empty doc_id falls back to
    # source_path-only dedup for legacy chunks predating the backfill.
    col = t3.get_collection(collection)
    seen_paths: set[str] = set()
    seen_doc_ids: set[str] = set()
    use_doc_id: bool | None = None
    offset = 0
    page_size = 300
    while True:
        page = col.get(
            include=["metadatas"], limit=page_size, offset=offset,
        )
        ids = page.get("ids") or []
        if not ids:
            break
        metas = page.get("metadatas") or []
        if use_doc_id is None and metas:
            use_doc_id = any(
                isinstance(m, dict) and m.get("doc_id", "")
                for m in metas
            )
        for meta in metas:
            if not isinstance(meta, dict):
                continue
            sp = meta.get("source_path", "")
            if not sp:
                continue
            if use_doc_id:
                did = meta.get("doc_id", "")
                if did and did in seen_doc_ids:
                    continue
                if did:
                    seen_doc_ids.add(did)
            seen_paths.add(sp)
        if len(ids) < page_size:
            break
        offset += page_size

    registered = 0
    for abs_path in sorted(seen_paths):
        # Anchor relative to repo_root when possible; fall back to the
        # raw path. The register-time guard rejects paths outside
        # repo_root regardless.
        if repo_root and abs_path.startswith(repo_root + "/"):
            rel = abs_path[len(repo_root) + 1:]
        else:
            rel = abs_path

        if dry_run:
            registered += 1
            continue

        existing = cat.by_file_path(owner, rel)
        if existing is not None:
            continue
        try:
            cat.register(
                owner=owner,
                title=Path(rel).name or rel,
                content_type=content_type,
                physical_collection=collection,
                file_path=rel,
            )
            registered += 1
        except ValueError as exc:
            # Cross-project anchor rejection from nexus-3e4s: the chunk
            # claims to live outside this repo. Skip and log; this is
            # exactly the contamination the guard exists to prevent.
            _log.warning(
                "backfill_from_t3_anchor_rejected",
                collection=collection,
                file_path=rel,
                error=str(exc),
            )

    return registered


@catalog.command("backfill", hidden=True)
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
@click.option(
    "--from-t3",
    "from_t3",
    is_flag=True,
    help=(
        "Per-file recovery mode: enumerate T3 chunks and register one "
        "catalog row per unique source_path under the repo owner. "
        "Skips the standard 4-pass backfill. Requires --collection or "
        "--all-repo-collections."
    ),
)
@click.option(
    "--collection",
    "from_t3_collection",
    default="",
    help=(
        "Single collection to recover from T3 chunks (used with "
        "--from-t3). Must be a docs__<repo>-<hash> or "
        "code__<repo>-<hash> collection whose repo owner is registered."
    ),
)
@click.option(
    "--all-repo-collections",
    "from_t3_all",
    is_flag=True,
    help=(
        "Run --from-t3 recovery against every docs__<repo>-<hash> and "
        "code__<repo>-<hash> collection in T3. Mutually exclusive with "
        "--collection."
    ),
)
def backfill_cmd(
    dry_run: bool,
    from_t3: bool,
    from_t3_collection: str,
    from_t3_all: bool,
) -> None:
    """Populate catalog from existing T3 collections and registry."""
    cat = _get_catalog()

    if from_t3:
        if from_t3_collection and from_t3_all:
            raise click.UsageError(
                "--collection and --all-repo-collections are mutually exclusive."
            )
        if not from_t3_collection and not from_t3_all:
            raise click.UsageError(
                "--from-t3 requires either --collection <NAME> or "
                "--all-repo-collections."
            )
        t3 = _make_t3()
        if from_t3_collection:
            targets = [from_t3_collection]
        else:
            targets = [
                c["name"] for c in t3.list_collections()
                if (c["name"].startswith("docs__") or c["name"].startswith("code__"))
                and "-" in c["name"].split("__", 1)[1]
            ]
        total_registered = 0
        for target in targets:
            try:
                count = _backfill_per_file_from_t3(
                    cat, t3, target, dry_run=dry_run,
                )
                mode = "would register" if dry_run else "registered"
                click.echo(f"  {target}: {mode} {count} row(s)")
                total_registered += count
            except click.ClickException as exc:
                # Non-repo-owned collection in --all sweep: skip with a note.
                if from_t3_all:
                    click.echo(f"  {target}: skipped ({exc.message})")
                    continue
                raise
        mode = "dry-run" if dry_run else "complete"
        click.echo(f"\nFrom-T3 recovery {mode}: {total_registered} row(s).")
        return

    registry = _make_registry()
    t3 = _make_t3()

    click.echo("Pass 1: Repos...")
    repo_count, repo_collections = _backfill_repos(cat, registry, dry_run)

    click.echo("Pass 2: Paper collections (docs__*)...")
    paper_count = _backfill_papers(cat, t3, dry_run, repo_collections=repo_collections)

    click.echo("Pass 3: Knowledge collections...")
    knowledge_count = _backfill_knowledge(cat, t3, dry_run)

    hash_updated = 0
    if not dry_run:
        click.echo("Pass 4: chunk_text_hash backfill...")
        from nexus.commands.collection import _backfill_chunk_text_hash
        for col_info in t3.list_collections():
            col = t3._client.get_collection(col_info["name"])
            updated, _, _ = _backfill_chunk_text_hash(col)
            hash_updated += updated

    mode = "dry-run" if dry_run else "registered"
    click.echo(f"\nBackfill complete ({mode}):")
    click.echo(f"  Repos:     {repo_count}")
    click.echo(f"  Papers:    {paper_count}")
    click.echo(f"  Knowledge: {knowledge_count}")
    if not dry_run:
        click.echo(f"  Hash:      {hash_updated} chunks updated")


# ── nx catalog remediate-paths ──────────────────────────────────────────────


# Default file extensions the remediator considers candidates. Mirrors the
# set of types the catalog tracks: PDFs (papers / docs__), markdown (RDR /
# docs__ prose). Code files are excluded by design — code ingest stores
# absolute paths from a registered repo root, not loose basenames.
_REMEDIATE_DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    ".pdf", ".md", ".markdown",
})


def _build_basename_index(
    source_dir: Path,
    extensions: frozenset[str] | None = _REMEDIATE_DEFAULT_EXTENSIONS,
) -> dict[str, list[Path]]:
    """Walk *source_dir* and return ``{basename: [absolute_path, ...]}``.

    Symlinks are followed; hidden directories (``.git``, ``.venv``) are
    pruned because they don't carry curated source documents and they
    would dominate the walk on large repos. ``extensions=None`` matches
    every file regardless of suffix (used by ``--extensions *``).
    """
    import os as _os
    index: dict[str, list[Path]] = {}
    for root, dirs, files in _os.walk(
        str(source_dir.resolve()), followlinks=True,
    ):
        # Prune hidden dirs in-place so os.walk doesn't descend into them.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        root_path = Path(root)
        for fname in files:
            if extensions is not None and Path(fname).suffix.lower() not in extensions:
                continue
            index.setdefault(fname, []).append(root_path / fname)
    return index


def _entry_needs_remediation(entry: object) -> tuple[bool, str]:
    """Return ``(needs_fix, reason)`` for a catalog entry.

    Reasons:
    * ``"basename"`` — file_path has no slash; resolves against cwd.
    * ``"missing"`` — file_path is absolute but does not exist on disk.
    * ``""`` — file_path is fine.

    Empty file_path entries (MCP-stored knowledge with no source file)
    are not remediable here — they return ``(False, "no-file-path")``.
    """
    fp = getattr(entry, "file_path", "") or ""
    if not fp:
        return (False, "no-file-path")
    if "/" not in fp:
        return (True, "basename")
    if not Path(fp).exists():
        return (True, "missing")
    return (False, "")


def _resolve_via_devonthink(entry: object) -> Path | None:
    """If ``entry.meta`` carries a ``devonthink_uri``, ask DEVONthink for
    the current filesystem path and return it when the file exists on
    disk. Returns ``None`` when no DT URI is recorded, when the platform
    isn't macOS, when osascript fails, or when DT reports a path that
    doesn't actually exist (a sign the resolver returned a stale cache).

    This is the companion path to making ``x-devonthink-item://`` a
    canonical source URI (nexus-bqda): even with a ``file://`` source URI
    we can still recover from DT relocations using the meta we already
    record on entries that came in via DEVONthink.
    """
    import sys  # noqa: PLC0415

    if sys.platform != "darwin":
        return None
    meta = getattr(entry, "meta", {}) or {}
    dt_uri = meta.get("devonthink_uri", "") if isinstance(meta, dict) else ""
    if not dt_uri or not dt_uri.startswith("x-devonthink-item://"):
        return None
    uuid = dt_uri[len("x-devonthink-item://"):]
    if not uuid:
        return None
    from nexus.aspect_readers import _devonthink_resolver_default  # noqa: PLC0415
    path, _detail = _devonthink_resolver_default(uuid)
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p


def _resolve_candidate(
    entry: object,
    candidates: list[Path],
    *,
    prefer_deepest: bool = False,
) -> tuple[Path | None, str]:
    """Pick a single candidate path for *entry*, or ``None``.

    Returns ``(path, note)`` where *note* explains the choice:
      * ``"unique"`` — exactly one candidate
      * ``"deepest"`` — multiple, picked the longest path
      * ``"ambiguous"`` — multiple and no resolution strategy applied
      * ``"none"`` — no candidates
    """
    if not candidates:
        return (None, "none")
    if len(candidates) == 1:
        return (candidates[0], "unique")
    if prefer_deepest:
        return (max(candidates, key=lambda p: len(str(p))), "deepest")
    return (None, "ambiguous")


# nexus-zg4c: RDR-prefix matcher. RDRs are renamed end-to-end occasionally
# (rdr-066-enrichment-time → rdr-066-composition-smoke) but their numeric
# id is the durable handle. ``rdr-NNN-`` is the contract: digits, then a
# dash, then the slug. Three or more digits accommodates the eventual
# four-digit RDRs without rewriting the regex.
import re as _re  # noqa: E402

_RDR_PREFIX_RE = _re.compile(r"^(rdr-\d{3,}-)")


def _rdr_prefix_of(file_path: str) -> str:
    """Return the ``rdr-NNN-`` prefix of *file_path*'s basename, or ``""``.

    Empty when *file_path* is empty, has no ``rdr-NNN-`` basename, or the
    digit run is shorter than three (which would match release tag
    artifacts like ``rdr-1-`` from migration scripts).
    """
    if not file_path:
        return ""
    basename = Path(file_path).name
    match = _RDR_PREFIX_RE.match(basename)
    return match.group(1) if match else ""


def _build_rdr_prefix_index(
    source_dir: Path,
) -> dict[str, list[Path]]:
    """Walk *source_dir* and return ``{rdr_prefix: [absolute_path, ...]}``.

    Only ``.md`` / ``.markdown`` files participate — RDRs are markdown.
    The prefix index lives alongside the basename index so the two-step
    lookup in ``--rdr-prefix-mode`` (basename first, prefix second) only
    walks the source tree once.
    """
    import os as _os
    index: dict[str, list[Path]] = {}
    for root, dirs, files in _os.walk(
        str(source_dir.resolve()), followlinks=True,
    ):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        root_path = Path(root)
        for fname in files:
            if Path(fname).suffix.lower() not in (".md", ".markdown"):
                continue
            prefix = _rdr_prefix_of(fname)
            if not prefix:
                continue
            index.setdefault(prefix, []).append(root_path / fname)
    return index


@catalog.command("remediate-paths")
@click.argument(
    "source_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--dry-run", is_flag=True,
    help="Show the transition table without writing.",
)
@click.option(
    "--collection", default="",
    help="Limit remediation to entries in this physical collection.",
)
@click.option(
    "--owner", default="",
    help="Limit remediation to entries under this owner tumbler prefix.",
)
@click.option(
    "--prefer-deepest", is_flag=True,
    help="When multiple candidates share a basename, pick the deepest path "
         "(longest absolute path string). Default: skip ambiguous entries.",
)
@click.option(
    "--mark-missing", is_flag=True,
    help="For entries with no candidate found in SOURCE_DIR, set "
         "meta.status='missing' so 'nx catalog gc' can sweep them.",
)
@click.option(
    "--extensions", default="",
    help="Comma-separated extensions to scan (default: .pdf,.md,.markdown). "
         "Use '*' to scan every file regardless of extension.",
)
@click.option(
    "--rdr-prefix-mode", is_flag=True,
    help="When basename match fails for an RDR file, fall back to matching "
         "by ``rdr-NNN-`` prefix. Catches RDRs renamed end-to-end "
         "(e.g. rdr-066-enrichment-time.md → rdr-066-composition-smoke.md).",
)
def remediate_paths_cmd(
    source_dir: Path,
    dry_run: bool,
    collection: str,
    owner: str,
    prefer_deepest: bool,
    mark_missing: bool,
    extensions: str,
    rdr_prefix_mode: bool,
) -> None:
    """Repair catalog entries whose file_path is a basename or has gone missing.

    Walks SOURCE_DIR and matches catalog entries by basename. For each
    remediable entry, updates file_path to an absolute path under
    SOURCE_DIR. Use this after moving PDFs from ~/Downloads into a
    git-backed papers archive, or any time the original ingest paths
    no longer exist on disk.

    \b
    Examples:
      nx catalog remediate-paths ~/papers-archive --dry-run
      nx catalog remediate-paths ~/papers --collection knowledge__hybridrag
      nx catalog remediate-paths ~/papers --prefer-deepest --mark-missing

    Strategy:
      * Catalog entries with file_path = basename only (no slash) → look up
        by basename, update on unique match.
      * Catalog entries with file_path = absolute path that doesn't exist
        on disk → same lookup; treat as moved/recovered.
      * Multiple basename matches in SOURCE_DIR → ambiguous, skip
        (use --prefer-deepest to break ties by path length).
      * No basename match → leave alone, optionally mark with --mark-missing.

    Idempotent: re-running on the same SOURCE_DIR is a no-op once entries
    are resolved.
    """
    ext_filter: frozenset[str] | None
    if extensions == "*":
        ext_filter = None  # match every file
    elif extensions:
        ext_filter = frozenset(
            e if e.startswith(".") else f".{e}"
            for e in (s.strip().lower() for s in extensions.split(","))
            if e
        )
    else:
        ext_filter = _REMEDIATE_DEFAULT_EXTENSIONS

    cat = _get_catalog()

    click.echo(f"Scanning {source_dir.resolve()}…")
    index = _build_basename_index(source_dir, ext_filter)
    click.echo(f"Indexed {sum(len(v) for v in index.values())} files "
               f"({len(index)} unique basenames).")

    # Build the RDR prefix index up front so the resolution loop is one
    # walk-cost: avoids scanning source_dir twice when --rdr-prefix-mode is on.
    prefix_index: dict[str, list[Path]] = (
        _build_rdr_prefix_index(source_dir) if rdr_prefix_mode else {}
    )
    if rdr_prefix_mode:
        click.echo(
            f"Indexed {sum(len(v) for v in prefix_index.values())} RDR file(s) "
            f"({len(prefix_index)} unique prefixes)."
        )

    # Select entries to consider.
    entries: list = []
    if owner:
        entries = cat.by_owner(Tumbler.parse(owner))
    elif collection:
        # CatalogTaxonomy doesn't expose by_physical_collection directly;
        # walk all_documents and filter.
        entries = [
            e for e in cat.all_documents()
            if e.physical_collection == collection
        ]
    else:
        entries = cat.all_documents()

    if not entries:
        click.echo("No catalog entries to consider.")
        return

    # Categorise.
    transitions: list[tuple[object, str, str, Path | None, str]] = []
    skipped_ok = 0
    skipped_no_file_path = 0
    n_devonthink = 0
    for entry in entries:
        needs, reason = _entry_needs_remediation(entry)
        if not needs:
            if reason == "no-file-path":
                skipped_no_file_path += 1
            else:
                skipped_ok += 1
            continue
        basename = Path(entry.file_path).name
        # nexus-srck: try DEVONthink resolution before basename scan.
        # When meta carries devonthink_uri and DT reports an existing
        # path, that's authoritative — no point ranking basename matches
        # against a SOURCE_DIR walk that wouldn't include DT's
        # Files.noindex tree anyway.
        dt_path = _resolve_via_devonthink(entry)
        if dt_path is not None:
            n_devonthink += 1
            transitions.append((entry, reason, "devonthink", dt_path, basename))
            continue
        candidates = index.get(basename, [])
        chosen, note = _resolve_candidate(
            entry, candidates, prefer_deepest=prefer_deepest,
        )
        # Fallback: same-RDR-prefix replacement. Only fires when basename
        # match found nothing, the entry's basename has a usable rdr-NNN-
        # prefix, and --rdr-prefix-mode was requested.
        if (
            chosen is None
            and note == "none"
            and rdr_prefix_mode
        ):
            prefix = _rdr_prefix_of(basename)
            if prefix:
                prefix_candidates = prefix_index.get(prefix, [])
                chosen, note = _resolve_candidate(
                    entry, prefix_candidates, prefer_deepest=prefer_deepest,
                )
                # Annotate the note so the table tells the operator the
                # match came from the RDR-prefix path, not the basename
                # path (relevant when sorting which renames you've shipped).
                if chosen is not None and note in ("unique", "deepest"):
                    note = f"rdr-prefix:{note}"
        transitions.append((entry, reason, note, chosen, basename))

    # Report.
    n_total = len(transitions)
    n_resolved = sum(1 for _, _, _, p, _ in transitions if p is not None)
    n_ambiguous = sum(1 for _, _, n, _, _ in transitions if n == "ambiguous")
    n_missing = sum(1 for _, _, n, _, _ in transitions if n == "none")

    click.echo(
        f"\n{n_total} entries need remediation "
        f"(skipped {skipped_ok} already-good, {skipped_no_file_path} no-file-path):"
    )
    click.echo(f"  {n_resolved:4d} resolvable")
    if n_devonthink:
        click.echo(f"    of which {n_devonthink:4d} via DEVONthink")
    click.echo(f"  {n_ambiguous:4d} ambiguous (multiple basename matches)")
    click.echo(f"  {n_missing:4d} no candidate found in SOURCE_DIR")

    if not transitions:
        return

    # Show first ~20 transitions for visibility.
    click.echo("\nSample (first 20):")
    for entry, why, note, chosen, basename in transitions[:20]:
        old = entry.file_path or "(empty)"
        new = str(chosen) if chosen else f"<{note}>"
        click.echo(f"  [{why:8s}] {entry.tumbler}  {basename}\n    {old}\n  → {new}")

    if dry_run:
        click.echo("\n(dry-run — no catalog writes performed.)")
        return

    # Apply.
    n_updated = 0
    n_marked = 0
    for entry, _why, _note, chosen, _basename in transitions:
        if chosen is not None:
            cat.update(entry.tumbler, file_path=str(chosen))
            n_updated += 1
        elif mark_missing:
            cat.update(entry.tumbler, meta={"status": "missing"})
            n_marked += 1

    click.echo(
        f"\nDone: updated {n_updated} file_paths"
        + (f", marked {n_marked} as missing" if mark_missing else "")
        + "."
    )


# ── nx catalog prune-stale (nexus-zg4c) ─────────────────────────────────────


@catalog.command("prune-stale")
@click.option(
    "--collection", default="",
    help="Limit prune to entries in this physical_collection.",
)
@click.option(
    "--owner", default="",
    help="Limit prune to entries under this owner tumbler prefix.",
)
@click.option(
    "--source-dir", "source_dir_opt", default="",
    type=click.Path(exists=False, file_okay=False, path_type=Path),
    help="Optional source directory to consult for RDR-prefix replacements; "
         "when set, entries whose ``rdr-NNN-`` prefix matches a file under "
         "SOURCE_DIR are skipped (preferring rename-aware remediation over "
         "destructive prune). Use --no-rdr-prefix-skip to disable that check.",
)
@click.option(
    "--rdr-prefix-skip/--no-rdr-prefix-skip",
    default=True,
    help="When --source-dir is set, skip entries whose RDR-prefix has a "
         "plausible replacement on disk. On by default.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to perform deletions.",
)
@click.option(
    "--confirm", is_flag=True, default=False,
    help="Required alongside --no-dry-run to actually delete catalog rows.",
)
def prune_stale_cmd(
    collection: str,
    owner: str,
    source_dir_opt: Path,
    rdr_prefix_skip: bool,
    dry_run: bool,
    confirm: bool,
) -> None:
    """Drop catalog entries whose ``file_path`` is absolute and missing on disk.

    Catalog-side counterpart to ``nx t3 prune-stale`` (#349). Pairs
    naturally with ``nx catalog remediate-paths --rdr-prefix-mode``: run
    the remediator first to repair what's recoverable, then prune the
    rest.

    \b
    Default is read-only (--dry-run is on). To actually delete:
      nx catalog prune-stale --no-dry-run --confirm

    \b
    Examples:
      nx catalog prune-stale                                 # report all
      nx catalog prune-stale -c rdr__nexus-571b8edd          # one collection
      nx catalog prune-stale --source-dir docs/rdr           # honour rename hints
      nx catalog prune-stale --no-dry-run --confirm          # actually delete

    \b
    Skip rules — these are never deleted:
      * Empty file_path (MCP-stored entries with no source file).
      * Basename-only file_path (no ``/``) — remediable, not stale.
      * file_path that exists on disk.
      * RDR entries whose ``rdr-NNN-`` prefix matches a file under
        --source-dir, when --rdr-prefix-skip is on (default).
    """
    will_delete = (not dry_run) and confirm
    if (not dry_run) and not confirm:
        click.echo(
            "--no-dry-run alone is treated as report-only. "
            "Add --confirm to actually delete catalog rows."
        )
        will_delete = False

    cat = _get_catalog()

    # Build the RDR-prefix index lazily — only when both --source-dir is
    # set and --rdr-prefix-skip is on. Skipping the walk on the no-source
    # path is important; nx catalog prune-stale with no args should be
    # fast enough to run in a CI loop.
    prefix_index: dict[str, list[Path]] = {}
    if source_dir_opt and rdr_prefix_skip and source_dir_opt.exists():
        prefix_index = _build_rdr_prefix_index(source_dir_opt)

    # Select candidate entries.
    if owner:
        entries = cat.by_owner(Tumbler.parse(owner))
    elif collection:
        entries = [
            e for e in cat.all_documents()
            if e.physical_collection == collection
        ]
    else:
        entries = cat.all_documents()

    # nexus-6ims: relative file_paths must resolve against the owner's
    # repo_root (RDR-060), not against the running process's cwd. Pre-fix
    # logic used ``Path(fp).exists()`` directly, which caught absolute
    # paths fine but mass-misclassified relative paths whenever the
    # operator ran the verb from a different repo (verified 2026-05-08:
    # 11,766 valid entries reported as stale because cwd was nexus, not
    # the entry's owning repo).
    owner_roots = dict(cat._db.execute(
        "SELECT tumbler_prefix, repo_root FROM owners WHERE repo_root != ''"
    ))

    stale: list = []  # entries to delete
    skipped_replacement: list = []  # entries with RDR-prefix replacement
    skipped_no_root: list = []  # owner has no repo_root — can't verify
    for entry in entries:
        fp = entry.file_path or ""
        if not fp:  # MCP-stored, no source file
            continue
        if "/" not in fp:  # basename-only — remediable
            continue

        if fp.startswith("/"):
            resolved = Path(fp)
        else:
            # Relative path — anchor at owner.repo_root.
            t_str = str(entry.tumbler)
            parts = t_str.split(".")
            owner_id = ".".join(parts[:2]) if len(parts) >= 2 else ""
            root = owner_roots.get(owner_id, "")
            if not root:
                # Owner has no repo_root (registered before RDR-060
                # added the column). Cannot verify presence; refuse to
                # delete — operator must repair owner.repo_root first
                # via ``nx catalog dedupe-owners`` or manual update.
                skipped_no_root.append(entry)
                continue
            resolved = Path(root) / fp

        if resolved.exists():  # live, not stale
            continue

        # Stale candidate. If a same-prefix replacement exists, prefer
        # remediation: skip prune.
        if prefix_index:
            prefix = _rdr_prefix_of(fp)
            if prefix and prefix_index.get(prefix):
                skipped_replacement.append(entry)
                continue
        stale.append(entry)

    n_stale = len(stale)
    n_skipped = len(skipped_replacement)
    n_no_root = len(skipped_no_root)
    parts_msg = []
    if n_skipped:
        parts_msg.append(f"skipped {n_skipped} with same-prefix replacement")
    if n_no_root:
        parts_msg.append(
            f"skipped {n_no_root} relative-path entries whose owner has "
            f"no repo_root (cannot verify)"
        )
    suffix = f" ({'; '.join(parts_msg)})" if parts_msg else ""
    click.echo(
        f"{n_stale} stale entr{'y' if n_stale == 1 else 'ies'}{suffix}."
    )

    if n_stale:
        click.echo("\nSample (first 20):")
        for entry in stale[:20]:
            click.echo(
                f"  {entry.tumbler}  [{entry.physical_collection or '-'}]  "
                f"{entry.file_path}"
            )

    if not will_delete:
        if dry_run:
            click.echo("\n(dry-run — no catalog writes performed.)")
        return

    # Backup snapshot before delete (RDR-106 Option A).
    from nexus.catalog.catalog_backup import snapshot_documents
    backup_path = snapshot_documents(
        cat,
        [str(e.tumbler) for e in stale],
        verb="prune-stale",
        reason="absolute path missing OR relative path missing under owner.repo_root",
        args={
            "collection": collection, "owner": owner,
            "source_dir": str(source_dir_opt) if source_dir_opt else "",
            "rdr_prefix_skip": rdr_prefix_skip,
        },
    )
    if backup_path:
        click.echo(
            f"\nBackup snapshot written: {backup_path}"
            f"\n  Restore with: nx catalog undelete {backup_path.name}"
        )

    n_deleted = 0
    for entry in stale:
        if cat.delete_document(entry.tumbler):
            n_deleted += 1

    click.echo(f"\nDone: deleted {n_deleted} catalog entr"
               f"{'y' if n_deleted == 1 else 'ies'}.")


# ── RDR-101 Phase 2: synthesize-log (in-place fallback recovery) ─────────


@catalog.command("synthesize-log")
@click.option(
    "--check", is_flag=True,
    help=(
        "Detect bootstrap-fallback mode without writing. Exit 0 when not "
        "in fallback, exit 1 when fallback is active."
    ),
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print event counts that would be synthesized; write nothing.",
)
@click.option(
    "--no-verify", is_flag=True,
    help=(
        "Skip the post-write replay-equality verification. Use only when "
        "you have already verified the catalog independently."
    ),
)
@click.option(
    "--force", is_flag=True,
    help=(
        "Synthesize even when the catalog is not in bootstrap-fallback. "
        "Existing event-log doc_ids are harvested and preserved so that "
        "T3 chunk metadata referencing them does not become stale."
    ),
)
def synthesize_log_cmd(
    check: bool, dry_run: bool, no_verify: bool, force: bool
) -> None:
    """Rebuild ``events.jsonl`` from the catalog's JSONL state in place.

    Companion to ``nx catalog doctor`` for catalogs in bootstrap-fallback
    mode. Calls ``nexus.catalog.synthesizer.synthesize_from_jsonl`` with
    ``mint_doc_id=True`` and writes the resulting envelope stream to
    ``events.jsonl`` atomically. Snapshots the entire catalog directory
    before touching it; on a verify FAIL, rolls the snapshot back into
    place and retains both copies for forensics.

    Lossless alternative to ``rm -rf catalog && nx catalog setup``, which
    discards user-authored typed links and owner registrations because
    those are not reconstructible from T3 alone.
    """
    import dataclasses
    import shutil
    import time
    from datetime import datetime, timezone

    from nexus.config import catalog_path
    from nexus.catalog import events as ev
    from nexus.catalog.synthesizer import synthesize_from_jsonl

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        raise click.ClickException(
            f"Catalog at {cat_path} is not initialized. "
            "Run 'nx catalog setup' first."
        )

    bootstrap_status = _check_bootstrap_status()
    fallback_active = bool(bootstrap_status.get("fallback_active"))

    if check:
        if fallback_active:
            click.echo(
                "fallback-active: events.jsonl is sparse vs documents.jsonl. "
                "Run 'nx catalog synthesize-log' to repair in place.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        click.echo("not-in-fallback: events.jsonl matches documents.jsonl.")
        return

    if not fallback_active and not force:
        click.echo(
            "no-op: catalog is not in bootstrap-fallback mode. "
            "Pass --force to synthesize anyway."
        )
        return

    # --force on a healthy catalog: harvest existing tumbler->doc_id from
    # events.jsonl so re-synthesis preserves T3-side doc_id references.
    preserve_doc_ids: dict[str, str] = {}
    events_path = cat_path / "events.jsonl"
    if force and events_path.exists():
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != ev.TYPE_DOCUMENT_REGISTERED:
                    continue
                payload = obj.get("payload") or {}
                tumbler = payload.get("tumbler")
                doc_id = payload.get("doc_id")
                if tumbler and doc_id:
                    preserve_doc_ids[tumbler] = doc_id

    # Synthesize the full event stream into memory and tally per-type.
    events_list = list(
        synthesize_from_jsonl(
            cat_path,
            mint_doc_id=True,
            preserve_doc_ids=preserve_doc_ids or None,
        )
    )
    counts: dict[str, int] = {}
    for e in events_list:
        counts[e.type] = counts.get(e.type, 0) + 1
    total = sum(counts.values())

    click.echo("== synthesizing events ==")
    for type_name in sorted(counts):
        click.echo(f"  {type_name:<28} {counts[type_name]:>6}")
    click.echo(f"  {'TOTAL':<28} {total:>6}")

    if dry_run:
        click.echo("(dry-run: no files written)")
        return

    # Snapshot the entire catalog directory to a sibling. Forensic
    # retention: this command never deletes the snapshot, even on PASS.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = cat_path.parent / f"{cat_path.name}.synth-snapshot-{ts}"
    if snapshot_dir.exists():
        # Disambiguate when a prior run within the same second exists.
        snapshot_dir = cat_path.parent / (
            f"{cat_path.name}.synth-snapshot-{ts}-{int(time.time() * 1000) % 1000}"
        )
    # Skip ``.db-shm`` and ``.db-wal`` (transient WAL artifacts). On
    # Linux either file may be listed by the directory scan but
    # disappear before the per-file copy, raising FileNotFoundError.
    # ``.db-shm`` is regenerated by SQLite on next open; ``.db-wal``
    # checkpoints fold back into the main db on connection close.
    # Nothing forensic survives a completed checkpoint, so omitting both
    # keeps the snapshot reproducible across runs without losing state.
    # nexus-fmhv: CI hit the race consistently in
    # test_force_synthesizes_when_not_in_fallback.
    shutil.copytree(
        cat_path,
        snapshot_dir,
        ignore=shutil.ignore_patterns("*.db-shm", "*.db-wal"),
    )
    click.echo(f"snapshot: {snapshot_dir}")

    # Atomic write: serialize to events.jsonl.tmp, fsync, rename.
    tmp_path = events_path.with_suffix(".jsonl.tmp")
    with tmp_path.open("w") as f:
        for e in events_list:
            line = json.dumps(
                {
                    "type": e.type,
                    "v": e.v,
                    "payload": dataclasses.asdict(e.payload),
                    "ts": e.ts,
                },
                separators=(",", ":"),
            )
            f.write(line)
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, events_path)
    click.echo(f"wrote: {events_path} ({total} events)")

    if no_verify:
        click.echo("PASS (verify skipped via --no-verify)")
        return

    # Verify by re-running the doctor's replay-equality check against
    # the freshly-written log. _run_replay_equality reads catalog_path()
    # so it picks up the new state automatically.
    report = _run_replay_equality()
    if report.get("pass"):
        click.echo("PASS: replay-equality verified against fresh log.")
        click.echo(f"snapshot retained for forensics: {snapshot_dir}")
        return

    # Verify FAIL: rotate the failed live state aside, restore from the
    # snapshot via copytree. Snapshot is left pristine so the operator
    # has three artifacts for forensics: the pristine pre-synthesis
    # snapshot, the failed live state, and the restored live catalog.
    failed_dir = cat_path.parent / f"{cat_path.name}.synth-failed-{ts}"
    if failed_dir.exists():
        failed_dir = cat_path.parent / (
            f"{cat_path.name}.synth-failed-{ts}-{int(time.time() * 1000) % 1000}"
        )
    os.rename(cat_path, failed_dir)
    shutil.copytree(snapshot_dir, cat_path)

    click.echo(
        f"FAIL: replay-equality verification did not pass: {report}",
        err=True,
    )
    click.echo(f"failed-state retained: {failed_dir}", err=True)
    click.echo(f"snapshot retained: {snapshot_dir}", err=True)
    click.echo(f"catalog restored from snapshot at: {cat_path}", err=True)
    raise click.exceptions.Exit(1)


# ── RDR-101 Phase 1: doctor --replay-equality ────────────────────────────


@catalog.command("doctor")
@click.option(
    "--replay-equality",
    "replay_equality",
    is_flag=True,
    help=(
        "Drive the synthesizer + projector against the live catalog and "
        "diff the projected SQLite against the live .catalog.db. "
        "Confirms that the event-sourced projection is deterministic for "
        "the current catalog state. Read-only against the live catalog."
    ),
)
@click.option(
    "--t3-doc-id-coverage",
    "t3_doc_id_coverage",
    is_flag=True,
    help=(
        "Walk every T3 collection and report doc_id coverage. PASS = "
        "every non-orphan chunk in every collection carries a doc_id "
        "matching what events.jsonl claims. Read-only against T3 and "
        "the catalog. The Phase 2 backfill verb that originally "
        "populated chunks with their doc_id was retired post Phase 5b "
        "(nexus-iftc); operators on conformant catalogs should see "
        "PASS without further action."
    ),
)
@click.option(
    "--strict-not-in-t3",
    "strict_not_in_t3",
    is_flag=True,
    help=(
        "With --t3-doc-id-coverage: treat 'event log claims a chunk T3 "
        "doesn't have' as a hard failure rather than a warning. Default "
        "is warning so legitimate operational deletions (re-ingestion, "
        "pruning) don't permanently red the doctor; pass --strict-not-"
        "in-t3 to enforce 'event log = authoritative ledger, T3 must "
        "match exactly'."
    ),
)
@click.option(
    "--collections-drift",
    "collections_drift",
    is_flag=True,
    help=(
        "Phase 6 check: every T3 collection and every distinct "
        "documents.physical_collection has a row in the collections "
        "projection. Drift is a release blocker; remediate with "
        "'nx catalog backfill-collections'."
    ),
)
@click.option(
    "--chunk-size-distribution",
    "chunk_size_distribution",
    is_flag=True,
    help=(
        "nexus-6dan: per-collection chunk size stats (p50/p95/p99/max). "
        "FAIL on any chunk > MAX_DOCUMENT_BYTES (Voyage will reject); "
        "WARN when >5% of chunks are < 100 bytes (micro-chunks)."
    ),
)
@click.option(
    "--chunk-text-dedup",
    "chunk_text_dedup",
    is_flag=True,
    help=(
        "nexus-6dan: collect chunk_text_hash across all collections. "
        "Within-collection dupe ratio > 5% signals a chunker bug; "
        "cross-collection dupe count > 100 chunks signals a cross-"
        "ingest investigation lead."
    ),
)
@click.option(
    "--t3-vs-catalog",
    "t3_vs_catalog",
    is_flag=True,
    help=(
        "nexus-6dan: bridge the projection-vs-T3 gap. Reports T3 "
        "collections with no catalog documents (orphan), T3 collections "
        "in catalog projection but with 0 chunks (zombie), and catalog "
        "documents whose physical_collection is gone from T3."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def doctor_cmd(
    replay_equality: bool,
    t3_doc_id_coverage: bool,
    strict_not_in_t3: bool,
    collections_drift: bool,
    chunk_size_distribution: bool,
    chunk_text_dedup: bool,
    t3_vs_catalog: bool,
    as_json: bool,
) -> None:
    """RDR-101 catalog doctor surface.

    Supports three checks today:
      - ``--replay-equality`` (Phase 1, PR C): synthesizer + projector
        round-trip against the live SQLite.
      - ``--t3-doc-id-coverage`` (Phase 2, PR δ): T3 chunks carry the
        doc_id metadata that events.jsonl claims.
      - ``--collections-drift`` (Phase 6, nexus-o6aa.14): every T3
        collection and every documents.physical_collection has a row
        in the collections projection.

    Future flags land in later phases.
    """
    any_check = (
        replay_equality or t3_doc_id_coverage or collections_drift
        or chunk_size_distribution or chunk_text_dedup or t3_vs_catalog
    )
    if not any_check:
        raise click.UsageError(
            "Pass a check flag: --replay-equality, "
            "--t3-doc-id-coverage, --collections-drift, "
            "--chunk-size-distribution, --chunk-text-dedup, "
            "or --t3-vs-catalog."
        )
    if strict_not_in_t3 and not t3_doc_id_coverage:
        raise click.UsageError(
            "--strict-not-in-t3 requires --t3-doc-id-coverage; the "
            "flag scopes the not-in-T3 fail behaviour of the coverage "
            "check and is meaningless without it."
        )

    overall_pass = True
    json_payload: dict = {}

    # RDR-101 Phase 3 follow-up B (nexus-o6aa.9.7): surface bootstrap
    # fallback state to the operator. When _ensure_consistent runtime-
    # decides to fall back to legacy reads (events.jsonl is non-empty
    # but sparse vs documents.jsonl), ES writes still land in the log
    # while reads come from legacy JSONL — a silent split state where
    # replay-equality is fundamentally not testing what it claims.
    # Construct a Catalog up front so _ensure_consistent runs and
    # bootstrap_fallback_active is current.
    bootstrap_status = _check_bootstrap_status()
    if bootstrap_status["fallback_active"]:
        if as_json:
            json_payload["bootstrap_fallback"] = bootstrap_status
        else:
            click.echo(
                "WARNING: catalog is operating in bootstrap-fallback mode.\n"
                "  events.jsonl is non-empty but sparse vs documents.jsonl;\n"
                "  ES writes are landing in the log but reads come from\n"
                "  legacy JSONL; replay equality is silently broken.\n"
                "\n"
                "  Restore in place with:\n"
                "    nx catalog synthesize-log\n"
                "\n"
                "  This rebuilds events.jsonl from the JSONL state with\n"
                "  zero data loss. 'nx catalog setup' from a clean state\n"
                "  is a lossy fallback - it cannot reconstruct user-\n"
                "  authored typed links or owner registrations from T3.\n",
                err=True,
            )
        overall_pass = False

    if replay_equality:
        report = _run_replay_equality()
        if as_json:
            json_payload["replay_equality"] = report
        else:
            _print_replay_equality_text(report)
        if not report["pass"]:
            overall_pass = False

    if t3_doc_id_coverage:
        report = _run_t3_doc_id_coverage(strict_not_in_t3=strict_not_in_t3)
        if as_json:
            json_payload["t3_doc_id_coverage"] = report
        else:
            if replay_equality:
                click.echo("")  # separator between checks
            _print_t3_doc_id_coverage_text(report)
        if not report["pass"]:
            overall_pass = False

    if collections_drift:
        report = _run_collections_drift()
        if as_json:
            json_payload["collections_drift"] = report
        else:
            if replay_equality or t3_doc_id_coverage:
                click.echo("")
            _print_collections_drift_text(report)
        if not report["pass"]:
            overall_pass = False

    # nexus-6dan: 3 new checks. Each is read-only against T3 + catalog.
    _printed_anything = (
        replay_equality or t3_doc_id_coverage or collections_drift
    )
    if chunk_size_distribution:
        report = _run_chunk_size_distribution()
        if as_json:
            json_payload["chunk_size_distribution"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_chunk_size_distribution_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if chunk_text_dedup:
        report = _run_chunk_text_dedup()
        if as_json:
            json_payload["chunk_text_dedup"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_chunk_text_dedup_text(report)
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if t3_vs_catalog:
        report = _run_t3_vs_catalog()
        if as_json:
            json_payload["t3_vs_catalog"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_t3_vs_catalog_text(report)
        if not report["pass"]:
            overall_pass = False

    if as_json:
        click.echo(json.dumps(json_payload, indent=2))

    if not overall_pass:
        raise click.exceptions.Exit(1)


def _run_collections_drift() -> dict:
    """Phase 6 check: collections projection vs T3 + documents.physical_collection.

    Returns ``{"pass": bool, "t3_not_in_projection": list,
    "doc_collections_not_in_projection": list, "projection_not_in_t3": list}``.

    A projection row whose ``superseded_by`` is set is allowed to be
    absent from T3 (post-rename state). Bypass-schema collections
    (``taxonomy__*``) are out of scope for this check.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415

    cat = _get_catalog()
    try:
        t3_db = make_t3()
        t3_names = {
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:
        return {
            "pass": False,
            "t3_not_in_projection": [],
            "doc_collections_not_in_projection": [],
            "projection_not_in_t3": [],
            "error": f"Failed to list T3 collections: {exc}",
        }

    projection = cat.list_collections()
    projection_names = {r["name"] for r in projection}
    superseded_names = {
        r["name"] for r in projection if r.get("superseded_by")
    }

    rows = cat._db.execute(
        "SELECT DISTINCT physical_collection FROM documents "
        "WHERE physical_collection != ''"
    ).fetchall()
    doc_collections = {r[0] for r in rows if r[0]}

    t3_not_in_projection = sorted(t3_names - projection_names)
    doc_not_in_projection = sorted(doc_collections - projection_names)
    projection_not_in_t3 = sorted(
        projection_names - t3_names - superseded_names
    )

    passed = (
        not t3_not_in_projection
        and not doc_not_in_projection
        and not projection_not_in_t3
    )
    return {
        "pass": passed,
        "t3_not_in_projection": t3_not_in_projection,
        "doc_collections_not_in_projection": doc_not_in_projection,
        "projection_not_in_t3": projection_not_in_t3,
    }


def _print_collections_drift_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"collections-drift: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"collections-drift: {status}")
    if report["t3_not_in_projection"]:
        click.echo(
            f"  T3 collections without projection rows "
            f"({len(report['t3_not_in_projection'])}):"
        )
        for n in report["t3_not_in_projection"]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog backfill-collections"
        )
    if report["doc_collections_not_in_projection"]:
        click.echo(
            f"  documents.physical_collection without projection rows "
            f"({len(report['doc_collections_not_in_projection'])}):"
        )
        for n in report["doc_collections_not_in_projection"]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog backfill-collections"
        )
    if report["projection_not_in_t3"]:
        click.echo(
            f"  Projection rows whose T3 collection is gone and not "
            f"superseded ({len(report['projection_not_in_t3'])}):"
        )
        for n in report["projection_not_in_t3"]:
            click.echo(f"    {n}")
        # 'rename-collection' would refuse here (it requires the old
        # T3 collection to exist). Direct supersede is the correct
        # recovery; a future 'nx catalog supersede-collection' verb
        # would wrap this script.
        click.echo(
            "  Remediate: register a target collection and supersede manually:\n"
            "    python -c \"from nexus.catalog.catalog import Catalog; "
            "from nexus.config import catalog_path; "
            "p=catalog_path(); c=Catalog(p, p / '.catalog.db'); "
            "c.register_collection('<TARGET>'); "
            "c.supersede_collection('<OLD>', '<TARGET>')\""
        )


# nexus-6dan: tunable thresholds for the 3 new doctor checks. Module-
# level constants so tests can stub them without re-implementing.
_MICRO_CHUNK_BYTES = 100
_MICRO_CHUNK_WARN_RATIO = 0.05
_WITHIN_COLL_DUPE_WARN_RATIO = 0.05
_CROSS_COLL_DUPE_WARN_COUNT = 100


def _percentile(sorted_values: list[int], q: float) -> int:
    """Return the q-th percentile (q in [0,1]) of a sorted-ascending
    int list. Empty list returns 0; single value returns itself.
    Linear interpolation between adjacent values; matches numpy
    default semantics closely enough for ops display.
    """
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return int(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def _run_chunk_size_distribution() -> dict:
    """Per-collection chunk-size statistics (nexus-6dan).

    Walks every T3 collection (paginating <= 300 records per call),
    measures ``len(document_text)`` for each chunk, and reports
    p50/p95/p99/max + counts of micro-chunks (< 100 bytes) and
    over-quota chunks (> ``MAX_DOCUMENT_BYTES``). FAIL on any
    over-quota chunk (Voyage will reject these at embed time);
    WARN flagged at the per-collection level when > 5% of chunks
    are micro-chunks (likely a chunker bug).

    Returns ``{"pass": bool, "tables": {coll_name: {...stats...}}}``.
    Bypass-schema (``taxonomy__*``) collections are skipped: they
    carry centroid embeddings, not chunked text, so size stats
    aren't meaningful.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "tables": {},
        }

    page = QUOTAS.MAX_QUERY_RESULTS  # 300
    max_doc_bytes = QUOTAS.MAX_DOCUMENT_BYTES
    overall_pass = True
    tables: dict[str, dict] = {}
    for name in collections:
        try:
            col = t3._client.get_collection(name=name)
        except Exception as exc:
            tables[name] = {"error": f"open: {exc}"}
            overall_pass = False
            continue
        sizes: list[int] = []
        offset = 0
        while True:
            try:
                got = col.get(
                    limit=page, offset=offset, include=["documents"],
                )
            except Exception as exc:
                tables[name] = {"error": f"get: {exc}"}
                overall_pass = False
                break
            docs = got.get("documents") or []
            if not docs:
                break
            sizes.extend(len(d or "") for d in docs)
            if len(docs) < page:
                break
            offset += page
        else:
            continue
        sizes.sort()
        n = len(sizes)
        micros = sum(1 for s in sizes if s < _MICRO_CHUNK_BYTES)
        over_quota = sum(1 for s in sizes if s > max_doc_bytes)
        ratio = (micros / n) if n else 0.0
        coll_pass = over_quota == 0
        if not coll_pass:
            overall_pass = False
        tables[name] = {
            "total_chunks": n,
            "p50": _percentile(sizes, 0.5),
            "p95": _percentile(sizes, 0.95),
            "p99": _percentile(sizes, 0.99),
            "max": sizes[-1] if sizes else 0,
            "micro_count": micros,
            "micro_ratio": round(ratio, 4),
            "over_quota_count": over_quota,
            "warn": ratio > _MICRO_CHUNK_WARN_RATIO,
            "pass": coll_pass,
        }
    return {
        "pass": overall_pass,
        "max_document_bytes": max_doc_bytes,
        "micro_chunk_bytes": _MICRO_CHUNK_BYTES,
        "tables": tables,
    }


def _print_chunk_size_distribution_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"chunk-size-distribution: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"chunk-size-distribution: {status}")
    click.echo(
        f"  thresholds: micro < {report['micro_chunk_bytes']}B, "
        f"over-quota > {report['max_document_bytes']}B"
    )
    for name, t in report["tables"].items():
        if "error" in t:
            click.echo(f"  ERROR {name}: {t['error']}")
            continue
        marker = "FAIL" if not t["pass"] else ("WARN" if t["warn"] else "ok")
        click.echo(
            f"  {marker} {name}  total={t['total_chunks']}  "
            f"p50={t['p50']}  p95={t['p95']}  p99={t['p99']}  "
            f"max={t['max']}  micro={t['micro_count']} "
            f"({t['micro_ratio']:.2%})  over_quota={t['over_quota_count']}"
        )


def _run_chunk_text_dedup() -> dict:
    """Cross-collection chunk_text_hash dedup audit (nexus-6dan).

    Walks every non-bypass-schema T3 collection, collects each
    chunk's ``chunk_text_hash`` metadata, and reports:
      - within-collection dupe ratio (one chash mapping to >1 cid):
        WARN when > 5% (signals a chunker bug producing non-distinct
        chunk text from distinct source positions).
      - cross-collection dupes (one chash present in >= 2 collections):
        WARN when count > 100 chunks (signals a cross-ingest pattern
        worth investigating, e.g. fixture re-import or multi-corpus
        leakage).

    Returns
    ``{"pass": bool, "within": {coll: {...}}, "cross": [{...}]}``.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "within": {},
            "cross": [],
        }

    page = QUOTAS.MAX_QUERY_RESULTS
    overall_pass = True
    within_summary: dict[str, dict] = {}
    chash_to_collections: dict[str, set[str]] = {}
    for name in collections:
        try:
            col = t3._client.get_collection(name=name)
        except Exception as exc:
            within_summary[name] = {"error": f"open: {exc}"}
            overall_pass = False
            continue
        chash_count: dict[str, int] = {}
        offset = 0
        while True:
            try:
                got = col.get(
                    limit=page, offset=offset, include=["metadatas"],
                )
            except Exception as exc:
                within_summary[name] = {"error": f"get: {exc}"}
                overall_pass = False
                break
            metas = got.get("metadatas") or []
            ids = got.get("ids") or []
            if not metas:
                break
            for meta in metas:
                meta = meta or {}
                ch = meta.get("chunk_text_hash") or ""
                if not ch:
                    continue
                chash_count[ch] = chash_count.get(ch, 0) + 1
                chash_to_collections.setdefault(ch, set()).add(name)
            if len(ids) < page:
                break
            offset += page
        else:
            continue
        total = sum(chash_count.values())
        # within-coll dupes: chashes seen >= 2 times in the same collection.
        dupe_chunks = sum(c for c in chash_count.values() if c >= 2)
        ratio = (dupe_chunks / total) if total else 0.0
        warn = ratio > _WITHIN_COLL_DUPE_WARN_RATIO
        within_summary[name] = {
            "total_chunks_with_hash": total,
            "dupe_chunks": dupe_chunks,
            "dupe_ratio": round(ratio, 4),
            "warn": warn,
        }
        # within-coll dupes are surfaced as WARN, not FAIL; the only
        # FAIL surface here is the open/get exception path.

    # Cross-collection: chashes present in >= 2 collections.
    cross = [
        {"chash": ch[:32], "collections": sorted(colls)}
        for ch, colls in chash_to_collections.items()
        if len(colls) >= 2
    ]
    cross_warn = len(cross) > _CROSS_COLL_DUPE_WARN_COUNT
    return {
        "pass": overall_pass,
        "within": within_summary,
        "cross_dupe_chunk_count": len(cross),
        "cross_dupe_warn_threshold": _CROSS_COLL_DUPE_WARN_COUNT,
        "cross_dupe_warn": cross_warn,
        "cross_sample": cross[:20],
    }


def _print_chunk_text_dedup_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"chunk-text-dedup: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"chunk-text-dedup: {status}")
    for name, t in report["within"].items():
        if "error" in t:
            click.echo(f"  ERROR {name}: {t['error']}")
            continue
        marker = "WARN" if t["warn"] else "ok"
        click.echo(
            f"  {marker} {name}  total={t['total_chunks_with_hash']}  "
            f"dupes={t['dupe_chunks']} ({t['dupe_ratio']:.2%})"
        )
    cross_marker = "WARN" if report["cross_dupe_warn"] else "ok"
    click.echo(
        f"  {cross_marker} cross-collection dupes: "
        f"{report['cross_dupe_chunk_count']} "
        f"(threshold {report['cross_dupe_warn_threshold']})"
    )


def _run_t3_vs_catalog() -> dict:
    """Bridge T3 vs catalog: surface 3 drift classes (nexus-6dan).

    Reports:
      - ``t3_orphans``: T3 collections with chunks but no catalog
        documents at all (no row referencing the collection).
      - ``zombies``: collections in the catalog projection that have
        a T3 collection but with 0 chunks.
      - ``docs_pointing_at_missing_t3``: catalog documents whose
        ``physical_collection`` value is not in T3 (e.g. T3 collection
        was deleted out from under the catalog).

    All read-only. PASS when all three lists are empty. Bypass-schema
    collections (``taxonomy__*``) are skipped from all three.
    """
    from nexus.db import make_t3  # noqa: PLC0415
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415

    cat = _get_catalog()
    try:
        t3_db = make_t3()
        t3_listing = {
            c["name"]: c for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "t3_orphans": [], "zombies": [],
            "docs_pointing_at_missing_t3": [],
        }

    t3_names = set(t3_listing.keys())
    rows = cat._db.execute(
        "SELECT physical_collection, COUNT(*) FROM documents "
        "WHERE physical_collection != '' GROUP BY physical_collection"
    ).fetchall()
    docs_per_coll: dict[str, int] = dict(rows)

    # T3 collections with chunks but zero catalog docs:
    t3_orphans = []
    for name in sorted(t3_names):
        if docs_per_coll.get(name, 0) > 0:
            continue
        # Only flag if the T3 collection actually has chunks; an empty
        # T3 collection with no docs is the zombie class below.
        try:
            col = t3_db._client.get_collection(name=name)
            count = col.count()
        except Exception:
            count = 0
        if count > 0:
            t3_orphans.append({"name": name, "chunk_count": count})

    # Zombies: in catalog projection AND in T3 BUT 0 chunks in T3.
    projection = cat.list_collections()
    projection_names = {
        r["name"] for r in projection if not r.get("superseded_by")
    }
    zombies = []
    for name in sorted(projection_names & t3_names):
        try:
            col = t3_db._client.get_collection(name=name)
            count = col.count()
        except Exception:
            continue
        if count == 0:
            zombies.append(name)

    # Catalog docs whose physical_collection is missing from T3.
    docs_missing = [
        {"physical_collection": pc, "doc_count": cnt}
        for pc, cnt in sorted(docs_per_coll.items())
        if pc and pc not in t3_names
    ]

    overall_pass = (
        not t3_orphans and not zombies and not docs_missing
    )
    return {
        "pass": overall_pass,
        "t3_orphans": t3_orphans,
        "zombies": zombies,
        "docs_pointing_at_missing_t3": docs_missing,
    }


def _print_t3_vs_catalog_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"t3-vs-catalog: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"t3-vs-catalog: {status}")
    if report["t3_orphans"]:
        click.echo(
            f"  T3 collections with chunks but no catalog docs "
            f"({len(report['t3_orphans'])}):"
        )
        for o in report["t3_orphans"][:20]:
            click.echo(f"    {o['name']}  chunks={o['chunk_count']}")
    if report["zombies"]:
        click.echo(
            f"  Zombie collections (registered, 0 chunks in T3) "
            f"({len(report['zombies'])}):"
        )
        for n in report["zombies"][:20]:
            click.echo(f"    {n}")
        click.echo(
            "  Remediate: nx catalog collection-gc --apply"
        )
    if report["docs_pointing_at_missing_t3"]:
        click.echo(
            f"  Catalog documents whose physical_collection is gone "
            f"from T3 ({len(report['docs_pointing_at_missing_t3'])}):"
        )
        for d in report["docs_pointing_at_missing_t3"][:20]:
            click.echo(
                f"    {d['physical_collection']}  docs={d['doc_count']}"
            )


def _check_bootstrap_status() -> dict:
    """Inspect the canonical-truth files at the configured catalog
    path and report whether the ES rebuild path would currently fall
    back to legacy (RDR-101 Phase 3 follow-up B, nexus-o6aa.9.7).

    Returns ``{"fallback_active": bool, "events_path": str,
    "documents_path": str}``. Used by the doctor verb to surface the
    silent split state where ``NEXUS_EVENT_SOURCED`` is on but reads
    come from legacy JSONL.

    Pure file inspection — does NOT construct a ``Catalog`` instance.
    Constructing one would trigger ``_ensure_consistent``, which
    re-projects events.jsonl into SQLite. That re-projection would
    silently overwrite any operator-injected drift the downstream
    doctor checks (e.g. ``--replay-equality``) are meant to detect.
    """
    from nexus.config import catalog_path
    from nexus.catalog.catalog import _read_event_sourced_gate
    from nexus.catalog import events as _ev

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        return {"fallback_active": False, "reason": "catalog_not_initialized"}

    events_path = cat_path / "events.jsonl"
    documents_path = cat_path / "documents.jsonl"

    if not _read_event_sourced_gate():
        # Legacy mode: no ES rebuild path runs, no fallback state.
        return {
            "fallback_active": False,
            "events_path": str(events_path),
            "documents_path": str(documents_path),
        }
    if (
        not events_path.exists()
        or events_path.stat().st_size == 0
        or not documents_path.exists()
    ):
        # ``use_event_log`` is False at the size gate before the
        # guardrail check fires; not a fallback state.
        return {
            "fallback_active": False,
            "events_path": str(events_path),
            "documents_path": str(documents_path),
        }

    # Replicate the ``_event_log_covers_legacy`` math non-mutatively.
    try:
        registered: set[str] = set()
        tombstoned: set[str] = set()
        with documents_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tumbler = rec.get("tumbler")
                if not tumbler:
                    continue
                if rec.get("_deleted"):
                    tombstoned.add(tumbler)
                else:
                    registered.add(tumbler)
        legacy_doc_count = len(registered - tombstoned)
        if legacy_doc_count == 0:
            return {
                "fallback_active": False,
                "events_path": str(events_path),
                "documents_path": str(documents_path),
            }

        event_doc_count = 0
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = obj.get("type")
                if t == _ev.TYPE_DOCUMENT_REGISTERED:
                    event_doc_count += 1
                elif t == _ev.TYPE_DOCUMENT_DELETED:
                    event_doc_count -= 1

        threshold = max(1, int(legacy_doc_count * 0.95))
        fallback_active = event_doc_count < threshold
    except Exception:
        fallback_active = False

    return {
        "fallback_active": fallback_active,
        "events_path": str(events_path),
        "documents_path": str(documents_path),
    }


def _run_replay_equality() -> dict:
    """Drive the projector against the live catalog and diff.

    Source of truth depends on the catalog's write path:

    * **Event-sourced** (``events.jsonl`` exists and is non-empty): replay
      the native event log directly. This is the path that matters once
      ``NEXUS_EVENT_SOURCED=1`` is on by default, since the legacy JSONL
      becomes a back-compat shadow rather than canonical state and a
      synthesizer-driven check would silently miss any divergence in the
      native write path.
    * **Legacy** (no events.jsonl, or empty): synthesize v: 0 events
      from ``owners.jsonl``/``documents.jsonl``/``links.jsonl`` (the
      Phase 1 path).

    Steps in either mode:
      1. Resolve ``catalog_path()`` and require an initialized catalog.
      2. Open ``.catalog.db`` read-only (sqlite URI ``mode=ro``) for the
         live snapshot. Snapshot owners + documents + links rows.
      3. Build a fresh ``CatalogDB`` under a TemporaryDirectory; drive
         ``Projector.apply_all`` over the chosen event stream into it.
         Snapshot the same three tables.
      4. Diff the snapshots. Report counts and the first 5 mismatches per
         table. Pass = every table identical; fail = any difference.

    The live ``.catalog.db`` is opened read-only so an operator running
    this verb on a working host cannot accidentally corrupt the cached
    SQLite. JSONL / events.jsonl files are read but not written. The
    projected SQLite is ephemeral and discarded with the
    TemporaryDirectory.
    """
    import sqlite3
    import tempfile
    from contextlib import closing

    from nexus.catalog.catalog import Catalog
    from nexus.catalog.catalog_db import CatalogDB
    from nexus.catalog.projector import Projector
    from nexus.catalog.synthesizer import synthesize_from_jsonl
    from nexus.config import catalog_path

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )

    live_db_path = cat_dir / ".catalog.db"
    if not live_db_path.exists():
        raise click.ClickException(
            f"Catalog SQLite missing at {live_db_path}; run 'nx catalog "
            "pull' to rebuild from JSONL."
        )

    # ── Pick the event source ──────────────────────────────────────────
    # Stat-only check; never construct ``EventLog`` here because its
    # constructor touch-creates ``events.jsonl`` if missing, which
    # would (a) shift the catalog dir's mtime mid-doctor-run and (b)
    # break the doctor's "read-only against live state" guarantee for a
    # legacy-mode catalog that has no events.jsonl yet.
    events_path = cat_dir / "events.jsonl"
    event_source: str
    if events_path.exists() and events_path.stat().st_size > 0:
        event_source = "events.jsonl"
    else:
        event_source = "synthesized"

    # Round-4 review (reviewer B EC-4): when ``NEXUS_EVENT_LOG_SHADOW=1``
    # but ``NEXUS_EVENT_SOURCED`` is unset, events.jsonl is being
    # shadow-emitted (subset of mutations, post-commit) — it is NOT
    # the canonical source of truth. A doctor replay against a
    # shadow-only log will report bogus divergence (legacy bootstrap
    # rows are absent from the log). Surface that explicitly so an
    # operator reading a FAIL report does not waste time hunting a
    # projector bug.
    shadow_only = (
        event_source == "events.jsonl"
        and os.environ.get("NEXUS_EVENT_LOG_SHADOW", "").strip().lower() in ("1", "true", "yes", "on")
        and os.environ.get("NEXUS_EVENT_SOURCED", "").strip().lower() not in ("1", "true", "yes", "on")
    )

    # ── Snapshot live ──────────────────────────────────────────────────
    # Links carry an autoincrement ``id`` PK that the projector restarts
    # at 1; the live db's ids depend on insertion history. RF-101-2 does
    # not claim the autoincrement is part of the projection contract, so
    # both snapshots exclude the ``id`` column by name (not by position
    # — a future schema migration that adds a column before ``id`` would
    # silently strip the wrong field under positional indexing).
    LINKS_EXCLUDE = ["id"]
    live_uri = f"file:{live_db_path}?mode=ro"
    with closing(sqlite3.connect(live_uri, uri=True)) as live_conn:
        live_snap = {
            "owners": _snapshot_table(live_conn, "owners"),
            "documents": _snapshot_table(live_conn, "documents"),
            "links": _snapshot_table(live_conn, "links", exclude_cols=LINKS_EXCLUDE),
            # RDR-101 Phase 6 prophylactic-review fix: include the
            # collections projection in replay-equality. Pre-fix this
            # gate was blind to Phase 6's new projection state.
            "collections": _snapshot_table(live_conn, "collections"),
        }

    # ── Project + snapshot ────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        projected_path = Path(tmpdir) / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            if event_source == "events.jsonl":
                # Local import: deferring to call-time avoids module-load
                # side effects in environments that never need this path.
                from nexus.catalog.event_log import EventLog
                applied = Projector(proj_db).apply_all(
                    EventLog(cat_dir).replay()
                )
            else:
                applied = Projector(proj_db).apply_all(
                    synthesize_from_jsonl(cat_dir)
                )
        finally:
            proj_db.close()

        with closing(sqlite3.connect(str(projected_path))) as proj_conn:
            projected_snap = {
                "owners": _snapshot_table(proj_conn, "owners"),
                "documents": _snapshot_table(proj_conn, "documents"),
                "links": _snapshot_table(proj_conn, "links", exclude_cols=LINKS_EXCLUDE),
                "collections": _snapshot_table(proj_conn, "collections"),
            }

    # ── Diff ──────────────────────────────────────────────────────────
    table_diffs: dict[str, dict] = {}
    overall_pass = True
    for table in ("owners", "documents", "links", "collections"):
        live_rows = live_snap[table]
        proj_rows = projected_snap[table]
        live_set = set(live_rows)
        proj_set = set(proj_rows)
        only_live = sorted(live_set - proj_set)
        only_proj = sorted(proj_set - live_set)
        equal = not only_live and not only_proj
        table_diffs[table] = {
            "live_count": len(live_rows),
            "projected_count": len(proj_rows),
            "only_in_live": [list(r) for r in only_live[:5]],
            "only_in_projected": [list(r) for r in only_proj[:5]],
            "equal": equal,
        }
        if not equal:
            overall_pass = False

    return {
        "pass": overall_pass,
        "events_applied": applied,
        "catalog_dir": str(cat_dir),
        "live_db": str(live_db_path),
        "event_source": event_source,
        "shadow_only": shadow_only,
        "tables": table_diffs,
    }


def _snapshot_table(
    conn, table: str, *, exclude_cols: list[str] | None = None,
) -> list[tuple]:
    """Snapshot one catalog table in deterministic row order.

    Sort by every column so the comparison is independent of insertion
    order. ``documents.metadata`` and ``links.metadata`` are JSON blobs
    that round-trip as strings, which sort byte-wise.

    ``exclude_cols`` removes named columns from both the SELECT and the
    ORDER BY. Used by the doctor's links snapshot to exclude the
    autoincrement ``id`` column without a fragile positional slice.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    if not cols:
        return []
    if exclude_cols:
        exclude = set(exclude_cols)
        cols = [c for c in cols if c not in exclude]
        if not cols:
            return []
    sort_cols = ", ".join(cols)
    rows = conn.execute(
        f"SELECT {sort_cols} FROM {table} ORDER BY {sort_cols}"
    ).fetchall()
    return rows


def _print_replay_equality_text(report: dict) -> None:
    """Operator-friendly text rendering of the replay-equality report."""
    click.echo(f"Catalog: {report['catalog_dir']}")
    click.echo(f"Live db: {report['live_db']}")
    click.echo(f"Event source: {report.get('event_source', 'synthesized')}")
    if report.get("shadow_only"):
        click.echo(
            "WARNING: events.jsonl is shadow-emitted "
            "(NEXUS_EVENT_LOG_SHADOW=1, NEXUS_EVENT_SOURCED unset). "
            "Divergence below may reflect missing bootstrap events, "
            "not a projector bug. The synthesize-log remediation verb "
            "was retired post Phase 5b (nexus-iftc); run with "
            "NEXUS_EVENT_SOURCED=1 to populate the log naturally."
        )
    click.echo(f"Events applied: {report['events_applied']}")
    click.echo("")

    for table, diff in report["tables"].items():
        marker = "✓" if diff["equal"] else "✗"
        click.echo(
            f"  {marker} {table:<10}  live={diff['live_count']:>6}  "
            f"projected={diff['projected_count']:>6}"
        )
        if not diff["equal"]:
            if diff["only_in_live"]:
                click.echo(
                    f"    only in live ({len(diff['only_in_live'])} sample"
                    + ("s" if len(diff["only_in_live"]) != 1 else "")
                    + "):"
                )
                for row in diff["only_in_live"]:
                    click.echo(f"      {row!r}")
            if diff["only_in_projected"]:
                click.echo(
                    f"    only in projected "
                    f"({len(diff['only_in_projected'])} sample"
                    + ("s" if len(diff["only_in_projected"]) != 1 else "")
                    + "):"
                )
                for row in diff["only_in_projected"]:
                    click.echo(f"      {row!r}")

    click.echo("")
    if report["pass"]:
        click.echo("PASS — projector replay matches live SQLite for the current catalog state.")
    else:
        click.echo("FAIL — projector replay diverges from live SQLite. See diffs above.")


_PRUNE_DEPRECATED_KEYS: frozenset[str] = frozenset({
    # RDR-101 Phase 4 (.10.2 audit, Category B).
    "source_path",
    "git_branch",
    "git_commit_hash",
    "git_project_name",
    "git_remote_url",
    # RDR-101 Phase 5c (nexus-o6aa.13).
    "corpus",
    "store_type",
    "git_meta",
})


def _run_t3_doc_id_coverage(
    *, strict_not_in_t3: bool = False, progress: bool = False,
) -> dict:
    """Walk every T3 collection in events.jsonl and report doc_id coverage.

    Steps:
      1. Read events.jsonl. Build the expected doc_id per (coll_id, chunk_id)
         from ChunkIndexed events. Track orphans separately.
      2. For each collection, paginate col.get(limit=300, offset=...,
         include=["metadatas"]); compare each chunk's actual doc_id against
         the expected one.
      3. Report per-collection counts: total_chunks, with_doc_id,
         missing_doc_id, mismatched_doc_id, expected_orphans.
      4. PASS = every non-orphan event has a matching T3 chunk with the
         right doc_id, AND no T3 chunk lacks a doc_id outside the
         expected-orphan set.

    Read-only against T3 (col.get only, no col.update).
    """
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.event_log import EventLog
    from nexus.catalog import events as ev
    from nexus.config import catalog_path
    from nexus.db import make_t3

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )
    log = EventLog(cat_dir)
    if not log.path.exists() or log.path.stat().st_size == 0:
        raise click.ClickException(
            f"events.jsonl is empty at {log.path}. The synthesize-log "
            "migration verb that historically populated it was retired "
            "post Phase 5b (nexus-iftc); restore by deleting the "
            "catalog directory and re-running 'nx catalog setup' to "
            "bootstrap from current T3 state."
        )

    # nexus-wszt: bypass-schema collections (taxonomy__*) carry their
    # own metadata vocabulary and intentionally have no doc_id (they
    # are BERTopic centroids / embedding anchors, not document chunks).
    # The doc_id-coverage audit must skip them or it reports 100%
    # orphan ratio on every centroid set (false positive class).
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415

    # Build expected (coll_id, chunk_id) → doc_id; track orphans.
    # RDR-102 D3: also track every coll_id that appears in events.jsonl
    # (whether non-orphan or orphan-only) so the orphan-ratio surface
    # can report on collections that don't appear in ``expected``.
    expected: dict[str, dict[str, str]] = {}
    expected_orphans: dict[str, set[str]] = {}
    all_event_collections: set[str] = set()
    for event in log.replay():
        if event.type != ev.TYPE_CHUNK_INDEXED:
            continue
        coll = event.payload.coll_id
        if coll.startswith(_BYPASS_SCHEMA_PREFIXES):
            continue
        cid = event.payload.chunk_id
        all_event_collections.add(coll)
        if event.payload.synthesized_orphan:
            expected_orphans.setdefault(coll, set()).add(cid)
            continue
        expected.setdefault(coll, {})[cid] = event.payload.doc_id

    try:
        t3 = make_t3()
    except Exception as exc:
        raise click.ClickException(
            f"Failed to open T3 client: {exc}. Check ChromaDB credentials."
        )

    # nexus-esrl (RDR-108 Phase 4 review D-M3): the audit reads
    # ``meta.get("doc_id", "")`` from chunk metadata to compare
    # against the event-log expected value. RDR-108 Phase 3
    # (nexus-bdag) removed doc_id from chunk metadata; the read
    # returns "" for every Phase-3 chunk. Without manifest
    # resolution the audit unconditionally reports near-100%
    # ``missing_doc_id``, masking real coverage problems.
    cat = Catalog(cat_dir, cat_dir / ".catalog.db")

    # nexus-yrka: collections renamed via ``nx catalog rename-collection``
    # leave their old name in events.jsonl (events are append-only) but
    # the old T3 collection no longer exists. The catalog records the
    # rename via ``superseded_by``; skip those in T3 lookups instead of
    # reporting them as ``error: open: Collection X does not exist``
    # (which would flip overall_pass to false on every renamed coll).
    superseded_map: dict[str, str] = {}
    try:
        rows = cat._db.execute(
            "SELECT name, superseded_by FROM collections "
            "WHERE superseded_by != ''"
        ).fetchall()
        for row in rows:
            superseded_map[row[0]] = row[1]
    except Exception:
        pass

    per_coll: dict[str, dict] = {}
    overall_pass = True
    skipped_superseded = 0
    coll_count = len(expected)
    import time as _time

    for coll_idx, (coll_name, expected_chunks) in enumerate(
        expected.items(), start=1,
    ):
        if progress:
            click.echo(
                f"  [coverage {coll_idx}/{coll_count}] {coll_name}: "
                f"{len(expected_chunks)} expected chunks…",
                err=True,
            )
        if coll_name in superseded_map:
            per_coll[coll_name] = {
                "skipped": f"superseded_by={superseded_map[coll_name]}",
                "expected_chunks": len(expected_chunks),
            }
            skipped_superseded += 1
            continue
        _tc = _time.monotonic()
        try:
            col = t3._client.get_collection(name=coll_name)
        except Exception as exc:
            per_coll[coll_name] = {
                "error": f"open: {exc}",
                "expected_chunks": len(expected_chunks),
            }
            overall_pass = False
            continue

        total = 0
        with_doc_id = 0
        mismatched: list[dict] = []
        missing: list[str] = []
        seen: set[str] = set()
        offset = 0
        while True:
            try:
                page = col.get(
                    limit=300, offset=offset, include=["metadatas"],
                )
            except Exception as exc:
                per_coll[coll_name] = {
                    "error": f"get: {exc}",
                    "expected_chunks": len(expected_chunks),
                }
                overall_pass = False
                break
            ids = page.get("ids") or []
            metas = page.get("metadatas") or []
            if not ids:
                break
            # nexus-esrl: resolve actual doc_id via the catalog
            # manifest for this page's chashes when chunk metadata
            # lacks doc_id (Phase-3 chunks). One batched lookup per
            # page; the per-chunk resolution below tries metadata
            # first, falls through to the manifest map.
            page_chashes = [
                (m or {}).get("chunk_text_hash", "") for m in metas
            ]
            page_chashes_nonempty = [c for c in page_chashes if c]
            chash_to_doc_for_page: dict[str, str] = {}
            if page_chashes_nonempty:
                try:
                    by_chash = cat.docs_for_chashes(page_chashes_nonempty)
                except Exception:
                    by_chash = {}
                for c, doc_ids in by_chash.items():
                    if doc_ids:
                        chash_to_doc_for_page[c] = sorted(doc_ids)[0]
            for cid, meta in zip(ids, metas):
                meta = meta or {}
                total += 1
                seen.add(cid)
                actual = meta.get("doc_id", "") or ""
                # Manifest fallback when metadata lacks doc_id.
                if not actual:
                    chash = meta.get("chunk_text_hash", "")
                    if chash:
                        actual = chash_to_doc_for_page.get(chash, "")
                expected_doc_id = expected_chunks.get(cid, "")
                is_orphan = cid in expected_orphans.get(coll_name, set())
                if actual:
                    with_doc_id += 1
                    if expected_doc_id and actual != expected_doc_id:
                        mismatched.append({
                            "chunk_id": cid,
                            "actual": actual,
                            "expected": expected_doc_id,
                        })
                else:
                    if not is_orphan:
                        missing.append(cid)
            if len(ids) < 300:
                break
            offset += 300

        # Chunks the event log expected but T3 doesn't have. By default
        # this is a WARNING rather than a hard failure: the most common
        # cause is legitimate operational deletion of T3 chunks (re-
        # ingestion, pruning) without a corresponding event in the log,
        # which over time would make the doctor permanently red. Pass
        # ``--strict-not-in-t3`` to make it a hard failure (the contract
        # then becomes "the event log is the authoritative ledger and
        # T3 must match it exactly").
        not_in_t3 = sorted(set(expected_chunks) - seen)

        coverage = with_doc_id / total if total else 1.0
        # RDR-102 D3: per-collection orphan ratio = orphan_events /
        # (orphan_events + non_orphan_events). The denominator is the
        # event-log population for this collection, NOT total T3
        # chunks, because the surface is "what fraction of this
        # collection's catalog projection is orphan'd". A 0/0 case
        # (collection appears in no events at all) is unreachable
        # here — we're inside the `for coll_name in expected.items()`
        # loop so this branch always has at least one non-orphan event.
        n_orphans = len(expected_orphans.get(coll_name, set()))
        n_non_orphan = len(expected_chunks)
        orphan_ratio = (
            n_orphans / (n_orphans + n_non_orphan)
            if (n_orphans + n_non_orphan)
            else 0.0
        )
        pass_for_coll = (
            not mismatched
            and not missing
            and (not strict_not_in_t3 or not not_in_t3)
        )
        per_coll[coll_name] = {
            "total_chunks": total,
            "with_doc_id": with_doc_id,
            "expected_chunks": len(expected_chunks),
            "expected_orphans": n_orphans,
            "orphan_ratio": round(orphan_ratio, 4),
            "missing_doc_id_sample": missing[:5],
            "missing_doc_id_count": len(missing),
            "mismatched_doc_id_sample": mismatched[:5],
            "mismatched_doc_id_count": len(mismatched),
            "not_in_t3_sample": not_in_t3[:5],
            "not_in_t3_count": len(not_in_t3),
            "coverage": round(coverage, 4),
            "pass": pass_for_coll,
        }
        if not pass_for_coll:
            overall_pass = False
        if progress:
            elapsed = _time.monotonic() - _tc
            click.echo(
                f"  [coverage {coll_idx}/{coll_count}] {coll_name}: "
                f"{with_doc_id}/{total} covered "
                f"({coverage * 100:.1f}%) in {elapsed:.1f}s",
                err=True,
            )

    # RDR-102 D3: surface orphan-only collections (those that appear in
    # events.jsonl but have ZERO non-orphan ChunkIndexed events). They
    # would otherwise be invisible because the per-coll loop only
    # iterates ``expected`` (non-orphan-bearing collections). For the
    # operator dashboard, orphan-only collections should appear in
    # tables with orphan_ratio=1.0 and total_chunks=0 (no T3 inspection
    # since the verb's strict_not_in_t3 contract doesn't have non-
    # orphan events to anchor against).
    for coll_name in sorted(all_event_collections - set(expected)):
        n_orphans = len(expected_orphans.get(coll_name, set()))
        per_coll[coll_name] = {
            "total_chunks": 0,
            "with_doc_id": 0,
            "expected_chunks": 0,
            "expected_orphans": n_orphans,
            "orphan_ratio": 1.0,
            "missing_doc_id_sample": [],
            "missing_doc_id_count": 0,
            "mismatched_doc_id_sample": [],
            "mismatched_doc_id_count": 0,
            "not_in_t3_sample": [],
            "not_in_t3_count": 0,
            "coverage": 1.0,
            "pass": True,  # nothing to fail on — all events are orphan
        }

    # Global orphan ratio across every event in the log.
    total_orphan_events = sum(len(s) for s in expected_orphans.values())
    total_non_orphan_events = sum(len(d) for d in expected.values())
    total_events = total_orphan_events + total_non_orphan_events
    global_orphan_ratio = (
        total_orphan_events / total_events if total_events else 0.0
    )

    return {
        "pass": overall_pass,
        "events_path": str(log.path),
        "collections_in_log": len(expected),
        "collections_in_log_total": len(all_event_collections),
        "orphan_ratio": round(global_orphan_ratio, 4),
        "strict_not_in_t3": strict_not_in_t3,
        "skipped_superseded": skipped_superseded,
        "tables": per_coll,
    }


_ORPHAN_RATIO_WARN_THRESHOLD = 0.50


def _print_t3_doc_id_coverage_text(report: dict) -> None:
    click.echo("=== T3 doc_id coverage ===")
    click.echo(f"Events path:        {report['events_path']}")
    # RDR-102 D3: clarified header. The original "Collections in log: N"
    # was the count of collections with at least one non-orphan
    # ChunkIndexed event (the slice the PASS gate sees), not the count
    # of distinct coll_id values in events.jsonl. On the host catalog
    # the numbers diverge ~30x (23 vs 783), and operators reading the
    # output would silently believe most collections were covered.
    in_log_total = report.get(
        "collections_in_log_total", report["collections_in_log"],
    )
    click.echo(
        f"Collections with non-orphan ChunkIndexed events: "
        f"{report['collections_in_log']} "
        f"(total in events.jsonl: {in_log_total})"
    )
    skipped = report.get("skipped_superseded", 0)
    if skipped:
        click.echo(f"Skipped (superseded): {skipped}")
    click.echo("")
    for coll_name, diff in report["tables"].items():
        if "skipped" in diff:
            click.echo(
                f"  - {coll_name:<40}  SKIPPED: {diff['skipped']} "
                f"(expected {diff['expected_chunks']} chunks)"
            )
            continue
        if "error" in diff:
            click.echo(
                f"  ✗ {coll_name:<40}  ERROR: {diff['error']} "
                f"(expected {diff['expected_chunks']} chunks)"
            )
            continue
        marker = "✓" if diff["pass"] else "✗"
        click.echo(
            f"  {marker} {coll_name:<40}  "
            f"total={diff['total_chunks']:>6}  "
            f"with_doc_id={diff['with_doc_id']:>6}  "
            f"coverage={diff['coverage']:.2%}"
        )
        if diff["mismatched_doc_id_count"]:
            click.echo(
                f"     mismatched: {diff['mismatched_doc_id_count']} "
                f"(first {len(diff['mismatched_doc_id_sample'])} shown)"
            )
            for m in diff["mismatched_doc_id_sample"]:
                click.echo(
                    f"       {m['chunk_id']}: actual={m['actual']!r} "
                    f"expected={m['expected']!r}"
                )
        if diff["missing_doc_id_count"]:
            click.echo(
                f"     missing doc_id: {diff['missing_doc_id_count']} "
                f"(first {len(diff['missing_doc_id_sample'])} shown): "
                f"{diff['missing_doc_id_sample']}"
            )
        if diff["not_in_t3_count"]:
            click.echo(
                f"     in event log but not in T3: {diff['not_in_t3_count']} "
                f"(first {len(diff['not_in_t3_sample'])} shown): "
                f"{diff['not_in_t3_sample']}"
            )
    click.echo("")
    # RDR-102 D3: orphan-ratio surface. PASS gate stays unchanged
    # (per A4 — tightening would invalidate the host catalog's current
    # PASS); orphan ratio is a SOFT signal alongside the gate. Any
    # collection above the WARN threshold prints a WARN line; the
    # global ratio prints regardless so operators see the headline.
    click.echo("=== Orphan ratio ===")
    global_ratio = report.get("orphan_ratio", 0.0)
    click.echo(f"Global: {global_ratio:.2%}")
    warn_lines: list[str] = []
    for coll_name, diff in report["tables"].items():
        if "error" in diff:
            continue
        ratio = diff.get("orphan_ratio", 0.0)
        if ratio > _ORPHAN_RATIO_WARN_THRESHOLD:
            warn_lines.append(
                f"  WARN: {coll_name:<40}  orphan_ratio={ratio:.2%}  "
                f"(orphans={diff['expected_orphans']}, non-orphans="
                f"{diff['expected_chunks']})"
            )
    if warn_lines:
        for line in warn_lines:
            click.echo(line)
        click.echo(
            "  The synthesize-log and t3-backfill-doc-id remediation "
            "verbs were retired post Phase 5b (nexus-iftc). Re-index the "
            "affected collections to repopulate orphan chunks with "
            "current doc_id metadata; see docs/migration/"
            "rdr-101-phase4-orphan-recovery.md for historical context."
        )
    click.echo("")
    if report["pass"]:
        click.echo("PASS — every non-orphan chunk carries the expected doc_id.")
    else:
        click.echo("FAIL — T3 doc_id metadata diverges from the event log.")
        # Post-iftc (RDR-101 Phase 5b irreversibility): the migrate /
        # synthesize-log / t3-backfill-doc-id verbs are gone. A FAIL
        # today means the catalog holds pre-Phase-4 state; restore by
        # bootstrapping a fresh catalog from current T3.
        click.echo("")
        click.echo("Next step:")
        click.echo(
            "  Delete the catalog directory and re-run 'nx catalog setup' "
            "to bootstrap a fresh event log from current T3 state."
        )
        click.echo(
            "See docs/rdr/post-mortem/101-event-sourced-catalog-migration.md "
            "for the arc record (verbs retired post Phase 5b)."
        )


# ── Backup-before-delete recovery surface (RDR-106 Option A) ─────────────


@catalog.command("list-backups")
def list_backups_cmd() -> None:
    """List backup snapshots written by destructive catalog verbs.

    Each destructive catalog verb (``delete``, ``gc``, ``prune-stale``,
    ``link-bulk-delete``) writes a JSONL snapshot of the rows about
    to be deleted under ``$NEXUS_CONFIG_DIR/catalog/.deleted-backups/``
    BEFORE the actual delete. This verb shows what's recoverable
    without inspecting the files manually.
    """
    from nexus.catalog.catalog_backup import list_backups
    cat = _get_catalog()
    records = list_backups(cat)
    if not records:
        click.echo("No backups found.")
        return
    click.echo(f"{len(records)} backup(s) (newest first):\n")
    for rec in records:
        click.echo(
            f"  {rec.path.name}\n"
            f"    verb={rec.verb}  ts={rec.timestamp}  "
            f"rows={rec.rows_count}\n"
            f"    reason={rec.reason or '<none>'}"
        )


@catalog.command("undelete")
@click.argument("backup")
def undelete_cmd(backup: str) -> None:
    """Restore documents (and their links) from a backup snapshot.

    BACKUP is either a filename inside ``.deleted-backups/`` or an
    absolute path. Documents are re-emitted as DocumentRegistered
    events in events.jsonl (event-sourced; full audit trail);
    inbound and outbound links are re-emitted as LinkCreated events
    via ``link_if_absent`` (idempotent).

    Documents are restored with their ORIGINAL tumblers — the tumbler
    minting path is bypassed. Re-running this on an already-restored
    backup is a no-op (DocumentRegistered on existing tumbler is
    idempotent via INSERT OR REPLACE).
    """
    from nexus.catalog.catalog_backup import restore_documents
    cat = _get_catalog()
    if backup.startswith("/"):
        backup_path = Path(backup)
    else:
        backup_path = cat._dir / ".deleted-backups" / backup
    if not backup_path.exists():
        raise click.ClickException(f"Backup not found: {backup_path}")

    docs, links = restore_documents(cat, backup_path)
    click.echo(
        f"Restored {docs} document(s) and {links} link(s) "
        f"from {backup_path.name}."
    )


@catalog.command("vacuum-backups")
@click.option(
    "--older-than-days", default=30, show_default=True,
    help="Drop backup files older than this many days.",
)
@click.option(
    "--dry-run/--no-dry-run", default=True,
    help="Report-only (default). Use --no-dry-run to delete.",
)
def vacuum_backups_cmd(older_than_days: int, dry_run: bool) -> None:
    """Drop old backup snapshots past the retention window.

    Default retention is 30 days. Removed files are gone for good —
    after vacuum, the rows in those backups are no longer recoverable
    via ``nx catalog undelete``.
    """
    from nexus.catalog.catalog_backup import vacuum_old_backups
    cat = _get_catalog()
    removed, kept = vacuum_old_backups(
        cat, older_than_days=older_than_days, dry_run=dry_run,
    )
    if dry_run:
        click.echo(
            f"Would remove {removed} backup file(s) "
            f"(keeping {kept}). "
            f"Run with --no-dry-run to actually delete."
        )
    else:
        click.echo(
            f"Removed {removed} backup file(s); kept {kept}."
        )


@catalog.command("chash-reconcile")
@click.option(
    "--apply",
    is_flag=True,
    default=False,
    help="Actually delete ghost rows. Without this flag the command "
    "is a dry-run report only.",
)
def chash_reconcile_cmd(apply: bool) -> None:
    """Sweep stale ``chash_index`` rows pointing at deleted T3 collections.

    \b
    The T2 ``chash_index`` is the routing table that resolves
    ``chash:<hex>`` link spans to the (collection, chunk) they live
    in. Rows accumulate over time: when a collection is deleted from
    T3 (``nx collection delete`` or operator-driven cleanup), the
    chash_index rows for that collection are NOT cascaded today, so
    they remain as ghosts pointing at a non-existent collection.

    \b
    ``Catalog.resolve_chash`` self-heals on access (drops a stale row
    when it tries to look up a chash and finds the collection
    missing in T3), but that's a per-access correction, not a sweep.
    This verb is the bulk equivalent.

    \b
    Default is dry-run: reports per-collection ghost counts without
    writing. Pass ``--apply`` to actually delete.

    \b
    Examples:
      nx catalog chash-reconcile         # dry-run report
      nx catalog chash-reconcile --apply # actually delete

    \b
    Filed under nexus-w9vq (RDR-108 Phase 5 follow-up).
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db import make_t3
    from nexus.db.t2.chash_index import ChashIndex

    db_path = default_db_path()
    if not db_path.exists():
        click.echo(
            f"No T2 db at {db_path}. Nothing to reconcile.",
            err=True,
        )
        raise SystemExit(1)

    try:
        t3 = make_t3()
        # nexus-l1yt: chromadb's list_collections shape varies by
        # backend version (Collection objects vs string names).
        # Every other call site in nexus uses the same defensive
        # ``isinstance(c, str)`` guard; without it this verb crashes
        # with AttributeError on the string-returning versions.
        live_collections = {
            (c if isinstance(c, str) else c.name)
            for c in t3._client.list_collections()
        }
    except Exception as exc:
        click.echo(f"Failed to list T3 collections: {exc}", err=True)
        raise SystemExit(1)

    idx = ChashIndex(db_path)
    try:
        indexed_collections = idx.distinct_collections()
        ghost_collections = sorted(indexed_collections - live_collections)
        live_in_index = indexed_collections & live_collections
        unindexed_in_t3 = sorted(live_collections - indexed_collections)

        if not ghost_collections:
            click.echo(
                f"chash_index: {len(indexed_collections)} distinct "
                f"collection(s); 0 ghost(s). Nothing to reconcile."
            )
            if unindexed_in_t3:
                click.echo(
                    f"  Note: {len(unindexed_in_t3)} T3 collection(s) "
                    f"have no chash_index rows (likely empty or never "
                    f"backfilled)."
                )
            return

        # Per-collection ghost row counts (read-only).
        ghost_counts: list[tuple[str, int]] = []
        for coll_name in ghost_collections:
            n = idx.count_for_collection(coll_name)
            ghost_counts.append((coll_name, n))
        total_ghost_rows = sum(n for _, n in ghost_counts)

        verb = "would delete" if not apply else "deleted"
        click.echo(
            f"chash_index: {len(indexed_collections)} distinct "
            f"collection(s); {len(ghost_collections)} ghost(s) "
            f"({total_ghost_rows} row(s) total)"
        )
        click.echo(f"  live (in both T3 and index): {len(live_in_index)}")
        if unindexed_in_t3:
            click.echo(
                f"  unindexed (in T3 but not index): {len(unindexed_in_t3)}"
            )

        # Per-ghost-collection breakdown (capped at 20 to keep output sane).
        for coll_name, n in ghost_counts[:20]:
            click.echo(f"  {verb} {n:>6} row(s) from ghost: {coll_name}")
        if len(ghost_counts) > 20:
            click.echo(f"  ... and {len(ghost_counts) - 20} more ghost collection(s)")

        if apply:
            actually_deleted = 0
            for coll_name in ghost_collections:
                actually_deleted += idx.delete_collection(coll_name)
            click.echo(
                f"\nSummary: deleted {actually_deleted} row(s) across "
                f"{len(ghost_collections)} ghost collection(s)."
            )
        else:
            click.echo(
                f"\nSummary: would delete {total_ghost_rows} row(s) "
                f"across {len(ghost_collections)} ghost collection(s). "
                f"Re-run with --apply to actually delete."
            )
    finally:
        idx.close()


# ── nx catalog collection-gc (nexus-ks40) ────────────────────────────────


@catalog.command("collection-gc")
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
    from nexus.db import make_t3
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES

    cat = _get_catalog()
    try:
        t3_db = make_t3()
        t3_collections = t3_db.list_collections()
    except Exception as exc:
        click.echo(f"Failed to list T3 collections: {exc}", err=True)
        raise SystemExit(1)

    # nexus-pz24 (RDR-108 Phase 4 review CR-M2): mirror the T3 path's
    # error-handling shape so a SQLite failure (locked DB, schema
    # mismatch, FS issue) surfaces a clean operator message rather
    # than a raw Python traceback.
    try:
        projection_names = {r["name"] for r in cat.list_collections()}
        doc_collection_rows = cat._db.execute(
            "SELECT DISTINCT physical_collection FROM documents "
            "WHERE physical_collection != ''"
        ).fetchall()
    except Exception as exc:
        click.echo(f"Failed to query catalog: {exc}", err=True)
        raise SystemExit(1)
    doc_collection_names = {r[0] for r in doc_collection_rows if r[0]}

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
            except Exception as exc:
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


# ── Catalog t3-doc-id-coverage support helpers ───────────────────────────



# ── nx catalog orphan-backfill (nexus-h2pm / nexus-4fw8 / nexus-oa9k) ──────


@catalog.group("orphan-backfill")
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
    from nexus.catalog.orphan_backfill import (  # noqa: PLC0415
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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    cat = _get_catalog()
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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    cat = _get_catalog()
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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415
    from nexus.config import nexus_config_dir  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    if not owner:
        owner = _get_owner_for(collection)
    owner_tumbler = Tumbler.parse(owner)
    cat = _get_catalog()
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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415
    from nexus.db import make_t3  # noqa: PLC0415

    cat = _get_catalog()
    t3 = make_t3()

    if dry_run:
        # Dry-run only: count without writing.
        rows = cat._db.execute(
            "SELECT COUNT(*) FROM documents "
            "WHERE physical_collection = ? AND title != ''"
            if match_by == "title" else
            "SELECT COUNT(*) FROM documents "
            "WHERE physical_collection = ? AND head_hash != ''",
            (collection,),
        ).fetchone()
        click.echo(
            f"Existing catalog Documents with {match_by}: {rows[0]}"
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
