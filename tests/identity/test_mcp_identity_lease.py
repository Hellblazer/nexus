# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP server's identity lease (``nexus.mcp.identity_lease``), one heartbeat at a time against a real engine, with
the server's own credential (a ``data`` token: it may start, resume, fork, refresh and end, nothing else). Clocks are
fixed (``world.now``); the engine's lapse is staged by backdating its last renewal. The tests that need the Step 2
engine (tokens and fences) are in test_identity_token_engine.py and skip until it lands.

The four rows are independent journeys; each runs on its own scoped world inside ONE tenant (``rows``, see
tests/identity/conftest.py), so the module mints one tenant, not four."""
from __future__ import annotations

import pytest

from nexus.identity.bind_client import IdentityApiError, evidence_body
from nexus.identity.token_file import read_token_file, token_path
from nexus.mcp.identity_lease import HEARTBEAT_S, IdentityLease
from tests.identity.conftest import T0, VERSION, Rows, World

T1 = "2026-11-17T09:00:00Z"


def _lease(world: World, pid: int) -> IdentityLease:
    return IdentityLease(client_factory=lambda: world.data_client, config_dir=world.config_dir, claude_pid=pid,
                         procs=world.procs, clock=lambda: world.now, client_version=VERSION, sleep=lambda s: None)


def row_a_fresh_server_reads_its_token_file_renews_each_minute_and_a_sleep_lapses_and_revives_without_a_generation_change(world: World) -> None:
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-1")
    lease = _lease(world, 100)

    first = lease.tick()
    assert first.adopted and first.renewed and (lease.identity, lease.generation, lease.session_id) == (alpha, 1, world.q("sid-1"))
    assert world.identity(alpha)["client_version"] == VERSION, "the renewal carries and stores the server's client version"
    world.now += 10
    assert not lease.tick().renewed, "not due again for 60 seconds"
    world.now += HEARTBEAT_S
    assert lease.tick().renewed

    # the laptop sleeps: the engine lapses the seat; on wake the same process revives it, no new generation
    world.lapse(alpha, seconds_ago=400)
    world.now += HEARTBEAT_S
    woke = lease.tick()
    assert woke.renewed and not woke.superseded
    assert world.identity(alpha)["state"] == "live" and world.identity(alpha)["generation"] == 1
    assert world.events(alpha) == ["start", "lapse", "revive"]


def row_the_heartbeat_reread_follows_a_cross_seat_switch_when_the_marker_is_lost(world: World) -> None:
    """A ``/resume`` switches the process from alpha to gamma. The hook's marker write failed (the test never
    delivers one); the server's next heartbeat re-reads the token file and adopts gamma within a minute."""
    alpha, gamma = world.seat("alpha"), world.seat("gamma")
    world.process(300, name="gamma", os_start=T1)
    world.hook(300, "startup", "g-old")
    world.client.bind(gamma, "end", 1)
    world.procs.stop(300, keep_registry=False)
    world.process(100, name="alpha")
    world.hook(100, "startup", "a-sid")
    lease = _lease(world, 100)
    assert lease.tick().adopted and lease.identity == alpha

    out = world.hook(100, "resume", "g-old")
    assert out.identity == gamma and out.switched_from == alpha
    world.now += HEARTBEAT_S
    report = lease.tick()
    assert report.adopted and report.renewed and not report.superseded
    assert (lease.identity, lease.generation, lease.session_id) == (gamma, 2, world.q("g-old"))
    assert world.identity(alpha)["state"] == "unheld" and world.identity(gamma)["holder"]["pid"] == 100


def row_a_branch_appends_a_fork_row_on_the_next_renewal_and_a_marker_is_adopted_only_from_its_own_token_file(world: World) -> None:
    """``/branch`` runs no hook: the registry file gets a new session id the index does not know. On its next
    renewal the server maps it through the registry's pid and startedAt and appends the ``fork`` row."""
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-1")
    lease = _lease(world, 100)
    lease.tick()

    world.registry(100, 100_000, T0, session_id="branch-sid", name="alpha")
    world.now += HEARTBEAT_S
    report = lease.tick()
    assert report.forked and lease.session_id == world.q("branch-sid")
    assert world.events(alpha) == ["start", "fork"]
    assert (world.indexed("branch-sid")["identity"], world.indexed("branch-sid")["generation"]) == (alpha, 1)
    assert world.log(alpha)[-1]["prior_session_id"] == world.q("sid-1")
    assert read_token_file(token_path(world.identity_dir, 100, 100_000)).session_id == world.q("branch-sid")
    world.now += HEARTBEAT_S
    assert not lease.tick().forked, "the id is known now: no second row"

    # a /clear delivers a marker: adopted from this process's own token file; a path naming another file is refused
    cleared = world.hook(100, "clear", "sid-2")
    own = str(token_path(world.identity_dir, 100, 100_000))
    assert not lease.adopt_marker(identity=alpha, generation=1, token_file=str(world.identity_dir / "999.999.token"))
    assert lease.adopt_marker(identity=cleared.identity, generation=1, token_file=own) and lease.session_id == world.q("sid-2")


def row_a_superseded_server_stops_writing_and_says_so_and_end_is_sent_only_when_claude_has_exited(world: World) -> None:
    alpha = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-1")
    old = _lease(world, 100)
    old.tick()

    world.procs.stop(100, keep_registry=True)                # process 100 crashed; process 101 takes the seat
    world.process(101, os_start=T1, name="alpha")
    assert world.hook(101, "startup", "sid-2").generation == 2
    world.now += HEARTBEAT_S
    gone = old.tick()
    assert gone.superseded and not old.bound and not gone.renewed
    before = world.log(alpha)
    world.now += HEARTBEAT_S
    assert not old.tick().renewed and world.log(alpha) == before, "no write after the refusal"
    old.shutdown(wait_for_claude=False)                      # superseded: it sends no `end`, and drops its file
    assert world.identity(alpha)["state"] == "live" and world.identity(alpha)["generation"] == 2
    assert not token_path(world.identity_dir, 100, 100_000).exists()
    with pytest.raises(IdentityApiError) as late:            # a late end carries the old generation: refused
        world.data_client.bind(alpha, "end", 1, evidence_body(client_version=VERSION))
    assert late.value.status == 409

    current = _lease(world, 101)
    current.tick()
    current.shutdown(wait_for_claude=False)                  # Claude still runs (a /mcp reconnect): nothing is ended
    assert world.identity(alpha)["state"] == "live" and token_path(world.identity_dir, 101, 101_000).exists()
    world.procs.stop(101, keep_registry=False)               # Claude exits: now the server's shutdown is the session's end
    current.shutdown(wait_for_claude=False)
    assert world.identity(alpha)["state"] == "ended" and world.events(alpha)[-1] == "end"
    assert not token_path(world.identity_dir, 101, 101_000).exists()


ROWS = (
    row_a_fresh_server_reads_its_token_file_renews_each_minute_and_a_sleep_lapses_and_revives_without_a_generation_change,
    row_the_heartbeat_reread_follows_a_cross_seat_switch_when_the_marker_is_lost,
    row_a_branch_appends_a_fork_row_on_the_next_renewal_and_a_marker_is_adopted_only_from_its_own_token_file,
    row_a_superseded_server_stops_writing_and_says_so_and_end_is_sent_only_when_claude_has_exited,
)


@pytest.mark.t2_tenant
def test_the_mcp_identity_lease(rows: Rows) -> None:
    """The lease's four journeys against one engine tenant; a failure names its row and the rows after it still run."""
    rows.run(ROWS, globals())
