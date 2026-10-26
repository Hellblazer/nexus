# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared manifest-gap heal core (nexus-8g0ch + nexus-c21fk).

One implementation of "find documents whose ``document_chunks`` manifest is
missing rows and rebuild it from the T3 chunks already stored" — consumed by
BOTH ``nx catalog reconcile`` (operator verb, whole-catalog scope) and the
``nx index repo`` catalog hook (self-heal pass, owner scope).

Why the indexer needs it (nexus-c21fk, GH #1397 field find): the staleness
check keys on T3 chunk state only — a document whose chunks exist in T3 but
whose manifest rows were dropped (interrupted run, failed hook) is skipped
as "current" forever, and the post-store manifest hooks that skipped files
never trigger can never repair it. The documented self-heal advice
("re-index repairs the manifest") was structurally wrong for exactly the
documents that needed it. This core rebuilds the manifest WITHOUT
re-embedding: the chunk rows are fetched back from T3 (batched ``$in`` over
whole-file content hashes, 8g0ch), ordered by their spans, and ADDED to the
manifest.

The heal never writes into a document that has manifest rows (nexus-8o06q). A T3
chunk row is shared by every document holding that text and its ``content_hash`` is
the LAST writer's, so the fetch cannot see a chunk the document shares with a
document that wrote it last, and it can see rows of an older chunker run. Neither
the rows it would add nor the position they belong at can be trusted for a document
that already has a manifest: a chunk missing from the middle would land after the
last row and, once the count matched, hide the gap for good.

* A gapped document WITH manifest rows is not touched. The heal stamps its RUNFENCE
  ``index_state='failed'``, and the next index run that walks its source re-indexes it
  (every staleness path treats a failed fence as stale: ``doc_indexer._index_run_fresh``,
  ``StalenessCache.never_fresh`` fed by the catalog pass for the code, prose and PDF
  indexers, ``_pdfs_needing_index``). The re-index rewrites the manifest at the right
  positions and skips re-embedding text that is unchanged. These documents are
  ``ManifestHealResult.reindex``. Nothing is appended and the document's
  ``chunk_count`` is not changed, so it stays gapped and is listed again on every
  pass until a re-index completes.
* A document with NO manifest rows is rebuilt from the fetch, with
  ``append_manifest_chunks`` (it cannot delete a row a concurrent writer landed;
  the document is re-read just before, and one whose fence is ``'indexing'`` is
  skipped). The append folds ``chunk_count`` to the number of rows rebuilt, in its
  own transaction; the heal never writes ``chunk_count`` itself. If the rebuild holds
  fewer rows than ``chunk_count`` (repeated text inside one document keeps one
  manifest row per position but one T3 row, so the heal cannot tell that from a lost
  chunk) the fence is failed BEFORE the write, so a crash in between leaves the
  document marked, and the document is also in ``reindex``. After the append its
  rows equal its folded ``chunk_count``, so it is no longer gapped and a later pass
  does not list it again; its failed fence is what makes the next index run that
  walks its source re-index it, and that restores the count. If the rebuild holds
  more rows than ``chunk_count`` (rows of an older chunker run) nothing is written
  and the fence is failed; that document still has no rows and is listed again until
  a re-index.

A marked document stays marked until an index run re-indexes it. One whose source
the run does not walk stays ``failed``: ``--since-head`` offers only changed files, a
deleted file offers none, and a document indexed by ``nx index pdf``/``md``/``rdr``
or written by ``nx store put`` is not in an ``nx index repo`` walk at all (re-run the
verb that indexed it; for a note, ``nx store put`` again under the same collection
and title). ``nx t3 gc`` will not move chunks out of a collection that holds a
non-complete document unless ``--allow-incomplete-index-state`` is passed.

Never-chunked documents (``chunk_count == 0`` and T3 genuinely has no rows —
e.g. empty ``__init__.py``) are reported as expected, not as gaps: the live
2026-07-13 heal run found ~8.6k of them, and burying a real chunks-LOST
regression under that noise was itself a defect.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import structlog

_log = structlog.get_logger(__name__)

#: Hashes per $in predicate. One top-level predicate (MAX_WHERE_PREDICATES
#: bounds predicate COUNT, not $in array size — no named constant bounds
#: that; 64 verified live against the pgvector engine). Documented tradeoff
#: (critique d470eda1): a batch-fetch failure marks up to this many docs
#: unmatched at once, vs 1 under the old per-doc loop — accepted because
#: _paginated_get already retries transients internally, so a surviving
#: failure is almost always collection-wide anyway.
IN_BATCH = 64


@dataclass(frozen=True)
class ReindexDoc:
    """A document the heal left for the next index run to rebuild."""

    entry: Any
    #: Rows its manifest holds after the heal (would hold, in a dry run).
    manifest_rows: int
    #: What the document said it held when the pass started. The heal never writes
    #: it; for a manifest-less document the append that rebuilds it folds the stored
    #: count to the rows written, so a rebuilt-short document ends at
    #: ``manifest_rows``, not at this value.
    chunk_count: int


@dataclass
class ManifestHealResult:
    """Outcome of one heal pass."""

    candidates: int = 0
    gapped: int = 0
    ghost_gapped: int = 0  # gapped subset with chunk_count == 0
    #: Documents the heal wrote manifest rows into (would write, in a dry run):
    #: only documents that had none.
    reconciled: int = 0
    write_failed: int = 0
    #: Documents the heal marked for re-index (nexus-8o06q): their RUNFENCE
    #: ``index_state`` is ``'failed'`` (stamped by this pass, or already so; in a
    #: dry run, would be) and the next index run that walks their source
    #: re-indexes them. Either
    #: the document has manifest rows and fewer than its ``chunk_count`` (the heal
    #: writes nothing into it), or it had none and the rebuild is short of
    #: ``chunk_count`` (written, so it is also in ``reconciled``) or longer than
    #: it (not written).
    reindex: list = field(default_factory=list)
    #: Gapped documents whose fence is ``'indexing'``: a run may be writing them
    #: now, so the heal leaves them alone. An indexing fence is stale to the next
    #: index run on its own.
    skipped_indexing: list = field(default_factory=list)
    #: Entries that could not be matched to T3 chunks, split by class:
    #: ``lost`` had chunk_count > 0 and NO manifest rows (a REAL gap: chunks
    #: are gone); ``never_chunked`` had chunk_count == 0 (expected: nothing to
    #: rebuild). A gapped document that has manifest rows is ``reindex``.
    lost: list = field(default_factory=list)
    never_chunked: list = field(default_factory=list)

    @property
    def unmatched(self) -> list:
        return self.lost + self.never_chunked


def _order_key(pair: tuple) -> tuple:
    """RDR-108 Phase 3 dropped chunk_index from chunk metadata, so the
    char/line span is the only ordering signal left; fall back to
    line_start, then to a stable id sort so identically-spanned rows
    (both absent) still get a deterministic order."""
    _cid, m = pair
    m = m or {}
    start = m.get("chunk_start_char")
    if start is None:
        start = m.get("line_start")
    return (0, start, _cid) if start is not None else (1, 0, _cid)


def _content_hash(entry: Any) -> str:
    """The whole-file hash the entry's chunks carry. ``meta.content_hash``
    is written when a run completes; a document whose first run failed has
    none yet, so fall back to ``index_content_hash``, the hash the fence
    begin recorded for the run that wrote the chunks (nexus-0ntxj)."""
    return (entry.meta or {}).get("content_hash") or getattr(entry, "index_content_hash", "") or ""


def heal_manifest_gaps(
    entries: list,
    cat: Any,
    t3_factory: Callable[[], Any],
    writer_factory: Callable[[], Any] | None,
    *,
    dry_run: bool = False,
    echo: Callable[[str], None] | None = None,
) -> ManifestHealResult:
    """Detect manifest gaps for *entries*; rebuild the manifest-less ones and
    mark the rest for re-index.

    Args:
        entries: Candidate ``CatalogEntry`` rows (any scope — whole catalog
            for the reconcile verb, one owner for the indexer self-heal).
            Filtered here to indexable candidates (``chunk_count > 0`` OR
            the ghost shape: recorded content_hash + physical_collection).
        cat: Catalog READER (``get_manifests``, ``by_doc_id``).
        t3_factory: Zero-arg factory for the T3 handle. Called LAZILY,
            only after gap detection finds a document with no manifest
            rows (review 4711f521 Medium-1: the eager form constructed a
            real writer + T3 client on every nothing-to-do invocation).
            The core closes what it creates.
        writer_factory: Zero-arg factory for the catalog WRITER
            (``append_manifest_chunks`` + ``fail_index_run``). May be
            ``None`` only when *dry_run* is True. Same lazy + close
            contract as *t3_factory*.
        dry_run: Report without writing and without failing a fence.
        echo: Optional line sink for progress output (the observability
            half of nexus-8g0ch: a silent long walk reads as a hang).
            ``None`` degrades to structlog-only.
    """
    say = echo or (lambda _s: None)
    result = ManifestHealResult()

    candidates = [
        e for e in entries
        if e.chunk_count > 0
        or (_content_hash(e) and e.physical_collection)
    ]
    result.candidates = len(candidates)
    if not candidates:
        return result

    manifests = cat.get_manifests([str(e.tumbler) for e in candidates])
    gapped = [
        e for e in candidates
        if len(manifests.get(str(e.tumbler), [])) < e.chunk_count
        or (e.chunk_count == 0 and not manifests.get(str(e.tumbler)))
    ]
    result.gapped = len(gapped)
    result.ghost_gapped = sum(1 for e in gapped if e.chunk_count == 0)
    if not gapped:
        return result

    with_rows = [e for e in gapped if manifests.get(str(e.tumbler))]
    without_rows = [e for e in gapped if not manifests.get(str(e.tumbler))]
    say(
        f"Scanning: {result.candidates} candidate(s), {result.gapped} gapped "
        f"({result.ghost_gapped} ghost-class chunk_count==0); "
        f"{len(with_rows)} with manifest rows are marked for re-index, "
        f"{len(without_rows)} without are rebuilt from T3 chunks…"
    )

    # Lazy instantiation (review 4711f521 Medium-1): only a run with real
    # gaps pays for a catalog writer, and only a manifest-less document pays
    # for a T3 client. The core closes both.
    writer = writer_factory() if (writer_factory and not dry_run) else None
    t3 = None
    healer = _Healer(cat, writer, result, manifests, dry_run)

    try:
        for entry in with_rows:
            healer.reindex_if_still_gapped(entry)

        by_coll: dict[str, list] = defaultdict(list)
        for entry in without_rows:
            content_hash = _content_hash(entry)
            if not content_hash or not entry.physical_collection:
                healer.classify_unmatched(entry)
                continue
            by_coll[entry.physical_collection].append((entry, content_hash))

        if by_coll:
            from nexus.indexer import _paginated_get  # noqa: PLC0415 — deferred: nexus.indexer is heavy and imports back into catalog
            from nexus.mcp_infra import _manifest_chunk_rows  # noqa: PLC0415 — deferred: circular-dep avoidance

            t3 = t3_factory()
            _heal_collections(
                by_coll, t3, healer, say,
                paginated_get=_paginated_get,
                manifest_chunk_rows=_manifest_chunk_rows,
            )
    finally:
        for handle in (t3, writer):
            close = getattr(handle, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 — best-effort handle cleanup
                    pass

    return result


class _Healer:
    """The per-document decisions and their writes, over one heal pass."""

    def __init__(
        self, cat: Any, writer: Any | None, result: ManifestHealResult,
        manifests: dict, dry_run: bool,
    ) -> None:
        self.cat = cat
        self.writer = writer
        self.result = result
        self.manifests = manifests
        self.dry_run = dry_run

    def classify_unmatched(self, entry: Any) -> None:
        """No chunk of the document's hash can be found. A document with rows
        never reaches here (it goes to ``reindex``); one without is lost or
        was never chunked."""
        (self.result.never_chunked if entry.chunk_count == 0
         else self.result.lost).append(entry)

    def fresh(self, entry: Any) -> tuple[str | None, int, int]:
        """The document's fence state, manifest row count and ``chunk_count``
        as of now.

        The pass works from a snapshot taken at its start; an index run may
        have landed rows, moved ``chunk_count`` or flipped the fence since. A
        dry run writes nothing, so it keeps the snapshot. A failed re-read keeps
        the snapshot too.
        """
        tumbler = str(entry.tumbler)
        state = getattr(entry, "index_state", None)
        count = entry.chunk_count
        rows = len(self.manifests.get(tumbler, []))
        if self.dry_run:
            return state, rows, count
        try:
            current = self.cat.by_doc_id(tumbler)
            if current is not None:
                state = getattr(current, "index_state", None)
                count = current.chunk_count
            rows = len(self.cat.get_manifests([tumbler]).get(tumbler, []))
        except Exception as exc:  # noqa: BLE001 — a failed re-read falls back to the snapshot, never aborts the pass
            _log.warning("manifest_heal_reread_failed", tumbler=tumbler, error=str(exc))
        return state, rows, count

    def mark_for_reindex(
        self, entry: Any, state: str | None, held: int, rows_after: int, why: str,
    ) -> bool:
        """Fail the document's fence so the next index run re-indexes it.

        Returns True when the document is marked (or already was, or would be
        in a dry run). A document whose fence is ``'indexing'`` is skipped.
        """
        if state == "indexing":
            self.result.skipped_indexing.append(entry)
            return False
        if not self.dry_run and state != "failed":
            try:
                self.writer.fail_index_run(
                    str(entry.tumbler),
                    f"manifest_heal: {why}; {held} of {entry.chunk_count} "
                    "manifest rows; re-index",
                )
            except Exception as exc:  # noqa: BLE001 — critique 4711f521 Sig-3: a mid-pass write failure must not lose the progress accounting
                _log.warning(
                    "manifest_heal_fence_fail_failed",
                    tumbler=str(entry.tumbler), error=str(exc),
                )
                self.result.write_failed += 1
                return False
        self.result.reindex.append(
            ReindexDoc(entry, rows_after, entry.chunk_count))
        return True

    def reindex_if_still_gapped(self, entry: Any) -> None:
        """A gapped document that has manifest rows: write nothing into it."""
        state, rows, count = self.fresh(entry)
        # Judge against the document as it is NOW, not the snapshot: a run that
        # re-indexed it since may have moved its chunk_count (a snapshot count of
        # 3 against 2 rows would fail the fence a run just completed). A
        # completed fence means rows == chunk_count, so this also keeps the heal
        # off a fence a run moved to 'complete'; a fence a run began is
        # 'indexing' and mark_for_reindex skips it.
        if rows >= count:
            return  # no longer gapped: a run completed it since the snapshot
        self.mark_for_reindex(
            entry, state, rows, rows, "manifest rows kept, not rebuilt")


def _heal_collections(
    by_coll: dict,
    t3: Any,
    healer: _Healer,
    say: Callable[[str], None],
    *,
    paginated_get: Any,
    manifest_chunk_rows: Any,
) -> None:
    """The per-collection fetch + rebuild walk for documents with no manifest
    rows (split from the entry point so handle cleanup wraps it in one place)."""
    _paginated_get = paginated_get
    _manifest_chunk_rows = manifest_chunk_rows
    result = healer.result
    writer = healer.writer
    for coll_i, (coll_name, pairs) in enumerate(sorted(by_coll.items()), 1):
        try:
            col = t3.get_collection(coll_name)
        except Exception as exc:  # noqa: BLE001 — boundary catch; one collection's failure must not abort the pass
            _log.warning(
                "manifest_heal_collection_unavailable",
                collection=coll_name, docs=len(pairs), error=str(exc),
            )
            for e, _ in pairs:
                healer.classify_unmatched(e)
            say(
                f"  [{coll_i}/{len(by_coll)}] {coll_name}: "
                f"UNAVAILABLE — {len(pairs)} doc(s) unmatched"
            )
            continue

        # hash -> (ids, metas), shared across docs with identical content.
        rows_by_hash: dict[str, tuple[list, list]] = defaultdict(
            lambda: ([], [])
        )
        hashes = sorted({ch for _, ch in pairs})
        fetched_rows = 0
        fetch_failed_docs = 0
        for i in range(0, len(hashes), IN_BATCH):
            batch = hashes[i:i + IN_BATCH]
            try:
                # nexus-wbfpw.10 (RDR-192 Step 5 amendment): a manifest-less
                # chunk (the unmanifested-gap shape this heal exists to
                # rebuild) has no live own-collection manifest owner --
                # live(c) hides exactly the population being healed unless
                # this asks for the physical scan.
                fetched = _paginated_get(
                    col, include=["metadatas"],
                    where={"content_hash": {"$in": batch}},
                    include_non_live=True,
                )
            except Exception as exc:  # noqa: BLE001 — boundary catch; parity with the old per-doc failure semantics
                _log.warning(
                    "manifest_heal_t3_fetch_failed",
                    collection=coll_name, batch_size=len(batch),
                    error=str(exc),
                )
                batch_set = set(batch)
                dropped = [e for e, ch in pairs if ch in batch_set]
                for e in dropped:
                    healer.classify_unmatched(e)
                fetch_failed_docs += len(dropped)
                pairs = [(e, ch) for e, ch in pairs if ch not in batch_set]
                continue
            for cid, m in zip(
                fetched.get("ids") or [], fetched.get("metadatas") or [],
            ):
                h = (m or {}).get("content_hash", "")
                if h:
                    rows_by_hash[h][0].append(cid)
                    rows_by_hash[h][1].append(m)
                    fetched_rows += 1
        # Review d470eda1 Medium-1: a batch-fetch failure mid-collection
        # must be VISIBLE in the progress line, not just structlog.
        fetch_note = (
            f", {fetch_failed_docs} doc(s) unmatched (fetch error)"
            if fetch_failed_docs else ""
        )
        say(
            f"  [{coll_i}/{len(by_coll)}] {coll_name}: {len(pairs)} doc(s), "
            f"{fetched_rows} chunk row(s) fetched{fetch_note}"
        )

        for entry, content_hash in pairs:
            ids, metas = rows_by_hash.get(content_hash, ([], []))
            if not ids:
                healer.classify_unmatched(entry)
                continue
            tumbler = str(entry.tumbler)
            ordered = sorted(zip(ids, metas), key=_order_key)
            indexed_metas = []
            for k, (cid, m) in enumerate(ordered):
                m = dict(m or {})
                if not m.get("chunk_text_hash"):
                    m["chunk_text_hash"] = cid
                m["chunk_index"] = k
                indexed_metas.append((k, m))
            chunks = _manifest_chunk_rows(indexed_metas)
            if not any(c["chash"] for c in chunks):
                healer.classify_unmatched(entry)
                continue
            n = len(chunks)
            cc = entry.chunk_count

            # The pass decided "no manifest rows" from its start snapshot; a
            # run may have landed rows or begun since.
            state, rows, _count = healer.fresh(entry)
            if rows:
                healer.reindex_if_still_gapped(entry)
                continue
            if state == "indexing":
                result.skipped_indexing.append(entry)
                continue

            if cc > 0 and n > cc:
                # More rows than the document holds: some belong to an older
                # chunker run of the same file (the fetch includes rows with no
                # live owner). Writing them would also fill a position the
                # document does not have; re-indexing decides.
                healer.mark_for_reindex(
                    entry, state, 0, 0,
                    f"T3 holds {n} chunk rows for its hash, more than chunk_count")
                continue
            if 0 < cc and n < cc:
                # Short: a lost chunk, or repeated text the heal cannot tell
                # from one. Mark BEFORE writing, so a crash between the two
                # leaves the document marked, not hidden.
                if not healer.mark_for_reindex(
                        entry, state, n, n, "rebuilt short of chunk_count"):
                    continue
            if not healer.dry_run:
                try:
                    # Append, not replace: it cannot delete a row a writer
                    # landed since the re-read above. It folds chunk_count to
                    # the row count in its own transaction.
                    writer.append_manifest_chunks(
                        tumbler, chunks, collection=coll_name)
                except Exception as exc:  # noqa: BLE001 — critique 4711f521 Sig-3: a mid-pass write failure must not lose the progress accounting
                    _log.warning(
                        "manifest_heal_write_failed",
                        tumbler=tumbler, error=str(exc),
                    )
                    result.write_failed += 1
                    continue
            result.reconciled += 1
