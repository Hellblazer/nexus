# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx dt`` — DEVONthink integration verbs (RDR-099 P2).

Glue between the macOS-only :mod:`nexus.devonthink` selectors and the
existing ``nx index pdf`` / ``nx index md`` ingest paths. The operator
picks records in DT (selection / tag / group / smart group / UUID) and
``nx dt index`` walks each ``(uuid, path)`` pair into the right indexer
by file extension.

Mutual exclusion is enforced at the Click layer — exactly one selector
flag must be supplied. ``--uuid`` accepts ``multiple=True`` so batch
ingest of a known UUID list (e.g. from a smart-rule) doesn't require
shell-side fan-out.

Per-record dispatch lives in :func:`_index_record`. Tests monkeypatch
this single function rather than the heavyweight ``doc_indexer``
machinery, so the CLI surface (flag wiring, mutual-exclusion, dry-run,
error mapping) is exercised independently of the indexer internals.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import click
import structlog

import nexus.devonthink as dt_mod
from nexus.devonthink import DTNotAvailableError, _is_darwin

_log = structlog.get_logger(__name__)


_SUPPORTED_EXTS: frozenset[str] = frozenset({".pdf", ".md"})


def _resolve_dt_collection(
    collection: str | None, corpus: str, ext: str,
) -> str:
    """nexus-cvaw: pick the right T3 collection for a DT-sourced record.

    ``--collection X`` always wins (operator override). Otherwise:

    * PDF (``.pdf``) -> ``knowledge__<corpus>-papers__voyage-context-3__v1``.
      Paper-shaped content goes to a knowledge__ collection so
      scholarly-paper-v1 can aspect-extract it.
    * Markdown (``.md``) -> ``docs__<corpus>__voyage-context-3__v1``.
      Notes / clippings / doc-shaped content goes to docs__, which
      deliberately doesn't route to any aspect extractor (nexus-z70w).

    RDR-103 Phase 5: the legacy 2-segment defaults
    (``knowledge__<corpus>-papers`` / ``docs__<corpus>``) are promoted
    to conformant 4-segment names so the strict-naming guard at
    ``T3Database.get_or_create_collection`` accepts them.
    """
    from nexus.corpus import resolve_write_embedding_model  # noqa: PLC0415 — command-local import (resolve_write_embedding_model)

    if collection:
        return collection
    owner = corpus.replace("_", "-")
    content_type = "knowledge" if ext == ".pdf" else "docs"
    suffix = f"{owner}-papers" if ext == ".pdf" else owner

    _db_cache: list = []  # nexus-o5x2c: memoize make_t3() across candidates below

    def _local_token_collection_exists(token: str) -> bool:
        # nexus-o5x2c: no live T3 handle in scope here — best-effort
        # construct one purely to probe for a pre-existing collection to
        # grandfather onto (a keyless voyage-configured local install
        # with an existing DT-sourced bge/minilm collection must not
        # crash on the next `nx dt index`). Wrapped by
        # resolve_write_embedding_model's own try/except. Constructed
        # ONCE and reused across every candidate in LOCAL_EMBEDDING_MODELS
        # (reviewer follow-up), not once per candidate.
        if not _db_cache:
            from nexus.db import make_t3  # noqa: PLC0415 — deferred, probe-local
            _db_cache.append(make_t3())
        candidate = (
            f"knowledge__{suffix}__{token}__v1" if ext == ".pdf"
            else f"docs__{suffix}__{token}__v1"
        )
        return _db_cache[0].collection_exists(candidate)

    model = resolve_write_embedding_model(
        content_type, collection_exists=_local_token_collection_exists,
    )
    if ext == ".pdf":
        return f"knowledge__{owner}-papers__{model}__v1"
    return f"docs__{owner}__{model}__v1"


def _index_record(
    uuid: str,
    path: str,
    *,
    collection: str | None,
    corpus: str,
    dry_run: bool,
    extractor: str = "auto",
) -> tuple[bool, int]:
    """Dispatch a single supported ``(uuid, path)`` to the right indexer.

    The caller (``index_cmd``) is responsible for filtering unsupported
    extensions before calling this function — that lets tests and the
    summary line see the skip count without having to introspect the
    dispatcher's internals.

    After the indexer registers the catalog entry (with the resolved
    ``file://`` source_uri it sees), this function stamps the DT
    identity onto the entry: ``source_uri = x-devonthink-item://<UUID>``
    and ``meta.devonthink_uri`` set to the same value. RDR-099 AC-1
    requires this — the catalog identity must be stable across DT
    relocations, and the file path returned by osascript at index time
    is not (DT moves files inside Files.noindex/ on its own schedule).

    Returns ``(stamped, chunks)`` (nexus-5xn3k.6 AC4 — before this, the
    indexer's chunk count was discarded and the caller could only do
    ``indexed += 1`` unconditionally, so a no-op re-index of an unchanged
    document reported "Indexed 1 record(s)" identically to a real write).
    ``stamped`` is the stamp's success status (``True`` when the catalog
    entry now carries the DT identity, ``False`` otherwise) so the caller
    can surface stamp misses in the summary line. ``chunks`` is the
    indexer's own return (0 means the staleness gate short-circuited —
    the document is unchanged — or the indexer produced no content).
    Indexing itself is treated as a precondition: an indexer exception
    will propagate.

    Tests monkeypatch this single function rather than the heavyweight
    ``doc_indexer`` machinery so the CLI surface is exercised
    independently of Voyage credentials and Chroma clients.
    """
    if dry_run:
        # Dry-run is handled in the command body before this function
        # is reached. If a caller invokes us with dry_run=True anyway,
        # treat it as a no-op rather than a silent indexing run.
        return True, 0

    from nexus.doc_indexer import index_markdown, index_pdf  # noqa: PLC0415 — command-local import (doc_indexer)

    file_path = Path(path)
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        # nexus-pxxyn: thread the operator's --extractor choice through so the
        # documented MinerU-failure recovery ("rerun with --extractor docling")
        # is actionable on the DT path. Markdown has no extractor backend.
        raw = index_pdf(file_path, corpus=corpus, collection_name=collection, extractor=extractor)
    else:  # .md — extension filtering happens in index_cmd
        raw = index_markdown(file_path, corpus=corpus, collection_name=collection)
    chunks = raw if isinstance(raw, int) else 0

    stamped = _stamp_dt_uri_on_entry(file_path, uuid)
    return stamped, chunks


def _stamp_dt_uri_on_entry(file_path: Path, uuid: str) -> bool:
    """Set ``source_uri`` and ``meta.devonthink_uri`` on the catalog
    entry that was just indexed for ``file_path``.

    The indexer registers the entry with the resolved local path as
    ``source_uri`` (``file://...``); this is fine for non-DT ingest but
    breaks RDR-099 AC-1, where the catalog identity must survive DT
    moving the underlying file inside its ``Files.noindex/`` tree.
    Looking up the entry by ``file_path`` immediately after the indexer
    call is reliable because no other registrar runs between the two.

    Returns ``True`` when the entry now carries the DT identity,
    ``False`` on any miss (uninitialized catalog, no matching row,
    SQLite exception). Failures are logged and surfaced in the dt
    index summary line by the caller; the function does not raise so
    a stamp miss leaves a recoverable ``file://`` entry rather than
    aborting the whole batch. ``nx catalog update --source-uri`` can
    recover after the fact.
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — command-local import (catalog.factory)

    dt_uri = f"x-devonthink-item://{uuid}"
    # nexus-kmo9h: factory delegation — None only in SQLite opt-out mode
    # when uninitialised; service mode always proceeds.
    reader = make_catalog_reader()
    if reader is None:
        _log.warning(
            "dt_stamp_skipped_uninitialized_catalog",
            file_path=str(file_path),
            uuid=uuid,
        )
        return False
    # RDR-146 P2 (nexus-5p2ci.12): foreground, user-initiated dt write —
    # the latency-sensitive op #1046 showed starved by background indexing.
    # Tag interactive so the daemon prioritises it over a batch index burst.
    writer = make_catalog_writer(priority="interactive")
    try:
        # Globally find the entry by file_path — no owner constraint
        # because we don't know it from here. ``documents`` is keyed
        # by tumbler primary key plus a unique (file_path) row per
        # indexed file, so this returns one row.
        # nexus-xnz0o: use catalog API (uniform SQLite + service mode).
        entry = reader.find_by_file_path(str(file_path))
        if entry is None:
            _log.warning(
                "dt_stamp_no_entry_found",
                file_path=str(file_path),
                uuid=uuid,
            )
            return False

        tumbler = entry.tumbler
        writer.update(
            tumbler,
            source_uri=dt_uri,
            meta={"devonthink_uri": dt_uri},
        )
        _log.debug(
            "dt_stamp_applied",
            tumbler=str(tumbler),
            uuid=uuid,
            dt_uri=dt_uri,
        )
        return True
    except Exception as e:  # noqa: BLE001 — DEVONthink boundary op is best-effort; failure logged via log.warning
        _log.warning(
            "dt_stamp_failed",
            file_path=str(file_path),
            uuid=uuid,
            error=str(e),
        )
        return False
    finally:
        writer.close()
        if reader is not None:
            reader.close()


def _link_semantic_record(uuid: str) -> bool:
    """Create Layer B DT-derived 'relates' edges for a just-indexed record.

    Resolves the record's tumbler via ``Catalog.by_source_uri`` (just stamped
    with ``x-devonthink-item://<uuid>``) and calls
    :func:`nexus.catalog.dt_link_generator.generate_dt_links`. Returns ``True``
    when at least one edge was created. Fail-soft: any error or unresolvable
    tumbler logs and returns ``False`` — linking never aborts the index batch.
    """
    from nexus.catalog.dt_link_generator import generate_dt_links  # noqa: PLC0415 — command-local import (catalog.dt_link_generator)
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — command-local import (catalog.factory)

    dt_uri = f"x-devonthink-item://{uuid}"
    reader = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
    if reader is None:
        return False
    # RDR-146 P2: foreground dt write — interactive priority (see above).
    writer = make_catalog_writer(priority="interactive")
    try:
        entry = reader.by_source_uri(dt_uri)
        if entry is None:
            _log.warning("dt_link_no_entry", uuid=uuid)
            return False
        counts = generate_dt_links(reader, entry.tumbler, uuid, writer=writer)
        return (counts["similar"] + counts["link"]) > 0
    except Exception as e:  # noqa: BLE001 — DEVONthink boundary op is best-effort; failure logged via log.warning
        _log.warning("dt_link_failed", uuid=uuid, error=str(e))
        return False
    finally:
        writer.close()
        if reader is not None:
            reader.close()


def _writeback_record(uuid: str) -> bool:
    """Stamp the nexus identity back onto a just-indexed DT record (Layer F).

    Resolves the record's tumbler via ``Catalog.by_source_uri`` (the entry was
    just stamped with ``x-devonthink-item://<uuid>``) and calls
    :func:`nexus.dt_writeback.writeback_record`. Returns ``True`` when at least
    one nexus-owned field was written. Fail-soft: any error or an unresolvable
    tumbler logs and returns ``False`` — write-back never aborts the index batch.

    Aspect-keyword tags (``nx-kw:*``) are supported by ``writeback_record`` but
    not sourced here: RDR-089 aspect extraction is queued AFTER index, so no
    keywords exist at ``nx dt index`` time. Stamping them is deferred to a
    follow-on re-stamp pass (tracked) rather than stamped empty.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import (catalog.factory)
    from nexus.dt_writeback import writeback_record  # noqa: PLC0415 — command-local import (dt_writeback)

    dt_uri = f"x-devonthink-item://{uuid}"
    cat = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
    if cat is None:
        return False
    try:
        entry = cat.by_source_uri(dt_uri)
        if entry is None:
            _log.warning("dt_writeback_no_entry", uuid=uuid)
            return False
        result = writeback_record(uuid, str(entry.tumbler))
        return any(result[k] for k in ("tags", "annotation", "metadata"))
    except Exception as e:  # noqa: BLE001 — DEVONthink boundary op is best-effort; failure logged via log.warning
        _log.warning("dt_writeback_failed", uuid=uuid, error=str(e))
        return False
    finally:
        cat.close()


def _open_highlights_store():
    """document_highlights store routed via the storage facade (nexus-g8r2h).

    The old direct ``DocumentHighlights(default_db_path())`` construction
    was a split-brain on migrated boxes: writes landed in local SQLite where
    no service-side consumer reads (write-to-nowhere), and reads missed
    every ETL'd-to-PG row ("no ingested highlights" against real data).
    Mirrors ``plan.py``'s ``_open_plan_library`` routing (RDR-179 pattern);
    the HTTP store has full method parity (upsert/get/get_by_source_uri).
    """
    # Seam COLLAPSED (nexus-i711w Stage 2 sub-stage A):
    # HttpDocumentHighlightsStore is the only highlights store — the SQLite
    # arm died with the store.
    from nexus.db.t2.http_document_highlights_store import HttpDocumentHighlightsStore  # noqa: PLC0415 — command-local import

    return HttpDocumentHighlightsStore()


def _ingest_highlights_record(uuid: str) -> bool:
    """RDR-139 Layer E: ingest a just-indexed record's DEVONthink highlights +
    mentions as a note attached to its catalog tumbler.

    Resolves the tumbler via ``Catalog.by_source_uri`` (the entry was just
    stamped with ``x-devonthink-item://<uuid>``), pulls the markdown blobs via
    :func:`devonthink.dt_extract_highlights` / ``dt_extract_mentions``, and
    upserts a :class:`HighlightRecord` into the dedicated ``document_highlights``
    T2 table. Returns ``True`` only when at least one blob had content AND the
    row was written. Fail-soft: no tumbler / no highlights / any error -> log +
    ``False``; highlight ingest never aborts the index batch.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import (catalog.factory)
    from nexus.db.t2.records import HighlightRecord  # noqa: PLC0415 — command-local import (db.t2.records)
    from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — command-local import (mcp_client.devonthink)

    dt_uri = f"x-devonthink-item://{uuid}"
    cat = make_catalog_reader()  # nexus-kmo9h: None ⇔ sqlite opt-out + uninitialised
    if cat is None:
        return False
    try:
        entry = cat.by_source_uri(dt_uri)
        if entry is None:
            _log.warning("dt_highlights_no_entry", uuid=uuid)
            return False
        highlights_md = _dt.dt_extract_highlights(uuid) or ""
        mentions_md = _dt.dt_extract_mentions(uuid) or ""
        if not (highlights_md or mentions_md):
            _log.debug("dt_highlights_none", uuid=uuid)
            return False
        # One-shot CLI ingest, routed via the storage facade (nexus-g8r2h) —
        # the previous direct DocumentHighlights construction wrote local
        # SQLite even on migrated boxes. Low contention either way: one
        # write per indexed record, not a long-lived worker (RDR-128 N/A).
        # Reviewer High fold: close per use (try/finally, like every
        # _open_plan_library caller) — this runs inside the dt-index batch
        # loop, and an unclosed HttpDocumentHighlightsStore leaks one httpx
        # connection pool PER RECORD. Construction itself is cheap (local
        # env/lease resolution, no network), so per-call open/close is the
        # minimal leak-free shape.
        store = _open_highlights_store()
        from datetime import datetime, timezone  # noqa: PLC0415 — stdlib deferred to call site (datetime)

        try:
            return store.upsert(HighlightRecord(
                doc_id=str(entry.tumbler),
                source_uri=dt_uri,
                collection=getattr(entry, "physical_collection", "") or "",
                highlights_md=highlights_md,
                mentions_md=mentions_md,
                ingested_at=datetime.now(timezone.utc).isoformat(),
            ))
        finally:
            _close = getattr(store, "close", None)
            if callable(_close):
                _close()
    except Exception as e:  # noqa: BLE001 — DEVONthink boundary op is best-effort; failure logged via log.warning
        _log.warning("dt_highlights_failed", uuid=uuid, error=str(e))
        return False
    finally:
        cat.close()


#: RDR-139 Layer D extraction-source provenance values for DT-sourced text.
#: Only ``dt_content`` (extract_record_content) is routed today; ``dt_ocr``
#: (ocr_record, scanned PDFs/images) and ``dt_transcribe`` (transcribe_record,
#: A/V) are enum-ready but unrouted — deferred to nexus-39b0f, surfaced at
#: Phase 2 close (substantive-critic), not silent scope reduction.
_DT_EXTRACTION_SOURCES: frozenset[str] = frozenset(
    {"dt_content", "dt_ocr", "dt_transcribe"}
)


def _index_dt_content_record(
    uuid: str,
    *,
    collection: str,
    corpus: str,
    extraction_source: str = "dt_content",
) -> bool:
    """RDR-139 Layer D: index a non-file-backed DT record from DT-extracted
    text (rather than an on-disk file).

    Sources the AI-optimised body via :func:`devonthink.dt_extract_content`,
    writes it through the existing Markdown chunking pipeline with every chunk
    stamped ``extraction_source`` (``dt_content`` by default), and stamps the
    DT identity (``x-devonthink-item://<uuid>``) onto the catalog entry so the
    record is addressable even though no real file backs it.

    Fail-soft: empty/unavailable DT text -> ``False`` (the caller skips the
    record), never an exception. Returns ``True`` only when chunks were written
    AND the DT identity was stamped.

    ``ChunkLandingUnverifiedError`` and ``IndexRunVerifyRefused`` (both
    ``NexusError`` subclasses ``index_markdown``'s fence can raise —
    errors.py:196, 234) are deliberately NOT part of the except tuple below
    and are left to propagate to the caller (nexus-hb10j) — mirroring
    ``_index_record``'s "indexer exception is a precondition" contract.
    ``index_cmd``'s ``dt_content_active`` branch catches both at the call
    site and converts them into a failed-record entry, exactly like the
    file-backed branch does.

    The extracted text is cached at a STABLE per-UUID path
    (``<catalog>/.dt-content/<uuid>.md``) rather than a throwaway temp file
    (code-review HIGH-1). A throwaway path breaks re-index idempotency — the
    catalog dedups by ``file_path``, so a fresh random name each run would
    accumulate a duplicate entry per re-index and leave the row's
    ``file_path`` pointing at a deleted file (a ghost path). The stable path
    makes the catalog ``by_file_path`` lookup hit on re-index and keeps the
    ``file_path`` column resolvable; the DT identity (``source_uri``) is still
    the canonical reference.
    """
    import json  # noqa: PLC0415 — stdlib deferred to call site (json)

    from nexus.config import catalog_path  # noqa: PLC0415 — command-local import (config)
    from nexus.doc_indexer import index_markdown  # noqa: PLC0415 — command-local import (doc_indexer)
    from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — command-local import (mcp_client.devonthink)

    if extraction_source not in _DT_EXTRACTION_SOURCES:
        raise ValueError(
            f"extraction_source {extraction_source!r} not a DT source "
            f"{sorted(_DT_EXTRACTION_SOURCES)}"
        )

    text = _dt.dt_extract_content(uuid)
    if not text or not text.strip():
        _log.warning("dt_content_empty", uuid=uuid, extraction_source=extraction_source)
        return False

    name = _dt.dt_record_name(uuid) or uuid
    # JSON-quote the title so a name with a colon / quote can't break the
    # strict frontmatter parse. The body follows verbatim.
    front = f"---\ntitle: {json.dumps(name)}\n---\n\n{text}"

    cache_dir = catalog_path() / ".dt-content"
    cache_path = cache_dir / f"{uuid}.md"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(front, encoding="utf-8")
        count = index_markdown(
            cache_path,
            corpus=corpus,
            collection_name=collection,
            extraction_source=extraction_source,
        )
        if not count:
            # We had non-empty text above, so a 0-chunk return is the
            # index_markdown staleness skip: this record's content is already
            # indexed and unchanged. That is a benign idempotent no-op (the
            # catalog row is not duplicated), not a failure — log at debug.
            _log.debug("dt_content_unchanged", uuid=uuid)
            # nexus-5xn3k.6 AC4: this was debug-only and invisible at normal
            # verbosity — the same silent-skip gap _index_record had. The
            # caller's "skipped" bucket already counts it; this line says WHY.
            click.echo(f"  skipped: index fresh (use --force)  {uuid}")
            return False
        return _stamp_dt_uri_on_entry(cache_path, uuid)
    except (RuntimeError, ImportError, OSError) as exc:
        _log.error(
            "dt_content_index_failed",
            uuid=uuid,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False


@click.group("dt")
def dt() -> None:
    """DEVONthink integration verbs (macOS only).

    Subcommands wrap DEVONthink so DT-side selections (or smart groups,
    tags, groups) flow into Nexus indexing without manual UUID/path
    copying. Requires DEVONthink to be running for selectors that read
    live application state.
    """


@dt.command("index")
@click.option(
    "--selection",
    "use_selection",
    is_flag=True,
    default=False,
    help="Index records currently selected in DEVONthink's UI.",
)
@click.option(
    "--tag",
    default=None,
    help="Index every record carrying this tag (use --database to scope).",
)
@click.option(
    "--group",
    "group_path",
    default=None,
    help="Index every record under this group path (recursive). "
    "Use --database to scope to one library.",
)
@click.option(
    "--smart-group",
    "smart_group",
    default=None,
    help="Execute the named smart group's query and index its results. "
    "Honours the smart group's own scope and exclude-subgroups flag.",
)
@click.option(
    "--uuid",
    "uuids",
    multiple=True,
    default=(),
    help="Index a single record by UUID. Repeat for batch ingest.",
)
@click.option(
    "--database",
    default=None,
    help="Limit selectors to one DEVONthink database. Default: all open libraries.",
)
@click.option(
    "--collection",
    default=None,
    help=(
        "T3 collection override. Wins over the extension-based "
        "default. e.g. ``--collection knowledge__delos``."
    ),
)
@click.option(
    "--corpus",
    default="dt",
    show_default=True,
    help=(
        "Corpus name used to derive the default collection when "
        "--collection is not set. PDFs route to "
        "``knowledge__<corpus>-papers`` (paper-shaped, aspect-eligible "
        "via scholarly-paper-v1); markdown notes route to "
        "``docs__<corpus>``. Pre-nexus-cvaw the default was "
        "``default`` and PDFs landed in ``docs__default`` where "
        "aspect extraction is intentionally disabled (nexus-z70w)."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the records that would be indexed; make no T3 writes.",
)
@click.option(
    "--link-semantic",
    "link_semantic",
    is_flag=True,
    default=False,
    help=(
        "After a record indexes, create 'relates' edges to its DEVONthink "
        "similarity + explicit-link neighbours that are also indexed in nexus "
        "(Layer B): created_by dt_similar / dt_link, deduped, idempotent. "
        "DT unavailable -> zero edges. Opt-in, default off."
    ),
)
@click.option(
    "--writeback",
    is_flag=True,
    default=False,
    help=(
        "After a record indexes, stamp the nexus identity back onto the "
        "DEVONthink record (Layer F): nx-indexed / nx-tumbler:<t> tags "
        "(add-mode, no clobber), a tumbler backlink annotation, and "
        "nxtumbler custom metadata. nexus-owned namespace only; never edits "
        "user content; honours Exclude-from-AI&MCP on a best-effort basis "
        "(records with empty AI-extracted content are skipped). Opt-in, "
        "default off."
    ),
)
@click.option(
    "--enrich",
    "enrich",
    is_flag=True,
    default=False,
    help=(
        "After indexing, run a DT-CrossRef bibliographic gap-fill pass over "
        "each touched collection (RDR-139 Layer C): the 'auto' primary "
        "backend, then DEVONthink's CrossRef resolver fills only still-empty "
        "bib_* fields. Strictly lowest-precedence; never overwrites an "
        "S2/OpenAlex value. DT unavailable -> primary-only. Opt-in, default off."
    ),
)
@click.option(
    "--dt-content",
    "dt_content",
    is_flag=True,
    default=False,
    help=(
        "Index non-file-backed records (web archives, bookmarks, formatted "
        "notes — anything without a .pdf/.md file) from DEVONthink's "
        "AI-extracted text instead of skipping them (RDR-139 Layer D). Every "
        "chunk is stamped extraction_source=dt_content; file-backed records "
        "still index from their file (provenance absent == file). DT "
        "unavailable -> records skipped exactly as today. Opt-in, default off."
    ),
)
@click.option(
    "--highlights",
    "highlights",
    is_flag=True,
    default=False,
    help=(
        "After a record indexes, ingest its DEVONthink highlights + mentions "
        "(extract_record_highlights / extract_record_mentions) as a markdown "
        "note attached to the record's catalog tumbler in the document_highlights "
        "T2 table (RDR-139 Layer E). DT unavailable or no highlights -> nothing "
        "ingested. Opt-in, default off."
    ),
)
@click.option(
    "--extractor",
    type=click.Choice(["auto", "docling", "mineru"], case_sensitive=False),
    default="auto",
    show_default=True,
    help=(
        "PDF extraction backend for file-backed records. ``mineru`` is "
        "formula-aware but OOM-fails on some formula-dense pages; the recovery "
        "is ``--extractor docling`` (formula-stripped, but always completes). "
        "``auto`` picks mineru when formulas are detected, else docling."
    ),
)
def index_cmd(
    use_selection: bool,
    tag: str | None,
    group_path: str | None,
    smart_group: str | None,
    uuids: tuple[str, ...],
    database: str | None,
    collection: str | None,
    corpus: str,
    dry_run: bool,
    link_semantic: bool,
    writeback: bool,
    enrich: bool,
    dt_content: bool,
    highlights: bool,
    extractor: str,
) -> None:
    """Index DEVONthink records into Nexus.

    Exactly one selector flag must be provided: ``--selection``,
    ``--tag``, ``--group``, ``--smart-group``, or one or more ``--uuid``.
    """
    selectors_used = sum([
        use_selection,
        tag is not None,
        group_path is not None,
        smart_group is not None,
        bool(uuids),
    ])
    if selectors_used == 0:
        raise click.UsageError(
            "Provide exactly one selector: --selection, --tag, --group, "
            "--smart-group, or --uuid (one or more).",
        )
    if selectors_used > 1:
        raise click.UsageError(
            "Selectors are mutually exclusive: pick one of --selection, "
            "--tag, --group, --smart-group, or --uuid.",
        )

    try:
        records = _gather_records(
            use_selection=use_selection,
            tag=tag,
            group_path=group_path,
            smart_group=smart_group,
            uuids=uuids,
            database=database,
        )
    except DTNotAvailableError as e:
        raise click.ClickException(str(e)) from e

    if not records:
        click.echo("No records found.")
        return

    if dry_run:
        click.echo(f"Would index {len(records)} record(s):")
        for uuid, path in records:
            click.echo(f"  {uuid}\t{path}")
        return

    indexed = 0
    unchanged = 0
    skipped = 0
    stamp_failed = 0
    written_back = 0
    linked = 0
    content_extracted = 0
    highlighted = 0
    touched_collections: set[str] = set()
    failed: list[tuple[str, str, str]] = []  # (uuid, path, error)

    # nexus-5xn3k.6 (RUNFENCE C4, bead scope note) + nexus-tp8yk D2b: zero
    # the completion-refusal / manifest-write-failure / identity-drop
    # collectors (index_repo_cmd's parity — full nx index reset() triple),
    # so this run's exit-code check below reflects only THIS run's
    # problems, not leftover state from an earlier call in the same
    # process. nexus-7f5qj: delegates to the shared commands._helpers
    # reset helper (this was the first of what became four near-identical
    # copies; see that module for the extraction rationale).
    from nexus.commands._helpers import reset_identity_drop_collectors  # noqa: PLC0415 — deliberate function-local import (per-run failure collector reset)
    reset_identity_drop_collectors()
    # nexus-5xn3k.6 substantive-critic CRITICAL (nexus-qo84l): must be bound
    # before the per-record try/except below reaches its `except
    # IndexRunVerifyRefused` clause.
    #
    # nexus-tp8yk substantive-critic CRITICAL (nexus-9800y, 2026-08-04): same
    # bind-before-use requirement for the new `except ChunkLandingUnverifiedError`
    # clause below.
    from nexus.errors import (  # noqa: PLC0415 — deferred: rare-branch exception type, matches file convention
        ChunkLandingUnverifiedError,
        IndexRunVerifyRefused,
        PER_RECORD_SURVIVABLE_EXCEPTIONS,
    )

    # RDR-139 Layer D: only probe DT availability once, and only when the
    # opt-in flag is set. Flag off -> the unsupported-extension skip path is
    # byte-identical to today (Gap 0).
    dt_content_active = False
    if dt_content:
        from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — command-local import (mcp_client.devonthink)

        dt_content_active = _dt.available()

    for uuid, path in records:
        ext = Path(path).suffix.lower()
        if ext not in _SUPPORTED_EXTS:
            # RDR-139 Layer D: non-file-backed record. With --dt-content and a
            # reachable DT, index it from DT-extracted text; otherwise skip
            # exactly as before.
            if dt_content_active:
                dt_collection = _resolve_dt_collection(collection, corpus, ext)
                try:
                    content_indexed = _index_dt_content_record(
                        uuid, collection=dt_collection, corpus=corpus,
                    )
                except PER_RECORD_SURVIVABLE_EXCEPTIONS as exc:
                    # nexus-hb10j (substantive-critic, 2xu6t adjudication,
                    # T2 [21480], 2026-08-05): mirrors the file-backed
                    # _index_record catch below. Both members of
                    # PER_RECORD_SURVIVABLE_EXCEPTIONS are NexusError, not
                    # (ImportError, RuntimeError, OSError) —
                    # _index_dt_content_record's own except tuple around
                    # index_markdown() never matches them, so they
                    # propagate here unchanged. Pre-fix this escaped the
                    # loop entirely and aborted the WHOLE --dt-content
                    # batch on the first affected record — third
                    # occurrence of the nexus-2fyb/qo84l/9800y regression
                    # class, this time for the non-file-backed ingest
                    # path. Convert to a failed-record entry with the SAME
                    # wording the file-backed branch renders.
                    #
                    # nexus-rlkgu: catches the shared tuple (one except
                    # clause) instead of one except clause per exception
                    # type, so a NEW per-record-raisable NexusError
                    # subclass needs no edit here — only an addition to
                    # PER_RECORD_SURVIVABLE_EXCEPTIONS in errors.py.
                    # isinstance dispatch below preserves the two KNOWN
                    # exceptions' differing structured-log fields; pure
                    # refactor, behavior unchanged for them.
                    #
                    # nexus-cy4oy (substantive-critic CRITICAL, round-2
                    # review of nexus-rlkgu): the dispatch is now TOTAL —
                    # an explicit isinstance branch per known type, plus a
                    # generic final `else` for any OTHER
                    # PER_RECORD_SURVIVABLE_EXCEPTIONS member. The
                    # original binary if/else assumed its else branch was
                    # always ChunkLandingUnverifiedError-shaped (accessed
                    # .collection/.count unconditionally); a hypothetical
                    # THIRD tuple member without that shape would have hit
                    # AttributeError INSIDE this handler and escaped the
                    # try/except, re-aborting the batch — occurrence-4 of
                    # the nexus-2fyb/qo84l/9800y/hb10j class, reproduced
                    # with both AST gates green (they inspect except-
                    # clause TYPES, not handler bodies). The generic
                    # branch below makes ANY tuple member safe: type name
                    # + str(exc), no attribute assumptions. Handler-body
                    # coverage (which the AST gates structurally cannot
                    # give) is proven by TestAllTupleMembersSurviveThe
                    # RealPerRecordPath and TestGenericFallbackHandles
                    # UnknownTupleMember in tests/test_commands_dt.py.
                    if isinstance(exc, IndexRunVerifyRefused):
                        from nexus.commands.index import _index_run_refused_message  # noqa: PLC0415 — deferred: avoids a module-load-time cross-import between commands/dt.py and commands/index.py
                        _log.error(
                            "dt_content_index_completion_refused",
                            uuid=uuid,
                            doc_id=exc.doc_id,
                            referenced=exc.referenced,
                            present=exc.present,
                            missing=exc.missing,
                        )
                        failed.append((uuid, path, _index_run_refused_message(exc, target_collection=dt_collection)))
                    elif isinstance(exc, ChunkLandingUnverifiedError):
                        _log.error(
                            "dt_content_chunk_landing_unverified",
                            uuid=uuid,
                            collection=exc.collection,
                            count=exc.count,
                        )
                        failed.append((uuid, path, str(exc)))
                    else:
                        # Generic fallback: a PER_RECORD_SURVIVABLE_
                        # EXCEPTIONS member this dispatch does not know
                        # about yet. No attribute assumptions — type name
                        # + str(exc) only.
                        _log.error(
                            "dt_content_index_unhandled_survivable_exception",
                            uuid=uuid,
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                        failed.append((uuid, path, f"{type(exc).__name__}: {exc}"))
                    continue
                if content_indexed:
                    content_extracted += 1
                    indexed += 1
                    touched_collections.add(dt_collection)
                    if link_semantic and _link_semantic_record(uuid):
                        linked += 1
                    if writeback and _writeback_record(uuid):
                        written_back += 1
                    if highlights and _ingest_highlights_record(uuid):
                        highlighted += 1
                else:
                    skipped += 1
                continue
            _log.warning(
                "dt_skip_unsupported_extension",
                uuid=uuid,
                path=path,
                ext=ext,
            )
            skipped += 1
            continue
        resolved_collection = _resolve_dt_collection(collection, corpus, ext)
        try:
            stamped, chunks = _index_record(
                uuid,
                path,
                collection=resolved_collection,
                corpus=corpus,
                dry_run=False,
                extractor=extractor,
            )
        except PER_RECORD_SURVIVABLE_EXCEPTIONS as exc:
            # nexus-5xn3k.6 substantive-critic CRITICAL (nexus-qo84l,
            # 2026-08-02) + nexus-tp8yk D1 / substantive-critic CRITICAL
            # (nexus-9800y, 2026-08-04): neither member of
            # PER_RECORD_SURVIVABLE_EXCEPTIONS is (ImportError, RuntimeError)
            # — both are NexusError. Any DT record large enough to hit the
            # streaming or incremental path (index_pdf -> pipeline_index_pdf
            # / _index_pdf_incremental -> _fence_complete) can propagate
            # IndexRunVerifyRefused by contract (the fail-loud completion
            # verify); index_pdf/index_markdown raise ChunkLandingUnverifiedError
            # from _upsert_skip_reembed when a stale-positive existing_ids
            # probe meets an engine response that omits "missing" (cannot
            # tell whether the batch landed) — BEFORE any manifest row is
            # committed. Pre-fix either fell through the narrow except tuple
            # entirely, escaping the loop and aborting the WHOLE `nx dt
            # index` batch on the first affected record — the nexus-2fyb/
            # nexus-qo84l/nexus-9800y regression class. Convert to a
            # failed-record entry so record N+1 still indexes.
            #
            # nexus-rlkgu: catches the shared tuple (one except clause)
            # instead of one except clause per exception type, so a NEW
            # per-record-raisable NexusError subclass needs no edit here —
            # only an addition to PER_RECORD_SURVIVABLE_EXCEPTIONS in
            # errors.py. isinstance dispatch below preserves the two KNOWN
            # exceptions' differing structured-log fields and message
            # wording (commands/index.py's _index_run_refused_message for
            # the refusal, str(exc) for the landing-unverified case) —
            # pure refactor, behavior unchanged for them.
            #
            # nexus-cy4oy (substantive-critic CRITICAL, round-2 review of
            # nexus-rlkgu): TOTAL dispatch — an explicit isinstance branch
            # per known type, plus a generic final `else` for any OTHER
            # PER_RECORD_SURVIVABLE_EXCEPTIONS member. Identical rationale
            # to the --dt-content branch's twin fix above: the old binary
            # if/else assumed its else branch was always
            # ChunkLandingUnverifiedError-shaped, so a hypothetical THIRD
            # tuple member without that shape would raise AttributeError
            # here and escape the try/except, re-aborting the batch —
            # exactly occurrence-4 of this bug class, invisible to both
            # AST gates (they inspect except-clause TYPES, not handler
            # bodies). See TestAllTupleMembersSurviveTheRealPerRecordPath
            # and TestGenericFallbackHandlesUnknownTupleMember in
            # tests/test_commands_dt.py for the handler-body coverage.
            if isinstance(exc, IndexRunVerifyRefused):
                from nexus.commands.index import _index_run_refused_message  # noqa: PLC0415 — deferred: avoids a module-load-time cross-import between commands/dt.py and commands/index.py
                _log.error(
                    "dt_index_completion_refused",
                    uuid=uuid,
                    path=path,
                    doc_id=exc.doc_id,
                    referenced=exc.referenced,
                    present=exc.present,
                    missing=exc.missing,
                )
                failed.append((uuid, path, _index_run_refused_message(exc, target_collection=resolved_collection)))
            elif isinstance(exc, ChunkLandingUnverifiedError):
                _log.error(
                    "dt_index_chunk_landing_unverified",
                    uuid=uuid,
                    path=path,
                    collection=exc.collection,
                    count=exc.count,
                )
                failed.append((uuid, path, str(exc)))
            else:
                # Generic fallback: a PER_RECORD_SURVIVABLE_EXCEPTIONS
                # member this dispatch does not know about yet. No
                # attribute assumptions — type name + str(exc) only.
                _log.error(
                    "dt_index_unhandled_survivable_exception",
                    uuid=uuid,
                    path=path,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                failed.append((uuid, path, f"{type(exc).__name__}: {exc}"))
            continue
        except (RuntimeError, ImportError) as exc:
            # nexus-2fyb code-review R4-I2: a single indexing failure must
            # NOT kill the whole DT batch. Pre-fix, formula PDFs silently
            # produced 0-chunk "successes" and the batch always completed;
            # post-fix, the loud-raise contract turned that into a strict
            # regression where one math PDF aborted the entire smart-group
            # run and left every subsequent record unprocessed. Catch
            # RuntimeError (extraction failures) and ImportError (corrupt
            # MinerU install) per-record, log, and continue.
            _log.error(
                "dt_index_failed",
                uuid=uuid,
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            failed.append((uuid, path, f"{type(exc).__name__}: {exc}"))
            continue
        # nexus-5xn3k.6 AC4: bucket on the indexer's OWN chunk count, not on
        # unconditional success — a chunks==0 return is the staleness gate's
        # skip (or a genuinely empty document), never a write, so it must
        # never inflate "Indexed N record(s)."
        if chunks:
            indexed += 1
            touched_collections.add(resolved_collection)
        else:
            unchanged += 1
            click.echo(f"  skipped: index fresh (use --force)  {uuid}\t{path}")
        if not stamped:
            stamp_failed += 1
            continue
        if link_semantic and _link_semantic_record(uuid):
            linked += 1
        if writeback and _writeback_record(uuid):
            written_back += 1
        if highlights and _ingest_highlights_record(uuid):
            highlighted += 1

    # RDR-139 Layer C: gap-fill bibliographic metadata over the collections we
    # just wrote to. Runs once per distinct collection (title-group oriented),
    # after all records land so a multi-record paper enriches as one group.
    if enrich and touched_collections:
        from nexus.commands.enrich import run_bib_enrichment  # noqa: PLC0415 — deferred to avoid circular import (commands.enrich)

        for coll in sorted(touched_collections):
            click.echo(f"\nEnriching bibliographic metadata: {coll}")
            run_bib_enrichment(coll, source="dt")

    summary = f"Indexed {indexed} record(s) ({skipped} skipped"
    if unchanged:
        summary += f", {unchanged} unchanged"
    if content_extracted:
        summary += f", {content_extracted} from DT content"
    if link_semantic:
        summary += f", {linked} semantically linked"
    if writeback:
        summary += f", {written_back} written back to DT"
    if highlights:
        summary += f", {highlighted} highlights ingested"
    if failed:
        summary += f", {len(failed)} failed"
    if stamp_failed:
        # Stamp failure leaves the entry recoverable via
        # 'nx catalog update --source-uri x-devonthink-item://<UUID>'
        # — flag it so the operator knows the round-trip is broken
        # for those records.
        summary += f", {stamp_failed} DT-URI stamp-failed"
    summary += ")."
    click.echo(summary)
    if failed:
        click.echo("\nFailures:")
        for uuid, path, err in failed:
            click.echo(f"  {uuid}\t{Path(path).name}: {err}")
    if stamp_failed:
        click.echo(
            "Some records were indexed but their catalog entry still "
            "carries source_uri=file://… instead of x-devonthink-item://"
            "<UUID>. Inspect ~/Library/Logs (or your structlog sink) "
            "for 'dt_stamp_failed' events and recover with "
            "'nx catalog update <tumbler> --source-uri x-devonthink-item://<UUID>'.",
        )

    # nexus-5xn3k.6 (RUNFENCE C4, bead scope note 2026-08-02 16:34) +
    # nexus-tp8yk D2b: a completion stamp REFUSED by the engine's
    # fail-closed verify, a manifest write failure, or an identity drop
    # used to leave rc=0 (WARNING-only, or silently folded into a clean
    # "Indexed N record(s)." summary) — reproducing, one layer up, the
    # exact silent-success shape the fence exists to close. nexus-7f5qj:
    # delegates to the shared commands._helpers collector-check (see that
    # module for the per-collector rationale this docstring used to carry
    # inline, including WHY the write-failure line says "document(s)" and
    # not "record(s)" despite this command's own convention elsewhere).
    #
    # substantive-critic SIGNIFICANT (2026-08-02, T2
    # nexus/5xn3k6-critique-2026-08-02 [21355]): a refused record still has
    # a non-zero chunk count (over-work-never-under-work — the rows
    # genuinely landed), so it was ALREADY counted in the "Indexed N
    # record(s)" headline above — deliberately NOT restructured, the
    # chunks are real. dt.py cannot correlate a specific uuid to the
    # doc_id the engine refused (mcp_infra's collector is process-global,
    # keyed on doc_id, not per-record uuid), so this states the overlap as
    # a count relationship rather than naming which of the indexed records
    # it is.
    from nexus.commands._helpers import (  # noqa: PLC0415 — deliberate function-local import (rare branch: only on refusal/failure/drop)
        emit_identity_drop_summary,
        raise_identity_drop_exception,
    )
    if emit_identity_drop_summary(
        indexed_count=indexed,
        # nexus-7f5qj code-review follow-up (T2 [21484]): preserve this
        # command's ORIGINAL print order exactly (refused, then
        # write-failed, then identity-drops) — the extraction's default
        # order matches index_repo_cmd's instead. No test pinned the
        # order for either caller, but matching it keeps the "behavior-
        # preserving refactor" claim exact. nexus-39upx hazard 4:
        # appended at the end so this surface also gets sweep
        # visibility — an explicit order that predates a check must not
        # silently opt that surface out of it.
        order=(
            "refused", "write_failed", "identity_drops",
            "superseded_swept", "superseded_sweep_skipped",
        ),
    ):
        # nexus-tp8yk D2b: mirrors commands/index.py's index_repo_cmd — a
        # completion refusal, a manifest write failure, or an identity
        # drop used to leave rc=0. Refusal != unconfirmed: a pre-fence
        # engine's None sentinel still warns at rc 0, untouched by this
        # bead — only a POSITIVE engine verdict or a write/identity
        # failure fails the run.
        raise_identity_drop_exception(subject="record")


def _gather_records(
    *,
    use_selection: bool,
    tag: str | None,
    group_path: str | None,
    smart_group: str | None,
    uuids: tuple[str, ...],
    database: str | None,
) -> list[tuple[str, str]]:
    """Resolve the chosen selector to ``[(uuid, path), ...]``.

    Mutual exclusion is enforced upstream — exactly one branch fires.
    Selectors are accessed via the :mod:`nexus.devonthink` module
    (rather than ``from nexus.devonthink import _dt_selection``) so
    tests can monkeypatch the module attributes.
    """
    if use_selection:
        return dt_mod._dt_selection()
    if tag is not None:
        return dt_mod._dt_tag_records(tag, database=database)
    if group_path is not None:
        return dt_mod._dt_group_records(group_path, database=database)
    if smart_group is not None:
        return dt_mod._dt_smart_group_records(smart_group, database=database)
    # uuids — one resolver call per UUID, results merged.
    out: list[tuple[str, str]] = []
    for u in uuids:
        out.extend(dt_mod._dt_uuid_record(u))
    return out


# ── nx dt open ───────────────────────────────────────────────────────────────


# DT records use canonical 8-4-4-4-12 hex UUIDs; tumblers are
# dot-separated decimal numbers (e.g. ``1.2.3``). The two shapes are
# disjoint — UUIDs have hyphens, tumblers have dots — so a single regex
# pair classifies the argument unambiguously.
_UUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
)
_TUMBLER_RE = re.compile(r"^\d+(\.\d+)+$")


def _select_dt_uri_from_entry(entry: object) -> str | None:
    """Pick the ``x-devonthink-item://`` URI off a catalog entry.

    Pure function over an entry-shaped object (anything exposing
    ``meta`` and ``source_uri``). Resolution order mirrors the
    substrate at ``catalog._resolve_via_devonthink``:

    1. ``meta.devonthink_uri`` if it starts with ``x-devonthink-item://``
       (the canonical reverse-lookup recorded on entries that came in
       via DEVONthink, e.g. anything indexed via ``nx dt index``).
    2. ``source_uri`` if it starts with ``x-devonthink-item://``
       (entries registered with a DT identity from the start).
    3. ``None`` otherwise — caller decides how to surface this.

    Extracted from :func:`_resolve_dt_uri_from_tumbler` so the
    selection rule is unit-testable without standing up a Catalog
    fixture.
    """
    meta = getattr(entry, "meta", {}) or {}
    if isinstance(meta, dict):
        dt_uri = meta.get("devonthink_uri", "")
        if isinstance(dt_uri, str) and dt_uri.startswith(
            "x-devonthink-item://",
        ):
            return dt_uri
    source_uri = getattr(entry, "source_uri", "")
    if isinstance(source_uri, str) and source_uri.startswith(
        "x-devonthink-item://",
    ):
        return source_uri
    return None


def _resolve_dt_uri_from_tumbler(tumbler: str) -> str | None:
    """Return the ``x-devonthink-item://`` URI for a tumbler, or
    ``None`` when the entry exists but carries no DT URI.

    Catalog plumbing only — the URI-selection rule lives in
    :func:`_select_dt_uri_from_entry`.

    Raises:
        click.ClickException: when the tumbler doesn't resolve to any
            catalog entry (caller surfaces this as a non-zero exit).
    """
    from nexus.catalog import resolve_tumbler  # noqa: PLC0415 — command-local import (catalog)
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — command-local import (catalog.factory)

    # nexus-kmo9h: factory delegation — the old local gate raised a false
    # "run 'nx catalog setup'" on healthy service-mode boxes.
    cat = make_catalog_reader()
    if cat is None:
        raise click.ClickException(
            "Catalog not initialized. Run 'nx catalog setup' first.",
        )
    try:
        t, err = resolve_tumbler(cat, tumbler)
        if err:
            raise click.ClickException(f"tumbler not found: {tumbler}")
        entry = cat.resolve(t)
        if entry is None:
            raise click.ClickException(f"tumbler not found: {tumbler}")
        return _select_dt_uri_from_entry(entry)
    finally:
        # CatalogDB owns the SQLite connection + WAL lock; close it
        # explicitly so back-to-back CliRunner invocations (and any
        # future in-process callers) don't leak the write lock until
        # GC. Existing nx catalog commands rely on process-exit cleanup
        # which is fine for one-shot CLI but not for in-process reuse.
        cat.close()


@dt.command("open")
@click.argument("tumbler_or_uuid")
def open_cmd(tumbler_or_uuid: str) -> None:
    """Open a record in DEVONthink by tumbler or UUID.

    A UUID-shaped argument (``8-4-4-4-12`` hex) is converted directly
    to ``x-devonthink-item://<UUID>`` — no catalog hit, no osascript.
    A tumbler (e.g. ``1.2.3``) is resolved through the catalog,
    preferring ``meta.devonthink_uri`` and falling back to
    ``source_uri`` when the entry was registered with a DT identity.
    """
    # Platform gate fires before any branch-specific work so non-darwin
    # users get the documented "macOS-only" message regardless of
    # argument shape. Previously the tumbler branch would open the
    # catalog and resolve the tumbler before checking platform, leaking
    # catalog errors (uninitialized, not-found) ahead of the real
    # diagnostic.
    if not _is_darwin():
        raise click.ClickException(
            "DEVONthink integration is macOS-only",
        )

    if _UUID_RE.match(tumbler_or_uuid):
        uri = f"x-devonthink-item://{tumbler_or_uuid}"
    elif _TUMBLER_RE.match(tumbler_or_uuid):
        uri = _resolve_dt_uri_from_tumbler(tumbler_or_uuid)
        if uri is None:
            raise click.ClickException(
                f"no DEVONthink URI for tumbler {tumbler_or_uuid}",
            )
    else:
        raise click.ClickException(
            "argument is neither a tumbler (e.g. 1.2.3) nor a UUID "
            "(e.g. 8EDC855D-213F-40AD-A9CF-9543CC76476B).",
        )

    subprocess.run(["open", uri], check=True)  # noqa: S603,S607


@dt.command("incorporate")
@click.argument("uuid")
def incorporate_cmd(uuid: str) -> None:
    """Incorporate an already-indexed DT record into the nexus graph.

    Layer B + F composite (nexus-goypg: relocated verbatim from the retired
    nx-mcp-devonthink proxy's ``dt_incorporate`` tool — the one capability
    the proxy had that DEVONthink's own MCP server cannot provide): resolves
    the record's tumbler (it must already be indexed — run ``nx dt index``
    or capture first), generates DT-derived ``relates`` edges to its
    similarity + explicit-link neighbours that are also indexed in nexus
    (Layer B), and stamps the nexus identity back onto the DT record
    (Layer F: nx-indexed / nx-tumbler tags + tumbler backlink annotation).
    """
    if not _is_darwin():
        raise click.ClickException("DEVONthink integration is macOS-only")
    if not _UUID_RE.match(uuid):
        raise click.ClickException(
            "argument must be a DEVONthink record UUID "
            "(e.g. 8EDC855D-213F-40AD-A9CF-9543CC76476B)."
        )

    from nexus.catalog.dt_link_generator import generate_dt_links  # noqa: PLC0415 — command-local import (nexus.catalog.dt_link_generator)
    from nexus.catalog.factory import (  # noqa: PLC0415 — command-local import (nexus.catalog.factory)
        make_catalog_reader,
        make_catalog_writer,
    )
    from nexus.dt_writeback import writeback_record  # noqa: PLC0415 — command-local import (nexus.dt_writeback)

    cat = None
    writer = None
    try:
        # nexus-kmo9h: factory delegation — None ⇔ sqlite opt-out + uninitialised.
        cat = make_catalog_reader()
        if cat is None:
            raise click.ClickException("nexus catalog is not initialized")
        entry = cat.by_source_uri(f"x-devonthink-item://{uuid}")
        if entry is None:
            raise click.ClickException(
                "record is not indexed in nexus; run "
                "`nx dt index --uuid <uuid>` (or capture) first"
            )
        tumbler = entry.tumbler
        # RDR-146 P1.2: generate_dt_links reads via the reader, writes
        # (link_if_absent) via the write-only daemon proxy. Foreground,
        # user-initiated: interactive priority (the #1046 starvation).
        writer = make_catalog_writer(priority="interactive")
        links = generate_dt_links(cat, tumbler, uuid, writer=writer)
        writeback = writeback_record(uuid, str(tumbler))
        click.echo(f"tumbler:   {tumbler}")
        click.echo(f"links:     {links}")
        click.echo(f"writeback: {writeback}")
    finally:
        if writer is not None:
            writer.close()
        if cat is not None:
            cat.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe


@dt.command("highlights")
@click.argument("tumbler_or_uuid")
def highlights_cmd(tumbler_or_uuid: str) -> None:
    """Show the DEVONthink highlights + mentions ingested for a record (Layer E).

    Accepts a tumbler (``1.2.3``) or a DEVONthink UUID. Reads the
    ``document_highlights`` T2 table populated by ``nx dt index --highlights``.
    This is a pure T2 read — DEVONthink need not be running.
    """
    if not (_UUID_RE.match(tumbler_or_uuid) or _TUMBLER_RE.match(tumbler_or_uuid)):
        raise click.ClickException(
            "argument is neither a tumbler (e.g. 1.2.3) nor a UUID.",
        )
    store = _open_highlights_store()
    try:
        if _UUID_RE.match(tumbler_or_uuid):
            rec = store.get_by_source_uri(f"x-devonthink-item://{tumbler_or_uuid}")
        else:
            rec = store.get(tumbler_or_uuid)
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — reviewer Medium (nexus-g8r2h): a connection-class failure must read as "service unavailable", never a raw traceback indistinguishable from "no highlights"
        raise click.ClickException(
            f"highlights store unavailable ({type(exc).__name__}: {exc}) — "
            "check `nx doctor` / service status. This is NOT 'no highlights "
            "ingested'."
        ) from exc
    finally:
        _close = getattr(store, "close", None)
        if callable(_close):
            _close()
    if rec is None:
        raise click.ClickException(
            f"no ingested highlights for {tumbler_or_uuid} "
            "(run 'nx dt index --highlights' first).",
        )
    click.echo(f"# Highlights for tumbler {rec.doc_id}")
    click.echo(f"source: {rec.source_uri}  (ingested {rec.ingested_at})")
    if rec.highlights_md:
        click.echo("\n" + rec.highlights_md)
    if rec.mentions_md:
        click.echo("\n## Mentions\n" + rec.mentions_md)


@dt.command("capture")
@click.argument("url", required=False)
@click.option("--doi", default=None, help="Capture by DOI: download the open-access PDF (Unpaywall).")
@click.option("--file", "file_path", default=None, help="Capture a loose file from this POSIX path.")
@click.option(
    "--type",
    "capture_type",
    type=click.Choice(["html", "webarchive", "markdown", "pdf"], case_sensitive=False),
    default="webarchive",
    show_default=True,
    help="Web-capture format (URL captures only). pdf and markdown index from "
         "the on-disk file DT creates; html and webarchive are non-file-backed.",
)
@click.option(
    "--contact-email",
    default=None,
    help="Caller email for Unpaywall PDF discovery on --doi (else $OPENALEX_MAILTO).",
)
@click.option("--collection", default=None, help="T3 collection override for the index step.")
@click.option("--corpus", default="dt", show_default=True, help="Corpus tag for the index step.")
@click.option("--link-semantic", "link_semantic", is_flag=True, default=False,
              help="After indexing, create Layer B 'relates' edges (see nx dt index).")
@click.option("--writeback", is_flag=True, default=False,
              help="After indexing, stamp nexus identity back onto the DT record (Layer F).")
@click.option("--highlights", "highlights", is_flag=True, default=False,
              help="After indexing, ingest the record's highlights (Layer E).")
@click.option("--enrich", "enrich", is_flag=True, default=False,
              help="After indexing, run DT-CrossRef bib gap-fill over the collection (Layer C).")
@click.option("--extractor",
              type=click.Choice(["auto", "docling", "mineru"], case_sensitive=False),
              default="auto", show_default=True,
              help="PDF extraction backend for the index step (docling = formula-stripped "
                   "recovery when mineru OOM-fails on formula-dense PDFs).")
@click.pass_context
def capture_cmd(
    ctx: click.Context,
    url: str | None,
    doi: str | None,
    file_path: str | None,
    capture_type: str,
    contact_email: str | None,
    collection: str | None,
    corpus: str,
    link_semantic: bool,
    writeback: bool,
    highlights: bool,
    enrich: bool,
    extractor: str,
) -> None:
    """Capture a URL, DOI, or file into DEVONthink and index it (RDR-139 Layer G).

    Provide exactly one source: a URL argument, ``--doi``, or ``--file``. The
    captured record is then indexed (and optionally linked / written-back /
    highlight-ingested) end to end.

    This is the ONE DT-bound verb: unlike ``nx dt index`` / ``--enrich`` (which
    degrade silently when DEVONthink is absent), ``nx dt capture`` reports
    DT-required and exits NON-ZERO, because capture is impossible without DT.
    """
    if not _is_darwin():
        raise click.ClickException("DEVONthink integration is macOS-only")

    # Count by truthiness so an empty --doi "" / --file "" is "no source" with
    # a clear message, not a confusing blank-target failure downstream.
    sources = [bool(url), bool(doi), bool(file_path)]
    if sum(sources) != 1:
        raise click.UsageError(
            "Provide exactly one capture source: a URL argument, --doi, or --file.",
        )

    from nexus.mcp_client import devonthink as _dt  # noqa: PLC0415 — command-local import (mcp_client.devonthink)

    if not _dt.available():
        # Gap-0 NON-OPTIONAL exception: capture cannot proceed without DT, so it
        # fails loud (non-zero) rather than silently doing nothing.
        raise click.ClickException(
            "nx dt capture requires DEVONthink to be running — this verb is "
            "DT-bound by design (unlike nx dt index, which degrades silently).",
        )

    if url:
        # pdf AND markdown captures are stored by DT as on-disk files (.pdf /
        # .md) — they index from the file (better fidelity than AI re-extract).
        # html / webarchive captures are non-file-backed -> Layer D dt_content.
        file_backed = capture_type.lower() in {"pdf", "markdown"}
        uuid = _dt.dt_capture_web_page(url, capture_type=capture_type)
        what = url
    elif doi:
        import os as _os  # noqa: PLC0415 — stdlib deferred to call site (os)

        email = contact_email or _os.environ.get("OPENALEX_MAILTO", "")
        if not email:
            click.echo(
                "Warning: no contact email (--contact-email / $OPENALEX_MAILTO); "
                "Unpaywall open-access PDF discovery is disabled, CrossRef "
                "metadata only.",
                err=True,
            )
        uuid = _dt.dt_download_pdf_from_doi(doi, contact_email=email)
        file_backed = True
        what = f"doi:{doi}"
    else:
        uuid = _dt.dt_import_file(file_path or "")
        file_backed = True
        what = file_path or ""

    if not uuid:
        hint = " (no open-access PDF found for this DOI)" if doi else ""
        raise click.ClickException(f"capture failed for {what}{hint} — no record created.")

    click.echo(f"Captured {what} -> DEVONthink record {uuid}")
    # Reuse the full index path. file_backed (pdf/markdown/doi/file) indexes
    # from the on-disk file; non-file-backed (html/webarchive) routes through
    # Layer D's --dt-content (CA6 finding).
    try:
        ctx.invoke(
            index_cmd,
            uuids=(uuid,),
            collection=collection,
            corpus=corpus,
            dt_content=not file_backed,
            link_semantic=link_semantic,
            writeback=writeback,
            highlights=highlights,
            enrich=enrich,
            extractor=extractor,
        )
    except click.ClickException:
        # Capture succeeded but indexing failed: the DT record exists but is
        # un-indexed (no catalog entry, invisible to nx dt open / de-index).
        # Surface the recovery path before propagating.
        click.echo(
            f"Note: DEVONthink record {uuid} was captured but indexing failed. "
            f"Recover with: nx dt index --uuid {uuid}",
            err=True,
        )
        raise


# ── DT-side AppleScript installer (nexus-tv5u) ────────────────────────────────


# Manifest mapping each shipped .applescript file to the DT subdirs it
# installs into. The actual files travel as wheel package data via
# ``[tool.hatch.build.targets.wheel.force-include]`` ("dt/scripts" ->
# "nexus/_resources/dt-scripts"); editable installs resolve the same
# path through the ``src/nexus/_resources/dt-scripts`` symlink. Adding
# a new script: drop it into ``dt/scripts/`` and add a manifest entry.
_DT_SCRIPT_MANIFEST: dict[str, tuple[str, ...]] = {
    "Index Selection in nx.applescript": ("Toolbar", "Menu"),
    "Index Selection in nx (Knowledge).applescript": ("Menu",),
    "Index Current Group in nx.applescript": ("Toolbar", "Menu"),
}

# DT4's bundle identifier. DT3 lives under ``com.devon-technologies.think3``;
# we deliberately target DT4 only because that's where the ``nx dt`` CLI
# was developed and exercised.
_DT_APP_SCRIPTS_SUBDIR = "com.devon-technologies.think"


def _default_app_scripts_dir() -> Path:
    """Default ``--app-scripts-dir`` location for installed scripts.

    DT4 watches subdirectories of ``~/Library/Application Scripts/
    com.devon-technologies.think/`` (Toolbar, Menu, Contextual Menu,
    Smart Rules, Reminders). The user must restart DT for a freshly-
    installed Toolbar script to be draggable in "View > Customize
    Toolbar…"; Menu items are picked up on next menu open.
    """
    return Path.home() / "Library" / "Application Scripts" / _DT_APP_SCRIPTS_SUBDIR


def _resolve_dt_script_source_dir() -> Path:
    """Resolve the package-data directory containing the shipped
    ``.applescript`` source files.

    Editable installs see ``src/nexus/_resources/dt-scripts`` (symlink
    to ``dt/scripts``). Wheel installs see the force-included copy
    inside the installed ``nexus/_resources/dt-scripts``. Both resolve
    via :func:`importlib.resources.files`.
    """
    from importlib.resources import as_file, files  # noqa: PLC0415 — stdlib importlib.resources deferred to function scope

    resource = files("nexus") / "_resources" / "dt-scripts"
    with as_file(resource) as resolved:
        return Path(resolved)


@dt.command("install-scripts")
@click.option(
    "--target",
    type=click.Choice(["toolbar", "menu", "all"], case_sensitive=False),
    default="all",
    show_default=True,
    help=(
        "Which DT script slot to install into. ``toolbar`` installs "
        "scripts into the Toolbar/ subdir (drag to add as toolbar "
        "buttons); ``menu`` installs into Menu/ (DT's Scripts menu, "
        "left of Help); ``all`` does both."
    ),
)
@click.option(
    "--uninstall",
    is_flag=True,
    help="Remove installed scripts instead of installing.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files without prompting.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would happen without writing or deleting.",
)
@click.option(
    "--app-scripts-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Override the DEVONthink Application Scripts directory. "
        "Defaults to ~/Library/Application Scripts/"
        "com.devon-technologies.think. Used by tests; rarely needed "
        "in practice."
    ),
)
def install_scripts_cmd(
    target: str,
    uninstall: bool,
    force: bool,
    dry_run: bool,
    app_scripts_dir: Path | None,
) -> None:
    """Install (or remove) DT-side AppleScripts that wrap ``nx dt index``.

    Drops one or more ``.applescript`` files into DEVONthink's
    Application Scripts subdirectories so the actions appear as
    toolbar buttons (Toolbar/) or in DT's own Scripts menu (Menu/).
    The scripts call back into ``nx dt index`` via ``do shell
    script``; this verb is purely the file-copying installer.

    Restart DEVONthink to make a newly-installed Toolbar script
    draggable in "View > Customize Toolbar…". Menu items are picked
    up on the next menu open.
    """
    if not _is_darwin():
        raise click.ClickException("DEVONthink is macOS-only")

    base = app_scripts_dir if app_scripts_dir is not None else _default_app_scripts_dir()

    targets_filter: set[str] = (
        {"Toolbar", "Menu"} if target == "all"
        else {target.capitalize()}
    )

    if uninstall:
        _uninstall_scripts(base, targets_filter, dry_run=dry_run)
        return

    src_dir = _resolve_dt_script_source_dir()
    _install_scripts(
        src_dir,
        base,
        targets_filter,
        force=force,
        dry_run=dry_run,
    )


def _install_scripts(
    src_dir: Path,
    base: Path,
    targets_filter: set[str],
    *,
    force: bool,
    dry_run: bool,
) -> None:
    """Copy each manifest entry into every applicable DT subdir."""
    written = 0
    skipped = 0
    for filename, manifest_targets in _DT_SCRIPT_MANIFEST.items():
        applicable = set(manifest_targets) & targets_filter
        if not applicable:
            continue
        source = src_dir / filename
        if not source.exists():
            raise click.ClickException(
                f"package-data file missing for manifest entry: {filename}",
            )
        for subdir in sorted(applicable):
            dest_dir = base / subdir
            dest = dest_dir / filename
            if dry_run:
                click.echo(f"would install: {dest}")
                continue

            if dest.exists() and not force:
                if not click.confirm(
                    f"{dest} already exists. Overwrite?",
                    default=False,
                ):
                    click.echo(f"skipped: {dest}")
                    skipped += 1
                    continue

            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            click.echo(f"installed: {dest}")
            written += 1

    if dry_run:
        return

    click.echo("")
    click.echo(f"Done: {written} installed, {skipped} skipped.")
    if written:
        click.echo(
            "Restart DEVONthink to pick up Toolbar scripts in the "
            "'Customize Toolbar…' sheet. Menu items appear on next "
            "menu open.",
        )


def _uninstall_scripts(
    base: Path,
    targets_filter: set[str],
    *,
    dry_run: bool,
) -> None:
    """Remove every manifest entry from every applicable DT subdir.

    Idempotent on missing files: a clean tree returns success with a
    "0 removed" line so the caller can run uninstall freely without
    pre-checking.
    """
    removed = 0
    for filename, manifest_targets in _DT_SCRIPT_MANIFEST.items():
        applicable = set(manifest_targets) & targets_filter
        for subdir in sorted(applicable):
            dest = base / subdir / filename
            if not dest.exists():
                continue
            if dry_run:
                click.echo(f"would remove: {dest}")
                continue
            dest.unlink()
            click.echo(f"removed: {dest}")
            removed += 1

    if dry_run:
        return

    click.echo("")
    click.echo(f"Done: {removed} removed.")
