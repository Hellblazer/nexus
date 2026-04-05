# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command: nx enrich — backfill bibliographic metadata for a collection."""
from __future__ import annotations

import time

import click
import structlog

_log = structlog.get_logger(__name__)


@click.command()
@click.argument("collection")
@click.option(
    "--delay",
    default=0.5,
    type=float,
    show_default=True,
    help="Delay in seconds between Semantic Scholar API calls.",
)
@click.option(
    "--limit",
    default=0,
    type=int,
    help="Maximum number of titles to enrich (0 = unlimited).",
)
def enrich(collection: str, delay: float, limit: int) -> None:
    """Backfill bibliographic metadata for chunks in COLLECTION.

    Queries Semantic Scholar for each unique source title found in the
    collection and writes bib_year, bib_venue, bib_authors, bib_citation_count,
    and bib_semantic_scholar_id back to every chunk with that title.

    Already-enriched chunks (bib_semantic_scholar_id is non-empty) are skipped
    — the command is idempotent.
    """
    from nexus.bib_enricher import enrich as bib_enrich
    from nexus.db import make_t3
    from nexus.retry import _chroma_with_retry

    db = make_t3()
    col = db.get_or_create_collection(collection)

    # Process incrementally: one batch at a time to bound memory usage.
    title_to_ids: dict[str, list[str]] = {}
    already_enriched = 0
    total_chunks = 0
    offset = 0
    while True:
        batch = _chroma_with_retry(
            col.get,
            include=["metadatas"],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        batch_meta = batch.get("metadatas", [])
        total_chunks += len(batch_ids)
        for chunk_id, meta in zip(batch_ids, batch_meta):
            if meta.get("bib_semantic_scholar_id", ""):
                already_enriched += 1
                continue
            title = meta.get("source_title", "") or ""
            if not title:
                continue
            title_to_ids.setdefault(title, []).append(chunk_id)
        if len(batch_ids) < 300:
            break
        offset += 300

    if not total_chunks:
        click.echo(f"Collection '{collection}' is empty — nothing to enrich.")
        return

    titles_to_process = list(title_to_ids.items())
    if limit > 0:
        titles_to_process = titles_to_process[:limit]

    click.echo(
        f"Collection '{collection}': {total_chunks} total chunks, "
        f"{already_enriched} already enriched, "
        f"{len(titles_to_process)} titles to look up"
        + (f" (capped at {limit})" if limit > 0 else "")
        + "."
    )

    enriched_titles = 0
    enriched_chunks = 0
    skipped_titles = 0

    for i, (title, chunk_ids) in enumerate(titles_to_process):
        if i > 0:
            time.sleep(delay)

        bib = bib_enrich(title)
        if not bib:
            skipped_titles += 1
            _log.debug("enrich_no_result", title=title)
            continue

        # Build per-chunk metadata updates: ChromaDB update requires full
        # metadata dicts, so we fetch and merge.
        fetch = _chroma_with_retry(col.get, ids=chunk_ids, include=["metadatas"])
        fetched_ids = fetch.get("ids", [])
        fetched_meta = fetch.get("metadatas", [])

        updated_ids: list[str] = []
        updated_meta: list[dict] = []
        for cid, meta in zip(fetched_ids, fetched_meta):
            merged = dict(meta)
            merged["bib_year"] = bib.get("year", 0)
            merged["bib_venue"] = bib.get("venue", "")
            merged["bib_authors"] = bib.get("authors", "")
            merged["bib_citation_count"] = bib.get("citation_count", 0)
            merged["bib_semantic_scholar_id"] = bib.get("semantic_scholar_id", "")
            updated_ids.append(cid)
            updated_meta.append(merged)

        if updated_ids:
            _chroma_with_retry(col.update, ids=updated_ids, metadatas=updated_meta)
            enriched_chunks += len(updated_ids)
            enriched_titles += 1
            _log.debug(
                "enrich_updated",
                title=title,
                chunks=len(updated_ids),
                year=bib.get("year"),
                venue=bib.get("venue"),
            )
            _catalog_enrich_hook(title=title, bib_meta=bib, collection_name=collection)

    click.echo(
        f"Done: enriched {enriched_chunks} chunks across {enriched_titles} titles; "
        f"{skipped_titles} titles had no Semantic Scholar match."
    )


def _catalog_enrich_hook(title: str, bib_meta: dict, collection_name: str = "") -> None:
    """Update catalog entry with bib metadata. Silently skipped if absent."""
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # Prefer exact physical_collection lookup over FTS title search
        entry = None
        if collection_name:
            row = cat._db.execute(
                "SELECT tumbler FROM documents WHERE physical_collection = ? LIMIT 1",
                (collection_name,),
            ).fetchone()
            if row:
                from nexus.catalog.tumbler import Tumbler
                entry = cat.resolve(Tumbler.parse(row[0]))

        # Fallback to FTS title search
        if entry is None:
            entries = cat.find(title, content_type="paper")
            entry = entries[0] if entries else None

        if entry:
            cat.update(
                entry.tumbler,
                author=bib_meta.get("authors", ""),
                year=bib_meta.get("year", 0),
                meta={
                    "venue": bib_meta.get("venue", ""),
                    "bib_semantic_scholar_id": bib_meta.get("semantic_scholar_id", ""),
                    "citation_count": bib_meta.get("citation_count", 0),
                },
            )
    except Exception:
        _log.debug("catalog_enrich_hook_failed", exc_info=True)
