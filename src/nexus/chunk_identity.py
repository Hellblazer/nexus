# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Canonical chunk natural-ID derivation (RDR-108 D1).

The T3 chunk natural ID — the Chroma/pgvector record id a chunk collapses to —
is ``sha256(chunk_text)[:32]`` (RDR-108 D1, bead nexus-kmb6). Identical chunk
text in the same collection collapses to one record; the catalog
``document_chunks`` manifest preserves position via ``(doc_id, position)``.

This is the single definition of that derivation. It lives in a neutral module
(not on the network client ``HttpVectorClient``, where it was a zero-caller
staticmethod — nexus-4pvho) so every indexer write path imports ONE source of
truth instead of re-spelling ``sha256(...).hexdigest()[:32]`` inline.
"""
from __future__ import annotations

import hashlib

#: Length of the chunk natural ID (hex chars) sliced from the full sha256 digest.
CHUNK_ID_LEN: int = 32


def chunk_id(text: str) -> str:
    """The canonical chunk natural ID for *text*: ``sha256(text)[:32]``."""
    return hashlib.sha256(text.encode()).hexdigest()[:CHUNK_ID_LEN]


def chunk_id_from_hash(full_hexdigest: str) -> str:
    """Derive the natural ID from an already-computed full sha256 hexdigest.

    Byte-identical to ``chunk_id(text)`` when ``full_hexdigest ==
    sha256(text).hexdigest()``. Use this at sites that ALSO need the full
    64-char hash for other metadata, so the digest is computed exactly once.
    """
    return full_hexdigest[:CHUNK_ID_LEN]
