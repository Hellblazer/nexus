"""Tests for the `.nexus.yml` verification-block reader.

The reader is ``nexus.hooks.verification_config`` (bead nexus-b5ugt); the
plugin-resident ``read_verification_config.py`` it was ported from is
deleted (nexus-z9cz2), so these cases call the wheel function directly.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.hooks.verification_config import read_verification_config

DEFAULTS = {
    "on_stop": False,
    "on_close": False,
    "test_command": "",
    "lint_command": "",
    "test_timeout": 120,
}


@pytest.fixture(autouse=True)
def _no_ambient_project_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """An inherited CLAUDE_PROJECT_DIR would win over the tmp_path cwd."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


class TestReadVerificationConfig:
    """Tests for read_verification_config()."""

    def test_defaults_no_nexus_yml(self, tmp_path: Path) -> None:
        assert read_verification_config(tmp_path) == DEFAULTS

    def test_defaults_when_no_verification_section(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("indexing:\n  code_extensions: [.sql]\n")
        assert read_verification_config(tmp_path) == DEFAULTS

    def test_reads_on_stop_true(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
        data = read_verification_config(tmp_path)
        assert data["on_stop"] is True
        assert data["on_close"] is False

    def test_reads_on_close_true(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_close: true\n")
        data = read_verification_config(tmp_path)
        assert data["on_close"] is True
        assert data["on_stop"] is False

    def test_reads_test_command_override(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_command: 'make check'\n"
        )
        # Also create a pyproject.toml to confirm explicit command wins over auto-detect
        (tmp_path / "pyproject.toml").write_text("")
        assert read_verification_config(tmp_path)["test_command"] == "make check"

    def test_auto_detects_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
        (tmp_path / "pyproject.toml").write_text("")
        assert read_verification_config(tmp_path)["test_command"] == "uv run pytest"

    def test_auto_detect_skipped_when_both_flags_false(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: false\n  on_close: false\n"
        )
        (tmp_path / "pyproject.toml").write_text("")
        assert read_verification_config(tmp_path)["test_command"] == ""

    def test_malformed_yaml_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: [unclosed\n  bad: yaml:\n")
        assert read_verification_config(tmp_path) == DEFAULTS

    def test_non_mapping_verification_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification: [1, 2]\n")
        assert read_verification_config(tmp_path) == DEFAULTS

    def test_unknown_keys_are_not_merged(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n  bogus: 1\n")
        assert "bogus" not in read_verification_config(tmp_path)

    def test_respects_claude_project_dir_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "project"
        config_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        (config_dir / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_command: 'special command'\n"
        )
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(config_dir))
        data = read_verification_config(other_dir)
        assert data["on_stop"] is True
        assert data["test_command"] == "special command"

    def test_reads_lint_command(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  lint_command: 'ruff check .'\n"
        )
        assert read_verification_config(tmp_path)["lint_command"] == "ruff check ."

    def test_reads_test_timeout(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_timeout: 300\n"
        )
        assert read_verification_config(tmp_path)["test_timeout"] == 300
