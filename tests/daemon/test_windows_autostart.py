# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-224 Phase 3 Step 3 (nexus-f9bgu.23): the Task Scheduler logon task.

Every test here runs on every host: the platform, the SID, the interpreter and
the whole ``schtasks`` surface are injected, so the Windows branch of the
installer is exercised on macOS and Linux and nothing skip-passes. The
non-vacuity asserts name what each group proved it reached. The behaviours that
only a real Task Scheduler can show (the job object, restart semantics, the
stop channel from a task session) are measured on a Windows box and recorded in
T2 ``nexus_rdr/224-f9bgu23-autostart``.
"""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from nexus import health, upgrade_finish
from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer, windows_autostart
from nexus.daemon.service_registry import ttl_for_tier
from nexus.util import win_job

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SID = "S-1-5-21-111-222-333-1001"
PYTHONW = r"C:\Users\sam\AppData\Roaming\uv\tools\conexus\Scripts\pythonw.exe"


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _find(root: ET.Element, path: str) -> ET.Element:
    node = root.find(path, NS)
    assert node is not None, f"no {path} in the task definition"
    return node


def _text(root: ET.Element, path: str) -> str:
    return (_find(root, path).text or "").strip()


# ── the task definition ──────────────────────────────────────────────────────


class TestTaskXml:
    def _xml(self, config_dir: str = r"C:\Users\sam\.config\nexus") -> str:
        return windows_autostart.task_xml(sid=SID, pythonw=PYTHONW, config_dir=config_dir)

    def test_logon_trigger_is_for_exactly_this_user(self) -> None:
        root = _root(self._xml())
        assert _text(root, "t:Triggers/t:LogonTrigger/t:UserId") == SID
        assert _text(root, "t:Triggers/t:LogonTrigger/t:Enabled") == "true"
        # Non-vacuity: exactly one trigger, and it is the logon trigger.
        assert len(list(_find(root, "t:Triggers"))) == 1

    def test_runs_only_while_logged_on_without_a_stored_password_or_elevation(self) -> None:
        root = _root(self._xml())
        assert _text(root, "t:Principals/t:Principal/t:UserId") == SID
        # InteractiveToken is the XML spelling of schtasks /IT.
        assert _text(root, "t:Principals/t:Principal/t:LogonType") == "InteractiveToken"
        assert _text(root, "t:Principals/t:Principal/t:RunLevel") == "LeastPrivilege"
        assert root.find(".//t:Password", NS) is None
        assert "S4U" not in self._xml() and "Password" not in self._xml()

    def test_settings_the_bead_names_and_the_battery_defaults_it_must_override(self) -> None:
        root = _root(self._xml())
        assert _text(root, "t:Settings/t:ExecutionTimeLimit") == "PT0S"
        # schtasks' defaults for both are true, which would never start or would
        # kill the service on a laptop on battery.
        assert _text(root, "t:Settings/t:DisallowStartIfOnBatteries") == "false"
        assert _text(root, "t:Settings/t:StopIfGoingOnBatteries") == "false"
        assert _text(root, "t:Settings/t:MultipleInstancesPolicy") == "IgnoreNew"
        assert _text(root, "t:Settings/t:Enabled") == "true"
        # Hidden is the Task Scheduler UI flag, not a window; a user must be able
        # to find the task to disable it.
        assert _text(root, "t:Settings/t:Hidden") == "false"
        restart = _find(root, "t:Settings/t:RestartOnFailure")
        assert (restart.findtext("t:Interval", namespaces=NS), restart.findtext("t:Count", namespaces=NS)) == (
            windows_autostart.TASK_RESTART_INTERVAL,
            str(windows_autostart.TASK_RESTART_COUNT),
        )

    def test_the_action_is_the_windowless_launcher_with_the_resolved_config_dir(self) -> None:
        root = _root(self._xml(r"C:\Users\sam\with space\nexus"))
        assert _text(root, "t:Actions/t:Exec/t:Command") == PYTHONW
        args = _text(root, "t:Actions/t:Exec/t:Arguments")
        assert args.startswith(f"-m {windows_autostart.LAUNCHER_MODULE} --config-dir ")
        assert r'"C:\Users\sam\with space\nexus"' in args
        assert "--foreground" not in args, "the task must run the launcher, never the supervisor itself"

    def test_the_document_is_ascii_with_no_encoding_declaration(self) -> None:
        # schtasks /Create /XML refuses a UTF-8 declaration ("unable to switch
        # the encoding", measured) and accepts an undeclared document; ASCII
        # keeps read_text and write_text in agreement under any locale.
        xml = windows_autostart.task_xml(sid=SID, pythonw=PYTHONW, config_dir="C:\\Users\\jos\u00e9 & co\\nexus")
        assert xml.isascii()
        assert "encoding" not in xml.splitlines()[0]
        root = _root(xml)
        args = _text(root, "t:Actions/t:Exec/t:Arguments")
        assert "jos\u00e9 & co" in args, "the escaped path must round-trip through the XML parser"

    def test_xml_special_characters_in_paths_are_escaped(self) -> None:
        xml = windows_autostart.task_xml(sid=SID, pythonw=r"C:\a&b\<x>\pythonw.exe", config_dir=r"C:\c&d")
        root = _root(xml)
        assert _text(root, "t:Actions/t:Exec/t:Command") == r"C:\a&b\<x>\pythonw.exe"


class TestTaskEnabled:
    def test_enabled_and_disabled(self) -> None:
        assert windows_autostart.task_enabled("<Task><Settings><Enabled>true</Enabled></Settings></Task>") is True
        assert windows_autostart.task_enabled("<Task><Settings><Enabled>false</Enabled></Settings></Task>") is False

    def test_a_trigger_level_enabled_flag_is_not_the_task_flag(self) -> None:
        xml = (
            "<Task><Triggers><LogonTrigger><Enabled>false</Enabled></LogonTrigger></Triggers>"
            "<Settings><Enabled>true</Enabled></Settings></Task>"
        )
        assert windows_autostart.task_enabled(xml) is True

    def test_schema_default_when_the_setting_is_absent(self) -> None:
        assert windows_autostart.task_enabled("<Task><Settings/></Task>") is True


class TestInterpreters:
    def test_console_python_sits_beside_pythonw(self) -> None:
        assert windows_autostart.console_python_for(PYTHONW) == PYTHONW.replace("pythonw.exe", "python.exe")
        assert windows_autostart.console_python_for(r"C:\x\python.exe") == r"C:\x\python.exe"

    def test_pythonw_falls_back_to_the_given_interpreter_when_no_sibling_exists(self) -> None:
        assert windows_autostart.pythonw_for(PYTHONW) == PYTHONW
        assert windows_autostart.pythonw_for(r"C:\no\such\dir\python.exe", exists=lambda _p: False) == (
            r"C:\no\such\dir\python.exe"
        )

    def test_pythonw_is_preferred_when_it_exists(self) -> None:
        seen: list[str] = []

        def exists(path: str) -> bool:
            seen.append(path)
            return True

        assert windows_autostart.pythonw_for(r"C:\x\python.exe", exists=exists) == r"C:\x\pythonw.exe"
        assert seen == [r"C:\x\pythonw.exe"], "the sibling is what must be probed"


# ── the launcher ─────────────────────────────────────────────────────────────


class _StopLoop(Exception):
    pass


class TestLauncherLoop:
    """launchd's KeepAlive/SuccessfulExit=false, applied by the launcher."""

    def _run(self, outcomes: list[object], *, stop_after_sleeps: int = 10) -> tuple[int | None, list[float], int]:
        sleeps: list[float] = []
        calls: list[Path] = []
        remaining = list(outcomes)

        def supervise(config_dir: Path) -> int:
            calls.append(config_dir)
            outcome = remaining.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return int(outcome)  # type: ignore[call-overload]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) >= stop_after_sleeps:
                raise _StopLoop

        try:
            code: int | None = windows_autostart.run_launcher(
                Path("/cfg"), supervise=supervise, sleep=sleep, throttle_s=7.0
            )
        except _StopLoop:
            code = None
        return code, sleeps, len(calls)

    def test_a_clean_exit_ends_the_launcher_without_a_respawn(self) -> None:
        code, sleeps, spawns = self._run([0])
        assert (code, sleeps, spawns) == (0, [], 1)

    @pytest.mark.parametrize("failure", [1, 2, 3, 4, 255, 0xC0000005])
    def test_every_non_zero_exit_is_respawned_after_the_throttle_until_a_clean_exit(self, failure: int) -> None:
        code, sleeps, spawns = self._run([failure, failure, 0])
        assert code == 0
        assert spawns == 3, "the supervisor must be respawned after each failure"
        assert sleeps == [7.0, 7.0]

    def test_a_spawn_that_cannot_start_is_retried_not_fatal(self) -> None:
        code, sleeps, spawns = self._run([FileNotFoundError("python.exe"), 0])
        assert (code, sleeps, spawns) == (0, [7.0], 2)

    def test_it_never_gives_up(self) -> None:
        code, sleeps, spawns = self._run([3] * 50, stop_after_sleeps=25)
        assert code is None, "a launcher that returns on repeated failure would abandon the service"
        assert spawns == 25 and len(sleeps) == 25

    def test_the_throttle_matches_launchd(self) -> None:
        assert windows_autostart.RESTART_THROTTLE_S == 30.0

    def test_the_throttle_outlasts_the_dead_owners_lease(self) -> None:
        """A respawn inside the dead owner's lease TTL could find the lease still
        fresh and stand down with exit 0, ending the launcher with no supervisor."""
        assert windows_autostart.RESTART_THROTTLE_S > ttl_for_tier("storage_service")


class TestSuperviseOnce:
    def test_spawns_the_supervisor_with_the_stop_channel_flags_and_returns_its_exit_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(windows_autostart.sys, "executable", PYTHONW)
        seen: dict[str, object] = {}

        class _Proc:
            pid = 4242

            def wait(self) -> int:
                return 3

        def fake_popen(argv: list[str], **kwargs: object) -> _Proc:
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _Proc()

        code = windows_autostart._supervise_once(tmp_path, popen=fake_popen, platform="win32")
        assert code == 3
        argv = seen["argv"]
        assert argv == [
            PYTHONW.replace("pythonw.exe", "python.exe"),
            "-m", "nexus.cli", "daemon", "service", "start", "--foreground",
            "--config-dir", str(tmp_path.resolve()),
        ]
        kwargs = seen["kwargs"]
        assert isinstance(kwargs, dict)
        # The two flags the stop channel depends on, and never DETACHED_PROCESS.
        assert kwargs["creationflags"] == win_job.CREATE_NEW_PROCESS_GROUP | win_job.CREATE_NO_WINDOW
        assert kwargs["stdin"] == subprocess.DEVNULL

    def test_the_supervisor_argv_is_the_one_the_client_spawn_path_builds(self, tmp_path: Path) -> None:
        assert daemon_cmd._supervisor_argv(tmp_path, nx_bin=["nx"]) == [
            "nx", "daemon", "service", "start", "--foreground",
            "--config-dir", str(tmp_path.resolve()),
        ]


# ── the installer's Windows arm ──────────────────────────────────────────────


class _FakeSchtasks:
    """A scriptable ``schtasks``: records argv and answers the verbs the installer uses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.registered = False
        self.enabled = True
        self.run_rc = 0
        self.list_rc = 0
        self.query_rc_override: int | None = None

    def verbs(self) -> list[str]:
        return [c[1].lstrip("/").lower() for c in self.calls]

    def __call__(self, argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
        # The absolute System32 path, never the bare name (nexus-f9bgu.33: the bare
        # name is resolved through the current directory on Windows).
        assert argv[0] == installer._windows_manager_path("schtasks"), (
            f"a Windows install must only ever call System32 schtasks, saw {argv[0]!r}"
        )
        self.calls.append(list(argv))
        verb = argv[1].lstrip("/").lower()
        if verb == "create":
            self.registered = True
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        if verb == "run":
            return subprocess.CompletedProcess(argv, self.run_rc, "", "ERROR: run refused" if self.run_rc else "")
        if verb == "end":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "delete":
            if not self.registered:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: The system cannot find the file specified.")
            self.registered = False
            return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")
        assert verb == "query", f"unexpected verb {verb!r}"
        if "/FO" in argv:
            rows = '"\\NexusStorageService","N/A","Ready"\n"\\Other","N/A","Ready"\n' if self.registered else '"\\Other","N/A","Ready"\n'
            return subprocess.CompletedProcess(argv, self.list_rc, rows, "")
        if self.query_rc_override is not None:
            return subprocess.CompletedProcess(argv, self.query_rc_override, "", "ERROR: access denied")
        if not self.registered:
            return subprocess.CompletedProcess(argv, 1, "", "ERROR: The system cannot find the file specified.")
        flag = "true" if self.enabled else "false"
        body = f"<Task><Settings><Enabled>{flag}</Enabled></Settings></Task>" if "/XML" in argv else ""
        return subprocess.CompletedProcess(argv, 0, body, "")


@pytest.fixture
def win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeSchtasks:
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "win32")
    monkeypatch.setattr(daemon_cmd, "_autostart_install_dir", lambda: tmp_path / "autostart")
    monkeypatch.setattr(daemon_cmd, "_autostart_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(installer, "_task_user_sid", lambda: SID)
    monkeypatch.setattr(installer, "_task_pythonw", lambda: PYTHONW)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    fake = _FakeSchtasks()
    monkeypatch.setattr(installer, "run_bounded", fake)
    return fake


class TestWindowsPlatformHelpers:
    def test_filename_install_dir_and_log_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "win32")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert daemon_cmd._autostart_filename_service() == windows_autostart.TASK_FILENAME
        assert daemon_cmd._autostart_install_dir() == tmp_path / "AppData" / "Local" / "nexus" / "autostart"
        assert daemon_cmd._autostart_log_dir() == tmp_path / "AppData" / "Local" / "nexus" / "logs"

    def test_other_platforms_are_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "linux")
        assert daemon_cmd._autostart_filename_service() == "nexus-service.service"
        monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: "freebsd")
        with pytest.raises(Exception, match="not supported"):
            daemon_cmd._autostart_install_dir()


class TestSchtasksClassification:
    @pytest.mark.parametrize(
        "cmd,mutating",
        [
            (["schtasks", "/Query", "/TN", "NexusStorageService", "/XML"], False),
            (["schtasks", "/query", "/FO", "CSV", "/NH"], False),
            (["schtasks.exe", "/Query"], False),
            (["C:\\Windows\\System32\\schtasks.exe", "/QUERY"], False),
            (["schtasks", "/Create", "/TN", "x", "/XML", "x.xml", "/F"], True),
            (["schtasks", "/Delete", "/TN", "x", "/F"], True),
            (["schtasks", "/Run", "/TN", "x"], True),
            (["schtasks", "/End", "/TN", "x"], True),
            (["schtasks", "/Change", "/TN", "x", "/ENABLE"], True),
            (["schtasks"], True),
            (["schtasks", "/frobnicate"], True),
        ],
    )
    def test_verbs(self, cmd: list[str], mutating: bool) -> None:
        assert installer.is_service_manager_cmd(cmd)
        assert installer.is_mutating_manager_cmd(cmd) is mutating

    def test_the_existing_managers_are_unchanged(self) -> None:
        assert installer.is_mutating_manager_cmd(["launchctl", "bootstrap", "gui/1", "x"]) is True
        assert installer.is_mutating_manager_cmd(["systemctl", "--user", "is-enabled", "x"]) is False


class TestWindowsInstall:
    def test_a_fresh_install_writes_the_task_registers_it_and_starts_it(self, win: _FakeSchtasks, tmp_path: Path) -> None:
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert result.dest == tmp_path / "autostart" / windows_autostart.TASK_FILENAME
        body = result.dest.read_text()
        assert body.startswith('<?xml version="1.0"?>')
        assert SID in body and PYTHONW in body
        assert str((tmp_path / "cfg").resolve()) in body
        assert win.verbs() == ["create", "run"], "register first, then start it now"
        assert win.calls[0] == [
            installer._windows_manager_path("schtasks"), "/Create", "/TN", windows_autostart.TASK_NAME, "/XML", str(result.dest), "/F",
        ]
        # The reported command is the logical one; the spawn resolved argv[0].
        assert result.activated_cmd == ["schtasks", *win.calls[0][1:]]
        assert "started via" in result.detail and result.warnings == ()

    def test_an_identical_registered_task_is_left_alone(self, win: _FakeSchtasks) -> None:
        installer.install_autostart(tier="service")
        win.calls.clear()
        again = installer.install_autostart(tier="service")
        assert again.status is installer.InstallStatus.ALREADY_PRESENT
        assert win.verbs() and set(win.verbs()) == {"query"}, "an idempotent install must not mutate anything"

    def test_an_identical_file_whose_task_is_gone_is_registered_again(self, win: _FakeSchtasks) -> None:
        installer.install_autostart(tier="service")
        win.registered = False
        win.calls.clear()
        again = installer.install_autostart(tier="service")
        assert again.status is installer.InstallStatus.NEWLY_INSTALLED
        assert win.verbs()[-2:] == ["create", "run"]

    def test_a_disabled_task_is_registered_again(self, win: _FakeSchtasks) -> None:
        installer.install_autostart(tier="service")
        win.enabled = False
        win.calls.clear()
        again = installer.install_autostart(tier="service")
        assert again.status is installer.InstallStatus.NEWLY_INSTALLED
        assert "create" in win.verbs()

    def test_a_drifted_definition_is_refused_without_force_and_replaced_with_it(self, win: _FakeSchtasks) -> None:
        first = installer.install_autostart(tier="service")
        first.dest.write_text("<Task>hand edited</Task>")
        with pytest.raises(installer.ContentDiffersError):
            installer.install_autostart(tier="service")
        win.calls.clear()
        forced = installer.install_autostart(tier="service", force=True)
        assert forced.status is installer.InstallStatus.NEWLY_INSTALLED
        # End the running launcher and drop the old definition before the re-create.
        assert win.verbs() == ["end", "delete", "create", "run"]
        assert SID in forced.dest.read_text()

    def test_a_task_that_cannot_be_started_now_is_a_warning_not_a_failure(self, win: _FakeSchtasks) -> None:
        win.run_rc = 1
        result = installer.install_autostart(tier="service")
        assert result.status is installer.InstallStatus.NEWLY_INSTALLED
        assert len(result.warnings) == 1 and "next logon" in result.warnings[0]

    def test_a_failed_registration_raises_the_activation_error(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
            win.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, "", "ERROR: Access is denied.")

        monkeypatch.setattr(installer, "run_bounded", refuse)
        with pytest.raises(installer.ActivationError, match="Access is denied"):
            installer.install_autostart(tier="service")

    def test_no_schtasks_on_the_box_is_an_activation_error(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("schtasks")

        monkeypatch.setattr(installer, "run_bounded", missing)
        with pytest.raises(installer.ActivationError, match="schtasks not found"):
            installer.install_autostart(tier="service")

    def test_the_windows_arm_ran_at_all(self, win: _FakeSchtasks) -> None:
        # Non-vacuity for the group: a regression that routed Windows through the
        # launchd/systemd arms would call a different binary and trip the fake.
        installer.install_autostart(tier="service")
        assert win.calls, "the injected Windows platform never reached schtasks"
        assert all(c[0] == installer._windows_manager_path("schtasks") for c in win.calls)


class TestWindowsActivationState:
    def _probe(self) -> installer.ActivationProbe:
        dest, _ = installer.rendered_unit_content("service")
        return installer.autostart_activation_state(dest, tier="service")

    def test_registered_and_enabled_is_active(self, win: _FakeSchtasks) -> None:
        win.registered = True
        assert self._probe().state is installer.ActivationState.ACTIVE

    def test_a_disabled_task_is_not_active_with_a_remedy(self, win: _FakeSchtasks) -> None:
        win.registered = True
        win.enabled = False
        probe = self._probe()
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        assert "/ENABLE" in probe.remedy and windows_autostart.TASK_NAME in probe.remedy

    def test_a_task_absent_from_an_answered_listing_is_not_active(self, win: _FakeSchtasks) -> None:
        probe = self._probe()
        assert probe.state is installer.ActivationState.NOT_ACTIVE
        assert probe.remedy == installer.REINSTALL_REMEDY
        assert "list" in "".join(" ".join(c) for c in win.calls).lower() or "/FO" in [a for c in win.calls for a in c]

    def test_a_query_that_fails_for_another_reason_is_unreachable_never_a_defect(self, win: _FakeSchtasks) -> None:
        win.registered = True
        win.query_rc_override = 1
        assert self._probe().state is installer.ActivationState.UNREACHABLE

    def test_an_unanswerable_listing_is_unreachable(self, win: _FakeSchtasks) -> None:
        win.list_rc = 1
        assert self._probe().state is installer.ActivationState.UNREACHABLE

    def test_no_schtasks_is_no_manager(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("schtasks")

        monkeypatch.setattr(installer, "run_bounded", missing)
        assert self._probe().state is installer.ActivationState.NO_MANAGER

    def test_undecodable_output_is_unreachable(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        def garbled(argv: list[str], *, timeout: float, **_kw: object) -> subprocess.CompletedProcess[str]:
            raise UnicodeDecodeError("cp1252", b"\x81", 0, 1, "undefined")

        monkeypatch.setattr(installer, "run_bounded", garbled)
        assert self._probe().state is installer.ActivationState.UNREACHABLE


class TestWindowsUninstall:
    def test_uninstall_ends_the_launcher_deletes_the_task_and_removes_the_kept_file(self, win: _FakeSchtasks) -> None:
        installed = installer.install_autostart(tier="service")
        win.calls.clear()
        result = installer.uninstall_autostart(tier="service")
        assert result.status is installer.UninstallStatus.REMOVED
        assert result.deactivated is True
        assert win.verbs() == ["end", "delete"]
        assert not installed.dest.exists()
        assert win.registered is False

    def test_a_registered_task_without_its_kept_file_is_still_removed(self, win: _FakeSchtasks) -> None:
        installed = installer.install_autostart(tier="service")
        installed.dest.unlink()
        win.calls.clear()
        result = installer.uninstall_autostart(tier="service")
        assert result.status is installer.UninstallStatus.REMOVED
        assert "delete" in win.verbs() and win.registered is False

    def test_nothing_installed_is_not_installed(self, win: _FakeSchtasks) -> None:
        result = installer.uninstall_autostart(tier="service")
        assert result.status is installer.UninstallStatus.NOT_INSTALLED
        assert set(win.verbs()) == {"query"}, "nothing may be mutated when nothing is installed"

    def test_a_failed_delete_is_a_warning_and_the_file_is_removed_anyway(self, win: _FakeSchtasks) -> None:
        installed = installer.install_autostart(tier="service")
        win.registered = False  # the task vanished: /Delete now fails
        result = installer.uninstall_autostart(tier="service")
        assert result.status is installer.UninstallStatus.REMOVED
        assert result.deactivated is False and result.warnings
        assert not installed.dest.exists()


class TestDoctorRowReachesTheWindowsTask:
    """The existing ``Service autostart unit (local mode)`` row is the doctor surface.

    It reads the kept file, compares it with the render and asks the manager, all
    three of which now have a Windows arm, so no second row is needed and a virgin
    box (no kept file) stays silent.
    """

    def test_a_current_registered_task_reports_ok(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        installer.install_autostart(tier="service")
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        rows = health._check_service_autostart_drift()
        assert len(rows) == 1, "an installed task must produce exactly one doctor row"
        assert rows[0].ok and "enabled for login" in rows[0].detail
        assert upgrade_finish._probe_service_autostart_drift() is not None

    def test_a_task_that_is_gone_warns_with_the_reinstall_remedy(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        installer.install_autostart(tier="service")
        win.registered = False
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        (row,) = health._check_service_autostart_drift()
        assert row.warn and not row.ok
        assert "not registered" in row.detail
        assert any("service install --autostart" in f for f in row.fix_suggestions)

    def test_a_virgin_box_has_no_row(self, win: _FakeSchtasks, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
        assert health._check_service_autostart_drift() == []
        assert win.calls == [], "a box with no kept task file must not even ask Task Scheduler"
