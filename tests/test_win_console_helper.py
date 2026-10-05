# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stop CLI never touches its OWN console (RDR-224, nexus-f9bgu.33, review S2).

``GenerateConsoleCtrlEvent`` reaches only a console the caller is attached to,
so the sender has to ``FreeConsole`` and ``AttachConsole`` to the target's.
Done in the CLI process that rebinds the CLI's console (process-global: every
thread loses it for the window) and leaves its std handles to whatever
``AttachConsole(ATTACH_PARENT_PROCESS)`` rebuilds. Production now runs the
sequence in a short-lived helper process spawned ``DETACHED_PROCESS`` (no
console of its own to begin with), and the CLI's console is never detached.

Every test injects the spawner or the platform, so they run on every host. The
real helper, console hosts and the CLI's own handles are measured on Windows
(T2 ``nexus_rdr/224-review-p3-fixes-a``).
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict

import pytest

from nexus.daemon import service_registry as sr
from nexus.util import nx_argv as nx_argv_mod
from nexus.util import win_console
from nexus.util.win_console import ConsoleBreakResult, send_ctrl_break_via_console


class _Api:
    def __init__(self, **kw: object) -> None:
        self.calls: list[str] = []
        self._kw = kw

    def free_console(self) -> bool:
        self.calls.append("free")
        return True

    def attach_console(self, pid: int) -> tuple[bool, int]:
        self.calls.append("attach")
        return (bool(self._kw.get("attach_ok", True)), int(self._kw.get("attach_error", 0)))  # type: ignore[call-overload]

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        self.calls.append("send")
        return True, 0

    def attach_parent_console(self) -> tuple[bool, int]:
        self.calls.append("parent")
        return True, 0

    def session_of(self, pid: int) -> int | None:
        return {1: 1}.get(pid, 0)


class TestHelperSequence:
    def test_the_helper_never_re_attaches_a_parent_console(self) -> None:
        api = _Api()
        result = send_ctrl_break_via_console(4242, api, reattach=False)
        # The helper owns no console worth restoring; every "parent" call is a
        # process-global console change the CLI path used to make.
        assert api.calls == ["free", "attach", "send", "free"]
        assert result.sent is True

    def test_a_refusal_in_the_helper_does_not_re_attach_either(self) -> None:
        api = _Api(attach_ok=False, attach_error=win_console.ERROR_ACCESS_DENIED)
        result = send_ctrl_break_via_console(4242, api, reattach=False)
        assert api.calls == ["free", "attach"]
        assert result.refused is True

    def test_the_in_process_default_still_restores_the_callers_console(self) -> None:
        api = _Api()
        send_ctrl_break_via_console(4242, api)
        assert api.calls == ["free", "attach", "send", "free", "parent"]


class _Run:
    """A recording ``subprocess.run`` stand-in that answers with canned stdout."""

    def __init__(self, stdout: str = "", *, raises: BaseException | None = None, rc: int = 0) -> None:
        self.stdout = stdout
        self.raises = raises
        self.rc = rc
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), dict(kwargs)))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.rc, self.stdout, "")


def _ok(**overrides: object) -> str:
    return json.dumps(asdict(ConsoleBreakResult(sent=True, **overrides)))  # type: ignore[arg-type]


class TestHelperSpawn:
    def test_the_helper_is_a_detached_console_python_running_this_module(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "executable", r"C:\venv\Scripts\pythonw.exe")
        run = _Run(_ok())
        result = win_console.send_ctrl_break_via_helper(4242, run=run)
        argv, kwargs = run.calls[0]
        assert argv == [r"C:\venv\Scripts\python.exe", "-m", "nexus.util.win_console", "4242"]
        # No console of its own: the CLI's console is never detached. Not
        # CREATE_NEW_CONSOLE (a window flash) and not CREATE_NO_WINDOW (a
        # hidden console the helper would only have to free).
        assert kwargs["extra_creationflags"] == win_console.DETACHED_PROCESS
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert float(kwargs["timeout"]) > 0  # type: ignore[arg-type]
        assert result.sent is True  # non-vacuity: the answer round-tripped

    def test_a_refusal_comes_back_with_both_sessions(self) -> None:
        run = _Run(_ok().replace('"sent": true', '"sent": false').replace(
            '"refused": false', '"refused": true').replace(
            '"target_session": null', '"target_session": 3').replace(
            '"own_session": null', '"own_session": 1'))
        result = win_console.send_ctrl_break_via_helper(9, run=run)
        assert (result.sent, result.refused, result.target_session, result.own_session) == (False, True, 3, 1)

    def test_structlog_noise_before_the_answer_is_ignored(self) -> None:
        stdout = "2026-10-05 [info] win_console_attach_failed pid=9\n" + _ok() + "\n"
        assert win_console.send_ctrl_break_via_helper(9, run=_Run(stdout)).sent is True

    @pytest.mark.parametrize(
        "run",
        [
            _Run("", raises=subprocess.TimeoutExpired(["x"], 1)),
            _Run("", raises=OSError("no interpreter")),
            _Run("not json at all"),
            _Run("", rc=1),
        ],
        ids=["timeout", "oserror", "garbage", "empty-nonzero"],
    )
    def test_a_helper_that_cannot_answer_is_not_a_send_and_never_raises(self, run: _Run) -> None:
        result = win_console.send_ctrl_break_via_helper(9, run=run)
        assert run.calls, "the spawner was never called: the test is vacuous"
        assert result.sent is False and result.refused is False
        assert result.stage == "helper"

    def test_an_invalid_pid_never_spawns_anything(self) -> None:
        run = _Run(_ok())
        assert win_console.send_ctrl_break_via_helper(0, run=run).stage == "invalid"
        assert run.calls == []

    def test_main_prints_one_json_result_and_exits_zero(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        api = _Api()
        monkeypatch.setattr(win_console, "ctypes_win_console_api", lambda: api)
        assert win_console._helper_main(["4242"]) == 0
        printed = capsys.readouterr().out.strip().splitlines()[-1]
        assert json.loads(printed)["sent"] is True
        assert api.calls == ["free", "attach", "send", "free"]  # the helper path, no re-attach


class TestRequestGracefulStopUsesTheHelper:
    def test_production_windows_stop_runs_the_helper_and_never_binds_the_clis_console(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sent: list[int] = []

        def helper(pid: int, **_kw: object) -> ConsoleBreakResult:
            sent.append(pid)
            return ConsoleBreakResult(sent=True)

        def forbidden() -> object:
            raise AssertionError("the CLI process must not build or use its own console API")

        monkeypatch.setattr(win_console, "send_ctrl_break_via_helper", helper)
        monkeypatch.setattr(win_console, "ctypes_win_console_api", forbidden)
        result = sr.request_graceful_stop(321, platform="win32")
        assert sent == [321]
        assert result.sent is True

    def test_an_injected_console_api_still_runs_in_process_for_the_tests_that_script_it(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            win_console, "send_ctrl_break_via_helper",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("helper used with an injected api")),
        )
        api = _Api()
        assert sr.request_graceful_stop(5, platform="win32", console_api=api).sent is True
        assert api.calls[0] == "free"

    def test_the_helper_refusal_reaches_the_graceful_stop_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            win_console, "send_ctrl_break_via_helper",
            lambda pid, **_k: ConsoleBreakResult(
                sent=False, refused=True, stage="attach", error=5, target_session=3, own_session=1,
            ),
        )
        r = sr.request_graceful_stop(5, platform="win32")
        assert (r.refused, r.target_session, r.own_session) == (True, 3, 1)

    def test_posix_never_reaches_the_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            win_console, "send_ctrl_break_via_helper",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("posix used the console helper")),
        )
        monkeypatch.setattr(nx_argv_mod, "_platform", lambda: "linux")
        killed: list[tuple[int, int]] = []
        monkeypatch.setattr(sr.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        assert sr.request_graceful_stop(77, platform="linux").sent is True
        assert killed and killed[0][0] == 77


class TestRunBoundedCarriesTheDetachedFlag:
    """The helper runs under ``run_bounded`` (the repo ratchet wants every capture-with-timeout
    spawn bounded); ``extra_creationflags`` is how it asks for ``DETACHED_PROCESS`` without losing
    the process-group kill ``run_bounded`` is for."""

    def test_the_flag_is_merged_into_the_windows_group_flag(self) -> None:
        from nexus.bounded_subprocess import _isolation_kwargs

        group = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        merged = _isolation_kwargs({"creationflags": group}, win_console.DETACHED_PROCESS)
        assert merged == {"creationflags": group | win_console.DETACHED_PROCESS}

    def test_posix_isolation_is_untouched(self) -> None:
        from nexus.bounded_subprocess import _isolation_kwargs

        assert _isolation_kwargs({"start_new_session": True}, win_console.DETACHED_PROCESS) == {
            "start_new_session": True,
        }

    def test_no_extra_flag_leaves_the_base_exactly_as_it_was(self) -> None:
        from nexus.bounded_subprocess import _isolation_kwargs

        base = {"creationflags": 0x200}
        assert _isolation_kwargs(base, 0) is base

    def test_the_real_spawn_receives_the_merged_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import nexus.bounded_subprocess as bs

        seen: dict[str, object] = {}

        class _Proc:
            args = ["x"]
            returncode = 0

            def communicate(self, input=None, timeout=None):  # noqa: A002, ANN001, ANN201
                return "", ""

        def popen(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
            seen.update(kwargs)
            return _Proc()

        monkeypatch.setattr(bs.subprocess, "Popen", popen)
        monkeypatch.setattr(bs, "isolation_popen_kwargs", lambda: {"creationflags": 0x200}, raising=False)
        monkeypatch.setattr("nexus.util.process_group.isolation_popen_kwargs", lambda: {"creationflags": 0x200})
        bs.run_bounded(["x"], timeout=5, extra_creationflags=win_console.DETACHED_PROCESS)
        assert seen["creationflags"] == 0x200 | win_console.DETACHED_PROCESS
