# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx storage migrate all`` — the CLI wrapper over the
``nexus.migration.orchestrator.migrate_all`` callable (RDR-153 Phase 3;
RDR-159 P-1a extracted the orchestration into the library).

The CLI is now thin: it builds the sources, calls ``migrate_all`` (which
runs the RDR-152 ladder, builds the ONE report, and verifies pg counts
through the service REST count source — RDR-152 bars a direct Python PG
connection), persists the returned report, echoes the verdict loudly, and
maps ``total_failed`` / the verification verdict onto exit codes. The
ladder-order / report-rollup / verification-semantics contracts are
unit-tested directly in ``tests/migration/test_orchestrator.py``; here we
pin the CLI seam.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.migration.etl_registry import EtlSources, StoreEtl

# ── Fakes ────────────────────────────────────────────────────────────────────


def _fake_etls(order_sink: list[str], *, fail_store: str | None = None):
    """Eight fake StoreEtls that record execution order and feed the shared
    collector realistic per-store data."""

    def _runner(store: str):
        def run(sources: EtlSources, collector) -> dict:
            order_sink.append(store)
            collector.count_read(store, store, 10)
            collector.count_written(store, store, 10)
            if store == "taxonomy":
                collector.record(
                    store, "topic_assignments",
                    issue_class="orphan_parent",
                    constraint="topic_assignments.topic_id -> topics.id",
                    reason="deleted topic",
                    action="skipped",
                    sample_id="d:9",
                )
            if store == fail_store:
                collector.record(
                    store, store,
                    issue_class="unexpected",
                    constraint=store,
                    reason="injected",
                    action="failed",
                    sample_id="x",
                )
            return {}

        return run

    return [
        StoreEtl(s, _runner(s))
        for s in ("catalog", "memory", "chash", "plans",
                  "taxonomy", "telemetry", "aspects",
                  "aspects_queue")  # deliberately shuffled
    ]


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _invoke_all(runner, tmp_path: Path, order_sink: list[str], *args,
                fail_store: str | None = None, verify: str = "verified"):
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "memory.db").touch()
    (config_dir / ".catalog.db").touch()
    # Patch the orchestrator seam: fake ETLs (no service) + a canned
    # verification verdict so the CLI behaviour is exercised in isolation.
    with patch(
        "nexus.migration.orchestrator.build_store_etls",
        return_value=_fake_etls(order_sink, fail_store=fail_store),
    ), patch(
        "nexus.migration.orchestrator.verify_counts",
        return_value=(verify, [], {}),
    ), patch.dict(
        "os.environ", {"NEXUS_CONFIG_DIR": str(config_dir)},
    ):
        return runner.invoke(main, ["storage", "migrate", "all", *args]), config_dir


# ── Orchestrator (CLI seam) ──────────────────────────────────────────────────


class TestMigrateAll:
    def test_runs_in_exact_ladder_order(self, runner, tmp_path: Path) -> None:
        # nexus-iy5se: aspects_queue (queue import) runs AFTER catalog so
        # queue rows with valid doc_ids do not fail FK constraints on a virgin
        # target where catalog_documents is still empty.
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order)
        assert result.exit_code == 0, result.output
        assert order == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "chash", "catalog", "aspects_queue",
        ]

    def test_single_merged_report_with_rollup(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        report_path = tmp_path / "out" / "report.json"
        result, _ = _invoke_all(
            runner, tmp_path, order, "--report", str(report_path),
        )
        assert result.exit_code == 0, result.output
        report = json.loads(report_path.read_text())
        assert report["schema_version"] == "1"
        stores = {s["store"] for s in report["stores"]}
        assert stores == {
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "chash", "catalog", "aspects_queue",
        }
        # 8 stores x 10 rows each = 80
        assert report["summary"]["total_read"] == 80
        assert report["summary"]["total_written"] == 80
        assert report["summary"]["by_action"]["skipped"] == 1
        assert report["summary"]["total_failed"] == 0
        assert report["summary"]["max_severity"] == 3

    def test_default_report_path_always_produces_artifact(
        self, runner, tmp_path: Path,
    ) -> None:
        order: list[str] = []
        result, config_dir = _invoke_all(runner, tmp_path, order)
        assert result.exit_code == 0, result.output
        reports = list((config_dir / "migration-reports").glob("migration-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text())
        assert report["migration_id"] in reports[0].name

    def test_failed_rows_exit_nonzero_and_say_so(
        self, runner, tmp_path: Path,
    ) -> None:
        order: list[str] = []
        result, _ = _invoke_all(
            runner, tmp_path, order, fail_store="telemetry",
        )
        assert result.exit_code != 0
        assert "total_failed=1" in result.output
        assert "NOT clean" in result.output

    def test_summary_lines_in_output(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order)
        assert "total_failed=0" in result.output
        assert "report:" in result.output  # the artifact path is announced

    def test_per_store_progress_announced(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order)
        assert "migrating memory …" in result.output

    def test_store_crash_echoed_in_real_time(self, runner, tmp_path: Path) -> None:
        """A store-level EXCEPTION (not a recorded event) must surface a
        per-store CRASHED line at crash time, not only in the final summary."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "memory.db").touch()
        (config_dir / ".catalog.db").touch()

        def _boom_etls(_sources):
            def run(sources, collector):
                raise RuntimeError("mid-run partition")

            return [StoreEtl("memory", run)]

        with patch(
            "nexus.migration.orchestrator.build_store_etls", _boom_etls,
        ), patch(
            "nexus.migration.orchestrator.verify_counts",
            return_value=("indeterminate", [], {}),
        ), patch.dict(
            "os.environ", {"NEXUS_CONFIG_DIR": str(config_dir)},
        ):
            result = runner.invoke(main, ["storage", "migrate", "all"])
        assert result.exit_code != 0  # crash → total_failed=1
        assert "memory: CRASHED — mid-run partition" in result.output


# ── r0esi: verification must never silently skip ─────────────────────────────


class TestVerificationLoudness:
    def test_indeterminate_verification_warns_loudly_never_all_passed(
        self, runner, tmp_path: Path,
    ) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order, verify="indeterminate")
        assert result.exit_code == 0, result.output  # data migrated fine
        out = result.output
        assert "VERIFICATION INDETERMINATE" in out
        assert "count source" in out
        assert "all passed" not in out.lower()

    def test_verified_counts_reported(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order, verify="verified")
        assert "verification: verified" in result.output
        # fake etls map memory + plans + (taxonomy) topic_assignments →
        # 3 verify relations in the checked set; the count is named, not vague.
        assert "3 mappable relations" in result.output

    def test_mismatch_fails_run(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order, verify="mismatch")
        assert result.exit_code != 0
        assert "VERIFICATION MISMATCH" in result.output

    def test_verification_verdict_recorded_in_artifact(
        self, runner, tmp_path: Path,
    ) -> None:
        """Critic S1: the artifact must be self-contained — the Phase-4
        triage surface reads the JSON, not the run's stdout."""
        order: list[str] = []
        report_path = tmp_path / "r.json"
        result, _ = _invoke_all(
            runner, tmp_path, order, "--report", str(report_path),
            verify="indeterminate",
        )
        assert result.exit_code == 0, result.output
        report = json.loads(report_path.read_text())
        assert report["verification"] == "indeterminate"


# ── Per-store --report (unchanged path) ──────────────────────────────────────

class TestPerStoreReport:
    def test_migrate_memory_report_writes_single_store_document(
        self, runner, tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()
        report_path = tmp_path / "memory-report.json"

        def fake_migrate(source_db_path, store, *, collector=None, **kw):
            if collector is not None:
                collector.count_read("memory", "memory", 4)
                collector.count_written("memory", "memory", 4)
            return {"read": 4, "written": 4}

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        with patch(
            "nexus.db.t2.memory_etl.migrate_memory_rows",
            side_effect=fake_migrate,
        ), patch(
            "nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore,
        ), patch.dict(
            "os.environ",
            {"NEXUS_CONFIG_DIR": str(config_dir),
             "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "memory",
                "--db", str(db),
                "--report", str(report_path),
            ])
        assert result.exit_code == 0, result.output
        report = json.loads(report_path.read_text())
        assert [s["store"] for s in report["stores"]] == ["memory"]
        assert report["summary"]["total_written"] == 4

    def test_per_store_default_path_always_produces_artifact(
        self, runner, tmp_path: Path,
    ) -> None:
        """Critic S3 / CRE L1: the per-store default-path guarantee."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()

        def fake_migrate(source_db_path, store, *, collector=None, **kw):
            if collector is not None:
                collector.count_read("memory", "memory", 1)
                collector.count_written("memory", "memory", 1)
            return {"read": 1, "written": 1}

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        with patch(
            "nexus.db.t2.memory_etl.migrate_memory_rows",
            side_effect=fake_migrate,
        ), patch(
            "nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore,
        ), patch.dict(
            "os.environ",
            {"NEXUS_CONFIG_DIR": str(config_dir),
             "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "memory", "--db", str(db),
            ])
        assert result.exit_code == 0, result.output
        reports = list((config_dir / "migration-reports").glob("migration-*.json"))
        assert len(reports) == 1

    def test_per_store_crash_still_writes_artifact(
        self, runner, tmp_path: Path,
    ) -> None:
        """Critic S2 / CRE M2: partial data beats no data — a mid-run crash
        must still leave the triage artifact."""
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        db = config_dir / "t2.db"
        db.touch()
        report_path = tmp_path / "crash-report.json"

        def exploding_migrate(source_db_path, store, *, collector=None, **kw):
            if collector is not None:
                collector.count_read("memory", "memory", 2)
                collector.count_written("memory", "memory", 1)
            raise RuntimeError("mid-run network partition")

        class _FakeStore:
            def __init__(self, *a, **k): ...
            def close(self): ...

        with patch(
            "nexus.db.t2.memory_etl.migrate_memory_rows",
            side_effect=exploding_migrate,
        ), patch(
            "nexus.db.t2.http_memory_store.HttpMemoryStore", _FakeStore,
        ), patch.dict(
            "os.environ",
            {"NEXUS_CONFIG_DIR": str(config_dir),
             "NX_SERVICE_TOKEN": "t", "NX_SERVICE_URL": "http://127.0.0.1:1"},
        ):
            result = runner.invoke(main, [
                "storage", "migrate", "memory", "--db", str(db),
                "--report", str(report_path),
            ])
        assert result.exit_code != 0
        assert "ETL failed" in result.output
        report = json.loads(report_path.read_text())
        assert report["summary"]["total_read"] == 2  # partial data preserved


# ── nexus-5drgy: pre-flight ETL import check aborts the whole run ────────────


class TestPreflightImportCheck:
    """RDR-178 Gap 1: a version-skewed ETL module must abort the ENTIRE run
    before any store executes — never a partial migration."""

    def test_missing_module_aborts_before_any_store_and_names_it(
        self, runner, tmp_path: Path,
    ) -> None:
        config_dir = tmp_path / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "memory.db").touch()
        (config_dir / ".catalog.db").touch()

        order: list[str] = []

        def _bad_etls(_sources):
            def run(sources, collector):
                from nexus.migration._nexus_5drgy_missing_module import Thing  # noqa: F401,PLC0415

                order.append("memory")
                return {}

            return [StoreEtl("memory", run)]

        with patch(
            "nexus.migration.orchestrator.build_store_etls", _bad_etls,
        ), patch.dict(
            "os.environ", {"NEXUS_CONFIG_DIR": str(config_dir)},
        ):
            result = runner.invoke(main, ["storage", "migrate", "all"])
        assert result.exit_code != 0
        assert order == [], "no store may execute once the preflight import check fails"
        assert "nexus.migration._nexus_5drgy_missing_module" in result.output
        # no report artifact — a preflight-failed run must not read as a
        # partial or completed migration
        assert not (config_dir / "migration-reports").exists() or not list(
            (config_dir / "migration-reports").glob("migration-*.json")
        )


# ── nexus-iy5se: aspects_queue ladder order ───────────────────────────────────


class TestAspectsQueueLadderOrder:
    """nexus-iy5se: aspects_queue must run after catalog in LADDER_ORDER."""

    def test_aspects_queue_follows_catalog_in_ladder_order(self) -> None:
        """LADDER_ORDER constant must list aspects_queue after catalog."""
        from nexus.migration.etl_registry import LADDER_ORDER

        assert "aspects_queue" in LADDER_ORDER
        assert LADDER_ORDER.index("aspects_queue") > LADDER_ORDER.index("catalog"), (
            "aspects_queue must appear after catalog in LADDER_ORDER so queue "
            "rows with valid doc_ids import after catalog_documents is populated"
        )

    def test_aspects_queue_is_separate_from_aspects_in_ladder(self) -> None:
        """aspects and aspects_queue are distinct slots; aspects runs before catalog."""
        from nexus.migration.etl_registry import LADDER_ORDER

        assert "aspects" in LADDER_ORDER
        assert LADDER_ORDER.index("aspects") < LADDER_ORDER.index("catalog")
        assert LADDER_ORDER.index("aspects") < LADDER_ORDER.index("aspects_queue")

    def test_aspects_without_queue_migrate_all_excludes_queue(self) -> None:
        """aspects_etl.migrate_without_queue does NOT call migrate_queue;
        document_aspects, highlights, and promotion_log are migrated."""
        from unittest.mock import MagicMock, patch

        import nexus.db.t2.aspects_etl as ae

        mock_aspects = MagicMock()
        mock_highlights = MagicMock()
        sqlite = MagicMock()

        with (
            patch.object(ae, "migrate_aspects", return_value={"imported": 1, "skipped": 0, "errors": 0}) as m_aspects,
            patch.object(ae, "migrate_highlights", return_value={"imported": 1, "skipped": 0, "errors": 0}) as m_highlights,
            patch.object(ae, "migrate_queue", return_value={"imported": 0, "skipped": 0, "errors": 0}) as m_queue,
            patch.object(ae, "migrate_promotion_log", return_value={"imported": 0, "skipped": 0, "errors": 0}) as m_promo,
        ):
            ae.migrate_without_queue(sqlite, mock_aspects, mock_highlights, collector=None)
            assert m_aspects.called
            assert m_highlights.called
            assert m_promo.called
            assert not m_queue.called, "migrate_queue must NOT be called from migrate_without_queue"

    def test_aspects_etl_has_migrate_queue_function(self) -> None:
        """migrate_queue is a public function importable from aspects_etl."""
        from nexus.db.t2.aspects_etl import migrate_queue  # noqa: F401
