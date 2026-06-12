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
        from unittest.mock import MagicMock, call, patch

        import nexus.db.t2.aspects_etl as ae

        mock_aspects = MagicMock()
        mock_highlights = MagicMock()
        mock_queue = MagicMock()
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


# ── nexus-d583z: _VERIFY_TABLES / _verify_pg_counts bug fixes ────────────────


class TestVerifyPgCountsBugFixes:
    """nexus-d583z: (a) relation names, (b) RLS-blind counts, (c) plans dedup."""

    def test_verify_tables_uses_catalog_documents_not_nexus_documents(
        self,
    ) -> None:
        """(a) The catalog/documents key must map to nexus.catalog_documents,
        not nexus.documents (which does not exist in the schema)."""
        from nexus.commands.storage_cmd import _VERIFY_TABLES

        assert _VERIFY_TABLES[("catalog", "documents")] == "nexus.catalog_documents"

    def test_verify_tables_uses_catalog_links_not_nexus_links(self) -> None:
        """(a) The catalog/links key must map to nexus.catalog_links."""
        from nexus.commands.storage_cmd import _VERIFY_TABLES

        assert _VERIFY_TABLES[("catalog", "links")] == "nexus.catalog_links"

    def test_verify_pg_counts_prefixes_count_with_set_tenant(
        self, tmp_path: Path,
    ) -> None:
        """(b) Each psql -c must include SET nexus.tenant = 'default'; before
        the SELECT count(*) so the admin user bypasses FORCE RLS."""
        from nexus.commands.storage_cmd import _verify_pg_counts

        captured_cmds: list[list[str]] = []

        def _fake_run(cmd, **kw):
            captured_cmds.append(cmd)

            class _R:
                returncode = 0
                stdout = "5"
            return _R()

        report = {
            "stores": [
                {
                    "store": "memory",
                    "tables": [{"table": "memory", "written": 5}],
                }
            ]
        }
        creds = {
            "PG_PORT": "5499",
            "NX_DB_ADMIN_USER": "nexus_admin",
            "NX_DB_ADMIN_PASS": "secret",
            "NX_DB_URL": "postgresql://127.0.0.1:5499/nexus",
        }
        with (
            patch("nexus.commands.storage_cmd._psql_for_verify", return_value="/usr/bin/psql"),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            outcome = _verify_pg_counts(report, creds)

        assert outcome == "verified"
        assert len(captured_cmds) == 1
        # The -c argument must contain the SET GUC prefix
        cmd = captured_cmds[0]
        c_flag_idx = cmd.index("-c")
        query_arg = cmd[c_flag_idx + 1]
        assert "SET nexus.tenant" in query_arg, (
            f"Expected SET nexus.tenant in psql -c arg, got: {query_arg!r}"
        )
        assert "SELECT count(*)" in query_arg

    def test_verify_pg_counts_plans_uses_dedup_aware_check(
        self, tmp_path: Path,
    ) -> None:
        """(c) Plans use server-side dedup (UNIQUE tenant/project/query).
        pg_count may legitimately be less than written (collapsed duplicates).
        The check: pg_count > 0 AND pg_count <= written (not pg_count >= written).
        """
        from nexus.commands.storage_cmd import _verify_pg_counts

        def _fake_run(cmd, **kw):
            class _R:
                returncode = 0
                stdout = "80"  # pg landed 80, but 98 were 'written' (HTTP acks)
            return _R()

        report = {
            "stores": [
                {
                    "store": "plans",
                    "tables": [{"table": "plans", "written": 98}],
                }
            ]
        }
        creds = {
            "PG_PORT": "5499",
            "NX_DB_ADMIN_USER": "nexus_admin",
            "NX_DB_ADMIN_PASS": "secret",
            "NX_DB_URL": "postgresql://127.0.0.1:5499/nexus",
        }
        with (
            patch("nexus.commands.storage_cmd._psql_for_verify", return_value="/usr/bin/psql"),
            patch("subprocess.run", side_effect=_fake_run),
        ):
            outcome = _verify_pg_counts(report, creds)

        # 80 landed < 98 written: dedup-aware semantics -> NOT a mismatch
        assert outcome == "verified", (
            "Plans dedup collapse (pg_count < written) must not be flagged as mismatch"
        )
