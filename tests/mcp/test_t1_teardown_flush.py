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
