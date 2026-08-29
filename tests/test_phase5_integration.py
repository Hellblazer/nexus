# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Phase 5 integration points (RDR-076).

hooks.json validation, MCP version check, doctor --check-schema.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# NO _clear_upgrade_done fixture: the ``_upgrade_done`` fast-path set died
# with ``nexus/db/migrations.py`` (RDR-158 P4 Stage 4, nexus-i711w).


# ── hooks.json tests ────────────────────────────────────────────────────────


class TestHooksJson:
    def test_valid_json(self) -> None:
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        assert "hooks" in data

    def test_upgrade_auto_is_first_session_start_hook(self) -> None:
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        startup_hooks = next(
            h["hooks"]
            for h in data["hooks"]["SessionStart"]
            if "startup" in h["matcher"]
        )
        # The first hook must start with `nx upgrade --auto`.  The 4.2.1
        # fallback appends a helpful error message for older CLIs.
        assert startup_hooks[0]["command"].startswith("nx upgrade --auto")
        assert startup_hooks[0]["timeout"] == 30


# ── MCP version check tests ────────────────────────────────────────────────


class TestMcpVersionCheck:
    """What remains of the CLI-side startup version check.

    The CLI <-> T2 schema-drift arm read the stored ``_nexus_version`` over the
    daemon's ``database.hello`` op (RDR-120 P4) and retired with the daemon
    (nexus-i711w Stage 2 sub-stage B). Its four tests went with it, and NOT only
    because one turned red: all four patched ``mcp_infra.default_db_path``,
    which ``check_version_compatibility`` no longer calls, so the three "no
    warning" ones had become vacuous — they would have kept passing against a
    function that no longer did anything they described.

    The never-block contract below is the surviving part, re-pointed at a call
    site the function still makes.
    """

    def test_exception_does_not_block(self) -> None:
        """A failure anywhere in the check must not break MCP startup.

        Patches ``importlib.metadata.version`` — reached on EVERY invocation —
        rather than the retired ``default_db_path`` gate, so the try/except is
        genuinely exercised instead of the patch landing on dead code.
        """
        from nexus.mcp_infra import check_version_compatibility

        with patch("importlib.metadata.version", side_effect=RuntimeError("boom")):
            check_version_compatibility()  # should not raise


class TestPluginCliVersionCheck:
    """Plugin↔CLI drift detection at MCP server startup.

    The MCP server is the single binding point between the Claude Code
    plugin and the conexus CLI (``nx-mcp`` / ``nx-mcp-catalog`` are
    conexus entry points). On startup, ``check_version_compatibility``
    reads the plugin manifest at ``${CLAUDE_PLUGIN_ROOT}/.claude-plugin/
    plugin.json`` and warns on minor or major divergence from the
    installed CLI.
    """

    def _write_plugin_manifest(self, root: Path, version: str, name: str = "conexus") -> None:
        """Plant a plugin.json for the version-check tests.

        nexus-mkj6u: the default plugin name is the new ``conexus`` so
        the version-check tests don't pick up the plugin-name-mismatch
        warning as collateral damage. Tests that specifically want to
        exercise the rename path pass name='nx' explicitly.
        """
        manifest_dir = root / ".claude-plugin"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "plugin.json").write_text(
            json.dumps({"name": name, "version": version})
        )

    def test_no_plugin_root_silent(self, tmp_path: Path, monkeypatch) -> None:
        """No CLAUDE_PLUGIN_ROOT env → plugin check is silent (CLI usage)."""
        from nexus.mcp_infra import check_version_compatibility
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            mock_get_logger.return_value.warning.assert_not_called()

    def test_plugin_version_matches_cli_no_warning(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.mcp_infra import check_version_compatibility

        plugin_root = tmp_path / "plugin"
        self._write_plugin_manifest(plugin_root, "4.9.2")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            mock_get_logger.return_value.warning.assert_not_called()

    def test_patch_divergence_no_warning(self, tmp_path: Path, monkeypatch) -> None:
        from nexus.mcp_infra import check_version_compatibility

        plugin_root = tmp_path / "plugin"
        self._write_plugin_manifest(plugin_root, "4.9.1")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            mock_get_logger.return_value.warning.assert_not_called()

    def test_cli_newer_warns_with_plugin_update_hint(self, tmp_path: Path, monkeypatch) -> None:
        """CLI is at a newer minor version → user should run /plugin update."""
        from nexus.mcp_infra import check_version_compatibility

        plugin_root = tmp_path / "plugin"
        self._write_plugin_manifest(plugin_root, "4.9.0")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.10.0"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            mock_log = mock_get_logger.return_value
            mock_log.warning.assert_called_once()
            event, kwargs = mock_log.warning.call_args.args[0], mock_log.warning.call_args.kwargs
            assert event == "plugin_cli_version_mismatch"
            assert kwargs["cli_version"] == "4.10.0"
            assert kwargs["plugin_version"] == "4.9.0"
            assert "/plugin update" in kwargs["hint"]

    def test_plugin_newer_warns_with_uv_upgrade_hint(self, tmp_path: Path, monkeypatch) -> None:
        """Plugin is at a newer minor version → user should run uv tool upgrade conexus."""
        from nexus.mcp_infra import check_version_compatibility

        plugin_root = tmp_path / "plugin"
        self._write_plugin_manifest(plugin_root, "4.10.0")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            mock_log = mock_get_logger.return_value
            mock_log.warning.assert_called_once()
            kwargs = mock_log.warning.call_args.kwargs
            assert "uv tool upgrade conexus" in kwargs["hint"]

    def test_plugin_newer_hint_follows_the_layout(self, tmp_path: Path, monkeypatch) -> None:
        """nexus-utpuw.13: on a generation box the hint must name the
        installer that actually upgrades it. conftest fences $HOME, so the
        test above only ever sees the legacy branch."""
        from nexus.mcp_infra import check_version_compatibility
        from tests import _generation_layout

        plugin_root = tmp_path / "plugin"
        self._write_plugin_manifest(plugin_root, "4.10.0")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        _generation_layout.build(tmp_path, monkeypatch)

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()
            kwargs = mock_get_logger.return_value.warning.call_args.kwargs
            assert "nx self install" in kwargs["hint"]
            assert "uv tool" not in kwargs["hint"]

    def test_corrupt_manifest_silent(self, tmp_path: Path, monkeypatch) -> None:
        """Corrupt plugin.json must not crash MCP startup."""
        from nexus.mcp_infra import check_version_compatibility

        plugin_root = tmp_path / "plugin"
        manifest_dir = plugin_root / ".claude-plugin"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "plugin.json").write_text("{not-json{{{")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=tmp_path / "no.db"),
            patch("importlib.metadata.version", return_value="4.9.2"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            check_version_compatibility()  # must not raise
            mock_get_logger.return_value.warning.assert_not_called()


# ── doctor --check-schema tests ─────────────────────────────────────────────


# ── GH #252: nx doctor --check-taxonomy ─────────────────────────────────────


class TestDoctorCheckTaxonomy:
    """``nx doctor --check-taxonomy`` validates the invariant
    ``topic_links`` ≡ aggregate of ``topic_assignments(assigned_by='projection')``.
    """

    @staticmethod
    def _service_reports(report: dict):
        """Stub the engine's /links/drift answer."""
        store = MagicMock()
        store.get_link_drift.return_value = report
        return patch(
            "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore",
            return_value=store,
        )

    def test_service_clean_exits_zero(self, runner: CliRunner) -> None:
        with self._service_reports(
            {"projection_total": 986, "drift_count": 0, "rows": []}
        ):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code == 0, result.output
        assert "986" in result.output
        # MUST NOT pass via the legacy fallback. These three assertions exist
        # because the first version of this test DID: the service branch threw
        # UnboundLocalError on a mis-scoped import, fell through to the SQLite
        # census, and "invariant holds" matched that path's wording too. A
        # green test proved nothing about the branch it named.
        assert "Engine check unavailable" not in result.output, result.output
        assert "frozen SQLite" not in result.output, result.output
        assert "legacy source" not in result.output, result.output

    def test_service_drift_exits_nonzero_and_names_rows(
        self, runner: CliRunner
    ) -> None:
        """The verdict comes from the ENGINE, not the frozen SQLite source.

        This is the whole point of nexus-ypori: before it, this check read a
        29-day-old migration relic and reported its rows as live faults while
        the store the system actually writes went unexamined.
        """
        with self._service_reports({
            "projection_total": 986,
            "drift_count": 2,
            "rows": [
                {"topic_id": 1559, "label": None, "collection": "docs__x"},
                {"topic_id": 1560, "label": "named", "collection": None},
            ],
        }):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code != 0, result.output
        assert "2/986" in result.output
        assert "Engine check unavailable" not in result.output, result.output
        assert "(unlabelled id=1559)" in result.output
        assert "named" in result.output
        assert "[docs__x]" in result.output
        # the remedy IS correct here — it rebuilds the Postgres view this
        # verdict was computed from
        assert "nx taxonomy project" in result.output
        # and the legacy census must not have run
        assert "frozen SQLite migration source" not in result.output

    def test_service_truncates_the_row_list_but_not_the_count(
        self, runner: CliRunner
    ) -> None:
        """drift_count is exact; rows are capped engine-side."""
        with self._service_reports({
            "projection_total": 100,
            "drift_count": 37,
            "rows": [
                {"topic_id": i, "label": f"t{i}", "collection": None}
                for i in range(1, 11)
            ],
        }):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code != 0
        assert "37/100" in result.output
        assert "… 27 more" in result.output
        assert "Engine check unavailable" not in result.output, result.output

    def test_engine_without_the_route_says_so_and_does_not_mask_it(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A 404 from an older engine must surface AS a 404.

        Regression pin. The first version of this branch referenced
        ``suppress`` before its import, so the cleanup in ``finally`` raised
        UnboundLocalError *while handling* the 404 and REPLACED it — the
        operator was told "cannot access local variable 'suppress'" when the
        real answer was "your engine predates this route". A masked cause is
        worse than a loud one, and the mock-only tests above could not see it
        because none of them made the call raise.
        """
        import httpx

        db_path = tmp_path / "memory.db"  # unread since 2026-08-29: the check is engine-only
        store = MagicMock()
        store.get_link_drift.side_effect = httpx.HTTPStatusError(
            "HttpTaxonomyStore./v1/taxonomy/links/drift failed: HTTP 404: not found",
            request=MagicMock(), response=MagicMock(),
        )
        with patch(
            "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore",
            return_value=store,
        ), patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        # 2026-08-29: no frozen-source fallback exists any more (there is no
        # path back to the Chroma/SQLite era) — an engine without the route is
        # UNVERIFIABLE, exit 2, never a pass.
        assert result.exit_code == 2, result.output
        assert "no /links/drift route" in result.output, result.output
        assert "suppress" not in result.output, (
            f"the cleanup error masked the real cause:\n{result.output}"
        )
        assert "frozen SQLite" not in result.output

    def test_no_engine_at_all_is_unverifiable_exit_2(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A transport failure (no engine answered) is exit 2 with a named
        remedy. Before 2026-08-29 this branch read a frozen SQLite file and
        could report 'nothing to check' at exit 0 on a fresh HOME."""
        import httpx

        db_path = tmp_path / "memory.db"  # unread since 2026-08-29: the check is engine-only
        store = MagicMock()
        store.get_link_drift.side_effect = httpx.ConnectError("connection refused", request=MagicMock())
        with patch(
            "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore",
            return_value=store,
        ), patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code == 2, result.output
        assert "no engine answered" in result.output, result.output
        assert "nothing to check" not in result.output
        assert "frozen SQLite" not in result.output

    def test_engine_side_error_fails_loud_never_reads_as_clean(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """nexus-b1v9z part A: a REACHABLE engine that answers with a real
        error (HTTP 500, not the 404 "route doesn't exist yet" case) is a
        genuine engine-side failure, not "no engine to ask". The old broad
        ``except Exception`` treated it identically to "unreachable" and
        fell through to the frozen SQLite census -- which, on a fresh HOME
        with no local db file, reported "nothing to check" at exit 0. That
        is the exact false-green nexus-ypori's engine-first fix was meant
        to end: an engine-side fault must be the verdict, not silently
        swallowed by a fallback meant for the no-engine-at-all case."""
        import httpx

        db_path = tmp_path / "nonexistent" / "memory.db"  # fresh HOME: no local db at all
        store = MagicMock()
        store.get_link_drift.side_effect = httpx.HTTPStatusError(
            "HttpTaxonomyStore./v1/taxonomy/links/drift failed: HTTP 500: internal error",
            request=MagicMock(), response=MagicMock(),
        )
        with patch(
            "nexus.db.t2.http_taxonomy_store.HttpTaxonomyStore",
            return_value=store,
        ), patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code != 0, result.output
        assert "500" in result.output
        # the false-green this test exists to close:
        assert "nothing to check" not in result.output, result.output
        assert "frozen SQLite migration source" not in result.output, result.output


class TestDoctorTrimTelemetry:
    """``nx doctor --trim-telemetry [--days N]`` — CLI contract over the
    engine-side trim.

    Converted from the old local-``memory.db`` pinned form (the SQLite arm
    died in nexus-i711w Stage 2 sub-stage A): the verb routes to
    ``HttpTelemetryStore`` and trim row-selection semantics are engine-side,
    so these tests pin flag wiring + per-table output rendering against a
    spy store. The ``local_t2_backend`` pin was removed with the fixture in
    the Stage 5 sweep.

    SERVICE HALF IS OWNED: tests/test_false_clean_diagnostics_service_mode.py
    ::test_trim_routes_to_the_service_and_never_opens_sqlite, plus the
    unresolvable-endpoint and mid-call transport-error cases in the same file.
    """

    def _spy_and_trim(
        self, runner: CliRunner, *, trim_days: int | None,
    ) -> tuple["object", "object"]:
        """Run the trim against a spy HttpTelemetryStore (nexus-i711w Stage 2
        sub-stage A: the verb's SQLite arm died; trim row-selection semantics
        are engine-side now, so the CLI contract pinned here is flag wiring +
        per-table output rendering)."""
        from unittest.mock import MagicMock

        spy = MagicMock()
        spy.trim_search_telemetry.return_value = 1
        spy.trim_hook_failures.return_value = 0
        args = ["doctor", "--trim-telemetry"]
        if trim_days is not None:
            args += ["--days", str(trim_days)]
        with patch(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            return_value=spy,
        ):
            result = runner.invoke(main, args)
        return result, spy

    def test_trims_rows_older_than_default_30d(
        self, runner: CliRunner,
    ) -> None:
        """Default 30d retention reaches the store as days=30; count rendered."""
        result, spy = self._spy_and_trim(runner, trim_days=None)
        assert result.exit_code == 0, result.output
        spy.trim_search_telemetry.assert_called_once_with(days=30, dry_run=False)
        spy.trim_hook_failures.assert_called_once_with(days=30, dry_run=False)
        assert "Trimmed 1 search_telemetry" in result.output

    def test_aggressive_retention_days_7(
        self, runner: CliRunner,
    ) -> None:
        """``--days 7`` is passed through to both engine-side trims."""
        result, spy = self._spy_and_trim(runner, trim_days=7)
        assert result.exit_code == 0, result.output
        spy.trim_search_telemetry.assert_called_once_with(days=7, dry_run=False)
        spy.trim_hook_failures.assert_called_once_with(days=7, dry_run=False)

    def test_dry_run_previews_the_count_and_says_would_trim(
        self, runner: CliRunner,
    ) -> None:
        """``--trim-telemetry --dry-run`` reports the preview count without
        deleting — the search_telemetry trim-preview gap this closes."""
        from unittest.mock import MagicMock

        spy = MagicMock()
        spy.trim_search_telemetry.return_value = 4
        spy.trim_hook_failures.return_value = 2
        # nexus-5uoxu: stub the dry-run engine-version gate as satisfied —
        # this test is about the preview plumbing, not the belt (the gate
        # has its own suite in test_false_clean_diagnostics_service_mode).
        probe_resp = MagicMock()
        probe_resp.json.return_value = {"release_version": "0.1.81"}
        probe_resp.raise_for_status.return_value = None
        with patch(
            "nexus.db.t2.http_telemetry_store.HttpTelemetryStore",
            return_value=spy,
        ), patch(
            "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
            return_value=("http://127.0.0.1:1", "tk"),
        ), patch("httpx.get", return_value=probe_resp):
            result = runner.invoke(
                main, ["doctor", "--trim-telemetry", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        spy.trim_search_telemetry.assert_called_once_with(days=30, dry_run=True)
        spy.trim_hook_failures.assert_called_once_with(days=30, dry_run=True)
        assert "Would trim 4 search_telemetry" in result.output
        assert "Would trim 2 hook_failures" in result.output
        assert "Trimmed" not in result.output

    def test_empty_table_is_safe(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Trim on an empty table is a no-op (zero deletions reported)."""
        # RDR-158 P4 Stage 4 (nexus-i711w): frozen-DDL seed replaces the
        # deleted apply_pending chain.
        from tests._t2_fixture_ops import bootstrap_migration_source

        db_path = tmp_path / "memory.db"
        bootstrap_migration_source(db_path)

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--trim-telemetry"])
        assert result.exit_code == 0, result.output
        assert "Trimmed 0 search_telemetry" in result.output

    def test_rejects_zero_days(self, runner: CliRunner) -> None:
        """``--days 0`` fails the click.IntRange(min=1) validator."""
        result = runner.invoke(
            main, ["doctor", "--trim-telemetry", "--days", "0"],
        )
        assert result.exit_code != 0

    # test_no_db_file_handled_gracefully DELETED (nexus-i711w Stage 2
    # sub-stage A): the "T2 database not found — nothing to trim" arm was a
    # SQLite-mode gate; the verb now always trims the engine-side tables and
    # a missing local file is meaningless to it.


_WARN_CHAR = "\u2717"  # ✗
