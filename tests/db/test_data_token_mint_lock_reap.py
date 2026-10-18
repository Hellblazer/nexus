# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaping and attribution of ``data_token_mint_lock.*`` files.

nexus-ndfc1: one lock file is created per (host:port, tenant) minted
against and nothing removed them (82 piled up on one laptop, one per
random-port engine). Stale ones are now reaped inline at mint time.

nexus-28bzt: the writer of the stray files was never identified, so the
first creation of a lock file logs one attributing event.

Real files in ``tmp_path``, the module's own flock primitive, no mocks of
the lock. Staleness is set with ``os.utime`` against real time (the reaper
reads filesystem mtimes, not the manager's injectable clock).
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from nexus._locking import lock_fd, unlock_fd
from nexus.db import data_token
from nexus.db.data_token import (
    _MINT_LOCK_PREFIX,
    _MINT_LOCK_REAP_MAX_PER_MINT,
    _MINT_LOCK_REOPEN_MAX,
    _MINT_LOCK_STALE_AFTER_S,
    DataTokenManager,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _reap_stale_mint_locks,
)
from tests._module_seam import setattr_in

BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"
OLD = time.time() - _MINT_LOCK_STALE_AFTER_S - 3600.0
RECENT = time.time() - 3600.0


def _hex(n: int) -> str:
    return f"{n:064x}"


def _lock_file(d: Path, n: int, *, mtime: float, prefix: str = _MINT_LOCK_PREFIX) -> Path:
    p = d / f"{prefix}{_hex(n)}"
    p.write_bytes(b"")
    os.utime(p, (mtime, mtime))
    return p


def _poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok-SECRET", "expires_in_seconds": 300}, {}


def _manager(config_dir: Path) -> DataTokenManager:
    return DataTokenManager(
        poster=_poster,
        mint_credential=lambda: "mintcred",
        mint_tenant=lambda: "",
        config_dir=config_dir,
        sleep=lambda s: None,
    )


@pytest.fixture(autouse=True)
def debug_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


# ── reaping (nexus-ndfc1) ────────────────────────────────────────────────


def test_stale_unheld_lock_is_removed_at_mint_time(tmp_path: Path) -> None:
    stale = _lock_file(tmp_path, 1, mtime=OLD)
    assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert not stale.exists()
    # the mint's own lock is untouched by the reap
    assert _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path).exists()


def test_recent_lock_is_kept(tmp_path: Path) -> None:
    recent = _lock_file(tmp_path, 1, mtime=RECENT)
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert recent.exists()


def test_held_stale_lock_is_never_removed(tmp_path: Path) -> None:
    """A lock another open file description holds is skipped even when old;
    its unheld stale neighbour is still removed, so the skip is per file."""
    held = _lock_file(tmp_path, 1, mtime=OLD)
    free = _lock_file(tmp_path, 2, mtime=OLD)
    fd = os.open(str(held), os.O_WRONLY)
    lock_fd(fd, blocking=True)
    try:
        assert _reap_stale_mint_locks(tmp_path) == 1
        assert held.exists()
        assert not free.exists()
    finally:
        unlock_fd(fd)
        os.close(fd)
    # released: now it is reapable, proving the earlier skip was the lock
    assert _reap_stale_mint_locks(tmp_path) == 1
    assert not held.exists()


def test_staleness_threshold_is_one_day(tmp_path: Path) -> None:
    """S3: safety rests on the flock and inode checks, not the age, so a file
    unused for two days goes and one unused for twelve hours stays."""
    now = time.time()
    two_days = _lock_file(tmp_path, 1, mtime=now - 2 * 86400)
    half_day = _lock_file(tmp_path, 2, mtime=now - 43200)
    assert _reap_stale_mint_locks(tmp_path) == 1
    assert not two_days.exists() and half_day.exists()


def test_removal_is_bounded_per_mint(tmp_path: Path) -> None:
    extra = 5
    files = [
        _lock_file(tmp_path, n, mtime=OLD)
        for n in range(1, _MINT_LOCK_REAP_MAX_PER_MINT + extra + 1)
    ]
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert sum(1 for f in files if f.exists()) == extra
    # a later mint drains the rest
    _manager(tmp_path).bearer_for(BASE_URL, "other-tenant")
    assert not any(f.exists() for f in files)


def test_only_exact_mint_lock_names_are_touched(tmp_path: Path) -> None:
    keepers = [
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)[:-1]}",  # 63 hex
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}0",  # 65 hex
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}.bak",
        tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1).upper().replace('0', 'A')}",
        tmp_path / f"x{_MINT_LOCK_PREFIX}{_hex(1)}",
        tmp_path / "config.yml",
        tmp_path / f"data_token_lease.{_hex(1)}",  # the lease family
    ]
    for p in keepers:
        p.write_bytes(b"")
        os.utime(p, (OLD, OLD))
    dir_named_like_lock = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(2)}"
    dir_named_like_lock.mkdir()
    os.utime(dir_named_like_lock, (OLD, OLD))
    victim = _lock_file(tmp_path, 3, mtime=OLD)

    assert _reap_stale_mint_locks(tmp_path) == 1
    assert not victim.exists()
    assert all(p.exists() for p in keepers)
    assert dir_named_like_lock.is_dir()


def test_symlink_named_like_a_lock_is_not_followed_or_removed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"")
    os.utime(target, (OLD, OLD))
    link = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}"
    link.symlink_to(target)
    assert _reap_stale_mint_locks(tmp_path) == 0
    assert link.is_symlink() and target.exists()


def test_the_mints_own_lock_is_touched_and_never_reaped(tmp_path: Path) -> None:
    """mtime means 'last used': a reused lock is refreshed, so an endpoint
    that mints every few days keeps its file; and the caller's own path is
    never a candidate even if stale."""
    own = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    own.write_bytes(b"")
    os.utime(own, (OLD, OLD))
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert own.exists()
    assert own.stat().st_mtime > time.time() - 60

    os.utime(own, (OLD, OLD))
    assert _reap_stale_mint_locks(tmp_path, keep=own) == 0
    assert own.exists()


def test_scan_failure_never_reaches_the_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _lock_file(tmp_path, 1, mtime=OLD)
    real_scandir = os.scandir

    def boom(path: Any = ".") -> Any:
        if Path(path) == tmp_path:
            raise PermissionError("denied")
        return real_scandir(path)

    setattr_in(monkeypatch, data_token, "os.scandir", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert "data_token_mint_lock_reap_scan_failed" in [e["event"] for e in captured]
    assert [e["log_level"] for e in captured if e["event"].startswith("data_token_mint_lock_reap")] == ["debug"]


def test_unlink_failure_never_reaches_the_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _lock_file(tmp_path, 1, mtime=OLD)
    real_unlink = os.unlink

    def boom(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path).name.startswith(_MINT_LOCK_PREFIX):
            raise PermissionError("denied")
        real_unlink(path, *a, **kw)

    setattr_in(monkeypatch, data_token, "os.unlink", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert stale.exists()
    failed = [e for e in captured if e["event"] == "data_token_mint_lock_reap_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "debug"


def test_lease_family_is_not_reaped_by_the_mint(tmp_path: Path) -> None:
    """Pins the scope of THIS fix, not an impossibility: the lease family is
    deferred to a sibling bead (the same-key mint lock is a lock to probe and
    an expired lease is dead by definition), so for now the mint-time reap
    leaves old lease files alone."""
    lease = tmp_path / f"data_token_lease.{_hex(7)}"
    lease.write_bytes(b"{}")
    os.utime(lease, (OLD, OLD))
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert lease.exists()
    assert _data_token_lease_path(BASE_URL, TENANT, tmp_path).exists()


# ── reaper safety (nexus-ndfc1 review: B1, H, W, T) ──────────────────────


class _Entry:
    """A DirEntry stand-in that the scan accepts: a regular, old file."""

    def __init__(self, path: Path, mtime: float = OLD) -> None:
        self.name, self.path, self._mtime = path.name, str(path), mtime

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return True

    def stat(self, *, follow_symlinks: bool = True) -> Any:
        return SimpleNamespace(st_mtime=self._mtime)


class _FakeScandir:
    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries

    def __enter__(self) -> "_FakeScandir":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Iterator[_Entry]:
        return iter(self._entries)


def _scan_returns(monkeypatch: pytest.MonkeyPatch, *paths: Path) -> None:
    """Make the reaper's scan return *paths* as stale regular files whatever
    they really are: the swap-after-scan races, deterministically."""
    entries = [_Entry(p) for p in paths]
    setattr_in(monkeypatch, data_token, "os.scandir", lambda path=".": _FakeScandir(entries))


def _wrap_lock(monkeypatch: pytest.MonkeyPatch, before: Any = None) -> list[int]:
    """Record every non-blocking lock attempt the module makes; run
    ``before(fd)`` ahead of the real lock. Returns the attempt log."""
    from nexus import _locking

    real = _locking.lock_fd
    calls: list[int] = []

    def wrapped(fd: int, *, blocking: bool) -> None:
        calls.append(fd)
        if before is not None:
            before(fd)
        real(fd, blocking=blocking)

    setattr_in(monkeypatch, data_token, "_locking.lock_fd", wrapped)
    return calls


def test_mtime_is_reread_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale at the scan, touched by a minter before the reaper's lock lands:
    the re-read under the lock keeps it."""
    f = _lock_file(tmp_path, 1, mtime=OLD)
    _wrap_lock(monkeypatch, before=lambda fd: os.utime(f))
    assert _reap_stale_mint_locks(tmp_path) == 0
    assert f.exists()


def test_reaper_does_not_unlink_a_file_recreated_after_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: another reaper removed the file the reaper opened and a minter
    re-created the path. The reaper's lock on the old inode succeeds and its
    mtime is old, but the path is not that inode: unlinking by path would
    remove the minter's live lock."""
    f = _lock_file(tmp_path, 1, mtime=OLD)

    def swap(fd: int) -> None:
        f.unlink()
        f.write_bytes(b"")  # the minter's fresh file, same name, new inode

    _wrap_lock(monkeypatch, before=swap)
    assert _reap_stale_mint_locks(tmp_path) == 0
    assert f.exists()


def test_reaper_never_locks_a_symlink_the_scan_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O_NOFOLLOW at the open, independent of the scan's is_file guard (the
    fake scan lets the link through): no lock is even attempted on the
    link's target."""
    target = tmp_path / "target"
    target.write_bytes(b"")
    os.utime(target, (OLD, OLD))
    link = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}"
    link.symlink_to(target)
    _scan_returns(monkeypatch, link)
    calls = _wrap_lock(monkeypatch)
    assert _reap_stale_mint_locks(tmp_path) == 0
    assert calls == []
    assert link.is_symlink() and target.exists()


def test_total_reap_attempts_are_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At most 4 * max_removals candidates are tried per call, however many
    held-or-failing ones the directory has."""
    cap = _MINT_LOCK_REAP_MAX_PER_MINT * 4
    held_fds = []
    for n in range(1, cap + 6):
        fd = os.open(str(_lock_file(tmp_path, n, mtime=OLD)), os.O_WRONLY)
        lock_fd(fd, blocking=True)
        held_fds.append(fd)
    calls = _wrap_lock(monkeypatch)
    try:
        assert _reap_stale_mint_locks(tmp_path) == 0
    finally:
        for fd in held_fds:
            os.close(fd)
    assert len(calls) == cap


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX fifos")
def test_reaper_does_not_block_on_a_fifo_swapped_in_after_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H: a name swapped to a FIFO between scan and open must not hang the
    mint (a blocking write-open waits for a reader for ever)."""
    fifo = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}"
    os.mkfifo(fifo)
    _scan_returns(monkeypatch, fifo)
    result: list[int] = []
    t = threading.Thread(target=lambda: result.append(_reap_stale_mint_locks(tmp_path)), daemon=True)
    t.start()
    t.join(5)
    hung = t.is_alive()
    if hung:  # release the blocked open so the thread ends
        os.close(os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK))
        t.join(5)
    assert not hung, "reaper blocked opening a FIFO"
    assert result == [0] and fifo.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX fifos")
def test_reaper_skips_a_non_regular_file_before_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H: with a reader attached the write-open of a FIFO succeeds; the
    fstat S_ISREG check must still stop the reaper before it locks."""
    fifo = tmp_path / f"{_MINT_LOCK_PREFIX}{_hex(1)}"
    os.mkfifo(fifo)
    reader = os.open(str(fifo), os.O_RDONLY | os.O_NONBLOCK)
    try:
        _scan_returns(monkeypatch, fifo)
        calls = _wrap_lock(monkeypatch)
        assert _reap_stale_mint_locks(tmp_path) == 0
        assert calls == []
        assert stat.S_ISFIFO(os.stat(fifo).st_mode)
    finally:
        os.close(reader)


def test_reap_scan_is_skipped_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W: an open handle blocks unlink there, so the scan does not run, and
    the skip is logged once per process, not once per mint."""
    stale = _lock_file(tmp_path, 1, mtime=OLD)
    scans: list[Any] = []
    real_scandir = os.scandir

    def spy(path: Any = ".") -> Any:
        scans.append(path)
        return real_scandir(path)

    setattr_in(monkeypatch, data_token, "os.scandir", spy)
    setattr_in(monkeypatch, data_token, "sys.platform", "win32")
    monkeypatch.setattr(data_token, "_reap_skip_logged", False)
    with structlog.testing.capture_logs() as captured:
        assert _reap_stale_mint_locks(tmp_path) == 0
        assert _reap_stale_mint_locks(tmp_path) == 0
    assert scans == []
    assert stale.exists()
    skipped = [e for e in captured if e["event"] == "data_token_mint_lock_reap_skipped"]
    assert len(skipped) == 1 and skipped[0]["log_level"] == "debug"


# ── the minter's side (nexus-ndfc1 review B2, H) ─────────────────────────


def test_minter_reopens_when_its_file_was_unlinked_before_it_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2, single-process: the file the minter opened is unlinked before its
    lock lands. It must not mint under a lock on the dead inode; it reopens
    the path (creating a fresh file) and locks that."""
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock_path.write_bytes(b"")
    unlinked = []

    def unlink_once(fd: int) -> None:
        if not unlinked:
            unlinked.append(os.fstat(fd).st_ino)
            lock_path.unlink()

    _wrap_lock(monkeypatch, before=unlink_once)
    opens: list[Path] = []
    real_open = data_token._open_mint_lock

    def counting(p: Path, describe: Any = None) -> Any:
        opens.append(p)
        return real_open(p, describe)

    monkeypatch.setattr(data_token, "_open_mint_lock", counting)
    assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert len(opens) == 2
    assert lock_path.exists() and lock_path.stat().st_ino != unlinked[0]


def test_minter_reopen_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: a path that never names the locked inode costs a bounded number of
    reopens, then the mint goes ahead (one extra mint, never a hang)."""
    opens: list[Path] = []
    real_open = data_token._open_mint_lock

    def counting(p: Path, describe: Any = None) -> Any:
        opens.append(p)
        assert len(opens) < _MINT_LOCK_REOPEN_MAX + 10, "reopen loop is unbounded"
        return real_open(p, describe)

    monkeypatch.setattr(data_token, "_open_mint_lock", counting)
    monkeypatch.setattr(data_token, "_fd_names_path", lambda fd, path: False)
    assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-SECRET"
    assert len(opens) == 1 + _MINT_LOCK_REOPEN_MAX


def test_the_mint_touches_the_inode_it_opened_not_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2: the refresh goes through the descriptor, so a path swapped after
    the open does not receive the touch."""
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock_path.write_bytes(b"")
    os.utime(lock_path, (OLD, OLD))
    real_open = os.open

    def swapping_open(p: Any, flags: int, mode: int = 0o777) -> int:
        fd = real_open(p, flags, mode)
        if str(p) == str(lock_path) and not (flags & os.O_EXCL):
            lock_path.unlink()
            lock_path.write_bytes(b"")  # a new inode appears under the name
            os.utime(lock_path, (OLD, OLD))
        return fd

    setattr_in(monkeypatch, data_token, "os.open", swapping_open)
    fd, record = data_token._open_mint_lock(lock_path)
    try:
        assert record is None
        assert os.fstat(fd).st_mtime > time.time() - 60  # the opened inode
        assert lock_path.stat().st_mtime == pytest.approx(OLD, abs=2)  # not the path
    finally:
        os.close(fd)


def test_a_base_exception_during_the_reap_does_not_leak_the_lock_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H: the reap sits inside the try that closes the lock descriptor."""
    opened: list[int] = []
    real = data_token._open_mint_lock_logged

    def recording(*a: Any, **k: Any) -> int:
        fd = real(*a, **k)
        opened.append(fd)
        return fd

    def interrupted(*a: Any, **k: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", recording)
    monkeypatch.setattr(data_token, "_reap_stale_mint_locks", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


# ── attribution (nexus-28bzt) ────────────────────────────────────────────


def test_first_creation_logs_one_attributing_event_and_writes_it_to_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock file's first line carries the same attribution as the info
    event, so a stray file names its writer even where info logs are dropped
    (CLI mode)."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setattr("nexus.session._command_name_of", lambda pid: "fake-parent")
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    created = [e for e in captured if e["event"] == "data_token_mint_lock_created"]
    assert len(created) == 1
    ev = created[0]
    assert ev["log_level"] == "info"
    assert ev["pid"] == os.getpid()
    assert ev["ppid"] == os.getppid()
    assert ev["parent_name"] == "fake-parent"
    assert ev["argv0"] == sys.argv[0]
    assert ev["pytest_current_test"] == os.environ["PYTEST_CURRENT_TEST"]
    assert ev["nexus_config_dir"] == str(tmp_path / "cfg")
    assert ev["cwd"] == os.getcwd()
    assert ev["home_matches_pwd_home"] is False  # HOME was moved off the real home
    assert ev["endpoint"] == "127.0.0.1:9999"
    assert ev["tenant"] == TENANT
    assert ev["ts"].endswith("Z")
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    assert ev["lock_file"] == lock_path.name

    lines = lock_path.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {k: v for k, v in ev.items() if k not in ("event", "log_level")}


def test_no_token_or_credential_reaches_the_event_or_the_lock_file(tmp_path: Path) -> None:
    """The mint path carries a credential, a minted token and a base URL with
    a token in its userinfo and query. None of them may reach the creation
    event or the lock file (the endpoint is host:port, not the netloc)."""
    url = "http://user:tok-URLSECRET@127.0.0.1:9999/p?token=tok-QUERYSECRET"
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(url, TENANT) == "tok-SECRET"
    ev = next(e for e in captured if e["event"] == "data_token_mint_lock_created")
    assert ev["endpoint"] == "127.0.0.1:9999"
    lock_file = next(tmp_path.glob(f"{_MINT_LOCK_PREFIX}*"))
    surfaces = repr(ev) + lock_file.read_text()
    for secret in ("tok-SECRET", "mintcred", "tok-URLSECRET", "tok-QUERYSECRET"):
        assert secret not in surfaces


def test_reused_lock_logs_no_creation_event(tmp_path: Path) -> None:
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    # a cold process: new manager, lease gone, lock file still there
    _data_token_lease_path(BASE_URL, TENANT, tmp_path).unlink()
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert "data_token_mint_lock_created" not in [e["event"] for e in captured]
    assert "data_token_minted" in [e["event"] for e in captured]
    # the reuse does not append a second attribution line
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    assert len(lock_path.read_text().splitlines()) == 1


def test_home_matching_the_real_home_is_reported_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pwd = pytest.importorskip("pwd")
    monkeypatch.setenv("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    ev = next(e for e in captured if e["event"] == "data_token_mint_lock_created")
    assert ev["home_matches_pwd_home"] is True
