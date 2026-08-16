# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the flock-guarded mint-on-miss fast-follow (nexus-nnr26).

Fast-follow from the nexus-9c7t9 critique (T2
``nexus/nexus-9c7t9-lease-cache-critique`` [22720]): the cross-process
lease-file cache's cold-start mint-on-miss is deliberately unlocked, so a
parallel fan-out of M>5 truly concurrent cold ``nx`` processes all miss the
lease, all mint, and the excess M-5 hard-fail with ``DataTokenMintError``
(``MintRateLimiter`` burst=5, refill 1/min; the client's own mint retry
ceiling, ~30-60s, cannot bridge that refill).

Fix under test: a NON-BLOCKING ``fcntl.flock`` around mint-on-miss ONLY,
mirroring the shipped ``nexus.db.t1._lock_guarded_mint_or_borrow`` precedent
(``tests/db/test_t1_mint_race.py`` is that precedent's own test file --
these tests mirror its shape: threads racing into the guarded path via a
``threading.Barrier``, a counting fake mint, exact-count assertions). The
key difference from the t1 precedent this bead's docstring calls out
explicitly: the wait for a losing racer is NON-BLOCKING poll-then-re-read-
the-lease, not a blocking ``LOCK_EX`` wait -- a loser can return as soon as
the winner PUBLISHES, without ever itself acquiring the lock.

Deterministic: injected clock + wall_clock + poster + (optionally) sleep, no
real network. The concurrency tests use REAL OS threads and a REAL
``fcntl.flock`` against a real ``tmp_path`` lock file -- flock semantics are
per OPEN FILE DESCRIPTION, not per process, so genuine intra-process
contention between independently-``os.open``'d file descriptors (one per
simulated "cold process", exactly as separate ``DataTokenManager``
instances model separate ``nx`` subprocesses sharing one ``config_dir``)
is exactly what real cross-process contention would do.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from nexus.db.data_token import (
    DataTokenManager,
    DataTokenMintError,
    _data_token_lease_path,
    _data_token_mint_lock_path,
)


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeWallClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakePoster:
    def __init__(self, responses: list[tuple[int, dict, dict]] | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._calls_lock = threading.Lock()
        self._responses = list(responses or [])
        self._default = (200, {"data_token": "tok-default", "expires_in_seconds": 300}, {})
        self.delay: float = 0.0

    def __call__(self, url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        with self._calls_lock:
            self.calls.append((url, dict(headers), dict(body)))
            resp = self._responses.pop(0) if self._responses else self._default
        if self.delay:
            time.sleep(self.delay)
        return resp

    def queue(self, status: int, body: dict, headers: dict | None = None) -> None:
        self._responses.append((status, body, dict(headers or {})))


BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"


def _manager(
    poster: _FakePoster,
    clock: _FakeClock,
    *,
    config_dir: Path,
    wall_clock: Callable[[], float],
    credential: str | None = "mintcred",
    sleep: Callable[[float], None] | None = None,
    lock_wait_ceiling_seconds: float | None = None,
) -> DataTokenManager:
    cred_fn = (lambda: credential) if credential is not None else (lambda: "")
    tenant_fn = lambda: ""  # noqa: E731 — test-only, no mint_tenant override
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if lock_wait_ceiling_seconds is not None:
        kwargs["lock_wait_ceiling_seconds"] = lock_wait_ceiling_seconds
    return DataTokenManager(
        clock=clock,
        poster=poster,
        mint_credential=cred_fn,
        mint_tenant=tenant_fn,
        config_dir=config_dir,
        wall_clock=wall_clock,
        **kwargs,
    )


# ── lock-file path helper ────────────────────────────────────────────────


def test_mint_lock_path_is_deterministic_and_distinct_from_lease_path(tmp_path: Path) -> None:
    lock1 = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lock2 = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    lease = _data_token_lease_path(BASE_URL, TENANT, tmp_path)

    assert lock1 == lock2
    assert lock1 != lease
    assert lock1.parent == tmp_path
    # Never embeds the raw URL in the filename (same discipline as the
    # lease path, nexus-9c7t9 design point 1).
    assert "127.0.0.1" not in lock1.name
    assert "9999" not in lock1.name


# ── concurrent cold-start racers converge to one mint ───────────────────


def test_concurrent_cold_start_racers_across_separate_managers_converge_to_one_mint(
    tmp_path: Path,
) -> None:
    """N SEPARATE DataTokenManager instances (modeling N separate cold `nx`
    subprocesses, each with its OWN empty in-process cache, sharing one
    config_dir) race `bearer_for` for the SAME (base_url, tenant). Exactly
    one must actually mint; every other racer must borrow the winner's
    published lease instead of independently minting a competing token."""
    wall = _FakeWallClock()
    poster = _FakePoster()
    poster.delay = 0.02  # widen the race window (mirrors test_data_token.py)
    poster.queue(200, {"data_token": "tok-winner", "expires_in_seconds": 300})
    # Extra queued responses in case of a regression that mints more than
    # once — the count assertion below is the real guard, this just keeps
    # a bug from raising instead of being visibly wrong.
    for i in range(1, 10):
        poster.queue(200, {"data_token": f"tok-extra-{i}", "expires_in_seconds": 300})

    n = 8
    barrier = threading.Barrier(n)
    results: list[str | None] = [None] * n
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker(i: int) -> None:
        mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
        barrier.wait()
        try:
            results[i] = mgr.bearer_for(BASE_URL, TENANT)
        except BaseException as exc:  # noqa: BLE001 — surfaced explicitly below
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    if errors:
        raise errors[0]

    assert len(poster.calls) == 1, (
        f"expected exactly one mint call, got {len(poster.calls)}"
    )
    assert all(r == "tok-winner" for r in results), results


def test_losers_borrow_the_published_lease_not_mint(tmp_path: Path) -> None:
    """Direct 2-actor check of the borrow path: manager B starts racing
    while manager A is mid-mint (delayed poster); B must end up returning
    A's minted token, never invoking the poster itself."""
    wall = _FakeWallClock()
    poster = _FakePoster()
    poster.delay = 0.05
    poster.queue(200, {"data_token": "tok-a", "expires_in_seconds": 300})

    mgr_a = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr_b = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=wall)

    results: dict[str, str | None] = {}

    def _run_a() -> None:
        results["a"] = mgr_a.bearer_for(BASE_URL, TENANT)

    def _run_b() -> None:
        results["b"] = mgr_b.bearer_for(BASE_URL, TENANT)

    t_a = threading.Thread(target=_run_a)
    t_b = threading.Thread(target=_run_b)
    t_a.start()
    time.sleep(0.01)  # let A win the flock race deterministically
    t_b.start()
    t_a.join()
    t_b.join()

    assert results["a"] == "tok-a"
    assert results["b"] == "tok-a"
    assert len(poster.calls) == 1


# ── sequential / uncontended happy path is unaffected ───────────────────


def test_uncontended_mint_returns_normally_and_releases_lock(tmp_path: Path) -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1

    # The lock must have been released — a fresh non-blocking acquire from
    # outside the manager must succeed immediately.
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail("lock was still held after an uncontended mint returned")
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_lease_hit_path_never_touches_the_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads stay lock-free (bead requirement): once a fresh lease exists,
    a SECOND manager's bearer_for must never even REACH
    :meth:`DataTokenManager._mint_guarded` — 'flock-guarded mint-on-miss
    ONLY', not a general-purpose guard. The FIRST (uncontended) mint above
    it DOES go through the guard and leaves its own lock file behind (see
    test_uncontended_mint_returns_normally_and_releases_lock) — a bare
    'no lock file on disk' assertion here would conflate that fact with
    what this test actually checks, so instead this monkeypatches
    ``_mint_guarded`` on the SECOND manager to fail the test if it is ever
    called at all."""
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr1.bearer_for(BASE_URL, TENANT)

    poster2 = _FakePoster()
    mgr2 = _manager(poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall)

    def _fail_if_called(*_args: object, **_kwargs: object) -> None:
        pytest.fail("bearer_for reached _mint_guarded on a lease-hit path")

    monkeypatch.setattr(mgr2, "_mint_guarded", _fail_if_called)

    token = mgr2.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert poster2.calls == []


def test_sequential_single_manager_byte_identical_to_before(tmp_path: Path) -> None:
    """No lock-contention cost on the happy path: a single manager, no
    contention, mint-then-refresh-then-reuse sequence behaves exactly as
    the pre-nnr26 unguarded design (same token values, same call counts)."""
    wall = _FakeWallClock()
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    clock = _FakeClock()
    mgr = _manager(poster, clock, config_dir=tmp_path, wall_clock=wall)

    first = mgr.bearer_for(BASE_URL, TENANT)
    assert first == "tok-1"
    assert len(poster.calls) == 1

    clock.advance(1)  # well within TTL — in-process cache hit
    second = mgr.bearer_for(BASE_URL, TENANT)
    assert second == "tok-1"
    assert len(poster.calls) == 1  # no new mint


# ── lock holder's mint failure does not deadlock a waiting racer ────────


def test_mint_guarded_failure_releases_lock_and_raises(tmp_path: Path) -> None:
    """Mirrors tests/db/test_t1_mint_race.py's
    test_mint_failure_still_releases_lock_and_raises: a mint failure must
    (a) propagate DataTokenMintError unchanged, and (b) actually release
    the flock -- verified by a fresh non-blocking acquire succeeding after."""
    poster = _FakePoster()
    for _ in range(5):
        poster.queue(500, {"error": "boom"})
    mgr = _manager(
        poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock(),
        sleep=lambda s: None,
    )

    with pytest.raises(DataTokenMintError):
        mgr.bearer_for(BASE_URL, TENANT)

    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail(
                "lock was still held after a mint failure -- bearer_for "
                "must release it even on error"
            )
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_waiting_racer_falls_through_to_its_own_mint_after_holders_failure(
    tmp_path: Path,
) -> None:
    """A sibling holds the lock (simulating a racer mid-mint) and then
    releases it WITHOUT publishing a lease (simulating that racer's mint
    having failed) -- the waiting manager must NOT deadlock: once the lock
    is free, it becomes the new holder and mints on its own (t1 precedent's
    documented behaviour: the failed holder's lock release lets the next
    waiter become the new holder, which then attempts its own mint)."""
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    held = threading.Event()
    release = threading.Event()

    def _sibling_holds_then_releases_without_publishing() -> None:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            release.wait(timeout=5)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    sibling = threading.Thread(target=_sibling_holds_then_releases_without_publishing)
    sibling.start()
    assert held.wait(timeout=5), "sibling never acquired the lock"

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-fallthrough", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    def _release_soon() -> None:
        time.sleep(0.05)
        release.set()

    threading.Thread(target=_release_soon).start()

    token = mgr.bearer_for(BASE_URL, TENANT)
    sibling.join(timeout=5)

    assert token == "tok-fallthrough"
    assert len(poster.calls) == 1


# ── lock acquisition ceiling: fails loud rather than waiting forever ────


def test_lock_wait_ceiling_exceeded_raises_named_error(tmp_path: Path) -> None:
    """A sibling holds the lock for the whole test (never releases, never
    publishes) -- the waiting manager must give up after its configured
    ceiling with a NAMED error, not hang indefinitely (mirrors
    nexus.db.t1._lock_guarded_mint_or_borrow's deadline-exceeded RuntimeError
    for the by875 bounded-poll variant)."""
    lock_path = _data_token_mint_lock_path(BASE_URL, TENANT, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        poster = _FakePoster()
        mgr = _manager(
            poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock(),
            sleep=lambda s: None, lock_wait_ceiling_seconds=0.05,
        )
        with pytest.raises(DataTokenMintError, match="lock wait"):
            mgr.bearer_for(BASE_URL, TENANT)
        assert poster.calls == []  # never attempted its own mint
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ── lock-file creation failure degrades to a direct unguarded mint ──────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_lock_file_unavailable_falls_back_to_direct_mint(tmp_path: Path) -> None:
    """If the lock file itself cannot be created (e.g. a read-only config
    dir), the guard must degrade gracefully to an unguarded mint rather
    than failing the whole bearer_for call -- the lock is an optimization,
    the mint is the source of truth (same stance as _write_lease's own
    best-effort failure handling)."""
    if os.geteuid() == 0:
        pytest.skip("permission bits are not enforced against root")

    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir(mode=0o700)
    readonly_dir.chmod(0o500)  # read + execute, no write
    try:
        poster = _FakePoster()
        poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
        mgr = _manager(poster, _FakeClock(), config_dir=readonly_dir, wall_clock=_FakeWallClock())

        token = mgr.bearer_for(BASE_URL, TENANT)

        assert token == "tok-1"
        assert len(poster.calls) == 1
    finally:
        readonly_dir.chmod(0o700)  # restore so tmp_path cleanup can remove it
