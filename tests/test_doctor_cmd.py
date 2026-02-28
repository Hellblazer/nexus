"""nx doctor — health check command tests."""
import sys
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from nexus.cli import main


def _runner() -> CliRunner:
    return CliRunner()


# ── All credentials present, tools found ─────────────────────────────────────

def test_doctor_all_healthy() -> None:
    """When everything is present, exit code is 0."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-fake-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.serve._read_pid", return_value=12345),
        patch("nexus.commands.serve._process_running", return_value=True),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "✓" in result.output


# ── Missing credentials ──────────────────────────────────────────────────────

def test_doctor_missing_credentials_exit_1() -> None:
    """Missing credentials cause exit code 1 with signup hints."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value=None),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "✗" in result.output
    assert "CHROMA_API_KEY" in result.output
    assert "nx config init" in result.output


# ── Missing tools ────────────────────────────────────────────────────────────

def test_doctor_missing_rg() -> None:
    """Missing ripgrep is reported with actionable fix hints."""
    runner = _runner()

    def which_side_effect(name):
        if name == "rg":
            return None
        return f"/usr/bin/{name}"

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", side_effect=which_side_effect),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "not found" in result.output
    assert "hybrid search disabled" in result.output
    assert "brew install ripgrep" in result.output


# ── Server status ────────────────────────────────────────────────────────────

def test_doctor_server_not_running() -> None:
    """Reports server not running (server is optional — does not fail)."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "not running" in result.output


# ── Partial credentials ──────────────────────────────────────────────────────

def test_doctor_partial_credentials() -> None:
    """Some present, some missing — inline Fix hints shown only for missing."""
    runner = _runner()

    def cred_side_effect(key):
        return "sk-key" if key in ("chroma_api_key", "voyage_api_key") else None

    with (
        patch("nexus.commands.doctor.get_credential", side_effect=cred_side_effect),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    # Missing ones should be listed with fix hints
    assert "CHROMA_TENANT" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "nx config set chroma_tenant" in result.output
    assert "nx config set anthropic_api_key" in result.output
    # Present keys should show as set, not have fix hints
    assert "nx config set chroma_api_key" not in result.output
    assert "nx config set voyage_api_key" not in result.output


# ── Python version check ─────────────────────────────────────────────────────

def test_doctor_python_version_shown() -> None:
    """Python version is always reported in output."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=12345),
        patch("nexus.commands.serve._process_running", return_value=True),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "Python" in result.output
    assert "3.12" in result.output


def test_doctor_python_version_too_old_fails() -> None:
    """Exit code 1 and fix hint when Python < 3.12."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor._python_ok", return_value=(False, "3.11.0")),
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=12345),
        patch("nexus.commands.serve._process_running", return_value=True),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "✗" in result.output
    assert "3.12" in result.output
    assert "python.org" in result.output


# ── Inline fix hints ──────────────────────────────────────────────────────────

def test_doctor_missing_credential_shows_inline_fix() -> None:
    """Each missing credential shows an inline Fix: hint with nx config set command."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value=None),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "nx config set chroma_api_key" in result.output
    assert "nx config set voyage_api_key" in result.output
    assert "nx config set anthropic_api_key" in result.output
    assert "trychroma.com" in result.output
    assert "voyageai.com" in result.output
    assert "console.anthropic.com" in result.output


def test_doctor_missing_rg_shows_platform_hints() -> None:
    """Missing ripgrep shows brew, apt, and URL fix hints."""
    runner = _runner()

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value=None),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "brew install ripgrep" in result.output
    assert "apt install ripgrep" in result.output
    assert "BurntSushi/ripgrep" in result.output


# ── _check helper ────────────────────────────────────────────────────────────

def test_check_helper_format() -> None:
    from nexus.commands.doctor import _check

    assert "✓" in _check("Test", True)
    assert "✗" in _check("Test", False)
    assert "some detail" in _check("Test", True, "some detail")
