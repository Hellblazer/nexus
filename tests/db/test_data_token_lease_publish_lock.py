# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every lease publish happens under the same-key mint lock, and a lock the
lease reaper creates names its creator (nexus-fcwbd).

The lease reaper unlinks a dead lease only under the same-key lock, on the
premise that every publisher holds it. The guarded cold mint did; the refresh
path of ``bearer_for`` and ``_mint_unguarded`` did not, so the reaper could
delete a lease published in the few syscalls between its inode check and its
unlink. These tests observe the lock from outside: a probe takes a
non-blocking flock on the lock file from inside the publish, and a counter
measures simultaneous publishers, not exits.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nexus import _locking
from nexus.db import data_token
from nexus.db.data_token import (
    _DATA_TOKEN_LEASE_PREFIX,
    _MINT_LOCK_PREFIX,
    DataTokenManager,
    _CachedToken,
    _data_token_mint_lock_path,
    _open_reap_lock,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")

BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"


def _poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok-minted", "expires_in_seconds": 300}, {}


def _manager(config_dir: Path, **kw: Any) -> DataTokenManager:
    return DataTokenManager(
        poster=_poster,
        mint_credential=lambda: "mintcred",
        mint_tenant=lambda: "",
        config_dir=config_dir,
        **kw,
    )


def _refresh_due(mgr: DataTokenManager) -> None:
    """Seed the in-process cache with a token that is past its refresh point,
    so the next ``bearer_for`` takes the refresh branch."""
    now = mgr._clock()
    mgr._cache[(BASE_URL, TENANT)] = _CachedToken(
        token="old", minted_at=now - 3500, expires_at=now + 10, ttl_seconds=3600.0,
    )


class _LockProbe:
    """Wraps ``_write_lease``: on each call, reports whether the same-key lock
    file is held by someone else (the publisher), and how many publishers are
    inside at once."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, config_dir: Path, hold_s: float = 0.0) -> None:
        self.lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, config_dir)
        self.held_at_publish: list[bool] = []
        self.inside = 0
        self.max_inside = 0
        self._mu = threading.Lock()
        self._hold_s = hold_s
        real = DataTokenManager._write_lease
        probe = self

        def spy(mgr: DataTokenManager, base_url: str, tenant: str, cached: _CachedToken) -> None:
            with probe._mu:
                probe.inside += 1
                probe.max_inside = max(probe.max_inside, probe.inside)
            try:
                probe.held_at_publish.append(probe._lock_is_held())
                if probe._hold_s:
                    time.sleep(probe._hold_s)
                real(mgr, base_url, tenant, cached)
            finally:
                with probe._mu:
                    probe.inside -= 1

        monkeypatch.setattr(DataTokenManager, "_write_lease", spy)

    def _lock_is_held(self) -> bool:
        try:
            fd = os.open(str(self.lock_path), os.O_WRONLY)
        except FileNotFoundError:
            return False
        try:
            try:
                _locking.lock_fd(fd, blocking=False)  # lifecycle-gate-allow: test probe for the same-key lock (nexus-fcwbd)
            except BlockingIOError:
                return True
            _locking.unlock_fd(fd)  # lifecycle-gate-allow: test probe release
            return False
        finally:
            os.close(fd)


# ── the lock is held at every publish ────────────────────────────────────────


def test_guarded_cold_mint_publishes_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the probe sees the lock held on the path that always
    took it. If this fails the probe is blind and the next two prove nothing."""
    probe = _LockProbe(monkeypatch, tmp_path)
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert probe.held_at_publish == [True]


def test_refresh_publishes_under_the_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _LockProbe(monkeypatch, tmp_path)
    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert probe.held_at_publish == [True]


def test_mint_unguarded_publishes_under_the_lock_when_it_can_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first open of the lock fails (a transient OSError), so the mint
    falls to ``_mint_unguarded``; the lock opens again by publish time, and the
    publish takes it."""
    probe = _LockProbe(monkeypatch, tmp_path)
    real = data_token._open_mint_lock_logged
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("EMFILE (simulated)")
        return real(*a, **k)

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", flaky)
    _manager(tmp_path).bearer_for(BASE_URL, TENANT)
    assert calls["n"] >= 2, "the unguarded path never tried the lock again"
    assert probe.held_at_publish == [True]


def test_mint_unguarded_still_publishes_when_the_lock_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is an optimization: an unopenable lock must not cost the lease."""
    monkeypatch.setattr(
        data_token, "_open_mint_lock_logged", lambda *a, **k: (_ for _ in ()).throw(OSError("EACCES (simulated)")),
    )
    mgr = _manager(tmp_path)
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert any(p.name.startswith(_DATA_TOKEN_LEASE_PREFIX) for p in tmp_path.iterdir())


def test_a_held_lock_delays_the_refresh_publish_and_does_not_lose_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder (a reaper, a cold minter) has the lock for 0.3s: the refresh
    publishes after it lets go, never alongside it."""
    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock_path.write_text("{}\n")
    holder = os.open(str(lock_path), os.O_WRONLY)
    _locking.lock_fd(holder, blocking=False)  # lifecycle-gate-allow: test holder standing in for a reaper (nexus-fcwbd)
    released_at: list[float] = []

    def release() -> None:
        time.sleep(0.3)
        released_at.append(time.monotonic())
        _locking.unlock_fd(holder)  # lifecycle-gate-allow: test holder release

    t = threading.Thread(target=release)
    published_at: list[float] = []
    real = DataTokenManager._write_lease

    def spy(m: DataTokenManager, *a: Any) -> None:
        published_at.append(time.monotonic())
        real(m, *a)

    monkeypatch.setattr(DataTokenManager, "_write_lease", spy)
    t.start()
    try:
        mgr.bearer_for(BASE_URL, TENANT)
    finally:
        t.join()
        os.close(holder)
    assert published_at and published_at[0] >= released_at[0]
    assert any(p.name.startswith(_DATA_TOKEN_LEASE_PREFIX) for p in tmp_path.iterdir())


# ── the publish never fails the caller who already holds a good token ────────


def _lease_files(config_dir: Path) -> list[Path]:
    return [p for p in config_dir.iterdir() if p.name.startswith(_DATA_TOKEN_LEASE_PREFIX)]


def test_a_lock_that_cannot_be_flocked_falls_back_to_the_plain_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENOLCK / EOPNOTSUPP from flock (NFS, some FUSE mounts) is an OSError that
    is not a BlockingIOError. It used to escape and fail ``bearer_for`` with a
    good token in hand. The lease is an optimization: write it unlocked."""
    import errno

    def refuse(fd: int, *, blocking: bool = True) -> None:
        raise OSError(errno.ENOLCK, "No locks available (simulated)")

    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    monkeypatch.setattr(_locking, "lock_fd", refuse)
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert len(_lease_files(tmp_path)) == 1


def test_a_cold_start_on_a_filesystem_that_cannot_flock_still_mints_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold path shares the guarded mint with the refresh (nexus-dncjg), so
    it falls back to the unguarded mint on ENOLCK too, where it used to raise
    the OSError out of ``bearer_for``. No cached token: this is a cold start."""
    import errno

    def refuse(fd: int, *, blocking: bool = True) -> None:
        raise OSError(errno.ENOLCK, "No locks available (simulated)")

    monkeypatch.setattr(_locking, "lock_fd", refuse)
    mgr = _manager(tmp_path)
    assert mgr.has_live_token(BASE_URL, TENANT) is False
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert len(_lease_files(tmp_path)) == 1


def test_a_lock_held_past_the_wait_skips_the_publish_and_still_returns_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A holder that never lets go (a wedged minter) against the UNGUARDED
    mint (its first lock open failed): the publisher waits
    ``_LEASE_PUBLISH_LOCK_WAIT_S``, then leaves the lease to it. No write, no
    raise, and the caller still gets the token it minted."""
    monkeypatch.setattr(data_token, "_LEASE_PUBLISH_LOCK_WAIT_S", 0.2)
    mgr = _manager(tmp_path)
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock_path.write_text("{}\n")
    holder = os.open(str(lock_path), os.O_WRONLY)
    _locking.lock_fd(holder, blocking=False)  # lifecycle-gate-allow: test holder that never lets go (nexus-fcwbd)
    real_open = data_token._open_mint_lock_logged
    opened = {"n": 0}

    def first_open_fails(*a: Any, **k: Any) -> int:
        opened["n"] += 1
        if opened["n"] == 1:
            raise OSError("EMFILE (simulated)")
        return real_open(*a, **k)

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", first_open_fails)
    writes: list[int] = []
    real = DataTokenManager._write_lease
    monkeypatch.setattr(
        DataTokenManager, "_write_lease", lambda m, *a: (writes.append(1), real(m, *a))[1],
    )
    try:
        started = time.monotonic()
        assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
        waited = time.monotonic() - started
    finally:
        _locking.unlock_fd(holder)  # lifecycle-gate-allow: test holder release
        os.close(holder)
    assert writes == [] and _lease_files(tmp_path) == []
    assert 0.2 <= waited < 5.0, f"waited {waited:.2f}s: the bound was not honoured"


def test_a_refresh_against_a_lock_that_never_frees_returns_the_cached_token_without_minting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-dncjg: the refresh no longer mints outside the lock. A holder that
    never lets go costs it ``_REFRESH_LOCK_WAIT_S``, then it hands back the
    still-valid cached token: no mint, no write, no raise."""
    monkeypatch.setattr(data_token, "_REFRESH_LOCK_WAIT_S", 0.2)
    mints: list[int] = []

    def poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        mints.append(1)
        return _poster(url, headers, body)

    mgr = DataTokenManager(
        poster=poster, mint_credential=lambda: "mintcred", mint_tenant=lambda: "", config_dir=tmp_path,
    )
    _refresh_due(mgr)
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock_path.write_text("{}\n")
    holder = os.open(str(lock_path), os.O_WRONLY)
    _locking.lock_fd(holder, blocking=False)  # lifecycle-gate-allow: test holder that never lets go (nexus-dncjg)
    try:
        started = time.monotonic()
        assert mgr.bearer_for(BASE_URL, TENANT) == "old"
        waited = time.monotonic() - started
    finally:
        _locking.unlock_fd(holder)  # lifecycle-gate-allow: test holder release
        os.close(holder)
    assert mints == [] and _lease_files(tmp_path) == []
    assert 0.2 <= waited < 5.0, f"waited {waited:.2f}s: the bound was not honoured"


def _unlink_the_lock_before_locking(monkeypatch: pytest.MonkeyPatch, lock_path: Path, times: int) -> list[int]:
    """The first *times* lock attempts find the lock file unlinked from under
    them (a reaper between our open and our flock): the flock then lands on an
    inode no path names."""
    real = _locking.lock_fd
    calls: list[int] = []

    def wrapped(fd: int, *, blocking: bool = True) -> None:
        calls.append(1)
        if len(calls) <= times:
            lock_path.unlink(missing_ok=True)
        real(fd, blocking=blocking)

    monkeypatch.setattr(_locking, "lock_fd", wrapped)
    return calls


def test_a_lock_unlinked_before_it_is_locked_is_reopened_and_the_publish_holds_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = _LockProbe(monkeypatch, tmp_path)
    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    calls = _unlink_the_lock_before_locking(monkeypatch, probe.lock_path, times=1)
    opens: list[int] = []
    real_open = data_token._open_mint_lock_logged
    monkeypatch.setattr(data_token, "_open_mint_lock_logged", lambda *a, **k: (opens.append(1), real_open(*a, **k))[1])
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert len(opens) == 2, "the unlinked inode was not reopened"
    assert len(calls) >= 2
    assert probe.held_at_publish == [True], "the publish did not hold a lock on the file the path names"
    assert len(_lease_files(tmp_path)) == 1


def test_reopening_is_bounded_and_the_lease_is_still_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaper that unlinks every time cannot starve the publisher for ever:
    after ``_MINT_LOCK_REOPEN_MAX`` reopens it writes anyway."""
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    calls = _unlink_the_lock_before_locking(monkeypatch, lock_path, times=10_000)
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    assert len(calls) == data_token._MINT_LOCK_REOPEN_MAX + 1
    assert len(_lease_files(tmp_path)) == 1


def test_a_reopen_that_fails_falls_back_to_the_plain_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    mgr = _manager(tmp_path)
    _refresh_due(mgr)
    _unlink_the_lock_before_locking(monkeypatch, lock_path, times=1)
    real_open = data_token._open_mint_lock_logged
    n = {"c": 0}

    def second_open_fails(*a: Any, **k: Any) -> int:
        n["c"] += 1
        if n["c"] == 2:
            raise OSError("EMFILE (simulated)")
        return real_open(*a, **k)

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", second_open_fails)
    assert mgr.bearer_for(BASE_URL, TENANT) == "tok-minted"
    # open 1: the guarded mint; open 2 (fails): its reopen, which falls to the
    # unguarded mint; open 3: that mint's publish, under the lock again.
    assert n["c"] == 3
    assert len(_lease_files(tmp_path)) == 1


# ── simultaneous publishers, counted ─────────────────────────────────────────


def test_two_refreshers_on_one_key_are_never_inside_the_publish_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two managers (two processes' worth: no shared in-process key lock)
    refresh the same key at once. The mint lock admits one at a time and the
    second adopts the first's lease instead of minting (nexus-dncjg): exactly
    one publish, and publishers inside at one instant never reach two."""
    probe = _LockProbe(monkeypatch, tmp_path, hold_s=0.15)
    managers = [_manager(tmp_path), _manager(tmp_path)]
    for m in managers:
        _refresh_due(m)
    barrier = threading.Barrier(len(managers))
    errors: list[BaseException] = []
    tokens: list[str | None] = []

    def run(m: DataTokenManager) -> None:
        try:
            barrier.wait()
            tokens.append(m.bearer_for(BASE_URL, TENANT))
        except BaseException as exc:  # noqa: BLE001 -- surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in managers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert tokens == ["tok-minted", "tok-minted"]
    assert len(probe.held_at_publish) == 1, "one refresher mints and publishes; the other adopts its lease"
    assert probe.held_at_publish == [True]
    assert probe.max_inside == 1, f"{probe.max_inside} publishers were inside at once"


# ── reaper-created locks name their creator ──────────────────────────────────


def test_reaper_created_lock_carries_an_attribution_line(tmp_path: Path) -> None:
    lock = tmp_path / (_MINT_LOCK_PREFIX + "a" * 64)
    lease = tmp_path / (_DATA_TOKEN_LEASE_PREFIX + "a" * 64)
    fd, created = _open_reap_lock(lock, lease_path=lease)
    os.close(fd)
    assert created
    first = lock.read_text().splitlines()[0]
    rec = json.loads(first)
    assert rec["created_by"] == "lease-reaper"
    assert rec["pid"] == os.getpid()
    assert rec["lock_file"] == lock.name and rec["lease_file"] == lease.name
    assert not {"token", "data_token", "credential"} & set(rec)


def test_the_reaper_does_not_spawn_ps_for_its_attribution_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_parent_process_name`` runs ``ps``. The reaper is on the mint path of
    every cold process and creates a lock only in a rare race, so it records
    the pid and ppid and leaves the name out rather than spawn a process."""
    def no_ps() -> str:
        raise AssertionError("the reaper spawned ps")

    monkeypatch.setattr(data_token, "_parent_process_name", no_ps)
    lock = tmp_path / (_MINT_LOCK_PREFIX + "d" * 64)
    fd, created = _open_reap_lock(lock, lease_path=tmp_path / (_DATA_TOKEN_LEASE_PREFIX + "d" * 64))
    os.close(fd)
    assert created
    rec = json.loads(lock.read_text().splitlines()[0])
    assert rec["created_by"] == "lease-reaper" and rec["ppid"] == os.getppid()
    assert rec["parent_name"] is None


def test_an_existing_lock_is_not_rewritten_by_the_reaper(tmp_path: Path) -> None:
    lock = tmp_path / (_MINT_LOCK_PREFIX + "b" * 64)
    lock.write_text('{"created_by": "minter"}\n')
    fd, created = _open_reap_lock(lock)
    os.close(fd)
    assert not created
    assert lock.read_text() == '{"created_by": "minter"}\n'


def test_reap_of_a_dead_lease_with_no_lock_leaves_no_lock_and_records_its_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: the reap creates the lock (with its record), removes the
    dead lease, and removes the lock it made."""
    lease = tmp_path / (_DATA_TOKEN_LEASE_PREFIX + "c" * 64)
    lease.write_text(json.dumps({
        "format_version": 1, "token": "x", "tenant": "t", "base_url_digest": "c" * 64,
        "expires_at": time.time() - 7200, "ttl_seconds": 3600.0, "minted_by_pid": 1,
    }))
    seen: list[str] = []
    real = data_token._open_reap_lock

    def spy(lock_path: Path, **k: Any) -> tuple[int, bool]:
        fd, created = real(lock_path, **k)
        seen.append(lock_path.read_text().splitlines()[0] if created else "")
        return fd, created

    monkeypatch.setattr(data_token, "_open_reap_lock", spy)
    assert data_token._reap_one_lease(lease, tmp_path, time.time()) is True
    assert json.loads(seen[0])["created_by"] == "lease-reaper"
    assert not lease.exists()
    assert not (tmp_path / (_MINT_LOCK_PREFIX + "c" * 64)).exists()
