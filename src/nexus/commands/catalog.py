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
       symlink back into ``conexus/plans``).
    2. **Repo-root relative** — ``<repo_root>/nx``. Works when the
       caller runs ``nx catalog setup`` from a nexus checkout.
    3. **Legacy ``__file__``-relative walk** — four levels up from
       this module plus ``/nx``. Retained for unusual install
       layouts that neither of the above covers.

    If none of the three resolves, returns the resource candidate so
    the caller's fail-loud guard surfaces a helpful error naming the
    package-data location.
    """
    from importlib.resources import as_file, files  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
        repo_root / "conexus",
        Path(__file__).resolve().parent.parent.parent.parent / "conexus",
    ])
    for candidate in candidates:
        if (candidate / "plans" / "builtin").is_dir():
            return candidate
    return candidates[0]


def _seed_plan_templates() -> int:
    """Seed pre-built plan templates into T2. Idempotent — skips existing.

    All templates are shipped as YAML files under ``conexus/plans/builtin/``
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
    from pathlib import Path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    seeded = 0
    with T2Database(default_db_path()) as db:  # epsilon-allow: one-shot `nx catalog setup` plan-seed loader passes Plan dataclasses not in the daemon RPC wire allowlist; not a contention hot path (RDR-128 P3 documented-irreducible)
        from nexus.indexer_utils import find_repo_root  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        from nexus.plans.loader import load_all_tiers  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        repo_root = find_repo_root(Path.cwd()) or Path.cwd()
        # Plugin root resolution (RDR-092 nexus-b9f3). The conexus/ plan
        # YAMLs ship as wheel package data via hatch force-include
        # (see pyproject.toml), landing at
        # ``<site-packages>/nexus/_resources/plans/builtin/*.yml`` on
        # installed builds. Editable installs get the same path via
        # a ``src/nexus/_resources/plans`` symlink back into
        # ``conexus/plans``. Either way ``importlib.resources.files`` is
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
        # missing or empty ``conexus/plans/builtin`` is a deployment gap
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
                "the conexus plugin or run 'nx doctor --check-plan-library' "
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
    """Read-only catalog reader for the admin CLI (RDR-146 P1.2).

    The ``nx catalog`` read commands (list / show / links / search / ...)
    use this; write commands additionally open :func:`_get_catalog_writer`
    and route their mutations through the daemon.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' to create and populate it."
        )
    return cat


def _get_catalog_writer():
    """Write-only catalog proxy for the admin CLI (RDR-146 P1.2).

    Routes the whitelisted write ops through the T2 daemon (the single
    .catalog.db writer) when reachable, else a direct in-process Catalog.
    Callers ``.close()`` it when done.
    """
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    if not Catalog.is_initialized(catalog_path()):
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' to create and populate it."
        )
    return make_catalog_writer()


def _resolve_tumbler(cat: Catalog, value: str) -> Tumbler:
    """Resolve a tumbler string OR title/filename. Raises ClickException on failure."""
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
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
    catalog_link). Use /conexus:query for multi-step citation and provenance queries.
    """


# Command families carved out of this module live in catalog_cmds/ and attach
# themselves to the group via their register() hook (nexus-kgyoz). Imported
# here (after the group exists) so every carved command (`nx catalog owners`,
# `dedupe-owners`, `backfill-owner-id`, …) resolves identically. catalog_cmds
# submodules reference this module's helpers lazily, so these imports stay
# acyclic. Keep import order and register order aligned as families accumulate.
from nexus.commands.catalog_cmds import owners as _owners_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import backfill as _backfill_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import links as _links_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import backups as _backups_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import collections as _collections_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import migration as _migration_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import maintenance as _maintenance_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import remediation as _remediation_cmds  # noqa: E402 — must follow the `catalog` group definition above

_owners_cmds.register(catalog)
_backfill_cmds.register(catalog)
_links_cmds.register(catalog)
_backups_cmds.register(catalog)
_collections_cmds.register(catalog)
_migration_cmds.register(catalog)
_maintenance_cmds.register(catalog)
_remediation_cmds.register(catalog)


@catalog.command("init")
@click.option("--remote", default="", help="Optional git remote URL")
def init_cmd(remote: str) -> None:
    """Initialize catalog git repository."""
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    path = catalog_path()
    if not Catalog.is_initialized(path):
        Catalog.init(path, remote=remote or None)
        click.echo(f"Catalog initialized at {path}")
    else:
        click.echo(f"Catalog already initialized at {path}")

    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    cat = make_catalog_reader()
    writer = make_catalog_writer()

    try:
        registry = _make_registry()
        t3 = _make_t3()

        import signal  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        def _timeout_handler(signum, frame):
            raise TimeoutError("T3 cloud call timed out — try again later or check connectivity")

        repo_count = paper_count = knowledge_count = 0

        click.echo("Populating from repos...")
        repo_count, repo_collections = _backfill_repos(cat, registry, dry_run=False, writer=writer)
        click.echo(f"  {repo_count} repo entries")

        # Paper and knowledge backfill query T3 cloud — timeout after 60s each
        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        try:
            click.echo("Populating from paper collections...")
            signal.alarm(60)
            paper_count = _backfill_papers(cat, t3, dry_run=False, repo_collections=repo_collections, writer=writer)
            signal.alarm(0)
            click.echo(f"  {paper_count} paper entries")

            click.echo("Populating from knowledge collections...")
            signal.alarm(30)
            knowledge_count = _backfill_knowledge(cat, t3, dry_run=False, writer=writer)
            signal.alarm(0)
            click.echo(f"  {knowledge_count} knowledge entries")

            click.echo("Populating from RDR collections...")
            signal.alarm(30)
            rdr_count = _backfill_rdrs(cat, t3, dry_run=False, writer=writer)
            signal.alarm(0)
            click.echo(f"  {rdr_count} RDR entries")
        except TimeoutError as exc:
            signal.alarm(0)
            click.echo(f"  Timed out ({exc}). Partial results saved — rerun setup to continue.")
        finally:
            signal.signal(signal.SIGALRM, old_handler)
    except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        click.echo(f"  Backfill incomplete ({type(exc).__name__}: {exc})")

    click.echo("Backfilling chunk_text_hash...")
    from nexus.commands.collection import _backfill_chunk_text_hash  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    hash_updated = 0
    try:
        for col_info in t3.list_collections():
            col = t3._client.get_collection(col_info["name"])
            updated, _, _ = _backfill_chunk_text_hash(col)
            hash_updated += updated
    except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        click.echo(f"  Hash backfill partial ({type(exc).__name__}: {exc})")
    click.echo(f"  {hash_updated} chunks updated")

    click.echo("Generating links...")
    from nexus.catalog.link_generator import generate_citation_links  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    cites = generate_citation_links(cat, writer=writer)
    click.echo(f"  Citations: {cites}")
    writer.close()

    click.echo("Seeding plan templates...")
    seeded = _seed_plan_templates()
    click.echo(f"  {seeded} templates seeded")

    # Check if a remote is configured for durability
    import subprocess  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
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
    from nexus.catalog.catalog import make_relative  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat = _get_catalog()
    writer = _get_catalog_writer()
    # Relativize absolute file_path if under a known repo (RDR-060).
    # RDR-137 Phase 3.3 (nexus-tts0d.8): catalog-backed enumeration.
    fp = file_path
    if fp and Path(fp).is_absolute():
        from nexus.catalog.catalog import _default_registry_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
        from nexus.repos import list_repos_dual  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

        reg_path = _default_registry_path()
        for repo_path_str in list_repos_dual(
            cat=cat, registry_path=reg_path,
        ):
            rel = make_relative(fp, Path(repo_path_str))
            if rel != fp:
                fp = rel
                break

    try:
        tumbler = writer.register(
            Tumbler.parse(owner), title,
            content_type=content_type, file_path=fp,
            corpus=corpus, author=author, year=year,
            source_uri=source_uri,
        )
    except ValueError as exc:
        # P3.1 register-boundary validation surfaced a malformed URI.
        # Hard error rather than silent persistence.
        raise click.ClickException(str(exc)) from exc
    finally:
        writer.close()
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
    writer = _get_catalog_writer()
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
                writer.update(entry.tumbler, **fields)
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
        writer.update(t, **fields)
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
    writer = _get_catalog_writer()
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
    from nexus.catalog.catalog_backup import snapshot_documents  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    backup_path = snapshot_documents(
        cat, [str(t)], verb="delete",
        reason=f"single-document delete: {entry.title}",
    )
    if backup_path:
        click.echo(
            f"Backup snapshot: {backup_path.name}"
            f"  (restore: nx catalog undelete {backup_path.name})"
        )

    deleted = writer.delete_document(t)
    if deleted:
        click.echo(f"Deleted: {t} ({entry.title}). Links preserved.")
    else:
        click.echo(f"Not found: {t}")


@catalog.command("sync")
@click.option("--message", "-m", default="catalog update")
def sync_cmd(message: str) -> None:
    """Commit and push catalog changes."""
    cat = _get_catalog_writer()
    try:
        cat.sync(message)
    finally:
        cat.close()
    click.echo("Catalog synced.")


@catalog.command("pull")
def pull_cmd() -> None:
    """Pull catalog from remote and rebuild SQLite."""
    cat = _get_catalog_writer()
    try:
        cat.pull()
    finally:
        cat.close()
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    try:
        db_path = default_db_path()
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return None
    if not db_path.exists():
        return None

    try:
        with T2Database(db_path) as db:  # epsilon-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
            # nexus-pyzk7: service-backed taxonomy has no raw .conn. This is a
            # read-only topic-stats display; skip cleanly in service mode rather
            # than relying on the AttributeError → except below (the docstring
            # promised a graceful skip the bare access did not deliver).
            if not hasattr(db.taxonomy, "conn"):
                return None
            conn = db.taxonomy.conn  # epsilon-allow: guarded by hasattr(db.taxonomy,'conn') skip above (nexus-pyzk7 Part 3)
            with db.taxonomy._lock:  # epsilon-allow: guarded by hasattr(db.taxonomy,'conn') skip above (nexus-pyzk7 Part 3)
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
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
    # nexus-xnz0o: stats() is uniform across SQLite and service mode.
    s = cat.stats()
    owner_count = s.get("owner_count", 0)
    doc_count = s.get("doc_count", 0)
    link_count = s.get("link_count", 0)
    chunk_count = s.get("chunk_count", 0)  # nexus-aeceu: now present on both backends
    # by_content_type is included in the stats() response (nexus-xnz0o).
    type_counts = s.get("by_content_type", {})
    link_type_counts = s.get("links_by_type", {})
    tax = _taxonomy_stats()
    if as_json:
        payload: dict = {
            "owners": owner_count,
            "documents": doc_count,
            "links": link_count,
            "chunks": chunk_count,
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
        click.echo(f"Chunks:    {chunk_count}")
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
    cat = _get_catalog_writer()
    try:
        removed = cat.compact()
    finally:
        cat.close()
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
    entries = cat.list_by_collection(collection)
    rows = [(str(e.tumbler), e.source_uri or "") for e in entries]

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

    writer = _get_catalog_writer()
    deleted = 0
    for t_str in purge_targets:
        try:
            t = Tumbler.parse(t_str)
        except Exception as e:  # noqa: BLE001 — best-effort per-item; logged and skipped, must not abort batch
            click.echo(f"  skip {t_str}: parse error {e}")
            continue
        if writer.delete_document(t):
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
    # nexus-xnz0o: replaced _db.execute with uniform catalog API.
    # all_documents() paginates through the full catalog 200 at a time.
    all_docs: list = []
    offset = 0
    while True:
        page = cat.all_documents(limit=200, offset=offset)
        if not page:
            break
        all_docs.extend(page)
        if len(page) < 200:
            break
        offset += 200
    rows = [
        (e.physical_collection, e.source_uri or "", str(e.tumbler))
        for e in all_docs
        if e.physical_collection
    ]

    owner_list = cat.list_owners()
    owners_by_prefix: dict[str, dict[str, str]] = {
        o["tumbler_prefix"]: {
            "owner_type": o.get("owner_type") or "",
            "repo_root":  o.get("repo_root") or "",
        }
        for o in owner_list
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
        except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
    from urllib.parse import urlparse  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    # nexus-xnz0o: orphaned_docs() is uniform across SQLite and service mode.
    orphans = cat.orphaned_docs()
    rows = [(d["tumbler"], d["title"], d["content_type"], d["file_path"]) for d in orphans]

    if not rows:
        click.echo("No orphan entries (all documents have at least one link).")
        return

    click.echo(f"Orphan entries ({len(rows)} with no links):")
    for tumbler, title, content_type, file_path in sorted(rows, key=lambda r: (r[2], r[0])):
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
    import json as _json  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    cat = _get_catalog()
    # nexus-xnz0o: replaced raw SQL with catalog API.
    # Fetch docs for a single collection or all distinct collections.
    if collection:
        coll_entries = cat.list_by_collection(collection)
        all_entries = [(e, collection) for e in coll_entries]
    else:
        # Paginate through all documents.
        all_entries = []
        offset = 0
        while True:
            page = cat.all_documents(limit=200, offset=offset)
            if not page:
                break
            for e in page:
                if e.physical_collection and not e.alias_of:
                    all_entries.append((e, e.physical_collection))
            if len(page) < 200:
                break
            offset += 200

    # Build rows: (tumbler, title, physical_collection, doc_id)
    # Only entries with a non-empty meta.doc_id are verifiable; entries
    # without doc_id are silently skipped (same semantics as original SQL
    # ``WHERE metadata->>'doc_id' != ''``).
    rows = [
        (str(e.tumbler), e.title, coll, e.meta.get("doc_id"))
        for e, coll in all_entries
        if not e.alias_of and coll and e.meta.get("doc_id")
    ]

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

    writer = _get_catalog_writer()
    try:
        _heal_ghosts(cat, ghosts_by_collection, writer=writer)
    finally:
        writer.close()


def _heal_ghosts(
    cat: Catalog,
    ghosts_by_collection: dict[str, list[dict]],
    *,
    writer: object = None,
) -> None:
    """Interactive heal loop for `nx catalog verify --heal`.

    Per ghost, prompt for one of:
      d  drop the tumbler (catalog.delete_document)
      p  print the `nx store put` invocation that would repopulate it
      s  skip
      q  quit the heal loop
    """
    w = writer if writer is not None else cat
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
                if w.delete_document(Tumbler.parse(g["tumbler"])):
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


@catalog.command("session-summary")
@click.option("--since", default=24, type=int, help="Hours to look back for git changes")
def session_summary_cmd(since: int) -> None:
    """Show link graph summary for recently modified files.

    \b
    Examples:
      nx catalog session-summary            # files modified in last 24 hours
      nx catalog session-summary --since 48 # last 48 hours
    """
    import subprocess  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
        click.echo("Could not determine recent file changes.")
        return

    if not files:
        click.echo(f"No files modified in the last {since} hours.")
    else:
        # nexus-xnz0o: replaced raw SQL with catalog API (uniform across backends).
        found_any = False
        for fp in sorted(files):
            entry = cat.find_by_file_path(fp)
            if not entry:
                continue
            tumbler = entry.tumbler
            # Collect titles of linked RDR documents (content_type == 'rdr').
            rdr_titles: list[str] = []
            for lnk in cat.links_from(tumbler):
                peer_t = getattr(lnk, "to_tumbler", None)
                if peer_t:
                    peer = cat.resolve(peer_t)
                    if peer and peer.content_type == "rdr":
                        rdr_titles.append(peer.title)
            for lnk in cat.links_to(tumbler):
                peer_t = getattr(lnk, "from_tumbler", None)
                if peer_t:
                    peer = cat.resolve(peer_t)
                    if peer and peer.content_type == "rdr":
                        rdr_titles.append(peer.title)
            rdr_titles = list(dict.fromkeys(rdr_titles))  # deduplicate order-preserving
            if rdr_titles:
                rdrs = ", ".join(rdr_titles)
                click.echo(f"  {fp} — {len(rdr_titles)} RDR(s): {rdrs}")
                found_any = True

        if not found_any:
            click.echo("No linked RDRs found for recently modified files.")

    total = cat.stats().get("link_count", 0)
    click.echo(f"\nLink graph: {total} links active.")


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
    rows = cat.coverage_by_content_type(owner_prefix)
    if not rows:
        click.echo("No documents in catalog.")
        return

    click.echo("Link coverage by content type:")
    for row in sorted(rows, key=lambda r: r["content_type"]):
        ct = row["content_type"] or "(none)"
        total = row["total"]
        linked = row["linked"]
        pct = (linked / total * 100) if total else 0.0
        click.echo(f"  {ct:<12} {linked:>4}/{total:<4} = {pct:5.1f}%")


# ── Backfill helpers ──────────────────────────────────────────────────────────


def _owner_by_name(cat: Catalog, name: str) -> Tumbler | None:
    """Look up a CURATOR owner by name.

    Filters on ``owner_type = 'curator'`` so a same-named REPO owner
    (e.g. a repo whose root path basename happens to be ``knowledge``
    or ``papers``) cannot silently shadow the intended curator. The
    namespaces are separate; repo owners are reachable only via
    ``Catalog.owner_for_repo(repo_hash)``.
    """
    # nexus-xnz0o: use curator_owner_tumbler_by_name() (portable API).
    prefix = cat.curator_owner_tumbler_by_name(name)
    return Tumbler.parse(prefix) if prefix else None


def _get_or_create_curator(cat: Catalog, name: str, *, writer: object = None) -> Tumbler:
    """Get or create a curator owner by name (reads via cat, writes via writer)."""
    owner = _owner_by_name(cat, name)
    if owner is None:
        owner = (writer if writer is not None else cat).register_owner(name, "curator")
    return owner


def _backfill_repos(
    cat: Catalog, registry: object, dry_run: bool, *, writer: object = None
) -> tuple[int, set[str]]:
    """Create owner per repo from registry.

    Returns (count, claimed_collections) — claimed_collections is the set of
    docs__* collection names owned by repos, so Pass 2 can exclude them.
    """
    from hashlib import sha256  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from pathlib import Path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    w = writer if writer is not None else cat
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
            owner = w.register_owner(
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
                w.register(
                    owner=owner, title=f"{repo_name} ({content_type})",
                    content_type=content_type,
                    physical_collection=col_name,
                    head_hash=head_hash,
                )
                count += 1

    if skipped:
        click.echo(f"  ({skipped} stale/missing repos skipped)")
    return count, claimed


def _backfill_knowledge(cat: Catalog, t3: object, dry_run: bool, *, writer: object = None) -> int:
    """Register knowledge__* collections in catalog."""
    w = writer if writer is not None else cat
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

        curator = _get_or_create_curator(cat, "knowledge", writer=w)
        # Idempotent: check by physical_collection
        existing = [e for e in cat.by_owner(curator) if e.physical_collection == col_name]
        if not existing:
            w.register(
                owner=curator, title=title, content_type="knowledge",
                physical_collection=col_name,
            )
        count += 1

    return count


def _backfill_rdrs(cat: Catalog, t3: object, dry_run: bool, *, writer: object = None) -> int:
    """Register rdr__* collections in catalog with per-document titles from T3 metadata."""
    w = writer if writer is not None else cat
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
                import hashlib  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

                from nexus.catalog.catalog import (  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
                    _default_registry_path,
                    make_relative,
                )
                from nexus.repos import list_repos_dual  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

                # RDR-137 Phase 3.3 (nexus-tts0d.8): catalog-backed
                # enumeration. Iterate every known repo_root, hash it,
                # match the trailing hash8 suffix on the collection
                # name; first match wins.
                reg_path = _default_registry_path()
                for repo_path_str in list_repos_dual(
                    cat=cat, registry_path=reg_path,
                ):
                    h = hashlib.sha256(
                        repo_path_str.encode(),
                    ).hexdigest()[:8]
                    if col_name.endswith(h):
                        repo_root = Path(repo_path_str)
                        owner = cat.owner_for_repo(h)
                        break
            except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
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
                    cat, col_name.replace("rdr__", ""), writer=w,
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
                    w.register(
                        owner=owner, title=title, content_type="rdr",
                        file_path=fp, physical_collection=col_name,
                    )
                    count += 1
        except Exception as exc:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
            click.echo(f"  warning: {col_name} — {exc}")
            _log.debug("backfill_rdrs_error", col=col_name, exc_info=True)

    return count


def _backfill_papers(
    cat: Catalog, t3: object, dry_run: bool, repo_collections: set[str] | None = None,
    *, writer: object = None,
) -> int:
    """Register docs__* paper collections, excluding repo-owned collections."""
    w = writer if writer is not None else cat
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
        except Exception:  # noqa: BLE001 — best-effort; error surfaced via log/echo, must not crash caller
            _log.debug("backfill_papers_metadata_error", col=col_name, exc_info=True)

        if dry_run:
            click.echo(f"  [dry-run] Would register paper: {title} → {col_name}")
            count += 1
            continue

        curator = _get_or_create_curator(cat, "papers", writer=w)
        existing = [e for e in cat.by_owner(curator) if e.physical_collection == col_name]
        if not existing:
            w.register(
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
    from nexus.catalog.consolidation import merge_corpus  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    if dry_run:
        result = merge_corpus(cat, None, corpus, dry_run=True)
        entries = cat.by_corpus(corpus)
        if not entries:
            raise click.ClickException(f"No entries with corpus={corpus!r}")
        # RDR-103 Phase 5: mirror the conformant target shape that
        # ``merge_corpus`` will use when run for real so the dry-run
        # message reports the same name.
        from nexus.corpus import effective_embedding_model_for_writes  # noqa: PLC0415  — command-local import (nexus.corpus)

        owner_segment = corpus.replace("_", "-")
        target = (
            f"docs__{owner_segment}__{effective_embedding_model_for_writes('docs')}__v1"
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


def _make_t3():
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    return make_t3()


def _make_registry():
    """RDR-137 Phase 5.3 (nexus-tts0d.20): tiny adapter exposing the
    two methods ``_backfill_repos`` consumes (``all_info``). Reads
    the legacy file shape with stdlib json so the catalog backfill
    verb keeps working without depending on the deleted RepoRegistry
    class.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.repos import _read_repos_json  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    class _LegacyRegistryReader:
        def __init__(self, path):
            self._path = path

        def all_info(self):
            return _read_repos_json(self._path)

    return _LegacyRegistryReader(nexus_config_dir() / "repos.json")


def _backfill_per_file_from_t3(
    cat: Catalog,
    t3: object,
    collection: str,
    *,
    dry_run: bool,
    writer: object = None,
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
    w = writer if writer is not None else cat
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
    # nexus-xnz0o: use get_owner_by_prefix() (uniform API).
    owner_rec = cat.get_owner_by_prefix(str(owner))
    repo_root = (owner_rec.get("repo_root") or "") if owner_rec else ""

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
            w.register(
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
    writer = _get_catalog_writer()

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
                    cat, t3, target, dry_run=dry_run, writer=writer,
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
    repo_count, repo_collections = _backfill_repos(cat, registry, dry_run, writer=writer)

    click.echo("Pass 2: Paper collections (docs__*)...")
    paper_count = _backfill_papers(cat, t3, dry_run, repo_collections=repo_collections, writer=writer)

    click.echo("Pass 3: Knowledge collections...")
    knowledge_count = _backfill_knowledge(cat, t3, dry_run, writer=writer)

    hash_updated = 0
    if not dry_run:
        click.echo("Pass 4: chunk_text_hash backfill...")
        from nexus.commands.collection import _backfill_chunk_text_hash  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
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
    import dataclasses  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    import shutil  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    import time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from datetime import datetime, timezone  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.synthesizer import synthesize_from_jsonl  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    "--name-vs-embed-dim",
    "name_vs_embed_dim",
    is_flag=True,
    help=(
        "nexus-j9ey: detect pre-4.32 mislabeled collections. Samples "
        "one chunk per conformant T3 collection and compares the "
        "actual embedding dim to the dim implied by the collection's "
        "__<model>__ segment. FAIL on mismatch; suggests `nx collection "
        "rename` to relabel the collection cosmetically (no re-embed)."
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
    name_vs_embed_dim: bool,
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
        or name_vs_embed_dim
    )
    if not any_check:
        raise click.UsageError(
            "Pass a check flag: --replay-equality, "
            "--t3-doc-id-coverage, --collections-drift, "
            "--chunk-size-distribution, --chunk-text-dedup, "
            "--t3-vs-catalog, or --name-vs-embed-dim."
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
            _printed_anything = True
        if not report["pass"]:
            overall_pass = False
    if name_vs_embed_dim:
        report = _run_name_vs_embed_dim()
        if as_json:
            json_payload["name_vs_embed_dim"] = report
        else:
            if _printed_anything:
                click.echo("")
            _print_name_vs_embed_dim_text(report)
            _printed_anything = True
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
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    cat = _get_catalog()
    try:
        t3_db = make_t3()
        t3_names = {
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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

    # nexus-xnz0o: use distinct_doc_collections() (uniform API).
    doc_collections = set(cat.distinct_doc_collections())

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
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.chroma_quotas)

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415  — command-local import (nexus.db.chroma_quotas)

    try:
        t3 = make_t3()
        collections = [
            c["name"] for c in t3.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    cat = _get_catalog()
    try:
        t3_db = make_t3()
        t3_listing = {
            c["name"]: c for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        }
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "error": f"Failed to list T3 collections: {exc}",
            "t3_orphans": [], "zombies": [],
            "docs_pointing_at_missing_t3": [],
        }

    t3_names = set(t3_listing.keys())
    # nexus-xnz0o: use collection_doc_counts() (uniform API).
    docs_per_coll: dict[str, int] = cat.collection_doc_counts()

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
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
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
        except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
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


# ── nexus-j9ey: --name-vs-embed-dim ──────────────────────────────────────


_VOYAGE_DIM = 1024
"""All current voyage-3 family embedders produce 1024-dim vectors
(voyage-3, voyage-code-3, voyage-context-3). Hardcoded because the
token alone has no dim suffix. If Voyage adds a different-dim model
to the canonical set this needs to grow into a map."""


def _expected_dim_for_model_token(token: str) -> int | None:
    """Return the dim implied by a conformant ``__<model>__`` segment,
    or None if the token is unrecognized.

    Local-mode tokens encode the dim in the suffix
    (``minilm-l6-v2-384`` -> 384, ``bge-base-en-v15-768`` -> 768).
    Voyage tokens are hardcoded to 1024."""
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        CANONICAL_EMBEDDING_MODELS,
        LOCAL_EMBEDDING_MODELS,
    )
    if token in CANONICAL_EMBEDDING_MODELS:
        return _VOYAGE_DIM
    if token in LOCAL_EMBEDDING_MODELS:
        tail = token.rsplit("-", 1)[-1]
        try:
            return int(tail)
        except ValueError:
            return None
    return None


def _run_name_vs_embed_dim() -> dict:
    """Detect mislabeled conformant collections (4.28-era write-side bug).

    Iterates T3 collections, skips bypass-schema and non-conformant
    names, samples one chunk per remaining collection, and compares
    actual embedding dim to the dim implied by the name's
    ``__<model>__`` segment. Read-only against T3."""
    from nexus.corpus import (  # noqa: PLC0415  — command-local import (nexus.corpus)
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

    mismatches: list[dict] = []
    empty: list[str] = []
    checked = 0
    skipped_non_conformant = 0
    unknown_token: list[dict] = []

    try:
        t3_db = make_t3()
        cols = [
            c["name"] for c in t3_db.list_collections()
            if not c["name"].startswith(_BYPASS_SCHEMA_PREFIXES)
        ]
    except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        return {
            "pass": False,
            "checked": 0,
            "mismatches": [],
            "empty": [],
            "skipped_non_conformant": 0,
            "unknown_token": [],
            "error": f"Failed to list T3 collections: {exc}",
        }

    client = t3_db._client  # type: ignore[attr-defined]
    for name in cols:
        if not is_conformant_collection_name(name):
            skipped_non_conformant += 1
            continue
        parsed = parse_conformant_collection_name(name)
        token = parsed["embedding_model"]
        expected = _expected_dim_for_model_token(token)
        if expected is None:
            unknown_token.append({"collection": name, "token": token})
            continue
        try:
            coll = client.get_collection(name)
            sample = coll.get(limit=1, include=["embeddings"])
        except Exception as exc:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
            unknown_token.append(
                {"collection": name, "token": token, "error": str(exc)}
            )
            continue
        embs = sample.get("embeddings")
        if embs is None or len(embs) == 0:
            empty.append(name)
            continue
        actual = len(embs[0])
        checked += 1
        if actual != expected:
            mismatches.append({
                "collection": name,
                "claimed_model": token,
                "expected_dim": expected,
                "actual_dim": actual,
            })

    return {
        "pass": not mismatches,
        "checked": checked,
        "mismatches": mismatches,
        "empty": empty,
        "skipped_non_conformant": skipped_non_conformant,
        "unknown_token": unknown_token,
    }


def _print_name_vs_embed_dim_text(report: dict) -> None:
    if report.get("error"):
        click.echo(f"name-vs-embed-dim: ERROR - {report['error']}")
        return
    status = "PASS" if report["pass"] else "FAIL"
    click.echo(f"name-vs-embed-dim: {status}")
    click.echo(
        f"  checked={report['checked']}  "
        f"mismatches={len(report['mismatches'])}  "
        f"empty={len(report['empty'])}  "
        f"skipped-non-conformant={report['skipped_non_conformant']}"
    )
    if report["mismatches"]:
        click.echo(
            f"\n  Mislabeled collections ({len(report['mismatches'])}):"
        )
        for m in report["mismatches"]:
            click.echo(
                f"    {m['collection']}\n"
                f"      claims {m['claimed_model']} "
                f"({m['expected_dim']}d) but holds {m['actual_dim']}d vectors"
            )
        click.echo(
            "\n  Remediate: relabel the collection to match its actual "
            "embeddings:\n"
            "    nx collection rename <old> <new>\n"
            "  Local-mode users: replace the voyage-* segment with the "
            "matching local token (e.g. minilm-l6-v2-384 for 384d, "
            "bge-base-en-v15-768 for 768d). No re-embed; cosmetic only."
        )
    if report["unknown_token"]:
        click.echo(
            f"\n  Collections with unrecognized model token "
            f"({len(report['unknown_token'])}):"
        )
        for u in report["unknown_token"][:20]:
            extra = f"  ({u['error']})" if u.get("error") else ""
            click.echo(f"    {u['collection']}  token={u['token']}{extra}")


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
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.catalog import _read_event_sourced_gate  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as _ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
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
    import tempfile  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.catalog_db import CatalogDB  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.projector import Projector  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.synthesizer import synthesize_from_jsonl  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    # nexus-vxz3: documents.chunk_count is a denormalised cache populated
    # by the post-store manifest-write batch hook (resync_chunk_count_cache),
    # not by event-log events. The in-memory replay catalog has no
    # document_chunks table to derive chunk_count from, so the projector's
    # post-replay re-derive sees zero manifest rows and keeps the
    # register-time chunk_count (typically 0). Live SQLite reflects the
    # hook-driven value (typically 1+ for docs with at least one chunk).
    # Exclude chunk_count from the comparison — it's intentionally
    # non-event-sourced and the boundary is documented at
    # mcp_infra.manifest_write_batch_hook.
    DOCUMENTS_EXCLUDE = ["chunk_count"]
    # RDR-120 P5.A.3 (nexus-nbsng): the live snapshot routes through the
    # T2 ``CatalogStore`` in read-only mode (``mode=ro`` URI) rather
    # than a direct ``sqlite3.connect`` so all catalog SQLite traffic
    # flows through the substrate-allowlisted path.
    from nexus.db.t2.catalog import CatalogStore as _CatalogStore  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    live_store = _CatalogStore(live_db_path, read_only=True)
    try:
        live_snap = {
            "owners": _snapshot_table(live_store, "owners"),
            "documents": _snapshot_table(
                live_store, "documents", exclude_cols=DOCUMENTS_EXCLUDE,
            ),
            "links": _snapshot_table(live_store, "links", exclude_cols=LINKS_EXCLUDE),
            # RDR-101 Phase 6 prophylactic-review fix: include the
            # collections projection in replay-equality. Pre-fix this
            # gate was blind to Phase 6's new projection state.
            "collections": _snapshot_table(live_store, "collections"),
        }
    finally:
        live_store.close()

    # ── Project + snapshot ────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        projected_path = Path(tmpdir) / "projected.db"
        proj_db = CatalogDB(projected_path)
        try:
            if event_source == "events.jsonl":
                # Local import: deferring to call-time avoids module-load
                # side effects in environments that never need this path.
                from nexus.catalog.event_log import EventLog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
                applied = Projector(proj_db).apply_all(
                    EventLog(cat_dir).replay()
                )
            else:
                applied = Projector(proj_db).apply_all(
                    synthesize_from_jsonl(cat_dir)
                )
            # Snapshot the projected DB through the same CatalogDB
            # instance — no second direct sqlite3 open required.
            projected_snap = {
                "owners": _snapshot_table(proj_db, "owners"),
                "documents": _snapshot_table(
                    proj_db, "documents", exclude_cols=DOCUMENTS_EXCLUDE,
                ),
                "links": _snapshot_table(proj_db, "links", exclude_cols=LINKS_EXCLUDE),
                "collections": _snapshot_table(proj_db, "collections"),
            }
        finally:
            proj_db.close()

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
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog.event_log import EventLog  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.catalog import events as ev  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    from nexus.db import make_t3  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
    from nexus.db.t3 import _BYPASS_SCHEMA_PREFIXES  # noqa: PLC0415  — command-local import (nexus.db.t3)

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
    except Exception as exc:  # noqa: BLE001 — re-raises after cleanup/translation
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
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance
    cat = make_catalog_reader()

    # nexus-yrka: collections renamed via ``nx catalog rename-collection``
    # leave their old name in events.jsonl (events are append-only) but
    # the old T3 collection no longer exists. The catalog records the
    # rename via ``superseded_by``; skip those in T3 lookups instead of
    # reporting them as ``error: open: Collection X does not exist``
    # (which would flip overall_pass to false on every renamed coll).
    # nexus-xnz0o: use list_collections() (uniform API) — superseded_by is in every row.
    superseded_map: dict[str, str] = {}
    try:
        superseded_map = {
            r["name"]: r["superseded_by"]
            for r in cat.list_collections()
            if r.get("superseded_by")
        }
    except Exception:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
        pass

    per_coll: dict[str, dict] = {}
    overall_pass = True
    skipped_superseded = 0
    coll_count = len(expected)
    import time as _time  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

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
        except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
            except Exception as exc:  # noqa: BLE001 — best-effort fallback path; failure is non-fatal here
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
                except Exception:  # noqa: BLE001 — boundary catch; third-party raises undocumented types, handled gracefully
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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

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
    from nexus.catalog import orphan_backfill as ob  # noqa: PLC0415  — command-local import (nexus.catalog.orphan_backfill)
    from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415  — command-local import (nexus.catalog.tumbler)
    from nexus.db import make_t3  # noqa: PLC0415  — command-local import (nexus.db)

    cat = _get_catalog()
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
