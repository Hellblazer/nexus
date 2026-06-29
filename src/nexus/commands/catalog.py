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


# Metadata keys the indexing/normalisation layer prunes from chunk metadata.
# Not a diagnostics concern — kept here (not in catalog_cmds/doctor) so the
# indexer-side contract tests that assert it stays disjoint from
# ALLOWED_TOP_LEVEL keep importing it from nexus.commands.catalog (nexus-whh61.4).
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
from nexus.commands.catalog_cmds import report as _report_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import integrity as _integrity_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import doctor as _doctor_cmds  # noqa: E402 — must follow the `catalog` group definition above
from nexus.commands.catalog_cmds import orphan_backfill as _orphan_backfill_cmds  # noqa: E402 — must follow the `catalog` group definition above

_owners_cmds.register(catalog)
_backfill_cmds.register(catalog)
_links_cmds.register(catalog)
_backups_cmds.register(catalog)
_collections_cmds.register(catalog)
_migration_cmds.register(catalog)
_maintenance_cmds.register(catalog)
_remediation_cmds.register(catalog)
_report_cmds.register(catalog)
_integrity_cmds.register(catalog)
_doctor_cmds.register(catalog)
_orphan_backfill_cmds.register(catalog)


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
    hash_updated = 0
    try:
        hash_updated = _backfill_all_chunk_text_hashes(t3)
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


def _backfill_all_chunk_text_hashes(t3) -> int:
    """Backfill ``chunk_text_hash`` across every T3 collection; return chunks updated.

    No-op in vector-service mode (nexus-84gbt): the Java service owns chunk
    identity (chash) via its manifest + post-store path, and the local,
    Chroma-specific paginate-and-upsert backfill reaches into ``t3._client``
    (a ``chromadb`` client attribute). In service mode ``t3`` is an
    ``HttpVectorClient`` with no ``_client`` — calling it there raised
    ``AttributeError`` and degraded ``nx catalog setup`` to "Hash backfill
    partial", leaving the manifest empty. Skip cleanly instead.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)

    # Instance-based guard (NOT env-based is_vector_service_mode): a service-
    # backed handle is an HttpVectorClient with no chroma ._client. Keying on the
    # handle keeps injected chroma-backed T3Database test fixtures on the legacy
    # branch regardless of NX_STORAGE_BACKEND_VECTORS (the documented preference
    # in http_vector_client.is_service_backed).
    if is_service_backed(t3):
        click.echo(
            "  (service mode: chunk_text_hash is owned by the service; "
            "skipping local backfill)"
        )
        return 0

    from nexus.commands.collection import _backfill_chunk_text_hash  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    hash_updated = 0
    for col_info in t3.list_collections():
        col = t3._client.get_collection(col_info["name"])
        updated, _, _ = _backfill_chunk_text_hash(col)
        hash_updated += updated
    return hash_updated


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
        hash_updated = _backfill_all_chunk_text_hashes(t3)

    mode = "dry-run" if dry_run else "registered"
    click.echo(f"\nBackfill complete ({mode}):")
    click.echo(f"  Repos:     {repo_count}")
    click.echo(f"  Papers:    {paper_count}")
    click.echo(f"  Knowledge: {knowledge_count}")
    if not dry_run:
        click.echo(f"  Hash:      {hash_updated} chunks updated")
