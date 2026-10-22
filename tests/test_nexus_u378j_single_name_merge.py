# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 4 Step 2 (nexus-u378j): a single-name read that now spans a
bge and a second-family sibling (P4.1) orders the merged hits by the calibrated
rules of Technical Design 12 (P1.5), not by raw distance.

The two halves are tested on their own elsewhere (``test_nexus_50uw6_read_resolver``
for the resolver, ``test_hf29s_cross_family_ranking`` for the merge). This file
joins them: the target comes from the resolver over cached catalog rows, and the
rows go through the merge the scoped tools use.
"""
from __future__ import annotations

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.mcp import core as mcp_core

BGE = "bge-base-en-v15-768"
OTHER_FAMILY = "voyage-model-b"      # any non-local model: the second family
BGE_ART = f"knowledge__art__{BGE}__v1"
OTHER_ART = f"knowledge__art__{OTHER_FAMILY}__v1"


def _row(name: str, model: str) -> dict:
    return {
        "name": name, "content_type": "knowledge", "owner_id": "art",
        "embedding_model": model, "lifecycle_state": "live", "count": 3,
    }


class _RowT3:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def collection_exists(self, name: str) -> bool:
        return any(r["name"] == name for r in self._rows)

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self._rows]


@pytest.fixture
def siblings(monkeypatch):
    """A bge and a second-family collection of one corpus, registered in the
    process collections cache the resolver and the merge both read."""
    rows = [_row(BGE_ART, BGE), _row(OTHER_ART, OTHER_FAMILY)]
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    mcp_infra.prime_collections_cache(rows)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", lambda **k: None)
    yield _RowT3(rows)
    mcp_infra.invalidate_collections_cache()


def test_a_shorthand_corpus_over_a_bge_and_a_second_family_sibling_merges_in_calibrated_order(siblings):
    target = mcp_core._resolve_corpus_target("knowledge__art", siblings)
    assert sorted(target) == sorted([BGE_ART, OTHER_ART])

    rows = {
        BGE_ART: [
            {"id": "b1", "collection": BGE_ART, "distance": 0.30},   # x1.37 = 0.411
            {"id": "b2", "collection": BGE_ART, "distance": 0.50},   # x1.37 = 0.685
        ],
        OTHER_ART: [
            {"id": "v1", "collection": OTHER_ART, "distance": 0.63},  # x0.69 = 0.436
            {"id": "v2", "collection": OTHER_ART, "distance": 0.70},  # x0.69 = 0.485
        ],
    }
    merged = mcp_core._grouped_combined_query(target, lambda group: list(rows[group[0]]))
    # Raw-distance order would be b1, b2, v1, v2: the bge rows would sit ahead of every other-family row.
    assert [r["id"] for r in merged] == ["b1", "v1", "v2", "b2"]
    assert {r["id"]: r["distance"] for r in merged} == {"b1": 0.30, "b2": 0.50, "v1": 0.63, "v2": 0.70}


def test_the_fully_qualified_name_reads_one_sibling(siblings):
    """An explicit name is still one collection: nothing is pulled in from the other family."""
    assert mcp_core._resolve_corpus_target(BGE_ART, siblings) == [BGE_ART]
