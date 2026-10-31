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
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import structlog

from nexus.catalog.membership import unknown_collection_message

_log = structlog.get_logger(__name__)

#: How many of the other exact-title matches ``_enrich_apply``'s ambiguity warning names
#: (the rest are counted in ``others_total``).
_TITLE_AMBIGUOUS_LOG_LIMIT = 5

#: A chunk's natural id: the full sha256 of its text (RDR-180).
_CHASH_RE = re.compile(r"[0-9a-f]{64}")


def _build_catalog_manifest_lookup() -> Callable[[str], list[Any]] | None:
    """nexus-8g79.2: build a ``manifest_lookup(doc_id) -> list[ManifestRow]``
    callable backed by ``Catalog.get_manifest``.

    The aspect/enrich chroma readers use this to reassemble chunks in
    canonical position order via the catalog ``document_chunks`` table
    rather than the dropped ``chunk_index`` chunk-metadata field.
    Returns ``None`` when the catalog is uninitialized; the reader
    falls back to ``chunk_index`` (post-Phase-3 this collapses to
    insertion order, the pre-fix behaviour).
    """
    try:

        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import
        cat = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
        if cat is None:
            return None
    except Exception:  # noqa: BLE001 — best-effort catalog init; any failure degrades to None (no lookup, legacy fallback)
        return None

    def _lookup(doc_id: str) -> list[Any]:
        try:
            return cat.get_manifest(doc_id)
        except Exception as exc:  # noqa: BLE001 — best-effort manifest lookup; a transport failure is surfaced, anything else degrades to an empty list
            from nexus.aspect_readers import (  # noqa: PLC0415 — deferred command-local import
                TransportReadFailure,
                is_transport_failure,
            )

            if is_transport_failure(exc):
                # The manifest is down, not empty. An empty list would read as a
                # verdict on the document and the aspect worker would drop it
                # (nexus-5r2sc.3.10); the reader maps this to ReadFail(transport).
                raise TransportReadFailure(
                    f"manifest lookup for {doc_id!r}: {type(exc).__name__}: {exc}"
                ) from exc
            return []

    return _lookup


def _catalog_documents_for_chunks(
    collection: str, chunk_ids: Iterable[str],
) -> tuple[dict[str, list[str]], dict[str, Any]]:
    """nexus-6pfgb.21: who holds these chunks, by the catalog.

    Returns ``(owners, entries)``: ``owners`` maps a chunk id (the chunk's chash)
    to the tumblers of the LIVE catalog documents whose manifest names it in
    *collection*, and ``entries`` maps each of those tumblers to its catalog
    entry. A chunk nobody holds is absent from ``owners``.

    A T3 chunk row is shared by every document that holds that text, and the
    keys on it that describe a document (``title``, ``content_hash``, ``bib_*``)
    are the last writer's. The manifest is the document's own binding to its
    chunks, so enrichment selects through it and never through a chunk row's
    title. One batched reverse lookup (``chash_positions`` where the engine has
    it, else the referencing documents' manifests, filtered to *collection* in
    both cases) and one ``resolve_many``; no per-chunk call.

    Raises ``click.ClickException`` when the catalog cannot answer: the
    pre-6pfgb.21 fallback (group by chunk title) is the defect, so there is no
    degraded mode to fall back to.
    """
    # A manifest names chunks by chash (64 hex); an id of any other shape is not a
    # chunk a document can hold, and the engine answers a malformed chash with 400.
    wanted = sorted({c for c in chunk_ids if c and _CHASH_RE.fullmatch(c)})
    if not wanted:
        return {}, {}
    from nexus.catalog.orphan_backfill import live_owners_in_collection  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    reader = _open_catalog_reader()
    try:
        owners = live_owners_in_collection(reader, wanted, collection)
        tumblers = sorted({t for ts in owners.values() for t in ts})
        entries = reader.resolve_many(tumblers) if tumblers else {}
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as a ClickException
        _log.warning("enrich_catalog_owners_unavailable", collection=collection, exc_info=True)
        raise click.ClickException(
            f"cannot read which documents hold the chunks of '{collection}': {exc}; "
            "nothing was changed."
        ) from exc
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    return owners, entries


def _open_catalog_reader() -> Any:
    """The catalog reader, or a ``ClickException`` when there is none: the enrich
    verbs attribute chunks to documents through the catalog and have no degraded
    mode (the chunk-title grouping they had is the defect nexus-6pfgb.21 removed)."""
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

    try:
        reader = make_catalog_reader()
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as a ClickException
        raise click.ClickException(f"the catalog is not reachable: {exc}") from exc
    if reader is None:
        raise click.ClickException(
            "the catalog is not initialised, so no chunk can be attributed to a "
            "document; nothing was changed."
        )
    return reader


@dataclass(frozen=True)
class _CollectionDocuments:
    """What :func:`_catalog_collection_documents` found: the documents that still
    need a lookup (tumbler -> ``(catalog entry, its chunk ids in the collection)``)
    and the counts of the rest."""

    to_look_up: dict[str, tuple[Any, list[str]]]
    total: int
    enriched: int
    untitled: int
    # Titled documents not yet enriched whose manifest names no chunk this
    # collection serves: nothing to read a lookup from, so none is made.
    no_chunks: int = 0


def _catalog_collection_documents(
    collection: str, id_field: str, served: set[str],
) -> _CollectionDocuments:
    """nexus-6pfgb.21: the collection's live catalog documents, split by whether
    the catalog row already holds the backend's id (*id_field*).

    Only a document that does NOT hold it is read further (its manifest, for its
    chunks in *collection* that T3 serves, *served*), so a fully enriched
    collection costs one catalog listing (the caller has already scanned T3 for
    *served*) and no chunk-to-owner reverse lookup. A document with no catalog
    title cannot be looked up and is counted in ``untitled``; a titled one with no
    served chunk in ``no_chunks``. ``--limit`` does not bound this read.
    Raises ``click.ClickException`` when the catalog cannot answer, as
    :func:`_catalog_documents_for_chunks` does.
    """
    reader = _open_catalog_reader()
    try:
        entries = reader.list_by_collection(collection, limit=1_000_000)
        pending = [e for e in entries if not getattr(e, id_field, "")]
        titled = [e for e in pending if e.title]
        manifests = reader.get_manifests([str(e.tumbler) for e in titled]) if titled else {}
    except Exception as exc:  # noqa: BLE001 — surfaced to the operator as a ClickException
        _log.warning("enrich_catalog_documents_unavailable", collection=collection, exc_info=True)
        raise click.ClickException(
            f"cannot read the documents of '{collection}' from the catalog: {exc}; "
            "nothing was changed."
        ) from exc
    finally:
        close = getattr(reader, "close", None)
        if close is not None:
            close()
    to_look_up: dict[str, tuple[Any, list[str]]] = {}
    for entry in titled:
        tumbler = str(entry.tumbler)
        chunk_ids = list(dict.fromkeys(
            m.chash for m in manifests.get(tumbler, ())
            if m.collection in (collection, None) and m.chash in served
        ))
        if chunk_ids:
            to_look_up[tumbler] = (entry, chunk_ids)
    return _CollectionDocuments(
        to_look_up=to_look_up, total=len(entries),
        enriched=len(entries) - len(pending), untitled=len(pending) - len(titled),
        no_chunks=len(titled) - len(to_look_up),
    )


def _entry_content_hash(entry: Any) -> str:
    """The whole-file hash a catalog document's chunks carry in their ``content_hash``:
    ``index_content_hash``, else the pre-fence ``meta.content_hash`` (the rule of
    ``link_generator._pdf_content_hash``, which orphan_backfill matches chunks on)."""
    return (
        getattr(entry, "index_content_hash", "")
        or (getattr(entry, "meta", None) or {}).get("content_hash")
        or ""
    )


def _row_names_document(
    row_doc_id: str, row_content_hash: str, tumbler: str, entry: Any,
) -> bool:
    """True when a T3 chunk row names the catalog document *tumbler* as the one that
    last INDEXED it. This is a weaker statement than "wrote its ``bib_*``".

    A chunk row is shared by every document that holds the text. Its identity keys
    (``content_hash``, ``title``) are restamped by the last indexer, while its
    ``bib_*`` keys survive a later index write (``metadata_schema.REWRITE_OWNED_KEYS``
    leaves them out and the engine merge never retracts a key). So a row that names
    this document may carry a ``bib_*`` another document wrote earlier and this
    document's re-index kept; that case cannot be told apart from the document's own
    bib, and the verbs that use this test (``--backfill-catalog``, the ``chunk_only``
    skip of ``nx enrich bib``) can copy such a bib onto this document's catalog row.

    A note's row carries ``catalog_doc_id`` (``catalog.note_write``). A chunk the
    indexers write (PDF, markdown, code) does NOT: its identity is the file's
    ``content_hash``, which is the document's ``index_content_hash`` (orphan_backfill
    matches chunks to documents on the same pair). A row naming another document, or
    carrying neither key, names this one for nobody.
    """
    if row_doc_id:
        return row_doc_id == tumbler
    key = _entry_content_hash(entry)
    return bool(key) and row_content_hash == key


def _chunk_bib(meta: dict | None) -> dict | None:
    """The bib a T3 chunk row carries (``year``, ``venue``, ``authors``,
    ``citation_count`` and the backend's id), or ``None`` for a row that carries
    none. Which document the bib describes is decided by the caller."""
    if not meta:
        return None
    s2_id = meta.get("bib_semantic_scholar_id", "") or ""
    oa_id = meta.get("bib_openalex_id", "") or ""
    if not (meta.get("bib_year") or s2_id or oa_id):
        return None
    bib: dict = {
        "year": meta.get("bib_year", 0),
        "venue": meta.get("bib_venue", ""),
        "authors": meta.get("bib_authors", ""),
        "citation_count": meta.get("bib_citation_count", 0),
    }
    if oa_id:
        bib["openalex_id"] = oa_id
        if meta.get("bib_doi"):
            bib["doi"] = meta.get("bib_doi", "")
    else:
        bib["semantic_scholar_id"] = s2_id
    return bib


def _chunk_idless_bib(meta: dict | None) -> dict | None:
    """The bib a chunk row carries when it has no backend id: ``year``, ``authors``, ``venue`` and
    ``citation_count``, or ``None`` when it carries none of them.

    A PDF indexed with ``--enrich`` on the batch path before nexus-6pfgb.28 stamped those four on its rows
    and no id (the lookup result was written to rows only, and the id was not). ``--backfill-catalog``
    reads them as the same evidence as an id-bearing row, minus the id."""
    if not meta:
        return None
    bib = {
        "year": meta.get("bib_year") or 0,
        "authors": meta.get("bib_authors") or "",
        "venue": meta.get("bib_venue") or "",
        "citation_count": meta.get("bib_citation_count") or 0,
    }
    return bib if any(bib.values()) else None


def _idless_fill(entry: Any, bib: dict) -> dict:
    """The catalog ``bib_*`` columns an id-less bib may fill: only the ones the document's row leaves
    empty, never one a curated or looked-up value holds."""
    held = {
        "bib_year": getattr(entry, "bib_year", 0),
        "bib_authors": getattr(entry, "bib_authors", ""),
        "bib_venue": getattr(entry, "bib_venue", ""),
        "bib_citation_count": getattr(entry, "bib_citation_count", 0),
    }
    wanted = {
        "bib_year": bib["year"], "bib_authors": bib["authors"],
        "bib_venue": bib["venue"], "bib_citation_count": bib["citation_count"],
    }
    return {k: v for k, v in wanted.items() if v and not held[k]}


def _fill_idless_bib(tumbler: str, fields: dict) -> bool:
    """Write the filled columns and ``bib_enriched_at`` to *tumbler*; no id, no author or year. Best effort:
    ``False`` on any failure."""
    try:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — command-local import

        writer = make_catalog_writer()
        try:
            writer.update(tumbler, bib_enriched_at=datetime.now(UTC).isoformat(), **fields)
        finally:
            writer.close()
        return True
    except Exception:  # noqa: BLE001 — best-effort, counted by the caller
        _log.debug("enrich_backfill_idless_write_failed", tumbler=tumbler, exc_info=True)
        return False


def _bib_key(bib: dict) -> tuple[bool, str]:
    """``(is OpenAlex, backend id)``: two chunk bibs of one document agree when
    their keys are equal. ``--backfill-catalog`` and ``nx enrich bib`` apply the
    same test, so a document one skips as conflicting the other looks up."""
    return (
        bool(bib.get("openalex_id")),
        bib.get("openalex_id") or bib.get("semantic_scholar_id") or "",
    )


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

        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import
        cat = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
        if cat is None:
            return None
    except Exception:  # noqa: BLE001 — best-effort catalog init; any failure degrades to None (no lookup, legacy fallback)
        return None

    def _lookup(collection: str, source_id: str) -> str:
        # Phase 1 contract: catalog tumbler doubles as doc_id when
        # metadata.doc_id is unpopulated (event-sourcing path not yet
        # run on this collection). Falls back to tumbler so the chroma
        # reader still receives a usable identity; chunks indexed via
        # the indexer's _doc_id_resolver carry str(tumbler) as their
        # doc_id metadata, which matches.
        # nexus-xnz0o: replaced raw SQL with catalog API uniform across SQLite
        # and service mode. Tries file_path match first, then title search.
        try:
            # Try file_path match via resolve (fast point-query on the service).
            tumbler = cat.lookup_doc_id_by_collection_and_path(collection, source_id)
            if tumbler:
                # doc_id and tumbler are identical in the catalog contract.
                return tumbler
            # _chroma_source_id_for_entry passes the absolute path from a
            # file:// source_uri, but file_path is stored repo-relative, so
            # the point-query misses a registered document. Match the row
            # by its source_uri, in this collection only.
            if source_id.startswith("/"):
                from urllib.parse import quote  # noqa: PLC0415 — branch-local stdlib import
                for uri in dict.fromkeys(
                    (f"file://{source_id}", f"file://{quote(source_id)}"),
                ):
                    hit = cat.by_source_uri(uri)
                    if hit is not None and hit.physical_collection == collection:
                        return str(hit.tumbler)
            # Try title search as fallback.
            hits = cat.find_all(source_id)
            for hit in hits:
                if hit.physical_collection == collection:
                    return str(hit.tumbler)
        except Exception as exc:  # noqa: BLE001 — best-effort doc_id resolve; a transport failure is surfaced, anything else degrades to empty string
            from nexus.aspect_readers import (  # noqa: PLC0415 — deferred command-local import
                TransportReadFailure,
                is_transport_failure,
            )

            if is_transport_failure(exc):
                # The catalog is down, not silent on this document: the reader
                # maps this to ReadFail(transport) and the caller retries.
                raise TransportReadFailure(str(exc)) from exc
            return ""
        return ""

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
    help=(
        "Maximum number of documents to look up (0 = unlimited). Bounds the "
        "backend calls only: the catalog manifest read and the chunk-owner "
        "lookup cover every document still to look up."
    ),
)
@click.option(
    "--source",
    type=click.Choice(["auto", "s2", "openalex", "dt"], case_sensitive=False),
    default="auto",
    show_default=True,
    help=(
        "Bibliographic backend. ``s2`` queries Semantic Scholar "
        "(needs S2_API_KEY env for higher rate limits; corporate or "
        "academic email required for key issuance). ``openalex`` "
        "queries OpenAlex (no key; set OPENALEX_MAILTO for the polite "
        "pool). ``auto`` picks ``s2`` when S2_API_KEY is set, else "
        "``openalex``. ``dt`` (RDR-139 Layer C) runs the ``auto`` "
        "primary backend and then gap-fills any still-empty ``bib_*`` "
        "field from DEVONthink's CrossRef resolver — strictly "
        "lowest-precedence, never overwriting an S2/OpenAlex value. "
        "When DEVONthink is unavailable it degrades to ``auto`` exactly."
    ),
)
@click.option(
    "--backfill-catalog",
    is_flag=True,
    help=(
        "Fill the catalog bib_* columns from already-enriched T3 chunk "
        "metadata; no external API calls. A recovery tool: when in doubt "
        "run `nx enrich bib` (an API lookup) instead. Fill-only per backend "
        "id: a document whose catalog row already holds that backend's id "
        "is left alone, while a row holding only the other backend's id is "
        "filled and its author, year and venue are overwritten. A chunk "
        "counts only when exactly one live document holds it AND its row "
        "names that document as its last indexer (catalog_doc_id, else the "
        "file content_hash). A row names its last INDEXER, not whoever "
        "wrote its bib_*: a bib a former co-holder wrote and a later "
        "re-index kept cannot be told apart and may be copied onto the "
        "indexing document's empty row. A document whose chunks disagree "
        "on the id is skipped and reported. A document with no id on any chunk "
        "(batch-path `nx index pdf --enrich` before nexus-6pfgb.28) is filled "
        "from its chunks' year/authors/venue/citation count, only into columns "
        "it leaves empty, with bib_enriched_at and no id."
    ),
)
def enrich_bib(
    collection: str, delay: float, limit: int, source: str,
    backfill_catalog: bool,
) -> None:
    """Backfill bibliographic metadata for the documents in COLLECTION.

    The unit is the catalog document, not the chunk's ``title``: a chunk row
    is shared by every document that holds the text, and its title and
    ``content_hash`` are whichever document indexed it last (its ``bib_*``
    keys are whichever document enriched it, kept across a later re-index).
    Each catalog document of the collection whose catalog row has no id for
    the selected backend (bib_semantic_scholar_id or bib_openalex_id) is
    looked up once, under its catalog title, and the result is written to its
    catalog row by tumbler: bib_year, bib_venue, bib_authors,
    bib_citation_count and the backend's id. OpenAlex and ``--source dt`` also
    try the DOI or arXiv id in the document's text or file name; Semantic
    Scholar looks up by title only. The same values go onto a chunk row only
    where that document is the chunk's sole live holder; a chunk two documents
    hold keeps the values it had. ``--limit`` bounds the backend calls only:
    the manifest read and the chunk-owner lookup cover every document still to
    look up.
    nexus-57mk added the OpenAlex backend so users without a Semantic
    Scholar API key can still enrich.

    A document whose catalog row holds the backend's id is skipped, so the
    command is idempotent per backend. A document enriched before the catalog
    carried bib_* columns (nexus-9l2lg) has the id only on its chunk rows:
    when a chunk it alone holds names it as last indexer and carries the id,
    and its attributed chunks agree on the id, it is skipped too, and
    ``--backfill-catalog`` fills its catalog row. Run that first on an old
    collection. A document whose attributed chunks disagree is looked up.
    The verb stops with no change when the catalog cannot answer.

    ``--backfill-catalog`` (nexus-9l2lg) skips the network path entirely: it
    fills the catalog ``bib_*`` columns from chunk metadata that a prior live
    enrichment already wrote, and makes zero external calls. It is a recovery
    tool; when in doubt use the API lookup above. Fill-only per backend id (a
    row that already holds that id is left as it is; a row holding only the
    other backend's id is filled, and its author, year and venue are
    overwritten). A chunk is used only when exactly one live document holds it
    and the chunk row names that document as its last indexer
    (``catalog_doc_id``, else the file ``content_hash``). The row names its
    last INDEXER, not whoever wrote its ``bib_*``: a bib that a former
    co-holder wrote and a later re-index kept cannot be told apart, and may be
    copied onto the indexing document's empty catalog row. A document whose
    chunks disagree on the id is skipped and reported.
    """
    # nexus-g276c: accept the bare subject every other CLI verb accepts.
    # Passed through unresolved, the engine's /v1/vectors/get refused
    # ``knowledge__vector-search`` with a 400 ("not four-segment conformant").
    from nexus.corpus import t3_collection_name  # noqa: PLC0415 — deferred command-local import
    from nexus.db import make_t3  # noqa: PLC0415 — deferred command-local import

    collection = t3_collection_name(collection, t3=make_t3())
    if backfill_catalog:
        _backfill_catalog_from_chunks(collection)
        return
    run_bib_enrichment(collection, delay=delay, limit=limit, source=source)


def _backfill_catalog_from_chunks(collection: str) -> tuple[int, int, int]:
    """Re-drive the catalog bib_* write from already-enriched T3 chunk
    metadata. No external API calls — zero imports of
    ``nexus.bib_enricher*``. Fill-only and idempotent: a document whose catalog
    row already holds the backend's id is left as it is (``documents_backfilled``
    counts documents a catalog row was written for in this run).

    nexus-6pfgb.21: a chunk row is shared by every document that holds the text.
    A chunk row is evidence about a document only when BOTH hold:

    * exactly one live document holds the chunk (the catalog manifest says who,
      :func:`_catalog_documents_for_chunks`), and
    * the row names that document as its last INDEXER (:func:`_row_names_document`:
      ``catalog_doc_id`` for a note, else the file ``content_hash`` the indexers
      stamp, against the document's own).

    KNOWN LIMIT: that names who last indexed the row, not who wrote its ``bib_*``.
    A later index write restamps ``content_hash`` and keeps the ``bib_*`` (the
    engine merge never retracts a key), so a bib a former co-holder wrote and a
    re-index by this document kept is indistinguishable from this document's own
    and can be copied onto its empty catalog row. This verb is a recovery tool;
    ``nx enrich bib`` (an API lookup) is the safe path when in doubt.

    Every such chunk of a document is read; a document whose chunks disagree on
    the backend id is skipped and reported, not guessed. The catalog row written
    is the holder's own, by tumbler. Fill-only per backend id: a row holding the
    other backend's id is still filled, and its author, year and venue replaced.

    Returns ``(documents_backfilled, chunks_scanned, chunks_skipped)``;
    ``chunks_skipped`` counts enriched chunks that were not used (held by none or
    by several documents, or not naming their holder).
    """
    from nexus.db import make_t3  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    db = make_t3()
    col = db.get_or_create_collection(collection)

    # Enriched chunks, in scan order: (chunk id, bib dict, row's catalog_doc_id,
    # row's content_hash). The bib is read from the chunk row; WHICH document it
    # describes is decided afterwards.
    enriched: list[tuple[str, dict, str, str]] = []
    chunks_scanned = 0
    offset = 0
    while True:
        batch = _vector_with_retry(
            col.get, include=["metadatas"], limit=300, offset=offset,
        )
        batch_ids = batch.get("ids", [])
        batch_meta = batch.get("metadatas", [])
        chunks_scanned += len(batch_ids)
        for chunk_id, meta in zip(batch_ids, batch_meta):
            bib = _chunk_bib(meta) or _chunk_idless_bib(meta)
            if bib is None:
                continue
            enriched.append((
                chunk_id, bib,
                meta.get("catalog_doc_id", "") or "", meta.get("content_hash", "") or "",
            ))
        if len(batch_ids) < 300:
            break
        offset += 300

    owners, entries = _catalog_documents_for_chunks(
        collection, (cid for cid, _bib, _d, _h in enriched),
    )

    # tumbler -> [(backend id, bib)] over the chunks that are sole-held AND name
    # their holder.
    doc_bibs: dict[str, list[tuple[tuple[bool, str], dict]]] = {}
    chunks_unheld = 0
    chunks_unattributed = 0
    for chunk_id, bib, row_doc_id, row_hash in enriched:
        held_by = owners.get(chunk_id, [])
        if len(held_by) != 1 or held_by[0] not in entries:
            chunks_unheld += 1
            _log.debug(
                "enrich_backfill_chunk_not_single_owner",
                chunk=chunk_id, owners=len(held_by),
            )
            continue
        tumbler = held_by[0]
        if not _row_names_document(row_doc_id, row_hash, tumbler, entries[tumbler]):
            chunks_unattributed += 1
            _log.debug(
                "enrich_backfill_chunk_names_another_writer",
                chunk=chunk_id, holder=tumbler,
            )
            continue
        doc_bibs.setdefault(tumbler, []).append((_bib_key(bib), bib))

    documents_backfilled = 0
    documents_not_updated = 0
    documents_already_filled: list[str] = []
    documents_conflicting: list[str] = []
    for tumbler, candidates in doc_bibs.items():
        entry = entries[tumbler]
        # nexus-6pfgb.28: a bib with no backend id (batch-path ``--enrich`` before that fix) is weaker evidence
        # than one with an id, so a document with any id-bearing chunk is judged on those alone.
        with_id = [c for c in candidates if c[0][1]]
        if not with_id:
            shapes = {tuple(sorted(b.items())) for _k, b in candidates}
            if len(shapes) > 1:
                documents_conflicting.append(tumbler)
                _log.warning("enrich_backfill_document_chunks_disagree", collection=collection, tumbler=tumbler, ids=[])
                continue
            fields = _idless_fill(entry, candidates[0][1])
            if not fields:
                documents_already_filled.append(tumbler)
            elif _fill_idless_bib(tumbler, fields):
                documents_backfilled += 1
            else:
                documents_not_updated += 1
            continue
        candidates = with_id
        bib = candidates[0][1]
        backend = "openalex" if bib.get("openalex_id") else "s2"
        id_field = "bib_openalex_id" if backend == "openalex" else "bib_semantic_scholar_id"
        if getattr(entry, id_field, ""):
            documents_already_filled.append(tumbler)
            continue
        if len({key for key, _b in candidates}) > 1:
            documents_conflicting.append(tumbler)
            _log.warning(
                "enrich_backfill_document_chunks_disagree", collection=collection,
                tumbler=tumbler, ids=sorted({key[1] for key, _b in candidates}),
            )
            continue
        updated = _catalog_enrich_hook(
            title=entry.title, bib_meta=bib, collection_name=collection,
            backend=backend, tumblers=[tumbler],
        )
        if updated:
            documents_backfilled += 1
        else:
            documents_not_updated += 1

    extra = ""
    if chunks_unattributed:
        extra += (
            f" {chunks_unattributed} enriched chunks skipped (the row does not name "
            "the document that holds it as its last indexer)."
        )
    if documents_already_filled:
        extra += (
            f" {len(documents_already_filled)} documents already hold the id "
            "(left as they were)."
        )
    if documents_conflicting:
        extra += (
            f" {len(documents_conflicting)} documents skipped (their chunks "
            f"disagree on the id: {', '.join(documents_conflicting)})."
        )
    if documents_not_updated:
        extra += f" {documents_not_updated} documents could not be updated."
    click.echo(
        f"Backfilled {documents_backfilled} documents from {chunks_scanned} "
        f"chunks in '{collection}' (no external calls); "
        f"{chunks_unheld} enriched chunks skipped (not held by exactly one "
        f"live document).{extra}"
    )
    return documents_backfilled, chunks_scanned, chunks_unheld + chunks_unattributed


def run_bib_enrichment(
    collection: str, *, delay: float = 0.5, limit: int = 0, source: str = "auto",
) -> None:
    """Core of ``nx enrich bib``; also invoked by ``nx dt index --enrich``
    (RDR-139 Layer C) to run a DT-CrossRef gap-fill pass over a freshly
    indexed collection. ``source="dt"`` enables the gap-fill layer."""
    from nexus.db import make_t3  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    # RDR-139 Layer C: ``--source dt`` is the ``auto`` primary backend plus a
    # lowest-precedence DT-CrossRef gap-fill pass. Resolve the primary first.
    dt_gapfill = source.lower() == "dt"
    backend, bib_enrich, id_field = _resolve_bib_backend(
        "auto" if dt_gapfill else source
    )
    dt_available = False
    if dt_gapfill:
        from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

        dt_available = _dt.available()
        click.echo(
            f"Backend: {backend} (id field: {id_field}) "
            + ("+ DT-CrossRef gap-fill" if dt_available else "(DT unavailable; primary only)")
        )
    else:
        click.echo(f"Backend: {backend} (id field: {id_field})")

    db = make_t3()
    col = db.get_or_create_collection(collection)

    # Every chunk id the collection serves (a plain read serves only chunks a
    # live document holds, RDR-192). The chunk rows' own ``title`` is not read:
    # it is the last writer's, shared by every document that holds the chunk.
    # What is kept per chunk is the bib it carries, with the row's two indexer
    # keys: a document enriched before the catalog carried bib_* columns has the
    # id only here (see ``chunk_only`` below).
    all_chunk_ids: list[str] = []
    # chunk id -> (row's catalog_doc_id, row's content_hash, the bib it carries),
    # for every chunk carrying a bib (the rows ``--backfill-catalog`` reads).
    row_bibs: dict[str, tuple[str, str, dict]] = {}
    total_chunks = 0
    offset = 0
    while True:
        batch = _vector_with_retry(
            col.get,
            include=["metadatas"],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        total_chunks += len(batch_ids)
        all_chunk_ids.extend(batch_ids)
        for cid, meta in zip(batch_ids, batch.get("metadatas") or []):
            chunk_bib = _chunk_bib(meta)
            if chunk_bib is not None:
                row_bibs[cid] = (
                    meta.get("catalog_doc_id", "") or "", meta.get("content_hash", "") or "",
                    chunk_bib,
                )
        if len(batch_ids) < 300:
            break
        offset += 300

    if not total_chunks:
        click.echo(f"Collection '{collection}' is empty — nothing to enrich.")
        return

    # nexus-6pfgb.21 (x54wv census rows 10, 11): the unit is the catalog
    # DOCUMENT. Its manifest names its chunks; whether it is enriched is the
    # catalog row's own id column; the lookup title is the catalog title (the
    # DEVONthink record name for a DT import, not the extractor's first line,
    # which found a stranger in nexus-g276c). Grouping by chunk title, as this
    # did, attributed a chunk restamped by another document's writer to that
    # document: it was enriched with the other's bib, counted as already
    # enriched, or left out.
    #
    # Only a document that does not hold the id yet is read past the listing:
    # the chunk-to-owner reverse lookup runs on ITS chunks alone, so a fully
    # enriched collection costs one listing.
    served = set(all_chunk_ids)
    found = _catalog_collection_documents(collection, id_field, served)
    owners: dict[str, list[str]] = {}
    if found.to_look_up:
        owners, _held_entries = _catalog_documents_for_chunks(
            collection,
            sorted({c for _e, cids in found.to_look_up.values() for c in cids}),
        )

    # nexus-9l2lg left documents with the id on their chunk rows and none in the
    # catalog. Looking those up again costs an API call each, and the lookup may
    # replace good values; a chunk this document alone holds that names it as
    # last indexer and carries the id is the same evidence ``--backfill-catalog``
    # uses, so the document is left to that verb, but only when that verb would
    # fill it: it skips a document whose attributed chunks disagree on the id,
    # so such a document is looked up here instead of being left to neither verb.
    chunk_only: set[str] = set()
    chunks_disagree: set[str] = set()
    for tumbler, (entry, cids) in found.to_look_up.items():
        keys = {
            _bib_key(row_bibs[c][2]) for c in cids
            if len(owners.get(c, [])) == 1 and c in row_bibs
            and _row_names_document(row_bibs[c][0], row_bibs[c][1], tumbler, entry)
        }
        if len(keys) > 1:
            chunks_disagree.add(tumbler)
        elif keys and next(iter(keys))[0] == (backend == "openalex") and next(iter(keys))[1]:
            chunk_only.add(tumbler)
    docs_to_process: list[tuple[str, Any, list[str]]] = [
        (tumbler, entry, cids)
        for tumbler, (entry, cids) in found.to_look_up.items()
        if tumbler not in chunk_only
    ]
    if limit > 0:
        docs_to_process = docs_to_process[:limit]

    notes = ""
    if found.untitled:
        notes += f", {found.untitled} without a catalog title (not looked up)"
    if found.no_chunks:
        notes += (
            f", {found.no_chunks} with no chunk in the collection (not looked up)"
        )
    if chunk_only:
        notes += (
            f", {len(chunk_only)} with the id on their chunks only (run "
            "--backfill-catalog to fill their catalog rows)"
        )
    if chunks_disagree:
        notes += (
            f", {len(chunks_disagree)} whose chunks disagree on the id (looked up "
            "here; --backfill-catalog skips them)"
        )
    click.echo(
        f"Collection '{collection}': {total_chunks} chunks, {found.total} documents, "
        f"{found.enriched} already enriched{notes}, "
        f"{len(docs_to_process)} documents to look up"
        + (f" (capped at {limit})" if limit > 0 else "")
        + "."
    )

    enriched_docs = 0
    enriched_chunks = 0
    skipped_docs = 0
    by_id_count = 0  # how many documents resolved via DOI / arXiv (nexus-sbzr)

    for i, (tumbler, entry, chunk_ids) in enumerate(docs_to_process):
        if i > 0:
            time.sleep(delay)

        title = entry.title
        # The document's own file name (nexus-sbzr: an arXiv id in it is a
        # direct lookup); the chunk rows' ``source_path`` key is gone.
        title_source_paths = [entry.file_path] if entry.file_path else []
        # Extract the record's DOI/arXiv identifiers once, up front, when
        # either the OpenAlex primary path or the DT-CrossRef gap-fill will
        # need them. Sharing the scan keeps the chunk read single-pass.
        ids = None
        if backend == "openalex" or dt_available:
            ids = _extract_identifiers_for_title(
                chunk_ids=chunk_ids, source_paths=title_source_paths, col=col,
            )

        bib = _resolve_bib_for_title(
            title=title,
            chunk_ids=chunk_ids,
            source_paths=title_source_paths,
            col=col,
            backend=backend,
            bib_enrich=bib_enrich,
            ids=ids,
        )
        # RDR-139 Layer C: gap-fill still-empty bib_* fields from DT-CrossRef.
        # DT is strictly lowest-precedence — it fills only fields the primary
        # backend left empty/zero and never overwrites a set value. The actual
        # DT round-trip is DOI-gated below (``if doi``), so a partial-S2 record
        # with no extractable DOI triggers no DT call — only the cheap, already
        # -needed identifier scan runs.
        if dt_available and (
            not bib
            or not all(bib.get(k) for k in ("year", "venue", "authors"))
        ):
            doi = (ids or {}).get("doi") or ""
            if doi:
                dt_bib = _dt_crossref_bib(doi)
                if dt_bib:
                    bib = _merge_bib_gapfill(bib, dt_bib)
        if bib.get("_resolved_via") in ("doi", "arxiv"):
            by_id_count += 1
            bib = {k: v for k, v in bib.items() if k != "_resolved_via"}
        elif "_resolved_via" in bib:
            bib = {k: v for k, v in bib.items() if k != "_resolved_via"}
        if not bib:
            skipped_docs += 1
            _log.debug("enrich_no_result", title=title)
            continue

        # Chunk rows carry the bib only where this document is the sole live
        # holder: a chunk another document also holds would show this
        # document's values to that one (and the other's, last, to this one).
        # The catalog row below is the document's own, whatever its chunks.
        own_ids = [c for c in chunk_ids if len(owners.get(c, ())) == 1]
        # Build per-chunk metadata updates: the stub's update takes full
        # metadata dicts, so we fetch and merge. Batch at 200 to stay under
        # the 300-record get/write limit.
        _BATCH = 200
        updated_ids: list[str] = []
        updated_meta: list[dict] = []
        for batch_start in range(0, len(own_ids), _BATCH):
            batch_ids = own_ids[batch_start:batch_start + _BATCH]
            fetch = _vector_with_retry(col.get, ids=batch_ids, include=["metadatas"])
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

        for batch_start in range(0, len(updated_ids), _BATCH):
            batch_end = min(batch_start + _BATCH, len(updated_ids))
            _vector_with_retry(
                col.update,
                ids=updated_ids[batch_start:batch_end],
                metadatas=updated_meta[batch_start:batch_end],
            )
        enriched_chunks += len(updated_ids)
        catalog_updated = _catalog_enrich_hook(
            title=title, bib_meta=bib,
            collection_name=collection, backend=backend,
            tumblers=[tumbler],
        )
        if updated_ids or catalog_updated:
            enriched_docs += 1
            _log.debug(
                "enrich_updated",
                title=title,
                tumbler=tumbler,
                chunks=len(updated_ids),
                year=bib.get("year"),
                venue=bib.get("venue"),
            )

    backend_label = "Semantic Scholar" if backend == "s2" else "OpenAlex"
    via_id_note = (
        f" ({by_id_count} via DOI/arXiv ID)" if by_id_count else ""
    )
    click.echo(
        f"Done: enriched {enriched_chunks} chunks across "
        f"{enriched_docs} documents{via_id_note}; "
        f"{skipped_docs} documents had no {backend_label} match."
    )

    # Auto-generate citation links if the catalog is reachable
    if enriched_docs > 0:
        try:
            from nexus.catalog.link_generator import generate_citation_links  # noqa: PLC0415 — circular-dep avoidance; command-local import
            from nexus.catalog.factory import (  # noqa: PLC0415 — circular-dep avoidance; command-local import
                make_catalog_reader,
                make_catalog_writer,
            )

            # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out +
            # uninitialised; the old local gate silently skipped citation-
            # link generation on fresh service-mode boxes.
            reader = make_catalog_reader()
            if reader is not None:
                # generate_citation_links reads (all_documents) via the reader
                # and writes (link_if_absent) via the write-only daemon proxy.
                writer = make_catalog_writer()
                try:
                    link_count = generate_citation_links(reader, writer=writer)
                finally:
                    writer.close()
                    if reader is not None:
                        reader.close()
                if link_count > 0:
                    click.echo(f"Auto-generated {link_count} citation links in catalog.")
        except Exception:  # noqa: BLE001 — best-effort auto-link; failure logged at debug, enrich continues without citation links
            _log.debug("auto_citation_links_failed", exc_info=True)


def _extract_identifiers_for_title(
    *, chunk_ids: list, source_paths: list[str], col,
) -> dict:
    """Scan a document's chunk text + filename for a DOI / arXiv ID.

    Returns the ``extract_identifiers`` dict (``{"doi": ..., "arxiv": ...}``).
    Shared by the OpenAlex direct-lookup path and the RDR-139 Layer C
    DT-CrossRef gap-fill so the chunk scan happens at most once per document.

    Scans ALL chunks: a layout extractor's reading order can put the page-1
    DOI text into chunks 0-4 OR much deeper depending on column layout,
    abstract length, and figure placement (live evidence on knowledge__delos:
    9/15 papers have an extractable DOI somewhere in full text but only 2/15
    in the first 5 chunks). Concatenating one document's chunks is cheap — ChromaDB
    reads are free, the regex is bounded, the direct lookup is unambiguous.
    """
    from nexus.bib_extractor import extract_identifiers  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    from nexus.retry import _vector_with_retry  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    body_text = ""
    if chunk_ids:
        try:
            collected_docs: list[str] = []
            for batch_start in range(0, len(chunk_ids), 300):
                batch = _vector_with_retry(
                    col.get,
                    ids=chunk_ids[batch_start:batch_start + 300],
                    include=["documents"],
                )
                docs = batch.get("documents") or []
                for d in docs:
                    if d:
                        collected_docs.append(d)
            body_text = "\n".join(collected_docs)
        except Exception as exc:  # noqa: BLE001 — best-effort chunk scan; failure logged at debug, falls back to filename
            _log.debug("enrich_chunk_scan_failed", error=str(exc))

    # The document's file name comes from its catalog row (the chunk rows'
    # ``source_path`` key was dropped, RDR-102 D2).
    filename = source_paths[0] if source_paths else ""

    return extract_identifiers(filename=filename, body_text=body_text)


def _crossref_author_str(a: object) -> str:
    """Coerce one CrossRef author entry to a display string. Accepts a plain
    string (live DT shape) or a ``{"given","family"}`` dict (CrossRef native)."""
    if isinstance(a, str):
        return a.strip()
    if isinstance(a, dict):
        return " ".join(
            p for p in (str(a.get("given", "")).strip(), str(a.get("family", "")).strip()) if p
        )
    return ""


def _dt_crossref_bib(doi: str) -> dict:
    """RDR-139 Layer C: resolve ``doi`` to a nexus bib dict via DEVONthink's
    CrossRef resolver, or ``{}`` when DT misses / is unavailable.

    Maps the live ``resolve_doi_metadata`` shape (a flat dict; ``year`` is a
    string, ``authors`` a list) onto the same keys the S2/OpenAlex enrichers
    emit. CrossRef carries no citation count, so ``citation_count`` is 0 — a
    permanent gap DT never fills.
    """
    # nexus-fqsm5 (deferred, surfaced at Phase 2 close): RDR §Approach Layer C
    # also names search_crossref (title fallback for DOI-less records) and
    # resolve_google_books_metadata (books). Only the DOI path ships here; the
    # title/books fallbacks are tracked, not silently dropped.
    if not doi:
        return {}
    from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    raw = _dt.dt_resolve_doi(doi)
    if not raw:
        return {}
    year_raw = raw.get("year")
    try:
        year = int(str(year_raw)) if year_raw else 0
    except (TypeError, ValueError):
        year = 0
    authors = raw.get("authors")
    if isinstance(authors, list):
        # Live DT returns author strings, but CrossRef's native shape is
        # ``[{"given","family"}]``; handle both so a dict form doesn't
        # silently collapse to an empty author list.
        authors_str = ", ".join(
            s for s in (_crossref_author_str(a) for a in authors[:5]) if s
        )
    else:
        authors_str = str(authors or "")
    return {
        "year": year,
        "venue": raw.get("journal", "") or "",
        "authors": authors_str,
        "citation_count": 0,
        "doi": raw.get("doi", "") or doi,
    }


def _merge_bib_gapfill(primary: dict, supplement: dict) -> dict:
    """Per-field gap-fill: copy a ``supplement`` value into ``primary`` only
    where ``primary`` left the field empty/zero/absent, and never overwrite a
    set value (RDR-139 Layer C precedence guard). Falsy supplement values are
    never inserted. The guard is per-field, not call-ordering."""
    merged = dict(primary)
    for k, v in supplement.items():
        if v and not merged.get(k):
            merged[k] = v
    return merged


def _resolve_bib_for_title(
    *,
    title: str,
    chunk_ids: list,
    source_paths: list[str],
    col,
    backend: str,
    bib_enrich,
    ids: dict | None = None,
) -> dict:
    """nexus-sbzr: DOI/arXiv-aware lookup with title fallback.

    Tries identifiers in order:
      1. DOI extracted from first chunk's body text -> openalex by-doi
      2. arXiv ID from filename or first chunk text -> openalex by-arxiv
      3. Title search at the backend (current behavior)

    Returns the bib dict with a synthetic ``_resolved_via`` key
    indicating which path produced the result (caller strips before
    storage). Empty dict on miss across all three paths.

    ``ids`` may be supplied by the caller (the enrich loop pre-extracts them
    when both the primary path and the Layer C gap-fill need them); when
    ``None`` they are scanned here. DOI/arXiv lookups are OpenAlex-only today;
    the S2 backend skips straight to title search.
    """
    if backend != "openalex":
        bib = bib_enrich(title)
        if bib:
            bib["_resolved_via"] = "title"
        return bib or {}

    from nexus.bib_enricher_openalex import enrich_by_arxiv_id, enrich_by_doi  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    if ids is None:
        ids = _extract_identifiers_for_title(
            chunk_ids=chunk_ids, source_paths=source_paths, col=col,
        )

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
    import os as _os  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution

    chosen = source.lower()
    if chosen == "auto":
        chosen = "s2" if _os.environ.get("S2_API_KEY") else "openalex"

    if chosen == "openalex":
        from nexus.bib_enricher_openalex import enrich as _openalex_enrich  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
        return "openalex", _openalex_enrich, "bib_openalex_id"
    if chosen == "s2":
        from nexus.bib_enricher import enrich as _s2_enrich  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
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
    tumblers: list[str] | None = None,
) -> bool:
    """Update catalog entry with bib metadata. Silently skipped if absent.

    nexus-6pfgb.21: ``tumblers`` names the catalog documents to update and
    bypasses every lookup below. The callers that know their document (the
    manifest-driven enrich and ``--backfill-catalog``) pass it; ``title`` and
    ``source_paths`` then only describe the write in logs. Matching a catalog
    row by a chunk row's title is how one document's bib reached another's.

    Returns True when a catalog row was matched and updated, False on
    any no-op (catalog not initialized, no matching row, or an
    exception — best-effort, never raises).

    nexus-57mk: ``backend`` selects which ID field is written. The
    OpenAlex backend writes ``bib_openalex_id`` (and ``bib_doi`` when
    present) so the citation-link generator can match references in
    OpenAlex's W-id space.

    nexus-9l2lg: ``bib_year``/``bib_authors``/``bib_venue``/
    ``bib_citation_count``/``bib_enriched_at`` plus the backend id
    field(s) write to the catalog Document's top-level ``bib_*``
    columns (RDR-101) via ``writer.update(**bib_fields)`` — not the
    ``meta`` JSON blob. ``meta`` now carries only ``references``.

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
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — circular-dep avoidance; command-local import

        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — circular-dep avoidance; command-local import

        # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out + uninitialised.
        reader = make_catalog_reader()
        if reader is None:
            return False
        writer = make_catalog_writer()
        try:
            return _enrich_apply(
                reader, writer, collection_name, title, source_paths,
                bib_meta, backend, Tumbler, explicit_tumblers=tumblers,
            )
        finally:
            writer.close()
            if reader is not None:
                reader.close()
    except Exception:  # noqa: BLE001 — best-effort catalog enrich hook; failure logged at debug, primary enrich unaffected
        _log.debug("catalog_enrich_hook_failed", exc_info=True)
        return False


def _resolve_enrich_targets(
    reader, collection_name, title, source_paths, Tumbler,
) -> list:
    """The catalog rows a caller that holds only a title and chunk-row paths
    means: source path first, then the collection's exact title, then an exact
    title anywhere. Only callers with no document in hand reach this; the
    manifest-driven paths name their tumbler."""
    target_tumblers: list[Tumbler] = []

    # nexus-tv22: prefer source_path match (unique per document).
    # Catalog rows store either absolute or relative file_path
    # (relative is canonical post-RDR-060; absolute lingers from
    # legacy ingests). Try both forms so the lookup works either way.
    # nexus-xnz0o: replaced raw _db.execute with uniform catalog API.
    if source_paths:
        # nexus-xnz0o: replaced raw _db.execute (which used LIKE '%/'||file_path
        # for suffix matching) with uniform catalog API.
        # Load the collection once for suffix matching to avoid repeated fetches.
        _coll_cache: list | None = None

        def _coll_entries() -> list:
            nonlocal _coll_cache
            if _coll_cache is None:
                _coll_cache = reader.list_by_collection(collection_name) if collection_name else []
            return _coll_cache

        for sp in source_paths:
            if not sp:
                continue
            found_any = False
            # Step 1: exact match on absolute or relative form.
            for path_variant in (sp, sp.lstrip("/")):
                tumbler_str = reader.lookup_doc_id_by_collection_and_path(
                    collection_name or "", path_variant
                )
                if tumbler_str:
                    target_tumblers.append(Tumbler.parse(tumbler_str))
                    found_any = True
                    break
            if found_any:
                continue
            # Step 1b: an ABSOLUTE path has an exact key — its `source_uri`
            # (`file://<abspath>`, RDR-096 P3.1), which is what the INDEXER
            # writes and resolves by (`doc_indexer.by_source_uri`). Step 1
            # above can never match one, because the catalog stores
            # `file_path` RELATIVE; without this, an absolute path always
            # fell through to the O(collection) walk below. Point query,
            # collection-checked, and immune to the basename collisions the
            # suffix match can hit (this repo really does carry both
            # `docs/rdr/X.md` and `docs/rdr/post-mortem/X.md`).
            if os.path.isabs(sp):
                by_uri = getattr(reader, "by_source_uri", None)
                if by_uri is not None:
                    try:
                        hit = by_uri("file://" + sp)
                    except Exception:  # noqa: BLE001 — best-effort; fall through to the walk
                        hit = None
                    if hit is not None and (
                        not collection_name
                        or hit.physical_collection == collection_name
                    ):
                        target_tumblers.append(Tumbler.parse(str(hit.tumbler)))
                        continue
            # Step 2: suffix match — sp LIKE '%/' || file_path from the original SQL.
            # The catalog stores relative file_path; the caller may supply an absolute path.
            # Walk the collection and check if sp ends with '/' + file_path.
            for entry in _coll_entries():
                fp = entry.file_path
                if fp and (fp == sp or sp.endswith("/" + fp)):
                    target_tumblers.append(entry.tumbler)
                    found_any = True
            # Note: no break after suffix match — original SQL returned ALL matching rows.

    # Title match as fallback when source_paths are not provided
    # (preserves backward compatibility with callers that haven't
    # been updated yet — e.g. third-party scripts).
    if not target_tumblers and collection_name:
        # Use list_by_collection + filter by title (avoids raw SQL).
        # For service mode this is a paginated HTTP fetch; acceptable since
        # this is a rare fallback path.
        coll_entries = reader.list_by_collection(collection_name)
        for e in coll_entries:
            if e.title == title:
                target_tumblers.append(e.tumbler)
                break

    # Last-resort title lookup across the whole catalog
    # (legacy path; only fires when the caller passed neither
    # source_paths nor a title that matches inside the collection).
    # EXACT title: this used to take the first hit of a full-text search,
    # which matches tokens, so "Attention Is All You Need" could enrich
    # "Attention Is All You Need, Revisited" (nexus-gacz0). One target, as
    # before: the first exact match in catalog order (the reader's order, the
    # text order of the tumbler under the database collation, which is not
    # numeric order), the others named in the log when there are several, the
    # first few and a count so a common title cannot write a huge line.
    if not target_tumblers:
        entries = reader.find_by_title_exact(title, content_type="paper")
        if entries:
            target_tumblers.append(entries[0].tumbler)
            if len(entries) > 1:
                _log.warning(
                    "catalog_enrich_hook_title_ambiguous",
                    title=title,
                    chose=str(entries[0].tumbler),
                    others=[str(e.tumbler) for e in entries[1:1 + _TITLE_AMBIGUOUS_LOG_LIMIT]],
                    others_total=len(entries) - 1,
                )

    return target_tumblers


def _enrich_apply(
    reader, writer, collection_name, title, source_paths, bib_meta, backend, Tumbler,
    *, explicit_tumblers: list[str] | None = None,
) -> bool:
    """Returns True when >=1 catalog row was matched and updated, False
    when the hook was a silent no-op (no matching row — nexus-6ha8a
    follow-up, critic finding 3: the caller needs this to distinguish
    a genuine backfill from a no-op so summary counts stay honest).

    RDR-146 P1.2: reads via the read-only reader, writes via the
    write-only daemon proxy. Exceptions propagate to the caller's
    catalog_enrich_hook_failed handler.
    """
    target_tumblers: list[Tumbler] = [Tumbler.parse(str(t)) for t in explicit_tumblers or ()]
    if not target_tumblers:
        target_tumblers = _resolve_enrich_targets(
            reader, collection_name, title, source_paths, Tumbler,
        )

    if not target_tumblers:
        _log.debug(
            "catalog_enrich_hook_no_match",
            title=title,
            collection=collection_name,
            source_paths=source_paths or [],
        )
        return False

    # Build the update payload once, apply to every matched row.
    #
    # nexus-9l2lg: bib_* fields write to the catalog Document's top-level
    # bib_* columns (RDR-101), not the meta JSON blob. Nothing read venue
    # / citation_count / the backend id fields back out of meta (verified
    # by grep before removing) — dual storage was pure YAGNI. ``meta``
    # keeps only ``references``, which has no catalog column.
    bib_fields: dict = {
        "bib_year": bib_meta.get("year", 0),
        "bib_authors": bib_meta.get("authors", ""),
        "bib_venue": bib_meta.get("venue", ""),
        "bib_citation_count": bib_meta.get("citation_count", 0),
        "bib_enriched_at": datetime.now(UTC).isoformat(),
    }
    if backend == "openalex":
        bib_fields["bib_openalex_id"] = bib_meta.get("openalex_id", "")
        if bib_meta.get("doi"):
            bib_fields["bib_doi"] = bib_meta.get("doi", "")
    else:
        bib_fields["bib_semantic_scholar_id"] = bib_meta.get(
            "semantic_scholar_id", "",
        )
    meta_update: dict = {}
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
        writer.update(
            tumbler,
            author=bib_meta.get("authors", ""),
            year=bib_meta.get("year", 0),
            meta=meta_update,
            **bib_fields,
        )
    return True


# ── nx enrich aspects (RDR-089 P2.2) ────────────────────────────────────────


# Per-paper cost estimate. Used by --dry-run and the post-run actual-cost
# line.
#
# RDR-196 .p0b (bead nexus-nyry9.6): the prior ``0.01`` literal was a
# fabricated ceiling with no measurement behind it -- ~100x under the real
# figure (see the RDR §Trade-offs correction). ``aspect_extractor.py``
# does not itself capture per-call usage telemetry (audit fold F1 chose
# NOT to reroute it through ``operators.dispatch.claude_dispatch``.
# CORRECTED reason (substantive-critic review, T2 nexus/review-nexus-
# nyry9.6): the real sync callers are ``aspect_worker.py:571`` (the
# worker-thread extraction loop) and ``commands/enrich.py:1363`` (this
# CLI's own extraction loop) -- NOT ``HookRegistry.fire_document``, which
# only enqueues (its own docstring says it does not call
# ``extract_aspects`` inline). The actual blocker is
# ``PR_SET_PDEATHSIG`` parity: ``aspect_extractor.py``'s ``Popen`` call
# (around line 1572-1580) arms parent-death protection for the
# aspect-worker daemon (RDR-173 RF-8 / nexus-4r9ja); ``claude_dispatch``'s
# ``asyncio.create_subprocess_exec`` call (dispatch.py:943-948) has no
# equivalent -- see nexus-nyry9.6's corrected bd comment for the full
# reasoning. This is a single measured sample, not a live-metered
# figure.
#
# Measured 2026-08-20 via tests/test_aspect_extraction_cost_measurement.py
# ``TestLiveMeasurement`` -- one real dispatch of the SAME
# ``_SCHOLARLY_PAPER_PROMPT`` template production uses, filled with a
# ~1000-word synthetic paper, through ``claude_dispatch`` with NO
# ``--model`` override, which matched production at the time.
# Result: cost_usd=1.179986, canonical model=claude-fable-5,
# output_tokens=1087, duration_ms=13444.
#
# Since nexus-oc98c production passes the config's model_version as
# ``--model`` (haiku for scholarly-paper-v1) and logs each extraction's
# actual cost and model (``aspect_extractor_usage``), so that 1.18 figure
# is from the wrong model. Re-measured 2026-09-07 on claude-haiku-4-5
# through the production prompt over three real papers reassembled from T3
# (54k, 62k and 109k prompt tokens): $0.131, $0.159 and $0.306, mean $0.20.
# Re-measured 2026-10-30 on claude-haiku-5-5 (nexus-j6un5.1) through
# extract_aspects over three cloud papers (47k, 104k and 361k chars; the last
# bounded to 150k by _bound_content; about 28k, 48k and 66k prompt tokens):
# $0.0065, $0.0093 and $0.0130 by the envelope's total_cost_usd (list basis).
# The constant is their mean; the per-document actual is the
# aspect_extractor_usage log line. No measured paper crossed Haiku 5.5's 100k
# prompt-token price step; _bound_content cuts a paper at 150k chars (66k
# tokens on the paper it cut).
_PER_PAPER_COST_USD = 0.0096

# What _PER_PAPER_COST_USD assumes, stated in every estimate line (nexus-shake7861).
# It is a mean over PAPER-SIZED documents (about 28k-66k prompt tokens each), so
# for a collection of short notes it is too high. A one-line note was measured
# at $0.0035 in the 7.86.1 shakeout on claude-haiku-4-5 (aspect_extractor_usage
# log: cost_usd=0.00350840, 5.4 s); that has not been re-measured on
# claude-haiku-5-5, whose per-token price is a tenth of 4-5's. The paper mean is
# NOT an upper bound: the three papers ranged $0.0065-$0.0130, so a larger
# paper or a retry costs more than the line says.
_COST_BASIS = (
    "typical for paper-sized documents (mean of three measured papers, "
    "$0.0065-0.013 each, about 28k-66k prompt tokens); short notes cost less"
)

# The write floor on aspect confidence: the engine's upsert refuses a row below
# it and reports written=False (service AspectRepository.MIN_CONFIDENCE).
# tests/test_enrich_aspects.py::TestRejectedRowsAndBackendGate pins the two to one value.
_ASPECT_CONFIDENCE_FLOOR = 0.3

# Default per the RDR's original Phase 2 spec. The P1.3 spike's
# 16.7% strict-equality "stability" rate measures whether the model
# emits the same token sequence on a re-run, which is a methodology
# question (the model paraphrases between runs and should), NOT a
# hallucination-detection question. operator_verify is the
# hallucination guard. Once token-overlap or embedding-similarity
# stability metrics exist, this default should be revisited from
# real signal.
_DEFAULT_VALIDATE_SAMPLE_PCT = 5

# nexus-xwvwx: default filename for --validate-sample's disagreement log,
# written under nexus_config_dir() (never the CWD -- see
# _run_validation_sample's docstring for the incident this closes).
_VALIDATION_FAILURES_FILENAME = "aspect_validation_failures.jsonl"

# nexus-xwvwx: the evidence handed to operator_verify is the SAME
# full reassembled document text extract_aspects prompted the extractor
# with (via read_indexed_text -> read_source, no truncation at that
# layer). This cap is a defensive ceiling only, sized well above any
# realistic paper (a full-length paper reassembled from T3 chunks runs to
# a few hundred KB of prose at most) so full hydration is the normal
# case; it exists to bound worst-case operator_verify subprocess cost on
# a pathological outlier, not to shrink the evidence window by default
# the way the prior hardcoded ``content[:50000]`` slice did.
_VALIDATION_EVIDENCE_CHAR_CAP = 500_000

# nexus-fdk1x code-review (T2 [24110], xwvwx finding): a flat 60s
# operator_verify timeout under-served exactly the large-paper population
# the _VALIDATION_EVIDENCE_CHAR_CAP raise above targets -- the evidence
# cap removal took worst-case evidence from ~50k chars (the old hardcoded
# content[:50000] slice) to the full 500k-char ceiling above (roughly
# 10x more prose, ~125k tokens worst case), and a validation sample that
# times out lands SILENTLY in `errored` (not `disagreement`/`unverifiable`),
# undermining the fix's own goal. Scales linearly with evidence size,
# capped so a pathological outlier can't stall the batch indefinitely.
_VERIFY_TIMEOUT_BASE_SECONDS = 60.0
#: critique [24114] S2: at the old 1s/5k rate the 500k-char cap scaled to
#: 160s, which the critic measured as near-zero margin against real
#: operator_verify latency on a full paper — a validation that timed out
#: was recorded as "errored", i.e. the same silence this bead set out to
#: remove. 1s/2k doubles the budget (500k -> 310s) and the cap moves with
#: it so the scaler, not the cap, is what binds at realistic sizes.
_VERIFY_TIMEOUT_PER_CHARS = 2_000
_VERIFY_TIMEOUT_SECONDS_PER_CHUNK = 1.0
_VERIFY_TIMEOUT_CAP_SECONDS = 600.0


def _scaled_verify_timeout(evidence_chars: int) -> float:
    """``60s + 1s per 2k evidence chars``, capped at 600s.

    At the _VALIDATION_EVIDENCE_CHAR_CAP ceiling (500k chars) this scales
    to 60 + 250 = 310s. The rate was doubled from 1s/5k after critique
    [24114] measured near-zero margin against real operator_verify
    latency at that ceiling; the cap exists so a hypothetical larger
    evidence string is bounded rather than growing the timeout
    unboundedly.
    """
    scaled = _VERIFY_TIMEOUT_BASE_SECONDS + (
        max(0, evidence_chars) / _VERIFY_TIMEOUT_PER_CHARS
    ) * _VERIFY_TIMEOUT_SECONDS_PER_CHUNK
    return min(scaled, _VERIFY_TIMEOUT_CAP_SECONDS)


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
        "to <nexus config dir>/aspect_validation_failures.jsonl (override "
        "with --validation-out). Pass 0 to skip validation."
    ),
)
@click.option(
    "--validation-out",
    type=str,
    default="",
    help=(
        "Path for the validation-sample failures JSONL (nexus-xwvwx). "
        "Defaults to <nexus config dir>/aspect_validation_failures.jsonl "
        "-- never the current working directory."
    ),
)
@click.option(
    "--all",
    "extract_all",
    is_flag=True,
    help=(
        "Re-extract EVERY document in the collection, including ones that "
        "already have an aspect row. This was the default before nexus-ym9ey; "
        "on a Claude-CLI-backed collection it re-spends on the whole corpus, "
        "so it is now opt-in. Without it, only documents with NO aspect row "
        "are processed."
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
    validation_out: str,
    extract_all: bool,
    re_extract: bool,
    extractor_version: str,
) -> None:
    """Batch-extract structured aspects for documents in COLLECTION.

    Iterates the catalog (one entry per source document, NOT per
    chunk), calls extract_aspects directly (bypassing the
    ``HookRegistry.fire_document`` chain to avoid double-firing on
    documents already triggered at ingest), and upserts AspectRecords
    to ``document_aspects``.

    Two prefixes are supported: ``knowledge__*`` and ``rdr__*``. Other
    collection prefixes error out at the config-selection step.

    ``rdr__*`` always uses the deterministic markdown + frontmatter
    parser (rdr-frontmatter-v1; zero API cost).

    ``knowledge__*`` is ROUTED PER DOCUMENT (nexus-kmbys), by a
    structural heuristic, not a blanket collection-wide choice:
    paper-shaped documents (an Abstract/References heading, "et al.",
    "we propose"/"we present", an arXiv/DOI id -- two or more of these)
    get the Claude-CLI scholarly-paper-v1 extractor; everything else
    gets general-prose-v1, which never fabricates datasets, baselines,
    or a venue onto a design note or essay. A single ``nx enrich
    aspects knowledge__x`` run can therefore write rows under BOTH
    extractor names.
    """
    from nexus.aspect_extractor import eligible_extractor_names, select_config  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    from nexus.corpus import split_candidate_collection_name  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    config = select_config(collection)
    if config is None:
        click.echo(
            f"No extractor config registered for collection "
            f"'{collection}'. Supported prefixes: knowledge__*, "
            f"rdr__*, and docs__* collections opted in through "
            f"aspects.docs_collections. Aborting."
        )
        # RDR-204 Phase 3 (nexus-ft04v.26), class (a): `collection` reaches
        # here because select_config already rejected it as unsupported --
        # it may be a genuinely mistyped --collection argument with no
        # catalog row at all, so a row lookup could not help distinguish
        # that from "docs__* is real but unsupported" anyway.
        # Candidate-string derivation for the hint.
        if split_candidate_collection_name(collection)[0] == "docs":
            click.echo(
                "Note: docs__* collections are extracted only when opted in "
                "(nexus-kk4ut): nx config set aspects.docs_collections "
                f"'{collection}' (glob patterns allowed). Their prose files "
                "then get general-prose-v1. Academic PDFs belong in "
                "knowledge__<name> via 'nx index pdf --collection "
                "knowledge__<name>'."
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
        extract_all=extract_all,
        extractor_version=extractor_version,
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
        # A per-paper mean from measured dispatches on the config's model
        # (see _PER_PAPER_COST_USD), not a live-metered per-run figure;
        # the per-document actual lands in the aspect_extractor_usage log.
        cost_str = (
            f"Estimated cost: ~${cost_estimate:.2f} "
            f"(model {config.model_version}; {_COST_BASIS})"
        )
    # nexus-kk4ut: a knowledge__* collection is routed PER DOCUMENT
    # (nexus-kmbys) — select_config's `config` is only the prefix's base
    # extractor, and a batch can write rows under BOTH extractor names.
    # Naming just the base one here would be honest for rdr__* (never
    # shape-routed) but a lie for knowledge__* the instant one document
    # in the batch turns out to be general prose. The eligible-name count
    # decides which is true, rather than hardcoding per prefix.
    eligible = eligible_extractor_names(collection)
    extractor_desc = (
        config.extractor_name
        if len(eligible) <= 1
        else f"routed per document ({' or '.join(eligible)})"
    )
    click.echo(
        f"{len(entries)} document(s) in '{collection}' "
        f"(extractor={extractor_desc}, "
        f"version={config.model_version}). {cost_str}."
    )

    if dry_run:
        click.echo("--dry-run: skipping extraction.")
        _dry_run_predict_skips(
            entries, collection, is_deterministic=is_deterministic,
        )
        return

    if not is_deterministic:
        _require_llm_backend()

    extracted = _run_extraction(entries, collection, config)
    if not extracted:
        click.echo("No aspects extracted.")
        return

    if validate_sample > 0:
        _run_validation_sample(
            extracted,
            collection=collection,
            sample_pct=validate_sample,
            validation_out=validation_out,
        )


def _claude_on_path() -> bool:
    """Whether a ``claude`` executable is on PATH (the seam tests patch, not ``shutil.which``)."""
    import shutil  # noqa: PLC0415 — deferred; keeps CLI startup light

    return shutil.which("claude") is not None


def _require_llm_backend() -> None:
    """Refuse an LLM extraction run when no LLM backend can run, once, up front.

    With neither a ``claude`` on PATH nor an API key, every document would reach
    the extractor's spawn, fail with a raw ``[Errno 2] ... 'claude'``, and come
    out as a null-fields row; the run then exited 0 having done nothing
    (7.86.1 shakeout). The backend check the other LLM steps make
    (``NoBackendAvailableError``, whose message names every remedy) is made
    here instead of per document. Only this verb is gated: the extractor keeps
    its null-record fallback for the hook and worker paths, which have no
    operator to tell and must not stop the daemon. A misconfigured explicit
    backend (``anthropic`` with no key) is refused the same way.
    """
    from nexus.operators.llm_backend import DispatchRequest, resolve_backend_name  # noqa: PLC0415 — deferred command-local import; keeps CLI startup light
    from nexus.operators.llm_types import OperatorError  # noqa: PLC0415 — deferred, as above

    from nexus.operators.llm_backend import NO_BACKEND_MESSAGE  # noqa: PLC0415 — deferred, as above

    try:
        name = resolve_backend_name(DispatchRequest(prompt="", json_schema=None))
    except OperatorError as exc:  # NoBackendAvailableError is a subclass
        raise click.ClickException(str(exc)) from exc
    # An explicit NX_LLM_BACKEND=cli resolves to "cli" whatever is on PATH (the spawn is
    # meant to fail later with a clear message); here that failure is the per-document
    # [Errno 2] run this gate exists to stop, so check PATH.
    if name == "cli" and not _claude_on_path():
        raise click.ClickException(NO_BACKEND_MESSAGE)


def _aspect_identity(entry) -> str:
    """The ONE key a catalog entry is matched to ``document_aspects.source_path`` by.

    nexus-bocft. Two verbs asked "does this entry have an aspect row" with
    two different keys: the gap-fill used ``file_path or title``, the
    ``--missing`` audit used ``file_path and ...`` -- which silently drops
    every title-only entry. On knowledge__knowledge (2026-08-27): 416
    entries, 10 with a file_path; gap-fill would dispatch 407, the audit
    reported 1, and that "1" was the number the 7.18.0 changelog cited as
    proof the gap-fill worked. The audit verb could not see the population
    the billing verb charges for.

    ``file_path or title`` is the WRITER's rule, not an arbitrary choice:
    ``catalog/store_hook.py`` mints a store_put document's identity from its
    title (``uri_for(collection, title)``; single-chunk store_put callers
    carry no source_path), and file-backed knowledge documents carry their
    path. Both verbs must call this, never restate it.
    """
    return entry.file_path or entry.title or ""


def _select_entries(
    *,
    collection: str,
    re_extract: bool,
    extractor_version: str,
    extract_all: bool = False,
) -> list | None:
    """Return the catalog entries to process, or None if the catalog
    is missing (terminal error already echoed)."""
    from nexus.aspect_extractor import eligible_extractor_names  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

    # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out + uninitialised.
    cat = make_catalog_reader()
    if cat is None:
        click.echo("Catalog is empty — index or store documents first (nx index repo / nx store put).")
        return None
    entries = cat.list_by_collection(collection)
    # nexus-kk4ut: an opted-in docs__ collection extracts only its prose
    # files, the same rule the enqueue paths apply.
    from nexus.aspect_extractor import extraction_applies_to_source  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    raw_empty = not entries  # before the prose filter: "no rows" means the catalog has none
    non_prose = [e for e in entries if not extraction_applies_to_source(collection, e.file_path or "")]
    if non_prose:
        entries = [e for e in entries if extraction_applies_to_source(collection, e.file_path or "")]
        click.echo(
            f"Skipping {len(non_prose)} non-prose document(s) in {collection} "
            f"(only prose files such as .md, .rst, .adoc and .txt are extracted in docs__)."
        )
    # nexus-3ygp3: zero catalog rows is a refusal, never "No documents to
    # process" at exit 0 (a bare name select_config prefix-matched but the
    # catalog does not know read as a completed, zero-cost extraction).
    # The refusal is raised after the aspect-side read below so it can
    # name the rows that exist under this name with no entry to claim
    # them, as the --missing audit does; --all takes no such read and
    # refuses without the count.
    orphan_rows: int | None = None

    if re_extract or not extract_all:
        # ONE T2 open serves both filters. nexus-ym9ey originally added a
        # second `with T2Database(...)` for the gap-fill branch; that is a new
        # construction site, and storage_boundary_lint counts them per file
        # (T2DATABASE_CONSTRUCTION_ALLOWLIST, enrich.py: 8). Growing that count
        # is an explicit decision recorded on a bead, never a side effect of an
        # unrelated fix — so both filters read from the same handle instead.
        with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
            existing_paths = {
                r.source_path
                for r in db.document_aspects.list_by_collection(collection)
            }
            # nexus-kk4ut: query EVERY extractor_name this collection's rows
            # can carry, not just select_config's base config. A
            # knowledge__* document routed to general-prose-v1 at write
            # time (nexus-kmbys) writes a row under THAT extractor_name --
            # a query pinned to scholarly-paper-v1 alone never matches it,
            # so an outdated general-prose-v1 row would sit forever below
            # --extractor-version's threshold, invisible to --re-extract.
            #
            # ASSUMPTION (nexus-kk4ut, latent): one caller-supplied
            # `extractor_version` threshold is applied to EVERY eligible
            # extractor here, unconditionally. That is only coherent because
            # every extractor sharing a _SHAPE_ROUTING_TABLE entry currently
            # carries the SAME model_version scheme (both scholarly-paper-v1
            # and general-prose-v1 pin to the same Claude model string
            # today) -- comparing one lexicographic threshold against two
            # DIFFERENT model families would not mean "outdated" in any
            # coherent sense. This is NOT re-derived per call; it is pinned
            # by test_shape_routed_configs_share_one_model_version_scheme in
            # tests/test_aspect_extractor.py, which fails the day the
            # assumption breaks. If that test ever fires, --extractor-version
            # needs a per-extractor threshold (or a documented escape hatch)
            # before shipping the divergent config.
            outdated_paths: set[str] = set()
            if re_extract:
                for name in eligible_extractor_names(collection):
                    outdated_paths |= {
                        r.source_path
                        for r in db.document_aspects.list_by_extractor_version(
                            name, extractor_version,
                        )
                    }
        orphan_rows = len(existing_paths)
        if raw_empty:
            raise click.ClickException(unknown_collection_message(collection, orphan_rows=orphan_rows, row_label="aspect row"))

        if re_extract:
            # "Ensure every entry is at >= version": rows below the threshold,
            # plus rows with no aspect row at all (missing is by definition
            # below any version).
            entries = [
                e for e in entries
                if _aspect_identity(e) in outdated_paths
                or _aspect_identity(e) not in existing_paths
            ]
        else:
            # nexus-ym9ey: gap-fill is the DEFAULT. Before this, a bare
            # `nx enrich aspects COLLECTION` re-extracted every document in
            # the collection on every invocation. Measured 2026-08-24 on
            # rdr__1-1__voyage-context-3__v1: three consecutive runs each
            # reported "294 extracted", none of them filling a gap. Free for a
            # deterministic parser; on a Claude-CLI-backed collection it
            # re-spends on the whole corpus AND re-rolls non-deterministic
            # extractions that were already correct.
            before = len(entries)
            entries = [
                e for e in entries
                if _aspect_identity(e) not in existing_paths
            ]
            skipped = before - len(entries)
            if skipped:
                click.echo(
                    f"Skipping {skipped} document(s) that already have "
                    f"aspects. Use --all to re-extract them, or --re-extract "
                    f"--extractor-version X to refresh only outdated rows."
                )

    if raw_empty:
        raise click.ClickException(unknown_collection_message(collection, orphan_rows=orphan_rows, row_label="aspect row"))
    return entries


#: How many entries the dry-run's read-side prediction actually reads. Each
#: is a vector-service round-trip; in cloud mode ~0.5-1s. 25 keeps a dry-run
#: under half a minute on any collection and is enough to project a skip rate.
_DRY_RUN_PREDICT_SAMPLE = 25


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
    try:
        from nexus.aspect_readers import ReadFail, note_uri, read_source  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
        from nexus.mcp_infra import get_t3  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
        t3 = get_t3()
    except Exception as exc:  # noqa: BLE001 — best-effort T3 setup for dry-run; any failure surfaced to operator and prediction skipped
        click.echo(f"  (read-side prediction skipped: {exc})")
        return

    # nexus-o6aa.10.1: chroma reader resolves source_id → doc_id via
    # this projection so the dry-run reflects post-prune behavior. ``None``
    # falls back to the legacy probe (catalog absent / pre-Phase-3).
    doc_id_lookup = _build_catalog_doc_id_lookup()
    # nexus-8g79.2: catalog manifest is authoritative for chunk position
    # post-RDR-108 Phase 3 (chunk_index dropped from metadata).
    manifest_lookup = _build_catalog_manifest_lookup()

    # BOUNDED. One read_source per entry is one HTTPS round-trip to the
    # vector service per entry, serially, with nothing printed in between.
    # On knowledge__knowledge (407 entries, cloud mode) that ran past 300s
    # and was killed twice as a "hang" (nexus-bocft, 2026-08-27) -- a
    # dry-run that takes longer than the operator will wait is not a
    # dry-run. Predict from a fixed-size prefix and project; the exact
    # per-entry verdicts come from the real run, which reads each document
    # anyway.
    sample = entries[:_DRY_RUN_PREDICT_SAMPLE]
    if len(sample) < len(entries):
        click.echo(
            f"  Read-side prediction samples the first {len(sample)} of "
            f"{len(entries)} entries (one round-trip each); the skip rate "
            f"is projected onto the rest."
        )

    planned: dict[str, int] = {}
    skip_lines: list[str] = []
    by_host: dict[str, int] = {}
    for entry in sample:
        sp = _chroma_source_id_for_entry(entry)
        if not sp:
            continue
        uri = note_uri(collection, sp, quote_rest=True)
        try:
            result = read_source(
                uri, t3=t3,
                doc_id_lookup=doc_id_lookup,
                manifest_lookup=manifest_lookup,
            )
        except Exception as exc:  # noqa: BLE001 — per-entry isolation; transient read failure bucketed under read_error, loop continues
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

    skipped_in_sample = sum(planned.values())
    if skipped_in_sample == 0:
        click.echo(
            "  All sampled entries readable; full extraction would proceed."
            if len(sample) < len(entries)
            else "  All entries readable; full extraction would proceed."
        )
        return
    # Project the sampled skip rate onto the whole population. When the
    # sample IS the population this is exact.
    skipped = round(skipped_in_sample * len(entries) / len(sample))
    if len(sample) < len(entries):
        click.echo(
            f"  Planned skips: {skipped_in_sample} of {len(sample)} sampled "
            f"document(s) would skip on read failure — projected "
            f"~{skipped} of {len(entries)}:"
        )
    else:
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
            f"  Predicted actual cost (excluding skips): ~${actual_cost:.2f} "
            f"({_COST_BASIS})"
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
    from urllib.parse import unquote, urlparse  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution

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
    from urllib.parse import urlparse  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution

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
    from nexus.aspect_extractor import ExtractFail, extract_aspects  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    extractor_name = config.extractor_name

    # (source_path, AspectRecord, lookup_path). lookup_path is the T3
    # identity used for the read (nexus-vwns1: re-fetched for validation).
    extracted: list[tuple[str, object, str]] = []
    success = 0
    null_fields = 0
    skipped = 0
    skipped_unreadable = 0
    write_skipped = 0
    below_floor = 0
    by_reason: dict[str, int] = {}
    # nexus-kk4ut: the ACTUAL extractor_name each successful record was
    # written under, not `config.extractor_name` (the base config) --
    # a knowledge__* batch is routed per document (nexus-kmbys) and can
    # mix scholarly-paper-v1 and general-prose-v1 rows in one run.
    by_extractor: dict[str, int] = {}

    # nexus-o6aa.10.1: a single catalog projection serves every entry
    # in this collection. Built once outside the loop because the SQL
    # connection inside the helper is cheap-but-not-free per-call.
    doc_id_lookup = _build_catalog_doc_id_lookup()
    # nexus-8g79.2: same — manifest_lookup is process-cached.
    manifest_lookup = _build_catalog_manifest_lookup()

    db_path = default_db_path()
    # RDR-137 followup (nexus-hb99x): the per-paper aspect WRITE routes
    # through t2_index_write -> database.complete_aspect (daemon-
    # serialized) so it no longer competes for memory.db's WAL writer
    # lock and crashes the whole batch on 'database is locked' when a
    # concurrent T2 writer (nx enrich bib, a subagent memory_put, the
    # indexer) is active. The `db` handle below is retained for READS
    # only (doc_id_lookup / manifest_lookup / skip checks); WAL permits
    # concurrent readers, so reads need no routing. The prior stale
    # annotation claimed document_aspects.upsert was "not routable" —
    # true for the raw AspectRecord arg, but complete_aspect(asdict(...))
    # IS routable (added in nexus-zir76) and is the correct path.
    import dataclasses as _dataclasses  # noqa: PLC0415 — stdlib deferred to call site (startup cost)
    from nexus.db.t2.records import with_doc_id  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.db.t2.records)
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost (nexus.mcp_infra)
    with T2Database(db_path) as db:  # boundary-allow: read-only handle; the aspect WRITE routes via t2_index_write -> complete_aspect (nexus-hb99x)
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
            # nexus-vwns1: capture the lookup identity used for the T3
            # read so --validate-sample can re-fetch the EXACT indexed
            # chunk text the extractor saw (not the raw source bytes).
            lookup_path = _chroma_source_id_for_entry(entry)
            record = extract_aspects(
                content="",
                source_path=source_path,
                collection=collection,
                lookup_path=lookup_path,
                doc_id_lookup=doc_id_lookup,
                manifest_lookup=manifest_lookup,
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

            # RDR-137 followup (nexus-hb99x): routed write. complete_aspect
            # upserts document_aspects AND clears any matching aspect_queue
            # row (idempotent no-op when the CLI batch path has no queued
            # row for this (collection, source_path)). Daemon path when
            # reachable; direct-T2Database fallback otherwise.
            # nexus-x1de2 (52): the entry IS the catalog identity — stamp
            # it so the row is attributable (the extractor never sets it).
            record = with_doc_id(record, str(getattr(entry, "tumbler", "") or ""))
            # nexus-r0kum (review S2): the SAME refusal the worker paths apply —
            # a file-routed row with no source_uri is unfindable by construction.
            # The catalog entry IS the identity, so take its source_uri when the
            # extractor left the field empty; refuse (count, never upsert) only
            # when the entry cannot supply one either.
            if not record.source_uri:
                entry_uri = str(getattr(entry, "source_uri", "") or "")
                if entry_uri:
                    record = _dataclasses.replace(record, source_uri=entry_uri)
            from nexus.aspect_worker import _is_unattributable  # noqa: PLC0415 — deferred import, heavy module (same pattern as the worker paths)
            if _is_unattributable(record):
                _log.warning(
                    "aspect_extract_skip",
                    uri=source_path,
                    reason="unattributable_identity",
                    collection=collection,
                    extractor_name=extractor_name,
                )
                skipped_unreadable += 1
                by_reason["unattributable_identity"] = by_reason.get("unattributable_identity", 0) + 1
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                    "skipped (reason=unattributable_identity)"
                )
                continue
            try:
                stored = t2_index_write(
                    lambda t2db, _rec=record: t2db.complete_aspect(
                        _dataclasses.asdict(_rec)
                    )
                )
            except Exception as exc:  # noqa: BLE001 — best-effort; failure logged via _log.warning and counted, batch continues
                # nexus-24rf9: a single per-doc write failure (classically
                # 'database is locked' under multi-writer WAL contention,
                # even with RDR-129 B1's 30s busy_timeout) must NOT abort
                # the batch and discard every already-extracted doc's
                # paid-for LLM work. Log + count + skip + continue. This is
                # LOUD (structured warning + per-doc echo + summary tally),
                # not a silent fallback — the doc simply misses its row this
                # run and a re-run picks it up (complete_aspect is
                # idempotent). B1's busy_timeout is the wait/retry layer; no
                # app-level retry is stacked here (it would 3x the
                # worst-case stall under a long checkpoint).
                write_skipped += 1
                _log.warning(
                    "aspect_write_skip",
                    source_path=source_path,
                    collection=collection,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                    f"write-skipped ({type(exc).__name__})"
                )
                continue

            if record.problem_formulation is None:
                null_fields += 1
                # No attempt count here: a hard failure (no backend, a schema
                # miss) makes ONE attempt, and the record cannot say how many.
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                    "null-fields (extraction failed)"
                )
            elif stored is False:
                # complete_aspect returns the upsert result: False is the
                # engine's confidence gate refusing the row. Counting it as
                # extracted reported work that was never stored (7.86.1
                # shakeout: "Done: 4 extracted", 0 rows written).
                below_floor += 1
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: "
                    f"below the confidence floor ({_ASPECT_CONFIDENCE_FLOOR}), "
                    f"not stored (confidence {record.confidence})"
                )
            else:
                success += 1
                extracted.append((source_path, record, lookup_path))
                by_extractor[record.extractor_name] = (
                    by_extractor.get(record.extractor_name, 0) + 1
                )
                click.echo(
                    f"  [{i}/{len(entries)}] {Path(source_path).name}: extracted"
                )

    summary = (
        f"Done: {success} extracted, {null_fields} null-fields, "
        f"{below_floor} below the confidence floor "
        f"({_ASPECT_CONFIDENCE_FLOOR}, not stored), "
        f"{skipped_unreadable} skipped (read-failure), "
        f"{write_skipped} skipped (write-failure), "
        f"{skipped} skipped (other)"
    )
    if by_reason:
        reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
        summary += f". by_reason: {reasons_str}"
    # nexus-kk4ut: report what was ACTUALLY used, not what the pre-run
    # estimate named -- the only honest source for a shape-routed
    # collection is the routed records themselves.
    if len(by_extractor) > 1:
        extractor_str = ", ".join(f"{k}={v}" for k, v in sorted(by_extractor.items()))
        summary += f". by_extractor: {extractor_str}"
    click.echo(summary)
    return extracted


def _run_validation_sample(
    extracted: list[tuple[str, object, str]],
    *,
    collection: str,
    sample_pct: int,
    validation_out: str = "",
) -> None:
    """Sample N% of extracted records, run operator_verify against the
    INDEXED T3 chunk text (the same evidence the extractor consumed), and
    write disagreements to ``<nexus config dir>/aspect_validation_failures.jsonl``
    (override with ``validation_out``).

    nexus-vwns1: this previously read the raw source file off disk and
    handed those bytes to the verifier. For PDFs that yields the
    compressed content-stream / image-object binary, not the extracted
    prose, so the validator verified the claim against different evidence
    than the extractor saw — reliable false negatives on scanned/OCR'd
    docs. We now re-fetch the reassembled chunk text via
    :func:`read_indexed_text`. A sample whose indexed text cannot be
    re-fetched is counted as ``errored`` and skipped (never read raw bytes
    as a silent fallback); an all-errored run is surfaced loudly (below).

    This applies to ALL collection types, not just PDFs: the indexed chunk
    text is the correct evidence the extractor saw for born-digital prose
    too. For a plain-text source that happens to be unindexed in T3 this is
    a behavior change (previously validated against file bytes); such a
    sample is now skipped-and-counted rather than validated against a
    different source than the extractor used.

    nexus-xwvwx — two fixes:

    * The failures log used to land at ``./validation_failures.jsonl`` —
      the CWD, which for an operator running the command from a checkout
      is the repo root, leaving an untracked file behind. It now defaults
      under :func:`nexus.config.nexus_config_dir` (``--validation-out``
      overrides), and the resolved path is always printed in the final
      summary line.
    * ``read_indexed_text`` already returns the FULL reassembled document
      text — the SAME evidence :func:`nexus.aspect_extractor.extract_aspects`
      prompted the extractor with, no truncation at that layer. The prior
      code then silently sliced it to the first 50,000 characters (the
      document's intro, for anything long) before calling
      ``operator_verify``, so a real claim about later sections (a
      results table, a later baseline) came back "unverified" against
      evidence that never contained it — a false disagreement, not a
      real one. This function now hands the verifier the full text and
      only falls back to a bounded window on a pathological outlier
      (:data:`_VALIDATION_EVIDENCE_CHAR_CAP`); when that fallback fires
      AND the verifier reports non-verified, the row is classified
      ``unverifiable`` (evidence window was partial), never
      ``disagreement`` — the summary counts the two separately.
    """
    import asyncio  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution
    import json  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution
    import random  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution
    from datetime import UTC, datetime  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution

    from nexus.aspect_extractor import read_indexed_text  # noqa: PLC0415 — command-local import; deferred to validation path
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — command-local import; deferred to validation path

    # Process-cached catalog projections (same builders the extractor used);
    # cheap to rebuild here so validation is self-contained.
    doc_id_lookup = _build_catalog_doc_id_lookup()
    manifest_lookup = _build_catalog_manifest_lookup()

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

    failures_path = (
        Path(validation_out) if validation_out
        else nexus_config_dir() / _VALIDATION_FAILURES_FILENAME
    )
    failures_path.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    unverifiable = 0
    verified = 0
    errored = 0

    for source_path, record, lookup_path in sample:
        content = read_indexed_text(
            collection=collection,
            source_path=source_path,
            lookup_path=lookup_path,
            doc_id_lookup=doc_id_lookup,
            manifest_lookup=manifest_lookup,
        )
        if not content:
            errored += 1
            _log.warning(
                "validate_sample_indexed_text_unavailable",
                source_path=source_path, collection=collection,
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

        document_chars = len(content)
        evidence_truncated = document_chars > _VALIDATION_EVIDENCE_CHAR_CAP
        evidence = (
            content[:_VALIDATION_EVIDENCE_CHAR_CAP] if evidence_truncated else content
        )
        evidence_chars = len(evidence)

        try:
            result = asyncio.run(_verify(claim_json, evidence))
        except Exception as exc:  # noqa: BLE001 — per-sample isolation; verify failure counted as errored, validation loop continues
            errored += 1
            _log.warning(
                "validate_sample_verify_failed",
                source_path=source_path, error=str(exc),
            )
            continue

        if result.get("verified", False):
            verified += 1
            continue

        # nexus-xwvwx: a non-verified result against a TRUNCATED evidence
        # window is not a real disagreement -- the verifier may simply not
        # have seen the section the claim references. Classify separately
        # so the summary never conflates "the extraction looks wrong" with
        # "the sample's evidence window was partial".
        if evidence_truncated:
            unverifiable += 1
        else:
            failures += 1
        with failures_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "source_path": source_path,
                "extracted_aspects": claim_payload,
                "operator_verify_reason": result.get("reason", ""),
                "citations": result.get("citations", []),
                "timestamp": datetime.now(UTC).isoformat(),
                "evidence_truncated": evidence_truncated,
                "evidence_chars": evidence_chars,
                "document_chars": document_chars,
            }) + "\n")

    # nexus-vwns1: an all-errored run (no sample's indexed text could be
    # re-fetched — T3 down, collection not indexed) must NOT read as a quiet
    # pass. Surface it loudly so a broad read_indexed_text regression can't
    # make --validate-sample silently verify nothing.
    if sample_count and errored == sample_count:
        click.echo(
            f"WARNING: validation verified 0 samples — indexed text was "
            f"unavailable for ALL {sample_count} sampled document(s). "
            f"Is the collection indexed and T3 reachable? "
            f"(no verification signal this run)",
            err=True,
        )
    elif failures or unverifiable:
        click.echo(
            f"Validation: {verified} verified, {failures} disagreement(s), "
            f"{unverifiable} unverifiable (evidence truncated), "
            f"{errored} errored. Failures written to {failures_path}."
        )
    else:
        click.echo(
            f"Validation: all {verified} sample(s) verified "
            f"({errored} errored). Output: {failures_path}."
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

    Timeout scales with evidence size (see :func:`_scaled_verify_timeout`)
    rather than a flat 60s -- a large-paper validation sample (up to the
    500k-char _VALIDATION_EVIDENCE_CHAR_CAP) needs more than 60s to
    subprocess-dispatch and reason over.
    """
    from nexus.mcp.core import operator_verify  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    return await operator_verify(
        claim=claim_json,
        evidence=evidence,
        timeout=_scaled_verify_timeout(len(evidence)),
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
        "P3.2). Common values: 'file', 'nx-note', 'https', "
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
    filters to a single scheme (e.g., ``--scheme=nx-note`` lists only
    chunk-reassembly-backed rows).
    """
    from urllib.parse import urlparse  # noqa: PLC0415 — stdlib deferred to call site (startup cost)

    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
        records = db.document_aspects.list_by_collection(
            collection, limit=limit if limit > 0 else None,
        )

    if scheme == "chroma":
        # RDR-231: a note's source_uri is nx-note:// now; chroma:// survives only as residue under a
        # file-routed collection. The filter still matches that residue literally, but an operator who
        # typed the old scheme for a note would see an empty list and no reason.
        click.echo(
            "Note: a note's source_uri scheme is now 'nx-note' (RDR-231); 'chroma' matches only "
            "legacy residue rows. Use --scheme nx-note for notes."
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
    import json  # noqa: PLC0415 — branch-local stdlib import; deferred to command execution

    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
        record = db.document_aspects.get(collection, source_path)

    if record is None:
        click.echo(
            f"No aspect row for ({collection!r}, {source_path!r})."
        )
        return

    from urllib.parse import urlparse  # noqa: PLC0415 — stdlib deferred to call site (startup cost)

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
@click.argument("source_path", required=False, default="")
@click.option(
    "--all", "delete_all",
    is_flag=True,
    help="Remove every aspect row in COLLECTION instead of one row by SOURCE_PATH.",
)
@click.option(
    "--no-dry-run",
    is_flag=True,
    help=(
        "With --all: actually delete the rows. Without it, --all only "
        "reports the row count and makes no writes (dry-run by default)."
    ),
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    help="Skip the confirmation prompt.",
)
def enrich_aspects_delete(
    collection: str, source_path: str, delete_all: bool, no_dry_run: bool, yes: bool,
) -> None:
    """Remove one aspect row by (COLLECTION, SOURCE_PATH), or every row in
    COLLECTION with --all.

    Idempotent: deleting a non-existent row prints a notice and
    exits 0. Re-extraction (``nx enrich aspects --re-extract``)
    will repopulate the row when run.

    ``--all`` (nexus-3foc9) removes every aspect row in COLLECTION in
    one shot -- the cleanup step for a collection that was opted OUT
    of extraction (``aspects.docs_collections``): opting out stops new
    writes but leaves rows already written fully visible to
    aspect-scoped search and groupby. It is DRY-RUN BY DEFAULT: it
    reports the row count and writes nothing. Pass ``--no-dry-run
    --yes`` (or answer the confirmation prompt) to actually delete.
    """
    if delete_all and source_path:
        raise click.UsageError("Pass either SOURCE_PATH or --all, not both.")
    if not delete_all and not source_path:
        raise click.UsageError(
            "Pass SOURCE_PATH, or --all to remove every aspect row in COLLECTION."
        )

    if delete_all:
        _enrich_aspects_delete_all(collection, dry_run=not no_dry_run, yes=yes)
        return

    if not yes:
        click.confirm(
            f"Delete aspect row for ({collection!r}, "
            f"{source_path!r})?",
            abort=True,
        )

    # RDR-128 P3 (nexus-sbxbe.3): route the delete through the daemon.
    # document_aspects.delete (str/str args, int return) is routable —
    # distinct from document_aspects.upsert, which is RPC-denied.
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
    deleted = t2_index_write(
        lambda db: db.document_aspects.delete(collection, source_path)
    )

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


def _enrich_aspects_delete_all(collection: str, *, dry_run: bool, yes: bool) -> None:
    """nexus-3foc9: bulk-remove every ``document_aspects`` row in *collection*.

    Pages ``list_by_collection`` at <=300 rows per call (the paging
    ceiling every T3/T2 list verb in this codebase honors; see
    AGENTS.md's quota table) and, when not a dry run, deletes row by
    row through the same routed write ``nx enrich delete`` uses for a
    single row -- the store exposes no bulk-delete RPC (see
    ``HttpDocumentAspectsStore``'s docstring), so per-row is the only
    available primitive.

    Refuses only when there is NOTHING to act on and the name itself
    looks unknown to the catalog (zero aspect rows AND zero catalog
    entries for *collection* -- the same ``raw_empty`` test the
    ``aspects``/``aspects-list --missing`` verbs in this file use,
    via ``unknown_collection_message``). A collection with real aspect
    rows is always actionable regardless of catalog state -- that is
    precisely the opted-out-collection scenario this bead exists for:
    the catalog still lists the collection's documents (indexing
    itself didn't stop), only extraction did, so refusing here would
    block the exact cleanup being asked for.
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    rows = []
    with T2Database(default_db_path()) as db:  # boundary-allow: read-only paged listing (nexus-3foc9)
        offset = 0
        while True:
            batch = db.document_aspects.list_by_collection(
                collection, limit=300, offset=offset,
            )
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 300:
                break
            offset += 300

    if not rows:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

        cat = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
        catalog_entries = cat.list_by_collection(collection) if cat is not None else []
        if not catalog_entries:
            raise click.ClickException(unknown_collection_message(collection))
        click.echo(f"No aspect rows for '{collection}' — nothing to delete.")
        return

    total = len(rows)
    click.echo(f"{total} aspect row(s) in '{collection}'.")

    if dry_run:
        click.echo(
            "--dry-run (default): no rows deleted. Pass --no-dry-run "
            "--yes to delete them."
        )
        return

    if not yes:
        click.confirm(
            f"Delete all {total} aspect row(s) in {collection!r}?",
            abort=True,
        )

    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    _PROGRESS_EVERY = 100
    deleted = 0
    for i, r in enumerate(rows, start=1):
        source_path = r.source_path
        deleted += t2_index_write(
            lambda db, sp=source_path: db.document_aspects.delete(collection, sp)
        )
        if i % _PROGRESS_EVERY == 0 or i == total:
            click.echo(f"  deleted {i}/{total} row(s)...")

    click.echo(f"Deleted {deleted} aspect row(s) from '{collection}'.")


# ── extras → fixed-column promotion: history only (RDR-089 Phase E) ─────────
#
# The runtime promotion verb is RETIRED (Hal decision 2026-07-25, bead
# nexus-70x7y): schema changes go through Liquibase in every mode, so there is
# no runtime ALTER TABLE left to run. The command keeps its name so an operator
# reaching for the old verb lands on the changeset recipe instead of a
# "no such command", and so --history stays where it has always been.


@enrich.command(name="aspects-promote-field")
@click.argument("field_name", required=False)
@click.option(
    "--history",
    is_flag=True,
    help="Print the promotion audit log and exit.",
)
def enrich_aspects_promote_field(field_name: str | None, history: bool) -> None:
    """Show the extras->column promotion history.

    Promoting a field is a Liquibase changeset, not a runtime command —
    invoking this with a FIELD_NAME prints the changeset recipe and exits
    non-zero. See ``src/nexus/aspect_promotion.py`` for the full template.

    ``--history`` reads ``aspect_promotion_log`` on whichever
    ``document_aspects`` backend is configured.
    """
    from nexus.aspect_promotion import (  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands
        PROMOTION_RETIRED, list_promotions,
    )
    import httpx  # noqa: PLC0415 — deferred command-local import; avoids import-time cost for unrelated CLI commands

    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    if not history:
        # A bare invocation with no field and no --history is a usage error;
        # with a field it is the retired verb. Both end at the recipe.
        click.echo(
            PROMOTION_RETIRED.format(field=field_name or "<field>"), err=True,
        )
        raise click.exceptions.Exit(2)

    try:
        with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
            entries = list_promotions(db)
    except RuntimeError as exc:
        # RuntimeError from T2Database init when the service backend is
        # configured but NX_SERVICE_PORT/NX_SERVICE_TOKEN are missing.
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(2)
    except httpx.HTTPError as exc:
        # Service backend with a RESOLVABLE endpoint that is down, wedged, or
        # answering non-2xx. The SQLite path could only ever fail at open
        # time; the HTTP read path adds a whole transport-error class that
        # would otherwise reach the operator as a traceback.
        click.echo(f"Error: promotion history unavailable: {exc}", err=True)
        raise click.exceptions.Exit(2)
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
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — circular-dep avoidance; command-local import

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

    # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out + uninitialised.
    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog is empty. Index or store documents first (nx index repo / nx store put)."
        )
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    _, entry = _resolve_catalog_entry(tumbler_or_title)
    if not entry.physical_collection or not entry.file_path:
        click.echo(
            f"No aspect record for tumbler {entry.tumbler}: catalog "
            "row has no physical_collection or file_path. Run "
            "'nx enrich aspects <COLLECTION>' to extract."
        )
        return

    with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
        # nexus-6xp2: post-drop, source_path-keyed lookup is unreliable
        # when the writer used a non-uri_for source_uri. Tumbler-keyed
        # get_by_doc_id is exact; fall back to legacy (coll, path) get
        # only when doc_id PK isn't in place yet.
        if db.document_aspects._has_doc_id_pk():
            record = db.document_aspects.get_by_doc_id(str(entry.tumbler))
        else:
            record = db.document_aspects.get(
                entry.physical_collection, entry.file_path,
            )

    if record is None:
        # nexus-hj7mg: the remedy this used to print unconditionally
        # ("Run 'nx enrich aspects <collection>'") aborts on any prefix
        # the extractor-config registry doesn't cover (code__* is the
        # large majority of the catalog — 15,428 of 19,328 documents,
        # measured 2026-08-24). Consult the SAME registry `nx enrich
        # aspects` itself uses so this can never drift out of sync with
        # it, and say the actual fact for an unsupported prefix instead
        # of naming a command that fails.
        from nexus.aspect_extractor import registered_prefixes, select_config  # noqa: PLC0415 — circular-dep avoidance; command-local import

        if select_config(entry.physical_collection) is None:
            prefixes = ", ".join(f"{p}*" for p in registered_prefixes())
            click.echo(
                f"Aspects are not extracted for "
                f"{entry.physical_collection!r} collections (tumbler "
                f"{entry.tumbler}, {entry.title!r}). Supported: {prefixes}."
            )
        else:
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
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    if missing:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

        # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out + uninitialised.
        cat = make_catalog_reader()
        if cat is None:
            raise click.ClickException(
                "Catalog is empty. Index or store documents first (nx index repo / nx store put)."
            )
        entries = cat.list_by_collection(collection)
        with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
            existing = {
                r.source_path for r in db.document_aspects.list_by_collection(
                    collection,
                )
            }
        if not entries:
            # Zero catalog rows means zero gaps by construction, and
            # "no missing aspects" for that is the vacuous pass the gate
            # doctrine bans (measured 2026-09-07: a bare subject name
            # reported full coverage while 54 of 58 rows had no aspect
            # record). Refuse, and still say what the aspect side holds,
            # since every one of those rows is an orphan of this name.
            raise click.ClickException(unknown_collection_message(collection, orphan_rows=len(existing), row_label="aspect row"))
        # THE SAME KEY THE GAP-FILL USES (nexus-bocft). This read
        # `e.file_path and e.file_path not in existing`, which is not a gap
        # test -- it is a gap test over the subset of entries that happen to
        # have a file_path, and for a knowledge collection that subset is
        # almost nothing. The number it printed was cited as evidence.
        gaps = [e for e in entries if _aspect_identity(e) not in existing]
        # Aspect rows no current entry claims. A large count here is the
        # signature of an identity-era change (paths or hashes recorded
        # under an older registration rule) and is what explains "437 rows
        # but 407 gaps" -- the audit is not complete without it.
        claimed = {_aspect_identity(e) for e in entries}
        orphaned = sorted(p for p in existing if p not in claimed)
        if as_json:
            click.echo(json.dumps({
                "collection": collection,
                "entries": len(entries),
                "aspect_rows": len(existing),
                "gaps": [
                    {
                        "tumbler": str(e.tumbler),
                        "title": e.title,
                        "file_path": e.file_path,
                        "identity": _aspect_identity(e),
                    }
                    for e in gaps
                ],
                "orphaned_aspect_rows": orphaned,
            }, indent=2))
            return
        if not gaps and not orphaned:
            click.echo(
                f"No missing aspects in '{collection}': every catalog "
                f"row has an extracted aspect record."
            )
            return
        if gaps:
            click.echo(
                f"{len(gaps)} of {len(entries)} catalog row(s) in '{collection}' "
                f"have no aspect record:"
            )
            for e in gaps[:limit] if limit else gaps:
                click.echo(f"  {e.tumbler}  {_aspect_identity(e)}")
            if limit and len(gaps) > limit:
                click.echo(f"  ... and {len(gaps) - limit} more.")
        else:
            click.echo(
                f"No missing aspects in '{collection}': every catalog "
                f"row has an extracted aspect record."
            )
        if orphaned:
            click.echo(
                f"{len(orphaned)} aspect row(s) in '{collection}' match no "
                f"current catalog entry (identities from an earlier "
                f"registration rule; they do not cover the gaps above)."
            )
        return

    with T2Database(default_db_path()) as db:  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)
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


@enrich.command(name="aspects-backfill-uri")
@click.option(
    "--collection", default="",
    help="Repair one collection. Default: every collection holding aspect rows.",
)
@click.option(
    "--apply", is_flag=True,
    help="Perform the repair (default: dry-run report only).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Emit JSON instead of the human-readable report.",
)
def aspects_backfill_uri_cmd(collection: str, apply: bool, as_json: bool) -> None:
    """Fill in a missing ``source_uri`` on existing aspect rows.

    ``document_aspects.source_uri`` is the key the ``aspect_sql`` operators
    look a row up by — ``operator_filter`` / ``operator_groupby`` /
    ``operator_confidence_aggregate`` re-derive it with ``uri_for`` and match
    it byte-equal in the engine. A row stored without one matches nothing, and
    the operator reports that miss as "does not match": a content verdict, not
    an error, so the row is silently absent from analytic answers rather than
    loudly missing.

    The rows were written by the batch aspect builder before it minted the URI
    (fixed in ``_build_record_from_entry``). This repairs the ones already
    stored. It changes ONLY ``source_uri``, and only where it is empty, so a
    second run is a no-op.

    Nothing is written without ``--apply``. The per-collection census prints
    either way, so the number is explained rather than eyeballed.
    """
    from nexus.aspect_uri_repair import plan_source_uri_backfill  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance; command-local import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance; command-local import

    with T2Database(default_db_path()) as db:  # boundary-allow: read-only until --apply
        if collection:
            collections = [collection]
        else:
            collections = sorted({
                c["name"] if isinstance(c, dict) else str(c)
                for c in _aspect_bearing_collections(db)
            })

        per_collection: list[dict] = []
        applied = 0
        rejected: list[tuple[str, str]] = []
        for coll in collections:
            rows = db.document_aspects.list_by_collection(coll)
            plan = plan_source_uri_backfill(rows)
            per_collection.append({
                "collection": coll,
                "total": plan.total,
                "already_attributed": plan.already_attributed,
                "repairable": len(plan.updates),
                "refused": len(plan.refusals),
                "refusals": [
                    {"source_path": r.source_path, "reason": reason}
                    for r, reason in plan.refusals
                ],
            })
            if not apply:
                continue
            for repaired, _new_uri in plan.updates:
                # upsert returns False when the engine's confidence gate drops
                # the write. The planner already refuses those, so a False here
                # is a surprise worth reporting rather than counting as done.
                if db.document_aspects.upsert(repaired):
                    applied += 1
                else:
                    rejected.append((coll, repaired.source_path))

        report = {
            "apply": apply,
            "collections": per_collection,
            "total_rows": sum(c["total"] for c in per_collection),
            "total_repairable": sum(c["repairable"] for c in per_collection),
            "total_refused": sum(c["refused"] for c in per_collection),
            "applied": applied,
            "rejected_by_store": [
                {"collection": c, "source_path": p} for c, p in rejected
            ],
        }

    if as_json:
        click.echo(json.dumps(report, indent=2, default=str))
        return

    for c in per_collection:
        if not (c["repairable"] or c["refused"]):
            continue
        click.echo(
            f"  {c['collection']}: {c['repairable']} repairable, "
            f"{c['refused']} refused, {c['already_attributed']} already attributed "
            f"(of {c['total']})"
        )
        for r in c["refusals"][:5]:
            click.echo(f"      REFUSED {r['source_path'][:24]}: {r['reason']}")
        if len(c["refusals"]) > 5:
            click.echo(f"      … {len(c['refusals']) - 5} more refusal(s) (--json for all)")

    click.echo(
        f"Rows: {report['total_rows']}, repairable: {report['total_repairable']}, "
        f"refused: {report['total_refused']}"
    )
    if apply:
        click.echo(f"Applied: {applied}")
        for c, p in rejected:
            click.echo(f"  REJECTED BY STORE {c} {p[:24]}")
    elif report["total_repairable"]:
        click.echo("Dry run — re-run with --apply to write.")


def _aspect_bearing_collections(db) -> list[str]:
    """Collection names that hold at least one aspect row.

    Derived from the catalog's collection projection rather than a distinct
    scan of ``document_aspects``: the store exposes no such verb, and a
    per-collection list is what the planner consumes anyway.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance; command-local import

    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog is empty. Index or store documents first."
        )
    names = {
        d.physical_collection for d in cat.all_documents()
        if getattr(d, "physical_collection", "")
    }
    return sorted(names)
