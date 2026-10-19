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

from tests import _tmp_root as tr

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "sweep-test-substrates.sh"


@pytest.fixture(autouse=True)
def _outer_run_marks_a_real_tempdir(tmp_path, monkeypatch) -> None:
    """What a lab-run grant gives every test process: NX_TEST_TMP_ORIG naming a tempdir
    that is not the one a test builds. Set here so a script run that inherits it looks
    in the wrong place on a laptop too (nexus-g2f0p)."""
    decoy = tmp_path / "outer-real-tempdir"
    decoy.mkdir()
    monkeypatch.setenv(tr.ORIG_ENV, str(decoy))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _write_sidecar(cluster_dir: Path, **fields) -> None:
    cluster_dir.mkdir(parents=True, exist_ok=True)
    (cluster_dir / "nexus_substrate_sidecar.json").write_text(json.dumps(fields))


def _run_script(
    fake_tmpdir: Path, pg_root: str = "default",
) -> subprocess.CompletedProcess:
    # NX_TEST_PG_SHM_DIR keeps the sweep off the box's real /dev/shm root
    # (nexus-me3gm), where a concurrent session's live clusters would otherwise
    # show up in this test's counts.
    # child_env_with_tmpdir drops the outer run's NX_TEST_TMP_ORIG: inside a lab-run grant
    # it names the box's real tempdir and wins over TMPDIR, so the sweep would look there
    # instead of at fake_tmpdir (nexus-kyujr). _outer_run_marks_a_real_tempdir below sets
    # it for every test here, so dropping it matters outside a grant too (nexus-g2f0p).
    env = {
        **tr.child_env_with_tmpdir(os.environ, fake_tmpdir),
        "NX_TEST_PG_ROOT": pg_root,
        # The sweep also reads the tmpfs root regardless of NX_TEST_PG_ROOT;
        # point that at a directory that does not exist.
        "NX_TEST_PG_SHM_DIR": str(fake_tmpdir / "no-shm"),
    }
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


class TestSweepsTheTmpfsRoot:
    def test_dead_owner_cluster_under_the_pg_root_is_reaped(self, tmp_path) -> None:
        """A crash-orphaned cluster in the tmpfs root is RAM until reaped
        (nexus-me3gm): the script must look there, not only in the tempdir."""
        fake_tmpdir = tmp_path / "empty_tmp"
        fake_tmpdir.mkdir()
        pg_root = tmp_path / "shm_root"
        cluster_dir = pg_root / "nexus_t2_substrate_pg_shmtest001"
        _write_sidecar(
            cluster_dir,
            owner_pytest_pid=_dead_pid(),
            postmaster_pid=_dead_pid(),
            postmaster_cmdline="",
            engine_pid=_dead_pid(),
            engine_cmdline="",
        )

        result = _run_script(fake_tmpdir, pg_root=str(pg_root))

        assert result.returncode == 0, result.stderr
        assert f"root: {pg_root}" in result.stdout
        assert "reaped (dead owner, killed + removed): 1" in result.stdout
        assert not cluster_dir.exists()


class TestSweepsAnIntegrationOwnerRecord:
    def test_dead_owner_integration_cluster_under_the_pg_root_is_reaped(self, tmp_path) -> None:
        """nexus-942h9: an integration fixture's cluster is named by its own prefix
        and recorded in nexus_integration_owner_<pid>.json, not matched by the
        substrate glob; the script must reap it from the root all the same."""
        fake_tmpdir = tmp_path / "empty_tmp"
        fake_tmpdir.mkdir()
        pg_root = tmp_path / "shm_root"
        cluster_dir = pg_root / "nexus_inttest_pg_scripttest"
        cluster_dir.mkdir(parents=True)
        (cluster_dir / "PG_VERSION").write_text("17\n")
        record = pg_root / "nexus_integration_owner_1.json"
        record.write_text(json.dumps({
            "owner_pytest_pid": _dead_pid(), "owner_cmdline": "",
            "pgdatas": [str(cluster_dir)], "children": [],
        }))

        result = _run_script(fake_tmpdir, pg_root=str(pg_root))

        assert result.returncode == 0, result.stderr
        assert "nexus_integration_owner_*.json" in result.stdout
        assert "reaped (dead owner, killed + removed): 1" in result.stdout
        assert str(cluster_dir) in result.stdout
        assert not cluster_dir.exists() and not record.exists()
