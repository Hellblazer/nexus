# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import click

import structlog

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)


def _get_catalog() -> Catalog:
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        raise click.ClickException("Catalog not initialized — run: nx catalog init")
    return Catalog(path, path / ".catalog.db")


def _resolve_tumbler(cat: Catalog, value: str) -> Tumbler:
    """Resolve a tumbler string OR a title/filename to a Tumbler.

    Tries numeric parse first. Falls back to FTS search, then file_path match.
    """
    # Try as tumbler first
    try:
        t = Tumbler.parse(value)
        if cat.resolve(t) is not None:
            return t
        # Valid tumbler format but document deleted/missing
        raise click.ClickException(f"Not found: {value}")
    except ValueError:
        pass

    # Try FTS search by title/filename
    results = cat.find(value)
    if results:
        exact = [r for r in results if r.title == value]
        if exact:
            return exact[0].tumbler
        if len(results) == 1:
            return results[0].tumbler
        raise click.ClickException(
            f"Ambiguous: {len(results)} documents match {value!r} — use tumbler"
        )

    raise click.ClickException(f"Not found: {value}")


def _entry_to_dict(entry) -> dict:
    return entry.to_dict()


def _link_to_dict(link) -> dict:
    return link.to_dict()


@click.group()
def catalog() -> None:
    """Git-backed Xanadu-inspired catalog for T3 (RDR-049)."""


@catalog.command("init")
@click.option("--remote", default="", help="Optional git remote URL")
def init_cmd(remote: str) -> None:
    """Initialize catalog git repository."""
    from nexus.config import catalog_path

    path = catalog_path()
    Catalog.init(path, remote=remote or None)
    click.echo(f"Catalog initialized at {path}")


@catalog.command("list")
@click.option("--owner", default="")
@click.option("--type", "content_type", default="")
@click.option("--limit", "-n", default=50)
@click.option("--json", "as_json", is_flag=True)
def list_cmd(owner: str, content_type: str, limit: int, as_json: bool) -> None:
    """List catalog entries."""
    cat = _get_catalog()
    if owner:
        entries = cat.by_owner(Tumbler.parse(owner))
    else:
        entries = cat.all_documents(limit=limit)
    if content_type:
        entries = [e for e in entries if e.content_type == content_type]
    entries = entries[:limit]

    if as_json:
        click.echo(json.dumps([_entry_to_dict(e) for e in entries], indent=2))
    else:
        for e in entries:
            click.echo(f"{str(e.tumbler):<12} {e.content_type:<10} {e.title}")


@catalog.command("show")
@click.argument("tumbler_or_title")
@click.option("--json", "as_json", is_flag=True)
def show_cmd(tumbler_or_title: str, as_json: bool) -> None:
    """Show full catalog entry. Accepts a tumbler (1.9.14) or title/filename."""
    cat = _get_catalog()
    t = _resolve_tumbler(cat, tumbler_or_title)
    entry = cat.resolve(t)
    if entry is None:
        raise click.ClickException(f"Not found: {tumbler_or_title}")

    if as_json:
        d = _entry_to_dict(entry)
        d["links_from"] = [_link_to_dict(l) for l in cat.links_from(entry.tumbler)]
        d["links_to"] = [_link_to_dict(l) for l in cat.links_to(entry.tumbler)]
        click.echo(json.dumps(d, indent=2))
    else:
        click.echo(f"Tumbler:    {entry.tumbler}")
        click.echo(f"Title:      {entry.title}")
        click.echo(f"Author:     {entry.author}")
        click.echo(f"Year:       {entry.year}")
        click.echo(f"Type:       {entry.content_type}")
        click.echo(f"File:       {entry.file_path}")
        click.echo(f"Corpus:     {entry.corpus}")
        click.echo(f"Collection: {entry.physical_collection}")
        click.echo(f"Chunks:     {entry.chunk_count}")
        click.echo(f"Hash:       {entry.head_hash}")
        click.echo(f"Indexed:    {entry.indexed_at}")
        out_links = cat.links_from(entry.tumbler)
        in_links = cat.links_to(entry.tumbler)
        if out_links:
            click.echo("Links out:")
            for l in out_links:
                click.echo(f"  → {l.to_tumbler} ({l.link_type})")
        if in_links:
            click.echo("Links in:")
            for l in in_links:
                click.echo(f"  ← {l.from_tumbler} ({l.link_type})")


@catalog.command("search")
@click.argument("query")
@click.option("--limit", "-n", default=20)
@click.option("--json", "as_json", is_flag=True)
def search_cmd(query: str, limit: int, as_json: bool) -> None:
    """Search catalog by title, author, corpus, or file path."""
    cat = _get_catalog()
    results = cat.find(query)[:limit]
    if as_json:
        click.echo(json.dumps([_entry_to_dict(e) for e in results], indent=2))
    else:
        if not results:
            click.echo("No results.")
            return
        for e in results:
            click.echo(f"{str(e.tumbler):<12} {e.content_type:<10} {e.title}")


@catalog.command("register", hidden=True)
@click.option("--title", "-t", required=True)
@click.option("--owner", "-o", required=True)
@click.option("--author", default="")
@click.option("--year", default=0, type=int)
@click.option("--type", "content_type", default="paper")
@click.option("--file-path", default="")
@click.option("--corpus", default="")
def register_cmd(
    title: str, owner: str, author: str, year: int,
    content_type: str, file_path: str, corpus: str,
) -> None:
    """Register a document in the catalog."""
    cat = _get_catalog()
    tumbler = cat.register(
        Tumbler.parse(owner), title,
        content_type=content_type, file_path=file_path,
        corpus=corpus, author=author, year=year,
    )
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
    """Delete a catalog document. Leaves links intact (orphaned links preserved)."""
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
    type=click.Choice(["cites", "supersedes", "quotes", "relates", "comments", "implements"]),
)
@click.option("--from-span", default="")
@click.option("--to-span", default="")
def link_cmd(
    from_tumbler: str, to_tumbler: str, link_type: str,
    from_span: str, to_span: str,
) -> None:
    """Create a typed link. Arguments accept tumblers or titles."""
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
    """Remove link(s). Arguments accept tumblers or titles."""
    cat = _get_catalog()
    ft = _resolve_tumbler(cat, from_tumbler)
    tt = _resolve_tumbler(cat, to_tumbler)
    removed = cat.unlink(ft, tt, link_type)
    click.echo(f"Removed {removed} link(s)")


@catalog.command("links")
@click.argument("tumbler")
@click.option("--direction", default="both", type=click.Choice(["in", "out", "both"]))
@click.option("--type", "link_type", default="")
@click.option("--depth", default=1, type=int)
@click.option("--json", "as_json", is_flag=True)
def links_cmd(
    tumbler: str, direction: str, link_type: str, depth: int, as_json: bool,
) -> None:
    """Show links for a catalog entry. TUMBLER accepts a tumbler or title."""
    cat = _get_catalog()
    t = _resolve_tumbler(cat, tumbler)
    result = cat.graph(t, depth=depth, direction=direction, link_type=link_type)
    if as_json:
        click.echo(json.dumps({
            "nodes": [_entry_to_dict(n) for n in result["nodes"]],
            "edges": [_link_to_dict(e) for e in result["edges"]],
        }, indent=2))
    else:
        for edge in result["edges"]:
            click.echo(f"{edge.from_tumbler} → {edge.to_tumbler} ({edge.link_type})")


@catalog.command("link-query")
@click.option("--from", "from_t", default="", help="From tumbler or title")
@click.option("--to", "to_t", default="", help="To tumbler or title")
@click.option("--tumbler", default="", help="Tumbler or title (with --direction)")
@click.option("--direction", default="both", type=click.Choice(["in", "out", "both"]))
@click.option("--type", "link_type", default="")
@click.option("--created-by", default="")
@click.option("--limit", "-n", default=50, type=int)
@click.option("--offset", default=0, type=int)
@click.option("--json", "as_json", is_flag=True)
def link_query_cmd(
    from_t: str, to_t: str, tumbler: str, direction: str,
    link_type: str, created_by: str,
    limit: int, offset: int, as_json: bool,
) -> None:
    """Query links by any combination of filters."""
    cat = _get_catalog()
    resolved_from = str(_resolve_tumbler(cat, from_t)) if from_t else ""
    resolved_to = str(_resolve_tumbler(cat, to_t)) if to_t else ""
    resolved_tumbler = str(_resolve_tumbler(cat, tumbler)) if tumbler else ""
    links = cat.link_query(
        from_t=resolved_from, to_t=resolved_to,
        tumbler=resolved_tumbler, direction=direction,
        link_type=link_type, created_by=created_by,
        limit=limit, offset=offset,
    )
    if as_json:
        click.echo(json.dumps([_link_to_dict(l) for l in links], indent=2))
    else:
        if not links:
            click.echo("No links found.")
            return
        for edge in links:
            click.echo(f"{edge.from_tumbler} → {edge.to_tumbler} ({edge.link_type}) by {edge.created_by}")


@catalog.command("link-bulk-delete")
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


@catalog.command("link-audit")
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


@catalog.command("pull")
def pull_cmd() -> None:
    """Pull catalog from remote and rebuild SQLite."""
    cat = _get_catalog()
    cat.pull()
    click.echo("Catalog pulled and rebuilt.")


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
    if as_json:
        click.echo(json.dumps({
            "owners": owner_count,
            "documents": doc_count,
            "links": link_count,
            "by_type": type_counts,
            "by_link_type": link_type_counts,
        }, indent=2))
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

    for col_info in knowledge_cols:
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

    for col_info in paper_cols:
        col_name = col_info["name"]

        # Try to extract metadata from first chunk
        title = col_name.replace("docs__", "")
        author = ""
        year = 0
        try:
            col = t3.get_or_create_collection(col_name)
            result = col.get(limit=1, include=["metadatas"])
            if result.get("ids") and result.get("metadatas"):
                meta = result["metadatas"][0]
                title = meta.get("source_title", "") or meta.get("title", "") or title
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
@click.option("--code-rdr/--no-code-rdr", default=True, help="Generate code-RDR links by heuristic")
@click.option("--dry-run", is_flag=True, help="Show what would be created without writing")
def generate_links_cmd(citations: bool, code_rdr: bool, dry_run: bool) -> None:
    """Auto-generate typed links from metadata cross-matching."""
    cat = _get_catalog()
    from nexus.catalog.link_generator import generate_citation_links, generate_code_rdr_links

    total = 0
    if citations:
        if dry_run:
            click.echo("Would generate citation links (dry-run mode not yet supported for link preview)")
        else:
            count = generate_citation_links(cat)
            click.echo(f"Citation links created: {count}")
            total += count

    if code_rdr:
        if dry_run:
            click.echo("Would generate code-RDR links (dry-run mode not yet supported for link preview)")
        else:
            count = generate_code_rdr_links(cat)
            click.echo(f"Code-RDR links created: {count}")
            total += count

    if not dry_run:
        click.echo(f"Total links generated: {total}")


def _make_t3():
    from nexus.db import make_t3
    return make_t3()


def _make_registry():
    from nexus.registry import RepoRegistry
    return RepoRegistry(Path.home() / ".config" / "nexus" / "repos.json")


@catalog.command("backfill")
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

    mode = "dry-run" if dry_run else "registered"
    click.echo(f"\nBackfill complete ({mode}):")
    click.echo(f"  Repos:     {repo_count}")
    click.echo(f"  Papers:    {paper_count}")
    click.echo(f"  Knowledge: {knowledge_count}")
