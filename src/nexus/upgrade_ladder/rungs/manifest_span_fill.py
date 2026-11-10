# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-236 (nexus-l8o33): copy a chunk row's line and character span onto the manifest rows it is exact for.

WHY A RUNG. The key-retirement sweep deletes the four span keys (``line_start``, ``line_end``, ``chunk_start_char``,
``chunk_end_char``) from every chunk row, after which the manifest's span columns (``line_start``, ``line_end``,
``char_start``, ``char_end``) are the only copy. A manifest row written before the writer sent a span, or by a
writer that sends none (the orphan backfill writes NULL spans on purpose, RDR-236 A4), has NULL columns where its
chunk row still holds the span. The engine counts those rows as ``null_span_rows`` and the sweep refuses while
any remains. Nothing else fills them, and ``doc_indexer`` skips an unchanged file, so a re-index never will.

WHY NOT AN EXTENSION OF ``manifest-section-labels``. That rung is recorded once per tenant and reads converged
for good (and on a managed service answers from an engine count that is about LABELS), so a tenant that already
recorded it would never run a span pass. Its guards are the same, which is why this module imports its helpers
(ownership read, chunk read, title guard, indexed_at restore, request cut) instead of copying them; the two
rungs stay independent and either can run first.

THE RULE. Same as the label rung's (RDR-236 "position labels"): a span is exact for a manifest row only when the
chash has exactly ONE live manifest row in the tenant, that row is this one, no tombstoned document holds the chash
(``chash_positions_with_tombstoned``), the document is not one the orphan backfill minted, and the chunk row is
attributable to the document (its ``indexed_at`` is no later than the document's plus the five-minute skew, and
its ``title`` names the document). A span is a property of one position in one file: a chunk shared by two rows
holds the span of whichever wrote last, so every shared row stays NULL. Nothing is invented: a column is filled only
from a chunk value ``manifest_span`` accepts as recorded (a positive line, a positive end, a start that is positive
or beside a recorded end). ``make_chunk_metadata`` writes 0 for "not recorded"; the engine's ``null_span_rows``
counts by the same value rule (``ChunkKeySweep.preconditions``, nexus-qmrfi), so a lineless chunk's 0 is not counted,
and the summary names those rows apart (``chunk_span_unrecorded``) as rows with nothing to copy and nothing to lose.

FILL-ONLY, COLUMN BY COLUMN, AND ONLY WHEN THE CHUNK ROW AGREES. Only a column that is NULL on the manifest row is
written, with the chunk row's value. A column that already holds a value is never sent (the engine's append upserts a
sent value over a stored one on the same chunk). A row whose stored column DIFFERS from a value the chunk row records is
not filled at all (``span_disagrees``): the disagreement is the one piece of evidence that the chunk row describes another
position, and filling the row's other columns from it would join the lines of one writer to the characters of another.
A row whose stored columns agree with the chunk row (or are not recorded there) is filled on its NULL ones.

WHAT THE SUMMARY ACCOUNTS FOR. The engine's ``null_span_rows`` (``ChunkKeySweep.preconditions``) counts a row with a NULL
manifest column whose chunk key RECORDS a value, by ``manifest_span``'s rule (a positive line, a positive end, a start that
is positive or beside a recorded end, integers only). The summary splits the rows the scan reads the same way: ``counted``
(what the engine counts) = ``fillable`` + the named buckets of :data:`LEFT_BUCKETS`. Every counted row has a recorded span on
the chunk row, so a row left in a bucket is a recorded span that stays unused, and accepting the residue is accepting named
rows. Rows the engine never counts (``chunk_missing``, ``chunk_without_span_keys``, ``chunk_span_unrecorded``) are reported
apart: there is nothing recorded to copy or lose. Once every fillable row is written the engine count is ``counted -
fillable``, and it reaches 0 when only unrecorded keys remain.

THE ONLY WRITE ROUTE, AND WHAT IT COSTS. The fill goes through ``append_manifest_many`` (``UPSERT_APPEND``,
``CatalogRepository.insertManifestChunkRows``), which upserts by position. On a conflict it takes a sent span value
for the same chunk (nexus-mzpsh) and rewrites the three labels and ``labels_written`` from the request. So the
rung sends the labels and ``labels_written`` it read from the row JUST BEFORE the write (the re-read), unchanged;
a row's labels are never altered. The engine stamps the document's ``indexed_at`` on every manifest write, which
would switch off the attribution guard for the document's other rows, so the stamp is written back after each
batch exactly as the label rung does (``update_many``, one retry per document, a count of what could not be restored).

WHAT IT DOES.
  detect   : converged at once when this tenant's completion is on file. Otherwise the dry run (a scan with no write),
             which stops between batches at the ``nx upgrade --auto`` time limit and then reports the rung pending.
             Unattended (``--auto``) against a MANAGED service with no record it reads nothing and is not applicable:
             the scan is a walk of the whole catalog, and unlike the label rung there is no engine count to ask
             (nexus-75w5d). An interactive ``nx upgrade`` does the scan. Not applicable when no catalog is available
             or the engine cannot list tombstoned holders.
  converge : the same scan, writing each batch of :data:`DOC_BATCH` documents. DEFERRED unattended on a managed
             service, at the time limit, on a refused write, and on an indexed_at that could not be restored.
  verify   : positive signal only: a whole dry run found no fillable row.

RESIDUAL WINDOW. Between the re-read and the POST a row can gain a co-owner, which the engine cannot see: run the rung
on a quiet tenant. A concurrent re-index or label write of the same position is closed (nexus-tj0wu): the upsert takes
chash, labels and ``labels_written`` from the request, so every row carries ``expected`` (the chash, labels,
``labels_written`` and span columns just read) and the engine writes it only while the stored row still equals them,
else refuses that row, which the rung counts as ``raced``. A failed request does not strand the ``indexed_at`` restore: it runs in a
``finally`` for every document an earlier request wrote; a 409 on a request (a superseded collection) defers that
request's rows (``write_refused_409``) and the scan goes on.

THE CHUNK READ. The span comes from the T3 chunk row, a LEGACY read of the four POSITION keys (RDR-236 key table)
and of ``indexed_at`` and ``title`` for the attribution guards; it is made in :func:`_spans_from_chunk_row`, the one
entry this module has in ``tests/chunk_metadata_read_sites.toml``, and goes away with the keys at Phase 4.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from nexus.catalog.types import manifest_span
from nexus.collection_errors import SupersededCollectionWriteError
from nexus.upgrade_ladder.auto_guard import (
    INTERACTIVE_REMEDY,
    managed_service,
    CHEAP_PENDING,
    detect_refuses,
    refuses_managed_unattended,
    refuses_unbounded_read,
    unattended_verify_refusal,
)
from nexus.upgrade_ladder.scan_reuse import ScanReuse
from nexus.upgrade_ladder.unattended import is_cheap_detect
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.deadline import active_expired
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_MANIFEST_SPAN_FILL
from nexus.upgrade_ladder.rungs.manifest_section_labels import (
    DOC_BATCH,
    MAX_ROWS_PER_REQUEST,
    TITLE_RULE_MARK,
    ManifestSectionLabelsRung,
    RouteAbsent,
    CollectionMemo,
    _chunk_metadatas,
    _close,
    _default_reader,
    _default_t3,
    _default_writer,
    _row_collection,
    _title_names_document,
    expected_values,
    minted_by_orphan_backfill,
    refused_positions,
)

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_MANIFEST_SPAN_FILL

#: The manifest span columns (wire and ``ManifestRow`` names) with the chunk-row key each is copied from.
SPAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("line_start", "line_start"),
    ("line_end", "line_end"),
    ("char_start", "chunk_start_char"),
    ("char_end", "chunk_end_char"),
)


#: The buckets a row the engine counts can end in without being filled, in the order a row is tried, with the words
#: the summary uses. Disjoint: a row is in exactly one, or is ``fillable``.
LEFT_BUCKETS: tuple[tuple[str, str], ...] = (
    ("orphan_minted", "in documents the orphan backfill minted"),
    ("multi_row_cross_document", "shared with another document"),
    ("multi_row_in_document", "repeated inside the document"),
    ("tombstoned_co_owner", "also held by a deleted document"),
    ("stale_listing", "whose ownership listing named another row"),
    ("chunk_newer_than_document", "with a chunk row written after the document"),
    ("chunk_without_indexed_at", "with no indexed_at on the chunk row or the document"),
    ("chunk_titled_for_another", "with a chunk row titled for another document"),
    ("span_disagrees", "whose chunk row differs from a column the manifest already holds"),
)


@dataclass
class Counts:
    """What one scan saw.

    A row with a NULL span column is in exactly one of four places: ``chunk_missing`` / ``chunk_without_span_keys`` /
    ``chunk_span_unrecorded`` (the engine does not count it: no chunk row, no chunk key beside the NULL column, or only
    0s and non-numbers there), or ``counted``, the rows the engine's ``null_span_rows`` counts (a NULL manifest column
    whose chunk key records a value, by ``manifest_span``'s rule). A counted row is ``fillable`` or in exactly one of
    :data:`LEFT_BUCKETS`, and a left one has a recorded span that stays unused."""

    documents: int = 0
    rows: int = 0                        # manifest rows examined
    span_complete: int = 0               # no NULL span column: never touched
    null_rows: int = 0                   # a row with a chash and at least one NULL span column
    counted: int = 0                     # of those, the ones the engine's null_span_rows counts
    fillable: int = 0                    # exact for at least one NULL column (includes raced / write failures)
    filled: int = 0
    multi_row_cross_document: int = 0    # the chash has rows in more than one document
    multi_row_in_document: int = 0       # the chash recurs inside this one document
    tombstoned_co_owner: int = 0         # a deleted document also holds the chash
    stale_listing: int = 0               # the one row chash_positions names is not this row
    orphan_minted: int = 0               # rows of documents the orphan backfill minted: left alone on purpose
    chunk_missing: int = 0               # no live chunk row for the chash (the engine does not count it)
    chunk_without_span_keys: int = 0     # the chunk row carries none of the keys of the NULL columns (not counted)
    chunk_span_unrecorded: int = 0       # it carries them, but as 0 / not a number: nothing recorded (not counted)
    chunk_newer_than_document: int = 0   # the chunk row was written after the document
    chunk_without_indexed_at: int = 0    # no indexed_at on one side, so not attributable
    chunk_titled_for_another: int = 0    # the chunk row's title names another document's file
    span_disagrees: int = 0              # a column the manifest holds differs from the chunk row's recorded value
    raced: int = 0                       # moved, gained a co-owner, or was filled before the write
    write_failed: int = 0
    write_refused_409: int = 0           # the engine answered 409 to the request (a superseded collection)
    restore_failed: int = 0              # documents whose indexed_at could not be written back after the fill
    collection_lookups: int = 0          # engine reads, nexus-mjmjl's call counter: collection lookups (stats listings)
    chunk_gets: int = 0                  # chunk-metadata pages
    owner_reads: int = 0                 # chash_positions pages
    restore_failed_docs: list[str] = field(default_factory=list)
    by_collection: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, int]:
        return {k: v for k, v in vars(self).items() if isinstance(v, int)}

    def left(self) -> int:
        """Counted rows this pass leaves NULL for a named reason (everything but the fillable ones)."""
        return sum(getattr(self, name) for name, _w in LEFT_BUCKETS)

    def reconciles(self) -> bool:
        """Every row the scan counted ended fillable or in exactly one named bucket. This is the rung's own
        bookkeeping, not a comparison with the engine: ``counted`` is what the engine's ``null_span_rows`` reads for
        the documents this scan walks (it skips ``alias_of`` documents), and the operator compares the two with the
        key-retirement sweep's dry run."""
        return self.counted == self.fillable + self.left()

    def engine_count_after(self) -> int:
        """What the engine's ``null_span_rows`` reads once every fillable row is written (a dry run predicts it): a
        fillable row is written on every NULL column its chunk row records, so it leaves the count whole."""
        return self.counted - self.fillable

    def summary(self) -> str:
        left = "; ".join(
            f"{getattr(self, name)} {words}"
            for name, words in LEFT_BUCKETS
        )
        return (
            f"{self.documents} document(s), {self.rows} row(s): {self.span_complete} with a complete span, "
            f"{self.null_rows} with a NULL span column. Engine-comparable (a NULL column whose chunk key records a "
            f"value; the engine counts these as null_span_rows, over every live document where this scan skips "
            f"alias_of ones: compare with the key-retirement sweep's dry run): {self.counted} = {self.fillable} fillable "
            f"({self.filled} filled, {self.raced} changed before the write, {self.write_failed} write failed, "
            f"{self.write_refused_409} refused 409) + left NULL, each with a recorded span unused: {left}. "
            f"Engine count once they are written: {self.engine_count_after()}"
            f"{'' if self.reconciles() else ' [DOES NOT RECONCILE]'}. Not counted by the engine (nothing recorded): "
            f"{self.chunk_missing} chunk missing, {self.chunk_without_span_keys} chunk with no key for the NULL columns, "
            f"{self.chunk_span_unrecorded} chunk whose keys are 0 or not a number. "
            f"{self.restore_failed} indexed_at not restored"
        )


def _default_record() -> CompletionRecord | None:
    """This tenant's completion record for the rung, from the engine's ledger. Raises when it cannot be read."""
    from nexus.upgrade_ladder.http_store import HttpLadderStore  # noqa: PLC0415 — deferred: keeps the registry import cheap

    with HttpLadderStore() as store:
        return store.completions().get(RUNG_NAME)


def _spans_from_chunk_row(
    metadata: dict, doc: Any, wanted: tuple[str, ...],
) -> tuple[dict[str, int] | None, dict[str, int | None], str]:
    """``(span, recorded, why)`` for one chunk row and the document whose manifest row lacks the *wanted* columns.

    *recorded* is every span column the chunk row records (None: not recorded; ``manifest_span`` decides what
    counts, so a 0 is not a span). *span* is the part of it for the *wanted* columns, ``None`` with a *why* when the
    row may not give any: ``"no_span_keys"`` (it carries none of the keys of the wanted columns), ``"newer"`` (its
    ``indexed_at`` is later than the document's plus the skew: someone else wrote it), ``"no_indexed_at"`` (either
    side has none), ``"other_title"`` (its title names another document's file), ``"unrecorded"`` (it carries the
    keys, but records none of the wanted columns). *span* is never an empty dict. This is the module's only read of
    a chunk row's metadata."""
    from nexus.catalog.note_ttl import PUT_SKEW, parse_iso  # noqa: PLC0415 — deferred: note_ttl has a wide import graph

    keys = dict(SPAN_COLUMNS)
    recorded: dict[str, int | None] = manifest_span(
        metadata.get("line_start"), metadata.get("line_end"),
        metadata.get("chunk_start_char"), metadata.get("chunk_end_char"))
    if not any(keys[c] in metadata for c in wanted):
        return None, recorded, "no_span_keys"
    stamped = parse_iso(metadata.get("indexed_at"))
    ceiling = parse_iso(getattr(doc, "indexed_at", ""))
    if stamped is None or ceiling is None:
        return None, recorded, "no_indexed_at"
    if stamped - ceiling > PUT_SKEW:
        return None, recorded, "newer"
    if not _title_names_document(metadata.get("title") or "", doc):
        return None, recorded, "other_title"
    span = {c: v for c in wanted if (v := recorded[c]) is not None}
    return (span, recorded, "") if span else (None, recorded, "unrecorded")


class ManifestSpanFillRung:
    """The rung. Collaborators are injected for tests, as the label rung's: *reader_factory* (``all_documents``,
    ``get_manifests``, ``chash_positions_with_tombstoned``, ``close``; ``None`` when no catalog is available),
    *writer_factory* (``append_manifest_many``, ``update_many``, ``update``, ``close``) and *t3_factory*
    (``get_collection``)."""

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
        managed_fn: Callable[[], bool] = managed_service,
    ) -> None:
        self._managed_fn = managed_fn
        self._reader_factory = reader_factory
        self._writer_factory = writer_factory
        self._t3_factory = t3_factory
        self._doc_batch = doc_batch
        self._max_rows = max_rows
        self._record_fn = record_fn
        self._cut = False  # the last scan stopped at the time limit
        self._verify_deferred = False
        self._filled = 0
        self._verify_detail = ""
        self._record_detail = ""
        self._clean_scan: ScanReuse[Counts] = ScanReuse()  # converge's walk that found nothing to fill
        self._reporter: ProgressReporter | None = None  # converge's, so verify's walk reports too
        self._partial = Counts()

    # ── the scan ─────────────────────────────────────────────────────────────

    def _scan(
        self, reader: Any, *, writer: Any | None, t3: Any, report: ProgressReporter | None = None,
        honour_deadline: bool = False, event: str = "manifest_span_fill_batch",
    ) -> Counts | None:
        """One pass over every live document; writes when *writer* is given. Returns the counts, or ``None`` when
        the ``nx upgrade --auto`` limit passed between batches (``self._cut`` is set). A failed read propagates."""
        counts = Counts()
        self._partial = counts
        self._cut = False
        t3 = CollectionMemo(t3) if t3 is not None else None
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
                report.emit(event, **counts.as_dict())
        return counts

    @staticmethod
    def _null_columns(row: Any) -> tuple[str, ...]:
        return tuple(c for c, _k in SPAN_COLUMNS if getattr(row, c, None) is None)

    def _batch(self, reader: Any, writer: Any | None, t3: Any, docs: list[Any], counts: Counts) -> None:
        by_tumbler = {str(d.tumbler): d for d in docs}
        manifests = reader.get_manifests(sorted(by_tumbler)) or {}
        counts.documents += len(docs)
        candidates: list[tuple[Any, Any, tuple[str, ...]]] = []  # (document, row, NULL columns)
        for tumbler, doc in by_tumbler.items():
            for row in manifests.get(tumbler, []):
                counts.rows += 1
                if not getattr(row, "chash", ""):
                    continue
                nulls = self._null_columns(row)
                if not nulls:
                    counts.span_complete += 1
                    continue
                counts.null_rows += 1
                candidates.append((doc, row, nulls))
        if not candidates:
            return

        # Ownership first (no chunk read needed to decide it); the verdict is a bucket name, or None for a single owner.
        live = [(doc, row) for doc, row, _n in candidates if not minted_by_orphan_backfill(doc)]
        owners, deleted = (
            ManifestSectionLabelsRung._owner_rows(reader, sorted({r.chash for _d, r in live}), counts) if live else ({}, {})
        )
        verdict: dict[int, str | None] = {}
        for i, (doc, row, _n) in enumerate(candidates):
            if minted_by_orphan_backfill(doc):
                verdict[i] = "orphan_minted"
                continue
            held = owners.get(row.chash, [])
            if len(held) > 1:
                same_doc = len({str(h["doc_id"]) for h in held}) == 1
                verdict[i] = "multi_row_in_document" if same_doc else "multi_row_cross_document"
            elif len(held) == 1 and (str(held[0]["doc_id"]), int(held[0]["position"])) == (str(doc.tumbler), int(row.position)):
                verdict[i] = "tombstoned_co_owner" if deleted.get(row.chash) else None
            else:
                verdict[i] = "stale_listing"

        # The chunk row of EVERY candidate, not only the single-owner ones: a row left NULL for ownership is reported
        # as lossy (a recorded span goes unused) or lossless (nothing recorded), which the summary cannot say unread.
        wanted: dict[str, list[str]] = {}
        for doc, row, _n in candidates:
            wanted.setdefault(_row_collection(row, doc), []).append(row.chash)
        chunk_metas: dict[tuple[str, str], dict] = {}
        for collection, chashes in wanted.items():
            for chash, meta in _chunk_metadatas(t3, collection, sorted(set(chashes)), counts).items():
                chunk_metas[(collection, chash)] = meta

        # (document, row, collection, columns to fill)
        plan: list[tuple[Any, Any, str, dict[str, int]]] = []
        for i, (doc, row, nulls) in enumerate(candidates):
            collection = _row_collection(row, doc)
            meta = chunk_metas.get((collection, row.chash))
            if meta is None:
                counts.chunk_missing += 1
                continue
            span, recorded, why = _spans_from_chunk_row(meta, doc, nulls)
            if why == "no_span_keys":
                counts.chunk_without_span_keys += 1
                continue
            if all(recorded[c] is None for c in nulls):
                # the keys are there as 0 or not a number: the engine does not count the row (it counts by value), and
                # there is nothing to copy or lose, whoever owns the chash
                counts.chunk_span_unrecorded += 1
                continue
            counts.counted += 1

            def leave(bucket: str) -> None:
                setattr(counts, bucket, getattr(counts, bucket) + 1)

            if verdict[i] is not None:
                leave(verdict[i])  # type: ignore[arg-type]
                continue
            if span is None:
                leave({
                    "newer": "chunk_newer_than_document",
                    "no_indexed_at": "chunk_without_indexed_at", "other_title": "chunk_titled_for_another",
                }[why])
                continue
            if any(
                getattr(row, c) is not None and recorded[c] is not None and getattr(row, c) != recorded[c]
                for c, _k in SPAN_COLUMNS
            ):
                # The chunk row contradicts a column the manifest holds: it describes another position, so none of its
                # values is trusted for this row (a fill would join lines of one writer to chars of another).
                leave("span_disagrees")
                continue
            plan.append((doc, row, collection, span))
        counts.fillable += len(plan)
        if writer is None or not plan:
            return
        self._write(reader, writer, plan, counts)

    def _write(
        self, reader: Any, writer: Any, plan: list[tuple[Any, Any, str, dict[str, int]]], counts: Counts,
    ) -> None:
        """Re-read the batch just before writing, drop what changed, and append the rest per collection."""
        docs = sorted({str(d.tumbler) for d, _r, _c, _s in plan})
        stamps = {str(d.tumbler): getattr(d, "indexed_at", "") for d, _r, _c, _s in plan}  # as read at scan
        fresh = reader.get_manifests(docs) or {}
        fresh_rows = {t: {r.position: r for r in rows} for t, rows in fresh.items()}
        again, again_deleted = ManifestSectionLabelsRung._owner_rows(
            reader, sorted({r.chash for _d, r, _c, _s in plan}), counts,
        )
        by_collection: dict[str, dict[str, list[dict]]] = {}
        for doc, row, collection, span in plan:
            tumbler = str(doc.tumbler)
            now = fresh_rows.get(tumbler, {}).get(row.position)
            if (
                now is None or now.chash != row.chash
                or len(again.get(row.chash, [])) != 1 or again_deleted.get(row.chash)
            ):
                counts.raced += 1
                continue
            if any(getattr(now, c, None) is not None and getattr(now, c) != v for c, v in span.items()):
                # another writer filled a column since the scan with a value the chunk row contradicts: filling the
                # rest from the chunk row would join two positions' spans (the scan-time span_disagrees rule)
                counts.raced += 1
                continue
            columns = {c: v for c, v in span.items() if getattr(now, c, None) is None}
            if not columns:  # someone filled every column meanwhile, with the chunk row's values
                counts.raced += 1
                continue
            by_collection.setdefault(collection, {}).setdefault(tumbler, []).append({
                "chash": now.chash, "position": now.position, **columns,
                # the upsert rewrites the labels from the request: send the row's own, as just read
                "section_title": now.section_title, "section_type": now.section_type,
                "page_number": now.page_number, "labels_written": bool(getattr(now, "labels_written", False)),
                "expected": expected_values(now),
            })
        written: set[str] = set()
        in_flight: list[str] = []  # the request being sent: a raise after the engine committed it restamped these too
        try:
            for collection, per_doc in sorted(by_collection.items()):
                refused = False
                for request in self._requests(sorted(per_doc.items())):
                    if refused:  # the collection answered 409 once: it will again, and the rows are counted
                        counts.write_refused_409 += sum(len(rows) for _d, rows in request)
                        continue
                    in_flight = [d for d, _rows in request]
                    try:
                        result = writer.append_manifest_many(request, collection=collection)
                    except (SupersededCollectionWriteError, httpx.HTTPStatusError) as exc:
                        # the client turns the engine's 409 collection_superseded into SupersededCollectionWriteError;
                        # any other 409 arrives as the bare status error
                        if isinstance(exc, httpx.HTTPStatusError) and (
                            exc.response is None or exc.response.status_code != 409
                        ):
                            raise
                        in_flight = []  # refused before any write
                        refused = True
                        counts.write_refused_409 += sum(len(rows) for _d, rows in request)
                        _log.warning("manifest_span_fill_write_refused_409", collection=collection, error=str(exc))
                        continue
                    in_flight = []
                    failed = {str(d) for d in (result or {}).get("failed_doc_ids") or []}
                    lost_positions = refused_positions(result)
                    for doc_id, rows in request:
                        if doc_id in failed:
                            counts.write_failed += len(rows)
                            continue
                        lost = sum(1 for r in rows if (doc_id, int(r["position"])) in lost_positions)
                        counts.raced += lost
                        if len(rows) > lost:
                            counts.filled += len(rows) - lost
                            counts.by_collection[collection] += len(rows) - lost
                            self._filled += len(rows) - lost
                            written.add(doc_id)
        finally:
            # also when a later request raised: the engine restamped indexed_at on every document an earlier request
            # wrote, and left alone that stamp switches the chunk-newer guard off for the document's other rows
            ManifestSectionLabelsRung._restore_batch(writer, sorted(written | set(in_flight)), stamps, counts)  # type: ignore[arg-type]  # reads only the restore counters

    def _requests(self, docs_rows: list[tuple[str, list[dict]]]) -> list[list[tuple[str, list[dict]]]]:
        """Cut ``(doc_id, rows)`` pairs into requests of at most ``max_rows`` rows and ``doc_batch`` documents; a
        document's rows spill into the next request."""
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

    # ── reading with the lifecycle of the collaborators ──────────────────────

    def _dry_run(self, *, honour_deadline: bool = False, report: ProgressReporter | None = None) -> Counts | None:
        """The counts of a read-only scan, or ``None`` when no catalog is available (or, with *honour_deadline*,
        when the time limit stopped it: ``self._cut``)."""
        self._cut = False
        if honour_deadline and active_expired():
            self._cut = True
            return None
        reader = self._reader_factory()
        if reader is None:
            return None
        t3 = self._t3_factory()
        try:
            return self._scan(
                reader, writer=None, t3=t3, honour_deadline=honour_deadline, report=report, event="manifest_span_fill_confirm_batch",
            )
        finally:
            _close(t3)
            _close(reader)

    # ── detect ───────────────────────────────────────────────────────────────

    def _record(self) -> CompletionRecord | None:
        """This tenant's completion record, or ``None`` (none on file, or an unreadable ledger, where the scan
        decides)."""
        try:
            return self._record_fn()
        except Exception as exc:  # noqa: BLE001 — an unreadable ledger is 'not recorded', and the scan decides
            _log.info("manifest_span_fill_ledger_unreadable", error=str(exc))
            return None

    def _recorded(self) -> bool:
        """True when this tenant's completion is on file under the current title rule, at any package version. A
        one-time transition: rows that stay NULL by design stay NULL."""
        record = self._record()
        return record is not None and TITLE_RULE_MARK in (getattr(record, "detail", "") or "")

    def detect(self) -> RungStatus:
        """READ-ONLY. Converged at once from the completion record; not applicable, with no read, unattended against
        a managed service (the answer is a walk of the whole catalog and no engine count exists to shorten it);
        otherwise the dry run, which honours the ``--auto`` time limit and then says pending."""
        record = self._record()
        if record is not None and TITLE_RULE_MARK in (getattr(record, "detail", "") or ""):
            return RungStatus(applicable=True, converged=True)
        if record is not None and not refuses_managed_unattended(self._managed_fn):
            # Written before the title rule stripped the document title's suffix (nexus-l8o33): pending once, so the
            # walk's converge, verify and record rewrite it with the mark even when nothing turns out fillable.
            return RungStatus(
                applicable=True, converged=False,
                pending_detail=(
                    f"the completion record predates the current title rule ({TITLE_RULE_MARK}): the rows the old "
                    "rule refused are scanned once more (`nx upgrade`)"
                ),
            )
        if detect_refuses(self._managed_fn):
            reason = (
                "unattended `nx upgrade --auto` does not scan a managed catalog for manifest rows that lack a span "
                f"(no completion record, and the engine has no count for it); {INTERACTIVE_REMEDY}"
            )
            _log.info("manifest_span_fill_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if is_cheap_detect():
            return RungStatus(applicable=True, converged=False, pending_detail=CHEAP_PENDING)
        try:
            counts = self._dry_run(honour_deadline=True)
        except RouteAbsent as exc:
            _log.info("manifest_span_fill_rung_skipped", reason=str(exc))
            return RungStatus(applicable=False, converged=False, pending_detail=str(exc))
        if counts is None and self._cut:
            return RungStatus(
                applicable=True, converged=False,
                pending_detail=(
                    "the `nx upgrade --auto` time limit stopped the dry run before it could tell whether any "
                    f"manifest row can take its chunk row's span (read {self._partial.documents} document(s)); "
                    "`nx upgrade` checks and fills"
                ),
            )
        if counts is None:
            reason = "no catalog is available"
            _log.info("manifest_span_fill_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if not counts.fillable:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True, converged=False,
            pending_detail=(
                f"{counts.fillable} manifest row(s) of {counts.null_rows} with a NULL span column can take their "
                f"chunk row's span (dry run: {counts.summary()})"
            ),
        )

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        if refuses_unbounded_read(self._managed_fn):
            reason = (
                "unattended `nx upgrade --auto` does not fill manifest spans of a managed service "
                f"(a scan of every document); {INTERACTIVE_REMEDY}"
            )
            _log.info("manifest_span_fill_converge_skipped", reason=reason)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=reason)
        self._clean_scan.clear()
        self._reporter = report
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
            report.emit("manifest_span_fill_deadline", filled=self._filled)
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the `nx upgrade --auto` time limit passed after {self._filled} row(s); the fill "
                    f"resumes on the next `nx upgrade` ({self._partial.summary()})"
                ),
            )
        if counts.write_failed or counts.write_refused_409:
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the engine refused the write for {counts.write_failed} row(s) and answered 409 for "
                    f"{counts.write_refused_409} (a superseded collection); retried on the next `nx upgrade` "
                    f"({counts.summary()})"
                ),
            )
        if counts.restore_failed:
            named = ", ".join(counts.restore_failed_docs[:10]) + (" ..." if len(counts.restore_failed_docs) > 10 else "")
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the fill could not write indexed_at back on {counts.restore_failed} document(s) ({named}); "
                    f"their stamp is now the run time, which switches off the chunk-newer guard for their "
                    f"remaining NULL-span rows ({counts.summary()})"
                ),
            )
        if not counts.fillable:
            # Nothing to fill means nothing written: this walk is the dry run verify would take next.
            self._clean_scan.keep(counts)
        return ConvergeResult(ConvergeOutcome.COMPLETED, detail=counts.summary())

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """True only when a whole scan completed and no row can still take a span. A missing or unreadable catalog
        is not 'nothing to fill': it is False, with :meth:`verify_detail`."""
        self._verify_detail = ""
        self._verify_deferred = False
        if refuses_unbounded_read(self._managed_fn):
            self._verify_deferred = True
            self._verify_detail = unattended_verify_refusal("every document and manifest")
            return False
        try:
            # nexus-mjmjl: a confirming walk after a fill prints the same progress lines as the fill.
            counts = self._clean_scan.take() or self._dry_run(report=self._reporter)
        except Exception as exc:  # noqa: BLE001 — an unreadable catalog is unknown, and unknown is not reached
            self._verify_detail = f"could not read the catalog to confirm the fill: {exc}"
            return False
        if counts is None:
            self._verify_detail = "cannot confirm the fill: no catalog is available"
            return False
        if counts.fillable:
            self._verify_detail = f"{counts.fillable} manifest row(s) can still take their chunk row's span ({counts.summary()})"
            return False
        self._record_detail = f"{self._filled} filled this run; {counts.summary()}; {TITLE_RULE_MARK}"
        return True

    def verify_deferred(self) -> bool:
        return self._verify_deferred

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail
