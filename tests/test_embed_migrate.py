# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-144 P4: safe 384->768 local-embedder migration engine.

The engine detects collections whose stored vectors do not match the
active local embedder's dimension (the canonical case: a corpus indexed
with the bundled 384-dim minilm fallback, then the user upgrades to
bge-768 via ``nx init``) and migrates them under a gate-locked safety
protocol:

    dry-run preview  ->  double-confirm  ->  reindex-first  ->  delete-after-verify

The hard invariant is NO DATA LOSS on failure: the old collection is
deleted ONLY after the new one is verified populated. A mid-reindex
failure must leave the old collection fully intact.

Tests seed raw vectors via the underlying client (explicit ``embeddings=``
bypasses the configured EF) so detection works without a real bge model,
and inject the reindex driver so the dangerous ordering logic is exercised
deterministically.
"""
from __future__ import annotations

import chromadb
import pytest

from nexus.db.embed_migrate import (
    MigrationOutcome,
    StaleCollection,
    detect_stale_local_collections,
    migrate_collection_safe,
    _default_reindex,
    _target_name,
)
from nexus.db.t3 import T3Database

_DIM_384 = 384
_DIM_768 = 768


def _vec(dim: int, fill: float = 0.1) -> list[float]:
    return [fill] * dim


_TEST_COLLECTIONS = [
    "docs__proj__minilm-l6-v2-384__v1",
    "docs__proj__bge-base-en-v15-768__v1",
    "code__proj__minilm-l6-v2-384__v1",
    "code__proj__bge-base-en-v15-768__v1",
    "knowledge__notes__minilm-l6-v2-384__v1",
]


@pytest.fixture()
def t3() -> T3Database:
    """Fresh EphemeralClient-backed T3Database. EphemeralClient is a
    process-shared singleton, so drop every collection this module uses
    on entry — otherwise a prior test's target leaks in and a later
    "nothing created" assertion silently passes."""
    client = chromadb.EphemeralClient()
    for name in _TEST_COLLECTIONS:
        try:
            client.delete_collection(name)
        except Exception:
            pass
    return T3Database(_client=client)


def _seed(db: T3Database, name: str, dim: int, *, n: int = 2,
          source_path: str | None = "doc.md") -> None:
    """Seed ``n`` raw vectors of dimension ``dim`` into ``name``.

    EphemeralClient is shared across tests in a process — delete first so
    a prior test's vectors (possibly a different dimension) don't leak in
    and pin the collection dimension.
    """
    try:
        db._client.delete_collection(name)
    except Exception:
        pass
    col = db._client.get_or_create_collection(name)
    meta = {"chunk_text_hash": "c" * 64}
    if source_path is not None:
        meta = {**meta, "source_path": source_path}
    col.add(
        ids=[f"{name}:{i}" for i in range(n)],
        embeddings=[_vec(dim) for _ in range(n)],
        documents=[f"chunk {i}" for i in range(n)],
        metadatas=[dict(meta) for _ in range(n)],
    )


# ── _target_name ──────────────────────────────────────────────────────────────


class TestTargetName:
    def test_swaps_conformant_model_segment(self) -> None:
        old = "docs__nexus-1-1__minilm-l6-v2-384__v1"
        assert (
            _target_name(old, "bge-base-en-v15-768")
            == "docs__nexus-1-1__bge-base-en-v15-768__v1"
        )

    def test_legacy_two_segment_name_unchanged(self) -> None:
        # Legacy names do not encode the model; reindex lands in place.
        assert _target_name("docs__legacy", "bge-base-en-v15-768") == "docs__legacy"


# ── detection ─────────────────────────────────────────────────────────────────


class TestDetect:
    def test_384_under_active_768_is_detected_not_silent(self, t3: T3Database) -> None:
        _seed(t3, "docs__proj__minilm-l6-v2-384__v1", _DIM_384)

        stale = detect_stale_local_collections(t3, active_dim=_DIM_768)

        names = {s.name for s in stale}
        assert "docs__proj__minilm-l6-v2-384__v1" in names

    def test_matching_dim_collection_not_flagged(self, t3: T3Database) -> None:
        _seed(t3, "docs__proj__bge-base-en-v15-768__v1", _DIM_768)

        stale = detect_stale_local_collections(t3, active_dim=_DIM_768)

        assert all(
            s.name != "docs__proj__bge-base-en-v15-768__v1" for s in stale
        )

    def test_code_collection_classified_as_deferred(self, t3: T3Database) -> None:
        _seed(t3, "code__proj__minilm-l6-v2-384__v1", _DIM_384)

        stale = detect_stale_local_collections(t3, active_dim=_DIM_768)
        match = next(s for s in stale if s.name.startswith("code__"))

        assert match.kind == "code"

    def test_all_sourceless_classified_as_deferred(self, t3: T3Database) -> None:
        _seed(t3, "knowledge__notes__minilm-l6-v2-384__v1", _DIM_384,
              source_path=None)

        stale = detect_stale_local_collections(t3, active_dim=_DIM_768)
        match = next(s for s in stale if s.name.startswith("knowledge__"))

        assert match.kind == "sourceless"
        assert match.sourceless == 2


# ── migration safety protocol ─────────────────────────────────────────────────


class TestMigrateSafe:
    def _stale(self, name: str, target: str) -> StaleCollection:
        return StaleCollection(
            name=name,
            count=2,
            source_paths=frozenset({"doc.md"}),
            sourceless=0,
            target_name=target,
            kind="reindexable",
        )

    def test_dry_run_mutates_nothing(self, t3: T3Database) -> None:
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)

        def _reindex_fn(db, tgt, sources, corpus):  # pragma: no cover - must not run
            raise AssertionError("dry-run must not invoke the reindex driver")

        outcome = migrate_collection_safe(
            t3, self._stale(old, target), dry_run=True, reindex_fn=_reindex_fn
        )

        assert outcome.status == "dry-run"
        assert t3.collection_info(old)["count"] == 2  # old untouched
        assert not t3.collection_exists(target)  # nothing created

    def test_success_reindexes_then_deletes_old(self, t3: T3Database) -> None:
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)
        # target must not pre-exist
        try:
            t3._client.delete_collection(target)
        except Exception:
            pass

        def _reindex_fn(db, tgt, sources, corpus):
            # Simulate a real reindex: populate target with 768-dim vectors.
            col = db._client.get_or_create_collection(tgt)
            col.add(
                ids=[f"{tgt}:0", f"{tgt}:1"],
                embeddings=[_vec(_DIM_768), _vec(_DIM_768)],
                documents=["a", "b"],
                metadatas=[{"source_path": "doc.md"}, {"source_path": "doc.md"}],
            )
            return (1, 2)  # (indexed_sources, after_count)

        outcome = migrate_collection_safe(
            t3, self._stale(old, target), dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "migrated"
        assert outcome.after == 2
        assert t3.collection_exists(target)
        assert t3.collection_info(target)["count"] == 2
        assert not t3.collection_exists(old)  # deleted ONLY after verify

    def test_reindex_failure_midway_leaves_old_intact(self, t3: T3Database) -> None:
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)

        def _reindex_fn(db, tgt, sources, corpus):
            # Partially write to target, then blow up — the classic
            # mid-reindex crash that must NOT cost the user their data.
            col = db._client.get_or_create_collection(tgt)
            col.add(
                ids=[f"{tgt}:0"],
                embeddings=[_vec(_DIM_768)],
                documents=["a"],
                metadatas=[{"source_path": "doc.md"}],
            )
            raise RuntimeError("simulated mid-reindex failure")

        outcome = migrate_collection_safe(
            t3, self._stale(old, target), dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "failed"
        assert t3.collection_exists(old)  # NO loss
        assert t3.collection_info(old)["count"] == 2  # exact, full
        # The partial target is left behind (the engine does not clean it up
        # on failure). Lock the exact partial state so a future change that
        # alters cleanup behaviour is detected; a re-run reuses upsert
        # semantics into the same target.
        assert t3.collection_exists(target)
        assert t3.collection_info(target)["count"] == 1  # partial, not complete

    def test_partial_reindex_does_not_delete_old(self, t3: T3Database) -> None:
        """reindex driver reports fewer sources indexed than expected =>
        treat as failure, keep old (no silent partial-loss)."""
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)

        def _reindex_fn(db, tgt, sources, corpus):
            col = db._client.get_or_create_collection(tgt)
            col.add(
                ids=[f"{tgt}:0"],
                embeddings=[_vec(_DIM_768)],
                documents=["a"],
                metadatas=[{"source_path": "doc.md"}],
            )
            return (0, 1)  # 0 of the expected sources indexed

        outcome = migrate_collection_safe(
            t3, self._stale(old, target), dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "failed"
        assert t3.collection_exists(old)
        assert t3.collection_info(old)["count"] == 2

    def test_empty_target_after_reindex_keeps_old(self, t3: T3Database) -> None:
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)

        def _reindex_fn(db, tgt, sources, corpus):
            return (1, 0)  # claims a source but target ended up empty

        outcome = migrate_collection_safe(
            t3, self._stale(old, target), dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "failed"
        assert t3.collection_exists(old)
        assert t3.collection_info(old)["count"] == 2

    def test_mixed_sourceless_skipped_by_default(self, t3: T3Database) -> None:
        """A reindexable collection that also holds sourceless chunks must NOT
        be migrated by default — deleting it would silently drop the notes."""
        old = "knowledge__notes__minilm-l6-v2-384__v1"
        target = "knowledge__notes__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)
        stale = StaleCollection(
            name=old, count=5, source_paths=frozenset({"doc.md"}),
            sourceless=3, target_name=target, kind="reindexable",
        )

        def _reindex_fn(db, tgt, sources, corpus):  # pragma: no cover
            raise AssertionError("must not reindex a mixed collection by default")

        outcome = migrate_collection_safe(
            t3, stale, dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "skipped"
        assert "3" in outcome.reason  # names the sourceless count
        assert t3.collection_exists(old)  # notes preserved
        assert t3.collection_info(old)["count"] == 2

    def test_mixed_sourceless_migrates_with_explicit_optin(self, t3: T3Database) -> None:
        """With allow_sourceless_loss=True the mixed collection migrates (the
        caller has accepted the documented note loss)."""
        old = "knowledge__notes__minilm-l6-v2-384__v1"
        target = "knowledge__notes__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)
        try:
            t3._client.delete_collection(target)
        except Exception:
            pass
        stale = StaleCollection(
            name=old, count=5, source_paths=frozenset({"doc.md"}),
            sourceless=3, target_name=target, kind="reindexable",
        )

        def _reindex_fn(db, tgt, sources, corpus):
            col = db._client.get_or_create_collection(tgt)
            col.add(
                ids=[f"{tgt}:0"], embeddings=[_vec(_DIM_768)],
                documents=["a"], metadatas=[{"source_path": "doc.md"}],
            )
            return (1, 1)

        outcome = migrate_collection_safe(
            t3, stale, dry_run=False, reindex_fn=_reindex_fn,
            allow_sourceless_loss=True,
        )

        assert outcome.status == "migrated"
        assert not t3.collection_exists(old)

    def test_old_collection_delete_runs_full_cascade(self, t3: T3Database) -> None:
        """nexus-prgf4 (O1): the migration must purge the old collection via the
        full cascade (catalog/taxonomy/chash), not a bare delete that orphans
        catalog rows."""
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)
        try:
            t3._client.delete_collection(target)
        except Exception:
            pass

        calls: list[tuple] = []

        def _spy_cascade(db, name):
            calls.append((db, name))
            db.delete_collection(name)  # still performs the physical delete
            from nexus.db.collection_purge import CascadeCounts

            return CascadeCounts(catalog_docs_deleted=2)

        import nexus.db.collection_purge as cp

        # patch the symbol embed_migrate imports at call time
        orig = cp.purge_collection_cascade
        cp.purge_collection_cascade = _spy_cascade
        try:
            def _reindex_fn(db, tgt, sources, corpus):
                col = db._client.get_or_create_collection(tgt)
                col.add(ids=[f"{tgt}:0"], embeddings=[_vec(_DIM_768)],
                        documents=["a"], metadatas=[{"source_path": "doc.md"}])
                return (1, 1)

            outcome = migrate_collection_safe(
                t3, self._stale(old, target), dry_run=False, reindex_fn=_reindex_fn
            )
        finally:
            cp.purge_collection_cascade = orig

        assert outcome.status == "migrated"
        assert calls == [(t3, old)]          # cascade invoked, not bare delete
        assert not t3.collection_exists(old)  # old physically gone

    def test_deferred_kinds_are_skipped_never_deleted(self, t3: T3Database) -> None:
        old = "code__proj__minilm-l6-v2-384__v1"
        _seed(t3, old, _DIM_384)
        stale = StaleCollection(
            name=old, count=2, source_paths=frozenset({"a.py"}),
            sourceless=0, target_name="code__proj__bge-base-en-v15-768__v1",
            kind="code",
        )

        def _reindex_fn(db, tgt, sources, corpus):  # pragma: no cover
            raise AssertionError("deferred kinds must not reindex")

        outcome = migrate_collection_safe(
            t3, stale, dry_run=False, reindex_fn=_reindex_fn
        )

        assert outcome.status == "skipped"
        assert t3.collection_exists(old)  # never deleted


class TestDefaultReindexCounting:
    """nexus-s5m44 (S2): _default_reindex must count only content-producing
    sources, so a now-empty/broken file cannot inflate ``indexed`` and let
    delete-after-verify drop the old collection while the new one is short."""

    def test_zero_chunk_source_not_counted(
        self, t3: T3Database, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = tmp_path / "good.md"
        good.write_text("# Good\n\nReal content.\n")
        empty = tmp_path / "empty.md"
        empty.write_text("")  # exists, but produces 0 chunks

        def _fake_index_markdown(p, *, corpus, collection_name, force):
            return 3 if p.name == "good.md" else 0

        monkeypatch.setattr("nexus.doc_indexer.index_markdown", _fake_index_markdown)

        indexed, _after = _default_reindex(
            t3,
            "docs__proj__bge-base-en-v15-768__v1",
            frozenset({str(good), str(empty)}),
            "proj",
        )

        # only the content-producing source counts
        assert indexed == 1

    def test_all_content_sources_counted(
        self, t3: T3Database, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = tmp_path / "a.md"; a.write_text("# A\n\nx\n")
        b = tmp_path / "b.md"; b.write_text("# B\n\ny\n")

        monkeypatch.setattr(
            "nexus.doc_indexer.index_markdown",
            lambda p, *, corpus, collection_name, force: 2,
        )

        indexed, _after = _default_reindex(
            t3, "docs__proj__bge-base-en-v15-768__v1",
            frozenset({str(a), str(b)}), "proj",
        )

        assert indexed == 2

    def test_rdr_branch_counts_only_indexed_status(
        self, t3: T3Database, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rdr__ branch uses batch_index_markdowns -> count only "indexed"
        (not "skipped"/"failed") so a failed/empty source can't inflate."""
        good = tmp_path / "good.md"; good.write_text("# Good\n\nx\n")
        bad = tmp_path / "bad.md"; bad.write_text("# Bad\n\ny\n")

        def _fake_batch(paths, *, corpus, collection_name, force):
            return {str(good): "indexed", str(bad): "failed"}

        monkeypatch.setattr("nexus.doc_indexer.batch_index_markdowns", _fake_batch)

        indexed, _after = _default_reindex(
            t3, "rdr__proj__bge-base-en-v15-768__v1",
            frozenset({str(good), str(bad)}), "proj",
        )

        assert indexed == 1  # only the "indexed" source


class TestCascadeFailureSurfacing:
    """nexus-prgf4 follow-up: a migration whose delete-cascade leaves orphans
    must SAY SO in the outcome, not report a fully-clean migration."""

    def test_cascade_failures_folded_into_reason(
        self, t3: T3Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = "docs__proj__minilm-l6-v2-384__v1"
        target = "docs__proj__bge-base-en-v15-768__v1"
        _seed(t3, old, _DIM_384)

        import nexus.db.collection_purge as cp
        from nexus.db.collection_purge import CascadeCounts

        def _failing_cascade(db, name):
            db.delete_collection(name)
            return CascadeCounts(failures=["catalog cascade failed: boom"])

        monkeypatch.setattr(cp, "purge_collection_cascade", _failing_cascade)

        def _reindex_fn(db, tgt, sources, corpus):
            col = db._client.get_or_create_collection(tgt)
            col.add(ids=[f"{tgt}:0"], embeddings=[_vec(_DIM_768)],
                    documents=["a"], metadatas=[{"source_path": "doc.md"}])
            return (1, 1)

        outcome = migrate_collection_safe(
            t3,
            StaleCollection(name=old, count=2, source_paths=frozenset({"doc.md"}),
                            sourceless=0, target_name=target, kind="reindexable"),
            dry_run=False, reindex_fn=_reindex_fn,
        )

        assert outcome.status == "migrated"
        assert "cleanup incomplete" in outcome.reason
        assert "boom" in outcome.reason
