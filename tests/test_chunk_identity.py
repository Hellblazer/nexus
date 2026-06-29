# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4pvho — canonical chunk natural-ID helper (RDR-108 D1)."""
from __future__ import annotations

import hashlib

from nexus.chunk_identity import CHUNK_ID_LEN, chunk_id, chunk_id_from_hash


def test_chunk_id_is_sha256_first_32() -> None:
    text = "RDR-108 D1 chunk text"
    expected = hashlib.sha256(text.encode()).hexdigest()[:32]
    assert chunk_id(text) == expected
    assert len(chunk_id(text)) == CHUNK_ID_LEN == 32


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
