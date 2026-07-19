# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4pvho — canonical chunk natural-ID helper (RDR-108 D1,
inverted to the FULL digest by RDR-180 / nexus-jxizy.3)."""
from __future__ import annotations

import hashlib

from nexus.chunk_identity import CHUNK_ID_LEN, chunk_id, chunk_id_from_hash


def test_chunk_id_is_the_full_sha256() -> None:
    # RDR-180: the [:32] truncation is retired — the full digest IS the id.
    text = "RDR-108 D1 chunk text"
    expected = hashlib.sha256(text.encode()).hexdigest()
    assert chunk_id(text) == expected
    assert len(chunk_id(text)) == CHUNK_ID_LEN == 64


def test_boundary_helpers_round_trip() -> None:
    from nexus.chunk_identity import to_citation_hex, to_storage_bytes

    full = hashlib.sha256(b"x").hexdigest()
    raw = to_storage_bytes(full)
    assert len(raw) == 32
    assert to_citation_hex(raw) == full
    import pytest
    with pytest.raises(ValueError, match="chash_alias"):
        to_storage_bytes(full[:32])  # legacy width names the resolver path


def test_chunk_id_from_hash_matches_chunk_id() -> None:
    # The two derivations MUST be byte-identical — the indexer sites that keep
    # the full hash for other metadata slice it via chunk_id_from_hash.
    for text in ("", "a", "unicode: café", "x" * 5000):
        full = hashlib.sha256(text.encode()).hexdigest()
        assert chunk_id_from_hash(full) == chunk_id(text)


def test_identical_text_collapses_to_one_id() -> None:
    assert chunk_id("dup") == chunk_id("dup")
    assert chunk_id("a") != chunk_id("b")


def test_helper_no_longer_on_http_vector_client() -> None:
    # nexus-4pvho re-homed the helper off the network client (it was a
    # zero-caller staticmethod). Guard against re-homing it back.
    from nexus.db.http_vector_client import HttpVectorClient

    assert not hasattr(HttpVectorClient, "chunk_id"), (
        "chunk_id must live in nexus.chunk_identity, not on the network client"
    )
