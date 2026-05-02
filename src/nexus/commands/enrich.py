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

import json
import time
from collections.abc import Callable
from pathlib import Path

import click
import structlog

_log = structlog.get_logger(__name__)


def _build_catalog_doc_id_lookup() -> Callable[[str, str], str] | None:
    """Build a ``doc_id_lookup(collection, source_id) -> doc_id``
    callable backed by the catalog's ``physical_collection`` +
    ``file_path`` (or ``title`` for slug-shaped knowledge entries)
    SQL projection.

    Returns ``None`` when the catalog is uninitialized; callers
    treat that as "no lookup; fall back to the legacy probe".

    nexus-o6aa.10.1: this is the catalog projection that the chroma
    reader expects. It converts the URI's source identifier (legacy
    source_path or slug-title) into the catalog's ``doc_id`` so chunk
    lookups stay consistent across the Phase 4 prune.
    """
    try:
        from nexus.catalog import Catalog, open_cached
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return None
        # nexus-6xqk follow-up: process-cached Catalog so the closure
        # this function returns reuses one instance across every
        # call (per-entry during enrich-aspects on a large collection).
        cat = open_cached(cat_path)
    except Exception:
        return None

    def _lookup(collection: str, source_id: str) -> str:
        # Phase 1 contract: catalog tumbler doubles as doc_id when
        # metadata.doc_id is unpopulated (event-sourcing path not yet
        # run on this collection). Falls back to tumbler so the chroma
        # reader still receives a usable identity; chunks indexed via
        # the indexer's _doc_id_resolver carry str(tumbler) as their
        # doc_id metadata, which matches.
        try:
            row = cat._db.execute(
                "SELECT json_extract(metadata, '$.doc_id'), tumbler "
                "FROM documents "
                "WHERE physical_collection = ? "
                "  AND (file_path = ? OR title = ?) "
                "LIMIT 1",
                (collection, source_id, source_id),
            ).fetchone()
        except Exception:
            return ""
        if not row:
            return ""
        # row[0] = metadata.doc_id (may be NULL/empty); row[1] = tumbler.
        return row[0] or row[1] or ""

    return _lookup


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
    help="Delay in seconds between API calls (per backend).",
)
@click.option(
    "--limit",
    default=0,
    type=int,
    help="Maximum number of titles to enrich (0 = unlimited).",
)
@click.option(
    "--source",
    type=click.Choice(["auto", "s2", "openalex"], case_sensitive=False),
    default="auto",
    show_default=True,
    help=(
        "Bibliographic backend. ``s2`` queries Semantic Scholar "
        "(needs S2_API_KEY env for higher rate limits; corporate or "
        "academic email required for key issuance). ``openalex`` "
        "queries OpenAlex (no key; set OPENALEX_MAILTO for the polite "
        "pool). ``auto`` picks ``s2`` when S2_API_KEY is set, else "
        "``openalex``."
    ),
)
def enrich_bib(
    collection: str, delay: float, limit: int, source: str,
) -> None:
    """Backfill bibliographic metadata for chunks in COLLECTION.

    Queries the selected backend for each unique source title in the
    collection and writes bib_year, bib_venue, bib_authors,
    bib_citation_count, and a backend-specific ID
    (bib_semantic_scholar_id or bib_openalex_id) back to every chunk
    with that title. nexus-57mk added the OpenAlex backend so users
    without a Semantic Scholar API key can still enrich.

    Already-enriched chunks (the backend's ID field is non-empty) are
    skipped — the command is idempotent per backend.
    """
    from nexus.db import make_t3
    from nexus.retry import _chroma_with_retry

    backend, bib_enrich, id_field = _resolve_bib_backend(source)
    click.echo(f"Backend: {backend} (id field: {id_field})")

    db = make_t3()
    col = db.get_or_create_collection(collection)

    # nexus-sbzr: also collect source_paths per title group so we can
    # try DOI / arXiv-ID extraction from chunk text before falling
    # through to fuzzy title search at the backend.
    title_to_ids: dict[str, list[str]] = {}
    title_to_source_paths: dict[str, set[str]] = {}
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
            if meta.get(id_field, ""):
                already_enriched += 1
                continue
            title = meta.get("title", "") or ""
            if not title:
                continue
            title_to_ids.setdefault(title, []).append(chunk_id)
            sp = meta.get("source_path", "") or ""
            if sp:
                title_to_source_paths.setdefault(title, set()).add(sp)
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
    by_id_count = 0  # how many titles resolved via DOI / arXiv (nexus-sbzr)

    for i, (title, chunk_ids) in enumerate(titles_to_process):
        if i > 0:
            time.sleep(delay)

        bib = _resolve_bib_for_title(
            title=title,
            chunk_ids=chunk_ids,
            source_paths=sorted(title_to_source_paths.get(title, set())),
            col=col,
            backend=backend,
            bib_enrich=bib_enrich,
        )
        if bib.get("_resolved_via") in ("doi", "arxiv"):
            by_id_count += 1
            bib = {k: v for k, v in bib.items() if k != "_resolved_via"}
        elif "_resolved_via" in bib:
            bib = {k: v for k, v in bib.items() if k != "_resolved_via"}
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
        # nexus-tv22: collect distinct source_paths in this title
        # group so the catalog hook can match catalog rows by
        # file_path (unambiguous) instead of by title (which drifts
        # after derive_title rewrites in --from-t3 recovery).
        source_paths: set[str] = set()
        for batch_start in range(0, len(chunk_ids), _BATCH):
            batch_ids = chunk_ids[batch_start:batch_start + _BATCH]
            fetch = _chroma_with_retry(col.get, ids=batch_ids, include=["metadatas"])
            for cid, meta in zip(fetch.get("ids", []), fetch.get("metadatas", [])):
                merged = dict(meta)
                merged["bib_year"] = bib.get("year", 0)
                merged["bib_venue"] = bib.get("venue", "")
                merged["bib_authors"] = bib.get("authors", "")
                merged["bib_citation_count"] = bib.get("citation_count", 0)
                # nexus-57mk: write the backend's native ID so re-runs
                # against the same backend dedupe correctly. The
                # citation-link generator matches against either field.
                if backend == "openalex":
                    merged["bib_openalex_id"] = bib.get("openalex_id", "")
                    if bib.get("doi"):
                        merged["bib_doi"] = bib.get("doi", "")
                else:
                    merged["bib_semantic_scholar_id"] = bib.get(
                        "semantic_scholar_id", "",
                    )
                updated_ids.append(cid)
                updated_meta.append(merged)
                sp = meta.get("source_path", "") if meta else ""
                if sp:
                    source_paths.add(sp)

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
            _catalog_enrich_hook(
                title=title, bib_meta=bib,
                collection_name=collection, backend=backend,
                source_paths=sorted(source_paths),
            )

    backend_label = "Semantic Scholar" if backend == "s2" else "OpenAlex"
    via_id_note = (
        f" ({by_id_count} via DOI/arXiv ID)" if by_id_count else ""
    )
    click.echo(
        f"Done: enriched {enriched_chunks} chunks across "
        f"{enriched_titles} titles{via_id_note}; "
        f"{skipped_titles} titles had no {backend_label} match."
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


def _resolve_bib_for_title(
    *,
    title: str,
    chunk_ids: list,
    source_paths: list[str],
    col,
    backend: str,
    bib_enrich,
) -> dict:
    """nexus-sbzr: DOI/arXiv-aware lookup with title fallback.

    Tries identifiers in order:
      1. DOI extracted from first chunk's body text -> openalex by-doi
      2. arXiv ID from filename or first chunk text -> openalex by-arxiv
      3. Title search at the backend (current behavior)

    Returns the bib dict with a synthetic ``_resolved_via`` key
    indicating which path produced the result (caller strips before
    storage). Empty dict on miss across all three paths.

    DOI/arXiv lookups are OpenAlex-only today; the S2 backend skips
    straight to title search. If S2 ever adds direct-DOI lookup we
    can extend the dispatcher.
    """
    if backend != "openalex":
        bib = bib_enrich(title)
        if bib:
            bib["_resolved_via"] = "title"
        return bib or {}

    from nexus.bib_enricher_openalex import enrich_by_arxiv_id, enrich_by_doi
    from nexus.bib_extractor import extract_identifiers
    from nexus.retry import _chroma_with_retry

    # Scan ALL chunks for DOI/arXiv ID. Docling's reading-order
    # extraction can put the page-1 DOI text into chunks 0-4 OR much
    # deeper, depending on column layout, abstract length, and figure
    # placement (live evidence on knowledge__delos: 9/15 papers have
    # an extractable DOI somewhere in their full text but only 2/15
    # have it in the first 5 chunks). Concatenating all chunks of one
    # title group is cheap — ChromaDB reads are free, the regex is
    # bounded, and the OpenAlex direct lookup is unambiguous.
    body_text = ""
    filename = ""
    if chunk_ids:
        try:
            collected_docs: list[str] = []
            for batch_start in range(0, len(chunk_ids), 300):
                batch = _chroma_with_retry(
                    col.get,
                    ids=chunk_ids[batch_start:batch_start + 300],
                    include=["documents", "metadatas"],
                )
                docs = batch.get("documents") or []
                metas = batch.get("metadatas") or []
                for d in docs:
                    if d:
                        collected_docs.append(d)
                if not filename:
                    for m in metas:
                        if m and m.get("source_path"):
                            filename = m["source_path"]
                            break
            body_text = "\n".join(collected_docs)
        except Exception as exc:
            _log.debug("enrich_chunk_scan_failed", title=title, error=str(exc))

    # Filename fallback when chunk metadata didn't carry source_path.
    if not filename and source_paths:
        filename = source_paths[0]

    ids = extract_identifiers(filename=filename, body_text=body_text)

    if ids["doi"]:
        # nexus-yy1m: pass our title so the OpenAlex backend can reject
        # a citation-DOI poisoning (a DOI extracted from references that
        # resolves to a foreign paper). On rejection, fall through.
        bib = enrich_by_doi(ids["doi"], expected_title=title)
        if bib:
            bib["_resolved_via"] = "doi"
            return bib
    if ids["arxiv"]:
        bib = enrich_by_arxiv_id(ids["arxiv"], expected_title=title)
        if bib:
            bib["_resolved_via"] = "arxiv"
            return bib

    bib = bib_enrich(title)
    if bib:
        bib["_resolved_via"] = "title"
    return bib or {}


def _resolve_bib_backend(source: str) -> tuple[str, callable, str]:
    """nexus-57mk: pick the bib enricher backend.

    Returns ``(backend_name, enrich_callable, chunk_id_field_name)``.
    ``auto`` defaults to ``s2`` when ``S2_API_KEY`` is set, else
    ``openalex``.
    """
    import os as _os

    chosen = source.lower()
    if chosen == "auto":
        chosen = "s2" if _os.environ.get("S2_API_KEY") else "openalex"

    if chosen == "openalex":
        from nexus.bib_enricher_openalex import enrich as _openalex_enrich
        return "openalex", _openalex_enrich, "bib_openalex_id"
    if chosen == "s2":
        from nexus.bib_enricher import enrich as _s2_enrich
        return "s2", _s2_enrich, "bib_semantic_scholar_id"
    raise click.UsageError(
        f"unknown bib source {source!r}; expected one of auto/s2/openalex"
    )


def _catalog_enrich_hook(
    title: str,
    bib_meta: dict,
    collection_name: str = "",
    backend: str = "s2",
    source_paths: list[str] | None = None,
) -> None:
    """Update catalog entry with bib metadata. Silently skipped if absent.

    nexus-57mk: ``backend`` selects which ID field is written into
    catalog meta. The OpenAlex backend writes ``bib_openalex_id`` (and
    ``bib_doi`` when present) so the citation-link generator can match
    references in OpenAlex's W-id space.

    nexus-tv22: ``source_paths`` (the absolute paths of the chunks
    being enriched) is the authoritative way to find the catalog row.
    Title matching can drift after migrations that rewrite chunk
    titles (e.g. derive_title) while leaving catalog row titles as
    placeholders. Match by ``file_path`` (the catalog row's
    relative-or-absolute path) against each ``source_path`` and apply
    the update to every matching row.

    The pre-fix LIMIT-1-on-collection fallback caused all 75 ART
    enrich calls to clobber the same first row in the collection,
    instead of finding the correct one per paper. That fallback is
    removed: when no source_paths are supplied AND the title doesn't
    match, the hook is a silent no-op rather than randomly picking a
    row to corrupt.
    """
    try:
        from nexus.catalog import Catalog
        from nexus.catalog.tumbler import Tumbler
        from nexus.config import catalog_path

        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            return

        cat = Catalog(cat_path, cat_path / ".catalog.db")

        target_tumblers: list[Tumbler] = []

        # nexus-tv22: prefer source_path match (unique per document).
        # Catalog rows store either absolute or relative file_path
        # (relative is canonical post-RDR-060; absolute lingers from
        # legacy ingests). Try both forms so the lookup works either
        # way.
        if source_paths:
            for sp in source_paths:
                if not sp:
                    continue
                rows = cat._db.execute(
                    "SELECT tumbler FROM documents "
                    "WHERE (physical_collection = ? OR ? = '') "
                    "AND (file_path = ? OR file_path = ? "
                    "OR ? LIKE '%/' || file_path)",
                    (
                        collection_name, collection_name,
                        sp, sp.lstrip("/"), sp,
                    ),
                ).fetchall()
                for (t_str,) in rows:
                    target_tumblers.append(Tumbler.parse(t_str))

        # Title match as fallback when source_paths are not provided
        # (preserves backward compatibility with callers that haven't
        # been updated yet — e.g. third-party scripts).
        if not target_tumblers and collection_name:
            row = cat._db.execute(
                "SELECT tumbler FROM documents "
                "WHERE physical_collection = ? AND title = ? LIMIT 1",
                (collection_name, title),
            ).fetchone()
            if row:
                target_tumblers.append(Tumbler.parse(row[0]))

        # Last-resort FTS title search across the whole catalog
        # (legacy path; only fires when the caller passed neither
        # source_paths nor a title that matches inside the collection).
        if not target_tumblers:
            entries = cat.find(title, content_type="paper")
            if entries:
                target_tumblers.append(entries[0].tumbler)

        if not target_tumblers:
            _log.debug(
                "catalog_enrich_hook_no_match",
                title=title,
                collection=collection_name,
                source_paths=source_paths or [],
            )
            return

        # Build the update payload once, apply to every matched row.
        meta_update: dict = {
            "venue": bib_meta.get("venue", ""),
            "citation_count": bib_meta.get("citation_count", 0),
        }
        if backend == "openalex":
            meta_update["bib_openalex_id"] = bib_meta.get("openalex_id", "")
            if bib_meta.get("doi"):
                meta_update["bib_doi"] = bib_meta.get("doi", "")
        else:
            meta_update["bib_semantic_scholar_id"] = bib_meta.get(
                "semantic_scholar_id", "",
            )
        refs = bib_meta.get("references", [])
        if refs:
            meta_update["references"] = refs

        # Dedupe in case multiple source_paths matched the same row.
        seen: set[str] = set()
        for tumbler in target_tumblers:
            key = str(tumbler)
            if key in seen:
                continue
            seen.add(key)
            cat.update(
                tumbler,
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

    Two extractor configs ship: ``knowledge__*`` routes to the
    Claude-CLI scholarly-paper-v1 extractor; ``rdr__*`` routes to
    the deterministic markdown + frontmatter parser
    (rdr-frontmatter-v1; zero API cost). Other collection prefixes
    error out at the config-selection step.
    """
    from nexus.aspect_extractor import select_config

    config = select_config(collection)
    if config is None:
        click.echo(
            f"No extractor config registered for collection "
            f"'{collection}'. Supported prefixes: knowledge__*, "
            f"rdr__*. Aborting."
        )
        if collection.startswith("docs__"):
            click.echo(
                "Note: docs__* collections are not paper-shaped (nexus-z70w "
                "reverted the #377 routing). Index academic PDFs into "
                "knowledge__<name> via 'nx index pdf --collection "
                "knowledge__<name>' for aspect extraction."
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

    # nexus-ow9f: deterministic parsers (parser_fn set, no LLM
    # subprocess) cost nothing at runtime. Reporting Haiku rates for
    # rdr-frontmatter-v1 misled operators into thinking they were
    # about to spend $2.45 to extract aspects from RDR markdown.
    is_deterministic = config.parser_fn is not None
    if is_deterministic:
        cost_str = "Estimated cost: $0 (deterministic parser, no API calls)"
    else:
        cost_estimate = len(entries) * _PER_PAPER_COST_USD
        cost_str = f"Estimated cost: ~${cost_estimate:.2f} at Haiku rates"
    click.echo(
        f"{len(entries)} document(s) in '{collection}' "
        f"(extractor={config.extractor_name}, "
        f"version={config.model_version}). {cost_str}."
    )

    if dry_run:
        click.echo("--dry-run: skipping extraction.")
        _dry_run_predict_skips(
            entries, collection, is_deterministic=is_deterministic,
        )
        return

    extracted = _run_extraction(entries, collection, config)
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


def _dry_run_predict_skips(
    entries: list, collection: str, *, is_deterministic: bool = False,
) -> None:
    """RDR-096 P1.3: predict ExtractFail entries via the read-side
    check, without invoking the Claude subprocess. One ``read_source``
    call per entry — chunk reassembly is the same path
    ``extract_aspects`` would take. T3 unavailable / any other failure
    is caught and the prediction step is skipped gracefully so dry-run
    still works in environments without chroma access.

    nexus-ow9f: skip lines surface ``entry.source_uri`` when distinct
    from the file_path-derived URI, so cross-project catalog
    contamination (e.g., ART-lhk1's
    ``file:///Users/.../nexus/`` URIs registered under
    ``rdr__ART-...``) is visible at a glance instead of hiding behind
    a bare ``empty`` reason. A per-host summary at the bottom counts
    distinct source_uri scheme+host pairs, surfacing contamination
    even when individual lines are truncated to the first 20.
    """
    from urllib.parse import quote

    try:
        from nexus.aspect_readers import ReadFail, read_source
        from nexus.mcp_infra import get_t3
        t3 = get_t3()
    except Exception as exc:
        click.echo(f"  (read-side prediction skipped: {exc})")
        return

    # nexus-o6aa.10.1: chroma reader resolves source_id → doc_id via
    # this projection so the dry-run reflects post-prune behavior. ``None``
    # falls back to the legacy probe (catalog absent / pre-Phase-3).
    doc_id_lookup = _build_catalog_doc_id_lookup()

    planned: dict[str, int] = {}
    skip_lines: list[str] = []
    by_host: dict[str, int] = {}
    for entry in entries:
        sp = _chroma_source_id_for_entry(entry)
        if not sp:
            continue
        uri = f"chroma://{collection}/{quote(sp, safe='/')}"
        try:
            result = read_source(uri, t3=t3, doc_id_lookup=doc_id_lookup)
        except Exception as exc:
            # Per-entry transient failure shouldn't abort the whole
            # prediction loop. Bucket under a synthetic ``read_error``
            # reason so the operator sees the count without losing
            # visibility into the rest of the catalog.
            planned["read_error"] = planned.get("read_error", 0) + 1
            skip_lines.append(
                f"    - {Path(sp).name}: read_error ({type(exc).__name__})"
            )
            continue
        if isinstance(result, ReadFail):
            planned[result.reason] = planned.get(result.reason, 0) + 1
            line = f"    - {Path(sp).name}: {result.reason}"
            entry_uri = getattr(entry, "source_uri", "") or ""
            if entry_uri:
                line += f"  [source_uri={entry_uri}]"
                host_key = _source_uri_host_key(entry_uri)
                by_host[host_key] = by_host.get(host_key, 0) + 1
            skip_lines.append(line)

    skipped = sum(planned.values())
    if skipped == 0:
        click.echo("  All entries readable; full extraction would proceed.")
        return
    click.echo(
        f"  Planned skips: {skipped} of {len(entries)} document(s) "
        f"would skip on read failure:"
    )
    # Cap output so a 500-entry collection doesn't flood the terminal.
    for line in skip_lines[:20]:
        click.echo(line)
    if len(skip_lines) > 20:
        click.echo(f"    ... and {len(skip_lines) - 20} more")
    reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(planned.items()))
    click.echo(f"  by_reason: {reasons_str}")
    if len(by_host) > 1:
        # Multiple source_uri "homes" under a single physical_collection
        # is the contamination signature. Surface it explicitly.
        host_str = ", ".join(
            f"{k}={v}" for k, v in sorted(by_host.items(), key=lambda kv: -kv[1])
        )
        click.echo(
            f"  by_source_uri_host: {host_str} "
            f"(multiple roots in one collection: likely cross-project "
            f"catalog contamination)"
        )
    if is_deterministic:
        click.echo(
            f"  Predicted actual extraction (excluding skips): "
            f"{len(entries) - skipped} document(s) at no API cost."
        )
    else:
        actual_cost = (len(entries) - skipped) * _PER_PAPER_COST_USD
        click.echo(
            f"  Predicted actual cost (excluding skips): ~${actual_cost:.2f}"
        )


def _chroma_source_id_for_entry(entry: object) -> str:
    """nexus-v9az: return the identity value the chroma reader should
    match against ``source_path`` metadata in T3 chunks.

    Rationale: after nexus-p03z's ``--from-t3`` recovery, catalog rows
    for ``docs__<repo>`` and ``code__<repo>`` collections carry
    relative ``file_path`` values (anchored to ``repo_root`` by the
    nexus-3e4s register-time guard). T3 chunks for those same files
    were ingested with absolute ``source_path`` metadata, so a lookup
    keyed on the relative path returns zero chunks ("empty" skip).

    The catalog row's ``source_uri`` is the absolute ``file://`` URI
    set at register time — exactly the form we need for the chroma
    lookup. Use it when present; fall back to the legacy
    ``file_path`` (or ``title`` for slug-shaped knowledge entries)
    when the URI is missing or non-file (curator owners, legacy rows
    pre-source_uri).
    """
    from urllib.parse import unquote, urlparse

    uri = getattr(entry, "source_uri", "") or ""
    if uri:
        p = urlparse(uri)
        if p.scheme == "file" and p.path:
            return unquote(p.path)
    return getattr(entry, "file_path", "") or getattr(entry, "title", "")


def _source_uri_host_key(uri: str) -> str:
    """Stable grouping key for source_uri "home" detection.

    For ``file://`` URIs, returns the first three path segments
    (e.g. ``/Users/hal.hildebrand/git/ART``) so two entries from the
    same repo cluster into one bucket regardless of the file inside
    that repo. For other schemes, returns ``<scheme>://<netloc>``
    so a mixed catalog still gets meaningful grouping.
    """
    from urllib.parse import urlparse

    p = urlparse(uri)
    if p.scheme == "file":
        # /Users/hal.hildebrand/git/ART/docs/rdr/X.md
        # -> ['', 'Users', 'hal.hildebrand', 'git', 'ART', 'docs', ...]
        # Take through the 5th component (the project root).
        parts = p.path.split("/")
        return "/".join(parts[:5]) if len(parts) >= 5 else p.path
    return f"{p.scheme}://{p.netloc}"


def _run_extraction(
    entries: list,
    collection: str,
    config,
) -> list[tuple[str, object]]:
    """Drive extract_aspects per entry, upsert document_aspects, return
    the list of (source_path, AspectRecord) tuples for the successful
    extractions (used as input for --validate-sample).

    ``config`` is the pre-validated :class:`ExtractorConfig` from the
    caller — the parent ``enrich_aspects`` already aborted when
    ``select_config`` returned None, so this function need not
    re-derive.

    RDR-096 P1.3 upsert-guard: ``extract_aspects`` may return
    ``ExtractFail`` on read failure. Such entries are logged
    (``aspect_extract_skip``) and skipped — no row is written. This
    is the structural guarantee that closes issue #331's null-field
    symptom even before Phase 2's schema migration ships.
    """
    from nexus.aspect_extractor import ExtractFail, extract_aspects
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    extractor_name = config.extractor_name

    extracted: list[tuple[str, object]] = []
    success = 0
    null_fields = 0
    skipped = 0
    skipped_unreadable = 0
    by_reason: dict[str, int] = {}

    # nexus-o6aa.10.1: a single catalog projection serves every entry
    # in this collection. Built once outside the loop because the SQL
    # connection inside the helper is cheap-but-not-free per-call.
    doc_id_lookup = _build_catalog_doc_id_lookup()

    db_path = default_db_path()
    with T2Database(db_path) as db:
        for i, entry in enumerate(entries, 1):
            source_path = entry.file_path or entry.title
            if not source_path:
                skipped += 1
                click.echo(f"  [{i}/{len(entries)}] (no source_path) — skipped")
                continue

            # nexus-v9az: chunks were ingested with absolute source_path
            # metadata. After --from-t3 recovery (nexus-p03z), catalog
            # rows may carry relative file_path, anchored to repo_root
            # by the nexus-3e4s register-time guard. Pass the absolute
            # path as ``lookup_path`` so the chroma reader's identity
            # match succeeds. ``source_path`` is preserved as the
            # storage key for AspectRecord.
            record = extract_aspects(
                content="",
                source_path=source_path,
                collection=collection,
                lookup_path=_chroma_source_id_for_entry(entry),
                doc_id_lookup=doc_id_lookup,
            )
            if record is None:
                # Defensive — select_config already passed at the parent
                # level, so this branch should not fire under Phase 1.
                skipped += 1
                click.echo(f"  [{i}/{len(entries)}] {Path(source_path).name}: no extractor — skipped")
                continue

            if isinstance(record, ExtractFail):
                # Typed read failure — skip without upsert. The log line
                # is the operator surface for triaging the underlying
                # source-identity drift; #331's symptom is closed by this
                # branch alone.
                _log.warning(
                    "aspect_extract_skip",
                    uri=record.uri,
                    reason=record.reason,
                    detail=record.detail,
                    collection=collection,
                    extractor_name=extractor_name,
                )
                skipped_unreadable += 1
                by_reason[record.reason] = by_reason.get(record.reason, 0) + 1
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                    f"skipped (reason={record.reason})"
                )
                continue

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

    summary = (
        f"Done: {success} extracted, {null_fields} null-fields, "
        f"{skipped_unreadable} skipped (read-failure), "
        f"{skipped} skipped (other)"
    )
    if by_reason:
        reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
        summary += f". by_reason: {reasons_str}"
    click.echo(summary)
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
    # Deterministic seed so a re-run of validation produces the same
    # sample. Operators investigating a failure can reproduce by
    # rerunning. The seed is a stable hash of the extraction set so
    # different collections sample differently.
    rng = random.Random(len(extracted))
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
@click.option(
    "--scheme",
    default="",
    help=(
        "Filter to rows whose source_uri scheme matches (RDR-096 "
        "P3.2). Common values: 'file', 'chroma', 'https', "
        "'nx-scratch'. Use '' (default) for no filter; rows with "
        "empty / NULL source_uri are excluded when --scheme is set."
    ),
)
def enrich_aspects_list(collection: str, limit: int, scheme: str) -> None:
    """List source paths with extracted aspects in COLLECTION.

    One row per source document, deterministic order
    (``source_path ASC``). For each row prints
    ``<scheme>:<source_path>  <fields_populated>/5  <model_version>``
    so an operator can spot null-fields rows AND identify which URI
    scheme each row will dispatch to. The ``--scheme`` flag pre-
    filters to a single scheme (e.g., ``--scheme=chroma`` lists only
    chunk-reassembly-backed rows).
    """
    from urllib.parse import urlparse  # noqa: PLC0415

    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    with T2Database(default_db_path()) as db:
        records = db.document_aspects.list_by_collection(
            collection, limit=limit if limit > 0 else None,
        )

    if scheme:
        records = [
            r for r in records
            if r.source_uri and urlparse(r.source_uri).scheme == scheme
        ]

    if not records:
        suffix = f" with scheme={scheme!r}" if scheme else ""
        click.echo(f"No aspect rows for '{collection}'{suffix}.")
        return

    for r in records:
        populated = sum(
            1 for v in (
                r.problem_formulation, r.proposed_method,
                r.experimental_results,
            ) if v
        ) + (1 if r.experimental_datasets else 0) \
          + (1 if r.experimental_baselines else 0)
        # Per-row scheme label so operators can see at a glance
        # which reader will dispatch. ``-`` denotes a row with empty
        # / NULL source_uri (legacy entry not yet backfilled).
        row_scheme = (
            urlparse(r.source_uri).scheme if r.source_uri else "-"
        ) or "-"
        click.echo(
            f"  [{row_scheme:<10}] {r.source_path}  {populated}/5  {r.model_version}"
        )
    suffix = f" matching scheme={scheme!r}" if scheme else ""
    click.echo(f"\n{len(records)} row(s) in '{collection}'{suffix}.")


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

    from urllib.parse import urlparse  # noqa: PLC0415

    scheme = (
        urlparse(record.source_uri).scheme
        if record.source_uri else ""
    )

    click.echo(json.dumps({
        "collection": record.collection,
        "source_path": record.source_path,
        # RDR-096 P3.2: surface URI + parsed scheme so operators
        # can see which reader will dispatch for re-extraction.
        "source_uri": record.source_uri,
        "scheme": scheme,
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


# ── extras → fixed-column promotion (RDR-089 Phase E) ───────────────────────


@enrich.command(name="aspects-promote-field")
@click.argument("field_name")
@click.option(
    "--type", "sql_type",
    type=click.Choice(["TEXT", "INTEGER", "REAL"], case_sensitive=False),
    default="TEXT",
    show_default=True,
    help="SQL type for the new column.",
)
@click.option(
    "--prune",
    is_flag=True,
    help=(
        "After backfilling, remove the key from extras. Only run "
        "after every reader has been updated to consume the typed "
        "column."
    ),
)
@click.option(
    "--history",
    is_flag=True,
    help="Print the promotion audit log and exit (no promotion).",
)
def enrich_aspects_promote_field(
    field_name: str, sql_type: str, prune: bool, history: bool,
) -> None:
    """Promote ``extras['<FIELD_NAME>']`` to its own typed column.

    Three-phase mechanic (see ``src/nexus/aspect_promotion.py`` for
    the full contract):

      1. ALTER TABLE document_aspects ADD COLUMN <field_name> <type>
         (idempotent)
      2. Backfill the new column from ``extras[<field_name>]`` for
         rows where the column is currently NULL and the extras
         key is set
      3. If ``--prune``: remove the key from ``extras`` so future
         readers always go to the typed column

    Phase 3 is opt-in. The default (no --prune) leaves ``extras``
    untouched, supporting a dual-read cutover where readers are
    updated incrementally.

    Each invocation logs to T2 ``aspect_promotion_log``; the
    promotion history is queryable via ``--history``.
    """
    from nexus.aspect_promotion import (
        list_promotions, promote_extras_field,
    )
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    if history:
        with T2Database(default_db_path()) as db:
            entries = list_promotions(db)
        if not entries:
            click.echo("No promotion history.")
            return
        for e in entries:
            note = " (pruned)" if e["pruned"] else ""
            click.echo(
                f"  {e['promoted_at']}  {e['field_name']:32s} "
                f"{e['sql_type']:8s} +{e['rows_backfilled']:>4d} rows"
                f"{note}"
            )
        return

    with T2Database(default_db_path()) as db:
        try:
            result = promote_extras_field(
                db, field_name,
                sql_type=sql_type.upper(),
                prune=prune,
            )
        except ValueError as exc:
            click.echo(f"Error: {exc}", err=True)
            raise click.exceptions.Exit(2)

    if result.column_added:
        click.echo(
            f"Added column {result.field_name} {result.sql_type}."
        )
    else:
        click.echo(
            f"Column {result.field_name} already exists "
            f"(promotion is idempotent)."
        )
    click.echo(f"Backfilled {result.rows_backfilled} row(s).")
    if result.pruned:
        click.echo(f"Pruned {result.rows_pruned} extras key(s).")


# ── nexus-bkvk: aspect read verbs (show / list) ─────────────────────────────


_ASPECT_FIELDS: tuple[str, ...] = (
    "problem_formulation",
    "proposed_method",
    "experimental_datasets",
    "experimental_baselines",
    "experimental_results",
    "extras",
    "confidence",
)


def _resolve_catalog_entry(tumbler_or_title: str):
    """Resolve a tumbler or title to (catalog, entry). Raises
    ClickException on miss."""
    from nexus.catalog import Catalog, resolve_tumbler
    from nexus.config import catalog_path

    cat_path = catalog_path()
    if not Catalog.is_initialized(cat_path):
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' first."
        )
    cat = Catalog(cat_path, cat_path / ".catalog.db")
    t, err = resolve_tumbler(cat, tumbler_or_title)
    if err:
        raise click.ClickException(err)
    entry = cat.resolve(t)
    if entry is None:
        raise click.ClickException(f"Not found: {tumbler_or_title}")
    return cat, entry


def _aspect_to_dict(record) -> dict:
    """Serialize an AspectRecord for --json output."""
    return {
        "collection": record.collection,
        "source_path": record.source_path,
        "problem_formulation": record.problem_formulation,
        "proposed_method": record.proposed_method,
        "experimental_datasets": list(record.experimental_datasets or []),
        "experimental_baselines": list(record.experimental_baselines or []),
        "experimental_results": record.experimental_results,
        "extras": dict(record.extras or {}),
        "confidence": record.confidence,
        "extracted_at": record.extracted_at,
        "model_version": record.model_version,
        "extractor_name": record.extractor_name,
        "source_uri": record.source_uri,
    }


def _print_aspect_record(record, *, field: str = "") -> None:
    """Human-readable formatter for `aspects-show`. ``field`` projects
    a single aspect field (raw value, no labels)."""
    if field:
        if field not in _ASPECT_FIELDS:
            raise click.ClickException(
                f"Unknown field {field!r}. Choose from: {', '.join(_ASPECT_FIELDS)}"
            )
        value = getattr(record, field)
        if isinstance(value, (list, dict)):
            click.echo(json.dumps(value, indent=2))
        elif value is None:
            click.echo("")
        else:
            click.echo(value)
        return

    click.echo(f"Collection:     {record.collection}")
    click.echo(f"Source path:    {record.source_path}")
    click.echo(f"Source URI:     {record.source_uri or '(legacy: not set)'}")
    click.echo(f"Extractor:      {record.extractor_name} ({record.model_version})")
    click.echo(f"Extracted at:   {record.extracted_at}")
    click.echo(f"Confidence:     {record.confidence if record.confidence is not None else '(null)'}")
    click.echo("")
    click.echo("--- Aspects ---")
    click.echo("")
    click.echo(f"problem_formulation:")
    click.echo(f"  {record.problem_formulation or '(empty)'}")
    click.echo("")
    click.echo(f"proposed_method:")
    click.echo(f"  {record.proposed_method or '(empty)'}")
    click.echo("")
    click.echo(f"experimental_datasets:  {record.experimental_datasets or '[]'}")
    click.echo(f"experimental_baselines: {record.experimental_baselines or '[]'}")
    click.echo("")
    click.echo(f"experimental_results:")
    click.echo(f"  {record.experimental_results or '(empty)'}")
    click.echo("")
    if record.extras:
        click.echo("extras:")
        click.echo(json.dumps(record.extras, indent=2))


@enrich.command(name="aspects-show")
@click.argument("tumbler_or_title")
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON instead of human-readable form.",
)
@click.option(
    "--field", default="",
    help=(
        "Project a single aspect field (problem_formulation, "
        "proposed_method, experimental_datasets, "
        "experimental_baselines, experimental_results, extras, "
        "confidence). Output is the raw value."
    ),
)
def aspects_show_cmd(tumbler_or_title: str, as_json: bool, field: str) -> None:
    """Display the aspect record for a single document.

    nexus-bkvk: pre-this-verb the only way to inspect aspects was raw
    SQL against ~/.config/nexus/memory.db. Resolves the tumbler (or
    title) via the catalog, looks up the aspect row by
    ``(physical_collection, file_path)``, and renders all fields.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    _, entry = _resolve_catalog_entry(tumbler_or_title)
    if not entry.physical_collection or not entry.file_path:
        click.echo(
            f"No aspect record for tumbler {entry.tumbler}: catalog "
            "row has no physical_collection or file_path. Run "
            "'nx enrich aspects <COLLECTION>' to extract."
        )
        return

    with T2Database(default_db_path()) as db:
        record = db.document_aspects.get(
            entry.physical_collection, entry.file_path,
        )

    if record is None:
        click.echo(
            f"No aspect record extracted yet for tumbler "
            f"{entry.tumbler} ({entry.title!r}). Run "
            f"'nx enrich aspects {entry.physical_collection}' to extract."
        )
        return

    if as_json:
        click.echo(json.dumps(_aspect_to_dict(record), indent=2))
        return
    _print_aspect_record(record, field=field)


@enrich.command(name="aspects-list")
@click.option(
    "--collection", required=True,
    help="T3 collection to inspect (e.g. knowledge__papers).",
)
@click.option(
    "--limit", default=20, show_default=True,
    help="Maximum rows to display (0 = unlimited).",
)
@click.option(
    "--missing", is_flag=True,
    help=(
        "Flip output: list catalog rows in COLLECTION that have NO "
        "aspect record. Used to find gaps after partial enrichment."
    ),
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON array instead of human-readable form.",
)
def aspects_list_cmd(
    collection: str, limit: int, missing: bool, as_json: bool,
) -> None:
    """List aspect records for COLLECTION, or the gaps with --missing.

    nexus-bkvk: companion to aspects-show; gives the same data at
    collection-level (preview/audit shape) instead of single-record
    detail. With ``--missing`` the verb inverts to gap detection:
    catalog rows in COLLECTION that don't have a matching aspect row.
    """
    from nexus.catalog import Catalog
    from nexus.commands._helpers import default_db_path
    from nexus.config import catalog_path
    from nexus.db.t2 import T2Database

    if missing:
        cat_path = catalog_path()
        if not Catalog.is_initialized(cat_path):
            raise click.ClickException(
                "Catalog not initialized. Run 'nx catalog setup' first."
            )
        cat = Catalog(cat_path, cat_path / ".catalog.db")
        entries = cat.list_by_collection(collection)
        with T2Database(default_db_path()) as db:
            existing = {
                r.source_path for r in db.document_aspects.list_by_collection(
                    collection,
                )
            }
        gaps = [e for e in entries if e.file_path and e.file_path not in existing]
        if as_json:
            click.echo(json.dumps([
                {
                    "tumbler": str(e.tumbler),
                    "title": e.title,
                    "file_path": e.file_path,
                }
                for e in gaps
            ], indent=2))
            return
        if not gaps:
            click.echo(
                f"No missing aspects in '{collection}': every catalog "
                f"row has an extracted aspect record."
            )
            return
        click.echo(
            f"{len(gaps)} catalog row(s) in '{collection}' have no "
            f"aspect record:"
        )
        for e in gaps[:limit] if limit else gaps:
            click.echo(f"  {e.tumbler}  {e.file_path}")
        if limit and len(gaps) > limit:
            click.echo(f"  ... and {len(gaps) - limit} more.")
        return

    with T2Database(default_db_path()) as db:
        records = db.document_aspects.list_by_collection(
            collection, limit=(limit if limit else None),
        )

    if as_json:
        click.echo(json.dumps(
            [_aspect_to_dict(r) for r in records], indent=2,
        ))
        return

    if not records:
        click.echo(
            f"No aspect records in '{collection}'. Run "
            f"'nx enrich aspects {collection}' to extract."
        )
        return

    click.echo(f"{len(records)} aspect record(s) in '{collection}':")
    click.echo("")
    for r in records:
        problem = (r.problem_formulation or "")[:80]
        method = (r.proposed_method or "")[:80]
        click.echo(f"  {r.source_path}")
        click.echo(f"    problem: {problem}{'...' if r.problem_formulation and len(r.problem_formulation) > 80 else ''}")
        click.echo(f"    method:  {method}{'...' if r.proposed_method and len(r.proposed_method) > 80 else ''}")
        click.echo("")
