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
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")  # nexus-507q: post-cutover default is daemon

        exit_code = doctor_cmd._run_check_storage_boundary()
        assert exit_code == 0

    def test_violation_advisory_when_not_daemon_mode(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=True)
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")  # nexus-507q: post-cutover default is daemon

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
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")  # nexus-507q: post-cutover default is daemon

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
        monkeypatch.setenv("NX_STORAGE_MODE", "direct")  # nexus-507q: post-cutover default is daemon

        exit_code = doctor_cmd._run_check_storage_boundary()
        assert exit_code == 0


# ---------------------------------------------------------------------------
# CR-4 (nexus-e8ao): db/ root must be allowlisted to match RDR-112 §5
# ---------------------------------------------------------------------------


def _make_pkg_with_subdir_violation(
    tmp_path: Path, *, subdir: str
) -> Path:
    """Synthetic package whose violation lives at ``src/nexus/<subdir>/x.py``."""
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "daemon").mkdir()
    (pkg / "daemon" / "__init__.py").write_text("")
    cmd = pkg / "commands"
    cmd.mkdir()
    (cmd / "__init__.py").write_text("")
    (cmd / "doctor.py").write_text("")
    sub = pkg / subdir
    sub.mkdir(parents=True)
    (sub / "__init__.py").write_text("")
    (sub / "store.py").write_text(
        "import sqlite3\n"
        "def open_db(path):\n"
        "    return sqlite3.connect(str(path))\n"
    )
    return pkg


class TestCR4DbAllowlist:
    """src/nexus/db/ must be on the allowlist alongside src/nexus/daemon/."""

    def test_db_root_is_allowlisted(self, tmp_path: Path, monkeypatch) -> None:
        pkg = _make_pkg_with_subdir_violation(tmp_path, subdir="db")
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        # db/ is on the allowed side of the boundary now.
        assert result.exit_code == 0, result.output
        assert "violation" not in result.output.lower() or "no direct" in result.output.lower()

    def test_other_subdirs_still_flagged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_subdir_violation(tmp_path, subdir="other")
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 2
        assert "other/store.py" in result.output


# ---------------------------------------------------------------------------
# CR-6 (nexus-nphw): chromadb.PersistentClient detection
# ---------------------------------------------------------------------------


def _make_pkg_with_chromadb_call(
    tmp_path: Path, *, where: str, marker: str | None = None
) -> Path:
    pkg = tmp_path / "nexus"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "daemon").mkdir()
    (pkg / "daemon" / "__init__.py").write_text("")
    cmd = pkg / "commands"
    cmd.mkdir()
    (cmd / "__init__.py").write_text("")
    (cmd / "doctor.py").write_text("")
    sub_parts = where.split("/")
    sub = pkg.joinpath(*sub_parts[:-1]) if len(sub_parts) > 1 else pkg
    sub.mkdir(parents=True, exist_ok=True)
    if sub != pkg:
        (sub / "__init__.py").write_text("")
    body = "import chromadb\n"
    if marker == "preceding":
        body += (
            "def open_c(path):\n"
            "    # storage-boundary-allow: synthetic-test\n"
            "    return chromadb.PersistentClient(path=str(path))\n"
        )
    elif marker == "same_line":
        body += (
            "def open_c(path):\n"
            "    return chromadb.PersistentClient(path=str(path))  "
            "# storage-boundary-allow: synthetic-test\n"
        )
    elif marker == "walk_up":
        body += (
            "def open_c(path):\n"
            "    # storage-boundary-allow: synthetic-test\n"
            "    # extra context line one\n"
            "    # extra context line two\n"
            "    return chromadb.PersistentClient(path=str(path))\n"
        )
    else:
        body += (
            "def open_c(path):\n"
            "    return chromadb.PersistentClient(path=str(path))\n"
        )
    (sub / sub_parts[-1]).write_text(body)
    return pkg


class TestCR6ChromadbDetection:
    """chromadb.PersistentClient outside allowlist must be a violation."""

    def test_chromadb_persistent_client_outside_allowlist_flagged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(tmp_path, where="other/x.py")
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 2
        assert "PersistentClient" in result.output
        assert "other/x.py" in result.output

    def test_chromadb_in_daemon_root_is_allowed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(
            tmp_path, where="daemon/c.py"
        )
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output

    def test_chromadb_in_db_root_is_allowed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(tmp_path, where="db/c.py")
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output

    def test_innocent_persistent_client_attr_not_flagged(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Other.PersistentClient(...) (non-chromadb) must NOT match."""
        pkg = tmp_path / "nexus"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "daemon").mkdir()
        (pkg / "daemon" / "__init__.py").write_text("")
        cmd = pkg / "commands"
        cmd.mkdir()
        (cmd / "__init__.py").write_text("")
        (cmd / "doctor.py").write_text("")
        (pkg / "stranger.py").write_text(
            "import something_else as chromadb_lookalike\n"
            "def f():\n"
            "    return chromadb_lookalike.PersistentClient(path='x')\n"
        )
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# CR-6 (nexus-nphw): `# storage-boundary-allow:` markers must be honoured
# ---------------------------------------------------------------------------


class TestCR6AllowMarker:
    """A `# storage-boundary-allow:` comment on the same line, the
    immediately preceding line, or earlier in an uninterrupted comment
    preamble must suppress the violation."""

    def test_same_line_marker_suppresses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(
            tmp_path, where="other/y.py", marker="same_line"
        )
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output

    def test_preceding_line_marker_suppresses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(
            tmp_path, where="other/y.py", marker="preceding"
        )
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output

    def test_walk_up_through_comment_preamble_suppresses(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_pkg_with_chromadb_call(
            tmp_path, where="other/y.py", marker="walk_up"
        )
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output

    def test_marker_on_sqlite_call_too(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Marker support must cover the existing sqlite3 detector too."""
        pkg = tmp_path / "nexus"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "daemon").mkdir()
        (pkg / "daemon" / "__init__.py").write_text("")
        cmd = pkg / "commands"
        cmd.mkdir()
        (cmd / "__init__.py").write_text("")
        (cmd / "doctor.py").write_text("")
        other = pkg / "other"
        other.mkdir()
        (other / "__init__.py").write_text("")
        (other / "x.py").write_text(
            "import sqlite3\n"
            "def f(path):\n"
            "    # storage-boundary-allow: synthetic-test\n"
            "    return sqlite3.connect(str(path))\n"
        )

        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.setenv("NX_STORAGE_MODE", "daemon")

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd, ["--check-storage-boundary"]
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# CR-5 (nexus-b43y): --fail-on-violation flag
# ---------------------------------------------------------------------------


class TestCR5FailOnViolation:
    """The new --fail-on-violation flag elevates advisory output to exit 2
    so CI can gate on it without requiring NX_STORAGE_MODE=daemon."""

    def test_fail_on_violation_red_with_violation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=True)
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd,
            ["--check-storage-boundary", "--fail-on-violation"],
        )
        assert result.exit_code == 2
        assert "violation" in result.output.lower()

    def test_fail_on_violation_green_with_no_violations(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        pkg = _make_synthetic_pkg(tmp_path, with_violation=False)
        monkeypatch.setattr(
            doctor_cmd, "__file__", str(pkg / "commands" / "doctor.py")
        )
        monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

        runner = CliRunner()
        result = runner.invoke(
            doctor_cmd.doctor_cmd,
            ["--check-storage-boundary", "--fail-on-violation"],
        )
        assert result.exit_code == 0, result.output
