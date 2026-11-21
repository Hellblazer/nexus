# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long tempdir on macOS no longer stops the substrate from starting Postgres.

hellmini, 2026-11-20: ``lab-remote`` ran from a worktree whose tempdir was
``/Users/you/xxxxxxx/lab-remote/<run>/.lab-remote/tmp``, so every cluster's unix
socket path came to 114 bytes, past macOS's 104-byte ``sun_path`` (103 usable),
and all 3331 tests errored at setup. The chooser knew only Linux's 107 and had no
fallback. These tests drive the decision with injected inputs (platform, tempdir,
uid), so no outcome depends on the box running them.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

import pytest
import structlog.testing

from tests import _engine_substrate as sub
from tests import _pg_ctl
from tests._module_seam import setattr_in

#: The decisions pinned here are the Darwin /tmp root and the Linux /dev/shm root, built from POSIX paths and
#: uids; on Windows those paths read as WindowsPath and a SID is not a uid, so the module cannot run there. Windows
#: itself is untouched by the change (decide_pg_root returns no root off Darwin and Linux).
pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX-only: Darwin /tmp and Linux /dev/shm roots, uid ownership")

#: The tempdir from the hellmini failure, as the substrate saw it.
_LONG_TMP = Path("/Users/you/xxxxxxx/lab-remote/marlowe-p2m1x-6f17406a/.lab-remote/tmp")
_SHORT_TMP = Path("/var/folders/xx/yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy/T")
_TMPFS_MOUNTS = "tmpfs /dev/shm tmpfs rw,nosuid,nodev 0 0\n/dev/nvme0n1p2 / ext4 rw 0 0\n"


def _decide(**kw) -> tuple[Path | None, str | None]:
    base = dict(env={}, system="Darwin", tempdir=_LONG_TMP, uid=501, short_dir=Path("/tmp"))
    return sub.decide_pg_root(**{**base, **kw})


def _root_of_length(n: int) -> Path:
    """An absolute path exactly *n* bytes long."""
    p = "/" + "a" * (n - 1)
    assert len(p) == n
    return Path(p)


@pytest.fixture(autouse=True)
def _fresh_session_decision(monkeypatch):
    """Start every test with no persisted root decision, run log or counted start."""
    monkeypatch.setenv(sub._PG_ROOT_SESSION_ENV, "")
    monkeypatch.delenv(sub._PG_ROOT_LOG_ENV, raising=False)
    monkeypatch.delenv("NX_TEST_PG_ROOT", raising=False)
    monkeypatch.setattr(sub, "_LOCAL_ROWS", [])
    monkeypatch.setattr(sub, "_OWNED_SESSION", [])
    monkeypatch.setattr(sub, "_REFUSAL_WARNED", [])


class TestSocketLimitPerPlatform:
    def test_darwin_is_103_and_linux_is_107(self) -> None:
        assert sub._socket_path_max("Darwin") == 103
        assert sub._socket_path_max("Linux") == 107

    def test_pg_ctl_is_the_one_source_of_the_per_platform_limit(self) -> None:
        # Pinned on every platform, so Linux CI checks the Darwin branch.
        assert _pg_ctl.sun_path_max("Darwin") == 103
        assert _pg_ctl.sun_path_max("Linux") == 107
        assert _pg_ctl.sun_path_max("Windows") == 107
        assert _pg_ctl.SUN_PATH_MAX == _pg_ctl.sun_path_max()

    def test_the_substrate_limit_follows_pg_ctl_so_the_two_cannot_disagree(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "sun_path_max", lambda system=None: 50 if system == "Darwin" else 107)
        assert sub._socket_path_max("Darwin") == 50
        assert sub._socket_path_max("Linux") == 107

    def test_the_edge_is_the_reserve_below_the_limit_and_differs_by_platform(self) -> None:
        room_darwin = sub._socket_path_max("Darwin") - sub._SOCKET_SUFFIX_RESERVE
        room_linux = sub._socket_path_max("Linux") - sub._SOCKET_SUFFIX_RESERVE
        assert room_linux - room_darwin == 4
        assert sub._socket_path_fits(_root_of_length(room_darwin), "Darwin")
        assert not sub._socket_path_fits(_root_of_length(room_darwin + 1), "Darwin")
        assert sub._socket_path_fits(_root_of_length(room_linux), "Linux")
        assert not sub._socket_path_fits(_root_of_length(room_linux + 1), "Linux")
        # A root Linux accepts and macOS cannot.
        assert sub._socket_path_fits(_root_of_length(room_darwin + 1), "Linux")

    def test_the_hellmini_tempdir_is_past_the_limit_on_darwin(self) -> None:
        assert not sub._socket_path_fits(_LONG_TMP, "Darwin")
        assert sub._socket_path_fits(_SHORT_TMP, "Darwin")


class TestDarwinFallsBackToAShortRoot:
    def test_a_long_tempdir_picks_the_per_uid_tmp_root(self) -> None:
        assert _decide() == (Path("/tmp/nexus-test-pg-501"), None)

    def test_the_uid_is_part_of_the_name(self) -> None:
        assert _decide(uid=4242)[0] == Path("/tmp/nexus-test-pg-4242")

    def test_a_short_tempdir_keeps_the_tempdir(self) -> None:
        assert _decide(tempdir=_SHORT_TMP) == (None, None)

    def test_the_boundary_is_the_reserve_below_the_darwin_limit(self) -> None:
        room = sub._socket_path_max("Darwin") - sub._SOCKET_SUFFIX_RESERVE
        assert _decide(tempdir=_root_of_length(room)) == (None, None)
        assert _decide(tempdir=_root_of_length(room + 1))[0] == Path("/tmp/nexus-test-pg-501")

    def test_the_fallback_itself_fits_and_names_like_the_tmpfs_root(self) -> None:
        root, _ = _decide()
        assert root is not None and root.name == f"{sub._TMPFS_DIR_PREFIX}-501"
        assert sub._socket_path_fits(root, "Darwin")

    def test_default_is_an_instruction_and_keeps_the_tempdir(self) -> None:
        assert _decide(env={"NX_TEST_PG_ROOT": "default"}) == (None, None)

    def test_a_fallback_that_cannot_fit_leaves_the_tempdir_for_the_loud_check(self) -> None:
        assert _decide(short_dir=_root_of_length(90)) == (None, None)

    def test_the_decision_reads_the_original_tempdir_by_default(self, monkeypatch) -> None:
        monkeypatch.setenv(sub._TMP_ORIG_ENV, str(_LONG_TMP))
        root, _ = sub.decide_pg_root(env={}, system="Darwin", uid=7, short_dir=Path("/tmp"))
        assert root == Path("/tmp/nexus-test-pg-7")

    def test_a_darwin_choice_is_silent_while_the_tempdir_is_short(self, recwarn) -> None:
        assert sub.choose_pg_root(
            env={}, system="Darwin", tempdir=_SHORT_TMP, uid=501, short_dir=Path("/tmp"),
        ) is None
        assert not [w for w in recwarn if "tmpfs" in str(w.message)]

    def test_the_fallback_is_not_a_warned_refusal(self, recwarn) -> None:
        assert sub.choose_pg_root(
            env={}, system="Darwin", tempdir=_LONG_TMP, uid=501, short_dir=Path("/tmp"),
        ) == Path("/tmp/nexus-test-pg-501")
        assert not [w for w in recwarn if "tmpfs" in str(w.message)]


class TestPersistedDecision:
    """The run's decision rides in ``NX_TEST_PG_ROOT_SESSION`` for every worker. It names a
    root, not a filesystem: the macOS ``/tmp`` disk root travels in it too."""

    def test_a_root_is_written_under_a_neutral_prefix(self) -> None:
        assert sub._encode_session_root(Path("/tmp/nexus-test-pg-501"), None) == "root=/tmp/nexus-test-pg-501"

    def test_a_disk_decision_keeps_its_prefix(self) -> None:
        assert sub._encode_session_root(None, "why") == "disk=why"

    def test_both_prefixes_decode_so_an_older_controllers_value_still_works(self) -> None:
        assert sub._decode_session_root("root=/tmp/x") == (True, Path("/tmp/x"), None)
        assert sub._decode_session_root("tmpfs=/dev/shm/nexus-test-pg-9") == (
            True, Path("/dev/shm/nexus-test-pg-9"), None,
        )
        assert sub._decode_session_root("root=")[0] is False
        assert sub._decode_session_root("") == (False, None, None)


class TestExplicitRootStillRefusesLoud:
    def test_a_too_long_explicit_root_raises_on_darwin(self) -> None:
        with pytest.raises(ValueError, match="sun_path") as err:
            _decide(env={"NX_TEST_PG_ROOT": str(_LONG_TMP)})
        assert "103-byte" in str(err.value)

    def test_the_explicit_root_is_not_replaced_by_the_fallback_even_when_long(self) -> None:
        with pytest.raises(ValueError):
            _decide(env={"NX_TEST_PG_ROOT": str(_LONG_TMP)}, tempdir=_SHORT_TMP)

    def test_a_root_that_fits_linux_but_not_darwin_refuses_only_on_darwin(self) -> None:
        room_darwin = sub._socket_path_max("Darwin") - sub._SOCKET_SUFFIX_RESERVE
        root = _root_of_length(room_darwin + 1)
        env = {"NX_TEST_PG_ROOT": str(root)}
        assert _decide(env=env, system="Linux") == (root, None)
        with pytest.raises(ValueError, match="sun_path"):
            _decide(env=env, system="Darwin")

    def test_an_explicit_short_root_is_used_as_given(self) -> None:
        assert _decide(env={"NX_TEST_PG_ROOT": "/tmp/mine"}) == (Path("/tmp/mine"), None)


class TestLinuxIsUnchanged:
    """And so are the platforms with no 104-byte ``sun_path`` (Windows)."""


    def _linux(self, **kw):
        base = dict(
            env={}, system="Linux", mounts_text=_TMPFS_MOUNTS, uid=1000, tempdir=_LONG_TMP,
            free_bytes=lambda _p: 64 * 1024**3, writable=lambda _p: True,
            cgroup_headroom=lambda: None, short_dir=Path("/tmp"),
        )
        return sub.decide_pg_root(**{**base, **kw})

    def test_a_long_tempdir_still_gets_the_tmpfs_root(self) -> None:
        assert self._linux() == (Path("/dev/shm/nexus-test-pg-1000"), None)

    def test_no_tmpfs_means_the_tempdir_and_a_refusal_not_the_tmp_fallback(self) -> None:
        root, refusal = self._linux(mounts_text="/dev/nvme0n1p2 / ext4 rw 0 0\n")
        assert root is None and refusal is not None and "not tmpfs" in refusal

    def test_windows_keeps_its_tempdir_whatever_its_length(self) -> None:
        assert _decide(system="Windows") == (None, None)

    def test_the_linux_limit_stays_107(self) -> None:
        assert sub._SOCKET_PATH_MAX == 107


class TestSweepLooksInTheFallbackRoot:
    def _roots(self, tmp_path: Path, **kw) -> list[Path]:
        base = dict(
            env={}, system="Darwin", uid=501, tempdir=tmp_path / "tmp",
            shm_dir=tmp_path / "shm", short_dir=tmp_path / "short",
        )
        return sub.substrate_pg_roots(**{**base, **kw})

    def test_an_existing_fallback_root_is_swept_on_darwin(self, tmp_path) -> None:
        cand = tmp_path / "short" / "nexus-test-pg-501"
        cand.mkdir(parents=True)
        assert self._roots(tmp_path) == [cand, tmp_path / "tmp"]

    def test_a_missing_fallback_root_is_not_listed(self, tmp_path) -> None:
        assert self._roots(tmp_path) == [tmp_path / "tmp"]

    def test_a_symlink_planted_under_the_name_is_not_our_root(self, tmp_path) -> None:
        short = tmp_path / "short"
        short.mkdir()
        (short / "nexus-test-pg-501").symlink_to(tmp_path)
        assert self._roots(tmp_path) == [tmp_path / "tmp"]

    def test_the_fallback_root_is_swept_whatever_the_override(self, tmp_path) -> None:
        cand = tmp_path / "short" / "nexus-test-pg-501"
        cand.mkdir(parents=True)
        assert cand in self._roots(tmp_path, env={"NX_TEST_PG_ROOT": "default"})
        assert cand in self._roots(tmp_path, env={"NX_TEST_PG_ROOT": str(tmp_path / "mine")})

    def test_a_fallback_root_owned_by_someone_else_is_skipped_and_said_so(
        self, tmp_path, monkeypatch,
    ) -> None:
        (tmp_path / "short" / "nexus-test-pg-501").mkdir(parents=True)
        owner = (tmp_path / "short").stat().st_uid
        monkeypatch.setattr(sub, "_current_uid", lambda: str(owner + 1))
        with structlog.testing.capture_logs() as logs:
            assert self._roots(tmp_path, uid=501) == [tmp_path / "tmp"]
        assert [e for e in logs if e["event"] == "nexus_t2_substrate.sweep_root_skipped"]

    def test_a_tmpfs_root_owned_by_someone_else_is_skipped_and_said_so(
        self, tmp_path, monkeypatch,
    ) -> None:
        (tmp_path / "shm" / "nexus-test-pg-1000").mkdir(parents=True)
        owner = (tmp_path / "shm").stat().st_uid
        monkeypatch.setattr(sub, "_current_uid", lambda: str(owner + 1))
        with structlog.testing.capture_logs() as logs:
            assert self._roots(tmp_path, system="Linux", uid=1000) == [tmp_path / "tmp"]
        assert [e for e in logs if e["event"] == "nexus_t2_substrate.sweep_root_skipped"]

    def test_a_root_of_ours_is_still_listed(self, tmp_path) -> None:
        cand = tmp_path / "shm" / "nexus-test-pg-1000"
        cand.mkdir(parents=True)
        assert self._roots(tmp_path, system="Linux", uid=1000) == [cand, tmp_path / "tmp"]

    def test_linux_does_not_look_there(self, tmp_path) -> None:
        (tmp_path / "short" / "nexus-test-pg-501").mkdir(parents=True)
        assert self._roots(tmp_path, system="Linux") == [tmp_path / "tmp"]

    def test_the_shm_seam_also_stands_in_for_the_short_dir(self, tmp_path, monkeypatch) -> None:
        seam = tmp_path / "seam"
        cand = seam / f"nexus-test-pg-{sub._current_uid()}"
        cand.mkdir(parents=True)
        monkeypatch.setenv(sub._SHM_DIR_SEAM_ENV, str(seam))
        roots = sub.substrate_pg_roots(env=dict(os.environ), system="Darwin", tempdir=tmp_path / "tmp")
        assert cand in roots


class TestTheSweepOfTheShortRoot:
    """``/tmp/nexus-test-pg-<uid>`` is a disk root: the sweep reports what it cannot prove
    stale and reaps only what a dead owner left, as for the tempdir."""

    @pytest.fixture
    def short_root(self):
        parent = Path(tempfile.mkdtemp(prefix="nxs", dir="/tmp"))
        root = parent / f"{sub._TMPFS_DIR_PREFIX}-501"
        root.mkdir()
        try:
            yield root
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    def test_it_is_not_a_tmpfs_root(self, short_root) -> None:
        assert not sub._is_tmpfs_root(short_root)
        assert not sub._is_tmpfs_root(Path("/tmp/nexus-test-pg-501"))

    def test_a_bare_old_dir_is_reported_never_age_reaped(self, short_root) -> None:
        d = short_root / "nexus_t2_substrate_pg_bare0001"
        d.mkdir()
        (d / "PG_VERSION").write_text("17\n")
        old = time.time() - 3600
        os.utime(d, (old, old))
        with pytest.warns(UserWarning, match="legacy"):
            result = sub.sweep_stale_substrate_clusters(tmp_root=short_root)
        assert result.reaped == [] and result.legacy_reported == [str(d)]
        assert d.exists()

    def test_a_cluster_with_a_live_owner_is_left_alone(self, short_root) -> None:
        d = short_root / "nexus_t2_substrate_pg_live0001"
        d.mkdir()
        (d / sub._SIDECAR_FILENAME).write_text(json.dumps({
            "owner_pytest_pid": os.getpid(), "postmaster_pid": 999_999_999,
            "postmaster_cmdline": "irrelevant", "engine_pid": 999_999_998,
            "engine_cmdline": "irrelevant",
        }))
        result = sub.sweep_stale_substrate_clusters(tmp_root=short_root)
        assert str(d) in result.live_untouched and result.reaped == []
        assert d.exists()


@pytest.mark.skipif(os.name == "nt", reason="the /tmp fallback is a macOS (POSIX) root")
class TestTheStartReportsTheFallback:
    @pytest.fixture
    def darwin_long_tmp(self, monkeypatch):
        # The stand-in for /tmp must itself be short, or the fallback does not fit (that is
        # the point of it); pytest's tmp_path is not.
        short = Path(tempfile.mkdtemp(prefix="nxs", dir="/tmp"))
        monkeypatch.setattr(sub, "_SHORT_ROOT_DIR", short)
        setattr_in(monkeypatch, sub, "platform.system", lambda: "Darwin")
        monkeypatch.setenv(sub._TMP_ORIG_ENV, str(_LONG_TMP))

        def _no_room_check(*_a, **_k) -> None:
            raise AssertionError("the tmpfs room check ran for a disk fallback root")

        monkeypatch.setattr(sub, "_check_tmpfs_has_room", _no_room_check)
        try:
            yield short / f"{sub._TMPFS_DIR_PREFIX}-{sub._current_uid()}"
        finally:
            shutil.rmtree(short, ignore_errors=True)

    def test_the_root_is_created_private_and_returned(self, darwin_long_tmp) -> None:
        assert sub.pg_cluster_parent() == str(darwin_long_tmp)
        st = darwin_long_tmp.lstat()
        assert stat.S_ISDIR(st.st_mode) and st.st_mode & 0o777 == 0o700

    def test_a_loose_dir_of_ours_is_tightened(self, darwin_long_tmp) -> None:
        darwin_long_tmp.mkdir(mode=0o755)
        darwin_long_tmp.chmod(0o755)
        assert sub.pg_cluster_parent() == str(darwin_long_tmp)
        assert darwin_long_tmp.stat().st_mode & 0o777 == 0o700

    def test_the_decision_is_made_once_and_inherited(self, darwin_long_tmp, monkeypatch) -> None:
        first = sub.pg_cluster_parent()
        monkeypatch.setenv(sub._TMP_ORIG_ENV, str(_SHORT_TMP))  # a later worker sees a different tempdir
        assert sub.pg_cluster_parent() == first

    def test_a_planted_symlink_under_the_name_is_refused_and_the_start_falls_back(
        self, darwin_long_tmp, tmp_path,
    ) -> None:
        darwin_long_tmp.parent.mkdir(exist_ok=True)
        darwin_long_tmp.symlink_to(tmp_path)
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None
        assert list(tmp_path.iterdir()) == []  # nothing was made through the link

    def test_a_dir_owned_by_someone_else_is_refused_and_the_start_falls_back(
        self, darwin_long_tmp, monkeypatch,
    ) -> None:
        monkeypatch.setattr(sub, "_current_uid", lambda: str(os.getuid() + 1))
        # The name carries the uid, so the dir the decision names is a different one; plant it.
        planted = darwin_long_tmp.parent / f"{sub._TMPFS_DIR_PREFIX}-{sub._current_uid()}"
        planted.mkdir()
        with pytest.warns(UserWarning, match="falling back"):
            assert sub.pg_cluster_parent() is None

    def test_the_log_row_is_info_on_disk_and_names_the_cause(self, darwin_long_tmp) -> None:
        old = structlog.get_config()["wrapper_class"]
        structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
        try:
            with structlog.testing.capture_logs() as logs:
                sub.pg_cluster_parent()
        finally:
            structlog.configure(wrapper_class=old)
        row = next(e for e in logs if e["event"] == "nexus_t2_substrate.cluster_root")
        assert row["log_level"] == "info"
        assert row["on"] == "disk" and row["root"] == str(darwin_long_tmp)
        assert row["reason"] is None
        assert "too long" in row["note"] and "sun_path" in row["note"]

    def test_the_summary_line_names_the_root_and_why(self, darwin_long_tmp) -> None:
        sub.pg_cluster_parent()
        line = sub.pg_root_run_summary() or ""
        assert "on disk 1" in line and "disk because" not in line
        assert f"clusters moved to {darwin_long_tmp}: the tempdir {_LONG_TMP} is too long" in line
        assert "103-byte sun_path" in line
        assert f"[root {darwin_long_tmp}]" in line
