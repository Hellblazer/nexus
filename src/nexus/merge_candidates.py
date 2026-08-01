# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx collection merge-candidates`` — RDR-087 Phase 4.3, RETIRED SWEEP.

The pair-wise cross-collection overlap analysis ranked
(source_collection, topics.collection) pairs by
``shared_topics * mean_similarity`` via raw SQL over the local SQLite
``topic_assignments`` table. That store was deleted in the RDR-158 P4
retirement, and the analysis has no service equivalent yet (the SQL —
self-joins over topic_assignments with hub exclusion — is not exposed by
the engine's taxonomy API), so the verb reports itself unavailable
rather than fabricating an answer (see the nexus-9613q.4 asymmetry note:
this raw read WAS the command's primary output, so an explicit
"unavailable" beats a silent empty result). A service-side twin is a
recorded GAP on nexus-i711w.1.

The dead analysis implementation (``compute_merge_candidates``, hub
exclusion, human/JSON renderers) is in git at 2e0f9eaf for whoever
builds the engine twin.
"""
from __future__ import annotations


def run_merge_candidates(
    *,
    min_shared: int,
    min_similarity: float,
    exclude_hubs: bool,
    hub_top_n: int,
    limit: int,
    fmt: str,
) -> str:
    """Report that merge-candidate analysis is unavailable.

    Parameters are retained for CLI signature stability; none is read.
    """
    return (
        "Merge-candidate analysis is unavailable: it ran raw SQL over the "
        "local SQLite taxonomy store, which was deleted in the RDR-158 P4 "
        "retirement (service mode is the only backend, and the engine "
        "does not expose this analysis yet). Track: nexus-i711w.1 GAP."
    )
