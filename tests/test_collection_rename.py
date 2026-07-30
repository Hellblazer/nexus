# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1ccq — `nx collection rename` + data-plane cascade coverage.

ChromaDB Cloud's ``collection.modify(name=...)`` is an O(1) metadata-only
rename. The CLI wraps it and cascades the new name through every surface
that stores a collection string:

  * The T2 cascade (``T2Database.rename_collection_cascade``) — since
    nexus-i711w Stage 2 sub-stages A/A3 deleted the SQLite T2 stores,
    every leg (chash index, document aspects, aspect queue, highlights,
    taxonomy, telemetry) routes through its Http domain store to the
    engine. Store-level routing coverage lives in
    ``tests/test_t2_rename_cascade_service_mode.py``; this module covers
    the data-plane ORCHESTRATION — ordering, error contracts, and count
    surfacing — plus the engine-backed ``document_aspects`` rename
    semantics (via the suite's hermetic engine substrate).
  * Catalog documents' ``physical_collection`` (JSONL + SQLite cache).

Error contracts: the T2 cascade fails CLOSED (ClickException, exit
non-zero — CG-1 / nexus-nhyh); the catalog cascade is fail-open after
T2+T3 land, mirroring the delete-cascade contract.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROMA_API_KEY", "test")
    monkeypatch.setenv("VOYAGE_API_KEY", "test")
    monkeypatch.setenv("CHROMA_TENANT", "test")
    monkeypatch.setenv("CHROMA_DATABASE", "test")


def _stub_t2_cascade(counts: dict[str, int] | None = None):
    """Return ``(spy_t2db, runner)`` for patching ``nexus.mcp_infra.t2_index_write``.

    The data plane's whole T2 leg is one call::

        t2_index_write(lambda t2db: t2db.rename_collection_cascade(old=..., new=...))

    The runner executes the caller's ``write_fn`` against a MagicMock
    T2Database whose ``rename_collection_cascade`` returns *counts*, so tests
    can assert the cascade was invoked with the right names AND that the data
    plane surfaces the returned counts — without a live engine (the SQLite
    cascade legs were deleted in nexus-i711w Stage 2 sub-stage A; the Http
    legs need a real service).
    """
    t2db = MagicMock(name="t2db-stub")
    t2db.rename_collection_cascade.return_value = dict(counts or {})

    def _runner(write_fn):
        return write_fn(t2db)

    return t2db, _runner


# ── ChashIndex.rename_collection ────────────────────────────────────────────


# TestChashIndexRename stood here (nexus-i711w Stage 2 sub-stage A). Its three
# tests called the SQLite ``ChashIndex.rename_collection`` directly and
# asserted raw ``chash_index`` table states through the store's ``conn`` /
# ``_lock`` seams — the store is deleted, so the subject is gone. The live
# cascade routing through ``HttpChashIndex.rename_collection`` is pinned by
# tests/test_t2_rename_cascade_service_mode.py; the row-level semantics
# (matching-rows count, no-rows-zero, and the nexus-v7mn collision precedence
# "source row's data wins") are the engine's responsibility now — see the
# GAP-CANDIDATE note in the sub-stage A2 port report for the collision-
# precedence contract.


# ── CatalogTaxonomy.rename_collection ───────────────────────────────────────


# TestTaxonomyRename stood here (nexus-i711w Stage 2 sub-stage C). Its two
# tests called ``CatalogTaxonomy.rename_collection`` directly — the deleted
# class's OWN method, not the cascade — so their subject is gone rather than
# merely unreachable. The service-side equivalent is covered by
# tests/test_t2_rename_cascade_service_mode.py, which spies
# HttpTaxonomyStore.rename_collection through the cascade and asserts the
# same three counts (topics / assignments / meta).
#
# The cascade's raw-SQL SQLite taxonomy leg that C deliberately left behind
# was retired in sub-stage A, as C's note promised: the cascade now routes
# the taxonomy leg through HttpTaxonomyStore unconditionally.


# ── Catalog.rename_collection ───────────────────────────────────────────────


class TestCatalogRename:
    def _seed(self, tmp_path: Path):
        # nexus-i711w terminal deletion: seeds through ActiveCatalog (live
        # catalog) — the raw local Catalog arm is gone.
        from tests._catalog_fixture_ops import ActiveCatalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = ActiveCatalog()
        # The engine's rename endpoint 404s on an unregistered source
        # collection (unlike the deleted local arm, which renamed bare
        # document strings) — register the rows the way production does.
        cat.register_collection("knowledge__old", embedding_model="test-model")
        cat.register_collection("knowledge__stays", embedding_model="test-model")
        owner = cat.register_owner("knowledge-corpus", "corpus")
        tumbler_a = cat.register(
            owner, title="doc-a", content_type="paper", file_path="a.pdf",
            physical_collection="knowledge__old", chunk_count=3,
        )
        tumbler_b = cat.register(
            owner, title="doc-b", content_type="paper", file_path="b.pdf",
            physical_collection="knowledge__stays", chunk_count=2,
        )
        return cat, cat_dir, tumbler_a, tumbler_b

    def test_updates_matching_docs(self, tmp_path: Path) -> None:
        cat, cat_dir, tumbler_a, tumbler_b = self._seed(tmp_path)
        count = cat.rename_collection("knowledge__old", "knowledge__new")
        assert count == 1

        # The catalog reflects the rename (public reads; the raw local-SQLite
        # readback died with the local catalog, nexus-i711w).
        assert [d.title for d in cat.list_by_collection("knowledge__new")] == ["doc-a"]
        assert cat.list_by_collection("knowledge__old") == []
        assert [d.title for d in cat.list_by_collection("knowledge__stays")] == ["doc-b"]

    # test_jsonl_appended_so_rebuild_preserves_rename retired (nexus-i711w
    # terminal deletion): documents.jsonl and the rebuild path died with
    # the local catalog.

    def test_unknown_collection_raises_not_found(self, tmp_path: Path) -> None:
        """Service behaviour-of-record (nexus-i711w terminal deletion): the
        engine 404s on renaming a collection that was never registered —
        the deleted local arm returned 0. Divergence recorded; whether the
        client should map 404 -> 0 belongs to the nexus-cecqy conformance
        family."""
        import httpx
        cat, *_ = self._seed(tmp_path)
        with pytest.raises(httpx.HTTPStatusError, match="collection not found"):
            cat.rename_collection("knowledge__ghost", "knowledge__phantom")

    # test_rename_preserves_source_mtime_across_jsonl_rebuild retired
    # (nexus-i711w terminal deletion): JSONL-rebuild round-tripping was a
    # property of the deleted local catalog.


# ── CLI `nx collection rename` ──────────────────────────────────────────────


class TestRenameCLI:
    def _fake_t3(self, *, old_exists: bool = True, new_exists: bool = False) -> MagicMock:
        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda name: (
                old_exists if name == "code__old" else
                new_exists if name == "code__new" else
                False
            ),
        )
        fake.rename_collection = MagicMock()
        return fake

    # test_rename_happy_path retired (nexus-i711w terminal deletion): it
    # pinned the client-side fan-out (t2_index_write T2 leg + separate T3
    # rename + surfaced stub counts), which the unconditional atomic
    # server-side re-home (rename_collection_cascade on the service
    # catalog client) replaced. Live coverage:
    # tests/test_collection_rename_service_mode.py.

    def test_rename_rejects_unknown_old(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        fake = self._fake_t3(old_exists=False, new_exists=False)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower()
        fake.rename_collection.assert_not_called()

    def test_rename_rejects_collision(self, tmp_path: Path, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        fake = self._fake_t3(old_exists=True, new_exists=True)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])
        assert result.exit_code != 0
        assert "already exists" in result.output.lower()
        fake.rename_collection.assert_not_called()

    def test_rename_rejects_prefix_mismatch(self, env_creds) -> None:
        from nexus.commands.collection import rename_cmd

        runner = CliRunner()
        # No _t3 patch — the prefix gate runs before we touch T3.
        result = runner.invoke(rename_cmd, ["code__foo", "docs__foo"])
        assert result.exit_code != 0
        assert "prefix mismatch" in result.output.lower()

    # test_force_prefix_change_bypasses_gate retired (nexus-i711w terminal
    # deletion): same retired client-side fan-out pins as
    # test_rename_happy_path above. The prefix/model gates themselves stay
    # covered by the three reject tests above.


# ── Partial-cascade failure modes: RETIRED (nexus-i711w terminal deletion) ──
# TestRenameCascadeFailureModes pinned the T2-first / T3-last ordering and
# the fail-open catalog leg of the client-side fan-out. The data plane is
# now ONE atomic server-side rename_collection_cascade: a failure leaves
# the collection fully unchanged and RAISES (fail-closed) — the fail-open
# catalog behaviour these tests asserted no longer exists by design.


# ── DocumentAspects.rename_collection (nexus-gp20) ─────────────────────────


class TestDocumentAspectsRename:
    """RDR-108 Phase 1d: ``document_aspects.collection`` is a denorm cache;
    rename_collection keeps it in sync with the T3 collection rename.

    Ported (nexus-i711w Stage 2 sub-stage A3): the SQLite ``DocumentAspects``
    store is deleted; ``T2Database(path).document_aspects`` is now the
    engine-backed ``HttpDocumentAspectsStore`` on the suite's hermetic
    per-test tenant, so the row-level rename semantics run for REAL against
    Postgres (AspectRepository.renameAspectCollection). Raw-SQL seeds became
    public ``upsert`` calls; raw COUNTs became ``list_by_collection`` reads.
    """

    @staticmethod
    def _record(collection: str, source_path: str, extractor: str = "test_extractor"):
        from nexus.db.t2.records import AspectRecord

        return AspectRecord(
            collection=collection,
            source_path=source_path,
            problem_formulation=None,
            proposed_method=None,
            experimental_datasets=[],
            experimental_baselines=[],
            experimental_results=None,
            extras={},
            confidence=0.9,
            extracted_at="2026-05-09T00:00:00Z",
            model_version="v1",
            extractor_name=extractor,
        )

    def _seed(self, tmp_path: Path):
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "t2.db")
        store = db.document_aspects
        store.upsert(self._record("code__old", "a.py"))
        store.upsert(self._record("code__old", "b.py"))
        store.upsert(self._record("code__stays", "c.py"))
        return store

    @staticmethod
    def _count(store, collection: str) -> int:
        return len(store.list_by_collection(collection))

    def test_updates_matching_rows(self, tmp_path: Path) -> None:
        store = self._seed(tmp_path)
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 2

        assert (
            self._count(store, "code__old"),
            self._count(store, "code__new"),
            self._count(store, "code__stays"),
        ) == (0, 2, 1)

    def test_source_path_untouched(self, tmp_path: Path) -> None:
        """source_path denorm cache must be byte-identical pre/post rename."""
        store = self._seed(tmp_path)

        def _all_paths() -> set[str]:
            return {
                r.source_path
                for coll in ("code__old", "code__new", "code__stays")
                for r in store.list_by_collection(coll)
            }

        paths_before = _all_paths()
        store.rename_collection(old="code__old", new="code__new")
        assert _all_paths() == paths_before

    def test_no_rows_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        store = T2Database(tmp_path / "t2.db").document_aspects
        assert store.rename_collection(old="docs__ghost", new="docs__phantom") == 0

    def test_idempotent_second_rename(self, tmp_path: Path) -> None:
        """Second call with old name that no longer has rows is safe no-op."""
        store = self._seed(tmp_path)
        store.rename_collection(old="code__old", new="code__new")
        # Second rename of same old name: no rows match → zero, no error.
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 0

    def test_only_matching_collection_updated(self, tmp_path: Path) -> None:
        """Rows for 'code__stays' must not be touched."""
        store = self._seed(tmp_path)
        store.rename_collection(old="code__old", new="code__new")
        got = store.get("code__stays", "c.py")
        assert got is not None
        assert got.collection == "code__stays"


# ── AspectExtractionQueue.rename_collection (nexus-gp20) ───────────────────


# TestAspectExtractionQueueRename stood here (nexus-i711w Stage 2 sub-stage
# A). Its four tests called the SQLite ``AspectExtractionQueue``'s OWN
# rename_collection and asserted raw ``aspect_extraction_queue`` table states
# through the store's ``conn`` — the store is deleted, so the subject is
# gone. The live cascade routing through ``HttpAspectQueue.rename_collection``
# is pinned by tests/test_t2_rename_cascade_service_mode.py; the row-level
# semantics (matching-rows count, source_path/doc_id untouched, idempotent
# second rename) are the engine's responsibility now.


# ── Aspect cascade wired into rename_collection_data_plane (nexus-gp20) ────


# TestAspectCascadeIntegration retired (nexus-i711w terminal deletion):
# it asserted the t2_index_write-routed T2 cascade surfaced aspect counts;
# counts now come from the atomic service cascade
# (rename_collection_cascade key mapping in
# nexus.collection_rename.rename_collection_data_plane).
# (Its sibling tombstone preserved: test_no_collateral_writes_to_chash
# died earlier with the SQLite stores, nexus-i711w sub-stage A.)


# ── Cascade orchestration: every leg invoked, counts aggregated ─────────────
#
# TestCascadeAtomicity stood here (K4, nexus-nhyh). Its rollback test pinned
# the single-SQLite-transaction atomicity of the cascade via the ``_conn``
# injection seam — a property OF the deleted SQLite legs. Since nexus-i711w
# Stage 2 sub-stage A the chash / queue / highlights / taxonomy / telemetry
# legs are HTTP calls to the engine, outside any local transaction, so a
# mid-cascade failure can no longer be rolled back client-side: the K4
# contract as tested is dead, not merely re-plumbed. GAP-CANDIDATE (recorded
# in the sub-stage A2 port report): cross-store rename atomicity on the local
# fan-out path. The service-mode answer is the engine's single-transaction
# rename (the RDR-164 P3 service branch of rename_collection_data_plane),
# covered by tests/test_collection_rename_service_mode.py.
#
# The happy-path half survives below as TestCascadeOrchestration.


class TestCascadeOrchestration:
    """The cascade calls every leg's store-level ``rename_collection`` and
    aggregates the returned counts (successor to TestCascadeAtomicity's
    happy path).

    Since nexus-i711w Stage 2 sub-stage A3 the ``document_aspects`` leg is
    engine-side like every other leg — the last SQLite else-arm is gone and
    the cascade is a pure HTTP fan-out — so ALL six legs are spied here via
    T2Database instance-attribute assignment. Row-level movement is the
    engine's job (real-engine coverage: TestDocumentAspectsRename above;
    per-store routing coverage: tests/test_t2_rename_cascade_service_mode.py).
    """

    def test_successful_cascade_updates_all_legs(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"

        tax_spy = MagicMock()
        tax_spy.rename_collection.return_value = {
            "topics": 1, "assignments": 0, "meta": 0,
        }
        tel_spy = MagicMock()
        tel_spy.rename_collection.return_value = {
            "search_telemetry": 2, "hook_failures": 0,
        }
        chash_spy = MagicMock(**{"rename_collection.return_value": 1})
        aspects_spy = MagicMock(**{"rename_collection.return_value": 1})
        queue_spy = MagicMock(**{"rename_collection.return_value": 1})
        highlights_spy = MagicMock(**{"rename_collection.return_value": 1})

        with T2Database(db_path) as t2db:
            t2db.chash_index = chash_spy
            t2db.document_aspects = aspects_spy
            t2db.aspect_queue = queue_spy
            t2db.document_highlights = highlights_spy
            t2db.taxonomy = tax_spy
            t2db.telemetry = tel_spy

            counts = t2db.rename_collection_cascade(old="code__old", new="code__new")

        chash_spy.rename_collection.assert_called_once_with(
            old="code__old", new="code__new")
        aspects_spy.rename_collection.assert_called_once_with(
            old="code__old", new="code__new")
        queue_spy.rename_collection.assert_called_once_with(
            old="code__old", new="code__new")
        highlights_spy.rename_collection.assert_called_once_with(
            old="code__old", new="code__new")
        tax_spy.rename_collection.assert_called_once_with("code__old", "code__new")
        tel_spy.rename_collection.assert_called_once_with(
            old="code__old", new="code__new")

        assert counts == {
            "chash": 1,
            "aspects": 1,
            "aspect_queue": 1,
            "highlights": 1,
            "tax_topics": 1,
            "tax_assignments": 0,
            "tax_meta": 0,
            "search_telemetry": 2,
            "hook_failures": 0,
        }


# ── K4 collision defense: DocumentAspects (queue's version died with the
#    SQLite store — see the tombstone below) ─────────────────────────────────


class TestDocumentAspectsCollisionDefense:
    """K4 (nexus-nhyh): the document_aspects rename must defend against
    UNIQUE collisions like the chash leg does, using pre-DELETE of
    conflicting new-side rows before UPDATE.

    Ported (nexus-i711w Stage 2 sub-stage A3): the defense is engine-side
    now (AspectRepository.renameAspectCollection pre-DELETEs new-side rows
    whose source_path collides with an old-side row), exercised for REAL
    against Postgres through the engine-backed store.
    """

    def test_pk_collision_source_side_wins(self, tmp_path: Path) -> None:
        """Pre-existing (new_collection, source_path) row is deleted before
        UPDATE so the UNIQUE (tenant, collection, source_path) natural key
        is never violated — and the SOURCE row's data survives."""
        from nexus.db.t2 import T2Database

        store = T2Database(tmp_path / "t2.db").document_aspects
        _rec = TestDocumentAspectsRename._record
        store.upsert(_rec("code__old", "a.py", extractor="source"))
        # Collision: (new, same source_path) already exists.
        store.upsert(_rec("code__new", "a.py", extractor="stale"))

        # Must not raise a UNIQUE-violation from the engine.
        count = store.rename_collection(old="code__old", new="code__new")
        assert count == 1

        new_rows = store.list_by_collection("code__new")
        assert len(new_rows) == 1
        # The source row's data won (nexus-v7mn precedence).
        assert new_rows[0].extractor_name == "source"
        assert store.list_by_collection("code__old") == []


# TestAspectQueueCollisionDefense stood here (K4, nexus-nhyh). Its one test
# pinned the SQLite ``AspectExtractionQueue.rename_collection`` pre-DELETE
# collision defense — the store is deleted (nexus-i711w Stage 2 sub-stage A),
# so UNIQUE-collision handling on rename is the engine's responsibility now
# (see the GAP-CANDIDATE note in the sub-stage A2 port report, shared with
# the ChashIndex collision-precedence contract above).


# ── K9: search_telemetry + hook_failures included in cascade ─────────────────


# TestTelemetryRenameCollection stood here (K9, nexus-nhyh). Its three tests
# called the SQLite ``Telemetry.rename_collection`` directly and asserted raw
# ``search_telemetry`` / ``hook_failures`` table states — the store is deleted
# (nexus-i711w Stage 2 sub-stage A). The store-level dict contract
# ({"search_telemetry": N, "hook_failures": N}) and its cascade routing
# through HttpTelemetryStore are pinned by
# tests/test_t2_rename_cascade_service_mode.py (_SpyTelemetry); the row-level
# UPDATE semantics are the engine's responsibility now.


# TestK9CascadeIncludesTelemetry retired (nexus-i711w terminal deletion):
# same retired t2_index_write wiring as TestAspectCascadeIntegration; the
# telemetry counts now flow from the atomic service cascade.


# ── SIG-8 T2-first ordering: RETIRED (nexus-i711w terminal deletion) ────────
# TestRenameOrdering pinned "T2 cascade committed before T3 rename". There
# is no client-side ordering left to pin: the re-home is one atomic
# server-side transaction and the data plane makes no separate T3 call.


# ── CG-1 half-cascade non-zero exit: RETIRED (nexus-i711w terminal
# deletion) ── TestHalfCascadeNonZeroExit bombed the client-side
# T2Database cascade; that leg is gone. The fail-closed contract survives
# in the atomic path: a rename_collection_cascade failure raises
# ClickException ("service rename failed -- collection ... is unchanged").


# ── RDR-162 P2: cross-model reference remap (copy-not-move) ──────────────────


class TestRemapCollectionReferences:
    """RDR-162 P2: ``remap_collection_references`` re-points T2 + catalog
    references source -> target WITHOUT renaming the T3 collection and WITHOUT
    guarding on target existence (the cross-model migrate already populated the
    target; the source is intentionally retained for re-runnability)."""

    def test_repoints_t2_references_no_t3_rename(
        self, tmp_path: Path, env_creds,
    ) -> None:
        # Ported (nexus-i711w Stage 2 sub-stage A): previously seeded real
        # SQLite chash rows and read them back; row movement is engine-side
        # now, so this pins the remap ORCHESTRATION — the T2 cascade runs
        # source -> target and its counts surface. No _t3 patch, as before:
        # remap must NEVER touch T3 (copy-not-move).
        from nexus.collection_rename import remap_collection_references

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        src = "knowledge__corpus__minilm-l6-v2-384__v1"
        tgt = "knowledge__corpus__bge-base-en-v15-768__v1"
        t2db, t2_runner = _stub_t2_cascade({"chash": 2})

        with patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = remap_collection_references(src, tgt)

        t2db.rename_collection_cascade.assert_called_once_with(old=src, new=tgt)
        assert counts["chash"] == 2

    def test_repoints_all_t2_cascade_tables(
        self, tmp_path: Path, env_creds,
    ) -> None:
        """S2 (RDR-162 P2 review): the remap re-points EVERY T2 table that
        names a collection (not just chash) via the production
        ``rename_collection_cascade``.

        Ported (nexus-i711w Stage 2 sub-stage A): the per-table row movement
        is the cascade's / engine's job (pinned by
        tests/test_t2_rename_cascade_service_mode.py). The S2 property this
        function owns is that remap invokes the FULL cascade and copies the
        whole per-table count surface through verbatim — dropping a key here
        is exactly how a table silently falls out of the remap.
        """
        from nexus.collection_rename import remap_collection_references

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        src = "knowledge__c__minilm-l6-v2-384__v1"
        tgt = "knowledge__c__bge-base-en-v15-768__v1"

        cascade_counts = {
            "tax_topics": 1,
            "tax_assignments": 2,
            "tax_meta": 1,
            "chash": 1,
            "aspects": 1,
            "aspect_queue": 1,
            "highlights": 1,
            "search_telemetry": 2,
            "hook_failures": 0,
        }
        t2db, t2_runner = _stub_t2_cascade(cascade_counts)

        with patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = remap_collection_references(src, tgt)

        t2db.rename_collection_cascade.assert_called_once_with(old=src, new=tgt)
        # The return dict carries the full cascade surface, incl. highlights.
        for key, expected in cascade_counts.items():
            assert counts[key] == expected, (
                f"remap dropped cascade count {key!r}: "
                f"got {counts.get(key)!r}, expected {expected!r}"
            )

    def test_t2_cascade_failure_raises(self, tmp_path: Path, env_creds) -> None:
        import click

        from nexus.collection_rename import remap_collection_references

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        def _t2_bomb(*a, **kw):
            raise RuntimeError("simulated T2 cascade failure")

        with patch("nexus.mcp_infra.t2_index_write", side_effect=_t2_bomb), \
             patch("nexus.config.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            with pytest.raises(click.ClickException) as exc:
                remap_collection_references("code__a__minilm-l6-v2-384__v1",
                                            "code__a__bge-base-en-v15-768__v1")
        assert "cascade" in str(exc.value).lower()

    def test_catalog_failure_is_fail_open(self, tmp_path: Path, env_creds) -> None:
        # Ported (nexus-i711w Stage 2 sub-stage A): the real SQLite chash seed
        # became a t2_index_write stub; the fail-open contract under test is
        # unchanged — a catalog bomb only warns, and the T2 counts still return.
        from nexus.collection_rename import remap_collection_references

        src = "code__o__minilm-l6-v2-384__v1"
        tgt = "code__o__bge-base-en-v15-768__v1"
        t2db, t2_runner = _stub_t2_cascade({"chash": 1})

        warnings: list[str] = []
        bomb = MagicMock()
        bomb.rename_collection = MagicMock(side_effect=RuntimeError("catalog down"))

        with patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner):
            counts = remap_collection_references(
                src, tgt, catalog=bomb, on_warn=warnings.append,
            )

        # T2 cascade still ran and its counts surfaced; catalog failure only warned.
        t2db.rename_collection_cascade.assert_called_once_with(old=src, new=tgt)
        assert counts["chash"] == 1
        assert any("catalog" in w.lower() for w in warnings)
