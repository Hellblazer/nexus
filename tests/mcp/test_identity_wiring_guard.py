# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 Step 3 wiring (nexus-hczdo.1.6 fix-later): the MCP server reaches the identity lease through a guarded seam, so a
failure to import the lease (the ``nexus.identity`` import chain) is logged and never reaches the server's start, its handoff
tick or its exit path; and the start path really starts the lease (a behavioural test, not a source-string match).

Tenant-free: no engine, no network. The lease module is made unimportable with ``sys.modules[...] = None`` and by dropping
the package attribute a ``from nexus.mcp import identity_lease`` would find first."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

import pytest
from structlog.testing import capture_logs

import nexus.mcp as mcp_pkg
import nexus.mcp.core as core
import nexus.mcp_infra as mcp_infra
from nexus.config import nexus_config_dir
from nexus.daemon.t1_handoff import write_handoff_marker

_CLAUDE_PID = 4242
_MCP_PID = 4300


@pytest.fixture
def lease_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from nexus.mcp import identity_lease`` raises ImportError, as an ImportError deep in ``nexus.identity`` would."""
    monkeypatch.delattr(mcp_pkg, "identity_lease", raising=False)
    monkeypatch.setitem(sys.modules, "nexus.mcp.identity_lease", None)


@pytest.fixture(autouse=True)
def _isolate_handoff_globals(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NX_T1_SESSION", raising=False)
    monkeypatch.delenv("NX_T1_SESSION_ID", raising=False)
    monkeypatch.setattr(core, "_T1_SESSION_REFRESH_TASK", None)
    monkeypatch.setattr(core, "_OWNED_T1_SESSION", {})
    monkeypatch.setattr(core, "_T1_SERVER_START_TIME", 0.0)
    monkeypatch.setattr(core, "_DEFERRED_T1_MINT", {})
    monkeypatch.setattr(core, "_T1_HANDOFF_CONSECUTIVE_FAILURES", 0)
    monkeypatch.setattr(core, "_T1_HANDOFF_BACKOFF_SESSION_ID", None)
    monkeypatch.setattr(core, "_T1_HANDOFF_NEXT_ATTEMPT_AT", 0.0)
    monkeypatch.setattr(core, "_T1_HANDOFF_GIVE_UP_LOGGED", False)
    monkeypatch.setattr(core, "_start_channel_waiter", lambda: None)
    monkeypatch.setattr(core, "_flush_flagged_t1_entries", lambda sid, **kw: 0)
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()
    yield
    task = core._T1_SESSION_REFRESH_TASK
    if task is not None:
        task.cancel()
    core._T1_SESSION_REFRESH_TASK = None
    mcp_infra.set_t1_pre_init_hook(None)
    mcp_infra.reset_t1_for_release()


def _fake_mint(session_id_arg: str, *, context: str = "") -> dict:
    return {"session_token": f"tok-{session_id_arg}", "expires_in_seconds": 3600}


async def _start_and_stop_the_watch_task() -> None:
    core._start_t1_handoff_watch_task()
    await core._cancel_t1_handoff_watch_task()


def test_the_start_path_starts_the_lease_when_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The behaviour the string match in the engine journey stood for: core's start path calls the lease's start."""
    from nexus.mcp import identity_lease

    started: list[str] = []
    monkeypatch.setattr(identity_lease, "start", lambda: started.append("start"))
    asyncio.run(_start_and_stop_the_watch_task())
    assert started == ["start"], "the server's start path did not start the identity lease"


def test_the_start_path_survives_a_lease_that_cannot_be_imported_and_says_so(lease_unimportable: None) -> None:
    with capture_logs() as logs:
        asyncio.run(_start_and_stop_the_watch_task())      # raised ModuleNotFoundError at server start before the guard
    said = [e for e in logs if e.get("event") == "identity_lease_unavailable"]
    assert len(said) == 1 and said[0]["log_level"] == "warning" and "ModuleNotFoundError" in said[0]["error"], logs


def test_the_exit_path_survives_a_lease_that_cannot_be_imported(
        lease_unimportable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_t1_shutdown", lambda: None)
    monkeypatch.setattr(core, "_remove_aura_state_files", lambda: None)
    core._exit_teardown()                                   # raised ModuleNotFoundError on exit before the guard


@pytest.mark.asyncio
async def test_the_handoff_tick_survives_a_lease_that_cannot_be_imported(
        lease_unimportable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker that carries an identity reaches ``follow_marker``; with the lease unimportable the tick still adopts the
    new session id (its T1 half) instead of raising."""
    from nexus.db import t1 as t1_mod

    monkeypatch.setattr(t1_mod, "mint_t1_session_token", _fake_mint)
    monkeypatch.setattr("nexus.session.find_immediate_claude_pid", lambda start_pid=None: _CLAUDE_PID)
    write_handoff_marker(_MCP_PID, new_session_id="new-sess", claude_pid=_CLAUDE_PID, config_dir=nexus_config_dir(),
                         identity="i7", generation=1, token_file="/nowhere/4242.1.token")
    monkeypatch.setenv("NX_T1_SESSION_ID", "old-sess")            # monkeypatch, not os.environ: the tick rewrites both
    monkeypatch.setenv("NX_T1_SESSION", "old-token")

    await core._t1_handoff_tick(_MCP_PID, MagicMock())

    assert os.environ["NX_T1_SESSION_ID"] == "new-sess"
