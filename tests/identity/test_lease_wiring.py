# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identity lease through the MCP server's own entry points (RDR-253 Step 3 wiring): started by core's start path on
its own thread, a marker adopted through ``_t1_handoff_tick``, shut down through the exit path. Real engine, real lease
thread, this process standing in for both Claude (a registry file in a tmp CLAUDE_CONFIG_DIR) and the MCP server."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
import structlog

from nexus.daemon.t1_handoff import write_handoff_marker
from nexus.hooks.identity_bind import bind_at_session_start, handoff_fields
from nexus.identity.holder import os_start_time
from nexus.identity.token_file import token_path
from tests.identity.conftest import World
from tests.identity.fakes import ps_style

pytestmark = pytest.mark.t2_tenant


def _registry(claude_dir: Path, pid: int, session_id: str) -> None:
    (claude_dir / "sessions").mkdir(parents=True, exist_ok=True)
    import json

    (claude_dir / "sessions" / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "startedAt": 5_000_000, "procStart": ps_style(os_start_time(pid)),
        "kind": "interactive", "entrypoint": "cli", "name": "", "updatedAt": 1}))


def test_the_lease_starts_from_cores_start_path_adopts_a_marker_through_the_handoff_tick_and_ends_at_exit(
        world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.daemon.service_registry as service_registry
    import nexus.session as session
    from nexus import config as nx_config
    from nexus.mcp import core, identity_lease

    me = os.getpid()
    claude_dir = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.delenv("NX_IDENTITY_LEASE", raising=False)               # the suite switches the lease off; this test wants it
    monkeypatch.setattr(session, "find_immediate_claude_pid", lambda start_pid=None: me)
    monkeypatch.setattr(session, "_command_name_of", lambda pid, **kw: "claude")
    config_dir = nx_config.nexus_config_dir()
    alpha = world.seat("alpha")
    _registry(claude_dir, me, "s1")
    try:
        started = bind_at_session_start("s1", "startup", env={"NX_SEAT": "alpha"}, claude_pid=me)
        assert started.outcome.identity == alpha and started.outcome.generation == 1

        core._start_identity_lease()                                       # the server's own start path
        lease = identity_lease.current()
        assert lease is not None
        deadline = time.monotonic() + 15
        while not lease.bound and time.monotonic() < deadline:             # its thread reads the token file within seconds
            time.sleep(0.2)
        assert lease.bound and (lease.identity, lease.generation) == (alpha, 1)

        _registry(claude_dir, me, "s2")                                    # /clear: the hook binds, then writes the marker
        cleared = bind_at_session_start("s2", "clear", env={}, claude_pid=me)
        write_handoff_marker(me, new_session_id="s2", claude_pid=me, config_dir=config_dir,
                             **handoff_fields(cleared, "clear"))
        monkeypatch.setenv("NX_T1_SESSION_ID", "s2")                       # the T1 side has nothing to re-lease
        asyncio.run(core._t1_handoff_tick(me, structlog.get_logger("test")))
        assert lease.session_id == "s2", "the marker's identity fields reached the lease through the real tick"
        assert world.events(alpha) == ["start", "clear"]

        monkeypatch.setattr(service_registry, "pid_alive", lambda pid, **kw: False)     # Claude has exited
        thread = core._start_session_name_release()                        # the exit path: name release + identity shutdown
        core._await_session_name_release(thread)
        assert world.identity(alpha)["state"] == "ended" and world.events(alpha)[-1] == "end"
        assert not token_path(config_dir / "identity", me, 5_000_000).exists()
        assert identity_lease.current() is None
    finally:
        identity_lease._STOP.set()
