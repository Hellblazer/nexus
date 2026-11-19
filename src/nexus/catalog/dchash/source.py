# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 2: a document's source file, a dchash span's text, and its status.

A dchash span is checked against the document's source file, never against
T3 chunks, so this module makes no vector-store call. Its only engine call
is ``catalog.resolve_path``, which raises ``httpx.HTTPError`` subclasses or
``RuntimeError`` when the engine fails.

- :func:`resolve_dchash_text` is the read path behind ``nx catalog show``: every
  failure, engine errors included, becomes ``None`` and a log line.
- :func:`dchash_status` checks one span: an engine error propagates, so an
  outage fails the check instead of marking the span stale. The audit
  (``audit.dchash_audit``) uses the same :func:`span_status` rules, reading
  each document once, and propagates engine errors the same way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from nexus.catalog.dchash.text import canonical_text, parse_dchash_span, text_hash

if TYPE_CHECKING:
    from nexus.catalog.types import CatalogEntry

_log = structlog.get_logger(__name__)

OK: Final = "ok"
MALFORMED: Final = "malformed"
MISSING: Final = "missing"
UNREADABLE: Final = "unreadable"
CHANGED: Final = "changed"
OUT_OF_RANGE: Final = "out_of_range"
STATUSES: Final[tuple[str, ...]] = (
    OK,
    MALFORMED,
    MISSING,
    UNREADABLE,
    CHANGED,
    OUT_OF_RANGE,
)


def dchash_source(entry: CatalogEntry, catalog: Any | None) -> Path | None:
    """The source file of *entry*, or None when none can be named.

    Order: ``catalog.resolve_path`` (repo-relative paths anchored on the
    owner's repository root; None for a curator-owned single file), then the
    entry's ``file://`` source_uri, then an absolute ``file_path``. A relative
    ``file_path`` is never anchored on the working directory.
    """
    if catalog is not None:
        path = catalog.resolve_path(entry.tumbler)
        if path is not None:
            return Path(path)
    from nexus.aspect_readers import file_uri_path  # noqa: PLC0415 — deferred: aspect_readers is outside the catalog package

    uri_path = file_uri_path(entry.source_uri or "")
    if uri_path:
        return Path(uri_path)
    if entry.file_path and os.path.isabs(entry.file_path):
        return Path(entry.file_path)
    return None


def read_canonical(path: Path) -> str:
    """The canonical text of the file at *path*; OSError and UnicodeDecodeError propagate."""
    return canonical_text(path.read_bytes())


def span_status(
    span: str, doc: tuple[str, int] | None, *, entry_found: bool = True
) -> str:
    """The status of *span*: the one mapping the status check, the resolver and the link audit share.

    *doc* is the document's canonical text as (hash, length), or None when it could not be
    read; *entry_found* is False when the entry is gone. Order: malformed, missing,
    unreadable, changed, out_of_range, ok.
    """
    try:
        span_hash, _start, end = parse_dchash_span(span)
    except ValueError:
        return MALFORMED
    if not entry_found:
        return MISSING
    if doc is None:
        return UNREADABLE
    doc_hash, doc_len = doc
    if doc_hash != span_hash:
        return CHANGED
    if end > doc_len:
        return OUT_OF_RANGE
    return OK


def read_doc(path: Path | None) -> tuple[str, int] | None:
    """(hash, length) of the canonical text at *path*, or None when there is no path or it cannot be read.

    Read failures (OSError, a decode error, a path with a NUL byte) become None; nothing else is caught.
    """
    if path is None:
        return None
    try:
        text = read_canonical(path)
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
    ):  # ValueError: a NUL byte in the path
        return None
    return text_hash(text), len(text)


def dchash_status(entry: CatalogEntry | None, span: str, catalog: Any | None) -> str:
    """One of :data:`STATUSES` for *span* on *entry* (None for an entry that is gone).

    ``httpx.HTTPError`` and ``RuntimeError`` from the catalog propagate.
    """
    if entry is None:
        return span_status(span, None, entry_found=False)
    if span_status(span, None) == MALFORMED:
        return MALFORMED
    return span_status(span, read_doc(dchash_source(entry, catalog)))


def resolve_dchash_text(
    entry: CatalogEntry, span: str, catalog: Any | None
) -> str | None:
    """The passage *span* names, or None when it does not resolve exactly.

    Every exception becomes None (and a ``dchash_span_unresolved`` log line), so a
    bad span or an engine outage inside this branch never breaks ``nx catalog show``.
    """
    try:
        _hash, start, end = parse_dchash_span(span)
        path = dchash_source(entry, catalog)
        if path is None:
            return None
        text = read_canonical(path)
        if span_status(span, (text_hash(text), len(text))) != OK:
            return None
        return text[start:end]
    except Exception as exc:  # noqa: BLE001 — the read path never raises (RDR-171 S-A)
        _log.warning(
            "dchash_span_unresolved",
            span=span,
            tumbler=str(entry.tumbler),
            error=type(exc).__name__,
        )
        return None
