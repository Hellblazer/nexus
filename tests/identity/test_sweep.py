# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart sweep (RDR-253 "Binding"): the hook reports gone holders on its host to ``gone``, skipping its own
process and every delegated identity, and leaving alone a binding rebound since it read the list. Real engine."""
from __future__ import annotations

from typing import Any

import pytest

from nexus.identity.bind_client import IdentityApiError, IdentityClient, evidence_body
from nexus.identity.sweep import sweep_host
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from tests.identity.conftest import T0, World
from tests.identity.fakes import HOST

pytestmark = pytest.mark.t2_tenant

T1 = "2026-11-17T09:00:00Z"
T2 = "2026-11-17T10:00:00Z"


class RacingClient(IdentityClient):
    """Reads the host's bindings, then lets another process rebind one before the sweep reports on it."""

    def __init__(self, inner: IdentityClient, after_read: Any) -> None:
        self.__dict__.update(inner.__dict__)
        self._after_read = after_read

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        rows = super().bindings_by_host(host, pid)
        if pid is None and self._after_read is not None:
            hook, self._after_read = self._after_read, None
            hook()
        return rows


def _token(world: World, pid: int, started_at: int, identity: str) -> None:
    write_token_file(token_path(world.identity_dir, pid, started_at),
                     TokenFile(identity=identity, generation=1, token=None, expires_at=None, session_id="s", pid=pid,
                               started_at=started_at, proc_start=T0, host=HOST, registry_dir=str(world.procs.sessions_dir)))


def test_the_sweep_marks_a_crashed_holder_gone_and_leaves_its_own_process_delegated_identities_and_a_rebound_binding_alone(world: World) -> None:
    crashed, running, mine, rebound = world.seat("crashed"), world.seat("running"), world.seat("mine"), world.seat("rebound")
    for pid, name in ((100, "crashed"), (101, "running"), (102, "mine"), (105, "rebound")):
        world.process(pid, name=name, os_start=T0 if pid != 105 else T1)
        world.hook(pid, "startup", f"sid-{pid}")
    delegated = world.client._request("POST", "/delegated", json={"parent": running, "bind": evidence_body(
        session_id="child", host=HOST, pid=104, started_at=104_000, proc_start=T0)})["id"]
    _token(world, 100, 100_000, crashed)
    world.procs.stop(100, keep_registry=True)           # crashed: registry file left behind
    world.procs.stop(102, keep_registry=False)          # "mine" looks gone, but it is the sweeper's own process
    world.procs.stop(105, keep_registry=False)          # about to be found gone, but rebound before the report

    def rebind_after_the_read() -> None:
        world.process(106, os_start=T2)
        out = world.hook(106, "startup", "sid-106", seat="rebound")
        assert out.generation == 2

    racing = RacingClient(world.client, rebind_after_the_read)
    report = sweep_host(racing, world.procs, host=HOST, own=(102, 102_000), identity_dir=world.identity_dir)

    assert report.recorded_gone == [crashed]
    assert report.skipped_own == 1 and report.skipped_delegated == 1 and report.skipped_rebound == 1 and report.running == 1
    assert report.token_files_removed == ["100.100000.token"] and not token_path(world.identity_dir, 100, 100_000).exists()
    assert world.identity(crashed)["state"] == "lapsed" and world.identity(crashed)["gone_at"] is not None
    assert world.events(crashed)[-1] == "gone" and world.log(crashed)[-1]["holder_gone"]["pid"] == 100
    assert world.identity(running)["state"] == "live"
    assert world.identity(mine)["state"] == "live"
    assert world.identity(delegated)["state"] == "live"
    assert (world.identity(rebound)["generation"], world.identity(rebound)["holder"]["pid"]) == (2, 106)

    again = sweep_host(world.client, world.procs, host=HOST, own=(102, 102_000), identity_dir=world.identity_dir)
    assert again.recorded_gone == [] and again.checked >= 1, "a second verdict for the same holder answers recorded:false"
    assert world.events(crashed).count("gone") == 1


def test_a_data_credential_cannot_report_gone_so_the_sweep_belongs_to_the_hook(world: World) -> None:
    """The MCP server's credential may not call ``gone`` (RDR: seat create, promote, rename, gone, delegated create
    and purge take the tenant credential). The pending-bind path therefore never needs it."""
    seat = world.seat("alpha")
    world.process(100, name="alpha")
    world.hook(100, "startup", "sid-100")
    holder = world.identity(seat)["holder"]
    with pytest.raises(IdentityApiError) as err:
        world.data_client.gone(seat, 1, {k: holder[k] for k in ("host", "pid", "started_at", "proc_start")})
    assert err.value.status == 403
    for event in ("clear", "release", "takeover"):
        with pytest.raises(IdentityApiError) as refused:
            world.data_client.bind(seat, event, 1, evidence_body(session_id="x", host=HOST, pid=100, started_at=100_000))
        assert refused.value.status == 403, event
