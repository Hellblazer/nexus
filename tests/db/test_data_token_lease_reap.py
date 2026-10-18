# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reaping of expired ``data_token_lease.*`` files (nexus-4gmw8).

One lease file (0600, it holds a data token) is written per (host:port,
tenant) and nothing removed a lease for an endpoint that never came back. The
mint now removes expired leases under the same-key mint lock, with the guards
of the mint-lock reaper (nexus-ndfc1): exact name, regular file, no symlink
follow, no FIFO block, inode identity before unlink, bounded per mint, never
the caller's own key, skipped on win32, failures a debug log.

Real files in ``tmp_path``, the module's own flock primitive, no mock of the
lock. A lease's age is its ``expires_at`` against the wall clock the manager
reads; its file mtime is set with ``os.utime``.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus import _locking
from nexus._locking import lock_fd, unlock_fd
from nexus.db import data_token
from nexus.db.data_token import (
    _DATA_TOKEN_LEASE_PREFIX,
    _LEASE_EXPIRED_MARGIN_S,
    _LEASE_REAP_MAX_PER_MINT,
    _MINT_LOCK_PREFIX,
    _MINT_LOCK_STALE_AFTER_S,
    DataTokenManager,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _fd_names_path,
    _open_plain_file,
    _reap_expired_leases,
    _reap_one_lease,
)
from tests._module_seam import setattr_in

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock/unlink semantics")

BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"
SECRET = "tok-LEASE-SECRET"


def _hex(n: int) -> str:
    return f"{n:064x}"


def _lease(
    d: Path,
    n: int,
    *,
    expires_in: float,
    mtime_age: float | None = None,
    fmt: int = 1,
    now: float | None = None,
) -> Path:
    """A well-formed lease for key ``n`` expiring ``expires_in`` seconds from
    *now* (negative: already expired). mtime defaults to an hour ago."""
    now = time.time() if now is None else now
    p = d / f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(n)}"
    p.write_text(json.dumps({
        "format_version": fmt, "token": SECRET, "tenant": "t", "base_url_digest": _hex(n),
        "expires_at": now + expires_in, "ttl_seconds": 3600.0, "minted_by_pid": 1,
    }))
    age = 3600.0 if mtime_age is None else mtime_age
    os.utime(p, (now - age, now - age))
    return p


def _raw_lease(d: Path, n: int, content: bytes, *, mtime_age: float) -> Path:
    p = d / f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(n)}"
    p.write_bytes(content)
    t = time.time() - mtime_age
    os.utime(p, (t, t))
    return p


def _lock_of(lease: Path) -> Path:
    return lease.with_name(_MINT_LOCK_PREFIX + lease.name[len(_DATA_TOKEN_LEASE_PREFIX):])


def _poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok-minted", "expires_in_seconds": 300}, {}


def _manager(config_dir: Path, **kw: Any) -> DataTokenManager:
    return DataTokenManager(
        poster=_poster,
        mint_credential=lambda: "mintcred",
        mint_tenant=lambda: "",
        config_dir=config_dir,
        sleep=lambda s: None,
        **kw,
    )


@pytest.fixture(autouse=True)
def debug_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


# ── what is reaped ───────────────────────────────────────────────────────


def test_expired_lease_is_removed_at_mint_time_and_leaves_no_lock_behind(tmp_path: Path) -> None:
    """The lease has no lock file (the mint-lock reaper took it): the reap
    makes one to hold, and removes it again."""
    dead = _lease(tmp_path, 1, expires_in=-3600)
    assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert not dead.exists()
    assert not _lock_of(dead).exists()
    assert _data_token_lease_path(BASE_URL, TENANT, tmp_path).exists()  # the mint's own lease


def test_an_existing_same_key_lock_is_kept_and_not_touched(tmp_path: Path) -> None:
    """The lease reaper must not refresh a stale lock's mtime: that would keep
    it from the mint-lock reaper for ever."""
    dead = _lease(tmp_path, 1, expires_in=-3600)
    lock = _lock_of(dead)
    lock.write_bytes(b"")
    old = time.time() - _MINT_LOCK_STALE_AFTER_S - 3600
    os.utime(lock, (old, old))
    assert _reap_expired_leases(tmp_path, now=time.time()) == 1
    assert not dead.exists()
    assert lock.exists()
    assert abs(lock.stat().st_mtime - old) < 1


def test_live_lease_is_kept(tmp_path: Path) -> None:
    live = _lease(tmp_path, 1, expires_in=1800)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert live.exists()
    assert not _lock_of(live).exists()  # a live lease costs a read, not a lock file


def test_margin_is_documented_and_applied(tmp_path: Path) -> None:
    """Past expires_at by less than the margin: kept. By more: removed."""
    now = time.time()
    inside = _lease(tmp_path, 1, expires_in=-(_LEASE_EXPIRED_MARGIN_S - 60), now=now)
    outside = _lease(tmp_path, 2, expires_in=-(_LEASE_EXPIRED_MARGIN_S + 60), now=now)
    assert _reap_expired_leases(tmp_path, now=now) == 1
    assert inside.exists()
    assert not outside.exists()


def test_the_managers_wall_clock_decides_not_the_real_one(tmp_path: Path) -> None:
    """``expires_at`` is read against the clock the reader uses. A lease that
    is expired by the real clock but live by the manager's injected one stays."""
    fake_now = 1_000_000.0
    lease = _lease(tmp_path, 1, expires_in=3600, now=fake_now)  # expires at fake_now + 3600: long past in real time
    _manager(tmp_path, wall_clock=lambda: fake_now).bearer_for(BASE_URL, TENANT)
    assert lease.exists()


def test_only_exact_lease_names_are_touched(tmp_path: Path) -> None:
    dead = _lease(tmp_path, 3, expires_in=-3600)
    keepers: list[Path] = []
    for name in (
        f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)[:-1]}",  # 63 hex
        f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}0",  # 65 hex
        f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}.bak",
        f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}.123.deadbeef.tmp",  # a publish in flight
        f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1).upper().replace('0', 'A')}",
        f"x{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}",
        f"{_MINT_LOCK_PREFIX}{_hex(1)}",  # the other family
    ):
        p = tmp_path / name
        p.write_text(json.dumps({"format_version": 1, "expires_at": 1.0}))
        os.utime(p, (1.0, 1.0))
        keepers.append(p)
    a_dir = tmp_path / f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(2)}"
    a_dir.mkdir()
    assert _reap_expired_leases(tmp_path, now=time.time()) == 1
    assert not dead.exists()
    assert all(p.exists() for p in keepers)
    assert a_dir.is_dir()


def test_symlink_named_like_a_lease_is_not_followed_or_removed(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text(json.dumps({"format_version": 1, "expires_at": 1.0}))
    link = tmp_path / f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}"
    link.symlink_to(target)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert link.is_symlink() and target.exists()


def test_the_callers_own_lease_is_never_a_candidate(tmp_path: Path) -> None:
    own = _lease(tmp_path, 1, expires_in=-3600)
    assert _reap_expired_leases(tmp_path, now=time.time(), keep=own) == 0
    assert own.exists()


def test_removal_is_bounded_per_mint_and_a_later_mint_drains_the_rest(tmp_path: Path) -> None:
    extra = 3
    files = [_lease(tmp_path, i, expires_in=-3600) for i in range(1, _LEASE_REAP_MAX_PER_MINT + extra + 1)]
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert sum(1 for f in files if f.exists()) == extra
    _manager(tmp_path).bearer_for(BASE_URL, "other-tenant")
    assert not any(f.exists() for f in files)


def test_examination_is_capped_and_oldest_write_goes_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More live leases than the examination cap, and one dead lease that is
    the oldest write: it is examined first (live leases cannot starve it) and
    no more than ``4 * max`` files are looked at."""
    now = time.time()
    dead = _lease(tmp_path, 1, expires_in=-3600, mtime_age=7200, now=now)
    for i in range(2, 4 * _LEASE_REAP_MAX_PER_MINT + 10):
        _lease(tmp_path, i, expires_in=1800, mtime_age=600, now=now)
    examined: list[Path] = []
    real = data_token._reap_one_lease

    def spy(lease_path: Path, config_dir: Path, now: float) -> bool:
        examined.append(lease_path)
        return real(lease_path, config_dir, now)

    monkeypatch.setattr(data_token, "_reap_one_lease", spy)
    assert _reap_expired_leases(tmp_path, now=now) == 1
    assert not dead.exists()
    assert examined[0] == dead
    assert len(examined) == 4 * _LEASE_REAP_MAX_PER_MINT


# ── unreadable leases ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"{}",
        b"[]",
        b'{"format_version": 1}',
        b'{"format_version": 1, "expires_at": "soon"}',
        b'{"format_version": 1, "expires_at": NaN}',
        b'{"format_version": 2, "expires_at": 1.0}',  # a newer writer's file: expiry may mean something else
        b"\xff\xfe\x00",
        b"x" * (data_token._LEASE_READ_MAX_BYTES + 5),
    ],
)
def test_an_unparseable_lease_is_reaped_only_past_the_mint_lock_staleness_bound(
    tmp_path: Path, content: bytes,
) -> None:
    recent = _raw_lease(tmp_path, 1, content, mtime_age=_MINT_LOCK_STALE_AFTER_S - 3600)
    old = _raw_lease(tmp_path, 2, content, mtime_age=_MINT_LOCK_STALE_AFTER_S + 3600)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 1
    assert recent.exists()
    assert not old.exists()


# ── the same-key lock ────────────────────────────────────────────────────


def test_a_held_same_key_lock_protects_the_lease_until_released(tmp_path: Path) -> None:
    dead = _lease(tmp_path, 1, expires_in=-3600)
    lock = _lock_of(dead)
    lock.write_bytes(b"")
    fd = os.open(str(lock), os.O_WRONLY)
    try:
        lock_fd(fd, blocking=False)
        assert _reap_expired_leases(tmp_path, now=time.time()) == 0
        assert dead.exists()
        unlock_fd(fd)
        assert _reap_expired_leases(tmp_path, now=time.time()) == 1
        assert not dead.exists()
    finally:
        os.close(fd)


def test_the_same_key_lock_is_held_while_the_lease_is_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = _lease(tmp_path, 1, expires_in=-3600)
    lock = _lock_of(dead)
    lock.write_bytes(b"")
    real_unlink = os.unlink
    held_at_unlink: list[bool] = []

    def spy(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == dead:
            probe = os.open(str(lock), os.O_WRONLY)
            try:
                try:
                    lock_fd(probe, blocking=False)
                except BlockingIOError:
                    held_at_unlink.append(True)
                else:
                    held_at_unlink.append(False)
                    unlock_fd(probe)
            finally:
                os.close(probe)
        real_unlink(path, *a, **kw)

    setattr_in(monkeypatch, data_token, "os.unlink", spy)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 1
    assert held_at_unlink == [True]


def _wrap_lock(monkeypatch: pytest.MonkeyPatch, before: Any) -> list[int]:
    real = _locking.lock_fd
    calls: list[int] = []

    def wrapped(fd: int, *, blocking: bool) -> None:
        calls.append(fd)
        before(fd)
        real(fd, blocking=blocking)

    setattr_in(monkeypatch, data_token, "_locking.lock_fd", wrapped)
    return calls


def _republish_live(lease: Path) -> None:
    """What a minter does under the lock: a fresh lease at the same path."""
    tmp = lease.with_name(lease.name + ".new.tmp")
    tmp.write_text(json.dumps({
        "format_version": 1, "token": "tok-fresh", "tenant": "t", "base_url_digest": "x",
        "expires_at": time.time() + 3000, "ttl_seconds": 3600.0,
    }))
    os.replace(tmp, lease)


def test_the_lease_is_read_again_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cheap look says dead; a minter republishes before the reaper gets
    the lock. The fresh lease must survive."""
    dead = _lease(tmp_path, 1, expires_in=-3600)
    _wrap_lock(monkeypatch, lambda fd: _republish_live(dead))
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert "tok-fresh" in dead.read_text()


def test_the_reaper_does_not_unlink_a_lease_replaced_after_it_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity: the path names a different inode than the one read as dead
    (an unlocked publisher, e.g. the no-lock fallback mint)."""
    dead = _lease(tmp_path, 1, expires_in=-3600)
    real = data_token._lease_is_dead
    calls: list[int] = []

    def swap_after_second_read(raw: bytes, mtime: float, now: float) -> bool:
        verdict = real(raw, mtime, now)
        calls.append(1)
        if len(calls) == 2:  # the read under the lock: it said dead, now the path changes
            _republish_live(dead)
        return verdict

    monkeypatch.setattr(data_token, "_lease_is_dead", swap_after_second_read)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert "tok-fresh" in dead.read_text()


def test_documented_residual_a_lockless_publish_in_the_check_to_unlink_gap_costs_one_remint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DOCUMENTED RESIDUAL, not a guarantee (nexus-fcwbd).

    The guarded mint publishes under the same-key lock; the refresh path in
    ``bearer_for`` and the unguarded mint do not. A lease they replace after
    the reaper's inode check and before its ``os.unlink`` is unlinked with
    the dead one. This pins what the cost is: the fresh lease is lost, the
    reap reports it removed, and the next cold process simply mints once
    more. No error, no stale token served. If this test starts failing because
    the fresh lease survives, the residual was closed (nexus-fcwbd): delete
    this test and the residual wording in the docstring, CHANGELOG entry and
    the comment at the re-read.
    """
    key_lease = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    key_lease.write_text(json.dumps({
        "format_version": data_token._LEASE_FORMAT_VERSION, "token": "tok-dead", "tenant": TENANT,
        "base_url_digest": data_token._lease_key(BASE_URL, TENANT),
        "expires_at": time.time() - 3600, "ttl_seconds": 3600.0,
    }))
    old = time.time() - 7200
    os.utime(key_lease, (old, old))

    lockless = _manager(tmp_path)  # stands in for a refreshing process holding no lock
    fresh = data_token._CachedToken(
        token="tok-fresh", minted_at=lockless._clock(), expires_at=lockless._clock() + 3000, ttl_seconds=3600.0,
    )
    real_unlink = os.unlink
    published: list[bool] = []

    def publish_then_unlink(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == key_lease and not published:
            published.append(True)
            lockless._write_lease(BASE_URL, TENANT, fresh)  # the refresh path's publish, no lock
        real_unlink(path, *a, **kw)

    setattr_in(monkeypatch, data_token, "os.unlink", publish_then_unlink)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 1
    assert published == [True]
    assert not key_lease.exists()  # the fresh lease went with the dead one

    posts: list[str] = []

    def counting(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        posts.append(url)
        return 200, {"data_token": "tok-reminted", "expires_in_seconds": 300}, {}

    cold = DataTokenManager(
        poster=counting, mint_credential=lambda: "mintcred", mint_tenant=lambda: "",
        config_dir=tmp_path, sleep=lambda s: None,
    )
    assert cold.bearer_for(BASE_URL, TENANT) == "tok-reminted"  # one extra mint, not an error
    assert len(posts) == 1


def test_the_reaper_docstring_states_the_lockless_publishers_and_the_cost() -> None:
    """The safety argument is prose, and it was once wrong ("a minter publishes
    only under that lock"). Pin the true statement: which paths hold the lock,
    which do not, and what the gap costs."""
    doc = " ".join((_reap_expired_leases.__doc__ or "").split())
    assert "guarded mint publishes its lease only while holding that lock" in doc
    assert "refresh path" in doc and "WITHOUT" in doc
    assert "one extra mint" in doc and "nexus-fcwbd" in doc
    assert "a minter publishes its lease only while holding" not in doc


def test_a_lock_file_replaced_before_the_reaper_locks_it_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock on an unlinked inode serializes nothing: the lease stays."""
    dead = _lease(tmp_path, 1, expires_in=-3600)
    lock = _lock_of(dead)
    lock.write_bytes(b"")

    def replace_lock(fd: int) -> None:
        lock.unlink()
        lock.write_bytes(b"")

    _wrap_lock(monkeypatch, replace_lock)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert dead.exists()


def test_a_lock_created_for_the_reap_is_removed_even_when_the_lease_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = _lease(tmp_path, 1, expires_in=-3600)
    _wrap_lock(monkeypatch, lambda fd: _republish_live(dead))
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert not _lock_of(dead).exists()


# ── safety against odd files ─────────────────────────────────────────────


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX fifos")
def test_a_fifo_swapped_in_for_a_lease_never_blocks_the_reaper(tmp_path: Path) -> None:
    fifo = tmp_path / f"{_DATA_TOKEN_LEASE_PREFIX}{_hex(1)}"
    os.mkfifo(fifo)
    outcome: list[str] = []

    def attempt() -> None:
        try:
            _reap_one_lease(fifo, tmp_path, time.time())
        except OSError:
            outcome.append("refused")

    t = threading.Thread(target=attempt, daemon=True)
    t.start()
    t.join(5)
    hung = t.is_alive()
    if hung:  # release the blocked open so the thread ends
        os.close(os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK))
        t.join(5)
    assert not hung, "reaper blocked opening a FIFO"
    assert outcome == ["refused"]
    assert fifo.exists()


def test_a_symlink_is_refused_by_the_plain_file_open(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_text("x")
    link = tmp_path / "l"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _open_plain_file(link, os.O_RDONLY)


def test_the_lock_path_a_symlink_is_not_trusted_or_followed(tmp_path: Path) -> None:
    dead = _lease(tmp_path, 1, expires_in=-3600)
    victim = tmp_path / "victim"
    victim.write_bytes(b"precious")
    _lock_of(dead).symlink_to(victim)
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert dead.exists()
    assert victim.read_bytes() == b"precious"
    assert _lock_of(dead).is_symlink()


def test_fd_names_path_is_what_the_reaper_asks(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_bytes(b"")
    fd = os.open(str(f), os.O_RDONLY)
    try:
        assert _fd_names_path(fd, f)
        f.unlink()
        f.write_bytes(b"")
        assert not _fd_names_path(fd, f)
    finally:
        os.close(fd)


def test_reap_is_skipped_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = _lease(tmp_path, 1, expires_in=-3600)
    scans: list[Any] = []
    real_scandir = os.scandir

    def spy(path: Any = ".") -> Any:
        scans.append(path)
        return real_scandir(path)

    setattr_in(monkeypatch, data_token, "os.scandir", spy)
    setattr_in(monkeypatch, data_token, "sys.platform", "win32")
    assert _reap_expired_leases(tmp_path, now=time.time()) == 0
    assert scans == []
    assert dead.exists()


# ── failures never reach the mint ────────────────────────────────────────


def test_scan_failure_never_reaches_the_mint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _lease(tmp_path, 1, expires_in=-3600)
    real_scandir = os.scandir

    def boom(path: Any = ".") -> Any:
        if Path(path) == tmp_path:
            raise PermissionError("denied")
        return real_scandir(path)

    setattr_in(monkeypatch, data_token, "os.scandir", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-minted"
    scan_failed = [e for e in captured if e["event"] == "data_token_lease_reap_scan_failed"]
    assert len(scan_failed) == 1 and scan_failed[0]["log_level"] == "debug"


def test_unlink_failure_never_reaches_the_mint_and_stops_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    stuck = _lease(tmp_path, 1, expires_in=-3600, mtime_age=7200)
    other = _lease(tmp_path, 2, expires_in=-3600, mtime_age=3600)
    real_unlink = os.unlink

    def boom(path: Any, *a: Any, **kw: Any) -> None:
        if Path(path) == stuck:
            raise PermissionError("denied")
        real_unlink(path, *a, **kw)

    setattr_in(monkeypatch, data_token, "os.unlink", boom)
    with structlog.testing.capture_logs() as captured:
        assert _manager(tmp_path).bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert stuck.exists()
    assert not other.exists()
    failed = [e for e in captured if e["event"] == "data_token_lease_reap_failed"]
    assert len(failed) == 1 and failed[0]["log_level"] == "debug"


def test_no_token_reaches_any_event_the_reap_emits(tmp_path: Path) -> None:
    _lease(tmp_path, 1, expires_in=-3600)
    with structlog.testing.capture_logs() as captured:
        _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert any(e["event"] == "data_token_lease_reaped" for e in captured)
    assert SECRET not in repr(captured)
