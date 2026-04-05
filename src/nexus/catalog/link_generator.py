# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-generate typed links in the catalog from metadata cross-matching."""

from __future__ import annotations

from pathlib import Path

import structlog

from nexus.catalog.catalog import Catalog, CatalogEntry
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger()


def _all_entries(cat: Catalog) -> list[CatalogEntry]:
    """Fetch all catalog entries."""
    rows = cat._db._conn.execute(
        "SELECT tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
        "FROM documents"
    ).fetchall()
    import json

    return [
        CatalogEntry(
            tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
            content_type=r[4], file_path=r[5], corpus=r[6],
            physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
            indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
        )
        for r in rows
    ]


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
                existing = cat.links_from(from_tumbler, link_type="cites")
                already = any(l.to_tumbler == to_tumbler for l in existing)
                if not already:
                    cat.link(from_tumbler, to_tumbler, "cites", created_by="bib_enricher")
                    count += 1
                    _log.debug("citation_link_created", from_t=str(from_tumbler), to_t=str(to_tumbler))

    return count


def generate_code_rdr_links(cat: Catalog) -> int:
    """Heuristic: match RDR entries to code files by module name in title.

    created_by='index_hook' per RF-8. Only matches module names > 3 chars.
    """
    entries = _all_entries(cat)
    rdr_entries = [e for e in entries if e.content_type == "rdr"]
    code_entries = [e for e in entries if e.content_type == "code"]

    count = 0
    for rdr in rdr_entries:
        rdr_title_norm = rdr.title.lower().replace("-", "").replace(" ", "").replace("_", "")
        for code in code_entries:
            module_name = Path(code.file_path).stem.replace("_", "").lower()
            if len(module_name) <= 3:
                continue
            if module_name in rdr_title_norm:
                existing = cat.links_from(rdr.tumbler, link_type="implements")
                already = any(l.to_tumbler == code.tumbler for l in existing)
                if not already:
                    cat.link(rdr.tumbler, code.tumbler, "implements", created_by="index_hook")
                    count += 1
                    _log.debug(
                        "code_rdr_link_created",
                        rdr=str(rdr.tumbler), code=str(code.tumbler),
                        module=module_name,
                    )

    return count
