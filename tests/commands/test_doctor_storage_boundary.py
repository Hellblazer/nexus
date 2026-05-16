# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx doctor --check-storage-boundary`` (RDR-112 §5, nexus-b7o1).

The lint AST-scans ``src/nexus/**/*.py`` for direct ``sqlite3.connect``
callers outside ``src/nexus/daemon/``. Severity is environment-aware:
advisory (exit 0) when ``NX_STORAGE_MODE`` is unset, hard fail (exit 2)
under ``NX_STORAGE_MODE=daemon``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands import doctor as doctor_cmd


def _make_synthetic_pkg(tmp_path: Path, *, with_violation: bool) -> Path:
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    daemon = pkg / "daemon"
    daemon.mkdir()
    (daemon / "__init__.py").write_text("")
    # Daemon-internal sqlite3.connect is ALWAYS allowed.
    (daemon / "service.py").write_text(
        "import sqlite3\n"
        "def open_db(path):\n"
        "    return sqlite3.connect(str(path))\n"
    )
    if with_violation:
        catalog = pkg / "catalog"
        catalog.mkdir()
        (catalog / "__init__.py").write_text("")
        (catalog / "open.py").write_text(
            "import sqlite3\n"
            "def open_catalog(path):\n"
            "    return sqlite3.connect(str(path))\n"
        )
    # ``doctor.py`` location is inferred from __file__; mimic the layout.
    cmd = pkg / "commands"
    cmd.mkdir()
    (cmd / "__init__.py").write_text("")
    (cmd / "doctor.py").write_text("")
    return pkg


class TestCheckStorageBoundary:
    """Static lint surfaces direct sqlite3.connect outside daemon/."""

    def test_all_daemon_callers_exit_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=False)
        # Reroute the lint at the synthetic package root via __file__.
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        exit_code = doctor_cmd._run_check_storage_boundary()
        assert exit_code == 0

    def test_violation_advisory_when_not_daemon_mode(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=True)
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        # Advisory: exit code is 0 even with violations.
        assert result.exit_code == 0
        assert "violation" in result.output.lower()
        assert "catalog/open.py" in result.output

    def test_violation_hard_fails_under_daemon_mode(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=True)
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 2
        assert "Exiting 2" in result.output

    def test_aliased_import_is_detected(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``import sqlite3 as _sqlite3; _sqlite3.connect(...)`` counts."""
        pkg = tmp_path / "nexus"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "daemon").mkdir()
        (pkg / "daemon" / "__init__.py").write_text("")
        cmd = pkg / "commands"
        cmd.mkdir()
        (cmd / "__init__.py").write_text("")
        (cmd / "doctor.py").write_text("")
        (pkg / "outside.py").write_text(
            "import sqlite3 as _sqlite3\n"
            "def open_db(path):\n"
            "    return _sqlite3.connect(str(path))\n"
        )

        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0  # advisory
        assert "outside.py" in result.output

    def test_unrelated_attribute_calls_are_not_flagged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``obj.connect()`` where obj is not a sqlite3 import is OK."""
        pkg = tmp_path / "nexus"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "daemon").mkdir()
        (pkg / "daemon" / "__init__.py").write_text("")
        cmd = pkg / "commands"
        cmd.mkdir()
        (cmd / "__init__.py").write_text("")
        (cmd / "doctor.py").write_text("")
        (pkg / "innocent.py").write_text(
            "class Client:\n"
            "    def connect(self): pass\n"
            "def use():\n"
            "    Client().connect()\n"
        )

        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        exit_code = doctor_cmd._run_check_storage_boundary()
        assert exit_code == 0
