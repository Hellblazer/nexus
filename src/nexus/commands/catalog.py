# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import click

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler


def _get_catalog() -> Catalog:
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        raise click.ClickException("Catalog not initialized — run: nx catalog init")
    return Catalog(path, path / ".catalog.db")


def _entry_to_dict(entry) -> dict:
    return {
        "tumbler": str(entry.tumbler),
        "title": entry.title,
        "author": entry.author,
        "year": entry.year,
        "content_type": entry.content_type,
        "file_path": entry.file_path,
        "corpus": entry.corpus,
        "physical_collection": entry.physical_collection,
        "chunk_count": entry.chunk_count,
        "head_hash": entry.head_hash,
        "indexed_at": entry.indexed_at,
        "meta": entry.meta,
    }


def _link_to_dict(link) -> dict:
    return {
        "from": str(link.from_tumbler),
        "to": str(link.to_tumbler),
        "type": link.link_type,
        "from_span": link.from_span,
        "to_span": link.to_span,
        "created_by": link.created_by,
        "created_at": link.created_at,
    }


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
        # List all documents via SQLite
        rows = cat._db._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents LIMIT ?",
            (limit,),
        ).fetchall()
        from nexus.catalog.catalog import CatalogEntry
        entries = [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
            )
            for r in rows
        ]
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
    """Show full catalog entry."""
    cat = _get_catalog()
    try:
        t = Tumbler.parse(tumbler_or_title)
        entry = cat.resolve(t)
    except ValueError:
        # Try FTS search by title
        results = cat.find(tumbler_or_title)
        entry = results[0] if results else None

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


@catalog.command("register")
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
@click.argument("tumbler")
@click.option("--title", default="")
@click.option("--author", default="")
@click.option("--year", default=0, type=int)
@click.option("--corpus", default="")
@click.option("--meta", default="", help="JSON string of additional metadata")
def update_cmd(
    tumbler: str, title: str, author: str, year: int, corpus: str, meta: str,
) -> None:
    """Update a catalog entry's metadata."""
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
    cat.update(Tumbler.parse(tumbler), **fields)
    click.echo(f"Updated: {tumbler}")


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
    """Create a typed link between catalog entries."""
    cat = _get_catalog()
    cat.link(
        Tumbler.parse(from_tumbler), Tumbler.parse(to_tumbler),
        link_type, created_by="user",
        from_span=from_span, to_span=to_span,
    )
    click.echo(f"Linked: {from_tumbler} → {to_tumbler} ({link_type})")


@catalog.command("unlink")
@click.argument("from_tumbler")
@click.argument("to_tumbler")
@click.option("--type", "link_type", default="")
def unlink_cmd(from_tumbler: str, to_tumbler: str, link_type: str) -> None:
    """Remove link(s) between catalog entries."""
    cat = _get_catalog()
    removed = cat.unlink(Tumbler.parse(from_tumbler), Tumbler.parse(to_tumbler), link_type)
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
    """Show links for a catalog entry."""
    cat = _get_catalog()
    t = Tumbler.parse(tumbler)
    result = cat.graph(t, depth=depth, direction=direction, link_type=link_type)
    if as_json:
        click.echo(json.dumps({
            "nodes": [_entry_to_dict(n) for n in result["nodes"]],
            "edges": [_link_to_dict(e) for e in result["edges"]],
        }, indent=2))
    else:
        for edge in result["edges"]:
            click.echo(f"{edge.from_tumbler} → {edge.to_tumbler} ({edge.link_type})")


@catalog.command("owners")
@click.option("--json", "as_json", is_flag=True)
def owners_cmd(as_json: bool) -> None:
    """List registered owners."""
    cat = _get_catalog()
    rows = cat._db._conn.execute(
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
    conn = cat._db._conn
    owner_count = conn.execute("SELECT count(*) FROM owners").fetchone()[0]
    doc_count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
    link_count = conn.execute("SELECT count(*) FROM links").fetchone()[0]
    type_counts = dict(
        conn.execute(
            "SELECT content_type, count(*) FROM documents GROUP BY content_type"
        ).fetchall()
    )
    link_type_counts = dict(
        conn.execute(
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
