# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-236 (nexus-1nrcu): copy a chunk row's section label onto the legacy manifest rows it is exact for.

WHY A RUNG. catalog-046 (nexus-tko2w) gave each manifest row its own ``section_title`` /
``section_type`` / ``page_number`` and a ``labels_written`` flag. A row written before it, or by a
writer that sends no label keys (the orphan backfill, ``manifest_backfill``), is *legacy*: NULL labels,
``labels_written`` false, and every reader falls back to the SHARED T3 chunk row's label, which belongs
to whichever document wrote that chunk last. ``doc_indexer`` skips unchanged files, so a re-index never
fills them. The upgrade ladder (RDR-185) is the standing mechanism for a one-time data transition, so
the fill is a rung: ``nx upgrade`` runs it once the engine is at the pinned tag.

THE RULE (RDR-236, "position labels"). A legacy row takes the chunk row's label only when the label is
exact: the chash has exactly ONE live manifest row in the tenant, that row is this one, and NO tombstoned
document holds the chash. Not "one owner": a document that holds a chash at two positions has one owner and
two rows, and the chunk row holds one label (RF-16 counted 4976 such groups in 1752 documents). Rows of a
chash held by more than one row stay legacy; the counts say how many, with in-document repeats apart from
cross-document sharing. A tombstoned holder is a former owner whose label the chunk row may carry: it is
read through ``chash_positions_with_tombstoned`` and a chash it holds is left legacy (nexus-6pfgb.58).

THREE MORE GUARDS, each a count in the summary.

* The chunk row must carry at least one of the three label keys. A chunk written by a writer that
  predates labels carries none, and copying "" would mark the position "unlabelled for good" where the
  truth is "never recorded". It stays legacy.
* The chunk row's own ``indexed_at`` must be no later than the document's plus the five-minute skew
  (the guard ``nexus.catalog.meta_backfill`` uses): a later row was written by someone else.
* The document must not be one the orphan backfill minted (catalog ``meta.backfill_from == "t3_orphan"``,
  :data:`ORPHAN_BACKFILL_MARKER`): its rows were written legacy on purpose (nexus-jz86a), because the shared
  chunk row's label is a former owner's, and its ``indexed_at`` is later than every chunk it holds, so the
  date guard would never stop the copy. Counted apart.
* The chunk row's ``title`` (the last writer's) must name this document: its title, its file path or file
  name, with or without the per-chunk suffix the indexers add (``:12-30``, ``:chunk-3``, ``:page-2``); a row
  that carries no title is not contradicted. This catches what the date guard cannot: a co-owner indexed
  within the skew and then deleted AND purged, whose manifest rows no ownership read can still name.
* Just before the write, the batch's manifests are read again. A row that became written, or no longer
  holds the same chash at that position, is skipped, and so is a chash that has gained a second row or
  a tombstoned holder.

TOMBSTONED CO-OWNERS (nexus-6pfgb.58). Ownership is read from LIVE manifests, which hide a deleted
document, so before this read a chash whose only other holder was tombstoned counted as single-row and its
label could be that holder's. The engine's ``/manifest/chash_positions`` now takes ``include_tombstoned`` and
lists the manifest rows of tombstoned documents beside the live rows; a chash with such a holder is left
legacy and counted. An engine that predates the key sends no list, which is "cannot say": the rung is then
not applicable and converge DEFERS, because filling without the read is the gap this closes. The
``indexed_at`` and ``title`` guards still stand against a departed owner whose document the engine purged
(its manifest rows are gone with it, so no read can name it).

SIDE EFFECT OF THE ONLY WRITE ROUTE, AND ITS REPAIR. The fill goes through ``append_manifest_many``, which
upserts by position and, on a conflict, rewrites only ``chash``, ``collection``, the three labels and
``labels_written`` (``CatalogRepository.insertManifestChunkRows``, ``UPSERT_APPEND``). The engine also
stamps the document's ``indexed_at`` on every manifest write. Left alone, that would switch off the
chunk-newer guard above for the document's OTHER rows (the guard compares against the document's stamp),
so the next pass would copy a departed co-owner's label, and ``meta_backfill``'s own guard would look at
the document more leniently. So after each document's append succeeds, the rung writes the document's
``indexed_at`` back to the value it read at scan (``update(doc, indexed_at=...)``; the engine honours a
caller-supplied stamp). A document whose stamp cannot be written back is counted and named, and the run
ends DEFERRED: its stamp is the run time until someone restores it.

WHAT IT DOES.
  detect   : converged at once when this tenant's completion is on file (any package version: the rung is a
             one-time transition, and rows that stay legacy by design stay legacy until a forced re-index).
             Otherwise converged on ONE bounded read (the catalog's ``/stats``: document count and manifest
             row count) when a clean scan is remembered (:data:`SCAN_MEMO_MAX_AGE`, same two counts;
             nexus-6pfgb.60). Otherwise the dry run: the full scan with no write, which stops between batches
             at the ``nx upgrade --auto`` time limit and then reports the rung pending (the answer is not
             known), never converged. Applicable unless no catalog is available, the engine has no
             ``chash_positions`` route or it cannot list tombstoned holders (nexus-6pfgb.58). Converged when no legacy row is fillable; rows that stay legacy
             (multi-row chash, no label keys, ...) are not pending. detect WRITES NOTHING, the memo included:
             it runs from ``nx doctor`` and ``--dry-run`` before the earlier rungs have done their work, and
             the RDR-192 backfill adds legacy manifest rows to documents already counted, so a "clean" answer
             kept from that moment would be outdated by the walk that follows.
  converge : the same scan, writing each batch of :data:`DOC_BATCH` documents through
             ``append_manifest_many``, one request per collection per ~50 documents and per 300 rows
             (a document's rows spill into the next request), so the number of writes is bounded by
             documents / 50 + rows / 300 + collections touched. The
             ``nx upgrade --auto`` time limit is honoured between batches (DEFERRED; filled rows drop
             out of the next scan). A write the engine reports as failed is DEFERRED with its count.
  verify   : positive signal only: the whole scan completed and no row is fillable. It never reads the memo
             and never stops at the time limit: it is the gate for the completion record. It is the memo's
             ONE writer (a cache note in T2, never rung state): a complete scan that found nothing fillable
             saves the two counts it began with, a fillable result clears the note. This is the one write a
             ladder ``detect``/``verify`` pair makes; ``protocol.py`` names it.
             When detect answered converged FROM THE MEMO and this scan then finds a fillable row, the memo
             was outdated by a change the two counts do not show: the rung reports :meth:`verify_deferred`
             (the walk maps it to DEFERRED, non-fatal) and the memo is cleared, so the next walk's detect
             scans and the fill runs. It is never VERIFY_FAILED: converge did not run, so nothing failed.

MEMO WINDOW. The two counts do not see a change that moves no document and no row: a chunk row gaining a label
in T3, a row swapped for another, a co-owner replaced by another. Within the age bound such a change is found
by the next verify; a walk whose detect trusted the memo reports it as DEFERRED (``verify_deferred``) and the
next walk scans.

RESIDUAL WINDOW. Between the re-read of a batch's manifests and the ``append_manifest_many`` POST, a row can
still become written or gain a co-owner: ``UPSERT_APPEND`` carries no ``WHERE labels_written = false``. The
re-read narrows the window to one request; it does not close it, and a row filled in that window takes a
label the guards would have refused a moment later.

FILL-ONLY. A row already ``labels_written`` is never a candidate, whatever its labels (an empty label
included). The rung never writes a label with ``labels_written`` false (the catalog-046 CHECK).

THE CHUNK READ. The label comes from the T3 chunk row, a LEGACY read of three POSITION keys (RDR-236
key table) and of ``indexed_at`` for the attribution guard; it is made in :func:`_labels_from_chunk_row`,
the one entry this module has in ``tests/chunk_metadata_read_sites.toml``, and goes away with the keys at
Phase 4.
"""
from __future__ import annotations

import os.path
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from nexus.upgrade_ladder.auto_guard import INTERACTIVE_REMEDY, managed_service, refuses_unbounded_read
from nexus.upgrade_ladder.census_progress import NoteStore, T2NoteStore, guarded
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.deadline import active_expired
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SECTION_LABELS

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_MANIFEST_SECTION_LABELS

#: Documents read, planned and written per step. A document's manifest can hold thousands of rows, so
#: this bounds the rows in memory and the size of one ``chash_positions`` / chunk read.
DOC_BATCH = 50

#: Manifest rows in one ``append_manifest_many`` request (the engine runs each document of a request in its
#: own transaction, and the sibling writers cut at 300). A document's rows spill into the next request.
MAX_ROWS_PER_REQUEST = 300

#: How long a remembered clean scan stands in for a new one (nexus-6pfgb.60). Matches
#: ``census_progress.CENSUS_PROGRESS_MAX_AGE`` and the RDR-192 rung's residual window.
SCAN_MEMO_MAX_AGE = timedelta(hours=24)

#: The T2 note (project ``upgrade_ladder_state``, the census-progress home) holding the remembered clean scan.
SCAN_MEMO_TITLE = f"{RUNG_NAME}.clean-scan"

_LABEL_KEYS = ("section_title", "section_type", "page_number")

#: The catalog ``meta`` marker every document the orphan backfill registers carries
#: (``nexus.catalog.orphan_backfill``: dt_link, synthetic, synthetic_chash, csv_curated). Such a document's
#: ``indexed_at`` is the backfill's run time, later than any chunk it holds, so the date guard always passes
#: for it, and the chunk label is a FORMER owner's (nexus-jz86a, decision (a)): its rows stay legacy.
ORPHAN_BACKFILL_MARKER = ("backfill_from", "t3_orphan")


def minted_by_orphan_backfill(doc: Any) -> bool:
    meta = getattr(doc, "meta", None)
    return isinstance(meta, dict) and meta.get(ORPHAN_BACKFILL_MARKER[0]) == ORPHAN_BACKFILL_MARKER[1]


class RouteAbsent(RuntimeError):
    """The engine has no ``chash_positions`` route: single-row ownership cannot be told."""


class TombstonedHoldersUnreadable(RouteAbsent):
    """The engine answers ``chash_positions`` but cannot list tombstoned holders (it predates
    ``include_tombstoned``): a chash's deleted co-owners cannot be counted, so nothing may be filled."""


@dataclass
class Counts:
    """What one scan saw. Every row of a legacy manifest lands in exactly one of the skip buckets, the
    fillable count, or ``filled``; ``raced`` and ``write_failed`` are fillable rows that were not written."""

    documents: int = 0
    rows: int = 0                       # manifest rows examined
    written_already: int = 0            # labels_written true: never touched
    legacy: int = 0                     # labels_written false, with a chash
    fillable: int = 0                   # legacy, single-row chash, chunk has labels, attributable
    filled: int = 0
    multi_row_cross_document: int = 0   # the chash has rows in more than one document
    multi_row_in_document: int = 0      # the chash recurs inside this one document
    tombstoned_co_owner: int = 0        # a deleted (tombstoned) document also holds the chash
    stale_listing: int = 0              # the one row chash_positions names is not this row
    chunk_missing: int = 0              # no live chunk row for the chash
    chunk_without_label_keys: int = 0   # the chunk row carries none of the three keys
    chunk_newer_than_document: int = 0  # the chunk row was written after the document
    chunk_without_indexed_at: int = 0   # no indexed_at on the chunk row, so not attributable
    chunk_titled_for_another: int = 0   # the chunk row's title names another document's file
    orphan_minted: int = 0              # legacy rows of documents the orphan backfill minted: left alone on purpose
    raced: int = 0                      # became written, moved, or gained a co-owner before the write
    write_failed: int = 0
    restore_failed: int = 0             # documents whose indexed_at could not be written back after the fill
    restore_failed_docs: list[str] = field(default_factory=list)
    by_collection: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, int]:
        out = {k: v for k, v in vars(self).items() if isinstance(v, int)}
        return out

    def summary(self) -> str:
        return (
            f"{self.documents} document(s), {self.rows} row(s): {self.written_already} already written, "
            f"{self.legacy} legacy ({self.fillable} fillable, {self.filled} filled; left legacy: "
            f"{self.multi_row_cross_document} shared with another document, {self.multi_row_in_document} "
            f"repeated inside the document, {self.tombstoned_co_owner} also held by a deleted document, "
            f"{self.chunk_without_label_keys} chunk with no label keys, "
            f"{self.orphan_minted} in documents the orphan backfill minted, {self.chunk_missing} chunk missing, {self.chunk_newer_than_document + self.chunk_without_indexed_at + self.chunk_titled_for_another} "
            f"chunk row not attributable to the document, {self.stale_listing + self.raced} changed "
            f"meanwhile, {self.write_failed} write failed, {self.restore_failed} indexed_at not restored)"
        )


def _default_reader() -> Any:
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_catalog_reader()


def _default_writer() -> Any:
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_catalog_writer()


def _default_record() -> CompletionRecord | None:
    """This tenant's completion record for the rung, from the engine's ledger. Raises when it cannot be read."""
    from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred: keeps the registry import cheap

    with HttpLadderStore() as store:
        return store.completions().get(RUNG_NAME)


def _default_memo() -> NoteStore:
    return T2NoteStore(SCAN_MEMO_TITLE, tags="upgrade-ladder,rdr236")


def _default_t3() -> Any:
    from nexus.db import make_t3  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_t3()


#: The per-chunk suffix an indexer appends to a chunk row's title: ``<relative path>:<line>-<line>`` (code),
#: ``:chunk-<n>`` (prose) and ``:page-<n>`` (page chunks).
_CHUNK_TITLE_SUFFIX = re.compile(r":(?:chunk-\d+|\d+-\d+|page-\d+)$")


def _title_names_document(title: str, doc: Any) -> bool:
    """True when a chunk row's *title* is this document's own: its title, its file path or file name, with
    or without a per-chunk suffix. A row that carries no title is not contradicted. The title is the LAST
    writer's, so one that names another file is a former owner's row."""
    if not title:
        return True
    own = {n for n in (
        getattr(doc, "title", "") or "",
        getattr(doc, "file_path", "") or "",
        os.path.basename(getattr(doc, "file_path", "") or ""),
    ) if n}
    stripped = _CHUNK_TITLE_SUFFIX.sub("", title)
    return bool({title, stripped, os.path.basename(stripped)} & own)


def _labels_from_chunk_row(metadata: dict, doc: Any) -> tuple[dict[str, Any] | None, str]:
    """``(labels, why)`` for one chunk row and the document whose legacy row would take them.

    *labels* are the three labels as a manifest row sends them, ``None`` with a *why* when the row may
    not give them: ``"no_label_keys"`` when it carries none of the three (a writer that predates
    labels: the position stays legacy), ``"newer"`` when its ``indexed_at`` is later than the
    document's plus the five-minute skew (someone else wrote it), ``"no_indexed_at"`` when either side
    has none (not attributable), ``"other_title"`` when its title names another document's file (a
    former owner wrote it, inside the skew). This is the module's only read of a chunk row's metadata."""
    from nexus.catalog.note_ttl import PUT_SKEW, parse_iso  # noqa: PLC0415 — deferred: note_ttl has a wide import graph

    if not any(k in metadata for k in _LABEL_KEYS):
        return None, "no_label_keys"
    stamped = parse_iso(metadata.get("indexed_at"))
    ceiling = parse_iso(getattr(doc, "indexed_at", ""))
    if stamped is None or ceiling is None:
        return None, "no_indexed_at"
    if stamped - ceiling > PUT_SKEW:
        return None, "newer"
    if not _title_names_document(metadata.get("title") or "", doc):
        return None, "other_title"
    return {
        "section_title": metadata.get("section_title") or "",
        "section_type": metadata.get("section_type") or "",
        "page_number": metadata.get("page_number") or None,
    }, ""


def _chunk_metadatas(t3: Any, collection: str, chashes: list[str]) -> dict[str, dict]:
    """``chash -> chunk row metadata`` for the live chunks of *collection*; a collection the engine does
    not have holds none."""
    from nexus.errors import collection_not_found_errors  # noqa: PLC0415 — deferred, imports back into the catalog

    if not chashes:
        return {}
    try:
        col = t3.get_collection(collection)
    except collection_not_found_errors() as exc:
        if getattr(exc, "__cause__", None) is not None:
            raise  # a service that could not be asked is not "the collection is absent"
        return {}
    result = col.get(ids=list(chashes), include=["metadatas"])
    ids: list[str] = result.get("ids") or []
    metas: list[Any] = result.get("metadatas") or []
    if len(metas) != len(ids):
        raise RuntimeError(
            f"manifest-section-labels: the engine returned {len(ids)} ids but {len(metas)} metadatas for "
            f"a read of {len(chashes)} chashes in {collection!r}; refusing to guess which are missing")
    return {cid: (m if isinstance(m, dict) else {}) for cid, m in zip(ids, metas)}


def _row_collection(row: Any, doc: Any) -> str:
    return getattr(row, "collection", None) or getattr(doc, "physical_collection", "") or ""


class ManifestSectionLabelsRung:
    """The rung. Collaborators are injected for tests: *reader_factory* (``all_documents``,
    ``get_manifests``, ``chash_positions_with_tombstoned``, ``close``; ``None`` when no catalog is available),
    *writer_factory* (``append_manifest_many``, ``close``) and *t3_factory* (``get_collection``).
    *memo_store* holds the remembered clean scan (default: a T2 note; ``verify`` writes it, nothing else
    does); *now_fn* is its clock. The reader also answers ``stats()`` (``doc_count``, ``chunk_count``)."""

    name = RUNG_NAME

    def __init__(
        self,
        *,
        reader_factory: Callable[[], Any] = _default_reader,
        writer_factory: Callable[[], Any] = _default_writer,
        t3_factory: Callable[[], Any] = _default_t3,
        doc_batch: int = DOC_BATCH,
        max_rows: int = MAX_ROWS_PER_REQUEST,
        record_fn: Callable[[], CompletionRecord | None] = _default_record,
        memo_store: NoteStore | None = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        managed_fn: Callable[[], bool] = managed_service,
    ) -> None:
        self._managed_fn = managed_fn
        self._reader_factory = reader_factory
        self._writer_factory = writer_factory
        self._t3_factory = t3_factory
        self._doc_batch = doc_batch
        self._max_rows = max_rows
        self._record_fn = record_fn
        self._memo: NoteStore = memo_store if memo_store is not None else _default_memo()
        self._now = now_fn
        self._cut = False  # the last scan stopped at the time limit
        self._scan_shape: tuple[int, int] | None = None  # (documents, manifest rows) per /stats when the last dry run began
        self._from_memo = False  # the last detect() answered converged from the remembered scan
        self._verify_deferred = False
        self._filled = 0
        self._verify_detail = ""
        self._record_detail = ""
        self._partial = Counts()

    # ── the scan ─────────────────────────────────────────────────────────────

    def _scan(
        self, reader: Any, *, writer: Any | None, t3: Any, report: ProgressReporter | None = None,
        honour_deadline: bool = False,
    ) -> Counts | None:
        """One pass over every live document. Writes when *writer* is given, else a dry run. Returns the
        counts, or ``None`` when the ``nx upgrade --auto`` limit passed between batches (the counts so
        far are in ``self._partial``, ``self._cut`` is set). Any failed read propagates: a partial scan is
        not 'nothing to do'. A writing scan always runs its first batch (a session that starts late still
        makes progress); a read-only one that *honour_deadline* (``detect``) writes nothing to protect, so it
        stops before every batch, the first included (``_dry_run`` has already checked before the first read)."""
        counts = Counts()
        self._partial = counts
        self._cut = False
        entries = sorted(
            (e for e in reader.all_documents() if not getattr(e, "alias_of", "")),
            key=lambda e: str(e.tumbler),
        )
        for start in range(0, len(entries), self._doc_batch):
            if (honour_deadline or (start and writer is not None)) and active_expired():
                self._cut = True
                return None
            self._batch(reader, writer, t3, entries[start:start + self._doc_batch], counts)
            if report is not None:
                report.emit("manifest_section_labels_batch", **counts.as_dict())
        return counts

    def _batch(self, reader: Any, writer: Any | None, t3: Any, docs: list[Any], counts: Counts) -> None:
        by_tumbler = {str(d.tumbler): d for d in docs}
        manifests = reader.get_manifests(sorted(by_tumbler)) or {}
        counts.documents += len(docs)
        legacy: list[tuple[Any, Any]] = []  # (document, row)
        for tumbler, doc in by_tumbler.items():
            for row in manifests.get(tumbler, []):
                counts.rows += 1
                if getattr(row, "labels_written", False):
                    counts.written_already += 1
                elif getattr(row, "chash", ""):
                    counts.legacy += 1
                    if minted_by_orphan_backfill(doc):
                        counts.orphan_minted += 1
                        continue
                    legacy.append((doc, row))
        if not legacy:
            return
        owners, deleted = self._owner_rows(reader, sorted({r.chash for _d, r in legacy}))

        single: list[tuple[Any, Any]] = []
        for doc, row in legacy:
            held = owners.get(row.chash, [])
            if len(held) > 1:
                if len({str(h["doc_id"]) for h in held}) == 1:
                    counts.multi_row_in_document += 1
                else:
                    counts.multi_row_cross_document += 1
            elif len(held) == 1 and (str(held[0]["doc_id"]), int(held[0]["position"])) == (str(doc.tumbler), int(row.position)):
                if deleted.get(row.chash):
                    counts.tombstoned_co_owner += 1  # a deleted document held it too: its label may be the chunk row's
                else:
                    single.append((doc, row))
            else:
                counts.stale_listing += 1

        # the chunk rows, one read per collection
        wanted: dict[str, list[str]] = {}
        for doc, row in single:
            wanted.setdefault(_row_collection(row, doc), []).append(row.chash)
        chunk_metas: dict[tuple[str, str], dict] = {}
        for collection, chashes in wanted.items():
            for chash, meta in _chunk_metadatas(t3, collection, sorted(set(chashes))).items():
                chunk_metas[(collection, chash)] = meta

        plan: list[tuple[Any, Any, str, dict[str, Any]]] = []  # (document, row, collection, labels)
        for doc, row in single:
            collection = _row_collection(row, doc)
            meta = chunk_metas.get((collection, row.chash))
            if meta is None:
                counts.chunk_missing += 1
                continue
            labels, why = _labels_from_chunk_row(meta, doc)
            if why == "no_label_keys":
                counts.chunk_without_label_keys += 1
                continue
            if why == "newer":
                counts.chunk_newer_than_document += 1
                continue
            if why == "other_title":
                counts.chunk_titled_for_another += 1
                continue
            if why:
                counts.chunk_without_indexed_at += 1
                continue
            plan.append((doc, row, collection, labels))
        counts.fillable += len(plan)
        if writer is None or not plan:
            return
        self._write(reader, writer, plan, counts)

    @staticmethod
    def _owner_rows(reader: Any, chashes: list[str]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        """``(live, deleted)``: the live manifest rows holding each chash, and the rows of tombstoned documents
        holding it. Raises :class:`RouteAbsent` when the engine has no route, :class:`TombstonedHoldersUnreadable`
        when it cannot list the tombstoned holders: neither is 'nobody holds it'."""
        read = getattr(reader, "chash_positions_with_tombstoned", None)
        got = read(chashes) if read is not None else None
        if got is None:
            raise RouteAbsent("the engine has no /manifest/chash_positions route (with tombstoned holders)")
        rows, tombstoned = got
        if tombstoned is None:
            raise TombstonedHoldersUnreadable(
                "the engine's /manifest/chash_positions cannot list tombstoned holders (no include_tombstoned): "
                "a chash's deleted co-owners cannot be counted, so no label is copied")
        live: dict[str, list[dict]] = {}
        for row in rows:
            live.setdefault(row["chash"], []).append(row)
        deleted: dict[str, list[dict]] = {}
        for row in tombstoned:
            deleted.setdefault(row["chash"], []).append(row)
        return live, deleted

    def _write(self, reader: Any, writer: Any, plan: list[tuple[Any, Any, str, dict[str, Any]]], counts: Counts) -> None:
        """Re-read the batch just before writing, drop what changed, and append the rest per collection."""
        docs = sorted({str(d.tumbler) for d, _r, _c, _l in plan})
        stamps = {str(d.tumbler): getattr(d, "indexed_at", "") for d, _r, _c, _l in plan}  # as read at scan
        fresh = reader.get_manifests(docs) or {}
        fresh_rows = {t: {r.position: r for r in rows} for t, rows in fresh.items()}  # one index for the batch
        again, again_deleted = self._owner_rows(reader, sorted({r.chash for _d, r, _c, _l in plan}))
        by_collection: dict[str, dict[str, list[dict]]] = {}
        for doc, row, collection, labels in plan:
            tumbler = str(doc.tumbler)
            now = fresh_rows.get(tumbler, {}).get(row.position)
            if (
                now is None or now.chash != row.chash or getattr(now, "labels_written", False)
                or len(again.get(row.chash, [])) != 1 or again_deleted.get(row.chash)
            ):
                counts.raced += 1
                continue
            by_collection.setdefault(collection, {}).setdefault(tumbler, []).append({
                "chash": row.chash, "position": row.position, **labels, "labels_written": True,
            })
        for collection, per_doc in sorted(by_collection.items()):
            written: set[str] = set()
            for request in self._requests(sorted(per_doc.items())):
                result = writer.append_manifest_many(request, collection=collection)
                failed = {str(d) for d in (result or {}).get("failed_doc_ids") or []}
                for doc_id, rows in request:
                    if doc_id in failed:
                        counts.write_failed += len(rows)
                    else:
                        counts.filled += len(rows)
                        counts.by_collection[collection] += len(rows)
                        self._filled += len(rows)
                        written.add(doc_id)
            for doc_id in sorted(written):  # once per document, however many requests carried its rows
                self._restore_indexed_at(writer, doc_id, stamps.get(doc_id, ""), counts)

    def _requests(self, docs_rows: list[tuple[str, list[dict]]]) -> list[list[tuple[str, list[dict]]]]:
        """Cut ``(doc_id, rows)`` pairs into requests of at most ``max_rows`` rows and ``doc_batch``
        documents; a document's rows spill into the next request."""
        out: list[list[tuple[str, list[dict]]]] = []
        current: dict[str, list[dict]] = {}
        total = 0
        for doc_id, rows in docs_rows:
            for row in rows:
                if total >= self._max_rows or (doc_id not in current and len(current) >= self._doc_batch):
                    out.append(list(current.items()))
                    current, total = {}, 0
                current.setdefault(doc_id, []).append(row)
                total += 1
        if current:
            out.append(list(current.items()))
        return out

    @staticmethod
    def _restore_indexed_at(writer: Any, doc_id: str, stamp: str, counts: Counts) -> None:
        """Write the document's ``indexed_at`` back to the value read at scan. The engine restamps it on
        every manifest write, which would turn off the chunk-newer guard for the document's other rows on
        the next pass. A failure is counted and the document named; the run ends DEFERRED."""
        error = ""
        for _attempt in range(2):  # one retry: a transient failure here would leave the guard off for the document
            try:
                writer.update(doc_id, indexed_at=stamp)
                return
            except Exception as exc:  # noqa: BLE001 — any failure to write the stamp back is reported, never swallowed
                error = str(exc)
        counts.restore_failed += 1
        counts.restore_failed_docs.append(doc_id)
        _log.warning("manifest_section_labels_restore_indexed_at_failed", doc_id=doc_id, error=error)

    # ── reading with the lifecycle of the collaborators ──────────────────────

    def _dry_run(self, *, honour_deadline: bool = False) -> Counts | None:
        """The counts of a read-only scan, or ``None`` when no catalog is available (or, with
        *honour_deadline*, when the time limit stopped it: ``self._cut``). Past the limit nothing is read,
        the ``/stats`` read included."""
        self._cut = False
        self._scan_shape = None
        if honour_deadline and active_expired():
            self._cut = True
            return None
        reader = self._reader_factory()
        if reader is None:
            return None
        t3 = self._t3_factory()
        try:
            self._scan_shape = guarded("scan_shape", lambda: _catalog_shape(reader), None)  # before the scan: a document or row added meanwhile makes the memo stale
            return self._scan(reader, writer=None, t3=t3, honour_deadline=honour_deadline)
        finally:
            _close(t3)
            _close(reader)

    # ── the remembered clean scan ────────────────────────────────────────────

    def _memo_stands(self) -> bool:
        """True when a clean scan is remembered, young enough, and the catalog still holds the same number
        of documents AND of manifest rows: ONE bounded read (``/stats``) in place of the scan. The row count
        is the key that moves when the RDR-192 backfill adds legacy rows to documents already counted. Any
        failure is 'no memo'."""
        def check() -> bool:
            note = self._memo.load()
            if not isinstance(note, dict):
                return False
            documents, chunks, stamp = note.get("documents"), note.get("chunks"), note.get("clean_at")
            if not _is_count(documents) or not _is_count(chunks) or not isinstance(stamp, str):
                return False
            age = self._now() - datetime.fromisoformat(stamp)
            if not timedelta(0) <= age <= SCAN_MEMO_MAX_AGE:
                return False
            reader = self._reader_factory()
            if reader is None:
                return False
            try:
                return _catalog_shape(reader) == (documents, chunks)
            finally:
                _close(reader)

        return guarded("scan_memo_check", check, False)

    def _remember(self, counts: Counts) -> None:
        """Keep a complete scan's verdict: clean when nothing is fillable, else drop any memo. Called by
        ``verify`` only."""
        if counts.fillable or self._scan_shape is None:
            guarded("scan_memo_clear", self._memo.clear, None)
            return
        documents, chunks = self._scan_shape
        note = {"clean_at": self._now().isoformat(timespec="seconds"), "documents": documents, "chunks": chunks}
        guarded("scan_memo_save", lambda: self._memo.save(note), None)

    # ── detect ───────────────────────────────────────────────────────────────

    def _recorded(self) -> bool:
        """True when this tenant's completion is on file, at any package version. An unreadable ledger is not
        a record. The rung is a one-time transition: rows that stay legacy by design stay legacy, and a
        rescan per release would copy what the guards refused once a co-owner left."""
        try:
            return self._record_fn() is not None
        except Exception as exc:  # noqa: BLE001 — an unreadable ledger is 'not recorded', and the scan decides
            _log.info("manifest_section_labels_ledger_unreadable", error=str(exc))
            return False

    def detect(self) -> RungStatus:
        """Converged at once when the completion is on file: ``nx doctor``, every ``nx upgrade --auto`` (each
        session start) and every walk call this, and a scan lists every document. Next cheapest, a remembered
        clean scan that still holds (one ``/stats`` read). Otherwise, under ``--auto`` against a MANAGED service,
        not applicable with no catalog read (nexus-75w5d: an interactive ``nx upgrade`` does the first
        scan). Otherwise the dry run, which honours the ``--auto``
        time limit and then says pending, because a scan that did not finish has not found 'nothing'.
        READ-ONLY: the memo is written by :meth:`verify` alone (see the module docstring)."""
        self._from_memo = False
        if self._recorded():
            return RungStatus(applicable=True, converged=True)
        if self._memo_stands():
            self._from_memo = True
            return RungStatus(applicable=True, converged=True)
        if refuses_unbounded_read(self._managed_fn):
            # nexus-75w5d: no record and no memo, so the answer is a walk of the whole catalog. On a
            # managed service that is a 15-minute read of shared capacity per session start (19 at once
            # on 2026-10-31), so an unattended run reads nothing. Not applicable, not pending: the walk
            # is skipped, and `nx doctor` (no deadline bound) still reports the true state.
            reason = (
                "unattended `nx upgrade --auto` does not scan a managed catalog with no completion "
                f"record and no remembered clean scan; {INTERACTIVE_REMEDY}"
            )
            _log.info("manifest_section_labels_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        try:
            counts = self._dry_run(honour_deadline=True)
        except RouteAbsent as exc:
            _log.info("manifest_section_labels_rung_skipped", reason=str(exc))
            return RungStatus(applicable=False, converged=False, pending_detail=str(exc))
        if counts is None and self._cut:
            return RungStatus(
                applicable=True,
                converged=False,
                pending_detail=(
                    "the `nx upgrade --auto` time limit stopped the dry run before it could tell whether "
                    f"any legacy manifest row is fillable (read {self._partial.documents} document(s)); "
                    "`nx upgrade` checks and fills"
                ),
            )
        if counts is None:
            reason = "no catalog is available"
            _log.info("manifest_section_labels_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if not counts.fillable:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True,
            converged=False,
            pending_detail=(
                f"{counts.fillable} legacy manifest row(s) of {counts.legacy} can take their chunk row's "
                f"section label (dry run: {counts.summary()})"
            ),
        )

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        reader = self._reader_factory()
        if reader is None:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail="no catalog is available")
        writer = self._writer_factory()
        t3 = self._t3_factory()
        try:
            try:
                counts = self._scan(reader, writer=writer, t3=t3, report=report)
            except RouteAbsent as exc:
                return ConvergeResult(ConvergeOutcome.DEFERRED, detail=str(exc))
        finally:
            _close(t3)
            _close(writer)
            _close(reader)
        if counts is None:
            partial = self._partial
            report.emit("manifest_section_labels_deadline", filled=self._filled)
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the `nx upgrade --auto` time limit passed after {self._filled} row(s); the fill "
                    f"resumes on the next `nx upgrade` ({partial.summary()})"
                ),
            )
        if counts.write_failed:
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=f"the engine refused the write for {counts.write_failed} row(s); retried on the next `nx upgrade` ({counts.summary()})",
            )
        if counts.restore_failed:
            named = ", ".join(counts.restore_failed_docs[:10]) + (" ..." if len(counts.restore_failed_docs) > 10 else "")
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the fill could not write indexed_at back on {counts.restore_failed} document(s) ({named}); "
                    f"their stamp is now the run time, which switches off the chunk-newer guard for their "
                    f"remaining legacy rows ({counts.summary()})"
                ),
            )
        return ConvergeResult(ConvergeOutcome.COMPLETED, detail=counts.summary())

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """True only when a whole scan completed and no legacy row is fillable. A catalog that is
        missing or cannot be read is not 'nothing to fill': it is False, with :meth:`verify_detail`.
        The memo's one writer. A fillable row found after detect answered converged FROM THE MEMO is a
        deferral (:meth:`verify_deferred`), not a failure: the memo was outdated, converge never ran."""
        self._verify_detail = ""
        self._verify_deferred = False
        from_memo, self._from_memo = self._from_memo, False
        try:
            counts = self._dry_run()
        except Exception as exc:  # noqa: BLE001 — an unreadable catalog is unknown, and unknown is not reached
            self._verify_detail = f"could not read the catalog to confirm the fill: {exc}"
            return False
        if counts is None:
            self._verify_detail = "cannot confirm the fill: no catalog is available"
            return False
        self._remember(counts)
        if counts.fillable:
            self._verify_detail = f"{counts.fillable} legacy manifest row(s) can still take their chunk row's label ({counts.summary()})"
            if from_memo:
                self._verify_deferred = True
                self._verify_detail = (
                    "a remembered clean scan was outdated: " + self._verify_detail
                    + "; the memo is cleared and the fill runs on the next `nx upgrade`"
                )
            return False
        self._record_detail = f"{self._filled} filled this run; {counts.summary()}"
        return True

    def verify_deferred(self) -> bool:
        """True when the last refusing :meth:`verify` is a deferral (the memo detect trusted was outdated),
        which the walk reports as DEFERRED instead of VERIFY_FAILED."""
        return self._verify_deferred

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _catalog_shape(reader: Any) -> tuple[int, int] | None:
    """``(documents, manifest rows of live documents)`` from ONE ``/stats`` read (catalog-019's
    ``doc_count`` and ``chunk_count``), or ``None`` when the reader cannot give both. The row count is what
    keeps a memo honest across the RDR-192 backfill, which adds rows without adding documents."""
    stats = getattr(reader, "stats", None)
    if stats is None:
        return None
    body = stats()
    if not isinstance(body, dict):
        return None
    documents, chunks = body.get("doc_count"), body.get("chunk_count")
    return (documents, chunks) if _is_count(documents) and _is_count(chunks) else None


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 — a failed close never fails the fill
            _log.debug("manifest_section_labels_rung_close_failed", exc_info=True)
