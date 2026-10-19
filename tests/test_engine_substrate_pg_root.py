# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where substrate clusters live, and how much WAL they may keep (nexus-me3gm).

The per-worker Postgres clusters wrote ~2.1 TB/day to chas's only NVMe. The
stopgap: clusters go on tmpfs (/dev/shm) on Linux when it is tmpfs with room,
and pg_wal is capped. These tests drive the root choice with injected mount
tables and free-space readings, so no outcome depends on the box running them.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests import _engine_substrate as sub

_TMPFS_MOUNTS = (
    "proc /proc proc rw 0 0\n"
    "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n"
    "/dev/nvme0n1p2 / ext4 rw 0 0\n"
)
_NO_TMPFS_MOUNTS = "proc /proc proc rw 0 0\n/dev/nvme0n1p2 / ext4 rw 0 0\n"


def _roomy(_p: Path) -> int:
    return 64 * 1024**3


def _tight(_p: Path) -> int:
    return 1 * 1024**3


def _writable(_p: Path) -> bool:
    return True


@pytest.fixture
def long_paths_ok(monkeypatch):
    """pytest's tmp_path on macOS is ~90 bytes, past the sun_path budget the
    override check enforces; these tests are about choosing, not path length."""
    monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", 10_000)


def _choose(**kw) -> Path | None:
    base = dict(
        env={}, system="Linux", mounts_text=_TMPFS_MOUNTS,
        free_bytes=_roomy, writable=_writable, uid=1000,
    )
    return sub.choose_pg_root(**{**base, **kw})


class TestRootChoice:
    def test_linux_tmpfs_with_room_gets_a_per_user_dir(self) -> None:
        assert _choose() == Path("/dev/shm/nexus-test-pg-1000")

    def test_uid_is_part_of_the_name(self) -> None:
        assert _choose(uid=4242) == Path("/dev/shm/nexus-test-pg-4242")

    def test_dev_shm_that_is_not_tmpfs_is_refused(self) -> None:
        assert _choose(mounts_text=_NO_TMPFS_MOUNTS) is None
        assert _choose(mounts_text="overlay /dev/shm overlay rw 0 0\n") is None

    def test_a_later_mount_shadows_an_earlier_one(self) -> None:
        shadowed = _TMPFS_MOUNTS + "overlay /dev/shm overlay rw 0 0\n"
        assert _choose(mounts_text=shadowed) is None

    def test_unwritable_tmpfs_is_refused(self) -> None:
        assert _choose(writable=lambda _p: False) is None

    def test_tmpfs_too_small_is_refused(self) -> None:
        assert _choose(free_bytes=_tight) is None

    def test_free_space_boundary_is_two_gib_plus_margin(self) -> None:
        need = sub._TMPFS_MIN_FREE_BYTES
        assert need >= 2 * 1024**3 + 512 * 1024**2
        assert _choose(free_bytes=lambda _p: need - 1) is None
        assert _choose(free_bytes=lambda _p: need) is not None

    def test_a_free_space_probe_that_raises_falls_back(self) -> None:
        def _boom(_p: Path) -> int:
            raise OSError("statvfs failed")

        assert _choose(free_bytes=_boom) is None

    def test_macos_stays_on_the_old_root(self) -> None:
        assert _choose(system="Darwin") is None

    def test_windows_stays_on_the_old_root(self) -> None:
        assert _choose(system="Windows") is None

    def test_override_wins_even_on_macos(self, tmp_path, long_paths_ok) -> None:
        root = tmp_path / "r"
        assert _choose(system="Darwin", env={"NX_TEST_PG_ROOT": str(root)}) == root

    def test_override_wins_over_a_roomy_tmpfs(self, tmp_path, long_paths_ok) -> None:
        root = tmp_path / "r"
        assert _choose(env={"NX_TEST_PG_ROOT": str(root)}) == root

    def test_default_forces_the_old_root(self) -> None:
        assert _choose(env={"NX_TEST_PG_ROOT": "default"}) is None

    def test_an_override_that_would_pass_sun_path_fails_loud(self) -> None:
        too_long = "/" + "x" * 80
        with pytest.raises(ValueError, match="sun_path"):
            _choose(env={"NX_TEST_PG_ROOT": too_long})

    def test_the_tmpfs_root_keeps_the_socket_path_within_sun_path(self) -> None:
        root = _choose(uid=2**31)
        assert root is not None
        socket = f"{root}/nexus_t2_substrate_pg_abcdefgh/.s.PGSQL.65535"
        assert len(socket) <= 107


class TestPgClusterParent:
    def test_no_choice_means_the_tempdir(self, monkeypatch) -> None:
        monkeypatch.setenv("NX_TEST_PG_ROOT", "default")
        assert sub.pg_cluster_parent() is None

    def test_an_override_dir_is_created_and_returned(
        self, tmp_path, monkeypatch, long_paths_ok,
    ) -> None:
        root = tmp_path / "made" / "here"
        monkeypatch.setenv("NX_TEST_PG_ROOT", str(root))
        assert sub.pg_cluster_parent() == str(root)
        assert root.is_dir()

    def test_an_override_that_cannot_be_created_fails_loud(
        self, tmp_path, monkeypatch, long_paths_ok,
    ) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        monkeypatch.setenv("NX_TEST_PG_ROOT", str(blocker / "sub"))
        with pytest.raises(OSError):
            sub.pg_cluster_parent()

    def test_a_tmpfs_dir_owned_by_someone_else_falls_back(
        self, tmp_path, monkeypatch,
    ) -> None:
        root = tmp_path / "nexus-test-pg-1"
        root.mkdir()
        owner = root.stat().st_uid
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "choose_pg_root", lambda **_k: root)
        monkeypatch.setattr(sub, "_current_uid", lambda: str(owner + 1))
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None

    def test_a_loose_dir_of_ours_is_tightened_to_0700(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-me"
        root.mkdir()
        root.chmod(0o755)
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "choose_pg_root", lambda **_k: root)
        assert sub.pg_cluster_parent() == str(root)
        assert root.stat().st_mode & 0o777 == 0o700


class TestSweepRoots:
    def test_default_is_the_tempdir_only(self, tmp_path) -> None:
        assert sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": "default"}, system="Linux", tempdir=tmp_path,
        ) == [tmp_path]

    def test_an_existing_tmpfs_root_is_swept_too(self, tmp_path) -> None:
        shm = tmp_path / "shm"
        cand = shm / "nexus-test-pg-1000"
        cand.mkdir(parents=True)
        roots = sub.substrate_pg_roots(
            env={}, system="Linux", uid=1000, tempdir=tmp_path / "tmp", shm_dir=shm,
        )
        assert roots == [cand, tmp_path / "tmp"]

    def test_an_absent_tmpfs_root_is_not_listed(self, tmp_path) -> None:
        roots = sub.substrate_pg_roots(
            env={}, system="Linux", uid=1000, tempdir=tmp_path, shm_dir=tmp_path / "shm",
        )
        assert roots == [tmp_path]

    def test_macos_never_lists_a_tmpfs_root(self, tmp_path) -> None:
        shm = tmp_path / "shm"
        (shm / "nexus-test-pg-1000").mkdir(parents=True)
        assert sub.substrate_pg_roots(
            env={}, system="Darwin", uid=1000, tempdir=tmp_path, shm_dir=shm,
        ) == [tmp_path]

    def test_override_and_tempdir_are_both_listed(self, tmp_path) -> None:
        override = tmp_path / "o"
        override.mkdir()
        assert sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": str(override)}, system="Linux", tempdir=tmp_path / "t",
        ) == [override, tmp_path / "t"]

    def test_duplicate_roots_collapse(self, tmp_path) -> None:
        assert sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": str(tmp_path)}, system="Linux", tempdir=tmp_path,
        ) == [tmp_path]


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


def _orphan(parent: Path, name: str) -> Path:
    cluster = parent / name
    cluster.mkdir(parents=True)
    (cluster / sub._SIDECAR_FILENAME).write_text(json.dumps({
        "owner_pytest_pid": _dead_pid(),
        "postmaster_pid": None,
        "engine_pid": None,
    }))
    return cluster


class TestSweepReapsAnOrphanInTheTmpfsRoot:
    def test_default_sweep_finds_and_removes_it(self, tmp_path, monkeypatch) -> None:
        """No tmp_root given (the _boot() path): a dead-owner cluster under the
        tmpfs root is reaped the same as one under the tempdir."""
        tmpfs_root = tmp_path / "shm" / "nexus-test-pg-1000"
        tmp_old = tmp_path / "tmp"
        tmp_old.mkdir()
        shm_orphan = _orphan(tmpfs_root, "nexus_t2_substrate_pg_orphan01")
        old_orphan = _orphan(tmp_old, "nexus_t2_substrate_pg_orphan02")
        monkeypatch.setattr(sub, "substrate_pg_roots", lambda: [tmpfs_root, tmp_old])

        result = sub.sweep_stale_substrate_clusters()

        assert sorted(result.reaped) == sorted([str(shm_orphan), str(old_orphan)])
        assert not shm_orphan.exists()
        assert not old_orphan.exists()

    def test_the_real_roots_function_feeds_the_sweep(self, tmp_path, monkeypatch) -> None:
        override = tmp_path / "root"
        orphan = _orphan(override, "nexus_t2_substrate_pg_orphan03")
        (tmp_path / "empty").mkdir()
        monkeypatch.setenv("NX_TEST_PG_ROOT", str(override))
        monkeypatch.setattr(sub.tempfile, "tempdir", str(tmp_path / "empty"))

        result = sub.sweep_stale_substrate_clusters()

        assert result.reaped == [str(orphan)]
        assert not orphan.exists()

    def test_an_explicit_tmp_root_sweeps_only_that_root(self, tmp_path, monkeypatch) -> None:
        other = tmp_path / "other"
        elsewhere = _orphan(other, "nexus_t2_substrate_pg_orphan04")
        target = tmp_path / "target"
        target.mkdir()
        monkeypatch.setattr(sub, "substrate_pg_roots", lambda: [other])

        result = sub.sweep_stale_substrate_clusters(tmp_root=target)

        assert result.reaped == []
        assert elsewhere.exists()


class TestWalCaps:
    def test_conf_carries_every_cap(self) -> None:
        assert set(sub.WAL_CAP_CONF.splitlines()) == {
            "wal_level = minimal",
            "max_wal_senders = 0",
            "max_wal_size = 64MB",
            "min_wal_size = 32MB",
            "wal_keep_size = 0",
        }

    def test_boot_appends_the_caps_after_the_fsync_lines(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "f.write(WAL_CAP_CONF)" in src
        assert src.index("full_page_writes = off") < src.index("f.write(WAL_CAP_CONF)")

    def test_boot_passes_the_chosen_parent_to_initdb(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "cluster_parent = pg_cluster_parent()" in src
        assert "parent_dir=cluster_parent" in src


class TestDynamicSharedMemory:
    """posix DSM on a SIGKILLed postmaster leaks /dev/shm/PostgreSQL.<n>; on a
    tmpfs cluster ``mmap`` keeps it inside the cluster dir (nexus-me3gm)."""

    MMAP = "dynamic_shared_memory_type = mmap\n"

    def test_mmap_for_a_cluster_on_the_tmpfs_root(self) -> None:
        assert sub.dsm_conf(
            "/dev/shm/nexus-test-pg-1000", mounts_text=_TMPFS_MOUNTS,
        ) == self.MMAP

    def test_nothing_for_the_default_root(self) -> None:
        assert sub.dsm_conf(None, mounts_text=_TMPFS_MOUNTS) == ""

    def test_nothing_for_a_disk_root_even_when_dev_shm_is_tmpfs(self) -> None:
        # mmap on disk is real writes: the exact thing this bead removes.
        assert sub.dsm_conf("/home/nxtest/pgroot", mounts_text=_TMPFS_MOUNTS) == ""

    def test_nothing_when_dev_shm_is_not_tmpfs(self) -> None:
        assert sub.dsm_conf(
            "/dev/shm/nexus-test-pg-1000", mounts_text=_NO_TMPFS_MOUNTS,
        ) == ""

    def test_nothing_when_there_is_no_mount_table(self, monkeypatch) -> None:
        # macOS has no /proc/mounts.
        monkeypatch.setattr(sub, "_read_mounts", lambda: None)
        assert sub.dsm_conf("/dev/shm/nexus-test-pg-1000") == ""

    def test_a_sibling_prefix_of_dev_shm_is_not_under_it(self) -> None:
        assert sub.dsm_conf("/dev/shmx/y", mounts_text=_TMPFS_MOUNTS) == ""

    def test_boot_appends_the_line_after_the_wal_caps(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "f.write(dsm_conf(cluster_parent))" in src
        assert src.index("f.write(WAL_CAP_CONF)") < src.index("f.write(dsm_conf(cluster_parent))")
