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


# ── Chroma identity-field dispatch (research-4, id 1011) ─────────────────────


# Two collection-shape conventions live in T3 today:
#   - nx index ingests (rdr__/docs__/code__) populate ``source_path``
#   - store_put MCP / nx memory promote ingests (knowledge__) populate ``title``
CHROMA_IDENTITY_FIELD: dict[str, str] = {
    "rdr__":       "source_path",
    "docs__":      "source_path",
    "code__":      "source_path",
    "knowledge__": "title",
}


def _identity_field_for(collection: str) -> str:
    """Return the chunk-metadata field that identifies a source document
    in ``collection``. Falls back to ``source_path`` for unknown
    prefixes — that's the dominant convention in legacy ingests.
    """
    for prefix, field in CHROMA_IDENTITY_FIELD.items():
        if collection.startswith(prefix):
            return field
    return "source_path"


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
    source_id = unquote(parsed.path.lstrip("/"))
    if not collection or not source_id:
        return ReadFail(
            reason="unreachable",
            detail=(
                f"malformed chroma uri (collection={collection!r}, "
                f"source_id={source_id!r})"
            ),
        )
    identity_field = _identity_field_for(collection)
    try:
        coll = t3.get_collection(collection)
    except Exception as e:
        return ReadFail(
            reason="unreachable",
            detail=f"get_collection({collection!r}) failed: {type(e).__name__}: {e}",
        )
    # Tuple is (chunk_index, insertion_seq, text). The insertion
    # sequence is the secondary sort key so that chunks with missing
    # or duplicate ``chunk_index`` values fall back to a deterministic
    # arrival-order tiebreak rather than relying on chromadb's
    # within-page ordering, which is not contractually stable.
    chunks: list[tuple[int, int, str]] = []
    seq = 0
    offset = 0
    page_limit = QUOTAS.MAX_QUERY_RESULTS
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
            detail=f"chroma.get failed: {type(e).__name__}: {e}",
        )
    if not chunks:
        return ReadFail(
            reason="empty",
            detail=(
                f"no chunks for {source_id!r} in {collection!r} "
                f"(identity_field={identity_field!r})"
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
