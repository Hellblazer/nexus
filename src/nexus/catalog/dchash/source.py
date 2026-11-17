# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 2: a document's source file, a dchash span's text, and its status.

A dchash span is checked against the document's source file, never against
T3 chunks, so this module makes no vector-store call. Its only engine call
is ``catalog.resolve_path``, which raises ``httpx.HTTPError`` subclasses or
``RuntimeError`` when the engine fails.

- :func:`resolve_dchash_text` is the read path behind ``nx catalog show``: every
  failure, engine errors included, becomes ``None`` and a log line.
- :func:`dchash_status` is the audit path: an engine error propagates, so an
  outage fails the audit instead of marking every span stale.
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


def span_status(span_hash: str, end: int, doc: tuple[str, int] | None) -> str:
    """The status of a parsed span against a document read as (hash, length), or None when unreadable.

    The one mapping both :func:`dchash_status` and the link audit use.
    """
    if doc is None:
        return UNREADABLE
    doc_hash, doc_len = doc
    if doc_hash != span_hash:
        return CHANGED
    if end > doc_len:
        return OUT_OF_RANGE
    return OK


def read_doc(path: Path | None) -> tuple[str, int] | None:
    """(hash, length) of the canonical text at *path*, or None when there is no path or it cannot be read."""
    if path is None:
        return None
    try:
        text = read_canonical(path)
    except (OSError, UnicodeDecodeError):
        return None
    return text_hash(text), len(text)


def dchash_status(entry: CatalogEntry | None, span: str, catalog: Any | None) -> str:
    """One of :data:`STATUSES` for *span* on *entry* (None for an entry that is gone).

    ``httpx.HTTPError`` and ``RuntimeError`` from the catalog propagate.
    """
    try:
        span_hash, _start, end = parse_dchash_span(span)
    except ValueError:
        return MALFORMED
    if entry is None:
        return MISSING
    return span_status(span_hash, end, read_doc(dchash_source(entry, catalog)))


def resolve_dchash_text(
    entry: CatalogEntry, span: str, catalog: Any | None
) -> str | None:
    """The passage *span* names, or None when it does not resolve exactly.

    Every exception becomes None (and a ``dchash_span_unresolved`` log line), so
    a bad span or an engine outage never breaks ``nx catalog show``.
    """
    try:
        span_hash, start, end = parse_dchash_span(span)
        path = dchash_source(entry, catalog)
        if path is None:
            return None
        text = read_canonical(path)
        if text_hash(text) != span_hash or end > len(text):
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
