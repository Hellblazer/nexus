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


@pytest.fixture(autouse=True)
def _pin_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the storage backend to SQLITE for the whole module.

    RDR-164 P3 added a SERVICE branch to ``rename_collection_data_plane`` that
    fans out to the Java service. The hard default is SERVICE (RDR-152), so
    without this pin these local-fan-out tests would route into the service
    branch. Service-mode coverage lives in
    ``test_collection_rename_service_mode.py``.

    Since nexus-i711w Stage 2 sub-stages A/A3 the pin no longer selects SQLite
    T2 stores — those are deleted. Under the pin, ``T2Database.taxonomy``
    RAISES on access and every cascade leg (chash / aspects / queue /
    highlights / telemetry) is Http-only, so a REAL cascade run cannot
    succeed here without the engine. Tests of the data-plane orchestration
    therefore stub ``nexus.mcp_infra.t2_index_write`` (see
    ``_stub_t2_cascade``) or inject spy stores on the T2Database instance;
    the store-level ``document_aspects`` rename tests below run against the
    suite's hermetic engine substrate directly.
    """
    from nexus.db.storage_mode import StorageBackend

    monkeypatch.setattr(
        "nexus.db.storage_mode.storage_backend_for",
        lambda store: StorageBackend.SQLITE,
    )


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
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
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

        # SQLite cache reflects the rename.
        rows = cat._db.execute(
            "SELECT physical_collection FROM documents ORDER BY tumbler",
        ).fetchall()
        assert [r[0] for r in rows] == ["knowledge__new", "knowledge__stays"]

    def test_jsonl_appended_so_rebuild_preserves_rename(self, tmp_path: Path) -> None:
        cat, cat_dir, tumbler_a, tumbler_b = self._seed(tmp_path)
        cat.rename_collection("knowledge__old", "knowledge__new")

        # Last record for tumbler 1.1 in JSONL must have the new collection.
        records = [
            json.loads(line)
            for line in (cat_dir / "documents.jsonl").read_text().splitlines()
            if line.strip()
        ]
        by_tumbler: dict[str, dict] = {}
        for r in records:
            by_tumbler[r["tumbler"]] = r
        assert by_tumbler[str(tumbler_a)]["physical_collection"] == "knowledge__new"
        assert by_tumbler[str(tumbler_b)]["physical_collection"] == "knowledge__stays"

    def test_no_matches_returns_zero(self, tmp_path: Path) -> None:
        cat, *_ = self._seed(tmp_path)
        assert cat.rename_collection("knowledge__ghost", "knowledge__phantom") == 0

    def test_rename_preserves_source_mtime_across_jsonl_rebuild(
        self, tmp_path: Path,
    ) -> None:
        """Regression: review-flagged Critical (Reviewer B/C1).

        `rename_collection` previously SELECTed 12 columns and appended a
        JSONL record without `source_mtime`. JSONL is the rebuild source
        of truth, so rebuild-from-JSONL silently reset mtime to 0.0 for
        every renamed document, breaking stale-source detection.
        """
        from nexus.catalog.catalog import Catalog

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        owner = cat.register_owner("papers", "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper", file_path="a.pdf",
            physical_collection="knowledge__old", chunk_count=1,
            source_mtime=1_700_000_000.0,
        )

        cat.rename_collection("knowledge__old", "knowledge__new")

        # Rebuild from JSONL — the durable source of truth. The mtime
        # must survive the round-trip.
        cat_rebuilt = Catalog(cat_dir, cat_dir / ".catalog-rebuilt.db")
        entry = cat_rebuilt.resolve(tumbler)
        assert entry is not None
        assert entry.physical_collection == "knowledge__new"
        assert entry.source_mtime == 1_700_000_000.0, (
            f"rename_collection lost source_mtime on JSONL rebuild: "
            f"got {entry.source_mtime}"
        )


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

    def test_rename_happy_path(self, tmp_path: Path, env_creds) -> None:
        """CLI wires the T2 cascade (via t2_index_write), then the T3 rename,
        and surfaces the cascade's counts in its summary line.

        Ported (nexus-i711w Stage 2 sub-stage A): previously seeded a real
        SQLite chash row and asserted the row moved; row-level movement is the
        engine's job now, so this pins the ORCHESTRATION — the cascade is
        invoked exactly once with (old, new) and its counts reach the output.
        """
        from nexus.commands.collection import rename_cmd

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        t2db, t2_runner = _stub_t2_cascade({"chash": 1})
        fake = self._fake_t3(old_exists=True, new_exists=False)
        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        assert result.exit_code == 0, result.output
        fake.rename_collection.assert_called_once_with("code__old", "code__new")

        # The T2 cascade ran, with the right names, and its counts surfaced.
        t2db.rename_collection_cascade.assert_called_once_with(
            old="code__old", new="code__new"
        )
        assert "1 chash rows" in result.output

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

    def test_force_prefix_change_bypasses_gate(self, tmp_path: Path, env_creds) -> None:
        # Ported (nexus-i711w Stage 2 sub-stage A): the "empty T2" seed that
        # made the cascade succeed trivially became a t2_index_write stub —
        # the SQLite cascade legs are gone and the Http legs need an engine.
        from nexus.commands.collection import rename_cmd

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__foo",
        )
        fake.rename_collection = MagicMock()
        _t2db, t2_runner = _stub_t2_cascade()

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(
                rename_cmd,
                ["code__foo", "docs__foo", "--force-prefix-change"],
            )
        assert result.exit_code == 0, result.output
        fake.rename_collection.assert_called_once_with("code__foo", "docs__foo")


# ── Partial-cascade failure mode (review finding + nexus-nhyh / CG-1) ────────


class TestRenameCascadeFailureModes:
    """T2 cascade failures must now exit non-zero (nexus-nhyh / CG-1).

    Old behavior (nexus-1ccq): T3 renamed first, T2 failures were fail-open
    (warn + exit 0). This left the system in an inconsistent state with no
    operator-facing signal that action was required.

    New behavior (nexus-nhyh SIG-8 + CG-1):
    - T2 cascade runs FIRST (T2-first ordering). On failure, T3 rename has
      NOT yet run, so the system is still fully consistent.
    - T2 failure raises ClickException (exit non-zero) with an actionable
      message. Operator must see the failure.
    - Catalog cascade remains fail-open: it is a derived view, and failures
      can be repaired by ``nx catalog rebuild``.
    """

    def test_t2_cascade_failure_exits_nonzero_and_aborts_t3(
        self, tmp_path: Path, env_creds,
    ) -> None:
        """T2 cascade throws → exit non-zero, T3 rename must NOT have fired
        (T2-first ordering: T3 only runs after T2 succeeds)."""
        from nexus.commands.collection import rename_cmd

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()

        def _t2_bomb(*a, **kw):
            raise RuntimeError("simulated T2 outage")

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.db.t2.T2Database", side_effect=_t2_bomb), \
             patch("nexus.config.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        # Must exit non-zero: T2 failure is not fail-open
        assert result.exit_code != 0, (
            f"T2 cascade failure must exit non-zero. Got {result.exit_code}. "
            f"Output: {result.output}"
        )
        # T3 must NOT have been called (T2-first ordering: T3 only runs after T2)
        fake.rename_collection.assert_not_called()
        # Error message must name the failure and be actionable
        assert "T2 cascade failed" in result.output or "cascade" in result.output.lower()
        assert "simulated T2 outage" in result.output

    def test_catalog_cascade_failure_prints_warn_and_continues(
        self, tmp_path: Path, env_creds,
    ) -> None:
        """Catalog cascade throws → T2 + T3 stay renamed, CLI still exits
        0 with a stderr warn line naming the catalog (fail-open).

        Ported (nexus-i711w Stage 2 sub-stage A): "seed an empty T2 so the
        cascade succeeds trivially" became a t2_index_write stub — a real
        cascade cannot succeed here (SQLite legs deleted, Http legs need an
        engine, and ``db.taxonomy`` raises under this module's =sqlite pin).
        """
        from nexus.commands.collection import rename_cmd

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()
        _t2db, t2_runner = _stub_t2_cascade()

        def _catalog_bomb(*a, **kw):
            raise RuntimeError("simulated catalog lock contention")

        runner = CliRunner()
        # RDR-146 P1.2: the catalog cascade routes the rename through
        # make_catalog_writer(); bomb that seam (patching in-process Catalog
        # would be bypassed by daemon routing).
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.catalog.factory.make_catalog_writer", side_effect=_catalog_bomb), \
             patch("nexus.catalog.catalog.Catalog", side_effect=_catalog_bomb):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        assert result.exit_code == 0, result.output
        # T3 was renamed (T2 succeeded, T3 ran after)
        fake.rename_collection.assert_called_once_with("code__old", "code__new")
        assert "catalog cascade failed" in result.output
        assert "simulated catalog lock contention" in result.output


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


class TestAspectCascadeIntegration:
    """rename_collection_data_plane must run the T2 cascade and surface BOTH
    aspect-family counts (nexus-gp20).

    Ported (nexus-i711w Stage 2 sub-stage A): the denorm tables live behind
    the engine now, so row-level movement is store/engine territory (routing
    pinned by tests/test_t2_rename_cascade_service_mode.py). What this file
    still owns is the data-plane wiring: the cascade is invoked and its
    ``aspects`` / ``aspect_queue`` counts are copied through verbatim.
    """

    def test_both_aspect_counts_surfaced_by_data_plane(
        self, tmp_path: Path, env_creds,
    ) -> None:
        from nexus.commands.collection import rename_collection_data_plane

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        t2db, t2_runner = _stub_t2_cascade({"aspects": 1, "aspect_queue": 1})
        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")
        fake.rename_collection = MagicMock()

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = rename_collection_data_plane("code__old", "code__new")

        t2db.rename_collection_cascade.assert_called_once_with(
            old="code__old", new="code__new"
        )
        assert counts["aspects"] == 1
        assert counts["aspect_queue"] == 1

    # test_no_collateral_writes_to_chash stood here. It renamed via the SQLite
    # ``DocumentAspects`` store and asserted the SQLite ``ChashIndex`` table
    # (a SEPARATE tmp DB file) was untouched — the chash side of that pin is
    # the deleted store, and with per-store engine endpoints the isolation is
    # structural, so the test died with its substrate (nexus-i711w sub-stage A).


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


class TestK9CascadeIncludesTelemetry:
    """K9 end-to-end: rename_collection_data_plane must include the telemetry
    tables in the cascade and surface their counts.

    Ported (nexus-i711w Stage 2 sub-stage A): the SQLite Telemetry store is
    deleted, so "the rows moved" is engine territory. The K9 wiring this file
    still owns is that the data plane copies the cascade's
    ``search_telemetry`` / ``hook_failures`` counts through to its callers.
    """

    def test_data_plane_updates_search_telemetry(
        self, tmp_path: Path, env_creds
    ) -> None:
        from nexus.commands.collection import rename_collection_data_plane

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        t2db, t2_runner = _stub_t2_cascade(
            {"search_telemetry": 1, "hook_failures": 0}
        )
        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")
        fake.rename_collection = MagicMock()

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            counts = rename_collection_data_plane("code__old", "code__new")

        t2db.rename_collection_cascade.assert_called_once_with(
            old="code__old", new="code__new"
        )
        assert counts.get("search_telemetry", 0) == 1
        assert counts.get("hook_failures", 0) == 0


# ── SIG-8: T2-first ordering (T3 rename last) ────────────────────────────────


class TestRenameOrdering:
    """SIG-8 (nexus-nhyh): T2 cascade must happen BEFORE T3 rename.
    Rationale: T2 UPDATEs are reversible; T3 chromadb rename is irrevocable.
    If T2 succeeds but T3 fails, operator can re-run rename. Reverse is
    unrecoverable.

    Test: simulate T3 rename failure; assert T2 was already committed
    (operator can reverse by running T2 cascade back to old name).
    """

    def test_t2_cascade_committed_before_t3_rename(
        self, tmp_path: Path, env_creds
    ) -> None:
        # Ported (nexus-i711w Stage 2 sub-stage A): previously proved
        # T2-first by reading the moved SQLite chash row after a T3 bomb;
        # row movement is engine-side now, so the ordering pin becomes:
        # the cascade HAS ALREADY RUN when the T3 rename fails.
        import click

        from nexus.commands.collection import rename_collection_data_plane

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        t2db, t2_runner = _stub_t2_cascade({"chash": 1})
        fake = MagicMock()
        fake.collection_exists = MagicMock(side_effect=lambda n: n == "code__old")

        def _t3_rename_bomb(old, new):
            raise RuntimeError("simulated T3 rename failure")

        fake.rename_collection = _t3_rename_bomb

        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.mcp_infra.t2_index_write", side_effect=t2_runner), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            with pytest.raises((RuntimeError, click.ClickException)):
                rename_collection_data_plane("code__old", "code__new")

        # T2-first: the cascade completed even though T3 failed afterwards.
        t2db.rename_collection_cascade.assert_called_once_with(
            old="code__old", new="code__new"
        )


# ── CG-1: half-cascade test + non-zero exit ──────────────────────────────────


class TestHalfCascadeNonZeroExit:
    """CG-1 (nexus-nhyh): CLI must exit non-zero when T2 cascade fails,
    not swallow the error and exit 0.

    The bead finding: broad `except Exception: on_warn(...)` swallows T2
    failures and returns exit 0 -- operator sees a warning but no indication
    that action is required.

    Fix: T2 cascade failure re-raises as ClickException (exit non-zero).
    """

    def test_t2_cascade_failure_exits_nonzero(
        self, tmp_path: Path, env_creds
    ) -> None:
        from nexus.commands.collection import rename_cmd
        from click.testing import CliRunner

        db_path = tmp_path / "memory.db"
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()

        fake = MagicMock()
        fake.collection_exists = MagicMock(
            side_effect=lambda n: n == "code__old",
        )
        fake.rename_collection = MagicMock()

        def _t2_bomb(*a, **kw):
            raise RuntimeError("simulated T2 cascade failure")

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake), \
             patch("nexus.db.t2.T2Database", side_effect=_t2_bomb), \
             patch("nexus.config.default_db_path", return_value=db_path), \
             patch("nexus.config.catalog_path", return_value=cat_dir):
            result = runner.invoke(rename_cmd, ["code__old", "code__new"])

        # Must exit non-zero so operator knows cascade failed
        assert result.exit_code != 0, (
            f"Expected non-zero exit when T2 cascade fails, got {result.exit_code}. "
            f"Output: {result.output}"
        )
        # Error message must be actionable
        assert "cascade" in result.output.lower() or "T2" in result.output, (
            f"Error message must name the failed cascade. Output: {result.output}"
        )


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
