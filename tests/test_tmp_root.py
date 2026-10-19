# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pytest basetemp and TMPDIR on tmpfs inside a lab-run grant (nexus-kyujr).

The decision is driven with injected mount tables, free space and cgroup headroom,
so no outcome depends on the box running it. The end-to-end tests run a real nested
pytest (serial, xdist and a SIGKILLed one) against a directory standing in for
``/dev/shm`` (``NX_TEST_TMP_SHM_DIR``), so they run on a laptop too.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import _engine_substrate as sub
from tests import _tmp_root as tr
from tests._module_seam import setattr_in

REPO_ROOT = Path(__file__).resolve().parents[1]

_TMPFS_MOUNTS = (
    "proc /proc proc rw 0 0\n"
    "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
    "/dev/nvme0n1p2 / ext4 rw 0 0\n"
)
_NOEXEC_MOUNTS = "tmpfs /dev/shm tmpfs rw,nosuid,nodev,noexec 0 0\n"
_NO_TMPFS_MOUNTS = "proc /proc proc rw 0 0\n/dev/nvme0n1p2 / ext4 rw 0 0\n"
_GRANT = {"LAB_CPUS": "8"}
#: The plugin names the root after the running user.
_ROOT = f"nexus-tmp-{sub._current_uid()}"


def _roomy(_p: Path) -> int:
    return 64 * 1024**3


def _decide(**kw) -> tr.TmpDecision:
    base = dict(
        env=dict(_GRANT), system="Linux", mounts_text=_TMPFS_MOUNTS,
        free_bytes=_roomy, writable=lambda _p: True, uid=1000,
        cgroup_headroom=lambda: None,
    )
    return tr.decide_tmp_root(**{**base, **kw})


class TestDecision:
    def test_a_linux_grant_with_room_gets_a_per_uid_root_beside_the_cluster_root(self) -> None:
        d = _decide()
        assert d.root == Path("/dev/shm/nexus-tmp-1000") and d.reason is None and not d.forced
        assert d.root != sub._tmpfs_candidate(1000)

    def test_outside_a_grant_nothing_moves(self) -> None:
        d = _decide(env={})
        assert d.root is None and "LAB_CPUS" in d.reason and not d.expected_tmpfs

    def test_a_blank_grant_variable_is_not_a_grant(self) -> None:
        assert _decide(env={"LAB_CPUS": "  "}).root is None

    def test_macos_in_a_grant_is_unchanged(self) -> None:
        d = _decide(system="Darwin")
        assert d.root is None and d.reason == "not Linux"

    def test_dev_shm_that_is_not_tmpfs_is_refused_and_warned_about(self) -> None:
        d = _decide(mounts_text=_NO_TMPFS_MOUNTS)
        assert d.root is None and "not tmpfs" in d.reason and d.expected_tmpfs

    def test_a_noexec_dev_shm_is_refused(self) -> None:
        d = _decide(mounts_text=_NOEXEC_MOUNTS)
        assert d.root is None and "noexec" in d.reason and d.expected_tmpfs

    def test_an_unreadable_mount_table_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_read_mounts", lambda: None)
        d = _decide(mounts_text=None)
        assert d.root is None and "/proc/mounts" in d.reason

    def test_an_unwritable_dev_shm_is_refused(self) -> None:
        d = _decide(writable=lambda _p: False)
        assert d.root is None and "not writable" in d.reason

    def test_too_little_free_space_is_refused(self) -> None:
        d = _decide(free_bytes=lambda _p: 1 * 1024**3)
        assert d.root is None and "1024 MiB free" in d.reason

    def test_low_cgroup_headroom_is_refused(self) -> None:
        d = _decide(cgroup_headroom=lambda: 2 * 1024**3)
        assert d.root is None and "cgroup memory headroom is 2048 MiB" in d.reason

    def test_no_cgroup_limit_is_not_a_refusal(self) -> None:
        assert _decide(cgroup_headroom=lambda: None).root is not None

    def test_the_floor_is_the_cluster_roots_floor(self) -> None:
        assert tr.MIN_FREE_BYTES == sub._TMPFS_MIN_FREE_BYTES
        assert _decide(cgroup_headroom=lambda: tr.MIN_FREE_BYTES).root is not None
        assert _decide(cgroup_headroom=lambda: tr.MIN_FREE_BYTES - 1).root is None

    def test_default_override_keeps_the_old_behaviour(self) -> None:
        d = _decide(env={**_GRANT, "NX_TEST_TMP_ROOT": "default"})
        assert d.root is None and "NX_TEST_TMP_ROOT=default" in d.reason and not d.expected_tmpfs

    def test_a_directory_override_is_forced_outside_a_grant_too(self, tmp_path) -> None:
        d = _decide(env={"NX_TEST_TMP_ROOT": str(tmp_path / "keep")})
        assert d.root == tmp_path / "keep" and d.forced

    def test_cluster_default_override_keeps_the_tempdir_for_everything(self) -> None:
        d = _decide(env={**_GRANT, "NX_TEST_PG_ROOT": "default"})
        assert d.root is None and "NX_TEST_PG_ROOT=default" in d.reason

    def test_the_seam_stands_in_for_dev_shm_on_any_platform(self, tmp_path) -> None:
        d = _decide(env={**_GRANT, "NX_TEST_TMP_SHM_DIR": str(tmp_path)}, system="Darwin", mounts_text="")
        assert d.root == tmp_path / "nexus-tmp-1000"

    def test_the_tmpdir_leaves_room_for_a_cluster_socket_path(self) -> None:
        d = _decide(uid=123456)
        assert d.root is not None
        assert sub._socket_path_fits(d.root / ("r" + "x" * 8) / "t")

    def test_a_root_whose_socket_paths_would_pass_sun_path_is_refused(self) -> None:
        d = _decide(uid="9" * 100)
        assert d.root is None and "sun_path" in d.reason and d.expected_tmpfs

    def test_the_seam_skips_the_sun_path_gate(self, tmp_path) -> None:
        """The seam stands in for /dev/shm on a laptop whose temp paths are long; the gate
        is for the real thing."""
        shm = tmp_path / ("s" * 120)
        shm.mkdir()
        d = _decide(env={"LAB_CPUS": "4", "NX_TEST_TMP_SHM_DIR": str(shm)})
        assert d.root is not None


class TestEnvSpellings:
    def test_the_names_the_service_fixture_spells_out_are_the_ones_set(self) -> None:
        """tests/db/_service_fixture.py cannot import these (it sits below both modules in the
        import graph), so it repeats them."""
        from tests.db import _service_fixture as fx

        assert fx.TMP_ORIG_ENV == sub._TMP_ORIG_ENV == tr.ORIG_ENV
        assert fx.TMP_SESSION_ENV == tr.SESSION_ENV


class TestRunDirs:
    def test_layout_privacy_and_owner_record(self, tmp_path) -> None:
        run = tr.make_run_dir(tmp_path / "root")
        assert run.parent == tmp_path / "root" and run.name.startswith("r")
        assert (tmp_path / "root").stat().st_mode & 0o777 == 0o700
        assert (run / "t").is_dir() and (run / "p").is_dir()
        rec = json.loads((run / "owner.json").read_text())
        assert rec["pid"] == os.getpid()

    def test_a_root_someone_else_owns_is_refused(self, tmp_path, monkeypatch) -> None:
        (tmp_path / "root").mkdir()
        monkeypatch.setattr(sub, "_current_uid", lambda: str(os.getuid() + 1))
        with pytest.raises(PermissionError, match="owned by uid"):
            tr.make_run_dir(tmp_path / "root")

    def test_a_root_that_is_a_symlink_is_refused(self, tmp_path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "root").symlink_to(real)
        with pytest.raises(PermissionError, match="not a real directory"):
            tr.make_run_dir(tmp_path / "root")

    def test_a_world_readable_root_is_tightened(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        tr.make_run_dir(root)
        assert root.stat().st_mode & 0o077 == 0

    def test_a_script_that_cannot_run_fails_the_probe_and_leaves_no_dir(self, tmp_path, monkeypatch) -> None:
        def refuse(*_a, **_k):
            return subprocess.CompletedProcess([], 126)

        setattr_in(monkeypatch, tr, "subprocess.run", refuse)
        with pytest.raises(PermissionError, match="cannot run"):
            tr.make_run_dir(tmp_path / "root")
        assert list((tmp_path / "root").iterdir()) == []


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class TestSweep:
    def test_a_run_whose_controller_is_gone_is_removed_and_drained_first(self, tmp_path) -> None:
        root = tmp_path / "root"
        run = tr.make_run_dir(root, pid=_dead_pid(), probe=False)
        (run / "p" / "stranded").write_text("x")
        seen: list[tuple[Path, bool]] = []
        removed = tr.sweep_stale_runs(root, drain=lambda p: seen.append((p, p.is_dir())))
        assert removed == [run] and not run.exists()
        assert seen == [(run / "p", True)], "the daemon reaper sees the tree BEFORE it is deleted"

    def test_a_live_controller_keeps_its_run(self, tmp_path) -> None:
        root = tmp_path / "root"
        run = tr.make_run_dir(root, probe=False)
        assert tr.sweep_stale_runs(root) == [] and run.is_dir()

    def test_a_recycled_pid_is_a_dead_controller(self, tmp_path) -> None:
        if tr._process_start(os.getpid()) is None:
            pytest.skip("no /proc: start times are not recorded here")
        root = tmp_path / "root"
        run = tr.make_run_dir(root, probe=False)
        rec = json.loads((run / "owner.json").read_text())
        rec["starttime"] = str(int(rec["starttime"]) + 1)
        (run / "owner.json").write_text(json.dumps(rec))
        assert tr.sweep_stale_runs(root) == [run]

    def test_a_bare_dir_is_kept_until_it_is_old_enough(self, tmp_path) -> None:
        root = tmp_path / "root"
        bare = root / "rbare0000"
        bare.mkdir(parents=True)
        assert tr.sweep_stale_runs(root) == [] and bare.is_dir()
        past = time.time() - tr.BARE_MIN_AGE_S - 5
        os.utime(bare, (past, past))
        assert tr.sweep_stale_runs(root) == [bare] and not bare.exists()

    def test_only_real_r_dirs_are_touched(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        other = root / "keepme"
        other.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "f").write_text("x")
        link = root / "rlink0000"
        link.symlink_to(elsewhere)
        (root / "rfile").write_text("x")
        assert tr.sweep_stale_runs(root, now=time.time() + 10 * tr.BARE_MIN_AGE_S) == []
        assert other.is_dir() and (elsewhere / "f").exists() and link.is_symlink()

    def test_a_read_only_tree_is_still_removed(self, tmp_path) -> None:
        root = tmp_path / "root"
        run = tr.make_run_dir(root, pid=_dead_pid(), probe=False)
        locked = run / "p" / "locked"
        locked.mkdir()
        (locked / "f").write_text("x")
        locked.chmod(0o500)
        assert tr.sweep_stale_runs(root) == [run] and not run.exists()

    def test_a_failing_drain_does_not_stop_the_removal(self, tmp_path) -> None:
        root = tmp_path / "root"
        run = tr.make_run_dir(root, pid=_dead_pid(), probe=False)

        def boom(_p: Path) -> None:
            raise RuntimeError("ps failed")

        assert tr.sweep_stale_runs(root, drain=boom) == [run]

    def test_a_run_dir_another_user_owns_is_never_touched(self, tmp_path, monkeypatch) -> None:
        """/dev/shm is shared: a dead-looking run dir that is not ours is left alone, drained
        by nobody and removed by nobody."""
        root = tmp_path / "root"
        run = tr.make_run_dir(root, pid=_dead_pid(), probe=False)
        drained: list[Path] = []
        monkeypatch.setattr(sub, "_current_uid", lambda: str(os.getuid() + 1))
        assert tr.sweep_stale_runs(root, drain=drained.append) == []
        assert run.is_dir() and drained == []
        monkeypatch.undo()
        assert tr.sweep_stale_runs(root) == [run], "the same dir IS swept when it is ours"

    def test_the_conftest_drain_signals_only_daemons_under_the_stale_tree(self, tmp_path, monkeypatch) -> None:
        """The real ``_drain_stale_tmp_run`` of tests/conftest.py, with ``ps`` and the signal
        stubbed: a postmaster rooted in the stale ``p`` tree gets SIGQUIT, one outside it and
        the user's own are never signalled."""
        from tests import conftest as cf

        stale_p = tmp_path / "shm" / _ROOT / "rdead0000" / "p"
        (stale_p / "pytest-of-u" / "pytest-0" / "pgdata").mkdir(parents=True)
        procs = [
            (31, f"/opt/pg/bin/postgres -D {stale_p.resolve()}/pytest-of-u/pytest-0/pgdata -p 5433"),
            (32, f"/opt/pg/bin/postgres -D {tmp_path.resolve()}/elsewhere/pgdata -p 5434"),
            (33, "/usr/local/pgsql/bin/postgres -D /Users/dev/pgdata"),
        ]
        killed: list[tuple[int, str]] = []
        monkeypatch.setattr(cf, "_ps_all", lambda: procs)
        cf._drain_stale_tmp_run(stale_p, _kill=lambda pid, sig: killed.append((pid, sig)))
        assert killed == [(31, "QUIT")]


def _plugin(tmp_path, env=None, **decide_kw) -> tuple[tr.TmpRootPlugin, dict]:
    shm = tmp_path / "shm"
    shm.mkdir(exist_ok=True)
    env = {"LAB_CPUS": "4", "NX_TEST_TMP_SHM_DIR": str(shm), "TMPDIR": "/orig/tmp"} if env is None else env
    plug = tr.TmpRootPlugin(
        env=env,
        decide=lambda env: tr.decide_tmp_root(
            env=env, free_bytes=_roomy, cgroup_headroom=lambda: None, **decide_kw),
    )
    return plug, env


def _config(basetemp=None, worker=False) -> SimpleNamespace:
    cfg = SimpleNamespace(option=SimpleNamespace(basetemp=basetemp))
    if worker:
        cfg.workerinput = {}
    return cfg


class TestPlugin:
    def test_the_decision_points_both_pytest_and_tempfile_into_the_run_dir(self, tmp_path) -> None:
        plug, env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        run = plug.run
        assert run is not None and run.parent == tmp_path / "shm" / _ROOT
        assert env["TMPDIR"] == str(run / "t")
        assert env["PYTEST_DEBUG_TEMPROOT"] == str(run / "p")
        assert env[sub._TMP_ORIG_ENV] == "/orig/tmp"
        assert env[tr.SESSION_ENV] == f"tmpfs={run}"

    def test_the_original_tempdir_is_recorded_for_the_boot_slots_and_the_sweep(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(sub._TMP_ORIG_ENV, "/orig/tmp")
        assert sub._original_tempdir() == Path("/orig/tmp")
        assert sub.substrate_pg_roots(env={}, system="Darwin") == [Path("/orig/tmp")]
        monkeypatch.delenv(sub._TMP_ORIG_ENV)
        assert sub._original_tempdir() == Path(tempfile.gettempdir())

    def test_a_worker_imported_after_the_switch_still_keeps_its_boot_slots_on_the_original_tempdir(self, tmp_path) -> None:
        """An xdist worker imports the substrate with TMPDIR already moved; its boot-slot
        directory must still be the one every other run on the box uses."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("NX_TEST_TMP")}
        env.update(TMPDIR=str(tmp_path / "moved"), NX_TEST_TMP_ORIG=str(tmp_path / "orig"), PYTHONPATH=str(REPO_ROOT))
        out = subprocess.run(
            [sys.executable, "-c", "import tests._engine_substrate as s; print(s._BOOT_SEMAPHORE_DIR.parent)"],
            env=env, capture_output=True, text=True, check=True, cwd=REPO_ROOT,
        ).stdout.strip()
        assert out == str(tmp_path / "orig")

    def test_a_given_basetemp_is_left_alone(self, tmp_path) -> None:
        plug, env = _plugin(tmp_path)
        plug.pytest_configure(_config(basetemp=str(tmp_path / "mine")))
        assert "PYTEST_DEBUG_TEMPROOT" not in env and env["TMPDIR"].startswith(str(tmp_path / "shm"))
        assert "--basetemp" in plug.summary_line()

    def test_a_worker_and_a_nested_run_do_not_decide(self, tmp_path) -> None:
        plug, env = _plugin(tmp_path)
        plug.pytest_configure(_config(worker=True))
        assert not plug.decided and tr.SESSION_ENV not in env
        env[tr.SESSION_ENV] = "disk=inherited"
        plug.pytest_configure(_config())
        assert not plug.decided and env["TMPDIR"] == "/orig/tmp" and plug.summary_line() is None

    def test_the_end_of_the_run_removes_the_run_and_puts_the_environment_back(self, tmp_path) -> None:
        plug, env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        run = plug.run
        (run / "p" / "pytest-of-x").mkdir()
        (run / "p" / "pytest-of-x" / "blob").write_bytes(b"0" * 3 * 1024**2)
        assert "3 MiB at the end of the run" in plug.summary_line()
        assert run.is_dir(), "the summary-time report plugins still write into the basetemp"
        plug.pytest_unconfigure(_config())
        assert not run.exists() and list((tmp_path / "shm" / _ROOT).iterdir()) == []
        assert env["TMPDIR"] == "/orig/tmp" and "PYTEST_DEBUG_TEMPROOT" not in env
        assert tr.SESSION_ENV not in env and sub._TMP_ORIG_ENV not in env

    def test_the_summary_line_names_the_root_the_size_and_the_report_home(self, tmp_path) -> None:
        plug, _env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        line = plug.summary_line()
        assert f"basetemp and TMPDIR on tmpfs {plug.run}" in line and "removed at exit" in line
        assert str(Path("/orig/tmp") / f"nexus-test-reports-{sub._current_uid()}" / plug.run.name) in line

    def test_nx_reports_are_copied_to_disk_before_the_basetemp_goes(self, tmp_path) -> None:
        plug, _env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        bt = plug.run / "p" / "pytest-of-u" / "pytest-0"
        (bt / "popen-gw0").mkdir(parents=True)
        (bt / "nx-ddl-report.json").write_text('{"a": 1}')
        (bt / "nx-storage-touch-report.json").write_text("{}")
        (bt / "popen-gw0" / "nx-ddl-worker-1.json").write_text("{}")  # intermediate: merged by the controller
        (bt / "test_x0").mkdir()
        (bt / "test_x0" / "nx-not-a-report.json").write_text("{}")
        orig = tmp_path / "orig"
        orig.mkdir()
        plug._orig_tempdir = orig
        plug.pytest_unconfigure(_config())
        home = orig / f"nexus-test-reports-{sub._current_uid()}" / plug.run.name
        assert sorted(f.name for f in home.iterdir()) == ["nx-ddl-report.json", "nx-storage-touch-report.json"]
        assert (home / "nx-ddl-report.json").read_text() == '{"a": 1}'
        assert not plug.run.exists()

    def test_only_the_newest_three_report_runs_are_kept(self, tmp_path) -> None:
        orig = tmp_path / "orig"
        homes = []
        for i in range(5):
            plug, _env = _plugin(tmp_path)
            plug.pytest_configure(_config())
            bt = plug.run / "p" / "pytest-of-u" / "pytest-0"
            bt.mkdir(parents=True)
            (bt / "nx-r.json").write_text(str(i))
            orig.mkdir(exist_ok=True)
            plug._orig_tempdir = orig
            plug.pytest_unconfigure(_config())
            home = plug._report_home()
            os.utime(home, (1_000_000 + i, 1_000_000 + i))
            homes.append(home)
        assert [h.exists() for h in homes] == [False, False, True, True, True]

    def test_a_run_with_no_reports_makes_no_report_dir(self, tmp_path) -> None:
        plug, _env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        orig = tmp_path / "orig"
        orig.mkdir()
        plug._orig_tempdir = orig
        plug.pytest_unconfigure(_config())
        assert list(orig.iterdir()) == []

    def test_a_forced_root_is_kept_and_never_swept(self, tmp_path) -> None:
        keep = tmp_path / "keep"
        plug, env = _plugin(tmp_path, env={"NX_TEST_TMP_ROOT": str(keep), "TMPDIR": "/orig/tmp"})
        plug.pytest_configure(_config())
        (keep / "p" / "failed-test-dir").mkdir()
        plug.pytest_unconfigure(_config())
        assert (keep / "p" / "failed-test-dir").is_dir()
        assert env["TMPDIR"] == str(keep / "t") and "forced by NX_TEST_TMP_ROOT" in plug.summary_line()

    def test_the_start_sweeps_a_crashed_runs_directory(self, tmp_path) -> None:
        root = tmp_path / "shm" / _ROOT
        stale = tr.make_run_dir(root, pid=_dead_pid(), probe=False)
        drained: list[Path] = []
        plug, _env = _plugin(tmp_path)
        plug._drain = drained.append
        plug.pytest_configure(_config())
        assert not stale.exists() and drained == [stale / "p"] and plug.run.is_dir()

    def test_a_refusal_stays_on_disk_says_why_and_warns_in_a_grant(self, tmp_path) -> None:
        plug, env = _plugin(tmp_path, mounts_text=_NO_TMPFS_MOUNTS, system="Linux", env={
            "LAB_CPUS": "4", "TMPDIR": "/orig/tmp"})
        with pytest.warns(UserWarning, match="not putting basetemp and TMPDIR on tmpfs"):
            plug.pytest_configure(_config())
        assert env["TMPDIR"] == "/orig/tmp" and env[tr.SESSION_ENV].startswith("disk=")
        assert "on disk" in plug.summary_line() and "not tmpfs" in plug.summary_line()

    def test_outside_a_grant_is_quiet_and_unchanged(self, tmp_path, recwarn) -> None:
        plug, env = _plugin(tmp_path, env={"TMPDIR": "/orig/tmp"})
        plug.pytest_configure(_config())
        assert not recwarn.list and env["TMPDIR"] == "/orig/tmp"
        assert "lab-run grant" in plug.summary_line()

    def test_a_decision_that_raises_falls_back_to_disk(self, tmp_path) -> None:
        def boom(env):
            raise RuntimeError("no")

        plug = tr.TmpRootPlugin(env={"TMPDIR": "/orig/tmp"}, decide=boom)
        with pytest.warns(UserWarning, match="could not decide"):
            plug.pytest_configure(_config())
        assert plug.run is None and plug.decided

    def test_the_real_conftest_registers_the_plugin_before_anything_else_in_configure(self) -> None:
        src = (REPO_ROOT / "tests" / "conftest.py").read_text()
        reg = src.index('register(TmpRootPlugin(drain=_drain_stale_tmp_run), "nexus-tmp-root")')
        assert reg < src.index("_clamp_xdist_workers(config)\n    _begin_pg_root_session(config)")


def _run_tree(tmp_path) -> Path:
    """A run dir shaped like a finished xdist run that had two failing tests, one passing test
    and one engine."""
    run = tr.make_run_dir(tmp_path / "shm" / _ROOT, probe=False)
    (run / "t" / "nexus-svc-log-abc").mkdir()
    (run / "t" / "nexus-svc-log-abc" / "engine.log").write_text("engine boom\n")
    (run / "t" / "unrelated-scratch").mkdir()
    bt = run / "p" / "pytest-of-u" / "pytest-0"
    for rel, body in (
        ("popen-gw0/test_failing_1_2_0", "param evidence"),
        ("popen-gw1/test_other_failure0", "worker evidence"),
        ("popen-gw1/test_passing0", "must not be copied"),
        ("test_serial_failure0", "controller evidence"),
    ):
        (bt / rel).mkdir(parents=True)
        (bt / rel / "data.txt").write_text(body)
    (run / "p" / "pytest-of-u" / "pytest-current").symlink_to(bt)
    return run


_FAILED = [
    "tests/test_x.py::TestA::test_failing[1-2]",
    "tests/test_y.py::test_other_failure",
    "tests/test_z.py::test_serial_failure",
]


class TestFailureEvidence:
    def test_the_tmp_dir_name_follows_pytests_own_rule(self) -> None:
        assert tr._tmp_dir_prefix("a.py::C::test_failing[1-2]") == "test_failing_1_2_"
        assert tr._tmp_dir_prefix("a.py::test_x[a::b]") == "test_x_a__b_"
        assert tr._tmp_dir_prefix("a.py::" + "t" * 50) == "t" * 30

    def test_the_failed_tests_dirs_are_found_in_the_controller_and_worker_basetemps(self, tmp_path) -> None:
        run = _run_tree(tmp_path)
        found = {d.name for d in tr.failed_tmp_dirs(run / "p", _FAILED)}
        assert found == {"test_failing_1_2_0", "test_other_failure0", "test_serial_failure0"}
        assert tr.failed_tmp_dirs(run / "p", []) == []

    def test_engine_logs_and_failed_dirs_are_copied_and_the_rest_is_not(self, tmp_path) -> None:
        run = _run_tree(tmp_path)
        dest = tmp_path / "out" / "failed"
        notes = tr.copy_failure_evidence(run, dest, _FAILED, exitstatus=1)
        assert (dest / "engine-logs" / "nexus-svc-log-abc" / "engine.log").read_text() == "engine boom\n"
        assert sorted(p.parent.name for p in dest.glob("tmp_path/**/data.txt")) == [
            "test_failing_1_2_0", "test_other_failure0", "test_serial_failure0",
        ], "the passing test's dir is not copied"
        assert not (dest / "unrelated-scratch").exists()
        # the worker's dir keeps its place in the tree, so two workers' same-named dirs do not collide
        assert (dest / "tmp_path" / "pytest-of-u" / "pytest-0" / "popen-gw0" / "test_failing_1_2_0" / "data.txt").is_file()
        summary = (dest / tr.FAILURE_SUMMARY_FILE).read_text()
        assert "exit status 1" in summary and "test_serial_failure" in summary and notes

    def test_the_size_cap_skips_and_says_so_and_engine_logs_come_first(self, tmp_path) -> None:
        run = _run_tree(tmp_path)
        big = run / "p" / "pytest-of-u" / "pytest-0" / "test_serial_failure0" / "big.bin"
        big.write_bytes(b"0" * 4096)
        dest = tmp_path / "out" / "failed"
        notes = tr.copy_failure_evidence(run, dest, _FAILED, exitstatus=1, cap=2048)
        assert (dest / "engine-logs" / "nexus-svc-log-abc" / "engine.log").is_file()
        assert not (dest / "tmp_path" / "pytest-of-u" / "pytest-0" / "test_serial_failure0").exists()
        assert any("skipped" in n and "test_serial_failure0" in n and "cap" in n for n in notes)
        assert "skipped" in (dest / tr.FAILURE_SUMMARY_FILE).read_text()

    def test_a_socket_in_a_failed_dir_does_not_stop_the_copy(self, tmp_path, monkeypatch) -> None:
        import socket

        run = _run_tree(tmp_path)
        d = run / "p" / "pytest-of-u" / "pytest-0" / "test_serial_failure0"
        monkeypatch.chdir(d)  # a relative bind: pytest's tmp_path is past sun_path on macOS
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind("s")
            dest = tmp_path / "out" / "failed"
            tr.copy_failure_evidence(run, dest, _FAILED, exitstatus=1)
            copied = dest / "tmp_path" / "pytest-of-u" / "pytest-0" / "test_serial_failure0"
            assert (copied / "data.txt").is_file() and not (copied / "s").exists()
        finally:
            sock.close()

    def test_the_fenced_home_is_copied_only_when_asked_and_ignores_the_cap(self, tmp_path) -> None:
        run = _run_tree(tmp_path)
        home = run / "t" / "nx-suite-home-xyz"
        (home / ".cache").mkdir(parents=True)
        (home / ".cache" / "blob").write_bytes(b"0" * 8192)
        (home / ".m2").symlink_to(tmp_path / "real-m2")
        asked = tmp_path / "asked" / "failed"
        tr.copy_failure_evidence(run, asked, _FAILED, exitstatus=1, keep_fenced_home=True, cap=16)
        assert (asked / "fenced-home" / "nx-suite-home-xyz" / ".cache" / "blob").stat().st_size == 8192
        assert (asked / "fenced-home" / "nx-suite-home-xyz" / ".m2").is_symlink(), "links stay links"
        not_asked = tmp_path / "not-asked" / "failed"
        tr.copy_failure_evidence(run, not_asked, _FAILED, exitstatus=1)
        assert not (not_asked / "fenced-home").exists()

    def _finished_plugin(self, tmp_path, exitstatus, *, env_extra=None):
        orig = tmp_path / "orig"
        orig.mkdir(exist_ok=True)
        plug, env = _plugin(tmp_path, env={
            "LAB_CPUS": "4", "NX_TEST_TMP_SHM_DIR": str(tmp_path / "shm"), "TMPDIR": str(orig), **(env_extra or {}),
        })
        plug.pytest_configure(_config())
        bt = plug.run / "p" / "pytest-of-u" / "pytest-0"
        (bt / "test_boom0").mkdir(parents=True)
        (bt / "test_boom0" / "data.txt").write_text("evidence")
        (plug.run / "t" / "nexus-svc-log-q").mkdir()
        (plug.run / "t" / "nexus-svc-log-q" / "engine.log").write_text("log")
        (plug.run / "t" / "nx-suite-home-q").mkdir()
        for nodeid, failed in (("t.py::test_boom", True), ("t.py::test_ok", False), ("t.py::test_boom", True)):
            plug.pytest_runtest_logreport(SimpleNamespace(nodeid=nodeid, failed=failed))
        plug.pytest_sessionfinish(None, exitstatus)
        return plug, orig

    def test_a_failed_run_keeps_its_evidence_under_the_report_home_before_the_run_goes(self, tmp_path) -> None:
        plug, orig = self._finished_plugin(tmp_path, 1)
        assert plug.failed_nodeids == ["t.py::test_boom"]
        run = plug.run
        plug.pytest_unconfigure(_config())
        failed = plug._report_home() / tr.FAILED_SUBDIR
        assert not run.exists()
        assert (failed / "engine-logs" / "nexus-svc-log-q" / "engine.log").read_text() == "log"
        assert (failed / "tmp_path" / "pytest-of-u" / "pytest-0" / "test_boom0" / "data.txt").read_text() == "evidence"
        assert "t.py::test_boom" in (failed / tr.FAILURE_SUMMARY_FILE).read_text()
        assert not (failed / "fenced-home").exists(), "removed unless NX_KEEP_FENCED_HOME is set"

    def test_keep_fenced_home_copies_the_home_out_of_the_run_dir(self, tmp_path) -> None:
        plug, _orig = self._finished_plugin(tmp_path, 1, env_extra={"NX_KEEP_FENCED_HOME": "1"})
        plug.pytest_unconfigure(_config())
        assert (plug._report_home() / tr.FAILED_SUBDIR / "fenced-home" / "nx-suite-home-q").is_dir()

    @pytest.mark.parametrize("exitstatus", [0, 5])
    def test_a_clean_run_keeps_no_evidence(self, tmp_path, exitstatus) -> None:
        plug, orig = self._finished_plugin(tmp_path, exitstatus)
        plug.pytest_unconfigure(_config())
        assert list(orig.iterdir()) == [], "nothing is written to disk for a passing run with no reports"

    def test_the_summary_line_says_a_failed_run_is_kept_and_where(self, tmp_path) -> None:
        plug, _orig = self._finished_plugin(tmp_path, 1)
        line = plug.summary_line()
        assert "FAILED" in line and str(plug._report_home() / tr.FAILED_SUBDIR) in line and "SUMMARY.txt" in line
        (tmp_path / "ok").mkdir()
        ok, _ = self._finished_plugin(tmp_path / "ok", 0)
        assert "FAILED" not in ok.summary_line()

    def test_the_terminal_summary_hook_learns_the_exit_status_too(self, tmp_path) -> None:
        plug, _env = _plugin(tmp_path)
        plug.pytest_configure(_config())
        written: list[str] = []
        plug.pytest_terminal_summary(SimpleNamespace(write_line=written.append), 1, _config())
        assert plug.failed_run and "FAILED" in written[0]


class TestKeptReportPath:
    def _env(self, run: Path) -> dict[str, str]:
        return {tr.SESSION_ENV: f"tmpfs={run}", tr.ORIG_ENV: "/orig/tmp"}

    def test_a_basetemp_report_is_named_by_its_disk_copy(self, tmp_path) -> None:
        run = tmp_path / "shm" / _ROOT / "rabc12345"
        report = run / "p" / "pytest-of-u" / "pytest-0" / "nx-ddl-report.json"
        assert tr.kept_report_path(report, self._env(run)) == (
            Path("/orig/tmp") / f"nexus-test-reports-{sub._current_uid()}" / "rabc12345" / "nx-ddl-report.json"
        )

    @pytest.mark.parametrize("rel", [
        "p/pytest-of-u/pytest-0/popen-gw0/nx-ddl-worker-1.json",  # not top level: never harvested
        "p/pytest-of-u/pytest-0/test_x0/nx-r.json",
        "p/pytest-of-u/pytest-0/notnx.json",
        "t/nx-ddl-report.json",  # TMPDIR, not the pytest root
    ])
    def test_what_the_harvest_does_not_copy_is_not_renamed(self, tmp_path, rel) -> None:
        run = tmp_path / "shm" / _ROOT / "rabc12345"
        assert tr.kept_report_path(run / rel, self._env(run)) == run / rel

    def test_outside_a_tmpfs_run_the_path_is_unchanged(self, tmp_path) -> None:
        p = tmp_path / "pytest-of-u" / "pytest-0" / "nx-ddl-report.json"
        for env in ({}, {tr.SESSION_ENV: "disk=because"}, {tr.SESSION_ENV: f"forced={tmp_path}"}):
            assert tr.kept_report_path(p, env) == p

    def test_both_report_plugins_print_the_kept_path(self, tmp_path, monkeypatch) -> None:
        import inspect

        from tests import _ddl_counter as ddl
        from tests import _storage_touch_guard as guard

        run = tmp_path / "shm" / _ROOT / "rabc12345"
        report = run / "p" / "pytest-of-u" / "pytest-0" / "nx-x.json"
        monkeypatch.setenv(tr.SESSION_ENV, f"tmpfs={run}")
        monkeypatch.setenv(tr.ORIG_ENV, str(tmp_path / "orig"))
        want = tr.kept_report_path(report)
        assert want != report
        assert ddl._summary_report_path(report) == want and guard._summary_report_path(report) == want
        assert "(report: {_summary_report_path(path)})" in inspect.getsource(ddl._Recorder.pytest_terminal_summary)
        assert 'f"storage-touch report: {_summary_report_path(path)}"' in inspect.getsource(guard)

    def test_a_failing_import_falls_back_to_the_given_path(self, tmp_path, monkeypatch) -> None:
        from tests import _ddl_counter as ddl

        monkeypatch.setitem(sys.modules, "tests._tmp_root", None)  # makes the import raise ImportError
        assert ddl._summary_report_path(tmp_path / "nx-r.json") == tmp_path / "nx-r.json"


# --- end to end: a real nested pytest --------------------------------------------

_SANDBOX_CONFTEST = '''\
from tests._tmp_root import TmpRootPlugin


def pytest_configure(config):
    config.pluginmanager.register(TmpRootPlugin(), "nexus-tmp-root")
'''

_SANDBOX_TEST = '''\
import json, os, subprocess, sys, tempfile, time
from pathlib import Path


def test_probe(tmp_path):
    session = os.environ["NX_TEST_TMP_SESSION"]
    out = {
        "session": session,
        "tmpdir_env": os.environ.get("TMPDIR"),
        "gettempdir": tempfile.gettempdir(),
        "tmp_path": str(tmp_path),
        "debug_root": os.environ.get("PYTEST_DEBUG_TEMPROOT"),
        "child_tempdir": subprocess.run(
            [sys.executable, "-c", "import tempfile;print(tempfile.gettempdir())"],
            capture_output=True, text=True, check=True).stdout.strip(),
        "worker": os.environ.get("PYTEST_XDIST_WORKER"),
    }
    if os.environ.get("PYTEST_XDIST_WORKER") is None:
        (tmp_path.parent / "nx-probe-report.json").write_text("{}")  # what the DDL counter does by default
    out_dir = Path(os.environ["PROBE_OUT"])
    (out_dir / f"{os.getpid()}.json").write_text(json.dumps(out))
    if os.environ.get("PROBE_HANG"):
        (out_dir / "hanging").write_text("x")
        time.sleep(120)
'''


_SANDBOX_FAILING_TEST = '''\
import os
from pathlib import Path


def test_fail(tmp_path):
    (tmp_path / "marker.txt").write_text("evidence-" + os.environ.get("PYTEST_XDIST_WORKER", "serial"))
    log_dir = Path(os.environ["TMPDIR"]) / "nexus-svc-log-e2e"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "engine.log").write_text("engine went away")
    assert False, "on purpose"
'''


def _sandbox(tmp_path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "conftest.py").write_text(_SANDBOX_CONFTEST)
    (proj / "test_probe.py").write_text(_SANDBOX_TEST)
    (proj / "pytest.ini").write_text("[pytest]\n")
    return proj


def _clean_env(tmp_path, **extra: str) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if not (k.startswith(("NX_TEST_TMP", "NX_TEST_PG", "PYTEST_")) or k in ("LAB_CPUS", "TMPDIR"))
    }
    orig = tmp_path / "orig-tmp"
    orig.mkdir(exist_ok=True)
    out = tmp_path / "probe-out"
    out.mkdir(exist_ok=True)
    env.update(
        TMPDIR=str(orig), PYTHONPATH=str(REPO_ROOT), PROBE_OUT=str(out),
        NX_NO_TELEMETRY="1", **extra,
    )
    return env


def _run_pytest(tmp_path, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = _clean_env(tmp_path, **env_extra)
    env["NX_TEST_T2_SUBSTRATE"] = "none"  # the child needs no engine (and must not read the build lease)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", "pytest.ini", *args],
        cwd=_sandbox_dir(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )


def _sandbox_dir(tmp_path) -> Path:
    return tmp_path / "proj" if (tmp_path / "proj").is_dir() else _sandbox(tmp_path)


def _probes(tmp_path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "probe-out").glob("*.json"))]


def _grant_env(tmp_path) -> dict[str, str]:
    shm = tmp_path / "shm"
    shm.mkdir(exist_ok=True)
    return {"LAB_CPUS": "4", "NX_TEST_TMP_SHM_DIR": str(shm)}


class TestEndToEnd:
    def test_serial_run_scratch_lives_in_the_private_dir_and_is_gone_after(self, tmp_path) -> None:
        r = _run_pytest(tmp_path, "-q", "test_probe.py", **_grant_env(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        (probe,) = _probes(tmp_path)
        run = Path(probe["session"].split("=", 1)[1])
        root = tmp_path / "shm" / _ROOT
        assert probe["session"].startswith("tmpfs=") and run.parent == root
        assert probe["tmpdir_env"] == str(run / "t") == probe["gettempdir"] == probe["child_tempdir"]
        assert probe["tmp_path"].startswith(str(run / "p") + os.sep)
        assert "pytest-of-" in probe["tmp_path"]
        assert not run.exists() and list(root.iterdir()) == [], "the basetemp is not retained on tmpfs"
        assert "FAILED" not in r.stdout
        assert f"basetemp and TMPDIR on tmpfs {run}" in r.stdout and "removed at exit" in r.stdout
        orig = tmp_path / "orig-tmp"
        assert [d.name for d in orig.iterdir()] == [f"nexus-test-reports-{sub._current_uid()}"], (
            "the only thing written to the original tempdir is the kept reports"
        )
        home = orig / f"nexus-test-reports-{sub._current_uid()}" / run.name
        assert (home / "nx-probe-report.json").is_file()
        assert not (home / tr.FAILED_SUBDIR).exists(), "a passing run keeps no failure evidence"

    def test_xdist_workers_inherit_the_one_decision(self, tmp_path) -> None:
        pytest.importorskip("xdist")
        (proj := _sandbox_dir(tmp_path)).joinpath("test_second.py").write_text(
            _SANDBOX_TEST.replace("test_probe", "test_probe_two"))
        r = _run_pytest(tmp_path, "-q", "-n", "2", "--dist", "loadfile", **_grant_env(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        probes = _probes(tmp_path)
        assert len(probes) == 2 and {p["worker"] for p in probes} == {"gw0", "gw1"}
        sessions = {p["session"] for p in probes}
        assert len(sessions) == 1
        run = Path(sessions.pop().split("=", 1)[1])
        for p in probes:
            assert p["gettempdir"] == p["child_tempdir"] == str(run / "t")
            assert p["tmp_path"].startswith(str(run / "p") + os.sep)
        assert not run.exists()
        assert proj.is_dir()

    def test_outside_a_grant_the_run_is_unchanged(self, tmp_path) -> None:
        r = _run_pytest(tmp_path, "-q", "test_probe.py", NX_TEST_TMP_SHM_DIR=str(tmp_path / "shm"))
        assert r.returncode == 0, r.stdout + r.stderr
        (probe,) = _probes(tmp_path)
        assert probe["session"].startswith("disk=not inside a lab-run grant")
        assert probe["tmpdir_env"] == str(tmp_path / "orig-tmp")
        assert probe["debug_root"] is None
        assert not (tmp_path / "shm").exists() or list((tmp_path / "shm").iterdir()) == []
        assert "on disk" in r.stdout and "not inside a lab-run grant" in r.stdout

    def test_default_override_in_a_grant_is_unchanged(self, tmp_path) -> None:
        r = _run_pytest(tmp_path, "-q", "test_probe.py", NX_TEST_TMP_ROOT="default", **_grant_env(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        (probe,) = _probes(tmp_path)
        assert probe["tmpdir_env"] == str(tmp_path / "orig-tmp") and probe["session"].startswith("disk=")

    def test_a_forced_directory_is_used_and_kept(self, tmp_path) -> None:
        keep = tmp_path / "keep"
        r = _run_pytest(tmp_path, "-q", "test_probe.py", NX_TEST_TMP_ROOT=str(keep))
        assert r.returncode == 0, r.stdout + r.stderr
        (probe,) = _probes(tmp_path)
        assert probe["tmpdir_env"] == str(keep / "t") and probe["tmp_path"].startswith(str(keep / "p"))
        assert (keep / "p").is_dir() and "forced by NX_TEST_TMP_ROOT" in r.stdout

    def test_a_killed_run_leaves_one_dir_and_the_next_run_removes_it(self, tmp_path) -> None:
        _sandbox(tmp_path)
        env = _clean_env(tmp_path, PROBE_HANG="1", **_grant_env(tmp_path))
        env["NX_TEST_T2_SUBSTRATE"] = "none"
        out = tmp_path / "probe-out"
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", "pytest.ini", "-q", "test_probe.py"],
            cwd=tmp_path / "proj", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 120
            while not (out / "hanging").exists():
                assert proc.poll() is None and time.monotonic() < deadline, "the probe never started"
                time.sleep(0.2)
        finally:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        root = tmp_path / "shm" / _ROOT
        (stale,) = list(root.iterdir())
        assert (stale / "owner.json").is_file()
        for f in out.iterdir():
            f.unlink()
        r = _run_pytest(tmp_path, "-q", "test_probe.py", **_grant_env(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert not stale.exists() and list(root.iterdir()) == []

    def _failed_home(self, tmp_path) -> tuple[Path, Path]:
        reports = tmp_path / "orig-tmp" / f"nexus-test-reports-{sub._current_uid()}"
        (home,) = list(reports.iterdir())
        return home, home / tr.FAILED_SUBDIR

    def test_a_failed_serial_run_keeps_the_failed_tests_dir_and_the_engine_log(self, tmp_path) -> None:
        _sandbox(tmp_path).joinpath("test_fail.py").write_text(_SANDBOX_FAILING_TEST)
        r = _run_pytest(tmp_path, "-q", "test_fail.py", **_grant_env(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        run_dirs = list((tmp_path / "shm" / _ROOT).iterdir())
        assert run_dirs == [], "the run dir is still removed"
        home, failed = self._failed_home(tmp_path)
        markers = list((failed / "tmp_path").glob("**/marker.txt"))
        assert [m.read_text() for m in markers] == ["evidence-serial"]
        assert (failed / "engine-logs" / "nexus-svc-log-e2e" / "engine.log").read_text() == "engine went away"
        assert "test_fail.py::test_fail" in (failed / tr.FAILURE_SUMMARY_FILE).read_text()
        assert "this run FAILED" in r.stdout and str(failed) in r.stdout

    def test_a_failed_xdist_run_keeps_the_workers_tmp_dir(self, tmp_path) -> None:
        pytest.importorskip("xdist")
        proj = _sandbox(tmp_path)
        proj.joinpath("test_fail.py").write_text(_SANDBOX_FAILING_TEST)
        proj.joinpath("test_second.py").write_text(_SANDBOX_TEST.replace("test_probe", "test_probe_two"))
        r = _run_pytest(tmp_path, "-q", "-n", "2", "--dist", "loadfile", **_grant_env(tmp_path))
        assert r.returncode == 1, r.stdout + r.stderr
        _home, failed = self._failed_home(tmp_path)
        (marker,) = list((failed / "tmp_path").glob("**/marker.txt"))
        assert marker.read_text().startswith("evidence-gw") and "popen-gw" in str(marker)
        assert (failed / "engine-logs" / "nexus-svc-log-e2e" / "engine.log").is_file()
