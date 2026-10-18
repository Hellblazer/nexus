# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two-process races on ``data_token_mint_lock.*`` (nexus-ndfc1 review B1, B2).

A flock serializes holders of one inode. A reaper that unlinks a lock file a
minter is about to use, or has just created, leaves two minters each holding
"the" lock on different inodes: two concurrent mints, which the lock exists
to prevent (nexus-nnr26). The reviewer reproduced both with fork and real
flock; these are those repros as spawned processes with the interleaving
fixed by events and a queue, never by sleeps. The only waits are bounded
``Event.wait``/``Queue.get`` calls that fail the test on timeout.

The observation is the first thing a second minter does once the first is
inside its mint (holding the lock, not yet published): ``"polling"`` (it saw
the lock held and went to wait for the lease: correct) or ``"minting"`` (it
walked past the lock and minted alongside: the bug).

The expired-lease reaper (nexus-4gmw8) has two more here: a minter publishing
a fresh lease is never reaped, whether the reaper arrives while the minter holds
the lock or decided "dead" before the minter published.

Children are module-level so a ``spawn`` start (no fork-after-network
hazard) can import them.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from nexus.db.data_token import (
    _MINT_LOCK_STALE_AFTER_S,
    DataTokenManager,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _reap_expired_leases,
    _reap_stale_mint_locks,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock/unlink semantics")

URL = "http://127.0.0.1:9999"
TENANT = "acme"
WAIT_S = 90.0  # spawn start-up is slow on a loaded box; a hang still fails


def _manager(cfg: Path, poster: Any, sleep: Any = time.sleep) -> DataTokenManager:
    return DataTokenManager(
        poster=poster,
        mint_credential=lambda: "mintcred",
        mint_tenant=lambda: "",
        config_dir=cfg,
        sleep=sleep,
    )


def _ok() -> tuple[int, dict, dict]:
    return 200, {"data_token": "tok", "expires_in_seconds": 300}, {}


# ── children ─────────────────────────────────────────────────────────────


def _minter(cfg: Path, in_mint: Any, release: Any, opened: Any = None, reaped: Any = None) -> None:
    """Mint, hold inside the mint until released. With ``opened``/``reaped``,
    pause between opening the lock file and refreshing it (the B2 window)."""
    if opened is not None:
        real_utime = os.utime

        def paused_utime(*a: Any, **k: Any) -> None:
            opened.set()
            reaped.wait(WAIT_S)
            return real_utime(*a, **k)

        os.utime = paused_utime  # type: ignore[assignment]

    def poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        in_mint.set()
        release.wait(WAIT_S)
        return _ok()

    _manager(cfg, poster).bearer_for(URL, TENANT)


def _second_minter(cfg: Path, q: Any) -> None:
    """Report the first thing it does: wait on a held lock, or mint."""
    def poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        q.put("minting")
        return _ok()

    def sleep(seconds: float) -> None:
        q.put("polling")
        time.sleep(0.02)

    _manager(cfg, poster, sleep).bearer_for(URL, TENANT)


def _paused_reaper(cfg: Path, opened: Any, go: Any, q: Any) -> None:
    """A reaper that has opened the stale file and is stopped just before it
    locks it (the B1 window)."""
    from nexus import _locking

    real = _locking.lock_fd

    def waiting(fd: int, *, blocking: bool) -> None:
        opened.set()
        go.wait(WAIT_S)
        real(fd, blocking=blocking)

    _locking.lock_fd = waiting  # type: ignore[assignment]
    q.put(_reap_stale_mint_locks(cfg))


def _paused_lease_reaper(cfg: Path, opened: Any, go: Any, q: Any) -> None:
    """An expired-lease reaper that has read the lease as dead and is stopped
    just before it locks the same-key lock."""
    from nexus import _locking

    real = _locking.lock_fd

    def waiting(fd: int, *, blocking: bool) -> None:
        opened.set()
        go.wait(WAIT_S)
        real(fd, blocking=blocking)

    _locking.lock_fd = waiting  # type: ignore[assignment]
    q.put(_reap_expired_leases(cfg, now=time.time()))


def _lease_reaper(cfg: Path, q: Any) -> None:
    q.put(_reap_expired_leases(cfg, now=time.time()))


# ── helpers ──────────────────────────────────────────────────────────────


def _stale_lock(cfg: Path) -> Path:
    f = _data_token_mint_lock_path(URL, TENANT, cfg)
    f.write_bytes(b"")
    old = time.time() - _MINT_LOCK_STALE_AFTER_S - 3600
    os.utime(f, (old, old))
    return f


def _expect(event: Any, what: str) -> None:
    assert event.wait(WAIT_S), f"timed out waiting for {what}"


def _finish(procs: list[Any], *events: Any) -> None:
    for e in events:
        e.set()
    for p in procs:
        p.join(30)
        if p.is_alive():
            p.terminate()
            p.join(5)


def test_second_reaper_does_not_unlink_a_live_minters_file(tmp_path: Path) -> None:
    """B1. R2 opens the stale file, R1 unlinks it, a minter creates a fresh
    file at the path and holds it mid-mint, then R2 locks its (dead) inode and
    sees an old mtime. R2 must not unlink by path: that removes the minter's
    live lock, and the next minter creates and locks a third file."""
    ctx = multiprocessing.get_context("spawn")
    opened, go, in_mint, release = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    r2_result, q = ctx.Queue(), ctx.Queue()
    f = _stale_lock(tmp_path)
    procs: list[Any] = []
    try:
        r2 = ctx.Process(target=_paused_reaper, args=(tmp_path, opened, go, r2_result))
        r2.start()
        procs.append(r2)
        _expect(opened, "reaper 2 to open the stale file")

        assert _reap_stale_mint_locks(tmp_path) == 1  # reaper 1: unlinks the inode reaper 2 holds
        assert not f.exists()

        m = ctx.Process(target=_minter, args=(tmp_path, in_mint, release))
        m.start()
        procs.append(m)
        _expect(in_mint, "the minter to be inside its mint")  # fresh file, locked

        go.set()  # reaper 2 now locks the dead inode
        removed_by_r2 = r2_result.get(timeout=WAIT_S)
        f_survived = f.exists()

        m2 = ctx.Process(target=_second_minter, args=(tmp_path, q))
        m2.start()
        procs.append(m2)
        first = q.get(timeout=WAIT_S)
    finally:
        _finish(procs, release, go)
    assert removed_by_r2 == 0
    assert f_survived
    assert first == "polling", "a second minter minted alongside the first"


def test_minter_whose_file_is_reaped_after_it_opened_it_does_not_mint_unserialized(
    tmp_path: Path,
) -> None:
    """B2. A minter opens the stale file; a reaper locks and unlinks it before
    the minter locks. The minter's lock is on a dead inode, so it must notice
    and reopen the path; otherwise the next minter creates a new file, locks
    it, and both mint at once."""
    ctx = multiprocessing.get_context("spawn")
    opened, reaped, in_mint, release = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    q = ctx.Queue()
    f = _stale_lock(tmp_path)
    procs: list[Any] = []
    try:
        m = ctx.Process(target=_minter, args=(tmp_path, in_mint, release, opened, reaped))
        m.start()
        procs.append(m)
        _expect(opened, "the minter to open the stale file")

        assert _reap_stale_mint_locks(tmp_path) == 1  # unlinks the inode the minter holds
        assert not f.exists()
        reaped.set()
        _expect(in_mint, "the minter to be inside its mint")

        m2 = ctx.Process(target=_second_minter, args=(tmp_path, q))
        m2.start()
        procs.append(m2)
        first = q.get(timeout=WAIT_S)
    finally:
        _finish(procs, release, reaped)
    assert first == "polling", "a second minter minted alongside the first"


def _expired_lease(cfg: Path) -> Path:
    """The previous generation's lease for the minter's key: expired, old."""
    f = _data_token_lease_path(URL, TENANT, cfg)
    f.write_text(json.dumps({
        "format_version": 1, "token": "tok-OLD", "tenant": TENANT, "base_url_digest": "x",
        "expires_at": time.time() - 3600, "ttl_seconds": 300.0,
    }))
    old = time.time() - 7200
    os.utime(f, (old, old))
    return f


def test_lease_reaper_skips_a_lease_whose_minter_holds_the_lock(tmp_path: Path) -> None:
    """The minter is inside its mint (lock held, fresh lease not yet written);
    the expired lease it will replace is on disk. The reaper must not touch
    it: it is not the reaper's to remove while a mint is mid-write. After the
    mint the lease is the fresh one and a second reap leaves it."""
    ctx = multiprocessing.get_context("spawn")
    in_mint, release = ctx.Event(), ctx.Event()
    q = ctx.Queue()
    old = _expired_lease(tmp_path)
    procs: list[Any] = []
    try:
        m = ctx.Process(target=_minter, args=(tmp_path, in_mint, release))
        m.start()
        procs.append(m)
        _expect(in_mint, "the minter to be inside its mint")

        r = ctx.Process(target=_lease_reaper, args=(tmp_path, q))
        r.start()
        procs.append(r)
        removed_while_held = q.get(timeout=WAIT_S)
        still_there = old.exists() and "tok-OLD" in old.read_text()

        release.set()
        m.join(WAIT_S)
        removed_after = _reap_expired_leases(tmp_path, now=time.time())
        fresh = old.read_text() if old.exists() else ""
    finally:
        _finish(procs, release)
    assert removed_while_held == 0 and still_there
    assert removed_after == 0
    assert "tok" in fresh and "tok-OLD" not in fresh, "the minter's fresh lease is gone or was never written"


def test_lease_reaper_that_judged_dead_before_a_minter_published_does_not_remove_the_fresh_lease(
    tmp_path: Path,
) -> None:
    """The reaper reads the lease as dead and stops just before it takes the
    same-key lock. A minter then takes the lock, publishes a fresh lease at the
    same path and exits. The reaper resumes with a stale verdict: it must look
    again under the lock and keep the fresh lease."""
    ctx = multiprocessing.get_context("spawn")
    opened, go, in_mint, release = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Event()
    q = ctx.Queue()
    old = _expired_lease(tmp_path)
    procs: list[Any] = []
    try:
        r = ctx.Process(target=_paused_lease_reaper, args=(tmp_path, opened, go, q))
        r.start()
        procs.append(r)
        _expect(opened, "the reaper to have read the lease and reached the lock")

        release.set()  # the minter does not wait inside its mint
        m = ctx.Process(target=_minter, args=(tmp_path, in_mint, release))
        m.start()
        procs.append(m)
        m.join(WAIT_S)
        assert m.exitcode == 0
        fresh_before = "tok-OLD" not in old.read_text()

        go.set()
        removed = q.get(timeout=WAIT_S)
    finally:
        _finish(procs, release, go)
    assert fresh_before, "the minter did not publish a fresh lease"
    assert removed == 0
    assert old.exists() and "tok-OLD" not in old.read_text()
