# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``source_format``, ``tags`` and ``category`` an indexed document carries in its catalog ``meta``
(nexus-6pfgb.23 and nexus-6pfgb.8, RDR-236 Phase 1).

Every indexer writes ``tags`` and ``category`` on each chunk row as constants of the file's kind: ``tags`` is
the file extension (``markdown`` for a markdown file, ``pdf`` for a PDF), ``category`` is ``code``, ``prose``
or ``paper``. A chunk shared by two documents holds one row, so the row says the last writer's value; the
document's own ``meta`` says its own. ``source_format`` (``pdf`` / ``markdown`` / ``code`` / ``prose``, the
chunk vocabulary) is the third such fact, and the one the engine's ``content_type=pdf`` / ``=markdown``
filters read per document. This module is the one place that states them for the catalog, mirroring the row
writers (``code_indexer``, ``prose_indexer``, ``doc_indexer``, and the repo PDF path in ``indexer``), and it
writes nothing it does not know:

* ``tags`` is a JSON array of one tag, the shape a note's tags take (nexus-6pfgb.38), so one display reads
  both. A file with no extension has no tag: the row writers wrote ``""`` for it, and an empty value is
  never sent, because the catalog merges ``meta`` and an explicit empty overwrites.
* ``source_format`` is :func:`catalog_source_format`, a Python copy of the engine's
  ``nexus.catalog_source_format(content_type, file_path)`` (vectors-043). A document is stamped with what the
  engine would derive for the same two columns, never with a value of its own, so a stamped document and a
  derived one cannot disagree (``tests/test_6pfgb8_23_indexed_meta.py`` pins the 36-row matrix of the Java
  parity test against it). A pair the rule derives nothing for sends no key.
* A ``content_type`` the indexers do not write (``knowledge``, the ``docs`` the orphan backfill can
  produce, anything else) yields ``{}`` from :func:`document_register_meta`: the values are not invented.
* The repo indexer's PDF path says category ``prose`` where ``nx index pdf`` says ``paper``. That is
  RECORDED here (``pdf`` versus ``paper`` below), not fixed: each document is stamped with what its own row
  writer wrote.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: The catalog ``meta`` key the derived format is stamped under.
SOURCE_FORMAT_KEY = "source_format"

#: Every value :func:`catalog_source_format` can answer (the chunk vocabulary).
SOURCE_FORMATS = frozenset({"pdf", "markdown", "code", "prose"})

#: The engine's extension regex, ``substring(file_path from '[^/.]\.([^./]+)$')``: the last extension, with no
#: dotfile and no dot in a directory. ``\Z`` where Postgres writes ``$``: a Python ``$`` also matches before a
#: trailing newline, which Postgres' does not.
_EXTENSION = re.compile(r"[^/.]\.([^./]+)\Z")

#: The markdown suffixes ``prose_indexer`` chunks as markdown (everything else under prose is tagged by
#: its own extension).
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: The catalog content types the prose indexer writes: ``rdr`` files are markdown indexed through it, so
#: the EXTENSION decides their tag, never the content type (this is not a second copy of
#: ``metadata_schema``'s rdr-to-markdown content-type map).
_PROSE_CONTENT_TYPES = frozenset({"prose", "rdr"})


def catalog_source_format(content_type: str | None, file_path: str | None) -> str | None:
    """The format the engine derives for a document of *content_type* at *file_path*, or ``None``.

    The rule of ``nexus.catalog_source_format`` (vectors-043), the extension read first: a last extension
    ``pdf``, or a catalog type ``pdf`` / ``paper``, is ``pdf``; an extension ``md`` / ``markdown`` is
    ``markdown`` whatever the catalog type; catalog type ``code`` is ``code``; ``prose`` / ``rdr`` is
    ``prose``; anything else (a note, no extension, a type outside the rules, an absent argument) is
    ``None``. The match is case-sensitive on the type and case-insensitive on the extension, as the SQL is.
    """
    found = _EXTENSION.search(file_path) if file_path else None
    ext = found.group(1).lower() if found else ""
    if ext == "pdf" or content_type in ("pdf", "paper"):
        return "pdf"
    if ext in ("md", "markdown"):
        return "markdown"
    if content_type == "code":
        return "code"
    if content_type in ("prose", "rdr"):
        return "prose"
    return None


def source_format_meta(content_type: str | None, file_path: str | None) -> dict[str, Any]:
    """``{"source_format": <derived>}``, or ``{}`` when the rule derives nothing."""
    derived = catalog_source_format(content_type, file_path)
    return {SOURCE_FORMAT_KEY: derived} if derived else {}


def _prose_tag(suffix: str) -> str:
    return "markdown" if suffix in _MARKDOWN_SUFFIXES else suffix.lstrip(".")


def markdown_register_meta(content_type: str, file_path: str | Path) -> dict[str, Any]:
    """What ``nx index md`` / ``nx index rdr`` / ``nx collection reindex`` / the DEVONthink content layer
    stamp: the tags and category ``doc_indexer``'s markdown path writes on every chunk, whatever the file
    is called, plus the derived ``source_format`` of *content_type* at *file_path*."""
    return {"tags": ["markdown"], "category": "prose", **source_format_meta(content_type, str(file_path))}


def pdf_register_meta(file_path: str | Path, registered_type: str = "paper") -> dict[str, Any]:
    """What ``nx index pdf`` stamps: the tags and category ``doc_indexer``'s PDF path writes on every chunk
    (it registers the document as content type ``paper``), plus the derived ``source_format``.

    *registered_type* is the content type the catalog document already carries. A document the repo
    indexer registered (``pdf``) keeps the repo path's values: ``nx index pdf`` on it would otherwise flip
    its category to ``paper`` and the next ``nx index repo`` back to ``prose``, a stamp that disagrees with
    itself run to run. Every other type takes the ``paper`` statement."""
    if registered_type == "pdf":
        return document_register_meta("pdf", file_path)
    return {"tags": ["pdf"], "category": "paper", **source_format_meta("paper", str(file_path))}


def document_register_meta(content_type: str, file_path: str | Path) -> dict[str, Any]:
    """The ``source_format``, ``tags`` and ``category`` the repo catalog pass stamps on a document of *content_type* at *file_path*.

    *content_type* is the one ``nexus.indexer._catalog_hook`` is given per file: ``code``, ``prose``,
    ``rdr`` (markdown, indexed through the prose indexer) or ``pdf`` (the repo PDF path). The extension
    is the file's last suffix lower-cased (``Path.suffix``, as the row writers read it): ``A.PY`` is
    ``py``, ``.bashrc`` and ``Makefile`` have none. Returns only the keys with a real value.
    """
    path = str(file_path)
    suffix = Path(path).suffix.lower()
    if content_type == "code":
        out = _with_tag(suffix.lstrip("."), "code")
    elif content_type in _PROSE_CONTENT_TYPES:
        out = _with_tag(_prose_tag(suffix), "prose")
    elif content_type == "pdf":
        out = {"tags": ["pdf"], "category": "prose"}  # the repo path: prose, where nx index pdf says paper
    else:
        return {}
    return {**out, **source_format_meta(content_type, path)}


def _with_tag(tag: str, category: str) -> dict[str, Any]:
    out: dict[str, Any] = {"category": category}
    if tag:
        out["tags"] = [tag]
    return out
