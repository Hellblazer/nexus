# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD-red for nexus-jwqjm: converge simultaneous stale-lease recoverers (flock).

LOCKED design: T2 ``nexus/design-jwqjm-t1-mint-race-flock.md`` (option (a),
flock-guarded double-check-then-mint-or-borrow). Plan: T2
``nexus/plan-jwqjm-t1-mint-race-flock.md``.

Targets two NOT-YET-BUILT symbols in :mod:`nexus.db.t1`:

* ``_t1_session_mint_lock_path(session_id, config_dir) -> Path`` -- mirrors
  ``_cli_dedicated_session_id``'s own per-purpose lock-file path helper
  (``config_dir / f"{name}.lock"``), shape:
  ``config_dir / f"t1_mint_{session_id}.lock"``.
* ``_lock_guarded_mint_or_borrow(session_id, config_dir) -> tuple[str, bool, float | None]``
  -- a SYNC helper (nx_plan_audit MEDIUM: extracted for testability) that
  ``fcntl.flock``-serializes the mint-or-borrow critical section: acquire the
  per-session lock, re-check ``read_t1_session_lease`` under the lock (a
  concurrent recoverer may have already won and published), return
  ``(leased_token, False, None)`` if fresh, else mint via
  ``mint_t1_session_token`` + ``publish_t1_session_lease`` and return
  ``(minted_token, True, ttl_seconds)`` -- ``ttl_seconds`` is the SAME value
  used for the publish, returned directly rather than recovered by a
  caller re-reading the lease file after the fact (code-review-expert
  Medium finding: a post-hoc re-read could observe a stale/unrelated file
  if the publish silently failed). Always releases the lock, including on
  a mint failure (the original ``RuntimeError`` propagates unchanged).

Plan-audit correction (folds an nx_plan_audit MEDIUM into this test file,
superseding an earlier draft of test #1): race the SYNC helper directly
across threads via a ``threading.Barrier``, exactly mirroring
``tests/db/test_t1_cli_dedicated_session.py::test_first_creation_race_is_safe``
(which races ``_cli_dedicated_session_id`` across 8 threads the same way).
This does NOT drive the full async ``_t1_lifespan`` across threads --
that needs a per-thread event loop and is out of scope for this bead
(Branch-0 wiring is nexus-jwqjm.2's job). Consequently these tests need NO
fake HTTP service harness: a tmp ``config_dir``, a monkeypatched
``nexus.db.t1.mint_t1_session_token`` counting fake, and the REAL
``publish_t1_session_lease`` / ``read_t1_session_lease`` against that tmp dir
are sufficient -- the double-check-under-lock genuinely reads whatever the
winner published.

Every test in this module is expected to fail at this stage with an
``AttributeError`` (the two helpers do not exist yet on ``nexus.db.t1``) --
that is the correct TDD-red state. Implementation (green) is nexus-jwqjm.2.
"""

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

import pytest

# ── Test 1: concurrent stale-lease recoverers converge to exactly one mint ──


def test_concurrent_stale_lease_recoverers_converge_to_one_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N threads race into `_lock_guarded_mint_or_borrow` for the SAME
    session_id, all starting from no published lease. Exactly one thread's
    call to `mint_t1_session_token` must actually fire -- every other
    thread must observe/borrow the winner's published lease rather than
    independently minting its own competing token."""
    from nexus.db import t1 as t1_mod

    session_id = "race-session-jwqjm"
    config_dir = tmp_path

    mint_calls: list[str] = []
    mint_calls_lock = threading.Lock()

    def _counting_fake_mint(session_id_arg: str, *, context: str = "") -> dict:
        with mint_calls_lock:
            n = len(mint_calls)
            mint_calls.append(session_id_arg)
        # A distinct token per call -- if more than one thread actually
        # mints, the tokens below would diverge and expose the bug.
        return {"session_token": f"tok-{n}", "expires_in_seconds": 3600}

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _counting_fake_mint)

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[tuple[str, bool, float | None]] = []
    results_lock = threading.Lock()
    errors: list[BaseException] = []

    def _worker() -> None:
        barrier.wait()
        try:
            result = t1_mod._lock_guarded_mint_or_borrow(session_id, config_dir)
        except BaseException as exc:  # noqa: BLE001 — surfaced explicitly below, not swallowed
            with results_lock:
                errors.append(exc)
            return
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # Fail on the ACTUAL worker exception (e.g. AttributeError on the
    # not-yet-built helper) rather than an indirect count mismatch --
    # pytest's default thread-exception handling only WARNS on exceptions
    # raised inside non-main threads, so without this the real cause would
    # be buried in the warnings section instead of the failure itself.
    if errors:
        raise errors[0]

    assert len(results) == n_threads

    # Exactly one real mint -- not >= 1 (project convention: exact fixture
    # regression assertions, never a loose lower bound).
    assert len(mint_calls) == 1, (
        f"expected exactly one mint call, got {len(mint_calls)}: {mint_calls}"
    )

    # Every thread's returned token equals the SAME (winner's) token --
    # borrowed, not independently minted.
    tokens = {token for token, _minted, _ttl in results}
    assert len(tokens) == 1, f"threads diverged on token: {tokens}"
    assert tokens == {"tok-0"}

    # Exactly one thread actually minted; every other thread borrowed.
    minted_results = [r for r in results if r[1]]
    borrowed_results = [r for r in results if not r[1]]
    assert len(minted_results) == 1, (
        f"expected exactly one thread to report minted=True, got {len(minted_results)}"
    )
    assert len(borrowed_results) == n_threads - 1

    # The minting thread's ttl_seconds is the mint response's own value
    # (3600, from _counting_fake_mint); every borrowing thread gets None --
    # a borrower never fabricates a TTL for a refresh task it must not start.
    assert minted_results[0][2] == 3600.0
    assert all(ttl is None for _token, _minted, ttl in borrowed_results)


# ── Test 2: single caller, no contention -- no regression on the uncontended path ──


def test_single_recoverer_mints_normally_no_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One caller, no contention: mint fires exactly once, the lease gets
    published, and the helper returns (token, minted=True). Backward-compat
    guard that the lock does not alter the uncontended path's behavior."""
    from nexus.db import t1 as t1_mod

    session_id = "solo-session-jwqjm"
    config_dir = tmp_path

    mint_calls: list[str] = []

    def _fake_mint(session_id_arg: str, *, context: str = "") -> dict:
        mint_calls.append(session_id_arg)
        return {"session_token": "solo-token", "expires_in_seconds": 3600}

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)

    token, minted, ttl_seconds = t1_mod._lock_guarded_mint_or_borrow(session_id, config_dir)

    assert token == "solo-token"
    assert minted is True
    assert ttl_seconds == 3600.0
    assert len(mint_calls) == 1
    assert mint_calls == [session_id]

    # The lease must actually have been published (real, unmocked
    # publish/read against the tmp config_dir).
    assert t1_mod.read_t1_session_lease(session_id, config_dir) == "solo-token"


# ── Test 3: mint failure still releases the lock and re-raises unchanged ──


def test_mint_failure_still_releases_lock_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mint failure must (a) propagate the original RuntimeError
    unchanged out of `_lock_guarded_mint_or_borrow`, and (b) actually
    release the flock -- verified by a fresh non-blocking LOCK_EX|LOCK_NB
    acquire on the same lock path succeeding afterward."""
    from nexus.db import t1 as t1_mod

    session_id = "fail-session-jwqjm"
    config_dir = tmp_path

    def _raising_fake_mint(session_id_arg: str, *, context: str = "") -> dict:
        raise RuntimeError("simulated mint failure for nexus-jwqjm")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _raising_fake_mint)

    with pytest.raises(RuntimeError, match="simulated mint failure for nexus-jwqjm"):
        t1_mod._lock_guarded_mint_or_borrow(session_id, config_dir)

    lock_path = t1_mod._t1_session_mint_lock_path(session_id, config_dir)
    fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pytest.fail(
                "lock was still held after a mint failure -- "
                "_lock_guarded_mint_or_borrow must release it even on error"
            )
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ── Test 4: lock path is per session id (pure unit, no service) ──


def test_lock_path_is_per_session_id(tmp_path: Path) -> None:
    from nexus.db import t1 as t1_mod

    config_dir = tmp_path

    path_a = t1_mod._t1_session_mint_lock_path("session-a", config_dir)
    path_b = t1_mod._t1_session_mint_lock_path("session-b", config_dir)

    assert path_a != path_b
    assert path_a == config_dir / "t1_mint_session-a.lock"
    assert path_b == config_dir / "t1_mint_session-b.lock"
