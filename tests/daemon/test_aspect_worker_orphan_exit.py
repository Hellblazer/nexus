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
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from nexus.aspect_worker import AspectExtractionWorker
from nexus.commands.daemon import aspect_worker_group, aspect_worker_start_cmd
from nexus.daemon import aspect_worker_daemon as awd
from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    stop_tier_holders,
    ttl_for_tier,
)

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
    assert not sup.orphaned, "one missing-directory tick is not yet an orphan (debounce)"
    sup.heartbeat_tick()
    assert sup.orphaned
    assert not cfg.exists(), "the stand-down must not re-create the directory it found missing"
    sup.heartbeat_tick()  # idempotent
    assert not cfg.exists()


def test_a_single_transient_missing_directory_tick_does_not_orphan_the_supervisor(tmp_path: Path) -> None:
    """A remote or automounted filesystem can answer ENOENT once, and a
    rename-swap of the directory shows it missing for a moment. Two CONSECUTIVE
    misses are the stand-down; a miss between present ticks resets the count."""
    cfg = tmp_path / "cfg"
    sup = _supervisor(cfg, stand_down=True)
    parked = tmp_path / "parked"
    cfg.rename(parked)
    sup.heartbeat_tick()  # miss 1
    assert not sup.orphaned
    parked.rename(cfg)  # the directory is back
    sup.heartbeat_tick()  # present: the count resets
    assert not sup.orphaned
    cfg.rename(parked)
    sup.heartbeat_tick()  # miss 1 again, not miss 2
    assert not sup.orphaned, "the earlier miss must not carry over past a present tick"
    sup.heartbeat_tick()  # miss 2, consecutive
    assert sup.orphaned


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


# ── fix round 1: the stop verb is bound to its directory ─────────────────────


def _lease_naming(cfg: Path, pid: int) -> ServiceRegistry:
    """A lease in *cfg* whose holder is *pid*, as a SIGKILLed worker leaves it."""
    registry = ServiceRegistry(dir=cfg, tier=awd.TIER, ttl=ttl_for_tier(awd.TIER))
    registry.publish("default", endpoint={"pid": pid}, version="t", owner_token="tok-test")
    return registry


@pytest.mark.parametrize(
    ("command", "serves", "expect"),
    [
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b --tenant default", "/a/b", True),
        ("python -m nexus.cli daemon aspect-worker start --config-dir=/a/b --tenant default", "/a/b", True),
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b", "/a/b", True),
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b c --tenant default", "/a/b c", True),
        ('python -m nexus.cli daemon aspect-worker start --config-dir "/a/b c" --tenant default', "/a/b c", True),
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/other --tenant default", "/a/b", False),
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b2 --tenant default", "/a/b", False),
        ("python -m nexus.cli daemon aspect-worker start --tenant default", "/a/b", False),
        ("sleep 60 --config-dir /a/b", "/a/b", False),
    ],
)
def test_the_directory_bound_predicate_reads_the_config_dir_off_the_command_line(
    command: str, serves: str, expect: bool,
) -> None:
    predicate = awd.is_aspect_worker_command_serving(Path(serves))
    assert predicate(command) is expect, command


@_posix_only
def test_the_directory_bound_predicate_compares_real_paths(tmp_path: Path) -> None:
    """macOS spells a temp dir /var/... and /private/var/...; the command line
    carries one spelling and the caller may hold the other."""
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    command = f"python -m nexus.cli daemon aspect-worker start --config-dir {real} --tenant default"
    assert awd.is_aspect_worker_command_serving(alias)(command)
    assert awd.is_aspect_worker_command_serving(real)(command.replace(str(real), str(alias)))


@_posix_only
def test_a_lease_in_one_dir_naming_a_worker_of_another_leaves_that_worker_alone(
    tmp_path: Path, workers: Callable[..., _RealWorker],
) -> None:
    """A lease left by a SIGKILLed worker whose pid was recycled onto a worker
    of ANOTHER directory: the stop for the first directory must not stop it."""
    victim = workers("victim", "--engine-unreachable-exit-seconds", "0")
    victim.wait_for_lease()
    stale_dir = tmp_path / "stale-dir"
    registry = _lease_naming(stale_dir, victim.proc.pid)
    result = awd.stop_aspect_workers(stale_dir)
    assert victim.alive(), "the stop for directory A signalled the worker serving directory B"
    assert result.stopped == ()
    assert result.foreign == (victim.proc.pid,)
    assert len(registry.records()) == 1, "a live unmatched holder's lease is not relinquished"


@_posix_only
def test_the_stop_verb_reaches_the_worker_through_a_symlinked_spelling_of_its_dir(
    tmp_path: Path, workers: Callable[..., _RealWorker],
) -> None:
    w = workers("aliased", "--engine-unreachable-exit-seconds", "0")
    w.wait_for_lease()
    alias = tmp_path / "alias"
    alias.symlink_to(w.cfg)
    result = awd.stop_aspect_workers(alias)
    assert result.stopped == (w.proc.pid,), result
    assert w.wait_exit(10.0) is not None


# ── fix round 1: the verb does not report success for a holder it left alone ─


@pytest.fixture
def bystander() -> Iterator[subprocess.Popen[bytes]]:
    """A live process that is not an aspect worker; stopped by pid here."""
    proc = subprocess.Popen(  # noqa: S603 - this interpreter, a fixed sleep
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def test_the_stop_verb_exits_1_and_keeps_the_lease_of_a_live_holder_it_declined_to_signal(
    tmp_path: Path, bystander: subprocess.Popen[bytes],
) -> None:
    cfg = tmp_path / "cfg"
    registry = _lease_naming(cfg, bystander.pid)
    result = CliRunner().invoke(aspect_worker_group, ["stop", "--config-dir", str(cfg)])
    assert result.exit_code == 1, (result.output, result.exception)
    assert "No aspect-worker running" not in result.output, "success text for a holder left alive"
    assert str(bystander.pid) in result.output, result.output
    assert bystander.poll() is None, "the bystander must not be signalled"
    assert len(registry.records()) == 1, "the lease of a live unmatched holder must stay"


def test_uninstall_still_drops_a_recycled_pids_lease(tmp_path: Path, bystander: subprocess.Popen[bytes]) -> None:
    """The shared primitive's default is unchanged: teardown for uninstall
    drops the record of a dead holder whose pid now runs something else."""
    cfg = tmp_path / "cfg"
    registry = _lease_naming(cfg, bystander.pid)
    result = stop_tier_holders(dir=cfg, tier=awd.TIER, is_ours=lambda c: "aspect-worker" in c, grace_s=1.0)
    assert result.foreign == (bystander.pid,)
    assert registry.records() == []


# ── fix round 1: the unreachable clock is the engine, not the reclaim thread ─


class _ContactWorker:
    """A worker that reports engine contact as a real one does after each
    successful claim round trip (the daemon hands it the hook)."""

    def __init__(self) -> None:
        self.handler: Callable[[], None] | None = None
        self.released = 0
        self._stopping = False
        self._thread: Any = None

    def set_engine_contact_handler(self, handler: Callable[[], None] | None) -> None:
        self.handler = handler

    def start(self) -> None:
        def _claims() -> None:
            while not self._stopping:
                if self.handler is not None:
                    self.handler()
                time.sleep(0.02)

        self._thread = threading.Thread(target=_claims, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 0) -> None:
        self._stopping = True

    def release_held_claims(self) -> int:
        self.released += 1
        return 0


def _contact_daemon(
    cfg: Path, behaviour: Callable[[], int], mono: _Mono, worker: Any, *,
    bound: float | None = 100.0, stale: int = 300,
) -> awd.AspectWorkerDaemon:
    return awd.AspectWorkerDaemon(
        config_dir=cfg, tenant="default",
        worker_factory=lambda: worker, queue_factory=lambda: _Queue(behaviour),
        heartbeat_interval=0.02, reclaim_interval=3600.0, stale_timeout_seconds=stale,
        engine_unreachable_exit_seconds=bound, monotonic=mono,
    )


def _down() -> int:
    raise ConnectionError("reclaim route failing")


def test_a_worker_whose_claims_succeed_never_exits_on_a_failing_reclaim(tmp_path: Path) -> None:
    """The reclaim route alone failing (a 401 on that route, an older engine
    without it) while claim and complete work: the engine is reachable, so the
    one-hour exit must not fire."""
    mono = _Mono()
    worker = _ContactWorker()
    sweeps = {"n": 0}

    def _counted_down() -> int:
        sweeps["n"] += 1
        raise ConnectionError("reclaim route failing")

    d = _contact_daemon(tmp_path / "cfg", _counted_down, mono, worker, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: sweeps["n"] >= 1), "the reclaim-first sweep ran and failed (non-vacuity)"
        for _ in range(5):
            mono.now += 500.0  # far past the bound, repeatedly
            time.sleep(0.15)
            assert not d._stop.is_set(), "a worker with a live claim path was stopped by the reclaim clock"
    finally:
        d.stop()


def test_the_same_failing_reclaim_without_claim_contact_does_exit(tmp_path: Path) -> None:
    """The control: with no claim contact the clock runs, so the test above is
    held up by the contact and not by something that disables the exit."""
    mono = _Mono()
    worker = _ContactWorker()
    worker.set_engine_contact_handler = lambda handler: None  # type: ignore[method-assign]
    d = _contact_daemon(tmp_path / "cfg", _down, mono, worker, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is not None)
        mono.now += 500.0
        assert _wait(d._stop.is_set), "the unreachable exit must still fire when nothing reaches the engine"
    finally:
        d.stop()


def test_a_long_stale_window_cannot_push_the_backoff_past_the_bound(tmp_path: Path) -> None:
    """The backoff cap is the stale window (300 s by default); with a bound
    shorter than that, a recovered engine would go unnoticed until after the
    exit. The wait is clamped to half the bound."""
    d = _contact_daemon(tmp_path / "cfg", lambda: 0, _Mono(), _ContactWorker(), bound=100.0, stale=3600)
    d._reclaim_interval = 30.0
    for _ in range(10):
        d._back_off_after_failed_sweep()
    assert d._current_reclaim_wait <= 50.0, d._current_reclaim_wait


def test_the_unreachable_exit_skips_the_tuple_claim_release(tmp_path: Path) -> None:
    """Releasing held claims asks the dead engine and waits out its client
    timeouts; the leases lapse on their own."""
    mono = _Mono()
    worker = _ContactWorker()
    worker.set_engine_contact_handler = lambda handler: None  # type: ignore[method-assign]
    d = _contact_daemon(tmp_path / "cfg", _down, mono, worker, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is not None)
        mono.now += 500.0
        assert _wait(d._stop.is_set)
    finally:
        d.stop()
    assert worker.released == 0, "the unreachable-exit path must not wait on the dead engine"


def test_an_ordinary_stop_still_releases_the_tuple_claims(tmp_path: Path) -> None:
    worker = _ContactWorker()
    d = _contact_daemon(tmp_path / "cfg", lambda: 0, _Mono(), worker)
    d.start()
    d.stop()
    assert worker.released == 1


def test_the_bound_option_rejects_a_negative_value() -> None:
    result = CliRunner().invoke(aspect_worker_group, ["start", "--engine-unreachable-exit-seconds", "-5"])
    assert result.exit_code == 2, (result.output, result.exception)
    assert "-5" in result.output


def test_the_cli_default_for_the_bound_is_the_module_constant_and_one_hour() -> None:
    opt = next(p for p in aspect_worker_start_cmd.params if p.name == "engine_unreachable_exit_seconds")
    with click.Context(aspect_worker_start_cmd) as ctx:
        assert opt.get_default(ctx) == awd._ENGINE_UNREACHABLE_EXIT_S
    assert awd._ENGINE_UNREACHABLE_EXIT_S == 3600.0, "an hour: outlasts a Liquibase migration and an engine restart"


# ── fix round 1: the worker reports engine contact from its claim loop ───────


def _drive_loop(worker: Any, *, until: Callable[[], bool], seconds: float = 5.0) -> bool:
    worker.start()
    try:
        return _wait(until, seconds)
    finally:
        worker.stop(timeout=5.0)


def test_the_real_worker_reports_contact_after_a_claim_that_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    calls: list[int] = []
    w.set_engine_contact_handler(lambda: calls.append(1))
    monkeypatch.setattr(w._tuple_side, "supported", lambda: True)
    monkeypatch.setattr(w._tuple_side, "claim_batch", lambda park: ([], False))
    monkeypatch.setattr(w, "_claim_table_rows", lambda: [])
    assert _drive_loop(w, until=lambda: bool(calls)), "an answered (empty) claim is engine contact"


def test_the_real_worker_reports_no_contact_when_every_claim_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    calls: list[int] = []
    w.set_engine_contact_handler(lambda: calls.append(1))
    attempts: list[int] = []

    def _fail() -> list:
        attempts.append(1)
        raise ConnectionError("engine gone")

    monkeypatch.setattr(w._tuple_side, "supported", lambda: True)
    monkeypatch.setattr(w._tuple_side, "claim_batch", lambda park: ([], True))
    monkeypatch.setattr(w, "_claim_table_rows", _fail)
    assert not _drive_loop(w, until=lambda: bool(calls), seconds=1.0)
    assert attempts, "the loop must have tried (non-vacuity)"
    assert not calls


def test_a_table_only_worker_reports_contact_after_its_table_claim_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An engine without the aspects template: the table claim is the only
    round trip, and it must carry the contact signal on its own."""
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    calls: list[int] = []
    w.set_engine_contact_handler(lambda: calls.append(1))
    monkeypatch.setattr(w._tuple_side, "supported", lambda: False)
    monkeypatch.setattr(w, "_claim_table_rows", lambda: [])
    assert _drive_loop(w, until=lambda: bool(calls))


def test_aspect_worker_running_reads_the_lease_in_the_given_dir(tmp_path: Path) -> None:
    assert awd.aspect_worker_running(tmp_path / "none") is False
    assert not (tmp_path / "none").exists(), "asking must not create the directory"
    cfg = tmp_path / "cfg"
    registry = _lease_naming(cfg, os.getpid())
    assert awd.aspect_worker_running(cfg) is True
    registry.relinquish(registry.records()[0])
    assert awd.aspect_worker_running(cfg) is False
