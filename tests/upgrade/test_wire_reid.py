# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.2 (nexus-n7u38.15): wire re-id + persisted old→new map.

The incident fix (GH #1408 / Gap-3): legacy-id chunks get their CORRECT
content address computed ON THE WIRE (sha256(chunk_text)[:32] from the
text being carried — no re-embed, no source files, Chroma source
byte-untouched), with every old→new pair persisted to the chash_remap
store BEFORE the target row lands (gate r2 commit ordering, by
construction: the transform persists its map batch, and the .14 seam
runs transforms strictly before target upserts).

Supersedes t3_reidentify.py for migration use (that tool mutates the
source collection in place; equivalence of derivation is pinned here).
GH #1390 stands: correct addresses only — an underivable chunk fails
loudly, never forces a wrong id through.
"""
from __future__ import annotations

import hashlib
import pathlib
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import pytest

from nexus.migration.etl_ports import run_batched_etl
from nexus.migration.wire_reid import (
    ChashRemapStore,
    RemapEntry,
    WireReidError,
    derive_wire_chash,
    make_wire_reid_transform,
)


def _sha32(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _legacy_chunk(cid: str, text: str, *, with_hash: bool = True) -> dict[str, Any]:
    meta: dict[str, Any] = {"k": "v"}
    if with_hash:
        meta["chunk_text_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"id": cid, "document": text, "metadata": meta}


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ChashRemapStore:
    with ChashRemapStore(tmp_path / "chash_remap.db", now_fn=lambda: "t0") as s:
        yield s


# ── derivation ───────────────────────────────────────────────────────────────


def test_derives_from_carried_text() -> None:
    chunk = _legacy_chunk("legacy-16-chars!", "hello world", with_hash=False)
    assert derive_wire_chash(chunk) == _sha32("hello world")


def test_text_derivation_agrees_with_metadata_hash() -> None:
    """When chunk_text_hash metadata is present and consistent, both routes
    agree — and both equal t3_reidentify's meta[chunk_text_hash][:32]
    derivation (the REUSE-OR-SUPERSEDE equivalence pin)."""
    chunk = _legacy_chunk("legacy-16-chars!", "same text")
    assert derive_wire_chash(chunk) == chunk["metadata"]["chunk_text_hash"][:32]


def test_metadata_hash_used_when_document_absent() -> None:
    """Reference-only rows may carry no document; the recorded hash is the
    only identity source."""
    full = hashlib.sha256(b"recorded text").hexdigest()
    chunk = {"id": "legacy", "document": None, "metadata": {"chunk_text_hash": full}}
    assert derive_wire_chash(chunk) == full[:32]


def test_mismatched_metadata_hash_prefers_carried_text() -> None:
    """The target keys on sha256(stored_text)[:32]; the wire carries what
    will be stored, so the TEXT derivation is self-consistent. A stale
    metadata hash is tolerated with the text winning (warn-only)."""
    chunk = _legacy_chunk("legacy", "actual text")
    chunk["metadata"]["chunk_text_hash"] = hashlib.sha256(b"different text").hexdigest()
    assert derive_wire_chash(chunk) == _sha32("actual text")


def test_nul_bearing_text_hashes_raw_matching_ecosystem_identity() -> None:
    """P2 review Medium (nexus-rvfwj class): derivation hashes the RAW text
    including NUL bytes — matching chunk_text_hash and the ecosystem's ids
    — even though the server strips NULs before storing (the pre-existing,
    tolerated stored-text/chash divergence for that population)."""
    text = "before\x00after"
    chunk = _legacy_chunk("legacy", text)
    assert derive_wire_chash(chunk) == _sha32(text)  # raw, not stripped
    assert derive_wire_chash(chunk) == chunk["metadata"]["chunk_text_hash"][:32]
    assert derive_wire_chash(chunk) != _sha32("beforeafter")  # not the stripped form


def test_underivable_chunk_fails_loud() -> None:
    """GH #1390: never force an id through — no text AND no recorded hash
    is a loud failure, not a guess."""
    with pytest.raises(WireReidError, match="cannot derive"):
        derive_wire_chash({"id": "legacy", "document": None, "metadata": {}})


def test_already_conformant_id_maps_to_itself() -> None:
    text = "conformant text"
    chunk = {"id": _sha32(text), "document": text, "metadata": {}}
    assert derive_wire_chash(chunk) == _sha32(text)


# ── ChashRemapStore ──────────────────────────────────────────────────────────


def test_record_batch_and_lookup_roundtrip(store: ChashRemapStore) -> None:
    entries = [
        RemapEntry("", "src-coll", "old-1", "a" * 32, "dst-coll", "rung:test"),
        RemapEntry("", "src-coll", "old-2", "b" * 32, "dst-coll", "rung:test"),
    ]
    store.record_batch(entries)
    assert store.lookup("src-coll", "old-1") == "a" * 32
    assert store.lookup("src-coll", "missing") is None
    assert store.entries_for_collection("src-coll") == {
        "old-1": "a" * 32,
        "old-2": "b" * 32,
    }


def test_record_batch_is_idempotent_upsert(store: ChashRemapStore) -> None:
    entry = RemapEntry("", "src", "old", "a" * 32, "dst", "run-1")
    store.record_batch([entry])
    store.record_batch([RemapEntry("", "src", "old", "a" * 32, "dst", "run-2")])
    assert store.lookup("src", "old") == "a" * 32
    assert len(store.entries_for_collection("src")) == 1


def test_store_is_durable_across_reopen(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "chash_remap.db"
    with ChashRemapStore(path) as s:
        s.record_batch([RemapEntry("", "src", "old", "a" * 32, "dst", "p")])
    with ChashRemapStore(path) as s:
        assert s.lookup("src", "old") == "a" * 32


def test_collapse_entries_share_new_chash(store: ChashRemapStore) -> None:
    """RDR-108 identical-text collapse: many old ids → one new chash is a
    legal many-to-one shape in the map (both source rows stay recoverable
    for rollback; manifest position rows key off the map independently)."""
    store.record_batch([
        RemapEntry("", "src", "old-a", "c" * 32, "dst", "p"),
        RemapEntry("", "src", "old-b", "c" * 32, "dst", "p"),
    ])
    assert store.lookup("src", "old-a") == store.lookup("src", "old-b") == "c" * 32
    assert store.old_ids_for("src", "c" * 32) == frozenset({"old-a", "old-b"})


def test_new_chash_length_enforced(store: ChashRemapStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.record_batch([RemapEntry("", "src", "old", "tooshort", "dst", "p")])


# ── the transform + commit ordering (gate r2) ────────────────────────────────


@dataclass
class OrderSpySink:
    """Records the interleaving of map persists and target upserts."""

    events: list[str] = field(default_factory=list)


def test_transform_rewrites_and_persists_map(store: ChashRemapStore) -> None:
    transform = make_wire_reid_transform(
        store, source_collection="src", target_collection="dst", provenance="run-1"
    )
    batch = [_legacy_chunk("legacy-a", "alpha"), _legacy_chunk("legacy-b", "beta")]
    out = transform(batch)
    assert [c["id"] for c in out] == [_sha32("alpha"), _sha32("beta")]
    assert store.lookup("src", "legacy-a") == _sha32("alpha")
    assert store.lookup("src", "legacy-b") == _sha32("beta")
    # Source dicts are not mutated in place (source stays byte-untouched).
    assert batch[0]["id"] == "legacy-a"


def test_conformant_ids_are_not_map_noise(store: ChashRemapStore) -> None:
    """Already-conformant chunks (old == new) add NO map entries."""
    text = "already fine"
    transform = make_wire_reid_transform(
        store, source_collection="src", target_collection="dst", provenance="p"
    )
    transform([{"id": _sha32(text), "document": text, "metadata": {}}])
    assert store.entries_for_collection("src") == {}


def test_map_batch_commits_strictly_before_target_write(
    tmp_path: pathlib.Path,
) -> None:
    """Gate r2 by construction: when the target write crashes, the map batch
    for that very batch is ALREADY durable — a crash can produce
    map-without-target (safe: resume re-upserts idempotently) but never
    target-without-map (the rollback-miss reproduction)."""
    map_path = tmp_path / "chash_remap.db"

    class CrashingTarget:
        def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
            raise ValueError("crash between map persist and target write")

        def count(self, collection: str) -> int:
            return 0

    class OneBatchSource:
        def iter_batches(self, collection, *, page, include_embeddings=False):
            yield [_legacy_chunk("legacy-a", "alpha")]

        def count(self, collection: str) -> int:
            return 1

    with ChashRemapStore(map_path) as store:
        transform = make_wire_reid_transform(
            store, source_collection="src", target_collection="dst", provenance="p"
        )
        result = run_batched_etl(
            OneBatchSource(), CrashingTarget(),
            source_collection="src", target_collection="dst", page=10,
            transform=transform,
        )
        assert not result.ok  # the write failed...
        assert store.lookup("src", "legacy-a") == _sha32("alpha")  # ...map survived


def test_underivable_chunk_fails_batch_without_partial_map(
    store: ChashRemapStore,
) -> None:
    """All-or-nothing per batch: a batch containing an underivable chunk
    persists NO map entries for that batch (the map never records a batch
    the target will never receive in that shape)."""
    transform = make_wire_reid_transform(
        store, source_collection="src", target_collection="dst", provenance="p"
    )
    batch = [
        _legacy_chunk("legacy-good", "fine"),
        {"id": "legacy-bad", "document": None, "metadata": {}},
    ]
    with pytest.raises(WireReidError):
        transform(batch)
    assert store.entries_for_collection("src") == {}


# ── end to end through the seam ──────────────────────────────────────────────


def test_legacy_collection_lands_conformant_end_to_end(
    tmp_path: pathlib.Path,
) -> None:
    """The acceptance shape: a legacy-id batch (incl. an identical-text
    pair) flows through the seam with the wire transform — conformant ids
    land, the collapse dedupes to one row, the map records every old id,
    zero re-embed (no embeddings requested), source untouched."""

    class Source:
        def iter_batches(self, collection, *, page, include_embeddings=False):
            yield [
                _legacy_chunk("legacy-1", "dup text"),
                _legacy_chunk("legacy-2", "dup text"),
                _legacy_chunk("legacy-3", "unique"),
            ]

        def count(self, collection: str) -> int:
            return 3

    class Target:
        def __init__(self) -> None:
            self.rows: dict[str, str] = {}

        def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
            assert embeddings is None  # zero re-embed, zero passthrough needed
            for cid, doc in zip(ids, documents):
                self.rows[cid] = doc

        def count(self, collection: str) -> int:
            return len(self.rows)

    target = Target()
    with ChashRemapStore(tmp_path / "chash_remap.db") as store:
        transform = make_wire_reid_transform(
            store, source_collection="src", target_collection="dst", provenance="p"
        )
        result = run_batched_etl(
            Source(), target,
            source_collection="src", target_collection="dst", page=10,
            transform=transform,
        )
        assert result.ok
        assert result.source_count == 3
        assert result.written == 2  # collapse
        assert set(target.rows) == {_sha32("dup text"), _sha32("unique")}
        assert store.old_ids_for("src", _sha32("dup text")) == frozenset(
            {"legacy-1", "legacy-2"}
        )
        assert store.lookup("src", "legacy-3") == _sha32("unique")
