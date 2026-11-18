# SPDX-License-Identifier: AGPL-3.0-or-later
"""The POSIX-only real-process tests of the stop's sweep (nexus-f3yqd.2.22).

They signal real child processes, fake EPERM on a POSIX signal, or run python under a symlink named java and
read its environment from /proc or KERN_PROCARGS2, so every test here skips on win32. They live in their
own module so the Windows test set can leave the module out (windows-pg-bundle-rehearsal.yml, --ignore)
instead of counting their skips against its non-vacuity ceiling. Their platform-independent siblings stay in
test_stop_sibling_matches.py and test_stop_spares_processes_naming_the_jar.py, which the Windows set runs.
"""
from __future__ import annotations

import errno
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus._install.winproc_core import render_command_line
from nexus.commands import daemon as daemon_mod
from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon.binary_lifecycle import well_known_binary_path
from nexus.daemon.service_registry import (
    process_command,
    storage_service_member_check,
    storage_service_stack_matcher,
    sweep_matching_processes,
    terminate_pids,
)
from tests._module_seam import patch_in
from tests.daemon._children import CHILD_PYTHON, spawn_sleeper

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="real POSIX processes and signals; left out of the Windows set")

_REAL_KILL = os.kill


@pytest.fixture
def jar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shared" / "nexus-service.jar"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(path))
    return path.resolve()




# ── 2. a pid both signals fail on with EPERM is not signalled, and does not block the stop ─────


def _deny_signals(pid_denied: int):
    """An ``os.kill`` that refuses SIGTERM and SIGKILL for one pid (another user owns it) and is the real call
    for everything else, signal 0 included (``pid_alive`` probes with it)."""

    def kill(pid: int, sig: int) -> None:
        if pid == pid_denied and sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
            raise PermissionError(errno.EPERM, "Operation not permitted")
        _REAL_KILL(pid, sig)

    return kill


@pytest.mark.skipif(sys.platform == "win32", reason="EPERM on a POSIX signal; the Windows arm is a refusal (nexus-f9bgu.17)")
class TestEpermIsReportedNotStubborn:
    @pytest.fixture
    def foreign(self):
        """A live child standing in for a process another user owns: the fake refuses to signal it."""
        proc = spawn_sleeper(["foreign-owner"])
        try:
            yield proc
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_terminate_pids_reports_the_unsignalled_pid_apart_from_the_survivors(self, foreign) -> None:
        unsignalled: list[int] = []
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=_deny_signals(foreign.pid)):
            survivors = terminate_pids([foreign.pid], grace_s=0.3, unsignalled_out=unsignalled)
        assert unsignalled == [foreign.pid]
        assert survivors == [], "a pid nobody could signal is not a survivor of a SIGKILL"

    def test_without_the_out_list_the_pid_is_still_a_survivor(self, foreign) -> None:
        """The other callers (upgrade_finish, uninstall) keep today's reading: still running means survivor."""
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=_deny_signals(foreign.pid)):
            assert terminate_pids([foreign.pid], grace_s=0.3) == [foreign.pid]

    def test_a_pid_that_dies_on_the_graceful_signal_is_not_unsignalled(self) -> None:
        proc = spawn_sleeper(["ordinary"])
        unsignalled: list[int] = []
        try:
            assert terminate_pids([proc.pid], grace_s=10, unsignalled_out=unsignalled) == []
        finally:
            proc.kill()
            proc.wait(timeout=10)
        assert unsignalled == []

    def _rows(self, cfg: Path, pid: int) -> list[tuple[int, int, str]]:
        return [(pid, 60, f"nx daemon service start --foreground --config-dir {cfg}")]

    def test_the_sweep_lists_it_as_not_signalled_and_not_stubborn(self, tmp_path: Path, foreign) -> None:
        cfg = tmp_path / "cfg"
        rows = self._rows(cfg, foreign.pid)
        with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), patch(
            "nexus.daemon.service_registry.process_command", side_effect=lambda pid: rows[0][2],
        ), patch("nexus.session._ppid_of", return_value=None), patch_in(
            "nexus.daemon.service_registry", "os.kill", side_effect=_deny_signals(foreign.pid),
        ):
            result = sweep_matching_processes(
                storage_service_stack_matcher(cfg), exclude_pid=99999, grace_s=0.3,
                member_check=storage_service_member_check(cfg),
            )
        assert result.stubborn == ()
        assert result.unsignalled == (foreign.pid,)
        assert result.unreadable == (), "it was identified as a member: that is not the unidentified class"
        assert result.pids == (), "it was not signalled, so it is not among the pids the sweep stopped"

    def test_the_stop_reports_it_as_unsignalled_and_never_claims_it_was_stopped(self, tmp_path: Path, foreign) -> None:
        cfg = tmp_path / "cfg"
        rows = self._rows(cfg, foreign.pid)
        with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), patch(
            "nexus.daemon.service_registry.process_command", side_effect=lambda pid: rows[0][2],
        ), patch("nexus.session._ppid_of", return_value=None), patch_in(
            "nexus.daemon.service_registry", "os.kill", side_effect=_deny_signals(foreign.pid),
        ):
            outcome = ssd.stop_storage_service(config_dir=cfg)
        assert outcome.stubborn == ()
        assert outcome.unsignalled == (foreign.pid,)
        assert outcome.unreadable == ()
        assert outcome.pids == ()
        assert outcome.already_stopped is False

    def test_a_pid_that_refuses_sigterm_but_takes_sigkill_is_killed_at_once(self, foreign) -> None:
        """EPERM on the graceful signal alone: the hard kill is tried without waiting out the grace, and the
        pid is neither unsignalled nor a survivor (the line was deletable with every other test green)."""

        def deny_sigterm_only(pid: int, sig: int) -> None:
            if pid == foreign.pid and sig == signal.SIGTERM:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            _REAL_KILL(pid, sig)

        unsignalled: list[int] = []
        started = time.monotonic()
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=deny_sigterm_only):
            survivors = terminate_pids([foreign.pid], grace_s=30, unsignalled_out=unsignalled)
        assert time.monotonic() - started < 20, "the denied pid waited out the grace instead of being killed"
        assert (survivors, unsignalled) == ([], [])
        assert foreign.wait(timeout=10) != 0, "the sleeper must have been SIGKILLed"




@pytest.mark.skipif(sys.platform == "win32", reason="the positive control runs python under a symlink named java")
class TestSweepWithRealProcesses:
    """The sweep the stop runs, against live processes. ``terminate_pids`` is
    recorded and never run, so nothing else on the box is signalled."""

    def _found(self, cfg: Path) -> set[int]:
        with patch("nexus.daemon.service_registry.terminate_pids", return_value=[]):
            result = sweep_matching_processes(
                storage_service_stack_matcher(cfg), member_check=storage_service_member_check(cfg),
            )
        return {pid for pid, _cmd in result.found}

    @staticmethod
    def _visible(pid: int) -> None:
        deadline = time.monotonic() + 10
        while not process_command(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_command(pid), f"fixture precondition: pid {pid} must be in the process table"

    def test_a_driver_naming_the_jar_is_not_swept_and_a_java_engine_is(
        self, tmp_path: Path, jar: Path,
    ) -> None:
        cfg = tmp_path / "cfg"
        # A stand-in JVM named java (POSIX only: this class skips Windows, where the launcher is java.exe).
        java = (tmp_path / "jdk" / "bin").joinpath("java")
        java.parent.mkdir(parents=True)
        os.symlink(CHILD_PYTHON, java)
        driver = spawn_sleeper(["scenario", "upgrade", "--work", str(tmp_path / "w"), "--jar", str(jar)])
        engine = None
        try:
            from tests._child_process import popen_in_group  # noqa: PLC0415

            engine = popen_in_group(
                [str(java), "-c", "import time; time.sleep(120)", "-Duser.timezone=UTC",
                 f"-XX:ErrorFile={tmp_path / 'hs_err_%p.log'}", "-jar", str(jar)],
                # the supervisor's env copy carries the directory, and the member check reads it (nexus-f3yqd.2.22)
                env={**os.environ, "NEXUS_CONFIG_DIR": str(cfg)},
            )
            self._visible(driver.pid)
            self._visible(engine.pid)
            assert f"-jar {jar}" in process_command(driver.pid), (
                "fixture precondition: the driver's command line must carry the old marker's text, "
                "or this proves nothing"
            )
            found = self._found(cfg)
            assert engine.pid in found, f"non-vacuity: the java engine must match ({process_command(engine.pid)!r})"
            assert driver.pid not in found, (
                f"pid {driver.pid} only names the jar as an option value and must never be signalled: "
                f"{process_command(driver.pid)!r}"
            )
        finally:
            for proc in (driver, engine):
                if proc is not None:
                    proc.kill()
                    proc.wait(timeout=10)




@pytest.mark.skipif(sys.platform == "win32", reason="the real engines run python under a symlink named java; Windows has no environment reader")
def test_two_real_engines_on_one_jar_are_told_apart_by_their_environment(tmp_path: Path, jar: Path) -> None:
    from tests._child_process import popen_in_group  # noqa: PLC0415

    a, b = tmp_path / "a", tmp_path / "b"
    java = (tmp_path / "jdk" / "bin").joinpath("java")  # not the bin/java spelling the spawn lint bans
    java.parent.mkdir(parents=True)
    os.symlink(CHILD_PYTHON, java)
    procs = []
    try:
        for cfg in (a, b):
            env = {**os.environ, "NEXUS_CONFIG_DIR": str(cfg)}
            procs.append(popen_in_group(
                [str(java), "-c", "import time; time.sleep(120)", "-Duser.timezone=UTC",
                 f"-XX:ErrorFile={tmp_path / 'hs_err_%p.log'}", "-jar", str(jar)], env=env,
            ))
        deadline = time.monotonic() + 10
        while not all(process_command(p.pid) for p in procs) and time.monotonic() < deadline:
            time.sleep(0.05)
        with patch("nexus.daemon.service_registry.terminate_pids", return_value=[]):
            result = sweep_matching_processes(
                storage_service_stack_matcher(a), member_check=storage_service_member_check(a),
            )
        found = {pid for pid, _cmd in result.found}
        assert procs[0].pid in found, "non-vacuity: this directory's own engine must be found"
        assert procs[1].pid not in found, "the engine of another install on the same jar must be left alone"
    finally:
        for proc in procs:
            proc.kill()
            proc.wait(timeout=10)
