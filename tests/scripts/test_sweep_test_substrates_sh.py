# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/sweep-test-substrates.sh (nexus-ui654) -- the manual-recovery
wrapper around tests._engine_substrate.sweep_stale_substrate_clusters().

The reaper LOGIC itself (dead-owner identity, PID-reuse guard, engine-then-
postmaster kill ordering, legacy-cluster refusal) is exhaustively covered
against fake process trees in tests/test_engine_substrate_sweep.py -- this
file only proves the SHELL SCRIPT wraps that logic correctly: it points at
the right tempdir root, reaps a genuinely dead-owner cluster, and refuses a
live-owner cluster, end to end through the actual subprocess a developer
would run.

Every cluster here lives under a per-test TMPDIR override passed to the
subprocess -- never the box's real tempdir -- so these tests cannot
interact with a real concurrent pytest session's live substrate either way
(same isolation discipline as tests/test_engine_substrate_sweep.py's
tmp_root= parameter, applied at the process-env level since the script
itself has no tmp_root argument).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sweep-test-substrates.sh"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _write_sidecar(cluster_dir: Path, **fields) -> None:
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / "nexus_substrate_sidecar.json").write_text(json.dumps(fields))


def _run_script(fake_tmpdir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "TMPDIR": str(fake_tmpdir)}
    return subprocess.run(
        [str(SCRIPT)], cwd=REPO_ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )


class TestScriptExistsAndIsExecutable:
    def test_script_is_executable(self) -> None:
        assert SCRIPT.exists(), f"missing: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"not executable: {SCRIPT}"


class TestCleanMachineIsANoOp:
    def test_empty_tempdir_reports_clean_and_exits_zero(self, tmp_path) -> None:
        fake_tmpdir = tmp_path / "empty_root"
        fake_tmpdir.mkdir()

        result = _run_script(fake_tmpdir)

        assert result.returncode == 0, result.stderr
        assert "nothing found -- clean" in result.stdout
        assert "sweep complete" in result.stdout


class TestReapsADeadOwnerCluster:
    def test_dead_owner_cluster_is_reaped_and_removed(self, tmp_path) -> None:
        fake_tmpdir = tmp_path / "root_with_stale"
        fake_tmpdir.mkdir()
        cluster_dir = fake_tmpdir / "nexus_t2_substrate_pg_scripttest001"
        _write_sidecar(
            cluster_dir,
            owner_pytest_pid=_dead_pid(),
            postmaster_pid=_dead_pid(),
            postmaster_cmdline="",
            engine_pid=_dead_pid(),
            engine_cmdline="",
        )

        result = _run_script(fake_tmpdir)

        assert result.returncode == 0, result.stderr
        assert "reaped (dead owner, killed + removed): 1" in result.stdout
        assert str(cluster_dir) in result.stdout
        assert not cluster_dir.exists(), (
            "the script must actually remove a dead-owner cluster's dir, "
            "not just report it"
        )


class TestRefusesALiveOwnerCluster:
    def test_live_owner_cluster_is_reported_untouched_never_killed(
        self, tmp_path,
    ) -> None:
        fake_tmpdir = tmp_path / "root_with_live"
        fake_tmpdir.mkdir()
        cluster_dir = fake_tmpdir / "nexus_t2_substrate_pg_scripttest002"
        # This very test process is alive for the whole subprocess call.
        _write_sidecar(
            cluster_dir,
            owner_pytest_pid=os.getpid(),
            postmaster_pid=999_999_999,
            postmaster_cmdline="irrelevant",
            engine_pid=999_999_998,
            engine_cmdline="irrelevant",
        )

        result = _run_script(fake_tmpdir)

        assert result.returncode == 0, result.stderr
        assert "live_untouched (owner still running -- refused): 1" in result.stdout
        assert str(cluster_dir) in result.stdout
        assert cluster_dir.exists(), (
            "a live owner's cluster must never be removed"
        )
        assert (cluster_dir / "nexus_substrate_sidecar.json").exists()
