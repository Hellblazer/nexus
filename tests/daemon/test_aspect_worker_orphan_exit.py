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
* the worker exits once its claim round trips have failed for the stated bound,
  and not before; with the bound off it stays up (the non-vacuity control);
* ``nx daemon aspect-worker stop --config-dir D`` stops the worker leased in D,
  by its lease record, and leaves a worker serving another directory alone.

Every process a test starts is stopped by pid in its own ``finally``.
"""
from __future__ import annotations

import itertools
import os
import re
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
from nexus.daemon import service_registry as sr
from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
    stop_tier_holders,
    ttl_for_tier,
)
from tests._module_seam import setattr_in

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

    setattr_in(monkeypatch, "nexus.daemon.service_registry", "os.stat", _denied)
    assert registry.dir_exists() is True, "an unreadable directory is not a removed one"


# ── unit: the daemon's two exits, on fakes ───────────────────────────────────


class _ClaimWorker:
    """A hosted worker that reports claim outcomes through the two hooks the
    daemon attaches (nexus-wl9pk). ``outcome`` runs once per tick on the worker
    thread and returns ``"failure"``, ``"contact"`` or ``None`` (no claim)."""

    def __init__(self, outcome: Callable[[], str | None]) -> None:
        self._outcome = outcome
        self.on_failure: Callable[[], None] | None = None
        self.on_contact: Callable[[], None] | None = None
        self.released = 0
        self._stopping = False
        self._thread: threading.Thread | None = None

    def set_engine_failure_handler(self, handler: Callable[[], None] | None) -> None:
        self.on_failure = handler

    def set_engine_contact_handler(self, handler: Callable[[], None] | None) -> None:
        self.on_contact = handler

    def start(self) -> None:
        def _claims() -> None:
            while not self._stopping:
                verdict = self._outcome()
                if verdict == "failure" and self.on_failure is not None:
                    self.on_failure()
                elif verdict == "contact" and self.on_contact is not None:
                    self.on_contact()
                time.sleep(0.02)

        self._thread = threading.Thread(target=_claims, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 0) -> None:
        self._stopping = True

    def release_held_claims(self) -> int:
        self.released += 1
        return 0


class _Mono:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _daemon(
    cfg: Path, worker: _ClaimWorker, mono: _Mono, bound: float | None = 100.0,
) -> awd.AspectWorkerDaemon:
    return awd.AspectWorkerDaemon(
        config_dir=cfg, tenant="default",
        worker_factory=lambda: worker,
        heartbeat_interval=0.02, engine_unreachable_exit_seconds=bound, monotonic=mono,
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
    d = _daemon(cfg, _ClaimWorker(lambda: None), _Mono())
    d.start()
    try:
        assert not d._stop.is_set()
        shutil.rmtree(cfg)
        assert _wait(d._stop.is_set), "the heartbeat loop must stop the daemon once the directory is gone"
    finally:
        d.stop()
    assert not cfg.exists(), "stop() must not mark or relinquish into a directory that is gone"


def test_failed_claims_stop_the_daemon_only_after_the_bound(tmp_path: Path) -> None:
    mono = _Mono()
    calls = {"n": 0}

    def _down() -> str:
        calls["n"] += 1
        return "failure"

    d = _daemon(tmp_path / "cfg", _ClaimWorker(_down), mono, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: calls["n"] >= 1), "the worker's first claim failed"
        time.sleep(0.2)  # several heartbeat ticks, clock not advanced
        assert not d._stop.is_set(), "a failing claim alone is not an exit"
        mono.now += 99.0
        time.sleep(0.2)
        assert not d._stop.is_set(), "99 s of a 100 s bound"
        mono.now += 2.0
        assert _wait(d._stop.is_set), "past the bound the daemon stops itself"
        assert d._left_for_unreachable_engine
    finally:
        d.stop()


def test_one_answered_claim_restarts_the_unreachable_clock(tmp_path: Path) -> None:
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: "contact"), mono, bound=100.0)
    d._unreachable_since = mono.now - 90.0  # failing for 90 s
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is None), "an answered claim clears the clock"
        mono.now += 500.0
        time.sleep(0.2)
        assert not d._stop.is_set()
    finally:
        d.stop()


def test_a_zero_bound_turns_the_exit_off(tmp_path: Path) -> None:
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: "failure"), mono, bound=0)
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
            claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            claude.chmod(0o755)
        else:
            (bindir / "claude.cmd").write_text("@exit /b 0\r\n", encoding="utf-8")
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

    def full_output(self) -> str:
        """The whole log. A marker is searched for here, never in output(): Windows run 42162135552 failed with the
        worker exiting 0 inside the bound and the marker absent from a 2000-character tail that began inside a
        traceback with long Windows paths, so the event was probably pushed out of the tail (nexus-reaia). The CI
        log cannot rule out the event never being logged; missing_marker() says which on a recurrence."""
        self._out.flush()
        return self.log.read_text(encoding="utf-8", errors="replace")

    def output(self) -> str:
        """The log's last 2000 characters, for a failure message."""
        return self.full_output()[-2000:]

    def missing_marker(self, marker: str) -> str:
        """A failure message for a marker absent from the whole log: every event the log holds, in order, then its
        tail, so a recurrence shows what the worker did log instead."""
        events = re.findall(r"event='([^']+)'", self.full_output())
        return f"{marker!r} is not in the log; its events: {events}\n{self.output()}"

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
    assert "config_dir_gone" in w.full_output(), w.missing_marker("config_dir_gone")


def test_a_real_worker_exits_once_its_engine_has_been_unreachable_for_the_bound(workers: Callable[..., _RealWorker]) -> None:
    bound = 4.0
    w = workers("unreachable", "--engine-unreachable-exit-seconds", str(bound))
    t_ready = w.wait_for_lease()
    rc = w.wait_exit(bound + _ORPHAN_EXIT_BOUND_S)
    assert rc is not None, f"still running {bound + _ORPHAN_EXIT_BOUND_S}s after start against a closed port:\n{w.output()}"
    assert rc == 0, w.output()
    elapsed = time.monotonic() - t_ready
    assert elapsed >= bound - 1.0, f"exited after {elapsed:.1f}s, before the {bound}s bound:\n{w.output()}"
    assert "engine_unreachable_exit" in w.full_output(), w.missing_marker("engine_unreachable_exit")


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
        env=mine.env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(mine.proc.pid) in done.stdout, done.stdout
    assert mine.wait_exit(10.0) is not None, "the worker named by the lease was not stopped"
    assert other.alive(), "stopping one directory's worker must not touch another's"
    assert not mine.lease.exists(), "the stop must drop the lease it stopped"
    again = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-m", "nexus.cli", "daemon", "aspect-worker", "stop", "--config-dir", str(mine.cfg)],
        env=mine.env, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
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
        # A worker serving "/a/b c" is not a worker serving "/a/b" (a joined
        # command line cannot tell the two apart by prefix).
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b c --tenant default", "/a/b", False),
        ("python -m nexus.cli daemon aspect-worker start --config-dir=/a/b c --tenant default", "/a/b", False),
        ("python -m nexus.cli daemon aspect-worker start --config-dir /a/b c", "/a/b", False),
        ('python -m nexus.cli daemon aspect-worker start --config-dir "/a/b c" --tenant default', "/a/b", False),
    ],
)
def test_the_directory_bound_predicate_reads_the_config_dir_off_the_command_line(
    command: str, serves: str, expect: bool,
) -> None:
    predicate = awd.is_aspect_worker_command_serving(Path(serves))
    assert predicate(command) is expect, command


@pytest.mark.parametrize(
    ("argv", "serves", "expect"),
    [
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir", "/a/b", "--tenant", "default"], "/a/b", True),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir=/a/b", "--tenant", "default"], "/a/b", True),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir", "/a/b c"], "/a/b c", True),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir", "/a/b c"], "/a/b", False),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir=/a/b c", "--tenant", "x"], "/a/b", False),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir", "/a/b", "c"], "/a/b c", False),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir", "/a/b2"], "/a/b", False),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--config-dir"], "/a/b", False),
        (["python", "-m", "nexus.cli", "daemon", "aspect-worker", "start", "--tenant", "default"], "/a/b", False),
        (["sleep", "60", "--config-dir", "/a/b"], "/a/b", False),
    ],
)
def test_the_argv_predicate_compares_the_exact_config_dir_value(
    argv: list[str], serves: str, expect: bool,
) -> None:
    assert awd.is_aspect_worker_argv_serving(Path(serves))(argv) is expect, argv


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
def test_a_lease_in_dir_a_naming_a_worker_of_dir_a_b_leaves_that_worker_alone(
    tmp_path: Path, workers: Callable[..., _RealWorker],
) -> None:
    """The worker serves ``<tmp>/a b/cfg``; the stale lease is in ``<tmp>/a``.
    A joined command line spells the first as ``--config-dir <tmp>/a b/cfg``,
    whose first token is the second directory."""
    victim = workers("a b", "--engine-unreachable-exit-seconds", "0")
    victim.wait_for_lease()
    stale_dir = tmp_path / "a"
    _lease_naming(stale_dir, victim.proc.pid)
    result = awd.stop_aspect_workers(stale_dir)
    assert victim.alive(), "the stop for <tmp>/a signalled the worker serving <tmp>/a b/cfg"
    assert result.stopped == ()
    assert result.foreign == (victim.proc.pid,)


def test_process_argv_splits_the_proc_cmdline_on_nul(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``/proc/<pid>/cmdline`` keeps an argument with a space whole; the joined
    text of ``process_command`` cannot. The procfs branch is pinned with an explicit
    ``platform="linux"`` so the test reads the fake procfs on every host (Windows takes
    the winproc branch otherwise)."""
    (tmp_path / "uptime").write_text("1 1\n", encoding="utf-8")
    (tmp_path / "123").mkdir()
    (tmp_path / "123" / "cmdline").write_bytes(b"python\x00--config-dir\x00/a/b c\x00--tenant\x00default\x00")
    monkeypatch.setattr(sr, "PROCFS_ROOT", tmp_path)
    assert sr.process_argv(123, platform="linux") == ["python", "--config-dir", "/a/b c", "--tenant", "default"]
    assert sr.process_argv(124, platform="linux") == [], "a gone pid has no argv"
    assert sr.process_command(123, platform="linux") == "python --config-dir /a/b c --tenant default"


def test_stop_tier_holders_trusts_the_argv_over_the_joined_text(tmp_path: Path) -> None:
    """When the argv is readable it decides; when it is not, the joined text
    does. The bystander is a live process either way."""
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # noqa: S603
    try:
        cfg = tmp_path / "cfg"
        registry = _lease_naming(cfg, bystander.pid)
        joined = "python -m nexus.cli daemon aspect-worker start --config-dir /a/b c"
        result = stop_tier_holders(
            dir=cfg, tier=awd.TIER, grace_s=1.0, keep_foreign_leases=True,
            command=lambda pid: joined, is_ours=lambda c: True,
            argv_of=lambda pid: joined.split(" ")[:-1] + ["/a/b c"],
            is_ours_argv=lambda argv: False,
        )
        assert result.foreign == (bystander.pid,), "the argv said not ours; the joined text must not override it"
        assert bystander.poll() is None
        assert len(registry.records()) == 1
        result = stop_tier_holders(
            dir=cfg, tier=awd.TIER, grace_s=1.0, keep_foreign_leases=True,
            command=lambda pid: joined, is_ours=lambda c: False,
            argv_of=lambda pid: [], is_ours_argv=lambda argv: True,
        )
        assert result.foreign == (bystander.pid,), "no argv: the joined-text test decides"
    finally:
        bystander.kill()
        bystander.wait(timeout=10)


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


# ── nexus-wl9pk: the claim outcomes drive the unreachable clock ──────────────


def test_a_worker_whose_claims_keep_answering_never_exits(tmp_path: Path) -> None:
    """Failed claim round trips, each followed by an answered one, never let the
    unreachable clock run for the bound: the engine is reachable, so the
    one-hour exit must not fire."""
    mono = _Mono()
    seen = {"failure": 0, "contact": 0}
    script = itertools.cycle(["failure", "contact"])

    def _outcome() -> str:
        verdict = next(script)
        seen[verdict] += 1
        return verdict

    d = _daemon(tmp_path / "cfg", _ClaimWorker(_outcome), mono, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: seen["failure"] >= 1), "non-vacuity: failed claims reached the daemon"
        # Time passes in steps shorter than the bound, with an answered claim in
        # between, as it does for a live worker. Twenty steps of 60 s carry the
        # clock to 12x the bound.
        for _ in range(20):
            before = seen["contact"]
            assert _wait(lambda: seen["contact"] > before), "an answered claim between clock steps"
            mono.now += 60.0
            assert not d._stop.is_set(), "a worker with a live claim path was stopped by the failure clock"
        assert mono.now >= 2200.0 and seen["failure"] >= 1
    finally:
        d.stop()


def test_the_same_failing_claims_without_contact_do_exit(tmp_path: Path) -> None:
    """The control: with no answered claim the clock runs, so the test above is
    held up by the contact and not by something that disables the exit."""
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: "failure"), mono, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is not None)
        mono.now += 500.0
        assert _wait(d._stop.is_set), "the unreachable exit must still fire when nothing reaches the engine"
    finally:
        d.stop()


def test_the_unreachable_exit_skips_the_tuple_claim_release(tmp_path: Path) -> None:
    """Releasing held claims asks the dead engine and waits out its client
    timeouts; the leases lapse on their own."""
    mono = _Mono()
    worker = _ClaimWorker(lambda: "failure")
    d = _daemon(tmp_path / "cfg", worker, mono, bound=100.0)
    d.start()
    try:
        assert _wait(lambda: d._unreachable_since is not None)
        mono.now += 500.0
        assert _wait(d._stop.is_set)
    finally:
        d.stop()
    assert worker.released == 0, "the unreachable-exit path must not wait on the dead engine"


def test_an_ordinary_stop_still_releases_the_tuple_claims(tmp_path: Path) -> None:
    worker = _ClaimWorker(lambda: None)
    d = _daemon(tmp_path / "cfg", worker, _Mono())
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


# ── the worker's claim loop reports each round trip to its host ─────────────


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
    assert _drive_loop(w, until=lambda: bool(calls)), "an answered (empty) claim is engine contact"


def test_the_real_worker_reports_failure_and_no_contact_when_a_claim_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    contacts: list[int] = []
    failures: list[int] = []
    attempts: list[int] = []
    w.set_engine_contact_handler(lambda: contacts.append(1))
    w.set_engine_failure_handler(lambda: failures.append(1))

    def _fail(park: bool) -> tuple[list, bool]:
        attempts.append(1)
        return [], True

    monkeypatch.setattr(w._tuple_side, "supported", lambda: True)
    monkeypatch.setattr(w._tuple_side, "claim_batch", _fail)
    assert _drive_loop(w, until=lambda: bool(failures)), "a failed claim reports failure to its host"
    assert attempts, "the loop must have tried (non-vacuity)"
    assert not contacts, "a failed claim is not engine contact"


def test_an_answered_tuple_claim_clears_the_clock_a_failed_one_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker whose claims answer (empty) clears the host's unreachable clock
    that an earlier failure started."""
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: None), mono, bound=100.0)
    d._unreachable_since = mono.now - 50.0
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    w.set_engine_contact_handler(d.note_engine_contact)
    attempts: list[int] = []

    def _answered(park: bool) -> tuple[list, bool]:
        attempts.append(1)
        return [], False

    monkeypatch.setattr(w._tuple_side, "supported", lambda: True)
    monkeypatch.setattr(w._tuple_side, "claim_batch", _answered)
    cleared = _drive_loop(w, until=lambda: d._unreachable_since is None)
    assert attempts, "the claim must have run (non-vacuity)"
    assert cleared, "an answered claim is engine contact and clears the clock"


def test_an_unsupported_tuple_side_still_starts_the_unreachable_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the engine lacks the ``aspects`` template the worker makes no claim round trip for up
    to an hour; the host's clock must still run, or an engine that goes away during that hour is
    not noticed until it is out (nexus-hbnml review r1)."""
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: None), mono, bound=100.0)
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    w.set_engine_contact_handler(d.note_engine_contact)
    w.set_engine_failure_handler(d.note_engine_failure)
    probes: list[int] = []

    def _claim_must_not_run(park: bool) -> tuple[list, bool]:
        raise AssertionError("an unsupported side makes no claim")

    def _probe() -> bool:
        probes.append(1)
        return False

    monkeypatch.setattr(w._tuple_side, "supported", lambda: False)
    monkeypatch.setattr(w._tuple_side, "claim_batch", _claim_must_not_run)
    monkeypatch.setattr(w._tuple_side, "probe_engine", _probe)
    assert d._unreachable_since is None
    assert _drive_loop(w, until=lambda: d._unreachable_since is not None), \
        "the failed probe starts the host's unreachable clock"
    assert probes, "the loop must have probed (non-vacuity)"


def test_an_unsupported_tuple_side_clears_the_clock_when_the_engine_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mono = _Mono()
    d = _daemon(tmp_path / "cfg", _ClaimWorker(lambda: None), mono, bound=100.0)
    d._unreachable_since = mono.now - 50.0
    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    w.set_engine_contact_handler(d.note_engine_contact)
    w.set_engine_failure_handler(d.note_engine_failure)
    monkeypatch.setattr(w._tuple_side, "supported", lambda: False)
    monkeypatch.setattr(w._tuple_side, "probe_engine", lambda: True)
    assert _drive_loop(w, until=lambda: d._unreachable_since is None), "an answered probe is engine contact"


def test_probe_engine_reads_a_missing_template_as_an_answer_and_a_transport_error_as_none(
    tmp_path: Path,
) -> None:
    import httpx

    from nexus.db.t2.http_tuple_store import UnknownSubspaceError

    w = AspectExtractionWorker(poll_interval=0.05, config_dir=tmp_path)
    side = w._tuple_side

    class _Store:
        def __init__(self, exc: Exception | None) -> None:
            self.exc = exc

        def subspace_stats(self, subspace: str, **kw: object) -> object:
            if self.exc is not None:
                raise self.exc
            return object()

    for exc, expected in ((None, True), (UnknownSubspaceError("aspects"), True), (httpx.ConnectError("down"), False)):
        store = _Store(exc)
        side._call = lambda fn, _s=store: fn(_s)  # type: ignore[method-assign]
        assert side.probe_engine() is expected, repr(exc)


def test_aspect_worker_running_reads_the_lease_in_the_given_dir(tmp_path: Path) -> None:
    assert awd.aspect_worker_running(tmp_path / "none") is False
    assert not (tmp_path / "none").exists(), "asking must not create the directory"
    cfg = tmp_path / "cfg"
    registry = _lease_naming(cfg, os.getpid())
    assert awd.aspect_worker_running(cfg) is True
    registry.relinquish(registry.records()[0])
    assert awd.aspect_worker_running(cfg) is False
