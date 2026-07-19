# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P2.1 (nexus-n7u38.14): the ETL source/target Protocol seam.

Generalizes RDR-176/178 batched ETL from concrete Chroma-source /
pgvector-target params to injected ports. Built FROM the existing
primitives (``iter_collection_chunks`` paging, ``_etl_batch_with_breaker``,
the GH #1390 nonconformant-id guard) — reused, not rewritten; the live
``_migrate_one`` path is untouched until P4 demotes it.

Load-bearing design pin: the id guard runs POST-transform. GH #1390
stands — destination constraints are never weakened — but the guard sits
after the wire transform so that .15's re-id (which COMPUTES the correct
chash) passes while an identity run over legacy ids still fails loudly
with the re-index diagnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nexus.migration.etl_ports import (
    ChromaReadSource,
    EtlRunResult,
    EtlSource,
    EtlTarget,
    VectorServiceTarget,
    run_batched_etl,
)


def _chunk(cid: str, doc: str = "text") -> dict[str, Any]:
    return {"id": cid, "document": doc, "metadata": {"k": "v"}}


# RDR-180: the conformant chash width is the full 64-hex sha256 digest
# (names kept as HEX32_* to minimize churn across this file's call sites).
HEX32_A = "a" * 64
HEX32_B = "b" * 64
HEX32_C = "c" * 64


@dataclass
class FixtureSource:
    batches: list[list[dict[str, Any]]]
    count_value: int = 0

    def iter_batches(self, collection: str, *, page: int, include_embeddings: bool = False):
        yield from self.batches

    def count(self, collection: str) -> int:
        return self.count_value or sum(len(b) for b in self.batches)


@dataclass
class FixtureTarget:
    upserts: list[tuple[str, list[str], list[Any], list[Any], Any]] = field(default_factory=list)
    fail_times: int = 0

    def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ValueError("target exploded")  # non-retryable class
        self.upserts.append((collection, list(ids), list(documents), list(metadatas), embeddings))

    def count(self, collection: str) -> int:
        seen: set[str] = set()
        for _, ids, _, _, _ in self.upserts:
            seen.update(ids)
        return len(seen)


def test_fixture_ports_satisfy_protocols() -> None:
    assert isinstance(FixtureSource([]), EtlSource)
    assert isinstance(FixtureTarget(), EtlTarget)


def test_run_migrates_all_batches() -> None:
    source = FixtureSource([[_chunk(HEX32_A), _chunk(HEX32_B)], [_chunk(HEX32_C)]])
    target = FixtureTarget()
    result = run_batched_etl(
        source, target, source_collection="src", target_collection="dst", page=2
    )
    assert result.ok
    assert result.source_count == 3
    assert result.written == 3
    assert [u[0] for u in target.upserts] == ["dst", "dst"]
    assert target.upserts[0][1] == [HEX32_A, HEX32_B]


def test_transform_rewrites_ids_on_the_wire() -> None:
    """The .15 seam: a wire transform turns legacy ids into correct chashes;
    the target receives ONLY post-transform ids."""
    legacy = [_chunk("legacy-id-16ch", doc="alpha"), _chunk("other-legacy-18", doc="beta")]
    remap = {"legacy-id-16ch": HEX32_A, "other-legacy-18": HEX32_B}

    def reid(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**c, "id": remap[c["id"]]} for c in batch]

    target = FixtureTarget()
    result = run_batched_etl(
        FixtureSource([legacy]), target,
        source_collection="src", target_collection="dst", page=10, transform=reid,
    )
    assert result.ok
    assert target.upserts[0][1] == [HEX32_A, HEX32_B]


def test_guard_fires_post_transform_on_legacy_ids() -> None:
    """Identity run over legacy ids fails loudly BEFORE any send of the bad
    batch (GH #1390 language preserved), and nothing is written for it."""
    target = FixtureTarget()
    result = run_batched_etl(
        FixtureSource([[_chunk("legacy-id-16ch")]]), target,
        source_collection="src", target_collection="dst", page=10,
    )
    assert not result.ok
    assert "legacy" in result.reason and "re-index" in result.reason.lower()
    assert target.upserts == []


def test_identical_text_collapse_dedupes_within_batch() -> None:
    """Two source chunks collapsing to one chash (RDR-108) are deduped
    post-transform (ChashRepository.upsertMany precedent) and the verify
    compares against DISTINCT post-transform ids, not raw source count."""
    batch = [_chunk("old-a", doc="same"), _chunk("old-b", doc="same"), _chunk("old-c", doc="uniq")]
    remap = {"old-a": HEX32_A, "old-b": HEX32_A, "old-c": HEX32_B}

    def reid(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**c, "id": remap[c["id"]]} for c in chunks]

    target = FixtureTarget()
    result = run_batched_etl(
        FixtureSource([batch]), target,
        source_collection="src", target_collection="dst", page=10, transform=reid,
    )
    assert result.ok
    assert result.source_count == 3
    assert result.written == 2  # collapsed
    assert target.upserts[0][1] == [HEX32_A, HEX32_B]  # deduped, first kept


def test_target_failure_reports_not_raises() -> None:
    target = FixtureTarget(fail_times=99)
    result = run_batched_etl(
        FixtureSource([[_chunk(HEX32_A)]]), target,
        source_collection="src", target_collection="dst", page=10,
    )
    assert not result.ok
    assert "upsert failed" in result.reason


def test_count_mismatch_fails_verification() -> None:
    @dataclass
    class LyingTarget(FixtureTarget):
        def count(self, collection: str) -> int:
            return 0  # rows vanished

    result = run_batched_etl(
        FixtureSource([[_chunk(HEX32_A)]]), LyingTarget(),
        source_collection="src", target_collection="dst", page=10,
    )
    assert not result.ok
    assert "count mismatch" in result.reason


# ── co-resident targets (nexus-tidtd) ────────────────────────────────────────


@dataclass
class PrePopulatedTarget(FixtureTarget):
    """A target that already holds rows this leg did not write — the
    co-resident shape: independently indexed data, or another era's
    migration, sharing the collection (nexus-tidtd live incident)."""

    baseline: int = 0

    def count(self, collection: str) -> int:
        return self.baseline + super().count(collection)


def test_prepopulated_target_passes_when_all_rows_land() -> None:
    """The non-resume post-write check must accept a target holding MORE
    rows than this leg wrote (nexus-tidtd): a co-resident target is a
    legitimate row-holder beyond the leg's own. The old `!=` assumed the
    leg exclusively owns the target — on the live incident that turned a
    fully-landed 3-row leg into etl_seam_count_mismatch (6712 != 3) and a
    forever-failing `nx upgrade`."""
    target = PrePopulatedTarget(baseline=6712)
    result = run_batched_etl(
        FixtureSource([[_chunk(HEX32_A), _chunk(HEX32_B)], [_chunk(HEX32_C)]]),
        target,
        source_collection="src", target_collection="dst", page=2,
    )
    assert result.ok
    assert result.written == 3


def test_prepopulated_target_cannot_mask_total_loss() -> None:
    """The `<` still fires when the target holds FEWER rows than this leg's
    distinct ids. Acknowledged weakening vs the old `!=`: pre-existing rows
    can mask a partial this-run loss at the seam, and NOTHING downstream
    catches that today (drop_converged_legs still tests count equality —
    the nexus-tidtd root cause — so it re-plans rather than verifying
    membership). Accepted, unmitigated, until the deferred nexus-tidtd
    full-membership convergence design lands PG-side."""
    @dataclass
    class SwallowingTarget(FixtureTarget):
        def upsert_chunks(self, collection, ids, documents, metadatas, *, embeddings=None):
            pass  # accepts the call, keeps nothing

        def count(self, collection: str) -> int:
            return 1  # fewer than the 3 distinct ids this run sent

    result = run_batched_etl(
        FixtureSource([[_chunk(HEX32_A), _chunk(HEX32_B), _chunk(HEX32_C)]]),
        SwallowingTarget(),
        source_collection="src", target_collection="dst", page=10,
    )
    assert not result.ok
    assert "count mismatch" in result.reason


# ── immutable-source discipline (RDR-176) ────────────────────────────────────


def test_source_port_exposes_no_write_surface() -> None:
    """The source adapter wraps the client PRIVATELY and exposes only
    iter_batches/count — a rung cannot reach a write verb through it."""

    class WritableClient:
        def get_collection(self, name):  # pragma: no cover - shape only
            raise AssertionError("not exercised here")

        def upsert(self, *a, **k):
            raise AssertionError("write reached the source client")

        def delete_collection(self, *a, **k):
            raise AssertionError("write reached the source client")

    adapter = ChromaReadSource(WritableClient())
    for verb in ("upsert", "upsert_chunks", "add", "delete", "delete_collection", "update"):
        assert not hasattr(adapter, verb), f"source adapter leaks write verb {verb!r}"
    # And the Protocol itself defines no write members.
    assert not any(hasattr(EtlSource, verb) for verb in ("upsert_chunks", "add", "delete"))


def test_chroma_adapter_delegates_paging_and_count() -> None:
    """Thin-delegation check against a chroma-shaped fake client (the same
    get(limit/offset) paging contract iter_collection_chunks drives)."""

    class FakeCol:
        def __init__(self) -> None:
            self._ids = [HEX32_A, HEX32_B, HEX32_C]

        def get(self, include=None, limit=None, offset=0):
            ids = self._ids[offset : offset + limit]
            return {
                "ids": ids,
                "documents": [f"doc-{i}" for i in ids],
                "metadatas": [{"m": 1} for _ in ids],
            }

        def count(self) -> int:
            return len(self._ids)

    class FakeClient:
        def get_collection(self, name):
            assert name == "src"
            return FakeCol()

    adapter = ChromaReadSource(FakeClient())
    batches = list(adapter.iter_batches("src", page=2))
    assert [len(b) for b in batches] == [2, 1]
    assert batches[0][0] == {"id": HEX32_A, "document": f"doc-{HEX32_A}", "metadata": {"m": 1}}
    assert adapter.count("src") == 3


def test_vector_target_adapter_delegates() -> None:
    class FakeVectorClient:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def upsert_chunks(self, collection, ids, documents, metadatas, embeddings=None):
            self.calls.append((collection, ids, documents, metadatas, embeddings))

        def count(self, collection):
            return 7

    client = FakeVectorClient()
    adapter = VectorServiceTarget(client)
    adapter.upsert_chunks("dst", ["i"], ["d"], [{}], embeddings=None)
    assert client.calls[0][0] == "dst"
    assert adapter.count("dst") == 7
    assert isinstance(adapter, EtlTarget)


def test_progress_hook_reports_per_batch() -> None:
    seen: list[tuple[int, int]] = []
    run_batched_etl(
        FixtureSource([[_chunk(HEX32_A)], [_chunk(HEX32_B)]]),
        FixtureTarget(),
        source_collection="src", target_collection="dst", page=1,
        on_batch=lambda written, total: seen.append((written, total)),
    )
    assert seen == [(1, 1), (2, 2)]
