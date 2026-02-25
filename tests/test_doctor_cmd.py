"""nx doctor — health check command tests."""
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
    """Missing ripgrep is reported."""
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
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "not found on PATH" in result.output


# ── Server status ────────────────────────────────────────────────────────────

def test_doctor_server_not_running() -> None:
    """Reports server not running."""
    runner = _runner()
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.serve._read_pid", return_value=None),
        patch("nexus.commands.serve._process_running", return_value=False),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "not running" in result.output


# ── Partial credentials ──────────────────────────────────────────────────────

def test_doctor_partial_credentials() -> None:
    """Some present, some missing — shows only missing ones."""
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
    # Missing ones should be listed
    assert "CHROMA_TENANT" in result.output
    assert "ANTHROPIC_API_KEY" in result.output
    # Present ones should not be in the missing list
    output_after_missing = result.output.split("Missing credentials")[1] if "Missing credentials" in result.output else ""
    assert "CHROMA_API_KEY" not in output_after_missing


# ── _check helper ────────────────────────────────────────────────────────────

def test_check_helper_format() -> None:
    from nexus.commands.doctor import _check

    assert "✓" in _check("Test", True)
    assert "✗" in _check("Test", False)
    assert "some detail" in _check("Test", True, "some detail")
