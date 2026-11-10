# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""What the index hook hands the link generators (RDR-193 Step 7, residual 6).

Split out of ``link_generator`` (nexus-u5m5g.8, fix round 1) to keep that module under its size
ceiling; ``link_generator`` re-exports both names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from nexus.catalog.tumbler import Tumbler


@dataclass(frozen=True)
class HookLinkInputs:
    """What the index hook already knows about the documents of this run.

    The hook registered them in one reconcile commit and holds each one's tumbler, repo-relative
    path and content type (``file_to_doc_id`` and the staged rows). Handing that to the generators
    is what lets them skip the catalog reads that used to learn it: the type listings, and the
    per-document probe.

    *owner* is the repository owner whose files these are; a path edge names it, so the engine
    resolves each path inside that repository. *docs* maps a tumbler to ``(file_path,
    content_type)``.
    """

    owner: str
    docs: Mapping[str, tuple[str, str]]


def hook_link_inputs(
    owner: object,
    *,
    file_to_doc_id: Mapping[Path, str],
    rel_to_abs: Mapping[str, Path],
    staged: Mapping[str, Mapping[str, object]],
    seeds: Iterable[Tumbler | str],
) -> HookLinkInputs:
    """Build the generators' inputs from the hook's own structures, with no catalog read.

    *seeds* are the tumblers the generators will be seeded with (this run's new documents, and the
    RDRs whose content changed). A seed the run has no staged row for is left out, never guessed:
    a generator that needs it falls back to one ``resolve_many`` for the missing ones.
    """
    abs_by_tumbler = {str(t): p for p, t in file_to_doc_id.items()}
    rel_by_abs = {p: rel for rel, p in rel_to_abs.items()}
    docs: dict[str, tuple[str, str]] = {}
    for seed in seeds:
        tumbler = str(seed)
        abs_path = abs_by_tumbler.get(tumbler)
        rel = rel_by_abs.get(abs_path) if abs_path is not None else None
        row = staged.get(rel) if rel is not None else None
        if row is None:
            continue
        docs[tumbler] = (str(row.get("file_path") or rel), str(row.get("content_type") or ""))
    return HookLinkInputs(owner=str(owner), docs=docs)
