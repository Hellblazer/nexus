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


def test_grammar_accepts_canonical_and_legacy_reference_widths():
    hex_chash, char_range = parse_chash_span(f"chash:{FULL}")
    assert hex_chash == FULL and char_range is None
    hex_chash, char_range = parse_chash_span(f"chash:{FULL}:5-12")
    assert char_range == (5, 12)
    # RDR-180 Failure Modes: legacy 32-hex REFERENCES parse (they resolve
    # via the chash_alias route) — only truly-malformed widths reject.
    hex_chash, char_range = parse_chash_span(f"chash:{FULL[:32]}")
    assert hex_chash == FULL[:32]
    with pytest.raises(ValueError):
        parse_chash_span("chash:" + "a" * 40)


def test_legacy_32_hex_citation_resolves_via_the_alias_route():
    """The Failure-Modes promise end-to-end (critic-180-cohort finding 1):
    a 32-hex reference from an old bead comment / T2 memory resolves —
    the engine lookup alias-chains and echoes the canonical, the client
    rewrites and fetches by the canonical identity."""
    class _AliasAwareIndex(_FakeChashIndex):
        def lookup(self, chash: str):
            self.lookups.append(chash)
            if chash == FULL[:32]:  # legacy ref: engine echoes canonical
                return [{"collection": _FakeCollection.name,
                         "created_at": "2026-07-18T00:00:00Z", "chash": FULL}]
            if chash == FULL:
                return [{"collection": _FakeCollection.name,
                         "created_at": "2026-07-18T00:00:00Z", "chash": FULL}]
            return []

    ref = resolve_chash_globally(f"chash:{FULL[:32]}", _FakeT3(), _AliasAwareIndex())
    assert ref is not None
    assert ref["chunk_text"] == TEXT
    assert ref["chunk_hash"] == FULL  # rewritten to the canonical identity


def test_unmapped_legacy_reference_is_dangling_not_an_error():
    ref = resolve_chash_globally(f"chash:{'0' * 32}", _FakeT3(), _FakeChashIndex())
    assert ref is None


class _ServiceShapedT3(_FakeT3):
    """The service-mode client shape: ``list_collections()`` returns DICTS
    (HttpVectorClient), not objects with ``.name``. --guided gate run 3
    catch (nexus-jxizy.10.10): the object-only read crashed every
    service-mode fallback scan and made the chash-index self-heal a
    permanent skip."""

    def list_collections(self):
        return [{"name": _FakeCollection.name, "count": 1}]


def test_resolver_survives_dict_shaped_list_collections_service_mode():
    # T2 path (self-heal consults list_collections): must resolve, not
    # skip-with-warning, when the client returns dict rows.
    ref = resolve_chash_globally(f"chash:{FULL}", _ServiceShapedT3(), _FakeChashIndex())
    assert ref is not None
    assert ref["chunk_text"] == TEXT


def test_fallback_scan_survives_dict_shaped_list_collections_service_mode():
    # Fallback path (empty chash index): the scan must enumerate dict rows
    # and find the chunk, not crash to a silent None.
    class _EmptyIndex(_FakeChashIndex):
        def lookup(self, chash: str):
            return []

    ref = resolve_chash_globally(f"chash:{FULL}", _ServiceShapedT3(), _EmptyIndex())
    assert ref is not None
    assert ref["chunk_text"] == TEXT


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


def test_resolver_never_deletes_rows_the_selfheal_is_retired():
    """RDR-187 (nexus-piwya.4): the delete_stale self-heal is DELETED
    OUTRIGHT. The lookup is chunk-backed truth engine-side (nexus-piwya.3),
    so a returned row cannot be stale — and the resolver must never again
    hold delete authority over the store (the nexus-8g79.3 purge-on-
    transient class died with it). This pin drives the exact pre-.4 heal
    scenario — a looked-up collection absent from T3 — and asserts the
    resolver falls through WITHOUT calling delete_stale (which the fake
    makes fatal, so a resurrected self-heal fails loudly here)."""
    class _GhostCollectionIndex(_FakeChashIndex):
        def lookup(self, chash: str):
            self.lookups.append(chash)
            if chash == FULL:
                return [{"collection": "ghost__gone", "created_at": "2026-07-18T00:00:00Z"}]
            return []

        def delete_stale(self, **kwargs):
            raise AssertionError(
                "resolve_chash_globally must NOT self-heal: the delete_stale "
                "leg was retired by RDR-187 (nexus-piwya.4)"
            )

    class _GhostAwareT3(_FakeT3):
        def get_collection(self, name):
            if name == "ghost__gone":
                raise KeyError("collection does not exist")
            return super().get_collection(name)

    # The ghost row fails per-candidate resolution; the T3 fallback scan
    # still finds the real chunk. No deletion anywhere on the way.
    ref = resolve_chash_globally(f"chash:{FULL}", _GhostAwareT3(), _GhostCollectionIndex())
    assert ref is not None
    assert ref["chunk_text"] == TEXT
