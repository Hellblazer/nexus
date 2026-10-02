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
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from nexus.aspect_readers import uri_for
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
    from nexus.corpus import embedding_model_for_collection_calibrated  # noqa: PLC0415 — deferred to avoid import cycle

    # Calibrated: the plain resolver reads a legacy two-segment local
    # collection as Voyage (nexus-mc1l1), which would leave it unsplit.
    window = window_for_model(embedding_model_for_collection_calibrated(collection))
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
    """``(chash -> owning tumblers, tumbler -> title, tumbler -> head chash,
    reason)`` for every manifested document in *collection*.

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
            return {}, {}, {}, "no catalog reader"
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
        return {c: tuple(t) for c, t in owners.items()}, titles, heads, ""
    except Exception as exc:  # noqa: BLE001 — a degraded listing is reported, never raised
        return {}, {}, {}, f"catalog unreadable ({exc.__class__.__name__})"
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
    owners, doc_titles, doc_heads, degraded = manifest_doc_index(collection)
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
    critique-nexus-xzyr3-26edb6662 [24589]): both ``T3Database.put`` and
    ``HttpVectorClient.put`` already refuse an over-quota document with
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
    owner_collection: str | None = None,
):
    """Resolve *chash* to the single store_put-origin catalog document it
    identifies, or ``None`` if there is no match or the match is ambiguous.

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
    ``nx catalog gc`` sweep to reap.

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
            and (collection is None or entry.physical_collection == collection)
            and (
                owner_collection is None
                or not entry.physical_collection
                or entry.physical_collection == owner_collection
            )
        ):
            candidates.append(entry)
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

    There is no dedicated exact-title index on the catalog reader
    protocol (only ``by_file_path`` / ``by_source_uri`` have that
    shape), so this filters ``find()``'s FTS5 results — which use
    token matching, not substring matching — down to entries whose
    ``title`` is a byte-for-byte match AND whose tumbler is a
    descendant of *owner* (the "knowledge" curator owner; ``find()``
    has no owner scoping of its own, and content_type="knowledge" alone
    is not owner-specific).

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
    for entry in reader.find_by_title_exact(title, content_type="knowledge"):
        if entry.chunk_count == 0 and owner.is_prefix_of(entry.tumbler):
            return entry
    return None


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
    pre_call_doc_id_out: dict[str, str] | None = None,
    error_out: dict[str, str] | None = None,
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
    (GH #1370 Defect 4a; legacy rows registered before this fix carry
    ``source_uri=""`` and are unreachable by the second lookup, so this
    third one stays the reconciliation path for them). Only when all
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
    a title-less ``chroma://<collection>/`` URI would collapse every
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
        # Reuses aspect_readers.uri_for's exact chroma:// convention (the
        # knowledge-collection identity field the reader already resolves
        # by is *title*, since these single-chunk callers carry no
        # source_path in chunk metadata post-RDR-102 D2) — one URI format,
        # never a second one. Empty title synthesizes nothing: see the
        # docstring for why a title-less URI must not be minted.
        #
        # Computed BEFORE the chash dedup below (moved up at nexus-bb6n2
        # round 2) so a dedup HIT can stamp it too.
        source_uri = uri_for(collection_name, title) if title else None

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
        existing = resolve_knowledge_doc_for_chash(
            reader, doc_id, log_event="catalog_store_hook_dedup",
            collection=collection_name,
        )
        # nexus-enej7: the same content under a DIFFERENT title is a second
        # document sharing the chunk (RDR-108), not a re-put of this one.
        # Reconciling onto it rewrote the first document's source_uri to the
        # new title and never registered the new one (shakeout 7.64.1 F1).
        # Only a row with the same identity, or a legacy row with no
        # source_uri to compare, reconciles here.
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
            writer = make_catalog_writer(priority="interactive")
            writer.update(
                existing.tumbler,
                physical_collection=collection_name,
                meta={"doc_id": doc_id},
                source_uri=source_uri or "",
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
        # RDR-146 P2 (nexus-5p2ci.12): store_put / memory promote are
        # user-initiated and latency-sensitive. The MCP server is non-tty, so
        # the isatty() fallback would misclassify these as batch; tag
        # interactive so they take fairness priority over a background index.
        writer = make_catalog_writer(priority="interactive")
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
                    meta={"doc_id": doc_id},
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
                meta={"doc_id": doc_id},
                source_uri=source_uri or "",
            )
            _log.debug(
                "catalog_store_hook_deduped",
                deduped_by="title", tumbler=str(ghost.tumbler),
            )
            return str(ghost.tumbler), False

        # KNOWN RESIDUAL (nexus-n90xg): a legacy NON-ghost row (chunk_count
        # > 0, source_uri="" — populated before nexus-sdp0u) is unreachable
        # by all three lookups above, so its first post-fix re-put falls
        # through here and mints ONE bounded duplicate. That new document
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
            meta={"doc_id": doc_id},
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

        writer = make_catalog_writer(priority="interactive")
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
    try:
        writer = make_catalog_writer(priority="interactive")
        writer.update(catalog_doc_id, meta={"doc_id": pre_call_doc_id})
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
    remaining = [
        {
            "position": r.position, "chash": r.chash, "chunk_index": r.chunk_index,
            "line_start": r.line_start, "line_end": r.line_end,
            "char_start": r.char_start, "char_end": r.char_end,
        }
        for r in current_rows if r.chash != chash
    ]
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
    must leave alone (no other path may reap a document this one refused).
    """

    documents: tuple[tuple[str, str], ...]
    chashes: tuple[str, ...]
    failures: tuple[tuple[str, str], ...]
    held: tuple[str, ...] = ()


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
    documents: list[tuple[str, str]] = []
    reaped_chashes: dict[str, None] = {}
    failures: list[tuple[str, str]] = []
    held: dict[str, None] = {}
    try:
        entries = title_reap_candidates(reader, collection, title, list(chashes), present)
        if not entries:
            return TitleReap((), (), ())
        writer = make_catalog_writer()
        for entry in entries:
            tumbler = str(entry.tumbler)
            rows = reader.get_manifest(tumbler)
            original = [
                {
                    "position": r.position, "chash": r.chash, "chunk_index": r.chunk_index,
                    "line_start": r.line_start, "line_end": r.line_end,
                    "char_start": r.char_start, "char_end": r.char_end,
                }
                for r in rows
            ]
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
            documents.append((tumbler, title))
            reaped_chashes.update(dict.fromkeys(r["chash"] for r in original))
        return TitleReap(tuple(documents), tuple(reaped_chashes), tuple(failures), tuple(held))
    finally:
        if writer is not None:
            writer.close()
        reader.close()


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

        writer = make_catalog_writer(priority="interactive")
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
