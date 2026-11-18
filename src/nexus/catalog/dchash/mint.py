# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 3: mint a ``dchash:`` span from offsets or from a quoted passage.

Both functions take a catalog reader (``resolve`` and, through
:func:`~nexus.catalog.dchash.source.dchash_source`, ``resolve_path``); they
write nothing, so the client's write whitelist does not change. The refusals
run before the source file is read, so a PDF whose bytes are not UTF-8 is
refused as a PDF, not as a decode error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nexus.aspect_readers import NOTE_URI_SCHEME
from nexus.catalog.dchash.source import dchash_source, read_canonical
from nexus.catalog.dchash.text import text_hash
from nexus.catalog.types import CatalogEntry

_PDF_TYPES = frozenset({"paper", "pdf"})


def _is_pdf(entry: CatalogEntry) -> bool:
    # `nx index pdf` registers PDFs as "paper" and the repo pass as "prose", so
    # the type alone misses them: the suffix of either path decides too.
    if entry.content_type in _PDF_TYPES:
        return True
    if entry.file_path.lower().endswith(".pdf"):
        return True
    try:
        uri_path = urlparse(entry.source_uri or "").path
    except ValueError:  # a malformed URI (e.g. "http://[bad") names no path
        uri_path = ""
    return uri_path.lower().endswith(".pdf")


def _source_path(catalog: Any, tumbler: str) -> Path:
    """The source path of *tumbler*; ValueError when it is unknown, a PDF or has no file."""
    entry = catalog.resolve(tumbler)
    if entry is None:
        raise ValueError(f"{tumbler}: no such catalog document")
    if _is_pdf(entry):
        raise ValueError(f"{tumbler} is a PDF; dchash spans need a text source")
    path = (
        None
        if (entry.source_uri or "").startswith(NOTE_URI_SCHEME)
        else dchash_source(entry, catalog)
    )
    if path is None:
        raise ValueError(f"{tumbler} has no file source")
    return path


def _span(text: str, start: int, end: int) -> str:
    return f"dchash:{text_hash(text)}:{start}-{end}"


def dchash_span(catalog: Any, tumbler: str, start: int, end: int) -> str:
    """The span for code points ``[start, end)`` of the canonical text of *tumbler*.

    ValueError for an unknown tumbler, a PDF, a document with no file source, or
    a range outside ``0 <= start < end <= len(text)``. ``OSError`` and
    ``UnicodeDecodeError`` from reading the source propagate.
    """
    text = read_canonical(_source_path(catalog, tumbler))
    if not 0 <= start < end <= len(text):
        raise ValueError(
            f"{tumbler}: range {start}-{end} is outside the document "
            f"(need 0 <= start < end <= {len(text)})"
        )
    return _span(text, start, end)


def dchash_span_for_passage(catalog: Any, tumbler: str, passage: str) -> str:
    """The span of the one place *passage* occurs in the canonical text of *tumbler*.

    The passage's line ends are normalized the way the text's are. ValueError
    for the refusals of :func:`dchash_span`, an empty passage, a passage that
    is absent, or one that occurs more than once (overlapping occurrences count).
    """
    if not passage:
        raise ValueError("passage is empty")
    path = _source_path(catalog, tumbler)
    needle = passage.replace("\r\n", "\n").replace("\r", "\n")
    text = read_canonical(path)
    first = text.find(needle)
    if first < 0:
        raise ValueError(f"{tumbler}: passage not found")
    count, idx = 0, first
    while idx >= 0:
        count += 1
        idx = text.find(needle, idx + 1)
    if count > 1:
        raise ValueError(f"{tumbler}: passage occurs {count} times; quote more text")
    return _span(text, first, first + len(needle))
