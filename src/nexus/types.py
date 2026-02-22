# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shared type definitions for Nexus."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """A single result returned by semantic or hybrid search."""

    id: str
    content: str
    distance: float
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    hybrid_score: float = 0.0
