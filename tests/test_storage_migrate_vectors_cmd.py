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
    # nexus-pebfx.1: the hardcoded :8080 default URL is retired (resolution is
    # env > ServiceRegistry lease > fail loud), so tests must pin BOTH halves.
    # The engine functions are monkeypatched below; no HTTP ever happens.
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:1")
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

        def fake_migrate_local(local_path, vector_client, *, collections=None, dry_run=False, page_size=None, on_result=None):
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
        # nexus-pebfx.3: counts surface in the summary table — pin the
        # NUMERIC rendering (right-aligned 8-wide columns), not just the
        # table structure: a zeroed source_count must fail this test.
        assert "TOTAL" in result.output
        assert _COLL in result.output
        assert f"{3:>8} {3:>8}" in result.output

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


class TestEtlOperability:
    """nexus-pebfx.3 CLI surface: skipped-empty stays green, live progress
    lines flush per collection, dry-run never needs endpoint resolution."""

    def test_skipped_empty_exits_zero(self, runner, monkeypatch, tmp_path) -> None:
        """The 2026-06-10 headline wart: 15 EMPTY non-conformant collections
        forced the run red and required hand-pinning 49 names."""

        def fake_migrate_local(local_path, vector_client, **kw):
            return _report(
                "local",
                CollectionResult(_COLL, 3, 3, "migrated"),
                CollectionResult(
                    "tuples__x", 0, 0, "skipped-empty",
                    "not conformant (source has 0 chunks — nothing to lose)",
                ),
            )

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_local", fake_migrate_local
        )
        result = runner.invoke(migrate_vectors_cmd, ["--local-path", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "skipped-empty" in result.output
        assert "NOT clean" not in result.output

    def test_live_progress_lines_emitted_per_collection(
        self, runner, monkeypatch, tmp_path
    ) -> None:
        """The CLI passes on_result; each completed collection emits a
        flushed line BEFORE the summary table."""

        def fake_migrate_local(local_path, vector_client, *, on_result=None, **kw):
            results = (
                CollectionResult(_COLL, 3, 3, "migrated", duration_s=1.2),
                CollectionResult("docs__d__voyage-context-3__v1", 5, 5, "migrated",
                                 duration_s=0.4),
            )
            assert on_result is not None
            for r in results:
                on_result(r)
            return _report("local", *results)

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_local", fake_migrate_local
        )
        result = runner.invoke(migrate_vectors_cmd, ["--local-path", str(tmp_path)])
        assert result.exit_code == 0, result.output
        # Two live lines plus the table row for each: collection name appears twice.
        assert result.output.count(_COLL) == 2
        # Live line includes the duration.
        assert "(1.2s)" in result.output

    def test_dry_run_without_token_or_lease(self, monkeypatch, tmp_path) -> None:
        """Counting source chunks never touches the service: no token, no
        lease, no NX_SERVICE_URL — dry-run must still run (item 3; the
        endpoint pre-flight is skipped for --dry-run)."""
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)

        def fake_migrate_local(local_path, vector_client, **kw):
            return _report(
                "local", CollectionResult(_COLL, 7, 0, "dry-run"),
            )

        monkeypatch.setattr(
            "nexus.migration.vector_etl.migrate_local", fake_migrate_local
        )
        result = CliRunner().invoke(
            migrate_vectors_cmd, ["--local-path", str(tmp_path), "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
