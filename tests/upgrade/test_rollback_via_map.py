# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.4 (nexus-n7u38.17): rollback consults the persisted map.

The gate r1 Critical: wire re-id rewrites target ids, so
``rollback_collections``' raw source-id equality misses every re-id'd
row and trips its own zero-removed guard. The revision translates each
source id through the persisted chash_remap map (identity fallback for
unmapped/conformant ids) before probing the target — rollback via the
map, never live id equality.
"""
from __future__ import annotations

import pathlib
from typing import Any

import pytest

from nexus.migration.vector_etl import rollback_collections
from nexus.migration.wire_reid import ChashRemapStore, RemapEntry

NEW_A = "a" * 32
NEW_B = "b" * 32


class FakeSourceCol:
    def __init__(self, ids: list[str]) -> None:
        self._ids = ids

    def get(self, include=None, limit=None, offset=0):
        ids = self._ids[offset : offset + limit]
        return {
            "ids": ids,
            "documents": ["t" for _ in ids],
            "metadatas": [{} for _ in ids],
        }

    def count(self) -> int:
        return len(self._ids)


class FakeReadClient:
    def __init__(self, collections: dict[str, list[str]]) -> None:
        self._collections = collections

    def get_collection(self, name: str) -> FakeSourceCol:
        return FakeSourceCol(self._collections[name])

    def list_collections(self):  # pragma: no cover - not used (explicit names)
        return list(self._collections)


class FakeTargetHandle:
    def __init__(self, rows: set[str]) -> None:
        self.rows = rows

    def get(self, ids=None, limit=None):
        return {"ids": [i for i in ids if i in self.rows]}

    def delete(self, ids) -> None:
        for i in ids:
            self.rows.discard(i)


class FakeVectorClient:
    def __init__(self, rows: dict[str, set[str]]) -> None:
        self._rows = rows

    def get_or_create_collection(self, name: str) -> FakeTargetHandle:
        return FakeTargetHandle(self._rows.setdefault(name, set()))

    def count(self, name: str) -> int:
        return len(self._rows.get(name, set()))


@pytest.fixture
def map_store(tmp_path: pathlib.Path) -> ChashRemapStore:
    with ChashRemapStore(tmp_path / "chash_remap.db") as s:
        yield s


def test_rollback_translates_source_ids_through_the_map(
    map_store: ChashRemapStore,
) -> None:
    """The r1 scenario: target holds re-id'd rows; raw equality would find
    nothing (and trip the zero-removed guard) — the map resolves them."""
    read = FakeReadClient({"coll": ["legacy-1", "legacy-2"]})
    vector = FakeVectorClient({"coll": {NEW_A, NEW_B}})
    map_store.record_batch([
        RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p"),
        RemapEntry("", "coll", "legacy-2", NEW_B, "coll", "p"),
    ])
    deleted = rollback_collections(
        read, vector, collections=["coll"], remap_store=map_store
    )
    assert deleted == {"coll": 2}
    assert vector.count("coll") == 0


def test_rollback_without_map_trips_zero_removed_guard(
    map_store: ChashRemapStore,
) -> None:
    """Companion non-vacuity: the SAME re-id'd state WITHOUT the map still
    refuses to report a clean zero — proving the map is what fixes r1,
    not a weakened guard."""
    read = FakeReadClient({"coll": ["legacy-1", "legacy-2"]})
    vector = FakeVectorClient({"coll": {NEW_A, NEW_B}})
    with pytest.raises(RuntimeError, match="no source chash resolved"):
        rollback_collections(read, vector, collections=["coll"])


def test_identity_fallback_for_unmapped_ids(map_store: ChashRemapStore) -> None:
    """Conformant rows never got a map entry — they roll back by identity,
    mixed freely with mapped legacy rows."""
    conformant = "c" * 32
    read = FakeReadClient({"coll": ["legacy-1", conformant]})
    vector = FakeVectorClient({"coll": {NEW_A, conformant}})
    map_store.record_batch([RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p")])
    deleted = rollback_collections(
        read, vector, collections=["coll"], remap_store=map_store
    )
    assert deleted == {"coll": 2}
    assert vector.count("coll") == 0


def test_collapse_deletes_shared_row_once(map_store: ChashRemapStore) -> None:
    """Identical-text collapse: two source ids share one target row — it is
    deleted once, counted once, and the count verification holds."""
    read = FakeReadClient({"coll": ["legacy-1", "legacy-2", "legacy-3"]})
    vector = FakeVectorClient({"coll": {NEW_A, NEW_B}})
    map_store.record_batch([
        RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p"),
        RemapEntry("", "coll", "legacy-2", NEW_A, "coll", "p"),  # collapse sibling
        RemapEntry("", "coll", "legacy-3", NEW_B, "coll", "p"),
    ])
    deleted = rollback_collections(
        read, vector, collections=["coll"], remap_store=map_store
    )
    assert deleted == {"coll": 2}
    assert vector.count("coll") == 0


def test_rollback_is_idempotent_with_map(map_store: ChashRemapStore) -> None:
    read = FakeReadClient({"coll": ["legacy-1"]})
    vector = FakeVectorClient({"coll": {NEW_A}})
    map_store.record_batch([RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p")])
    first = rollback_collections(read, vector, collections=["coll"], remap_store=map_store)
    second = rollback_collections(read, vector, collections=["coll"], remap_store=map_store)
    assert first == {"coll": 1}
    assert second == {"coll": 0}  # empty target: guard does not fire (target_before == 0)


def test_source_is_never_written(map_store: ChashRemapStore) -> None:
    """Immutable source (RDR-176): rollback reads the source manifest only;
    the fake read leg has no write surface to reach."""
    read = FakeReadClient({"coll": ["legacy-1"]})
    assert not hasattr(read, "delete")
    assert not hasattr(FakeSourceCol([]), "delete")  # matches chroma_read's read-only module
    vector = FakeVectorClient({"coll": {NEW_A}})
    map_store.record_batch([RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p")])
    rollback_collections(read, vector, collections=["coll"], remap_store=map_store)
