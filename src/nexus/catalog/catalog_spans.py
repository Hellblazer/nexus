# SPDX-License-Identifier: AGPL-3.0-or-later
"""Span resolution for the catalog (nexus-mbm extraction 2/5).

A *span* names a fragment of a catalog document — either by content
hash (``chash:<sha256hex>`` ± a char-range), by line range
(``42-57``), or by chunk + char-range (``3:100-250``). This module
centralises the parsers and resolvers that turn a span string into
chunk text + metadata. None of the resolvers need ``Catalog``
instance state for the parse / T3-lookup paths, so they live as
free functions; ``Catalog`` keeps thin method delegates for
backward compatibility.

Public surface:

- :func:`parse_chash_span` — parse ``chash:<hex>`` or
  ``chash:<hex>:<start>-<end>`` into ``(hex, optional char-range)``.
- :func:`resolve_span_in_t3` — given a ``chash:`` span, a physical
  collection, and a T3 client, return the chunk text + metadata
  (or ``None`` when no row matches).
- :func:`resolve_chash_globally` — RDR-086 Phase 2 global chash
  lookup via the T2 ``chash_index`` with parallel T3 fallback.
- :func:`resolve_span_text_for_entry` — line-range / chunk-char /
  chash dispatcher used by :meth:`Catalog.resolve_span_text`. Takes
  a resolved :class:`CatalogEntry` so callers without one can
  resolve via the catalog first.
- :func:`fallback_chash_scan` — parallel scan over T3 collections
  when the T2 index is empty for a given chash.
- :func:`reset_chash_fallback_warning_for_tests` — test hook for
  the once-per-process fallback warning flag.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import structlog

if TYPE_CHECKING:
    from chromadb.api import ClientAPI
    from nexus.catalog.catalog import CatalogEntry

_log = structlog.get_logger(__name__)


_CHASH_FALLBACK_CONCURRENCY: int = 10
_CHASH_FALLBACK_DEADLINE_S: float = 30.0
_chash_fallback_warned: bool = False


def parse_chash_span(span: str) -> tuple[str, tuple[int, int] | None]:
    """Parse a ``chash:`` span (or bare hex) into ``(hex, char_range)``.

    Accepts:
      * ``chash:<sha256hex>``
      * ``chash:<sha256hex>:<start>-<end>``
      * bare ``<sha256hex>`` (the leading ``chash:`` is optional)

    Raises ``ValueError`` for any other shape.
    """
    body = span[len("chash:"):] if span.startswith("chash:") else span
    m = re.match(r"^([0-9a-f]{64}):(\d+)-(\d+)$", body)
    if m:
        return m.group(1), (int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"[0-9a-f]{64}", body):
        return body, None
    raise ValueError(f"malformed chash span: {span!r}")


def negate_iso(ts: str) -> str:
    """Build a sort key that sorts newer ISO timestamps first.

    ISO-8601 strings sort lexicographically oldest-to-newest; we
    flip by mapping each digit ``d -> 9 - d``. Non-digits pass
    through, so ``-``, ``:``, ``T``, ``+`` separators keep their
    positions. Assumes a canonical
    ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00`` shape (what
    ``ChashIndex.upsert`` writes); other shapes still sort
    deterministically but lose the "newest first" guarantee.
    Fails gracefully for year ≥ 10000 (out of scope this decade).
    """
    return "".join(
        str(9 - int(c)) if c.isdigit() else c for c in ts
    )


def resolve_span_in_t3(
    span: str, physical_collection: str, t3: "ClientAPI",
) -> dict | None:
    """Resolve a ``chash:`` span to chunk content + metadata in T3.

    Returns ``None`` for any non-``chash:`` span (legacy positional
    spans are out of scope here) or when the chunk is not found.
    Raises ``ValueError`` for a malformed ``chash:`` span.

    Output dict keys: ``chunk_text``, ``metadata``, ``chunk_hash``,
    optionally ``char_range``.
    """
    if not span.startswith("chash:"):
        return None
    hex_chash, char_range = parse_chash_span(span)
    col = t3.get_collection(physical_collection)
    result = col.get(
        where={"chunk_text_hash": hex_chash},
        include=["documents", "metadatas"],
    )
    if not result["ids"]:
        return None
    text = result["documents"][0]
    if char_range:
        text = text[char_range[0]:char_range[1]]
    out: dict = {
        "chunk_text": text,
        "metadata": result["metadatas"][0],
        "chunk_hash": hex_chash,
    }
    if char_range:
        out["char_range"] = char_range
    return out


def fallback_chash_scan(
    *,
    hex_chash: str,
    span: str,
    t3: "ClientAPI",
    build_ref: Callable[..., dict],
) -> dict | None:
    """Parallel scan across all T3 collections for a missing chash.

    Invoked when the T2 ``chash_index`` returns no live row. Runs
    :func:`resolve_span_in_t3` against every collection in batches
    of ``_CHASH_FALLBACK_CONCURRENCY``; the first hit wins. The
    full scan is bounded by a 30-second deadline. Logs a single
    warning per process to surface the missing-index signal to
    operators.
    """
    global _chash_fallback_warned

    try:
        all_cols = [c.name for c in t3.list_collections()]
    except Exception:
        _log.debug("chash_fallback_list_collections_failed", exc_info=True)
        return None

    if not _chash_fallback_warned:
        _log.warning(
            "resolve_chash_fallback_scanning",
            chash_prefix=hex_chash[:16],
            collection_count=len(all_cols),
            guidance="run 'nx collection backfill-hash --all' to populate T2",
        )
        _chash_fallback_warned = True

    def _probe(coll: str) -> dict | None:
        try:
            return resolve_span_in_t3(span, coll, t3)
        except Exception:
            return None

    deadline = time.monotonic() + _CHASH_FALLBACK_DEADLINE_S
    idx = 0
    # Manual lifecycle so early return can shutdown(cancel_futures=True)
    # instead of blocking on in-flight futures during __exit__.
    ex = ThreadPoolExecutor(max_workers=_CHASH_FALLBACK_CONCURRENCY)
    try:
        in_flight: dict = {}
        while idx < len(all_cols) or in_flight:
            while (
                len(in_flight) < _CHASH_FALLBACK_CONCURRENCY
                and idx < len(all_cols)
            ):
                col = all_cols[idx]
                in_flight[ex.submit(_probe, col)] = col
                idx += 1

            if not in_flight:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log.warning(
                    "resolve_chash_fallback_timeout",
                    chash_prefix=hex_chash[:16],
                    deadline_s=_CHASH_FALLBACK_DEADLINE_S,
                )
                return None

            done, _pending = wait(
                list(in_flight.keys()),
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                _log.warning(
                    "resolve_chash_fallback_timeout",
                    chash_prefix=hex_chash[:16],
                    deadline_s=_CHASH_FALLBACK_DEADLINE_S,
                )
                return None

            for fut in done:
                coll = in_flight.pop(fut)
                span_res = fut.result()
                if span_res is None:
                    continue
                # Recover doc_id from the hit collection — for fallback
                # we have no T2 row, so query the collection directly.
                try:
                    col = t3.get_collection(coll)
                    hit = col.get(
                        where={"chunk_text_hash": hex_chash},
                        include=[],
                    )
                    doc_id = hit["ids"][0] if hit["ids"] else ""
                except Exception:
                    doc_id = ""
                return build_ref(
                    coll=coll, doc_id=doc_id, span_result=span_res,
                )

        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def reset_chash_fallback_warning_for_tests() -> None:
    """Test hook — clear the once-per-process fallback warning flag."""
    global _chash_fallback_warned
    _chash_fallback_warned = False


def resolve_chash_globally(
    chash: str,
    t3: "ClientAPI",
    chash_index: "Any",
    *,
    prefer_collection: str | None = None,
) -> dict | None:
    """Globally resolve a chash to the chunk it names (RDR-086 Phase 2).

    Unlike :func:`resolve_span_in_t3`, the caller does not need to
    know which collection holds the chunk: the T2 ``chash_index``
    populated by Phase 1 dual-write answers that question in one
    SQL lookup.

    Resolution order:
      1. T2 lookup via ``chash_index.lookup(chash)``.
      2. Drop rows whose ``physical_collection`` no longer exists
         in T3 (self-healing: the stale row is deleted on access).
      3. Tie-break the survivors — ``prefer_collection`` first,
         then newest ``created_at``, then deterministic name sort.
      4. Delegate to :func:`resolve_span_in_t3` for chunk text +
         metadata on the winner. On any per-candidate failure fall
         through.
      5. If T2 was empty or exhausted, fall back to a parallel T3
         scan across all collections. Missing from index is a
         performance hit, not a correctness one —
         ``nx collection backfill-hash --all`` reconciles.

    Accepts the same input forms as :func:`parse_chash_span`.
    """
    hex_chash, char_range = parse_chash_span(chash)

    # Reconstruct the span form that resolve_span_in_t3 expects.
    span = f"chash:{hex_chash}"
    if char_range:
        span = f"{span}:{char_range[0]}-{char_range[1]}"

    def _build_ref(
        *, coll: str, doc_id: str, span_result: dict,
    ) -> dict:
        ref = {
            "chash": hex_chash,
            "chunk_hash": hex_chash,
            "physical_collection": coll,
            "doc_id": doc_id,
            "chunk_text": span_result["chunk_text"],
            "metadata": span_result["metadata"],
        }
        if "char_range" in span_result:
            ref["char_range"] = span_result["char_range"]
        return ref

    # ── T2 path ─────────────────────────────────────────────────────
    rows = chash_index.lookup(hex_chash)
    if rows:
        try:
            live = {c.name for c in t3.list_collections()}
        except Exception:
            live = set()
        survivors: list[dict] = []
        for row in rows:
            if row["collection"] in live:
                survivors.append(row)
            else:
                # Go through the locked public API — this must not race
                # a concurrent upsert / delete_collection on the same
                # store. Direct conn.execute() would bypass the lock.
                try:
                    chash_index.delete_stale(
                        chash=hex_chash, collection=row["collection"],
                    )
                except Exception:
                    _log.debug(
                        "chash_index_selfheal_delete_failed",
                        chash_prefix=hex_chash[:16],
                        collection=row["collection"],
                        exc_info=True,
                    )

        def _sort_key(r: dict) -> tuple:
            preferred = 0 if r["collection"] == prefer_collection else 1
            return (preferred, negate_iso(r["created_at"]), r["collection"])

        for row in sorted(survivors, key=_sort_key):
            try:
                span_res = resolve_span_in_t3(span, row["collection"], t3)
            except Exception:
                span_res = None
            if span_res is None:
                continue
            # RDR-108 Phase 4b / nexus-kosc: T3 chunk natural ID equals
            # ``hex_chash[:32]`` (RDR-108 D1). Derive doc_id directly
            # from the resolved chash rather than reading the
            # ``chunk_chroma_id`` column from chash_index (the column
            # is dropped in nexus-mmf5).
            return _build_ref(
                coll=row["collection"],
                doc_id=hex_chash[:32],
                span_result=span_res,
            )

    # ── T3 fallback ─────────────────────────────────────────────────
    return fallback_chash_scan(
        hex_chash=hex_chash, span=span, t3=t3, build_ref=_build_ref,
    )


def resolve_span_text_for_entry(
    entry: "CatalogEntry", span: str,
) -> str | None:
    """Resolve a span to text given an already-resolved CatalogEntry.

    Span formats:
      * ``""`` → ``None`` (whole document, no sub-addressing).
      * ``42-57`` → lines 42-57 from the document's source file.
      * ``3:100-250`` → characters 100-250 from chunk index 3 in T3.
      * ``chash:<sha256hex>`` → content-addressed chunk from T3.

    The minimal transclusion read path: given a resolved entry +
    span, retrieve the exact passage. ``Catalog.resolve_span_text``
    is the public entry point that performs the tumbler→entry
    resolution before calling here.
    """
    if not span:
        return None

    # Content-hash span: look up by chunk_text_hash in T3
    if span.startswith("chash:") and entry.physical_collection:
        try:
            from nexus.db import make_t3
            t3 = make_t3()
            result = resolve_span_in_t3(
                span, entry.physical_collection, t3._client,
            )
            return result["chunk_text"] if result else None
        except Exception:
            _log.warning(
                "resolve_span_text_failed",
                span=span,
                collection=entry.physical_collection,
                exc_info=True,
            )
            return None

    # Line-range span: read from source file
    m = re.match(r"^(\d+)-(\d+)$", span)
    if m and entry.file_path:
        start, end = int(m.group(1)), int(m.group(2))
        try:
            lines = Path(entry.file_path).read_text(
                encoding="utf-8",
            ).splitlines()
            return "\n".join(lines[start - 1:end])
        except Exception:
            return None

    # Chunk:char span: read from T3
    m = re.match(r"^(\d+):(\d+)-(\d+)$", span)
    if m and entry.physical_collection:
        chunk_idx, char_start, char_end = (
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
        )
        try:
            from nexus.db import make_t3
            t3 = make_t3()
            col = t3.get_or_create_collection(entry.physical_collection)
            # nexus-dcym: chunk identity is the catalog ``doc_id``,
            # not the legacy ``source_path``. ``doc_id`` is stored by
            # the projector under ``meta.doc_id``; older entries fall
            # back to ``str(entry.tumbler)`` (Phase 1's stand-in).
            doc_id = entry.meta.get("doc_id") or str(entry.tumbler)
            where_filter: dict = {
                "chunk_index": chunk_idx,
                "doc_id": doc_id,
            }
            result = col.get(
                where={"$and": [{k: v} for k, v in where_filter.items()]},
                include=["documents"],
                limit=1,
            )
            docs = result.get("documents", [])
            if docs:
                text = docs[0]
                return text[char_start:char_end]
        except Exception:
            return None

    return None
