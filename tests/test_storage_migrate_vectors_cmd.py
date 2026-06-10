# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx storage migrate vectors`` CLI wiring tests (RDR-155 P5.2, nexus-9n4pn).

The engine is fully covered by ``tests/migration/test_vector_etl.py``;
these tests pin the thin Click wiring only: leg/flag routing, token gate,
exit-code semantics, and that the source path / collections subset reach
the engine verbatim.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.storage_cmd import migrate_vectors_cmd
from nexus.migration.vector_etl import CollectionResult, MigrationReport

_COLL = "knowledge__cli__minilm-l6-v2-384__v1"


def _report(leg: str, *results: CollectionResult) -> MigrationReport:
    return MigrationReport(leg=leg, results=tuple(results))


@pytest.fixture()
def runner(monkeypatch) -> CliRunner:
    monkeypatch.setenv("NX_SERVICE_TOKEN", "cli-test-token")
    return CliRunner()


class TestMigrateVectorsCmd:
    def test_missing_token_is_a_clean_error(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        result = CliRunner().invoke(migrate_vectors_cmd, [])
        assert result.exit_code != 0
        assert "NX_SERVICE_TOKEN" in result.output

    def test_dry_run_and_rollback_mutually_exclusive(self, runner) -> None:
        result = runner.invoke(migrate_vectors_cmd, ["--dry-run", "--rollback"])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output

    def test_local_leg_passes_path_and_collections_verbatim(
        self, runner, monkeypatch, tmp_path
    ) -> None:
        calls: list[dict] = []

        def fake_migrate_local(local_path, vector_client, *, collections=None, dry_run=False, page_size=None):
            calls.append({"path": Path(local_path), "collections": collections, "dry_run": dry_run})
            return _report("local", CollectionResult(_COLL, 3, 3, "migrated"))

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_local", fake_migrate_local
        )
        result = runner.invoke(
            migrate_vectors_cmd,
            ["--local-path", str(tmp_path), "--collections", f"{_COLL}, other__x__minilm-l6-v2-384__v1"],
        )
        assert result.exit_code == 0, result.output
        assert calls == [
            {
                "path": tmp_path,
                "collections": [_COLL, "other__x__minilm-l6-v2-384__v1"],
                "dry_run": False,
            }
        ]
        assert "source=3" in result.output

    def test_cloud_flag_routes_to_cloud_leg(self, runner, monkeypatch) -> None:
        legs: list[str] = []

        def fake_migrate_cloud(vector_client, *, collections=None, dry_run=False, page_size=None, **kw):
            legs.append("cloud")
            return _report("cloud", CollectionResult(_COLL, 2, 2, "migrated"))

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_cloud", fake_migrate_cloud
        )
        result = runner.invoke(migrate_vectors_cmd, ["--cloud"])
        assert result.exit_code == 0, result.output
        assert legs == ["cloud"]
        assert "cloud leg" in result.output

    def test_not_ok_report_exits_nonzero(self, runner, monkeypatch, tmp_path) -> None:
        """A skipped/failed collection must surface as a failing exit code —
        a partial migration is never a green CLI run."""

        def fake_migrate_local(local_path, vector_client, **kw):
            return _report(
                "local",
                CollectionResult(_COLL, 3, 3, "migrated"),
                CollectionResult("knowledge__legacy", 0, 0, "skipped", "not conformant"),
            )

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_local", fake_migrate_local
        )
        result = runner.invoke(migrate_vectors_cmd, ["--local-path", str(tmp_path)])
        assert result.exit_code != 0
        assert "NOT clean" in result.output
        assert "not conformant" in result.output

    def test_rollback_routes_through_engine_and_reports_counts(
        self, runner, monkeypatch, tmp_path
    ) -> None:
        opened: list[Path] = []

        def fake_open_local(path):
            opened.append(Path(path))
            return object()

        def fake_rollback(read_client, vector_client, *, collections=None, page_size=None):
            return {_COLL: 7}

        monkeypatch.setattr(
            "nexus.migration.chroma_read.open_local_read_client", fake_open_local
        )
        monkeypatch.setattr(
            "nexus.migration.vector_etl.rollback_collections", fake_rollback
        )
        result = runner.invoke(migrate_vectors_cmd, ["--rollback", "--local-path", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert opened == [tmp_path]
        assert "7 chunk(s) removed" in result.output
        assert "source untouched" in result.output
