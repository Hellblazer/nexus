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


def _source_uri_home_key(uri: str) -> str:
    """Stable grouping key for source_uri "home" detection.

    For ``file://`` URIs, returns the first four path segments
    (e.g. ``/Users/hal.hildebrand/git/ART``) so two entries from the
    same repo cluster regardless of the file inside that repo. For
    other schemes returns ``<scheme>://<netloc>``. Empty URIs map to
    ``""`` so missing-source-uri entries form their own bucket.
    """
    from urllib.parse import urlparse

    if not uri:
        return ""
    p = urlparse(uri)
    if p.scheme == "file":
        # path = "/Users/hal.hildebrand/git/ART/docs/rdr/X.md"
        # parts = ["", "Users", "hal.hildebrand", "git", "ART", ...]
        # Take through the 5th component (the project root).
        parts = p.path.split("/")
        return "/".join(parts[:5]) if len(parts) >= 5 else p.path
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
        "the catalog. Run after 'nx catalog t3-backfill-doc-id' to "
        "confirm Phase 2 is complete on a host."
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
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def doctor_cmd(
    replay_equality: bool,
    t3_doc_id_coverage: bool,
    strict_not_in_t3: bool,
    as_json: bool,
) -> None:
    """RDR-101 catalog doctor surface.

    Supports two checks today:
      - ``--replay-equality`` (Phase 1, PR C): synthesizer + projector
        round-trip against the live SQLite.
      - ``--t3-doc-id-coverage`` (Phase 2, PR δ): T3 chunks carry the
        doc_id metadata that events.jsonl claims.

    Future flags (``--legacy-collection-grandfather``, etc.) land in
    later phases.
    """
    if not replay_equality and not t3_doc_id_coverage:
        raise click.UsageError(
            "Pass a check flag: --replay-equality or "
            "--t3-doc-id-coverage."
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
                "  legacy JSONL — replay equality is silently broken.\n"
                "  Remediate with:\n"
                "    nx catalog synthesize-log --force\n"
                "    nx catalog t3-backfill-doc-id\n",
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

    if as_json:
        click.echo(json.dumps(json_payload, indent=2))

    if not overall_pass:
        raise click.exceptions.Exit(1)


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
            }

    # ── Diff ──────────────────────────────────────────────────────────
    table_diffs: dict[str, dict] = {}
    overall_pass = True
    for table in ("owners", "documents", "links"):
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
            "not a projector bug. Run 'nx catalog synthesize-log' "
            "before relying on this verb in shadow mode."
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


# ── RDR-101 Phase 2: synthesize-log verb ─────────────────────────────────


@catalog.command("synthesize-log")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Walk the catalog JSONL and report what would be written to "
        "events.jsonl, without actually writing. Use to size the log "
        "before committing to the synthesis."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite an existing events.jsonl. Default is to refuse so "
        "an accidental re-synthesis cannot replace a log that already "
        "carries production state."
    ),
)
@click.option(
    "--chunks/--no-chunks",
    default=False,
    help=(
        "Also walk every T3 collection and emit one ChunkIndexed event "
        "per chunk, with doc_id resolved from source_path → catalog "
        "source_uri → tumbler → doc_id (or the title fallback for "
        "knowledge__* collections that have empty source_uri rows). "
        "Off by default so synthesizing the document side does not "
        "require ChromaDB credentials. Phase 2 deployment guides will "
        "set this on for the canonical synthesis run."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def synthesize_log_cmd(
    dry_run: bool, force: bool, chunks: bool, as_json: bool,
) -> None:
    """RDR-101 Phase 2: synthesize events.jsonl from existing JSONL state.

    Walks ``owners.jsonl`` + ``documents.jsonl`` + ``links.jsonl``,
    mints fresh UUID7 ``doc_id`` values per the RDR-101 §Migration /
    Phase 1 rule, and writes the resulting v: 0 events to
    ``catalog_dir/events.jsonl``.

    Idempotent only against an empty events.jsonl: the verb refuses to
    overwrite a non-empty log unless ``--force`` is passed. ``--dry-run``
    reports counts and exits without writing.

    Phase 1's ``synthesize_from_jsonl`` (used by ``nx catalog doctor
    --replay-equality``) preserves the original tumbler-as-doc_id mapping
    for replay-equality testing. This verb is the canonical Phase 2 +
    bridge: it mints UUID7 doc_ids that Phase 3 native writes will adopt.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.event_log import EventLog
    from nexus.catalog.synthesizer import synthesize_from_jsonl
    from nexus.config import catalog_path

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )

    log = EventLog(cat_dir)
    existing_size = (
        log.path.stat().st_size if log.path.exists() else 0
    )

    if existing_size > 0 and not force and not dry_run:
        raise click.ClickException(
            f"events.jsonl is non-empty ({existing_size} bytes) at "
            f"{log.path}. Pass --force to overwrite, --dry-run to size "
            "the synthesis without writing, or back up the existing log "
            "before re-running."
        )

    # Preserve existing tumbler→doc_id mapping across --force re-synthesis
    # so a prior ``t3-backfill-doc-id`` run's metadata stays valid. Without
    # this, --force would mint fresh UUID7s and silently invalidate every
    # T3 chunk's doc_id; the doctor's --t3-doc-id-coverage would
    # catastrophically fail until the operator re-ran the backfill.
    #
    # LAST-occurrence wins (not first-occurrence): if a tumbler was
    # tombstoned then re-registered with a new UUID7 (legitimate
    # resurrection), the active doc_id is the LAST one in the log. A
    # first-wins guard would preserve the dead doc_id and silently
    # mismatch every T3 chunk that carries the post-resurrection id.
    # Warn loudly when a tumbler appears with conflicting doc_ids so
    # an operator can investigate.
    preserve_map: dict[str, str] = {}
    if existing_size > 0:
        from nexus.catalog import events as ev_mod
        for prior in log.replay():
            if prior.type != ev_mod.TYPE_DOCUMENT_REGISTERED:
                continue
            t = getattr(prior.payload, "tumbler", "") or ""
            d = getattr(prior.payload, "doc_id", "") or ""
            if not (t and d):
                continue
            if t in preserve_map and preserve_map[t] != d:
                _log.warning(
                    "synthesize_preserve_map_tumbler_conflict",
                    tumbler=t,
                    prior_doc_id=preserve_map[t],
                    new_doc_id=d,
                    note=(
                        "tumbler appears with multiple doc_ids in prior "
                        "log; last-occurrence wins"
                    ),
                )
            preserve_map[t] = d  # last-occurrence wins

    # nexus-o6aa.9.17: stage-by-stage progress output. Stderr only so
    # ``--json`` stdout stays machine-clean; suppressed when --json is
    # set so JSON consumers see no progress noise.
    import time as _time
    show_progress = not as_json

    if show_progress:
        click.echo(
            "  [synthesize-log] walking owners.jsonl + documents.jsonl + "
            "links.jsonl…",
            err=True,
        )
    _t0 = _time.monotonic()
    events = list(synthesize_from_jsonl(
        cat_dir, mint_doc_id=True, preserve_doc_ids=preserve_map,
    ))
    if show_progress:
        click.echo(
            f"  [synthesize-log] {len(events)} events from JSONL in "
            f"{_time.monotonic() - _t0:.1f}s",
            err=True,
        )

    chunk_events: list = []
    orphan_count = 0
    if chunks:
        from nexus.catalog.synthesizer import synthesize_t3_chunks
        from nexus.db import make_t3

        if show_progress:
            click.echo(
                "  [synthesize-log] walking T3 collections (Chroma read; "
                "may take several minutes on a large catalog)…",
                err=True,
            )
        _tc = _time.monotonic()
        try:
            t3 = make_t3()
            chunk_events = list(
                synthesize_t3_chunks(t3._client, events)
            )
            orphan_count = sum(
                1 for e in chunk_events
                if getattr(e.payload, "synthesized_orphan", False)
            )
            if show_progress:
                click.echo(
                    f"  [synthesize-log] {len(chunk_events)} chunk events "
                    f"({orphan_count} orphans) in "
                    f"{_time.monotonic() - _tc:.1f}s",
                    err=True,
                )
        except Exception as exc:
            raise click.ClickException(
                f"Failed to walk T3 chunks: {exc}. Pass --no-chunks to "
                "skip the chunk synthesis pass and re-run later once "
                "ChromaDB credentials are configured."
            )

    all_events = events + chunk_events

    counts: dict[str, int] = {}
    for e in all_events:
        counts[e.type] = counts.get(e.type, 0) + 1

    report = {
        "dry_run": dry_run,
        "events_path": str(log.path),
        "existing_bytes": existing_size,
        "events_total": len(all_events),
        "events_by_type": counts,
        "chunks_synthesized": chunks,
        "orphan_chunks": orphan_count,
        "wrote": False,
    }

    if not dry_run:
        if existing_size > 0 and force:
            log.truncate()
        log.append_many(all_events)
        report["wrote"] = True

    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        _print_synthesize_log_text(report)


def _print_synthesize_log_text(report: dict) -> None:
    click.echo(f"Events path:    {report['events_path']}")
    click.echo(f"Existing size:  {report['existing_bytes']} bytes")
    click.echo(f"Events total:   {report['events_total']}")
    if report.get("chunks_synthesized"):
        click.echo(f"Orphan chunks:  {report.get('orphan_chunks', 0)}")
    if report["events_by_type"]:
        click.echo("By type:")
        for t, c in sorted(report["events_by_type"].items(), key=lambda kv: kv[0]):
            click.echo(f"  {t:<24} {c}")
    if report["dry_run"]:
        click.echo("(dry-run — events.jsonl was not written.)")
    elif report["wrote"]:
        click.echo("Wrote events.jsonl.")
    else:
        click.echo("(no write performed.)")


# ── RDR-101 Phase 2: t3-backfill-doc-id verb ─────────────────────────────


# nexus-o6aa.9.18: deferred-class detector for the per-chunk retry
# path. Errors carrying a class string in this set are treated as
# expected during the Phase 4 transition (chunks over the 32-key
# NumMetadataKeys cap that pre-date the cap or carry deprecated
# metadata Phase 4 will prune). They land in ``chunks_deferred``
# rather than ``errors`` so the verb exits 0 — the operator sees
# a deferred-cleanup list, not a failure.
_DEFERRED_QUOTA_HINTS: tuple[str, ...] = (
    "NumMetadataKeys",
    "Number of metadata dictionary keys",
)


def _is_deferred_error(exc_text: str) -> bool:
    return any(hint in exc_text for hint in _DEFERRED_QUOTA_HINTS)


def _bisect_update(
    col, ids: list[str], metas: list[dict], coll_name: str,
) -> tuple[int, list[dict], list[dict]]:
    """Recurse-and-halve to isolate failing chunks (nexus-o6aa.9.19).

    On a batch update failure, halving recovers from a small number
    of failing chunks in O(log N) col.update calls instead of the
    O(N) per-chunk retry. Worst case (1 over-cap chunk per batch of
    300): ~9 update calls instead of 300, ~30× faster against Cloud
    T3 latency.

    Returns ``(updated_count, deferred_records, error_records)``.

    Single-chunk failures classify the same way as before:
    deferred-class quotas (``NumMetadataKeys``) → deferred_records;
    everything else → error_records (operator-visible exit 1).
    """
    if not ids:
        return 0, [], []

    # Try the full slice first. Cheap to retry the whole thing — if
    # it now succeeds (transient network blip), we save log(N) calls.
    try:
        col.update(ids=ids, metadatas=metas)
        return len(ids), [], []
    except Exception as exc:
        if len(ids) == 1:
            # Leaf failure — classify and return.
            cid = ids[0]
            meta = metas[0]
            msg = str(exc)
            record = {
                "collection": coll_name,
                "chunk_id": cid,
                "key_count": len(meta),
                "error": msg[:200],
            }
            if _is_deferred_error(msg):
                return 0, [record], []
            return 0, [], [{
                **record, "stage": "update_per_chunk", "batch_size": 1,
            }]

        # Bisect: split in half, recurse on each side.
        mid = len(ids) // 2
        left_ok, left_def, left_err = _bisect_update(
            col, ids[:mid], metas[:mid], coll_name,
        )
        right_ok, right_def, right_err = _bisect_update(
            col, ids[mid:], metas[mid:], coll_name,
        )
        return (
            left_ok + right_ok,
            left_def + right_def,
            left_err + right_err,
        )


@catalog.command("t3-backfill-doc-id")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Walk events.jsonl and report what would change in T3, without "
        "calling ChromaDB.update. Use to size the backfill (per-"
        "collection chunk count, orphan count) before committing."
    ),
)
@click.option(
    "--collection",
    "collection_filter",
    default="",
    help=(
        "Restrict the backfill to one collection name. Default is "
        "every collection that appears in the event log. Use to "
        "stage a per-collection rollout on a large catalog."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def t3_backfill_doc_id_cmd(
    dry_run: bool, collection_filter: str, as_json: bool,
) -> None:
    """RDR-101 Phase 2: backfill T3 chunks with the doc_id from the event log.

    Reads ``events.jsonl`` for ``ChunkIndexed`` events (which the Phase 2
    ``synthesize-log --chunks`` walker emits with resolved ``doc_id``
    values), opens every collection that appears in the log, and calls
    ``col.update(ids=[chunk_id], metadatas=[{..existing.., doc_id: X}])``
    for each chunk whose stored metadata does not already carry a matching
    doc_id. Idempotent — re-running on already-backfilled chunks is a
    cheap no-op.

    Orphan chunks (``synthesized_orphan=True``) are skipped and surfaced
    in the report; the operator runs ``nx catalog repair-orphan-chunks``
    to assign them manually before they GC after the orphan window.
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
            f"events.jsonl is empty at {log.path}. Run 'nx catalog "
            "synthesize-log --chunks' first to populate the log with "
            "ChunkIndexed events."
        )

    # Group ChunkIndexed events by collection so each ChromaDB
    # collection is opened once, regardless of how many chunks live in
    # it. Skip orphans (no doc_id to backfill).
    by_coll: dict[str, list] = {}
    orphans_total = 0
    for event in log.replay():
        if event.type != ev.TYPE_CHUNK_INDEXED:
            continue
        if event.payload.synthesized_orphan or not event.payload.doc_id:
            orphans_total += 1
            continue
        if collection_filter and event.payload.coll_id != collection_filter:
            continue
        by_coll.setdefault(event.payload.coll_id, []).append(event.payload)

    chunks_updated = 0
    chunks_already_correct = 0
    chunks_deferred: list[dict] = []
    errors: list[dict] = []

    if not dry_run:
        try:
            t3 = make_t3()
        except Exception as exc:
            raise click.ClickException(
                f"Failed to open T3 client: {exc}. Check ChromaDB credentials."
            )

    # nexus-o6aa.9.17: per-collection progress so operators see the
    # verb is working through Cloud T3 (each collection is a network
    # round-trip per 300-id batch; large collections can be tens of
    # minutes). Stderr only; suppressed under --json.
    import time as _time
    show_progress = not dry_run and not as_json
    coll_count = len(by_coll)

    for coll_idx, (coll_name, payloads) in enumerate(by_coll.items(), start=1):
        if dry_run:
            chunks_updated += len(payloads)
            continue
        if show_progress:
            click.echo(
                f"  [t3-backfill {coll_idx}/{coll_count}] {coll_name}: "
                f"{len(payloads)} chunks…",
                err=True,
            )
        _tc = _time.monotonic()
        coll_updated_before = chunks_updated
        try:
            col = t3._client.get_collection(name=coll_name)
        except Exception as exc:
            errors.append({
                "collection": coll_name,
                "stage": "open",
                "error": str(exc),
            })
            if show_progress:
                click.echo(
                    f"  [t3-backfill {coll_idx}/{coll_count}] {coll_name}: "
                    f"could not open ({exc})",
                    err=True,
                )
            continue

        # Idempotency: read current metadata in batches; only update
        # chunks that don't already carry the right doc_id.
        ids = [p.chunk_id for p in payloads]
        _last_progress = _time.monotonic()
        for batch_start in range(0, len(ids), 300):
            batch_ids = ids[batch_start:batch_start + 300]
            batch_payloads = payloads[batch_start:batch_start + 300]
            try:
                existing = col.get(ids=batch_ids, include=["metadatas"])
            except Exception as exc:
                errors.append({
                    "collection": coll_name,
                    "stage": "read",
                    "batch_size": len(batch_ids),
                    "error": str(exc),
                })
                continue

            existing_meta = {
                cid: m for cid, m in zip(
                    existing.get("ids") or [],
                    existing.get("metadatas") or [],
                )
            }

            update_ids: list[str] = []
            update_metas: list[dict] = []
            for payload in batch_payloads:
                current = existing_meta.get(payload.chunk_id) or {}
                if current.get("doc_id") == payload.doc_id:
                    chunks_already_correct += 1
                    continue
                merged = dict(current)
                merged["doc_id"] = payload.doc_id
                update_ids.append(payload.chunk_id)
                update_metas.append(merged)

            if update_ids:
                # nexus-o6aa.9.18 + .9.19: batch-level rejection is
                # all-or-nothing on Cloud T3. _bisect_update tries the
                # whole slice first; on failure it bisects-and-recurses
                # to isolate the failing chunks in O(log N) update calls
                # instead of O(N) per-chunk retry. The .9.18 per-chunk
                # path was correct but took ~30 min wall-clock against
                # Cloud T3 latency on a 9k-chunk over-cap surface; .9.19
                # brings that down to ~log2(300) = 9 calls per failed
                # batch (~30x faster).
                ok, deferred_records, error_records = _bisect_update(
                    col, update_ids, update_metas, coll_name,
                )
                chunks_updated += ok
                chunks_deferred.extend(deferred_records)
                errors.extend(error_records)

            # Per-batch heartbeat every 5 seconds so large collections
            # don't look hung.
            if show_progress and _time.monotonic() - _last_progress >= 5.0:
                progressed = batch_start + len(batch_ids)
                pct = (progressed / len(ids)) * 100 if ids else 0
                click.echo(
                    f"      batch {batch_start // 300 + 1}: "
                    f"{progressed}/{len(ids)} ({pct:.0f}%), "
                    f"updated={chunks_updated - coll_updated_before}",
                    err=True,
                )
                _last_progress = _time.monotonic()

        if show_progress:
            updated_here = chunks_updated - coll_updated_before
            elapsed = _time.monotonic() - _tc
            click.echo(
                f"  [t3-backfill {coll_idx}/{coll_count}] {coll_name}: "
                f"{updated_here}/{len(payloads)} updated in {elapsed:.1f}s",
                err=True,
            )

    report = {
        "dry_run": dry_run,
        "events_path": str(log.path),
        "collection_filter": collection_filter or "(all)",
        "collections_processed": len(by_coll),
        "chunks_eligible": sum(len(p) for p in by_coll.values()),
        "chunks_updated": chunks_updated,
        "chunks_already_correct": chunks_already_correct,
        "orphans_skipped": orphans_total,
        "chunks_deferred": chunks_deferred,
        "chunks_deferred_count": len(chunks_deferred),
        "errors": errors,
    }

    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        _print_t3_backfill_text(report)

    # nexus-o6aa.9.18: deferred-class failures (NumMetadataKeys quota)
    # are expected during the Phase 4 transition — they represent
    # over-cap chunks awaiting the prune-deprecated-keys verb. Exit 0
    # when the only failures were deferred-class. Genuine errors
    # (network, auth, schema, etc.) still surface as exit 1.
    if errors and not dry_run:
        raise click.exceptions.Exit(1)


def _print_t3_backfill_text(report: dict) -> None:
    click.echo(f"Events path:           {report['events_path']}")
    click.echo(f"Collection filter:     {report['collection_filter']}")
    click.echo(f"Collections processed: {report['collections_processed']}")
    click.echo(f"Chunks eligible:       {report['chunks_eligible']}")
    if report["dry_run"]:
        click.echo("(dry-run — no T3 writes performed.)")
    else:
        click.echo(f"Chunks updated:        {report['chunks_updated']}")
        click.echo(f"Already correct:       {report['chunks_already_correct']}")
    click.echo(f"Orphans skipped:       {report['orphans_skipped']}")
    deferred = report.get("chunks_deferred_count", 0)
    if deferred:
        click.echo(
            f"Deferred (over-cap):   {deferred}  (Phase 4 prune-keys remediation)"
        )
        # First few examples for operator visibility.
        for d in (report.get("chunks_deferred") or [])[:3]:
            click.echo(
                f"  {d['collection']} {d['chunk_id'][:24]}…  "
                f"keys={d['key_count']}"
            )
        if deferred > 3:
            click.echo(f"  …and {deferred - 3} more (use --json for full list)")
    if report["errors"]:
        click.echo(f"\nErrors ({len(report['errors'])}):")
        for e in report["errors"][:10]:
            click.echo(f"  {e['collection']} ({e['stage']}): {e['error']}")


# ── RDR-101 Phase 2: doctor --t3-doc-id-coverage ─────────────────────────


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
            f"events.jsonl is empty at {log.path}. Run 'nx catalog "
            "synthesize-log --chunks' first."
        )

    # Build expected (coll_id, chunk_id) → doc_id; track orphans.
    expected: dict[str, dict[str, str]] = {}
    expected_orphans: dict[str, set[str]] = {}
    for event in log.replay():
        if event.type != ev.TYPE_CHUNK_INDEXED:
            continue
        coll = event.payload.coll_id
        cid = event.payload.chunk_id
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

    per_coll: dict[str, dict] = {}
    overall_pass = True
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
            for cid, meta in zip(ids, metas):
                meta = meta or {}
                total += 1
                seen.add(cid)
                actual = meta.get("doc_id", "") or ""
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
        pass_for_coll = (
            not mismatched
            and not missing
            and (not strict_not_in_t3 or not not_in_t3)
        )
        per_coll[coll_name] = {
            "total_chunks": total,
            "with_doc_id": with_doc_id,
            "expected_chunks": len(expected_chunks),
            "expected_orphans": len(expected_orphans.get(coll_name, set())),
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

    return {
        "pass": overall_pass,
        "events_path": str(log.path),
        "collections_in_log": len(expected),
        "strict_not_in_t3": strict_not_in_t3,
        "tables": per_coll,
    }


def _print_t3_doc_id_coverage_text(report: dict) -> None:
    click.echo("=== T3 doc_id coverage ===")
    click.echo(f"Events path:        {report['events_path']}")
    click.echo(f"Collections in log: {report['collections_in_log']}")
    click.echo("")
    for coll_name, diff in report["tables"].items():
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
    if report["pass"]:
        click.echo("PASS — every non-orphan chunk carries the expected doc_id.")
    else:
        click.echo("FAIL — T3 doc_id metadata diverges from the event log.")
        # nexus-o6aa.10.4: surface the next operator verb. The most
        # common cause of a coverage FAIL is a pre-Phase-4 catalog
        # whose chunks lack doc_id metadata; ``nx catalog migrate``
        # is the single-command remediation. Mismatched doc_ids point
        # at a different (rarer) class (synthesize-log drift)
        # which is caught by the same verb's projection rebuild step.
        click.echo("")
        click.echo("Next step:")
        click.echo("  nx catalog migrate --i-have-completed-the-reader-migration")
        click.echo(
            "  (or 'nx catalog t3-backfill-doc-id' alone if you have not "
            "shipped the Phase 4 reader migration yet)"
        )
        click.echo(
            "See docs/migration/rdr-101.md § 'Post-Phase-4 cleanup' for "
            "the full remediation walk-through."
        )


# ── RDR-101 Phase 2: repair-orphan-chunks verb ───────────────────────────


@catalog.command("repair-orphan-chunks")
@click.option(
    "--list",
    "list_mode",
    is_flag=True,
    help=(
        "List orphan ChunkIndexed events from events.jsonl (chunks the "
        "Phase 2 synthesizer could not resolve to a document). Default "
        "mode when neither --list nor --assign is passed."
    ),
)
@click.option(
    "--assign",
    "assignments",
    multiple=True,
    help=(
        "Repeatable. Format: CHUNK_ID:DOC_ID. Each pair appends a "
        "corrective ChunkIndexed event with synthesized_orphan=False and "
        "the assigned doc_id; the projector applies last-event-wins so "
        "the orphan is healed without rewriting the existing log lines."
    ),
)
@click.option(
    "--collection",
    "collection_filter",
    default="",
    help="Restrict listing/assignment to one collection name.",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
def repair_orphan_chunks_cmd(
    list_mode: bool,
    assignments: tuple,
    collection_filter: str,
    as_json: bool,
) -> None:
    """RDR-101 Phase 2: list or repair orphan ChunkIndexed events.

    The Phase 2 synthesizer (``synthesize-log --chunks``) emits
    ``ChunkIndexed`` events with ``doc_id=""`` and
    ``synthesized_orphan=True`` for chunks whose ``source_path`` did not
    match any catalog ``source_uri`` and whose ``title`` did not match
    any catalog title. This verb lets an operator review and repair
    those orphans before the GC sweeps them after the orphan window.

    Repair appends a new ``ChunkIndexed`` event with the resolved
    ``doc_id`` and ``synthesized_orphan=False``. The original orphan
    event stays in the log; the projector dispatches on the last event
    per ``(coll_id, chunk_id)``, so replay sees the corrected state.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.event_log import EventLog
    from nexus.catalog import events as ev
    from nexus.config import catalog_path

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )
    log = EventLog(cat_dir)
    if not log.path.exists() or log.path.stat().st_size == 0:
        raise click.ClickException(
            f"events.jsonl is empty at {log.path}. Run 'nx catalog "
            "synthesize-log --chunks' first."
        )

    if not list_mode and not assignments:
        list_mode = True  # default to listing

    if list_mode and assignments:
        raise click.UsageError(
            "Pass either --list (default) or --assign, not both."
        )

    # Build the current orphan set: walk events.jsonl, apply last-write
    # semantics per (coll_id, chunk_id), and surface those that end up
    # synthesized_orphan=True.
    state: dict[tuple[str, str], dict] = {}
    for event in log.replay():
        if event.type != ev.TYPE_CHUNK_INDEXED:
            continue
        key = (event.payload.coll_id, event.payload.chunk_id)
        state[key] = {
            "coll_id": event.payload.coll_id,
            "chunk_id": event.payload.chunk_id,
            "doc_id": event.payload.doc_id,
            "chash": event.payload.chash,
            "position": event.payload.position,
            "content_hash": event.payload.content_hash,
            "embedded_at": event.payload.embedded_at,
            "synthesized_orphan": event.payload.synthesized_orphan,
        }
    orphans = [
        s for s in state.values()
        if s["synthesized_orphan"]
        and (not collection_filter or s["coll_id"] == collection_filter)
    ]

    if list_mode:
        report = {
            "mode": "list",
            "collection_filter": collection_filter or "(all)",
            "events_path": str(log.path),
            "orphans": orphans,
            "orphans_count": len(orphans),
        }
        if as_json:
            click.echo(json.dumps(report, indent=2))
        else:
            click.echo(f"Events path:       {log.path}")
            click.echo(f"Collection filter: {collection_filter or '(all)'}")
            click.echo(f"Orphans:           {len(orphans)}")
            click.echo("")
            for o in orphans[:50]:
                click.echo(
                    f"  {o['chunk_id']:<40} {o['coll_id']:<35} chash={o['chash'][:16]}"
                )
            if len(orphans) > 50:
                click.echo(f"  ... and {len(orphans) - 50} more")
        return

    # Assign mode. Parse CHUNK_ID:DOC_ID pairs.
    # Parse and dedupe by chunk_id (last assignment wins). Pre-fix a
    # duplicate `--assign chunk_id:X --assign chunk_id:Y` would silently
    # under-count `remaining_orphans` because only the first match
    # appended a repair while both pairs counted as input.
    pairs_by_chunk: dict[str, str] = {}
    for raw in assignments:
        if ":" not in raw:
            raise click.UsageError(
                f"--assign expects CHUNK_ID:DOC_ID, got {raw!r}"
            )
        chunk_id, _, doc_id = raw.partition(":")
        chunk_id = chunk_id.strip()
        doc_id = doc_id.strip()
        if not chunk_id or not doc_id:
            raise click.UsageError(
                f"--assign expects non-empty CHUNK_ID and DOC_ID, got {raw!r}"
            )
        pairs_by_chunk[chunk_id] = doc_id  # later --assign wins

    # Build the set of doc_ids that ever appeared in a DocumentRegistered
    # event so we can warn the operator if they --assign a doc_id that
    # was never registered (typo / typo'd UUID7 / unknown id). The doc
    # log is the canonical source of truth for "what doc_ids exist."
    known_doc_ids: set[str] = set()
    for prior in log.replay():
        if prior.type == ev.TYPE_DOCUMENT_REGISTERED:
            d = getattr(prior.payload, "doc_id", "") or ""
            if d:
                known_doc_ids.add(d)

    # Resolve each assignment to a current orphan to copy chash / coll_id
    # from. Refuse to assign a chunk that is not currently an orphan.
    orphan_by_chunk_id = {
        o["chunk_id"]: o for o in orphans
    }

    repairs: list = []
    skipped: list = []
    for chunk_id, doc_id in pairs_by_chunk.items():
        if chunk_id not in orphan_by_chunk_id:
            skipped.append({
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "reason": (
                    "not currently an orphan (already resolved or unknown)"
                ),
            })
            continue
        if known_doc_ids and doc_id not in known_doc_ids:
            # Soft warning: the doc_id is unknown to the event log. The
            # repair still applies (operator may be intentionally
            # introducing a forward-reference doc_id), but surface the
            # mismatch so a typo is visible.
            _log.warning(
                "repair_orphan_unknown_doc_id",
                chunk_id=chunk_id,
                doc_id=doc_id,
                note=(
                    "doc_id does not appear in any DocumentRegistered "
                    "event; check for typo or run synthesize-log first"
                ),
            )
        orphan = orphan_by_chunk_id[chunk_id]
        # Preserve the orphan's intrinsic chunk fields (position,
        # content_hash, embedded_at) on the corrective event. Pre-fix
        # ``position=0`` was hardcoded, dropping the original chunk
        # ordering on every repair — Phase 5's chunks-table schema
        # depends on position for reading-order reconstruction.
        repairs.append(ev.Event(
            type=ev.TYPE_CHUNK_INDEXED, v=0,
            payload=ev.ChunkIndexedPayload(
                chunk_id=chunk_id,
                chash=orphan["chash"],
                doc_id=doc_id,
                coll_id=orphan["coll_id"],
                position=int(orphan.get("position", 0) or 0),
                content_hash=orphan.get("content_hash", "") or "",
                embedded_at=orphan.get("embedded_at", "") or "",
                synthesized_orphan=False,
            ),
            ts=ev.now_ts(),
        ))

    log.append_many(repairs)

    report = {
        "mode": "assign",
        "collection_filter": collection_filter or "(all)",
        "events_path": str(log.path),
        "repairs_count": len(repairs),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "remaining_orphans": len(orphans) - len(repairs),
    }
    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo(f"Repairs applied:   {len(repairs)}")
        if skipped:
            click.echo(f"Skipped:           {len(skipped)}")
            for s in skipped[:10]:
                click.echo(f"  {s['chunk_id']}: {s['reason']}")
        click.echo(f"Remaining orphans: {report['remaining_orphans']}")


# ──────────────────────────────────────────────────────────────────────
# RDR-101 Phase 3 follow-up D (nexus-o6aa.9.9): one-shot migration verb.
# ──────────────────────────────────────────────────────────────────────


@catalog.command("migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Report what would happen without writing. Shows the current "
        "bootstrap state (fallback active vs not), what synthesize-log "
        "would produce, what t3-backfill would touch."
    ),
)
@click.option(
    "--no-chunks",
    is_flag=True,
    help=(
        "Skip the T3 chunk synthesis + backfill steps. Use when the "
        "catalog has no T3 collections yet, or when staging the "
        "document-side migration before opening the T3 connection."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
@click.option(
    "--i-have-completed-the-reader-migration",
    "reader_migration_done",
    is_flag=True,
    default=False,
    help=(
        "Phase 4 finisher. When set, migrate also runs "
        "prune-deprecated-keys (drops 5 legacy chunk-metadata keys) "
        "and a second t3-backfill-doc-id pass to drain the deferred "
        "class. Without this flag the verb stops after Phase 3. "
        "Acknowledges that every audit-listed reader (see "
        "docs/migration/rdr-101-phase4-reader-audit.md) has migrated "
        "to doc_id-keyed lookups; pruning ahead of the readers "
        "produces silent empty results."
    ),
)
@click.pass_context
def migrate_cmd(
    ctx: click.Context,
    dry_run: bool,
    no_chunks: bool,
    as_json: bool,
    reader_migration_done: bool,
) -> None:
    """RDR-101 Phase 3 (and Phase 4) migration: one-shot upgrade verb.

    Composed verb that sequences the steps an upgrading operator
    would otherwise run by hand:

      1. ``nx catalog synthesize-log --force [--chunks]`` — rebuild
         events.jsonl from the legacy JSONL state.
      2. ``nx catalog t3-backfill-doc-id`` — write doc_id metadata to
         existing T3 chunks (skipped under --no-chunks).
      3. **Phase 4 finisher** (only when
         ``--i-have-completed-the-reader-migration`` is set):
         ``nx catalog prune-deprecated-keys`` drops 5 legacy chunk-
         metadata keys, then a second ``t3-backfill-doc-id`` pass
         drains the deferred class.
      4. ``nx catalog doctor --replay-equality [--t3-doc-id-coverage
         --strict-not-in-t3]`` — verify the result.

    **Idempotent.** Pre-checks ``Catalog.bootstrap_fallback_active``.
    If the catalog is already migrated (events.jsonl matches
    documents.jsonl, no fallback) the verb exits 0 with "nothing to do"
    rather than regenerating a perfectly-good event log. The empty-
    events.jsonl synthesizer-PASS case (no ES mutations have happened
    yet on a freshly-upgraded catalog) is treated as a proactive
    migration opportunity, not a no-op; proactively populating the
    log avoids the bootstrap-fallback state arising on first ES write.

    **Pre-RDR-101 operators.** Catalogs that span the pre-Phase-3 era
    typically carry chunks at the 32-key Cloud quota with both
    ``source_path`` + ``git_*`` legacy keys present. Step 2 cannot
    add ``doc_id`` to those chunks (``chunks_deferred`` in the
    backfill report). The Phase 4 finisher (step 3) frees the slots
    and the second backfill pass catches them in a single run. Pass
    ``--i-have-completed-the-reader-migration`` once your nexus
    version includes Phase 4's reader migrations (PRs #471-#480, on
    main since 2026-05-02) to take that single-command path.

    Verified by ``scripts/validate/rdr-101-migration-e2e.sh`` (PR ζ
    sandbox e2e harness, nexus-o6aa.9.10).
    """
    from nexus.catalog.catalog import Catalog
    from nexus.catalog.event_log import EventLog
    from nexus.config import catalog_path

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it first."
        )

    status = _check_bootstrap_status()
    fallback_active = status.get("fallback_active", False)

    log = EventLog(cat_dir)
    documents_path = cat_dir / "documents.jsonl"
    has_legacy_docs = documents_path.exists() and documents_path.stat().st_size > 0
    has_events = log.path.exists() and log.path.stat().st_size > 0

    # Decision matrix:
    #   has_legacy_docs=False & has_events=False → empty catalog: nothing to do
    #   has_legacy_docs=True  & has_events=True  & not fallback_active
    #     → Phase 3 done; Phase 4 may still be needed (run when the
    #       operator has set --i-have-completed-the-reader-migration)
    #   has_legacy_docs=True  & has_events=False
    #     → freshly-upgraded, no ES mutations yet: proactively migrate
    #   has_legacy_docs=True  & fallback_active   → migrate
    phase3_needs_migration = has_legacy_docs and (
        not has_events or fallback_active
    )
    # nexus-o6aa.10.4 fix: with --i-have-completed-the-reader-migration,
    # the Phase 4 finisher (prune + drain backfill) needs to run even
    # when Phase 3 is already done. The previous gate skipped to
    # "nothing to do" and never reached the Phase 4 step. Idempotent on
    # already-pruned catalogs (chunks_already_pruned counts the no-ops).
    phase4_needs_run = (
        reader_migration_done and not no_chunks and has_events
    )
    needs_migration = phase3_needs_migration or phase4_needs_run

    if not needs_migration:
        report = {
            "needs_migration": False,
            "reason": (
                "catalog is empty"
                if not has_legacy_docs
                else "events.jsonl already covers documents.jsonl (no fallback active)"
            ),
            "fallback_active": fallback_active,
            "documents_jsonl": str(documents_path),
            "events_jsonl": str(log.path),
        }
        if as_json:
            click.echo(json.dumps(report, indent=2))
        else:
            click.echo(f"Nothing to do — {report['reason']}.")
        return

    # Track whether the Phase 3 steps need to run (Phase 4 alone path
    # skips synthesize-log + first backfill). Steps below honor this.
    phase3_only = phase3_needs_migration and not phase4_needs_run
    skip_phase3 = phase4_needs_run and not phase3_needs_migration

    if dry_run:
        would_run = []
        if not skip_phase3:
            would_run.append(
                f"nx catalog synthesize-log --force"
                + ("" if no_chunks else " --chunks")
            )
            if not no_chunks:
                would_run.append("nx catalog t3-backfill-doc-id")
        if reader_migration_done and not no_chunks:
            would_run.extend([
                "nx catalog prune-deprecated-keys "
                "--i-have-completed-the-reader-migration "
                "--skip-coverage-check",
                "nx catalog t3-backfill-doc-id  # drain deferred class",
            ])
        would_run.append(
            "nx catalog doctor --replay-equality"
            + ("" if no_chunks else " --t3-doc-id-coverage --strict-not-in-t3")
        )
        report = {
            "needs_migration": True,
            "fallback_active": fallback_active,
            "phase4_finisher": reader_migration_done,
            "would_run": would_run,
            "documents_jsonl": str(documents_path),
            "events_jsonl": str(log.path),
        }
        if as_json:
            click.echo(json.dumps(report, indent=2))
        else:
            click.echo("Migration plan:")
            for step in report["would_run"]:
                click.echo(f"  {step}")
        return

    # nexus-o6aa.9.17: per-step timing so the operator sees how long
    # each phase took. Real-data finding (Hal's first migration):
    # synthesize-log was ~5 min, t3-backfill was ~3 min, doctor was
    # ~1 min — but with no echo until the next step's "==>" line, the
    # operator was guessing whether the process was alive.
    import time as _time

    # Steps 1-2: Phase 3 work. Skipped when only the Phase 4 finisher
    # needs to run (Phase 3 is already complete).
    if not skip_phase3:
        # Step 1: synthesize-log --force.
        click.echo("==> nx catalog synthesize-log --force"
                   + ("" if no_chunks else " --chunks"))
        _step1_t0 = _time.monotonic()
        try:
            ctx.invoke(synthesize_log_cmd, **{
                "dry_run": False,
                "force": True,
                "chunks": not no_chunks,
                "as_json": False,
            })
        except click.ClickException as exc:
            raise click.ClickException(
                f"synthesize-log failed: {exc.message}. Migration aborted; "
                "no further steps run."
            ) from exc
        click.echo(
            f"    synthesize-log: {_time.monotonic() - _step1_t0:.1f}s",
        )

        # Step 2: t3-backfill-doc-id (unless --no-chunks).
        if not no_chunks:
            click.echo("==> nx catalog t3-backfill-doc-id")
            _step2_t0 = _time.monotonic()
            try:
                ctx.invoke(t3_backfill_doc_id_cmd, **{
                    "dry_run": False,
                    "collection_filter": "",
                    "as_json": False,
                })
            except click.ClickException as exc:
                raise click.ClickException(
                    f"t3-backfill-doc-id failed: {exc.message}. Migration "
                    "partial; events.jsonl is current but T3 chunks may "
                    "lack doc_id metadata. Re-run 'nx catalog "
                    "t3-backfill-doc-id' after fixing the underlying issue."
                ) from exc
            click.echo(
                f"    t3-backfill-doc-id: {_time.monotonic() - _step2_t0:.1f}s",
            )
    else:
        click.echo(
            "Phase 3 already complete (events.jsonl covers documents.jsonl). "
            "Running Phase 4 finisher only."
        )

    # Synchronize live SQLite to events.jsonl before doctor runs.
    # nexus-o6aa.9.14: ``synthesize-log`` writes events.jsonl but does
    # NOT re-project into SQLite — the live SQLite from before the
    # migration retains its legacy-rebuild shape. Doctor's
    # ``_run_replay_equality`` opens ``.catalog.db`` directly via
    # ``sqlite3.connect(...mode=ro)`` (read-only snapshot) and bypasses
    # ``Catalog._ensure_consistent`` entirely, so it never triggers
    # the ES rebuild that would heal the live state. Without this
    # explicit sync, doctor reports FAIL on every row whose JSONL
    # shape differs from what the synthesizer emits (e.g. ``meta:
    # null`` in JSONL → ``'null'`` in legacy SQLite vs ``'{}'`` in
    # the projection). Constructing a fresh Catalog here triggers
    # ``_ensure_consistent``'s ES rebuild path, which DELETEs and
    # replays events.jsonl into the live SQLite. After that, doctor's
    # comparison is apples-to-apples.
    click.echo("==> sync live SQLite to events.jsonl")
    _sync_t0 = _time.monotonic()
    sync_cat = Catalog(cat_dir, cat_dir / ".catalog.db")
    try:
        # Constructor calls ``_ensure_consistent`` at the end of
        # __init__; the rebuild is in the live ``.catalog.db`` by the
        # time ``_db.commit()`` returns.
        sync_cat._db.commit()
    finally:
        sync_cat._db.close()
    click.echo(
        f"    sync: {_time.monotonic() - _sync_t0:.1f}s",
    )

    # Phase 4 finisher (optional, gated on the operator flag).
    # nexus-o6aa.10.4: pre-101 catalogs hit the over-cap chunk class
    # in step 2 (t3-backfill-doc-id can't fit doc_id alongside the
    # legacy git_* + source_path keys on chunks already at the 32-key
    # quota). Pruning frees those slots, then the second backfill
    # pass drains the deferred class. Idempotent on catalogs without
    # over-cap chunks (chunks_already_pruned=N, second backfill is
    # a no-op).
    if reader_migration_done and not no_chunks:
        click.echo("==> nx catalog prune-deprecated-keys")
        _step3_t0 = _time.monotonic()
        try:
            ctx.invoke(prune_deprecated_keys_cmd, **{
                "dry_run": False,
                "collection_filter": "",
                "as_json": False,
                "reader_migration_done": True,
                # The migrate orchestrator owns the gate: step 2 just
                # ran, so unmapped doc_ids are exactly the deferred
                # class the prune is meant to free.
                "skip_coverage_check": True,
            })
        except click.ClickException as exc:
            raise click.ClickException(
                f"prune-deprecated-keys failed: {exc.message}. "
                "Migration partial; events.jsonl is current and "
                "T3 has whatever doc_id coverage step 2 reached. "
                "Inspect the report above and re-run."
            ) from exc
        click.echo(
            f"    prune-deprecated-keys: "
            f"{_time.monotonic() - _step3_t0:.1f}s",
        )

        click.echo("==> nx catalog t3-backfill-doc-id  # drain deferred class")
        _step4_t0 = _time.monotonic()
        try:
            ctx.invoke(t3_backfill_doc_id_cmd, **{
                "dry_run": False,
                "collection_filter": "",
                "as_json": False,
            })
        except click.ClickException as exc:
            raise click.ClickException(
                f"second t3-backfill-doc-id (drain deferred) failed: "
                f"{exc.message}. Re-run 'nx catalog t3-backfill-doc-id'."
            ) from exc
        click.echo(
            f"    t3-backfill-doc-id (drain): "
            f"{_time.monotonic() - _step4_t0:.1f}s",
        )

    # Step 3: doctor verification.
    click.echo("==> nx catalog doctor --replay-equality"
               + ("" if no_chunks else " --t3-doc-id-coverage --strict-not-in-t3"))
    _doc_t0 = _time.monotonic()
    try:
        ctx.invoke(doctor_cmd, **{
            "replay_equality": True,
            "t3_doc_id_coverage": not no_chunks,
            "strict_not_in_t3": not no_chunks,
            "as_json": False,
        })
    except click.exceptions.Exit as exc:
        if exc.exit_code != 0:
            raise click.ClickException(
                "Migration completed but doctor verification did not "
                "PASS. Inspect the report above; re-running this verb "
                "is safe and idempotent."
            ) from exc
    click.echo(
        f"    doctor: {_time.monotonic() - _doc_t0:.1f}s",
    )

    click.echo("\nMigration complete.")


# ── RDR-101 Phase 4: prune-deprecated-keys verb (nexus-o6aa.10.3) ─────────


# Five legacy chunk-metadata keys that the Phase 4 reader migrations
# stopped depending on. ``title`` is intentionally NOT in this set; the
# .10.2 audit (docs/migration/rdr-101-phase4-reader-audit.md, Category C)
# kept it permanently as the slug-shaped identity for ``knowledge__knowledge``
# and the universal display field across formatters.
_PRUNE_DEPRECATED_KEYS: frozenset[str] = frozenset({
    "source_path",
    "git_branch",
    "git_commit_hash",
    "git_project_name",
    "git_remote_url",
})


@catalog.command("prune-deprecated-keys")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Walk every collection and report what would change without "
        "calling col.update. Use to size the prune (per-collection "
        "chunk count, total deprecated-key occurrences) before "
        "committing."
    ),
)
@click.option(
    "--collection",
    "collection_filter",
    default="",
    help=(
        "Restrict the prune to one collection name. Default: every "
        "collection in T3. Use to stage a per-collection rollout."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit machine-readable JSON instead of text output.",
)
@click.option(
    "--i-have-completed-the-reader-migration",
    "reader_migration_done",
    is_flag=True,
    default=False,
    help=(
        "REQUIRED. Acknowledges that every audit-listed reader "
        "(docs/migration/rdr-101-phase4-reader-audit.md) has migrated "
        "to doc_id-keyed lookups. Without this flag the verb refuses "
        "to run; pruning before the readers migrate produces silent "
        "empty results across aspect extraction, link boost, "
        "incremental sync, and display formatters."
    ),
)
@click.option(
    "--skip-coverage-check",
    is_flag=True,
    default=False,
    help=(
        "Bypass the --t3-doc-id-coverage gate. The default refuses "
        "to run on a collection whose chunks aren't 100%% covered by "
        "doc_id metadata. Override is for operators who have a known "
        "orphan-chunk class they intend to clean up post-prune."
    ),
)
def prune_deprecated_keys_cmd(
    dry_run: bool,
    collection_filter: str,
    as_json: bool,
    reader_migration_done: bool,
    skip_coverage_check: bool,
) -> None:
    """RDR-101 Phase 4: drop legacy metadata keys from every T3 chunk.

    Removes the five keys whose readers migrated to doc_id-keyed
    catalog lookups in Phase 4: ``source_path``, ``git_branch``,
    ``git_commit_hash``, ``git_project_name``, ``git_remote_url``.
    Idempotent: chunks that already lack the keys are no-ops.

    The chunk metadata key budget on ChromaDB Cloud is 32 top-level
    keys (``MAX_SAFE_TOP_LEVEL_KEYS``). Over-cap chunks on Hal's
    catalog carry 35-36 keys; dropping these five brings them to
    30-31 keys and leaves room for ``doc_id`` + future additions.

    Pre-flight gates (refuse to proceed unless overridden):
    - ``--i-have-completed-the-reader-migration`` is required. Without
      it the verb errors out with the migration audit doc reference.
    - Per-collection ``--t3-doc-id-coverage = 100%%`` (the
      ``nx catalog doctor --t3-doc-id-coverage`` check). Override with
      ``--skip-coverage-check`` only after deciding what to do about
      the missing-doc_id chunks; the prune leaves them strictly
      unreachable for the post-Phase-5 reader.

    Bisect-on-failure (mirrors ``t3-backfill-doc-id`` per .9.19): on
    a batch update failure, halve and recurse to isolate failing
    chunks in O(log N) update calls instead of O(N) per-chunk retry.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path
    from nexus.db import make_t3

    cat_dir = catalog_path()
    if not Catalog.is_initialized(cat_dir):
        raise click.ClickException(
            f"Catalog not initialized at {cat_dir}. "
            "Run 'nx catalog setup' to create and populate it."
        )

    if not reader_migration_done:
        raise click.ClickException(
            "Refusing to prune. The Phase 4 reader migration must be "
            "complete first; pruning ahead of the readers produces "
            "silent empty results.\n\n"
            "Read docs/migration/rdr-101-phase4-reader-audit.md for "
            "the migration scope, verify every listed reader has "
            "shipped, then re-run with "
            "--i-have-completed-the-reader-migration."
        )

    try:
        t3 = make_t3()
    except Exception as exc:
        raise click.ClickException(
            f"Failed to open T3 client: {exc}. Check ChromaDB credentials."
        )

    # Resolve target collections.
    if collection_filter:
        target_collections = [collection_filter]
    else:
        try:
            target_collections = [
                c["name"] if isinstance(c, dict) else c.name
                for c in t3.list_collections()
            ]
        except Exception as exc:
            raise click.ClickException(
                f"Failed to list collections: {exc}"
            )

    # Coverage gate. Reuse the doctor's projection; refuse if any
    # target collection drops below 100%.
    if not skip_coverage_check:
        if not as_json:
            click.echo(
                "Running t3-doc-id-coverage gate (this walks every "
                "collection in events.jsonl)…",
                err=True,
            )
        try:
            coverage_report = _run_t3_doc_id_coverage(
                progress=not as_json,
            )
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Coverage check failed: {exc}. Re-run with "
                "--skip-coverage-check to override."
            )
        per_coll = coverage_report.get("tables", {})
        offenders: list[dict] = []
        for name in target_collections:
            row = per_coll.get(name)
            if row is None:
                continue  # not in events.jsonl: nothing claimed; skip.
            total = row.get("total_chunks", 0)
            with_doc_id = row.get("with_doc_id", 0)
            if total > 0 and with_doc_id < total:
                offenders.append({
                    "collection": name,
                    "total": total,
                    "with_doc_id": with_doc_id,
                    "missing": total - with_doc_id,
                })
        if offenders:
            lines = [
                f"  {o['collection']}: {o['with_doc_id']}/{o['total']} "
                f"covered (missing={o['missing']})"
                for o in offenders
            ]
            raise click.ClickException(
                "Refusing to prune. The following collections have "
                "chunks whose doc_id is unpopulated; pruning would "
                "leave them strictly unreachable:\n"
                + "\n".join(lines)
                + "\n\nRun 'nx catalog t3-backfill-doc-id' on each, "
                "or pass --skip-coverage-check to proceed anyway."
            )

    # Walk + rewrite.
    # nexus-o6aa.10.4: progress is shown in dry-run too (the walk is
    # the slow part regardless of whether we update). Suppressed only
    # when --json is set so the structured payload stays parseable.
    import time as _time
    show_progress = not as_json
    chunks_updated = 0
    chunks_already_pruned = 0
    chunks_deferred: list[dict] = []
    errors: list[dict] = []
    per_collection_summary: dict[str, dict] = {}

    if show_progress:
        click.echo(
            f"\nWalking {len(target_collections)} collections "
            f"({'dry run' if dry_run else 'live'})…",
            err=True,
        )

    for coll_idx, coll_name in enumerate(target_collections, start=1):
        try:
            col = t3._client.get_collection(name=coll_name)
        except Exception as exc:
            errors.append({
                "collection": coll_name,
                "stage": "open",
                "error": str(exc),
            })
            continue

        # Pre-fetch the total chunk count so progress can show
        # offset/total. Cheap (one round-trip) versus walking 200+
        # pages with no denominator. Falls back to "?" on error.
        try:
            total_chunks = col.count()
        except Exception:
            total_chunks = None

        if show_progress:
            click.echo(
                f"  [prune {coll_idx}/{len(target_collections)}] "
                f"{coll_name}: {total_chunks if total_chunks is not None else '?'} chunks…",
                err=True,
            )
        _tc = _time.monotonic()
        _last_progress = _tc
        coll_updated_before = chunks_updated
        coll_already_before = chunks_already_pruned

        offset = 0
        while True:
            try:
                page = col.get(
                    limit=300, offset=offset, include=["metadatas"],
                )
            except Exception as exc:
                errors.append({
                    "collection": coll_name,
                    "stage": "read",
                    "offset": offset,
                    "error": str(exc),
                })
                break
            ids = page.get("ids") or []
            metas = page.get("metadatas") or []
            if not ids:
                break
            update_ids: list[str] = []
            update_metas: list[dict] = []
            for cid, meta in zip(ids, metas):
                meta = meta or {}
                deprecated_present = _PRUNE_DEPRECATED_KEYS.intersection(meta)
                if not deprecated_present:
                    chunks_already_pruned += 1
                    continue
                # ChromaDB's ``col.update(metadatas=...)`` MERGES rather
                # than replaces; passing only the kept keys leaves the
                # deprecated keys in place. The delete-key shape is
                # ``{key: None}``, which removes the key from storage.
                deletion_patch: dict = {k: None for k in deprecated_present}
                update_ids.append(cid)
                update_metas.append(deletion_patch)

            if update_ids and not dry_run:
                ok, deferred_records, error_records = _bisect_update(
                    col, update_ids, update_metas, coll_name,
                )
                chunks_updated += ok
                chunks_deferred.extend(deferred_records)
                errors.extend(error_records)
            elif update_ids:
                # Dry run: count what would change.
                chunks_updated += len(update_ids)

            # Per-page heartbeat every 5 seconds so large collections
            # don't look hung. Threshold-throttled rather than
            # per-page so small collections stay quiet.
            if show_progress and _time.monotonic() - _last_progress >= 5.0:
                pages_done = offset // 300 + 1
                progressed = offset + len(ids)
                pct = (
                    f"{(progressed / total_chunks) * 100:.0f}%"
                    if total_chunks
                    else "?"
                )
                click.echo(
                    f"      page {pages_done}: "
                    f"{progressed}/{total_chunks if total_chunks else '?'} "
                    f"({pct}), updated={chunks_updated - coll_updated_before}",
                    err=True,
                )
                _last_progress = _time.monotonic()

            if len(ids) < 300:
                break
            offset += 300

        per_collection_summary[coll_name] = {
            "updated": chunks_updated - coll_updated_before,
            "already_pruned": chunks_already_pruned - coll_already_before,
            "elapsed_seconds": round(_time.monotonic() - _tc, 2),
        }

        if show_progress:
            updated_here = chunks_updated - coll_updated_before
            elapsed = _time.monotonic() - _tc
            click.echo(
                f"  [prune {coll_idx}/{len(target_collections)}] "
                f"{coll_name}: {updated_here} updated "
                f"in {elapsed:.1f}s",
                err=True,
            )

    report = {
        "dry_run": dry_run,
        "collection_filter": collection_filter or "(all)",
        "deprecated_keys": sorted(_PRUNE_DEPRECATED_KEYS),
        "chunks_updated": chunks_updated,
        "chunks_already_pruned": chunks_already_pruned,
        "chunks_deferred": chunks_deferred,
        "errors": errors,
        "per_collection": per_collection_summary,
    }

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
    else:
        _print_prune_deprecated_keys_text(report)

    if errors:
        raise click.exceptions.Exit(1)


def _print_prune_deprecated_keys_text(report: dict) -> None:
    """Render the prune-deprecated-keys report in plain text."""
    if report["dry_run"]:
        click.echo("DRY RUN: no col.update calls issued.")
    click.echo(
        f"Pruned keys: {', '.join(report['deprecated_keys'])}"
    )
    click.echo(f"  chunks_updated:        {report['chunks_updated']}")
    click.echo(f"  chunks_already_pruned: {report['chunks_already_pruned']}")
    if report["chunks_deferred"]:
        click.echo(
            f"  chunks_deferred:       {len(report['chunks_deferred'])}"
        )
    if report["errors"]:
        click.echo(f"  errors:                {len(report['errors'])}")
        for err in report["errors"][:5]:
            click.echo(f"    {err}")
        if len(report["errors"]) > 5:
            click.echo(
                f"    ... and {len(report['errors']) - 5} more"
            )
    if report["per_collection"]:
        click.echo("\nPer collection:")
        for name, summary in sorted(report["per_collection"].items()):
            click.echo(
                f"  {name:<40}  updated={summary['updated']}  "
                f"already={summary['already_pruned']}  "
                f"elapsed={summary['elapsed_seconds']}s"
            )
