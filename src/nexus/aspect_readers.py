# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Scheme-keyed reader registry for aspect extraction (RDR-096 P1.1).

Replaces the implicit ``open(source_path)`` plus
``_source_content_from_t3`` fallback in ``aspect_extractor`` with
explicit URI dispatch and a typed Result contract that surfaces read
failures as ``ReadFail`` instead of silently producing null-field
aspect rows.

Initial schemes (Phase 1):

* ``file:///abs/path/to/file.md`` — disk read.
* ``chroma://<collection>/<source-identifier>`` — chunk reassembly
  from T3 by metadata-field match.
* ``x-devonthink-item://<UUID>`` — macOS-only, resolves the UUID to
  a current filesystem path via the DEVONthink AppleScript bridge
  (osascript) and reads the file. Lets DT-managed PDFs survive
  relocations inside DT's database without breaking catalog URIs
  (nexus-bqda).

Knowledge collections (``knowledge__*``) identify documents by metadata
field ``title`` (slug); ``nx index``-ingested collections
(``rdr__/docs__/code__``) identify by ``source_path``. The
``CHROMA_IDENTITY_FIELD`` dispatch table picks the right field per
collection prefix — querying ``where={"source_path": ...}`` against
``knowledge__*`` chunks returns empty (root cause behind issue #333,
verified empirically in research-4, id 1011).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlparse

import structlog

from nexus.db.limits import QUOTAS

_log = structlog.get_logger(__name__)


__all__ = [
    "CHROMA_IDENTITY_FIELD",
    "ReadFail",
    "ReadFailReason",
    "ReadOk",
    "ReadResult",
    "StalenessSignal",
    "StatFail",
    "StatOk",
    "StatResult",
    "read_source",
    "staleness_signal",
    "stat_source",
    "uri_for",
]


# ── URI construction (RDR-096 P2.1) ──────────────────────────────────────────


def uri_for(collection: str, source_path: str) -> str | None:
    """Persistent URI for ``(collection, source_path)``. Single source
    of truth: both the going-forward writer in
    :mod:`nexus.aspect_extractor` and the backfill migration in
    :mod:`nexus.db.migrations` import this helper to avoid silent
    divergence on future prefix additions.

    Returns ``None`` when ``source_path`` is empty — that maps to
    SQLite ``NULL`` in :class:`AspectRecord` writes and matches the
    migration's NULL-on-empty backfill behavior.

    Filesystem-backed collections (``rdr__/docs__/code__``) use
    ``file://`` with ``os.path.abspath``; everything else
    (``knowledge__`` and any future prefix) uses ``chroma://`` with
    the literal source_path as the path component. The chroma reader
    handles the title/source_path identity-field fallback for
    knowledge collections internally (research-5, id 1014).

    Note: ``abspath`` resolves against the caller's CWD, so URIs
    produced from a relative ``source_path`` depend on where the
    writer (or migration) ran. Stored absolute paths round-trip
    deterministically; relative paths may diverge between the
    backfill site and going-forward writers if those run from
    different CWDs.
    """
    import os.path  # noqa: PLC0415 — deliberate deferred import: branch-local / startup-cost avoidance

    if not source_path:
        return None
    if collection.startswith(("rdr__", "docs__", "code__")):
        return "file://" + os.path.abspath(source_path)
    return f"chroma://{collection}/{source_path}"


# ── Result types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReadOk:
    """Successful source read. ``text`` is the document body suitable
    for passing to an extractor; ``metadata`` carries scheme-specific
    provenance (chunk count, identity field used, byte size, etc.).
    """
    text: str
    metadata: dict[str, Any]


ReadFailReason = Literal["unreachable", "unauthorized", "scheme_unknown", "empty"]


@dataclass(frozen=True)
class ReadFail:
    """Failed source read with a typed reason. The upsert-guard in
    ``commands/enrich.py`` skips on any ``ReadFail``; no row is
    written to ``document_aspects``.

    Reasons:

    * ``unreachable`` — caller could not reach the resource named by
      the URI. Covers I/O errors, missing files, chroma client errors,
      and malformed-but-scheme-known URIs (the URI parsed but had no
      collection or no source identifier). Operators distinguish
      transient-network failures from caller-side malformed-URI
      failures via the ``detail`` field.
    * ``unauthorized`` — caller lacks credentials (HTTPS auth, S3
      ACL). Not retriable without out-of-band intervention.
    * ``scheme_unknown`` — URI scheme has no registered reader (or no
      scheme at all).
    * ``empty`` — read succeeded but produced no content (zero
      chunks, empty file).
    """
    reason: ReadFailReason
    detail: str


ReadResult = ReadOk | ReadFail


# ── Chroma identity-field dispatch (RDR-101 Phase 4, nexus-o6aa.10.1) ────────


# Post-Phase-4 dispatch: every collection prefix identifies chunks by the
# catalog-stable ``doc_id`` (mirrors ``Document.doc_id``). The chroma reader
# resolves the URI's source identifier to a ``doc_id`` via a caller-supplied
# projection callable (``doc_id_lookup`` keyword on ``_read_chroma_uri`` /
# ``read_source``); without that callable the reader falls back to the
# legacy ``(source_path, title)`` probe so callers without catalog access
# (tests, ad-hoc CLI runs) keep working.
#
# Pre-Phase-4 the dispatch was per-prefix and per-shape: ``source_path`` for
# ``nx index``-ingested collections, ``(source_path, title)`` for
# ``knowledge__*`` to handle slug-shaped MCP-promoted notes. The legacy probe
# below preserves that behavior for back-compat callers; the load-bearing
# claim post-RDR-101 is the doc_id-keyed path.
CHROMA_IDENTITY_FIELD: dict[str, tuple[str, ...]] = {
    "rdr__":       ("doc_id",),
    "docs__":      ("doc_id",),
    "code__":      ("doc_id",),
    "knowledge__": ("doc_id",),
}


# Legacy fallback fields, used by ``_read_chroma_uri`` only when the caller
# does not supply a ``doc_id_lookup``. Mirrors the pre-Phase-4 dispatch
# table. Slated for removal once the prune verb (.10.3) lands and chunks
# uniformly carry ``doc_id`` metadata.
_LEGACY_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "rdr__":       ("source_path",),
    "docs__":      ("source_path",),
    "code__":      ("source_path",),
    "knowledge__": ("source_path", "title"),
}


def _identity_fields_for(collection: str) -> tuple[str, ...]:
    """Return the ordered tuple of chunk-metadata fields that identify
    a source document in ``collection``. Post-RDR-101 Phase 4 every
    prefix maps to ``("doc_id",)`` uniformly; the legacy multi-field
    probe lives behind ``_legacy_identity_fields_for`` for back-compat.
    """
    for prefix, fields in CHROMA_IDENTITY_FIELD.items():
        if collection.startswith(prefix):
            return fields
    return ("doc_id",)


def _legacy_identity_fields_for(collection: str) -> tuple[str, ...]:
    """Return the pre-RDR-101 identity fields for *collection*.

    ``source_path`` for ``nx index`` ingests, ``(source_path, title)``
    for ``knowledge__*``. Used by ``_read_chroma_uri`` only when no
    ``doc_id_lookup`` is supplied.
    """
    for prefix, fields in _LEGACY_IDENTITY_FIELDS.items():
        if collection.startswith(prefix):
            return fields
    return ("source_path",)


# ── file:// reader ───────────────────────────────────────────────────────────


def _read_file_uri(uri: str, **_kw: Any) -> ReadResult:
    """Read a ``file://`` URI from disk.

    ``urlparse`` handles ``file:///abs/path`` and
    ``file://localhost/abs/path`` shapes; the path component is
    URL-unquoted to recover characters that were percent-encoded at
    write time (spaces, unicode, etc.).
    """
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if not path:
        return ReadFail(reason="unreachable", detail=f"empty path in {uri!r}")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError as e:
        return ReadFail(reason="unreachable", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        return ReadFail(reason="unauthorized", detail=f"PermissionError: {e}")
    except OSError as e:
        return ReadFail(reason="unreachable", detail=f"{type(e).__name__}: {e}")
    if not data:
        return ReadFail(reason="empty", detail=f"empty file at {path!r}")
    text = data.decode("utf-8", errors="replace")
    return ReadOk(
        text=text,
        metadata={"scheme": "file", "path": path, "bytes": len(data)},
    )


# ── chroma:// reader ─────────────────────────────────────────────────────────


def _gather_chroma_chunks_by_field(
    *,
    coll: Any,
    collection: str,
    source_id: str,
    identity_field: str,
    match_value: str,
    manifest_lookup: Callable[[str], list[Any]] | None = None,
) -> ReadResult:
    """Page through ``coll.get(where={identity_field: match_value})`` and
    return a ``ReadOk`` with chunks reassembled in canonical order.

    nexus-8g79.2: When ``manifest_lookup`` is provided and
    ``identity_field == 'doc_id'`` (so ``match_value`` is the catalog
    tumbler), the canonical chunk order comes from
    ``cat.get_manifest(doc_id)`` keyed by ``chash``. Post-RDR-108
    Phase 3, chunk metadata no longer carries ``chunk_index``, so the
    legacy ``md.get("chunk_index", 0)`` ordering collapses to position
    zero for every chunk and only the insertion-sequence tiebreaker
    survives — which is wrong for multi-chunk docs. The manifest is
    authoritative; fall back to ``chunk_index`` (then to insertion
    sequence) only when no manifest is available.
    """
    chunks: list[tuple[int, int, str]] = []
    seq = 0
    offset = 0
    page_limit = QUOTAS.MAX_QUERY_RESULTS
    # Build chash → manifest position map when available.
    chash_position: dict[str, int] = {}
    if manifest_lookup is not None and identity_field == "doc_id":
        try:
            rows = manifest_lookup(match_value)
        except Exception:  # noqa: BLE001 — best-effort row fetch; degrades to empty list
            rows = []
        for row in rows or []:
            chash = getattr(row, "chash", None) or (
                row.get("chash") if isinstance(row, dict) else None
            )
            position = getattr(row, "position", None)
            if position is None and isinstance(row, dict):
                position = row.get("position")
            if chash and position is not None:
                chash_position[chash] = int(position)
    # nexus-m8a7: when the catalog manifest is available, select chunks
    # by id (chash[:32]) rather than by ``where={doc_id: ...}``. Post
    # RDR-108 Phase 3 chunks no longer carry ``doc_id`` metadata; the
    # where-filter returns zero rows even though the manifest knows the
    # exact chashes. Chroma's natural id IS chash[:32] (RDR-108), so a
    # batched ``coll.get(ids=...)`` fetches them deterministically.
    use_manifest_ids = bool(chash_position) and identity_field == "doc_id"
    try:
        if use_manifest_ids:
            ids = list(chash_position)  # RDR-180: the full chash IS the id
            for batch_start in range(0, len(ids), page_limit):
                page = coll.get(
                    ids=ids[batch_start : batch_start + page_limit],
                    include=["documents", "metadatas"],
                )
                docs = page.get("documents") or []
                mds = page.get("metadatas") or []
                for md, doc in zip(mds, docs):
                    if not doc:
                        continue
                    if isinstance(md, dict):
                        chash = md.get("chunk_text_hash", "")
                        ci = chash_position.get(
                            chash, int(md.get("chunk_index", 0) or 0)
                        )
                    else:
                        ci = 0
                    chunks.append((ci, seq, doc))
                    seq += 1
        if not use_manifest_ids or not chunks:
            # Either no manifest available, or manifest-ids returned
            # nothing (chunk id ≠ chash[:32] — pre-RDR-108 ingest, or
            # tests that seed synthetic ids). Fall back to where-filter.
            offset = 0
            while True:
                page = coll.get(
                    where={identity_field: match_value},
                    limit=page_limit,
                    offset=offset,
                    include=["documents", "metadatas"],
                )
                docs = page.get("documents") or []
                mds = page.get("metadatas") or []
                if not docs:
                    break
                for md, doc in zip(mds, docs):
                    if not doc:
                        continue
                    if isinstance(md, dict):
                        chash = md.get("chunk_text_hash", "")
                        if chash_position and chash in chash_position:
                            ci = chash_position[chash]
                        else:
                            ci = int(md.get("chunk_index", 0) or 0)
                    else:
                        ci = 0
                    chunks.append((ci, seq, doc))
                    seq += 1
                if len(docs) < page_limit:
                    break
                offset += page_limit
    except Exception as e:  # noqa: BLE001 — boundary catch; error surfaced to caller as a ReadFail result
        return ReadFail(
            reason="unreachable",
            detail=(
                f"chroma.get failed (field={identity_field!r}): "
                f"{type(e).__name__}: {e}"
            ),
        )
    if not chunks:
        return ReadFail(
            reason="empty",
            detail=f"no chunks for {match_value!r} in {collection!r}",
        )
    # nexus-dxly: post-RDR-108 Phase 3 chunks lack ``chunk_index``
    # metadata. When the manifest lookup is unavailable (catalog gap
    # for a Phase-3 doc) the fallback ``md.get("chunk_index", 0)``
    # returns 0 for every chunk and multi-chunk docs reassemble in
    # chroma insertion order (chunk_text_hash-driven, not document
    # order). Fail loud so callers see the structural problem instead
    # of silently extracting / scoring against a scrambled document.
    if (
        identity_field == "doc_id"
        and not chash_position
        and len(chunks) > 1
        and {triple[0] for triple in chunks} == {0}
    ):
        return ReadFail(
            reason="unreachable",
            detail=(
                f"Phase-3 reassembly unsafe for {match_value!r} in "
                f"{collection!r}: manifest_lookup returned no rows and "
                f"chunks lack chunk_index ordering. Cannot reassemble "
                f"{len(chunks)} chunks deterministically. Initialize the "
                f"catalog or re-index to populate the document_chunks "
                f"manifest."
            ),
        )
    chunks.sort(key=lambda triple: (triple[0], triple[1]))
    text = "\n\n".join(doc for _, _, doc in chunks)
    return ReadOk(
        text=text,
        metadata={
            "scheme": "chroma",
            "collection": collection,
            "source_id": source_id,
            "identity_field": identity_field,
            "chunk_count": len(chunks),
            "manifest_ordered": bool(chash_position),
        },
    )


def _read_chroma_uri(
    uri: str,
    *,
    t3: Any = None,
    doc_id_lookup: Callable[[str, str], str] | None = None,
    manifest_lookup: Callable[[str], list[Any]] | None = None,
    **_kw: Any,
) -> ReadResult:
    """Reassemble a document from chroma chunks.

    URI shape: ``chroma://<collection>/<source-identifier>``. Post
    RDR-101 Phase 4 (nexus-o6aa.10.1), the ``<source-identifier>`` is
    resolved to the catalog-stable ``doc_id`` via the caller's
    ``doc_id_lookup`` projection, and the chunk lookup keys on
    ``doc_id`` only. When no ``doc_id_lookup`` is provided the reader
    falls back to the legacy ``(source_path, title)`` multi-field probe
    so callers without catalog access (tests, ad-hoc CLI runs) keep
    working.

    ``doc_id_lookup`` is ``Callable[[collection, source_id], doc_id]``
    where an empty-string return signals "no catalog entry maps this
    source_id". An empty doc_id surfaces as
    ``ReadFail(reason='unreachable')``, a structural failure shape
    distinct from ``ReadFail(reason='empty')`` so the Phase 4 dry-run
    gate can distinguish "chunk-set exists but unbacked by a catalog
    row" (orphan / pre-backfill) from "no chunks at all".

    ``t3`` may be a ``T3Database`` instance, a raw chromadb client
    (``EphemeralClient`` / ``PersistentClient`` / ``CloudClient``),
    or any object exposing ``.get_collection(name)`` that returns a
    chromadb-like Collection.
    """
    if t3 is None:
        return ReadFail(reason="unreachable", detail="no chroma client provided")
    parsed = urlparse(uri)
    collection = parsed.netloc
    # ``urlparse`` always prefixes the path with ``/`` when a netloc
    # is present. ``removeprefix`` strips exactly one leading slash,
    # preserving any that are part of the source_path itself (e.g.,
    # ``/Users/.../paper.pdf`` round-trips intact when constructed as
    # ``chroma://collection//Users/...``).
    source_id = unquote(parsed.path.removeprefix("/"))
    if not collection or not source_id:
        return ReadFail(
            reason="unreachable",
            detail=(
                f"malformed chroma uri (collection={collection!r}, "
                f"source_id={source_id!r})"
            ),
        )
    try:
        coll = t3.get_collection(collection)
    except Exception as e:  # noqa: BLE001 — boundary catch; error surfaced to caller as a ReadFail result
        return ReadFail(
            reason="unreachable",
            detail=f"get_collection({collection!r}) failed: {type(e).__name__}: {e}",
        )

    # nexus-o6aa.10.1 doc_id-keyed path: resolve source_id → doc_id and
    # query on doc_id first. An empty doc_id (catalog gap) is a
    # structural failure, NOT "no chunks". When the strict doc_id query
    # returns no chunks, fall back to the legacy multi-field probe:
    # catalog metadata may carry a doc_id that pre-Phase-4 chunks lack.
    # Post-iftc the t3-backfill-doc-id migration verb is gone; the
    # fallback survives for in-the-wild pre-Phase-4 collections until
    # they re-index.
    if doc_id_lookup is not None:
        try:
            doc_id = doc_id_lookup(collection, source_id) or ""
        except Exception as e:  # noqa: BLE001 — boundary catch; error surfaced to caller as a ReadFail result
            return ReadFail(
                reason="unreachable",
                detail=(
                    f"doc_id_lookup({collection!r}, {source_id!r}) raised "
                    f"{type(e).__name__}: {e}"
                ),
            )
        if not doc_id:
            return ReadFail(
                reason="unreachable",
                detail=(
                    f"no doc_id mapped for {source_id!r} in {collection!r} "
                    f"(catalog gap or pre-Phase-4 chunk; re-index the "
                    f"affected collection to populate doc_id metadata)"
                ),
            )
        result = _gather_chroma_chunks_by_field(
            coll=coll, collection=collection, source_id=source_id,
            identity_field="doc_id", match_value=doc_id,
            manifest_lookup=manifest_lookup,
        )
        if isinstance(result, ReadOk):
            return result
        # nexus-dxly: a structural reassembly failure (Phase-3 doc with
        # no manifest_lookup, multi-chunk, no chunk_index) must propagate
        # not silently fall through to legacy probe — the legacy probe
        # would find nothing and the corruption stays hidden behind an
        # ``empty`` report.
        if result.reason == "unreachable":
            return result
        # doc_id mapped but T3 query returned empty: chunks predate
        # the Phase 4 doc_id write contract. Fall through to legacy
        # probe rather than report ``empty`` so legacy chunks remain
        # readable until re-indexed.

    # Legacy multi-field probe (back-compat for callers without
    # catalog access). Removed in Phase 5b once chunks uniformly carry
    # doc_id and the prune verb has dropped source_path / title.
    identity_fields = _legacy_identity_fields_for(collection)
    last_fail: ReadFail | None = None
    for identity_field in identity_fields:
        result = _gather_chroma_chunks_by_field(
            coll=coll, collection=collection, source_id=source_id,
            identity_field=identity_field, match_value=source_id,
        )
        if isinstance(result, ReadOk):
            return result
        last_fail = result
        # Only ``empty`` is retriable across the next legacy field;
        # ``unreachable`` (chroma.get raised) means stop probing.
        if result.reason != "empty":
            return result
    return last_fail or ReadFail(
        reason="empty",
        detail=(
            f"no chunks for {source_id!r} in {collection!r} "
            f"(identity_fields tried={list(identity_fields)})"
        ),
    )


# ── nx-scratch:// reader (RDR-096 P4.1) ──────────────────────────────────────


def _read_scratch_uri(uri: str, *, scratch: Any = None, **_kw: Any) -> ReadResult:
    """Read an ``nx-scratch://session/<session-id>/<entry-id>`` URI
    from T1 scratch storage.

    Use case: an agent synthesizes a document in T1 scratch and later
    promotes it to T3. The catalog can preserve provenance back to
    the originating session by storing the nx-scratch URI as
    ``source_uri``. Subsequent re-extractions read the original
    synthesis text from scratch when it is still live; once the
    scratch session has expired or the entry has been deleted, the
    reader returns ``ReadFail(reason='unreachable')`` and the
    upsert-guard skips.

    The ``session-id`` segment is a routing hint. Each session has
    its own chromadb HTTP server with its own collection of scratch
    entries; ``scratch.get(entry_id)`` returns ``None`` when the
    entry isn't accessible from the caller's session (cross-session
    lookups generally fail by design).

    ``scratch`` is a ``T1Database``-compatible object exposing
    ``.get(entry_id) -> dict | None`` where the returned dict
    carries a ``content`` key.
    """
    if scratch is None:
        return ReadFail(reason="unreachable", detail="no scratch client provided")
    parsed = urlparse(uri)
    if parsed.netloc != "session":
        return ReadFail(
            reason="unreachable",
            detail=(
                f"unexpected netloc {parsed.netloc!r} in nx-scratch URI; "
                f"expected 'session' (shape: nx-scratch://session/<session-id>/<entry-id>)"
            ),
        )
    path = parsed.path.removeprefix("/")
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return ReadFail(
            reason="unreachable",
            detail=(
                f"malformed nx-scratch uri {uri!r}; expected "
                f"nx-scratch://session/<session-id>/<entry-id>"
            ),
        )
    session_id, entry_id = parts
    try:
        entry = scratch.get(entry_id)
    except Exception as e:  # noqa: BLE001 — boundary catch; error surfaced to caller as a ReadFail result
        return ReadFail(
            reason="unreachable",
            detail=f"scratch.get({entry_id!r}) failed: {type(e).__name__}: {e}",
        )
    if entry is None:
        return ReadFail(
            reason="unreachable",
            detail=(
                f"scratch entry {entry_id!r} not found "
                f"(may be expired, deleted, or in a different "
                f"session than {session_id!r})"
            ),
        )
    text = entry.get("content", "") if isinstance(entry, dict) else ""
    if not text:
        return ReadFail(
            reason="empty",
            detail=f"scratch entry {entry_id!r} has empty content",
        )
    return ReadOk(
        text=text,
        metadata={
            "scheme": "nx-scratch",
            "session_id": session_id,
            "entry_id": entry_id,
            "actual_session_id": (
                entry.get("session_id", "") if isinstance(entry, dict) else ""
            ),
        },
    )


# ── https:// reader (RDR-096 P4.2) ───────────────────────────────────────────


def _read_https_uri(
    uri: str,
    *,
    t3: Any = None,
    http_client: Any = None,
    force_refresh: bool = False,
    chroma_hint: tuple[str, str] | None = None,
    **_kw: Any,
) -> ReadResult:
    """Read an ``https://`` URI for paywalled or dynamic upstreams
    (Confluence pages, web research, RFC archives).

    **Chroma-first preference**: when the caller provides
    ``chroma_hint = (collection, source_id)`` AND ``force_refresh``
    is False AND ``t3`` is available, the reader first tries the
    chroma equivalent — returning the cached chunk-reassembled text
    without a network round-trip when the chunk exists. The hint
    is the caller's job to compute (e.g., the catalog can map a
    URL to a (collection, source_id) tuple via its source_uri
    column). Operator opt-in via ``force_refresh=True`` bypasses
    the cache and always fetches live.

    Failure modes:

    * 401 / 403 → ``ReadFail(reason='unauthorized')`` (auth fixable
      out of band; not retriable in-process).
    * 404 / 5xx / network error → ``ReadFail(reason='unreachable')``.
    * empty body → ``ReadFail(reason='empty')``.
    * chroma-first miss → falls through to httpx (no error surfaced
      from the chroma probe).

    ``http_client`` is injected for tests; production calls
    construct a short-timeout httpx.Client.
    """
    # Step 1 — chroma-first preference.
    if (
        not force_refresh
        and chroma_hint is not None
        and t3 is not None
    ):
        coll, src = chroma_hint
        chroma_uri = f"chroma://{coll}/{src}"
        chroma_result = _read_chroma_uri(chroma_uri, t3=t3)
        if isinstance(chroma_result, ReadOk):
            return ReadOk(
                text=chroma_result.text,
                metadata={
                    **chroma_result.metadata,
                    "https_uri": uri,
                    "served_from": "chroma",
                },
            )
        # Chroma miss: fall through to live fetch.

    # Step 2 — live fetch via httpx.
    own_client = False
    if http_client is None:
        import httpx  # noqa: PLC0415  — optional/heavy dependency deferred (httpx)

        http_client = httpx.Client(timeout=30, follow_redirects=True)
        own_client = True
    try:
        try:
            response = http_client.get(uri)
        except Exception as e:  # noqa: BLE001 — boundary catch; error surfaced to caller as a ReadFail result
            return ReadFail(
                reason="unreachable",
                detail=f"http fetch failed: {type(e).__name__}: {e}",
            )
    finally:
        if own_client:
            http_client.close()

    status = response.status_code
    if status in (401, 403):
        return ReadFail(
            reason="unauthorized",
            detail=f"HTTP {status} from {uri!r}",
        )
    if status >= 400:
        return ReadFail(
            reason="unreachable",
            detail=f"HTTP {status} from {uri!r}",
        )
    text = response.text or ""
    if not text:
        return ReadFail(
            reason="empty",
            detail=f"HTTP {status} with empty body from {uri!r}",
        )
    return ReadOk(
        text=text,
        metadata={
            "scheme": "https",
            "url": uri,
            "http_status": status,
            "served_from": "network",
        },
    )


# ── x-devonthink-item:// reader (nexus-bqda) ─────────────────────────────────


def _devonthink_resolver_default(uuid: str) -> tuple[str | None, str]:
    """Resolve a DEVONthink record UUID to a filesystem path via osascript.

    Returns ``(path, error_detail)``. On success ``path`` is the absolute
    path reported by DEVONthink and ``error_detail`` is ``""``. On failure
    ``path`` is ``None`` and ``error_detail`` describes the cause
    (osascript not present, timeout, missing record, non-zero exit).

    DEVONthink registers as application id ``DNtp`` so the script works
    against any installed edition (DT 3 / DT Pro / DT Server). The
    sentinel ``__NX_DT_MISSING__`` distinguishes "record not found" from
    a literal empty path.
    """
    import subprocess  # noqa: PLC0415  — stdlib deferred to call site (subprocess)
    script = (
        'tell application id "DNtp"\n'
        f'  set theItem to get record with uuid "{uuid}"\n'
        '  if theItem is missing value then\n'
        '    return "__NX_DT_MISSING__"\n'
        '  else\n'
        '    return path of theItem\n'
        '  end if\n'
        'end tell'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return None, "osascript not found (macOS-only)"
    except subprocess.TimeoutExpired:
        return None, "osascript timed out resolving DEVONthink UUID"
    except OSError as e:
        return None, f"osascript failed: {type(e).__name__}: {e}"
    if proc.returncode != 0:
        return None, (
            f"osascript rc={proc.returncode}: {proc.stderr.strip() or '(no stderr)'}"
        )
    path = proc.stdout.strip()
    if not path or path == "__NX_DT_MISSING__":
        return None, f"DEVONthink record {uuid!r} not found"
    return path, ""


def _read_devonthink_uri(
    uri: str,
    *,
    dt_resolver: Callable[[str], tuple[str | None, str]] | None = None,
    **_kw: Any,
) -> ReadResult:
    """Read an ``x-devonthink-item://<UUID>`` URI.

    Resolves the UUID via DEVONthink (macOS app, registered as ``DNtp``)
    using osascript, then reads the file at the resolved path. macOS-only;
    on other platforms returns a clear ``ReadFail`` so callers don't have
    to special-case the platform check themselves.

    The ``dt_resolver`` keyword exists so tests can drive the reader
    without launching osascript; production calls fall through to
    :func:`_devonthink_resolver_default`.
    """
    import sys  # noqa: PLC0415  — stdlib deferred to call site (sys)

    if dt_resolver is None:
        if sys.platform != "darwin":
            return ReadFail(
                reason="unreachable",
                detail="DEVONthink integration is macOS-only",
            )
        dt_resolver = _devonthink_resolver_default

    parsed = urlparse(uri)
    # ``urlparse`` puts the UUID in netloc when ``://`` is present;
    # tolerate the path shape too in case a writer ever emits
    # ``x-devonthink-item:UUID`` without the double slash.
    uuid = parsed.netloc or parsed.path.lstrip("/")
    if not uuid:
        return ReadFail(
            reason="unreachable",
            detail=f"empty UUID in DEVONthink URI {uri!r}",
        )

    path, error_detail = dt_resolver(uuid)
    if path is None:
        return ReadFail(
            reason="unreachable",
            detail=error_detail or "DEVONthink resolver returned no path",
        )

    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError as e:
        return ReadFail(reason="unreachable", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        return ReadFail(reason="unauthorized", detail=f"PermissionError: {e}")
    except OSError as e:
        return ReadFail(reason="unreachable", detail=f"{type(e).__name__}: {e}")
    if not data:
        return ReadFail(reason="empty", detail=f"empty file at {path!r}")
    text = data.decode("utf-8", errors="replace")
    return ReadOk(
        text=text,
        metadata={
            "scheme": "x-devonthink-item",
            "uuid": uuid,
            "resolved_path": path,
            "bytes": len(data),
        },
    )


# ── obsidian:// reader (RDR-169 G3) ──────────────────────────────────────────

# Vault roots that are filesystem roots or known system directories. A real
# Obsidian vault is never at "/" or any system prefix; these values indicate
# misconfiguration or injection. The per-path traversal guard is defense-in-depth,
# not the primary check — a "/" vault_root makes every path relative_to("/"),
# neutralising it. Shared by the reader (_read_obsidian_uri) and the stat path
# (_stat_obsidian_uri) so the security blocklist cannot drift between them.
# vault_path is resolved (symlinks followed) before the check, so on macOS
# "/etc" → "/private/etc"; both the symlinked short name and canonical path are listed.
_OBSIDIAN_BLOCKED_ROOTS = frozenset({
    "/",
    "/etc", "/private/etc",
    "/usr", "/private/usr",
    "/var", "/private/var",
    "/bin", "/sbin",
    "/private",
    "/System",
})



def _read_obsidian_uri(
    uri: str,
    *,
    vault_root: Any = None,
    **_kw: Any,
) -> ReadResult:
    """Read an ``obsidian://open?vault=<v>&file=<rel>`` URI.

    LOCAL scheme (RDR-169 G3 split): the file lives in the requesting
    tenant's Obsidian vault.  ``vault_root`` MUST be supplied as an
    absolute ``pathlib.Path`` (or path-coercible value) identifying the
    tenant's vault directory.  Without it the handler cannot resolve the
    path and returns ``ReadFail(reason='unreachable')`` — cross-tenant
    resolution is structurally impossible because the handler only ever
    sees the ``vault_root`` supplied for the current request's tenant.

    URI shape: ``obsidian://open?vault=<vault-name>&file=<url-encoded-path>``
    (the ``vault`` query param is informational; resolution uses ``vault_root``
    passed in by the caller, not the vault name in the URI, to ensure the
    correct tenant vault root is always used).

    **Security contract (IMPORTANT):**

    ``vault_root`` MUST be server-provisioned per-tenant configuration —
    it must come from a trusted registry/config store, NEVER from client-
    supplied request data such as HTTP query params, request headers, or
    the ``vault`` param of the ``obsidian://`` URI itself.  This invariant
    is enforced at the call boundary (``read_source`` extracts
    ``tenant["vault_root"]`` from a caller-supplied mapping; the caller is
    responsible for populating that mapping from a trusted source only).

    The path-traversal guard below (``relative_to`` after ``.resolve()``)
    is DEFENSE-IN-DEPTH against a malicious ``file=`` query param — it is
    NOT the primary isolation mechanism.  The primary isolation is: the
    handler only ever sees the vault_root that was server-provisioned for
    the current request's tenant; a different tenant's vault_root never
    appears here.

    As an additional defense this handler REJECTS ``vault_root`` values
    that are system roots (``/``, ``/etc``, ``/usr``, ``/private``,
    ``/var``, ``/bin``, ``/sbin``, ``/System``) — a vault legitimately
    never lives at a filesystem root, and accepting them would make the
    traversal guard vacuous.  Such values indicate misconfiguration or an
    attempted injection and are returned as
    ``ReadFail(reason='unauthorized')``.
    """
    from pathlib import Path  # noqa: PLC0415 — stdlib deferred to call site

    if vault_root is None:
        return ReadFail(
            reason="unreachable",
            detail="obsidian:// reader requires vault_root (tenant context not provided)",
        )

    vault_path = Path(vault_root).resolve()

    # ``vault_path`` is already resolved; see _OBSIDIAN_BLOCKED_ROOTS for rationale.
    if not vault_path.is_absolute() or str(vault_path) in _OBSIDIAN_BLOCKED_ROOTS:
        return ReadFail(
            reason="unauthorized",
            detail=(
                f"vault_root {str(vault_path)!r} is not permitted — must be an "
                f"absolute path to a user vault directory, never a system root. "
                f"vault_root MUST be server-provisioned config, not client-supplied."
            ),
        )

    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    file_parts = params.get("file")
    if not file_parts or not file_parts[0]:
        return ReadFail(
            reason="unreachable",
            detail=f"obsidian URI missing 'file' query param: {uri!r}",
        )
    rel_file = unquote(file_parts[0])

    # Resolve the full path and confirm it stays inside the vault.
    candidate = (vault_path / rel_file).resolve()
    try:
        candidate.relative_to(vault_path)
    except ValueError:
        return ReadFail(
            reason="unauthorized",
            detail=(
                f"resolved path {str(candidate)!r} escapes vault root "
                f"{str(vault_path)!r} (path traversal blocked)"
            ),
        )

    try:
        data = candidate.read_bytes()
    except FileNotFoundError as e:
        return ReadFail(reason="unreachable", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        return ReadFail(reason="unauthorized", detail=f"PermissionError: {e}")
    except OSError as e:
        return ReadFail(reason="unreachable", detail=f"{type(e).__name__}: {e}")

    if not data:
        return ReadFail(reason="empty", detail=f"empty file at {str(candidate)!r}")

    text = data.decode("utf-8", errors="replace")
    return ReadOk(
        text=text,
        metadata={
            "scheme": "obsidian",
            "vault_root": str(vault_path),
            "relative_path": rel_file,
            "resolved_path": str(candidate),
            "bytes": len(data),
        },
    )


# ── Reader registry + dispatch helper ────────────────────────────────────────


_READERS: dict[str, Callable[..., ReadResult]] = {
    "file": _read_file_uri,
    "chroma": _read_chroma_uri,
    "nx-scratch": _read_scratch_uri,
    "https": _read_https_uri,
    "x-devonthink-item": _read_devonthink_uri,
    "obsidian": _read_obsidian_uri,
}


def read_source(
    uri: str,
    *,
    t3: Any = None,
    scratch: Any = None,
    doc_id_lookup: Callable[[str, str], str] | None = None,
    manifest_lookup: Callable[[str], list[Any]] | None = None,
    tenant: dict[str, Any] | None = None,
) -> ReadResult:
    """Dispatch ``uri`` to its registered reader by scheme.

    Unknown schemes return ``ReadFail(reason="scheme_unknown")``; the
    upsert-guard in ``commands/enrich.py`` (Phase 1.3) logs and skips
    on any ``ReadFail``, so a missing reader is a visible-but-graceful
    failure mode, not a crash.

    ``doc_id_lookup`` (RDR-101 Phase 4 / nexus-o6aa.10.1) is forwarded
    to the chroma reader as the source-identifier → ``doc_id``
    projection. ``manifest_lookup`` (nexus-8g79.2) is forwarded as the
    ``doc_id -> list[ManifestRow]`` projection so the chroma reader can
    order multi-chunk docs by the catalog manifest's canonical
    position rather than the dropped ``chunk_index`` metadata field.
    Other readers ignore both.

    ``tenant`` carries per-request context for LOCAL-scheme handlers
    (RDR-169 G3 split).  Currently ``obsidian://`` uses
    ``tenant["vault_root"]`` to resolve vault-relative paths.

    **Registered Python-side schemes** (all schemes currently in ``_READERS``):
    ``file``, ``chroma``, ``nx-scratch``, ``https``, ``x-devonthink-item``,
    ``obsidian``.

    **RDR-169 G3 reachability split:** the Java service also has handlers for
    ``chroma://`` and ``https://`` in its ``UriSchemeResolverRegistry``
    (``dev.nexus.service.resolver``).  The SPLIT means: at /v1 serving time a
    server-reachable scheme (``chroma``, ``https``) MAY be resolved inline by
    the Java handler; a local scheme (``file``, ``obsidian``,
    ``x-devonthink-item``, ``nx-scratch``) is resolved client-side by calling
    this function.  The Python-side ``chroma`` and ``https`` readers exist for
    the Python bridge's own aspect-enrichment path (``commands/enrich.py``),
    which runs in the MCP process on the tenant's local machine where both local
    and network sources are reachable.

    **Security contract for ``obsidian://`` (and any future local-scheme that
    uses ``tenant``):** ``tenant["vault_root"]`` MUST be server-provisioned
    per-tenant config, NEVER sourced from client-supplied request data.  The
    traversal guard in ``_read_obsidian_uri`` is defense-in-depth.
    Cross-tenant isolation is structural: the handler only ever sees the
    context supplied for the current request's tenant.
    """
    if not uri:
        return ReadFail(reason="unreachable", detail="empty uri")
    parsed = urlparse(uri)
    scheme = parsed.scheme
    if not scheme:
        return ReadFail(reason="scheme_unknown", detail=f"no scheme in {uri!r}")
    reader = _READERS.get(scheme)
    if reader is None:
        return ReadFail(
            reason="scheme_unknown",
            detail=f"no reader for scheme {scheme!r}",
        )
    # ``tenant`` carries per-request context for local-scheme handlers (e.g.
    # obsidian:// needs ``tenant["vault_root"]``).  Handlers that don't need it
    # accept ``**_kw`` and ignore it; the kwarg is always forwarded so new
    # handlers can adopt it without a signature change at the call site.
    vault_root = tenant.get("vault_root") if tenant else None
    return reader(
        uri, t3=t3, scratch=scratch,
        doc_id_lookup=doc_id_lookup,
        manifest_lookup=manifest_lookup,
        vault_root=vault_root,
    )


# ── RDR-169 G6: Staleness / dangling signal ───────────────────────────────────
#
# A reference-only chunk's bytes can change or disappear outside nexus.
# G6 defines a READ-TIME signal: compare the reference's RECORDED source_mtime
# (catalog_documents.source_mtime, a POSIX float; callers coerce NULL → 0.0)
# against the resolver's CURRENT view of the source.  Four outcomes:
#
#   fresh    — recorded mtime >= current source mtime (no change since indexing),
#              OR scheme has no meaningful mtime (chroma, nx-scratch, https Phase A)
#   stale    — recorded mtime < current source mtime  (source newer than record)
#   dangling — source is CONFIRMED absent (FileNotFoundError on a known path)
#   unknown  — check was indeterminate (transient error, deferred scheme, etc.)
#
# The ``dangling`` outcome (CONFIRMED-ABSENT only) mirrors the ``allow_dangling``
# convention in catalog_links.py:  with ``allow_dangling=False`` a confirmed-absent
# reference raises ``ValueError``; with ``allow_dangling=True`` it is surfaced as a
# signal but does not raise.  ``unknown`` (indeterminate) NEVER raises regardless
# of ``allow_dangling`` — absence was not confirmed, so the strict raise would be
# a false alarm.
#
# SPLIT (inherits RDR-169 G3 reachability split):
#   Python-side (this file): ALL schemes currently in _READERS.
#     file://             — os.stat().st_mtime
#     obsidian://         — os.stat() on the resolved vault path
#     x-devonthink-item:// — os.stat() on the dt_resolver path (macOS only)
#     nx-scratch://       — scratch.get() existence check (no mtime on scratch)
#     chroma://           — content-addressed; chash IS identity → staleness N/A
#                          (always returns StatOk(current_mtime=None))
#     https://            — HEAD + Last-Modified; DEFERRED to Phase B (nexus-dtnpu)
#                          (returns StatFail so staleness_signal returns 'dangling'
#                          and callers know to defer the check)
#   Java-side: deferred to Phase B (nexus-dtnpu).  The UriSchemeHandler interface
#     has a comment-only seam; no stat/head capability is implemented yet.


@dataclass(frozen=True)
class StatOk:
    """Source is reachable.  ``current_mtime`` is the POSIX float mtime of the
    source at the time of the stat, or ``None`` for schemes where a meaningful
    mtime is unavailable (e.g. ``chroma://`` is content-addressed, ``nx-scratch``
    lacks per-entry mtime).  When ``None``, ``staleness_signal`` returns
    ``'fresh'`` — absence of a mtime signal is treated as non-stale.
    """
    current_mtime: float | None


@dataclass(frozen=True)
class StatFail:
    """Source is unreachable or the scheme has no stat capability.
    ``reason`` mirrors ``ReadFailReason``; ``detail`` is a human-readable message.
    """
    reason: str
    detail: str


StatResult = StatOk | StatFail

StalenessSignal = Literal["fresh", "stale", "dangling", "unknown"]

# StatFail.reason taxonomy for G6:
#   "absent"       — confirmed gone: FileNotFoundError on a local path we could stat.
#                    staleness_signal raises ValueError (allow_dangling=False) or
#                    returns "dangling" (allow_dangling=True).
#   "error"        — indeterminate: PermissionError, transient OSError, resolver
#                    failure, missing context, etc.  staleness_signal returns
#                    "unknown" and NEVER raises — absence was not confirmed.
#   "deferred"     — scheme has no stat capability in Phase A (https://).
#                    staleness_signal returns "unknown".
#   "scheme_unknown" — no handler registered.  staleness_signal returns "unknown".
#   "unreachable"  — legacy / generic failure; treated as "error" (indeterminate).
#
# source_mtime coercion note: catalog_documents.source_mtime is a FLOAT NOT NULL
# DEFAULT 0.0 (or equivalent at the Python layer).  Callers of staleness_signal
# must coerce NULL → 0.0 before calling; this function receives 0.0 as the
# "never stamped" sentinel and handles it correctly (0.0 < any real mtime → stale).


# ── Per-scheme stat handlers ──────────────────────────────────────────────────


def _stat_file_uri(uri: str, **_kw: Any) -> StatResult:
    """Stat a ``file://`` URI — return current mtime without reading bytes."""
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if not path:
        return StatFail(reason="error", detail=f"empty path in {uri!r}")
    try:
        st = os.stat(path)
    except FileNotFoundError as e:
        # Confirmed absent — staleness_signal will treat as "dangling".
        return StatFail(reason="absent", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        # Indeterminate — can't confirm absence.
        return StatFail(reason="error", detail=f"PermissionError: {e}")
    except OSError as e:
        return StatFail(reason="error", detail=f"{type(e).__name__}: {e}")
    return StatOk(current_mtime=st.st_mtime)


def _stat_obsidian_uri(
    uri: str,
    *,
    vault_root: Any = None,
    **_kw: Any,
) -> StatResult:
    """Stat an ``obsidian://`` URI — resolve vault path, stat without reading.

    Uses the same ``vault_root`` convention as ``_read_obsidian_uri``
    (server-provisioned per-tenant config, never from URI params).

    Applies the same ``_BLOCKED_ROOTS`` + ``is_absolute()`` guard as
    ``_read_obsidian_uri`` to prevent "/" vault_root from making the
    traversal check vacuous.
    """
    if vault_root is None:
        return StatFail(
            reason="error",
            detail="obsidian:// stat requires vault_root (tenant context not provided)",
        )
    from pathlib import Path  # noqa: PLC0415 — deferred, matches _read_obsidian_uri

    # Identical guard to _read_obsidian_uri via the shared _OBSIDIAN_BLOCKED_ROOTS.
    # vault_root is resolved before the check so macOS symlinks (/etc → /private/etc)
    # are caught in their canonical form.
    vault_path = Path(vault_root).resolve()
    if not vault_path.is_absolute() or str(vault_path) in _OBSIDIAN_BLOCKED_ROOTS:
        return StatFail(
            reason="error",
            detail=(
                f"vault_root {str(vault_path)!r} is not permitted — must be an "
                f"absolute path to a user vault directory, never a system root. "
                f"vault_root MUST be server-provisioned config, not client-supplied."
            ),
        )

    parsed = urlparse(uri)
    params = parse_qs(parsed.query)
    file_parts = params.get("file")
    if not file_parts or not file_parts[0]:
        return StatFail(
            reason="error",
            detail=f"obsidian:// URI missing 'file' query param: {uri!r}",
        )
    raw_file = file_parts[0]  # parse_qs already unquotes
    try:
        candidate = (vault_path / raw_file).resolve()
        candidate.relative_to(vault_path)  # traversal guard
    except ValueError:
        return StatFail(
            reason="error",
            detail=f"path traversal rejected for {uri!r}",
        )
    try:
        st = os.stat(candidate)
    except FileNotFoundError as e:
        # Confirmed absent — staleness_signal will treat as "dangling".
        return StatFail(reason="absent", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        return StatFail(reason="error", detail=f"PermissionError: {e}")
    except OSError as e:
        return StatFail(reason="error", detail=f"{type(e).__name__}: {e}")
    return StatOk(current_mtime=st.st_mtime)


def _stat_devonthink_uri(
    uri: str,
    *,
    dt_resolver: Callable[[str], tuple[str | None, str]] | None = None,
    **_kw: Any,
) -> StatResult:
    """Stat an ``x-devonthink-item://`` URI — resolve path via dt, stat without reading.

    macOS-only; returns ``StatFail`` on other platforms (same as the full reader).
    """
    import sys  # noqa: PLC0415 — deferred, matches _read_devonthink_uri
    if dt_resolver is None:
        if sys.platform != "darwin":
            return StatFail(
                reason="unreachable",
                detail="DEVONthink stat is macOS-only",
            )
        dt_resolver = _devonthink_resolver_default
    parsed = urlparse(uri)
    uuid = parsed.netloc or parsed.path.lstrip("/")
    if not uuid:
        return StatFail(
            reason="unreachable",
            detail=f"empty UUID in DEVONthink URI {uri!r}",
        )
    path, error_detail = dt_resolver(uuid)
    if path is None:
        return StatFail(
            reason="error",
            detail=error_detail or "DEVONthink resolver returned no path",
        )
    try:
        st = os.stat(path)
    except FileNotFoundError as e:
        # Confirmed absent — staleness_signal will treat as "dangling".
        return StatFail(reason="absent", detail=f"FileNotFoundError: {e}")
    except PermissionError as e:
        return StatFail(reason="error", detail=f"PermissionError: {e}")
    except OSError as e:
        return StatFail(reason="error", detail=f"{type(e).__name__}: {e}")
    return StatOk(current_mtime=st.st_mtime)


def _stat_scratch_uri(uri: str, *, scratch: Any = None, **_kw: Any) -> StatResult:
    """Stat an ``nx-scratch://session/<session-id>/<entry-id>`` URI.

    Parses the canonical shape (same as ``_read_scratch_uri``) so that
    ``nx-scratch://session/sess123/entry-abc`` correctly extracts
    ``entry_id = "entry-abc"`` rather than ``"sess123/entry-abc"``.

    Scratch entries have no per-entry mtime, so ``StatOk.current_mtime`` is
    ``None`` on success.  The caller then receives ``'fresh'`` from
    ``staleness_signal`` — a live scratch entry is never considered stale.
    """
    if scratch is None:
        return StatFail(reason="error", detail="no scratch client provided")
    parsed = urlparse(uri)
    if parsed.netloc != "session":
        return StatFail(
            reason="error",
            detail=(
                f"unexpected netloc {parsed.netloc!r} in nx-scratch URI; "
                f"expected 'session' (shape: nx-scratch://session/<session-id>/<entry-id>)"
            ),
        )
    path = parsed.path.removeprefix("/")
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return StatFail(
            reason="error",
            detail=(
                f"malformed nx-scratch uri {uri!r}; expected "
                f"nx-scratch://session/<session-id>/<entry-id>"
            ),
        )
    _session_id, entry_id = parts
    entry = scratch.get(entry_id)
    if entry is None:
        # Confirmed absent — entry expired or deleted.
        return StatFail(
            reason="absent",
            detail=f"scratch entry {entry_id!r} not found (expired or deleted)",
        )
    return StatOk(current_mtime=None)  # no mtime on scratch → treated as fresh


def _stat_chroma_uri(uri: str, **_kw: Any) -> StatResult:
    """Stat a ``chroma://`` URI.

    ``chroma://`` is content-addressed: the chash IS the identity, so the
    content cannot change out from under the reference — a chunk either
    exists with the recorded chash or it does not.  Staleness in the
    mtime sense does not apply; this handler always returns
    ``StatOk(current_mtime=None)`` so ``staleness_signal`` returns
    ``'fresh'``.

    Dangling detection (chunk deleted from T3) is handled by the resolve
    path at serving time, not here.  Phase B may add a lightweight
    ``chroma://`` existence check if the serving layer needs it.
    """
    # Content-addressed: mtime is N/A.  Return StatOk with None so
    # staleness_signal returns 'fresh' and the caller knows to defer to
    # the serve-path existence check.
    return StatOk(current_mtime=None)


def _stat_https_uri(uri: str, **_kw: Any) -> StatResult:
    """Stat an ``https://`` URI.

    A full implementation would issue a HEAD request and parse the
    ``Last-Modified`` header.  This is DEFERRED to Phase B (nexus-dtnpu):
    HEAD + Last-Modified adds a network round-trip to the serving hot-path
    and requires timeout / retry plumbing that belongs in the Phase B
    reference-only serving milestone, not Phase A.

    In Phase A this returns ``StatOk(current_mtime=None)`` — same semantics
    as ``chroma://`` and ``nx-scratch://``: "can't check yet → treat as fresh."
    Phase B replaces this body with a live HEAD call that either returns a
    real mtime (enabling stale detection) or a confirmed-absent signal.
    """
    # Phase A: can't check → fresh (not "dangling" — absence not confirmed).
    return StatOk(current_mtime=None)


# ── Stat registry ─────────────────────────────────────────────────────────────


_STAT_HANDLERS: dict[str, Callable[..., StatResult]] = {
    "file":               _stat_file_uri,
    "obsidian":           _stat_obsidian_uri,
    "x-devonthink-item":  _stat_devonthink_uri,
    "nx-scratch":         _stat_scratch_uri,
    "chroma":             _stat_chroma_uri,
    "https":              _stat_https_uri,
}


def stat_source(
    uri: str,
    *,
    scratch: Any = None,
    tenant: dict[str, Any] | None = None,
    dt_resolver: Callable[[str], tuple[str | None, str]] | None = None,
) -> StatResult:
    """Dispatch ``uri`` to its registered stat handler by scheme.

    Returns a ``StatOk`` (source reachable, ``current_mtime`` is the POSIX
    float mtime or ``None`` for schemes without a meaningful mtime) or
    ``StatFail`` (source unreachable or scheme has no stat capability).

    Unknown schemes return ``StatFail(reason='scheme_unknown')``.

    ``tenant`` carries per-request context for local-scheme handlers
    (same contract as ``read_source``): ``tenant["vault_root"]`` is used by
    the ``obsidian://`` handler.

    ``dt_resolver`` is a test-injection hook for the ``x-devonthink-item://``
    handler; production callers leave it ``None``.

    RDR-169 G6 split (inherits G3):
    - Python-side handlers: ``file``, ``obsidian``, ``x-devonthink-item``,
      ``nx-scratch``, ``chroma`` (content-addressed → always fresh),
      ``https`` (deferred Phase A → StatOk(None) → fresh; Phase B adds HEAD).
    - Java-side stat: deferred to Phase B (nexus-dtnpu).
    """
    if not uri:
        return StatFail(reason="error", detail="empty uri")
    parsed = urlparse(uri)
    scheme = parsed.scheme
    if not scheme:
        return StatFail(reason="scheme_unknown", detail=f"no scheme in {uri!r}")
    handler = _STAT_HANDLERS.get(scheme)
    if handler is None:
        return StatFail(
            reason="scheme_unknown",
            detail=f"no stat handler for scheme {scheme!r}",
        )
    vault_root = tenant.get("vault_root") if tenant else None
    return handler(
        uri,
        scratch=scratch,
        vault_root=vault_root,
        dt_resolver=dt_resolver,
    )


def staleness_signal(
    recorded_mtime: float,
    stat_result: StatResult,
    *,
    allow_dangling: bool = False,
) -> StalenessSignal:
    """Decide the staleness state of a reference-only chunk.

    Compares the reference's RECORDED ``source_mtime`` (from
    ``catalog_documents.source_mtime``, a POSIX float seconds epoch; callers
    must coerce SQL NULL → 0.0 before calling) against the resolver's CURRENT
    view of the source (from ``stat_source()``).

    Four outcomes (``StalenessSignal = Literal['fresh', 'stale', 'dangling', 'unknown']``):

    * ``'fresh'``    — ``StatOk`` and ``current_mtime is None`` (scheme has no
                        meaningful mtime, e.g. ``chroma://``, ``nx-scratch://``,
                        or ``https://`` in Phase A)
                        OR ``StatOk`` and ``recorded_mtime >= current_mtime``
                        (source has not changed since the reference was recorded).
    * ``'stale'``    — ``StatOk`` and ``recorded_mtime < current_mtime``
                        (source has changed since the reference was recorded).
    * ``'dangling'`` — ``StatFail(reason='absent')`` only: source is confirmed
                        gone (``FileNotFoundError`` on a path we know existed).
                        With ``allow_dangling=False`` (default) this raises
                        ``ValueError``; with ``allow_dangling=True`` it returns
                        ``'dangling'`` without raising.
    * ``'unknown'``  — ``StatFail`` with any reason OTHER than ``'absent'``
                        (``'error'``, ``'deferred'``, ``'scheme_unknown'``,
                        ``'unreachable'``, etc.): the check was indeterminate.
                        NEVER raises; callers can retry or treat as non-fatal.

    The ``allow_dangling`` parameter mirrors the convention in
    ``catalog_links.py``:

    * ``allow_dangling=False`` (default) — confirmed-absent raises ``ValueError``
      matching the catalog-links error shape
      ``ValueError("dangling reference: <detail>")``.
    * ``allow_dangling=True`` — confirmed-absent is returned as ``'dangling'``
      without raising; the caller decides how to handle missing sources.

    ``recorded_mtime=0.0`` is treated as "no recorded mtime" (the catalog
    default sentinel; callers coerce NULL → 0.0 at the SQL/ORM boundary).
    When the scheme provides a real mtime, ``0.0 < current_mtime`` always,
    so a never-stamped reference is classified as ``'stale'``, which is the
    correct conservative default.

    Args:
        recorded_mtime: POSIX float from ``catalog_documents.source_mtime``
                        (callers coerce NULL → 0.0).
        stat_result:    result of ``stat_source(source_uri)``.
        allow_dangling: when ``False`` (default), confirmed-absent raises
                        ``ValueError``; when ``True``, returns ``'dangling'``.

    Returns:
        ``'fresh'``, ``'stale'``, ``'dangling'``, or ``'unknown'``.

    Raises:
        ValueError: when ``allow_dangling=False`` and ``stat_result`` is
                    ``StatFail(reason='absent')``.  Message begins
                    ``"dangling reference: "`` followed by ``StatFail.detail``.
    """
    if isinstance(stat_result, StatFail):
        if stat_result.reason == "absent":
            # Confirmed gone — matches catalog-links allow_dangling semantics.
            if allow_dangling:
                return "dangling"
            raise ValueError(f"dangling reference: {stat_result.detail}")
        # Indeterminate (transient error, deferred scheme, missing context, etc.)
        # — NEVER raise; cannot confirm absence.
        return "unknown"

    # StatOk
    current_mtime = stat_result.current_mtime
    if current_mtime is None:
        # Scheme has no meaningful mtime (chroma://, nx-scratch://).
        # Treat as fresh — absence of mtime signal is not evidence of staleness.
        return "fresh"

    if recorded_mtime >= current_mtime:
        return "fresh"
    return "stale"
