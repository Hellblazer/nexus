# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared type definitions for Nexus."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromotionReport:
    """Result of promoting a T1 scratch entry to T2.

    action: 'new' (no overlap), 'merged' (FTS5 found similar content),
            or 'conflicting' (reserved for Phase 3 semantic conflict detection).
    """

    action: str  # Literal["new", "merged", "conflicting"]
    existing_title: str | None = None
    merged: bool = False


@dataclass
class SearchResult:
    """A single result returned by semantic or hybrid search."""

    id: str
    content: str
    distance: float
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    hybrid_score: float = 0.0
