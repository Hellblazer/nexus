# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-k9sec: a T1 borrower survives its lease owner's exit.

An nx-mcp that bound T1 by borrowing a live lease (USE_LEASED, or the
mint-race loser) shares the owner's token. The owner's teardown clears the
lease and revokes the token (``POST /v1/sessions/close`` deletes the
``session_tokens`` row), so the borrower's next T1 call gets HTTP 401
(``AuthFilter`` ``session_not_minted``). Sam's decision B (2026-09-29): the
borrower re-mints its own token for the same session id under the existing
mint flock and becomes the owner.

Everything here runs against the real engine substrate: a real minted
session, the real ``_t1_session_shutdown`` for the owner's exit, the real
lease files and mint flock.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid

import pytest

from nexus import mcp_infra
from nexus.config import nexus_config_dir
from nexus.db import t1 as t1_mod
from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER, HttpScratchStore
from nexus.db.t1 import (
    _lock_guarded_mint_or_borrow,
    mint_t1_session_token,
    publish_t1_session_lease,
    read_t1_session_lease,
)
from nexus.db.t2.http_token_store import HttpTokenStore
from nexus.mcp import core


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    """Every test starts and ends with no borrower/owner state, no T1
    singleton, no recovery hook, and the shutdown latch open."""
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    monkeypatch.setattr(core, "_BORROWED_T1_SESSION", {})
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", False)
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    mcp_infra.reset_t1_for_release()
    mcp_infra.set_t1_session_recovery_hook(None)
    yield
    mcp_infra.set_t1_session_recovery_hook(None)
    mcp_infra.reset_t1_for_release()


@pytest.fixture
def bg_loop():
    """A live event loop on a thread: the loop the lifespan would own, so a
    recovered borrower can schedule its refresh task onto it."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    asyncio.run_coroutine_threadsafe(core._cancel_t1_session_refresh_task(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(10)
    loop.close()


@pytest.fixture
def owner():
    """A live owner: the conftest-minted session with its lease published."""
    sid = os.environ["NX_T1_SESSION_ID"]
    token = os.environ["NX_T1_SESSION"]
    cfg = nexus_config_dir()
    publish_t1_session_lease(sid, token, cfg, ttl_seconds=3600)
    return sid, token, cfg


def _borrower_store() -> HttpScratchStore:
    store = HttpScratchStore()
    store.session_recovery = core._recover_borrowed_t1_session
    return store


def _owner_exits(sid: str) -> None:
    """The owner process's real teardown: flush, clear the lease, revoke."""
    core._OWNED_T1_SESSION["session_id"] = sid
    core._t1_session_shutdown()
    assert core._OWNED_T1_SESSION == {}


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _count_mints(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = t1_mod.mint_t1_session_token

    def _counting(session_id, *, context=""):
        calls.append(context)
        return real(session_id, context=context)

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _counting)
    return calls


# ── recovery after the owner's teardown ─────────────────────────────────────


def test_borrower_recovers_and_becomes_owner_after_owner_teardown(owner, bg_loop) -> None:
    sid, owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    assert borrower.put("written while the owner lived")

    _owner_exits(sid)
    assert read_t1_session_lease(sid, cfg) is None  # the owner cleared its lease

    entry_id = borrower.put("written after the owner exited")

    assert borrower.get(entry_id)["content"] == "written after the owner exited"
    assert core._OWNED_T1_SESSION == {"session_id": sid}
    assert core._BORROWED_T1_SESSION == {}
    new_token = read_t1_session_lease(sid, cfg)
    assert new_token and new_token != owner_token, "the borrower published its own lease"
    assert os.environ["NX_T1_SESSION"] == new_token
    assert _wait_for(lambda: core._T1_SESSION_REFRESH_TASK is not None), (
        "the new owner must refresh its own token"
    )


def test_recovered_borrower_teardown_revokes_and_clears_its_own_lease(owner, bg_loop) -> None:
    sid, _owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    borrower.put("forces the recovery")
    new_token = read_t1_session_lease(sid, cfg)
    assert new_token

    core._t1_session_shutdown()  # the recovered owner's own exit, same _OWNED path

    assert read_t1_session_lease(sid, cfg) is None
    with pytest.raises(RuntimeError, match="unauthorized"):
        HttpScratchStore(session_id=sid, _session_token=new_token).list_entries()


def test_a_crashed_owner_leaves_a_lease_naming_a_dead_token(owner, bg_loop, monkeypatch) -> None:
    """SIGKILL: no teardown ran, so the lease file still names the revoked
    token and still reads as fresh. Borrowing it again would 401 forever; the
    recovery must mint instead."""
    sid, owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    with HttpTokenStore(prefer_data_token=True) as ts:
        ts.close_session(sid)
    assert read_t1_session_lease(sid, cfg) == owner_token
    mints = _count_mints(monkeypatch)

    assert borrower.put("after the crash")

    assert len(mints) == 1
    assert read_t1_session_lease(sid, cfg) != owner_token
    assert core._OWNED_T1_SESSION == {"session_id": sid}


# ── what the revoke does to the session's rows (measured, 2026-09-29) ───────


def test_revoke_alone_leaves_the_rows_reachable_to_a_new_token(owner, bg_loop) -> None:
    """The owner's SIGTERM path revokes the token but never deletes rows, and
    the rows are keyed by (tenant, session_id), not by token. The recovered
    borrower therefore SEES the pad the owner left."""
    sid, _owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    kept = borrower.put("row written before the owner exited")

    _owner_exits(sid)

    assert kept in [e["id"] for e in borrower.list_entries()]


def test_a_clean_owner_exit_that_closed_the_rows_leaves_an_empty_pad(owner, bg_loop) -> None:
    """The lifespan's clean exit deletes the rows (``close_session``) before
    the revoke, so a borrower recovering after THAT starts with an empty pad."""
    sid, _owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    borrower.put("row that dies with the clean exit")

    HttpScratchStore().close_session()
    _owner_exits(sid)

    assert borrower.list_entries() == []
    assert core._OWNED_T1_SESSION == {"session_id": sid}


# ── single minter ───────────────────────────────────────────────────────────


def test_two_recoverers_of_one_departed_owner_mint_exactly_once(monkeypatch, tmp_path) -> None:
    sid = str(uuid.uuid4())
    dead = mint_t1_session_token(sid, context="owner")["session_token"]
    publish_t1_session_lease(sid, dead, tmp_path, ttl_seconds=3600)  # crashed owner's lease
    with HttpTokenStore(prefer_data_token=True) as ts:
        ts.close_session(sid)
    mints = _count_mints(monkeypatch)

    barrier = threading.Barrier(2)
    results: list[tuple[str, bool, float | None]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _recover() -> None:
        barrier.wait()
        try:
            got = _lock_guarded_mint_or_borrow(sid, tmp_path, stale_token=dead)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(got)

    threads = [threading.Thread(target=_recover) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(mints) == 1
    assert sorted(minted for _tok, minted, _ttl in results) == [False, True]
    assert len({tok for tok, _m, _t in results}) == 1
    (token,) = {tok for tok, _m, _t in results}
    assert token != dead
    assert HttpScratchStore(session_id=sid, _session_token=token).list_entries() == []


def test_two_borrower_calls_in_one_process_end_with_one_token(owner, bg_loop, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    mints = _count_mints(monkeypatch)

    barrier = threading.Barrier(2)
    tokens: list[str | None] = []
    lock = threading.Lock()

    def _recover() -> None:
        barrier.wait()
        got = core._recover_borrowed_t1_session(sid, owner_token)
        with lock:
            tokens.append(got)

    threads = [threading.Thread(target=_recover) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(mints) == 1
    assert len(set(tokens)) == 1 and tokens[0] and tokens[0] != owner_token
    assert core._OWNED_T1_SESSION == {"session_id": sid}


def test_a_borrower_adopts_a_sibling_recoverers_fresh_lease_without_minting(
    owner, bg_loop, monkeypatch,
) -> None:
    """Another process already re-minted and published: this borrower takes
    that lease and stays a borrower (the single-minter rule)."""
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    sibling_token = mint_t1_session_token(sid, context="sibling recoverer")["session_token"]
    publish_t1_session_lease(sid, sibling_token, cfg, ttl_seconds=3600)
    mints = _count_mints(monkeypatch)

    got = core._recover_borrowed_t1_session(sid, owner_token)

    assert got == sibling_token
    assert mints == []
    assert core._OWNED_T1_SESSION == {}
    assert core._BORROWED_T1_SESSION.get("session_id") == sid


# ── a borrower never touches what it does not own ───────────────────────────


def test_a_borrowers_teardown_never_revokes_or_clears_the_owners_lease(owner, bg_loop) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)

    core._t1_shutdown()
    core._t1_session_shutdown()

    assert read_t1_session_lease(sid, cfg) == owner_token
    assert HttpScratchStore().list_entries() is not None  # the owner's token still resolves


def test_a_process_that_borrows_nothing_never_mints(owner, monkeypatch) -> None:
    """The inherited-token nested subprocess and the unresolved-session
    process are not borrowers: the hook refuses without minting."""
    sid, owner_token, _cfg = owner
    mints = _count_mints(monkeypatch)

    assert core._recover_borrowed_t1_session(sid, owner_token) is None
    assert mints == []
    assert core._OWNED_T1_SESSION == {}


def test_a_borrower_of_another_session_id_never_mints_for_this_one(owner, bg_loop, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed("some-other-session", cfg, bg_loop)
    mints = _count_mints(monkeypatch)

    assert core._recover_borrowed_t1_session(sid, owner_token) is None
    assert mints == []


# ── bounded, one attempt per failure ────────────────────────────────────────


def test_a_failed_remint_costs_one_attempt_per_failed_call(owner, bg_loop, monkeypatch) -> None:
    sid, _owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    attempts: list[str] = []

    def _down(session_id, *, context=""):
        attempts.append(context)
        raise RuntimeError("T1 session token mint failed: service unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _down)

    for expected in (1, 2):
        with pytest.raises(RuntimeError, match=SESSION_UNAUTHORIZED_MARKER.split(" (")[0]):
            borrower.put("service is down")
        assert len(attempts) == expected  # one re-mint attempt per failure, no loop
    assert core._OWNED_T1_SESSION == {}
    assert core._BORROWED_T1_SESSION.get("session_id") == sid


def test_a_recovery_that_does_not_cure_the_401_is_not_repeated_every_call(owner) -> None:
    """The recovery latch: once a recovered token still 401s, the token was
    never the cause, so later calls stop re-minting until a call succeeds."""
    sid, _owner_token, _cfg = owner
    store = HttpScratchStore()
    calls: list[tuple[str, str]] = []

    def _useless(session_id: str, dead_token: str) -> str:
        calls.append((session_id, dead_token))
        return "another-token-the-engine-does-not-know"

    store.session_recovery = _useless
    with HttpTokenStore(prefer_data_token=True) as ts:
        ts.close_session(sid)

    for _ in range(3):
        with pytest.raises(RuntimeError, match="unauthorized"):
            store.put("still unauthorized")
    assert len(calls) == 1


def test_only_a_401_reaches_the_recovery_hook(owner) -> None:
    store = HttpScratchStore()
    calls: list[tuple[str, str]] = []
    store.session_recovery = lambda sid, tok: calls.append((sid, tok))

    assert store.get("no-such-entry-id") is None  # 404, the token is fine
    assert store.list_entries() is not None
    assert calls == []


# ── wiring ──────────────────────────────────────────────────────────────────


def test_get_t1_attaches_the_registered_recovery_hook(owner) -> None:
    mcp_infra.set_t1_session_recovery_hook(core._recover_borrowed_t1_session)
    t1, _ = mcp_infra.get_t1()
    assert t1.session_recovery is core._recover_borrowed_t1_session

    mcp_infra.set_t1_session_recovery_hook(None)  # also reaches the live instance
    assert t1.session_recovery is None


def test_lifespan_borrower_survives_the_owners_exit_and_owns_its_teardown(owner, monkeypatch) -> None:
    """The whole path: nx-mcp starts as a USE_LEASED borrower, the owner's real
    teardown revokes the token underneath it, the next scratch call recovers,
    and the lifespan's own exit then revokes, clears and deletes the rows."""
    sid, owner_token, cfg = owner
    monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: sid)
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    seen: dict = {}

    async def _run() -> None:
        async with core._t1_lifespan(None):
            assert core._BORROWED_T1_SESSION.get("session_id") == sid
            assert core._OWNED_T1_SESSION == {}
            t1, _ = mcp_infra.get_t1()
            await asyncio.to_thread(t1.put, "borrowed and healthy")

            await asyncio.to_thread(_owner_exits, sid)

            entry_id = await asyncio.to_thread(t1.put, "after the owner left")
            seen["got"] = (await asyncio.to_thread(t1.get, entry_id))["content"]
            seen["owned"] = dict(core._OWNED_T1_SESSION)
            seen["token"] = read_t1_session_lease(sid, cfg)

    asyncio.run(_run())

    assert seen["got"] == "after the owner left"
    assert seen["owned"] == {"session_id": sid}
    assert seen["token"] and seen["token"] != owner_token
    assert read_t1_session_lease(sid, cfg) is None, "the recovered owner's exit clears its lease"
    with pytest.raises(RuntimeError, match="unauthorized"):
        HttpScratchStore(session_id=sid, _session_token=seen["token"]).list_entries()
    fresh = mint_t1_session_token(sid, context="post-mortem")["session_token"]
    assert HttpScratchStore(session_id=sid, _session_token=fresh).list_entries() == [], (
        "the lifespan's clean exit deletes the pad"
    )
    assert core._BORROWED_T1_SESSION == {}
    assert mcp_infra._t1_session_recovery_hook is None
