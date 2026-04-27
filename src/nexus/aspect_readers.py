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

Knowledge collections (``knowledge__*``) identify documents by metadata
field ``title`` (slug); ``nx index``-ingested collections
(``rdr__/docs__/code__``) identify by ``source_path``. The
``CHROMA_IDENTITY_FIELD`` dispatch table picks the right field per
collection prefix — querying ``where={"source_path": ...}`` against
``knowledge__*`` chunks returns empty (root cause behind issue #333,
verified empirically in research-4, id 1011).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import structlog

from nexus.db.chroma_quotas import QUOTAS

_log = structlog.get_logger(__name__)


__all__ = [
    "CHROMA_IDENTITY_FIELD",
    "ReadFail",
    "ReadFailReason",
    "ReadOk",
    "ReadResult",
    "read_source",
]


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


# ── Chroma identity-field dispatch (research-4, id 1011; P2.0 refined) ───────


# Three collection-shape conventions live in T3 today:
#   - nx index ingests (rdr__/docs__/code__/knowledge__<paper-coll>) populate
#     ``source_path`` (filesystem path or PDF path).
#   - store_put MCP / nx memory promote ingests (knowledge__knowledge) populate
#     ``title`` (slug). The chunk has NO source_path metadata.
#   - PDF papers ingested into knowledge__<corpus> via nx index pdf populate
#     ``source_path`` (filesystem path); ``title`` is None on these chunks.
#
# The mapping value is an ORDERED tuple: the reader tries each field in turn
# and uses the first one that returns chunks. ``source_path`` is preferred
# because it dominates the catalog by chunk-volume; ``title`` is the
# slug-shaped fallback for memory-promoted entries.
CHROMA_IDENTITY_FIELD: dict[str, tuple[str, ...]] = {
    "rdr__":       ("source_path",),
    "docs__":      ("source_path",),
    "code__":      ("source_path",),
    "knowledge__": ("source_path", "title"),
}


def _identity_fields_for(collection: str) -> tuple[str, ...]:
    """Return the ordered tuple of chunk-metadata fields that identify
    a source document in ``collection``. Falls back to
    ``("source_path",)`` for unknown prefixes — the dominant
    convention in legacy ingests.
    """
    for prefix, fields in CHROMA_IDENTITY_FIELD.items():
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


def _read_chroma_uri(uri: str, *, t3: Any = None, **_kw: Any) -> ReadResult:
    """Reassemble a document from chroma chunks.

    URI shape: ``chroma://<collection>/<source-identifier>``. The
    identity field that ``<source-identifier>`` is matched against is
    determined by the collection prefix via
    :data:`CHROMA_IDENTITY_FIELD` — ``source_path`` for
    rdr__/docs__/code__, ``title`` for knowledge__.

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
    identity_fields = _identity_fields_for(collection)
    try:
        coll = t3.get_collection(collection)
    except Exception as e:
        return ReadFail(
            reason="unreachable",
            detail=f"get_collection({collection!r}) failed: {type(e).__name__}: {e}",
        )
    # Try each identity field in order; first non-empty result wins.
    # Empirically (P2.0 spike survey 2026-04-27): knowledge__delos and
    # knowledge__art chunks identify via ``source_path``; only
    # knowledge__knowledge (slug-shaped MCP-promoted notes) identify via
    # ``title``. The fallback list lets one reader handle both shapes.
    matched_field = ""
    chunks: list[tuple[int, int, str]] = []
    page_limit = QUOTAS.MAX_QUERY_RESULTS
    for identity_field in identity_fields:
        # Tuple is (chunk_index, insertion_seq, text). The insertion
        # sequence is the secondary sort key so chunks with missing
        # or duplicate ``chunk_index`` values fall back to a
        # deterministic arrival-order tiebreak rather than relying on
        # chromadb's within-page ordering, which is not contractually
        # stable.
        chunks = []
        seq = 0
        offset = 0
        try:
            while True:
                page = coll.get(
                    where={identity_field: source_id},
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
                    ci = md.get("chunk_index", 0) if isinstance(md, dict) else 0
                    chunks.append((int(ci), seq, doc))
                    seq += 1
                if len(docs) < page_limit:
                    break
                offset += page_limit
        except Exception as e:
            return ReadFail(
                reason="unreachable",
                detail=(
                    f"chroma.get failed (field={identity_field!r}): "
                    f"{type(e).__name__}: {e}"
                ),
            )
        if chunks:
            matched_field = identity_field
            break
    if not chunks:
        return ReadFail(
            reason="empty",
            detail=(
                f"no chunks for {source_id!r} in {collection!r} "
                f"(identity_fields tried={list(identity_fields)})"
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
            "identity_field": matched_field,
            "chunk_count": len(chunks),
        },
    )


# ── Reader registry + dispatch helper ────────────────────────────────────────


_READERS: dict[str, Callable[..., ReadResult]] = {
    "file": _read_file_uri,
    "chroma": _read_chroma_uri,
}


def read_source(
    uri: str,
    *,
    t3: Any = None,
    scratch: Any = None,
) -> ReadResult:
    """Dispatch ``uri`` to its registered reader by scheme.

    Unknown schemes return ``ReadFail(reason="scheme_unknown")``; the
    upsert-guard in ``commands/enrich.py`` (Phase 1.3) logs and skips
    on any ``ReadFail``, so a missing reader is a visible-but-graceful
    failure mode, not a crash.
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
    return reader(uri, t3=t3, scratch=scratch)
