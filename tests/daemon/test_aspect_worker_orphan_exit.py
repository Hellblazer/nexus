# SPDX-License-Identifier: AGPL-3.0-or-later
"""An aspect worker must not outlive what it serves (nexus-wl9pk).

Found 2026-11-02: 24 ``nx daemon aspect-worker start --config-dir
<TMPDIR>/nx-local-service-gate.dual.XXXXXX`` processes, parent pid 1, the oldest
from Oct 31. Each was spawned (spawn-if-absent) by an ``nx`` call a gate made
against its scratch service; the gate stopped the service and deleted the
scratch directory but never the worker, and the worker kept polling a dead
engine and re-created its config directory on every heartbeat.

Two halves are pinned here, with REAL worker processes (a fake ``claude`` on
PATH satisfies the credential guard; the engine endpoint is a closed loopback
port, so no write reaches any store):

* the worker exits within seconds of its config directory being removed, and
  does not bring the directory back;
* the worker exits once every reclaim sweep has failed for the stated bound,
  and not before; with the bound off it stays up (the non-vacuity control);
* ``nx daemon aspect-worker stop --config-dir D`` stops the worker leased in D,
  by its lease record, and leaves a worker serving another directory alone.

Every process a test starts is stopped by pid in its own ``finally``.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon import aspect_worker_daemon as awd
from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor, ttl_for_tier

_POSIX = sys.platform != "win32"
_posix_only = pytest.mark.skipif(not _POSIX, reason="removing a directory under a running worker needs POSIX unlink semantics")

#: Seconds a worker may take to notice a removed directory (the heartbeat tick is 1 s).
_ORPHAN_EXIT_BOUND_S = 15.0


# ── unit: the shared primitive ───────────────────────────────────────────────


def _supervisor(config_dir: Path, *, stand_down: bool) -> ServiceSupervisor:
    registry = ServiceRegistry(dir=config_dir, tier=awd.TIER, ttl=ttl_for_tier(awd.TIER))
    sup = ServiceSupervisor(
        registry, scope_key="default", version="t", endpoint_provider=lambda: {"pid": os.getpid()},
        stand_down_when_dir_gone=stand_down,
    )
    sup.publish_once()
    return sup


def test_a_supervisor_that_opted_in_stands_down_and_leaves_the_directory_gone(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    sup = _supervisor(cfg, stand_down=True)
    sup.heartbeat_tick()
    assert not sup.orphaned and cfg.is_dir(), "a present directory is a healthy owner"
    shutil.rmtree(cfg)
    sup.heartbeat_tick()
    assert sup.orphaned
    assert not cfg.exists(), "the stand-down must not re-create the directory it found missing"
    sup.heartbeat_tick()  # idempotent
    assert not cfg.exists()


def test_without_the_opt_in_the_heartbeat_re_creates_the_directory(tmp_path: Path) -> None:
    """The control: this is the behaviour the 24 orphans showed, and still the
    storage supervisor's. It is why the stand-down is a per-tier opt-in and not
    an unconditional change to the shared heartbeat."""
    cfg = tmp_path / "cfg"
    sup = _supervisor(cfg, stand_down=False)
    shutil.rmtree(cfg)
    sup.heartbeat_tick()
    assert not sup.orphaned
    assert cfg.is_dir(), "control: the unguarded heartbeat brings the directory back"


def test_dir_exists_reads_a_stat_error_as_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ServiceRegistry(dir=tmp_path, tier=awd.TIER, ttl=ttl_for_tier(awd.TIER))

    def _denied(_p: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("nexus.daemon.service_registry.os.stat", _denied)
    assert registry.dir_exists() is True, "an unreadable directory is not a removed one"


# ── unit: the daemon's two exits, on fakes ───────────────────────────────────


class _Worker:
    def start(self) -> None: ...
    def stop(self, timeout: float = 0) -> None: ...


class _Queue:
    def __init__(self, behaviour: Callable[[], int]) -> None:
        self._behaviour = behaviour

    def reclaim_stale(self, _t: float, *, count_attempt: bool = True) -> int:
        return self._behaviour()

    def close(self) -> None: ...


class _Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _daemon(cfg: Path, behaviour: Callable[[], int], mono: _Mono, bound: float | None = 100.0) -> awd.AspectWorkerDaemon:
    return awd.AspectWorkerDaemon(
        config_dir=cfg, tenant="default",
        worker_factory=_Worker, queue_factory=lambda: _Queue(behaviour),
        heartbeat_interval=0.02, reclaim_interval=3600.0,
        engine_unreachable_exit_seconds=bound, monotonic=mono,
    )


def _wait(pred: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_the_daemon_requests_its_own_stop_when_its_directory_is_removed(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    d = _daemon(cfg, lambda: 0, _Mono())
    d.start()
    try:
        assert not d._stop.is_set()
        shutil.rmtree(cfg)
        assert _wait(d._stop.is_set), "the heartbeat loop must stop the daemon once the directory is gone"
    finally:
        d.stop()
    assert not cfg.exists(), "stop() must not mark or relinquish into a directory that is gone"


def test_unreachable_sweeps_stop_the_daemon_only_after_the_bound(tmp_path: Path) -> None:
    mono = _Mono()
    calls = {"n": 0}

    def _down() -> int:
        calls["n"] += 1
        raise ConnectionError("engine unreachable")

    d = _daemon(tmp_path / "cfg", _down, mono, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: calls["n"] >= 1), "the reclaim-first sweep runs at start"
        time.sleep(0.2)  # several heartbeat ticks, clock not advanced
        assert not d._stop.is_set(), "a failing sweep alone is not an exit"
        mono.now += 99.0
        time.sleep(0.2)
        assert not d._stop.is_set(), "99 s of a 100 s bound"
        mono.now += 2.0
        assert _wait(d._stop.is_set), "past the bound the daemon stops itself"
        assert d._left_for_unreachable_engine
    finally:
        d.stop()


def test_one_good_sweep_restarts_the_unreachable_clock(tmp_path: Path) -> None:
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", lambda: 0, mono, bound=100.0)
    d._unreachable_since = mono.now - 90.0  # failing for 90 s
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is None), "the start sweep succeeded: the clock is cleared"
        mono.now += 500.0
        time.sleep(0.2)
        assert not d._stop.is_set()
    finally:
        d.stop()


def test_a_zero_bound_turns_the_exit_off(tmp_path: Path) -> None:
    mono = _Mono()

    def _down() -> int:
        raise ConnectionError("engine unreachable")

    d = _daemon(tmp_path / "cfg", _down, mono, bound=0)
    d.start()
    try:
        time.sleep(0.2)
        mono.now += 10_000_000.0
        time.sleep(0.2)
        assert not d._stop.is_set()
    finally:
        d.stop()


# ── real processes ───────────────────────────────────────────────────────────


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RealWorker:
    """One real ``nx daemon aspect-worker start`` process in a scratch config dir."""

    def __init__(self, root: Path, name: str, extra: list[str]) -> None:
        self.cfg = root / name / "cfg"
        self.cfg.mkdir(parents=True)
        home = root / name / "home"
        home.mkdir()
        bindir = root / name / "bin"
        bindir.mkdir()
        # The credential guard wants an LLM backend on PATH; this one is never run.
        if _POSIX:
            claude = bindir / "claude"
            claude.write_text("#!/bin/sh\nexit 0\n")
            claude.chmod(0o755)
        else:
            (bindir / "claude.cmd").write_text("@exit /b 0\r\n")
        env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("NX_", "NEXUS_", "ANTHROPIC", "CLAUDE"))
        }
        env.update({
            "HOME": str(home), "USERPROFILE": str(home),
            "PATH": os.pathsep.join([str(bindir), os.environ.get("PATH", "")]),
            "NEXUS_CONFIG_DIR": str(self.cfg),
            "NX_NO_TELEMETRY": "1",
            "NX_SERVICE_URL": f"http://127.0.0.1:{_closed_port()}",
            "NX_SERVICE_TOKEN": "unreachable-endpoint-test",
            "NX_ALLOW_PROD_WRITE": "nexus-wl9pk test: loopback port nothing listens on, no write reaches any store",
        })
        self.env = env
        self.log = root / name / "worker.out"
        self._out = open(self.log, "wb")  # noqa: SIM115 - closed in stop()
        self.proc = subprocess.Popen(  # noqa: S603 - argv is this interpreter and a fixed module
            [sys.executable, "-m", "nexus.cli", "daemon", "aspect-worker", "start",
             "--config-dir", str(self.cfg), *extra],
            env=env, stdin=subprocess.DEVNULL, stdout=self._out, stderr=subprocess.STDOUT,
        )
        self.lease = self.cfg / "aspect_worker_addr.default"

    def wait_for_lease(self, seconds: float = 60.0) -> float:
        assert _wait(lambda: self.lease.exists() or self.proc.poll() is not None, seconds)
        assert self.proc.poll() is None, f"the worker died at start:\n{self.output()}"
        return time.monotonic()

    def output(self) -> str:
        self._out.flush()
        return self.log.read_text(errors="replace")[-2000:]

    def alive(self) -> bool:
        return self.proc.poll() is None

    def wait_exit(self, seconds: float) -> int | None:
        try:
            return self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        self._out.close()


@pytest.fixture
def workers(tmp_path: Path) -> Iterator[Callable[..., _RealWorker]]:
    made: list[_RealWorker] = []

    def _make(name: str, *extra: str) -> _RealWorker:
        w = _RealWorker(tmp_path, name, list(extra))
        made.append(w)
        return w

    try:
        yield _make
    finally:
        for w in made:
            w.stop()


@_posix_only
def test_a_real_worker_exits_when_its_config_dir_is_removed(workers: Callable[..., _RealWorker]) -> None:
    w = workers("orphan")
    w.wait_for_lease()
    time.sleep(1.5)  # a few heartbeats with the directory present: it must stay up
    assert w.alive(), w.output()
    shutil.rmtree(w.cfg)
    t0 = time.monotonic()
    rc = w.wait_exit(_ORPHAN_EXIT_BOUND_S)
    assert rc is not None, f"still running {_ORPHAN_EXIT_BOUND_S}s after its config dir was removed:\n{w.output()}"
    assert rc == 0, w.output()
    assert time.monotonic() - t0 < _ORPHAN_EXIT_BOUND_S
    assert not w.cfg.exists(), "the exiting worker re-created the directory it was serving"
    assert "config_dir_gone" in w.output()


def test_a_real_worker_exits_once_its_engine_has_been_unreachable_for_the_bound(workers: Callable[..., _RealWorker]) -> None:
    bound = 4.0
    w = workers("unreachable", "--engine-unreachable-exit-seconds", str(bound))
    t_ready = w.wait_for_lease()
    rc = w.wait_exit(bound + _ORPHAN_EXIT_BOUND_S)
    assert rc is not None, f"still running {bound + _ORPHAN_EXIT_BOUND_S}s after start against a closed port:\n{w.output()}"
    assert rc == 0, w.output()
    elapsed = time.monotonic() - t_ready
    assert elapsed >= bound - 1.0, f"exited after {elapsed:.1f}s, before the {bound}s bound:\n{w.output()}"
    assert "engine_unreachable_exit" in w.output()


def test_the_unreachable_exit_is_the_bound_and_not_something_else(workers: Callable[..., _RealWorker]) -> None:
    """Non-vacuity: the same worker against the same closed port stays up when
    the bound is off, so the exit above is caused by the bound."""
    w = workers("control", "--engine-unreachable-exit-seconds", "0")
    w.wait_for_lease()
    assert w.wait_exit(8.0) is None, f"exited with the bound off:\n{w.output()}"
    assert w.alive()


@_posix_only
def test_the_stop_verb_stops_the_worker_leased_in_that_dir_and_no_other(workers: Callable[..., _RealWorker]) -> None:
    mine = workers("mine", "--engine-unreachable-exit-seconds", "0")
    other = workers("other", "--engine-unreachable-exit-seconds", "0")
    mine.wait_for_lease()
    other.wait_for_lease()
    done = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "nexus.cli", "daemon", "aspect-worker", "stop", "--config-dir", str(mine.cfg)],
        env=mine.env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(mine.proc.pid) in done.stdout, done.stdout
    assert mine.wait_exit(10.0) is not None, "the worker named by the lease was not stopped"
    assert other.alive(), "stopping one directory's worker must not touch another's"
    assert not mine.lease.exists(), "the stop must drop the lease it stopped"
    again = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "nexus.cli", "daemon", "aspect-worker", "stop", "--config-dir", str(mine.cfg)],
        env=mine.env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert again.returncode == 0 and "No aspect-worker running" in again.stdout, "idempotent"
    assert other.alive()
