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


# ── fill_missing: telemetry-shaped MULTI-COLUMN conflict keys (P3b) ──────────
#
# verify-fill P3b (nexus-s3dd4.14): the telemetry inner loop reuses
# fill_missing UNMODIFIED against HttpTelemetryStore.probe_ids, whose
# conflict keys are multi-column TUPLES (e.g. hook_failures' (doc_id,
# hook_name, occurred_at)), not the single-string keys every other caller
# (chash/catalog) uses. fill_missing's key_fn/IdentitySource contract is
# declared str-typed but never enforced at runtime -- this proves tuple
# keys work identically (hashable, comparable), the same widening precedent
# ManifestSource's _manifest_key already established for a different shape.


class TestFillMissingTupleKeys:
    def test_multi_column_conflict_key_diff_fills_only_missing(self) -> None:
        # shaped like hook_failures: (doc_id, hook_name, occurred_at)
        source_rows = [
            {"doc_id": "d1", "hook_name": "h1", "occurred_at": "2024-01-01T00:00:00Z"},
            {"doc_id": "d2", "hook_name": "h1", "occurred_at": "2024-01-02T00:00:00Z"},
            {"doc_id": "d3", "hook_name": "h2", "occurred_at": "2024-01-03T00:00:00Z"},
        ]
        # d1's row already landed; d2/d3 are missing
        present = {("d1", "h1", "2024-01-01T00:00:00Z")}
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: (r["doc_id"], r["hook_name"], r["occurred_at"]),
            identity_source=_FakeIdentitySource(present),  # type: ignore[arg-type]
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 2
        assert result["filled"] == 2
        assert {(r["doc_id"], r["hook_name"], r["occurred_at"]) for b in spy.batches for r in b} == {
            ("d2", "h1", "2024-01-02T00:00:00Z"),
            ("d3", "h2", "2024-01-03T00:00:00Z"),
        }

    def test_single_column_tuple_key_frecency_shaped(self) -> None:
        # shaped like frecency: (chunk_id,) -- a 1-tuple, not a bare string
        source_rows = [{"chunk_id": f"c{i}"} for i in range(5)]
        present = {(f"c{i}",) for i in range(3)}
        spy = _SpyImportFn()

        result = vf.fill_missing(
            source_rows=source_rows,
            key_fn=lambda r: (r["chunk_id"],),
            identity_source=_FakeIdentitySource(present),  # type: ignore[arg-type]
            import_fn=spy,
            batch_size=200,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 2
        assert {r["chunk_id"] for b in spy.batches for r in b} == {"c3", "c4"}


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
        # NON-empty target set proving absence (empty now means ambiguous —
        # the renamed-collection fix, 2026-07-02)
        identity_factory = _FakeIdentitySourceFactory({"coll-1": {"z" * 32}})
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

    def test_manifest_source_accepts_real_manifestrow_dataclasses(self) -> None:
        # R3 substantive-critic regression (2026-07-02): the only real
        # manifest fetch -- HttpCatalogClient.get_manifest -- returns
        # list[ManifestRow] (frozen dataclass, attribute access), NOT dicts.
        # The diff must consume that shape verbatim, or .5/.6 wiring the
        # real client raises TypeError on the first candidate resolution.
        from nexus.catalog.catalog_writes import ManifestRow  # noqa: PLC0415 — real-shape import scoped to this regression

        chash_present = "c" * 32
        chash_missing = "d" * 32
        source_rows = [
            {"doc_id": "doc-A", "position": 1, "chash": chash_present},  # landed
            {"doc_id": "doc-A", "position": 2, "chash": chash_present},  # hole, same chash
            {"doc_id": "doc-A", "position": 3, "chash": chash_missing},  # definite miss
        ]
        identity_factory = _FakeIdentitySourceFactory({"coll-1": {chash_present}})
        manifest_source = _FakeManifestSource({
            "doc-A": [ManifestRow(position=1, chash=chash_present)],
        })
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc={"doc-A": "coll-1"},
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 2
        assert result["filled"] == 2
        sent = [row for _, batch in spy.calls for row in batch]
        # definite-missing rows fill before resolved candidates; assert
        # exact CONTENT as a set (order is an implementation detail).
        assert {(r["position"], r["chash"]) for r in sent} == {
            (2, chash_present), (3, chash_missing),
        }


    def test_prefilter_accepts_full_64hex_chashes_from_the_real_manifest(self) -> None:
        # --hole-punch journey regression (2026-07-02): the catalog manifest
        # stores FULL 64-hex chashes and /manifest/chashes returns them raw,
        # while source rows diff on chash[:32]. An unnormalized prefilter set
        # matches NOTHING -> every row "definitely missing" -> the WHOLE doc
        # re-sends (observed: filled=12 for a 3-row hole). Fixture uses real
        # 64-hex shape on the TARGET side, 64-hex source rows diffed by [:32].
        full = lambda ch: ch * 2  # 32-hex -> 64-hex
        present_32 = ["a" * 32, "b" * 32]
        holed_32 = ["c" * 32]
        source_rows = [
            {"doc_id": "doc-A", "position": i, "chash": full(ch)}
            for i, ch in enumerate(present_32 + holed_32)
        ]
        identity_factory = _FakeIdentitySourceFactory(
            {"coll-1": {full(ch) for ch in present_32}}  # RAW 64-hex, as the server returns
        )
        manifest_source = _FakeManifestSource({
            "doc-A": [
                {"position": i, "chash": full(ch)} for i, ch in enumerate(present_32)
            ],
        })
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc={"doc-A": "coll-1"},
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 1
        assert result["filled"] == 1
        sent = [row for _, batch in spy.calls for row in batch]
        assert [(r["position"], r["chash"]) for r in sent] == [(2, full("c" * 32))]


    def test_renamed_collection_empty_prefilter_resolves_via_manifest_not_full_resend(self) -> None:
        # Hole-punch journey (2026-07-02): cross-model migration RENAMES the
        # physical_collection (minilm-384 -> bge-768, RDR-160), but the
        # prefilter is queried by the SOURCE name -> EMPTY set. Empty must be
        # treated as AMBIGUOUS (resolve per-doc via the collection-independent
        # manifest), never as all-definitely-missing -- that re-sent whole
        # docs (observed filled=12 for a 3-row hole).
        present = ["a" * 32, "b" * 32]
        holed = ["c" * 32]
        source_rows = [
            {"doc_id": "doc-A", "position": i, "chash": ch}
            for i, ch in enumerate(present + holed)
        ]
        # source-named collection matches NOTHING target-side (renamed)
        identity_factory = _FakeIdentitySourceFactory({"coll-minilm": set()})
        manifest_source = _FakeManifestSource({
            "doc-A": [{"position": i, "chash": ch} for i, ch in enumerate(present)],
        })
        spy = _SpyChunkImportFn()

        result = vf.fill_missing_document_chunks(
            source_rows=source_rows,
            collection_for_doc={"doc-A": "coll-minilm"},
            identity_source_factory=identity_factory,
            manifest_source=manifest_source,
            import_fn=spy,
            batch_size=300,
            breaker=EtlCircuitBreaker(),
        )

        assert result["missing"] == 1
        assert result["filled"] == 1
        sent = [row for _, batch in spy.calls for row in batch]
        assert [(r["position"], r["chash"]) for r in sent] == [(2, "c" * 32)]
        # the ambiguity was resolved precisely: one manifest fetch, per doc
        assert manifest_source.calls == ["doc-A"]

    def test_docs_with_all_chashes_present_never_trigger_manifest_fetch(self) -> None:
        source_rows = [{"doc_id": "doc-A", "position": 0, "chash": "c" * 32}]
        collection_for_doc = {"doc-A": "coll-1"}
        # non-empty set not containing the chash: provable absence, no
        # manifest fetch needed (empty would mean ambiguous -> fetch)
        identity_factory = _FakeIdentitySourceFactory({"coll-1": {"z" * 32}})
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
        # empty prefilter -> ambiguous (renamed-collection fix); the doc's
        # REACHABLE empty manifest proves everything missing -> 25 fill
        identity_factory = _FakeIdentitySourceFactory({"coll-1": set()})
        manifest_source = _FakeManifestSource({"doc-A": []})
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
