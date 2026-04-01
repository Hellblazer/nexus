"""Tests for the read_verification_config standalone script."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nx" / "hooks" / "scripts" / "read_verification_config.py"

DEFAULTS = {
    "on_stop": False,
    "on_close": False,
    "test_command": "",
    "lint_command": "",
    "test_timeout": 120,
}


def _run_script(
    *,
    cwd: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **(env_overrides or {})}
    return subprocess.run(
        ["python3", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(cwd) if cwd else None,
        env=env,
    )


class TestReadVerificationConfig:
    """Tests for read_verification_config.py."""

    def test_script_exists(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"

    def test_outputs_valid_json_no_nexus_yml(self, tmp_path: Path) -> None:
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data == DEFAULTS

    def test_outputs_defaults_when_no_verification_section(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("indexing:\n  code_extensions: [.sql]\n")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data == DEFAULTS

    def test_reads_on_stop_true(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["on_stop"] is True
        assert data["on_close"] is False

    def test_reads_on_close_true(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_close: true\n")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["on_close"] is True
        assert data["on_stop"] is False

    def test_reads_test_command_override(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_command: 'make check'\n"
        )
        # Also create a pyproject.toml to confirm explicit command wins over auto-detect
        (tmp_path / "pyproject.toml").write_text("")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["test_command"] == "make check"

    def test_auto_detects_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: true\n")
        (tmp_path / "pyproject.toml").write_text("")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["test_command"] == "uv run pytest"

    def test_auto_detect_skipped_when_test_command_set(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_command: 'my custom test'\n"
        )
        (tmp_path / "pyproject.toml").write_text("")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["test_command"] == "my custom test"

    def test_auto_detect_skipped_when_both_flags_false(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: false\n  on_close: false\n"
        )
        (tmp_path / "pyproject.toml").write_text("")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["test_command"] == ""

    def test_malformed_yaml_returns_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text("verification:\n  on_stop: [unclosed\n  bad: yaml:\n")
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data == DEFAULTS

    def test_respects_claude_project_dir_env(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "project"
        config_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        (config_dir / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_command: 'special command'\n"
        )
        result = _run_script(
            cwd=other_dir,
            env_overrides={"CLAUDE_PROJECT_DIR": str(config_dir)},
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["on_stop"] is True
        assert data["test_command"] == "special command"

    def test_reads_lint_command(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  lint_command: 'ruff check .'\n"
        )
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["lint_command"] == "ruff check ."

    def test_reads_test_timeout(self, tmp_path: Path) -> None:
        (tmp_path / ".nexus.yml").write_text(
            "verification:\n  on_stop: true\n  test_timeout: 300\n"
        )
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["test_timeout"] == 300

    def test_exits_zero_always(self, tmp_path: Path) -> None:
        """Script must always exit 0 regardless of errors."""
        result = _run_script(cwd=tmp_path)
        assert result.returncode == 0
