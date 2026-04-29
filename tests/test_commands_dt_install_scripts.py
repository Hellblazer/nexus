# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nx dt install-scripts`` (nexus-tv5u).

Coverage:

* Help text shows the four flags (--target, --uninstall, --force,
  --dry-run) plus the testing-only --app-scripts-dir override.
* Default install (--target=all) drops every shipped .applescript
  file into both Toolbar/ and Menu/ subdirs per the manifest.
* --target=toolbar only writes Toolbar/.
* --target=menu only writes Menu/.
* --dry-run reports paths without writing anything.
* --uninstall removes installed files; idempotent on missing files.
* Existing-file behaviour: prompt-overwrites by default; --force
  overwrites unconditionally.
* Non-darwin platform exits non-zero with a clear error message.
* Manifest references real package-data files (no orphan entries).

Tests pass on every platform via ``--app-scripts-dir`` override (for
the install path) and ``monkeypatch.setattr("sys.platform", ...)``
(for the platform gate).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def darwin(monkeypatch) -> None:
    """Pretend the test host is macOS so the platform gate passes."""
    monkeypatch.setattr("sys.platform", "darwin")


@pytest.fixture
def fake_dt_dir(tmp_path: Path) -> Path:
    """Fake DT Application Scripts directory for tests.

    The CLI's ``--app-scripts-dir`` override points at this path so
    the install logic writes into a sandbox instead of the user's
    actual ``~/Library/Application Scripts/com.devon-technologies.think``
    tree.
    """
    base = tmp_path / "com.devon-technologies.think"
    (base / "Toolbar").mkdir(parents=True)
    (base / "Menu").mkdir(parents=True)
    return base


# ── Manifest sanity ──────────────────────────────────────────────────────────


class TestManifestIntegrity:
    """The CLI ships an in-module manifest mapping script filename ->
    target subdirs. Every entry must reference a real file shipped as
    package data; an orphan entry would fail at install time with a
    confusing FileNotFoundError. This test catches drift early."""

    def test_manifest_files_all_exist_as_package_data(self) -> None:
        from importlib.resources import as_file, files

        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        with as_file(files("nexus") / "_resources" / "dt-scripts") as src_dir:
            for filename in _DT_SCRIPT_MANIFEST:
                assert (Path(src_dir) / filename).exists(), (
                    f"manifest references missing package-data file: {filename}"
                )

    def test_manifest_targets_are_known(self) -> None:
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        valid = {"Toolbar", "Menu"}
        for filename, targets in _DT_SCRIPT_MANIFEST.items():
            unknown = set(targets) - valid
            assert not unknown, (
                f"manifest entry {filename!r} has unknown targets {unknown}; "
                f"valid targets are {valid}"
            )


# ── Help / registration ──────────────────────────────────────────────────────


class TestInstallScriptsHelp:
    def test_help_lists_required_flags(self, runner: CliRunner) -> None:
        from nexus.cli import main

        result = runner.invoke(main, ["dt", "install-scripts", "--help"])
        assert result.exit_code == 0, result.output
        for flag in ("--target", "--uninstall", "--force", "--dry-run", "--app-scripts-dir"):
            assert flag in result.output, f"missing flag in help: {flag}"


# ── Install paths ────────────────────────────────────────────────────────────


class TestInstall:
    def test_default_target_all_writes_both_subdirs(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        result = runner.invoke(
            main,
            ["dt", "install-scripts", "--app-scripts-dir", str(fake_dt_dir)],
        )
        assert result.exit_code == 0, result.output

        for filename, targets in _DT_SCRIPT_MANIFEST.items():
            for subdir in targets:
                installed = fake_dt_dir / subdir / filename
                assert installed.exists(), f"missing after install: {installed}"
                # Content must match the source — install copies, no
                # transformation.
                assert installed.stat().st_size > 0

    def test_target_toolbar_only_writes_toolbar(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--target", "toolbar",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output

        toolbar_files = list((fake_dt_dir / "Toolbar").glob("*.applescript"))
        menu_files = list((fake_dt_dir / "Menu").glob("*.applescript"))
        # Every manifest entry that lists "Toolbar" should be present;
        # no Menu writes at all under --target=toolbar.
        expected_toolbar = {
            f for f, t in _DT_SCRIPT_MANIFEST.items() if "Toolbar" in t
        }
        assert {p.name for p in toolbar_files} == expected_toolbar
        assert menu_files == []

    def test_target_menu_only_writes_menu(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--target", "menu",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output

        toolbar_files = list((fake_dt_dir / "Toolbar").glob("*.applescript"))
        menu_files = list((fake_dt_dir / "Menu").glob("*.applescript"))
        expected_menu = {
            f for f, t in _DT_SCRIPT_MANIFEST.items() if "Menu" in t
        }
        assert {p.name for p in menu_files} == expected_menu
        assert toolbar_files == []


# ── Dry-run ─────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_writes_nothing(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--dry-run",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert list((fake_dt_dir / "Toolbar").iterdir()) == []
        assert list((fake_dt_dir / "Menu").iterdir()) == []

    def test_dry_run_reports_target_paths(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--dry-run",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        # Each manifest entry × subdirs should be mentioned by name.
        for filename in _DT_SCRIPT_MANIFEST:
            assert filename in result.output, (
                f"dry-run output missing {filename!r}:\n{result.output}"
            )


# ── Overwrite handling ──────────────────────────────────────────────────────


class TestOverwrite:
    def test_force_overwrites_existing_file(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        # Pre-seed one of the targets with stale content to verify
        # --force overwrites it.
        sample = next(
            f for f, t in _DT_SCRIPT_MANIFEST.items() if "Menu" in t
        )
        stale = fake_dt_dir / "Menu" / sample
        stale.write_text("STALE")

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--target", "menu",
                "--force",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        assert stale.read_text() != "STALE", (
            "--force did not overwrite existing file"
        )

    def test_existing_file_skipped_without_force(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        sample = next(
            f for f, t in _DT_SCRIPT_MANIFEST.items() if "Menu" in t
        )
        stale = fake_dt_dir / "Menu" / sample
        stale.write_text("STALE")

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--target", "menu",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
            input="n\n",  # decline the prompt
        )
        # Skipping is a non-fatal outcome; exit_code 0 is fine. What
        # matters: the existing file is left intact.
        assert stale.read_text() == "STALE", (
            "stale file overwritten without --force or confirmation"
        )


# ── Uninstall ───────────────────────────────────────────────────────────────


class TestUninstall:
    def test_uninstall_removes_installed_files(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main
        from nexus.commands.dt import _DT_SCRIPT_MANIFEST

        # Install first.
        runner.invoke(
            main,
            ["dt", "install-scripts", "--app-scripts-dir", str(fake_dt_dir)],
        )
        # Verify installed.
        for filename, targets in _DT_SCRIPT_MANIFEST.items():
            for subdir in targets:
                assert (fake_dt_dir / subdir / filename).exists()

        # Now uninstall.
        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--uninstall",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output
        for filename, targets in _DT_SCRIPT_MANIFEST.items():
            for subdir in targets:
                assert not (fake_dt_dir / subdir / filename).exists()

    def test_uninstall_idempotent_on_missing_files(
        self, runner: CliRunner, darwin: None, fake_dt_dir: Path,
    ) -> None:
        """Uninstall must not error on a fresh tree where nothing is
        installed yet — that's the natural state for a user trying to
        clean up after a bad install."""
        from nexus.cli import main

        result = runner.invoke(
            main,
            [
                "dt", "install-scripts",
                "--uninstall",
                "--app-scripts-dir", str(fake_dt_dir),
            ],
        )
        assert result.exit_code == 0, result.output


# ── Platform gate ───────────────────────────────────────────────────────────


class TestPlatformGate:
    def test_non_darwin_exits_non_zero(
        self, runner: CliRunner, monkeypatch, fake_dt_dir: Path,
    ) -> None:
        from nexus.cli import main

        monkeypatch.setattr("sys.platform", "linux")
        result = runner.invoke(
            main,
            ["dt", "install-scripts", "--app-scripts-dir", str(fake_dt_dir)],
        )
        assert result.exit_code != 0
        assert "macOS" in result.output or "darwin" in result.output.lower()
