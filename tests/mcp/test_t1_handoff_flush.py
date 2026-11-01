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


class _RecordingLog:
    """Stands in for the flush's structlog logger: records (level, event, fields).

    capture_logs cannot see an info line when the app's structlog config
    filters below warning, so the flush's logger is replaced outright."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        return lambda event, **kw: self.events.append((level, event, kw))

    def ops(self, event: str) -> list[str]:
        return [kw["op"] for _, e, kw in self.events if e == event]


@pytest.fixture
def flush_log(monkeypatch) -> _RecordingLog:
    import structlog  # noqa: PLC0415

    rec = _RecordingLog()
    monkeypatch.setattr(structlog, "get_logger", lambda *a, **k: rec)
    return rec


def _released_flush_count(log: MagicMock) -> int:
    """flushed_to_t2 from the tick's own t1_handoff_released line."""
    lines = [c for c in log.info.call_args_list if c.args and c.args[0] == "t1_handoff_released"]
    assert len(lines) == 1, log.info.call_args_list
    return lines[0].kwargs["flushed_to_t2"]


@pytest.mark.asyncio
async def test_a_handoff_flushes_the_old_sessions_flagged_rows_to_t2(handoff) -> None:
    old_id, new_id = handoff
    project = f"nexus_test_mtheb1_{uuid.uuid4().hex[:6]}"
    t1, _ = mcp_infra.get_t1()
    t1.put("kept across the clear", persist=True, flush_project=project, flush_title="kept")
    stranded_id = t1.put("an ordinary scratch note")  # unflagged: stays behind with the old id

    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == new_id  # the handoff itself still happened
    assert log.info.call_args.kwargs["flushed_to_t2"] == 1  # and its log line says how many crossed
    got = mcp_infra.t2_index_write(lambda db: db.memory.get(project=project, title="kept"))
    assert got is not None and got["content"] == "kept across the clear"
    assert _t2_titles(project) == ["kept"]  # only the flagged row crossed
    # JDR-001: the old scope keeps ALL its rows, the flagged one and the unflagged
    # one. `t1` is the store bound to the old session id before the tick.
    assert stranded_id in {e["id"] for e in t1.list_entries()}
    assert {e["content"] for e in t1.list_entries()} == {
        "kept across the clear", "an ordinary scratch note",
    }


@pytest.mark.asyncio
async def test_a_row_flushed_by_the_handoff_and_again_is_one_t2_entry(handoff) -> None:
    """The handoff flush and the exit flush both write the same (project, title):
    a row flushed twice stays one T2 entry."""
    old_id, _ = handoff
    t1, _ = mcp_infra.get_t1()
    t1.put("flagged with the default destination", persist=True)  # scratch_sessions / <sid>_<id>

    assert core._flush_flagged_t1_entries(old_id) == 1  # the exit-path flush, first
    log = MagicMock()
    await core._t1_handoff_tick(_MCP_PID, log)  # the handoff flush, second
    # The tick's own flush ran (it is the second write of the row); without
    # this the exit flush alone yields one entry and the test pins nothing.
    assert _released_flush_count(log) == 1

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
async def test_a_flush_that_raises_outside_the_worker_still_completes_the_handoff(
    handoff, monkeypatch,
) -> None:
    """A raise from the to_thread call itself (Thread.start() failing, say) is
    outside the worker's own try; the claimed marker must still be consumed."""
    _, new_id = handoff

    def _raises(session_id, *, op="t1_teardown_flush"):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(core, "_flush_flagged_t1_entries", _raises)
    log = MagicMock()

    await core._t1_handoff_tick(_MCP_PID, log)

    assert os.environ["NX_T1_SESSION_ID"] == new_id  # the handoff completed
    assert _released_flush_count(log) == 0
    assert [c.args[0] for c in log.warning.call_args_list if c.args] == ["t1_handoff_flush_raised"]
    from nexus.daemon.t1_handoff import claim_handoff_marker  # noqa: PLC0415
    assert claim_handoff_marker(_MCP_PID, config_dir=nexus_config_dir()) is None  # consumed, not stranded


def test_a_handoff_flush_failure_is_logged_with_its_op(handoff, monkeypatch, flush_log) -> None:
    """A handoff flush is told apart from an exit flush by `op` in every event."""
    old_id, _ = handoff
    t1, _ = mcp_infra.get_t1()
    t1.put("flagged", persist=True)

    assert core._flush_flagged_t1_entries(old_id, op="t1_handoff_flush") == 1
    assert flush_log.ops("t1_teardown_flush_complete") == ["t1_handoff_flush"]

    def _broken():
        raise RuntimeError("T1 unreachable")

    monkeypatch.setattr(mcp_infra, "get_t1", _broken)
    assert core._flush_flagged_t1_entries(old_id, op="t1_handoff_flush") == 0
    assert flush_log.ops("t1_teardown_flush_failed") == ["t1_handoff_flush"]

    core._flush_flagged_t1_entries(old_id)  # the default op is the exit flush's
    assert flush_log.ops("t1_teardown_flush_failed") == ["t1_handoff_flush", "t1_teardown_flush"]


def test_an_incomplete_handoff_flush_is_logged_with_its_op(handoff, monkeypatch, flush_log) -> None:
    import threading  # noqa: PLC0415

    old_id, _ = handoff
    release = threading.Event()

    def _hangs():
        release.wait(5)
        raise RuntimeError("released")

    monkeypatch.setattr(mcp_infra, "get_t1", _hangs)
    monkeypatch.setattr(core, "_TEARDOWN_FLUSH_TIMEOUT_S", 0.05)
    try:
        assert core._flush_flagged_t1_entries(old_id, op="t1_handoff_flush") == 0
        assert flush_log.ops("t1_teardown_flush_incomplete") == ["t1_handoff_flush"]
    finally:
        release.set()


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


def test_scratch_manage_flag_refuses_an_ambiguous_prefix_and_flags_nothing() -> None:
    # Ids are hex uuids: 17 rows put two on the same first character.
    ids = [
        core.scratch(action="put", content=f"row {i}").split("Stored:")[1].strip()
        for i in range(17)
    ]
    by_first: dict[str, list[str]] = {}
    for doc_id in ids:
        by_first.setdefault(doc_id[0], []).append(doc_id)
    shared = next(c for c, v in by_first.items() if len(v) > 1)

    out = core.scratch_manage(action="flag", entry_id=shared)

    assert out.startswith("Error: ambiguous prefix"), out
    t1, _ = mcp_infra.get_t1()
    assert not ({e["id"] for e in t1.flagged_entries()} & set(ids))

