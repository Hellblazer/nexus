# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-083: Corpus-evidence resolvers — ``{{nx-anchor:…}}`` + future kin.

Plugs into RDR-082's :class:`~nexus.doc.resolvers.ResolverRegistry`
as the first external consumer, verifying that the extension-point
design does the job.
"""
from __future__ import annotations

from typing import Any

from nexus.doc.resolvers import ResolutionError

__all__ = ["AnchorResolver"]


class AnchorResolver:
    """``{{nx-anchor:<collection>[|top=N]}}`` — top-N projected topics
    for a collection, rendered as a markdown bullet list.

    Reads from ``topic_assignments`` via
    :meth:`CatalogTaxonomy.top_topics_for_collection`. No new schema.
    """

    def __init__(self, *, taxonomy: Any) -> None:
        self._tax = taxonomy

    def resolve(
        self, key: str, field: str | None, filters: dict[str, str],
    ) -> str:
        top = _int_filter(filters, "top", default=5)
        rows = self._tax.top_topics_for_collection(key, top_n=top)
        if not rows:
            raise ResolutionError(
                f"no projection data for collection {key!r} "
                f"(run `nx taxonomy project` to populate)"
            )
        lines = []
        for row in rows:
            label = row.get("label") or "<unlabeled>"
            chunks = row.get("chunks")
            if chunks:
                lines.append(f"- {label} ({chunks} chunks)")
            else:
                lines.append(f"- {label}")
        return "\n".join(lines)


def _int_filter(filters: dict[str, str], key: str, *, default: int) -> int:
    raw = filters.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
