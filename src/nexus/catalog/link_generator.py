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


_MAX_ENTITY_MATCHES_PER_CODE = 10


def _normalize_name(name: str) -> list[str]:
    """CamelCase, snake_case, kebab-case -> list of lowercase tokens (len > 2)."""
    # CamelCase split: insert space before uppercase preceded by lowercase
    camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    # Replace non-alphanumeric with space
    flat = re.sub(r"[_\-\s]+", " ", camel).strip().lower()
    return [t for t in flat.split() if len(t) > 2]


def generate_entity_name_links(
    cat: Catalog,
    *,
    new_tumblers: list[Tumbler] | None = None,
) -> int:
    """Match code symbol names against knowledge/RDR titles via normalization.

    Exact normalized match -> 'relates' link.
    created_by='entity_name_matcher'.

    When *new_tumblers* is provided, only those entries are evaluated
    (incremental mode). Pass ``None`` for full-scan.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0

    entries = _all_entries(cat)
    code_entries = [e for e in entries if e.content_type == "code"]
    prose_entries = [
        e for e in entries if e.content_type in ("knowledge", "rdr")
    ]

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        new_code = [e for e in code_entries if str(e.tumbler) in new_set]
        new_prose = [e for e in prose_entries if str(e.tumbler) in new_set]
        # New code × all prose, plus all code × new prose (minus overlap)
        pairs: list[tuple[CatalogEntry, list[CatalogEntry]]] = []
        if new_code:
            pairs.extend((c, prose_entries) for c in new_code)
        if new_prose:
            for code in code_entries:
                if str(code.tumbler) in new_set:
                    continue  # already covered
                pairs.append((code, new_prose))
    else:
        pairs = [(c, prose_entries) for c in code_entries]

    # Pre-normalize prose titles
    prose_normalized: dict[int, tuple[CatalogEntry, set[str]]] = {}
    for i, prose in enumerate(prose_entries):
        tokens = set(_normalize_name(prose.title))
        if tokens:
            prose_normalized[id(prose)] = (prose, tokens)

    count = 0
    for code, prose_list in pairs:
        code_tokens = set(_normalize_name(code.title))
        if not code_tokens:
            continue
        matches_for_code = 0
        for prose in prose_list:
            cached = prose_normalized.get(id(prose))
            if cached is None:
                continue
            _, prose_tokens = cached
            # Exact match: all code tokens are a subset of prose tokens
            if code_tokens.issubset(prose_tokens):
                try:
                    created = cat.link_if_absent(
                        code.tumbler, prose.tumbler, "relates",
                        created_by="entity_name_matcher",
                    )
                except ValueError:
                    continue
                if created:
                    count += 1
                    matches_for_code += 1
                    _log.debug(
                        "entity_name_link_created",
                        code=str(code.tumbler),
                        prose=str(prose.tumbler),
                        code_tokens=list(code_tokens),
                    )
                if matches_for_code >= _MAX_ENTITY_MATCHES_PER_CODE:
                    _log.warning(
                        "entity_name_link_cap_reached",
                        code=str(code.tumbler),
                    )
                    break

    return count


_MAX_RDR_MATCHES_PER_CODE = 5


def generate_code_rdr_links(cat: Catalog, *, new_tumblers: list[Tumbler] | None = None) -> int:
    """Heuristic: match RDR entries to code files by module name in title.

    Uses link_type='implements-heuristic' (not 'implements') to distinguish
    these substring-matched links from manually created or API-backed links.
    created_by='index_hook' per RF-8. Only matches module names > 3 chars.
    Capped at _MAX_RDR_MATCHES_PER_CODE matches per code file to prevent saturation.

    When *new_tumblers* is provided (incremental mode), only newly added entries
    are evaluated:
    - New code entries × all RDRs
    - All code entries × new RDR entries
    Pass ``None`` (default) for the full-scan behavior.
    """
    if new_tumblers is not None and len(new_tumblers) == 0:
        return 0

    entries = _all_entries(cat)
    rdr_entries = [e for e in entries if e.content_type == "rdr"]
    code_entries = [e for e in entries if e.content_type == "code"]

    # Pre-normalize RDR titles once (avoid O(n*m) re-normalization)
    rdr_normalized: list[tuple[CatalogEntry, str]] = [
        (rdr, rdr.title.lower().replace("-", "").replace(" ", "").replace("_", ""))
        for rdr in rdr_entries
    ]

    if new_tumblers is not None:
        new_set = {str(t) for t in new_tumblers}
        new_code = [e for e in code_entries if str(e.tumbler) in new_set]
        new_rdrs_normalized = [(rdr, norm) for rdr, norm in rdr_normalized if str(rdr.tumbler) in new_set]

        # Determine which (code, rdr) pairs to evaluate:
        # 1. new code × all RDRs
        # 2. all code × new RDRs  (minus pairs already covered by #1 to avoid double-count)
        pairs: list[tuple[CatalogEntry, list[tuple[CatalogEntry, str]]]] = []
        if new_code:
            pairs.extend((c, rdr_normalized) for c in new_code)
        if new_rdrs_normalized:
            new_rdr_set = {str(rdr.tumbler) for rdr, _ in new_rdrs_normalized}
            for code in code_entries:
                if str(code.tumbler) in new_set:
                    continue  # already covered in #1
                pairs.append((code, new_rdrs_normalized))
    else:
        pairs = [(code, rdr_normalized) for code in code_entries]

    count = 0
    for code, rdrs_to_check in pairs:
        module_name = Path(code.file_path).stem.replace("_", "").lower()
        if len(module_name) <= 3:
            continue
        matches_for_code = 0
        for rdr, rdr_title_norm in rdrs_to_check:
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
