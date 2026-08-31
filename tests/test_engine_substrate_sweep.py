# SPDX-License-Identifier: AGPL-3.0-or-later
"""Crash-durable teardown for the T2 engine test substrate (nexus-lgdy1 fix b).

``tests/_engine_substrate.py``'s ``atexit``-only teardown leaks a
postmaster+engine PAIR on a hard-killed pytest; both reparent to PID 1 and
squat an ephemeral port apiece, which Docker Desktop can silently redirect a
testcontainers client onto (T2 nexus/cascade-test-container-failure-
diagnosis-2026-07-31, bead nexus-lgdy1). This exercises the sidecar +
session-start sweep that makes teardown crash-durable, entirely against
FAKE process trees (subprocess ``sleep``/``python -c`` children this file
owns) — never against the box's real stray population, and never by
booting the real engine substrate.

Deliberately run with ``NX_TEST_T2_SUBSTRATE=none`` (see
``tests/conftest.py``'s ``_pin_t2_substrate``) so this file's own tests
don't pay for booting a real PG + JVM they don't need:

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_sweep.py -q

Every cluster dir used here lives under a per-test ``tmp_path``, passed
explicitly as ``tmp_root=`` — never the real ``tempfile.gettempdir()`` — so
these tests cannot interact with the box's real (or another concurrent
session's live) substrate clusters either way.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.daemon.service_registry import pid_alive, process_command
from tests._engine_substrate import (
    _LOW_PORT_RANGE,
    _SIDECAR_FILENAME,
    _WORKER_SHARD_MAX_INDEX,
    _WORKER_SHARD_WIDTH,
    SweepResult,
    _free_port,
    _identify_leg,
    _kill_engine_leg,
    _kill_postmaster_leg,
    _owner_is_live,
    _pg_bin,
    _read_postmaster_pid,
    _worker_shard_range,
    sweep_stale_substrate_clusters,
)


def _dead_pid() -> int:
    """A PID guaranteed to be dead RIGHT NOW: spawn a trivial child, wait
    for it to exit, and hand back its (now-recycled-eligible) pid. The
    window for a genuine PID-reuse collision between this and a caller's
    subsequent liveness check is milliseconds; accepted as the standard
    cost of testing PID-liveness code without touching real processes."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _spawn_sleeper() -> subprocess.Popen:
    """A real, live child process this test fully owns and cleans up."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )


def _cmdline_of(proc: subprocess.Popen) -> str:
    return process_command(proc.pid)


def _write_cluster_sidecar(cluster_dir: Path, **fields) -> None:
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / _SIDECAR_FILENAME).write_text(json.dumps(fields))


@pytest.fixture
def owned_children():
    """Live children spawned by a test, force-killed at teardown even if
    the test's own assertions fail partway through."""
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


class TestStaleSidecarClusterReaped:
    def test_reaped_pair_ordered_engine_then_postmaster(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        from tests import _engine_substrate as sub

        # Ordering is proven by recording call order on the two kill
        # entry points directly; the MECHANISM each uses (process-group
        # signal vs pg_ctl) is covered by its own dedicated test class
        # below, so here they're stubbed to a fast, unconditional kill
        # (real pg_ctl against a non-PG fake dir would just no-op through
        # its 5s fallback grace, slowing this test for nothing it tests).
        calls: list[str] = []

        def _record_and_kill(label):
            def _fn(pid, *_args, **_kwargs):
                calls.append(label)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return _fn

        monkeypatch.setattr(sub, "_kill_engine_leg", _record_and_kill("engine"))
        monkeypatch.setattr(sub, "_kill_postmaster_leg", _record_and_kill("postmaster"))

        engine_proc = _spawn_sleeper()
        pg_proc = _spawn_sleeper()
        owned_children.extend([engine_proc, pg_proc])
        # Give the OS a beat to settle the new processes' /proc entries
        # before reading their cmdlines back.
        time.sleep(0.1)

        cluster_dir = tmp_path / "nexus_t2_substrate_pg_stale001"
        _write_cluster_sidecar(
            cluster_dir,
            owner_pytest_pid=_dead_pid(),
            postmaster_pid=pg_proc.pid,
            postmaster_cmdline=_cmdline_of(pg_proc),
            engine_pid=engine_proc.pid,
            engine_cmdline=_cmdline_of(engine_proc),
        )

        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert calls == ["engine", "postmaster"], (
            "reap order must be engine THEN postmaster (pool connections "
            f"close before the server dies) -- got {calls}"
        )
        assert str(cluster_dir) in result.reaped
        assert not cluster_dir.exists()
        engine_proc.wait(timeout=5)
        pg_proc.wait(timeout=5)
        assert engine_proc.poll() is not None
        assert pg_proc.poll() is not None


class TestPartialLegMismatchBlocksEntireReap:
    """Round-2 code review, critical-adjacent finding: reaping legs
    independently let a MATCHED postmaster get killed while its paired
    MISMATCHED engine was reported as though the cluster had been left
    fully untouched (``mismatch_refused``) — silently breaking both the
    engine-then-postmaster ordering invariant and the "leave the cluster
    for manual review" contract. This is the reviewer's prescribed
    regression: nothing may be signalled unless EVERY present leg
    identifies clean."""

    def test_engine_mismatch_blocks_postmaster_kill_too(
        self, tmp_path, owned_children,
    ) -> None:
        postmaster_proc = _spawn_sleeper()
        engine_proc = _spawn_sleeper()
        owned_children.extend([postmaster_proc, engine_proc])
        time.sleep(0.1)

        cluster_dir = tmp_path / "nexus_t2_substrate_pg_partial001"
        _write_cluster_sidecar(
            cluster_dir,
            owner_pytest_pid=_dead_pid(),
            # Engine: live, but its recorded cmdline is deliberately wrong
            # -- a forced mismatch (a PID-reuse scenario).
            engine_pid=engine_proc.pid,
            engine_cmdline="totally-unrelated-process --not-the-engine",
            # Postmaster: live AND correctly matched -- on its own this
            # leg alone would be reaped.
            postmaster_pid=postmaster_proc.pid,
            postmaster_cmdline=_cmdline_of(postmaster_proc),
        )

        with pytest.warns(UserWarning, match="PID-reuse guard"):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) in result.mismatch_refused
        assert str(cluster_dir) not in result.reaped
        assert cluster_dir.exists(), (
            "a mismatch on ANY leg must leave the cluster in place"
        )
        assert engine_proc.poll() is None, "the mismatched engine must not be killed"
        assert postmaster_proc.poll() is None, (
            "REGRESSION GUARD: a correctly-matched postmaster must NOT be "
            "killed when its paired engine leg mismatches -- reaping legs "
            "independently silently broke the ordering invariant and the "
            "'leave cluster untouched on mismatch' contract (round-2 "
            "review, critical-adjacent finding)"
        )


class TestLiveOwnerClusterUntouched:
    def test_live_owner_pid_is_never_touched(self, tmp_path) -> None:
        cluster_dir = tmp_path / "nexus_t2_substrate_pg_live001"
        _write_cluster_sidecar(
            cluster_dir,
            owner_pytest_pid=os.getpid(),  # this very test process: alive
            postmaster_pid=999_999_999,   # never consulted -- must short-circuit
            postmaster_cmdline="irrelevant",
            engine_pid=999_999_998,
            engine_cmdline="irrelevant",
        )

        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) in result.live_untouched
        assert result.reaped == []
        assert cluster_dir.exists()
        assert (cluster_dir / _SIDECAR_FILENAME).exists()


class TestPidReuseMismatchRefuses:
    def test_cmdline_mismatch_blocks_reap_and_leaves_cluster(
        self, tmp_path, owned_children,
    ) -> None:
        live_proc = _spawn_sleeper()
        owned_children.append(live_proc)
        time.sleep(0.1)

        cluster_dir = tmp_path / "nexus_t2_substrate_pg_reuse001"
        _write_cluster_sidecar(
            cluster_dir,
            owner_pytest_pid=_dead_pid(),
            engine_pid=_dead_pid(),  # already gone -- "dead", not a mismatch
            engine_cmdline="whatever the sidecar recorded",
            postmaster_pid=live_proc.pid,
            # Deliberately wrong: the live pid was reused by an unrelated
            # process since the sidecar was written.
            postmaster_cmdline="totally-unrelated-process --not-postgres",
        )

        with pytest.warns(UserWarning, match="PID-reuse guard"):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) in result.mismatch_refused
        assert str(cluster_dir) not in result.reaped
        assert cluster_dir.exists(), "mismatch must leave the cluster in place"
        assert live_proc.poll() is None, (
            "the live process must NOT be signalled on a cmdline mismatch"
        )


class TestSidecarLessLegacyClusterReported:
    def test_legacy_cluster_is_reported_never_killed(
        self, tmp_path, owned_children,
    ) -> None:
        live_proc = _spawn_sleeper()
        owned_children.append(live_proc)
        time.sleep(0.1)

        cluster_dir = tmp_path / "nexus_t2_substrate_pg_legacy001"
        cluster_dir.mkdir()
        (cluster_dir / "postmaster.pid").write_text(f"{live_proc.pid}\n")
        # No sidecar file -- this is the pre-nexus-lgdy1 shape.

        with pytest.warns(UserWarning, match="sidecar-less legacy cluster"):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) in result.legacy_reported
        assert result.reaped == []
        assert cluster_dir.exists()
        assert live_proc.poll() is None, (
            "a sidecar-less legacy cluster must never be auto-killed"
        )

    def test_legacy_cluster_missing_postmaster_pid_file_still_reported(
        self, tmp_path,
    ) -> None:
        """No sidecar AND no postmaster.pid (pure debris) -- still a loud
        report, never a crash."""
        cluster_dir = tmp_path / "nexus_t2_substrate_pg_legacy002"
        cluster_dir.mkdir()

        with pytest.warns(UserWarning, match="sidecar-less legacy cluster"):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) in result.legacy_reported


class TestSweepFailureIsLoudNotFatal:
    def test_malformed_sidecar_warns_and_does_not_abort_other_clusters(
        self, tmp_path,
    ) -> None:
        broken_dir = tmp_path / "nexus_t2_substrate_pg_broken001"
        broken_dir.mkdir()
        (broken_dir / _SIDECAR_FILENAME).write_text("{not valid json")

        live_dir = tmp_path / "nexus_t2_substrate_pg_ok002"
        _write_cluster_sidecar(
            live_dir,
            owner_pytest_pid=os.getpid(),
            postmaster_pid=0,
            postmaster_cmdline="",
            engine_pid=0,
            engine_cmdline="",
        )

        with pytest.warns(UserWarning, match="unreadable sidecar"):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(broken_dir) in result.errors
        # The sweep kept going and correctly classified the OTHER cluster --
        # proof the malformed one did not abort the pass.
        assert str(live_dir) in result.live_untouched
        assert broken_dir.exists(), "a parse failure must leave the dir alone"

    def test_empty_tmp_root_is_a_noop(self, tmp_path) -> None:
        empty = tmp_path / "nothing-here"
        result = sweep_stale_substrate_clusters(tmp_root=empty)
        assert result == SweepResult()


class TestReadPostmasterPid:
    def test_reads_first_line_as_int(self, tmp_path) -> None:
        (tmp_path / "postmaster.pid").write_text("12345\nother stuff\n")
        assert _read_postmaster_pid(str(tmp_path)) == 12345

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert _read_postmaster_pid(str(tmp_path)) is None

    def test_malformed_file_returns_none(self, tmp_path) -> None:
        (tmp_path / "postmaster.pid").write_text("not-a-pid\n")
        assert _read_postmaster_pid(str(tmp_path)) is None


class TestFreePortRange:
    def test_prefers_the_low_sub_range(self) -> None:
        port = _free_port()
        assert 20000 <= port < 29000, (
            f"expected a port from the low sub-range (below both Linux's "
            f"32768 default ip_local_port_range floor and macOS/Docker's "
            f"49152 IANA ephemeral floor), got {port}"
        )

    def test_falls_back_loudly_when_range_exhausted(self) -> None:
        # A genuinely free port, then hold it open as the SOLE candidate in
        # a synthetic 1-port "range" so every probe fails and the fallback
        # path fires deterministically.
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            occupied_port = holder.getsockname()[1]
            with pytest.warns(UserWarning, match="exhausted"):
                port = _free_port(
                    prefer_range=range(occupied_port, occupied_port + 1),
                    attempts=5,
                )
        assert port != occupied_port
        assert port > 0

    def test_binds_a_genuinely_usable_port(self) -> None:
        port = _free_port()
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))  # must not raise: truly free


class TestIdentifyLeg:
    """_identify_leg is side-effect-free (round-2 review, critical-adjacent
    fix): it must NEVER signal a process, only classify it, so a caller
    can check every leg before acting on any of them."""

    def test_matching_cmdline_is_ok(self, owned_children) -> None:
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        assert _identify_leg(proc.pid, _cmdline_of(proc)) == "ok"
        assert proc.poll() is None, "_identify_leg must never signal the process"

    def test_mismatched_cmdline_is_mismatch(self, owned_children) -> None:
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        assert _identify_leg(proc.pid, "definitely not this process") == "mismatch"
        assert proc.poll() is None

    def test_already_dead_pid_is_dead(self) -> None:
        assert _identify_leg(_dead_pid(), "anything") == "dead"


class TestKillEngineLegUsesProcessGroup:
    """Round-2 review, Important: the engine leg must signal its PROCESS
    GROUP (matching _boot()'s preexec_fn=os.setsid + _teardown()'s
    os.killpg for the identical process shape), not just the bare PID."""

    def test_signals_the_process_group_not_just_the_pid(
        self, owned_children, monkeypatch,
    ) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            preexec_fn=os.setsid,
        )
        owned_children.append(proc)
        time.sleep(0.1)
        expected_pgid = os.getpgid(proc.pid)

        killpg_calls: list[tuple[int, int]] = []
        real_killpg = os.killpg

        def _tracking_killpg(pgid, sig):
            killpg_calls.append((pgid, sig))
            return real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", _tracking_killpg)

        _kill_engine_leg(proc.pid, grace_s=1.0)

        assert killpg_calls, "must signal via os.killpg, not a bare os.kill"
        assert killpg_calls[0] == (expected_pgid, signal.SIGTERM)
        proc.wait(timeout=5)
        assert proc.poll() is not None

    def test_falls_back_to_bare_signal_if_process_already_gone(self) -> None:
        # getpgid raises ProcessLookupError for a dead pid -- must degrade
        # to a no-op, not raise.
        _kill_engine_leg(_dead_pid(), grace_s=0.2)  # must not raise


class TestKillPostmasterLegUsesPgCtl:
    """Round-2 review, Important: the postmaster leg must stop via
    ``pg_ctl -D <dir> stop -m immediate`` (this file's own established
    convention, and Hal's fast-shutdown manual-reap precedent) rather than
    a raw signal straight to the postmaster PID."""

    def test_stops_a_real_postmaster_via_pg_ctl_immediate(self) -> None:
        # nexus-v460j: _pg_bin() resolves lazily at this first-use point
        # (test RUN time, never collection time).
        if not _pg_bin().exists():
            pytest.skip("no PG bundle discoverable for this throwaway cluster")

        import tempfile

        # A raw tempfile.mkdtemp() -- NOT pytest's tmp_path fixture, whose
        # deep pytest-of-<user>/pytest-<N>/<test-name>/ nesting overruns
        # the 103-byte Unix-domain-socket path limit on macOS (measured:
        # pg_ctl start failed loud with exactly that FATAL). Matches how
        # _boot() itself allocates pgdata, for the same reason.
        pgdata = Path(tempfile.mkdtemp(prefix="nexus_lgdy1_throwaway_pg_"))
        try:
            subprocess.run(
                [str(_pg_bin() / "initdb"), "-D", str(pgdata), "--no-locale",
                 "-E", "UTF8", "--auth=trust"],
                check=True, capture_output=True,
            )
            with open(pgdata / "postgresql.conf", "a") as f:
                # Unix socket only -- no TCP port needed for this test.
                f.write("listen_addresses = ''\n")
            subprocess.run(
                [str(_pg_bin() / "pg_ctl"), "-D", str(pgdata), "-l",
                 str(pgdata / "pg.log"), "-o", f"-k {pgdata}", "start", "-w"],
                check=True, capture_output=True,
            )
            try:
                pm_pid = _read_postmaster_pid(str(pgdata))
                assert pm_pid is not None
                assert pid_alive(pm_pid)

                _kill_postmaster_leg(pm_pid, pgdata, grace_s=5.0)

                assert not pid_alive(pm_pid), (
                    "pg_ctl stop -m immediate must actually stop a real postmaster"
                )
            finally:
                subprocess.run(
                    [str(_pg_bin() / "pg_ctl"), "-D", str(pgdata),
                     "stop", "-m", "immediate"],
                    capture_output=True,
                )
        finally:
            shutil.rmtree(pgdata, ignore_errors=True)

    def test_falls_back_to_sigkill_when_pg_ctl_finds_nothing(
        self, tmp_path, owned_children,
    ) -> None:
        """A fake (non-PG) process in a dir pg_ctl cannot parse -- pg_ctl
        no-ops, and the fallback SIGKILL on the identity-verified pid
        still clears it."""
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        not_a_real_pgdata = tmp_path / "not_a_real_pgdata"
        not_a_real_pgdata.mkdir()

        _kill_postmaster_leg(proc.pid, not_a_real_pgdata, grace_s=0.5)

        proc.wait(timeout=5)
        assert proc.poll() is not None


class TestOwnerCmdlineReuseGuard:
    """Round-3 critique, Significant-1: owner-liveness used to be
    ``pid_alive(owner_pid)`` alone, despite ``_write_sidecar`` already
    recording ``owner_cmdline`` for exactly this comparison -- that field
    was written but never read (grep-confirmed single occurrence pre-fix).
    A pytest owner PID later REUSED by an unrelated long-lived process
    (Docker daemon, IDE, MCP server -- realistic on a dev box) made that
    cluster permanently ``live_untouched``, since nothing could ever prove
    the recorded owner was actually gone."""

    def test_matching_cmdline_is_live(self, owned_children) -> None:
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        assert _owner_is_live(proc.pid, _cmdline_of(proc)) is True

    def test_mismatched_cmdline_is_not_live(self, owned_children) -> None:
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        assert _owner_is_live(proc.pid, "an unrelated process --not-pytest") is False

    def test_dead_pid_is_not_live(self) -> None:
        assert _owner_is_live(_dead_pid(), "anything") is False

    def test_empty_recorded_cmdline_falls_back_to_pid_only(
        self, owned_children,
    ) -> None:
        """A sidecar written before this check existed (or a write-time
        read that came back empty) has no recorded cmdline to compare --
        falls back to the pid-only signal rather than manufacturing a
        mismatch out of missing data."""
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        assert _owner_is_live(proc.pid, "") is True

    def test_unreadable_current_cmdline_is_safe_direction_live(
        self, owned_children, monkeypatch,
    ) -> None:
        proc = _spawn_sleeper()
        owned_children.append(proc)
        time.sleep(0.1)
        monkeypatch.setattr(
            "nexus.daemon.service_registry.process_command", lambda pid: "",
        )
        assert _owner_is_live(proc.pid, "whatever was recorded") is True

    def test_reused_owner_pid_makes_a_stale_cluster_sweepable(
        self, tmp_path, owned_children,
    ) -> None:
        """Integration-shaped (reviewer's prescription): the owner pid is
        genuinely alive (a live child THIS test owns), but its RECORDED
        cmdline no longer matches what's actually running there --
        exactly what a PID-reuse looks like from the sweep's point of
        view. Before this fix, this cluster would be live_untouched
        forever; it must now be correctly identified as stale and swept."""
        reused_owner_proc = _spawn_sleeper()
        owned_children.append(reused_owner_proc)
        time.sleep(0.1)

        cluster_dir = tmp_path / "nexus_t2_substrate_pg_ownerreuse001"
        _write_cluster_sidecar(
            cluster_dir,
            owner_pytest_pid=reused_owner_proc.pid,
            owner_cmdline="pytest tests/some/old/session --that-exited-long-ago",
            engine_pid=_dead_pid(),
            engine_cmdline="",
            postmaster_pid=_dead_pid(),
            postmaster_cmdline="",
        )

        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)

        assert str(cluster_dir) not in result.live_untouched, (
            "REGRESSION GUARD: a reused owner pid must not keep a stale "
            "cluster live_untouched forever"
        )
        assert str(cluster_dir) in result.reaped
        assert not cluster_dir.exists()


class TestWorkerPortSharding:
    """Round-3 critique, Significant-2: under `-n auto`, every xdist
    worker independently drew from the SAME 20000-29000 range -- a
    probe-vs-bind race between two workers could hard-fail a whole
    worker's session (ensure_engine() remembers _boot_error and re-raises
    it for the rest of that process). Sharding by PYTEST_XDIST_WORKER
    removes the collision surface for the common case."""

    def test_no_worker_env_gets_the_full_range(self, monkeypatch) -> None:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
        assert _worker_shard_range() == _LOW_PORT_RANGE

    def test_gw0_gets_the_first_shard(self, monkeypatch) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        expected = range(
            _LOW_PORT_RANGE.start, _LOW_PORT_RANGE.start + _WORKER_SHARD_WIDTH,
        )
        assert _worker_shard_range() == expected

    def test_gw3_gets_a_disjoint_later_shard(self, monkeypatch) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
        start = _LOW_PORT_RANGE.start + 3 * _WORKER_SHARD_WIDTH
        assert _worker_shard_range() == range(start, start + _WORKER_SHARD_WIDTH)

    def test_shards_across_the_full_range_are_pairwise_disjoint(
        self, monkeypatch,
    ) -> None:
        seen: set[int] = set()
        for index in range(_WORKER_SHARD_MAX_INDEX + 1):
            monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{index}")
            shard = _worker_shard_range()
            shard_set = set(shard)
            assert not (shard_set & seen), (
                f"gw{index}'s shard overlaps an earlier worker"
            )
            seen |= shard_set
            assert shard.stop <= _LOW_PORT_RANGE.stop, (
                f"gw{index}'s shard spills past the low range's own ceiling"
            )

    def test_worker_index_beyond_capacity_falls_back_to_full_range(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", f"gw{_WORKER_SHARD_MAX_INDEX + 1}")
        assert _worker_shard_range() == _LOW_PORT_RANGE

    def test_malformed_worker_id_falls_back_to_full_range(self, monkeypatch) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "master")
        assert _worker_shard_range() == _LOW_PORT_RANGE

    def test_non_numeric_worker_suffix_falls_back_to_full_range(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gwabc")
        assert _worker_shard_range() == _LOW_PORT_RANGE

    def test_free_port_honours_the_worker_shard_when_env_set(
        self, monkeypatch,
    ) -> None:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        expected = range(
            _LOW_PORT_RANGE.start + _WORKER_SHARD_WIDTH,
            _LOW_PORT_RANGE.start + 2 * _WORKER_SHARD_WIDTH,
        )
        port = _free_port()
        assert port in expected
