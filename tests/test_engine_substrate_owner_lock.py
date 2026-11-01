# SPDX-License-Identifier: AGPL-3.0-or-later
"""The substrate sweep judges an owner live by the flock it holds, not by its pid (nexus-fuz9t).

lab-run #88 puts each CI job in its own PID namespace. A sweeper in one job sees every
sibling job's owner pid, and the sibling's postmaster and engine pids, as dead, and
``shutil.rmtree``s a live cluster's pgdata. The same fix landed for the tmp-root sweep in
``tests/_tmp_root.py`` (e5afc436c): the owner takes ``flock(LOCK_EX)`` on a lock file and
holds it for as long as it owns the cluster; a sweeper that cannot take the lock treats the
owner as live, whatever its pid says.

Two producers carry the lock: the substrate's own cluster (``<pgdata>/nexus_substrate_owner.lock``,
taken by ``_write_sidecar`` before the sidecar exists) and the integration owner record
(``nexus_integration_owner_<pid>-<token>.lock`` beside the ``.json``). Every test here builds a fixture
cluster dir or record only, with real child processes for the holders; no PG and no engine.

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_owner_lock.py -q
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from nexus.daemon.service_registry import process_command
from tests import _engine_substrate as sub
from tests._module_seam import setattr_in
from tests._engine_substrate import (
    _SIDECAR_FILENAME,
    sweep_stale_substrate_clusters,
)

needs_flock = pytest.mark.skipif(sub.fcntl is None, reason="no fcntl (Windows): the pid rule is the only rule")

#: Set to 1 where a skip of the cross-namespace test must be a failure.
REQUIRE_PIDNS_ENV = "NX_REQUIRE_PIDNS"

_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("READY", os.readlink("/proc/self/ns/pid") if os.path.exists("/proc/self/ns/pid") else "", flush=True)
sys.stdin.read()
"""

_TRY_LOCK = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
"""


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    return proc.pid


@contextlib.contextmanager
def _holding(lock: Path, *, wrap: tuple[str, ...] = ()):
    """A real child process that holds ``flock(LOCK_EX)`` on *lock* until the block ends
    (the child is gone after it, and the lock with it). Yields the child's PID-namespace id, or
    None where there is no procfs. A *wrap* the host refuses (``unshare``) raises RuntimeError."""
    child = subprocess.Popen(
        [*wrap, sys.executable, "-c", _HOLDER, str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        line = child.stdout.readline()
        if not line.startswith("READY"):
            child.wait(timeout=30)
            raise RuntimeError(child.stderr.read().strip() or f"exit {child.returncode}")
        parts = line.split()
        yield parts[1] if len(parts) > 1 else None
    finally:
        with contextlib.suppress(OSError):
            child.stdin.close()
        child.wait(timeout=30)
        child.stdout.close()
        child.stderr.close()


def _cannot_lock(path: Path) -> bool:
    """True while some other process holds *path*'s flock."""
    return subprocess.run([sys.executable, "-c", _TRY_LOCK, str(path)], check=False).returncode == 1


def _wait_for_cmdline(proc: subprocess.Popen, budget_s: float = 10.0) -> None:
    """Return once *proc*'s command line is readable, as the sweep will read it.

    ``Popen`` returns when the exec closes the child's close-on-exec pipe, which the kernel does
    before the new image's argv is in place: for that moment ``/proc/<pid>/cmdline`` reads empty,
    and ``_identify_leg`` reads an empty command line as a dead leg (no PID-reuse warning, the
    cluster reaped). A sweep within a millisecond of ``Popen`` on a loaded runner hits it."""
    deadline = time.monotonic() + budget_s
    while not process_command(proc.pid):
        assert proc.poll() is None, f"the leg exited at startup (rc={proc.returncode})"
        assert time.monotonic() < deadline, f"pid {proc.pid}'s command line never became readable"
        time.sleep(0.01)


# ── The sidecar cluster ─────────────────────────────────────────────────────────


def _cluster(root: Path, *, owner_pid: int, name: str = "nexus_t2_substrate_pg_lock001", lock: bool = True) -> Path:
    """A cluster dir in the shape of a booted one as far as the sweep can tell: a sidecar naming
    *owner_pid* with no legs, and (by default) an owner lock file for a holder to lock."""
    d = root / name
    d.mkdir(parents=True)
    (d / _SIDECAR_FILENAME).write_text(json.dumps({"owner_pytest_pid": owner_pid, "owner_cmdline": ""}))
    (d / "PG_VERSION").write_text("17")
    if lock:
        (d / sub._OWNER_LOCK_FILENAME).touch()
    return d


@needs_flock
class TestSidecarClusterOwnerLock:
    def test_a_held_lock_keeps_the_cluster_though_its_owner_pid_is_dead(self, tmp_path) -> None:
        d = _cluster(tmp_path, owner_pid=_dead_pid())
        with _holding(d / sub._OWNER_LOCK_FILENAME):
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
            assert str(d) in result.live_untouched
            assert result.reaped == [] and result.mismatch_refused == []
            assert (d / "PG_VERSION").exists()

    def test_the_same_cluster_goes_when_the_holder_exits_though_its_owner_pid_is_live(self, tmp_path) -> None:
        """A live owner pid: only the lock can condemn it."""
        d = _cluster(tmp_path, owner_pid=os.getpid())
        with _holding(d / sub._OWNER_LOCK_FILENAME):
            assert sweep_stale_substrate_clusters(tmp_root=tmp_path).reaped == []
        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
        assert result.reaped == [str(d)] and not d.exists()

    def test_a_cluster_with_no_lock_follows_the_pid_rule_both_ways(self, tmp_path) -> None:
        """What a booter of an older tree left: no lock file, so a live owner pid keeps it and a dead one does not."""
        live = _cluster(tmp_path, owner_pid=os.getpid(), name="nexus_t2_substrate_pg_live", lock=False)
        dead = _cluster(tmp_path, owner_pid=_dead_pid(), name="nexus_t2_substrate_pg_dead", lock=False)
        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
        assert result.live_untouched == [str(live)] and result.reaped == [str(dead)]
        assert live.is_dir() and not dead.exists()

    def test_a_lock_that_cannot_be_tried_keeps_the_cluster(self, tmp_path) -> None:
        d = _cluster(tmp_path, owner_pid=_dead_pid(), lock=False)
        (d / sub._OWNER_LOCK_FILENAME).symlink_to(tmp_path)  # O_NOFOLLOW refuses: not ours to judge
        result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
        assert result.reaped == [] and d.is_dir()
        assert str(d) in result.live_untouched

    def test_a_held_lock_signals_no_leg(self, tmp_path) -> None:
        """The legs of a live owner are never looked at: a postmaster pid the sweeper cannot see
        reads as dead, and a pid that means something else here reads as a mismatch."""
        live_leg = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            d = _cluster(tmp_path, owner_pid=_dead_pid())
            sidecar = d / _SIDECAR_FILENAME
            sidecar.write_text(json.dumps({
                "owner_pytest_pid": _dead_pid(),
                "postmaster_pid": live_leg.pid,
                "postmaster_cmdline": "not what this pid runs",
            }))
            with _holding(d / sub._OWNER_LOCK_FILENAME):
                result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
            assert result.live_untouched == [str(d)] and result.mismatch_refused == []
            assert live_leg.poll() is None
        finally:
            live_leg.kill()
            live_leg.wait(timeout=5)

    def test_a_freed_lock_still_checks_the_legs_before_reaping(self, tmp_path) -> None:
        """The lock replaces only the owner-liveness test; the PID-reuse guard on the legs stays."""
        live_leg = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            _wait_for_cmdline(live_leg)
            d = _cluster(tmp_path, owner_pid=os.getpid())
            (d / _SIDECAR_FILENAME).write_text(json.dumps({
                "owner_pytest_pid": os.getpid(),
                "postmaster_pid": live_leg.pid,
                "postmaster_cmdline": "not what this pid runs",
            }))
            with pytest.warns(UserWarning, match="PID-reuse guard"):
                result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
            assert result.mismatch_refused == [str(d)] and d.is_dir() and live_leg.poll() is None
        finally:
            live_leg.kill()
            live_leg.wait(timeout=5)

    def test_the_boot_holds_a_lock_a_second_process_cannot_take(self, tmp_path) -> None:
        """``_write_sidecar`` is the first thing a booted cluster has in it: the lock is in place and
        held by the time a sweeper can see the sidecar, and it is held by the writer's own process."""
        pgdata = tmp_path / "nexus_t2_substrate_pg_boot001"
        pgdata.mkdir()
        try:
            sub._write_sidecar(str(pgdata), pg_port=1, svc_port=None, postmaster_pid=None, engine_pid=None)
            lock = pgdata / sub._OWNER_LOCK_FILENAME
            assert lock.is_file() and (pgdata / _SIDECAR_FILENAME).is_file()
            assert not list(pgdata.glob("*.tmp")), "the temporary name is gone once the lock is in place"
            assert _cannot_lock(lock), "the lock must be held while the owner lives"
            assert sweep_stale_substrate_clusters(tmp_root=tmp_path).live_untouched == [str(pgdata)]
            # A later write (the postmaster and engine checkpoints) keeps the one lock.
            sub._write_sidecar(str(pgdata), pg_port=1, svc_port=None, postmaster_pid=None, engine_pid=None)
            assert _cannot_lock(lock)
        finally:
            sub._release_cluster_lock(str(pgdata))
        assert not _cannot_lock(pgdata / sub._OWNER_LOCK_FILENAME), "releasing drops the lock"

    def test_the_lock_is_not_inherited_by_a_child_the_owner_execs(self, tmp_path) -> None:
        """The engine and postmaster are children of the owner. If one held the lock, a SIGKILLed owner
        would look alive for as long as its engine did, and the dead owner's cluster would never be reaped."""
        pgdata = tmp_path / "nexus_t2_substrate_pg_inherit"
        pgdata.mkdir()
        sub._write_sidecar(str(pgdata), pg_port=1, svc_port=None, postmaster_pid=None, engine_pid=None)
        lock = pgdata / sub._OWNER_LOCK_FILENAME
        fd = sub._cluster_lock_fds[str(pgdata)]
        survivor = None
        try:
            assert os.get_inheritable(fd) is False
            survivor = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
        finally:
            sub._release_cluster_lock(str(pgdata))  # the owner "dies" while its child lives on
        try:
            assert not _cannot_lock(lock)
        finally:
            survivor.stdin.close()
            survivor.wait(timeout=30)

    def test_a_cluster_in_another_pid_namespace_is_not_swept(self, tmp_path) -> None:
        """The incident, for real: the holder runs under ``unshare --pid`` and the sweeper records
        its owner pid as dead. ``unshare`` is refused where unprivileged user namespaces are (chas:
        AppArmor); that skips, unless ``NX_REQUIRE_PIDNS=1`` makes the refusal a failure."""
        if shutil.which("unshare") is None:
            if os.environ.get(REQUIRE_PIDNS_ENV) == "1":
                pytest.fail("NX_REQUIRE_PIDNS=1 but unshare is not installed")
            pytest.skip("unshare is not installed")
        d = _cluster(tmp_path, owner_pid=_dead_pid())
        wrap = ("unshare", "--user", "--map-root-user", "--pid", "--fork", "--mount-proc")
        holder = _holding(d / sub._OWNER_LOCK_FILENAME, wrap=wrap)
        try:
            ns = holder.__enter__()
        except RuntimeError as exc:
            if os.environ.get(REQUIRE_PIDNS_ENV) == "1":
                pytest.fail(f"NX_REQUIRE_PIDNS=1 but unshare refused: {exc}")
            pytest.skip(f"unshare refused: {exc}")
        try:
            if ns is not None:
                assert ns != os.readlink("/proc/self/ns/pid"), "the holder must be in a PID namespace of its own"
            assert sweep_stale_substrate_clusters(tmp_root=tmp_path).reaped == [] and d.is_dir()
        finally:
            holder.__exit__(None, None, None)
        assert sweep_stale_substrate_clusters(tmp_root=tmp_path).reaped == [str(d)], "gone with its holder"


# ── The integration owner record ────────────────────────────────────────────────


def _record(root: Path, *, owner_pid: int, lock: bool = True) -> tuple[Path, Path, Path]:
    """An owner record naming *owner_pid* and one cluster dir, plus (by default) the lock file beside
    it. Returns (record, lock path, cluster dir)."""
    root.mkdir(parents=True, exist_ok=True)
    pgdata = root / "nexus_integration_pg_x1"
    pgdata.mkdir()
    (pgdata / "PG_VERSION").write_text("17")
    rec = root / f"{sub._OWNER_RECORD_PREFIX}{owner_pid}.json"
    rec.write_text(json.dumps({"owner_pytest_pid": owner_pid, "owner_cmdline": "", "pgdatas": [str(pgdata)], "children": []}))
    lock_path = rec.with_suffix(".lock")
    if lock:
        lock_path.touch()
    return rec, lock_path, pgdata


@needs_flock
class TestOwnerRecordLock:
    def test_a_held_lock_keeps_the_record_and_its_cluster_though_the_pid_is_dead(self, tmp_path) -> None:
        rec, lock, pgdata = _record(tmp_path / "root", owner_pid=_dead_pid())
        with _holding(lock):
            result = sub.SweepResult()
            sub._sweep_owner_records(tmp_path / "root", result)
            assert result.live_untouched == [str(rec)] and result.reaped == []
            assert rec.exists() and (pgdata / "PG_VERSION").exists()

    def test_the_same_record_goes_when_the_holder_exits_though_its_pid_is_live(self, tmp_path) -> None:
        rec, lock, pgdata = _record(tmp_path / "root", owner_pid=os.getpid())
        with _holding(lock):
            result = sub.SweepResult()
            sub._sweep_owner_records(tmp_path / "root", result)
            assert result.reaped == []
        result = sub.SweepResult()
        sub._sweep_owner_records(tmp_path / "root", result)
        assert result.reaped == [str(pgdata)] and not pgdata.exists()
        assert not rec.exists() and not lock.exists(), "the reaped owner's lock file goes with its record"

    def test_a_record_with_no_lock_follows_the_pid_rule_both_ways(self, tmp_path) -> None:
        live_rec, _l, live_dir = _record(tmp_path / "live", owner_pid=os.getpid(), lock=False)
        dead_rec, _l2, dead_dir = _record(tmp_path / "dead", owner_pid=_dead_pid(), lock=False)
        live_result, dead_result = sub.SweepResult(), sub.SweepResult()
        sub._sweep_owner_records(tmp_path / "live", live_result)
        sub._sweep_owner_records(tmp_path / "dead", dead_result)
        assert live_result.live_untouched == [str(live_rec)] and live_dir.is_dir() and live_rec.exists()
        assert dead_result.reaped == [str(dead_dir)] and not dead_dir.exists() and not dead_rec.exists()

    def test_a_lock_that_cannot_be_tried_keeps_the_record(self, tmp_path) -> None:
        rec, lock, pgdata = _record(tmp_path / "root", owner_pid=_dead_pid(), lock=False)
        lock.symlink_to(tmp_path)
        result = sub.SweepResult()
        sub._sweep_owner_records(tmp_path / "root", result)
        assert result.live_untouched == [str(rec)] and result.reaped == [] and pgdata.is_dir()

    def test_registering_a_cluster_takes_the_lock_before_the_record_is_visible(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_owner_records", {})
        monkeypatch.setattr(sub, "_owner_record_lock_fds", {})
        monkeypatch.setattr(sub, "_owner_exit_registered", True)  # no atexit hook from a unit test
        root = tmp_path / "root"
        root.mkdir()
        pgdata = root / "nexus_integration_pg_x2"
        pgdata.mkdir()
        sub.register_owned_cluster(root, str(pgdata))
        rec = sub._owner_record_path(root)
        lock = rec.with_suffix(".lock")
        try:
            assert rec.is_file() and lock.is_file()
            assert not list(root.glob("*.tmp")), "the temporary names are gone"
            assert _cannot_lock(lock), "the record's lock is held by the process that wrote it"
            assert os.get_inheritable(sub._owner_record_lock_fds[root]) is False
            result = sub.SweepResult()
            sub._sweep_owner_records(root, result)
            assert result.live_untouched == [str(rec)] and pgdata.is_dir()
        finally:
            for fd in sub._owner_record_lock_fds.values():
                sub.release_owner_lock(fd)
        assert not _cannot_lock(lock)

    def test_dropping_the_finished_record_removes_its_lock_too(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_owner_records", {})
        monkeypatch.setattr(sub, "_owner_record_lock_fds", {})
        monkeypatch.setattr(sub, "_owner_exit_registered", True)
        root = tmp_path / "root"
        root.mkdir()
        pgdata = root / "nexus_integration_pg_x3"
        pgdata.mkdir()
        sub.register_owned_cluster(root, str(pgdata))
        shutil.rmtree(pgdata)  # the fixture's teardown
        sub._drop_finished_owner_records()
        assert list(root.iterdir()) == [], "neither the record nor its lock is left"
        assert sub._owner_record_lock_fds == {}


# ── Record names: one per process, never one per pid number (nexus-fuz9t review) ─────


def _as_a_new_process(monkeypatch) -> tuple[dict, dict]:
    """Module state as a second pytest process starts it. Its pid is still ours: that is the case
    under lab-run #88, where two jobs' workers carry the same pid number. Returns the record and
    lock-descriptor tables it will fill, so the test can later let go of this "process" alone."""
    records: dict = {}
    fds: dict = {}
    monkeypatch.setattr(sub, "_owner_records", records)
    monkeypatch.setattr(sub, "_owner_record_lock_fds", fds)
    monkeypatch.setattr(sub, "_owner_record_id_cache", None)
    monkeypatch.setattr(sub, "_owner_exit_registered", True)  # no atexit hook from a unit test
    return records, fds


class TestOwnerRecordNames:
    def test_the_name_carries_the_pid_and_a_token_chosen_once(self, monkeypatch) -> None:
        monkeypatch.setattr(sub, "_owner_record_id_cache", None)
        first = sub._owner_record_id()
        assert re.fullmatch(rf"{os.getpid()}-[0-9a-f]{{8}}", first), first
        assert sub._owner_record_id() == first, "chosen once per process"
        assert sub._OWNER_TMP_NAME.fullmatch(f"{sub._OWNER_RECORD_PREFIX}{first}.json.tmp")

    def test_a_forked_child_does_not_share_its_parents_name(self, monkeypatch) -> None:
        """The cache is keyed by pid: a child of this process (a different pid) asks again."""
        monkeypatch.setattr(sub, "_owner_record_id_cache", (os.getpid() + 1, f"{os.getpid() + 1}-deadbeef"))
        assert sub._owner_record_id() != f"{os.getpid() + 1}-deadbeef"
        assert sub._owner_record_id().startswith(f"{os.getpid()}-")

    @needs_flock
    def test_two_owners_that_share_a_pid_keep_their_own_record_and_lock(self, tmp_path, monkeypatch) -> None:
        """The reviewer's reproduction: two owners with one pid number in one root. When the pid
        named the record, B's lock replaced A's live one and B adopted A's clusters, so the sweep
        that followed B's exit reaped A's cluster while A still held its flock."""
        root = tmp_path / "root"
        root.mkdir()
        a_dir, b_dir = root / "nexus_integration_pg_a", root / "nexus_integration_pg_b"
        a_dir.mkdir()
        b_dir.mkdir()

        _a_records, a_fds = _as_a_new_process(monkeypatch)
        sub.register_owned_cluster(root, str(a_dir))
        a_rec = sub._owner_record_path(root)
        a_lock = a_rec.with_suffix(".lock")

        b_records, b_fds = _as_a_new_process(monkeypatch)
        sub.register_owned_cluster(root, str(b_dir))
        b_rec = sub._owner_record_path(root)
        b_lock = b_rec.with_suffix(".lock")
        try:
            assert a_rec != b_rec and a_lock != b_lock, "one pid number, two owners, two files each"
            a_info, b_info = json.loads(a_rec.read_text()), json.loads(b_rec.read_text())
            assert a_info["owner_pytest_pid"] == b_info["owner_pytest_pid"] == os.getpid()
            assert a_info["pgdatas"] == [str(a_dir)] and b_info["pgdatas"] == [str(b_dir)], "B adopted nothing of A's"
            assert _cannot_lock(a_lock) and _cannot_lock(b_lock), "both locks are held, each on its own file"

            for fd in b_fds.values():  # B exits: its lock drops, its record and cluster are left
                sub.release_owner_lock(fd)
            assert b_records and not _cannot_lock(b_lock) and _cannot_lock(a_lock)
            result = sub.SweepResult()
            sub._sweep_owner_records(root, result)
            assert result.live_untouched == [str(a_rec)], "A is a live owner"
            assert result.reaped == [str(b_dir)], "and only B's cluster went"
            assert a_dir.is_dir() and a_rec.exists() and a_lock.exists()
            assert not b_dir.exists() and not b_rec.exists() and not b_lock.exists()
        finally:
            for fd in a_fds.values():
                sub.release_owner_lock(fd)


# ── Create, lock, then publish: the lock is held when the file first has its name ─────


def _lock_held_at_each_lock_rename(monkeypatch) -> list[bool]:
    """Wrap ``os.replace``: for every rename onto a ``*.lock`` name, record whether the source file
    was already flocked (by this process) at that moment. A sweeper that sees the final name must
    see a held lock; renaming first and locking after leaves a window where it sees a free one and
    reaps a live owner."""
    seen: list[bool] = []
    real = os.replace

    def spy(src, dst, *args, **kwargs):
        if str(dst).endswith(".lock"):
            fd = os.open(src, os.O_RDONLY)
            try:
                sub.fcntl.flock(fd, sub.fcntl.LOCK_EX | sub.fcntl.LOCK_NB)
            except BlockingIOError:
                seen.append(True)
            else:
                seen.append(False)
            finally:
                os.close(fd)
        return real(src, dst, *args, **kwargs)

    setattr_in(monkeypatch, sub, "os.replace", spy)
    return seen


@needs_flock
class TestLockIsHeldBeforeItIsPublished:
    def test_an_owner_record_lock(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "root"
        root.mkdir()
        pgdata = root / "nexus_integration_pg_o1"
        pgdata.mkdir()
        _records, fds = _as_a_new_process(monkeypatch)
        seen = _lock_held_at_each_lock_rename(monkeypatch)
        try:
            sub.register_owned_cluster(root, str(pgdata))
        finally:
            for fd in fds.values():
                sub.release_owner_lock(fd)
        assert seen == [True], f"the record lock was renamed into place {len(seen)} time(s), held at each: {seen}"

    def test_a_substrate_cluster_lock(self, tmp_path, monkeypatch) -> None:
        pgdata = tmp_path / "nexus_t2_substrate_pg_o2"
        pgdata.mkdir()
        seen = _lock_held_at_each_lock_rename(monkeypatch)
        try:
            sub._hold_cluster_lock(str(pgdata))
        finally:
            sub._release_cluster_lock(str(pgdata))
        assert seen == [True], f"the cluster lock was renamed into place {len(seen)} time(s), held at each: {seen}"


# ── Debris an owner killed between creating its lock and publishing it leaves ─────────


@needs_flock
class TestOwnerLockDebris:
    _NOW = 1_800_000_000.0

    def _file(self, root: Path, name: str, *, age_s: float) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        p = root / name
        p.touch()
        os.utime(p, (self._NOW - age_s, self._NOW - age_s))
        return p

    def _sweep(self, root: Path) -> sub.SweepResult:
        result = sub.SweepResult()
        sub._sweep_owner_records(root, result, now=self._NOW)
        return result

    def test_an_old_free_lock_tmp_is_removed(self, tmp_path) -> None:
        root = tmp_path / "root"
        tmp = self._file(root, f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock.tmp", age_s=sub._OWNER_TMP_MIN_AGE_S + 1)
        assert self._sweep(root).stale_tmp_removed == [str(tmp)] and not tmp.exists()

    def test_an_old_lock_tmp_that_is_held_is_not_debris(self, tmp_path) -> None:
        root = tmp_path / "root"
        tmp = self._file(root, f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock.tmp", age_s=sub._OWNER_TMP_MIN_AGE_S + 1)
        with _holding(tmp):
            assert self._sweep(root).stale_tmp_removed == [] and tmp.exists()

    def test_a_young_lock_tmp_is_left_alone(self, tmp_path) -> None:
        root = tmp_path / "root"
        tmp = self._file(root, f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock.tmp", age_s=5)
        assert self._sweep(root).stale_tmp_removed == [] and tmp.exists()

    def test_an_old_free_lock_with_no_record_beside_it_is_removed(self, tmp_path) -> None:
        """The owner died between taking the lock and the first flush of its record."""
        root = tmp_path / "root"
        lock = self._file(root, f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock", age_s=sub._OWNER_TMP_MIN_AGE_S + 1)
        assert self._sweep(root).stale_tmp_removed == [str(lock)] and not lock.exists()

    def test_a_held_lock_with_no_record_beside_it_stays(self, tmp_path) -> None:
        root = tmp_path / "root"
        lock = self._file(root, f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock", age_s=sub._OWNER_TMP_MIN_AGE_S + 1)
        with _holding(lock):
            assert self._sweep(root).stale_tmp_removed == [] and lock.exists()

    def test_a_lock_with_its_record_beside_it_is_not_debris(self, tmp_path) -> None:
        root = tmp_path / "root"
        rec, lock, pgdata = _record(root, owner_pid=os.getpid())
        os.utime(lock, (self._NOW - 10_000, self._NOW - 10_000))
        with _holding(lock):
            result = self._sweep(root)
        assert result.stale_tmp_removed == [] and lock.exists() and rec.exists() and pgdata.is_dir()

    def test_a_symlink_with_the_lock_tmp_name_is_neither_followed_nor_removed(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        link = root / f"{sub._OWNER_RECORD_PREFIX}77-0a1b2c3d.lock.tmp"
        link.symlink_to(victim)
        os.utime(link, (self._NOW - 10_000,) * 2, follow_symlinks=False)
        assert self._sweep(root).stale_tmp_removed == [] and link.is_symlink() and victim.read_text() == "keep"


# ── A dir kept as live for hours is reported, not reaped ──────────────────────────────


@needs_flock
class TestLongKeptIsReported:
    def _events(self, logs) -> list[dict]:
        return [e for e in logs if e.get("event") == "test_substrate_kept_long"]

    def test_a_record_whose_lock_is_held_for_hours_is_logged_and_kept(self, tmp_path) -> None:
        rec, lock, pgdata = _record(tmp_path / "root", owner_pid=_dead_pid())
        old = time.time() - sub._LONG_KEPT_AGE_S - 60
        os.utime(rec, (old, old))
        with _holding(lock), capture_logs() as logs:
            result = sub.SweepResult()
            sub._sweep_owner_records(tmp_path / "root", result)
        (event,) = self._events(logs)
        assert event["path"] == str(rec) and event["state"] == "live" and event["bead"] == "nexus-fuz9t"
        assert event["age_s"] > sub._LONG_KEPT_AGE_S
        assert result.live_untouched == [str(rec)] and result.reaped == [] and pgdata.is_dir(), "report only"

    def test_a_record_held_for_minutes_is_not_logged(self, tmp_path) -> None:
        rec, lock, _pgdata = _record(tmp_path / "root", owner_pid=_dead_pid())
        with _holding(lock), capture_logs() as logs:
            sub._sweep_owner_records(tmp_path / "root", sub.SweepResult())
        assert self._events(logs) == []

    def test_a_cluster_whose_lock_is_held_for_hours_is_logged_and_kept(self, tmp_path) -> None:
        d = _cluster(tmp_path, owner_pid=_dead_pid())
        old = time.time() - sub._LONG_KEPT_AGE_S - 60
        os.utime(d, (old, old))
        with _holding(d / sub._OWNER_LOCK_FILENAME), capture_logs() as logs:
            result = sweep_stale_substrate_clusters(tmp_root=tmp_path)
        (event,) = self._events(logs)
        assert event["path"] == str(d) and event["state"] == "live"
        assert str(d) in result.live_untouched and d.is_dir(), "report only"
