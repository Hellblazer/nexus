# SPDX-License-Identifier: AGPL-3.0-or-later
"""Knowledge-entry catalog registration helper.

nexus-8g79.10 (V1): hosted at this lower layer so MCP infra
(``mcp/core.py``) and CLI command modules can both invoke without
the MCP layer reaching up into the CLI presentation layer.
Previously this function lived in ``commands/store.py`` and was
imported FROM ``mcp/core.py:1029`` — a layering inversion flagged
by the post-4.32.4 multi-agent audit.

Callers: :mod:`nexus.catalog.note_write` (``put_note``, the one writer behind MCP
``store_put``, ``nx store put``, ``nx memory promote`` and the recovery-bundle
import), plus the read-side and cleanup helpers ``mcp/core.py`` and
``commands/store.py`` use for ``store_get``, ``store_list`` and ``store_delete``.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from nexus.aspect_readers import uri_for
from nexus.catalog.types import manifest_row_wire
from nexus.embed_window import window_for_model

_log = structlog.get_logger(__name__)


class ManifestVerifyUncertainError(RuntimeError):
    """The note writer could not confirm whether its one-request write landed.

    Raised by :mod:`nexus.catalog.note_write` (RDR-192 Step 3a, nexus-wbfpw.28;
    RDR-223) when the catalog reader used to verify was unavailable, or the
    verify READ ITSELF raised, on every one of the bounded retry attempts
    (:func:`_read_manifest_rows_with_retry`), or when the write returned without
    being stamped complete. This is distinct from ``NoteWriteError``, which
    means the write is CONFIRMED not to have landed. This one means the outcome is UNKNOWN: the write may have landed and
    simply could not be observed, so callers report the uncertainty and never
    treat the note as absent. See :mod:`nexus.catalog.note_write`'s module
    docstring for the outcome split.
    """


#: RDR-192 Step 3a fix-round 2 (critic (a) / Decision 2): a bounded number
#: of manifest-verify attempts before declaring the outcome uncertain — a
#: single transient read hiccup (a momentary connection blip, a gateway
#: retry the client layer below this hasn't finished absorbing) must not
#: immediately cost a caller a rollback-vs-uncertain decision when one more
#: attempt would likely have resolved it.
_MANIFEST_VERIFY_RETRY_ATTEMPTS = 3
#: Fixed backoff between attempts, in seconds. Deliberately NOT
#: exponential — this guards a read, not a write, and the read's own
#: round trip already provides some natural spacing; a fixed, short delay
#: keeps the worst-case added latency small and predictable.
_MANIFEST_VERIFY_RETRY_BACKOFF_S = 0.2


def _manifest_verify_retry_sleep(seconds: float) -> None:
    """Sleep between manifest-verify retry attempts (RDR-192 Step 3a
    fix-round 2, Decision 2). A separate top-level function — never an
    inlined ``time.sleep`` call — so tests can monkeypatch this exact
    name to a no-op and assert on call count/timing without real delays.
    """
    import time  # noqa: PLC0415 — deferred: only imported on the retry path

    time.sleep(seconds)


def _read_manifest_rows_with_retry(catalog_doc_id: str, *, context: str) -> list[tuple[int, str]]:
    """Read *catalog_doc_id*'s current manifest as ``[(position, chash), ...]`` in position
    order, retrying up to :data:`_MANIFEST_VERIFY_RETRY_ATTEMPTS` times with
    :data:`_MANIFEST_VERIFY_RETRY_BACKOFF_S` backoff between attempts before giving up
    (RDR-192 Step 3a fix-round 2, critic (a) / Decision 2). Raises
    :class:`ManifestVerifyUncertainError` -- never returns a partial or best-guess result --
    when EVERY attempt fails: no catalog reader available, or the read itself raises.

    *context* names the calling situation (e.g. "post-write verify" or
    "write-exception arbitration") so the eventual uncertain-outcome
    message is legible without a caller having to infer which of
    the note writer's verify call sites raised.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import at module load

    last_exc: Exception | None = None
    for attempt in range(_MANIFEST_VERIFY_RETRY_ATTEMPTS):
        if attempt > 0:
            _manifest_verify_retry_sleep(_MANIFEST_VERIFY_RETRY_BACKOFF_S)
        reader = None
        try:
            reader = make_catalog_reader()
            if reader is None:
                raise RuntimeError("catalog reader unavailable")  # noqa: TRY301 — converted to ManifestVerifyUncertainError below if every attempt fails
            return sorted((int(getattr(row, "position", 0)), row.chash) for row in reader.get_manifest(catalog_doc_id))
        except Exception as exc:  # noqa: BLE001 — retried; converted below if every attempt fails
            last_exc = exc
        finally:
            if reader is not None:
                try:
                    reader._db.close()
                except Exception:  # noqa: BLE001 — service-mode reader has no SQLite handle (property raises)
                    pass
    raise ManifestVerifyUncertainError(
        f"manifest write for {catalog_doc_id}: {context} — verify failed "
        f"after {_MANIFEST_VERIFY_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


def single_chunk_manifest_metadata(content: str) -> tuple[str, list[dict]]:
    """Compute the T3 natural id and manifest-hook chunk metadata for a
    single-chunk note (what :func:`note_manifest_metadata` returns for a
    one-piece note).

    Mirrors ``T3Database.put``'s single-chunk derivation (RDR-108 D1 /
    nexus-kmb6; width per RDR-180): the T3 natural id is the FULL
    ``sha256(content).hexdigest()``. ``manifest_write_batch_hook``
    (GH #1371) gets the same full hex under ``chunk_text_hash``
    (stored verbatim — the [:32] write-time truncation is retired).
    A one-piece note is single-chunk by construction, so
    ``chunk_start_char=0`` / ``chunk_end_char=len(content)``
    span the whole document and position defaults to 0 (the batch's only
    element).

    Returns ``(doc_id, metadatas)`` — *metadatas* is a 1-element list
    ready to pass straight through as the ``fire_batch`` /
    ``fire_store_chains`` ``metadatas`` argument. Without real metadata
    here ``manifest_write_batch_hook`` short-circuits on
    ``if not metadatas: return`` and no ``catalog_document_chunks``
    manifest row (nor the ``documents.chunk_count`` update) is ever
    written for a single-chunk note (GH #1370 Defect 4b). (Historically
    this also unblocked the chash dual-write hook, which hit the same
    ``metadatas`` guard — that hook was retired by RDR-187 /
    nexus-piwya.4.)
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    doc_id = content_hash  # RDR-180: the full digest IS the natural id
    metadata = {
        "chunk_text_hash": content_hash,
        "chunk_start_char": 0,
        "chunk_end_char": len(content),
    }
    return doc_id, [metadata]


#: Character cap a stored note is split at when its model imposes no window
#: (nexus-b2tld). CHARACTERS, deliberately, not tokens: Voyage exposes no
#: tokenizer to this client, so any token figure here would be characters
#: divided by an assumed ratio and then labelled as a measurement.
#:
#: 1,689 matches :mod:`nexus.md_chunker`'s effective ceiling, which is the only
#: chunk geometry in this repo with a measured retrieval number attached: over
#: the 320 files of ``docs/rdr`` it yields a 1,126-character median and 84 to 92
#: percent deep-band recall at 5. Matching a measured geometry is a smaller
#: claim than inventing a smaller one from another project's sweep. A note at
#: the population median of 3,921 characters becomes two or three pieces.
NOTE_SPLIT_CHARS = 1689


def note_pieces(content: str, collection: str) -> list[str]:
    """The chunks a stored note is written as (nexus-spujb, nexus-b2tld).

    ``"".join(pieces) == content`` always: :func:`note_manifest_metadata`
    derives each piece's span by accumulating ``len(piece)`` with no gaps, and
    :func:`store_get` reassembles by position, so a splitter that added or
    dropped a character would misreport every span after the first. That rules
    out the markdown chunker here, which prepends a section heading to each
    chunk and overlaps neighbours.

    Two reasons a note splits, and they compose:

    * its model reads fewer tokens than the note holds (bge-base reads 512),
      the original nexus-spujb case, split by the model's real token window;
    * its model imposes no window at all (voyage-context-3 reads 32,000, above
      the 12,288-byte chunk cap, so nothing can exceed it), in which case the
      note used to be stored WHOLE however long it was. One vector then had to
      represent every topic the note covered, and search could find the note
      but not the part of it. Measured 2026-09-19: 309 of 310 single-chunk
      knowledge documents were notes, median 3,921 characters, and on four
      near-identical notes the owning part ranked first in only 2 of 4 probes,
      with one fact verbatim in a note failing to return it at all. Such a
      note now splits at :data:`NOTE_SPLIT_CHARS`.

    The byte quota is :func:`raise_if_oversized`'s, unchanged.
    """
    from nexus.chunker import split_text_to_char_cap, split_text_to_token_window  # noqa: PLC0415 — deferred, heavy import graph

    from nexus.corpus import embedding_model_row_preferred  # noqa: PLC0415 — deferred to avoid import cycle

    window = window_for_model(embedding_model_row_preferred(collection))
    if window is None:
        # No window to fit: split for retrieval granularity instead.
        if len(content) <= NOTE_SPLIT_CHARS:
            return [content]
        return split_text_to_char_cap(content, NOTE_SPLIT_CHARS)
    if window.fits(content):
        return [content]
    return split_text_to_token_window(content, window)


def note_manifest_metadata(pieces: list[str]) -> tuple[str, list[dict]]:
    """``(first chunk id, manifest metadata per piece)`` for a note written
    as *pieces*.

    One piece is exactly :func:`single_chunk_manifest_metadata`, so a note
    that fits writes the manifest it always did. Several pieces carry their
    position and their span of the whole note.
    """
    if len(pieces) == 1:
        return single_chunk_manifest_metadata(pieces[0])
    metadatas: list[dict] = []
    offset = 0
    for i, piece in enumerate(pieces):
        metadatas.append({
            "chunk_text_hash": hashlib.sha256(piece.encode()).hexdigest(),
            "chunk_index": i,
            "chunk_start_char": offset,
            "chunk_end_char": offset + len(piece),
        })
        offset += len(piece)
    return metadatas[0]["chunk_text_hash"], metadatas


def note_content_hash(content: str, manifest_metadatas: list[dict]) -> str:
    """The content hash the fence and the manifest completion stamp key on.

    A one-piece note keeps the derivation it always had, its manifest row's
    ``chunk_text_hash`` (which is the whole note's hash); a split note uses
    the whole note's hash, since no single chunk's hash names it.
    """
    if len(manifest_metadatas) == 1:
        return manifest_metadatas[0].get("chunk_text_hash", "")
    return hashlib.sha256(content.encode()).hexdigest()


def manifest_doc_index(
    collection: str,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str], str]:
    """``(chash -> owning tumblers, tumbler -> title, tumbler -> head chash, reason)`` for every
    manifested document in *collection*: :func:`_manifest_doc_index_full` without the index dates."""
    owners, titles, heads, _stamps, reason = _manifest_doc_index_full(collection)
    return owners, titles, heads, reason


def _manifest_doc_index_full(
    collection: str,
) -> tuple[dict[str, tuple[str, ...]], dict[str, str], dict[str, str], dict[str, str], str]:
    """``(chash -> owning tumblers, tumbler -> title, tumbler -> head chash,
    tumbler -> indexed_at, reason)`` for every manifested document in *collection*.

    A chash maps to EVERY document whose manifest holds it (nexus-sis0m.5):
    identical chunk text in one collection is one T3 row by design, so two
    documents with the same text share every chunk, and mapping each chash
    to one tumbler listed the pair as a single document.

    The grouping a document-level listing needs. The ``document_chunks``
    manifest is the authoritative document-to-chunk map — RDR-108 Phase 3
    (nexus-bdag) dropped the chunk-level ``doc_id`` / ``chunk_index`` /
    ``chunk_count`` mirrors precisely so read paths would consult it — and
    both listing surfaces instead grouped by each chunk row's own
    ``content_hash``, on the premise that a ``store_put`` note is always one
    chunk. Note splitting (nexus-spujb, nexus-b2tld) falsified that: a long
    note is several pieces, each with its own content hash, under ONE
    document. Both then showed a split note as one row per piece, sharing a
    title, which is indistinguishable from duplicate copies and was read as
    exactly that on 2026-09-19.

    Lives here rather than beside either caller because there were already
    two independent copies of the broken grouping (``mcp/core.py``'s
    ``_store_list_docs`` and ``commands/store.py``'s ``_list_documents``) and
    a third would be the same bug waiting to be fixed once more.

    *reason* is empty on success and names the failure otherwise, so a caller
    can say its listing is degraded rather than silently under-grouping
    (nexus-39upx hazard 4: a skip is never silent). Two catalog round trips
    for the whole collection: one ``list_by_collection``, one batched
    ``get_manifests``. A document with no manifest rows contributes nothing
    and its chunks fall through to per-chunk keying at the call site — which
    is right for both a manifest-less legacy note and a superseded chunk
    no sweep has reaped.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle

    reader = None
    try:
        reader = make_catalog_reader()
        if reader is None:
            return {}, {}, {}, {}, "no catalog reader"
        entries = list(reader.list_by_collection(collection) or [])
        titles = {
            str(t): (getattr(e, "title", "") or "")
            for e in entries
            if (t := getattr(e, "tumbler", ""))
        }
        manifests = reader.get_manifests(list(titles)) if titles else {}
        owners: dict[str, list[str]] = {}
        heads: dict[str, str] = {}
        for tumbler, rows in (manifests or {}).items():
            ordered = sorted(rows, key=lambda r: r.position)
            if not ordered:
                continue
            heads[str(tumbler)] = ordered[0].chash
            for row in ordered:
                holders = owners.setdefault(row.chash, [])
                if str(tumbler) not in holders:
                    holders.append(str(tumbler))
        stamps = {
            str(t): (getattr(e, "indexed_at", "") or "")
            for e in entries
            if (t := getattr(e, "tumbler", ""))
        }
        return {c: tuple(t) for c, t in owners.items()}, titles, heads, stamps, ""
    except Exception as exc:  # noqa: BLE001 — a degraded listing is reported, never raised
        return {}, {}, {}, {}, f"catalog unreadable ({exc.__class__.__name__})"
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup
                pass


@dataclass(frozen=True)
class DocumentListing:
    """One row of a document-level listing (:func:`group_documents`).

    *handle* is the id ``store_get`` accepts: the manifest's head chash for a
    manifested document, so it does not depend on which piece the paging
    reached first, else the chunk's own id.
    """

    key: str
    title: str
    handle: str
    chunks: int
    entry: dict
    #: The document's own ``indexed_at`` (the catalog column, nexus-6pfgb.13), ``""`` for a chunk-keyed
    #: row with no catalog document; the listing then shows the chunk row's.
    indexed_at: str = ""


def group_documents(
    t3: Any, collection: str, total: int,
) -> tuple[list[DocumentListing], str]:
    """``(rows sorted by title, degraded reason)`` for *collection*'s
    document-level listing.

    Pages every chunk row and credits it to each document whose manifest
    holds it (:func:`manifest_doc_index`), so two documents sharing every
    chunk list as two rows (nexus-sis0m.5). A chunk no manifest holds keys
    by its own content hash. Both listing surfaces call this
    (``commands/store.py`` and ``mcp/core.py``), for the reason
    :func:`manifest_doc_index` gives: two copies of one grouping loop had to
    be fixed twice.
    """
    owners, doc_titles, doc_heads, doc_stamps, degraded = _manifest_doc_index_full(collection)
    seen: dict[str, dict] = {}
    counts: dict[str, int] = {}
    offset = 0
    while offset < total:
        entries = t3.list_store(collection, limit=300, offset=offset)
        if not entries:
            break
        for e in entries:
            chash = e.get("id", "")
            for key in owners.get(chash) or (e.get("content_hash", chash),):
                seen.setdefault(key, e)
                counts[key] = counts.get(key, 0) + 1
        offset += 300
    rows = [
        DocumentListing(
            key=key,
            title=doc_titles.get(key) or e.get("title") or "",
            handle=doc_heads.get(key) or e.get("id") or key,
            chunks=counts[key],
            entry=e,
            indexed_at=doc_stamps.get(key, ""),
        )
        for key, e in seen.items()
    ]
    rows.sort(key=lambda r: r.title)
    return rows, degraded


def split_note_text(t3: Any, collection: str, chunk_ids: list[str]) -> tuple[str, str, int] | None:
    """``(first chunk id, full text, chunk count)`` when *chunk_ids* are
    chunks of ONE split note, else ``None`` (nexus-spujb, nexus-b2tld).

    One id resolves to the note it belongs to; several ids (a title lookup)
    resolve only when all of them belong to one note. A note missing any of
    its chunks returns ``None``, never a partial text.

    nexus-b2tld: this used to short-circuit on
    ``has_small_window(...)`` before any catalog round trip, on the premise
    that "only a collection whose model has a small token window can hold a
    split note". :func:`note_pieces` splitting a WINDOWLESS collection for
    retrieval granularity made that premise false, and the short-circuit
    then declined to join pieces that were written and manifested correctly
    — :func:`store_get` fell back to the first piece alone, with no
    ``Chunks:`` line and no error. Measured against the live cloud store on
    2026-09-19: a 2,064-character note read back as 1,621 characters.

    The manifest is now the only authority: ``len(chashes) < 2`` below is
    the honest test of whether a document is split, and it costs one catalog
    round trip per read that the short-circuit used to save.
    """
    if not chunk_ids:
        return None
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import at module load

    reader = make_catalog_reader()
    if reader is None:
        return None
    by_chash = reader.docs_for_chashes(list(chunk_ids))
    for tumbler in sorted({t for ts in by_chash.values() for t in ts}):
        rows = sorted(reader.get_manifest(tumbler), key=lambda r: r.position)
        chashes = [r.chash for r in rows]
        if len(chashes) < 2 or not set(chunk_ids) <= set(chashes):
            continue
        parts: list[tuple[str, int | None, int | None]] = []
        for chash in chashes:
            entry = t3.get_by_id(collection, chash)
            if entry is None:
                return None
            parts.append((
                entry.get("content", ""),
                _span_offset(entry.get("chunk_start_char")),
                _span_offset(entry.get("chunk_end_char")),
            ))
        return chashes[0], join_manifest_parts(parts), len(chashes)
    return None


def _span_offset(value: Any) -> int | None:
    """A chunk's recorded source offset as an int, or ``None`` when it has
    none. Chunk metadata round-trips through JSON, so an offset can arrive
    as a string; anything that is not a whole number is treated as absent,
    which costs the rebuild only the overlap trim."""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Shortest suffix/prefix agreement the rebuild will act on. Below this, an
#: agreement is as likely to be a coincidence of punctuation or markup as a
#: real overlap, and trimming one would delete text the document needs.
MIN_VERIFIED_OVERLAP: int = 12

#: A leading markdown ATX heading line (``_split_large_section``'s own
#: re-injection shape: ``"#" * level + " " + header + "\n\n"``). Matched only
#: to recognize a DUPLICATE heading already present in `joined`; never used
#: to strip a heading that has no confirmed span overlap behind it.
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6} .+)\n\n")


def join_manifest_parts(parts: list[tuple[str, int | None, int | None]]) -> str:
    """Join ``(text, start_char, end_char)`` manifest parts in position
    order, dropping the PDF chunker's overlap where the recorded spans and
    the text agree there is one (nexus-kas9u).

    This used to be ``"".join``, which is correct for a ``store_put`` note:
    :func:`note_pieces` cuts a note into non-overlapping pieces, and
    ``tests/test_store_put_split.py`` pins that they rejoin byte-exactly.
    :class:`~nexus.pdf_chunker.PDFChunker` overlaps its chunks by
    ``_DEFAULT_OVERLAP`` of the window on purpose, so the same join
    reproduced the overlap for every PDF document. Measured on the KnowFeat
    paper (tumbler 1.12.152, 57 chunks): 59 duplicated runs covering 14,199
    of 75,999 characters, the longest exactly ``overlap_chars``, with
    mid-token splices where a chunk ended mid-word. The stored chunks were
    clean the whole time; only the rebuild was wrong, identically on the MCP
    ``store_get`` and ``nx store get`` paths.

    The spans only ever PROPOSE a trim and the text decides, because the
    stored text is not a verbatim slice of the source: ``PDFChunker``
    strips each chunk, and a chunk opening inside a table is prefixed with
    that table's header row. So three shapes must survive untouched, and
    each is a real case rather than a hypothetical:

    * **Sub-pieces of one window.** ``pdf_chunker``'s byte/token post-pass
      splits an oversized chunk with ``dict(c.metadata)``, so the pieces
      carry identical spans. An equal span means "same window", never an
      overlap of the window's whole length.
    * **A ``note_pieces`` note.** Its pieces each report
      ``chunk_start_char=0`` with DIFFERENT ends — measured on a live
      two-piece note as ``(0, 1352)`` then ``(0, 1550)``. Read as an
      overlap that would be 1,352 characters of a 1,550-character piece,
      so a coincidental twelve-character agreement would silently delete
      most of the note. The start must therefore ADVANCE for an overlap to
      be considered, which excludes this regime by construction rather
      than leaving it to the text check to refuse. ``note_pieces``' own
      docstring already states the write-side half of this
      (``"".join(pieces) == content`` always, which "rules out the
      markdown chunker here, which prepends a section heading to each
      chunk and overlaps neighbours"); the read side never had the
      matching exclusion, which is how nexus-b2tld's fix to an under-join
      opened an over-join.
    * **No recorded span.** ``store_put`` notes, and any chunk written
      before the spans existed. Nothing to propose a trim, so none happens.
    * **An unconfirmed overlap.** A table continuation's header prefix
      means it does not begin with the previous part's tail. Trimming on
      the span alone would eat real rows, so a span with no matching text
      is logged and left whole.

    Only removal is possible here. No separator is ever inserted, both
    because the note contract above forbids it and because the chunker
    starts a chunk at the previous chunk's exact end only around a table,
    where a lost strip character cannot glue two words together.
    """
    joined = ""
    prev_span: tuple[int | None, int | None] = (None, None)
    for index, (text, start, end) in enumerate(parts):
        if not joined:
            joined, prev_span = text, (start, end)
            continue
        prev_start, prev_end = prev_span
        trim = 0
        # A real overlap advances the window: start strictly greater than the
        # previous start, and short of the previous end. Requiring the
        # ADVANCE, rather than merely a different span, is what keeps the
        # note regimes out by construction instead of by luck — see the
        # note_pieces case in this function's docstring.
        overlap = (
            prev_end - start
            if start is not None
            and prev_start is not None
            and prev_end is not None
            and prev_start < start < prev_end
            else 0
        )
        append_text = text
        if overlap >= MIN_VERIFIED_OVERLAP:
            # nexus-yz7se: _split_large_section re-prefixes the section
            # heading onto every chunk it emits from an oversized section —
            # deliberate, kas9u's own precedent for PDFChunker._table_header
            # on a table continuation, so a lone chunk stays independently
            # readable. The heading is not part of the overlap span (the
            # writer only backs the span up by the overlap_tail's own
            # length), so it never confirms against `joined`'s tail; try the
            # match with a duplicate leading heading stripped FIRST, and
            # fall back to the untouched text if that does not confirm
            # either — never strip on the strength of the heading alone.
            #
            # nexus-yz7se round-2 (T2 nexus/review-burndown-batch2-2026-09-23):
            # this used to be `heading_match.group(0) in joined` — a
            # substring search across the WHOLE accumulated rebuild, which
            # a heading string merely mentioned mid-body somewhere earlier
            # in the document (prose citing "## Results" as an ATX-syntax
            # example, say) would satisfy just as well as a genuine
            # repeat. Anchored instead to the heading *parts[index - 1][0]*
            # — the part this overlap is actually WITH — carried at its
            # own head, compared exactly, never a substring test.
            match_text = text
            heading_match = _MARKDOWN_HEADING_RE.match(text)
            prev_heading_match = _MARKDOWN_HEADING_RE.match(parts[index - 1][0])
            if (
                heading_match
                and prev_heading_match
                and heading_match.group(1) == prev_heading_match.group(1)
            ):
                match_text = text[heading_match.end():]
            # The match is bounded ABOVE by the recorded overlap: stripping
            # can only ever shorten the agreement, never lengthen it.
            for k in range(min(overlap, len(match_text), len(joined)), MIN_VERIFIED_OVERLAP - 1, -1):
                if joined.endswith(match_text[:k]):
                    trim = k
                    append_text = match_text
                    break
            if trim == 0:
                _log.debug(
                    "document_rebuild_overlap_unconfirmed",
                    part_index=index,
                    recorded_overlap=overlap,
                )
        joined += append_text[trim:]
        prev_span = (start, end)
    return joined


def raise_if_oversized(content: str, *, doc_id: str, collection: str) -> None:
    """Refuse an over-quota single-chunk store BEFORE any catalog work.

    nexus-xzyr3 fold-in (code-review-nexus-xzyr3-26edb6662 [24586] /
    critique-nexus-xzyr3-26edb6662 [24589]): ``T3Database.put`` (and, until it
    was retired, ``HttpVectorClient.put``) already refuses an over-quota document with
    ``PutOversizedError`` (``fail_on_oversized=True``) — but ONLY once
    called, which is AFTER ``catalog_store_hook_tracked`` has already
    minted a catalog row for every one of the note producers (MCP
    ``store_put``, ``nx store put``, ``nx memory promote``, the recovery
    import). Every oversized attempt was paying for a wasted mint + rollback
    round trip. Its one caller now is
    :func:`nexus.catalog.note_write.put_note`, which calls this FIRST — right
    after splitting the note, before ``catalog_store_hook_tracked`` — so an
    over-quota note fails fast with no catalog side effect at all. ``put()``'s own check stays as the
    defense-in-depth backstop for any caller that skips this pre-check
    (direct test calls, future callers).

    Raises:
        PutOversizedError: same shape and message ``put()`` raises —
            single source of truth for both the check and the wording.
    """
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost
    from nexus.errors import PutOversizedError  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    doc_bytes = len(content.encode())
    if doc_bytes > QUOTAS.MAX_DOCUMENT_BYTES:
        raise PutOversizedError(
            doc_id=doc_id,
            doc_bytes=doc_bytes,
            max_bytes=QUOTAS.MAX_DOCUMENT_BYTES,
            collection=collection,
        )


def resolve_knowledge_doc_for_chash(
    reader, chash: str, *, log_event: str, collection: str | None = None,
    owner_collection: str | None = None, title: str | None = None,
):
    """Resolve *chash* to the single store_put-origin catalog document it
    identifies, or ``None`` if there is no match or the match is ambiguous.

    *title*, when given (``""`` included), restricts candidates to documents
    whose ``title`` equals it, BEFORE the ambiguity count (nexus-6pfgb.31).
    The put path passes it: a note's identity is (collection, title), so a put
    reconciles only onto the note it names, and an untitled put only onto an
    untitled one with an empty ``source_uri``, which every untitled put writes (one
    that has a source_uri was registered some other way, by hand through ``catalog
    register`` since nothing in the tree writes one, and is not an earlier untitled
    put, nexus-6pfgb.33). Without it a put adopted whatever note held the text, rewrote
    its source_uri and stamped its own ``ttl_days`` on it; with the filter after the count,
    a titled note sharing the text would hide the put's own note as ambiguous.

    *collection*, when given, additionally restricts candidates to
    entries whose ``physical_collection`` equals it (nexus-bb6n2 round 2).
    ``docs_for_chashes`` is a catalog-WIDE reverse lookup — chash is a
    pure function of chunk text, collection-independent — so an
    unscoped call can match a document registered under a DIFFERENT
    collection whose manifest happens to reference an identical chunk.
    Pass *collection* for a store_put RECONCILE, whose contract is keyed
    on (collection, title), so a cross-collection chash coincidence can
    never be mistaken for "this document already exists in the
    collection I am writing to."

    *owner_collection* is the delete-path scope (nexus-r3cdg): candidates
    are entries whose ``physical_collection`` equals it OR is blank (a
    ghost, which has no collection to mismatch; nexus-sz89e). A chunk row
    lives in one collection, and the engine's delete anti-join and
    live(c) both count only manifest rows in that collection, so an
    owner in another collection neither owns nor protects it. This scopes
    by the document's ``physical_collection``, the engine by the manifest
    row's own ``collection`` column; the schema does not force the two
    equal (see ``manifest_backfill.py``), but every writer sets them
    together, and a document whose rows name another collection is
    already outside what the delete path can repair. Before
    this scope the delete callers resolved catalog-wide: after an
    ``.nxexp`` import into another collection the chash had two owners,
    resolution called it ambiguous, nothing was reaped, and the home
    document's own manifest row then protected the chunk (shakeout
    7.64.1 F2: "Deleted 0 entries", exit 0).

    nexus-5axey: ``by_doc_id`` is a TUMBLER-only lookup on the engine (the
    settled wji11 contract: tumbler is the only document identity); it
    cannot answer "which document has this content-chash in its
    ``meta.doc_id``" — that used to alias ``resolve()`` and simply mismatch
    every chash-shaped input. This is the chash-appropriate replacement,
    built on :meth:`docs_for_chashes` (chash -> ``[doc_id, ...]``, the
    reverse-manifest-lookup primitive present on every deployed engine).

    chash -> document is one-to-many in general: identical chunk text in a
    collection collapses to one T3 row, and the manifest can point many
    documents at that shared chash (RDR-108's collapsing-by-design). For
    store_put / memory-promote-origin documents specifically (identified by
    ``content_type == "knowledge"`` with no ``file_path`` — the same filter
    the pre-existing delete-path cleanup already applied) an UNAMBIGUOUS
    single match is trusted. More than one candidate is treated
    conservatively as "no safe match" — this function returns ``None`` and
    logs at WARNING — rather than acting on an arbitrary pick or every
    candidate: acting on the wrong one (or deleting/reconciling onto all of
    them) is a worse outcome than leaving one ghost row for the periodic
    ``nx catalog gc`` sweep to reap. One exception: an UNTITLED put
    (``title == ""``) that finds several untitled candidates returns the
    lowest tumbler, since untitled documents sharing the text are
    interchangeable and treating them as ambiguous minted another on every
    later untitled put (nexus-6pfgb.31).

    *log_event* names the calling site (e.g. ``"catalog_store_hook_dedup"``)
    so the ambiguity warning is attributable to dedup vs. delete-path vs.
    tombstone-reap without three near-identical log statements.

    A malformed *chash* (not the hex digest production always produces —
    e.g. a legacy non-hex meta.doc_id) 400s at the wire rather than simply
    missing; that specific case is treated the same as a miss (``None``,
    WARNING logged). Anything else (connectivity failure, 5xx, ...)
    PROPAGATES — nexus-f1itv/ou4tb's fail-loud contract depends on this
    reaching the caller's own broad except (WARNING + audit row), not
    being silently absorbed into "proceed as if nothing existed".
    """
    try:
        by_chash = reader.docs_for_chashes([chash])
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 400:
            raise
        # 400 == the wire rejected *chash* itself (not valid hex) — a
        # malformed input can never resolve, so it is a miss, not a fault.
        _log.warning(f"{log_event}_malformed_chash", chash=chash, error=str(exc))
        return None
    matches = by_chash.get(chash, [])
    candidates = []
    for tumbler in matches:
        entry = reader.resolve(tumbler)
        if (
            entry is not None
            and entry.content_type == "knowledge"
            and not entry.file_path
            and (title is None or entry.title == title)
            # nexus-6pfgb.33: an untitled put reconciles only onto an earlier untitled put, which writes
            # no source_uri; an untitled document that has one (a path-keyed import) is that path's.
            and (title != "" or not entry.source_uri)
            and (collection is None or entry.physical_collection == collection)
            and (
                owner_collection is None
                or not entry.physical_collection
                or entry.physical_collection == owner_collection
            )
        ):
            candidates.append(entry)
    if len(candidates) > 1 and title == "":
        # Several UNTITLED documents share the text (concurrent first untitled puts leave them: no unique
        # index guards source_uri ""). They are interchangeable, and calling them ambiguous made every
        # later untitled put mint another (2 -> 5 after 3 puts). Reconcile onto the lowest tumbler, the
        # oldest. Only for the untitled put: it writes no source_uri, so the pick cannot collide with the
        # live-source_uri index; a titled put stays ambiguous and finds its note by source_uri.
        _log.warning(
            f"{log_event}_duplicate_untitled",
            chash=chash, candidate_count=len(candidates), tumblers=[str(c.tumbler) for c in candidates],
        )
        return min(candidates, key=lambda c: c.tumbler)
    if len(candidates) > 1:
        _log.warning(
            f"{log_event}_ambiguous_chash",
            chash=chash,
            candidate_count=len(candidates),
            tumblers=[str(c.tumbler) for c in candidates],
        )
        return None
    return candidates[0] if candidates else None


def _find_ghost_by_title(reader, owner, title: str):
    """Return an existing GHOST catalog entry under *owner* whose title
    exactly matches *title*, or ``None``.

    GH #1370 Defect 4a: a pre-existing catalog entry with the same
    title (e.g. a ghost with ``chunk_count=0`` and empty ``head_hash``,
    left behind by a pre-migration catalog or an earlier failed index)
    is invisible to :func:`resolve_knowledge_doc_for_chash` — that entry's
    ``meta.doc_id`` predates this content's hash. Without this lookup,
    ``catalog_store_hook``
    mints a brand-new document with a fresh tumbler and the ghost is
    never reconciled.

    The reader answers an exact-title, owner-scoped lookup itself
    (``find_by_title_exact(..., owner=owner, chunk_count=0)``, nexus-gacz0): one
    engine request for the byte-for-byte title matches among *owner*'s ghosts.
    Against an engine without that route the reader falls back to filtering
    the full-text results, which use token matching, not substring matching;
    the owner-prefix check below stays so both paths hold the same contract
    (the "knowledge" curator owner; content_type="knowledge" alone is not
    owner-specific).

    Restricted to GHOST entries (``chunk_count == 0``): reconciling
    onto a non-ghost entry would silently repoint an already-populated
    document's ``meta.doc_id`` / ``physical_collection`` at unrelated
    new content, orphaning its existing ``document_chunks`` manifest
    rows — a worse outcome than the duplicate-entry bug being fixed
    here. A same-titled non-ghost match therefore falls through to
    ``register()`` exactly as before.

    Skipped (returns ``None`` immediately) when *title* is empty — an
    empty title must never match arbitrary same-titled ("") entries.
    """
    if not title:
        return None
    # Every exact-title match, not find()'s first 50: a ghost past row 50
    # went unmatched and register() minted a duplicate (nexus-3bafq sibling).
    # chunk_count=0 asks the reader for ghosts only (the engine filters it); the checks
    # below stay as the guard, so a reader that ignores either filter cannot widen a match.
    for entry in reader.find_by_title_exact(
        title, content_type="knowledge", owner=owner, chunk_count=0,
    ):
        if entry.chunk_count == 0 and owner.is_prefix_of(entry.tumbler):
            return entry
    return None


#: The note keys that have no catalog column, kept in the document's ``meta`` (nexus-6pfgb.5). The
#: chunk row carries them too, but a chunk shared by two notes carries only the last writer's.
#: ``put_at`` (nexus-6pfgb.15) is the time of the note's last put: the clock its own ``ttl_days``
#: counts from (:mod:`nexus.catalog.note_ttl`), and the marker that the catalog, not the shared
#: chunk row, holds this note's TTL. ``tags`` (nexus-6pfgb.38, RDR-236 RF-17) is a JSON ARRAY, the
#: tags being a set: the chunk row keeps the comma string, the catalog entry the array, so a
#: containment filter matches one tag of several. A string in a stored ``meta`` is a legacy value
#: (written before nexus-6pfgb.38), converted by the ``note-tags-array`` upgrade rung.
NOTE_META_KEYS: tuple[str, ...] = ("tags", "category", "ttl_days", "source_agent", "put_at")

#: What a note key reads as when it is unset: the retraction sentinel written over a key the entry held
#: (catalog ``update`` has no meta delete-keys, so a written key cannot be removed). In catalog meta a
#: ``ttl_days`` of 0 is that sentinel, not an accepted input (``put_note`` and ``write_note`` reject 0;
#: the chunk row spells permanent as ``None``/absent): readers treat 0 and absent as permanent. The
#: unset ``tags`` is the empty array; a legacy ``""`` reads as unset too (:func:`_note_key_unset`).
_NOTE_META_UNSET: dict[str, Any] = {"tags": [], "category": "", "ttl_days": 0, "source_agent": "", "put_at": ""}


def split_note_tags(tags: object) -> list[str]:
    """A note's tags as the array the catalog ``meta`` holds (nexus-6pfgb.38): the comma string the CLI
    and MCP pass through (or a list), each tag trimmed, empties dropped, duplicates removed, first-seen
    order kept (tags are a set; the order is the writer's). ``None`` and ``""`` are ``[]``."""
    if not tags:
        return []
    parts = tags.split(",") if isinstance(tags, str) else [str(t) for t in tags]  # type: ignore[attr-defined,union-attr]
    out: list[str] = []
    for part in parts:
        tag = part.strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def _note_key_unset(key: str, value: Any) -> bool:
    """True when *value* is what the note key reads as unset: absent, the key's sentinel, and for
    ``tags`` a legacy empty string or an array with no tag in it."""
    if value is None:
        return True
    if key == "tags":
        return not split_note_tags(value)
    return value == _NOTE_META_UNSET[key]


def note_catalog_meta(
    *, tags: str | list[str] = "", category: str = "", ttl_days: int | None = None, source_agent: str = "",
    put_at: str = "", prior: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The note keys one put writes to its catalog entry's ``meta``, and what undoes them.

    Returns ``(write, revert)``. *write* holds a key only when this put gives it a real value (the
    engine merges ``meta`` and an explicit empty overwrites, so an unset key is never sent), plus the
    unset value for a key the entry (*prior*, its ``meta`` before this call) holds and this put
    leaves unset: a re-put under the same title replaces the note's keys, as it does on the chunk
    row, and a ``ttl_days`` of 7 that outlived a permanent re-put would expire the note. *revert* maps
    each written key to the value *prior* held (or the unset value), for
    :func:`restore_pre_call_stamp` when the write does not land.

    *tags* is the comma string every caller passes (or a list); it is split once into the array the
    entry stores (:func:`split_note_tags`, nexus-6pfgb.38). A prior legacy string retracts to ``[]``
    like an array, and *revert* gives it back verbatim; a prior ``""`` is unset.

    *put_at* (nexus-6pfgb.15) is the time of this put, an ISO-8601 stamp. Every put sends it, so the
    TTL clock of a re-put note restarts as the chunk row's does, and a failed put restores the old
    stamp with the rest.
    """
    given: dict[str, Any] = {
        "tags": split_note_tags(tags), "category": category or "",
        "ttl_days": ttl_days if ttl_days and ttl_days > 0 else 0, "source_agent": source_agent or "",
        "put_at": put_at or "",
    }
    prior = prior or {}
    write: dict[str, Any] = {}
    revert: dict[str, Any] = {}
    for key in NOTE_META_KEYS:
        unset = _NOTE_META_UNSET[key]
        if not _note_key_unset(key, given[key]):
            write[key] = given[key]
        elif not _note_key_unset(key, prior.get(key)):
            write[key] = unset
        else:
            continue
        revert[key] = prior[key] if prior.get(key) is not None else unset
    return write, revert


def imported_note_meta(meta: dict[str, Any] | None, *, put_at: str) -> dict[str, Any]:
    """The note keys an ``.nxexp`` import stamps on a note's catalog entry (nexus-6pfgb.45): ``put_at``
    = *put_at* and ``ttl_days`` = 0 (permanent), only when the entry (*meta*) holds none of
    :data:`NOTE_META_KEYS`. Fill-only: an entry that holds any note key keeps every one, so a
    note an earlier put gave a TTL keeps it. The export carries no TTL the catalog could keep true, so
    an imported note makes no claim to expire; ``ttl_days`` 0 is the value readers show as permanent
    (:func:`nexus.catalog.note_ttl.note_ttl_days`), and its presence is what tells them the catalog,
    not a shared chunk row, holds this note's TTL. Returns ``{}`` when there is nothing to stamp."""
    if any(key in (meta or {}) for key in NOTE_META_KEYS):
        return {}
    return {"put_at": put_at, "ttl_days": 0}


def catalog_store_hook(
    title: str, doc_id: str, collection_name: str,
) -> str:
    """Back-compat wrapper over :func:`catalog_store_hook_tracked`.

    Returns only the tumbler string; callers that need to know whether
    the row was minted in this call (nexus-b6enc C2 ghost-register
    compensation) use the tracked variant directly.
    """
    tumbler, _created = catalog_store_hook_tracked(title, doc_id, collection_name)
    return tumbler


def catalog_store_hook_tracked(
    title: str, doc_id: str, collection_name: str, *,
    pre_call_doc_id_out: dict[str, Any] | None = None,
    error_out: dict[str, str] | None = None,
    tags: str = "", category: str = "", ttl_days: int | None = None, source_agent: str = "",
    put_at: str | None = None,
) -> tuple[str, bool]:
    """Register a knowledge entry in the catalog.

    Returns ``(tumbler, created)`` — *tumbler* is the catalog
    ``Document.doc_id`` (Tumbler string) so the caller can pass it to
    ``T3Database.put()`` as ``catalog_doc_id`` for chunk-write-time
    embedding (RDR-101 Phase 3 PR δ Stage B.4); *created* is True only
    when this call MINTED a brand-new document row (the
    ``writer.register`` path). Dedup hits (:func:`resolve_knowledge_doc_for_chash`,
    the nexus-sdp0u source_uri reconcile below, or the GH #1370
    ghost-by-title reconcile) return ``created=False`` so the nexus-b6enc
    C2 compensation never deletes a pre-existing row the put deduped
    onto. The ``writer.register`` leg itself now (nexus-vfef0) also
    returns ``created=False`` for the one race the three prechecks above
    cannot close — a genuinely CONCURRENT first-put race on a brand-new
    (collection, title), where the engine's wire response tells a race
    LOSER (this call landed on the WINNER's row, not its own) apart from
    a genuine mint; see ``HttpCatalogClient.register``'s ``with_created``
    kwarg. Returns ``("", False)`` when an error occurs, or in the
    SQLite opt-out mode
    when no local catalog is initialised (service mode always has a
    catalog — the Java service owns it; nexus-f1itv) — the schema
    funnel drops empty ``doc_id`` at the boundary.

    A put never adopts, re-sources or re-keys another note (nexus-6pfgb.31): the
    chash dedup considers only a document with this put's own *title* (an
    untitled put: only an untitled one with no ``source_uri``, nexus-6pfgb.33), and every other holder of the text
    stays as it was while the put mints a second document sharing the chunk.
    A repeated untitled put reuses its untitled document (the lowest tumbler when several share
    the text) and never overwrites a non-empty ``source_uri`` with an empty one.

    ``doc_id`` here is the T3 chunk natural-id (RDR-108 D1 / nexus-kmb6;
    the FULL ``sha256(content)`` hex per RDR-180). It is consulted for legacy
    ``meta.doc_id`` dedup via :func:`resolve_knowledge_doc_for_chash`
    (nexus-5axey; ``docs_for_chashes``-backed, since ``by_doc_id`` is a
    TUMBLER-only lookup and cannot answer a chash-keyed question): catalog
    entries written before Phase 4 stored the legacy 16-char sha256-of-
    collection-and-title under ``meta.doc_id``, so this lookup misses
    on those legacy entries and the hook re-registers. When that
    happens, a second lookup keyed on the synthesized ``source_uri``
    identity (nexus-sdp0u; non-empty *title* only, see below) reconciles
    a RE-PUT of the same (collection, title) onto its existing row
    regardless of chunk_count. When that also misses, a third,
    title-scoped lookup (:func:`_find_ghost_by_title`) reuses a
    pre-existing GHOST entry's tumbler instead of minting a duplicate
    (GH #1370 Defect 4a). A legacy row registered before that fix carries
    ``source_uri=""``, which the second lookup cannot see: the first
    lookup reaches it under its own title and heals its ``source_uri``
    (nexus-6pfgb.31), and this third one is the path for a ghost. Only when all
    three lookups miss does the hook register a brand-new document —
    with the synthesized ``source_uri`` attached, so a LATER re-put finds
    it via the second lookup instead of falling through to this one.

    nexus-sdp0u: pre-fix, this function always passed ``source_uri=""``
    to ``writer.register`` — the engine's upsert-on-``(tenant,
    source_uri)`` identity (``CatalogRepository.registerDocument``'s
    leg-1 SELECT) therefore never matched, and re-putting the same
    title minted an unbounded run of documents with contradictory
    content (production: 1.1.1/1.1.2/1.1.3 from three puts of one
    title). When *title* is non-empty this now synthesizes a stable
    ``source_uri`` via :func:`nexus.aspect_readers.uri_for` — the SAME
    convention the aspect-extraction reader already uses to resolve
    knowledge-collection identity (chunk metadata carries no
    ``source_path`` for these single-chunk callers since RDR-102 D2,
    nexus-bm8dd, so the reader's knowledge-collection identity field is
    *title* — this reuses that exact convention rather than forking a
    second one). Empty *title* synthesizes nothing (``source_uri=None``):
    a title-less ``nx-note://<collection>/`` URI would collapse every
    untitled document under one identity, which is worse than today's
    unlimited-duplicate behavior for that one case — so it is left
    exactly as before (nexus-39upx: legacy-duplicate collapse is a
    separate, out-of-scope backfill).

    Args:
        pre_call_doc_id_out: nexus-k54nk fix-round 1 (T2
            ``nexus/critique-k54nk`` Critical 1). Optional out-parameter
            (never a return-shape change, so every existing 2-tuple
            caller/mock is untouched). When any of the three RECONCILE
            branches below (chash dedup, ``by_source_uri``, ghost-by-
            title) reconciles onto a pre-existing row, this call sets
            ``pre_call_doc_id_out["doc_id"]`` to that row's
            ``meta.doc_id`` AS IT STOOD BEFORE this call's own
            ``writer.update`` overwrites it — the value the caller needs
            to tell :func:`restore_pre_call_stamp` "this is
            what the document legitimately owned before I touched it",
            distinct from what it owns NOW (which may be this call's
            own not-yet-landed, and possibly about-to-fail, content).
            Left untouched (absent) on the brand-new-mint leg
            (``writer.register``, ``created=True``): a freshly minted
            document has no prior identity to protect by construction.
        tags, category, ttl_days, source_agent: the note's own keys
            (nexus-6pfgb.5). A chunk shared by two notes carries only the
            last writer's, so each note's values live in its catalog entry's
            ``meta`` as well, written in the same update as ``doc_id`` on
            every put (see :func:`note_catalog_meta`: only a real value is
            sent; an unset key the entry already holds is retracted).
            ``pre_call_doc_id_out["note_meta"]`` carries what
            :func:`restore_pre_call_stamp` needs to put them back and
            ``["note_meta_stamped"]`` what this call wrote (its compare-and-set).
        put_at: the time of this put, written with them as ``meta.put_at``, the clock the
            note's own ``ttl_days`` counts from (nexus-6pfgb.15, :mod:`nexus.catalog.note_ttl`).
            ``None`` (the default) stamps the current time; tests pass a fixed one.
        error_out: Optional out-parameter, like *pre_call_doc_id_out* (no
            return-shape change). The ``("", False)`` return does not say
            WHY; when it is returned, ``error_out["error"]`` names the
            cause (``"RuntimeError: catalog service is down"``, or that no
            catalog is available), so a caller can report it instead of a
            bare "registration failed" (RDR-223 Phase 2 review).
    """
    # RDR-146 P1.2: this hook fires on every store_put / memory promote,
    # including the long-lived MCP server process. It MUST NOT open a
    # direct .catalog.db writer (the two-writer hazard RDR-146 closes).
    # Reads go through the read-only reader; writes route through the
    # write-only daemon proxy (the single writer). Handles closed in
    # finally so the hot path does not leak.
    reader = None
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 - deferred to avoid circular import at module load

        # nexus-f1itv: presence semantics belong to the factory. In service
        # mode the Java service owns the catalog and no local state exists —
        # the old local ``Catalog.is_initialized(catalog_path())`` pre-check
        # silently skipped registration on every fresh box (migrated boxes
        # passed it only via the frozen migration-source ``.catalog.db``).
        # ``make_catalog_reader()`` returns ``None`` only in the SQLite
        # opt-out mode with an uninitialised local catalog.
        reader = make_catalog_reader()
        if reader is None:
            if error_out is not None:
                error_out["error"] = "no catalog is available (make_catalog_reader returned None)"
            return "", False

        # nexus-sdp0u: stable, collection-scoped identity for this document.
        # Reuses aspect_readers.uri_for's exact nx-note:// convention (the
        # knowledge-collection identity field the reader already resolves
        # by is *title*, since these single-chunk callers carry no
        # source_path in chunk metadata post-RDR-102 D2) — one URI format,
        # never a second one. Empty title synthesizes nothing: see the
        # docstring for why a title-less URI must not be minted.
        #
        # Computed BEFORE the chash dedup below (moved up at nexus-bb6n2
        # round 2) so a dedup HIT can stamp it too.
        source_uri = uri_for(collection_name, title) if title else None
        put_at_stamp = put_at if put_at is not None else datetime.now(UTC).isoformat()

        def stamp_for(entry) -> dict[str, Any]:
            """The ``meta`` to merge into *entry* (``None`` for a brand-new row): the identity stamp
            and the note keys. Records the note keys' undo in ``pre_call_doc_id_out``."""
            write, revert = note_catalog_meta(
                tags=tags, category=category, ttl_days=ttl_days, source_agent=source_agent, put_at=put_at_stamp,
                prior=(getattr(entry, "meta", None) or {}) if entry is not None else None)
            if pre_call_doc_id_out is not None and revert:
                pre_call_doc_id_out["note_meta"] = revert
                pre_call_doc_id_out["note_meta_stamped"] = write
            return {"doc_id": doc_id, **write}

        # Dedup by chash stored in meta.doc_id. nexus-5axey: by_doc_id is a
        # TUMBLER-only lookup on the engine and always mismatched this
        # chash-shaped doc_id; resolve_knowledge_doc_for_chash uses
        # docs_for_chashes, the chash-appropriate reverse lookup.
        #
        # nexus-bb6n2 round 2: scoped to THIS collection. Pre-fix, this
        # lookup was collection-agnostic — docs_for_chashes is a catalog-
        # WIDE reverse lookup (chash is a pure function of chunk text,
        # collection-independent) — so a re-put whose first chunk happened
        # to byte-match a chunk already manifested under a DIFFERENT
        # collection's same-titled document silently adopted THAT
        # document's tumbler here, with NO update to its
        # physical_collection or source_uri: the catalog row kept
        # pointing at the old collection while its manifest and T3 chunks
        # moved to the new one. Measured live 2026-09-23: a 6-chunk split
        # note re-put into a new collection reconciled onto an existing
        # document from knowledge__1-1 this way; a 1-chunk unsplit note
        # re-put the same way did not, only because its single, whole-note
        # chash happened not to collide with anything catalog-wide — same
        # code path, no structural difference between "split" and
        # "unsplit" here. Docstring contract is (collection, title); a
        # cross-collection chash coincidence must never satisfy it.
        #
        # nexus-6pfgb.31: only a document with THIS put's title is a candidate
        # (an untitled put: only an untitled one with no source_uri, nexus-6pfgb.33). The source_uri check below
        # cannot say it for an untitled put (no source_uri of its own to compare)
        # or for a legacy row (empty source_uri): such a put adopted a titled
        # note, blanked or rewrote its source_uri and stamped its own ttl_days
        # on it, and ``nx store expire`` retired a note its owner had made
        # permanent. An untitled put of text a titled note holds mints an
        # untitled document of its own sharing the chunk (RDR-108).
        existing = resolve_knowledge_doc_for_chash(
            reader, doc_id, log_event="catalog_store_hook_dedup",
            collection=collection_name, title=title,
        )
        # nexus-enej7: the same content under a DIFFERENT title is a second
        # document sharing the chunk (RDR-108), not a re-put of this one.
        # Reconciling onto it rewrote the first document's source_uri to the
        # new title and never registered the new one (shakeout 7.64.1 F1).
        # The title filter above already holds that; this stays for the
        # same-title row whose source_uri names another identity.
        if (
            existing is not None
            and source_uri is not None
            and getattr(existing, "source_uri", "")
            and existing.source_uri != source_uri
        ):
            existing = None
        if existing is not None:
            # nexus-k54nk fix-round 1: capture the row's meta.doc_id AS IT
            # STOOD before this call's own writer.update below overwrites
            # it — see catalog_store_hook_tracked's own docstring for
            # pre_call_doc_id_out.
            if pre_call_doc_id_out is not None:
                pre_call_doc_id_out["doc_id"] = (
                    existing.meta or {}
                ).get("doc_id", "")
            # Scoped to collection_name above, so physical_collection
            # already matches — this stamps meta.doc_id at the new
            # content's chash and, defensively, source_uri (a legacy row
            # reachable only via this chash dedup, same shape as the
            # nexus-sdp0u ghost-reconcile fix-round below, could still
            # carry a stale/empty one).
            writer = make_catalog_writer()
            # Send source_uri only when this put has one (nexus-6pfgb.31). An untitled put reconciles
            # only onto a document with none (nexus-6pfgb.33), so this guards a case it cannot reach.
            uri_field = {"source_uri": source_uri} if source_uri else {}
            writer.update(
                existing.tumbler,
                physical_collection=collection_name,
                meta=stamp_for(existing),
                **uri_field,
            )
            return str(existing.tumbler), False

        # Get or create "knowledge" curator owner, filtered on owner_type so
        # a same-named REPO owner cannot shadow the intended curator (same
        # bug shape as the doc_indexer family fix). Via the protocol method
        # (nexus-qnp5s, implemented on BOTH backends), NOT raw reader._db
        # SQL: HttpCatalogClient._db raises RuntimeError in service mode,
        # and the raw-SQL version of this lookup made the outer best-effort
        # except swallow that — turning this entire hook into a silent
        # no-op for every service-mode store_put (GH #1370 review finding).
        owner_t = reader.curator_owner_tumbler_by_name("knowledge")
        writer = make_catalog_writer()
        owner = owner_t if owner_t is not None else writer.register_owner(
            "knowledge", "curator"
        )

        # nexus-sdp0u: reconcile a RE-PUT of the same (collection, title)
        # identity onto its existing LIVE row — regardless of chunk_count,
        # unlike the ghost-by-title fallback below — instead of minting a
        # sibling. by_source_uri is an exact-identity lookup (the URI
        # already encodes the exact collection+title pair store_put /
        # memory-promote register under), so unlike _find_ghost_by_title's
        # FTS token match it needs no owner-prefix post-filter.
        #
        # This precheck (rather than letting the engine's own leg-1
        # idempotency SELECT inside registerDocument silently return the
        # existing tumbler from writer.register) is what lets this
        # function keep created=True meaning "genuinely minted": get that
        # wrong and a later t3.put failure would run
        # rollback_minted_catalog_entry against a live, already-populated
        # document instead of the row this call actually minted
        # (nexus-b6enc C2 firing on the wrong row).
        if source_uri is not None:
            existing_by_uri = reader.by_source_uri(source_uri)
            if existing_by_uri is not None:
                # nexus-k54nk fix-round 1 (T2 nexus/critique-k54nk Critical 1):
                # pinned now by b6enc's TestZ0o2p12McpFailedReput::
                # test_a_failed_reput_leaves_the_old_note_whole_and_restores_the_stamp.
                # THIS branch is the mainline note-edit path and fires on
                # ANY re-put of an existing title, regardless of whether
                # content changed — capture the row's meta.doc_id AS IT
                # STOOD before the writer.update below overwrites it to
                # doc_id (this call's NEW, not-yet-manifested chash).
                if pre_call_doc_id_out is not None:
                    pre_call_doc_id_out["doc_id"] = (
                        existing_by_uri.meta or {}
                    ).get("doc_id", "")
                writer.update(
                    existing_by_uri.tumbler,
                    physical_collection=collection_name,
                    meta=stamp_for(existing_by_uri),
                )
                _log.debug(
                    "catalog_store_hook_reput",
                    tumbler=str(existing_by_uri.tumbler), source_uri=source_uri,
                )
                return str(existing_by_uri.tumbler), False

        # GH #1370 Defect 4a: reconcile onto a pre-existing ghost with the
        # same title (under the knowledge curator owner) instead of minting
        # a near-duplicate. See _find_ghost_by_title for the ghost-only
        # restriction rationale.
        #
        # nexus-sdp0u fix-round (round-1 critique CRITICAL): this branch
        # MUST also stamp source_uri, not just physical_collection/meta.
        # A legacy ghost carries source_uri="" (pre-dates this fix); if the
        # reconcile below left it "", the row's manifest gets populated
        # right after (chunk_count becomes > 0) and it falls out of BOTH
        # identity checks on the NEXT re-put — by_source_uri misses on ""
        # and _find_ghost_by_title requires chunk_count == 0 — so that
        # very next re-put would mint a fresh duplicate, reproducing this
        # bead's own bug for the entire RDR-145 ghost population. Passing
        # source_uri here is what lets the SECOND-and-later re-put reach
        # this document via the by_source_uri lookup above instead.
        ghost = _find_ghost_by_title(reader, owner, title)
        if ghost is not None:
            # nexus-k54nk fix-round 1: same capture as the two reconcile
            # branches above, for symmetry — a ghost's meta.doc_id is
            # normally empty (chunk_count == 0), but capture whatever it
            # actually held before this call's own writer.update below.
            if pre_call_doc_id_out is not None:
                pre_call_doc_id_out["doc_id"] = (
                    ghost.meta or {}
                ).get("doc_id", "")
            writer.update(
                ghost.tumbler,
                physical_collection=collection_name,
                meta=stamp_for(ghost),
                source_uri=source_uri or "",
            )
            _log.debug(
                "catalog_store_hook_deduped",
                deduped_by="title", tumbler=str(ghost.tumbler),
            )
            return str(ghost.tumbler), False

        # KNOWN RESIDUAL (nexus-n90xg): a legacy NON-ghost row (chunk_count
        # > 0, source_uri="" — populated before nexus-sdp0u) whose chash is
        # no longer its put's chash (the row's meta.doc_id is stale, e.g. the
        # note's text changed, or a pre-Phase-4 16-char id) is unreachable by
        # all three lookups above, so its first post-fix re-put falls
        # through here and mints ONE bounded duplicate. A legacy row whose
        # meta.doc_id still is the text's chash IS reached, by the first
        # lookup, under its own title (nexus-6pfgb.31). The new document
        # carries the synthesized source_uri, so the second-and-later
        # re-puts converge onto it via by_source_uri. The legacy row itself
        # is collapsed by the nexus-n90xg one-shot backfill sweep, not here.
        #
        # nexus-vfef0: this is also the ONLY leg exposed to the genuinely
        # CONCURRENT first-put race the three prechecks above cannot close
        # (two callers both miss all three lookups and both reach here for
        # a brand-new (collection, title)). ``with_created=True`` surfaces
        # the engine's created-vs-matched wire signal so the race LOSER
        # reports ``created=False`` (its own tumbler-shaped return is
        # actually the WINNER's row) instead of the previously-hardcoded
        # ``True`` — the exact gap rollback_minted_catalog_entry's KNOWN
        # RESIDUAL documented.
        #
        # nexus-r1tnx round 2 (code-review finding): this call requests
        # ``with_created`` but deliberately does NOT run
        # ``find_cross_owner_conflict``/``announce_cross_owner_mint``
        # (path_ambiguity.py) the way the four file_path-keyed mint
        # branches do. Not applicable here: a knowledge doc's identity is
        # ``title`` + ``source_uri`` (this call passes no ``file_path`` at
        # all — it defaults to ``""`` on the wire), and
        # ``find_cross_owner_conflict`` is keyed on
        # ``find_all_by_file_path``, which has nothing to search for
        # against an empty path. There is no cross-OWNER file_path
        # collision this call site could ever mint a second document over.
        tumbler, created = writer.register(
            owner=owner, title=title, content_type="knowledge",
            physical_collection=collection_name,
            meta=stamp_for(None),
            source_uri=source_uri or "",
            with_created=True,
        )
        return str(tumbler), created
    except Exception as exc:  # noqa: BLE001 - best-effort post-store catalog hook must not crash caller; logged + audited
        # nexus-ou4tb: the "" return is indistinguishable from "no tumbler
        # assigned", so at DEBUG this was a silent non-registration. WARNING +
        # audit row so nx doctor can say how many documents are affected.
        # An anticipated failure (the catalog service down, a refused registration) prints no
        # traceback; anything else keeps its stack (see note_write.is_anticipated_failure).
        from nexus.catalog.note_write import _stack_unless_anticipated  # noqa: PLC0415 — deferred: note_write imports this module

        _log.warning("catalog_store_hook_failed", **_stack_unless_anticipated(exc))
        if error_out is not None:
            error_out["error"] = f"{type(exc).__name__}: {exc}"
        from nexus.hook_registry import record_catalog_hook_failure  # noqa: PLC0415 — deferred, avoids an import cycle

        record_catalog_hook_failure(
            source_path=doc_id or title or "", collection=collection_name or "",
            hook_name="catalog_store_hook", error=str(exc),
        )
        return "", False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally; a raising close AFTER a successful register would DISCARD the (tumbler, created=True) return (return-in-try + raising-finally semantics) and orphan the created-flag the nexus-b6enc C2 compensation depends on
                _log.warning("catalog_store_hook_writer_close_failed", exc_info=True)
        if reader is not None:
            try:
                reader._db.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally; close failure is non-critical and intentionally silent
                pass


def rollback_minted_catalog_entry(tumbler: str, *, original_error: str = "") -> bool:
    """Best-effort delete of a catalog row minted earlier IN THIS CALL
    (nexus-b6enc C2 ghost-register compensation).

    Every note producer registers the catalog row BEFORE the write
    (:func:`nexus.catalog.note_write.put_note`); when the write fails the
    just-minted row must not survive as a ghost
    (row + zero manifest + zero chunks — unrecoverable content loss for
    agent callers that drop MCP error strings). Callers invoke this ONLY
    when :func:`catalog_store_hook_tracked` reported ``created=True`` —
    a dedup hit must never be deleted.

    Fail-loud discipline: this compensation must never MASK the original
    put error, so it never raises — its own failure is logged at WARNING
    with *original_error* attached so both failures are visible.

    FIXED (nexus-vfef0, was a KNOWN RESIDUAL from the nexus-sdp0u round-1
    code review): ``created=True`` from :func:`catalog_store_hook_tracked`
    used to be reliable only for every SEQUENTIAL case (the
    ``by_source_uri`` / ghost-by-title prechecks close those), but NOT for
    a genuinely CONCURRENT first-put race on a brand-new ``(collection,
    title)``: two callers can both precheck-miss and both call
    ``writer.register()``; the engine's own upsert-on-``source_uri``
    idempotency (or its unique-constraint-loser retry) then hands the RACE
    LOSER back the WINNER's tumbler. Pre-fix, the wire response carried no
    created-vs-matched signal to tell the two apart, so the loser's call
    still reported ``created=True`` — if that loser's subsequent
    ``t3.put()`` then failed, this function was invoked against the
    WINNER's live, possibly-already-populated tumbler and deleted it.

    The engine's ``/doc/register`` and ``/doc/register_many`` responses now
    carry a per-call/per-entry ``created`` boolean (additive wire field;
    ``CatalogRepository.RegisterOutcome`` on the engine side), threaded
    through here via ``HttpCatalogClient.register(..., with_created=True)``
    — a race LOSER now reports ``created=False`` and this function is never
    invoked against it. The residual now spans ONLY engines predating this
    field: an older engine omits ``created`` entirely, and the client
    treats an absent field as ``created=True`` (the historical assumption,
    preserved for compatibility) — so against a pre-tag engine the race
    loser can still report ``created=True`` and this function can still be
    invoked against the winner's row. This narrows to zero once the
    deployed engine floor reaches the tag that shipped this field (floor
    bump rides a later, paired release — see AGENTS.md's paired-release
    choreography — not this bead).

    Returns True when the row was deleted.
    """
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred, avoids import cycle

        writer = make_catalog_writer()
        deleted = bool(writer.delete_document(Tumbler.parse(tumbler)))
        _log.warning(
            "store_put_ghost_register_compensated",
            tumbler=tumbler,
            deleted=deleted,
            original_error=original_error[:300],
        )
        return deleted
    except Exception:  # noqa: BLE001 — compensation must not mask the original t3.put error; both are logged
        _log.warning(
            "store_put_ghost_register_compensation_failed",
            tumbler=tumbler,
            original_error=original_error[:300],
            exc_info=True,
        )
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass


def restore_pre_call_stamp(
    catalog_doc_id: str, pre_call_doc_id: str, stamped_doc_id: str,
    pre_call_note_meta: dict[str, Any] | None = None,
    stamped_note_meta: dict[str, Any] | None = None,
) -> None:
    """Undo :func:`catalog_store_hook_tracked`'s own pre-write identity
    stamp on a refused or failed note write (nexus-k54nk fix-round 2).

    Every RECONCILE branch in :func:`catalog_store_hook_tracked` (chash-
    dedup, ``by_source_uri``, ghost-by-title) stamps *catalog_doc_id*'s
    document's ``meta.doc_id`` to *stamped_doc_id* (``doc_ids[0]``, the
    first chash of the note about to be written) BEFORE the write is
    attempted. Left in place after a write that did not land, that stamp
    names a chunk that was never written.

    This function exists because the one remaining consumer of
    :func:`nexus.indexer_utils.live_note_chashes`, the ``mcp_infra``
    supersede sweeps (``nx t3 gc`` and the prune sites no longer read it),
    reads a document's ``meta.doc_id`` as "a manifest-less note owns this
    chash" and so protects a chash the stamp names from deletion. That guard
    is permanent (RDR-192 Step 11 retains it by decision, Sam 2026-10-02), so
    this function, ``pre_call_doc_id_out`` and their test stay with it. A
    dangling stamp over-retains: a later note that owns and then drops that
    chash has it kept until the reaper collects it.

    Restores *catalog_doc_id*'s ``meta.doc_id`` to *pre_call_doc_id*, the
    same document's identity as it stood before this call touched it, or
    ``""`` (an unstamped document's own shape, per every reconcile
    branch's own ``.get("doc_id", "")`` default) when this call minted a
    brand-new row with no prior identity and the row's removal failed.
    :func:`nexus.catalog.note_write.put_note` calls it on every failure
    leg that leaves the row standing.

    COMPARE-AND-SET, not a blind write: only when the document's CURRENT
    ``meta.doc_id`` still equals *stamped_doc_id* (this call's own,
    about-to-be-stale value) is it restored. A concurrent re-put that has
    already re-stamped the SAME document (its own
    ``catalog_store_hook_tracked`` ran again before this restore ran) has moved ``meta.doc_id`` on to ITS OWN new value —
    unconditionally overwriting that with this call's now-stale
    *pre_call_doc_id* would silently undo a newer, possibly-still-landing
    call's own stamp. Not a true compare-and-swap (no such primitive on
    the wire — a race between this read and the write below remains);
    this narrows the window, it does not close it.

    *pre_call_note_meta* (``pre_call_doc_id_out["note_meta"]``, nexus-6pfgb.5) is
    what the note keys (tags, category, ttl_days, source_agent) held before
    this call wrote them; it goes back in the same update, so a failed
    re-put leaves the old note's keys whole. *stamped_note_meta*
    (``pre_call_doc_id_out["note_meta_stamped"]``) is what this call wrote there:
    the restore runs only while the entry still holds every one of those values,
    so a failed re-put racing a successful retag of the same content (same
    ``doc_id``, different keys) does not overwrite the newer keys.

    Best-effort like every other step in
    :func:`nexus.catalog.note_write.put_note`'s failure path: a lookup or
    write failure here is logged and never raises — the caller's own
    error is what must surface, never masked by this cleanup step.
    """
    if not catalog_doc_id or not stamped_doc_id:
        return
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load

    reader = make_catalog_reader()
    try:
        current = reader.resolve(catalog_doc_id)
    except Exception:  # noqa: BLE001 — best-effort: cannot prove the stamp is stale, leave it
        _log.warning(
            "store_put_rollback_stamp_restore_lookup_failed",
            catalog_doc_id=catalog_doc_id, exc_info=True,
        )
        current = None
    finally:
        try:
            reader._db.close()
        except Exception:  # noqa: BLE001 — service-mode reader has no SQLite handle (property raises)
            pass
    if current is None:
        return
    if (current.meta or {}).get("doc_id", "") != stamped_doc_id:
        # A newer stamp landed since this call's own write (a concurrent
        # re-put reconciled onto the same document) — never clobber it.
        return
    cur_meta = current.meta or {}
    if any(cur_meta.get(key) != value for key, value in (stamped_note_meta or {}).items()):
        # Same doc_id, different note keys: a concurrent put of the same content landed its own
        # keys after this call's write (nexus-6pfgb.5) — never clobber them either.
        return
    try:
        writer = make_catalog_writer()
        writer.update(catalog_doc_id, meta={"doc_id": pre_call_doc_id, **(pre_call_note_meta or {})})
    except Exception:  # noqa: BLE001 — best-effort restore; the caller's own store_put error must still surface
        _log.warning(
            "store_put_rollback_stamp_restore_failed",
            catalog_doc_id=catalog_doc_id, exc_info=True,
        )
        return
    _log.info(
        "store_put_rollback_stamp_restored",
        catalog_doc_id=catalog_doc_id,
        restored_to=pre_call_doc_id or "(cleared)",
    )


def _retract_manifest_rows_for_chash(
    reader, writer, entry, chash: str, *, expected_collection: str | None = None,
) -> None:
    """Explicitly retract *chash*'s own manifest row(s) from *entry*'s
    document BEFORE the caller tombstones it and deletes the T3 chunk
    (nexus-mmkqe / nexus-rnqbw, RDR-191 GATE-2).

    GHOST-DOC GUARD (nexus-d9fwj): *entry* can be a ghost/sourceless
    document registered with an EMPTY ``physical_collection`` (a
    documented live population — see ``health.py``'s null-collection
    census). Post-GATE-2, ``write_manifest`` rejects a blank/None
    ``collection`` client-side with ``ValueError`` — a regression vs the
    pre-GATE-2 engine-inferred-collection behavior, where a blank
    collection here was silently tolerated. Both of this helper's
    callers previously let that ``ValueError`` reach their own
    exception handling: ``store_delete_catalog_cleanup``'s broad except
    swallowed it and skipped ``delete_document`` entirely (the whole
    catalog cleanup abandoned), and ``reap_catalog_manifest_for_chashes``'s
    narrow except logged and let the tombstone proceed but left the
    manifest row in place, keeping the chunk anti-join-protected.

    When *entry* has no ``physical_collection``, *expected_collection*
    (the caller-supplied collection scope, when one is available on
    that call) is used as a fallback so the retraction can still
    succeed with a real, known-good collection value. When neither is
    available, the retraction is skipped outright (not attempted, not
    raised) and logged at WARNING naming the tumbler and the reason —
    the manifest row is left in place, exactly as if retraction had
    failed, but without corrupting either caller's outer control flow.

    TWO-TIER PROTECTION (T2 nexus/rdr-191-dangling-definition-of-record
    [22364], amended 2026-08-12): ``PgVectorRepository#delete``'s anti-join
    now protects a chunk referenced by a TOMBSTONED owner's manifest row
    unconditionally (definition class b) -- correct for an INDEPENDENT
    deleter (GC, quarantine, a different session), none of which may
    destroy content another document's manifest still names, tombstoned or
    not. That is exactly what breaks the SAME-CALL tombstone-then-delete
    pattern this helper's three callers use (``nx store delete --id``, MCP
    ``store_delete``, ``HttpVectorClient.expire()`` -- all funnel through
    either this function or :func:`reap_catalog_manifest_for_chashes`,
    which also calls it): tombstoning *entry* no longer implicitly
    unblocks deleting ITS OWN chunk, because the freshly-tombstoned
    manifest row is now class-b-protected too.

    The fix is NOT to weaken the anti-join back toward tombstone-blindness
    -- that would reopen the independent-deleter hole nexus-mmkqe closed --
    it is to make the OWNER's own reap explicit: retract the specific
    manifest row(s) naming *chash* here, via a full-manifest
    read-filter-rewrite (``write_manifest`` replace, not a partial delete
    primitive -- none exists on the caller-facing write surface), so by the
    time the T3 delete runs there is no manifest row left for the
    anti-join to protect. Every OTHER document's manifest row referencing
    the same chash (RDR-108 collapse-by-design) is untouched and keeps
    protecting it exactly as designed -- this only ever touches *entry*'s
    own manifest.

    Raises on failure (a manifest read/write error, a closed handle, ...)
    rather than swallowing -- propagates to the caller's own try/except,
    matching whichever fail-loud or best-effort contract that caller
    already has. This helper carries no contract of its own to preserve.
    """
    tumbler_str = str(entry.tumbler)
    current_rows = reader.get_manifest(tumbler_str)
    remaining = [manifest_row_wire(r) for r in current_rows if r.chash != chash]
    if len(remaining) == len(current_rows):
        return  # chash was not present in this entry's manifest -- nothing to retract
    collection = entry.physical_collection or expected_collection
    if not collection:
        _log.warning(
            "manifest_retraction_skipped_blank_collection",
            tumbler=tumbler_str, chash=chash,
            reason=(
                "ghost/sourceless document has no physical_collection and "
                "no expected_collection fallback was supplied — retraction "
                "skipped; the manifest row is left in place"
            ),
        )
        return
    writer.write_manifest(tumbler_str, remaining, collection=collection)


@dataclass(frozen=True)
class TitleReap:
    """What :func:`reap_catalog_documents_by_title` did.

    *documents* are ``(tumbler, title)`` of every document tombstoned;
    *chashes* the chunks their manifests named, which the caller then asks
    T3 to delete; *failures* ``(tumbler, error)`` for any document left
    live; *held* the chunks of those failed documents, which the caller
    must leave alone (no other path may reap a document this one refused);
    *stale* ``(tumbler, title)`` of each lapsed note the TTL sweep left alone
    because a chunk row of its own records a later put than its catalog TTL
    keys do (nexus-6pfgb.30). Their chunks are in *held* too.
    """

    documents: tuple[tuple[str, str], ...]
    chashes: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]
    held: tuple[str, ...] = ()
    stale: tuple[tuple[str, str], ...] = ()


def _is_title_reap_candidate(entry, title: str, collection: str) -> bool:
    """The documents ``--title`` names: store_put-origin (knowledge, no
    file_path), titled *title*, in *collection* or a ghost with no
    collection (the owner scope :func:`resolve_knowledge_doc_for_chash`
    uses)."""
    return (
        (getattr(entry, "title", "") or "") == title
        and entry.content_type == "knowledge"
        and not entry.file_path
        and (not entry.physical_collection or entry.physical_collection == collection)
    )


def title_reap_candidates(
    reader, collection: str, title: str, chashes: list[str] = (),
    present: Callable[[str], bool] | None = None,
) -> list:
    """Every document :func:`reap_catalog_documents_by_title` would reap.

    Documents in *collection* come from ``list_by_collection``. A ghost has
    no collection to list it under, so ghosts come from two places: the
    manifests of *chashes* (the chunks titled *title* in T3), and an exact
    catalog title lookup. The second does not depend on the chunk rows'
    own titles, which are their last writer's, so a ghost whose shared chunk
    another note wrote last is still found. A ghost from the title lookup is
    kept only when *present* confirms one of its chunks is in *collection*,
    because a ghost has no collection to scope it by; without *present*
    that source is skipped.
    """
    found = {
        str(e.tumbler): e
        for e in (reader.list_by_collection(collection) or [])
        if _is_title_reap_candidate(e, title, collection)
    }
    if chashes:
        try:
            owners = reader.docs_for_chashes(list(chashes)) or {}
        except httpx.HTTPStatusError as exc:
            # 400: an id that is not a hex chash (a legacy id) can own
            # nothing; a miss, as in resolve_knowledge_doc_for_chash.
            if exc.response.status_code != 400:
                raise
            owners = {}
        for tumblers in owners.values():
            for t in tumblers:
                if str(t) in found:
                    continue
                e = reader.resolve(t)
                if e is not None and not e.physical_collection and _is_title_reap_candidate(e, title, collection):
                    found[str(t)] = e
    if present is not None:
        for e in reader.find_by_title_exact(title, content_type="knowledge") or []:
            t = str(e.tumbler)
            if t in found or e.physical_collection or not _is_title_reap_candidate(e, title, collection):
                continue
            if any(present(r.chash) for r in reader.get_manifest(t)):
                found[t] = e
    return list(found.values())


def reap_catalog_documents_by_title(
    collection: str, title: str, chashes: list[str] = (),
    present: Callable[[str], bool] | None = None,
) -> TitleReap | None:
    """Tombstone every document ``--title`` names (:func:`title_reap_candidates`),
    retracting its own manifest first (nexus-sis0m.5).

    ``nx store delete --title`` names documents, so they are found by catalog
    title rather than through their chunks. The chash route
    (:func:`reap_catalog_manifest_for_chashes`) refuses a chash two
    documents share, correctly for a delete by chunk id, and so left a note
    whose body another note duplicates live. Only these documents'
    manifests are rewritten; every other document naming the same chash
    keeps protecting it through the engine's delete anti-join.

    All or nothing per document. The whole manifest is retracted in ONE
    ``write_manifest`` (an atomic replace), then the document is tombstoned.
    Retracting chash by chash, as the chash route does, could fail part way
    and leave a live document with some of its rows stripped. If the
    tombstone fails after the retraction, the original manifest is written
    back so the document is left as it was. Either failure lands the
    document in *failures*, live and intact; a failed write-back is named.

    ``None`` when the catalog is unavailable, so the caller can fall back to
    the chunk-title path.
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = make_catalog_reader()
    if reader is None:
        return None
    writer = None
    try:
        entries = title_reap_candidates(reader, collection, title, list(chashes), present)
        if not entries:
            return TitleReap((), (), ())
        writer = make_catalog_writer()
        return _reap_documents(reader, writer, entries, collection, title=title)
    finally:
        if writer is not None:
            writer.close()
        reader.close()


def _reap_documents(
    reader, writer, entries, collection: str, *, title: str = "",
    still_due: Callable[[Any], bool] | None = None,
    guard: Callable[[Any, list[str]], tuple[str, str] | None] | None = None,
) -> TitleReap:
    """Tombstone each of *entries*, retracting its own manifest first; the one note delete path
    behind ``nx store delete --title`` and the TTL sweep (nexus-6pfgb.15).

    All or nothing per document, as :func:`reap_catalog_documents_by_title` documents. A document
    that cannot be reaped is left live and intact, named in ``failures``, and its chunks in ``held``.

    *still_due*, when given, is asked of each document's entry freshly read just before its
    retraction; a document that is gone, or for which it says no, is skipped untouched (not counted,
    its chunks not returned). The TTL sweep passes it so a note re-put since the listing (made
    permanent, or its clock restarted) is not retired from the stale entry. There is no
    compare-and-set on the catalog, so this narrows the window to one read and does not close it.

    *guard*, when given, is asked ``guard(fresh_entry, chashes)`` after *still_due* and before the
    retraction, with the chashes of the document's manifest. It returns ``None`` to go ahead, or
    ``(kind, detail)`` to leave the document untouched: ``"stale"`` (the TTL sweep found a chunk row
    that records a later put than the catalog's TTL keys, nexus-6pfgb.30) is reported in ``stale``;
    any other kind (``"unreadable"``: the rows could not be read, so the answer is unknown) is a
    failure. Either way the document stays live and its chunks are held.
    """
    documents: list[tuple[str, str]] = []
    reaped_chashes: dict[str, None] = {}
    failures: list[tuple[str, str]] = []
    held: dict[str, None] = {}
    stale: list[tuple[str, str]] = []
    for entry in entries:
        tumbler = str(entry.tumbler)
        fresh = entry
        if still_due is not None:
            fresh = reader.resolve_many([tumbler]).get(tumbler)
            if fresh is None or not still_due(fresh):
                continue
        rows = reader.get_manifest(tumbler)
        original = [manifest_row_wire(r) for r in rows]
        if guard is not None:
            verdict = guard(fresh, [r["chash"] for r in original])
            if verdict is not None:
                kind, detail = verdict
                held.update(dict.fromkeys(r["chash"] for r in original))
                if kind == "stale":
                    stale.append((tumbler, getattr(entry, "title", "") or title))
                else:
                    failures.append((tumbler, detail))
                continue
        row_collection = entry.physical_collection or collection
        try:
            if original:
                writer.write_manifest(tumbler, [], collection=row_collection)
        except Exception as exc:  # noqa: BLE001 — nothing changed; reported, the caller fails loud
            _log.warning("catalog_title_reap_retract_failed", tumbler=tumbler, exc_info=True)
            failures.append((tumbler, f"manifest not retracted ({type(exc).__name__}: {exc})"))
            held.update(dict.fromkeys(r["chash"] for r in original))
            continue
        try:
            writer.delete_document(entry.tumbler)
        except Exception as exc:  # noqa: BLE001 — restore below; reported, the caller fails loud
            _log.warning("catalog_title_reap_tombstone_failed", tumbler=tumbler, exc_info=True)
            detail = f"not tombstoned ({type(exc).__name__}: {exc})"
            if original:
                try:
                    writer.write_manifest(tumbler, original, collection=row_collection)
                except Exception as restore_exc:  # noqa: BLE001 — named so the operator can repair it
                    _log.error("catalog_title_reap_restore_failed", tumbler=tumbler, exc_info=True)
                    detail += (
                        f"; its manifest could NOT be restored ({type(restore_exc).__name__}) "
                        f"and is empty: re-run the delete, or repair with nx catalog reconcile"
                    )
            failures.append((tumbler, detail))
            held.update(dict.fromkeys(r["chash"] for r in original))
            continue
        documents.append((tumbler, getattr(entry, "title", "") or title))
        reaped_chashes.update(dict.fromkeys(r["chash"] for r in original))
    return TitleReap(tuple(documents), tuple(reaped_chashes), tuple(failures), tuple(held), tuple(stale))


def lapsed_note_documents(reader, collection: str, *, now: datetime) -> list:
    """The live store_put-origin notes in *collection* whose own TTL has run out at *now*
    (nexus-6pfgb.15).

    Each is judged by its own catalog entry (:mod:`nexus.catalog.note_ttl`): ``meta.ttl_days``
    counted from ``meta.put_at``. A chunk row's ``ttl_days`` and ``indexed_at`` are the last writer's
    and are never read here. A note with ``ttl_days`` absent or ``0`` is permanent. Reads one catalog
    listing of the collection, so the cost follows the collection's document count.
    """
    from nexus.catalog import note_ttl  # noqa: PLC0415 — sibling module, deferred like the other catalog imports here

    return [
        e for e in (reader.list_by_collection(collection) or [])
        if e.content_type == "knowledge" and not e.file_path
        and (e.physical_collection or collection) == collection
        and note_ttl.lapsed(e.meta, e.indexed_at, now=now)
    ]


def reap_lapsed_note_documents(
    collection: str, *, now: datetime,
    row_reader: Callable[[list[str]], dict[str, dict]] | None = None,
) -> TitleReap | None:
    """Tombstone every note in *collection* whose own TTL has lapsed, each through the note delete
    path of ``nx store delete --title`` (manifest retracted, then the document tombstoned; all or
    nothing per note), and return what that did (nexus-6pfgb.15).

    The caller then asks T3 to delete the returned ``chashes``; the engine's delete anti-join keeps
    any chunk another live document still holds, so a co-owner keeps serving it. ``None`` when the
    catalog is unavailable.

    *row_reader*, when given, maps chashes to their chunk rows' metadata (a chash with no row is absent).
    Before a lapsed note is retired its own rows are read through it, and the note is left alone,
    its chunks held, when one records a put the catalog's TTL keys do not
    (:func:`~nexus.catalog.note_ttl.catalog_ttl_stale`, nexus-6pfgb.30: a re-put by an older client, or a
    writer that bypasses the catalog hook, leaves the catalog entry stale). If the rows cannot be read
    the note is not retired and is reported as a failure: no read, no deletion.
    """
    from nexus.catalog import note_ttl  # noqa: PLC0415 — sibling module, deferred like the other catalog imports here
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = make_catalog_reader()
    if reader is None:
        return None
    writer = None
    try:
        entries = lapsed_note_documents(reader, collection, now=now)
        if not entries:
            return TitleReap((), (), ())
        writer = make_catalog_writer()
        return _reap_documents(
            reader, writer, entries, collection,
            still_due=lambda e: note_ttl.lapsed(e.meta, e.indexed_at, now=now),
            guard=None if row_reader is None else _row_guard(row_reader),
        )
    finally:
        if writer is not None:
            writer.close()
        reader.close()


def _row_guard(row_reader: Callable[[list[str]], dict[str, dict]]):
    """The :func:`_reap_documents` guard of the TTL sweep: read the note's own chunk rows and refuse to
    retire it when they show a put its catalog TTL keys do not (nexus-6pfgb.30)."""
    from nexus.catalog import note_ttl  # noqa: PLC0415 — sibling module, deferred like the other catalog imports here

    def guard(entry, chashes: list[str]) -> tuple[str, str] | None:
        try:
            rows = row_reader(chashes) if chashes else {}
        except Exception as exc:  # noqa: BLE001 — fail closed: unknown is not "no later put"
            _log.warning("catalog_ttl_guard_rows_unreadable", tumbler=str(entry.tumbler), exc_info=True)
            return ("unreadable", f"its chunk rows could not be read ({type(exc).__name__}: {exc}), so a later put cannot be ruled out")
        if note_ttl.catalog_ttl_stale(
            entry.meta, entry.indexed_at, list(rows.values()),
            owner_tumbler=entry.tumbler, owner_title=getattr(entry, "title", "") or "",
        ):
            return ("stale", "")
        return None

    return guard


def _docs_for_chashes_isolating(reader, chashes: list[str]) -> dict[str, list]:
    """``reader.docs_for_chashes`` over *chashes* in pages of 300; a page the engine rejects with 400
    (one id that is not hex, a legacy id, fails the whole batch) is retried id by id, and an id that
    still 400s owns nothing. Any other HTTP failure raises."""
    page = 300
    owners: dict[str, list] = {}
    for i in range(0, len(chashes), page):
        batch = chashes[i:i + page]
        try:
            owners.update(reader.docs_for_chashes(batch) or {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            for chash in batch:  # one bad id 400s the whole batch: isolate it
                try:
                    owners.update(reader.docs_for_chashes([chash]) or {})
                except httpx.HTTPStatusError as one:
                    if one.response.status_code != 400:
                        raise
    return owners


def live_note_owners(reader, collection: str, chashes: list[str]) -> dict[str, list]:
    """``chash -> [CatalogEntry, ...]`` of the LIVE documents in *collection* (or ghosts, which have
    none) whose manifests name each chash; a chash nobody holds maps to ``[]`` (nexus-6pfgb.15).

    Unlike :func:`live_holders_of_chashes` this keeps the entries, so a caller can read each owner's
    ``meta``, and it raises on a catalog failure: the expiry sweep must not treat "could not look"
    as "no owner". A chash the wire rejects as not hex (a legacy id) owns nothing.
    """
    out: dict[str, list] = {c: [] for c in chashes}
    if not chashes:
        return out
    owners = _docs_for_chashes_isolating(reader, chashes)
    tumblers = sorted({str(t) for ts in owners.values() for t in ts})
    entries = reader.resolve_many(tumblers) if tumblers else {}
    for chash, ts in owners.items():
        out[chash] = [
            entries[str(t)] for t in ts
            if str(t) in entries and (entries[str(t)].physical_collection or "") in ("", collection)
        ]
    return out


def live_holders_of_chashes(collection: str, chashes: list[str]) -> dict[str, list[tuple[str, str]]]:
    """``chash -> [(tumbler, title), ...]`` of the LIVE documents in
    *collection* (or ghosts, which have none) whose manifests name each
    chash; chashes nobody holds are omitted.

    Resolves only the owning documents (``docs_for_chashes`` then
    ``resolve_many``, which skips tombstones), so the cost follows the owner
    count, not the collection's size; ``store_get`` calls it per lookup.
    Best-effort: ``{}`` when the catalog is unavailable or unreadable, since
    this only names who holds a chunk.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not chashes:
        return {}
    reader = make_catalog_reader()
    if reader is None:
        return {}
    try:
        owners = reader.docs_for_chashes(list(chashes)) or {}
        tumblers = sorted({str(t) for ts in owners.values() for t in ts})
        entries = reader.resolve_many(tumblers) if tumblers else {}
        out: dict[str, list[tuple[str, str]]] = {}
        for chash, ts in owners.items():
            holders = [
                (str(t), entries[str(t)].title or "")
                for t in ts
                if str(t) in entries
                and (entries[str(t)].physical_collection or "") in ("", collection)
            ]
            if holders:
                out[chash] = holders
        return out
    except Exception:  # noqa: BLE001 — naming holders is best-effort
        _log.debug("live_holders_lookup_failed", exc_info=True)
        return {}
    finally:
        reader.close()


def live_owner_entries(collection: str, chashes: list[str]) -> dict[str, list[Any]] | None:
    """``chash -> [CatalogEntry, ...]`` of the LIVE documents in *collection* holding each chash, from ONE
    ``docs_for_chashes`` and ONE ``resolve_many`` (nexus-6pfgb.13): the owner lookup every note display
    (``store_get``, ``store_list``, ``nx store get/list``) shares, so a page of chunks costs one catalog
    read however many fields it shows. A chash nobody holds maps to ``[]``.

    ``None`` (not ``{}``) when the catalog is unavailable or unreadable: "could not look" is not "no
    owner", and a caller must not read it as a ghost chunk whose row is the only record. A display is
    best-effort, so this never raises.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not chashes:
        return {}
    try:
        reader = make_catalog_reader()
    except Exception:  # noqa: BLE001 — decorating a display is best-effort
        # Warning, not debug: the display then falls back to the chunk row's values (the last writer's)
        # and omits the Degraded line, which reads like a clean document; the log is the only place it shows.
        _log.warning("live_owner_entries_failed", collection=collection, chashes=len(chashes), exc_info=True)
        return None
    if reader is None:
        return None
    try:
        return live_note_owners(reader, collection, list(chashes))
    except Exception:  # noqa: BLE001 — decorating a display is best-effort
        _log.warning("live_owner_entries_failed", collection=collection, chashes=len(chashes), exc_info=True)
        return None
    finally:
        reader.close()


def owner_holders(owners: list[Any]) -> list[tuple[str, str]]:
    """``[(tumbler, title), ...]`` of *owners* (:func:`live_holders_of_chashes`' shape). Reads attributes
    with defaults, so a hand-built stub entry works."""
    return [(str(getattr(e, "tumbler", "")), getattr(e, "title", "") or "") for e in owners]


def owner_tags(entries: list[Any], *, title: str | None = None) -> list[str] | None:
    """The tags the owners of one chunk record in their catalog ``meta`` (nexus-6pfgb.38, RDR-236 RF-17):
    the union in first-seen order, each owner's array (a legacy string reads as its split array).
    ``None`` when no owner carries a ``tags`` key at all (an indexed file, a note stored before
    nexus-6pfgb.5), which is the caller's cue to consider the row; an owner whose array is empty (the
    retraction) counts as carrying the key, so the answer is ``[]`` and a stale row stays hidden.
    *title*, when given, keeps only owners with that title."""
    tags: list[str] = []
    keyed = False
    for entry in entries:
        if title is not None and (getattr(entry, "title", "") or "") != title:
            continue
        meta = getattr(entry, "meta", None) or {}
        if "tags" not in meta:
            continue
        keyed = True
        tags.extend(t for t in split_note_tags(meta["tags"]) if t not in tags)
    return tags if keyed else None


def catalog_tags_for_chunks(
    collection: str, chashes: list[str], *,
    holders: dict[str, list[tuple[str, str]]] | None = None, title: str | None = None,
    owners: dict[str, list[Any]] | None = None,
) -> dict[str, list[str]]:
    """``chash -> tags`` for the chunks of *collection* whose live holders record tags in their catalog
    ``meta`` (nexus-6pfgb.38, RDR-236 RF-17): the tags half of :func:`note_display`, kept as the
    tested entry for the union rule (:func:`owner_tags`).

    A chunk row carries the last writer's tags; each holding note carries its own, as an array. A chunk
    is OMITTED when no holder carries a ``tags`` key at all, which is the caller's cue to fall back to
    the row. *title*, when given, keeps only holders with that title. *owners* is what
    :func:`live_owner_entries` already returned (saves the lookup); *holders* is what
    :func:`live_holders_of_chashes` returned, which still costs the resolve.

    Best-effort: ``{}`` when the catalog is unavailable or unreadable, since this only decorates a listing.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    if not chashes:
        return {}
    if owners is None and holders is None:
        owners = live_owner_entries(collection, list(chashes))
        if owners is None:
            return {}
    elif owners is None:
        try:
            reader = make_catalog_reader()
        except Exception:  # noqa: BLE001 — decorating a listing is best-effort
            _log.debug("catalog_tags_reader_unavailable", exc_info=True)
            return {}
        if reader is None:
            return {}
        try:
            names = {c: [t for t, _title in hs] for c, hs in (holders or {}).items()}
            tumblers = sorted({t for ts in names.values() for t in ts})
            entries = reader.resolve_many(tumblers) if tumblers else {}
            owners = {
                c: [
                    entries[t] for t in ts
                    if t in entries and (entries[t].physical_collection or "") in ("", collection)
                ]
                for c, ts in names.items()
            }
        except Exception:  # noqa: BLE001 — decorating a listing is best-effort
            _log.debug("catalog_tags_lookup_failed", exc_info=True)
            return {}
        finally:
            reader.close()
    out: dict[str, list[str]] = {}
    for chash in chashes:
        tags = owner_tags(owners.get(chash, []), title=title)
        if tags is not None:
            out[chash] = tags
    return out


def format_note_tags(tags: list[str]) -> str:
    """A tag array as the display text: ``alpha, one``."""
    return ", ".join(tags)


@dataclass(frozen=True)
class NoteDisplay:
    """What ``store_get`` / ``store_list`` print for one chunk (:func:`note_display`): *title*, *tags*
    (display text), *indexed* (``YYYY-MM-DD``) and *ttl* (``permanent``, ``expires YYYY-MM-DD``,
    ``ttl Nd``, or ``unknown``). An empty string is a field with nothing to say."""

    title: str
    tags: str
    indexed: str
    ttl: str


def _agreed(values: list[str | None]) -> str | None:
    """The one value every entry of *values* states, else ``None`` (any is missing, or they differ)."""
    if not values or any(v is None for v in values) or len(set(values)) != 1:
        return None
    return values[0]


def note_display(
    owners: list[Any] | None, *, row: Mapping[str, Any], row_title: str, row_tags: str,
    row_ttl_days: Any, row_indexed_at: str, title: str | None = None,
) -> NoteDisplay:
    """The title, tags, expiry and index date of one chunk as its OWNING documents state them (nexus-6pfgb.13,
    RDR-236 Phase 2). A T3 chunk row is one per chunk text: its ``title``, ``tags``, ``ttl_days`` and
    ``indexed_at`` are the last writer's, so a note whose text another note wrote later would print that
    note's values. Each owner's catalog entry holds its own: ``meta.tags``, ``meta.ttl_days`` with its
    clock ``meta.put_at`` (:mod:`nexus.catalog.note_ttl`, the module the expiry sweep reads), and the
    ``indexed_at`` column.

    *owners* are the live entries holding the chunk (:func:`live_owner_entries`; ``None`` when the
    catalog could not be read), *title* keeps only owners with that title (a read asked by title). Several
    owners: tags are their union (:func:`owner_tags`); the title is theirs joined; the index date and the
    expiry are shown only when every owner states the same one, never the row's. The row (*row* is the
    chunk row, *row_title* and the others its values, read by the caller) speaks only where no catalog
    value exists and the chunk has at most one owner: a ghost or an unreadable catalog (no owner), or a
    sole owner with no value of its own (an indexed file's tags until Phase 3, a note stored before
    nexus-6pfgb.5). A keyless note's row TTL is read as the note's only when the note wrote the row
    (:func:`~nexus.catalog.note_ttl.row_written_by`): a TTL a departed co-owner left must not appear as
    this note's. A keyless owner among several has no value to agree with, so the field is blank.
    """
    from nexus.catalog import note_ttl  # noqa: PLC0415 — sibling module, deferred like the other catalog imports here
    from nexus.context_annotations import owner_titles  # noqa: PLC0415 — deferred for startup cost
    from nexus.metadata_schema import ttl_listing_label  # noqa: PLC0415 — deferred for startup cost

    live = list(owners or [])
    held = [e for e in live if title is None or (getattr(e, "title", "") or "") == title]
    # Sole ownership is judged over ALL live owners, before the title filter: with owners A and B sharing a
    # chunk, a read asked for A's title still has a row that B may have written (Phase 2 review H2). With
    # none (a ghost, an unreadable catalog) the row speaks; with one it speaks only if that one is asked for.
    row_may_speak = len(live) <= 1 and (not live or len(held) == 1)

    shown_title = title if title is not None else (
        owner_titles([getattr(e, "title", "") or "" for e in held]) if held else row_title
    )

    keyed_tags = owner_tags(held, title=title)
    if keyed_tags is not None:
        tags = format_note_tags(keyed_tags)
    elif not row_may_speak:
        tags = ""
    elif held and not note_ttl.row_may_give_tags(
        getattr(held[0], "tumbler", ""), row if isinstance(row, dict) else {}, owner_title=getattr(held[0], "title", "") or "",
    ):
        tags = ""  # a row another note wrote is not this note's, as in the recovery-bundle export
    else:
        tags = row_tags

    stamps: list[str | None] = []
    for e in held:
        stamp = getattr(e, "indexed_at", "") or ""
        stamps.append(stamp or None)
    if not live:
        indexed_full: str | None = row_indexed_at or None
    elif not held:
        indexed_full = None
    elif len(held) == 1:
        indexed_full = stamps[0] or (row_indexed_at if row_may_speak else "") or None
    else:
        parsed = [note_ttl._parse_iso(s_) if s_ else None for s_ in stamps]  # noqa: SLF001 — one parser for the catalog clock
        indexed_full = stamps[0] if parsed[0] is not None and len({*parsed}) == 1 else None
    indexed = (indexed_full or "")[:10]

    labels: list[str | None] = []
    for e in held:
        meta = getattr(e, "meta", None) or {}
        if note_ttl.has_own_keys(meta):
            labels.append(ttl_listing_label(
                note_ttl.note_ttl_days(meta), note_ttl.clock_start(meta, getattr(e, "indexed_at", "") or ""),
            ))
        elif len(held) == 1 and note_ttl.row_written_by(
            getattr(e, "tumbler", ""), row, owner_title=getattr(e, "title", "") or "",
        ):
            labels.append(ttl_listing_label(row_ttl_days, row_indexed_at))
        else:
            labels.append(None)
    ttl = ttl_listing_label(row_ttl_days, row_indexed_at) if not live else (_agreed(labels) or "unknown")

    return NoteDisplay(title=shown_title, tags=tags, indexed=indexed, ttl=ttl)


def _held(owners: list[Any] | None, title: str | None) -> list[Any]:
    """The owners a read about one note looks at: all of *owners*, or only those titled *title*."""
    return [e for e in (owners or []) if title is None or (getattr(e, "title", "") or "") == title]


def extraction_facts(owners: list[Any] | None, *, title: str | None = None) -> list[dict[str, Any]]:
    """The extraction provenance each owner of a chunk states in its catalog ``meta`` (nexus-6pfgb.16,
    RDR-236 Phase 2), one dict per owner that states any: ``tumbler``, ``title``, ``extraction_method``
    (``""`` when it states none), ``quality_gate_overridden`` (``True`` / ``False`` as written, ``None`` when
    the key is absent: absent is UNKNOWN, an explicit ``False`` is "not overridden") and
    ``extraction_source`` (an absent source is ``file``). *title* keeps only owners with that title.

    These are DOCUMENT facts: a chunk row is one per chunk text, so its ``extraction_method`` and
    ``quality_gate_overridden`` are the last writer's, and a clean re-index of one document used to erase
    the marker of another that shares the chunk. The row is not read."""
    out: list[dict[str, Any]] = []
    for e in _held(owners, title):
        meta = getattr(e, "meta", None) or {}
        method = str(meta.get("extraction_method") or "")
        flag = meta.get("quality_gate_overridden")
        override = flag if isinstance(flag, bool) else None
        source = str(meta.get("extraction_source") or "")
        if not (method or override is not None or source):
            continue
        out.append({
            "tumbler": str(getattr(e, "tumbler", "")), "title": getattr(e, "title", "") or "",
            "extraction_method": method, "quality_gate_overridden": override,
            "extraction_source": source or "file",
        })
    return out


def _fact_lines(prefix: str, facts: list[dict[str, Any]], held: int, value: Callable[[dict[str, Any]], str]) -> list[str]:
    """``prefix + value`` for a fact every one of the *held* owners states alike (one unlabelled line), else one
    line per stating owner labelled with its title (its tumbler when it has none). ``value`` returns ``""``
    for an owner that has nothing to show."""
    shown = [(f, v) for f in facts if (v := value(f))]
    if not shown:
        return []
    if len(shown) == held and len({v for _f, v in shown}) == 1:
        return [f"{prefix}{shown[0][1]}"]
    return [f"{prefix}{f['title'] or f['tumbler']}: {v}" for f, v in shown]


def extraction_lines(owners: list[Any] | None, *, title: str | None = None) -> list[str]:
    """The ``Extractor:`` / ``Gate:`` / ``Source:`` lines ``store_get`` and ``nx store get`` print, from the
    chunk's owning documents' catalog ``meta`` (nexus-6pfgb.16). A fact every owner states alike prints once;
    owners that differ print one labelled line each. ``Gate:`` appears only for an owner that overrode the
    quality gate and ``Source:`` only for text that did not come from a file (an absent source is a file).
    No owner states a method, or the catalog could not be read (*owners* ``None``): no line, never the chunk
    row's value, which is the last writer's. A document indexed before nexus-6pfgb.6 gets its keys from
    ``nx catalog backfill-meta`` (nexus-6pfgb.10) or its next index run."""
    held = _held(owners, title)
    facts = extraction_facts(held)
    return [
        *_fact_lines("Extractor:  ", facts, len(held), lambda f: f["extraction_method"]),
        *_fact_lines("Gate:       ", facts, len(held), lambda f: "overridden" if f["quality_gate_overridden"] else ""),
        *_fact_lines("Source:     ", facts, len(held), lambda f: "" if f["extraction_source"] == "file" else f["extraction_source"]),
    ]


#: The rungs a document's ``extraction_pages`` map can name, in display order. Mirrors
#: ``nexus.pdf_extractor._DEGRADED_METHODS``, which this module cannot import without pulling the
#: extractor stack into ``nx store get``; ``tests/test_degraded_pages_line.py`` pins the two equal.
_DEGRADED_RUNG_ORDER: tuple[str, ...] = ("mineru-no-formula", "pymupdf-text", "unextracted")


def _clean_extraction_pages(raw: object) -> dict[str, list[int]]:
    """A catalog ``meta['extraction_pages']`` value as ``{rung: [pages]}``, non-empty rungs only.
    Anything malformed reads as ``{}``: this feeds a display line, never a decision."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[int]] = {}
    for rung, pages in raw.items():
        if isinstance(pages, list) and pages and all(isinstance(p, int) for p in pages):
            out[str(rung)] = sorted(pages)
    return out


def format_degraded_pages(extraction_pages: dict[str, list[int]]) -> str:
    """``mineru-no-formula 3,7; pymupdf-text 12``: the rungs in ladder order (an unknown rung
    after them, by name), each with its sorted 1-based pages."""
    known = [r for r in _DEGRADED_RUNG_ORDER if r in extraction_pages]
    rest = sorted(r for r in extraction_pages if r not in _DEGRADED_RUNG_ORDER)
    return "; ".join(f"{r} {','.join(str(p) for p in extraction_pages[r])}" for r in known + rest)


def degraded_pages_for_chunk(
    collection: str, chash: str, *, holders: list[tuple[str, str]] | None = None,
    owners: list[Any] | None = None,
) -> tuple[list[tuple[str, str, dict[str, list[int]]]], int]:
    """``([(tumbler, title, extraction_pages), ...], holder_count)``: the live documents holding
    chunk *chash* in *collection* whose catalog ``meta`` records degraded pages (RDR-237), and how
    many documents hold it. Documents with no degraded page are left out of the list.

    *holders* is the ``(tumbler, title)`` list :func:`live_holders_of_chashes` already returned for
    this chunk; ``None`` looks it up. *owners* (nexus-6pfgb.13) is the chunk's live owner entries
    (:func:`live_owner_entries`), which the display already holds: with it nothing is looked up or
    resolved. Best-effort like :func:`live_holders_of_chashes`: ``([], 0)`` when the catalog is
    unavailable or unreadable, so a catalog fault never breaks a ``get``.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    try:
        if owners is not None:
            entries = {str(getattr(e, "tumbler", "")): e for e in owners}
            holders = owner_holders(owners)
        elif holders is None:
            holders = live_holders_of_chashes(collection, [chash]).get(chash, [])
        if not holders:
            return [], 0
        if owners is None:
            reader = make_catalog_reader()
            if reader is None:
                return [], len(holders)
            try:
                entries = reader.resolve_many([t for t, _title in holders])
            finally:
                reader.close()
        rows: list[tuple[str, str, dict[str, list[int]]]] = []
        for tumbler, title in holders:
            entry = entries.get(tumbler)
            meta = getattr(entry, "meta", None) or {}
            pages = _clean_extraction_pages(meta.get("extraction_pages"))
            if meta.get("extraction_method") == "pymupdf":
                # The explicit text-only extractor's map lists every page under its rung, so its
                # ``pymupdf-text`` pages are its normal output, not a fall-back. Only the pages with
                # no text layer are worth a "Degraded:" line. A mixture (mineru+degraded) keeps them.
                pages.pop("pymupdf-text", None)
            if pages:
                rows.append((tumbler, title, pages))
        return rows, len(holders)
    except Exception:  # noqa: BLE001 — a display line is best-effort
        # Warning, not debug: a fault here leaves "Extractor: mineru+degraded" with no Degraded
        # line, which reads like "clean" or "never recorded".
        _log.warning("degraded_pages_lookup_failed", collection=collection, chash=chash, exc_info=True)
        return [], 0


def degraded_pages_lines(
    collection: str, chash: str, *, holders: list[tuple[str, str]] | None = None,
    owners: list[Any] | None = None,
) -> list[str]:
    """The ``Degraded:`` lines ``nx store get`` and the MCP ``store_get`` print after ``Extractor:``:
    one per document whose catalog record has degraded pages, ``[]`` when none does. A chunk held
    by one document prints ``Degraded:   mineru-no-formula 3,7; pymupdf-text 12``; held by several,
    each line is labelled with its document's title (its tumbler when it has none)."""
    rows, holder_count = degraded_pages_for_chunk(collection, chash, holders=holders, owners=owners)
    lines = []
    for tumbler, title, pages in rows:
        text = format_degraded_pages(pages)
        lines.append(f"Degraded:   {title or tumbler}: {text}" if holder_count > 1 else f"Degraded:   {text}")
    return lines


def catalog_chashes_for_title(collection: str, title: str) -> list[list[str]]:
    """The ordered manifest chashes of each store_put-origin document titled
    *title* in *collection*, one list per document (nexus-sis0m.5).

    A chunk row's title is its last writer's, so a title lookup against T3
    misses a note whose text another note wrote later; the catalog keeps
    each document's own title. Uses the catalog's exact title lookup, so
    the cost follows how many documents carry the title, not the
    collection's size. A ghost qualifies only when the caller can load its
    chunk from *collection* (``store_get`` reads it with ``get_by_id``).
    ``[]`` when the catalog is unavailable.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

    reader = make_catalog_reader()
    if reader is None:
        return []
    try:
        docs = []
        for e in reader.find_by_title_exact(title, content_type="knowledge") or []:
            if not _is_title_reap_candidate(e, title, collection):
                continue
            rows = sorted(reader.get_manifest(str(e.tumbler)), key=lambda r: r.position)
            if rows:
                docs.append([r.chash for r in rows])
        return docs
    except Exception:  # noqa: BLE001 — a lookup fallback; the caller reports not found
        _log.debug("catalog_title_lookup_failed", exc_info=True)
        return []
    finally:
        reader.close()


def store_delete_catalog_cleanup(
    chash_doc_id: str, *, expected_collection: str | None,
) -> tuple[str, str]:
    """Delete-asymmetry compensation for ``store_delete`` (nexus-b6enc C4).

    MCP ``store_delete`` historically removed only the T3 chunk; the
    catalog row + manifest survived with a stale ``chunk_count`` — a
    permanent ghost. For store_put-origin docs (``content_type ==
    'knowledge'`` with no ``file_path``) whose ``meta.doc_id`` matches
    the deleted chunk's natural id, delete the catalog row too via
    ``delete_document``.

    *expected_collection* is REQUIRED, no default (nexus-h7nax: a keyword
    that is silently skippable by omission is exactly what produced this
    bug class twice — see :func:`reap_catalog_manifest_for_chashes`'s own
    docstring for the full history). Pass ``None`` explicitly at a call
    site that genuinely has no collection to scope to (e.g. a test
    exercising ambiguity-detection or the "nothing to clean" miss path,
    where no live document exists to protect either way) — that is a
    deliberate, visible choice, not an accident of a default nobody
    noticed. When given, the chash -> document resolution below is scoped
    to it plus blank-collection ghosts (``owner_collection``; nexus-c53hy,
    nexus-sz89e, nexus-r3cdg): a document in another collection is neither
    tombstoned nor counted as a second owner, and a miss returns
    ``("", "")``, same as "nothing to clean". Callers that have already
    confirmed (via a collection-scoped T3 existence check) that this chash
    exists in a specific collection should pass that collection here; this
    is the second of two layers — see ``mcp/core.py::store_delete``'s own
    existence pre-check, which is the primary defense and is what actually
    stops the reap from firing on a doc_id that is not in the target
    collection at all.

    CORRECTED (nexus-3ck2g; this docstring previously claimed
    ``delete_document`` cascades the manifest on both backends — false).
    The engine soft-tombstones: it stamps ``deleted_at`` on the catalog row
    and DELIBERATELY leaves ``document_chunks`` (the manifest) and the T3
    chunk rows untouched, so ``nx catalog restore`` (nexus-dkymw — the
    operator-facing caller nexus-xavu7 found missing) stays possible and so
    ``nexus.purge_trash``'s own orphan predicate
    (``EXISTS`` manifest row AND ``NOT EXISTS`` a live parent) still has
    something to find later — cascading at tombstone time would strand
    those chunks (manifest-less) forever, since ``purge_trash`` never
    sweeps a manifest-less chunk (pinned by
    ``CatalogDocumentCascadeTest`` / ``SoftDeleteTest``). The manifest and
    T3 chunks survive until an operator runs ``nx catalog purge-trash
    --no-dry-run --confirm`` (the engine's ``nexus.purge_trash(interval)``,
    wired to a caller by nexus-3ck2g) — and since catalog-026 (nexus-5da44;
    this caveat stated the earlier not-age-gated behaviour for two weeks
    after the engine retired it, nexus-kcm6c) the chunk sweep protects
    every tombstone still inside the ``--older-than-days`` grace window:
    row, manifest, and chunks stay TOGETHER until the window passes. THIS
    IS THE GENERIC ``delete_document`` CONTRACT, NOT WHAT THIS FUNCTION
    ITSELF PRODUCES (review round 2, T2 [24834]): the ``delete_document``
    call this function makes runs AFTER the MCP ``store_delete`` tool has
    already hard-deleted the T3 chunk in the same call (see this
    function's own docstring above — the T3 chunk was already gone before
    this tombstone was ever taken). ``nx catalog restore`` on a tumbler
    tombstoned via THIS path still clears ``deleted_at`` and reports
    success on the ROW, but the chunk it would need to restore CONTENT is
    already gone regardless of the purge-trash window — restore cannot
    undo that; recovery is a re-index. (Once the window above passes for a
    genuinely `nx catalog delete`-originated tombstone, ``nx catalog
    restore`` returns 0 — nothing left to restore; recovery becomes
    re-indexing there too.) Until the engine ships the RDR-156 read-side tombstone
    filter (also nexus-3ck2g), the deleted content also stays fully
    searchable in the interim — this cleanup only stops the CATALOG ROW
    from resolving.

    Returns ``(tumbler, error)`` — ``("", "")`` when no matching
    store_put-origin row exists (nothing to clean), ``(tumbler, "")`` on
    successful cleanup, ``(tumbler, error)`` when a row was found but
    cleanup failed (caller surfaces it — fail loud, never silent).
    """
    reader = None
    entry = None
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import at module load

        reader = make_catalog_reader()
        if reader is None:
            return "", ""
        # nexus-5axey: by_doc_id is a TUMBLER-only lookup (settled wji11
        # contract) and always mismatched this chash-shaped id;
        # resolve_knowledge_doc_for_chash is the chash-appropriate lookup
        # and already applies the content_type == "knowledge" / no
        # file_path filter below.
        entry = resolve_knowledge_doc_for_chash(
            reader, chash_doc_id, log_event="store_delete_catalog_lookup",
            owner_collection=expected_collection,
        )
    except Exception as exc:  # noqa: BLE001 — lookup failure must not mask the successful T3 delete; surfaced to caller
        _log.warning(
            "store_delete_catalog_lookup_failed",
            doc_id=chash_doc_id, exc_info=True,
        )
        return "", f"catalog lookup failed: {exc}"
    finally:
        if reader is not None:
            try:
                reader._db.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass

    if entry is None:
        return "", ""

    # nexus-c53hy's cross-collection guard and nexus-sz89e's ghost rule now
    # live in the resolve above (owner_collection, nexus-r3cdg): an entry
    # it returns is in expected_collection or a blank-collection ghost.
    tumbler = str(entry.tumbler)
    writer = None
    retract_reader = None
    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred, avoids import cycle

        writer = make_catalog_writer()
        # nexus-mmkqe/rnqbw (RDR-191 GATE-2): retract chash_doc_id's own
        # manifest row(s) BEFORE tombstoning -- the anti-join now protects
        # a tombstoned owner's manifest row too (definition class b), so
        # tombstoning alone no longer unblocks THIS caller's own T3 delete
        # the way it used to. See _retract_manifest_rows_for_chash.
        retract_reader = make_catalog_reader()
        if retract_reader is not None:
            _retract_manifest_rows_for_chash(
                retract_reader, writer, entry, chash_doc_id,
                expected_collection=expected_collection,
            )
        writer.delete_document(Tumbler.parse(tumbler))
        _log.info(
            "store_delete_catalog_row_removed",
            tumbler=tumbler, doc_id=chash_doc_id,
        )
        return tumbler, ""
    except Exception as exc:  # noqa: BLE001 — cleanup failure surfaced to the caller, never silently swallowed
        _log.warning(
            "store_delete_catalog_cleanup_failed",
            tumbler=tumbler, doc_id=chash_doc_id, exc_info=True,
        )
        return tumbler, str(exc)
    finally:
        if retract_reader is not None:
            try:
                retract_reader._db.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup in finally
                pass


def reap_catalog_manifest_for_chashes(
    chashes: list[str], *, expected_collection: str | None,
) -> None:
    """Best-effort: tombstone the catalog entry that owns each *chash*,
    BEFORE the caller deletes the corresponding T3 chunk.

    *expected_collection* is REQUIRED, no default (nexus-h7nax). This
    function shipped its collection-scoping guard at nexus-c53hy and a
    THIRD call site (``db/http_vector_client.py::expire()``) was still
    found omitting it two rounds of critique later, on a defaultable
    keyword nobody remembered to pass — the identical shape as the bug
    the guard itself exists to close, one layer up. A required keyword
    turns "forgot to scope it" into a ``TypeError`` at the call site
    instead of a silent global-chash resolution; callers with genuinely
    nothing to scope to (e.g. a test exercising the ambiguity guard or
    the plain "no match" miss path, where no live document exists to
    protect either way) pass ``expected_collection=None`` explicitly —
    a visible decision, not an accident.

    When given as a real collection name, the per-chash resolution below
    is scoped to it plus blank-collection ghosts (``owner_collection``;
    nexus-c53hy, nexus-sz89e, nexus-r3cdg): a document in another
    collection that shares the chash (an ``.nxexp`` import copy, duplicate
    content) is neither tombstoned nor counted as a second owner, matching
    the engine's collection-scoped delete anti-join. That is the second
    layer — the primary defense is the
    caller doing a collection-scoped T3 existence check before calling
    this at all (see ``commands/store.py::delete_cmd``'s ``--id`` branch).

    Relocated from ``commands/store.py::_reap_catalog_for_doc_ids``
    (nexus-o8dil.5 Fix 1, RDR-191 P2) so ``db/http_vector_client.py`` and
    ``commands/store.py`` can both call it without either reaching into
    the other's layer — same layering rationale as this module's own
    header docstring for :func:`single_chunk_manifest_metadata`.

    ORDERING IS LOAD-BEARING (nexus-o8dil.5 round 2 finding). Callers
    MUST invoke this BEFORE deleting the T3 chunk(s), not after. Reaping
    AFTER the chunk delete (the pre-nexus-o8dil.5 order in both this
    module's callers) means the manifest row is still live at delete
    time, every deletion is silently refused, and reaping afterward
    tombstones a document whose chunk never actually left T3 — exactly
    the TTL-sweep leak this fix closes.

    RETRACTION, NOT JUST TOMBSTONE (nexus-mmkqe/rnqbw amendment, RDR-191
    GATE-2, 2026-08-12 — supersedes this section's original mechanism,
    kept below for provenance). Originally, reaping first was enough on
    its own: ``PgVectorRepository#delete``'s anti-join treated a
    tombstoned owner's manifest row as no longer "live", so
    ``delete_document``'s soft tombstone alone unblocked the chunk delete.
    nexus-mmkqe made that anti-join protect a TOMBSTONED owner's manifest
    row too (definition-of-record class b — correct for an INDEPENDENT
    deleter, e.g. GC or a different session, which must not destroy
    content another document's manifest still names). That reopened this
    exact leak for THIS function's own same-call pattern: tombstoning no
    longer implicitly unblocks the tombstoner's own delete. Each per-chash
    iteration below now calls :func:`_retract_manifest_rows_for_chash`
    BEFORE ``delete_document`` — an explicit manifest-row retraction, not
    reliance on a blind filter — so the anti-join has nothing left to
    protect for *this* chash by the time the T3 delete runs, while a
    GENUINELY shared chash (another live OR tombstoned-but-in-grace-window
    document's manifest row) stays fully protected (this function still
    no-ops when :func:`resolve_knowledge_doc_for_chash` finds no
    unambiguous store_put-origin owner — see that function's own
    ambiguity-handling docstring).

    *chashes* are T3 chunk natural ids, not tumblers (nexus-5axey:
    ``resolve_knowledge_doc_for_chash`` is the chash-appropriate lookup;
    ``by_doc_id`` is tumbler-only and always mismatches these). Skipped
    silently when the catalog is uninitialised, and per-chash lookup/
    tombstone failures are logged at DEBUG and swallowed — this is a
    best-effort cleanup, not a fail-loud boundary (contrast
    :func:`store_delete_catalog_cleanup`, MCP ``store_delete``'s
    fail-loud sibling). A failed reap for one chash simply means the
    anti-join will (correctly) refuse to delete that chash's chunk; the
    caller's own deleted-count must come from the ACTUAL server response
    (never from ``len(requested ids)``) so a reap failure here shows up
    as an honest under-count, not a silent lie.
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
        for chash in chashes:
            entry = resolve_knowledge_doc_for_chash(
                reader, chash, log_event="catalog_reap",
                owner_collection=expected_collection,
            )
            if entry is None:
                continue
            # nexus-mmkqe/rnqbw: retract THIS chash's own manifest row(s)
            # BEFORE tombstoning — see _retract_manifest_rows_for_chash and
            # this function's own "RETRACTION, NOT JUST TOMBSTONE" docstring
            # section.
            #
            # nexus-review-kmo9h-regression: retraction gets its OWN narrow
            # try/except, separate from delete_document below. Letting a
            # retraction failure propagate to the OUTER try/except (as an
            # earlier revision of this loop did) aborted the ENTIRE batch —
            # not just this chash's tombstone, every remaining chash in
            # *chashes* too — which is strictly worse than this function's
            # own documented best-effort contract ("a failed reap for one
            # chash simply means the anti-join will (correctly) refuse to
            # delete that chash's chunk"). WARNING, not DEBUG: a failed
            # retraction is now consequential (the chunk stays anti-join-
            # protected until a later reap or purge_trash, not merely a
            # missed catalog tombstone), so it should be visible without
            # DEBUG-level logging enabled. delete_document still runs
            # UNCONDITIONALLY after — the document gets tombstoned either
            # way; if retraction failed, the manifest row survives and the
            # anti-join loudly (not silently) blocks the caller's own T3
            # delete, which is the SAFE direction (data protected, not lost).
            try:
                _retract_manifest_rows_for_chash(
                    reader, writer, entry, chash,
                    expected_collection=expected_collection,
                )
            except Exception:  # noqa: BLE001 — retraction is best-effort; see comment above
                _log.warning(
                    "catalog_reap_retraction_failed",
                    chash=chash, tumbler=str(entry.tumbler), exc_info=True,
                )
            writer.delete_document(entry.tumbler)
    except Exception:  # noqa: BLE001 — best-effort catalog reap; failure logged at debug, cleanup in finally
        _log.debug("catalog_reap_failed", exc_info=True, doc_ids=chashes)
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()
