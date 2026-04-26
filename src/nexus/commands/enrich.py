# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command: ``nx enrich`` — backfill metadata over an existing collection.

Subcommands:

  bib       — Semantic Scholar bibliographic metadata (existing).
  aspects   — Structured aspect extraction (RDR-089 P2.2).

The group structure replaces the previous ``nx enrich <collection>``
single command. Migration: ``nx enrich <coll>`` → ``nx enrich bib
<coll>``. The aspects subcommand is new in this restructure.
"""
from __future__ import annotations

import time
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


@click.group(name="enrich")
def enrich() -> None:
    """Enrich a collection with bibliographic or aspect metadata.

    Subcommands:

    \b
      bib       — backfill bibliographic metadata via Semantic Scholar
      aspects   — extract structured aspects via the synchronous
                  Claude CLI extractor (RDR-089 P2.2)
    """


# ── nx enrich bib (existing functionality, moved to subcommand) ─────────────


@enrich.command(name="bib")
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
def enrich_bib(collection: str, delay: float, limit: int) -> None:
    """Backfill bibliographic metadata for chunks in COLLECTION.

    Queries Semantic Scholar for each unique source title found in the
    collection and writes bib_year, bib_venue, bib_authors,
    bib_citation_count, and bib_semantic_scholar_id back to every
    chunk with that title.

    Already-enriched chunks (bib_semantic_scholar_id is non-empty) are
    skipped — the command is idempotent.
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
        # metadata dicts, so we fetch and merge. Batch at 200 to stay under
        # ChromaDB Cloud's 300-record get/write limit.
        _BATCH = 200
        updated_ids: list[str] = []
        updated_meta: list[dict] = []
        for batch_start in range(0, len(chunk_ids), _BATCH):
            batch_ids = chunk_ids[batch_start:batch_start + _BATCH]
            fetch = _chroma_with_retry(col.get, ids=batch_ids, include=["metadatas"])
            for cid, meta in zip(fetch.get("ids", []), fetch.get("metadatas", [])):
                merged = dict(meta)
                merged["bib_year"] = bib.get("year", 0)
                merged["bib_venue"] = bib.get("venue", "")
                merged["bib_authors"] = bib.get("authors", "")
                merged["bib_citation_count"] = bib.get("citation_count", 0)
                merged["bib_semantic_scholar_id"] = bib.get("semantic_scholar_id", "")
                updated_ids.append(cid)
                updated_meta.append(merged)

        if updated_ids:
            for batch_start in range(0, len(updated_ids), _BATCH):
                batch_end = min(batch_start + _BATCH, len(updated_ids))
                _chroma_with_retry(
                    col.update,
                    ids=updated_ids[batch_start:batch_end],
                    metadatas=updated_meta[batch_start:batch_end],
                )
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

    # Auto-generate citation links if catalog is initialized
    if enriched_titles > 0:
        try:
            from nexus.catalog import Catalog
            from nexus.config import catalog_path

            cat_path = catalog_path()
            if Catalog.is_initialized(cat_path):
                from nexus.catalog.link_generator import generate_citation_links

                cat = Catalog(cat_path, cat_path / ".catalog.db")
                link_count = generate_citation_links(cat)
                if link_count > 0:
                    click.echo(f"Auto-generated {link_count} citation links in catalog.")
        except Exception:
            _log.debug("auto_citation_links_failed", exc_info=True)


def _catalog_enrich_hook(title: str, bib_meta: dict, collection_name: str = "") -> None:
    """Update catalog entry with bib metadata. Silently skipped if absent."""
    try:
        from nexus.catalog import Catalog
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        # Look up by collection + title jointly for precision
        entry = None
        if collection_name:
            from nexus.catalog.tumbler import Tumbler
            row = cat._db.execute(
                "SELECT tumbler FROM documents WHERE physical_collection = ? AND title = ? LIMIT 1",
                (collection_name, title),
            ).fetchone()
            if row:
                entry = cat.resolve(Tumbler.parse(row[0]))
            if entry is None:
                # Fallback: collection-only (for renamed/enriched titles)
                row = cat._db.execute(
                    "SELECT tumbler FROM documents WHERE physical_collection = ? LIMIT 1",
                    (collection_name,),
                ).fetchone()
                if row:
                    entry = cat.resolve(Tumbler.parse(row[0]))

        # Fallback to FTS title search (no collection context)
        if entry is None:
            entries = cat.find(title, content_type="paper")
            entry = entries[0] if entries else None

        if entry:
            meta_update: dict = {
                "venue": bib_meta.get("venue", ""),
                "bib_semantic_scholar_id": bib_meta.get("semantic_scholar_id", ""),
                "citation_count": bib_meta.get("citation_count", 0),
            }
            refs = bib_meta.get("references", [])
            if refs:
                meta_update["references"] = refs
            cat.update(
                entry.tumbler,
                author=bib_meta.get("authors", ""),
                year=bib_meta.get("year", 0),
                meta=meta_update,
            )
    except Exception:
        _log.debug("catalog_enrich_hook_failed", exc_info=True)


# ── nx enrich aspects (RDR-089 P2.2) ────────────────────────────────────────


# Per-paper Haiku cost estimate (RDR §Trade-offs). Conservative ceiling
# for ~5K-token output on Haiku-4-class models. Used by --dry-run.
_PER_PAPER_COST_USD = 0.01

# Default per the RDR's original Phase 2 spec. The P1.3 spike's
# 16.7% strict-equality "stability" rate measures whether the model
# emits the same token sequence on a re-run, which is a methodology
# question (the model paraphrases between runs and should), NOT a
# hallucination-detection question. operator_verify is the
# hallucination guard. Once token-overlap or embedding-similarity
# stability metrics exist, this default should be revisited from
# real signal.
_DEFAULT_VALIDATE_SAMPLE_PCT = 5


@enrich.command(name="aspects")
@click.argument("collection")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report document count + cost estimate. No API calls. No T2 writes.",
)
@click.option(
    "--validate-sample",
    type=int,
    default=_DEFAULT_VALIDATE_SAMPLE_PCT,
    show_default=True,
    help=(
        "Validate N%% of newly-extracted aspects via operator_verify "
        "(claim=aspects, evidence=document text). Disagreements append "
        "to ./validation_failures.jsonl. Pass 0 to skip validation."
    ),
)
@click.option(
    "--re-extract",
    is_flag=True,
    help="Re-run only on rows whose model_version < --extractor-version.",
)
@click.option(
    "--extractor-version",
    default="",
    help="Threshold for --re-extract (lexicographic STRICT-less-than).",
)
def enrich_aspects(
    collection: str,
    dry_run: bool,
    validate_sample: int,
    re_extract: bool,
    extractor_version: str,
) -> None:
    """Batch-extract structured aspects for documents in COLLECTION.

    Iterates the catalog (one entry per source document, NOT per
    chunk), calls extract_aspects directly (bypassing the
    fire_post_document_hooks chain to avoid double-firing on
    documents already triggered at ingest), and upserts AspectRecords
    to ``document_aspects``.

    Phase 1 supports ``knowledge__*`` collections only. Other
    collections error out at the config-selection step.
    """
    from nexus.aspect_extractor import select_config

    config = select_config(collection)
    if config is None:
        click.echo(
            f"No extractor config registered for collection "
            f"'{collection}'. Phase 1 (RDR-089) supports knowledge__* "
            f"only. Aborting."
        )
        return

    if re_extract and not extractor_version:
        click.echo(
            "--re-extract requires --extractor-version (the threshold "
            "below which rows are re-run). Aborting."
        )
        return

    entries = _select_entries(
        collection=collection,
        re_extract=re_extract,
        extractor_version=extractor_version,
        config_extractor_name=config.extractor_name,
    )
    if entries is None:  # catalog missing
        return

    if not entries:
        click.echo(f"No documents to process in '{collection}'.")
        return

    cost_estimate = len(entries) * _PER_PAPER_COST_USD
    click.echo(
        f"{len(entries)} document(s) in '{collection}' "
        f"(extractor={config.extractor_name}, "
        f"version={config.model_version}). "
        f"Estimated cost ~${cost_estimate:.2f} at Haiku rates."
    )

    if dry_run:
        click.echo("--dry-run: skipping extraction.")
        return

    extracted = _run_extraction(entries, collection)
    if not extracted:
        click.echo("No aspects extracted.")
        return

    if validate_sample > 0:
        _run_validation_sample(extracted, sample_pct=validate_sample)


def _select_entries(
    *,
    collection: str,
    re_extract: bool,
    extractor_version: str,
    config_extractor_name: str,
) -> list | None:
    """Return the catalog entries to process, or None if the catalog
    is missing (terminal error already echoed)."""
    from nexus.catalog import Catalog
    from nexus.commands._helpers import default_db_path
    from nexus.config import catalog_path
    from nexus.db.t2 import T2Database

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        click.echo("Catalog not initialized — run 'nx catalog setup' first.")
        return None
    cat = Catalog(cat_path, cat_path / ".catalog.db")
    entries = cat.list_by_collection(collection)

    if re_extract:
        # Filter to entries whose existing aspect row has model_version
        # below the threshold. Rows without an existing aspect entry
        # are also included (they need first-time extraction).
        with T2Database(default_db_path()) as db:
            outdated_paths = {
                r.source_path
                for r in db.document_aspects.list_by_extractor_version(
                    config_extractor_name, extractor_version,
                )
            }
            # Find entries missing from document_aspects so they get
            # included too (re-extract is "ensure all entries are at
            # >= version"; a missing row is by definition at < version).
            existing_paths = set()
            for r in db.document_aspects.list_by_collection(collection):
                existing_paths.add(r.source_path)

        filtered = []
        for e in entries:
            sp = e.file_path or e.title
            if sp in outdated_paths or sp not in existing_paths:
                filtered.append(e)
        entries = filtered

    return entries


def _run_extraction(entries: list, collection: str) -> list[tuple[str, object]]:
    """Drive extract_aspects per entry, upsert document_aspects, return
    the list of (source_path, AspectRecord) tuples for the successful
    extractions (used as input for --validate-sample).
    """
    from nexus.aspect_extractor import extract_aspects
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    extracted: list[tuple[str, object]] = []
    success = 0
    null_fields = 0
    skipped = 0

    db_path = default_db_path()
    for i, entry in enumerate(entries, 1):
        source_path = entry.file_path or entry.title
        if not source_path:
            skipped += 1
            click.echo(f"  [{i}/{len(entries)}] (no source_path) — skipped")
            continue

        record = extract_aspects(
            content="",
            source_path=source_path,
            collection=collection,
        )
        if record is None:
            # Defensive — select_config already passed at the parent
            # level, so this branch should not fire under Phase 1.
            skipped += 1
            click.echo(f"  [{i}/{len(entries)}] {Path(source_path).name}: no extractor — skipped")
            continue

        with T2Database(db_path) as db:
            db.document_aspects.upsert(record)

        if record.problem_formulation is None:
            null_fields += 1
            click.echo(
                f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                f"null-fields (extractor failed 3x)"
            )
        else:
            success += 1
            extracted.append((source_path, record))
            click.echo(
                f"  [{i}/{len(entries)}] {Path(source_path).name}: extracted"
            )

    click.echo(
        f"Done: {success} extracted, {null_fields} null-fields, "
        f"{skipped} skipped."
    )
    return extracted


def _run_validation_sample(
    extracted: list[tuple[str, object]],
    *,
    sample_pct: int,
) -> None:
    """Sample N% of extracted records, run operator_verify against the
    raw document text, and write disagreements to
    ``./validation_failures.jsonl``.
    """
    import asyncio
    import json
    import random
    from datetime import UTC, datetime

    sample_count = max(1, len(extracted) * sample_pct // 100)
    sample_count = min(sample_count, len(extracted))
    rng = random.Random()
    sample = rng.sample(extracted, sample_count)
    click.echo(
        f"Validating {sample_count} of {len(extracted)} extractions "
        f"({sample_pct}%) via operator_verify..."
    )

    failures_path = Path("validation_failures.jsonl")
    failures = 0
    verified = 0
    errored = 0

    for source_path, record in sample:
        try:
            content = (
                Path(source_path)
                .read_text(encoding="utf-8", errors="replace")
                .replace("\x00", "")
            )
        except OSError as exc:
            errored += 1
            _log.warning(
                "validate_sample_read_failed",
                source_path=source_path, error=str(exc),
            )
            continue

        claim_payload = {
            "problem_formulation": record.problem_formulation,
            "proposed_method": record.proposed_method,
            "experimental_datasets": record.experimental_datasets,
            "experimental_baselines": record.experimental_baselines,
            "experimental_results": record.experimental_results,
        }
        claim_json = json.dumps(claim_payload)

        try:
            result = asyncio.run(_verify(claim_json, content[:50000]))
        except Exception as exc:
            errored += 1
            _log.warning(
                "validate_sample_verify_failed",
                source_path=source_path, error=str(exc),
            )
            continue

        if result.get("verified", False):
            verified += 1
            continue

        failures += 1
        with failures_path.open("a") as f:
            f.write(json.dumps({
                "source_path": source_path,
                "extracted_aspects": claim_payload,
                "operator_verify_reason": result.get("reason", ""),
                "citations": result.get("citations", []),
                "timestamp": datetime.now(UTC).isoformat(),
            }) + "\n")

    if failures:
        click.echo(
            f"Validation: {verified} verified, {failures} disagreement(s) "
            f"written to {failures_path}, {errored} errored."
        )
    else:
        click.echo(
            f"Validation: all {verified} sample(s) verified "
            f"({errored} errored)."
        )


async def _verify(claim_json: str, evidence: str) -> dict:
    """Async wrapper around operator_verify so the CLI can call it
    from synchronous click code via ``asyncio.run``.

    Caveat: ``asyncio.run`` raises ``RuntimeError`` if invoked
    inside a running event loop (e.g. if ``nx`` were ever wrapped
    as an MCP tool body, or invoked from pytest-asyncio with
    ``asyncio_mode='auto'``). The current production path
    (``nx enrich aspects`` from a plain shell) is purely synchronous,
    so this caveat is forward-risk only. If the CLI ever gets
    invoked from inside an event loop, restructure this helper
    to run the coroutine in a dedicated thread.
    """
    from nexus.mcp.core import operator_verify
    return await operator_verify(
        claim=claim_json,
        evidence=evidence,
        timeout=60.0,
    )


# ── Day 2 Operations: list / info / delete ──────────────────────────────────


@enrich.command(name="list")
@click.argument("collection")
@click.option(
    "--limit",
    type=int,
    default=0,
    help="Maximum rows to print (0 = unlimited).",
)
def enrich_aspects_list(collection: str, limit: int) -> None:
    """List source paths with extracted aspects in COLLECTION.

    One row per source document, deterministic order
    (``source_path ASC``). For each row prints
    ``<source_path>  <fields_populated>/5  <model_version>``
    so an operator can spot null-fields rows quickly.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    with T2Database(default_db_path()) as db:
        records = db.document_aspects.list_by_collection(
            collection, limit=limit if limit > 0 else None,
        )

    if not records:
        click.echo(f"No aspect rows for '{collection}'.")
        return

    for r in records:
        populated = sum(
            1 for v in (
                r.problem_formulation, r.proposed_method,
                r.experimental_results,
            ) if v
        ) + (1 if r.experimental_datasets else 0) \
          + (1 if r.experimental_baselines else 0)
        click.echo(
            f"  {r.source_path}  {populated}/5  {r.model_version}"
        )
    click.echo(f"\n{len(records)} row(s) in '{collection}'.")


@enrich.command(name="info")
@click.argument("collection")
@click.argument("source_path")
def enrich_aspects_info(collection: str, source_path: str) -> None:
    """Show the AspectRecord JSON for one document in COLLECTION."""
    import json

    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    with T2Database(default_db_path()) as db:
        record = db.document_aspects.get(collection, source_path)

    if record is None:
        click.echo(
            f"No aspect row for ({collection!r}, {source_path!r})."
        )
        return

    click.echo(json.dumps({
        "collection": record.collection,
        "source_path": record.source_path,
        "problem_formulation": record.problem_formulation,
        "proposed_method": record.proposed_method,
        "experimental_datasets": record.experimental_datasets,
        "experimental_baselines": record.experimental_baselines,
        "experimental_results": record.experimental_results,
        "extras": record.extras,
        "confidence": record.confidence,
        "extracted_at": record.extracted_at,
        "model_version": record.model_version,
        "extractor_name": record.extractor_name,
    }, indent=2))


@enrich.command(name="delete")
@click.argument("collection")
@click.argument("source_path")
@click.option(
    "--yes", "-y",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
def enrich_aspects_delete(
    collection: str, source_path: str, yes: bool,
) -> None:
    """Remove one aspect row by (COLLECTION, SOURCE_PATH).

    Idempotent: deleting a non-existent row prints a notice and
    exits 0. Re-extraction (``nx enrich aspects --re-extract``)
    will repopulate the row when run.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    if not yes:
        click.confirm(
            f"Delete aspect row for ({collection!r}, "
            f"{source_path!r})?",
            abort=True,
        )

    with T2Database(default_db_path()) as db:
        deleted = db.document_aspects.delete(collection, source_path)

    if deleted:
        click.echo(
            f"Deleted aspect row for ({collection!r}, "
            f"{source_path!r})."
        )
    else:
        click.echo(
            f"No aspect row for ({collection!r}, "
            f"{source_path!r}) — nothing to delete."
        )
