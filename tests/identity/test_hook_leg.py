# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart hook's whole identity leg (``nexus.hooks.identity_bind``), against a real engine: facts from the
process, the policy table, the sweep, the local cleanup and the fields the T1 handoff marker carries. The rows of
the table have their own tests (test_session_start_bind.py); this one checks they are wired together."""
from __future__ import annotations

import pytest

from nexus.hooks.identity_bind import bind_at_session_start, handoff_fields
from tests.identity.conftest import World

pytestmark = pytest.mark.t2_tenant

T1 = "2026-11-17T09:00:00Z"


class _Time:
    """A clock that only moves when the code sleeps, so a 5-second wait costs no time."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _leg(world: World, pid: int, source: str, sid: str, *, env: dict | None = None, clock: _Time | None = None):
    clock = clock or _Time()
    return bind_at_session_start(sid, source, env=env or {}, claude_pid=pid, procs=world.procs, client=world.client,
                                 config_dir=world.config_dir, clock=clock, sleep=clock.sleep)


def test_the_hook_leg_binds_sweeps_hands_the_marker_the_identity_and_skips_children(world: World) -> None:
    alpha, crashed = world.seat("alpha"), world.seat("crashed")
    world.process(300, name="crashed", os_start=T1, session_id="c-sid")
    _leg(world, 300, "startup", "c-sid")
    world.procs.stop(300, keep_registry=True)                       # crashed: the sweep reports it

    world.process(100, name="alpha-renamed", entrypoint="cli", session_id="sid-1")
    started = _leg(world, 100, "startup", "sid-1", env={"NX_SEAT": "alpha"})
    assert (started.outcome.identity, started.outcome.action, started.outcome.generation) == (alpha, "bound", 1)
    assert any("display name is 'alpha-renamed' but the seat is 'alpha'" in n for n in started.notices)
    assert started.sweep.recorded_gone == [crashed], "the sweep ran after the bind and reported the crashed holder"
    assert handoff_fields(started, "startup") == {}, "a startup spawns a new MCP server: no marker fields"
    assert world.identity(crashed)["state"] == "lapsed"

    world.procs.set_session(100, "sid-2")
    cleared = _leg(world, 100, "clear", "sid-2")
    fields = handoff_fields(cleared, "clear")
    assert fields["identity"] == alpha and fields["generation"] == 1
    assert fields["token_file"] == str(world.identity_dir / "100.100000.token") and "token" not in fields
    assert handoff_fields(cleared, "compact") == {}

    # a delegated child and a nested subprocess that inherited its parent's session id bind nothing
    assert _leg(world, 100, "startup", "child-sid", env={"NX_IDENTITY_TOKEN": "/x/y.token"}).outcome is None
    inherited = _leg(world, 100, "startup", "parent-sid", env={"NX_SESSION_ID": "parent-sid"})
    assert inherited.outcome is None and inherited.notices == ["identity: this process inherited NX_SESSION_ID (a nested subprocess), so it binds nothing"]
    assert world.indexed("sid-2")["identity"] == alpha
    with pytest.raises(Exception):
        world.indexed("child-sid")
