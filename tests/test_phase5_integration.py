# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Phase 5 integration points (RDR-076).

hooks.json validation, MCP version check, doctor --check-schema.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_upgrade_done() -> None:
    from nexus.db import migrations

    migrations._upgrade_done.clear()


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

    def _setup_db(self, tmp_path: Path) -> Path:
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, _current_version())
        conn.close()
        return db_path

    def test_no_db_file(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "memory.db"
        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_clean_db_no_assignments(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Empty taxonomy tables still satisfy the invariant vacuously."""
        db_path = self._setup_db(tmp_path)
        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])
        assert result.exit_code == 0
        assert "invariant holds" in result.output

    def test_drift_detected(self, runner: CliRunner, tmp_path: Path) -> None:
        """Projection assignments with co-occurring projection partner but no
        topic_links row → exit 1. nexus-346q: drift detection requires the
        co-occurring partner since a link is structurally impossible without one."""
        db_path = self._setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (42, "orphan-topic", "docs__test", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (43, "partner-topic", "docs__other", 0, "2026-01-01T00:00:00Z"),
        )
        # Two projection assignments on the same doc — a topic_links pair is
        # structurally possible here, so the absence of topic_links is real drift.
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-xyz", 42, "projection"),
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-xyz", 43, "projection"),
        )
        conn.commit()
        conn.close()

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code == 1
        assert "topic_links drift" in result.output
        assert "orphan-topic" in result.output
        assert "nx taxonomy project" in result.output

    def test_isolated_projection_topic_not_flagged(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """nexus-346q: a topic whose doc has exactly ONE projection assignment
        cannot structurally produce a topic_links row (a link needs from + to).
        The check must not flag these as drift — the 4.9.10 shakeout found 15
        such false positives out of 20 residual after a backfill.
        """
        db_path = self._setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (99, "solitary-topic", "docs__alone", 0, "2026-01-01T00:00:00Z"),
        )
        # Only ONE projection assignment for doc-solo — no pair possible.
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-solo", 99, "projection"),
        )
        conn.commit()
        conn.close()

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code == 0, result.output
        assert "invariant holds" in result.output
        assert "solitary-topic" not in result.output

    def test_co_occurring_must_also_be_projection(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """nexus-346q: a projection assignment whose only co-occurring topic
        was assigned via a non-projection path (centroid, bertopic) is still
        structurally isolated from the projection perspective — no projection
        pair means no aggregated topic_links row. Must not flag as drift.
        """
        db_path = self._setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (100, "projection-side", "docs__x", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (101, "centroid-side", "docs__y", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-mix", 100, "projection"),
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-mix", 101, "centroid"),
        )
        conn.commit()
        conn.close()

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code == 0, result.output
        assert "invariant holds" in result.output
        assert "projection-side" not in result.output

    def test_invariant_holds_with_matching_link(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Projection assignment + topic_links row referencing it → pass."""
        db_path = self._setup_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (7, "topic-a", "docs__a", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topics (id, label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (8, "topic-b", "docs__b", 0, "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES (?, ?, ?)",
            ("doc-1", 7, "projection"),
        )
        conn.execute(
            "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count) "
            "VALUES (?, ?, ?)",
            (7, 8, 1),
        )
        conn.commit()
        conn.close()

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-taxonomy"])

        assert result.exit_code == 0
        assert "invariant holds" in result.output


# ── RDR-087 Phase 2.4: nx doctor --trim-telemetry ───────────────────────────


@pytest.mark.usefixtures("local_t2_backend")
class TestDoctorTrimTelemetry:
    """``doctor --trim-telemetry`` against a REAL local memory.db.

    PINNED, not converted (nexus-aqbrk). These seed aged rows into a local
    ``search_telemetry`` and assert the local row count afterwards. In
    service mode the verb correctly routes to
    ``HttpTelemetryStore.trim_search_telemetry`` and never opens the frozen
    local file (nexus-ingey) — so the seeded rows survive and the counts
    cannot match.

    Note this class contained a VACUOUS PASS before the pin:
    ``test_empty_table_is_safe`` asserts "Trimmed 0 search_telemetry" and
    passed under the engine substrate for the wrong reason — the service
    trimmed its own empty tenant, not the local table the test seeded. The
    pin makes it test its subject again.

    SERVICE HALF IS OWNED: tests/test_false_clean_diagnostics_service_mode.py
    ::test_trim_routes_to_the_service_and_never_opens_sqlite, plus the
    unresolvable-endpoint and mid-call transport-error cases in the same file.
    """

    """``nx doctor --trim-telemetry [--days N]`` deletes ``search_telemetry``
    rows older than the retention window. Default 30d; --days validates min=1.
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
        spy.trim_search_telemetry.assert_called_once_with(days=30)
        spy.trim_hook_failures.assert_called_once_with(days=30)
        assert "Trimmed 1 search_telemetry" in result.output

    def test_aggressive_retention_days_7(
        self, runner: CliRunner,
    ) -> None:
        """``--days 7`` is passed through to both engine-side trims."""
        result, spy = self._spy_and_trim(runner, trim_days=7)
        assert result.exit_code == 0, result.output
        spy.trim_search_telemetry.assert_called_once_with(days=7)
        spy.trim_hook_failures.assert_called_once_with(days=7)

    def test_empty_table_is_safe(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Trim on an empty table is a no-op (zero deletions reported)."""
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, _current_version())
        conn.close()

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
