# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-generate typed links in the catalog from metadata cross-matching."""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from nexus.catalog.catalog import Catalog, CatalogEntry
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger()


def _all_entries(cat: Catalog) -> list[CatalogEntry]:
    """Fetch all catalog entries via the public API."""
    return cat.all_documents()


def generate_citation_links(cat: Catalog) -> int:
    """Auto-create 'cites' links via bib ID cross-matching.

    Uses metadata already on catalog entries — no API calls.
    created_by='bib_enricher' per RF-8.

    nexus-57mk: indexes both ``bib_semantic_scholar_id`` (Semantic
    Scholar paper IDs) and ``bib_openalex_id`` (OpenAlex W-ids) so a
    catalog enriched by either backend produces cite links. The
    ``references`` list on each entry contains IDs from whichever
    backend enriched that entry; matching is exact-string against the
    same ID space, so cross-backend references (a paper enriched by
    OpenAlex referencing one enriched only by S2) won't match — that's
    the correct conservative behavior, since the two ID spaces are
    distinct and we don't have a DOI bridge yet.
    """
    entries = _all_entries(cat)

    # Build index: bib ID -> tumbler. Both backends' IDs share one map
    # because their ID spaces don't collide (S2 paperIds are 40-hex
    # SHA-shaped strings; OpenAlex IDs start with 'W' followed by
    # digits). A collision would only happen if a future backend
    # introduced overlapping namespacing.
    id_to_tumbler: dict[str, Tumbler] = {}
    entries_with_refs: list[tuple[Tumbler, list[str]]] = []

    for entry in entries:
        meta = entry.meta or {}
        for id_field in ("bib_semantic_scholar_id", "bib_openalex_id"):
            bib_id = meta.get(id_field, "")
            if bib_id:
                id_to_tumbler[bib_id] = entry.tumbler
        refs = meta.get("references", [])
        if refs:
            entries_with_refs.append((entry.tumbler, refs))

    count = 0
    for from_tumbler, ref_ids in entries_with_refs:
        for ref_id in ref_ids:
            to_tumbler = id_to_tumbler.get(ref_id)
            if to_tumbler and to_tumbler != from_tumbler:
                if cat.link_if_absent(from_tumbler, to_tumbler, "cites", created_by="bib_enricher"):
                    count += 1
                    _log.debug("citation_link_created", from_t=str(from_tumbler), to_t=str(to_tumbler))

    return count


_FILE_PATH_RE = re.compile(
    r"(?:src|tests|lib|pkg|cmd|internal|app)/"  # must start with a source root
    r"[\w/.-]+"                                  # path chars
    r"\.(?:py|java|go|rs|ts|tsx|js|jsx|c|cpp|h|rb|php|swift|kt|scala)"  # source extension
)


def generate_rdr_filepath_links(cat: Catalog, *, new_tumblers: list[Tumbler] | None = None) -> int:
    """Extract file paths from RDR content and link to matching code entries.

    Scans each RDR's file on disk for source file paths (e.g.,
    ``src/nexus/catalog/catalog.py``). Matches against catalog code entries
    by file_path. Creates ``implements`` links (RDR → code).
    created_by='filepath_extractor'.

    When *new_tumblers* is provided, only those entries are scanned (incremental
    mode). Pass ``None`` (default) for the full-scan behavior.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0

    entries = _all_entries(cat)
    rdr_entries = [e for e in entries if e.content_type == "rdr" and e.file_path]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        rdr_entries = [e for e in rdr_entries if str(e.tumbler) in new_set]

    # Index: file_path → tumbler (code entries)
    path_to_code: dict[str, Tumbler] = {}
    for code in code_entries:
        path_to_code[code.file_path] = code.tumbler

    count = 0
    for rdr in rdr_entries:
        resolved = cat.resolve_path(rdr.tumbler)
        if resolved is None or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(errors="replace")
        except OSError:
            continue

        # Find all file paths in the RDR text
        seen_targets: set[str] = set()
        for match in _FILE_PATH_RE.finditer(text):
            fpath = match.group(0)
            if fpath in seen_targets:
                continue
            seen_targets.add(fpath)
            code_tumbler = path_to_code.get(fpath)
            if code_tumbler is None:
                continue
            try:
                created = cat.link_if_absent(
                    rdr.tumbler, code_tumbler, "implements",
                    created_by="filepath_extractor",
                )
            except ValueError:
                continue
            if created:
                count += 1
                _log.debug(
                    "rdr_filepath_link_created",
                    rdr=str(rdr.tumbler), code=str(code_tumbler),
                    path=fpath,
                )

    return count


