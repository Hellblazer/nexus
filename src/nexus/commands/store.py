# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)

from nexus.corpus import t3_collection_name
from nexus.db import make_t3
from nexus.db.t3 import T3Database
from nexus.ttl import parse_ttl


def _t3() -> T3Database:
    from nexus.config import get_credential, is_local_mode

    if not is_local_mode():
        database = get_credential("chroma_database")
        api_key = get_credential("chroma_api_key")
        voyage_api_key = get_credential("voyage_api_key")

        if not api_key:
            raise click.ClickException(
                "chroma_api_key not set — run: nx config set chroma_api_key <value>"
            )
        if not voyage_api_key:
            raise click.ClickException(
                "voyage_api_key not set — run: nx config set voyage_api_key <value>"
            )
        if not database:
            raise click.ClickException(
                "chroma_database not set — run: nx config init"
            )
    try:
        return make_t3()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def store() -> None:
    """Permanent semantic knowledge store (local ChromaDB or Cloud + Voyage AI)."""


@store.command("put")
@click.argument("source")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--title", "-t", default="", help="Document title (required when SOURCE is -)")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option("--category", default="", help="Category label")
@click.option("--ttl", default="permanent", show_default=True,
              help="TTL: Nd, Nw, or permanent")
@click.option("--session-id", default="", hidden=True)
@click.option("--agent", default="", hidden=True, help="Source agent name")
def put_cmd(
    source: str,
    collection: str,
    title: str,
    tags: str,
    category: str,
    ttl: str,
    session_id: str,
    agent: str,
) -> None:
    """Store SOURCE (file path or '-' for stdin) in the T3 knowledge store.

    SOURCE may be a file path or '-' to read from stdin.  When reading from
    stdin, --title is required.

    \b
    Examples:
      nx store put ./notes.md --collection knowledge --tags "arch,decision"
      echo "key insight" | nx store put - --title "finding-01" --collection knowledge
      nx store put ./doc.md --ttl 30d --title "sprint-notes"
    """
    if source == "-":
        if not title:
            raise click.ClickException("--title is required when reading from stdin (-)")
        content = sys.stdin.read()
    else:
        path = Path(source)
        if not path.exists():
            raise click.ClickException(f"File not found: {source}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise click.ClickException(f"File {source!r} is not valid UTF-8.")
        if not title:
            title = path.name

    try:
        days = parse_ttl(ttl)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ttl_days = days if days is not None else 0

    col_name = t3_collection_name(collection)
    db = _t3()
    doc_id = db.put(
        collection=col_name,
        content=content,
        title=title,
        tags=tags,
        category=category,
        session_id=session_id,
        source_agent=agent,
        ttl_days=ttl_days,
    )
    click.echo(f"Stored: {doc_id}  →  {col_name}")
    _catalog_store_hook(title=title, doc_id=doc_id, collection_name=col_name)


def _catalog_store_hook(title: str, doc_id: str, collection_name: str) -> None:
    """Register knowledge entry in catalog. Silently skipped if absent."""
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # Dedup by doc_id
        if cat.by_doc_id(doc_id) is not None:
            return

        # Get or create "knowledge" curator owner
        rows = cat._db.execute(
            "SELECT tumbler_prefix FROM owners WHERE name = 'knowledge'"
        ).fetchone()
        if rows:
            from nexus.catalog.tumbler import Tumbler
            owner = Tumbler.parse(rows[0])
        else:
            owner = cat.register_owner("knowledge", "curator")

        cat.register(
            owner=owner, title=title, content_type="knowledge",
            physical_collection=collection_name,
            meta={"doc_id": doc_id},
        )
    except Exception:
        _log.debug("catalog_store_hook_failed", exc_info=True)


@store.command("list")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--limit", "-n", default=200, show_default=True,
              help="Maximum entries to show")
@click.option("--offset", default=0, show_default=True,
              help="Skip this many entries (for pagination)")
@click.option("--docs", is_flag=True, default=False,
              help="Show unique documents instead of individual chunks")
def list_cmd(collection: str, limit: int, offset: int, docs: bool) -> None:
    """List entries in a T3 knowledge collection."""
    col_name = t3_collection_name(collection)
    db = _t3()

    if docs:
        _list_documents(db, col_name)
        return

    entries = db.list_store(col_name, limit=limit, offset=offset)
    if not entries:
        click.echo(f"No entries in {col_name} at offset {offset}.")
        return

    # Get total count for page info
    try:
        total = db.collection_info(col_name)["count"]
    except (KeyError, Exception):
        total = "?"

    shown_start = offset + 1
    shown_end = offset + len(entries)
    click.echo(f"{col_name}  (showing {shown_start}-{shown_end} of {total})\n")
    from datetime import datetime, timedelta  # noqa: PLC0415
    for e in entries:
        doc_id = e.get("id", "")[:16]
        title = (e.get("title") or "")[:40]
        tags = e.get("tags") or ""
        ttl_days = e.get("ttl_days", 0)
        indexed_at_full = e.get("indexed_at") or ""
        indexed_at = indexed_at_full[:10]
        # Derive expiry from indexed_at + ttl_days (expires_at no longer
        # stored — see metadata_schema.is_expired).
        if ttl_days and ttl_days > 0 and indexed_at_full:
            try:
                exp = (datetime.fromisoformat(indexed_at_full)
                       + timedelta(days=ttl_days)).date().isoformat()
                ttl_str = f"expires {exp}"
            except ValueError:
                ttl_str = f"ttl {ttl_days}d"
        else:
            ttl_str = "permanent"
        tag_str = f"  [{tags}]" if tags else ""
        click.echo(f"  {doc_id}  {title:<40}  {ttl_str:<24}  {indexed_at}{tag_str}")

    if shown_end < (total if isinstance(total, int) else float("inf")):
        click.echo(f"\n  Next page: --offset {shown_end}")


def _list_documents(db: T3Database, col_name: str) -> None:
    """List unique documents (deduplicated by content_hash) in a collection."""
    try:
        total_chunks = db.collection_info(col_name)["count"]
    except (KeyError, Exception):
        click.echo(f"Collection not found: {col_name}")
        return

    # Page through all chunks to collect unique documents
    seen: dict[str, dict] = {}  # content_hash → metadata
    offset = 0
    batch = 300
    while offset < total_chunks:
        entries = db.list_store(col_name, limit=batch, offset=offset)
        if not entries:
            break
        for e in entries:
            h = e.get("content_hash", e.get("id", ""))
            if h not in seen:
                seen[h] = e
        offset += batch

    if not seen:
        click.echo(f"No documents in {col_name}.")
        return

    docs = sorted(seen.values(), key=lambda d: d.get("title") or "")
    click.echo(f"{col_name}  ({len(docs)} documents, {total_chunks} chunks)\n")
    for i, d in enumerate(docs, 1):
        title = (d.get("title") or "untitled")[:60]
        chunks = d.get("chunk_count", "?")
        pages = d.get("page_count", "?")
        method = d.get("extraction_method", "")
        indexed = (d.get("indexed_at") or "")[:10]
        click.echo(f"  {i:3d}. {title:<60}  {chunks:>4} chunks  {pages:>3}p  {method:<8}  {indexed}")



@store.command("get")
@click.argument("doc_id")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Output as JSON")
def get_cmd(doc_id: str, collection: str, json_out: bool) -> None:
    """Retrieve a T3 knowledge entry by its document ID.

    DOC_ID is the 16-char hex ID shown by 'nx store list'.

    \b
    Examples:
      nx store get a1b2c3d4e5f6g7h8
      nx store get a1b2c3d4e5f6g7h8 --collection code__myrepo --json
    """
    col_name = t3_collection_name(collection)
    entry = _t3().get_by_id(col_name, doc_id)
    if entry is None:
        raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")

    if json_out:
        import json
        click.echo(json.dumps(entry, indent=2))
    else:
        title = entry.get("title", "")
        tags = entry.get("tags", "")
        indexed_at = (entry.get("indexed_at") or "")[:10]
        click.echo(f"ID:         {entry['id']}")
        click.echo(f"Collection: {col_name}")
        if title:
            click.echo(f"Title:      {title}")
        if tags:
            click.echo(f"Tags:       {tags}")
        if indexed_at:
            click.echo(f"Indexed:    {indexed_at}")
        click.echo(f"\n{entry.get('content', '')}")


def _reap_catalog_for_doc_ids(doc_ids: list[str]) -> None:
    """Best-effort: tombstone catalog entries for deleted T3 docs.

    Why: ``nx store delete`` removed only the T3 doc, leaving the catalog
    entry visible to ``nx catalog list`` until the next ``nx catalog gc``.
    Eventual consistency surprised users who expected delete to be atomic.
    Skipped silently when the catalog is uninitialised.
    """
    from nexus.catalog import Catalog
    from nexus.config import catalog_path

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        return
    try:
        cat = Catalog(cat_path, cat_path / ".catalog.db")
        for doc_id in doc_ids:
            entry = cat.by_doc_id(doc_id)
            if entry is not None:
                cat.delete_document(entry.tumbler)
    except Exception:
        _log.debug("catalog_reap_failed", exc_info=True, doc_ids=doc_ids)


@store.command("delete")
@click.option("--collection", "-c", required=True,
              help="Collection name (required)")
@click.option("--id", "doc_id", default=None,
              help="Exact 16-char document ID from 'nx store list'")
@click.option("--title", default=None,
              help="Exact title metadata match (deletes all matching chunks)")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip confirmation prompt")
def delete_cmd(collection: str, doc_id: str | None, title: str | None, yes: bool) -> None:
    """Delete an entry from a T3 knowledge collection.

    Use --id for a single known entry, --title to delete all chunks of a document.
    To remove an entire collection use: nx collection delete <name>
    """
    if not doc_id and not title:
        raise click.UsageError("provide --id or --title")
    if doc_id and title:
        raise click.UsageError("--id and --title are mutually exclusive")

    col_name = t3_collection_name(collection)
    db = _t3()

    if doc_id:
        if not db.delete_by_id(col_name, doc_id):
            raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")
        _reap_catalog_for_doc_ids([doc_id])
        click.echo(f"Deleted: {doc_id}  from  {col_name}")
    else:
        ids = db.find_ids_by_title(col_name, title)
        if not ids:
            raise click.ClickException(f"No entries with title {title!r} in {col_name}")
        if not yes:
            n = "entry" if len(ids) == 1 else "entries"
            click.echo(f"Found {len(ids)} {n} with title {title!r} in {col_name}.")
            click.confirm("Delete?", abort=True)
        db.batch_delete(col_name, ids)
        _reap_catalog_for_doc_ids(ids)
        click.echo(f"Deleted {len(ids)} {'entry' if len(ids) == 1 else 'entries'} with title {title!r} from {col_name}.")

@store.command("expire")
def expire_cmd() -> None:
    """Remove T3 knowledge__ entries whose TTL has expired."""
    count = _t3().expire()
    click.echo(f"Expired {count} {'entry' if count == 1 else 'entries'}.")


@store.command("export")
@click.argument("collection", default="", required=False)
@click.option("--output", "-o", default=None,
              help="Output file path (.nxexp) or directory (when --all).")
@click.option("--include", "includes", multiple=True,
              help="Glob pattern matched against source_path. Repeat for OR logic.")
@click.option("--exclude", "excludes", multiple=True,
              help="Glob pattern matched against source_path. Repeat for OR logic.")
@click.option("--all", "export_all", is_flag=True, default=False,
              help="Export every collection to separate .nxexp files.")
def export_cmd(
    collection: str,
    output: str | None,
    includes: tuple[str, ...],
    excludes: tuple[str, ...],
    export_all: bool,
) -> None:
    """Export a T3 collection to a portable .nxexp backup file.

    The export preserves all documents, metadata, and embeddings, enabling
    later import without re-embedding (saves Voyage AI API costs).

    \b
    Examples:
      nx store export code__myrepo -o myrepo-backup.nxexp
      nx store export code__myrepo --include "*.py" -o python-only.nxexp
      nx store export --all
      nx store export --all -o /path/to/backup-dir/
    """
    from datetime import date

    from nexus.corpus import t3_collection_name as _t3col
    from nexus.errors import EmbeddingModelMismatch, FormatVersionError
    from nexus.exporter import export_collection

    if export_all and collection:
        raise click.UsageError("Cannot specify COLLECTION together with --all.")
    if not export_all and not collection:
        raise click.UsageError("Provide a COLLECTION name or use --all.")

    db = _t3()

    if export_all:
        # One .nxexp file per collection; output may be a directory.
        out_dir = Path(output) if output else Path.cwd()
        if output and not out_dir.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
        collections_info = db.list_collections()
        if not collections_info:
            click.echo("No collections found.")
            return
        today = date.today().isoformat()
        total_exported = 0
        for info in collections_info:
            col_name: str = info["name"]
            fname = f"{col_name}-{today}.nxexp"
            out_path = out_dir / fname
            try:
                result = export_collection(
                    db=db,
                    collection_name=col_name,
                    output_path=out_path,
                    includes=includes,
                    excludes=excludes,
                )
                click.echo(
                    f"Exported {result['exported_count']:>6} records  "
                    f"{col_name}  ->  {out_path.name}"
                )
                total_exported += result["exported_count"]
            except Exception as exc:
                click.echo(f"ERROR exporting {col_name}: {exc}", err=True)
        click.echo(f"\nTotal: {total_exported} records across {len(collections_info)} collections.")
    else:
        col_name = collection if "__" in collection else _t3col(collection)
        out_path = Path(output) if output else Path(f"{col_name}.nxexp")
        try:
            result = export_collection(
                db=db,
                collection_name=col_name,
                output_path=out_path,
                includes=includes,
                excludes=excludes,
            )
        except (EmbeddingModelMismatch, FormatVersionError) as exc:
            raise click.ClickException(str(exc)) from exc
        except Exception as exc:
            raise click.ClickException(f"Export failed: {exc}") from exc

        size_kb = result["file_bytes"] / 1024
        click.echo(
            f"Exported {result['exported_count']} records from {col_name} "
            f"-> {out_path}  ({size_kb:.1f} KB, {result['elapsed_seconds']:.1f}s)"
        )


@store.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--collection", "-c", default=None,
              help="Override target collection name (default: from export header).")
@click.option("--remap", "remaps", multiple=True,
              help="Path substitution: /old/path:/new/path  (repeat for multiple remaps).")
def import_cmd(
    file: str,
    collection: str | None,
    remaps: tuple[str, ...],
) -> None:
    """Import a .nxexp export file into T3.

    Embedding model validation is enforced: importing a code__ export into a
    docs__ collection (or vice versa) is rejected to prevent silent corruption
    of the target collection's vector space.

    \b
    Examples:
      nx store import myrepo-backup.nxexp
      nx store import myrepo-backup.nxexp --remap "/old/path:/new/path"
      nx store import myrepo-backup.nxexp --collection code__newname
    """
    from nexus.errors import EmbeddingModelMismatch, FormatVersionError
    from nexus.exporter import import_collection

    # Parse --remap options (format: old:new).
    parsed_remaps: list[tuple[str, str]] = []
    for remap in remaps:
        if ":" not in remap:
            raise click.UsageError(
                f"--remap requires old:new format (e.g. /old/path:/new/path), "
                f"got: {remap!r}"
            )
        old, new = remap.split(":", 1)
        if not old:
            raise click.UsageError(
                f"--remap old prefix cannot be empty, got: {remap!r}"
            )
        parsed_remaps.append((old, new))

    db = _t3()
    input_path = Path(file)

    try:
        result = import_collection(
            db=db,
            input_path=input_path,
            target_collection=collection,
            remaps=parsed_remaps,
        )
    except FormatVersionError as exc:
        raise click.ClickException(str(exc)) from exc
    except EmbeddingModelMismatch as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Import failed: {exc}") from exc

    click.echo(
        f"Imported {result['imported_count']} records into "
        f"{result['collection_name']}  ({result['elapsed_seconds']:.1f}s)"
    )
