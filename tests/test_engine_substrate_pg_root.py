# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where substrate clusters live, and how much WAL they may keep (nexus-me3gm).

The per-worker Postgres clusters wrote ~2.1 TB/day to chas's only NVMe. The
stopgap: clusters go on tmpfs (/dev/shm) on Linux when it is tmpfs with room,
and pg_wal is capped. These tests drive the root choice with injected mount
tables and free-space readings, so no outcome depends on the box running them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


@pytest.fixture(autouse=True)
def _fresh_session_decision(monkeypatch):
    """The conftest controller persists the run's root decision in the env and
    counts cluster starts; these tests drive the decision themselves, so they
    start with neither and leave neither behind (nexus-vo9qn)."""
    monkeypatch.setenv(sub._PG_ROOT_SESSION_ENV, "")
    monkeypatch.delenv(sub._PG_ROOT_LOG_ENV, raising=False)
    monkeypatch.setattr(sub, "_LOCAL_ROWS", [])


def _choose(**kw) -> Path | None:
    base = dict(
        env={}, system="Linux", mounts_text=_TMPFS_MOUNTS,
        free_bytes=_roomy, writable=_writable, uid=1000,
        cgroup_headroom=lambda: None,
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
        monkeypatch.setattr(sub, "decide_pg_root", lambda **_k: (root, None))
        monkeypatch.setattr(sub, "_current_uid", lambda: str(owner + 1))
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None

    def test_a_loose_dir_of_ours_is_tightened_to_0700(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "nexus-test-pg-me"
        root.mkdir()
        root.chmod(0o755)
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "decide_pg_root", lambda **_k: (root, None))
        assert sub.pg_cluster_parent() == str(root)
        assert root.stat().st_mode & 0o777 == 0o700


class TestSweepRoots:
    def test_default_is_the_tempdir_when_no_tmpfs_root_exists(self, tmp_path) -> None:
        assert sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": "default"}, system="Linux", tempdir=tmp_path,
            shm_dir=tmp_path / "shm",
        ) == [tmp_path]

    def test_an_existing_tmpfs_root_is_swept_too(self, tmp_path) -> None:
        shm = tmp_path / "shm"
        cand = shm / "nexus-test-pg-1000"
        cand.mkdir(parents=True)
        roots = sub.substrate_pg_roots(
            env={}, system="Linux", uid=1000, tempdir=tmp_path / "tmp", shm_dir=shm,
        )
        assert roots == [cand, tmp_path / "tmp"]

    def test_default_never_hides_an_existing_tmpfs_root_from_the_sweep(
        self, tmp_path,
    ) -> None:
        """=default changes where NEW clusters go, never what the sweep looks at:
        a cluster stranded in tmpfs is RAM until reaped."""
        shm = tmp_path / "shm"
        cand = shm / "nexus-test-pg-1000"
        cand.mkdir(parents=True)
        roots = sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": "default"}, system="Linux", uid=1000,
            tempdir=tmp_path / "tmp", shm_dir=shm,
        )
        assert roots == [cand, tmp_path / "tmp"]

    def test_an_override_never_hides_an_existing_tmpfs_root_either(
        self, tmp_path,
    ) -> None:
        shm = tmp_path / "shm"
        cand = shm / "nexus-test-pg-1000"
        cand.mkdir(parents=True)
        override = tmp_path / "o"
        override.mkdir()
        roots = sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": str(override)}, system="Linux", uid=1000,
            tempdir=tmp_path / "tmp", shm_dir=shm,
        )
        assert roots == [override, cand, tmp_path / "tmp"]

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
            shm_dir=tmp_path / "shm",
        ) == [override, tmp_path / "t"]

    def test_duplicate_roots_collapse(self, tmp_path) -> None:
        assert sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": str(tmp_path)}, system="Linux", tempdir=tmp_path,
            shm_dir=tmp_path / "shm",
        ) == [tmp_path]

    def test_the_shm_dir_seam_env_redirects_the_default_lookup(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The sweep script's tests need a hermetic /dev/shm stand-in."""
        shm = tmp_path / "shm"
        cand = shm / f"nexus-test-pg-{sub._current_uid()}"
        cand.mkdir(parents=True)
        monkeypatch.setenv("NX_TEST_PG_SHM_DIR", str(shm))
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        roots = sub.substrate_pg_roots(system="Linux", tempdir=tmp_path / "tmp")
        assert roots == [cand, tmp_path / "tmp"]


class TestRelativeOverride:
    def test_a_relative_override_is_made_absolute(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", 10_000)
        got = _choose(env={"NX_TEST_PG_ROOT": "rel/root"})
        assert got is not None and got.is_absolute()
        assert got == Path(os.path.abspath(tmp_path / "rel" / "root"))

    def test_the_length_check_sees_the_absolute_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        absolute = os.path.abspath(tmp_path / "r")
        edge = len(absolute) + sub._SOCKET_SUFFIX_RESERVE
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", edge)
        assert _choose(env={"NX_TEST_PG_ROOT": "r"}) == Path(absolute)
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", edge - 1)
        with pytest.raises(ValueError, match="sun_path"):
            _choose(env={"NX_TEST_PG_ROOT": "r"})

    def test_the_sweep_roots_are_absolute_too(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        roots = sub.substrate_pg_roots(
            env={"NX_TEST_PG_ROOT": "rel"}, system="Linux", tempdir=tmp_path / "t",
            shm_dir=tmp_path / "shm",
        )
        assert roots[0] == Path(os.path.abspath(tmp_path / "rel"))


class TestTmpfsRootIsAPrivateRealDirectory:
    def test_a_symlink_in_place_of_the_root_is_refused(
        self, tmp_path, monkeypatch,
    ) -> None:
        target = tmp_path / "elsewhere"
        target.mkdir()
        link = tmp_path / "nexus-test-pg-me"
        link.symlink_to(target)
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "decide_pg_root", lambda **_k: (link, None))
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None

    def test_a_plain_file_in_place_of_the_root_is_refused(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "nexus-test-pg-me"
        f.write_text("x")
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "decide_pg_root", lambda **_k: (f, None))
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None


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
        assert "f.write(wal_cap_conf(cluster_parent))" in src
        assert src.index("full_page_writes = off") < src.index("f.write(wal_cap_conf(cluster_parent))")

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
        assert src.index("f.write(wal_cap_conf(cluster_parent))") < src.index("f.write(dsm_conf(cluster_parent))")


def test_the_split_shard_probe_keeps_its_clusters_in_its_private_tmpdir() -> None:
    """The probe globs its own TMPDIR for clusters; on Linux without
    NX_TEST_PG_ROOT=default they would land in /dev/shm, unseen and stranded."""
    probe = Path(__file__).with_name("test_mode_declarations_are_explicit.py").read_text()
    assert '"NX_TEST_PG_ROOT": "default"' in probe


class TestWalCapOnlyWhereItHelps:
    """On disk the 64 MB cap only multiplies checkpoints; macOS stays unchanged."""

    def test_caps_for_a_cluster_on_the_tmpfs_root(self) -> None:
        assert sub.wal_cap_conf(
            "/dev/shm/nexus-test-pg-1000", env={}, mounts_text=_TMPFS_MOUNTS,
        ) == sub.WAL_CAP_CONF

    def test_caps_for_an_explicit_root(self) -> None:
        assert sub.wal_cap_conf(
            "/data/pgroot", env={"NX_TEST_PG_ROOT": "/data/pgroot"},
            mounts_text=_NO_TMPFS_MOUNTS,
        ) == sub.WAL_CAP_CONF

    def test_nothing_for_the_tempdir_fallback(self) -> None:
        assert sub.wal_cap_conf(None, env={}, mounts_text=_TMPFS_MOUNTS) == ""

    def test_nothing_for_a_disk_root_that_is_not_explicit(self) -> None:
        assert sub.wal_cap_conf(
            "/home/nxtest/pgroot", env={}, mounts_text=_TMPFS_MOUNTS,
        ) == ""

    def test_default_override_is_not_explicit(self) -> None:
        assert sub.wal_cap_conf(
            "/home/nxtest/pgroot", env={"NX_TEST_PG_ROOT": "default"},
            mounts_text=_TMPFS_MOUNTS,
        ) == ""

    def test_nothing_without_a_mount_table(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_read_mounts", lambda: None)
        assert sub.wal_cap_conf("/dev/shm/nexus-test-pg-1000", env={}) == ""

    def test_boot_writes_the_helper_not_the_constant(self) -> None:
        src = Path(sub.__file__).read_text()
        assert "f.write(wal_cap_conf(cluster_parent))" in src
        assert "f.write(WAL_CAP_CONF)" not in src


class TestFreeSpaceGateSeesTheCgroup:
    """/dev/shm free is ~60 GB on chas and can never fail; the resource that runs
    out is the cgroup's memory, which tmpfs pages are charged to."""

    def test_cgroup_headroom_below_the_threshold_refuses(self) -> None:
        assert _choose(cgroup_headroom=lambda: 1 * 1024**3) is None

    def test_cgroup_headroom_above_the_threshold_allows(self) -> None:
        assert _choose(cgroup_headroom=lambda: 64 * 1024**3) is not None

    def test_the_minimum_of_the_two_decides(self) -> None:
        need = sub._TMPFS_MIN_FREE_BYTES
        assert _choose(free_bytes=lambda _p: need, cgroup_headroom=lambda: need - 1) is None
        assert _choose(free_bytes=lambda _p: need - 1, cgroup_headroom=lambda: need) is None
        assert _choose(free_bytes=lambda _p: need, cgroup_headroom=lambda: need) is not None

    def test_no_cgroup_limit_leaves_only_the_tmpfs_gate(self) -> None:
        assert _choose(cgroup_headroom=lambda: None) is not None


class TestCgroupHeadroomReader:
    def _tree(self, tmp_path, levels: dict[str, tuple[str, int]]) -> Path:
        root = tmp_path / "cg"
        for rel, (mx, cur) in levels.items():
            d = root / rel if rel else root
            d.mkdir(parents=True, exist_ok=True)
            (d / "memory.max").write_text(f"{mx}\n")
            (d / "memory.current").write_text(f"{cur}\n")
        return root

    def test_own_cgroup_limit_minus_usage(self, tmp_path) -> None:
        root = self._tree(tmp_path, {"user.slice/s": ("1000", 400)})
        assert sub._cgroup_headroom(
            proc_cgroup="0::/user.slice/s\n", cgroup_root=root,
        ) == 600

    def test_an_ancestor_limit_binds_when_tighter(self, tmp_path) -> None:
        root = self._tree(tmp_path, {
            "": ("max", 0), "a": ("500", 450), "a/b": ("max", 10),
        })
        assert sub._cgroup_headroom(proc_cgroup="0::/a/b\n", cgroup_root=root) == 50

    def test_unlimited_everywhere_is_none(self, tmp_path) -> None:
        root = self._tree(tmp_path, {"a": ("max", 5)})
        assert sub._cgroup_headroom(proc_cgroup="0::/a\n", cgroup_root=root) is None

    def test_cgroup_v1_or_unreadable_is_none(self, tmp_path) -> None:
        assert sub._cgroup_headroom(
            proc_cgroup="12:memory:/x\n", cgroup_root=tmp_path,
        ) is None
        assert sub._cgroup_headroom(
            proc_cgroup="0::/missing\n", cgroup_root=tmp_path,
        ) is None


class TestLinuxFallbackIsLoud:
    def _warns(self, **kw) -> str:
        with pytest.warns(UserWarning, match="tmpfs") as rec:
            assert _choose(**kw) is None
        return str(rec[0].message)

    def test_not_tmpfs_names_the_reason(self) -> None:
        assert "not tmpfs" in self._warns(mounts_text=_NO_TMPFS_MOUNTS)

    def test_not_writable_names_the_reason(self) -> None:
        assert "not writable" in self._warns(writable=lambda _p: False)

    def test_low_free_names_the_reason_and_the_numbers(self) -> None:
        msg = self._warns(free_bytes=_tight)
        assert "free" in msg and "1024" in msg

    def test_low_cgroup_headroom_names_the_cgroup(self) -> None:
        assert "cgroup" in self._warns(cgroup_headroom=lambda: 1)

    def test_socket_path_too_long_names_the_reason(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_SOCKET_PATH_MAX", 20)
        assert "socket" in self._warns()

    def test_macos_fallback_is_silent(self, recwarn) -> None:
        assert _choose(system="Darwin") is None
        assert not [w for w in recwarn if "tmpfs" in str(w.message)]

    def test_default_override_is_silent(self, recwarn) -> None:
        assert _choose(env={"NX_TEST_PG_ROOT": "default"}) is None
        assert not [w for w in recwarn if "tmpfs" in str(w.message)]

    def test_success_is_silent(self, recwarn) -> None:
        assert _choose() is not None
        assert not [w for w in recwarn if "tmpfs" in str(w.message)]


class TestSidecarlessTmpfsClusterIsReaped:
    """Killed during initdb: no sidecar. On disk that is inert debris; in tmpfs it
    pins RAM, so the sweep reaps it once it is clearly abandoned."""

    @pytest.fixture
    def shm(self, tmp_path, monkeypatch):
        shm = tmp_path / "shm"
        monkeypatch.setenv("NX_TEST_PG_SHM_DIR", str(shm))
        return shm

    def _bare(self, parent: Path, name: str, age_s: float) -> Path:
        d = parent / name
        d.mkdir(parents=True)
        (d / "PG_VERSION").write_text("17\n")
        t = time.time() - age_s
        os.utime(d, (t, t))
        return d

    def test_old_bare_dir_with_no_postmaster_pid_is_reaped(self, shm) -> None:
        root = shm / "nexus-test-pg-1000"
        d = self._bare(root, "nexus_t2_substrate_pg_bare0001", 3600)
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.reaped == [str(d)]
        assert not d.exists()

    def test_a_young_bare_dir_is_left_alone(self, shm) -> None:
        root = shm / "nexus-test-pg-1000"
        d = self._bare(root, "nexus_t2_substrate_pg_bare0002", 30)
        result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.reaped == []
        assert d.exists()

    def test_a_bare_dir_with_a_postmaster_pid_is_reported_not_reaped(self, shm) -> None:
        root = shm / "nexus-test-pg-1000"
        d = self._bare(root, "nexus_t2_substrate_pg_bare0003", 3600)
        (d / "postmaster.pid").write_text("1\n")
        os.utime(d, (time.time() - 3600,) * 2)
        with pytest.warns(UserWarning, match="legacy"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=root)
        assert result.reaped == []
        assert result.legacy_reported == [str(d)]
        assert d.exists()

    def test_the_same_bare_dir_on_disk_stays_inert(self, shm, tmp_path) -> None:
        disk = tmp_path / "disk"
        d = self._bare(disk, "nexus_t2_substrate_pg_bare0004", 3600)
        with pytest.warns(UserWarning, match="legacy"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=disk)
        assert result.reaped == []
        assert d.exists()


class TestRootIsDecidedOncePerRun:
    """nexus-vo9qn. choose_pg_root used to run per worker at every cluster start;
    once the grant's memory filled, later clusters went to ext4 (lab-l, -n auto:
    3.9 GB written in 4 minutes). The decision is now taken once and inherited."""

    def _decide(self, env: dict, **kw):
        base = dict(
            system="Linux", mounts_text=_TMPFS_MOUNTS, free_bytes=_roomy,
            writable=_writable, uid=1000, cgroup_headroom=lambda: None,
        )
        return sub.session_pg_root(env=env, **{**base, **kw})

    def test_a_later_squeeze_does_not_move_the_run_to_disk(self) -> None:
        env: dict[str, str] = {}
        first = self._decide(env)
        squeezed = self._decide(env, cgroup_headroom=lambda: 1 * 1024**3)
        assert first[0] == Path("/dev/shm/nexus-test-pg-1000")
        assert squeezed == first

    def test_a_refusal_is_just_as_sticky(self) -> None:
        env: dict[str, str] = {}
        with pytest.warns(UserWarning, match="tmpfs"):
            first = self._decide(env, free_bytes=_tight)
        again = self._decide(env)  # roomy now: the run stays where it started
        assert first[0] is None and "free" in (first[1] or "")
        assert again == first

    def test_the_decision_travels_in_the_env_workers_inherit(self) -> None:
        env: dict[str, str] = {}
        self._decide(env)
        worker_env = dict(env)  # what an xdist worker process starts with
        assert self._decide(worker_env, free_bytes=_tight)[0] == Path(
            "/dev/shm/nexus-test-pg-1000"
        )

    def test_an_explicit_root_is_a_standing_instruction_not_memoised(
        self, tmp_path, long_paths_ok,
    ) -> None:
        env = {"NX_TEST_PG_ROOT": str(tmp_path / "a")}
        assert self._decide(env)[0] == tmp_path / "a"
        env["NX_TEST_PG_ROOT"] = str(tmp_path / "b")
        assert self._decide(env)[0] == tmp_path / "b"
        assert self._decide(env)[2] is True  # explicit
        assert sub._PG_ROOT_SESSION_ENV not in env

    def test_garbage_in_the_env_is_decided_afresh(self) -> None:
        env = {sub._PG_ROOT_SESSION_ENV: "wat"}
        assert self._decide(env)[0] == Path("/dev/shm/nexus-test-pg-1000")

    def test_the_controller_decides_before_workers_exist(self, monkeypatch) -> None:
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        calls: list[int] = []

        def _decide(**_k):
            calls.append(1)
            return Path("/dev/shm/nexus-test-pg-9"), None

        monkeypatch.setattr(sub, "decide_pg_root", _decide)
        monkeypatch.setattr(sub, "_OWNED_LOGS", set())
        sub.begin_pg_root_session()
        try:
            assert os.environ[sub._PG_ROOT_SESSION_ENV] == "tmpfs=/dev/shm/nexus-test-pg-9"
            assert Path(os.environ[sub._PG_ROOT_LOG_ENV]).is_file()
            sub.session_pg_root()  # a "worker" reading the same env
            assert calls == [1]
        finally:
            sub.pg_root_run_summary(finish=True)

    def test_conftest_takes_the_decision_for_an_xdist_controller_only(
        self, monkeypatch,
    ) -> None:
        from tests import conftest as ct

        started: list[int] = []
        monkeypatch.setattr(sub, "begin_pg_root_session", lambda: started.append(1))

        class Opt:
            numprocesses = 4

        class Cfg:
            option = Opt()

        ct._begin_pg_root_session(Cfg())
        assert started == [1]

        class Worker(Cfg):
            workerinput = {"workerid": "gw0"}

        ct._begin_pg_root_session(Worker())
        Opt.numprocesses = 0  # serial: decided lazily at its first cluster
        ct._begin_pg_root_session(Cfg())
        assert started == [1]

    def test_pytest_configure_calls_the_controller_hook(self) -> None:
        src = Path(__file__).with_name("conftest.py").read_text()
        body = src[src.index("def pytest_configure(config):"):]
        body = body[: body.index("\ndef ", 10)]
        assert "_begin_pg_root_session(config)" in body


class TestNoMidRunFallbackToDisk:
    def _tmpfs_session(self, monkeypatch, tmp_path):
        root = tmp_path / "nexus-test-pg-me"
        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(sub, "_cgroup_headroom", lambda **_k: None)
        return root

    def test_cluster_after_a_squeeze_stays_on_the_chosen_tmpfs(
        self, monkeypatch, tmp_path,
    ) -> None:
        root = self._tmpfs_session(monkeypatch, tmp_path)
        answers = iter([(root, None)])  # a second per-cluster decision would StopIteration
        calls: list[int] = []

        def _decide(**_k):
            calls.append(1)
            return next(answers, (None, "the cgroup memory headroom is 1024 MiB"))

        monkeypatch.setattr(sub, "decide_pg_root", _decide)
        # 1 GiB left: under the 3 GiB START floor, over the 512 MiB hard floor.
        monkeypatch.setattr(sub, "_cgroup_headroom", lambda **_k: 1 * 1024**3)
        first = sub.pg_cluster_parent()
        second = sub.pg_cluster_parent()
        assert first == second == str(root)
        assert calls == [1]

    def test_a_cluster_that_cannot_fit_fails_loud_naming_cause_and_remedy(
        self, monkeypatch, tmp_path,
    ) -> None:
        root = self._tmpfs_session(monkeypatch, tmp_path)
        monkeypatch.setattr(sub, "decide_pg_root", lambda **_k: (root, None))
        assert sub.pg_cluster_parent() == str(root)
        monkeypatch.setattr(sub, "_cgroup_headroom", lambda **_k: 100 * 1024**2)
        with pytest.raises(sub.SubstrateRootExhausted) as ei:
            sub.pg_cluster_parent()
        msg = str(ei.value)
        assert "cgroup" in msg and "100 MiB" in msg
        assert "will not fall back to disk" in msg
        assert "-n N" in msg and "NX_TEST_PG_ROOT" in msg and "lab-run class" in msg

    def test_a_full_tmpfs_fails_loud_too(self, tmp_path) -> None:
        with pytest.raises(sub.SubstrateRootExhausted, match="MiB free"):
            sub._check_tmpfs_has_room(
                tmp_path, free_bytes=lambda _p: 10 * 1024**2,
                cgroup_headroom=lambda: None,
            )

    def test_room_above_the_hard_floor_passes(self, tmp_path) -> None:
        sub._check_tmpfs_has_room(
            tmp_path, free_bytes=lambda _p: sub._TMPFS_HARD_FLOOR_BYTES,
            cgroup_headroom=lambda: sub._TMPFS_HARD_FLOOR_BYTES,
        )

    def test_the_hard_floor_is_below_the_start_floor(self) -> None:
        # a run whose headroom shrank from 3 GiB toward 512 MiB keeps going
        assert sub._TMPFS_HARD_FLOOR_BYTES < sub._TMPFS_MIN_FREE_BYTES
        assert sub._TMPFS_HARD_FLOOR_BYTES >= 4 * 108 * 1024**2  # one cluster, measured

    def test_droppable_page_cache_counts_as_room_for_the_mid_run_check(
        self, tmp_path,
    ) -> None:
        d = tmp_path / "cg" / "g"
        d.mkdir(parents=True)
        (d / "memory.max").write_text("1000\n")
        (d / "memory.current").write_text("990\n")
        (d / "memory.stat").write_text("anon 100\nshmem 50\nactive_file 300\ninactive_file 400\n")
        kw = dict(proc_cgroup="0::/g\n", cgroup_root=tmp_path / "cg")
        assert sub._cgroup_headroom(**kw) == 10
        assert sub._cgroup_headroom(credit_reclaimable=True, **kw) == 710

    def test_cgroup_memory_limit_is_the_tightest_max(self, tmp_path) -> None:
        for rel, mx in {"": "max", "a": "5000", "a/b": "3000"}.items():
            d = tmp_path / "cg" / rel if rel else tmp_path / "cg"
            d.mkdir(parents=True, exist_ok=True)
            (d / "memory.max").write_text(f"{mx}\n")
        assert sub.cgroup_memory_limit(
            proc_cgroup="0::/a/b\n", cgroup_root=tmp_path / "cg",
        ) == 3000
        assert sub.cgroup_memory_limit(
            proc_cgroup="0::/\n", cgroup_root=tmp_path / "cg",
        ) is None


class TestEveryClusterStartSaysWhereItWent:
    def test_a_disk_start_with_a_reason_is_a_warning_naming_it(self, monkeypatch) -> None:
        import structlog.testing

        monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
        monkeypatch.setattr(
            sub, "decide_pg_root",
            lambda **_k: (None, "the cgroup memory headroom is 1 MiB"),
        )
        with structlog.testing.capture_logs() as logs, pytest.warns(UserWarning):
            assert sub.pg_cluster_parent() is None
        row = next(e for e in logs if e["event"] == "nexus_t2_substrate.cluster_root")
        assert row["log_level"] == "warning"
        assert row["on"] == "disk" and "cgroup" in row["reason"]

    def test_the_run_summary_counts_tmpfs_and_disk_across_workers(
        self, monkeypatch, tmp_path,
    ) -> None:
        log = tmp_path / "roots.log"
        log.write_text("")
        monkeypatch.setenv(sub._PG_ROOT_LOG_ENV, str(log))
        sub._record_cluster_root("tmpfs", "/dev/shm/nexus-test-pg-1", None)
        sub._record_cluster_root("tmpfs", "/dev/shm/nexus-test-pg-1", None)
        sub._record_cluster_root("disk", None, "the cgroup memory headroom is 1 MiB")
        line = sub.pg_root_run_summary()
        assert line is not None
        assert "clusters on tmpfs 2, on disk 1" in line
        assert "cgroup memory headroom" in line
        assert len(log.read_text().splitlines()) == 3  # workers append to the shared log

    def test_a_serial_run_summarises_its_own_starts(self) -> None:
        assert sub.pg_root_run_summary() is None  # nothing started: no line
        sub._record_cluster_root("disk", None, None)
        assert "clusters on tmpfs 0, on disk 1" in (sub.pg_root_run_summary() or "")

    def test_the_session_summary_line_is_printed_by_the_controller_only(
        self, monkeypatch,
    ) -> None:
        from tests import conftest as ct

        sub._record_cluster_root("tmpfs", "/dev/shm/x", None)

        class Reporter:
            lines: list[str] = []

            def write_line(self, line: str) -> None:
                self.lines.append(line)

        class Cfg:
            pass

        rep = Reporter()
        ct.pytest_terminal_summary(rep, 0, Cfg())
        assert rep.lines and "clusters on tmpfs 1, on disk 0" in rep.lines[0]

        class WorkerCfg:
            workerinput = {}

        rep2 = Reporter()
        rep2.lines = []
        ct.pytest_terminal_summary(rep2, 0, WorkerCfg())
        assert rep2.lines == []

    def test_a_real_cluster_start_is_recorded(self, monkeypatch, tmp_path, long_paths_ok) -> None:
        monkeypatch.setenv("NX_TEST_PG_ROOT", str(tmp_path / "r"))
        assert sub.pg_cluster_parent() == str(tmp_path / "r")
        assert sub._LOCAL_ROWS and sub._LOCAL_ROWS[-1]["root"] == str(tmp_path / "r")
