# SPDX-License-Identifier: AGPL-3.0-or-later
"""The refresh-due branch of ``DataTokenManager.bearer_for`` reads the lease,
mints under the host's mint lock, keeps a still-valid token on a failed mint,
and is jittered per process (nexus-dncjg); the mint backoff is full-jitter
(nexus-kcdbx).

The incident: after a reboot, a dozen processes that had borrowed one lease
shared its wall-clock expiry, became refresh-due in the same second, minted
at once past the engine's burst of 5 per (credential, tenant), and the
losers kept retrying a mint that the limiter refused instead of adopting the
winner's lease. Every test here uses a fake poster, fake clocks and a tmp
config dir; no token or credential value is printed.
"""
from __future__ import annotations

import errno
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus import _locking
from nexus.db import data_token
from nexus.db.data_token import (
    _LEASE_FORMAT_VERSION,
    _MINT_BACKOFF_SCHEDULE,
    _MINT_RETRY_AFTER_CAP_S,
    _REFRESH_THRESHOLD,
    _REFRESH_THRESHOLD_JITTER,
    DataTokenManager,
    DataTokenMintError,
    _data_token_lease_path,
    _data_token_mint_lock_path,
    _lease_key,
)

BASE = "https://engine.example:443"
TENANT = "nexus"
KEY = (BASE, TENANT)
TTL = 3600
#: 81% of the TTL later: 684s (19%) remain, so a manager at threshold 0.20 is
#: refresh-due while its cached token is still valid for 11 minutes.
DUE_AFTER_S = 2916


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _MidRng:
    """``uniform(a, b)`` returns the midpoint: threshold 0.20, retry window
    30s, backoff 0.5s / 1.0s. Pins every draw so a test names its numbers."""

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


class _EdgeRng:
    def __init__(self, high: bool) -> None:
        self._high = high

    def uniform(self, a: float, b: float) -> float:
        return b if self._high else a


class _Endpoint:
    """A fake mint endpoint: answers ``script`` entries in order (a status, or
    a ``(status, headers)`` pair), then 200. ``ttl_for[n]`` overrides the TTL
    of call number ``n``'s 200."""

    def __init__(self) -> None:
        self.script: list[int | tuple[int, dict[str, str]]] = []
        self.ttl_for: dict[int, int] = {}
        self.calls = 0
        self.delay = 0.0
        self._mu = threading.Lock()

    def __call__(self, url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        with self._mu:
            self.calls += 1
            n = self.calls
            entry = self.script.pop(0) if self.script else 200
        status, resp_headers = entry if isinstance(entry, tuple) else (entry, {})
        if self.delay:
            time.sleep(self.delay)
        if status == 200:
            return 200, {"data_token": f"tok-{n}", "expires_in_seconds": self.ttl_for.get(n, TTL)}, {}
        return status, {"error": "rate limit exceeded"}, resp_headers


def _mgr(tmp_path: Path, endpoint: Any, clock: _Clock, **kw: Any) -> DataTokenManager:
    kw.setdefault("rng", _MidRng())
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("wall_clock", clock)
    return DataTokenManager(
        clock=clock, poster=endpoint,
        mint_credential=lambda: "cred", mint_tenant=lambda: "", config_dir=tmp_path, **kw,
    )


def _hold_the_mint_lock(tmp_path: Path) -> int:
    """Take the same-key mint lock from outside, as a sibling mid-mint would."""
    lock_path = _data_token_mint_lock_path(BASE, TENANT, tmp_path)
    lock_path.write_text("{}\n")
    holder = os.open(str(lock_path), os.O_WRONLY)
    _locking.lock_fd(holder, blocking=False)  # lifecycle-gate-allow: test holder standing in for a sibling's mint (nexus-dncjg)
    return holder


def _release(holder: int) -> None:
    _locking.unlock_fd(holder)  # lifecycle-gate-allow: test holder release (nexus-dncjg)
    os.close(holder)


def _write_foreign_lease(tmp_path: Path, token: str, *, expires_at_wall: float, ttl: float) -> None:
    """A lease a sibling published: the on-disk shape ``_write_lease`` writes."""
    path = _data_token_lease_path(BASE, TENANT, tmp_path)
    payload = {
        "format_version": _LEASE_FORMAT_VERSION, "token": token, "tenant": TENANT,
        "base_url_digest": _lease_key(BASE, TENANT), "expires_at": expires_at_wall,
        "ttl_seconds": ttl, "minted_by_pid": 1,
    }
    path.write_text(json.dumps(payload))


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock semantics")


# ── 1. the refresh branch reads the lease and mints under the lock ───────────


def test_a_refresh_due_process_adopts_a_siblings_lease_instead_of_minting(tmp_path: Path) -> None:
    """The incident, as a sequence: a long-lived process, a sibling that wins
    one admitted refresh, then a limiter that refuses everyone else. Revert:
    the refresh mints directly, never reads the lease, and raises the 429."""
    clock, ep = _Clock(), _Endpoint()
    long_lived = _mgr(tmp_path, ep, clock)
    assert long_lived.bearer_for(BASE, TENANT) == "tok-1"

    clock.t += DUE_AFTER_S
    sibling = _mgr(tmp_path, ep, clock)
    sibling._cache[KEY] = long_lived._cache[KEY]  # a second long-lived process on the same lease
    assert sibling.bearer_for(BASE, TENANT) == "tok-2"  # wins the one admitted refresh, publishes

    ep.script[:] = [429] * 30  # the limiter now refuses everyone else
    before = ep.calls
    assert long_lived.bearer_for(BASE, TENANT) == "tok-2"
    assert ep.calls == before, "the refresh minted instead of adopting the sibling's lease"

    cold = _mgr(tmp_path, ep, clock)
    assert cold.bearer_for(BASE, TENANT) == "tok-2"
    assert ep.calls == before


def test_concurrent_refresh_due_processes_make_one_mint_between_them(tmp_path: Path) -> None:
    """Eight managers (eight processes' worth: no shared in-process lock) hold
    one lease and are refresh-due in the same second. The mint lock admits one
    refresher; the rest adopt its lease. Revert: eight mints."""
    clock, ep = _Clock(), _Endpoint()
    # A small real sleep: the losers poll the busy lock, and a no-op sleep
    # would turn that poll into a busy spin for the length of the mint.
    def pause(seconds: float) -> None:
        time.sleep(min(seconds, 0.005))

    first = _mgr(tmp_path, ep, clock, sleep=pause)
    assert first.bearer_for(BASE, TENANT) == "tok-1"
    managers = [first] + [_mgr(tmp_path, ep, clock, sleep=pause) for _ in range(7)]
    for m in managers[1:]:
        assert m.bearer_for(BASE, TENANT) == "tok-1"  # borrowed
    assert ep.calls == 1

    clock.t += DUE_AFTER_S
    ep.delay = 0.05  # widen the race: a mint takes time
    barrier = threading.Barrier(len(managers))
    tokens: list[str | None] = []
    errors: list[BaseException] = []

    def run(m: DataTokenManager) -> None:
        try:
            barrier.wait()
            tokens.append(m.bearer_for(BASE, TENANT))
        except BaseException as exc:  # noqa: BLE001 -- surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in managers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert ep.calls == 2, f"{ep.calls - 1} refresh mints; one refresher per host should mint"
    assert tokens == ["tok-2"] * len(managers)


# ── 2. a failed refresh keeps a still-valid token ────────────────────────────


def test_a_failed_refresh_keeps_the_valid_token_and_waits_out_a_jittered_window(tmp_path: Path) -> None:
    """Revert: the first call raises the 429 and every call re-mints."""
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S  # due, 684s of life left

    ep.script[:] = [429] * 100
    before = ep.calls
    assert mgr.bearer_for(BASE, TENANT) == "tok-1", "a failed refresh must hand back the still-valid token"
    assert ep.calls - before == 1  # one mint, ONE attempt

    before = ep.calls
    clock.t += 10  # inside the 30s window (_MidRng)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert ep.calls == before, "a call inside the retry window re-minted"

    clock.t += 25  # 35s after the failure: window over
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert ep.calls - before == 1, "the window never reopened"


@pytest.mark.parametrize(
    "refusal",
    [503, 429, (429, {"Retry-After": "15"}), (503, {"Retry-After": "15"})],
    ids=["503", "429", "429-retry-after-15", "503-retry-after-15"],
)
def test_a_refused_refresh_with_a_valid_token_makes_one_attempt_and_never_sleeps(
    tmp_path: Path, refusal: Any,
) -> None:
    """The key lock and the mint flock are held while the mint runs, so a
    retry sleep stalls every sibling and every request thread behind them (30s
    measured with Retry-After 15). The retry is the refresh window. Revert
    (the refresh gets the cold path's 3 attempts): 3 calls and 2 sleeps."""
    sleeps: list[float] = []
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock, sleep=sleeps.append)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [refusal] * 10
    before = ep.calls
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert ep.calls - before == 1
    assert sleeps == [], f"slept {sleeps} while holding the key lock and the mint flock"


def test_a_refused_refresh_on_a_filesystem_that_cannot_flock_makes_one_attempt_and_never_sleeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENOLCK sends the refresh to the unguarded mint, which holds the key lock
    just as the guarded one does, so it too makes one attempt and never sleeps
    while the cached token is valid. Revert (the unguarded mint ignores the
    refresh): 3 calls and 2 sleeps."""
    sleeps: list[float] = []
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock, sleep=sleeps.append)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"

    def _no_flock(fd: int, *, blocking: bool) -> None:  # noqa: ARG001
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(data_token._locking, "lock_fd", _no_flock)
    clock.t += DUE_AFTER_S
    ep.script[:] = [(429, {"Retry-After": "15"})] * 10
    before = ep.calls
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert ep.calls - before == 1
    assert sleeps == [], f"slept {sleeps} on the unguarded refresh"


def test_a_refresh_after_the_cached_token_expired_keeps_the_full_retry_budget(tmp_path: Path) -> None:
    """With no valid token to fall back on the refresh is a cold start and
    retries as one: 3 attempts, 2 sleeps. Revert of the validity condition
    (single attempt whenever refreshing): 1 call."""
    sleeps: list[float] = []
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock, sleep=sleeps.append)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL + 10  # expired
    ep.script[:] = [503] * 10
    before = ep.calls
    with pytest.raises(DataTokenMintError, match=r"\(503\)"):
        mgr.bearer_for(BASE, TENANT)
    assert ep.calls - before == data_token._MINT_MAX_ATTEMPTS
    assert len(sleeps) == data_token._MINT_MAX_ATTEMPTS - 1


def test_the_mint_error_carries_the_status_and_the_retry_after(tmp_path: Path) -> None:
    """What the refresh reads to log loudly and to wait as long as asked."""
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    ep.script[:] = [(429, {"Retry-After": "120"})] * 3
    with pytest.raises(DataTokenMintError) as e429:
        mgr.bearer_for(BASE, TENANT)
    assert e429.value.status == 429
    assert e429.value.retry_after == 120

    ep.script[:] = [403]
    with pytest.raises(DataTokenMintError) as e403:
        mgr.bearer_for(BASE, TENANT)
    assert e403.value.status == 403
    assert e403.value.retry_after is None

    def dead(url: str, headers: dict, body: dict) -> Any:
        raise OSError("connection refused")

    with pytest.raises(DataTokenMintError) as e_net:
        _mgr(tmp_path / "net", dead, clock).bearer_for(BASE, TENANT)
    assert e_net.value.status is None


@pytest.mark.parametrize(
    ("retry_after", "left_s", "expected_wait_s"),
    [
        ("100", 684, 100.0),  # longer than the 30s window: the limiter's ask wins
        ("5", 684, 30.0),  # shorter: the window wins
        (None, 684, 30.0),  # none sent: the window
        ("200", 20, 20.0),  # never past the cached token's own life
    ],
    ids=["retry-after-wins", "window-wins", "no-header", "capped-at-remaining-life"],
)
def test_the_next_attempt_waits_at_least_the_retry_after_never_past_the_tokens_life(
    tmp_path: Path, retry_after: str | None, left_s: int, expected_wait_s: float,
) -> None:
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL - left_s
    ep.script[:] = [(429, {"Retry-After": retry_after} if retry_after else {})] * 10
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert mgr._refresh_retry_at[KEY] - clock.t == pytest.approx(expected_wait_s)


def test_the_retry_window_is_jittered_and_never_outlives_the_cached_token(tmp_path: Path) -> None:
    clock, ep = _Clock(), _Endpoint()
    low = _mgr(tmp_path, ep, clock, rng=_EdgeRng(high=False))
    assert low.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += 3100  # 14% left: due even at the lowest threshold (0.15)
    ep.script[:] = [429] * 100
    low.bearer_for(BASE, TENANT)
    assert low._refresh_retry_at[KEY] - clock.t == pytest.approx(data_token._REFRESH_RETRY_WINDOW_S[0])

    ep2 = _Endpoint()
    ep2.script[:] = [200] + [429] * 100
    high = _mgr(tmp_path / "other", ep2, clock, rng=_EdgeRng(high=True))
    assert high.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL - 20  # 20s of life left, less than the shortest window
    high.bearer_for(BASE, TENANT)
    assert high._refresh_retry_at[KEY] - clock.t == pytest.approx(20.0)


def test_a_failed_refresh_after_the_cached_token_expired_still_raises(tmp_path: Path) -> None:
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [429] * 100
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"  # kept while valid

    clock.t += 700  # 684s of life, now expired
    with pytest.raises(DataTokenMintError, match=r"\(429\)"):
        mgr.bearer_for(BASE, TENANT)


def _refresh_log(captured: list[dict[str, Any]]) -> dict[str, Any]:
    (entry,) = [e for e in captured if e["event"] == "data_token_refresh_failed_keeping_cached"]
    return entry


@pytest.mark.parametrize("status", [401, 403])
def test_a_refresh_refused_as_unauthorized_keeps_the_token_but_logs_an_error(tmp_path: Path, status: int) -> None:
    """A revoked credential or a wrong tenant is not a busy limiter: nothing
    will ever mint another token. The valid one is still served, and the log
    says so at error level with the status. Revert: a warning."""
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [status]
    with structlog.testing.capture_logs() as captured:
        assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    entry = _refresh_log(captured)
    assert entry["log_level"] == "error"
    assert entry["status"] == status


@pytest.mark.parametrize("status", [429, 503, 500])
def test_a_refresh_refused_as_busy_keeps_the_token_with_a_warning(tmp_path: Path, status: int) -> None:
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [status] * 5
    with structlog.testing.capture_logs() as captured:
        assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    entry = _refresh_log(captured)
    assert entry["log_level"] == "warning"
    assert entry["status"] == status


def test_a_lease_published_inside_the_retry_window_is_adopted(tmp_path: Path) -> None:
    """The window holds back the MINT, not the lease read: a process whose own
    refresh failed converges on the sibling's token on its next call."""
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [429]
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"  # failed, window open

    sibling = _mgr(tmp_path, ep, clock)
    sibling._cache[KEY] = mgr._cache[KEY]
    assert sibling.bearer_for(BASE, TENANT) == "tok-3"  # call 1 was tok-1, call 2 the refused attempt
    before = ep.calls
    clock.t += 1  # still inside the window
    assert mgr.bearer_for(BASE, TENANT) == "tok-3"
    assert ep.calls == before


# ── 2b. the lock is busy ─────────────────────────────────────────────────────


def test_an_expired_token_is_never_served_while_the_mint_lock_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock-busy shortcut hands back the cached token only while it is
    valid. Expired, the caller waits for the sibling's lease like a cold
    start and fails loud when none comes. Revert of the validity condition:
    the expired token is returned."""
    monkeypatch.setattr(data_token, "_REFRESH_LOCK_WAIT_S", 0.0)
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock, lock_wait_ceiling_seconds=0.3)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL + 10  # expired
    holder = _hold_the_mint_lock(tmp_path)
    try:
        with pytest.raises(DataTokenMintError, match="lock wait exceeded"):
            mgr.bearer_for(BASE, TENANT)
    finally:
        _release(holder)
    assert ep.calls == 1, "nothing minted while a sibling held the lock"


def test_a_busy_lock_with_a_valid_token_defers_the_next_attempt_by_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning the cached token because a sibling holds the lock must set the
    retry window, or every call re-enters the lock and waits again. Revert of
    the deferral: the second call re-opens the lock file."""
    monkeypatch.setattr(data_token, "_REFRESH_LOCK_WAIT_S", 0.0)
    opened: list[int] = []
    real_open = data_token._open_mint_lock_logged

    def counting_open(*a: Any, **k: Any) -> int:
        opened.append(1)
        return real_open(*a, **k)

    monkeypatch.setattr(data_token, "_open_mint_lock_logged", counting_open)
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    holder = _hold_the_mint_lock(tmp_path)
    try:
        opened.clear()
        assert mgr.bearer_for(BASE, TENANT) == "tok-1"
        assert len(opened) == 1
        clock.t += 10  # inside the 30s window
        assert mgr.bearer_for(BASE, TENANT) == "tok-1"
        assert len(opened) == 1, "a call inside the window went back to the busy lock"
        clock.t += 25  # window over
        assert mgr.bearer_for(BASE, TENANT) == "tok-1"
        assert len(opened) == 2, "the window never reopened"
    finally:
        _release(holder)


# ── 2c. a retry window does not outlive a replaced token ─────────────────────
#
# A token that replaces the cached one can come due before the old window
# ends only when its TTL is short; these tests give the replacement a 20s TTL
# (due at 4s left) so a window left behind by the old token would hold its
# refresh back.


def _fail_one_refresh(mgr: DataTokenManager, ep: _Endpoint, clock: _Clock) -> None:
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    ep.script[:] = [429]
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert KEY in mgr._refresh_retry_at


def test_invalidate_clears_the_retry_window(tmp_path: Path) -> None:
    """Revert: the cold mint after the invalidate leaves the old window in
    place and the 20s replacement is not refreshed when it comes due."""
    clock, ep = _Clock(), _Endpoint()
    ep.ttl_for[3] = 20  # call 1 tok-1, call 2 the refused refresh, call 3 the cold mint
    mgr = _mgr(tmp_path, ep, clock)
    _fail_one_refresh(mgr, ep, clock)
    mgr.invalidate(BASE, TENANT)
    assert mgr.bearer_for(BASE, TENANT) == "tok-3"
    clock.t += 17  # 3s of the 20s left: due at the nominal 4s, inside the old 30s window
    assert mgr.bearer_for(BASE, TENANT) == "tok-4"


def test_adopting_a_siblings_lease_clears_the_retry_window(tmp_path: Path) -> None:
    """The refusal's Retry-After holds the window to the cached token's last
    second (100s left, window 100s). The sibling's remint lives 101s, so it
    comes due 20s before it ends, inside that window. Revert (the adoption
    leaves the window): the adopted token is not refreshed when it comes due."""
    clock, ep = _Clock(), _Endpoint()
    ep.ttl_for[3] = 101  # the sibling's remint: ends 1s after tok-1
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL - 100  # 100s of tok-1 left
    ep.script[:] = [(429, {"Retry-After": "100"})]
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    assert mgr._refresh_retry_at[KEY] - clock.t == pytest.approx(100.0)

    sibling = _mgr(tmp_path, ep, clock)
    sibling._cache[KEY] = mgr._cache[KEY]
    assert sibling.bearer_for(BASE, TENANT) == "tok-3"
    clock.t += 1
    assert mgr.bearer_for(BASE, TENANT) == "tok-3"  # adopted
    assert KEY not in mgr._refresh_retry_at
    clock.t += 80  # 20s of the 101s left: due at 20.2s, still inside the old window
    assert mgr.bearer_for(BASE, TENANT) == "tok-4"


# ── 2d. what counts as a sibling's remint ────────────────────────────────────


def test_the_very_token_being_refreshed_is_not_a_remint_even_after_a_wall_clock_step(tmp_path: Path) -> None:
    """A wall clock stepped back 40s makes the cached token's own lease look
    40s newer once re-derived. Token equality, not a time tolerance, says it
    is not news: the refresh mints. Revert (a time-only comparison, or a 1s
    tolerance): the refresh adopts its own token and never mints."""
    clock, wall, ep = _Clock(), _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock, wall_clock=wall)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S
    wall.t += DUE_AFTER_S - 40
    assert mgr.bearer_for(BASE, TENANT) == "tok-2"
    assert ep.calls == 2


@pytest.mark.parametrize("lock_busy", [False, True], ids=["lock-free", "lock-busy"])
def test_a_different_token_that_expires_no_later_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_busy: bool,
) -> None:
    """A lease of another token that would END the cached token's life sooner
    is no improvement. Revert of the later-expiry condition (in either read,
    the first or the one beside the lock): it is adopted."""
    monkeypatch.setattr(data_token, "_REFRESH_LOCK_WAIT_S", 0.0)
    clock, ep = _Clock(), _Endpoint()
    mgr = _mgr(tmp_path, ep, clock)
    assert mgr.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += DUE_AFTER_S  # tok-1 has 684s left
    # A 300s token with 200s left: well above 20% of its own TTL, ends sooner.
    _write_foreign_lease(tmp_path, "tok-short", expires_at_wall=clock.t + 200, ttl=300)
    holder = _hold_the_mint_lock(tmp_path) if lock_busy else None
    try:
        got = mgr.bearer_for(BASE, TENANT)
    finally:
        if holder is not None:
            _release(holder)
    if lock_busy:
        assert got == "tok-1", "returned the cached token (still valid) without adopting"
        assert ep.calls == 1
    else:
        assert got == "tok-2", "minted its own refresh"
        assert ep.calls == 2


# ── 3. the refresh point is jittered per process ─────────────────────────────


def test_each_manager_draws_its_own_refresh_point_within_the_band(tmp_path: Path) -> None:
    lo = DataTokenManager(rng=_EdgeRng(high=False))._refresh_threshold
    hi = DataTokenManager(rng=_EdgeRng(high=True))._refresh_threshold
    assert lo == pytest.approx(_REFRESH_THRESHOLD - _REFRESH_THRESHOLD_JITTER) == pytest.approx(0.15)
    assert hi == pytest.approx(_REFRESH_THRESHOLD + _REFRESH_THRESHOLD_JITTER) == pytest.approx(0.25)


def test_borrowers_of_one_lease_get_distinct_refresh_instants(tmp_path: Path) -> None:
    """Revert: one fixed threshold, so all eight come due in the same second."""
    clock, ep = _Clock(), _Endpoint()
    rng = random.Random(20261025)
    managers = [_mgr(tmp_path, ep, clock, rng=rng) for _ in range(8)]
    for m in managers:
        assert m.bearer_for(BASE, TENANT) == "tok-1"  # one mint, seven borrows
    assert ep.calls == 1

    start = clock.t
    instants: list[float] = []
    for m in managers:
        cached = m._cache[KEY]
        due_at = None
        for step in range(TTL):
            clock.t = start + step
            if m._needs_refresh(cached):
                due_at = step
                break
        assert due_at is not None
        instants.append(due_at)
    assert len(set(instants)) == len(managers), f"managers share a refresh instant: {sorted(instants)}"
    assert all(0.75 * TTL <= i <= 0.85 * TTL + 1 for i in instants), sorted(instants)


@pytest.mark.parametrize(
    ("high", "left_fraction", "adopts"),
    [
        (True, 0.30, True),  # 30% left, due at 25%: adopt
        (True, 0.22, False),  # 22% left, due at 25%: the lease is already due for this manager
        (False, 0.18, True),  # 18% left, due at 15%: adopt, a fixed 20% bar would refuse it
        (False, 0.12, False),  # 12% left, due at 15%: due
    ],
    ids=["high-30", "high-22", "low-18", "low-12"],
)
def test_a_cold_reader_accepts_a_lease_exactly_when_it_is_not_due_for_its_own_refresh_point(
    tmp_path: Path, high: bool, left_fraction: float, adopts: bool,
) -> None:
    """The jittered refresh band (15% to 25%) straddles the old fixed 20%
    acceptance bar: a high draw adopted a lease that was refresh-due on its
    next call, a low draw minted past a lease it would have used. Now the bar
    is the manager's own refresh point, so an accepted lease is never due on
    the very next call and a refused one is. Revert: adopts and mints cross."""
    clock, ep = _Clock(), _Endpoint()
    producer = _mgr(tmp_path, ep, clock)
    assert producer.bearer_for(BASE, TENANT) == "tok-1"
    clock.t += TTL * (1 - left_fraction)

    reader = _mgr(tmp_path, ep, clock, rng=_EdgeRng(high))
    first = reader.bearer_for(BASE, TENANT)
    assert first == ("tok-1" if adopts else "tok-2")
    assert ep.calls == (1 if adopts else 2)
    # Never due on the very next call: no second mint, same token.
    assert reader.bearer_for(BASE, TENANT) == first
    assert ep.calls == (1 if adopts else 2)


@pytest.mark.parametrize("high", [False, True], ids=["low-draw", "high-draw"])
def test_the_lease_peeks_keep_the_fixed_twenty_percent_bar(tmp_path: Path, high: bool) -> None:
    """``has_fresh_lease`` (nx doctor) and ``fresh_lease_token`` (the mailbox
    drain) answer for a caller that never mints; their bar must not move with
    the manager's random refresh point."""
    clock, ep = _Clock(), _Endpoint()
    producer = _mgr(tmp_path, ep, clock)
    assert producer.bearer_for(BASE, TENANT) == "tok-1"
    peek = _mgr(tmp_path, ep, clock, rng=_EdgeRng(high))

    clock.t += TTL * (1 - 0.22)  # 22% left
    assert peek.has_fresh_lease(BASE, TENANT) is True
    assert peek.fresh_lease_token(BASE, TENANT) == "tok-1"
    clock.t += TTL * 0.04  # 18% left
    assert peek.has_fresh_lease(BASE, TENANT) is False
    assert peek.fresh_lease_token(BASE, TENANT) is None
    assert peek.fresh_lease_token(BASE, TENANT, near_expiry_threshold=0.0) == "tok-1"


# ── 4. nexus-kcdbx: full jitter on the mint backoff ──────────────────────────


def test_the_mint_backoff_is_full_jitter_under_the_schedule(tmp_path: Path) -> None:
    """Revert: the sleeps are exactly (1.0, 2.0) for every client."""

    def sleeps_for(seed: int) -> list[float]:
        slept: list[float] = []
        ep = _Endpoint()
        ep.script[:] = [503, 503, 503]
        mgr = DataTokenManager(
            clock=_Clock(), wall_clock=_Clock(), poster=ep, sleep=slept.append,
            mint_credential=lambda: "cred", mint_tenant=lambda: "", config_dir=tmp_path / str(seed),
            rng=random.Random(seed),
        )
        with pytest.raises(DataTokenMintError, match="503"):
            mgr.bearer_for(BASE, TENANT)
        return slept

    a, b = sleeps_for(1), sleeps_for(2)
    for slept in (a, b):
        assert len(slept) == 2
        assert all(0.0 <= s < ceiling for s, ceiling in zip(slept, _MINT_BACKOFF_SCHEDULE, strict=True)), slept
    assert a != b, "clients that failed together would retry together"


def test_the_backoff_schedule_ceilings_fit_the_retry_wall_constants() -> None:
    """Pins constants, not behaviour: the retry-wall derivation
    (``_MINT_RETRY_WALL_WORST_CASE_S``, and the lock-wait ceiling built on it)
    assumes an unforced backoff never exceeds the Retry-After cap. Full
    jitter only shortens a sleep, so the derivation holds iff the schedule's
    ceilings do. Fails if someone raises the schedule past the cap."""
    assert max(_MINT_BACKOFF_SCHEDULE) <= _MINT_RETRY_AFTER_CAP_S
    assert sum(_MINT_BACKOFF_SCHEDULE) <= data_token._MINT_MAX_ATTEMPTS * _MINT_RETRY_AFTER_CAP_S
