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
    import os.path

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
) -> ReadResult:
    """Page through ``coll.get(where={identity_field: match_value})`` and
    return a ``ReadOk`` with chunks reassembled in ``chunk_index`` order
    (insertion-sequence tiebreaker). ``ReadFail(reason='empty')`` when
    no chunks match. Shared between the doc_id-keyed primary path
    and the legacy multi-field probe.
    """
    chunks: list[tuple[int, int, str]] = []
    seq = 0
    offset = 0
    page_limit = QUOTAS.MAX_QUERY_RESULTS
    try:
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
    if not chunks:
        return ReadFail(
            reason="empty",
            detail=f"no chunks for {match_value!r} in {collection!r}",
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


def _read_chroma_uri(
    uri: str,
    *,
    t3: Any = None,
    doc_id_lookup: Callable[[str, str], str] | None = None,
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
    except Exception as e:
        return ReadFail(
            reason="unreachable",
            detail=f"get_collection({collection!r}) failed: {type(e).__name__}: {e}",
        )

    # nexus-o6aa.10.1 doc_id-keyed path: resolve source_id → doc_id and
    # query strictly on doc_id. An empty doc_id is a structural failure
    # (catalog gap), not "no chunks".
    if doc_id_lookup is not None:
        try:
            doc_id = doc_id_lookup(collection, source_id) or ""
        except Exception as e:
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
                    f"(catalog gap or pre-backfill chunk; the prune verb "
                    f"requires --t3-doc-id-coverage=100% before running)"
                ),
            )
        return _gather_chroma_chunks_by_field(
            coll=coll, collection=collection, source_id=source_id,
            identity_field="doc_id", match_value=doc_id,
        )

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
    except Exception as e:
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
        import httpx  # noqa: PLC0415

        http_client = httpx.Client(timeout=30, follow_redirects=True)
        own_client = True
    try:
        try:
            response = http_client.get(uri)
        except Exception as e:
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
    import subprocess  # noqa: PLC0415
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
    import sys  # noqa: PLC0415

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


# ── Reader registry + dispatch helper ────────────────────────────────────────


_READERS: dict[str, Callable[..., ReadResult]] = {
    "file": _read_file_uri,
    "chroma": _read_chroma_uri,
    "nx-scratch": _read_scratch_uri,
    "https": _read_https_uri,
    "x-devonthink-item": _read_devonthink_uri,
}


def read_source(
    uri: str,
    *,
    t3: Any = None,
    scratch: Any = None,
    doc_id_lookup: Callable[[str, str], str] | None = None,
) -> ReadResult:
    """Dispatch ``uri`` to its registered reader by scheme.

    Unknown schemes return ``ReadFail(reason="scheme_unknown")``; the
    upsert-guard in ``commands/enrich.py`` (Phase 1.3) logs and skips
    on any ``ReadFail``, so a missing reader is a visible-but-graceful
    failure mode, not a crash.

    ``doc_id_lookup`` (RDR-101 Phase 4 / nexus-o6aa.10.1) is forwarded
    to the chroma reader as the source-identifier → ``doc_id``
    projection. Other readers ignore it.
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
    return reader(uri, t3=t3, scratch=scratch, doc_id_lookup=doc_id_lookup)
