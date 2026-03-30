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
def enrich(collection: str, delay: float) -> None:
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

    # Retrieve all chunks with their metadata (paginated).
    all_ids: list[str] = []
    all_metadatas: list[dict] = []
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
        all_ids.extend(batch_ids)
        all_metadatas.extend(batch_meta)
        if len(batch_ids) < 300:
            break
        offset += 300

    if not all_ids:
        click.echo(f"Collection '{collection}' is empty — nothing to enrich.")
        return

    # Group chunk ids by source_title; skip already-enriched chunks.
    title_to_ids: dict[str, list[str]] = {}
    already_enriched = 0
    for chunk_id, meta in zip(all_ids, all_metadatas):
        if meta.get("bib_semantic_scholar_id", ""):
            already_enriched += 1
            continue
        title = meta.get("source_title", "") or ""
        if not title:
            continue
        title_to_ids.setdefault(title, []).append(chunk_id)

    click.echo(
        f"Collection '{collection}': {len(all_ids)} total chunks, "
        f"{already_enriched} already enriched, "
        f"{len(title_to_ids)} unique titles to look up."
    )

    enriched_titles = 0
    enriched_chunks = 0
    skipped_titles = 0

    for i, (title, chunk_ids) in enumerate(title_to_ids.items()):
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

    click.echo(
        f"Done: enriched {enriched_chunks} chunks across {enriched_titles} titles; "
        f"{skipped_titles} titles had no Semantic Scholar match."
    )
