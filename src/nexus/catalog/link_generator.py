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
    """Auto-create 'cites' links via bib_semantic_scholar_id cross-matching.

    Uses metadata already on catalog entries — no API calls.
    created_by='bib_enricher' per RF-8.
    """
    entries = _all_entries(cat)

    # Build index: SS ID → tumbler
    id_to_tumbler: dict[str, Tumbler] = {}
    entries_with_refs: list[tuple[Tumbler, list[str]]] = []

    for entry in entries:
        ss_id = entry.meta.get("bib_semantic_scholar_id", "")
        if ss_id:
            id_to_tumbler[ss_id] = entry.tumbler
        refs = entry.meta.get("references", [])
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


def generate_rdr_filepath_links(cat: Catalog) -> int:
    """Extract file paths from RDR content and link to matching code entries.

    Scans each RDR's file on disk for source file paths (e.g.,
    ``src/nexus/catalog/catalog.py``). Matches against catalog code entries
    by file_path. Creates ``implements`` links (RDR → code).
    created_by='filepath_extractor'.
    """
    entries = _all_entries(cat)
    rdr_entries = [e for e in entries if e.content_type == "rdr" and e.file_path]
    code_entries = [e for e in entries if e.content_type == "code" and e.file_path]

    # Index: file_path → tumbler (code entries)
    path_to_code: dict[str, Tumbler] = {}
    for code in code_entries:
        path_to_code[code.file_path] = code.tumbler

    count = 0
    for rdr in rdr_entries:
        rdr_path = Path(rdr.file_path)
        if not rdr_path.is_file():
            continue
        try:
            text = rdr_path.read_text(errors="replace")
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


_MAX_RDR_MATCHES_PER_CODE = 5


def generate_code_rdr_links(cat: Catalog) -> int:
    """Heuristic: match RDR entries to code files by module name in title.

    Uses link_type='implements-heuristic' (not 'implements') to distinguish
    these substring-matched links from manually created or API-backed links.
    created_by='index_hook' per RF-8. Only matches module names > 3 chars.
    Capped at _MAX_RDR_MATCHES_PER_CODE matches per code file to prevent saturation.
    """
    entries = _all_entries(cat)
    rdr_entries = [e for e in entries if e.content_type == "rdr"]
    code_entries = [e for e in entries if e.content_type == "code"]

    # Pre-normalize RDR titles once (avoid O(n*m) re-normalization)
    rdr_normalized: list[tuple[CatalogEntry, str]] = [
        (rdr, rdr.title.lower().replace("-", "").replace(" ", "").replace("_", ""))
        for rdr in rdr_entries
    ]

    count = 0
    for code in code_entries:
        module_name = Path(code.file_path).stem.replace("_", "").lower()
        if len(module_name) <= 3:
            continue
        matches_for_code = 0
        for rdr, rdr_title_norm in rdr_normalized:
            if module_name in rdr_title_norm:
                created = cat.link_if_absent(code.tumbler, rdr.tumbler, "implements-heuristic", created_by="index_hook")
                if created:
                    count += 1
                    matches_for_code += 1
                    _log.debug(
                        "code_rdr_link_created",
                        code=str(code.tumbler), rdr=str(rdr.tumbler),
                        module=module_name,
                    )
                if matches_for_code >= _MAX_RDR_MATCHES_PER_CODE:
                    _log.warning("code_rdr_link_cap_reached", code=str(code.tumbler), module=module_name)
                    break

    return count
