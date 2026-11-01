# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mtheb.1: flagged T1 rows reach T2 at every session handoff.

A ``/clear``, a ``/resume`` into another id or a ``/branch`` moves the MCP
server to a new session id (``_t1_handoff_tick``). The old id's rows strand on
purpose (JDR-001), so a row flagged for T2 had no flush on that path: only the
racing SessionEnd hook could save it. These tests drive the real tick against
the engine substrate: a minted T1 session under the ``t2_tenant`` fixture is
the OLD session, real flagged rows are written to it, and only the mint for
the NEW id is faked (the idiom of ``test_t1_handoff_release.py``).
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

import pytest

import nexus.mcp.core as core
import nexus.mcp_infra as mcp_infra
from nexus.config import nexus_config_dir
from nexus.daemon.t1_handoff import write_handoff_marker

pytestmark = pytest.mark.t2_tenant

_CLAUDE_PID = 4242
_MCP_PID = 4300


@pytest.fixture
def handoff(monkeypatch):
    """The test's real T1 session as the OLD id, plus a marker to a NEW one."""
    from nexus.db import t1 as t1_mod

    old_id = os.environ["NX_T1_SESSION_ID"]
    # Re-set through monkeypatch so the tick's own env swap is undone at teardown.
    monkeypatch.setenv("NX_T1_SESSION_ID", old_id)
    monkeypatch.setenv("NX_T1_SESSION", os.environ["NX_T1_SESSION"])
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    monkeypatch.setattr(core, "_T1_SERVER_START_TIME", 0.0)
    monkeypatch.setattr(core, "_DEFERRED_T1_MINT", {})
    monkeypatch.setattr(core, "_T1_HANDOFF_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(core, "_T1_HANDOFF_BACKOFF_SESSION_ID", None)
    monkeypatch.setattr(core, "_T1_HANDOFF_NEXT_ATTEMPT_AT", 0.0)
    monkeypatch.setattr(core, "_T1_HANDOFF_GIVE_UP_LOGGED", False)
    monkeypatch.setattr(core, "_start_channel_waiter", lambda: None)
    monkeypatch.setattr(
        t1_mod, "mint_t1_session_token",
        lambda sid, *, context="": {"session_token": f"tok-{sid}", "expires_in_seconds": 3600},
    )
    monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID)
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()
    new_id = f"new-{uuid.uuid4().hex[:8]}"
    write_handoff_marker(
        _MCP_PID, new_session_id=new_id, claude_pid=_CLAUDE_PID, config_dir=nexus_config_dir(),
    )
    yield old_id, new_id
    task = core._T1_SESSION_REFRESH_TASK
    if task is not None:
        task.cancel()
    core._T1_SESSION_REFRESH_TASK = None
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()


def _t2_titles(project: str) -> list[str]:
    rows = mcp_infra.t2_index_write(lambda db: db.memory.list_entries(project=project))
    return sorted(r["title"] for r in rows)


@pytest.mark.asyncio
async def test_a_handoff_flushes_the_old_sessions_flagged_rows_to_t2(handoff) -> None:
    old_id, new_id = handoff
    project = f"nexus_test_mtheb1_{uuid.uuid4().hex[:6]}"
    t1, _ = mcp_infra.get_t1()
    t1.put("kept across the clear", persist=True, flush_project=project, flush_title="kept")
    t1.put("an ordinary scratch note")  # unflagged: stays behind with the old id

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == new_id  # the handoff itself still happened
    assert log.info.call_args.kwargs["flushed_to_t2"] == 1  # and its log line says how many crossed
    got = mcp_infra.t2_index_write(lambda db: db.memory.get(project=project, title="kept"))
    assert got is not None and got["content"] == "kept across the clear"
    assert _t2_titles(project) == ["kept"]  # only the flagged row crossed


@pytest.mark.asyncio
async def test_a_row_flushed_by_the_handoff_and_again_is_one_t2_entry(handoff) -> None:
    """The handoff flush and the exit flush both write the same (project, title):
    a row flushed twice stays one T2 entry."""
    old_id, _ = handoff
    t1, _ = mcp_infra.get_t1()
    t1.put("flagged with the default destination", persist=True)  # scratch_sessions / <sid>_<id>

    assert core._flush_flagged_t1_entries(old_id) == 1  # the exit-path flush, first
    await core._t1_handoff_tick(_MCP_PID, MagicMock())  # the handoff flush, second

    titles = [t for t in _t2_titles("scratch_sessions") if t.startswith(f"{old_id}_")]
    assert len(titles) == 1, titles


@pytest.mark.asyncio
async def test_a_failing_flush_never_blocks_the_handoff(handoff, monkeypatch) -> None:
    _, new_id = handoff

    attempts: list[str] = []

    def _broken():
        attempts.append("get_t1")
        raise RuntimeError("T1 unreachable")

    monkeypatch.setattr(mcp_infra, "get_t1", _broken)

    await core._t1_handoff_tick(_MCP_PID, MagicMock())

    assert attempts == ["get_t1"]  # the flush was tried, once
    assert os.environ["NX_T1_SESSION_ID"] == new_id


@pytest.mark.asyncio
async def test_a_failed_mint_does_not_flush_and_the_retry_does(handoff, monkeypatch) -> None:
    """The flush rides the successful re-lease, so a mint that is retried on a
    backoff does not re-run the flush on every attempt."""
    from nexus.db import t1 as t1_mod

    old_id, new_id = handoff
    project = f"nexus_test_mtheb1_{uuid.uuid4().hex[:6]}"
    t1, _ = mcp_infra.get_t1()
    t1.put("waits for the mint", persist=True, flush_project=project, flush_title="waits")
    good_mint = t1_mod.mint_t1_session_token

    def _refused(sid, *, context=""):
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _refused)
    await core._t1_handoff_tick(_MCP_PID, MagicMock())
    assert os.environ["NX_T1_SESSION_ID"] == old_id
    assert _t2_titles(project) == []

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", good_mint)
    monkeypatch.setattr(core, "_T1_HANDOFF_NEXT_ATTEMPT_AT", 0.0)
    await core._t1_handoff_tick(_MCP_PID, MagicMock())
    assert os.environ["NX_T1_SESSION_ID"] == new_id
    assert _t2_titles(project) == ["waits"]


# ── MCP scratch: a row can be flagged when it is written ─────────────────────


def test_mcp_scratch_put_can_flag_the_row_at_write_time() -> None:
    doc_id = core.scratch(action="put", content="flag me at birth", persist=True).split("Stored:")[1].strip()
    t1, _ = mcp_infra.get_t1()
    flagged = {e["id"] for e in t1.flagged_entries()}
    assert doc_id in flagged


def test_mcp_scratch_put_is_unflagged_by_default() -> None:
    doc_id = core.scratch(action="put", content="plain note").split("Stored:")[1].strip()
    t1, _ = mcp_infra.get_t1()
    assert doc_id not in {e["id"] for e in t1.flagged_entries()}


def test_scratch_manage_flag_takes_the_prefix_that_scratch_list_prints() -> None:
    doc_id = core.scratch(action="put", content="listed then flagged").split("Stored:")[1].strip()
    out = core.scratch_manage(action="flag", entry_id=doc_id[:8])
    assert out.startswith("Flagged:"), out
    t1, _ = mcp_infra.get_t1()
    assert doc_id in {e["id"] for e in t1.flagged_entries()}
