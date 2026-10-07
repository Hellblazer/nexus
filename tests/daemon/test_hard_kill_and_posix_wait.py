# SPDX-License-Identifier: AGPL-3.0-or-later
"""``hard_kill_pid`` swallows only gone-pid errors, and POSIX stop confirms the
kill by exit (RDR-224, nexus-f9bgu.33, code review m3).

Two behaviours that RDR-224 changed on POSIX in passing, now stated and pinned:

* ``hard_kill_pid`` used to swallow EVERY ``OSError`` once it became shared. It
  now swallows ESRCH and EPERM on POSIX (what each stop site caught before it was
  shared) and, on Windows, WinError 87 and access denied; any other error
  propagates, so a kill that failed for another reason is not reported as "the
  process was already dead".
* ``stop_storage_service`` follows its hard kill with ``wait_for_exit`` on every
  platform. POSIX used to send SIGKILL and move on; a supervisor that outlives
  the kill (an unreaped or foreign-uid process) is now reported as stubborn.
"""
from __future__ import annotations

import errno
from pathlib import Path

import pytest

from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd
from tests._module_seam import patch_in


class TestOnlyGonePidErrorsAreSwallowed:
    def test_a_bad_signal_number_is_not_a_dead_process_on_posix(self) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            with pytest.raises(OSError, match="Invalid argument"):
                sr.hard_kill_pid(4242, platform="linux")

    def test_the_same_errno_is_a_gone_pid_on_windows(self) -> None:
        # Non-vacuity for the test above: EINVAL IS swallowed where it means WinError 87.
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=OSError(errno.EINVAL, "The parameter is incorrect")):
            assert sr.hard_kill_pid(4242, platform="win32") is False

    def test_an_unrelated_windows_error_propagates(self) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=OSError(errno.EIO, "I/O error")):
            with pytest.raises(OSError, match="I/O error"):
                sr.hard_kill_pid(4242, platform="win32")

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_a_foreign_process_is_reported_not_raised_on_both(self, platform: str) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=PermissionError(13, "access denied")):
            assert sr.hard_kill_pid(4242, platform=platform) is False

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_a_delivered_kill_is_true(self, platform: str) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill") as kill:
            assert sr.hard_kill_pid(4242, platform=platform) is True
        assert kill.call_args.args[0] == 4242


class TestPosixStopConfirmsTheKillByExit:
    def _lease(self, cfg: Path, pid: int) -> None:
        reg = sr.ServiceRegistry(dir=cfg, tier="storage_service")
        reg.publish(
            sr.service_identity(), endpoint={"host": "127.0.0.1", "port": 1, "pid": pid},
            version="v", owner_token="o", payload={"supervisor_pid": pid},
        )

    def _stop(self, cfg: Path, monkeypatch: pytest.MonkeyPatch, *, survives_the_kill: bool) -> ssd.StopOutcome:
        pid = 4_242_424
        self._lease(cfg, pid)
        running = {"alive": True}
        monkeypatch.setattr(ssd, "_pid_is_alive", lambda p: running["alive"])
        monkeypatch.setattr(ssd, "_pid_is_running", lambda p: running["alive"])
        monkeypatch.setattr(sr, "pid_running", lambda p: running["alive"])
        monkeypatch.setattr(ssd, "_SUPERVISOR_STOP_GRACE", 0.1)  # it never exits on SIGTERM
        monkeypatch.setattr(
            ssd, "request_graceful_stop", lambda p, **k: sr.GracefulStopSend(pid=p, sent=True),
        )

        def kill(p: int, **_k: object) -> bool:
            if not survives_the_kill:
                running["alive"] = False
            return True

        monkeypatch.setattr(ssd, "hard_kill_pid", kill)
        monkeypatch.setattr(
            ssd, "wait_for_exit", lambda pids, **k: sr.wait_for_exit(pids, timeout_s=0.3),
        )
        monkeypatch.setattr(
            sr, "sweep_matching_processes",
            lambda *a, **k: sr.ProcessSweepResult(available=True, error=None, found=(), stubborn=()),
        )
        return ssd.stop_storage_service(config_dir=cfg, platform="linux")

    def test_a_supervisor_that_outlives_sigkill_is_reported_stubborn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outcome = self._stop(tmp_path, monkeypatch, survives_the_kill=True)
        assert outcome.stubborn == (4_242_424,)

    def test_a_supervisor_that_the_kill_ends_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outcome = self._stop(tmp_path, monkeypatch, survives_the_kill=False)
        assert outcome.stubborn == ()
        assert outcome.pids == (4_242_424,)  # non-vacuity: the stop did signal it
