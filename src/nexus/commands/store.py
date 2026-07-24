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
    # No credential pre-flight (nexus-c7aj3): make_t3() constructs the
    # service-backed client unconditionally (RDR-155 P4a.2) — no call site
    # here can reach a direct-Chroma client, so a Chroma/Voyage cred check
    # at this boundary only ever produced false failures on migrated
    # installs. Legacy creds are migration-source config; the ETL that
    # reads them does its own checks. Real construction failures surface
    # as make_t3()'s own honest errors.
    try:
        return make_t3()
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def store() -> None:
    """Permanent semantic knowledge store (served by the native nexus-service: bge-768 locally, Voyage AI in cloud mode)."""


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

    db = _t3()
    # nexus-hmxi: pass t3 so the resolver grandfathers an existing
    # legacy 2-segment collection ahead of the auto-promoted
    # conformant shape, keeping store/list/search aligned.
    col_name = t3_collection_name(collection, t3=db)

    # RDR-101 Phase 3 PR δ Stage B.4: pre-register the catalog entry
    # so the T3 chunk can carry the resulting tumbler as ``doc_id``
    # at write-time. chunk_chroma_id mirrors ``T3Database.put``'s
    # natural-id derivation (chunk_text_hash[:32] per RDR-108 D1 /
    # nexus-kmb6; for single-chunk MCP docs chunk_text == content).
    # The hook returns the catalog tumbler string (or "" when the
    # catalog is absent).
    chunk_chroma_id, manifest_metadatas = _single_chunk_manifest_metadata(content)
    catalog_doc_id, catalog_row_minted = _catalog_store_hook_tracked(
        title=title, doc_id=chunk_chroma_id, collection_name=col_name,
    )

    # nexus-b6enc C2: the catalog row is registered BEFORE db.put — on a
    # put failure, delete the row minted IN THIS CALL (never a
    # pre-existing dedup target) so no ghost row survives, then surface
    # the original error. The compensation never raises.
    try:
        doc_id = db.put(
            collection=col_name,
            content=content,
            title=title,
            tags=tags,
            category=category,
            session_id=session_id,
            source_agent=agent,
            ttl_days=ttl_days,
            catalog_doc_id=catalog_doc_id,
        )
    except Exception as put_exc:
        if catalog_doc_id and catalog_row_minted:
            _rollback_minted_catalog_entry(
                catalog_doc_id, original_error=str(put_exc),
            )
        raise

    # nexus-b6enc C3: manifest leg off the swallowing fire_batch chain —
    # direct write + verify; failure becomes an explicit non-"Stored:"
    # error after the remaining hook chains fire.
    manifest_error = ""
    if catalog_doc_id:
        try:
            _store_put_manifest_direct(catalog_doc_id, manifest_metadatas)
        except Exception as manifest_exc:  # noqa: BLE001 — captured for the explicit error below
            manifest_error = str(manifest_exc)
            # CRE Minor 5: structlog twin of the MCP path's
            # store_put_manifest_direct_failed — the ClickException below
            # reaches the interactive user but must also reach structured
            # logs for cross-caller grep parity.
            import structlog  # noqa: PLC0415 — branch-local logging
            structlog.get_logger(__name__).warning(
                "store_put_manifest_direct_failed",
                doc_id=doc_id,
                catalog_doc_id=catalog_doc_id,
                collection=col_name,
                error=manifest_error[:300],
                exc_info=True,
            )
    # nexus-9099: fire the three post-store hook chains so the chash
    # index, taxonomy assignment, and aspect-extraction queue see CLI
    # store-put events. RDR-095 symmetric-fire; this path was missed by
    # the original commit. doc_id is the source identity here (no
    # on-disk file at the CLI boundary, mirroring MCP store_put).
    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    hooks = HookRegistry()
    install_default_hooks(hooks)
    # nexus-lf8f: pass catalog_doc_id through to HookRegistry.fire_store_chains
    # so the manifest-write batch hook can populate document_chunks and
    # documents.chunk_count for this CLI store path. Without it the
    # hook short-circuits and the catalog row ships with chunk_count=0
    # (the same regression class as nexus-zq79 / 4.32.4 fixed for
    # `nx index repo`).
    hooks.fire_store_chains(
        [doc_id], col_name, [content],
        metadatas=manifest_metadatas,
        catalog_doc_id=catalog_doc_id,
    )
    if manifest_error:
        # CRE Imp 3: 'nx catalog reconcile' is a verified no-op for this
        # failure mode (heal_manifest_gaps' candidate filter excludes
        # chunk_count=0 rows without meta.content_hash) — suggest the
        # retry, which IS effective (by_doc_id dedup + idempotent put).
        raise click.ClickException(
            f"stored to T3 ({doc_id} in {col_name}) but NOT cataloged: "
            f"{manifest_error}. Catalog row {catalog_doc_id} may show "
            f"chunk_count=0; retry 'nx store put' with the same content "
            f"(idempotent dedup makes retry safe)."
        )
    click.echo(f"Stored: {doc_id}  →  {col_name}")


# nexus-8g79.10 (V1): catalog_store_hook moved to
# ``nexus.catalog.store_hook`` (lower layer) so MCP infra can invoke
# without the MCP layer reaching up into this CLI module. Re-exported
# here under the legacy private name for back-compat.
from nexus.catalog.store_hook import catalog_store_hook as _catalog_store_hook  # noqa: E402
# nexus-b6enc: tracked variant (created-vs-deduped) + compensation +
# direct fail-loud manifest write for the store_put path.
from nexus.catalog.store_hook import catalog_store_hook_tracked as _catalog_store_hook_tracked  # noqa: E402
from nexus.catalog.store_hook import rollback_minted_catalog_entry as _rollback_minted_catalog_entry  # noqa: E402
from nexus.catalog.store_hook import store_put_manifest_direct as _store_put_manifest_direct  # noqa: E402
# GH #1370 Defect 4b: shared with MCP store_put — see store_hook.py's
# docstring for why real metadatas (not None) must reach fire_store_chains.
from nexus.catalog.store_hook import single_chunk_manifest_metadata as _single_chunk_manifest_metadata  # noqa: E402


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
    db = _t3()
    col_name = t3_collection_name(collection, t3=db)

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
    except Exception:  # noqa: BLE001 — best-effort total count for display; '?' on any backend failure (incl. KeyError)
        total = "?"

    shown_start = offset + 1
    shown_end = offset + len(entries)
    click.echo(f"{col_name}  (showing {shown_start}-{shown_end} of {total})\n")
    from datetime import datetime, timedelta  # noqa: PLC0415  — stdlib deferred to call site (datetime)
    for e in entries:
        doc_id = e.get("id", "")  # RDR-180: full id — the list->get handle must round-trip
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
    except Exception:  # noqa: BLE001 — collection-open failure (incl. KeyError) surfaced to user via click.echo, returns
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
    # extraction_method / page_count not in ALLOWED_TOP_LEVEL — normalize()
    # drops them so the reads always returned empty. Removed in nexus-59j0.
    for i, d in enumerate(docs, 1):
        title = (d.get("title") or "untitled")[:60]
        chunks = d.get("chunk_count", "?")
        indexed = (d.get("indexed_at") or "")[:10]
        click.echo(f"  {i:3d}. {title:<60}  {chunks:>4} chunks  {indexed}")



@store.command("get")
@click.argument("doc_id")
@click.option("--collection", "-c", default="knowledge", show_default=True,
              help="Collection name or prefix (default: knowledge)")
@click.option("--json", "json_out", is_flag=True, default=False,
              help="Output as JSON")
def get_cmd(doc_id: str, collection: str, json_out: bool) -> None:
    """Retrieve a T3 knowledge entry by its document ID.

    DOC_ID is the 64-char content-hash ID shown by 'nx store list'
    (the full sha256(chunk_text) hexdigest — RDR-180; pre-RDR-180
    stores used the [:32] prefix form).

    \b
    Examples:
      nx store get a1b2c3d4e5f6789012345678901234abcdef0123456789abcdef0123456789ab
      nx store get a1b2c3d4e5f6789012345678901234abcdef0123456789abcdef0123456789ab --collection code__myrepo --json
    """
    db = _t3()
    col_name = t3_collection_name(collection, t3=db)
    entry = db.get_by_id(col_name, doc_id)
    if entry is None:
        raise click.ClickException(f"Entry {doc_id!r} not found in {col_name}")

    if json_out:
        import json  # noqa: PLC0415 — stdlib import kept branch-local
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
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = None
    writer = None
    try:
        # nexus-kmo9h: presence semantics belong to the factory (None only
        # in SQLite opt-out mode when uninitialised) — the old local
        # is_initialized gate silently skipped the post-delete catalog
        # tombstone reap on every fresh service-mode box.
        reader = make_catalog_reader()
        if reader is None:
            return
        writer = make_catalog_writer()
        for doc_id in doc_ids:
            entry = reader.by_doc_id(doc_id)
            if entry is not None:
                writer.delete_document(entry.tumbler)
    except Exception:  # noqa: BLE001 — best-effort catalog reap; failure logged at debug, cleanup in finally
        _log.debug("catalog_reap_failed", exc_info=True, doc_ids=doc_ids)
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()


@store.command("delete")
@click.option("--collection", "-c", required=True,
              help="Collection name (required)")
@click.option("--id", "doc_id", default=None,
              help="Exact 64-char content-hash document ID from 'nx store list'")
@click.option("--title", default=None,
              help="Exact title metadata match (deletes all matching chunks)")
@click.option("--yes", "-y", is_flag=True, default=False,
              help="Skip confirmation prompt")
def delete_cmd(collection: str, doc_id: str | None, title: str | None, yes: bool) -> None:
    """Delete an entry from a T3 knowledge collection.

    Use --id for a single known entry, --title to delete all chunks of a document.
    To remove an entire collection use: nx collection delete <name>

    Note (RDR-108 D1 / RDR-180): T3 chunk natural IDs are content-derived
    (the full sha256(text) hexdigest). Two documents with different titles but
    identical content share one Chroma row; deleting one --title
    removes the shared row, which also removes the other title's
    content. If you need both titles to remain, store them under
    distinct content.
    """
    if not doc_id and not title:
        raise click.UsageError("provide --id or --title")
    if doc_id and title:
        raise click.UsageError("--id and --title are mutually exclusive")

    db = _t3()
    col_name = t3_collection_name(collection, t3=db)

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
    from datetime import date  # noqa: PLC0415 — stdlib import kept branch-local

    from nexus.corpus import t3_collection_name as _t3col  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.errors import EmbeddingModelMismatch, FormatVersionError  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.exporter import export_collection  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

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
            except Exception as exc:  # noqa: BLE001 — per-collection export failure surfaced via click.echo, loop continues
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
@click.option("--assume-model", default=None,
              help="Override the export header's declared embedding model. "
                   "Pre-migration .nxexp files can carry a wrong label (GH #1370); "
                   "use this to supply the true model instead of trusting the header.")
@click.option("--skip-existing", is_flag=True, default=False,
              help="Skip records whose id already exists in the target collection, "
                   "instead of overwriting. Useful for resuming a partial import.")
def import_cmd(
    file: str,
    collection: str | None,
    remaps: tuple[str, ...],
    assume_model: str | None,
    skip_existing: bool,
) -> None:
    """Import a .nxexp export file into T3.

    Embedding model validation is enforced: importing a code__ export into a
    docs__ collection (or vice versa) is rejected to prevent silent corruption
    of the target collection's vector space. Non-conformant legacy chunk ids
    (pre-migration backups) are re-hashed to content-derived ids automatically.

    \b
    Examples:
      nx store import myrepo-backup.nxexp
      nx store import myrepo-backup.nxexp --remap "/old/path:/new/path"
      nx store import myrepo-backup.nxexp --collection code__newname
      nx store import old-backup.nxexp --assume-model bge-base-en-v15-768
      nx store import partial-backup.nxexp --skip-existing
    """
    from nexus.errors import (  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
        EmbeddingDimensionMismatch,
        EmbeddingModelMismatch,
        FormatVersionError,
    )
    from nexus.exporter import import_collection  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

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
            assume_model=assume_model,
            skip_existing=skip_existing,
        )
    except FormatVersionError as exc:
        raise click.ClickException(str(exc)) from exc
    except EmbeddingDimensionMismatch as exc:
        raise click.ClickException(str(exc)) from exc
    except EmbeddingModelMismatch as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(f"Import failed: {exc}") from exc

    click.echo(
        f"Imported {result['imported_count']} records into "
        f"{result['collection_name']}  ({result['elapsed_seconds']:.1f}s)"
    )
    if result.get("skipped_count"):
        click.echo(f"  Skipped {result['skipped_count']} existing records (--skip-existing).")
    if result.get("rehashed_count"):
        click.echo(
            f"  Re-hashed {result['rehashed_count']} non-conformant legacy "
            "chunk ids to conformant content hashes."
        )
