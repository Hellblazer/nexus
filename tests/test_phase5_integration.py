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
        hooks_path = Path(__file__).parent.parent / "nx" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        assert "hooks" in data

    def test_upgrade_auto_is_first_session_start_hook(self) -> None:
        hooks_path = Path(__file__).parent.parent / "nx" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        startup_hooks = next(
            h["hooks"]
            for h in data["hooks"]["SessionStart"]
            if "startup" in h["matcher"]
        )
        assert startup_hooks[0]["command"] == "nx upgrade --auto"
        assert startup_hooks[0]["timeout"] == 30


# ── MCP version check tests ────────────────────────────────────────────────


class TestMcpVersionCheck:
    def test_no_db_file_no_error(self) -> None:
        from nexus.mcp_infra import check_version_compatibility

        with patch(
            "nexus.mcp_infra.default_db_path",
            return_value=Path("/nonexistent/memory.db"),
        ):
            check_version_compatibility()  # should not raise

    def test_version_match_no_warning(self, tmp_path: Path) -> None:
        from nexus.mcp_infra import check_version_compatibility

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _nexus_version VALUES ('cli_version', '4.1.2')"
        )
        conn.commit()
        conn.close()

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=db_path),
            patch("nexus.mcp_infra._pkg_version", return_value="4.1.2", create=True),
            patch("importlib.metadata.version", return_value="4.1.2"),
        ):
            check_version_compatibility()  # no warning

    def test_patch_divergence_no_warning(self, tmp_path: Path) -> None:
        from nexus.mcp_infra import check_version_compatibility

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _nexus_version VALUES ('cli_version', '4.1.2')"
        )
        conn.commit()
        conn.close()

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=db_path),
            patch("importlib.metadata.version", return_value="4.1.3"),
        ):
            check_version_compatibility()  # patch only — no warning

    def test_minor_version_divergence_warns(self, tmp_path: Path) -> None:
        """Minor version mismatch should emit a structured warning."""
        import structlog

        from nexus.mcp_infra import check_version_compatibility

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE _nexus_version (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO _nexus_version VALUES ('cli_version', '4.1.2')"
        )
        conn.commit()
        conn.close()

        with (
            patch("nexus.mcp_infra.default_db_path", return_value=db_path),
            patch("importlib.metadata.version", return_value="4.2.0"),
            patch("structlog.get_logger") as mock_get_logger,
        ):
            mock_log = mock_get_logger.return_value
            check_version_compatibility()
            mock_log.warning.assert_called_once()
            call_kwargs = mock_log.warning.call_args
            assert "version_mismatch" in str(call_kwargs)

    def test_exception_does_not_block(self) -> None:
        from nexus.mcp_infra import check_version_compatibility

        with patch(
            "nexus.mcp_infra.default_db_path", side_effect=RuntimeError("boom")
        ):
            check_version_compatibility()  # should not raise


# ── doctor --check-schema tests ─────────────────────────────────────────────


class TestDoctorCheckSchema:
    def test_no_db_file(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "nonexistent" / "memory.db"
        with patch("nexus.commands._helpers.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_healthy_schema(self, runner: CliRunner, tmp_path: Path) -> None:
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")
        conn.close()

        with patch("nexus.commands._helpers.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0
        assert "passed" in result.output.lower()

    def test_missing_version_table(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        with patch("nexus.commands._helpers.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0
        # Should report issues
        assert "nx upgrade" in result.output.lower() or _WARN_CHAR in result.output


_WARN_CHAR = "\u2717"  # ✗
