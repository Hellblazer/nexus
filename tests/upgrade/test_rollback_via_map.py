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


def test_cross_model_rollback_deletes_from_recorded_target(
    map_store: ChashRemapStore,
) -> None:
    """P2 critique Critical (audit C2 realized): a cross-model leg wrote to
    a RENAMED target collection — rollback must delete from the RECORDED
    target_collection per map row, and the guards must run over the summed
    involved-target counts (the source-named collection reading 0 must not
    defuse the zero-removed guard into a silent clean report)."""
    src = "knowledge__notes__all-minilm-l6-v2__v1"
    dst = "knowledge__notes__voyage-context-3__v1"
    read = FakeReadClient({src: ["legacy-1", "legacy-2"]})
    vector = FakeVectorClient({dst: {NEW_A, NEW_B}})  # rows live under the RENAMED name
    map_store.record_batch([
        RemapEntry("", src, "legacy-1", NEW_A, dst, "p"),
        RemapEntry("", src, "legacy-2", NEW_B, dst, "p"),
    ])
    deleted = rollback_collections(read, vector, collections=[src], remap_store=map_store)
    assert deleted == {src: 2}
    assert vector.count(dst) == 0
    assert vector.count(src) == 0  # nothing ever created/left under the source name


def test_cross_model_conformant_ids_roll_back_via_target_names(
    map_store: ChashRemapStore,
) -> None:
    """Conformant rows in a cross-model leg carry NO map entry but still
    landed under the renamed target — target_names supplies their home
    (the verify_fill_collections parameter shape)."""
    src = "knowledge__notes__all-minilm-l6-v2__v1"
    dst = "knowledge__notes__voyage-context-3__v1"
    conformant = "c" * 32
    read = FakeReadClient({src: ["legacy-1", conformant]})
    vector = FakeVectorClient({dst: {NEW_A, conformant}})
    map_store.record_batch([RemapEntry("", src, "legacy-1", NEW_A, dst, "p")])
    deleted = rollback_collections(
        read, vector, collections=[src], remap_store=map_store,
        target_names={src: dst},
    )
    assert deleted == {src: 2}
    assert vector.count(dst) == 0


def test_source_is_never_written(map_store: ChashRemapStore) -> None:
    """Immutable source (RDR-176): rollback reads the source manifest only;
    the fake read leg has no write surface to reach."""
    read = FakeReadClient({"coll": ["legacy-1"]})
    assert not hasattr(read, "delete")
    assert not hasattr(FakeSourceCol([]), "delete")  # matches chroma_read's read-only module
    vector = FakeVectorClient({"coll": {NEW_A}})
    map_store.record_batch([RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p")])
    rollback_collections(read, vector, collections=["coll"], remap_store=map_store)


# ── nexus-146xx.8: whole-leg map-clear ordering (RDR-186 D2) ─────────────────
#
# The leg's chash_remap rows are cleared ONLY after the WHOLE function's
# verification completes — strictly after every collection's target_after
# check, never eagerly, never per-page/per-collection. The map is
# load-bearing for rollback's OWN retry idempotency (gate Critical): a
# mid-rollback crash must find the translation table intact. Before the
# clear, cascade_revert points the local stores back at the old ids — a
# leg is not "rolled back" while local stores still reference its new
# chashes.


class _OrderSpy:
    """Records the interleaving of deletes, reverts, and clears."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def revert_fn(self, leg_entries):
        from nexus.migration.remap_cascade import RevertReport, StoreCascadeResult

        self.events.append(("revert", tuple(sorted(leg_entries))))
        return RevertReport(stores=[StoreCascadeResult("spy-store", True)])

    def clear_fn(self, source: str, target: str) -> int:
        self.events.append(("clear", source, target))
        return len([e for e in self.events if e[0] == "clear"])


class _SpyingVectorClient(FakeVectorClient):
    """FakeVectorClient that also records deletes into the shared spy."""

    def __init__(self, rows, spy: _OrderSpy) -> None:
        super().__init__(rows)
        self._spy = spy

    def get_or_create_collection(self, name: str):
        handle = super().get_or_create_collection(name)
        spy = self._spy
        orig_delete = handle.delete

        def spying_delete(ids):
            spy.events.append(("delete", name, tuple(sorted(ids))))
            orig_delete(ids)

        handle.delete = spying_delete  # type: ignore[method-assign]
        return handle


def test_map_clear_fires_only_after_every_collection_verified(
    map_store: ChashRemapStore,
) -> None:
    """Happy path over TWO collections: every delete (and its verification)
    precedes every revert, and every revert precedes every clear — the
    whole-function scope, not per-collection. Falsify by moving the
    revert/clear block inside the per-collection loop: the interleaving
    assertion fails (collection B's delete would follow collection A's
    clear)."""
    spy = _OrderSpy()
    read = FakeReadClient({"collA": ["legacy-1"], "collB": ["legacy-2"]})
    vector = _SpyingVectorClient({"collA": {NEW_A}, "collB": {NEW_B}}, spy)
    map_store.record_batch([
        RemapEntry("", "collA", "legacy-1", NEW_A, "collA", "p"),
        RemapEntry("", "collB", "legacy-2", NEW_B, "collB", "p"),
    ])

    deleted = rollback_collections(
        read, vector, collections=["collA", "collB"], remap_store=map_store,
        cascade_revert_fn=spy.revert_fn, map_clear_fn=spy.clear_fn,
    )

    assert deleted == {"collA": 1, "collB": 1}
    kinds = [e[0] for e in spy.events]
    assert kinds == ["delete", "delete", "revert", "clear", "revert", "clear"], (
        "whole-function ordering: ALL deletes/verifications first, then per-leg "
        f"revert-then-clear — got {kinds}"
    )
    assert ("clear", "collA", "collA") in spy.events
    assert ("clear", "collB", "collB") in spy.events


def test_crash_mid_rollback_leaves_map_untouched(
    map_store: ChashRemapStore,
) -> None:
    """The gate Critical: a crash before the whole-function verification
    completes (collection B's target refuses deletes → count-verification
    raises) must leave the map INTACT — no revert, no clear — so a retry
    still translates every id. Mutation per the bead: clear per-collection
    and this test fails (collA's rows would already be gone)."""
    spy = _OrderSpy()

    class RefusingVectorClient(FakeVectorClient):
        def get_or_create_collection(self, name: str):
            handle = super().get_or_create_collection(name)
            if name == "collB":
                handle.delete = lambda ids: None  # type: ignore[method-assign] — swallowed delete: count never moves
            return handle

    read = FakeReadClient({"collA": ["legacy-1"], "collB": ["legacy-2"]})
    vector = RefusingVectorClient({"collA": {NEW_A}, "collB": {NEW_B}})
    map_store.record_batch([
        RemapEntry("", "collA", "legacy-1", NEW_A, "collA", "p"),
        RemapEntry("", "collB", "legacy-2", NEW_B, "collB", "p"),
    ])

    with pytest.raises(RuntimeError, match="swallowed|count went"):
        rollback_collections(
            read, vector, collections=["collA", "collB"], remap_store=map_store,
            cascade_revert_fn=spy.revert_fn, map_clear_fn=spy.clear_fn,
        )

    assert spy.events == [], (
        "a failed rollback must neither revert local stores nor clear the map "
        f"— retry idempotency depends on it; got {spy.events}"
    )
    assert map_store.entries_with_targets("collA") != {}, "map intact for retry"


def test_clear_targets_the_recorded_leg_pairs(map_store: ChashRemapStore) -> None:
    """Cross-model: the clear is issued per RECORDED (source, target) pair —
    the co-residency-safe scoped clear the engine endpoint requires."""
    spy = _OrderSpy()
    read = FakeReadClient({"coll": ["legacy-1", "legacy-2"]})
    vector = FakeVectorClient({"coll__voyage": {NEW_A, NEW_B}})
    map_store.record_batch([
        RemapEntry("", "coll", "legacy-1", NEW_A, "coll__voyage", "p"),
        RemapEntry("", "coll", "legacy-2", NEW_B, "coll__voyage", "p"),
    ])

    rollback_collections(
        read, vector, collections=["coll"], remap_store=map_store,
        target_names={"coll": "coll__voyage"},
        cascade_revert_fn=spy.revert_fn, map_clear_fn=spy.clear_fn,
    )

    clears = [e for e in spy.events if e[0] == "clear"]
    assert clears == [("clear", "coll", "coll__voyage")]


def test_no_map_entries_means_no_revert_no_clear(map_store: ChashRemapStore) -> None:
    """A conformant-id collection has no leg claims — nothing to revert,
    nothing to clear (the fns must not be called with empty legs)."""
    spy = _OrderSpy()
    read = FakeReadClient({"coll": [NEW_A]})
    vector = FakeVectorClient({"coll": {NEW_A}})

    rollback_collections(
        read, vector, collections=["coll"], remap_store=map_store,
        cascade_revert_fn=spy.revert_fn, map_clear_fn=spy.clear_fn,
    )

    assert [e for e in spy.events if e[0] in ("revert", "clear")] == []


def test_partial_revert_failure_blocks_the_map_clear(
    map_store: ChashRemapStore,
) -> None:
    """The reviewer-146xx-8 Critical, pinned: a revert that fails on ANY
    store must abort BEFORE the map clear — clearing anyway would erase the
    only signal (the map) that can ever detect the unreverted store. The
    failure is loud and the whole rollback stays retryable."""
    from nexus.migration.remap_cascade import RevertReport, StoreCascadeResult

    events: list[tuple] = []

    def failing_revert(leg_entries):
        events.append(("revert", tuple(sorted(leg_entries))))
        return RevertReport(stores=[
            StoreCascadeResult("document_chunks", True, rewritten=1),
            StoreCascadeResult("document_aspects", False, reason="database is locked"),
        ])

    def must_not_clear(source, target):
        events.append(("clear", source, target))
        raise AssertionError("map clear must never run after a partial revert")

    read = FakeReadClient({"coll": ["legacy-1"]})
    vector = FakeVectorClient({"coll": {NEW_A}})
    map_store.record_batch([RemapEntry("", "coll", "legacy-1", NEW_A, "coll", "p")])

    with pytest.raises(RuntimeError, match="revert failed.*NOT[\\s\\S]*cleared|document_aspects"):
        rollback_collections(
            read, vector, collections=["coll"], remap_store=map_store,
            cascade_revert_fn=failing_revert, map_clear_fn=must_not_clear,
        )

    assert ("revert", ("legacy-1",)) in events
    assert not [e for e in events if e[0] == "clear"]
    assert map_store.entries_with_targets("coll") != {}, (
        "the map survives a failed revert — the unreverted store stays detectable"
    )
