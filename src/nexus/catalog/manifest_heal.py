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
whole-file content hashes, 8g0ch), ordered by their spans, and written
through the same atomic-replace path the indexer uses.

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


@dataclass
class ManifestHealResult:
    """Outcome of one heal pass."""

    candidates: int = 0
    gapped: int = 0
    ghost_gapped: int = 0  # gapped subset with chunk_count == 0
    reconciled: int = 0
    dup_collapsed: int = 0
    dup_old_total: int = 0
    dup_new_total: int = 0
    #: Entries that could not be matched to T3 chunks, split by class:
    #: ``lost`` had chunk_count > 0 (a REAL gap — chunks are gone);
    #: ``never_chunked`` had chunk_count == 0 (expected — nothing to rebuild).
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


def heal_manifest_gaps(
    entries: list,
    cat: Any,
    t3: Any,
    writer: Any | None,
    *,
    dry_run: bool = False,
    echo: Callable[[str], None] | None = None,
) -> ManifestHealResult:
    """Detect and rebuild manifest gaps for *entries*.

    Args:
        entries: Candidate ``CatalogEntry`` rows (any scope — whole catalog
            for the reconcile verb, one owner for the indexer self-heal).
            Filtered here to indexable candidates (``chunk_count > 0`` OR
            the ghost shape: recorded content_hash + physical_collection).
        cat: Catalog READER (``get_manifests``).
        t3: T3 database handle (``get_collection``).
        writer: Catalog WRITER (``atomic_manifest_replace`` +
            ``resync_chunk_count_cache``). May be ``None`` only when
            *dry_run* is True.
        dry_run: Report without writing.
        echo: Optional line sink for progress output (the observability
            half of nexus-8g0ch: a silent long walk reads as a hang).
            ``None`` degrades to structlog-only.
    """
    say = echo or (lambda _s: None)
    result = ManifestHealResult()

    candidates = [
        e for e in entries
        if e.chunk_count > 0
        or ((e.meta or {}).get("content_hash") and e.physical_collection)
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

    say(
        f"Scanning: {result.candidates} candidate(s), {result.gapped} gapped "
        f"({result.ghost_gapped} ghost-class chunk_count==0); rebuilding "
        f"manifests from T3 chunks…"
    )

    from nexus.indexer import _paginated_get  # noqa: PLC0415 — deferred: nexus.indexer is heavy and imports back into catalog
    from nexus.mcp_infra import _manifest_chunk_rows  # noqa: PLC0415 — deferred: circular-dep avoidance

    def _classify_unmatched(entry: Any) -> None:
        (result.never_chunked if entry.chunk_count == 0
         else result.lost).append(entry)

    by_coll: dict[str, list] = defaultdict(list)
    for entry in gapped:
        content_hash = (entry.meta or {}).get("content_hash", "")
        if not content_hash or not entry.physical_collection:
            _classify_unmatched(entry)
            continue
        by_coll[entry.physical_collection].append((entry, content_hash))

    for coll_i, (coll_name, pairs) in enumerate(sorted(by_coll.items()), 1):
        try:
            col = t3.get_collection(coll_name)
        except Exception as exc:  # noqa: BLE001 — boundary catch; one collection's failure must not abort the pass
            _log.warning(
                "manifest_heal_collection_unavailable",
                collection=coll_name, docs=len(pairs), error=str(exc),
            )
            for e, _ in pairs:
                _classify_unmatched(e)
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
                fetched = _paginated_get(
                    col, include=["metadatas"],
                    where={"content_hash": {"$in": batch}},
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
                    _classify_unmatched(e)
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
                _classify_unmatched(entry)
                continue
            ordered = sorted(zip(ids, metas), key=_order_key)
            indexed_metas = []
            for i, (cid, m) in enumerate(ordered):
                m = dict(m or {})
                if not m.get("chunk_text_hash"):
                    m["chunk_text_hash"] = cid
                m["chunk_index"] = i
                indexed_metas.append((i, m))
            chunks = _manifest_chunk_rows(indexed_metas)
            if not any(c["chash"] for c in chunks):
                _classify_unmatched(entry)
                continue
            if len(chunks) < entry.chunk_count:
                # RDR-108: duplicate chunk text collapses to one T3 row by
                # design, so a rebuilt manifest can legitimately have fewer
                # rows than the document's stale chunk_count. Not an error —
                # tracked so the summary reports it instead of hiding it.
                result.dup_collapsed += 1
                result.dup_old_total += entry.chunk_count
                result.dup_new_total += len(chunks)
            if not dry_run:
                writer.atomic_manifest_replace(str(entry.tumbler), chunks)
                # atomic_manifest_replace's local-SQLite path re-derives
                # chunk_count in-transaction, but the HTTP/service-mode path
                # only resyncs when told to — without this the gap detector
                # re-flags the same documents forever (GH #1371 follow-up).
                writer.resync_chunk_count_cache(str(entry.tumbler))
            result.reconciled += 1

    return result
