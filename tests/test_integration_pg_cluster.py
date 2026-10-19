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

from nexus.daemon.service_registry import pid_alive, process_command
from tests import _child_process as cp
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

    def test_the_conf_follows_the_pgdata_parent(self, tmp_path) -> None:
        pgdata = str(Path("/dev/shm/nexus-test-pg-1000") / "nexus_x_pg_abc")
        conf = sub.integration_cluster_conf(
            pgdata, env={}, mounts_text=_TMPFS_MOUNTS, shm_dir=_SHM,
        )
        assert "wal_level = minimal" in conf
        disk = sub.integration_cluster_conf(
            str(tmp_path / "nexus_x_pg_abc"), env={}, mounts_text=_TMPFS_MOUNTS,
        )
        assert "wal_level" not in disk and "fsync = off" in disk

    def test_the_boot_and_the_integration_conf_share_one_fsync_constant(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "f.write(THROWAWAY_FSYNC_CONF)" in src
        assert src.count("full_page_writes = off") == 1

    def test_child_process_exposes_the_conf_helper(self, tmp_path) -> None:
        pgdata = tmp_path / "nexus_x_pg_abc"
        assert "fsync = off" in cp.integration_pg_conf(str(pgdata))


class TestPgDataTempdirRouting:
    def test_it_creates_the_dir_under_the_chosen_root(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-1000"
        root.mkdir()
        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: str(root))
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        assert path.parent == root and path.name.startswith("nexus_foo_pg_")

    def test_no_chosen_root_means_the_tempdir(self, monkeypatch) -> None:
        import tempfile

        monkeypatch.setattr(sub, "pg_cluster_parent", lambda: None)
        path = Path(cp.pg_data_tempdir("nexus_foo_pg_"))
        try:
            assert path.parent == Path(tempfile.gettempdir())
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
    #: provenance scan covers for every real caller.
    _NOT_FIXTURES = {"_child_process.py", "test_integration_pg_cluster.py", "test_ddl_uncounted_clusters_lint.py"}

    def test_every_caller_appends_the_conf(self) -> None:
        counts: dict[str, tuple[int, int]] = {}
        for p in sorted(_TESTS.rglob("*.py")):
            if p.name in self._NOT_FIXTURES:
                continue
            code = _code_only(p.read_text())
            makes = code.count("pg_data_tempdir(")
            if makes:
                counts[str(p.relative_to(_TESTS))] = (makes, code.count("integration_pg_conf("))
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

    def test_a_pg_ctl_that_cannot_launch_still_removes_the_dir(self, tmp_path: Path) -> None:
        pgdata = tmp_path / "cluster"
        pgdata.mkdir()

        def missing(argv: list[str], **kw: object) -> None:
            raise FileNotFoundError(argv[0])

        with pytest.raises(FileNotFoundError):
            cp.stop_and_remove_cluster("pg_ctl", pgdata, run=missing)
        assert not pgdata.exists()
