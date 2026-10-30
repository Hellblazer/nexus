# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``tags`` and ``category`` an indexed document carries in its catalog ``meta`` (nexus-6pfgb.8, RDR-236).

Every indexer writes the two keys on each chunk row as constants of the file's kind: ``tags`` is the file
extension (``markdown`` for a markdown file, ``pdf`` for a PDF), ``category`` is ``code``, ``prose`` or
``paper``. A chunk shared by two documents holds one row, so the row says the last writer's value; the
document's own ``meta`` says its own. This module is the one place that states those constants for the
catalog, mirroring the row writers (``code_indexer``, ``prose_indexer``, ``doc_indexer``, and the repo PDF
path in ``indexer``), and it writes nothing it does not know:

* ``tags`` is a JSON array of one tag, the shape a note's tags take (nexus-6pfgb.38), so one display reads
  both. A file with no extension has no tag: the row writers wrote ``""`` for it, and an empty value is
  never sent, because the catalog merges ``meta`` and an explicit empty overwrites.
* A ``content_type`` the indexers do not write (``knowledge``, the ``docs`` the orphan backfill can
  produce, anything else) yields ``{}``: the values are not invented.
* The repo indexer's PDF path says category ``prose`` where ``nx index pdf`` says ``paper``. That is
  RECORDED here (``pdf`` versus ``paper`` below), not fixed: each document is stamped with what its own
  row writer wrote.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

#: The markdown suffixes ``prose_indexer`` chunks as markdown (everything else under prose is tagged by
#: its own extension).
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: The catalog content types the prose indexer writes: ``rdr`` files are markdown indexed through it, so
#: the EXTENSION decides their tag, never the content type (this is not a second copy of
#: ``metadata_schema``'s rdr-to-markdown content-type map).
_PROSE_CONTENT_TYPES = frozenset({"prose", "rdr"})


def _prose_tag(suffix: str) -> str:
    return "markdown" if suffix in _MARKDOWN_SUFFIXES else suffix.lstrip(".")


def markdown_tags_category() -> dict[str, Any]:
    """What ``doc_indexer``'s markdown path writes on every chunk, whatever the file is called."""
    return {"tags": ["markdown"], "category": "prose"}


def pdf_tags_category() -> dict[str, Any]:
    """What ``doc_indexer``'s PDF path (``nx index pdf``) writes on every chunk."""
    return {"tags": ["pdf"], "category": "paper"}


def document_tags_category(content_type: str, file_path: str | Path) -> dict[str, Any]:
    """The ``tags`` and ``category`` the repo catalog pass stamps on a document of *content_type* at *file_path*.

    *content_type* is the one ``nexus.indexer._catalog_hook`` is given per file: ``code``, ``prose``,
    ``rdr`` (markdown, indexed through the prose indexer) or ``pdf`` (the repo PDF path). The extension
    is the file's last suffix lower-cased (``Path.suffix``, as the row writers read it): ``A.PY`` is
    ``py``, ``.bashrc`` and ``Makefile`` have none. Returns only the keys with a real value.
    """
    suffix = Path(str(file_path)).suffix.lower()
    if content_type == "code":
        return _with_tag(suffix.lstrip("."), "code")
    if content_type in _PROSE_CONTENT_TYPES:
        return _with_tag(_prose_tag(suffix), "prose")
    if content_type == "pdf":
        return {"tags": ["pdf"], "category": "prose"}
    return {}


def _with_tag(tag: str, category: str) -> dict[str, Any]:
    out: dict[str, Any] = {"category": category}
    if tag:
        out["tags"] = [tag]
    return out

