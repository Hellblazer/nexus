# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 wave-2 verify-fill P3a (nexus-s3dd4.4): the inner identity-diff +
fill loop for EXISTING-surface tables (chash_index, catalog owners/
collections/document_chunks — the 2026-07-01 incident-recovery slice).

``fill_missing`` diffs a source row set against a target's identity
surface (a flat key set, e.g. registered chashes / owner tumbler_prefixes /
collection names) and re-sends ONLY the missing rows through the existing
idempotent importBatch route, wrapped in the shared ``EtlCircuitBreaker``.

``fill_missing_document_chunks`` handles the one table whose ONLY identity
surface is chash-level (a set of chashes present in a physical_collection)
while the real conflict key is position-bearing ``(doc_id, position)``.
RDR-108 D1: identical chunk text collapses to ONE chash — so multiple
``(doc_id, position)`` rows can point at the same chash. A chash present in
the collection's target set does NOT prove any specific ``(doc_id,
position)`` row is present; only a chash ABSENT from the set proves the
row is definitely missing. Ambiguous ("candidate") rows are resolved via a
precise per-document manifest fetch, called ONLY for docs that have at
least one candidate row.
"""
from __future__ import annotations

from typing import Any

from nexus.migration import verify_fill as vf
from nexus.retry import EtlCircuitBreaker


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeIdentitySource:
    """A :class:`vf.IdentitySource` returning a canned present-set (or
    ``None`` to simulate an unreachable surface -> indeterminate)."""

    def __init__(self, present: set[str] | None):
        self._present = present
        self.call_count = 0

    def present(self) -> set[str] | None:
        self.call_count += 1
        return self._present


class _SpyImportFn:
    """Records every batch passed to it; returns a canned response."""

    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def __call__(self, batch: list[dict[str, Any]]) -> dict[str, Any]:
        self.batches.append(list(batch))
        return {"imported": len(batch)}

    @property
    def total_rows_sent(self) -> int:
        return sum(len(b) for b in self.batches)

    @property
    def call_count(self) -> int:
        return len(self.batches)


# ── fill_missing: basic hole-of-size-K-in-table-of-size-N ────────────────────


class TestFillMissingBasic:
    def test_fills_only_missing_rows_not_all(self) -> None:
        # table of size N=10, target already has 7 -> hole of K=3
        source_rows = [{"id": f"row-{i}"} for i in range(10)]
        present = {f"row-{i}" for i in range(7)}  # rows 0..6 present; 7,8,9 missing
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: r["id"],
            identity_source=_FakeIdentitySource(present),
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result == {
            "source_count": 10,
            "target_count": 7,
            "missing": 3,
            "filled": 3,
            "status": "filled",
        }
        # the spy must have received EXACTLY the 3 missing rows, not the 10 source rows
        assert spy.total_rows_sent == 3
        assert {r["id"] for b in spy.batches for r in b} == {"row-7", "row-8", "row-9"}

    def test_no_missing_rows_is_parity_and_no_import_call(self) -> None:
        source_rows = [{"id": f"row-{i}"} for i in range(5)]
        present = {f"row-{i}" for i in range(5)}
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: r["id"],
            identity_source=_FakeIdentitySource(present),
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result == {
            "source_count": 5,
            "target_count": 5,
            "missing": 0,
            "filled": 0,
            "status": "parity",
        }
        assert spy.call_count == 0

    def test_empty_source_rows_is_parity_no_probe_needed(self) -> None:
        src = _FakeIdentitySource({"whatever"})
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=[],
            key_fn=lambda r: r["id"],
            identity_source=src,
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result == {
            "source_count": 0,
            "target_count": None,
            "missing": 0,
            "filled": 0,
            "status": "parity",
        }
        # an empty source has nothing to diff -- never probe the target
        assert src.call_count == 0
        assert spy.call_count == 0

    def test_unreachable_identity_source_is_indeterminate_no_blind_fill(self) -> None:
        source_rows = [{"id": f"row-{i}"} for i in range(10)]
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: r["id"],
            identity_source=_FakeIdentitySource(None),
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result == {
            "source_count": 10,
            "target_count": None,
            "missing": None,
            "filled": 0,
            "status": "indeterminate",
        }
        # never a blind re-send when we cannot compute the hole
        assert spy.call_count == 0


# ── fill_missing: batching ────────────────────────────────────────────────────


class TestFillMissingBatching:
    def test_hole_larger_than_batch_size_splits_into_multiple_calls(self) -> None:
        source_rows = [{"id": f"row-{i}"} for i in range(25)]
        present: set[str] = set()  # everything missing -> hole of 25
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: r["id"],
            identity_source=_FakeIdentitySource(present),
            import_fn=spy,
            batch_size=10,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 25
        assert result["filled"] == 25
        assert spy.total_rows_sent == 25
        # ceil(25/10) = 3 batches, each <= 10
        assert spy.call_count == 3
        assert all(len(b) <= 10 for b in spy.batches)

    def test_identity_source_probed_exactly_once(self) -> None:
        # the present-set is fetched once and reused for the whole diff, not
        # re-queried per row.
        source_rows = [{"id": f"row-{i}"} for i in range(4)]
        src = _FakeIdentitySource({"row-0"})
        vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: r["id"],
            identity_source=src,
            import_fn=_SpyImportFn(),
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )
        assert src.call_count == 1


# ── fill_missing_document_chunks: position-bearing manifest ──────────────────


class _FakeIdentitySourceFactory:
    """Maps collection name -> canned present-chash-set (or None)."""

    def __init__(self, by_collection: dict[str, set[str] | None]):
        self._by_collection = by_collection
        self.calls: list[str] = []

    def __call__(self, collection: str) -> _FakeIdentitySource:
        self.calls.append(collection)
        return _FakeIdentitySource(self._by_collection.get(collection))


class _FakeManifestSource:
    """Maps doc_id -> canned target manifest rows (or None -> unreachable)."""

    def __init__(self, by_doc: dict[str, list[dict[str, Any]] | None]):
        self._by_doc = by_doc
        self.calls: list[str] = []

    def manifest_for(self, doc_id: str) -> list[dict[str, Any]] | None:
        self.calls.append(doc_id)
        return self._by_doc.get(doc_id)


class _SpyChunkImportFn:
    """Records (doc_id, batch) pairs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def __call__(self, doc_id: str, batch: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append((doc_id, list(batch)))
        return {"imported": len(batch)}

    @property
    def total_rows_sent(self) -> int:
        return sum(len(b) for _, b in self.calls)


class TestFillMissingDocumentChunks:
    def test_chash_absent_from_collection_is_definite_missing_no_manifest_fetch(self) -> None:
        # doc-A has one row whose chash is nowhere in the target collection ->
        # definitely missing; no manifest_for call is needed to prove it.
        source_rows = [
            {"doc_id": "doc-A", "position": 0, "chash": "a" * 32},
        ]
        collection_for_doc = {"doc-A": "coll-1"}
        identity_factory = _FakeIdentitySourceFactory({"coll-1": set()})  # nothing present
        manifest_source = _FakeManifestSource({})
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["source_count"] == 1
        assert result["missing"] == 1
        assert result["filled"] == 1
        assert result["indeterminate"] == 0
        assert spy.total_rows_sent == 1
        assert spy.calls == [("doc-A", [{"doc_id": "doc-A", "position": 0, "chash": "a" * 32}])]
        # the definite-missing row never needed a precise manifest fetch
        assert manifest_source.calls == []

    def test_shared_chash_across_docs_does_not_mask_a_missing_position(self) -> None:
        # RDR-108 D1: doc-A/position-2 and doc-B/position-5 share one chash.
        # doc-A's row already landed (so the chash IS present in the
        # collection's target set) but doc-B's row has NOT landed. A naive
        # chash-set-only diff would wrongly conclude doc-B's row is present
        # too (false negative) -- this must NOT happen.
        shared_chash = "b" * 32
        source_rows = [
            {"doc_id": "doc-A", "position": 2, "chash": shared_chash},
            {"doc_id": "doc-B", "position": 5, "chash": shared_chash},
        ]
        collection_for_doc = {"doc-A": "coll-1", "doc-B": "coll-1"}
        # collection-level pre-filter: the chash IS present (landed via doc-A)
        identity_factory = _FakeIdentitySourceFactory({"coll-1": {shared_chash}})
        # precise per-doc manifest: doc-A already has position 2; doc-B has nothing
        manifest_source = _FakeManifestSource({
            "doc-A": [{"position": 2, "chash": shared_chash}],
            "doc-B": [],
        })
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["source_count"] == 2
        assert result["missing"] == 1  # only doc-B/position-5
        assert result["filled"] == 1
        assert spy.total_rows_sent == 1
        assert spy.calls == [("doc-B", [{"doc_id": "doc-B", "position": 5, "chash": shared_chash}])]
        # both docs were ambiguous candidates (chash present in the collection
        # set) so both required a precise manifest fetch
        assert set(manifest_source.calls) == {"doc-A", "doc-B"}

    def test_docs_with_all_chashes_present_never_trigger_manifest_fetch(self) -> None:
        source_rows = [{"doc_id": "doc-A", "position": 0, "chash": "c" * 32}]
        collection_for_doc = {"doc-A": "coll-1"}
        identity_factory = _FakeIdentitySourceFactory({"coll-1": set()})  # definite-missing path
        manifest_source = _FakeManifestSource({})
        spy = _SpyChunkImportFn()

        vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )
        assert manifest_source.calls == []

    def test_unreachable_collection_prefilter_treats_rows_as_candidates(self) -> None:
        # pre-filter unreachable for coll-1 -> every row in that collection
        # must fall back to the precise per-doc manifest check, never a
        # silent skip and never a false "present".
        source_rows = [{"doc_id": "doc-A", "position": 0, "chash": "d" * 32}]
        collection_for_doc = {"doc-A": "coll-1"}
        identity_factory = _FakeIdentitySourceFactory({"coll-1": None})
        manifest_source = _FakeManifestSource({"doc-A": []})  # target has nothing for doc-A
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert manifest_source.calls == ["doc-A"]
        assert result["missing"] == 1
        assert result["filled"] == 1
        assert result["indeterminate"] == 0

    def test_unreachable_manifest_for_candidate_doc_is_indeterminate_no_blind_fill(self) -> None:
        shared_chash = "e" * 32
        source_rows = [{"doc_id": "doc-A", "position": 0, "chash": shared_chash}]
        collection_for_doc = {"doc-A": "coll-1"}
        # chash present in the collection -> ambiguous candidate
        identity_factory = _FakeIdentitySourceFactory({"coll-1": {shared_chash}})
        # per-doc manifest unreachable
        manifest_source = _FakeManifestSource({"doc-A": None})
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["indeterminate"] == 1
        assert result["missing"] == 0
        assert result["filled"] == 0
        assert spy.calls == []

    def test_batches_respect_batch_size_within_one_doc(self) -> None:
        source_rows = [
            {"doc_id": "doc-A", "position": i, "chash": f"{i:032d}"} for i in range(25)
        ]
        collection_for_doc = {"doc-A": "coll-1"}
        identity_factory = _FakeIdentitySourceFactory({"coll-1": set()})
        manifest_source = _FakeManifestSource({})
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc=collection_for_doc,
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=10,
            breaker=EtlCircuitBreaker(),
        )

        assert result["filled"] == 25
        assert spy.total_rows_sent == 25
        assert len(spy.calls) == 3  # ceil(25/10)
        assert all(len(b) <= 10 for _, b in spy.calls)
        assert all(doc_id == "doc-A" for doc_id, _ in spy.calls)

    def test_empty_source_rows_is_noop(self) -> None:
        identity_factory = _FakeIdentitySourceFactory({})
        manifest_source = _FakeManifestSource({})
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=[],
            collection_for_doc={},
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result == {
            "source_count": 0,
            "missing": 0,
            "filled": 0,
            "indeterminate": 0,
        }
        assert identity_factory.calls == []
        assert spy.calls == []
