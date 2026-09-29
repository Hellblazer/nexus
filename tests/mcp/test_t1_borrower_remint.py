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
    monkeypatch.setattr(core, "_T1_RECOVERY_STATE", {})
    monkeypatch.setattr(core, "_OWNED_T1_MINTED", {})
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
    store.session_recovery = core._recover_t1_session
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


def test_rows_deleted_before_the_revoke_leave_a_recovering_borrower_an_empty_pad(owner, bg_loop) -> None:
    """The engine measurement behind the lifespan's choice to KEEP rows at
    owner exit: an explicit ``close_session`` before the revoke empties the
    pad for a borrower that recovers afterwards. The owner no longer does it
    (see ``test_a_clean_owner_exit_keeps_the_pad_for_a_surviving_borrower``)."""
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
        got = core._recover_t1_session(sid, owner_token)
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

    got = core._recover_t1_session(sid, owner_token)

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

    assert core._recover_t1_session(sid, owner_token) is None
    assert mints == []
    assert core._OWNED_T1_SESSION == {}


def test_a_borrower_of_another_session_id_never_mints_for_this_one(owner, bg_loop, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed("some-other-session", cfg, bg_loop)
    mints = _count_mints(monkeypatch)

    assert core._recover_t1_session(sid, owner_token) is None
    assert mints == []


# ── bounded, one attempt per failure ────────────────────────────────────────


def test_a_failed_remint_costs_one_attempt_then_a_cooldown(owner, bg_loop, monkeypatch) -> None:
    sid, _owner_token, cfg = owner
    borrower = _borrower_store()
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    attempts: list[str] = []

    def _down(session_id, *, context=""):
        attempts.append(context)
        raise RuntimeError("T1 session token mint failed: service unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _down)

    for _ in range(2):
        with pytest.raises(RuntimeError, match=SESSION_UNAUTHORIZED_MARKER.split(" (")[0]):
            borrower.put("service is down")
    # One attempt, then the cooldown: the second failed call pays no flock
    # wait, no mint round trip.
    assert len(attempts) == 1
    core._T1_RECOVERY_STATE[sid]["next_at"] = 0.0  # the cooldown elapses
    with pytest.raises(RuntimeError, match=SESSION_UNAUTHORIZED_MARKER.split(" (")[0]):
        borrower.put("service is still down")
    assert len(attempts) == 2
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
    mcp_infra.set_t1_session_recovery_hook(core._recover_t1_session)
    t1, _ = mcp_infra.get_t1()
    assert t1.session_recovery is core._recover_t1_session

    mcp_infra.set_t1_session_recovery_hook(None)  # also reaches the live instance
    assert t1.session_recovery is None


def test_lifespan_borrower_survives_the_owners_exit_and_owns_its_teardown(owner, monkeypatch) -> None:
    """The whole path: nx-mcp starts as a USE_LEASED borrower, the owner's real
    teardown revokes the token underneath it, the next scratch call recovers,
    and the lifespan's own exit then revokes and clears. The pad is kept."""
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
    kept = [e["content"] for e in HttpScratchStore(session_id=sid, _session_token=fresh).list_entries()]
    assert "after the owner left" in kept, "the clean exit keeps the pad for the next incarnation"
    assert core._BORROWED_T1_SESSION == {}
    assert mcp_infra._t1_session_recovery_hook is None


# ═════════════════════════════════════════════════════════════════════════════
# Fix round (reviews of 26174cd65): rows kept, flagged rows drained, owners
# recover too, every wiring site pinned.
# ═════════════════════════════════════════════════════════════════════════════

from unittest.mock import MagicMock  # noqa: E402 — fix-round block

from nexus.daemon.t1_handoff import write_handoff_marker  # noqa: E402
from nexus.db import http_scratch_store as scratch_mod  # noqa: E402

_CLAUDE_PID = 4242
_MCP_PID = 4300


def _resolve_as(monkeypatch, sid: str) -> None:
    """Make the lifespan see a process that inherited no token and resolves *sid*."""
    monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: sid)
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)


def _contents(sid: str, token: str) -> list[str]:
    return [e["content"] for e in HttpScratchStore(session_id=sid, _session_token=token).list_entries()]


def _revoke(sid: str) -> None:
    with HttpTokenStore(prefer_data_token=True) as ts:
        ts.close_session(sid)


# ── item 1: a never-recovered borrower's exit never touches the owner's pad ──


def test_a_deferred_mint_borrower_exit_leaves_the_owners_pad_token_and_lease(owner, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    HttpScratchStore().put("the live owner's row")
    t1_mod.clear_t1_session_lease(sid, cfg)  # nobody owns a lease at startup
    _resolve_as(monkeypatch, sid)

    def _down(session_id, *, context=""):
        raise RuntimeError("T1 session token mint failed: service unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _down)
    seen: dict = {}

    async def _run() -> None:
        async with core._t1_lifespan(None):
            assert core._DEFERRED_T1_MINT["session_id"] == sid
            publish_t1_session_lease(sid, owner_token, cfg, ttl_seconds=3600)  # the owner comes up
            await asyncio.to_thread(mcp_infra.get_t1)  # first T1 use: the deferred mint borrows
            seen["borrowed"] = core._BORROWED_T1_SESSION.get("session_id")
            seen["owned"] = dict(core._OWNED_T1_SESSION)
            seen["hook"] = mcp_infra._t1_session_recovery_hook

    asyncio.run(_run())

    assert seen["borrowed"] == sid and seen["owned"] == {}
    assert seen["hook"] is core._recover_t1_session
    assert read_t1_session_lease(sid, cfg) == owner_token, "a borrower clears no lease"
    assert "the live owner's row" in _contents(sid, owner_token), (
        "a borrower's clean exit deletes no rows and revokes nothing"
    )
    assert core._BORROWED_T1_SESSION == {}
    assert mcp_infra._t1_session_recovery_hook is None


# ── item 4: the mint-race loser wiring ──────────────────────────────────────


def test_the_mint_race_loser_is_a_borrower_and_its_exit_touches_nothing(owner, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    HttpScratchStore().put("the live owner's row")
    _resolve_as(monkeypatch, sid)
    real_read = t1_mod.read_t1_session_lease
    reads = {"n": 0}

    def _blind_to_the_lease_once(session_id, config_dir):
        # The routing read at startup sees no lease (the sibling had not
        # published yet); the read under the mint flock finds it.
        reads["n"] += 1
        return None if reads["n"] == 1 else real_read(session_id, config_dir)

    monkeypatch.setattr(t1_mod, "read_t1_session_lease", _blind_to_the_lease_once)
    mints = _count_mints(monkeypatch)
    seen: dict = {}

    async def _run() -> None:
        async with core._t1_lifespan(None):
            seen["borrowed"] = core._BORROWED_T1_SESSION.get("session_id")
            seen["owned"] = dict(core._OWNED_T1_SESSION)
            seen["hook"] = mcp_infra._t1_session_recovery_hook

    asyncio.run(_run())

    assert mints == []
    assert seen["borrowed"] == sid and seen["owned"] == {}
    assert seen["hook"] is core._recover_t1_session
    assert real_read(sid, cfg) == owner_token
    assert "the live owner's row" in _contents(sid, owner_token)


# ── item 4: handoff wiring, driven through the real tick ────────────────────


@pytest.fixture
def handoff(monkeypatch):
    monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID)
    monkeypatch.setattr(core, "_T1_SERVER_START_TIME", 0.0)
    monkeypatch.setattr(core, "_DEFERRED_T1_MINT", {})
    monkeypatch.setattr(core, "_start_channel_waiter", lambda: None)
    monkeypatch.setattr(core, "_T1_HANDOFF_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(core, "_T1_HANDOFF_BACKOFF_SESSION_ID", None)
    monkeypatch.setattr(core, "_T1_HANDOFF_NEXT_ATTEMPT_AT", 0.0)
    monkeypatch.setattr(core, "_T1_HANDOFF_GIVE_UP_LOGGED", False)
    yield
    task = core._T1_SESSION_REFRESH_TASK
    if task is not None:
        task.cancel()


def _write_marker(new_sid: str) -> None:
    write_handoff_marker(_MCP_PID, new_session_id=new_sid, claude_pid=_CLAUDE_PID, config_dir=nexus_config_dir())


@pytest.mark.asyncio
async def test_a_handoff_that_borrows_makes_this_process_a_borrower_of_the_new_session(handoff, monkeypatch) -> None:
    cfg = nexus_config_dir()
    new_sid = str(uuid.uuid4())
    token = mint_t1_session_token(new_sid, context="the new session's owner")["session_token"]
    publish_t1_session_lease(new_sid, token, cfg, ttl_seconds=3600)
    mints = _count_mints(monkeypatch)
    _write_marker(new_sid)

    await core._t1_handoff_tick(_MCP_PID, MagicMock())

    assert mints == []
    assert core._BORROWED_T1_SESSION.get("session_id") == new_sid
    assert core._OWNED_T1_SESSION == {}
    assert mcp_infra._t1_session_recovery_hook is core._recover_t1_session


@pytest.mark.asyncio
async def test_a_handoff_that_mints_ends_the_old_borrower_state_and_owns_the_new_session(handoff) -> None:
    cfg = nexus_config_dir()
    old_sid = os.environ["NX_T1_SESSION_ID"]
    core._note_t1_borrowed(old_sid, cfg, asyncio.get_running_loop())
    mcp_infra.set_t1_session_recovery_hook(None)  # so only the handoff itself can arm it
    new_sid = str(uuid.uuid4())  # no lease: this tick mints
    _write_marker(new_sid)

    await core._t1_handoff_tick(_MCP_PID, MagicMock())

    assert core._BORROWED_T1_SESSION == {}, "the old session's borrower state must not outlive the handoff"
    assert core._OWNED_T1_SESSION == {"session_id": new_sid}
    assert mcp_infra._t1_session_recovery_hook is core._recover_t1_session, "an owner recovers too"


@pytest.mark.asyncio
async def test_a_recovery_in_flight_across_a_handoff_never_owns_the_old_session(owner, handoff, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, asyncio.get_running_loop())
    _owner_exits(sid)
    entered, release = threading.Event(), threading.Event()
    real = t1_mod.mint_t1_session_token

    def _slow(session_id, *, context=""):
        out = real(session_id, context=context)
        entered.set()
        release.wait(10)
        return out

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _slow)
    recovery = asyncio.create_task(asyncio.to_thread(core._recover_t1_session, sid, owner_token))
    assert await asyncio.to_thread(entered.wait, 10)

    new_sid = str(uuid.uuid4())
    new_token = real(new_sid, context="the new session's owner")["session_token"]
    publish_t1_session_lease(new_sid, new_token, cfg, ttl_seconds=3600)
    _write_marker(new_sid)
    await core._t1_handoff_tick(_MCP_PID, MagicMock())
    release.set()

    assert await recovery is None
    assert core._OWNED_T1_SESSION.get("session_id") != sid
    assert read_t1_session_lease(sid, cfg) is None, "the abandoned recovery withdrew the lease it published"


# ── item 4: the shutdown decline and the abandon branch ─────────────────────


def test_recovery_declines_once_shutdown_is_in_flight(owner, bg_loop, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)
    monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", True)
    mints = _count_mints(monkeypatch)

    assert core._recover_t1_session(sid, owner_token) is None
    assert mints == []


@pytest.mark.parametrize("raced_by", ["shutdown", "handoff"])
def test_a_recovery_that_loses_a_race_to_teardown_withdraws_its_lease(owner, bg_loop, monkeypatch, raced_by) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)
    real = t1_mod.mint_t1_session_token

    def _mint_then_lose_the_race(session_id, *, context=""):
        out = real(session_id, context=context)
        if raced_by == "shutdown":
            monkeypatch.setattr(core, "_SHUTDOWN_IN_FLIGHT", True)
        else:
            core._BORROWED_T1_SESSION.clear()
        return out

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _mint_then_lose_the_race)

    assert core._recover_t1_session(sid, owner_token) is None
    assert core._OWNED_T1_SESSION == {}
    assert read_t1_session_lease(sid, cfg) is None


# ── item 2: the owner's clean exit drains flagged rows and keeps the pad ────


def test_a_clean_owner_exit_keeps_the_pad_for_a_surviving_borrower(monkeypatch) -> None:
    """Sam's decision B has a survivor, so the owner's clean exit must not
    delete the pad under it. Measured before this change: the SIGTERM path
    (the documented normal stdio shutdown) already left rows, and the engine's
    scheduled sweep (24h) reaps whatever nobody claims."""
    sid = str(uuid.uuid4())
    _resolve_as(monkeypatch, sid)
    title = f"k9sec-{uuid.uuid4().hex[:8]}"

    async def _run() -> None:
        async with core._t1_lifespan(None):
            assert core._OWNED_T1_SESSION == {"session_id": sid}
            assert mcp_infra._t1_session_recovery_hook is core._recover_t1_session, "an owner recovers too"
            t1, _ = mcp_infra.get_t1()
            await asyncio.to_thread(t1.put, "plain scratch the survivor keeps")
            await asyncio.to_thread(
                t1.put, "flagged note", persist=True, flush_project="nexus_test_k9sec", flush_title=title,
            )

    asyncio.run(_run())

    got = mcp_infra.t2_index_write(lambda db: db.memory.get(project="nexus_test_k9sec", title=title))
    assert got is not None and got["content"] == "flagged note", "the drain must run while the rows exist"
    fresh = mint_t1_session_token(sid, context="the survivor")["session_token"]
    assert "plain scratch the survivor keeps" in _contents(sid, fresh)


# ── item 3: an owner whose token 401s recovers too, boundedly ───────────────


def _owner_store(sid: str) -> HttpScratchStore:
    core._OWNED_T1_SESSION["session_id"] = sid
    mcp_infra.set_t1_session_recovery_hook(core._recover_t1_session)
    store = HttpScratchStore()
    store.session_recovery = core._recover_t1_session
    return store


def test_an_owner_whose_token_401s_re_mints_once_under_the_flock(owner, monkeypatch) -> None:
    sid, owner_token, cfg = owner  # lease names the owner's token, as a failed republish would leave it
    store = _owner_store(sid)
    _revoke(sid)  # the displaced-by-a-successor / expired-under-a-stalled-owner shape
    mints = _count_mints(monkeypatch)

    entry_id = store.put("after my own token died")

    assert store.get(entry_id)["content"] == "after my own token died"
    assert len(mints) == 1
    assert core._OWNED_T1_SESSION == {"session_id": sid}
    new_token = read_t1_session_lease(sid, cfg)
    assert new_token and new_token != owner_token and os.environ["NX_T1_SESSION"] == new_token


def test_owner_recovery_is_bounded_by_a_cooldown(owner, monkeypatch) -> None:
    """Ping-pong guard: a second death right after a recovery is declined."""
    sid, _tok, _cfg = owner
    store = _owner_store(sid)
    mints = _count_mints(monkeypatch)
    _revoke(sid)
    store.put("first recovery")
    _revoke(sid)

    with pytest.raises(RuntimeError, match="unauthorized"):
        store.put("second death inside the cooldown")

    assert len(mints) == 1


def test_owner_recovery_is_bounded_by_a_cap(owner, monkeypatch) -> None:
    sid, _tok, _cfg = owner
    monkeypatch.setattr(core, "_T1_RECOVERY_COOLDOWN_S", 0.0)
    monkeypatch.setattr(core, "_T1_RECOVERY_MAX_MINTS", 2)
    store = _owner_store(sid)
    mints = _count_mints(monkeypatch)

    for _ in range(2):
        _revoke(sid)
        store.put("recovers")
    _revoke(sid)
    with pytest.raises(RuntimeError, match="unauthorized"):
        store.put("over the cap")

    assert len(mints) == 2


def test_a_displaced_owners_exit_revokes_and_clears_nothing_of_its_successors(owner) -> None:
    sid, own_token, cfg = owner
    core._OWNED_T1_SESSION["session_id"] = sid
    core._note_t1_minted(sid, own_token)
    successor = mint_t1_session_token(sid, context="successor owner")["session_token"]  # rotates: own_token is dead
    publish_t1_session_lease(sid, successor, cfg, ttl_seconds=3600)
    assert own_token != successor

    core._t1_session_shutdown()

    assert read_t1_session_lease(sid, cfg) == successor
    HttpScratchStore(session_id=sid, _session_token=successor).list_entries()  # still resolves


# ── item 6: the failed-mint cooldown is per session ─────────────────────────


def test_the_cooldown_after_a_failed_mint_is_recorded_per_session(owner, bg_loop, monkeypatch) -> None:
    sid, owner_token, cfg = owner
    core._note_t1_borrowed(sid, cfg, bg_loop)
    _owner_exits(sid)

    def _down(session_id, *, context=""):
        raise RuntimeError("service unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _down)
    before = time.monotonic()

    assert core._recover_t1_session(sid, owner_token) is None

    assert core._T1_RECOVERY_STATE[sid]["next_at"] >= before + core._T1_RECOVERY_COOLDOWN_S - 1


# ── items 7 and 8: guidance text and the latch's escape ─────────────────────


def test_the_recovered_suffix_does_not_claim_a_mint_the_hook_may_not_have_done() -> None:
    HEAL_RECOVERED_SUFFIX = scratch_mod.HEAL_RECOVERED_SUFFIX  # noqa: N806 — the constant's own name

    text = f"{SESSION_UNAUTHORIZED_MARKER} on /v1/t1/put {HEAL_RECOVERED_SUFFIX}: bad"
    message = core._mcp_tool_error("scratch", RuntimeError(text))

    assert "re-minted" not in HEAL_RECOVERED_SUFFIX  # the hook can also adopt a sibling's lease
    assert "Reconnect" in message and "session recovery" in message


def test_the_recovery_latch_expires_so_a_dead_sibling_lease_is_not_final(owner, monkeypatch) -> None:
    sid, _tok, _cfg = owner
    monkeypatch.setattr(scratch_mod, "_RECOVERY_FUTILE_WINDOW_S", 0.3)
    store = HttpScratchStore()
    calls: list[str] = []

    def _adopts_a_dead_sibling_lease(session_id: str, dead_token: str) -> str:
        calls.append(dead_token)
        return "dead-sibling-token"

    store.session_recovery = _adopts_a_dead_sibling_lease
    _revoke(sid)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="unauthorized"):
            store.put("still unauthorized")
    assert len(calls) == 1  # latched inside the window
    time.sleep(0.4)
    with pytest.raises(RuntimeError, match="unauthorized"):
        store.put("window over")
    assert len(calls) == 2  # the window is over: recovery ran again


def test_a_deferred_mint_that_mints_makes_this_process_an_owner_that_recovers(monkeypatch) -> None:
    """The deferred-mint OWNER arm: the service was down at start, comes up at
    first T1 use, and this process mints. It owns the session, remembers the
    token it minted, and has the recovery hook armed."""
    sid = str(uuid.uuid4())
    _resolve_as(monkeypatch, sid)
    real = t1_mod.mint_t1_session_token
    calls = {"n": 0}

    def _down_then_up(session_id, *, context=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("T1 session token mint failed: service unreachable")
        return real(session_id, context=context)

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _down_then_up)
    seen: dict = {}

    async def _run() -> None:
        async with core._t1_lifespan(None):
            assert core._DEFERRED_T1_MINT["session_id"] == sid
            await asyncio.to_thread(mcp_infra.get_t1)
            seen["owned"] = dict(core._OWNED_T1_SESSION)
            seen["minted"] = set(core._OWNED_T1_MINTED.get(sid, set()))
            seen["token"] = os.environ["NX_T1_SESSION"]
            seen["hook"] = mcp_infra._t1_session_recovery_hook

    asyncio.run(_run())

    assert seen["owned"] == {"session_id": sid}
    assert seen["minted"] == {seen["token"]}
    assert seen["hook"] is core._recover_t1_session
    assert read_t1_session_lease(sid, nexus_config_dir()) is None, "the owner's clean exit clears its lease"


def test_a_lifespan_owner_displaced_mid_session_leaves_its_successor_alone_at_exit(monkeypatch) -> None:
    """The whole displaced-owner path through a real lifespan: a successor
    mints (rotating this owner's token dead) and publishes its lease; this
    owner's clean exit must not revoke the session or clear that lease, even
    though its own store adopted the successor's token into the process env
    during the exit drain (the 401 self-heal), which is why teardown compares
    against the tokens this process minted, not against the env."""
    sid = str(uuid.uuid4())
    cfg = nexus_config_dir()
    _resolve_as(monkeypatch, sid)
    seen: dict = {}

    async def _run() -> None:
        async with core._t1_lifespan(None):
            t1, _ = mcp_infra.get_t1()
            await asyncio.to_thread(t1.put, "before the successor")
            seen["successor"] = mint_t1_session_token(sid, context="successor owner")["session_token"]
            publish_t1_session_lease(sid, seen["successor"], cfg, ttl_seconds=3600)

    asyncio.run(_run())

    assert read_t1_session_lease(sid, cfg) == seen["successor"]
    assert "before the successor" in _contents(sid, seen["successor"]), "the successor's token still resolves"
