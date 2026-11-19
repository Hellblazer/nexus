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


# ── the review round: the file lock, the loop that must not wait, the fork under a stale generation ──────────────


def test_a_lease_that_still_holds_the_old_identity_never_overwrites_a_switch_the_hook_wrote(tmp_path: Path) -> None:
    """Compare-and-swap under the file lock: the hook rewrote the file for i2 (a cross-seat /resume) between the lease's
    read and its refresh; the lease, still holding i1, leaves the file alone and the next heartbeat adopts i2."""
    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    assert lease.tick().adopted and lease.identity == "i1"
    path = token_path(tmp_path / "config" / "identity", 100, 100_000)
    write_token_file(path, TokenFile(identity="i2", generation=4, token="tok-i2", expires_at=now[0] + 3600, session_id="g-sid",
                                     pid=100, started_at=100_000, proc_start=T0, session_ids=("g-sid",)))
    lease.token, lease.token_expires_at = "tok-refreshed", now[0] + 3600
    lease._rewrite_file()
    from nexus.identity.token_file import read_token_file

    kept = read_token_file(path)
    assert (kept.identity, kept.generation, kept.token) == ("i2", 4, "tok-i2")
    now[0] += HEARTBEAT_S
    follow = lease.tick()
    assert follow.adopted and (lease.identity, lease.generation) == ("i2", 4)
    path.unlink()
    lease.token = "tok-x"
    lease._rewrite_file()
    assert not path.exists(), "a deleted file is not resurrected"


def test_a_marker_is_adopted_while_a_heartbeat_is_stuck_in_an_engine_call(tmp_path: Path) -> None:
    """The handoff watcher calls adopt_marker on the asyncio loop: it must never wait on the engine. The heartbeat holds
    its own lock across the call; the state lock adopt_marker needs is free."""
    import threading

    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    lease.tick()
    release = threading.Event()
    entered = threading.Event()

    def slow_renew(identity: str, generation: int, client_version: str | None = None) -> dict:
        entered.set()
        release.wait(10)
        return {}

    engine.renew = slow_renew                         # type: ignore[method-assign]
    now[0] += HEARTBEAT_S
    worker = threading.Thread(target=lease.tick, daemon=True)
    worker.start()
    assert entered.wait(5)
    _file(tmp_path, "tok-new", expires_at=now[0] + 3600, generation=1)
    own = str(token_path(tmp_path / "config" / "identity", 100, 100_000))
    result: list[bool] = []
    caller = threading.Thread(target=lambda: result.append(lease.adopt_marker(identity="i1", generation=1, token_file=own)), daemon=True)
    caller.start()
    caller.join(2.0)
    release.set()
    worker.join(5)
    assert not caller.is_alive() and result == [True], "adopt_marker returned while the heartbeat was in the engine call"


def test_a_fork_refused_for_a_stale_generation_flags_superseded(tmp_path: Path) -> None:
    now = [1_000.0]
    lease, engine, procs = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    lease.tick()
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]

    def refuse(*a: Any, **k: Any) -> dict:
        raise IdentityApiError(409, "generation_superseded", "i1 is at generation 2", {})

    engine.bind = refuse                              # type: ignore[method-assign]
    now[0] += HEARTBEAT_S
    assert lease.tick().superseded and not lease.bound


def test_refresh_fork_and_end_carry_the_process_they_speak_for(tmp_path: Path) -> None:
    """host, pid, startedAt and OS start time ride every bind the lease sends, so the engine's row names the process."""
    now = [1_000.0]
    lease, engine, procs = _setup(tmp_path, now)
    seen: list[tuple[str, dict]] = []
    orig = engine.bind

    def spy(identity: str, event: str, generation: int, evidence: Any = None) -> dict:
        seen.append((event, dict(evidence or {})))
        return orig(identity, event, generation, evidence)

    engine.bind = spy                                  # type: ignore[method-assign]
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    _file(tmp_path, "tok-0", expires_at=now[0] + 1_000)           # under half of the hour left: a refresh is due
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    lease.tick()                                                 # the fork row (its answer carries a fresh token)
    lease.token_expires_at = now[0] + 1_000                      # under half of the hour left: a refresh is due
    lease._next_renew = 0.0
    lease.tick()
    procs.stop(100, keep_registry=True)
    lease.shutdown(wait_for_claude=False)
    events = [e for e, _ in seen]
    assert events == ["fork", "refresh", "end"]
    for _event, ev in seen:
        assert (ev["host"], ev["pid"], ev["started_at"], ev["proc_start"]) == (procs.host, 100, 100_000, T0)


def test_the_lease_reuses_one_engine_client_closes_it_on_shutdown_and_rebuilds_after_an_unreachable_engine(tmp_path: Path) -> None:
    from nexus.identity.bind_client import IdentityUnavailableError

    now = [1_000.0]
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(100, T0, started_at=100_000, session_id="sid-1")
    made: list[Any] = []

    class _Client(_Engine):
        closed = False

        def close(self) -> None:
            self.closed = True

    def factory() -> _Client:
        made.append(_Client())
        return made[-1]

    lease = IdentityLease(client_factory=factory, config_dir=tmp_path / "config", claude_pid=100, procs=procs,
                          clock=lambda: now[0], client_version="1", sleep=lambda s: None, close_client=True)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    lease.tick()
    now[0] += HEARTBEAT_S
    lease.tick()
    assert len(made) == 1, "one client for the life of the lease"
    made[0].renew = lambda *a, **k: (_ for _ in ()).throw(IdentityUnavailableError("connection refused"))  # type: ignore[method-assign]
    now[0] += HEARTBEAT_S
    assert "engine unavailable" in lease.tick().error and made[0].closed
    now[0] += HEARTBEAT_S
    assert lease.tick().renewed and len(made) == 2, "rebuilt: the endpoint is resolved again"
    procs.stop(100, keep_registry=False)
    lease.shutdown(wait_for_claude=False)
    assert made[1].closed


def test_a_call_that_was_out_when_a_marker_moved_the_lease_changes_nothing_of_the_new_identity(tmp_path: Path) -> None:
    """The heartbeat's refresh (or a refused renewal) is about the identity it was sent for. If adopt_marker moved the
    lease to another identity while the call was in flight, its answer must neither overwrite the new token nor flag the
    new identity superseded."""
    import threading

    for outcome in ("refresh_answers", "renew_refused"):
        now = [1_000.0]
        lease, engine, _ = _setup(tmp_path / outcome, now)
        _file(tmp_path / outcome, "tok-0", expires_at=now[0] + 1_000)        # a refresh is due
        lease.tick()                                                          # adopts i1 (the first call may refresh)
        lease._next_renew = 0.0
        lease.token_expires_at = now[0] + 1_000
        entered, release = threading.Event(), threading.Event()
        if outcome == "refresh_answers":
            orig = engine.bind

            def slow(identity: str, event: str, generation: int, evidence: Any = None) -> dict:
                entered.set()
                release.wait(10)
                return orig(identity, event, generation, evidence)

            engine.bind = slow                        # type: ignore[method-assign]
            engine.renew = lambda *a, **k: {}        # type: ignore[method-assign]
        else:
            def refuse(*a: Any, **k: Any) -> dict:
                entered.set()
                release.wait(10)
                raise IdentityApiError(409, "generation_superseded", "i1 is at generation 2", {})

            engine.renew = refuse                    # type: ignore[method-assign]
        worker = threading.Thread(target=lease.tick, daemon=True)
        worker.start()
        assert entered.wait(5)
        _file(tmp_path / outcome, "tok-i2", expires_at=now[0] + 3600, generation=4)
        write_token_file(token_path(tmp_path / outcome / "config" / "identity", 100, 100_000),
                         TokenFile(identity="i2", generation=4, token="tok-i2", expires_at=now[0] + 3600, session_id="sid-1",
                                   pid=100, started_at=100_000, proc_start=T0))
        own = str(token_path(tmp_path / outcome / "config" / "identity", 100, 100_000))
        assert lease.adopt_marker(identity="i2", generation=4, token_file=own)
        release.set()
        worker.join(5)
        assert (lease.identity, lease.generation, lease.token) == ("i2", 4, "tok-i2"), outcome
        assert not lease.superseded, outcome


def test_the_swap_never_regresses_the_session_id_the_hook_wrote(tmp_path: Path) -> None:
    """The hook wrote s2 (a /clear) after the lease last read the file; the lease's refresh still thinks s1. The file keeps
    s2: it is later in the file's own list."""
    from nexus.identity.token_file import read_token_file

    now = [1_000.0]
    lease, engine, _ = _setup(tmp_path, now)
    _file(tmp_path, "tok-0", expires_at=now[0] + 3600)
    lease.tick()
    path = token_path(tmp_path / "config" / "identity", 100, 100_000)
    write_token_file(path, TokenFile(identity="i1", generation=1, token="tok-0", expires_at=now[0] + 3600, session_id="s2",
                                     pid=100, started_at=100_000, proc_start=T0, session_ids=("sid-1", "s2")))
    lease.token, lease.token_expires_at = "tok-refreshed", now[0] + 3600
    lease._rewrite_file()
    kept = read_token_file(path)
    assert (kept.session_id, kept.session_ids, kept.token) == ("s2", ("sid-1", "s2"), "tok-refreshed")
    lease.session_id = "branch"                       # a fork the lease just made is newer than anything in the file
    lease._rewrite_file()
    assert read_token_file(path).session_id == "branch"
