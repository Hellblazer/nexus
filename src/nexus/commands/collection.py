# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import sys
from collections.abc import Callable

import click

from nexus.commands.store import _t3
from nexus.corpus import embedding_model_for_collection, index_model_for_collection


@click.group()
def collection() -> None:
    """Manage ChromaDB collections (list, info, verify, delete)."""


@collection.command("list")
def list_cmd() -> None:
    """List all T3 collections with chunk counts."""
    cols = _t3().list_collections()
    if not cols:
        click.echo("No collections found.")
        return
    width = max(len(c["name"]) for c in cols)
    for c in sorted(cols, key=lambda x: x["name"]):
        click.echo(f"{c['name']:<{width}}  {c['count']:>6} chunks")


@collection.command("info")
@click.argument("name")
def info_cmd(name: str) -> None:
    """Show details for a single collection."""
    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")

    query_model = embedding_model_for_collection(name)
    idx_model   = index_model_for_collection(name)

    info = db.collection_info(name)

    col = db.get_or_create_collection(name)
    # Paginate for accurate MAX(indexed_at) timestamp (nexus-j857).
    all_timestamps: list[str] = []
    offset = 0
    while True:
        batch = col.get(limit=300, offset=offset, include=["metadatas"])
        for meta in batch.get("metadatas") or []:
            if meta and "indexed_at" in meta:
                all_timestamps.append(meta["indexed_at"])
        if len(batch.get("ids", [])) < 300:
            break
        offset += 300
    last_indexed = max(all_timestamps) if all_timestamps else "unknown"

    click.echo(f"Collection:  {match['name']}")
    click.echo(f"Chunks:      {match['count']}")
    click.echo(f"Index model: {idx_model}")
    click.echo(f"Query model: {query_model}")
    click.echo(f"Indexed:     {last_indexed}")


@collection.command("delete")
@click.argument("name")
@click.option("--yes", "-y", "--confirm", is_flag=True, help="Skip interactive confirmation prompt")
def delete_cmd(name: str, yes: bool) -> None:
    """Delete a T3 collection (irreversible)."""
    if not yes:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)
    _t3().delete_collection(name)
    click.echo(f"Deleted: {name}")


@collection.command("reindex")
@click.argument("name")
@click.option("--force", is_flag=True, help="Force reindex even if sourceless entries exist")
def reindex_cmd(name: str, force: bool) -> None:
    """Delete and re-index a collection from its source files."""
    from pathlib import Path

    from nexus.db.t3 import verify_collection_deep
    from nexus.doc_indexer import batch_index_markdowns, index_markdown, index_pdf

    db = _t3()

    # 1. Check collection exists
    try:
        info = db.collection_info(name)
    except KeyError:
        raise click.ClickException(f"collection not found: {name!r}")

    before_count = info["count"]

    # 2. Pre-delete safety: paginate for sourceless entries (nexus-unyc)
    col = db.get_or_create_collection(name)
    sourceless: list[str] = []
    source_paths: set[str] = set()
    offset = 0
    while True:
        batch = col.get(limit=300, offset=offset, include=["metadatas"])
        for mid, meta in zip(batch["ids"], batch["metadatas"] or []):
            sp = (meta or {}).get("source_path", "")
            if sp:
                source_paths.add(sp)
            else:
                sourceless.append(mid)
        if len(batch["ids"]) < 300:
            break
        offset += 300

    if sourceless and not force:
        raise click.ClickException(
            f"{len(sourceless)} entries lack source_path (manual entries). "
            f"These cannot be re-indexed and will be LOST. Use --force to proceed."
        )

    # 3. Delete collection
    click.echo(f"Deleting collection '{name}' ({before_count} chunks)...")
    db.delete_collection(name)

    # 5. Re-index based on collection type
    # Derive corpus from collection name so chunk metadata gets correct provenance.
    # e.g. "rdr__nexus-abc123" → "nexus-abc123", "docs__manual" → "manual"
    corpus = name.split("__", 1)[1] if "__" in name else ""

    indexed = 0
    missing: list[str] = []

    if name.startswith("code__"):
        click.echo(
            f"Re-indexing code collection — use 'nx index repo <path>' for full re-index"
        )
        click.echo(f"Source paths: {len(source_paths)} files")

    elif name.startswith("rdr__"):
        rdr_files = [Path(sp) for sp in source_paths if Path(sp).exists()]
        missing = [sp for sp in source_paths if not Path(sp).exists()]
        if rdr_files:
            click.echo(f"Re-indexing {len(rdr_files)} RDR documents...")
            try:
                batch_index_markdowns(
                    rdr_files, corpus=corpus, collection_name=name, force=True
                )
            except Exception as exc:
                click.echo(
                    f"Re-indexing failed: {exc}\n"
                    f"Collection '{name}' was deleted. Re-run 'nx collection reindex {name}' "
                    f"after resolving the error, or re-index manually.",
                    err=True,
                )
                raise click.exceptions.Exit(1)
            indexed = len(rdr_files)

    elif name.startswith("docs__") or name.startswith("knowledge__"):
        for sp in source_paths:
            p = Path(sp)
            if not p.exists():
                missing.append(sp)
                continue
            try:
                if p.suffix.lower() == ".pdf":
                    index_pdf(p, corpus=corpus, collection_name=name, force=True)
                else:
                    index_markdown(p, corpus=corpus, collection_name=name, force=True)
                indexed += 1
            except Exception as exc:
                click.echo(f"  Warning: failed to re-index {p.name}: {exc}", err=True)

    else:
        click.echo(f"Unknown collection type for '{name}' — no re-index strategy available")

    # 6. Warn about missing source files
    if missing:
        click.echo(f"Warning: {len(missing)} source files not found (moved or deleted)")
        for m in missing[:5]:
            click.echo(f"  - {m}")
        if len(missing) > 5:
            click.echo(f"  ... and {len(missing) - 5} more")

    # 7. Report before/after counts and verify
    try:
        after_info = db.collection_info(name)
        after_count = after_info["count"]
    except KeyError:
        after_count = 0

    click.echo(
        f"Re-indexed: {before_count} -> {after_count} chunks ({indexed} sources processed)"
    )

    if after_count >= 2:
        try:
            result = verify_collection_deep(db, name)
            dist_str = (
                f" (distance: {result.distance:.4f})" if result.distance is not None else ""
            )
            click.echo(f"Verify: {result.status}{dist_str}")
        except Exception as exc:
            click.echo(f"Verify failed: {exc}", err=True)


@collection.command("verify")
@click.argument("name")
@click.option("--deep", is_flag=True, help="Run embedding probe query to verify index health")
def verify_cmd(name: str, deep: bool) -> None:
    """Verify a collection exists and report its document count."""
    from nexus.db.t3 import verify_collection_deep

    db = _t3()
    cols = db.list_collections()
    match = next((c for c in cols if c["name"] == name), None)
    if match is None:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")

    if not deep:
        click.echo(f"Collection '{name}': {match['count']} chunks — OK")
        return

    try:
        result = verify_collection_deep(db, name)
    except KeyError:
        raise click.ClickException(f"collection not found: {name!r} — use: nx collection list")
    except Exception as exc:
        click.echo(
            f"embedding probe failed for '{name}': {exc} — check voyage_api_key with: nx config get voyage_api_key",
            err=True,
        )
        raise click.exceptions.Exit(1)

    if result.status == "skipped":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — skipped (too few for probe)"
        )
        return

    dist_str = (
        f" (distance: {result.distance:.4f}, {result.metric})"
        if result.distance is not None
        else ""
    )
    if result.probe_hit_rate is not None:
        hit_str = f" [{result.probe_hit_rate:.0%} probe hit rate]"
    else:
        hit_str = ""
    if result.status == "healthy":
        click.echo(f"Collection '{name}': {result.doc_count} chunks — embedding health OK{dist_str}{hit_str}")
    elif result.status == "degraded":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — DEGRADED: {result.probe_hit_rate:.0%} probe hit rate{dist_str}",
            err=True,
        )
        raise click.exceptions.Exit(1)
    elif result.status == "broken":
        click.echo(
            f"Collection '{name}': {result.doc_count} chunks — BROKEN: 0% probe hit rate (no probes found in top-10){dist_str}",
            err=True,
        )
        raise click.exceptions.Exit(1)


_BACKFILL_BATCH = 300


def _backfill_chunk_text_hash(
    col,
    on_progress: Callable[[int, int, int], None] | None = None,
) -> tuple[int, int, int]:
    """Add chunk_text_hash to chunks that are missing it. Returns (updated, skipped, total).

    Args:
        col: ChromaDB collection.
        on_progress: Optional callback(updated, skipped, total) called after each batch.
    """
    updated = 0
    skipped = 0
    total = 0
    offset = 0
    while True:
        batch = col.get(limit=_BACKFILL_BATCH, offset=offset, include=["documents", "metadatas"])
        ids = batch.get("ids") if isinstance(batch, dict) else []
        if not ids or not isinstance(ids, list):
            break
        update_ids: list[str] = []
        update_metas: list[dict] = []
        for chunk_id, doc, meta in zip(ids, batch["documents"], batch["metadatas"]):
            total += 1
            if meta and meta.get("chunk_text_hash"):
                skipped += 1
                continue
            new_meta = dict(meta) if meta else {}
            new_meta["chunk_text_hash"] = hashlib.sha256(doc.encode()).hexdigest()
            update_ids.append(chunk_id)
            update_metas.append(new_meta)
        if update_ids:
            try:
                col.update(ids=update_ids, metadatas=update_metas)
                updated += len(update_ids)
            except Exception as exc:
                exc_msg = str(exc)
                if "Quota exceeded" in exc_msg or "NumMetadataKeys" in exc_msg:
                    skipped += len(update_ids)  # count as skipped — too many metadata keys
                else:
                    raise
        offset += len(ids)
        if on_progress:
            on_progress(updated, skipped, total)
    return updated, skipped, total


@collection.command("backfill-hash")
@click.argument("name", required=False, default=None)
@click.option("--all", "all_collections", is_flag=True, help="Backfill all collections")
def backfill_hash_cmd(name: str | None, all_collections: bool) -> None:
    """Add chunk_text_hash to chunks missing it (no re-embedding).

    Reads each chunk's stored text from ChromaDB and computes
    sha256(text.encode()).hexdigest(). Updates metadata in-place —
    embeddings and documents are untouched.

    \\b
    Examples:
      nx collection backfill-hash code__myrepo   # single collection
      nx collection backfill-hash --all           # all collections
    """
    if not name and not all_collections:
        raise click.ClickException("specify a collection name or use --all")

    db = _t3()

    if all_collections:
        targets = [c["name"] for c in db.list_collections()]
    else:
        targets = [name]

    grand_updated = 0
    for i, col_name in enumerate(sorted(targets), 1):
        try:
            col = db._client.get_collection(col_name)
        except Exception as exc:
            click.echo(f"  [{i}/{len(targets)}] {col_name}: {type(exc).__name__}, skipping", err=True)
            continue

        def _progress(updated: int, skipped: int, total: int) -> None:
            msg = f"\r  [{i}/{len(targets)}] {col_name}: {updated} updated, {skipped} skipped, {total} scanned..."
            sys.stderr.write(msg)
            sys.stderr.flush()

        updated, skipped, total_count = _backfill_chunk_text_hash(col, on_progress=_progress)
        grand_updated += updated
        # Clear the progress line
        sys.stderr.write("\r" + " " * 120 + "\r")
        sys.stderr.flush()
        if updated:
            click.echo(f"  [{i}/{len(targets)}] {col_name}: {updated} updated, {skipped} already had hash ({total_count} total)")
        else:
            click.echo(f"  [{i}/{len(targets)}] {col_name}: all {total_count} chunks already have hash")

    click.echo(f"Done: {grand_updated} chunks updated across {len(targets)} collection(s)")


@collection.command("rewrite-metadata")
@click.argument("name", required=False, default=None)
@click.option("--all", "all_collections", is_flag=True,
              help="Rewrite metadata in every T3 collection.")
@click.option("--source-path", default=None,
              help="Only rewrite chunks whose source_path equals this value.")
@click.option("--dry-run", is_flag=True,
              help="Report counts without issuing any writes.")
def rewrite_metadata_cmd(
    name: str | None,
    all_collections: bool,
    source_path: str | None,
    dry_run: bool,
) -> None:
    """Rewrite each chunk's metadata to the canonical schema (nexus-2my).

    Operationalises the nexus-40t metadata schema rationalisation on
    already-indexed corpora. Chunks ingested before 4.3.1 keep their
    pre-canonical metadata (cargo keys, flat git_*, oversized records)
    until this command is run; ``nx index --force`` is a silent no-op
    when the pipeline-state DB still has the content_hash on file.

    \\b
    Examples:
      nx collection rewrite-metadata knowledge__delos
      nx collection rewrite-metadata knowledge__delos --source-path paper.pdf
      nx collection rewrite-metadata --all --dry-run
    """
    from nexus.db.t3 import _rewrite_collection_metadata

    if not name and not all_collections:
        raise click.ClickException("specify a collection name or use --all")
    if name and all_collections:
        raise click.ClickException("--all is mutually exclusive with NAME")

    db = _t3()
    targets = (
        sorted(c["name"] for c in db.list_collections())
        if all_collections else [name]
    )

    grand_updated = 0
    grand_skipped = 0
    grand_total = 0
    for i, col_name in enumerate(targets, 1):
        try:
            updated, skipped, total = _rewrite_collection_metadata(
                db, col_name,
                source_path=source_path,
                dry_run=dry_run,
            )
        except Exception as exc:
            click.echo(
                f"  [{i}/{len(targets)}] {col_name}: "
                f"{type(exc).__name__}: {exc}",
                err=True,
            )
            continue

        grand_updated += updated
        grand_skipped += skipped
        grand_total += total
        verb = "would rewrite" if dry_run else "rewrote"
        click.echo(
            f"  [{i}/{len(targets)}] {col_name}: {verb} {updated}, "
            f"skipped {skipped} (already canonical), {total} scanned"
        )

    verb = "Would rewrite" if dry_run else "Rewrote"
    click.echo(
        f"Done: {verb} {grand_updated} chunks "
        f"({grand_skipped} already canonical, {grand_total} scanned) "
        f"across {len(targets)} collection(s)."
    )
