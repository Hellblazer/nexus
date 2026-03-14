"""nx doctor — health check command tests."""
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from nexus.cli import main

SENTINEL_BEGIN = "# >>> nexus managed begin >>>"


def _runner() -> CliRunner:
    return CliRunner()


# ── All credentials present, tools found ─────────────────────────────────────

def test_doctor_all_healthy() -> None:
    """When everything is present, exit code is 0."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-fake-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "✓" in result.output


# ── Missing credentials ──────────────────────────────────────────────────────

def test_doctor_missing_credentials_exit_1() -> None:
    """Missing credentials cause exit code 1 with signup hints."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value=None),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
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

    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", side_effect=which_side_effect),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "not found" in result.output
    assert "hybrid search disabled" in result.output
    assert "brew install ripgrep" in result.output


# ── Hooks status ──────────────────────────────────────────────────────────────

def test_doctor_hooks_section_present() -> None:
    """nx doctor output includes a hooks section (shows 'git hooks' in output)."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "git hooks" in result.output


def test_doctor_hooks_no_repos_registered() -> None:
    """When no repos in registry, doctor shows 'no repos registered' message."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "no repos registered" in result.output
    assert "nx index repo" in result.output
    # Always ✓ — never causes exit 1
    assert "✓ git hooks" in result.output


def test_doctor_hooks_installed() -> None:
    """When hooks are installed, doctor shows tick with repo path and hook names."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = ["/some/repo"]

    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)
        for name in ("post-commit", "post-merge", "post-rewrite"):
            (hooks_dir / name).write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\nnx index repo ...\n")

        with (
            patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
            patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
            patch("nexus.commands.doctor._effective_hooks_dir", return_value=hooks_dir),
            patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
        ):
            result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "✓ git hooks" in result.output
    assert "/some/repo" in result.output
    assert "post-commit" in result.output


def test_doctor_hooks_not_installed() -> None:
    """When hooks are missing, doctor shows tick (non-fatal) with 'not installed' and Fix hint."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = ["/some/repo"]

    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)  # empty — no hook files

        with (
            patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
            patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
            patch("nexus.commands.doctor._effective_hooks_dir", return_value=hooks_dir),
            patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
        ):
            result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0  # Always non-fatal
    assert "✓ git hooks" in result.output
    assert "not installed" in result.output
    assert "nx hooks install /some/repo" in result.output


def test_doctor_hooks_check_always_nonfatal() -> None:
    """Hooks check is always ✓ (non-fatal), even when hooks are missing."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = ["/some/repo"]

    with tempfile.TemporaryDirectory() as td:
        hooks_dir = Path(td)  # empty — no hook files

        with (
            patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
            patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
            patch("nexus.commands.doctor._effective_hooks_dir", return_value=hooks_dir),
            patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
        ):
            result = runner.invoke(main, ["doctor"])

    # Credentials all set, tools found — only hooks missing, which is non-fatal
    assert result.exit_code == 0
    assert "✗ git hooks" not in result.output


def test_doctor_hooks_exception_does_not_propagate() -> None:
    """Exception in hooks check is swallowed — doctor still exits 0 when creds are ok."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = ["/some/repo"]

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor._effective_hooks_dir",
              side_effect=RuntimeError("git error")),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "git hooks" in result.output


# ── Index log check ───────────────────────────────────────────────────────────

def test_doctor_index_log_shows_path() -> None:
    """Doctor output contains the index log label."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "index log" in result.output
    assert "✓ index log" in result.output


def test_doctor_index_log_not_created_yet() -> None:
    """When index.log does not exist, doctor reports 'not created yet'."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        # Point HOME to a temp dir that has no index.log
        fake_home = Path(tmpdir)
        (fake_home / ".config" / "nexus").mkdir(parents=True, exist_ok=True)
        # Do not create index.log — it should not exist

        with (
            patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
            patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
            patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
            patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
        ):
            # Patch Path.home so the log_path and registry_path use our tmp dir
            with patch.object(Path, "home", return_value=fake_home):
                result = runner.invoke(main, ["doctor"])

    assert "index log" in result.output
    assert "not created yet" in result.output


# ── No serve import ───────────────────────────────────────────────────────────

def test_doctor_does_not_mention_serve_start() -> None:
    """doctor output does NOT mention 'nx serve start' (serve check removed)."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "nx serve start" not in result.output
    assert "Nexus server" not in result.output


# ── Partial credentials ──────────────────────────────────────────────────────

def test_doctor_partial_credentials() -> None:
    """Some present, some missing — inline Fix hints shown only for missing."""
    runner = _runner()

    def cred_side_effect(key):
        return "sk-key" if key in ("chroma_api_key", "voyage_api_key") else None

    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", side_effect=cred_side_effect),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    # CHROMA_TENANT is optional — shown informationally but no fix hint
    assert "CHROMA_TENANT" in result.output
    assert "nx config set chroma_tenant" not in result.output
    # Missing CHROMA_DATABASE should produce a fix hint
    assert "nx config set chroma_database" in result.output
    # Present keys should show as set, not have fix hints
    assert "nx config set chroma_api_key" not in result.output
    assert "nx config set voyage_api_key" not in result.output


# ── Python version check ─────────────────────────────────────────────────────

def test_doctor_python_version_shown() -> None:
    """Python version is always reported in output."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "Python" in result.output
    assert "3.12" in result.output


def test_doctor_python_version_too_old_fails() -> None:
    """Exit code 1 and fix hint when Python < 3.12."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor._python_ok", return_value=(False, "3.11.0")),
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
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
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value=None),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/tool"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "nx config set chroma_api_key" in result.output
    assert "nx config set voyage_api_key" in result.output
    assert "trychroma.com" in result.output
    assert "voyageai.com" in result.output


def test_doctor_missing_rg_shows_platform_hints() -> None:
    """Missing ripgrep shows brew, apt, and URL fix hints."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value=None),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "brew install ripgrep" in result.output
    assert "apt install ripgrep" in result.output
    assert "BurntSushi/ripgrep" in result.output


# ── Single-database check ─────────────────────────────────────────────────────

def test_doctor_single_db_calls_cloud_client() -> None:
    """doctor checks the single database (reachability + pipeline + pagination)."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    mock_client = MagicMock()
    mock_client.list_collections.return_value = []

    import chromadb.errors
    def cloud_client_side_effect(**kwargs):
        db_name = kwargs.get("database", "")
        # Old layout probe for {base}_code should fail
        if db_name.endswith("_code"):
            raise chromadb.errors.NotFoundError("probe: not found")
        return mock_client

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient",
              side_effect=cloud_client_side_effect),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "reachable" in result.output


def test_doctor_single_db_unreachable_fails_with_fix_hint() -> None:
    """When the database is unreachable, exit code 1 and fix hint shown."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient",
              side_effect=RuntimeError("connection refused")),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "not reachable" in result.output
    assert "nx config init" in result.output


def test_doctor_single_db_error_does_not_expose_exception_text() -> None:
    """doctor check prints 'not reachable' without raw exception detail."""
    runner = _runner()
    mock_reg = MagicMock()
    mock_reg.all.return_value = []

    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", return_value="/usr/bin/rg"),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient",
              side_effect=RuntimeError("HTTP 401: invalid api_key SUPERSECRET")),
    ):
        result = runner.invoke(main, ["doctor"])

    assert "SUPERSECRET" not in result.output
    assert "not reachable" in result.output


# ── bd (beads) ───────────────────────────────────────────────────────────────

def test_doctor_missing_bd_does_not_fail() -> None:
    """Missing bd is reported as informational — does not cause exit code 1."""
    runner = _runner()

    def which_side_effect(name):
        if name == "bd":
            return None
        return f"/usr/bin/{name}"

    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", side_effect=which_side_effect),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0
    assert "bd (beads" in result.output
    assert "not found" in result.output
    assert "BeadsProject/beads" in result.output


def test_doctor_missing_uv_does_not_fail() -> None:
    """uv is an install-time tool and is not checked by doctor at all."""
    runner = _runner()

    def which_side_effect(name):
        if name == "uv":
            return None
        return f"/usr/bin/{name}"

    mock_reg = MagicMock()
    mock_reg.all.return_value = []
    with (
        patch("nexus.commands.doctor.get_credential", return_value="sk-key"),
        patch("nexus.commands.doctor.shutil.which", side_effect=which_side_effect),
        patch("nexus.commands.doctor.RepoRegistry", return_value=mock_reg),
        patch("nexus.commands.doctor.chromadb.CloudClient", return_value=MagicMock()),
    ):
        result = runner.invoke(main, ["doctor"])

    assert result.exit_code == 0


# ── _check helper ────────────────────────────────────────────────────────────

def test_check_helper_format() -> None:
    from nexus.commands.doctor import _check

    assert "✓" in _check("Test", True)
    assert "✗" in _check("Test", False)
    assert "some detail" in _check("Test", True, "some detail")
