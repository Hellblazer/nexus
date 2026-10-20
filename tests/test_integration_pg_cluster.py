# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration-marked test clusters share the substrate's root and conf (nexus-942h9).

About 23 ``tests/db`` and ``tests/benchmarks`` files boot their own Postgres cluster
through ``tests._child_process.pg_data_tempdir``. They had default ``fsync``,
``full_page_writes`` and a 1 GB ``max_wal_size`` and lived on the tempdir (ext4 NVMe
on chas), so every ``-m integration`` run wrote WAL to the disk nexus-me3gm moved the
substrate off. These tests pin the routing (same root chooser, same conf) and the
crash cleanup (a killed integration cluster in tmpfs is reaped by the sweep). All
decisions use injected mount tables and fake process trees, never machine state.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import structlog.testing

from nexus.daemon.service_registry import pid_alive, process_command
from tests import _child_process as cp
from tests import _ddl_cluster_sites as csites
from tests import _engine_substrate as sub
from tests._module_seam import setattr_in

_TMPFS_MOUNTS = (
    "proc /proc proc rw 0 0\n"
    "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
    "/dev/nvme0n1p2 / ext4 rw 0 0\n"
)
_SHM = Path("/dev/shm")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


@pytest.fixture
def owned_children():
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@pytest.fixture(autouse=True)
def _isolated_owner_state(monkeypatch):
    """Each test starts with no records this process "holds", and never runs the
    real once-per-process sweep (which would look at the box's real roots)."""
    monkeypatch.setattr(sub, "_owner_records", {})
    monkeypatch.setattr(sub, "_integration_swept", True)
    # pytest's tmp_path is deep on macOS; the real limit is pinned where it is tested.
    monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", 10_000)


class TestClusterConf:
    def test_a_tmpfs_cluster_gets_fsync_off_wal_cap_and_mmap_dsm(self) -> None:
        conf = sub.throwaway_cluster_conf(
            "/dev/shm/nexus-test-pg-1000", env={}, mounts_text=_TMPFS_MOUNTS,
        )
        lines = set(conf.splitlines())
        assert {"fsync = off", "synchronous_commit = off", "full_page_writes = off"} <= lines
        assert set(sub.WAL_CAP_CONF.splitlines()) <= lines
        assert "dynamic_shared_memory_type = mmap" in lines

    def test_a_disk_cluster_keeps_the_fsync_lines_but_not_the_wal_cap_or_mmap(self) -> None:
        conf = sub.throwaway_cluster_conf(
            "/home/nxtest/tmp", env={}, mounts_text=_TMPFS_MOUNTS,
        )
        lines = set(conf.splitlines())
        assert lines == {"fsync = off", "synchronous_commit = off", "full_page_writes = off"}

    def test_the_conf_follows_the_pgdata_parent(self) -> None:
        pgdata = str(Path("/dev/shm/nexus-test-pg-1000") / "nexus_x_pg_abc")
        conf = sub.integration_cluster_conf(
            pgdata, env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        )
        assert "wal_level = minimal" in conf
        # A fixed disk path, not tmp_path: inside a lab-run grant tmp_path is itself on
        # /dev/shm (nexus-kyujr), where the tmpfs conf is the right answer.
        disk = sub.integration_cluster_conf(
            "/home/nxtest/tmp/nexus_x_pg_abc", env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        )
        assert "wal_level" not in disk and "fsync = off" in disk

    def test_the_boot_and_the_integration_conf_share_one_fsync_constant(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "f.write(THROWAWAY_FSYNC_CONF)" in src
        assert src.count("full_page_writes = off") == 1

    def test_child_process_exposes_the_conf_helper(self, tmp_path) -> None:
        pgdata = tmp_path / "nexus_x_pg_abc"
        assert "fsync = off" in cp.integration_pg_conf(str(pgdata))


class TestReloadableConf:
    """After ``provision()`` has started the cluster, only settings a ``pg_ctl reload`` applies can be given to it
    (nexus-0ajjm): ``wal_level``, ``max_wal_senders`` and ``dynamic_shared_memory_type`` are restart-only."""

    _TRIO = {"fsync = off", "synchronous_commit = off", "full_page_writes = off"}

    def test_a_tmpfs_cluster_gets_the_trio_and_the_wal_sizes_but_no_restart_only_setting(self) -> None:
        lines = set(sub.reloadable_cluster_conf(
            "/dev/shm/nexus-test-pg-1000/nexus_x_pg_abc", env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        ).splitlines())
        assert lines == self._TRIO | {"max_wal_size = 64MB", "min_wal_size = 32MB", "wal_keep_size = 0"}

    def test_a_disk_cluster_gets_only_the_trio(self) -> None:
        lines = set(sub.reloadable_cluster_conf(
            "/home/nxtest/tmp/nexus_x_pg_abc", env={}, mounts_text=_TMPFS_MOUNTS,
        ).splitlines())
        assert lines == self._TRIO

    def test_the_restart_only_set_names_exactly_what_the_full_conf_gives_beyond_the_reloadable_part(self) -> None:
        full = sub.integration_cluster_conf(
            "/dev/shm/nexus-test-pg-1000/nexus_x_pg_abc", env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        ).splitlines()
        part = sub.reloadable_cluster_conf(
            "/dev/shm/nexus-test-pg-1000/nexus_x_pg_abc", env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        ).splitlines()
        assert {ln.split("=")[0].strip() for ln in full if ln not in part} == sub.RESTART_ONLY_SETTINGS

    def test_it_appends_to_the_nested_conf_and_reloads_that_pgdata(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "nexus_provision_pg_abc"
        nested = config_dir / "postgres"
        nested.mkdir(parents=True)
        (nested / "postgresql.conf").write_text("port = 5433\n")
        calls: list[list[str]] = []

        def fake_reload(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        conf = cp.apply_reloadable_throwaway_conf("pg_ctl", config_dir, run=fake_reload)

        text = (nested / "postgresql.conf").read_text()
        assert text.startswith("port = 5433\n") and text.endswith(conf)
        assert self._TRIO <= set(text.splitlines())
        assert not sub.RESTART_ONLY_SETTINGS & {ln.split("=")[0].strip() for ln in text.splitlines()}
        assert calls == [["pg_ctl", "-D", str(nested), "reload"]]

    def test_a_failed_reload_is_logged_not_raised(self, tmp_path: Path) -> None:
        nested = tmp_path / "cfg" / "postgres"
        nested.mkdir(parents=True)
        (nested / "postgresql.conf").write_text("")

        def failing(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 1, "", "pg_ctl: no server running")

        with structlog.testing.capture_logs() as logs:
            cp.apply_reloadable_throwaway_conf("pg_ctl", tmp_path / "cfg", run=failing)
        events = [e for e in logs if e["event"] == "apply_reloadable_throwaway_conf.reload_failed"]
        assert len(events) == 1 and events[0]["returncode"] == 1 and events[0]["log_level"] == "warning", logs

    def test_a_cluster_with_no_conf_file_is_a_loud_error(self, tmp_path: Path) -> None:
        (tmp_path / "cfg" / "postgres").mkdir(parents=True)
        with pytest.raises(FileNotFoundError):
            cp.apply_reloadable_throwaway_conf("pg_ctl", tmp_path / "cfg", run=lambda *a, **k: pytest.fail("reloaded"))


class TestPgDataTempdirRouting:
    def test_it_creates_the_dir_under_the_chosen_root(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        assert path.parent == root and path.name.startswith("nexus_foo_pg_")

    def test_no_chosen_root_means_the_tempdir(self, tmp_path, monkeypatch) -> None:
        # The ORIGINAL tempdir: inside a lab-run grant TMPDIR is the run's tmpfs and a
        # cluster whose root decided on disk must not land in RAM (nexus-kyujr). Outside a
        # grant the two are the same directory, so name a different one explicitly: a
        # revert to tempfile.gettempdir() then fails on a laptop too (nexus-g2f0p).
        orig = tmp_path / "orig"
        orig.mkdir()
        monkeypatch.setenv(sub._TMP_ORIG_ENV, str(orig))
        assert Path(tempfile.gettempdir()) != orig
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: None)
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        try:
            assert path.parent == orig
        finally:
            path.rmdir()

    def test_an_explicit_parent_dir_wins_and_is_not_recorded(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "chosen"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        mine = tmp_path / "mine"
        mine.mkdir()
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_", parent_dir=str(mine)))
        assert path.parent == mine
        assert list(root.iterdir()) == []

    def test_the_sweep_runs_once_per_process_before_the_first_cluster(
        self, tmp_path, monkeypatch,
    ) -> None:
        monkeypatch.setattr(sub, "_integration_swept", False)
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: None)
        calls: list[int] = []
        monkeypatch.setattr(
            sub, "sweep_integration_owner_records", lambda: calls.append(1),
        )
        made = [Path(cp.pg_data_tempdir("nexus_foo_pg_")) for _ in range(3)]
        try:
            assert calls == [1]
        finally:
            for m in made:
                m.rmdir()

    def test_a_failing_sweep_never_blocks_the_cluster(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_integration_swept", False)
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: None)

        def _boom():
            raise RuntimeError("sweep exploded")

        monkeypatch.setattr(sub, "sweep_integration_owner_records", _boom)
        with pytest.warns(UserWarning, match="sweep exploded"):
            path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        path.rmdir()


class TestOwnerRecord:
    def _record(self, root: Path) -> dict:
        return json.loads((root / f"{sub._OWNER_RECORD_PREFIX}{os.getpid()}.json").read_text())

    def test_a_cluster_in_a_chosen_root_is_recorded_with_its_owner(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        path = cp.pg_data_tempdir("nexus_foo_pg_")
        rec = self._record(root)
        assert rec["owner_pytest_pid"] == os.getpid()
        assert rec["owner_cmdline"] == process_command(os.getpid())
        assert rec["pgdatas"] == [path]
        assert rec["children"] == []

    def test_a_tempdir_cluster_is_not_recorded(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: None)
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        try:
            assert sub._owner_records == {}
        finally:
            path.rmdir()

    def test_a_child_is_recorded_with_its_cmdline(self, tmp_path, monkeypatch, owned_children) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        cp.pg_data_tempdir("nexus_foo_pg_")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        owned_children.append(child)
        time.sleep(0.1)
        sub.record_integration_child(child)
        rec = self._record(root)
        assert rec["children"] == [{"pid": child.pid, "cmdline": process_command(child.pid)}]

    def test_recording_a_child_is_a_no_op_when_no_cluster_is_recorded(self, owned_children) -> None:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        owned_children.append(child)
        sub.record_integration_child(child)  # must not raise or create anything
        assert sub._owner_records == {}

    def test_a_removed_pgdata_is_pruned_on_the_next_update(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        first = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        first.rmdir()
        second = cp.pg_data_tempdir("nexus_bar_pg_")
        assert self._record(root)["pgdatas"] == [second]

    def test_a_stale_record_left_by_an_earlier_process_of_the_same_pid_is_kept(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        orphan = root / "nexus_old_pg_zzz"
        orphan.mkdir()
        (root / f"{sub._OWNER_RECORD_PREFIX}{os.getpid()}.json").write_text(json.dumps({
            "owner_pytest_pid": os.getpid(), "owner_cmdline": "", "pgdatas": [str(orphan)],
            "children": [],
        }))
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        new = cp.pg_data_tempdir("nexus_foo_pg_")
        assert set(self._record(root)["pgdatas"]) == {str(orphan), new}

    def test_exit_drops_a_record_with_nothing_left_to_reap(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        sub._drop_finished_owner_records()
        assert (root / f"{sub._OWNER_RECORD_PREFIX}{os.getpid()}.json").exists(), (
            "a record whose pgdata still exists must stay for the sweep"
        )
        path.rmdir()
        sub._drop_finished_owner_records()
        assert not (root / f"{sub._OWNER_RECORD_PREFIX}{os.getpid()}.json").exists()


def _write_record(root: Path, *, owner: int, pgdatas=(), children=(), name=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rec = root / (name or f"{sub._OWNER_RECORD_PREFIX}{owner}.json")
    rec.write_text(json.dumps({
        "owner_pytest_pid": owner, "owner_cmdline": "",
        "pgdatas": [str(p) for p in pgdatas], "children": list(children),
    }))
    return rec


def _sleeper(owned_children, *extra: str) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *extra])
    owned_children.append(p)
    time.sleep(0.1)
    return p


class TestSweepReapsKilledIntegrationClusters:
    def test_dead_owner_postmaster_engine_and_dir_are_reaped_engine_first(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        pm = _sleeper(owned_children, str(pgdata))
        engine = _sleeper(owned_children)
        (pgdata / "postmaster.pid").write_text(f"{pm.pid}\n{pgdata}\n")
        rec = _write_record(
            root, owner=_dead_pid(), pgdatas=[pgdata],
            children=[{"pid": engine.pid, "cmdline": process_command(engine.pid)}],
        )
        calls: list[str] = []

        def _kill(label):
            def _fn(pid, *_a, **_k):
                calls.append(label)
                os.kill(pid, 9)
            return _fn

        monkeypatch.setattr(sub, "_kill_engine_leg", _kill("engine"))
        monkeypatch.setattr(sub, "_kill_postmaster_leg", _kill("postmaster"))

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert calls == ["engine", "postmaster"]
        assert result.reaped == [str(pgdata)]
        assert not pgdata.exists() and not rec.exists()
        pm.wait(timeout=5)
        engine.wait(timeout=5)

    def test_a_postmaster_still_exiting_when_its_stop_returns_is_waited_for_then_reaped(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        """A signal is asynchronous: on Linux a SIGKILLed pid reads ``R`` for ~100 us after ``kill()`` returns.
        The survivor guard waits for the exit (to its settle bound) instead of probing once, so a postmaster that
        is still on its way out is not counted as a survivor (nexus-0ajjm: a single probe kept the dir on chas)."""
        from nexus.daemon import service_registry as sr

        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        pm = _sleeper(owned_children, str(pgdata))
        (pgdata / "postmaster.pid").write_text(f"{pm.pid}\n{pgdata}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[pgdata])
        stopped: list[int] = []
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda pid, *_a, **_k: stopped.append(pid))
        probes: list[int] = []
        real_pid_running = sr.pid_running

        def _exits_on_the_third_probe(pid: int) -> bool:
            if pid != pm.pid:
                return real_pid_running(pid)
            probes.append(pid)
            return len(probes) < 3

        monkeypatch.setattr(sr, "pid_running", _exits_on_the_third_probe)

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert stopped == [pm.pid]
        assert len(probes) == 3, "the guard must keep probing until the pid stops running"
        assert result.reaped == [str(pgdata)] and result.errors == []
        assert not pgdata.exists() and not rec.exists()

    def test_a_live_owner_is_never_touched(self, tmp_path, monkeypatch, owned_children) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        pm = _sleeper(owned_children, str(pgdata))
        (pgdata / "postmaster.pid").write_text(f"{pm.pid}\n")
        rec = _write_record(root, owner=os.getpid(), pgdatas=[pgdata])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.live_untouched == [str(rec)]
        assert result.reaped == []
        assert pgdata.exists() and rec.exists() and pid_alive(pm.pid)

    def test_a_reused_pid_in_the_postmaster_slot_refuses_the_whole_record(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        stranger = _sleeper(owned_children)  # cmdline does not name the pgdata
        engine = _sleeper(owned_children)
        (pgdata / "postmaster.pid").write_text(f"{stranger.pid}\n")
        rec = _write_record(
            root, owner=_dead_pid(), pgdatas=[pgdata],
            children=[{"pid": engine.pid, "cmdline": process_command(engine.pid)}],
        )
        monkeypatch.setattr(sub, "_kill_engine_leg", lambda *a, **k: pytest.fail("killed"))
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        with pytest.warns(UserWarning, match="refusing"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.mismatch_refused == [str(rec)]
        assert pgdata.exists() and rec.exists()
        assert pid_alive(stranger.pid) and pid_alive(engine.pid)

    def test_a_reused_pid_in_the_child_slot_refuses_the_whole_record(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        stranger = _sleeper(owned_children)
        rec = _write_record(
            root, owner=_dead_pid(), pgdatas=[pgdata],
            children=[{"pid": stranger.pid, "cmdline": "java -jar the-engine.jar"}],
        )
        monkeypatch.setattr(sub, "_kill_engine_leg", lambda *a, **k: pytest.fail("killed"))
        with pytest.warns(UserWarning, match="refusing"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.mismatch_refused == [str(rec)]
        assert pgdata.exists() and pid_alive(stranger.pid)

    def test_a_dead_owner_with_everything_already_gone_just_drops_the_record(
        self, tmp_path,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        rec = _write_record(
            root, owner=_dead_pid(), pgdatas=[root / "gone"],
            children=[{"pid": _dead_pid(), "cmdline": "x"}],
        )
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert not rec.exists()
        assert result.reaped == [] and result.errors == []

    def test_a_pgdata_outside_the_root_is_never_removed(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        elsewhere = tmp_path / "precious"
        elsewhere.mkdir()
        (elsewhere / "keep").write_text("x")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[elsewhere])
        with pytest.warns(UserWarning, match="outside"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert (elsewhere / "keep").exists()
        assert result.errors == [str(rec)]

    def test_an_unreadable_record_is_reported_and_left(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        rec = root / f"{sub._OWNER_RECORD_PREFIX}1.json"
        rec.write_text("{not json")
        with pytest.warns(UserWarning, match="unreadable"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.errors == [str(rec)] and rec.exists()

    def test_the_record_glob_is_found_under_every_swept_root(self, tmp_path, monkeypatch) -> None:
        shm = tmp_path / "shm"
        cand = shm / f"nexus-test-pg-{sub._current_uid()}"  # the record's file is ours
        cand.mkdir(parents=True)
        rec = _write_record(cand, owner=_dead_pid())
        monkeypatch.setenv("NX_TEST_PG_SHM_DIR", str(shm))
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        setattr_in(monkeypatch, "tests._engine_substrate", "platform.system", lambda: "Linux")
        setattr_in(
            monkeypatch, "tests._engine_substrate", "tempfile.gettempdir",
            lambda: str(tmp_path / "emptytmp"),
        )
        sub.sweep_stale_substrate_clusters()
        assert not rec.exists()

    def test_the_records_only_sweep_leaves_substrate_clusters_and_their_warnings_alone(
        self, tmp_path, monkeypatch, recwarn,
    ) -> None:
        shm = tmp_path / "shm"
        cand = shm / f"nexus-test-pg-{sub._current_uid()}"  # the record's file is ours
        legacy = cand / "nexus_t2_substrate_pg_legacy01"
        legacy.mkdir(parents=True)
        rec = _write_record(cand, owner=_dead_pid())
        monkeypatch.setenv("NX_TEST_PG_SHM_DIR", str(shm))
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        setattr_in(monkeypatch, "tests._engine_substrate", "platform.system", lambda: "Linux")
        setattr_in(
            monkeypatch, "tests._engine_substrate", "tempfile.gettempdir",
            lambda: str(tmp_path / "emptytmp"),
        )
        result = sub.sweep_integration_owner_records()
        assert not rec.exists()
        assert legacy.exists() and result.legacy_reported == []
        assert not [w for w in recwarn if "legacy" in str(w.message)]


class TestSweepOwnerRecordTrust:
    """The record is a JSON file in a directory other uids may be able to write to
    (a shared NX_TEST_PG_ROOT); the sweep rmtree's what it names and signals the
    pids it names, so both the file and every named dir are checked first
    (nexus-942h9 review)."""

    @staticmethod
    def _no_kills(monkeypatch) -> None:
        monkeypatch.setattr(sub, "_kill_engine_leg", lambda *a, **k: pytest.fail("killed engine"))
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed pm"))

    def test_a_record_naming_the_roots_parent_deletes_nothing(
        self, tmp_path, monkeypatch,
    ) -> None:
        self._no_kills(monkeypatch)
        parent = tmp_path / "shared"
        root = parent / "nexus-test-pg-1000"
        root.mkdir(parents=True)
        (parent / "keep").write_text("x")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[str(root) + "/.."])
        with pytest.warns(UserWarning, match="outside"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert (parent / "keep").exists() and root.exists() and rec.exists()
        assert result.errors == [str(rec)] and result.reaped == []

    def test_a_dot_and_an_empty_name_are_refused_too(self, tmp_path, monkeypatch) -> None:
        self._no_kills(monkeypatch)
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        (tmp_path / "keep").write_text("x")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[str(root) + "/", str(root) + "/."])
        with pytest.warns(UserWarning):
            sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert (tmp_path / "keep").exists() and root.exists() and rec.exists()

    def test_a_symlinked_pgdata_is_never_followed_or_removed(
        self, tmp_path, monkeypatch,
    ) -> None:
        self._no_kills(monkeypatch)
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        precious = tmp_path / "precious"
        precious.mkdir()
        (precious / "keep").write_text("x")
        link = root / "nexus_inttest_pg_link"
        link.symlink_to(precious, target_is_directory=True)
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[link])
        with pytest.warns(UserWarning, match="symlink"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert (precious / "keep").exists() and link.is_symlink() and rec.exists()
        assert result.errors == [str(rec)] and result.reaped == []

    def test_a_record_owned_by_another_uid_is_ignored_and_signals_nothing(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        self._no_kills(monkeypatch)
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        engine = _sleeper(owned_children)
        rec = _write_record(
            root, owner=_dead_pid(), pgdatas=[pgdata],
            children=[{"pid": engine.pid, "cmdline": process_command(engine.pid)}],
        )
        # The service identity the module compares the file's owner with.
        monkeypatch.setattr(sub, "_current_uid", lambda: str(os.stat(rec).st_uid + 1))
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.untrusted == [str(rec)]
        assert result.reaped == [] and pgdata.exists() and rec.exists()
        assert pid_alive(engine.pid)

    def test_a_record_owned_by_us_passes_the_owner_check(self, tmp_path) -> None:
        """The control for the test above: without it, 'ignored' could be a sweep
        that ignores every record."""
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[pgdata])
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.untrusted == [] and result.reaped == [str(pgdata)]
        assert not rec.exists()

    def test_a_symlinked_record_file_is_ignored(self, tmp_path, monkeypatch) -> None:
        self._no_kills(monkeypatch)
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        real = _write_record(tmp_path / "elsewhere", owner=_dead_pid(), pgdatas=[pgdata], name="real.json")
        link = root / f"{sub._OWNER_RECORD_PREFIX}1.json"
        link.symlink_to(real)
        with pytest.warns(UserWarning, match="regular file"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.untrusted == [str(link)]
        assert pgdata.exists() and link.is_symlink() and real.exists()

    def test_a_record_that_vanished_between_glob_and_read_is_not_an_error(
        self, tmp_path,
    ) -> None:
        import warnings as _w

        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        result = sub.SweepResult()
        with _w.catch_warnings():
            _w.simplefilter("error")
            sub._sweep_owner_record(root / f"{sub._OWNER_RECORD_PREFIX}9.json", root, result)
        assert result.errors == [] and result.untrusted == [] and result.reaped == []


class TestSweepReadsANestedProvisionPgdata:
    """``pg_provision.provision(config_dir)`` puts the cluster at ``<config_dir>/postgres``. A test that
    provisions on a recorded dir (nexus-0ajjm) leaves ``postmaster.pid`` one level below the recorded path, so a
    sweep that reads only the top would ``rmtree`` the dir under a live orphan postmaster."""

    def _nested(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        root = tmp_path / "nexus-test-pg-1000"
        config_dir = root / "nexus_provision_pg_abc"
        nested = config_dir / "postgres"
        nested.mkdir(parents=True)
        (config_dir / "pg_credentials").write_text("PG_PORT=1\n")
        return root, config_dir, nested

    def test_a_dead_owners_live_nested_postmaster_is_stopped_by_its_nested_path_then_the_dir_reaped(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        pm = _sleeper(owned_children, str(nested))  # `postgres -D <nested>`: the path is in its own cmdline
        (nested / "postmaster.pid").write_text(f"{pm.pid}\n{nested}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        stopped: list[tuple[int, Path]] = []

        def _kill(pid, cluster_dir, **_k):
            stopped.append((pid, cluster_dir))
            os.kill(pid, 9)
            pm.wait(timeout=5)

        monkeypatch.setattr(sub, "_kill_postmaster_leg", _kill)

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert stopped == [(pm.pid, nested)], "pg_ctl must be pointed at the nested pgdata"
        assert result.reaped == [str(config_dir)]
        assert not config_dir.exists() and not rec.exists()

    def test_a_nested_postmaster_that_survives_its_stop_keeps_the_dir_and_the_record(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        pm = _sleeper(owned_children, str(nested))
        (nested / "postmaster.pid").write_text(f"{pm.pid}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: None)  # the stop did nothing
        monkeypatch.setattr(sub, "_SURVIVOR_SETTLE_S", 0.3)  # a real survivor is waited for; keep the wait short

        with pytest.warns(UserWarning, match="still running"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert pid_alive(pm.pid)
        assert (nested / "postmaster.pid").exists() and config_dir.is_dir(), "removed under a live postmaster"
        assert rec.exists() and result.reaped == [] and result.errors == [str(rec)]

    def test_a_top_level_postmaster_that_survives_its_stop_keeps_the_dir_and_the_record(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        """The survivor guard is not nested-only: a recorded dir with its own ``postmaster.pid`` and a live
        postmaster whose stop did nothing keeps the dir and the record (before nexus-0ajjm it was removed under it)."""
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        assert sub._owner_pgdata_candidates(pgdata) == [pgdata]
        pm = _sleeper(owned_children, str(pgdata))
        (pgdata / "postmaster.pid").write_text(f"{pm.pid}\n{pgdata}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[pgdata])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: None)  # the stop did nothing
        monkeypatch.setattr(sub, "_SURVIVOR_SETTLE_S", 0.3)  # a real survivor is waited for; keep the wait short

        with pytest.warns(UserWarning, match="still running"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert pid_alive(pm.pid)
        assert (pgdata / "postmaster.pid").exists() and pgdata.is_dir(), "removed under a live postmaster"
        assert rec.exists() and result.reaped == [] and result.errors == [str(rec)]

    def test_a_dead_nested_postmaster_is_reaped_without_a_kill(self, tmp_path, monkeypatch) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        (nested / "postmaster.pid").write_text(f"{_dead_pid()}\n{nested}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.reaped == [str(config_dir)] and result.errors == []
        assert not config_dir.exists() and not rec.exists()

    def test_a_nested_pid_that_names_a_stranger_refuses_the_record(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        stranger = _sleeper(owned_children)  # its cmdline does not name the nested pgdata
        (nested / "postmaster.pid").write_text(f"{stranger.pid}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        with pytest.warns(UserWarning, match="refusing"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.mismatch_refused == [str(rec)]
        assert config_dir.is_dir() and rec.exists() and pid_alive(stranger.pid)

    def test_the_nested_postmaster_is_identified_by_the_nested_path_not_the_recorded_dir(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        """A process that names only the recorded dir (not ``<dir>/postgres``) is not this cluster's postmaster."""
        root, config_dir, nested = self._nested(tmp_path)
        other = _sleeper(owned_children, str(config_dir))
        (nested / "postmaster.pid").write_text(f"{other.pid}\n")
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        with pytest.warns(UserWarning, match="refusing"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.mismatch_refused == [str(rec)] and pid_alive(other.pid)

    def test_a_live_owners_nested_cluster_is_never_touched(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        pm = _sleeper(owned_children, str(nested))
        (nested / "postmaster.pid").write_text(f"{pm.pid}\n")
        rec = _write_record(root, owner=os.getpid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.live_untouched == [str(rec)] and nested.is_dir() and pid_alive(pm.pid)

    def test_a_symlinked_nested_pgdata_is_refused_and_nothing_is_read_through_it(
        self, tmp_path, monkeypatch, owned_children,
    ) -> None:
        root, config_dir, nested = self._nested(tmp_path)
        nested.rmdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        pm = _sleeper(owned_children, str(nested))
        (elsewhere / "postmaster.pid").write_text(f"{pm.pid}\n")
        nested.symlink_to(elsewhere)
        rec = _write_record(root, owner=_dead_pid(), pgdatas=[config_dir])
        monkeypatch.setattr(sub, "_kill_postmaster_leg", lambda *a, **k: pytest.fail("killed"))

        with pytest.warns(UserWarning, match="symlink"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)

        assert result.errors == [str(rec)] and config_dir.is_dir() and rec.exists() and pid_alive(pm.pid)

    def test_a_top_level_cluster_still_reads_its_own_pid_file(self, tmp_path, monkeypatch, owned_children) -> None:
        """The control: a dir with no ``postgres/`` child is read at its top, as before."""
        root = tmp_path / "nexus-test-pg-1000"
        pgdata = root / "nexus_inttest_pg_abc"
        pgdata.mkdir(parents=True)
        assert sub._owner_pgdata_candidates(pgdata) == [pgdata]
        pm = _sleeper(owned_children, str(pgdata))
        (pgdata / "postmaster.pid").write_text(f"{pm.pid}\n")
        _write_record(root, owner=_dead_pid(), pgdatas=[pgdata])
        killed: list[Path] = []
        monkeypatch.setattr(
            sub, "_kill_postmaster_leg", lambda pid, d, **k: (killed.append(d), os.kill(pid, 9), pm.wait(timeout=5)),
        )
        sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert killed == [pgdata] and not pgdata.exists()


class TestSocketPathReserve:
    """The reserve covers the longest cluster prefix in use, and the actual path is
    checked when the dir is made (nexus-942h9 review)."""

    @pytest.fixture(autouse=True)
    def _real_limit(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", 107)

    def test_the_reserve_covers_the_longest_prefix_the_suite_uses(self) -> None:
        longest = max(
            len(m)
            for p in _TESTS.rglob("*.py")
            if p.name != "test_integration_pg_cluster.py"
            for m in re.findall(r'pg_data_tempdir\(\s*(?:prefix=)?"([^"]*)"', p.read_text())
        )
        assert longest >= 26, "scan is vacuous"
        needed = 1 + longest + 8 + len("/.s.PGSQL.65535")
        assert sub._SOCKET_SUFFIX_RESERVE >= needed

    def test_a_60_byte_root_is_refused_at_choice_time(self) -> None:
        root = "/" + "a" * 59
        assert len(root) == 60
        with pytest.raises(ValueError, match="too long"):
            sub.choose_pg_root(env={"NX_TEST_PG_ROOT": root})

    def test_a_root_at_the_new_limit_is_accepted(self) -> None:
        room = sub._SOCKET_PATH_MAX - sub._SOCKET_SUFFIX_RESERVE
        root = "/" + "a" * (room - 1)
        assert sub.choose_pg_root(env={"NX_TEST_PG_ROOT": root}) == Path(root)

    def test_the_actual_path_is_checked_when_the_dir_is_made(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", len(str(root)) + 40)
        with pytest.raises(ValueError, match="socket"):
            sub.integration_cluster_dir("nexus_recovery_inttest_pg_")
        assert list(root.iterdir()) == [], "a refused dir must not be left behind"
        ok = sub.integration_cluster_dir("p_")
        assert Path(ok).parent == root


class TestThrowawayClusterDefaultRoot:
    """throwaway_pg_cluster with no parent_dir takes the substrate's root, its conf
    and an owner record; an explicit parent_dir takes none of them."""

    def _fakes(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        pg_ctl = bin_dir / "pg_ctl"
        pg_ctl.write_text("#!/bin/sh\nexit 0\n")
        pg_ctl.chmod(0o755)
        made: list[str] = []

        def _initdb(bin_d, *, prefix, parent_dir=None, username=None):
            path = tempfile.mkdtemp(prefix=prefix, dir=parent_dir)
            made.append(path)
            return path

        monkeypatch.setattr(sub, "_pg_bin", lambda: bin_dir)
        monkeypatch.setattr(sub, "_initdb_cluster", _initdb)
        monkeypatch.setattr(
            sub, "pg_ctl_start",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
        )
        return made

    def test_no_parent_dir_uses_the_chosen_root_conf_and_record(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        made = self._fakes(tmp_path, monkeypatch)
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        monkeypatch.setattr(sub, "throwaway_cluster_conf", lambda parent: "# CHOSEN\n")
        with sub.throwaway_pg_cluster(lock_dir=tmp_path / "locks") as pgdata:
            assert pgdata.parent == root and str(pgdata) == made[0]
            assert "# CHOSEN" in (pgdata / "postgresql.conf").read_text()
            rec = json.loads((root / f"{sub._OWNER_RECORD_PREFIX}{os.getpid()}.json").read_text())
            assert rec["pgdatas"] == [str(pgdata)]

    def test_an_explicit_parent_dir_is_neither_recorded_nor_asked_for_a_root(
        self, tmp_path, monkeypatch,
    ) -> None:
        mine = tmp_path / "mine"
        mine.mkdir()
        self._fakes(tmp_path, monkeypatch)
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: pytest.fail("asked for a root"))
        with sub.throwaway_pg_cluster(lock_dir=tmp_path / "locks", parent_dir=str(mine)) as pgdata:
            assert pgdata.parent == mine
        assert sub._owner_records == {}


_TESTS = Path(__file__).resolve().parent


def _code_only(text: str) -> str:
    """*text* with ``#`` comments removed, so a call named in a comment is not a call."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


@pytest.mark.lint
class TestEveryIntegrationClusterGetsTheConf:
    """A file that makes a cluster with ``pg_data_tempdir`` and forgets
    ``integration_pg_conf`` quietly goes back to default fsync and a 1 GB WAL on
    whatever root it landed in. Census, with a non-vacuity floor: a scan that
    found no callers proves nothing. Comments are stripped, and a file needs at
    least as many conf calls as cluster-making calls (one conf call cannot cover
    two clusters)."""

    #: This file exercises the helper with fakes; _child_process defines it; the cluster-sites lint
    #: names it inside source strings it feeds its own scanner (nexus-szyun), which that lint's
    #: provenance scan covers for every real caller; ``_ddl_cluster_sites.py`` IS that scanner and names the helper in
    #: its docstrings and as the call it traces.
    _NOT_FIXTURES = {
        "_child_process.py", "_ddl_cluster_sites.py", "test_integration_pg_cluster.py",
        "test_ddl_uncounted_clusters_lint.py",
    }

    #: The five files whose cluster is made by ``pg_provision.provision(config_dir)`` on a ``pg_data_tempdir``
    #: dir (nexus-0ajjm); the one list is ``tests/_ddl_cluster_sites.PROVISION_DRIVEN``, relative to this directory
    #: here. provision() runs initdb and starts the cluster inside one call, so there is no moment to append the conf
    #: before the first start. After provision() each fixture appends the settings a reload applies
    #: (``apply_reloadable_throwaway_conf``: fsync, synchronous_commit, full_page_writes, and on tmpfs
    #: max_wal_size / min_wal_size / wal_keep_size). ``wal_level`` and ``dynamic_shared_memory_type`` need a restart and
    #: stay at provision's values: wal_level=replica, DSM posix. Each file must really call ``provision(``, and
    #: ``_APPLY_CALLS`` pins how many fixtures apply the reload settings. Skipped: the two scram tests that call
    #: provision() in their own body under a ``_run`` spy or a simulated crash (pwfile permission audit, crash after
    #: initdb); their clusters keep provision's defaults.
    _PROVISION_DRIVEN = {p.removeprefix("tests/") for p in csites.PROVISION_DRIVEN}
    _APPLY_CALLS = {
        "db/test_pg_provision.py": 4,
        "db/test_pg_provision_ca3_bundle.py": 1,
        "db/test_pg_bundle_relocation.py": 1,
        "db/test_pg_scram_auth.py": 1,
        "daemon/test_storage_service_daemon_pg_monitor_backfill.py": 1,
    }

    def test_every_provision_driven_file_applies_the_reloadable_conf(self) -> None:
        assert set(self._APPLY_CALLS) == self._PROVISION_DRIVEN
        for rel, want in self._APPLY_CALLS.items():
            code = _code_only((_TESTS / rel).read_text())
            assert code.count("apply_reloadable_throwaway_conf(bins.pg_ctl, config_dir)") == want, rel

    def test_every_caller_appends_the_conf(self) -> None:
        counts: dict[str, tuple[int, int]] = {}
        exempt: set[str] = set()
        for p in sorted(_TESTS.rglob("*.py")):
            if p.name in self._NOT_FIXTURES:
                continue
            code = _code_only(p.read_text())
            makes = code.count("pg_data_tempdir(")
            rel = str(p.relative_to(_TESTS))
            if makes and rel in self._PROVISION_DRIVEN:
                assert "provision(" in code, f"{rel}: exempt as provision()-driven but never calls provision("
                exempt.add(rel)
            elif makes:
                counts[rel] = (makes, code.count("integration_pg_conf("))
        assert exempt == self._PROVISION_DRIVEN, f"exemption list and tree disagree: {exempt ^ self._PROVISION_DRIVEN}"
        assert len(counts) >= 20, f"census saw only {len(counts)} callers: scan is vacuous"
        short = {f: c for f, c in counts.items() if c[1] < c[0]}
        assert short == {}, f"(clusters made, conf calls) per file: {short}"

    def test_the_comment_stripper_ignores_a_call_named_in_a_comment(self) -> None:
        assert "integration_pg_conf(" not in _code_only("x = 1  # integration_pg_conf(p)\n")
        assert "integration_pg_conf(" in _code_only("c = integration_pg_conf(p)\n")


class TestStopAndRemoveCluster:
    """The teardown of a ``pg_data_tempdir`` cluster removes the directory even when ``pg_ctl stop`` hangs
    (nexus-szyun): a ``TimeoutExpired`` that escaped before the ``rmtree`` left the dir on the run's shared
    tmpfs root, holding RAM until the crash sweep."""

    def test_it_stops_then_removes_the_dir(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        (pgdata / "base").mkdir(parents=True)
        calls: list[list[str]] = []

        def fake_stop(argv: list[str], **kw: object) -> None:
            assert pgdata.is_dir(), "the dir must still exist while pg_ctl stop runs"
            assert kw["timeout"] == 30 and kw["check"] is False
            calls.append(argv)

        cp.stop_and_remove_cluster("pg_ctl", pgdata, run=fake_stop)
        assert calls == [["pg_ctl", "-D", str(pgdata), "stop", "-m", "immediate"]]
        assert not pgdata.exists()

    def test_a_stop_that_times_out_still_removes_the_dir_and_still_raises(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        (pgdata / "base").mkdir(parents=True)

        def hung_stop(argv: list[str], **kw: object) -> None:
            raise subprocess.TimeoutExpired(argv, 30)

        with pytest.raises(subprocess.TimeoutExpired):
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=hung_stop)
        assert not pgdata.exists(), "a hung pg_ctl stop left the cluster dir on the shared tmpfs root"

    def test_a_stop_that_times_out_is_logged_before_the_removal(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        pgdata.mkdir()

        def hung_stop(argv: list[str], **kw: object) -> None:
            raise subprocess.TimeoutExpired(argv, 30)

        with structlog.testing.capture_logs() as logs, pytest.raises(subprocess.TimeoutExpired):
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=hung_stop)
        events = [e for e in logs if e["event"] == "stop_and_remove_cluster.stop_timed_out"]
        assert len(events) == 1 and events[0]["pgdata"] == str(pgdata) and events[0]["log_level"] == "warning", logs

    def test_a_stop_that_exits_nonzero_is_logged_and_the_dir_is_still_removed(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        pgdata.mkdir()

        def refused(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 4, "", "pg_ctl: directory is not a database cluster directory")

        with structlog.testing.capture_logs() as logs:
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=refused)
        events = [e for e in logs if e["event"] == "stop_and_remove_cluster.stop_failed"]
        assert len(events) == 1 and events[0]["returncode"] == 4 and events[0]["pgdata"] == str(pgdata), logs
        assert events[0]["log_level"] == "warning" and "not a database cluster" in events[0]["stderr"]
        assert not pgdata.exists()

    def test_a_clean_stop_logs_nothing(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        pgdata.mkdir()

        def ok(argv: list[str], **kw: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(argv, 0, "", "")

        with structlog.testing.capture_logs() as logs:
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=ok)
        assert [e for e in logs if str(e["event"]).startswith("stop_and_remove_cluster")] == []

    def test_a_provisioned_cluster_is_stopped_at_its_nested_pgdata_and_its_config_dir_removed(
        self, tmp_path: Path,
    ) -> None:
        """``provision(dir)`` nests the cluster at ``<dir>/postgres`` beside its credentials file: the stop
        targets the nested dir and the removal takes the whole ``<dir>``, or the credentials are left on the tmpfs
        root (nexus-0ajjm)."""
        config_dir = tmp_path / "cfg"
        (config_dir / "postgres" / "base").mkdir(parents=True)
        (config_dir / "pg_credentials").write_text("PG_PORT=1\n")
        calls: list[list[str]] = []

        def fake_stop(argv: list[str], **kw: object) -> None:
            assert (config_dir / "postgres").is_dir(), "the nested dir must still exist while pg_ctl stop runs"
            calls.append(argv)

        cp.stop_and_remove_cluster(
            "pg_ctl", config_dir / "postgres", remove_dir=config_dir, run=fake_stop,
        )
        assert calls == [["pg_ctl", "-D", str(config_dir / "postgres"), "stop", "-m", "immediate"]]
        assert not config_dir.exists()

    def test_a_remove_dir_is_removed_even_when_the_stop_times_out(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        (config_dir / "postgres").mkdir(parents=True)

        def hung_stop(argv: list[str], **kw: object) -> None:
            raise subprocess.TimeoutExpired(argv, 30)

        with pytest.raises(subprocess.TimeoutExpired):
            cp.stop_and_remove_cluster("pg_ctl", config_dir / "postgres", remove_dir=config_dir, run=hung_stop)
        assert not config_dir.exists()

    def test_a_pg_ctl_that_cannot_launch_still_removes_the_dir(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        pgdata.mkdir()

        def missing(argv: list[str], **kw: object) -> None:
            raise FileNotFoundError(argv[0])

        with pytest.raises(FileNotFoundError):
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=missing)
        assert not pgdata.exists()


class TestSweepRemovesAStaleOwnerRecordTmp:
    """``_flush_owner_record`` writes ``<record>.json.tmp`` then replaces it onto the record. A process killed
    between the two leaves the ``.json.tmp``, which ``_OWNER_RECORD_GLOB`` (``*.json``) never matches, so
    nothing swept it (nexus-qsqc6). The sweep removes one that is older than a safe age, only by its own
    naming pattern, only a regular file of ours, only directly under the root it sweeps."""

    _NOW = 1_800_000_000.0
    _PID = 4242

    @classmethod
    def _tmp(cls, root: Path, *, age_s: float, name: str | None = None) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        p = root / (name or f"{sub._OWNER_RECORD_PREFIX}{cls._PID}.json.tmp")
        p.write_text("{}")
        os.utime(p, (cls._NOW - age_s, cls._NOW - age_s))
        return p

    def _sweep(self, root: Path) -> sub.SweepResult:
        result = sub.SweepResult()
        sub._sweep_owner_records(root, result, now=self._NOW)
        return result

    def test_a_tmp_older_than_the_safe_age_is_removed_and_reported(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        tmp = self._tmp(root, age_s=sub._OWNER_TMP_MIN_AGE_S + 1)
        result = self._sweep(root)
        assert not tmp.exists()
        assert result.stale_tmp_removed == [str(tmp)]

    def test_a_fresh_tmp_is_left_alone(self, tmp_path) -> None:
        """A writer between its write and its replace owns it right now."""
        root = tmp_path / "nexus-test-pg-1000"
        tmp = self._tmp(root, age_s=1)
        result = self._sweep(root)
        assert tmp.exists() and result.stale_tmp_removed == []

    def test_the_boundary_is_strictly_older_than_the_safe_age(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        tmp = self._tmp(root, age_s=sub._OWNER_TMP_MIN_AGE_S)
        self._sweep(root)
        assert tmp.exists()

    @pytest.mark.parametrize("name", [
        "other.json.tmp",                                    # not our prefix
        f"{sub._OWNER_RECORD_PREFIX}abc.json.tmp",           # pid is not digits
        f"{sub._OWNER_RECORD_PREFIX}4242.json.tmp.bak",      # extra suffix
        f"{sub._OWNER_RECORD_PREFIX}4242.json",              # the record itself, old as it is
        f"x{sub._OWNER_RECORD_PREFIX}4242.json.tmp",         # prefix not at the start
    ])
    def test_only_the_records_own_tmp_name_is_matched(self, tmp_path, name) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        keep = self._tmp(root, age_s=10 * sub._OWNER_TMP_MIN_AGE_S, name=name)
        result = self._sweep(root)
        assert keep.exists() and result.stale_tmp_removed == []

    def test_a_tmp_below_the_root_is_not_reached(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        nested = self._tmp(root / "nexus_inttest_pg_abc", age_s=10 * sub._OWNER_TMP_MIN_AGE_S)
        sibling = self._tmp(tmp_path / "elsewhere", age_s=10 * sub._OWNER_TMP_MIN_AGE_S)
        root.mkdir(exist_ok=True)
        self._sweep(root)
        assert nested.exists() and sibling.exists()

    def test_a_symlink_with_the_tmp_name_is_neither_followed_nor_removed(self, tmp_path) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        link = root / f"{sub._OWNER_RECORD_PREFIX}{self._PID}.json.tmp"
        link.symlink_to(victim)
        os.utime(link, (self._NOW - 10 * sub._OWNER_TMP_MIN_AGE_S,) * 2, follow_symlinks=False)
        result = self._sweep(root)
        assert link.is_symlink() and victim.read_text() == "keep"
        assert result.stale_tmp_removed == []

    def test_the_real_sweep_removes_a_stale_tmp_under_its_root(self, tmp_path) -> None:
        """The public path, real clock: an hour-old tmp goes, a just-written one stays."""
        root = tmp_path / "nexus-test-pg-1000"
        old = self._tmp(root, age_s=0, name=f"{sub._OWNER_RECORD_PREFIX}1.json.tmp")
        os.utime(old, (time.time() - 3600,) * 2)
        new = self._tmp(root, age_s=0, name=f"{sub._OWNER_RECORD_PREFIX}2.json.tmp")
        os.utime(new, (time.time(),) * 2)
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert not old.exists() and new.exists()
        assert result.stale_tmp_removed == [str(old)]
