# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Canonical chunk natural-ID derivation (RDR-108 D1, inverted by RDR-180).

A chash IS the 32-byte SHA-256 digest of the chunk text — the FULL digest
(RDR-180 Item1/Item4, bead nexus-jxizy.3). The T3/pgvector record id a chunk
collapses to is its 64-lowercase-hex interchange rendering; storage keys on
the 32 raw bytes engine-side (``bytea``, ``octet_length=32``). Identical
chunk text in the same collection collapses to one record; the catalog
``document_chunks`` manifest preserves position via ``(doc_id, position)``.

HISTORY: RDR-108 D1 chose ``sha256(text)[:32]`` — 32 hex chars = HALF the
digest — as a compact Chroma-era record id; RDR-180 retires that truncation
(it silently downgraded content addressing to 128 bits while the citation
grammar advertised 256). ``chunk_id_from_hash`` is now the identity
function, retained so single-digest-computation call sites keep one shape.

This is the single definition of that derivation (nexus-4pvho): every
indexer write path imports ONE source of truth. The boundary discipline
mirrors the engine's ``Chash`` type: :func:`to_storage_bytes` /
:func:`to_citation_hex` are the ONLY encode/decode seam client-side.
"""
from __future__ import annotations

import hashlib

#: Length of the chunk natural ID in its hex interchange form (64 chars =
#: the full SHA-256). The pre-RDR-180 value was 32 (the [:32] truncation).
CHUNK_ID_LEN: int = 64

#: Storage width in BYTES (the engine's ``CHECK (octet_length(chash) = 32)``).
CHUNK_ID_BYTES: int = 32


def chunk_id(text: str) -> str:
    """The canonical chunk natural ID for *text*: the FULL sha256 hexdigest."""
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_id_from_hash(full_hexdigest: str) -> str:
    """Derive the natural ID from an already-computed full sha256 hexdigest.

    RDR-180: the identity function (the [:32] truncation is retired — the
    full digest IS the id). Kept so sites that also need the digest for
    other metadata compute it exactly once and share one derivation shape.
    """
    return full_hexdigest


def to_citation_hex(value: str | bytes) -> str:
    """The 64-lowercase-hex interchange form (wire values, ``chash:<hex>``
    citations). Accepts either form; the single decode/encode seam
    client-side (mirrors the engine ``Chash.toHex``)."""
    if isinstance(value, bytes):
        if len(value) != CHUNK_ID_BYTES:
            raise ValueError(
                f"chash storage form must be exactly {CHUNK_ID_BYTES} bytes, "
                f"got {len(value)}"
            )
        return value.hex()
    _require_canonical_hex(value)
    return value


def to_storage_bytes(value: str | bytes) -> bytes:
    """The 32-byte storage form (mirrors the engine ``Chash.toBytes``)."""
    if isinstance(value, bytes):
        if len(value) != CHUNK_ID_BYTES:
            raise ValueError(
                f"chash storage form must be exactly {CHUNK_ID_BYTES} bytes, "
                f"got {len(value)}"
            )
        return value
    _require_canonical_hex(value)
    return bytes.fromhex(value)


def _require_canonical_hex(value: str) -> None:
    if len(value) != CHUNK_ID_LEN:
        hint = (
            " — a legacy 32-hex (pre-RDR-180 half-digest) id? resolve it "
            "through the chash_alias map first; never truncate or pad"
            if len(value) == 32 else ""
        )
        raise ValueError(
            f"chash interchange form must be {CHUNK_ID_LEN} lowercase hex "
            f"chars, got {len(value)}{hint}"
        )
    if any(c not in "0123456789abcdef" for c in value):
        raise ValueError(
            f"chash interchange form must be lowercase hex, got {value!r}"
        )
