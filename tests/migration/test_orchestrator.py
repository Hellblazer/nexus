# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P-1a (nexus-0wz93): the T2 ``migrate all`` orchestration as a
library callable.

``nexus.migration.orchestrator.migrate_all`` runs the seven-store ladder,
builds the single RDR-153 report, and verifies pg counts through an
injected :class:`CountSource` (RDR-152 bars a direct Python PG connection;
the default source counts via the service REST endpoint). The CLI
(``nx storage migrate all``) and the conexus upgrade veneer both consume
this one callable — there is no second orchestration code path.
"""
from __future__ import annotations

import pytest

from nexus.migration import orchestrator as orch
from nexus.migration.etl_registry import EtlSources, StoreEtl


# ── Fakes ────────────────────────────────────────────────────────────────────


def _fake_etls(order_sink: list[str], *, fail_store: str | None = None):
    """Fake StoreEtls recording execution order and feeding the collector
    one row per store, plus the verify-mapped relations memory/plans."""

    def _runner(store: str):
        def run(sources: EtlSources, collector) -> dict:
            order_sink.append(store)
            collector.count_read(store, store, 10)
            collector.count_written(store, store, 10)
            if store == fail_store:
                collector.record_event(
                    store, store,
                    issue_class="unexpected", constraint=store,
                    reason="injected", action="failed",
                )
            return {}

        return run

    # deliberately shuffled — orchestrator must impose ladder order
    return [
        StoreEtl(s, _runner(s))
        for s in ("catalog", "memory", "plans", "taxonomy",
                  "telemetry", "aspects", "aspects_queue")
    ]


class _FakeCountSource:
    """A :class:`CountSource` returning canned pg counts (or ``None`` to
    simulate an unreachable count source → indeterminate)."""

    def __init__(self, counts: dict[str, int] | None):
        self._counts = counts
        self.seen: list[str] | None = None

    def counts(self, relations: list[str]) -> dict[str, int] | None:
        self.seen = list(relations)
        return self._counts


def _sources(tmp_path) -> EtlSources:
    db = tmp_path / "memory.db"
    cat = tmp_path / ".catalog.db"
    db.touch()
    cat.touch()
    return EtlSources(sqlite_path=db, catalog_db_path=cat)


# ── Orchestration ────────────────────────────────────────────────────────────


def test_chash_store_retired_from_the_etl_registry():
    """RDR-187 (nexus-piwya.10): the legacy --cold chash ETL leg is GONE —
    the router is dropped; /v1/chash/import accept-and-no-ops; running the
    leg would waste ETL time and report misleading migrated-row counts.
    The chunks migration IS the chash registration."""
    from nexus.migration.etl_registry import LADDER_ORDER
    assert "chash" not in LADDER_ORDER


class TestMigrateAll:
    def test_runs_in_exact_ladder_order(self, tmp_path, monkeypatch) -> None:
        order: list[str] = []
        monkeypatch.setattr(
            orch, "build_store_etls", lambda s: _fake_etls(order),
        )
        orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
        )
        assert order == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "catalog", "aspects_queue",
        ]

    def test_returns_report_dict_with_rollup_and_id(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            orch, "build_store_etls", lambda s: _fake_etls([]),
        )
        report = orch.migrate_all(
            _sources(tmp_path),
            count_source=_FakeCountSource({"nexus.memory": 10, "nexus.plans": 10}),
        )
        assert report["schema_version"] == "1"
        assert report["migration_id"]
        assert report["summary"]["total_read"] == 70  # 7 stores × 10 (chash retired, RDR-187)
        assert report["summary"]["total_written"] == 70
        assert report["summary"]["total_failed"] == 0

    def test_on_store_callback_fires_per_store_in_order(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(
            orch, "build_store_etls", lambda s: _fake_etls([]),
        )
        seen: list[str] = []
        orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
            on_store=seen.append,
        )
        assert seen == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "catalog", "aspects_queue",
        ]

    def test_store_crash_recorded_not_raised(self, tmp_path, monkeypatch) -> None:
        def _boom_etls(_s):
            def run(sources, collector):
                collector.count_read("memory", "memory", 2)
                collector.count_written("memory", "memory", 1)
                raise RuntimeError("mid-run partition")

            return [StoreEtl("memory", run)]

        monkeypatch.setattr(orch, "build_store_etls", _boom_etls)
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource(None),
        )
        assert report["summary"]["total_read"] == 2  # partial data preserved
        assert report["summary"]["total_failed"] == 1

    def test_on_store_failed_fires_with_store_and_exc(
        self, tmp_path, monkeypatch,
    ) -> None:
        def _boom_etls(_s):
            def run(sources, collector):
                raise RuntimeError("mid-run partition")

            return [StoreEtl("memory", run)]

        monkeypatch.setattr(orch, "build_store_etls", _boom_etls)
        seen: list[tuple[str, str]] = []
        orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource(None),
            on_store_failed=lambda store, exc: seen.append((store, str(exc))),
        )
        assert seen == [("memory", "mid-run partition")]

    def test_relations_checked_recorded_in_report(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        report = orch.migrate_all(
            _sources(tmp_path),
            count_source=_FakeCountSource({"nexus.memory": 10, "nexus.plans": 10}),
        )
        # only memory + plans map to verify relations in the fake
        assert report["relations_checked"] == 2


class TestSkipStores:
    """RDR-178 Gap 7 (nexus-1sx01): the already-migrated skip seam."""

    def test_skipped_store_is_not_run(self, tmp_path, monkeypatch) -> None:
        order: list[str] = []
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls(order))
        orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
            skip_stores=frozenset({"catalog"}),
        )
        assert "catalog" not in order
        assert order == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "aspects_queue",
        ]

    def test_skipped_store_gets_no_callbacks(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        on_store_seen: list[str] = []
        on_progress_seen: list[str] = []
        orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
            skip_stores=frozenset({"memory"}),
            on_store=on_store_seen.append,
            on_progress=lambda store, w, r: on_progress_seen.append(store),
        )
        assert "memory" not in on_store_seen
        assert "memory" not in on_progress_seen

    def test_skipped_stores_recorded_in_report_not_counted(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
            skip_stores=frozenset({"memory", "plans"}),
        )
        assert report["skipped_stores"] == ["memory", "plans"]
        assert report["summary"]["total_read"] == 50  # 5 run stores × 10 (7-store registry post-RDR-187, 2 skipped)
        assert report["summary"]["total_written"] == 50
        assert report["summary"]["total_failed"] == 0

    def test_no_skip_stores_key_when_nothing_skipped(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
        )
        assert "skipped_stores" not in report


# ── Count verification through the injected source ───────────────────────────


class TestVerifyCounts:
    def test_verified_when_pg_counts_meet_written(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        src = _FakeCountSource({"nexus.memory": 10, "nexus.plans": 10})
        report = orch.migrate_all(_sources(tmp_path), count_source=src)
        assert report["verification"] == "verified"
        # only the verify-mapped relations are queried
        assert set(src.seen) == {"nexus.memory", "nexus.plans"}

    def test_indeterminate_when_source_unreachable(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource(None),
        )
        assert report["verification"] == "indeterminate"

    def test_indeterminate_when_source_omits_a_relation(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        # plans missing → cannot confirm → indeterminate, never a silent pass
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({"nexus.memory": 10}),
        )
        assert report["verification"] == "indeterminate"

    def test_mismatch_when_pg_below_written(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls([]))
        report = orch.migrate_all(
            _sources(tmp_path),
            count_source=_FakeCountSource({"nexus.memory": 3, "nexus.plans": 10}),
        )
        assert report["verification"] == "mismatch"

    def test_plans_convergence_collapse_is_verified_with_note(self) -> None:
        # plans is a DEDUP relation: pg_count < written is convergence, not loss.
        report = {
            "stores": [
                {"store": "plans", "tables": [{"table": "plans", "written": 98}]},
            ],
        }
        status, notes, _ = orch.verify_counts(
            report, _FakeCountSource({"nexus.plans": 80}),
        )
        assert status == "verified"
        assert len(notes) == 1
        assert "nexus.plans" in notes[0]
        assert "80" in notes[0] and "98" in notes[0] and "18" in notes[0]
        assert "UNIQUE" in notes[0]

    def test_plans_written_zero_is_trivial_pass(self) -> None:
        report = {
            "stores": [
                {"store": "plans", "tables": [{"table": "plans", "written": 0}]},
            ],
        }
        status, notes, _ = orch.verify_counts(
            report, _FakeCountSource({"nexus.plans": 50}),
        )
        assert status == "verified"
        assert notes == []

    def test_no_mappable_relations_is_indeterminate(self) -> None:
        # telemetry/telemetry is not a verify-mapped relation → nothing to check
        report = {
            "stores": [
                {"store": "telemetry", "tables": [{"table": "telemetry", "written": 5}]},
            ],
        }
        status, notes, _ = orch.verify_counts(
            report, _FakeCountSource({"nexus.memory": 1}),
        )
        assert status == "indeterminate"
        assert notes == []


class TestBuildStoreEtls:
    def test_returns_stores_in_ladder_order(self) -> None:
        # A direct consumer (the RDR-159 guided engine) that iterates the
        # list without ordered() must still get FK-safe ladder order —
        # aspects_queue trails catalog.
        from nexus.migration.etl_registry import LADDER_ORDER

        # build_store_etls constructs HTTP stores lazily, so listing the
        # store names does not touch the service.
        etls = orch.build_store_etls(EtlSources(sqlite_path=None, catalog_db_path=None))  # type: ignore[arg-type]
        assert [e.store for e in etls] == list(LADDER_ORDER)


# ── nexus-5drgy: pre-flight ETL import check (RDR-178 Gap 1) ─────────────────
#
# Jun-30 production run: `build_store_etls` defers each store's imports
# (PLC0415) to when that store's turn comes up, so a version-skewed wheel
# (orchestrator referencing an ETL module the installed wheel lacked) only
# surfaced AFTER earlier stores had already written. `migrate_all` must
# import every ladder step's ETL module up front and abort the ENTIRE run —
# before any store executes — on the first missing module.


class TestEtlImportModules:
    def test_derives_module_names_from_runner_bytecode(self) -> None:
        # Not a hand-maintained copy: parsed straight off the real
        # build_store_etls closures, so it cannot drift from what each
        # store's runner actually imports at call time.
        etls = orch.build_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None)  # type: ignore[arg-type]
        )
        by_store = {e.store: orch._etl_import_modules(e.run) for e in etls}
        assert set(by_store["memory"]) == {
            "nexus.db.t2.http_memory_store", "nexus.db.t2.memory_etl",
        }
        assert set(by_store["aspects"]) == {
            "nexus.db.t2.aspects_etl",
            "nexus.db.t2.http_document_aspects_store",
            "nexus.db.t2.http_document_highlights_store",
        }
        assert set(by_store["catalog"]) == {
            "nexus.catalog.factory", "nexus.db.t2.catalog_etl",
        }

    def test_runner_with_no_imports_yields_empty_tuple(self) -> None:
        def run(sources: EtlSources, collector) -> dict:
            return {}

        assert orch._etl_import_modules(run) == ()


class TestAssertEtlsImportable:
    def test_green_path_all_real_etl_modules_import_cleanly(self) -> None:
        etls = orch.build_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None)  # type: ignore[arg-type]
        )
        orch.assert_etls_importable(etls)  # must not raise

    def test_missing_module_raises_before_returning(self) -> None:
        def run(sources: EtlSources, collector) -> dict:
            from nexus.migration._nexus_5drgy_missing_module import Thing  # noqa: F401,PLC0415

            return {}

        with pytest.raises(orch.EtlPreflightFailed) as excinfo:
            orch.assert_etls_importable([StoreEtl("memory", run)])
        assert "nexus.migration._nexus_5drgy_missing_module" in str(excinfo.value)
        assert "memory" in str(excinfo.value)


class TestMigrateAllPreflight:
    def test_aborts_before_any_store_executes(self, tmp_path, monkeypatch) -> None:
        executed: list[str] = []

        def _bad_etls(_s):
            def run(sources: EtlSources, collector) -> dict:
                from nexus.migration._nexus_5drgy_missing_module import Thing  # noqa: F401,PLC0415

                executed.append("memory")  # would only run if preflight failed to gate
                return {}

            return [StoreEtl("memory", run)]

        monkeypatch.setattr(orch, "build_store_etls", _bad_etls)
        with pytest.raises(orch.EtlPreflightFailed) as excinfo:
            orch.migrate_all(_sources(tmp_path), count_source=_FakeCountSource({}))
        assert executed == []
        assert "nexus.migration._nexus_5drgy_missing_module" in str(excinfo.value)

    def test_skipped_store_import_breakage_does_not_block_pending_stores(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Wave-1 composed review (2026-07-02): pre-flight (Gap 1) must not
        run against stores excluded via skip_stores (Gap 7) — an import
        breakage in an already-migrated store never executes, so it must not
        abort genuinely-pending stores."""
        executed: list[str] = []

        def _etls(_s):
            def bad_run(sources: EtlSources, collector) -> dict:
                from nexus.migration._nexus_5drgy_missing_module import Thing  # noqa: F401,PLC0415

                return {}

            def good_run(sources: EtlSources, collector) -> dict:
                executed.append("plans")
                return {}

            return [StoreEtl("memory", bad_run), StoreEtl("plans", good_run)]

        monkeypatch.setattr(orch, "build_store_etls", _etls)
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
            skip_stores=frozenset({"memory"}),
        )
        assert executed == ["plans"]
        assert report["skipped_stores"] == ["memory"]

    def test_green_path_proceeds_through_the_ladder(self, tmp_path, monkeypatch) -> None:
        order: list[str] = []
        monkeypatch.setattr(orch, "build_store_etls", lambda s: _fake_etls(order))
        report = orch.migrate_all(
            _sources(tmp_path), count_source=_FakeCountSource({}),
        )
        assert order == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "catalog", "aspects_queue",
        ]
        assert report["summary"]["total_failed"] == 0


class TestVerifyTables:
    def test_catalog_relations_are_catalog_prefixed(self) -> None:
        assert orch._VERIFY_TABLES[("catalog", "documents")] == "nexus.catalog_documents"
        assert orch._VERIFY_TABLES[("catalog", "links")] == "nexus.catalog_links"

    def test_plans_is_the_only_dedup_relation(self) -> None:
        assert orch._VERIFY_TABLES_DEDUP == frozenset({"nexus.plans"})
