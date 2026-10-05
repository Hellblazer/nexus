# SPDX-License-Identifier: AGPL-3.0-or-later
"""A deliberate stop is authoritative against the Task Scheduler launcher
(RDR-224, nexus-f9bgu.33; code review S3, critique S1).

The launcher respawns the supervisor after any non-zero exit. Three paths turn
a user's stop into a non-zero exit or a missed stop: the hard-kill fallback
(``TerminateProcess`` exits 1), a ``CTRL_BREAK`` that lands before the
supervisor installed its handler (the CRT default ends the process with
``0xC000013A``), and a stop that arrives while the launcher sits in its
throttle sleep (the CLI finds nothing and says "already stopped"). The stop now
writes a marker before it signals; the launcher does not respawn while the
marker is not older than its last spawn; a start clears it.

Every platform and clock is injected, so these run on every host and none of
them skip-passes.
"""
from __future__ import annotations

import signal
import json
import os
from pathlib import Path

import pytest

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import service_registry as sr
from nexus.daemon import storage_service_daemon as ssd
from nexus.daemon import windows_autostart
from nexus.db import service_endpoint

SCOPE = "S-1-5-21-111-222-333-1001"
TIER = "storage_service"
_EMPTY_SWEEP = sr.ProcessSweepResult(available=True, error=None, found=(), stubborn=())


#: Every ``open_private`` the code under test makes, recorded. The Windows ACL step is the
#: seam: off Windows it cannot run, so the POSIX arm creates the file (0600) in its place.
OPENED: list[Path] = []


@pytest.fixture(autouse=True)
def _private_open(monkeypatch: pytest.MonkeyPatch) -> None:
    OPENED.clear()
    real = sr.open_private

    def spy(path, flags, **kw):  # noqa: ANN001, ANN003
        OPENED.append(Path(path))
        return real(path, flags, platform="linux")

    monkeypatch.setattr(sr, "open_private", spy)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return tmp_path


def _marker(cfg: Path) -> Path:
    return sr.stop_marker_path(cfg, TIER, SCOPE)


class TestMarkerPrimitives:
    def test_the_marker_lives_in_the_config_dir_named_for_tier_and_identity(self, cfg: Path) -> None:
        assert _marker(cfg) == cfg / f"storage_service_stop.{SCOPE}"

    def test_a_windows_write_records_the_request_time_and_goes_through_open_private(
        self, cfg: Path,
    ) -> None:
        path = sr.write_stop_marker(cfg, TIER, SCOPE, platform="win32", clock=lambda: 1234.5)
        assert path == _marker(cfg)
        assert OPENED == [_marker(cfg)], "the marker must be created owner-only"
        assert json.loads(path.read_text())["requested_at"] == 1234.5

    def test_a_posix_stop_writes_no_marker(self, cfg: Path) -> None:
        assert sr.write_stop_marker(cfg, TIER, SCOPE, platform="linux") is None
        assert not _marker(cfg).exists()

    def test_requested_since_is_true_only_for_a_marker_not_older_than_the_spawn(self, cfg: Path) -> None:
        sr.write_stop_marker(cfg, TIER, SCOPE, platform="win32", clock=lambda: 100.0)
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 99.0) is True
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 100.0) is True
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 100.5) is False

    def test_no_marker_is_no_request(self, cfg: Path) -> None:
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 0.0) is False

    def test_an_unparseable_marker_falls_back_to_its_file_time(self, cfg: Path) -> None:
        _marker(cfg).write_text("{not json")
        os.utime(_marker(cfg), (500.0, 500.0))
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 400.0) is True
        assert sr.stop_requested_since(cfg, TIER, SCOPE, 600.0) is False

    def test_clear_removes_it_and_is_idempotent(self, cfg: Path) -> None:
        sr.write_stop_marker(cfg, TIER, SCOPE, platform="win32")
        sr.clear_stop_marker(cfg, TIER, SCOPE)
        assert not _marker(cfg).exists()
        sr.clear_stop_marker(cfg, TIER, SCOPE)  # nothing there: no error

    def test_the_marker_is_per_identity(self, cfg: Path) -> None:
        sr.write_stop_marker(cfg, TIER, SCOPE, platform="win32", clock=lambda: 100.0)
        assert sr.stop_requested_since(cfg, TIER, "S-1-5-21-999-1-1-1", 0.0) is False


class _Launch:
    """Drives ``run_launcher`` with a scripted supervisor and an injected clock."""

    def __init__(self, cfg: Path, *, marker_before_start: float | None = None) -> None:
        self.cfg = cfg
        self.now = 1000.0
        self.spawns: list[float] = []
        self.sleeps: list[float] = []
        self.on_supervise: list[object] = []   # per spawn: exit code, or a callable run first
        self.on_sleep: list[object] = []
        if marker_before_start is not None:
            sr.write_stop_marker(cfg, TIER, SCOPE, platform="win32", clock=lambda: marker_before_start)

    def clock(self) -> float:
        return self.now

    def stop(self, at: float) -> None:
        sr.write_stop_marker(self.cfg, TIER, SCOPE, platform="win32", clock=lambda: at)

    def supervise(self, _cfg: Path) -> int:
        self.spawns.append(self.now)
        step = self.on_supervise.pop(0)
        if callable(step):
            step()
            step = self.on_supervise.pop(0)
        self.now += 10.0
        return int(step)  # type: ignore[call-overload]

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep:
            step = self.on_sleep.pop(0)
            if callable(step):
                step()
        self.now += seconds
        if len(self.sleeps) > 5:
            raise AssertionError("the launcher kept respawning: a stop marker was not honoured")

    def run(self) -> int:
        return windows_autostart.run_launcher(
            self.cfg,
            supervise=self.supervise,
            sleep=self.sleep,
            throttle_s=30.0,
            clock=self.clock,
            stop_requested=lambda since: sr.stop_requested_since(self.cfg, TIER, SCOPE, since),
        )


class TestLauncherHonoursAStop:
    def test_path_a_hard_kill_exit_one_after_a_stop_is_not_respawned(self, cfg: Path) -> None:
        run = _Launch(cfg)
        run.on_supervise = [lambda: run.stop(at=run.now + 1.0), 1]
        assert run.run() == 0
        assert len(run.spawns) == 1 and run.sleeps == []

    def test_path_b_the_break_before_the_handler_exit_code_is_not_respawned(self, cfg: Path) -> None:
        run = _Launch(cfg)
        run.on_supervise = [lambda: run.stop(at=run.now + 1.0), 0xC000013A]
        assert run.run() == 0
        assert len(run.spawns) == 1

    def test_path_c_a_stop_during_the_throttle_sleep_ends_the_launcher_at_wake(self, cfg: Path) -> None:
        run = _Launch(cfg)
        run.on_supervise = [3]  # a crash, no stop yet
        run.on_sleep = [lambda: run.stop(at=run.now + 5.0)]
        assert run.run() == 0
        assert len(run.spawns) == 1, "the launcher respawned after the user had said stop"
        assert run.sleeps == [30.0]

    def test_a_crash_with_no_stop_is_still_respawned(self, cfg: Path) -> None:
        # Non-vacuity for the three paths above: without a marker the same exits respawn.
        run = _Launch(cfg)
        run.on_supervise = [1, 0xC000013A, 0]
        assert run.run() == 0
        assert len(run.spawns) == 3 and run.sleeps == [30.0, 30.0]

    def test_a_marker_older_than_the_launcher_start_is_ignored(self, cfg: Path) -> None:
        run = _Launch(cfg, marker_before_start=500.0)  # a previous session's stop
        run.on_supervise = [2, 0]
        assert run.run() == 0
        assert len(run.spawns) == 2, "an old stop must not stop a launcher started after it"

    def test_a_marker_newer_than_the_launcher_start_but_before_the_first_spawn_wins(self, cfg: Path) -> None:
        run = _Launch(cfg, marker_before_start=1001.0)  # stop landed as the launcher came up
        run.on_supervise = [0]
        assert run.run() == 0
        assert run.spawns == []

    def test_an_unreadable_stop_check_never_stops_the_launcher(self, cfg: Path) -> None:
        run = _Launch(cfg)
        run.on_supervise = [1, 0]

        def boom(_since: float) -> bool:
            raise OSError("marker unreadable")

        assert windows_autostart.run_launcher(
            cfg, supervise=run.supervise, sleep=run.sleep, throttle_s=1.0,
            clock=run.clock, stop_requested=boom,
        ) == 0
        assert len(run.spawns) == 2


class TestStopWritesTheMarkerBeforeItSignals:
    def _lease(self, cfg: Path, pid: int) -> None:
        reg = sr.ServiceRegistry(dir=cfg, tier=TIER)
        reg.publish(
            sr.service_identity(), endpoint={"host": "127.0.0.1", "port": 1, "pid": pid},
            version="v", owner_token="o", payload={"supervisor_pid": pid},
        )

    def test_the_marker_exists_when_the_break_is_sent(
        self, cfg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marker = sr.stop_marker_path(cfg, TIER, sr.service_identity())
        seen: list[bool] = []

        class _Api:
            def free_console(self) -> bool:
                return True

            def attach_console(self, pid: int) -> tuple[bool, int]:
                return True, 0

            def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
                seen.append(marker.exists())
                return True, 0

            def attach_parent_console(self) -> tuple[bool, int]:
                return True, 0

            def session_of(self, pid: int) -> int | None:
                return 1

        self._lease(cfg, os.getpid())
        monkeypatch.setattr(ssd, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(ssd, "_pid_is_running", lambda pid: False)  # "exits" at once
        monkeypatch.setattr(sr, "sweep_matching_processes", lambda *a, **k: _EMPTY_SWEEP)
        ssd.stop_storage_service(config_dir=cfg, platform="win32", console_api=_Api())
        assert seen == [True], "the marker must be on disk before CTRL_BREAK is sent"

    def test_an_already_stopped_stop_still_leaves_the_marker(
        self, cfg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Path c: the launcher is asleep and nothing is running, so the CLI finds nothing.
        monkeypatch.setattr(sr, "sweep_matching_processes", lambda *a, **k: _EMPTY_SWEEP)
        outcome = ssd.stop_storage_service(config_dir=cfg, platform="win32")
        assert outcome.source == "none"
        assert sr.stop_marker_path(cfg, TIER, sr.service_identity()).exists()

    def test_a_marker_that_cannot_be_written_never_stops_the_stop(
        self, cfg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def refuse(*_a: object, **_k: object) -> None:
            raise OSError("no ACL on this filesystem")

        monkeypatch.setattr(ssd, "write_stop_marker", refuse)
        monkeypatch.setattr(sr, "sweep_matching_processes", lambda *a, **k: _EMPTY_SWEEP)
        outcome = ssd.stop_storage_service(config_dir=cfg, platform="win32")
        assert outcome.source == "none"  # it ran to completion

    def test_a_posix_stop_writes_no_marker(self, cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sr, "sweep_matching_processes", lambda *a, **k: _EMPTY_SWEEP)
        ssd.stop_storage_service(config_dir=cfg, platform="linux")
        assert list(cfg.glob("storage_service_stop.*")) == []


class TestAStartClearsIt:
    def test_ensure_storage_supervisor_clears_the_marker_before_it_looks_for_a_lease(
        self, cfg: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sr.write_stop_marker(cfg, TIER, sr.service_identity(), platform="win32")
        marker = sr.stop_marker_path(cfg, TIER, sr.service_identity())
        assert marker.exists()  # non-vacuity

        class _Lease:
            endpoint: dict[str, object] = {}

        monkeypatch.setattr(service_endpoint, "discover_storage_service_lease", lambda *a, **k: _Lease())
        monkeypatch.setattr(sr, "reclaim_lease_if_dead_owner", lambda *a, **k: False)
        monkeypatch.setattr(ssd, "_raise_or_warn_on_artifact_mismatch", lambda *a, **k: None)
        daemon_cmd.ensure_storage_supervisor(cfg)
        assert not marker.exists(), "a start must clear the stop marker, even when it short-circuits"


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="Windows arms faulthandler instead of an interval timer")
def test_the_per_test_watchdog_is_armed_for_this_file() -> None:
    """A hang here fails instead of stalling the run (tests/daemon/_watchdog.py)."""
    assert signal.getitimer(signal.ITIMER_REAL)[0] > 0, (
        "the autouse watchdog fixture did not arm for this module: check WATCHED_MODULES"
    )
