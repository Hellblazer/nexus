# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mgu1k: the MCP server drains its flagged T1 scratch entries to T2
before its teardown clears the session lease and revokes the token.

The detached SessionEnd flush did this, racing the same teardown on the same
stdin EOF, and lost every time measured (6 of 6 session_end events logged
``session_end_flush_t1_unavailable``): the lease was gone and the token
revoked before it ran, so flagged entries were never written. Runs against
the real engine substrate: a minted T1 session and real T2 memory.
"""
from __future__ import annotations

import os
import uuid

import pytest

from nexus import mcp_infra
from nexus.mcp import core


@pytest.fixture
def owned_session(monkeypatch):
    """This test's minted T1 session, registered as the MCP's owned one, with
    the teardown's lease-clear and token-revoke recorded in call order."""
    session_id = os.environ["NX_T1_SESSION_ID"]
    mcp_infra.reset_t1_for_release()
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {"session_id": session_id})
    order: list[str] = []
    real_write = mcp_infra.t2_index_write

    def _recording_write(fn, *, op="t2_write"):
        order.append(f"t2:{op}")
        return real_write(fn, op=op)

    monkeypatch.setattr(mcp_infra, "t2_index_write", _recording_write)
    monkeypatch.setattr("nexus.db.t1.clear_t1_session_lease", lambda sid, cfg: order.append("lease_cleared"))

    class _Tokens:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close_session(self, sid):
            order.append("token_closed")

    monkeypatch.setattr("nexus.db.t2.http_token_store.HttpTokenStore", _Tokens)
    yield session_id, order
    mcp_infra.reset_t1_for_release()


def test_flagged_entries_reach_t2_before_the_lease_and_token_go(owned_session) -> None:
    session_id, order = owned_session
    t1, _ = mcp_infra.get_t1()
    title = f"mgu1k-{uuid.uuid4().hex[:8]}"
    t1.put("teardown flush body", persist=True, flush_project="nexus_test_mgu1k", flush_title=title)

    core._t1_session_shutdown()

    got = mcp_infra.t2_index_write(lambda db: db.memory.get(project="nexus_test_mgu1k", title=title))
    assert got is not None and got["content"] == "teardown flush body"
    assert order[:3] == ["t2:t1_teardown_flush", "lease_cleared", "token_closed"], order


def test_no_flagged_entries_writes_nothing_and_teardown_proceeds(owned_session) -> None:
    _, order = owned_session
    core._t1_session_shutdown()
    assert order == ["lease_cleared", "token_closed"], order


def test_a_failing_flush_never_stops_the_teardown(owned_session, monkeypatch) -> None:
    _, order = owned_session

    def _broken():
        raise RuntimeError("T1 unreachable")

    monkeypatch.setattr(mcp_infra, "get_t1", _broken)
    core._t1_session_shutdown()
    assert order == ["lease_cleared", "token_closed"], order


def test_a_hanging_t2_write_is_abandoned_at_the_bound(owned_session, monkeypatch) -> None:
    """The flush runs inside the SIGTERM handler's chain: a stalled engine
    must not delay the lease clear and revoke past the harness's grace,
    which would turn SIGTERM into SIGKILL and skip both (critique of
    c4312a874)."""
    import threading  # noqa: PLC0415 — test-local import
    import time  # noqa: PLC0415 — test-local import

    _, order = owned_session
    t1, _ = mcp_infra.get_t1()
    t1.put("stalled", persist=True, flush_project="nexus_test_mgu1k", flush_title=f"stall-{uuid.uuid4().hex[:8]}")
    release = threading.Event()

    def _hang(fn, *, op="t2_write"):
        order.append(f"t2:{op}")
        release.wait(30)
        return 0

    monkeypatch.setattr(mcp_infra, "t2_index_write", _hang)
    monkeypatch.setattr(core, "_TEARDOWN_FLUSH_TIMEOUT_S", 0.3)
    started = time.monotonic()
    try:
        core._t1_session_shutdown()
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert order == ["t2:t1_teardown_flush", "lease_cleared", "token_closed"], order


def test_a_hanging_store_lookup_is_bounded_too(owned_session, monkeypatch) -> None:
    """Resolving the store and listing flagged entries are network calls as
    well; they sit inside the bound, not before it (review of c4312a874)."""
    import threading  # noqa: PLC0415 — test-local import
    import time  # noqa: PLC0415 — test-local import

    _, order = owned_session
    release = threading.Event()

    def _hang():
        release.wait(30)
        raise RuntimeError("never reached in time")

    monkeypatch.setattr(mcp_infra, "get_t1", _hang)
    monkeypatch.setattr(core, "_TEARDOWN_FLUSH_TIMEOUT_S", 0.3)
    started = time.monotonic()
    try:
        core._t1_session_shutdown()
    finally:
        release.set()
    assert time.monotonic() - started < 5
    assert order == ["lease_cleared", "token_closed"], order


def test_a_hanging_token_revoke_is_bounded_too(owned_session, monkeypatch) -> None:
    """The revoke that follows the flush waited up to 30 s plus a 12 s rebind
    retry in the same SIGTERM chain (critique of cf234888a)."""
    import threading  # noqa: PLC0415 — test-local import
    import time  # noqa: PLC0415 — test-local import

    release = threading.Event()

    class _Hanging:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close_session(self, sid):
            release.wait(30)

    monkeypatch.setattr("nexus.db.t2.http_token_store.HttpTokenStore", _Hanging)
    monkeypatch.setattr(core, "_TEARDOWN_FLUSH_TIMEOUT_S", 0.3)
    started = time.monotonic()
    try:
        core._t1_session_shutdown()
    finally:
        release.set()
    assert time.monotonic() - started < 5


def test_an_outcome_after_the_bound_is_still_logged(owned_session, monkeypatch) -> None:
    """A write the caller abandoned at the bound, then failing (against the
    just-revoked token, say), is recorded, not dropped (review of a956bb57f)."""
    import threading  # noqa: PLC0415 — test-local import

    import structlog  # noqa: PLC0415 — test-local import

    t1, _ = mcp_infra.get_t1()
    t1.put("late", persist=True, flush_project="nexus_test_mgu1k", flush_title=f"late-{uuid.uuid4().hex[:8]}")
    release, done = threading.Event(), threading.Event()

    def _slow_then_fail(fn, *, op="t2_write"):
        release.wait(30)
        done.set()
        raise RuntimeError("token revoked")

    monkeypatch.setattr(mcp_infra, "t2_index_write", _slow_then_fail)
    monkeypatch.setattr(core, "_TEARDOWN_FLUSH_TIMEOUT_S", 0.3)
    with structlog.testing.capture_logs() as logs:
        core._t1_session_shutdown()
        release.set()
        assert done.wait(5)
        for _ in range(50):
            if any(e["event"] == "t1_teardown_flush_failed" for e in logs):
                break
            threading.Event().wait(0.05)
    events = [e["event"] for e in logs]
    assert "t1_teardown_flush_incomplete" in events
    assert any(e["event"] == "t1_teardown_flush_failed" and e["error"] == "token revoked" for e in logs), events
