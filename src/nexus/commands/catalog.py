# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
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
        entries = cat.by_owner(Tumbler.parse(owner))
    else:
        entries = cat.all_documents(limit=limit + offset + 1)
    if content_type:
        entries = [e for e in entries if e.content_type == content_type]
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
@click.option("--owner", default="", help="Batch: update all entries for this owner")
@click.option("--search", "search_query", default="", help="Batch: update all entries matching this search")
def update_cmd(
    tumbler: str, title: str, author: str, year: int, corpus: str, meta: str,
    owner: str, search_query: str,
) -> None:
    """Update catalog entry metadata. TUMBLER can be a tumbler or title.

    Batch mode: use --owner or --search to update multiple entries at once.
    Example: nx catalog update --owner 1.9 --corpus schema-evolution
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
        for entry in entries:
            cat.update(entry.tumbler, **fields)
        click.echo(f"Updated {len(entries)} entries")
        return

    # Single entry mode
    if not tumbler:
        raise click.ClickException("Provide a tumbler/title or use --owner/--search for batch")
    t = _resolve_tumbler(cat, tumbler)
    cat.update(t, **fields)
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
        click.confirm(f"Delete '{entry.title}' ({t})? Links will be preserved.", abort=True)
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
@click.option("--dry-run", is_flag=True)
def link_bulk_delete_cmd(
    from_t: str, to_t: str, link_type: str, created_by: str,
    created_at_before: str, dry_run: bool,
) -> None:
    """Bulk delete links matching filters."""
    cat = _get_catalog()
    resolved_from = str(_resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_resolve_tumbler(cat, to_t)) if to_t else ""
    count = cat.bulk_unlink(
        from_t=resolved_from, to_t=resolved_to,
        link_type=link_type, created_by=created_by,
        created_at_before=created_at_before, dry_run=dry_run,
    )
    mode = "Would remove" if dry_run else "Removed"
    click.echo(f"{mode} {count} link(s)")


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
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be deleted without deleting.")
def gc_cmd(dry_run: bool) -> None:
    """Remove orphan catalog entries that have miss_count >= 2.

    \b
    Orphans are entries that were absent in two or more consecutive index runs.
    Use --dry-run to preview deletions without applying them.

    \b
    Examples:
      nx catalog gc              # delete orphan entries
      nx catalog gc --dry-run   # preview without deleting
    """
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

    click.echo(f"Found {len(orphans)} orphan {'entry' if len(orphans) == 1 else 'entries'} (miss_count >= 2):")
    for tumbler_str, title, file_path in orphans:
        loc = f" ({file_path})" if file_path else ""
        if dry_run:
            click.echo(f"  [dry-run] would delete {tumbler_str}: {title}{loc}")
        else:
            cat.delete_document(Tumbler.parse(tumbler_str))
            click.echo(f"  deleted {tumbler_str}: {title}{loc}")

    if dry_run:
        click.echo(f"\n{len(orphans)} {'entry' if len(orphans) == 1 else 'entries'} would be deleted. Run without --dry-run to apply.")
    else:
        click.echo(f"\nDeleted {len(orphans)} orphan {'entry' if len(orphans) == 1 else 'entries'}.")


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
    """Look up owner by name."""
    row = cat._db.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = ?", (name,)
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
        curator = _get_or_create_curator(cat, col_name.replace("rdr__", ""))

        try:
            col = t3.get_or_create_collection(col_name)
            # Paginate to discover ALL unique source_path values
            seen_paths: dict[str, str] = {}  # path → title
            offset = 0
            while True:
                result = col.get(include=["metadatas"], limit=200, offset=offset)
                metas = result.get("metadatas", [])
                for meta in metas:
                    path = meta.get("source_path", "")
                    if path and path not in seen_paths:
                        title = meta.get("title", "") or Path(path).stem
                        seen_paths[path] = title
                if len(metas) < 200:
                    break
                offset += 200

            # Derive repo root from registry for relativization (RDR-060)
            repo_root: Path | None = None
            try:
                import hashlib

                from nexus.catalog.catalog import _default_registry_path, make_relative
                from nexus.registry import RepoRegistry

                reg_path = _default_registry_path()
                if reg_path.exists():
                    for repo_path_str in RepoRegistry(reg_path).all_info():
                        h = hashlib.sha256(repo_path_str.encode()).hexdigest()[:8]
                        if col_name.endswith(h):
                            repo_root = Path(repo_path_str)
                            break
            except Exception:
                pass  # non-fatal — store as-is

            for path, title in seen_paths.items():
                if dry_run:
                    click.echo(f"  [dry-run] {title} → {col_name}")
                    count += 1
                    continue
                fp = make_relative(path, repo_root) if repo_root else path
                existing = [e for e in cat.by_owner(curator) if e.file_path in (path, fp)]
                if not existing:
                    cat.register(
                        owner=curator, title=title, content_type="rdr",
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
        target = f"docs__{corpus}"
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


@catalog.command("link-generate")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be done without writing")
def link_generate_cmd(dry_run: bool) -> None:
    """Run all link generators over the full catalog (batch scan)."""
    cat = _get_catalog()
    if dry_run:
        click.echo("[dry-run] Would run full link generation scan")
        return
    from nexus.catalog.link_generator import generate_rdr_filepath_links
    count = generate_rdr_filepath_links(cat)
    click.echo(f"Generated {count} filepath links.")


def _make_t3():
    from nexus.db import make_t3
    return make_t3()


def _make_registry():
    from nexus.config import nexus_config_dir
    from nexus.registry import RepoRegistry

    return RepoRegistry(nexus_config_dir() / "repos.json")


@catalog.command("backfill", hidden=True)
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
def backfill_cmd(dry_run: bool) -> None:
    """Populate catalog from existing T3 collections and registry."""
    cat = _get_catalog()

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

    stale: list = []  # entries to delete
    skipped_replacement: list = []  # entries with RDR-prefix replacement
    for entry in entries:
        fp = entry.file_path or ""
        if not fp:  # MCP-stored, no source file
            continue
        if "/" not in fp:  # basename-only — remediable
            continue
        if Path(fp).exists():  # live, not stale
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
    click.echo(
        f"{n_stale} stale entr{'y' if n_stale == 1 else 'ies'}"
        + (
            f" (skipped {n_skipped} with same-prefix replacement)"
            if n_skipped else ""
        )
        + "."
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

    n_deleted = 0
    for entry in stale:
        if cat.delete_document(entry.tumbler):
            n_deleted += 1

    click.echo(f"\nDone: deleted {n_deleted} catalog entr"
               f"{'y' if n_deleted == 1 else 'ies'}.")
