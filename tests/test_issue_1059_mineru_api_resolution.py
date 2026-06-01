# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for GH #1059: resolve mineru-api from venv bin before PATH fallback.

The `_resolve_mineru_api_bin()` helper must:
  (a) Return the sys.executable-sibling candidate when it exists and is executable.
  (b) Walk up from __file__ and return the first executable bin/mineru-api found
      (covers MCP/daemon restart where the running interpreter differs).
  (c) Fall back to shutil.which() when (a) and (b) both miss.
  (d) Return None when all three miss.

Additional guards:
  - Non-executable files are REJECTED even when they exist as regular files.
  - Candidate (a) takes precedence over shutil.which() when both resolve.

The two call sites (commands/mineru.py start + pdf_extractor.py restart)
must route through the shared helper so argv[0] is the absolute resolved path.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import nexus.commands.mineru as _mineru_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executable(p: Path) -> Path:
    """Write a minimal shell script to p and set the executable bit."""
    p.write_text("#!/bin/sh\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _resolver_ctx(monkeypatch, *, sys_exe: str, file_anchor: str):
    """Context that patches both sys.executable and the module's __file__."""
    monkeypatch.setattr(sys, "executable", sys_exe)
    monkeypatch.setattr(_mineru_mod, "__file__", file_anchor)


# ---------------------------------------------------------------------------
# Resolver unit tests
# ---------------------------------------------------------------------------

class TestResolveMineruApiBin:
    """Unit tests for _resolve_mineru_api_bin()."""

    def test_venv_bin_candidate_exists(self, tmp_path: Path, monkeypatch) -> None:
        """Candidate (a): sys.executable sibling exists and is executable -> returned."""
        bin_dir = tmp_path / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        fake_exe = _make_executable(bin_dir / "mineru-api")
        fake_python = bin_dir / "python"
        fake_python.write_text("#!/bin/sh\n")

        # __file__ anchored somewhere with NO bin/mineru-api in ancestry
        _resolver_ctx(
            monkeypatch,
            sys_exe=str(fake_python),
            file_anchor=str(tmp_path / "isolated" / "lib" / "site-packages" / "nexus" / "commands" / "mineru.py"),
        )

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result == str(fake_exe)

    def test_non_executable_file_rejected(self, tmp_path: Path, monkeypatch) -> None:
        """Candidate (a): file exists but is NOT executable -> skipped, returns None."""
        bin_dir = tmp_path / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        bad_file = bin_dir / "mineru-api"
        bad_file.write_text("#!/bin/sh\n")
        bad_file.chmod(0o644)  # readable but NOT executable
        fake_python = bin_dir / "python"
        fake_python.write_text("#!/bin/sh\n")

        # __file__ anchored in isolated dir with no bin/mineru-api
        _resolver_ctx(
            monkeypatch,
            sys_exe=str(fake_python),
            file_anchor=str(tmp_path / "isolated" / "lib" / "site-packages" / "nexus" / "commands" / "mineru.py"),
        )
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result is None, (
            "non-executable candidate must be rejected even when it is_file()"
        )

    def test_venv_first_over_which(self, tmp_path: Path, monkeypatch) -> None:
        """Candidate (a) takes precedence over shutil.which when both resolve."""
        bin_dir = tmp_path / "venv" / "bin"
        bin_dir.mkdir(parents=True)
        venv_exe = _make_executable(bin_dir / "mineru-api")
        fake_python = bin_dir / "python"
        fake_python.write_text("#!/bin/sh\n")

        _resolver_ctx(
            monkeypatch,
            sys_exe=str(fake_python),
            file_anchor=str(tmp_path / "isolated" / "lib" / "site-packages" / "nexus" / "commands" / "mineru.py"),
        )
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/mineru-api")

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result == str(venv_exe), (
            f"venv-bin candidate must win over PATH; got {result!r}"
        )

    def test_file_anchored_candidate_cross_interpreter(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Candidate (b): __file__ walk finds bin/mineru-api when sys.executable's bin lacks it.

        Simulates the MCP/daemon case where the running interpreter is different
        from the conexus tool-venv. nexus's __file__ is inside the conexus venv's
        site-packages, so walking up eventually reaches the venv root's bin/.
        """
        # The conexus venv has bin/mineru-api
        conexus_venv = tmp_path / "conexus-venv"
        conexus_bin = conexus_venv / "bin"
        conexus_bin.mkdir(parents=True)
        expected = _make_executable(conexus_bin / "mineru-api")

        # Simulate mineru.py at: conexus-venv/lib/python3.12/site-packages/nexus/commands/mineru.py
        # parents[5] of .resolve() = conexus-venv  (commands -> nexus -> site-packages -> python3.12 -> lib -> conexus-venv)
        fake_file = (
            conexus_venv
            / "lib" / "python3.12" / "site-packages" / "nexus" / "commands" / "mineru.py"
        )

        # A DIFFERENT interpreter without mineru-api nearby
        other_venv_bin = tmp_path / "other-venv" / "bin"
        other_venv_bin.mkdir(parents=True)
        other_python = other_venv_bin / "python"
        other_python.write_text("#!/bin/sh\n")

        _resolver_ctx(
            monkeypatch,
            sys_exe=str(other_python),
            file_anchor=str(fake_file),
        )
        import shutil
        monkeypatch.setattr(shutil, "which", lambda name: None)

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result == str(expected), (
            f"__file__-anchored walk must find the conexus-venv bin; got {result!r}"
        )

    def test_which_fallback_when_venv_absent(self, tmp_path: Path, monkeypatch) -> None:
        """Candidates (a) and (b) miss -> falls back to shutil.which()."""
        import shutil

        other_bin = tmp_path / "other" / "bin"
        other_bin.mkdir(parents=True)
        fake_python = other_bin / "python"
        fake_python.write_text("#!/bin/sh\n")

        _resolver_ctx(
            monkeypatch,
            sys_exe=str(fake_python),
            file_anchor=str(tmp_path / "isolated" / "lib" / "site-packages" / "nexus" / "commands" / "mineru.py"),
        )
        sentinel = "/usr/local/bin/mineru-api"
        monkeypatch.setattr(shutil, "which", lambda name: sentinel)

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result == sentinel

    def test_both_absent_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        """All candidates miss -> returns None."""
        import shutil

        other_bin = tmp_path / "other" / "bin"
        other_bin.mkdir(parents=True)
        fake_python = other_bin / "python"
        fake_python.write_text("#!/bin/sh\n")

        _resolver_ctx(
            monkeypatch,
            sys_exe=str(fake_python),
            file_anchor=str(tmp_path / "isolated" / "lib" / "site-packages" / "nexus" / "commands" / "mineru.py"),
        )
        monkeypatch.setattr(shutil, "which", lambda name: None)

        result = _mineru_mod._resolve_mineru_api_bin()
        assert result is None


# ---------------------------------------------------------------------------
# Call-site integration: commands/mineru.py `start` command
# ---------------------------------------------------------------------------

class TestStartCommandUsesResolver:
    """The `nx mineru start` command must pass the resolved path as argv[0]."""

    def test_start_uses_resolver_path(self, tmp_path: Path) -> None:
        """When resolver returns an absolute path, that path is argv[0]."""
        from click.testing import CliRunner
        from nexus.cli import main

        runner = CliRunner()
        sentinel_bin = "/venv/bin/mineru-api"
        proc = MagicMock()
        proc.pid = 42
        proc.poll.return_value = None
        resp = MagicMock()
        resp.status_code = 200

        with patch("nexus.commands.mineru._resolve_mineru_api_bin",
                   return_value=sentinel_bin) as mock_resolver, \
             patch("nexus.commands.mineru.subprocess.Popen",
                   return_value=proc) as mock_popen, \
             patch("nexus.commands.mineru.httpx.get", return_value=resp), \
             patch("nexus.commands.mineru._find_free_port", return_value=8010), \
             patch("nexus.config.load_config", return_value={}), \
             patch("nexus.config.set_config_value"), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = runner.invoke(main, ["mineru", "start"])

        mock_resolver.assert_called_once()
        assert result.exit_code == 0, result.output
        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == sentinel_bin, f"expected {sentinel_bin!r} as argv[0], got {cmd[0]!r}"

    def test_start_not_found_when_resolver_returns_none(self, tmp_path: Path) -> None:
        """When resolver returns None, the command must exit non-zero with an error."""
        from click.testing import CliRunner
        from nexus.cli import main

        runner = CliRunner()
        with patch("nexus.commands.mineru._resolve_mineru_api_bin",
                   return_value=None), \
             patch("nexus.config.load_config", return_value={}), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = runner.invoke(main, ["mineru", "start"])

        assert result.exit_code != 0
        combined = result.output + (result.stderr if hasattr(result, "stderr") and result.stderr else "")
        assert "mineru-api" in combined.lower()


# ---------------------------------------------------------------------------
# Call-site integration: pdf_extractor.py restart path
# ---------------------------------------------------------------------------

class TestPdfExtractorRestartUsesResolver:
    """The pdf_extractor restart path must route through _resolve_mineru_api_bin."""

    def _make_extractor(self):
        """Return a PDFExtractor instance with restart state set up."""
        from nexus.pdf_extractor import PDFExtractor

        extractor = object.__new__(PDFExtractor)
        extractor._mineru_server_restarts = 0
        extractor._MINERU_MAX_RESTARTS = 3
        return extractor

    def test_restart_uses_resolver_path(self, tmp_path: Path) -> None:
        """_restart_mineru_server passes the resolver's path as argv[0] to Popen."""
        import subprocess as _real_subprocess

        extractor = self._make_extractor()

        sentinel_bin = "/venv/bin/mineru-api"
        proc = MagicMock()
        proc.pid = 55
        proc.poll.return_value = None
        resp = MagicMock()
        resp.status_code = 200

        pid_file = tmp_path / "mineru.pid"

        # _restart_mineru_server does `import subprocess as _sp` locally so
        # patch at the stdlib level; the local alias is the same object.
        # read_pid_file and _pid_file_path are locally imported from
        # nexus._mineru_pid, so patch there.
        with patch("nexus.commands.mineru._resolve_mineru_api_bin",
                   return_value=sentinel_bin) as mock_resolver, \
             patch.object(_real_subprocess, "Popen", return_value=proc) as mock_popen, \
             patch("nexus.pdf_extractor.httpx.get", return_value=resp), \
             patch("nexus._mineru_pid.read_pid_file", return_value=None), \
             patch("nexus.commands.mineru._find_free_port", return_value=8099), \
             patch("nexus._mineru_pid._pid_file_path",
                   return_value=pid_file):
            result = extractor._restart_mineru_server()

        mock_resolver.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert cmd[0] == sentinel_bin, (
            f"expected {sentinel_bin!r} as argv[0], got {cmd[0]!r}"
        )

    def test_restart_logs_not_found_when_resolver_returns_none(
        self, tmp_path: Path
    ) -> None:
        """When resolver returns None, restart returns False without spawning."""
        import subprocess as _real_subprocess

        extractor = self._make_extractor()

        with patch("nexus.commands.mineru._resolve_mineru_api_bin",
                   return_value=None), \
             patch.object(_real_subprocess, "Popen") as mock_popen, \
             patch("nexus._mineru_pid.read_pid_file", return_value=None):
            result = extractor._restart_mineru_server()

        assert result is False
        mock_popen.assert_not_called()
