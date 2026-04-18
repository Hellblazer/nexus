# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Catalog type aliases (RDR-086 Phase 2).

``ChunkRef`` documents the return shape of ``Catalog.resolve_chash``.
``Catalog.resolve_span`` returns the same shape (minus the explicit
``physical_collection`` and ``doc_id`` fields it leaves to its caller)
but pre-dates the TypedDict — we keep both ``chash`` and ``chunk_hash``
keys on the dict for back-compat so existing resolve_span consumers
continue to work unchanged.
"""
from __future__ import annotations

from typing import NotRequired, TypedDict


class ChunkRef(TypedDict):
    """A single resolved chunk, collection-aware.

    Every field except ``char_range`` is present on every return. The
    ``chash`` / ``chunk_hash`` pair is intentional — legacy ``resolve_span``
    callers read ``chunk_hash`` while Phase 2 callers read ``chash``.
    """

    chash: str
    chunk_hash: str              # alias of ``chash`` for resolve_span back-compat
    physical_collection: str
    doc_id: str
    chunk_text: str
    metadata: dict
    char_range: NotRequired[tuple[int, int]]
