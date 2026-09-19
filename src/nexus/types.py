# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared type definitions for Nexus."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class PromotionReport:
    """Result of promoting a T1 scratch entry to T2.

    action:
      - 'new': no similar T2 entry found — clean write
      - 'overlap_detected': FTS5 found similar content under a different title;
        the entry was still written to T2 as a separate row. The agent must
        decide whether to merge/dedupe manually.
      - 'conflicting': reserved for Phase 3 semantic conflict detection

    Note: 'overlap_detected' does NOT mean merge was performed. T2.put() always
    writes the row; the report only signals that a similar entry may exist.
    """

    action: Literal["new", "overlap_detected", "conflicting"]
    existing_title: str | None = None
    merged: bool = False


@dataclass
class SearchResult:
    """A single result returned by semantic or hybrid search.

    ``content`` is ``str | None`` (RDR-169 Phase B, bead nexus-zw2em):
    ``None`` means a reference-only chunk (``retention='reference-only'``,
    ``chunk_text`` is NULL at the store) — the wire genuinely allows this
    for a row reached via plain vector search. Most construction sites
    (``search_engine.py``'s ``SearchResult`` boundary) coerce it to ``""``
    before it reaches a caller, but the type says what the wire allows
    rather than what one call site happens to guarantee.
    """

    id: str
    content: str | None
    distance: float
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    hybrid_score: float = 0.0

    #: Relevance credit from RDR-070's topic boost, in DISTANCE units, to be
    #: subtracted when an effective distance is computed. Never subtracted
    #: from :attr:`distance` itself.
    #:
    #: ``apply_topic_boost`` used to write straight into ``distance``,
    #: because ``hybrid_score`` is computed later and would overwrite
    #: anything put there. The cost was that ``distance`` — the one absolute,
    #: comparable number a consumer can judge a hit by — silently carried up
    #: to 0.15 of relevance engineering on the search path, while every
    #: surface reported it as the raw vector distance (nexus-la5pr). Holding
    #: the credit here keeps the ranking identical and the reported number
    #: honest: ``apply_hybrid_scoring`` computes ``max(0.0, distance -
    #: topic_boost) * calibration`` locally, exactly as before, and writes
    #: back nothing.
    topic_boost: float = 0.0
