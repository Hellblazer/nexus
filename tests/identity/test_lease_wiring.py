# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identity lease through the MCP server's own entry points (RDR-253 Step 3 wiring): started by core's start path on
its own thread, a marker adopted through ``_t1_handoff_tick``, shut down through the exit path. Real engine, real lease
thread, this process standing in for both Claude (a registry file in a tmp CLAUDE_CONFIG_DIR) and the MCP server.

The one row runs on a scoped world inside the shared tenant of tests/identity/test_identity_engine_journeys.py (this
module holds no test of its own). Its environment patches end with the row (``pytest.MonkeyPatch.context``) and its
lease thread is stopped and joined before the row returns, so the next row starts with no lease running."""
from __future__ import annotations

import asyncio
import contextlib
import os
import threading
import time
from pathlib import Path

import pytest
import structlog

from nexus.daemon.t1_handoff import write_handoff_marker
from nexus.hooks.identity_bind import bind_at_session_start, handoff_fields
from nexus.identity import token_header
from nexus.identity.holder import os_start_time
from nexus.identity.token_file import token_path
from tests.identity.conftest import World
from tests.identity.fakes import ps_style


def _registry(claude_dir: Path, pid: int, session_id: str) -> None:
    (claude_dir / "sessions").mkdir(parents=True, exist_ok=True)
    import json

    (claude_dir / "sessions" / f"{pid}.json").write_text(json.dumps({
        "pid": pid, "sessionId": session_id, "startedAt": 5_000_000, "procStart": ps_style(os_start_time(pid)),
        "kind": "interactive", "entrypoint": "cli", "name": "", "updatedAt": 1}))


def row_the_lease_starts_from_cores_start_path_adopts_a_marker_through_the_handoff_tick_and_ends_at_exit(
        world: World) -> None:
    import nexus.daemon.service_registry as service_registry
    import nexus.session as session
    from nexus import config as nx_config
    from nexus.mcp import core, identity_lease

    me = os.getpid()
    claude_dir = world.tmp / "claude"
    with pytest.MonkeyPatch.context() as monkeypatch:                          # the row's patches end with the row
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
        monkeypatch.delenv("NX_IDENTITY_LEASE", raising=False)               # the suite switches the lease off; this row wants it
        monkeypatch.setattr(session, "find_immediate_claude_pid", lambda start_pid=None: me)
        monkeypatch.setattr(session, "_command_name_of", lambda pid, **kw: "claude")
        config_dir = nx_config.nexus_config_dir()
        alpha = world.seat("alpha")
        s1, s2 = world.q("s1"), world.q("s2")
        _registry(claude_dir, me, s1)
        try:
            started = bind_at_session_start(s1, "startup", env={"NX_SEAT": world.q("alpha")}, claude_pid=me)
            assert started.outcome.identity == alpha and started.outcome.generation == 1

            async def start_through_cores_start_path() -> None:
                core._start_t1_handoff_watch_task()                            # the real start path, which starts the lease
                await core._cancel_t1_handoff_watch_task()

            asyncio.run(start_through_cores_start_path())      # tests/mcp/test_identity_wiring_guard.py pins the call without an engine
            lease = identity_lease.current()
            assert lease is not None and token_header._LEASE_OWNER, "the server owns the binding: its spawn-time session id is not checked"
            deadline = time.monotonic() + 15
            while not lease.bound and time.monotonic() < deadline:             # its thread reads the token file within seconds
                time.sleep(0.2)
            assert lease.bound and (lease.identity, lease.generation) == (alpha, 1)

            _registry(claude_dir, me, s2)                                      # /clear: the hook binds, then writes the marker
            cleared = bind_at_session_start(s2, "clear", env={}, claude_pid=me)
            write_handoff_marker(me, new_session_id=s2, claude_pid=me, config_dir=config_dir,
                                 **handoff_fields(cleared, "clear"))
            monkeypatch.setenv("NX_T1_SESSION_ID", s2)                         # the T1 side has nothing to re-lease
            asyncio.run(core._t1_handoff_tick(me, structlog.get_logger("test")))
            assert lease.session_id == s2, "the marker's identity fields reached the lease through the real tick"
            assert world.events(alpha) == ["start", "clear"]

            monkeypatch.setattr(service_registry, "pid_alive", lambda pid, **kw: False)     # Claude has exited
            core.identity_seam.start_shutdown()                                # the exit path's identity shutdown
            core.identity_seam.await_shutdown()
            assert world.identity(alpha)["state"] == "ended" and world.events(alpha)[-1] == "end"
            assert not token_path(config_dir / "identity", me, 5_000_000).exists()
            assert identity_lease.current() is None and not token_header._LEASE_OWNER
        finally:
            with contextlib.suppress(Exception):
                identity_lease.shutdown()                                      # idempotent: stops, joins and clears whatever a failure left
    left = [t for t in threading.enumerate() if t.name == "nx-identity-lease"]
    for t in left:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in left), "the lease thread must be gone before the next row"


ROWS = (
    row_the_lease_starts_from_cores_start_path_adopts_a_marker_through_the_handoff_tick_and_ends_at_exit,
)
