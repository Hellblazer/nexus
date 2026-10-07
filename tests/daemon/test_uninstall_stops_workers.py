# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx uninstall`` stops the nexus background workers (RDR-224, nexus-7xzc1).

Measured on nx-clean-win11 (T2 ``nexus_rdr/224-windows-self-install``): an
``nx store put`` started the aspect worker, ``nx uninstall --yes --remove-data``
stopped only the service stack, and the worker kept
``aspect_worker_daemon.crash.log`` open, so ``.config\\nexus`` could not be
removed (WinError 32), ``.cache\\nexus`` was left, and part of the generation
stayed locked. ``nx daemon service stop`` leaves the worker by design; only
``restart-stale`` stopped it, and that respawns it.

Uninstall now stops every worker it can identify from nexus's own records,
after the service stop and before any data removal: aspect workers from their
leases (``service_registry.stop_tier_holders``), the detached ``nx taxonomy
label`` run from the pid file its spawner writes, and MinerU through
``nx mineru stop`` when nexus started it (``mineru.pid``). Each pid is
re-checked against its live command line before it is signalled.

The workers here are real child processes whose argv carries the worker's
command; the unit manager and the stop verb are fakes, and the platform is
injected through the autostart layout.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("launchd_uid")

from nexus.commands import daemon as daemon_cmd
from nexus.daemon import installer
from nexus.daemon.aspect_worker_daemon import TIER as ASPECT_TIER
from nexus.daemon.service_registry import ServiceRegistry
from tests._child_process import popen_in_group

_ASPECT_ARGV = ["-m", "nexus.cli", "daemon", "aspect-worker", "start"]
_LABEL_ARGV = ["-m", "nexus.cli", "taxonomy", "label"]


def _spawn(tail: list[str]) -> subprocess.Popen[bytes]:
    """A live process whose command line reads like the worker's.

    Spawned in its own process group, as production spawns every worker this
    uninstall can stop: the Windows stop is a ``CTRL_BREAK`` addressed to the
    pid, and a pid that is not a group id sends the break to every process on
    the console, the test runner and its shell included.
    """
    return popen_in_group(
        [sys.executable, "-c", "import time; time.sleep(120)", *tail],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


class _Manager:
    """The unit manager and ``nx`` shell-outs: every argv is recorded, all succeed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, timeout, **_kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request) -> tuple[Path, _Manager]:
    platform = getattr(request, "param", "win32")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(daemon_cmd, "_autostart_platform", lambda: platform)
    monkeypatch.setattr(daemon_cmd, "_resolve_nx_bin", lambda: ["/opt/conexus/bin/nx"])
    config = tmp_path / "cfg"
    config.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(config))
    manager = _Manager()
    monkeypatch.setattr(installer, "run_bounded", manager)
    monkeypatch.setattr(installer, "_windows_task_registered", lambda: False)
    monkeypatch.setattr(installer, "_probe_survivors", lambda *, tier: ())
    monkeypatch.setattr(installer, "_stop_service_stack_best_effort", lambda: (True, None))
    install_dir = daemon_cmd._autostart_install_dir()
    install_dir.mkdir(parents=True)
    (install_dir / daemon_cmd._autostart_filename_service()).write_text("<unit/>")
    return config, manager


def _publish_aspect(config: Path, pid: int, tenant: str = "default") -> None:
    ServiceRegistry(dir=config, tier=ASPECT_TIER).publish(
        tenant, endpoint={"pid": pid}, version="7.73.0", owner_token=f"tok-{tenant}",
    )


def _reap(*procs: subprocess.Popen[bytes]) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize("layout", ["win32", "darwin", "linux"], indirect=True)
def test_uninstall_stops_every_aspect_worker_and_removes_the_data_dir(layout) -> None:
    config, _ = layout
    workers = [_spawn(_ASPECT_ARGV), _spawn(_ASPECT_ARGV)]
    try:
        _publish_aspect(config, workers[0].pid, "default")
        _publish_aspect(config, workers[1].pid, "acme")
        (config / "aspect_worker_daemon.crash.log").write_text("held open on Windows")

        report = installer.uninstall_daemon(confirm=True, remove_data=True)

        for proc in workers:
            assert proc.wait(timeout=15) is not None
        assert report.workers_stopped == tuple(sorted(p.pid for p in workers))
        assert "aspect worker" in report.message
        assert report.data_removed, report.warnings
        assert not config.exists()
    finally:
        _reap(*workers)


def test_a_recycled_aspect_worker_pid_is_left_running(layout) -> None:
    config, _ = layout
    stranger = _spawn(["unrelated-program"])
    try:
        _publish_aspect(config, stranger.pid)

        report = installer.uninstall_daemon(confirm=True, remove_data=False)

        assert stranger.poll() is None, "a pid running something else is never signalled"
        assert report.workers_stopped == ()
        assert ServiceRegistry(dir=config, tier=ASPECT_TIER).records() == []
    finally:
        _reap(stranger)


def test_the_workers_are_stopped_without_remove_data_too(layout) -> None:
    """A worker left running after uninstall runs against a removed install."""
    config, _ = layout
    worker = _spawn(_ASPECT_ARGV)
    try:
        _publish_aspect(config, worker.pid)
        report = installer.uninstall_daemon(confirm=True, remove_data=False)
        assert worker.wait(timeout=15) is not None
        assert report.workers_stopped == (worker.pid,)
    finally:
        _reap(worker)


def test_the_stop_runs_after_the_service_stop_and_before_data_removal(
    layout, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = layout
    order: list[str] = []
    monkeypatch.setattr(
        installer, "_stop_service_stack_best_effort",
        lambda: (order.append("service"), (True, None))[1],
    )

    def workers(config_dir: Path):
        order.append("workers" if config_dir.exists() else "workers-after-data-removal")
        return (), []

    monkeypatch.setattr(installer, "_stop_background_workers", workers)

    installer.uninstall_daemon(confirm=True, remove_data=True)

    assert order == ["service", "workers"]
    assert not config.exists()


def test_the_preview_names_the_workers_and_stops_nothing(layout) -> None:
    config, _ = layout
    worker = _spawn(_ASPECT_ARGV)
    try:
        _publish_aspect(config, worker.pid)
        report = installer.uninstall_daemon(confirm=False, remove_data=True)
        assert "background workers" in report.message
        assert worker.poll() is None
    finally:
        _reap(worker)


def test_the_deferred_labeling_run_is_stopped_from_its_pid_file(layout) -> None:
    config, _ = layout
    labeler = _spawn(_LABEL_ARGV)
    try:
        (config / installer.DEFERRED_LABELING_PID_NAME).write_text(f"{labeler.pid}\n")

        report = installer.uninstall_daemon(confirm=True, remove_data=False)

        assert labeler.wait(timeout=15) is not None
        assert labeler.pid in report.workers_stopped
        assert not (config / installer.DEFERRED_LABELING_PID_NAME).exists()
    finally:
        _reap(labeler)


def test_a_stale_labeling_pid_file_naming_another_program_is_left_alone(layout) -> None:
    config, _ = layout
    stranger = _spawn(["unrelated-program"])
    try:
        (config / installer.DEFERRED_LABELING_PID_NAME).write_text(f"{stranger.pid}\n")
        report = installer.uninstall_daemon(confirm=True, remove_data=False)
        assert stranger.poll() is None
        assert report.workers_stopped == ()
        assert not (config / installer.DEFERRED_LABELING_PID_NAME).exists()
    finally:
        _reap(stranger)


def test_mineru_is_stopped_through_its_verb_only_when_nexus_started_it(layout) -> None:
    config, manager = layout
    installer.uninstall_daemon(confirm=True, remove_data=False)
    assert not any(argv[-2:] == ["mineru", "stop"] for argv in manager.calls)

    (config / "mineru.pid").write_text('{"pid": 999999999, "port": 1}')
    installer.uninstall_daemon(confirm=True, remove_data=False)
    assert ["/opt/conexus/bin/nx", "mineru", "stop"] in manager.calls

