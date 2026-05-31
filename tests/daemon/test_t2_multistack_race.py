# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-140 P2.1 (nexus-remh4): multi-stack race harness — the P2/P3 GATE artifact.

Spawns K real, concurrent ``nx daemon t2 ensure-running`` processes against ONE
``memory.db`` (isolated via ``NEXUS_CONFIG_DIR``) and asserts the convergence
invariant. Real subprocesses, not mocks: the load-bearing race is the OS-level
``fcntl`` spawn-lock contention between independently-started ``t2 start``
children, which only a real process race exercises.

Why this is a gate, not just a test
-----------------------------------
Before RDR-140, a K-way race converged to one daemon but via crash+reap churn —
the A4 research signature was ``started=1 / crashed=9 / stop_requested=1`` for a
single converged daemon: eight siblings crash-looped and were reaped before the
survivor stuck. P1 (loser quiet-attach) removes the crashes. P2 (Gap 3
single-flight election) removes the *redundant spawns* themselves so only one
stack ever runs ``t2 start``.

Two invariants, split by which phase delivers them:

* ``test_kway_race_converges_without_crash_or_reap`` — GREEN as of P1. Exactly
  one ``t2_daemon_started``, zero ``t2_daemon_crashed``, zero healthy-peer
  reaps, every racer exits 0, exactly one live daemon at the end. This is the
  regression guard P2 and P3 must keep green.
* ``test_kway_race_is_single_flight`` — RED until P2.2 (nexus-fkhe2). Asserts
  zero redundant spawns (``t2_daemon_spawn_lost`` count == 0): with a real
  election lock, only the winner spawns ``t2 start`` at all. ``xfail(strict)``
  so it fails loudly — telling us to drop the marker — once P2.2 lands.

Race outcomes are stochastic; a single green pass proves nothing. Both tests
loop over ``_ITERATIONS`` fresh cold-start config_dirs and assert on every run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# K racers per iteration; _ITERATIONS independent cold-start races. Both are
# deliberately modest — every iteration spawns K+1 real processes, so the wall
# cost is K*_ITERATIONS daemon spawns. Enough to surface a stochastic race
# without making the suite slow.
_K = 5
_ITERATIONS = 5

# Bounded waits — the harness must never hang, even when the invariant is
# violated; it fails with a diagnostic instead of timing out the suite.
_ENSURE_TIMEOUT = 60.0
_CONVERGE_TIMEOUT = 30.0


def _child_env(config_dir: Path) -> dict[str, str]:
    """Env for a racer subprocess: isolate to *config_dir* and force the inner
    ``t2 start`` spawn to use this interpreter's in-tree source.

    ``ensure-running`` resolves the daemon binary via ``shutil.which("nx")``,
    which would pick up the installed (possibly stale) shim. Stripping every
    PATH entry that contains an ``nx`` executable makes ``_resolve_nx_bin``
    fall back to ``[sys.executable, "-m", "nexus.cli"]`` — the editable src the
    test itself runs — so the race exercises the code under development.
    """
    env = os.environ.copy()
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    kept = [
        p for p in env.get("PATH", "").split(os.pathsep)
        if p and not (Path(p) / "nx").exists()
    ]
    env["PATH"] = os.pathsep.join(kept)
    return env


def _spawn_k_ensure_running(config_dir: Path, k: int) -> list[int]:
    """Launch *k* concurrent ``ensure-running`` racers; return their exit codes.

    All k are started before any is awaited so they genuinely contend.
    """
    env = _child_env(config_dir)
    argv = [
        sys.executable, "-m", "nexus.cli", "daemon", "t2", "ensure-running",
        "--config-dir", str(config_dir), "--quiet",
    ]
    procs = [
        subprocess.Popen(
            argv, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(k)
    ]
    codes: list[int] = []
    for p in procs:
        try:
            _out, err = p.communicate(timeout=_ENSURE_TIMEOUT)
        except subprocess.TimeoutExpired:
            p.kill()
            _out, err = p.communicate()
            raise AssertionError(
                f"ensure-running racer hung > {_ENSURE_TIMEOUT}s; stderr={err!r}"
            )
        codes.append(p.returncode)
    return codes


def _count_daemon_events(config_dir: Path) -> dict[str, int]:
    """Count structlog event occurrences in the daemon's rotating log file.

    The file formatter renders ``<asctime> <name> <level> <event> k=v ...`` via
    KeyValueRenderer, so the event name appears as a whitespace-delimited token.
    We count lines containing each event token.
    """
    log_path = config_dir / "logs" / "t2_daemon.log"
    counts = {
        "t2_daemon_started": 0,
        "t2_daemon_crashed": 0,
        "t2_daemon_spawn_lost": 0,
        "t2_predecessor_reaped": 0,
        "t2_predecessor_sigkilled": 0,
    }
    if not log_path.exists():
        return counts
    for line in log_path.read_text(errors="replace").splitlines():
        for event in counts:
            if event in line:
                counts[event] += 1
    return counts


def _live_daemon_count(config_dir: Path) -> int:
    """0 or 1: whether the recorded discovery pid is a live process."""
    from nexus.daemon.t2_daemon import t2_discovery_path

    disc = t2_discovery_path(config_dir)
    if not disc.exists():
        return 0
    try:
        pid = json.loads(disc.read_text()).get("pid")
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(pid, int):
        return 0
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return 0
    return 1


def _wait_for_convergence(config_dir: Path) -> None:
    """Bounded wait until exactly one live daemon is reachable."""
    deadline = time.monotonic() + _CONVERGE_TIMEOUT
    while time.monotonic() < deadline:
        if _live_daemon_count(config_dir) == 1:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"no single live daemon within {_CONVERGE_TIMEOUT}s "
        f"(events={_count_daemon_events(config_dir)})"
    )


def _fresh_config_dir() -> Path:
    # Short /tmp path: macOS caps AF_UNIX socket paths at 104 chars.
    return Path(tempfile.mkdtemp(prefix="nxt2race-", dir="/tmp"))


def _teardown(config_dir: Path) -> None:
    from tests._daemon_leak_guard import reap_tmp_daemons

    try:
        reap_tmp_daemons(config_dir, tiers=("t2",))
    except BaseException:  # noqa: BLE001 — teardown guard must never raise
        pass
    shutil.rmtree(config_dir, ignore_errors=True)


class TestMultiStackRace:
    def test_kway_race_converges_without_crash_or_reap(self) -> None:
        """GREEN as of P1: K racers converge to exactly one daemon with no
        crash and no healthy-peer reap, on every cold-start iteration."""
        for i in range(_ITERATIONS):
            cd = _fresh_config_dir()
            try:
                codes = _spawn_k_ensure_running(cd, _K)
                _wait_for_convergence(cd)
                events = _count_daemon_events(cd)

                assert codes == [0] * _K, (
                    f"iter {i}: ensure-running exit codes {codes} != all-0"
                )
                assert events["t2_daemon_started"] == 1, (
                    f"iter {i}: started={events['t2_daemon_started']} != 1"
                )
                assert events["t2_daemon_crashed"] == 0, (
                    f"iter {i}: crashed={events['t2_daemon_crashed']} != 0"
                )
                assert events["t2_predecessor_reaped"] == 0, (
                    f"iter {i}: healthy-peer reaped="
                    f"{events['t2_predecessor_reaped']} != 0"
                )
                assert events["t2_predecessor_sigkilled"] == 0, (
                    f"iter {i}: healthy-peer sigkilled="
                    f"{events['t2_predecessor_sigkilled']} != 0"
                )
                assert _live_daemon_count(cd) == 1, (
                    f"iter {i}: live daemon count != 1"
                )
            finally:
                _teardown(cd)

    def test_holder_dies_mid_spawn_waiters_recover(self) -> None:
        """A daemon that dies after acquiring the spawn lock but before binding
        must not wedge the system: a subsequent K-way ensure-running race
        recovers to exactly one live daemon, no crash, no deadlock.

        Pre-P2 the contended lock is the fcntl spawn lock (the OS releases it on
        the holder's death); P2's election lock generalises the same recovery
        contract. Drivable and GREEN now — a regression guard either way. We
        widen the kill window using the cold-start migration delay: a fresh DB
        forces a multi-second migration while the spawn lock is held and before
        discovery is written.
        """
        from nexus.daemon.t2_daemon import _SPAWN_LOCK_FILE, t2_discovery_path

        cd = _fresh_config_dir()
        try:
            env = _child_env(cd)
            start_argv = [
                sys.executable, "-m", "nexus.cli", "daemon", "t2", "start",
                "--config-dir", str(cd),
            ]
            holder = subprocess.Popen(
                start_argv, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            # Wait until it has taken the spawn lock but not yet written
            # discovery (mid-spawn), bounded; then kill it there.
            lock_file = cd / _SPAWN_LOCK_FILE
            disc = t2_discovery_path(cd)
            deadline = time.monotonic() + _CONVERGE_TIMEOUT
            while time.monotonic() < deadline:
                if lock_file.exists() and not disc.exists():
                    break
                if holder.poll() is not None:
                    break
                time.sleep(0.02)
            holder.kill()
            holder.communicate(timeout=_ENSURE_TIMEOUT)

            # Waiters recover: a fresh K-way race brings up exactly one daemon.
            codes = _spawn_k_ensure_running(cd, _K)
            _wait_for_convergence(cd)
            events = _count_daemon_events(cd)

            assert codes == [0] * _K, f"recovery racer exit codes {codes} != all-0"
            assert events["t2_daemon_crashed"] == 0, (
                f"crashed={events['t2_daemon_crashed']} != 0 after holder death"
            )
            assert _live_daemon_count(cd) == 1, "no single live daemon after recovery"
        finally:
            _teardown(cd)

    def test_kway_race_is_single_flight(self) -> None:
        """GREEN as of P2.2 (nexus-fkhe2): zero redundant spawns across all
        iterations.

        The convergence guard above tolerates redundant spawns that quiet-
        attach; this one demands the single-flight election lock eliminate them
        — only the election holder cold-spawns ``t2 start``, waiters block then
        re-discover the live winner and attach without spawning, so no sibling
        ever reaches the daemon spawn lock (``t2_daemon_spawn_lost`` == 0).
        Summed over iterations so a single race that happens to serialise can't
        mask a herd regression.
        """
        total_spawn_lost = 0
        for i in range(_ITERATIONS):
            cd = _fresh_config_dir()
            try:
                _spawn_k_ensure_running(cd, _K)
                _wait_for_convergence(cd)
                events = _count_daemon_events(cd)
                # Exactly one stack spawned (guards the silent-winner-death
                # regression where a second waiter wins and starts a second
                # daemon while spawn_lost stays 0).
                assert events["t2_daemon_started"] == 1, (
                    f"iter {i}: started={events['t2_daemon_started']} != 1"
                )
                total_spawn_lost += events["t2_daemon_spawn_lost"]
            finally:
                _teardown(cd)
        assert total_spawn_lost == 0
