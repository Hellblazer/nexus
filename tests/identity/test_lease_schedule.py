# SPDX-License-Identifier: AGPL-3.0-or-later
"""The identity lease's schedule (RDR-253 "Handover to the MCP server", "Renewal, refresh, lapse and sleep"): a new server
polls for its token file once a second for up to 30 seconds, then on the 60-second heartbeat; it renews every 60 seconds
and refreshes at half the token's life. A fake engine and a fixed clock: tenant-free."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError
from nexus.identity.token_file import TokenFile, token_path, write_token_file
from nexus.mcp.identity_lease import HEARTBEAT_S, STARTUP_POLL_S, STARTUP_WINDOW_S, IdentityLease
from tests.identity.fakes import FakeProcesses

T0 = "2026-11-17T08:00:00Z"


class _Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.refuse_renew: IdentityApiError | None = None

    def renew(self, identity: str, generation: int, client_version: str | None = None) -> dict:
        self.calls.append(("renew", (identity, generation)))
        if self.refuse_renew:
            raise self.refuse_renew
        return {}

    def session(self, session_id: str) -> dict:
        return {"identity": "i1"}

    def bind(self, identity: str, event: str, generation: int, evidence: Any = None) -> dict:
        self.calls.append((event, (identity, generation)))
        return {"id": identity, "generation": generation, "identity_token": f"tok-{len(self.calls)}",
                "identity_token_expires_in_seconds": 3600}


def _setup(tmp_path: Path, now: list[float]) -> tuple[IdentityLease, _Engine, FakeProcesses]:
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(100, T0, started_at=100_000, session_id="sid-1")
    engine = _Engine()
    lease = IdentityLease(client_factory=lambda: engine, config_dir=tmp_path / "config", claude_pid=100, procs=procs,
                          clock=lambda: now[0], client_version="1", sleep=lambda s: None)
    return lease, engine, procs


def _file(tmp_path: Path, token: str | None, expires_at: float | None, generation: int = 1) -> None:
    write_token_file(token_path(tmp_path / "config" / "identity", 100, 100_000),
                     TokenFile(identity="i1", generation=generation, token=token, expires_at=expires_at, session_id="sid-1",
                               pid=100, started_at=100_000, proc_start=T0))


def test_an_unbound_server_polls_each_second_for_thirty_seconds_then_on_the_heartbeat(tmp_path: Path) -> None:
    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    gaps = []
    for _ in range(3):
        lease.tick()
        gaps.append(lease._next_heartbeat - now[0])
        now[0] += STARTUP_POLL_S
    assert gaps == [STARTUP_POLL_S] * 3 and not lease.bound
    now[0] = 1_000.0 + STARTUP_WINDOW_S + 1
    lease.tick()
    assert lease._next_heartbeat - now[0] == HEARTBEAT_S, "past the window it is the 60-second heartbeat"
    _file(tmp_path, None, None)                      # the hook's file appears late: the next heartbeat adopts it
    now[0] += HEARTBEAT_S
    report = lease.tick()
    assert report.adopted and report.renewed and lease.bound and engine.calls == [("renew", ("i1", 1))]


def test_renewal_is_every_sixty_seconds_and_refresh_comes_at_half_the_token_life(tmp_path: Path) -> None:
    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    assert lease.tick().renewed
    now[0] += 30
    assert not lease.tick().renewed
    now[0] += 31
    assert lease.tick().renewed and not lease.tick().refreshed
    now[0] = 1_000.0 + 1_801                         # less than half of the hour left
    refreshed = lease.tick()
    assert refreshed.refreshed and ("refresh", ("i1", 1)) in engine.calls
    path = token_path(tmp_path / "config" / "identity", 100, 100_000)
    from nexus.identity.token_file import read_token_file

    on_disk = read_token_file(path)
    assert on_disk.token and on_disk.token != "tok-0" and on_disk.expires_at == now[0] + 3600


def test_a_refused_renewal_stops_the_server_writing_until_the_file_names_a_newer_holder(tmp_path: Path) -> None:
    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    engine.refuse_renew = IdentityApiError(409, "generation_superseded", "i1 is at generation 2", {})
    report = lease.tick()
    assert report.superseded and not lease.bound
    engine.calls.clear()
    now[0] += HEARTBEAT_S
    lease.tick()
    assert engine.calls == [], "no write after the refusal"
    engine.refuse_renew = None                       # a hook gave this process the seat again, at generation 3
    _file(tmp_path, "tok-3", expires_at=now[0] + 3600, generation=3)
    now[0] += HEARTBEAT_S
    back = lease.tick()
    assert back.adopted and lease.bound and lease.generation == 3 and engine.calls == [("renew", ("i1", 3))]
