# SPDX-License-Identifier: AGPL-3.0-or-later
"""Three siblings of the jar-arm fix in the stop's sweep (nexus-f3yqd.2.22, review of 3f549e254).

The first fix made a ``NEXUS_SERVICE_JAR`` engine match by its java command, not by the jar path
appearing anywhere. Its critique named three more places where the same stop could signal a process that
is not this directory's stack, or exit 1 before its PostgreSQL step:

1. the supervisor arm matched ``daemon service start`` plus ``--config-dir <dir>`` anywhere in a command
   line, so ``sudo -n lab-hand-exec ... nx daemon service start --config-dir X``, ``lab-run -- ...``,
   ``ssh h ...`` and ``bash -c "..."`` joined the kill set (a root-owned wrapper meets EPERM on both
   signals). It now needs an nx entry point at the head of the argv, as the flagless branch always did;
2. a pid that both signals fail on with EPERM stayed in ``stubborn`` with a "survived SIGKILL" line. A
   member the stop identified but could not signal now has its own field (``unsignalled``) and its own
   wording, and the stop still exits 1 before PostgreSQL; only a pid the stop could not identify
   (``unreadable``) warns and goes on;
3. the jar arm names no directory, so two installs sharing one jar path would each sweep the other's
   engine. The member check now reads a jar match's own ``NEXUS_CONFIG_DIR`` as it does for a flagless start.
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

_REAL_KILL = os.kill


# ── 1. the supervisor arm is anchored on the argv head ───────────────────────────


class TestSupervisorArmIsAnchoredOnTheArgvHead:
    @pytest.mark.parametrize(
        "command",
        [
            # the wrappers the critique named
            "sudo -n /usr/local/libexec/lab-hand-exec 524a6b6d /home/u/tmp/lab-run-env.c63k "
            "nx daemon service start --foreground --config-dir {cfg}",
            "lab-run -- nx daemon service start --foreground --config-dir {cfg}",
            "ssh h nx daemon service start --foreground --config-dir {cfg}",
            "bash -c nx daemon service start --foreground --config-dir {cfg}",
            # the same, with the flag spelled --config-dir=<dir>
            "ssh h nx daemon service start --foreground --config-dir={cfg}",
            "sudo -u svc /usr/local/bin/nx daemon service start --config-dir={cfg} --foreground",
            # an editor, a pager and a watcher that carry the text
            "vim /x/y daemon service start --config-dir {cfg}",
            "watch -n 5 nx daemon service start --foreground --config-dir {cfg}",
        ],
    )
    def test_a_wrapper_that_only_carries_the_supervisors_text_does_not_match(
        self, tmp_path: Path, command: str,
    ) -> None:
        cfg = tmp_path / "cfg"
        assert not storage_service_stack_matcher(cfg)(command.format(cfg=cfg))

    @pytest.mark.parametrize(
        "command",
        [
            "nx daemon service start --foreground --config-dir {cfg}",
            "/usr/local/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/usr/local/bin/nx daemon service start --foreground --config-dir={cfg}",
            "nx daemon service start --config-dir {cfg}",
            "/x/gen/bin/python /x/gen/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/x/gen/bin/python3.12 -m nexus.cli daemon service start --foreground --config-dir {cfg}",
            "/bin/sh /home/u/.local/bin/nx daemon service start --foreground --config-dir {cfg}",
            "C:/bin/nx.exe daemon service start --foreground --config-dir {cfg}",
        ],
    )
    def test_the_supervisor_as_nexus_spawns_it_still_matches(self, tmp_path: Path, command: str) -> None:
        cfg = tmp_path / "cfg"
        assert storage_service_stack_matcher(cfg)(command.format(cfg=cfg))

    @pytest.mark.parametrize(
        "command",
        [
            "/Users/Sam Smith/.local/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/Users/Sam Smith/.local/bin/nx daemon service start --foreground",
            "/Users/Sam Smith/venv/bin/python /Users/Sam Smith/venv/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/Users/Sam Smith/venv/bin/python -m nexus.cli daemon service start --foreground --config-dir {cfg}",
            "/bin/sh /Users/Sam Smith/.local/bin/nx daemon service start --foreground --config-dir {cfg}",
        ],
    )
    def test_a_posix_nx_path_with_a_space_still_matches(self, tmp_path: Path, command: str) -> None:
        """A spaced macOS home: the process table is one space-joined string, so the path arrives as words."""
        assert storage_service_stack_matcher(tmp_path / "cfg")(command.format(cfg=tmp_path / "cfg"))

    @pytest.mark.parametrize(
        "command",
        [
            "/usr/bin/ssh h /usr/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/sudo /usr/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/sudo -n /usr/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/env A=1 /usr/bin/nx daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/ssh h /x/python -m nexus.cli daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/sudo /x/python -m nexus.cli daemon service start --foreground --config-dir {cfg}",
            "/usr/bin/sudo python -m nexus.cli daemon service start --foreground --config-dir {cfg}",
            "/bin/bash /usr/local/libexec/lab-hand-exec 524a6b6d /tmp/e /usr/bin/nx daemon service start --config-dir {cfg}",
            "/usr/bin/watch -n 5 /usr/bin/nx daemon service start --config-dir {cfg}",
            "/Users/Sam Smith/bin/lab-run -- /Users/Sam Smith/.local/bin/nx daemon service start --config-dir {cfg}",
        ],
    )
    def test_a_wrapper_before_an_absolute_nx_does_not_match(self, tmp_path: Path, command: str) -> None:
        assert not storage_service_stack_matcher(tmp_path / "cfg")(command.format(cfg=tmp_path / "cfg"))

    def test_another_directory_still_never_matches(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        match = storage_service_stack_matcher(cfg)
        assert match(f"nx daemon service start --foreground --config-dir {cfg}")  # non-vacuity
        assert not match(f"nx daemon service start --foreground --config-dir {cfg}-staging")
        assert not match(f"nx daemon service start --foreground --config-dir {tmp_path / 'other'}")

    CFG = Path(r"C:\Users\Sam Smith\.config\nexus")
    PY = r"C:\Users\Sam Smith\AppData\Local\Programs\Python\Python312\python.exe"
    NX = r"C:\Users\Sam Smith\AppData\Local\Programs\Python\Python312\Scripts\nx.exe"

    @pytest.mark.parametrize(
        "raw",
        [
            '"{py}" -m nexus.cli daemon service start --foreground --config-dir "{cfg}"',
            '"{nx}" daemon service start --foreground --config-dir "{cfg}"',
        ],
    )
    def test_a_windows_supervisor_under_a_spaced_user_directory_matches(self, raw: str) -> None:
        """The process table dequotes, so the spaced path arrives as words (winproc_core.render_command_line)."""
        rendered = render_command_line(raw.format(py=self.PY, nx=self.NX, cfg=self.CFG))
        assert '"' not in rendered and "Sam Smith" in rendered, rendered
        assert storage_service_stack_matcher(self.CFG, platform="win32")(rendered)

    @pytest.mark.parametrize(
        "raw",
        [
            r'"C:\Windows\System32\OpenSSH\ssh.exe" host nx daemon service start --config-dir "{cfg}"',
            r'"C:\Windows\System32\OpenSSH\ssh.exe" host nx.exe daemon service start --config-dir "{cfg}"',
            r'"C:\tools\lab-run.exe" -- "C:\bin\nx.exe" daemon service start --config-dir "{cfg}"',
        ],
    )
    def test_a_windows_wrapper_does_not_match(self, raw: str) -> None:
        rendered = render_command_line(raw.format(cfg=self.CFG))
        assert not storage_service_stack_matcher(self.CFG, platform="win32")(rendered)


class TestSweepSkipsAWrapperThatCarriesTheSupervisorsCommand:
    def test_only_the_supervisor_is_signalled(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        supervisor = f"nx daemon service start --foreground --config-dir {cfg}"
        rows = [
            (196, 60, supervisor),
            (700, 5, f"sudo -n /usr/local/libexec/lab-hand-exec 524a6b6d /tmp/e {supervisor}"),
            (701, 5, f"/bin/bash /usr/local/libexec/lab-hand-exec 524a6b6d /tmp/e {supervisor}"),
        ]
        terminated: list[list[int]] = []
        with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), patch(
            "nexus.daemon.service_registry.process_command",
            side_effect=lambda pid: dict((p, c) for p, _a, c in rows)[pid],
        ), patch("nexus.session._ppid_of", return_value=None), patch(
            "nexus.daemon.service_registry.terminate_pids",
            side_effect=lambda pids, **_: terminated.append(sorted(pids)) or [],
        ):
            result = sweep_matching_processes(
                storage_service_stack_matcher(cfg), exclude_pid=99999,
                member_check=storage_service_member_check(cfg),
            )
        assert sorted(result.pids) == [196]
        assert terminated == [[196]]


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


def _invoke(outcome: ssd.StopOutcome, cfg: Path, *args: str):
    with patch.object(ssd, "stop_storage_service", return_value=outcome), patch.object(
        daemon_mod, "_service_stop_postgres",
    ) as postgres, patch.object(daemon_mod, "_stop_mineru_with_stack"):
        result = CliRunner().invoke(daemon_mod.service_stop_cmd, ["--config-dir", str(cfg), *args])
    return result, postgres


class TestTheCliSeparatesTheUnidentifiedFromTheIdentifiedButUnsignalled:
    def _unidentified(self) -> ssd.StopOutcome:
        return ssd.StopOutcome(pids=(), stubborn=(), source="none", sweep_verified=False, unreadable=(4242,))

    def _unsignalled(self, **kw) -> ssd.StopOutcome:
        return ssd.StopOutcome(pids=(), stubborn=(), source="none", unsignalled=(4242,), **kw)

    def test_an_unidentified_pid_warns_and_the_stop_goes_on_to_postgres(self, tmp_path: Path) -> None:
        result, postgres = _invoke(self._unidentified(), tmp_path, "--with-pg")
        assert result.exit_code == 0, result.output
        assert postgres.called
        assert "could not be identified" in result.output and "NOT signalled" in result.output
        assert "EPERM" not in result.output

    def test_an_identified_member_that_could_not_be_signalled_stops_the_stop_before_postgres(self, tmp_path: Path) -> None:
        result, postgres = _invoke(self._unsignalled(), tmp_path, "--with-pg")
        assert result.exit_code == 1, result.output
        assert not postgres.called, "PostgreSQL must not be touched under a service that is still running"
        out = result.output
        assert "4242" in out and "EPERM" in out and "another user owns" in out
        assert "identified as this config directory's service" in out
        assert "Do not run 'nx daemon service start'" in out

    def test_the_wording_never_says_nothing_was_identified_or_already_stopped_or_survived(self, tmp_path: Path) -> None:
        out = _invoke(self._unsignalled(), tmp_path)[0].output
        for false_claim in ("No process was identified", "already stopped", "survived", "Storage service stopped"):
            assert false_claim not in out, (false_claim, out)

    def test_a_pid_that_is_both_a_lease_survivor_and_unsignalled_is_reported_once(self, tmp_path: Path) -> None:
        """The lease path as stop_storage_service builds it: the root-owned supervisor is in ``pids`` (it was
        'signalled' from the lease), in ``stubborn`` (the kill failed), and in ``unsignalled`` (the sweep found it)."""
        outcome = ssd.StopOutcome(
            pids=(4242,), stubborn=(4242,), source="lease", lease_seen=True, unsignalled=(4242,),
        )
        out = _invoke(outcome, tmp_path)[0].output
        assert "survived" not in out
        assert "Storage service stopped" not in out, "it was not stopped"
        assert out.count("4242") == 1, out

    def test_pids_that_were_stopped_are_still_listed_beside_an_unsignalled_one(self, tmp_path: Path) -> None:
        outcome = ssd.StopOutcome(pids=(100, 4242), stubborn=(4242,), source="lease", lease_seen=True, unsignalled=(4242,))
        out = _invoke(outcome, tmp_path)[0].output
        assert "Storage service stopped (pid(s)=100)." in out
        assert out.count("4242") == 1, out


# ── 3. a jar match is placed by its own NEXUS_CONFIG_DIR ─────────────────────────


@pytest.fixture
def jar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shared" / "nexus-service.jar"
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    monkeypatch.delenv("NEXUS_SERVICE_BIN", raising=False)
    monkeypatch.setenv("NEXUS_SERVICE_JAR", str(path))
    return path.resolve()


def _env(mapping: dict[int, dict[str, str] | None]):
    return lambda pid, _names: mapping[pid]


class TestMemberCheckPlacesAJarEngineByItsOwnDirectory:
    def _engine(self, jar: Path) -> str:
        return f"/usr/bin/java -Duser.timezone=UTC -jar {jar}"

    def test_an_engine_of_this_directory_is_a_member(self, tmp_path: Path, jar: Path) -> None:
        cfg = tmp_path / "a"
        check = storage_service_member_check(cfg, platform="linux", environment_of=_env({7: {"NEXUS_CONFIG_DIR": str(cfg)}}))
        assert check(7, self._engine(jar)) == "member"

    def test_an_engine_of_another_install_sharing_the_jar_is_left_alone(self, tmp_path: Path, jar: Path) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        check = storage_service_member_check(a, platform="linux", environment_of=_env({7: {"NEXUS_CONFIG_DIR": str(b)}}))
        assert check(7, self._engine(jar)) == "other"

    def test_the_default_directory_is_read_from_the_engines_home(self, tmp_path: Path, jar: Path) -> None:
        home = tmp_path / "home"
        default = home / ".config" / "nexus"
        check = storage_service_member_check(default, platform="linux", environment_of=_env({7: {"HOME": str(home)}}))
        assert check(7, self._engine(jar)) == "member"
        elsewhere = storage_service_member_check(tmp_path / "a", platform="linux", environment_of=_env({7: {"HOME": str(home)}}))
        assert elsewhere(7, self._engine(jar)) == "other"

    def test_an_unreadable_environment_is_unreadable_not_a_member(self, tmp_path: Path, jar: Path) -> None:
        check = storage_service_member_check(tmp_path / "a", platform="linux", environment_of=_env({7: None}))
        assert check(7, self._engine(jar)) == "unreadable"

    def test_on_windows_a_jar_match_is_a_member_because_there_is_no_environment_reader(
        self, tmp_path: Path, jar: Path,
    ) -> None:
        """process_environment_values is None on Windows for every pid; the jar arm must not turn that into
        'never swept' (a bare flagless start stays unreadable there, as before)."""
        cfg = tmp_path / "a"
        check = storage_service_member_check(cfg, platform="win32", environment_of=lambda pid, names: None)
        assert check(7, self._engine(jar)) == "member"
        assert check(8, "nx daemon service start --foreground") == "unreadable"
        assert storage_service_stack_matcher(cfg, platform="win32")(self._engine(jar)), "non-vacuity: the arm matches"

    def test_a_well_known_engine_and_a_supervisor_need_no_environment(self, tmp_path: Path, jar: Path) -> None:
        cfg = tmp_path / "a"

        def boom(pid: int, names: tuple[str, ...]):
            raise AssertionError("the environment must not be read for a command that names its directory")

        check = storage_service_member_check(cfg, environment_of=boom)
        assert check(7, f"nx daemon service start --foreground --config-dir {cfg}") == "member"
        assert check(8, str(well_known_binary_path(cfg)) + " -Xmx1g") == "member"

    def test_the_sweep_signals_only_this_directorys_engine_and_reports_the_unplaced_one(
        self, tmp_path: Path, jar: Path,
    ) -> None:
        a, b = tmp_path / "a", tmp_path / "b"
        rows = [(10, 5, self._engine(jar)), (11, 5, self._engine(jar)), (12, 5, self._engine(jar))]
        env = {10: {"NEXUS_CONFIG_DIR": str(a)}, 11: {"NEXUS_CONFIG_DIR": str(b)}, 12: None}
        terminated: list[list[int]] = []
        with patch("nexus.daemon.service_registry.all_process_rows", return_value=rows), patch(
            "nexus.daemon.service_registry.process_command",
            side_effect=lambda pid: dict((p, c) for p, _a, c in rows)[pid],
        ), patch("nexus.session._ppid_of", return_value=None), patch(
            "nexus.daemon.service_registry.terminate_pids",
            side_effect=lambda pids, **_: terminated.append(sorted(pids)) or [],
        ):
            result = sweep_matching_processes(
                storage_service_stack_matcher(a), exclude_pid=99999,
                member_check=storage_service_member_check(a, platform="linux", environment_of=_env(env)),
            )
        assert terminated == [[10]], "the other install's engine must not be signalled"
        assert result.unreadable == (12,)


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
