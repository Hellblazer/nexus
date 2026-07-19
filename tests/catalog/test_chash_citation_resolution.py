# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-180 Item7 (nexus-jxizy.9): the citation grammar is now TRUTHFUL.

``chash:[0-9a-f]{64}`` always advertised the full digest while storage
held the [:32] truncation (G1 — resolution effectively ran at 128 bits).
Post-flip the stored natural id IS the full digest, so a 64-hex citation
resolves end-to-end at 256 bits. These tests prove the resolver path with
seam fakes; the legacy 32-hex resolution rides the chash_alias map (the
engine-side read seam), pinned here at the grammar/derivation level.
"""
from __future__ import annotations

import hashlib

import pytest

from nexus.catalog.catalog_spans import (
    parse_chash_span,
    resolve_chash_globally,
    resolve_span_in_t3,
)

TEXT = "the truthful citation resolves at 256 bits"
FULL = hashlib.sha256(TEXT.encode()).hexdigest()


class _FakeCollection:
    name = "knowledge__cite"

    def get(self, **kwargs):
        where = kwargs.get("where") or {}
        if where.get("chunk_text_hash") == FULL:
            return {
                "ids": [FULL],
                "documents": [TEXT],
                "metadatas": [{"chunk_text_hash": FULL, "title": "t"}],
            }
        return {"ids": [], "documents": [], "metadatas": []}


class _FakeT3:
    def get_collection(self, name):
        assert name == _FakeCollection.name
        return _FakeCollection()

    def list_collections(self):
        return [_FakeCollection()]


class _FakeChashIndex:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def lookup(self, chash: str):
        self.lookups.append(chash)
        if chash == FULL:
            return [{"collection": _FakeCollection.name, "created_at": "2026-07-18T00:00:00Z"}]
        return []


def test_grammar_accepts_the_full_digest_and_nothing_shorter():
    hex_chash, char_range = parse_chash_span(f"chash:{FULL}")
    assert hex_chash == FULL and char_range is None
    hex_chash, char_range = parse_chash_span(f"chash:{FULL}:5-12")
    assert char_range == (5, 12)
    with pytest.raises(ValueError):
        parse_chash_span(f"chash:{FULL[:32]}")  # legacy width is NOT grammar-legal


def test_64_hex_citation_resolves_to_the_stored_chunk():
    """The Test Plan's resolver proof: a chash:<64hex> citation resolves to
    a stored chunk whose natural id IS that digest."""
    ref = resolve_chash_globally(f"chash:{FULL}", _FakeT3(), _FakeChashIndex())
    assert ref is not None
    assert ref["chunk_text"] == TEXT
    assert ref["chunk_hash"] == FULL
    assert ref["physical_collection"] == _FakeCollection.name


def test_char_range_slices_the_resolved_chunk():
    ref = resolve_span_in_t3(f"chash:{FULL}:4-12", _FakeCollection.name, _FakeT3())
    assert ref is not None
    assert ref["chunk_text"] == TEXT[4:12]
    assert ref["char_range"] == (4, 12)


def test_resolution_key_is_the_producer_derivation():
    """G1 closure by construction: the id the producer derives is byte-for-
    byte the value the grammar names — the two lineages agree at 256 bits."""
    from nexus.chunk_identity import CHUNK_ID_LEN, chunk_id

    assert chunk_id(TEXT) == FULL
    assert len(FULL) == CHUNK_ID_LEN == 64
