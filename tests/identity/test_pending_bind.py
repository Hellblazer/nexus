# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pending bind (RDR-253 "A holder that no longer exists", last paragraph) against a real engine: the hook's
registry wait timed out and left ``pending.<claude pid>.<OS start epoch>``; the MCP server, unbound, checks for it on
every heartbeat, takes only the file named for its own Claude process, binds from it with the DATA credential (which
may not clear, release, take over or report gone, and the pending bind needs none of them), writes the token file
no hook will and deletes the file. A stale file from a reused pid is ignored by the server and removed by the hook.

The one row runs on a scoped world inside the shared tenant of tests/identity/test_identity_engine_journeys.py (this
module holds no test of its own)."""
from __future__ import annotations

from nexus.hooks.identity_bind import bind_at_session_start
from nexus.identity.holder import epoch_seconds
from nexus.identity.pending import Pending, pending_files, pending_path, write_pending
from nexus.identity.sweep import clean_local_files
from nexus.identity.token_file import read_token_file, token_path
from nexus.mcp.identity_lease import HEARTBEAT_S, IdentityLease
from tests.identity.conftest import T0, VERSION, World

T1 = "2026-11-17T09:00:00Z"


def _lease(world: World, pid: int) -> IdentityLease:
    return IdentityLease(client_factory=lambda: world.data_client, config_dir=world.config_dir, claude_pid=pid,
                         procs=world.procs, clock=lambda: world.now, client_version=VERSION, sleep=lambda s: None)


def row_a_timeout_leaves_a_pending_file_the_server_binds_from_and_deletes_and_a_stale_one_is_ignored_and_removed(world: World) -> None:
    alpha = world.seat("alpha")
    world.procs.start(800, T0, registry=False)                    # a Desktop process whose registry file is late
    lease = _lease(world, 800)
    assert not lease.tick().pending_bound, "no registry file and no pending file: unbound, and quiet"

    # the hook (tenant credential) waits its 5 seconds, binds nothing and writes the payload
    clock = [1_000.0]
    hooked = bind_at_session_start(world.q("sid-p"), "startup", env={"NX_SEAT": world.q("alpha")}, claude_pid=800, procs=world.procs,
                                   client=world.client, config_dir=world.config_dir, clock=lambda: clock[0],
                                   sleep=lambda s: clock.__setitem__(0, clock[0] + s))
    assert hooked.outcome is None
    epoch = epoch_seconds(T0)
    mine = pending_path(world.identity_dir, 800, epoch)
    assert mine in pending_files(world.identity_dir)
    stale = write_pending(world.identity_dir, Pending(session_id=world.q("old"), source="startup", seat=world.q("alpha"), os_start="2026-11-16T01:00:00Z",
                                                      claude_pid=800), epoch - 86_400)

    world.now += HEARTBEAT_S
    assert not lease.tick().pending_bound, "the registry file still has not appeared"
    world.procs.write_registry(800, 800_000, T0, session_id=world.q("sid-p"), entrypoint="claude-desktop")
    world.now += HEARTBEAT_S
    report = lease.tick()                                         # the next heartbeat after the file exists
    assert report.pending_bound and report.error is None
    assert (lease.identity, lease.generation) == (alpha, 1)
    shown = world.identity(alpha)
    assert shown["lease"] == "unleased" and shown["holder"]["pid"] == 800, "a Desktop session's bind is unleased"
    assert world.events(alpha) == ["start"] and world.indexed("sid-p")["identity"] == alpha
    assert not mine.exists(), "the server deleted the file it bound from"
    assert stale.exists(), "a file named for an earlier owner of the pid is not this process's"
    on_disk = read_token_file(token_path(world.identity_dir, 800, 800_000))
    assert (on_disk.identity, on_disk.generation, on_disk.session_id) == (alpha, 1, world.q("sid-p")), "no hook will write it, so the server did"

    removed = clean_local_files(world.identity_dir, world.procs, host=world.host)
    assert removed.pending_files_removed == [stale.name] and not stale.exists()

    # an unnamed launch binds an ephemeral identity through the data credential too
    world.procs.start(801, T1, registry=False)
    eph = _lease(world, 801)
    write_pending(world.identity_dir, Pending(session_id=world.q("sid-q"), source="resume", seat=None, os_start=T1, claude_pid=801),
                  epoch_seconds(T1))
    world.procs.write_registry(801, 801_000, T1, session_id=world.q("sid-q"))
    got = eph.tick()
    assert got.pending_bound and eph.identity is not None and eph.identity != alpha
    assert world.identity(eph.identity)["kind"] == "ephemeral" and world.identity(eph.identity)["lease"] == "leased"


ROWS = (
    row_a_timeout_leaves_a_pending_file_the_server_binds_from_and_deletes_and_a_stale_one_is_ignored_and_removed,
)
