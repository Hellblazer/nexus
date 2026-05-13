# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import sys
from collections.abc import Callable
from typing import Any

import click
import structlog

from nexus.commands.store import _t3
from nexus.corpus import embedding_model_for_collection, index_model_for_collection

_log = structlog.get_logger(__name__)


def _doc_id_to_file_path(doc_id: str) -> str:
    """nexus-7b5n: resolve a chunk's ``doc_id`` to the catalog's
    ``file_path``. Returns "" when the catalog is uninitialized or has
    no entry. Used by ``reindex_cmd``'s pre-delete safety check so
    post-prune chunks (which lack ``source_path``) still drive the
    reindex via the catalog projection. Best-effort; any failure
    returns "" and the caller treats the chunk as sourceless.
    """
    try:
        from nexus.catalog import Catalog, open_cached
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return ""
        # nexus-6xqk follow-up: process-cached singleton avoids the
        # storm of _ensure_consistent rebuilds when this helper fires
        # per-chunk on a large collection.
        cat = open_cached(cat_path)
        entry = cat.by_doc_id(doc_id)
        if entry is None:
            return ""
        return entry.file_path or ""
    except Exception:
        return ""


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

    from nexus.config import is_local_mode
    if is_local_mode():
        from nexus.db.local_ef import LocalEmbeddingFunction
        ef = LocalEmbeddingFunction()
        query_model = idx_model = f"{ef.model_name} (local)"
    else:
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
    """Delete a T3 collection + cascade-purge taxonomy state (irreversible)."""
    if not yes:
        click.confirm(f"Delete collection '{name}'? This cannot be undone.", abort=True)

    # nexus-lub: T3 delete may fail with NotFoundError when the caller
    # is recovering from an orphan-taxonomy state where the collection
    # was deleted previously but the cascade didn't run (pre-4.5.0). We
    # still run the cascade in that case so the orphan rows are cleaned.
    # Any other T3 error aborts before cascade.
    from chromadb.errors import NotFoundError as _ChromaNotFoundError
    t3_absent = False
    try:
        _t3().delete_collection(name)
    except _ChromaNotFoundError:
        t3_absent = True
        click.echo(
            f"note: T3 collection '{name}' already absent — running cascade anyway",
            err=True,
        )

    # Cascade-purge taxonomy state (topics, assignments, links, meta)
    # so `nx taxonomy status` / hub detection don't drag ghost rows.
    # RDR-086 Phase 1.4: same block also purges chash_index so Phase 2's
    # Catalog.resolve_chash never returns (collection, doc_id) tuples
    # pointing at chunks that no longer exist in T3.
    taxonomy_counts: dict[str, int] | None = None
    chash_deleted = 0
    try:
        from nexus.commands._helpers import default_db_path
        from nexus.db.t2 import T2Database

        with T2Database(default_db_path()) as db:
            taxonomy_counts = db.taxonomy.purge_collection(name)
            chash_deleted = db.chash_index.delete_collection(name)
    except Exception as exc:
        prefix = "absent" if t3_absent else "succeeded"
        click.echo(
            f"warn: T3 delete {prefix} but T2 cascade failed: {exc}",
            err=True,
        )

    # nexus-8a8e: purge streaming-pipeline rows keyed to this collection.
    # pdf_pipeline.status='completed' otherwise makes the next `nx index pdf`
    # return "skip" (0 chunks) for every content_hash that was previously
    # indexed into *name*, even though T3 + T2 are now empty.
    pipeline_rows_deleted = 0
    try:
        from nexus.pipeline_buffer import PIPELINE_DB_PATH, PipelineDB
        pipeline_rows_deleted = PipelineDB(PIPELINE_DB_PATH).delete_pipeline_data_for_collection(name)
    except Exception as exc:
        click.echo(f"warn: pipeline-state cleanup failed: {exc}", err=True)

    # nexus-jm3z: cascade to catalog. Pre-fix, `nx collection delete`
    # left catalog.documents rows whose physical_collection pointed at
    # the now-gone collection, plus a stale catalog.collections
    # projection row. Both surfaced as doctor FAILs (t3-vs-catalog +
    # collections-drift) requiring per-tumbler manual cleanup. Run the
    # cascade here so a single delete leaves the catalog clean.
    catalog_docs_deleted = 0
    catalog_projection_deleted = 0
    try:
        from nexus.catalog.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if Catalog.is_initialized(cat_path):
            cat = Catalog(cat_path, cat_path / ".catalog.db")
            # Delete every document row pointing at the gone collection.
            # Use the public delete_document so the event log + JSONL
            # tombstone stay in sync with the SQLite projection.
            orphan_tumblers = [
                row[0]
                for row in cat._db.execute(
                    "SELECT tumbler FROM documents WHERE physical_collection = ?",
                    (name,),
                ).fetchall()
            ]
            for tumbler in orphan_tumblers:
                try:
                    if cat.delete_document(tumbler):
                        catalog_docs_deleted += 1
                except Exception:
                    _log.debug(
                        "collection_delete_cascade_document_failed",
                        tumbler=tumbler, exc_info=True,
                    )
            # nexus-vxz3: emit CollectionDeleted event so replay
            # produces SQLite state matching the live delete. The
            # projector's _v0_collection_deleted handler drops the
            # projection row.
            from nexus.catalog.events import CollectionDeletedPayload
            from nexus.catalog import catalog as _cat_mod

            row_before = cat._db.execute(
                "SELECT 1 FROM collections WHERE name = ?", (name,),
            ).fetchone()
            if row_before is not None:
                event = _cat_mod._make_event(
                    CollectionDeletedPayload(
                        coll_id=name,
                        reason="nx collection delete",
                    ),
                    v=0,
                )
                if cat._event_sourced_enabled:
                    cat._write_to_event_log(event)
                    cat._projector.apply(event)
                    cat._db.commit()
                else:
                    cat._db.execute(
                        "DELETE FROM collections WHERE name = ?",
                        (name,),
                    )
                    cat._db.commit()
                    cat._emit_shadow_event(event)
                catalog_projection_deleted = 1
    except Exception as exc:
        click.echo(f"warn: catalog cascade failed: {exc}", err=True)

    parts: list[str] = []
    if taxonomy_counts and any(taxonomy_counts.values()):
        parts.append(
            f"{taxonomy_counts['topics']} topics, "
            f"{taxonomy_counts['assignments']} assignments, "
            f"{taxonomy_counts['links']} links, "
            f"{taxonomy_counts['meta']} meta"
        )
    if chash_deleted:
        parts.append(f"{chash_deleted} chash rows")
    if pipeline_rows_deleted:
        parts.append(f"{pipeline_rows_deleted} pipeline rows")
    if catalog_docs_deleted:
        parts.append(f"{catalog_docs_deleted} catalog docs")
    if catalog_projection_deleted:
        parts.append(f"{catalog_projection_deleted} catalog projection row")
    if parts:
        click.echo(f"Deleted: {name} ({'; '.join(parts)})")
    else:
        click.echo(f"Deleted: {name}")


# nexus-8g79.10 (V5): rename_collection_data_plane extracted to
# nexus.collection_rename (peer to indexer.py) so the indexer's
# RDR-103 P5 conformant-shape migrator at indexer.py:435 doesn't
# reach up into commands/. The library version requires explicit
# ``t3_db``; CLI callers within this module pass ``_t3()``.
from nexus.collection_rename import rename_collection_data_plane as _rename_collection_data_plane  # noqa: E402


def rename_collection_data_plane(
    old: str,
    new: str,
    *,
    t3_db=None,
    catalog: Any | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """CLI-shape wrapper: default ``t3_db=_t3()`` when omitted.

    See ``nexus.collection_rename.rename_collection_data_plane`` for
    the full data-plane contract. This wrapper preserves the
    ``t3_db=None`` → ``_t3()`` convenience used by the CLI rename
    command and by the orphan-collection cleanup paths.
    """
    if t3_db is None:
        t3_db = _t3()
    return _rename_collection_data_plane(
        old, new, t3_db=t3_db, catalog=catalog, on_warn=on_warn,
    )


@collection.command("rename")
@click.argument("old")
@click.argument("new")
@click.option(
    "--force-prefix-change",
    is_flag=True,
    help=(
        "Allow a cross-prefix rename (e.g. code__foo → docs__foo). "
        "Embedding-model spaces differ across prefixes, so the renamed "
        "collection is query-incompatible with its old clients. Use only "
        "when you've deleted every downstream reader of the old name."
    ),
)
def rename_cmd(old: str, new: str, force_prefix_change: bool) -> None:
    """Rename a collection in-place via ChromaDB's native modify(name=).

    O(1) metadata update — no embedding re-upload, no Voyage cost,
    no ChromaDB egress. Cascades the new name through T2 taxonomy,
    chash_index, and catalog (JSONL + SQLite).

    Cross-prefix renames (e.g. ``code__`` ↔ ``docs__``) change the
    embedding-model space and are rejected unless ``--force-prefix-change``
    is set; otherwise search hits would be garbage.
    """
    old_prefix = old.split("__", 1)[0] if "__" in old else ""
    new_prefix = new.split("__", 1)[0] if "__" in new else ""
    if old_prefix != new_prefix and not force_prefix_change:
        raise click.ClickException(
            f"prefix mismatch: {old_prefix!r} → {new_prefix!r} would change "
            f"the embedding-model space. Pass --force-prefix-change if this "
            f"is intentional (rare — usually means the caller is resurrecting "
            f"an orphaned collection with the wrong prefix)."
        )

    counts = rename_collection_data_plane(old, new)

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
    if parts:
        click.echo(f"Renamed: {old} → {new} ({'; '.join(parts)})")
    else:
        click.echo(f"Renamed: {old} → {new}")


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

    # 2. Pre-delete safety: paginate for sourceless entries (nexus-unyc).
    # nexus-7b5n: a chunk is "reindexable" when it carries either
    # ``source_path`` (legacy chunks predating the doc_id backfill) or
    # ``doc_id`` (post-Phase-4 chunks). After the prune verb drops
    # source_path from chunk metadata, doc_id is the only signal; the
    # catalog row pointed to by doc_id holds the file_path used for
    # the actual reindex.
    #
    # nexus-vn48 (RDR-108 Phase 4 review D-M1): RDR-108 Phase 3
    # (nexus-bdag) removed doc_id from chunk metadata too. For
    # Phase-3 chunks both source_path and doc_id are gone; the
    # only remaining signal is ``chunk_text_hash``. Resolve via
    # the catalog's chash -> doc_id manifest reverse-lookup, then
    # fall through to the existing _doc_id_to_file_path helper.
    col = db.get_or_create_collection(name)
    sourceless: list[str] = []
    source_paths: set[str] = set()
    offset = 0
    # Build a one-shot catalog handle for the manifest fallback.
    _cat = None
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path
        _cp = catalog_path()
        if Catalog.is_initialized(_cp):
            _cat = Catalog(_cp, _cp / ".catalog.db")
    except Exception:
        pass
    while True:
        batch = col.get(limit=300, offset=offset, include=["metadatas"])
        # nexus-vn48: page-batch chash -> doc_id resolution to amortise
        # SQLite calls across the page rather than per-chunk.
        page_chashes = [
            (m or {}).get("chunk_text_hash", "")
            for m in (batch["metadatas"] or [])
        ]
        page_chashes_nonempty = [c for c in page_chashes if c]
        chash_to_doc: dict[str, str] = {}
        if _cat is not None and page_chashes_nonempty:
            try:
                by_chash = _cat.docs_for_chashes(page_chashes_nonempty)
            except Exception:
                by_chash = {}
            for c, doc_ids in by_chash.items():
                if doc_ids:
                    chash_to_doc[c] = sorted(doc_ids)[0]

        for mid, meta in zip(batch["ids"], batch["metadatas"] or []):
            meta = meta or {}
            sp = meta.get("source_path", "")
            did = meta.get("doc_id", "")
            # Phase-3 fallback: resolve via manifest when metadata
            # lacks doc_id but carries chunk_text_hash.
            if not did:
                chash = meta.get("chunk_text_hash", "")
                if chash:
                    did = chash_to_doc.get(chash, "")
            if sp:
                source_paths.add(sp)
            elif did:
                # Post-prune chunk: source_path was dropped but the
                # catalog still holds the file_path keyed on doc_id.
                # Resolve it so the reindex driver below has the path.
                resolved = _doc_id_to_file_path(did)
                if resolved:
                    source_paths.add(resolved)
                else:
                    # Catalog gap: treat as sourceless so the safety
                    # check fires. Post-iftc, operator restores the
                    # catalog by deleting the catalog directory and
                    # re-running ``nx catalog setup`` (the
                    # synthesize-log + t3-backfill-doc-id verbs that
                    # historically repaired this case were retired).
                    sourceless.append(mid)
            else:
                sourceless.append(mid)
        if len(batch["ids"]) < 300:
            break
        offset += 300

    # If EVERY entry is sourceless, --force does nothing useful — there is
    # no source to reindex from, so the operation collapses to "delete the
    # collection". GitHub #367: a user lost 28 store_put-only entries this
    # way during an embedding-model migration. Refuse unconditionally and
    # point at `nx collection delete` for the genuine-delete path.
    if sourceless and not source_paths:
        raise click.ClickException(
            f"Refusing to reindex '{name}': all {len(sourceless)} entries "
            f"lack source_path (e.g. manual store_put entries, "
            f"taxonomy__centroids, or other programmatically-populated "
            f"collections). There is no source to re-index from — this "
            f"would destroy every chunk with no recovery path.\n\n"
            f"  • If you want to delete the collection, run:\n"
            f"      nx collection delete {name}\n"
            f"  • In-place re-embedding (preserve content, swap embedding "
            f"model) is not yet supported. Track at GitHub #367.\n\n"
            f"--force does not bypass this check — there is nothing to force."
        )

    if sourceless and not force:
        raise click.ClickException(
            f"{len(sourceless)} entries lack source_path (manual entries) "
            f"and {len(source_paths)} have source files. The {len(sourceless)} "
            f"sourceless entries cannot be re-indexed and will be LOST. "
            f"Use --force to proceed and accept that loss."
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

    # GH #369: run the same post-processing chain ``nx index repo``
    # runs after a successful index — taxonomy discover, Claude
    # labeling, cross-collection projection, cooccurrence links,
    # topic-link compute, L1 context refresh. Pre-fix the chain only
    # fired from index_repo_cmd, so a bulk reindex (e.g. after an
    # embedding-model upgrade) left the catalog/taxonomy/links stale
    # and the operator had to know to re-run ``nx index repo`` per
    # repo. ``run_collection_postprocessing`` is now the shared entry
    # point.
    if indexed > 0:
        try:
            from nexus.commands.index import run_collection_postprocessing
            # Resolve repo_path from the registry when available so the
            # L1 context refresh fires; falls back to ``None`` for
            # collections with no registered owner (the L1 step is the
            # only thing in the chain that requires a path, and it
            # short-circuits cleanly when ``repo_path`` is ``None``).
            repo_path: Path | None = None
            try:
                from nexus.indexer import RepoRegistry
                from nexus.commands._helpers import default_db_path  # noqa: F401
                from nexus.config import nexus_config_dir
                reg = RepoRegistry(nexus_config_dir() / "repos.json")
                for rp_str, info in reg.all_info().items():
                    coll = info.get("collection") or info.get("docs_collection")
                    if coll == name or info.get("docs_collection") == name:
                        repo_path = Path(rp_str)
                        break
            except Exception:
                pass  # repo-path resolution is best-effort
            run_collection_postprocessing([name], repo_path=repo_path)
        except Exception as exc:
            click.echo(
                f"Note: post-processing (taxonomy / links / context) "
                f"failed: {exc}. Run `nx index repo <path>` to retry.",
                err=True,
            )


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

# Collections whose rows store embedding + label metadata only — no document
# text — so chunk_text_hash backfill cannot meaningfully process them. Walking
# them produces one ``backfill_chunk_text_hash_none_doc`` warning per row with
# no actionable signal. nexus-uebj.
_DOCUMENTLESS_COLLECTIONS: frozenset[str] = frozenset({"taxonomy__centroids"})


def _backfill_chunk_text_hash(
    col,
    on_progress: Callable[[int, int, int], None] | None = None,
    *,
    chash_index: "ChashIndex | None" = None,
) -> tuple[int, int, int]:
    """Add chunk_text_hash to chunks that are missing it. Returns (updated, skipped, total).

    Args:
        col: ChromaDB collection.
        on_progress: Optional callback(updated, skipped, total) called after each batch.
        chash_index: Optional T2 store (RDR-086 Phase 1.3). When provided, every
            chunk that has, or gains, a chunk_text_hash is registered as a
            ``(chash, physical_collection)`` row. Reconciles gaps left by
            Phase 1.2 dual-write failures and pre-Phase-1 collections. Pass
            ``None`` (default) to preserve the T3-only behaviour that legacy
            callers in ``commands/catalog.py`` still rely on.

    Implementation (nexus-o9an): two-pass walk, mirrors
    ``reidentify_collection``. Pass 1 paginates ``col.get(include=[])``
    to collect every chunk id; pass 2 fetches by exact id and re-upserts
    chunks needing the hash with a canonical-schema-normalized payload.

    Why two-pass: ChromaDB Cloud's offset-based ``col.get`` is order-
    unstable, so a naive ``offset += len(ids)`` loop can revisit some
    chunks and miss others. The two-pass design sidesteps this entirely
    (pass 2's exact-id lookups are deterministic).

    Why upsert + normalize instead of update: many legacy collections
    carry chunks with 32+ metadata keys (pre-RDR-101-Phase-5c cargo +
    pre-RDR-108 doc/chunk fields). ``col.update`` MERGES metadata, so
    adding ``chunk_text_hash`` would push them to 33+ and trip the
    ChromaDB Cloud per-row ``NumMetadataKeys`` quota. The previous
    implementation caught the quota error and silently incremented
    ``skipped``, leaving the chunks broken. The upsert + normalize
    path REPLACES metadata via the canonical schema funnel, dropping
    cargo so the row lands back under quota.
    """
    if getattr(col, "name", "") in _DOCUMENTLESS_COLLECTIONS:
        return (0, 0, 0)

    from nexus.db.t2.chash_index import dual_write_chash_index
    from nexus.db.t3 import _normalize_for_write

    # Pass 1: collect every chunk id. Lightweight payload (no metadata,
    # no documents, no embeddings); offset is stable because pass 1 only
    # reads. ChromaDB Cloud's offset semantics are order-unstable across
    # long walks, but pass 1 (read-only, ids only) completes fast enough
    # for the single-operator scenario that the collection state stays
    # consistent. Best-effort only: concurrent indexer writes during
    # pass 1 may produce incomplete coverage on this iteration. The verb
    # is idempotent, so re-running picks up any chunks the previous pass
    # missed. nexus-2exh review caveat #4.
    all_ids: list[str] = []
    offset = 0
    while True:
        page = col.get(limit=_BACKFILL_BATCH, offset=offset, include=[])
        ids = page.get("ids") if isinstance(page, dict) else []
        if not ids or not isinstance(ids, list):
            break
        all_ids.extend(ids)
        if len(ids) < _BACKFILL_BATCH:
            break
        offset += _BACKFILL_BATCH

    updated = 0
    skipped = 0
    total = len(all_ids)
    coll_name = getattr(col, "name", "")

    # Pass 2: fetch by exact id (deterministic), then upsert chunks
    # needing the hash with canonical-schema-normalized metadata.
    for start in range(0, len(all_ids), _BACKFILL_BATCH):
        batch_ids = all_ids[start : start + _BACKFILL_BATCH]
        page = col.get(
            ids=batch_ids,
            include=["documents", "embeddings", "metadatas"],
        )
        page_ids = page.get("ids") or []
        page_docs = page.get("documents") or [None] * len(page_ids)
        page_embs = page.get("embeddings")
        if page_embs is None:
            page_embs = [None] * len(page_ids)
        page_metas = page.get("metadatas") or [{}] * len(page_ids)

        upsert_ids: list[str] = []
        upsert_docs: list[str] = []
        upsert_embs: list = []
        upsert_metas: list[dict] = []
        # Parallel lists for the T2 reconciliation write: ids + metas for
        # every row whose metadata ends this batch with a chunk_text_hash,
        # whether newly computed or previously present.
        t2_ids: list[str] = []
        t2_metas: list[dict] = []

        for chunk_id, doc, emb, meta in zip(
            page_ids, page_docs, page_embs, page_metas
        ):
            if meta and meta.get("chunk_text_hash"):
                skipped += 1
                # Reconciliation path: T3 already has the hash but T2 may not.
                t2_ids.append(chunk_id)
                t2_metas.append(dict(meta))
                continue
            if doc is None:
                # nexus-p03z: Cloud T3 occasionally returns rows whose
                # ``documents`` entry is None even when the chunk exists.
                # Hashing a missing doc is impossible; skip and keep
                # going.
                skipped += 1
                _log.warning(
                    "backfill_chunk_text_hash_none_doc",
                    chunk_id=chunk_id,
                    collection=coll_name,
                )
                continue
            new_meta = dict(meta) if meta else {}
            new_meta["chunk_text_hash"] = hashlib.sha256(
                doc.encode()
            ).hexdigest()
            # Canonical schema funnel: drops cargo (corpus, store_type,
            # expires_at, extraction_method, etc) so chunks with 32+
            # keys land back under the per-row metadata quota.
            normalized = _normalize_for_write(new_meta, coll_name)
            upsert_ids.append(chunk_id)
            upsert_docs.append(doc)
            upsert_embs.append(emb)
            upsert_metas.append(normalized)
            t2_ids.append(chunk_id)
            t2_metas.append(normalized)

        if upsert_ids:
            try:
                col.upsert(
                    ids=upsert_ids,
                    documents=upsert_docs,
                    embeddings=upsert_embs,
                    metadatas=upsert_metas,
                )
                updated += len(upsert_ids)
            except Exception as exc:
                exc_msg = str(exc)
                if "Quota exceeded" in exc_msg or "NumMetadataKeys" in exc_msg:
                    # Even after normalization the row is over quota;
                    # operator must re-index from source. Count as
                    # skipped so the totals still balance.
                    skipped += len(upsert_ids)
                    _log.warning(
                        "backfill_chunk_text_hash_quota_after_normalize",
                        collection=coll_name,
                        affected=len(upsert_ids),
                    )
                else:
                    raise
        if chash_index is not None and t2_ids:
            # Best-effort: dual_write_chash_index swallows per-row failures.
            dual_write_chash_index(chash_index, coll_name, t2_ids, t2_metas)
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

    # RDR-086 Phase 1.3: open a single long-lived ChashIndex connection for
    # the whole backfill run so each collection reuses it instead of opening
    # a fresh sqlite3 connection per chunk batch.
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2.chash_index import ChashIndex
    from tqdm import tqdm

    chash_index = ChashIndex(default_db_path())
    try:
        grand_updated = 0
        for i, col_name in enumerate(sorted(targets), 1):
            try:
                col = db._client.get_collection(col_name)
            except Exception as exc:
                click.echo(f"  [{i}/{len(targets)}] {col_name}: {type(exc).__name__}, skipping", err=True)
                continue

            # Query collection count so tqdm has a known total. On quota
            # failure, fall back to an indeterminate bar.
            try:
                col_total = col.count()
            except Exception:
                col_total = 0

            # disable=None lets tqdm auto-detect TTY — bar shows in an
            # interactive terminal, silently no-ops in CI logs. The
            # per-collection click.echo summary below is always emitted.
            bar = tqdm(
                total=col_total or None,
                disable=None,
                desc=f"[{i}/{len(targets)}] {col_name}",
                unit="chunk",
                leave=False,
            )

            def _progress(updated: int, skipped: int, total: int) -> None:
                # total = cumulative scanned so far; update bar position.
                bar.n = total
                bar.refresh()

            try:
                updated, skipped, total_count = _backfill_chunk_text_hash(
                    col, on_progress=_progress, chash_index=chash_index,
                )
            finally:
                bar.close()

            grand_updated += updated
            if updated:
                click.echo(f"  [{i}/{len(targets)}] {col_name}: {updated} updated, {skipped} already had hash ({total_count} total)")
            else:
                click.echo(f"  [{i}/{len(targets)}] {col_name}: all {total_count} chunks already have hash")

        click.echo(f"Done: {grand_updated} chunks updated across {len(targets)} collection(s)")
    finally:
        chash_index.close()


_REEMBED_SUPPORTED_MODELS = ("voyage-3", "voyage-code-3")


def _reembed_collection(
    db,
    col_name: str,
    target_model: str,
    *,
    dry_run: bool,
    on_progress=None,
) -> tuple[int, int]:
    """Re-embed every chunk in *col_name* with *target_model*.

    Preserves chunk id, document text, and metadata. Only the embedding
    vector changes. Returns ``(processed, skipped)``.

    nexus-bw65: in-place re-embed for non-CCE Voyage models. CCE
    (``voyage-context-3``) requires sliding-window context across chunks
    and is intentionally out of scope; the CLI rejects it up front.
    """
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.retry import _voyage_with_retry

    try:
        col = db._client.get_collection(col_name)
    except Exception as exc:
        raise click.ClickException(
            f"collection {col_name!r}: {type(exc).__name__}: {exc}"
        )

    total = col.count()
    if total == 0:
        return 0, 0

    voyage_client = None
    if not dry_run:
        from nexus.config import get_credential

        key = get_credential("voyage_api_key")
        if not key:
            raise click.ClickException(
                "VOYAGE_API_KEY not set; cannot re-embed without it. "
                "(Dry-run only requires read access.)"
            )
        import voyageai  # noqa: PLC0415

        voyage_client = voyageai.Client(api_key=key)

    processed = 0
    skipped = 0
    page = QUOTAS.MAX_QUERY_RESULTS  # 300
    voyage_batch = 128  # Voyage embed API batch limit
    offset = 0
    while offset < total:
        page_rows = col.get(
            limit=page, offset=offset,
            include=["documents", "metadatas"],
        )
        ids = page_rows.get("ids") or []
        documents = page_rows.get("documents") or []
        metadatas = page_rows.get("metadatas") or []
        if not ids:
            break

        valid_idx = [
            i for i, d in enumerate(documents)
            if isinstance(d, str) and d.strip()
        ]
        skipped += len(ids) - len(valid_idx)
        if not valid_idx:
            offset += page
            continue

        v_ids = [ids[i] for i in valid_idx]
        v_docs = [documents[i] for i in valid_idx]
        v_metas = [metadatas[i] for i in valid_idx]

        if not dry_run:
            embeddings: list[list[float]] = []
            for batch_start in range(0, len(v_docs), voyage_batch):
                batch = v_docs[batch_start : batch_start + voyage_batch]
                result = _voyage_with_retry(
                    voyage_client.embed,
                    texts=batch,
                    model=target_model,
                    input_type="document",
                )
                embeddings.extend(result.embeddings)
            # Stamp the new model on metadata so check_staleness reads
            # right post-re-embed; otherwise next index pass would treat
            # every chunk as stale and re-embed twice.
            for m in v_metas:
                if isinstance(m, dict):
                    m["embedding_model"] = target_model
            db.upsert_chunks_with_embeddings(
                collection_name=col_name,
                ids=v_ids,
                documents=v_docs,
                embeddings=embeddings,
                metadatas=v_metas,
            )
            # nexus-bw65 / nexus-9099: fire post-store chains so the
            # invariant 'every CLI T3 write also fires the chain'
            # (test_every_cli_t3_write_function_fires_store_chains)
            # holds. Re-embed preserves doc_id / chash / manifest
            # position, so the chain's hooks (chash_dual_write,
            # taxonomy_assign, manifest_write) re-touch existing rows
            # idempotently.
            from nexus.mcp_infra import fire_store_chains

            fire_store_chains(
                v_ids, col_name, v_docs,
                source_paths=[
                    (m.get("source_path", "") if isinstance(m, dict) else "")
                    for m in v_metas
                ],
                embeddings=embeddings,
                metadatas=v_metas,
                catalog_doc_id="",
            )

        processed += len(v_ids)
        if on_progress is not None:
            on_progress(processed, total)
        offset += page

    return processed, skipped


@collection.command("re-embed")
@click.argument("name")
@click.option(
    "--to", "target_model", required=True,
    type=click.Choice(_REEMBED_SUPPORTED_MODELS),
    help="Target embedding model (CCE models like voyage-context-3 are "
         "intentionally not supported — see nexus-bw65).",
)
@click.option("--dry-run/--no-dry-run", default=True,
              help="Default dry-run. Pass --no-dry-run to actually write.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip the destructive-action confirmation prompt.")
def reembed_cmd(
    name: str, target_model: str, dry_run: bool, yes: bool,
) -> None:
    """In-place re-embed: preserve content, swap embedding model.

    nexus-bw65: rebuild embeddings for every chunk in NAME using
    --to MODEL. Chunk ids, document text, and metadata are
    preserved; only the vector changes.

    Use case: an embedding-model upgrade on a sourceless collection
    (store_put-only / MCP-promoted notes). For source-backed
    collections prefer ``nx collection reindex`` so the indexer
    re-derives chunk boundaries with the new chunker contract.

    Limitations:
      - Only non-CCE Voyage models are supported. Contextualized
        Chunk Embeddings (voyage-context-3) require sliding-window
        context across chunks and need a different pipeline.
      - The collection's name often encodes the embedding model
        (RDR-103 / nexus-1-1__voyage-code-3__v1). This command does
        NOT rename the collection; run ``nx collection rename`` if
        the name needs to track the new model.
    """
    if not dry_run and not yes:
        click.confirm(
            f"Re-embed {name!r} with {target_model!r}? This rewrites "
            f"every chunk's vector in place.",
            abort=True,
        )

    db = _t3()
    if dry_run:
        col = db._client.get_collection(name)
        n = col.count()
        click.echo(
            f"dry-run: would re-embed {n} chunk(s) in {name!r} with "
            f"{target_model!r}. Pass --no-dry-run --yes to apply."
        )
        return

    processed, skipped = _reembed_collection(
        db, name, target_model, dry_run=False,
    )
    click.echo(
        f"re-embedded {processed} chunk(s) in {name!r} with "
        f"{target_model!r}; skipped {skipped} empty-document row(s)."
    )


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


@collection.command("health")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice([
        "name", "chunk_count", "last_indexed",
        "zero_hit_rate_30d", "median_query_distance_30d",
        "cross_projection_rank", "orphan_catalog_rows",
        "hub_domination_score",
    ]),
    default="name",
    show_default=True,
    help="Sort the health table by the named column.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
def health_cmd(sort_by: str, fmt: str) -> None:
    """Composite per-collection health report (RDR-087 Phase 3.4).

    Folds catalog, T2 telemetry, and topic-assignment signals into one
    row per collection. Use ``--format=json`` for agents and dashboards.
    """
    from nexus.collection_health import run_collection_health

    click.echo(run_collection_health(sort_by=sort_by, fmt=fmt))


@collection.command("audit")
@click.argument("name")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--live",
    is_flag=True,
    default=False,
    help=(
        "When the 30-day search_telemetry histogram is empty, sample N "
        "chunks from ChromaDB and derive the distance histogram from "
        "self-queries (budget ~10 s at N=25). Reuses stored embeddings "
        "— no Voyage API roundtrips."
    ),
)
@click.option(
    "--live-n",
    "live_n",
    type=int,
    default=25,
    show_default=True,
    help="Number of live-probe samples when --live fires.",
)
def audit_cmd(name: str, fmt: str, live: bool, live_n: int) -> None:
    """Deep-dive audit for a single collection (RDR-087 Phase 4.2).

    Five sections: distance histogram (30d telemetry, ``--live`` to
    probe ChromaDB when telemetry is cold), top-5 cross-projections,
    orphan chunks (>30d, no incoming links), top-10 cross-collection
    hub topic assignments, chash_index coverage.
    """
    from nexus.collection_audit import (
        format_audit_human,
        format_audit_json,
        run_collection_audit,
    )

    report = run_collection_audit(name, live=live, live_n=live_n)
    if fmt == "json":
        click.echo(format_audit_json(report))
    else:
        click.echo(format_audit_human(report))


@collection.command("merge-candidates")
@click.option(
    "--min-shared", "min_shared", type=int, default=3, show_default=True,
    help="Minimum distinct shared topics between two collections "
         "to qualify as a candidate.",
)
@click.option(
    "--min-similarity", "min_similarity", type=float, default=0.5,
    show_default=True,
    help="Minimum mean ``similarity`` across shared topics.",
)
@click.option(
    "--exclude-hubs", "exclude_hubs", is_flag=True, default=False,
    help="Drop top-N cross-collection hub topics from the shared-topic "
         "count before thresholding (reduces false positives from "
         "generic hubs).",
)
@click.option(
    "--hub-top-n", "hub_top_n", type=int, default=10, show_default=True,
    help="Hub depth used by --exclude-hubs.",
)
@click.option(
    "--limit", "limit", type=int, default=50, show_default=True,
    help="Max number of candidate pairs returned.",
)
@click.option(
    "--format", "fmt",
    type=click.Choice(["table", "json"]), default="table", show_default=True,
    help="Output format.",
)
@click.option(
    "--create-link", "create_link", is_flag=True, default=False,
    help="(deferred) Write catalog `relates`/`bridges` edges for each "
         "surfaced pair. Currently reports a deferred-workflow advisory "
         "per RDR §bridge-link — use nx catalog link manually.",
)
def merge_candidates_cmd(
    min_shared: int, min_similarity: float,
    exclude_hubs: bool, hub_top_n: int,
    limit: int, fmt: str, create_link: bool,
) -> None:
    """Pair-wise cross-collection overlap ranking (RDR-087 Phase 4.3).

    Surfaces (a, b) pairs where collection *a* projects into topics in
    collection *b* with high similarity — hints at merge or bridge-
    link opportunities for a human / agent to decide on. NEVER writes
    catalog edges automatically; --create-link is advisory and deferred.
    """
    if create_link:
        raise click.ClickException(
            "--create-link is deferred per RDR-087 §bridge-link workflow. "
            "Use `nx catalog link` manually after reviewing the candidates."
        )
    from nexus.merge_candidates import run_merge_candidates

    click.echo(
        run_merge_candidates(
            min_shared=min_shared,
            min_similarity=min_similarity,
            exclude_hubs=exclude_hubs,
            hub_top_n=hub_top_n,
            limit=limit,
            fmt=fmt,
        )
    )
