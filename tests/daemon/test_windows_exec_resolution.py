# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 Phase 3 fix round A (nexus-f9bgu.33, review S1): no Windows child is
resolved through the current directory.

``CreateProcess`` searches the current directory before ``System32``, and
Python 3.12's ``shutil.which`` prepends it on Windows, so a bare ``schtasks``
or ``nx`` argv[0] runs whatever ``schtasks.exe`` / ``nx.exe`` sits in the
user's working directory. Every test here injects the platform, so the Windows
arm runs on every host and none of it skip-passes.
"""
from __future__ import annotations

import ntpath
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus import upgrade_finish as uf
from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer
from nexus.util import nx_argv as nx_argv_mod
from tests._module_seam import setattr_in

SYSTEM32_SCHTASKS = ntpath.join(r"C:\Windows", "System32", "schtasks.exe")


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "win32")
    monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "win32")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")


class TestSchtasksResolution:
    def test_schtasks_is_system32_even_when_path_resolution_finds_a_planted_one(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # shutil.which on Windows finds ".\schtasks.exe" first; model that.
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *a, **k: name)
        assert installer._manager_executable("schtasks") == SYSTEM32_SCHTASKS

    def test_schtasks_is_never_the_bare_name_when_system32_has_none(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda *a, **k: None)
        setattr_in(monkeypatch, "nexus.daemon.installer", "os.access", lambda *a, **k: False)
        assert installer._manager_executable("schtasks") == SYSTEM32_SCHTASKS

    def test_systemroot_comes_from_the_environment(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SystemRoot", r"D:\WinNT")
        assert installer._manager_executable("schtasks") == r"D:\WinNT\System32\schtasks.exe"

    def test_run_manager_spawns_the_absolute_path_and_keeps_the_verb(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[list[str]] = []

        def fake(argv, **_kw):  # noqa: ANN001
            seen.append(list(argv))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(installer, "run_bounded", fake)
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *a, **k: name)
        installer._run_manager(["schtasks", "/Query", "/TN", "x"], timeout=5)
        assert seen == [[SYSTEM32_SCHTASKS, "/Query", "/TN", "x"]]  # non-vacuity: it spawned once

    def test_manager_found_reads_the_system32_path_not_path_lookup(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *a, **k: name)  # a planted one
        setattr_in(monkeypatch, "nexus.daemon.installer", "os.access", lambda path, mode: path == SYSTEM32_SCHTASKS)
        assert installer._manager_found("schtasks") is True
        setattr_in(monkeypatch, "nexus.daemon.installer", "os.access", lambda path, mode: False)
        assert installer._manager_found("schtasks") is False  # the planted one does not count

    def test_a_manager_that_does_not_exist_on_windows_keeps_the_old_resolution(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda *a, **k: None)
        setattr_in(monkeypatch, "nexus.daemon.installer", "os.access", lambda *a, **k: False)
        assert installer._manager_executable("launchctl") == "launchctl"

    def test_posix_resolution_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "linux")
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *a, **k: "/usr/bin/" + name)
        assert installer._manager_executable("systemctl") == "systemctl"


class TestNxArgv:
    def test_windows_nx_is_this_interpreter_running_the_cli_module(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\python.exe")
        assert nx_argv_mod.nx_argv("daemon", "service", "stop") == [
            r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli", "daemon", "service", "stop",
        ]

    def test_pythonw_maps_to_the_console_interpreter(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\pythonw.exe")
        assert nx_argv_mod.nx_argv("x")[0] == r"C:\tools\conexus\Scripts\python.exe"

    def test_the_cli_module_is_runnable_as_main(self) -> None:
        # The argv above runs `python -m nexus.cli`; that is only the same
        # program as the `nx` console script if the module has a main guard.
        text = (Path(__file__).parents[2] / "src" / "nexus" / "cli.py").read_text(encoding="utf-8")
        assert 'if __name__ == "__main__"' in text
        assert 'nx = "nexus.cli:main"' in (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")

    def test_posix_nx_is_the_bare_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
        assert nx_argv_mod.nx_argv("a", "b") == ["nx", "a", "b"]


class TestResolveNxBin:
    def test_windows_never_asks_which_and_never_returns_a_bare_nx(
        self, windows: None, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*_a, **_k):  # noqa: ANN002, ANN003
            raise AssertionError("shutil.which searches the cwd on Windows")

        setattr_in(monkeypatch, daemon_cmd, "shutil.which", boom)
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\python.exe")
        assert daemon_cmd._resolve_nx_bin() == [
            r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli",
        ]

    def test_posix_still_prefers_the_nx_on_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "linux")
        setattr_in(monkeypatch, daemon_cmd, "shutil.which", lambda name: "/opt/bin/nx")
        assert daemon_cmd._resolve_nx_bin() == ["/opt/bin/nx"]


class TestUpgradeFinishUsesTheResolver:
    def test_the_restart_cycle_spawns_the_interpreter_not_a_bare_nx(
        self, windows: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\python.exe")
        spawned: list[list[str]] = []

        def fake(argv, **_kw):  # noqa: ANN001
            spawned.append(list(argv))
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(uf, "run_bounded", fake)
        uf._restart_service_after_unit_reinstall(tmp_path)
        assert spawned, "the function spawned nothing: the test would pass vacuously"
        assert all(a[0] != "nx" for a in spawned)
        assert spawned[0][:3] == [r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli"]


class TestWhichOffCwd:
    """``shutil.which`` on Windows answers with a current-directory hit first; a
    result that reaches an argv must not be one (RDR-224 test review S3)."""

    CWD = r"C:\work\clone"
    TOOLS = r"C:\tools\bin"

    @pytest.fixture(autouse=True)
    def _where(self, windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
        setattr_in(monkeypatch, "nexus.util.nx_argv", "os.getcwd", lambda: self.CWD)
        setattr_in(monkeypatch, "nexus.util.nx_argv", "os.pathsep", ";")  # the Windows separator, on every host
        monkeypatch.setenv("PATH", f"{self.CWD};{self.TOOLS}")

    def test_a_relative_cwd_hit_is_retried_against_path_without_the_cwd(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[object] = []

        def which(name: str, *, path: str | None = None) -> str | None:
            calls.append(path)
            return rf".\{name}.exe" if path is None else rf"{self.TOOLS}\{name}.exe"

        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", which)
        assert nx_argv_mod.which_off_cwd("claude") == rf"{self.TOOLS}\claude.exe"
        assert calls == [None, self.TOOLS], "the retry must drop the working directory's own entry"

    def test_an_absolute_hit_inside_the_cwd_is_a_cwd_hit_too(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        planted = rf"{self.CWD}\claude.exe"
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: planted)
        assert nx_argv_mod.which_off_cwd("claude") is None  # the retry hands the same file back

    def test_a_lookup_that_finds_nothing_off_the_cwd_is_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: rf".\{name}.exe" if path is None else None,
        )
        assert nx_argv_mod.which_off_cwd("bd") is None

    def test_a_hit_on_path_is_returned_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: rf"{self.TOOLS}\{name}.exe")
        assert nx_argv_mod.which_off_cwd("git") == rf"{self.TOOLS}\git.exe"

    def test_posix_is_shutil_which_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: "./looks-like-cwd")
        assert nx_argv_mod.which_off_cwd("nx") == "./looks-like-cwd"

    def test_nx_argv_for_is_the_interpreter_form_on_windows_and_the_lookup_on_posix(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\python.exe")
        assert nx_argv_mod.nx_argv_for(r"C:\anything\nx.exe", "self", "gc") == [
            r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli", "self", "gc",
        ]
        monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
        assert nx_argv_mod.nx_argv_for("/opt/bin/nx", "self", "gc") == ["/opt/bin/nx", "self", "gc"]


class TestWindowsSpawnSitesNeverSpawnALookup:
    """The two session-start hooks, on Windows, with a working-directory plant in
    front of PATH. (The plugin lockstep's ``claude`` is pinned in
    ``tests/test_plugin_lockstep.py``.)"""

    @pytest.fixture(autouse=True)
    def _windows(self, windows: None, monkeypatch: pytest.MonkeyPatch) -> None:
        setattr_in(monkeypatch, "nexus.util.nx_argv", "os.getcwd", lambda: r"C:\work\clone")
        setattr_in(monkeypatch, ("nexus.util.nx_argv", "nexus.commands.daemon", "nexus.daemon.installer"), "sys.executable", r"C:\tools\conexus\Scripts\python.exe")

    def test_self_gc_spawns_the_interpreter_form(self, monkeypatch: pytest.MonkeyPatch) -> None:

        from nexus.hooks import self_gc

        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: r"C:\tools\bin\nx.exe")
        argvs: list[list[str]] = []
        setattr_in(monkeypatch, "nexus.hooks.self_gc", "subprocess.run", lambda argv, **k: argvs.append(list(argv)))
        self_gc.run(None)
        assert argvs == [[r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli", "self", "gc"]]

    def test_self_gc_does_nothing_when_only_a_planted_nx_is_found(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:

        from nexus.hooks import self_gc

        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: r".\nx.exe")
        argvs: list[list[str]] = []
        setattr_in(monkeypatch, "nexus.hooks.self_gc", "subprocess.run", lambda argv, **k: argvs.append(list(argv)))
        self_gc.run(None)
        assert argvs == []

    def test_upgrade_auto_spawns_the_interpreter_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.hooks import upgrade_auto

        setattr_in(monkeypatch, ("nexus.daemon.installer", "nexus.util.nx_argv", "nexus.commands.daemon"), "shutil.which", lambda name, *, path=None: r"C:\tools\bin\nx.exe")
        spawned: list[list[str]] = []

        def spawn(argv: list[str], *a: object, **k: object) -> object:
            spawned.append(list(argv))
            raise OSError("stop here: the argv is what is under test")

        monkeypatch.setattr(upgrade_auto, "_spawn_detached", spawn)
        upgrade_auto.run(None)
        assert spawned == [
            [r"C:\tools\conexus\Scripts\python.exe", "-m", "nexus.cli", "upgrade", "--auto"],
        ]
