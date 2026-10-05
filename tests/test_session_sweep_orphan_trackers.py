# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.session.sweep_orphan_resource_trackers (issue nexus-9h1s).

Each ungraceful MCP shutdown (SIGKILL/OOM) leaves chroma's
multiprocessing workers' resource_tracker subprocesses re-parented to
init (PPID=1). They continue holding POSIX named semaphores until
killed; the semaphore namespace is bounded
(``kern.posix.sem.max=10000`` on macOS) so chronic accumulation
produces ``Errno 28`` system-wide.

``safe_killpg`` from ``stop_t1_server`` only signals the CURRENT
chroma's process group; orphan workers from PRIOR sessions live in
different (now-empty) process groups and cannot be reached. The
existing ``sweep_orphan_tmpdirs`` only reaps directories, not the
processes that hold the kernel-level resources.

Live shakeout (2026-05-08 03:30 PT) found 3,314 such orphans holding
8,359 of the 10,000 macOS POSIX-semaphore namespace. After manual
``ps | awk | xargs kill -TERM``: 0 orphan trackers, 74 semaphores.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Pure-unit tests (no subprocess); validate the parser + filter logic.
# ─────────────────────────────────────────────────────────────────────────────


class TestParseOrphanTrackerCandidates:
    """Validate the parser that walks ``ps -eo pid,ppid,etime,command``
    output and produces a candidate list for SIGTERM."""

    def test_parses_valid_orphan_lines(self):
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 python -c from multiprocessing.resource_tracker import main;main(8)\n"
            "  212     1       12:34 python -c from multiprocessing.spawn import spawn_main; spawn_main(tracker_fd=9) --multiprocessing-fork\n"
            "  500     1        00:05 python -c from multiprocessing.resource_tracker import main;main(8)\n"
        )
        # min_age_seconds=60 -> excludes the 5-second-old PID 500.
        candidates = _parse_orphan_tracker_candidates(
            ps_output, min_age_seconds=60.0
        )
        assert candidates == [211, 212]

    def test_excludes_non_init_parents(self):
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  500   100       12:34 python -c from multiprocessing.resource_tracker import main;main(8)\n"
        )
        assert _parse_orphan_tracker_candidates(ps_output) == []

    def test_excludes_non_multiprocessing_processes(self):
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  500     1       12:34 /usr/bin/some-daemon --foo\n"
            "  501     1       12:34 python script.py\n"
        )
        assert _parse_orphan_tracker_candidates(ps_output) == []

    def test_parses_etime_dd_hh_mm_ss(self):
        """`ps` etime can be '11-19:02:41' for old processes (days-h:m:s).
        Must parse to age >> any plausible min_age_seconds."""
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            " 8651     1 11-19:02:41 python -c from multiprocessing.spawn import spawn_main\n"
        )
        # An 11-day-old process must always be selected, even at
        # arbitrarily large min_age thresholds.
        assert _parse_orphan_tracker_candidates(
            ps_output, min_age_seconds=86400.0
        ) == [8651]

    def test_parses_etime_h_mm_ss(self):
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  500     1     2:30:45 python -c from multiprocessing.resource_tracker import main\n"
        )
        # 2h30m45s > 60s.
        assert _parse_orphan_tracker_candidates(
            ps_output, min_age_seconds=60.0
        ) == [500]

    def test_excludes_pids_in_protected_set(self):
        from nexus.session import _parse_orphan_tracker_candidates

        ps_output = (
            "  PID  PPID     ELAPSED COMMAND\n"
            "  211     1       12:34 python -c from multiprocessing.resource_tracker import main\n"
            "  212     1       12:34 python -c from multiprocessing.resource_tracker import main\n"
        )
        protected = {211}
        assert _parse_orphan_tracker_candidates(
            ps_output, protected_pids=protected
        ) == [212]


# ─────────────────────────────────────────────────────────────────────────────
# Kill-helper tests: real subprocesses, no multiprocessing. The actual
# kernel-level resource_tracker leak is unreliable to reproduce in a unit
# test (the tracker's atexit cleanup fires when the parent exits via
# sys.exit, so a *graceful* fork-then-exit doesn't orphan the way a
# SIGKILL'd parent does). The kill helper is the testable surface; the
# parser handles the discrimination logic and is unit-tested above.
# ─────────────────────────────────────────────────────────────────────────────


def _spawn_long_sleeper() -> subprocess.Popen:
    """Spawn a subprocess that sleeps for 60 s. Used to verify
    ``_kill_orphan_tracker_pids`` reliably SIGTERMs targets."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestKillOrphanTrackerPids:
    """The kill helper takes a list of PIDs, SIGTERMs each, escalates
    to SIGKILL on survivors after grace_seconds. Tested with regular
    sleepers; multiprocessing-specific behaviour is not relevant
    to this code path."""

    def test_signals_each_pid_sigterm(self):
        from nexus.session import _kill_orphan_tracker_pids

        sleepers = [_spawn_long_sleeper() for _ in range(3)]
        pids = [s.pid for s in sleepers]
        try:
            killed = _kill_orphan_tracker_pids(pids, grace_seconds=2.0)
            assert killed == 3
            for s in sleepers:
                rc = s.wait(timeout=5.0)
                # SIGTERM exit code on POSIX: -SIGTERM (Popen.wait
                # returns negative for signal exits) or 143 in some
                # shells. Either way: not 0 (clean exit).
                assert rc != 0
        finally:
            for s in sleepers:
                if s.poll() is None:
                    s.kill()
                    s.wait(timeout=5.0)

    def test_handles_already_dead_pid_gracefully(self):
        from nexus.session import _kill_orphan_tracker_pids

        # Spawn-then-kill so the PID is dead by the time the helper
        # signals it. The helper must not raise.
        s = _spawn_long_sleeper()
        s.kill()
        s.wait(timeout=5.0)
        # ProcessLookupError is silently skipped; signalled count
        # should be 0.
        killed = _kill_orphan_tracker_pids([s.pid], grace_seconds=0.5)
        assert killed == 0

    def test_escalates_to_sigkill_on_survivor(self):
        """A subprocess that traps SIGTERM survives the graceful
        signal. The escalation must SIGKILL it within grace_seconds."""
        from nexus.session import _kill_orphan_tracker_pids

        # Subprocess that ignores SIGTERM but cannot block SIGKILL.
        # Setpgrp so the parent's signal-routing doesn't reach it
        # via process-group propagation; we rely solely on the
        # explicit os.kill in the helper.
        proc = subprocess.Popen(
            [
                sys.executable, "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            # SIGTERM-trap subprocess takes a moment to install the
            # handler. Wait briefly so our SIGTERM lands after the
            # trap is in place; otherwise the default handler kills
            # it before we can verify escalation.
            time.sleep(0.3)
            t0 = time.time()
            killed = _kill_orphan_tracker_pids(
                [proc.pid], grace_seconds=1.0
            )
            elapsed = time.time() - t0
            assert killed == 1
            # Wait for the (escalated) SIGKILL to take effect.
            rc = proc.wait(timeout=5.0)
            assert rc != 0, "SIGTERM-trap process exited cleanly"
            # nexus-scc9t: loose hang guard, not a precision timing
            # check. This is a genuine real-time claim (the helper's
            # grace_seconds=1.0 poll-then-escalate loop), not pure
            # logic, so wall time can't be fully eliminated. 12.0s is
            # 12x the 1.0s grace budget -- guards against escalation
            # never firing (an outright hang) while clear of ordinary
            # -n auto scheduler delay; it does not slow down a passing
            # run, only how fast a genuine regression is caught.
            assert elapsed < 12.0, (
                f"helper took {elapsed:.2f}s for grace_seconds=1.0"
            )
        finally:
            if proc.poll() is None:
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5.0)


class TestKillOrphanTrackerPidsGoneShapes:
    """nexus-f9bgu.44: on Windows ``os.kill`` of a pid that has already exited
    raises a plain ``OSError`` (``WinError 87``), not ``ProcessLookupError``, so
    a process that left between the listing and the signal turned the sweep
    into a traceback. The sweep itself is a no-op on Windows (it returns before
    any listing), so this is reachable only by a direct caller; the helper is
    still held to the shared contract: a gone pid is skipped, never raised."""

    def test_a_gone_pid_of_any_oserror_shape_is_skipped_at_the_sigterm_step(
        self, monkeypatch
    ):
        import nexus.session as session

        def _gone(pid, sig):
            raise OSError(22, "The parameter is incorrect")  # the WinError 87 shape

        monkeypatch.setattr(session.os, "kill", _gone)
        monkeypatch.setattr(session, "_is_pid_alive", lambda pid: False)
        assert session._kill_orphan_tracker_pids([4242], grace_seconds=0.0) == 0

    def test_the_escalation_goes_through_the_shared_hard_kill(self, monkeypatch):
        """A survivor of the grace is killed by ``hard_kill_pid`` (the one place
        that knows a gone pid raises ``OSError`` on Windows), not a raw
        ``os.kill`` with a narrower except."""
        import nexus.daemon.service_registry as registry
        import nexus.session as session

        sigterms: list[int] = []
        hard: list[int] = []
        monkeypatch.setattr(session.os, "kill", lambda pid, sig: sigterms.append(pid))
        monkeypatch.setattr(session, "_is_pid_alive", lambda pid: True)
        monkeypatch.setattr(
            registry, "hard_kill_pid", lambda pid: hard.append(pid) or True
        )
        assert session._kill_orphan_tracker_pids([4242], grace_seconds=0.0) == 1
        assert sigterms == [4242]
        assert hard == [4242], "the escalation must use service_registry.hard_kill_pid"

    def test_a_survivor_that_exits_before_the_hard_kill_does_not_raise(
        self, monkeypatch
    ):
        """The window the narrow except left open: alive at the check, gone at
        the kill. ``hard_kill_pid`` swallows that; the sweep must not raise."""
        import nexus.session as session

        calls: list[int] = []

        def _kill(pid, sig):
            calls.append(sig)
            if len(calls) > 1:  # the SIGKILL step: the pid is already gone
                raise OSError(22, "The parameter is incorrect")

        monkeypatch.setattr(session.os, "kill", _kill)
        monkeypatch.setattr(session, "_is_pid_alive", lambda pid: True)
        assert session._kill_orphan_tracker_pids([4242], grace_seconds=0.0) == 1
