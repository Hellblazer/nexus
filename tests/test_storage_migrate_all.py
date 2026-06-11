# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-153 Phase 3 (bead nexus-o61zy, TDD-red): ``nx storage migrate all``
orchestrator + ``--report`` emission.

Contract (RDR §Approach 3): stores run in the RDR-152 Phase 2 LADDER ORDER
exactly (memory→plans→telemetry→taxonomy→aspects→chash→catalog LAST);
per-store results merge into ONE report document; ``--report`` defaults to
``<config>/migration-reports/migration-<id>.json`` so a run ALWAYS
produces an artifact; count-verification FAILS/WARNS loudly when psql is
unresolved — never SKIP-then-'all passed' (the nexus-r0esi hollow-verify
bug).
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
    """Seven fake StoreEtls that record execution order and feed the shared
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
                  "taxonomy", "telemetry", "aspects")  # deliberately shuffled
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
    with patch(
        "nexus.commands.storage_cmd._build_store_etls",
        return_value=_fake_etls(order_sink, fail_store=fail_store),
    ), patch(
        "nexus.commands.storage_cmd._verify_pg_counts",
        return_value=verify,
    ), patch.dict(
        "os.environ", {"NEXUS_CONFIG_DIR": str(config_dir)},
    ):
        return runner.invoke(main, ["storage", "migrate", "all", *args]), config_dir


# ── Orchestrator ─────────────────────────────────────────────────────────────


class TestMigrateAll:
    def test_runs_in_exact_ladder_order(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order)
        assert result.exit_code == 0, result.output
        assert order == [
            "memory", "plans", "telemetry", "taxonomy",
            "aspects", "chash", "catalog",
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
            "aspects", "chash", "catalog",
        }
        assert report["summary"]["total_read"] == 70
        assert report["summary"]["total_written"] == 70
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
        assert "psql" in out
        assert "all passed" not in out.lower()

    def test_verified_counts_reported(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order, verify="verified")
        assert "verification: verified" in result.output

    def test_mismatch_fails_run(self, runner, tmp_path: Path) -> None:
        order: list[str] = []
        result, _ = _invoke_all(runner, tmp_path, order, verify="mismatch")
        assert result.exit_code != 0
        assert "VERIFICATION MISMATCH" in result.output

    def test_verify_pg_counts_indeterminate_without_psql(
        self, tmp_path: Path,
    ) -> None:
        """The unit seam: no resolvable psql → 'indeterminate', never
        'verified' (the r0esi hollow-pass)."""
        from nexus.commands.storage_cmd import _verify_pg_counts

        with patch(
            "nexus.commands.storage_cmd._psql_for_verify", return_value=None,
        ):
            outcome = _verify_pg_counts(
                {"summary": {"total_written": 5}, "stores": []},
                {"PG_PORT": "5499", "NX_DB_ADMIN_USER": "a",
                 "NX_DB_ADMIN_PASS": "p",
                 "NX_DB_URL": "jdbc:postgresql://127.0.0.1:5499/nexus"},
            )
        assert outcome == "indeterminate"


# ── Per-store --report ───────────────────────────────────────────────────────

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


class TestP3ReviewRegressions:
    """P3.3/P3.4 review findings (2026-06-11)."""

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
