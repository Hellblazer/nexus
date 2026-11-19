# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-253 engine 0.1.198 hardening, the client half (nexus-hczdo; Sam 2026-11-19, option A, cap 5).

A data credential's ``refresh`` or ``fork`` must carry the identity's own live token (the fence's check); an expired token
cannot refresh, and the session rebinds on its next start. The seat cap is 5 per identity: the sixth is ``409 seat_limit``.
Tenant-free: an httpx MockTransport stands for the engine, and the lease runs on a fake engine and injected clocks.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from structlog.testing import capture_logs

from nexus.commands.identity_cmd import identity_group
from nexus.identity import ops, token_contract, token_header
from nexus.identity.binder import LaunchFacts, SessionBinder
from nexus.identity.bind_client import (
    SCOPE_DATA, SCOPE_TENANT, IdentityApiError, IdentityClient, IdentityUnavailableError, seat_refusal_message,
)
from nexus.identity.ops import CallerFacts, IdentityEnv
from nexus.identity.token_file import TokenFile, read_token_file, token_path, write_token_file
from nexus.mcp.identity_lease import HEARTBEAT_S, WAKE_RETRY_S, IdentityLease
from nexus.operators.delegated_identity import DelegatedIdentity
from tests.identity.fake_engine import FakeEngine
from tests.identity.fakes import HOST, FakeProcesses

HEADER = token_contract.IDENTITY_TOKEN_HEADER
T0 = "2026-11-17T08:00:00Z"
LAPSED_LINE = "the identity token expired while the session slept; it rebinds on the next start"


# ── the client: refresh and fork carry the token, nothing else does ─────────


class Engine:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.answers.get((request.method, request.url.path), (200, {"id": "i7", "generation": 1}))
        return httpx.Response(status, json=body)

    def client(self, scope: str = SCOPE_DATA) -> IdentityClient:
        return IdentityClient("http://engine.test", "bearer", scope=scope, http=httpx.Client(transport=httpx.MockTransport(self)))

    @property
    def headers(self) -> list[str | None]:
        return [r.headers.get(HEADER) for r in self.requests]


@pytest.fixture
def engine() -> Engine:
    return Engine()


def _record(token: str | None = "tok-own", expires_at: float | None = 9e9) -> TokenFile:
    return TokenFile(identity="i7", generation=1, token=token, expires_at=expires_at, session_id="sid-1", pid=111,
                     started_at=111_000, proc_start=None, host=HOST)


@pytest.mark.parametrize("event", ["refresh", "fork"])
def test_a_data_credential_refresh_and_fork_send_the_token_they_are_given(engine: Engine, event: str) -> None:
    engine.client().bind("i7", event, 1, {"pid": 1}, token="tok-a")
    assert engine.headers == ["tok-a"]
    assert json.loads(engine.requests[0].content) == {"event": event, "expected_generation": 1, "pid": 1}, "the body is unchanged"


@pytest.mark.parametrize("event", ["start", "resume", "clear", "end", "release", "takeover"])
def test_every_other_bind_event_sends_no_token_even_when_one_is_given(engine: Engine, event: str) -> None:
    engine.client().bind("i7", event, 1, token="tok-a")
    assert engine.headers == [None]


@pytest.mark.parametrize("event", ["refresh", "fork"])
def test_a_refresh_or_fork_sends_exactly_the_token_given_and_never_an_ambient_one(
        engine: Engine, monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    """Every caller names the token of the identity it binds (the lease its own, the hook its own file's, a delegated child
    its own). The nearest Claude ancestor's file may be another identity's (a delegated child, a test under a live
    session), so given none, none is sent and the engine's 403 is the answer."""
    monkeypatch.setattr(token_header, "current", lambda **_: _record("tok-ambient"))
    engine.client().bind("i7", event, 1, token="tok-a")
    engine.client().bind("i7", event, 1)
    assert engine.headers == ["tok-a", None]


@pytest.mark.parametrize("event", ["refresh", "fork"])
def test_a_process_with_no_live_token_sends_none_and_the_engines_403_is_the_answer(
        engine: Engine, monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    monkeypatch.setattr(token_header, "current", lambda **_: None)
    engine.answers[("POST", "/v1/identity/i7/bind")] = (403, {"error": "identity_token_required", "message": "no token"})
    with pytest.raises(IdentityApiError) as err:
        engine.client().bind("i7", event, 1)
    assert engine.headers == [None] and (err.value.status, err.value.code) == (403, "identity_token_required")


@pytest.mark.parametrize("event", ["refresh", "fork"])
def test_a_tenant_credential_bind_is_unchanged(engine: Engine, monkeypatch: pytest.MonkeyPatch, event: str) -> None:
    """The tenant credential is the trust boundary for a request that names no process; an identity token the engine cannot
    resolve is a 401 on every route it rides, so the tenant client never adds one."""
    monkeypatch.setattr(token_header, "current", lambda **_: _record())
    engine.client(SCOPE_TENANT).bind("i7", event, 1, token="tok-a")
    assert engine.headers == [None]


# ── 409 seat_limit ──────────────────────────────────────────────────────────


def _limit(**body: Any) -> IdentityApiError:
    return IdentityApiError(409, "seat_limit", "i7 already created 5 seats",
                            {"error": "seat_limit", "cap": 5, "seats_created": 5, "identity": "i7", **body})


SEAT_LIMIT_LINE = ("i7 has created its 5 seats, the most a session's credential may: reuse one of them, or make a new one "
                   "from a local-mode or operator shell (the tenant credential has no cap)")


def test_seat_limit_is_one_pinned_line_that_says_to_reuse_a_seat_and_never_to_remove_one() -> None:
    said = seat_refusal_message(_limit(), "i7")
    assert said == SEAT_LIMIT_LINE
    assert "\n" not in said and "purge" not in said and "rename" not in said, "no route removes a seat: do not advise it"


def test_seat_limit_reads_the_cap_the_engine_names() -> None:
    assert "its 7 seats" in seat_refusal_message(_limit(cap=7), "i7")
    assert "its 5 seats" in seat_refusal_message(IdentityApiError(409, "seat_limit", "x", {"limit": 9}), "i7"), (
        "a body with no cap says the constant")


def test_seat_limit_names_the_identity_the_body_carries_when_the_caller_passes_none() -> None:
    assert seat_refusal_message(_limit(identity="i9"), None).startswith("i9 has created its 5 seats")
    assert seat_refusal_message(IdentityApiError(409, "seat_limit", "x", {"cap": 5}), None).startswith("this identity has created")


def test_seat_limit_is_worded_per_caller() -> None:
    other = seat_refusal_message(_limit(), "i7", seat_limit="ask the operator to make one")
    assert other == "i7 has created its 5 seats, the most a session's credential may: ask the operator to make one"


def test_seat_limit_through_nx_identity_create_is_a_command_error_with_that_line(engine: Engine, tmp_path: Path) -> None:
    engine.answers[("POST", "/v1/identity")] = (409, {"error": "seat_limit", "message": "i7 already created 5 seats", "cap": 5,
                                                      "seats_created": 5, "identity": "i7"})
    env = IdentityEnv(client=engine.client(), procs=FakeProcesses(tmp_path / "claude-sessions", host=HOST),
                      identity_dir=tmp_path / "identity", environ={}, caller=lambda: CallerFacts(claude_pid=0, ancestors=()),
                      clock=lambda: 1_000.0, own_token=lambda: _record())
    result = CliRunner().invoke(identity_group, ["create", "sixth"], obj={"identity_env": env})
    assert result.exit_code == 1 and "i7 has created its 5 seats" in result.output, (
        "a create names no identity itself: the line says the one the engine's body names")
    with pytest.raises(ops.IdentityCommandError, match="5"):
        ops.create(env, "sixth")


# ── the lease ───────────────────────────────────────────────────────────────


class _Engine:
    """A fake engine that records the token each bind carried and can refuse a refresh."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.tokens: list[tuple[str, str | None]] = []
        self.refuse_bind: IdentityApiError | None = None
        self.unreachable = False

    def renew(self, identity: str, generation: int, client_version: str | None = None) -> dict:
        self.calls.append(("renew", (identity, generation)))
        if self.unreachable:
            raise IdentityUnavailableError("connection refused")
        return {}

    def session(self, session_id: str) -> dict:
        return {"identity": "i1"}

    def bind(self, identity: str, event: str, generation: int, evidence: Any = None, *, token: str | None = None) -> dict:
        self.calls.append((event, (identity, generation)))
        self.tokens.append((event, token))
        if self.refuse_bind is not None:
            raise self.refuse_bind
        return {"id": identity, "generation": generation, "identity_token": f"tok-{len(self.calls)}",
                "identity_token_expires_in_seconds": 3600}

    def count(self, event: str) -> int:
        return sum(1 for e, _ in self.calls if e == event)


class _Clocks:
    """A wall clock and a monotonic clock that advance together, or apart (a sleep moves only the wall clock)."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.wall, self.mono = start, 50.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    def sleep(self, seconds: float) -> None:
        self.wall += seconds


REFUSED = IdentityApiError(403, "identity_token_required", "a live identity token is required", {"error": "identity_token_required"})


def _lease(tmp_path: Path, clocks: _Clocks, *, with_mono: bool = True) -> tuple[IdentityLease, _Engine, FakeProcesses]:
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(100, T0, started_at=100_000, session_id="sid-1")
    engine = _Engine()
    extra: dict[str, Any] = {"monotonic": lambda: clocks.mono} if with_mono else {}
    lease = IdentityLease(client_factory=lambda: engine, config_dir=tmp_path / "config", claude_pid=100, procs=procs,
                          clock=lambda: clocks.wall, client_version="1", sleep=lambda s: None, **extra)
    return lease, engine, procs


def _path(tmp_path: Path) -> Path:
    return token_path(tmp_path / "config" / "identity", 100, 100_000)


def _file(tmp_path: Path, token: str | None, expires_at: float | None, generation: int = 1) -> None:
    write_token_file(_path(tmp_path), TokenFile(identity="i1", generation=generation, token=token, expires_at=expires_at,
                                                session_id="sid-1", pid=100, started_at=100_000, proc_start=T0))


def test_the_lease_refresh_and_fork_carry_the_token_being_refreshed(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)           # under half of the hour left: a refresh is due
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    clocks.advance(HEARTBEAT_S)
    lease.tick()
    assert engine.tokens[0] == ("fork", "tok-0")
    refreshed_with = lease.token
    clocks.advance(HEARTBEAT_S)
    lease.token_expires_at = clocks.wall + 1_000
    lease.tick()
    assert ("refresh", refreshed_with) in engine.tokens, "the refresh carries the token the lease holds, the one it replaces"


def test_a_refresh_answered_identity_token_required_lapses_the_token_logs_once_and_stops_refreshing(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)           # a refresh is due
    engine.refuse_bind = REFUSED
    on_disk = _path(tmp_path).read_bytes()
    with capture_logs() as logs:
        first = lease.tick()
        for _ in range(5):
            clocks.advance(HEARTBEAT_S)
            report = lease.tick()
            assert report.renewed and not report.refreshed and report.error is None, "the heartbeat goes on; only refresh stops"
    assert first.error is None and not first.refreshed
    assert engine.count("refresh") == 1, "no refresh after the refusal, until a SessionStart bind writes a fresh token"
    said = [e for e in logs if LAPSED_LINE in str(e.get("consequence", "")) or LAPSED_LINE in str(e.get("event", ""))]
    assert len(said) == 1, "logged once, not once a minute"
    assert not any(e.get("event") == "identity_lease_refused" for e in logs), "not the generic per-tick refusal warning"
    assert _path(tmp_path).read_bytes() == on_disk, "the token file is left as the next SessionStart bind will find it"
    assert lease.bound and not lease.superseded
    assert engine.count("start") == 0 and engine.count("resume") == 0, "no fallback bind"


def test_a_fork_refused_identity_token_required_lapses_it_too_and_never_blocks_the_renewal(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    engine.refuse_bind = REFUSED
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_000)
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    with capture_logs() as logs:
        report = lease.tick()
        clocks.advance(HEARTBEAT_S)
        lease.tick()
    assert report.renewed and report.error is None, "the seat keeps its lease"
    assert engine.count("fork") == 1
    assert len([e for e in logs if LAPSED_LINE in str(e.get("consequence", ""))]) == 1


@pytest.mark.parametrize("refusal", [
    IdentityApiError(403, "not_owner", "the token names another identity", {}),
    IdentityApiError(500, None, "engine error", {}),
    REFUSED,
], ids=["not_owner", "server_error", "identity_token_required"])
@pytest.mark.parametrize("token", ["tok-0", None], ids=["token", "no_token"])
def test_a_refused_fork_never_stops_the_renewal_on_the_same_tick(tmp_path: Path, refusal: IdentityApiError,
                                                                token: str | None) -> None:
    """The fork row is sent before the renewal in one heartbeat: a refusal of it used to skip the renewal, retry every tick
    and lapse the seat in 300 seconds with the session alive."""
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    engine.refuse_bind = refusal
    _file(tmp_path, token, expires_at=clocks.wall + 3_000 if token else None)
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    for _ in range(3):
        report = lease.tick()
        assert report.renewed and not report.superseded, "the lease is renewed whatever the fork row did"
        clocks.advance(HEARTBEAT_S)
    assert engine.count("renew") == 3
    if refusal is REFUSED:
        assert engine.count("fork") == 1, "a lapsed token is not retried"
    else:
        assert engine.count("fork") == 3, "another refusal is retried on the next tick"


def test_a_fork_refused_for_a_stale_generation_still_flags_superseded_and_skips_the_renewal(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    engine.refuse_bind = IdentityApiError(409, "generation_superseded", "i1 is at generation 2", {})
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_000)
    procs.write_registry(100, 100_000, T0, session_id="branch-sid")
    report = lease.tick()
    assert report.superseded and not report.renewed and engine.count("renew") == 0


def test_another_403_or_a_stale_generation_is_not_a_lapse(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)
    engine.refuse_bind = IdentityApiError(403, "not_owner", "the token names another identity", {})
    with capture_logs() as logs:
        assert lease.tick().error is not None
        clocks.advance(HEARTBEAT_S)
        lease.tick()
    assert engine.count("refresh") == 2, "a refusal that is not a lapse is retried as before"
    assert not any(LAPSED_LINE in str(e.get("consequence", "")) for e in logs)


def test_a_new_live_token_in_the_file_ends_the_lapse(tmp_path: Path) -> None:
    """The next SessionStart bind writes a live token into the file; the lease adopts it and refreshes with it again."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)
    engine.refuse_bind = REFUSED
    lease.tick()
    clocks.advance(HEARTBEAT_S)
    lease.tick()
    assert engine.count("refresh") == 1
    engine.refuse_bind = None
    _file(tmp_path, "tok-rebound", expires_at=clocks.wall + 1_000)
    clocks.advance(HEARTBEAT_S)
    assert lease.tick().refreshed and engine.tokens[-1] == ("refresh", "tok-rebound")


# ── wake catch-up ───────────────────────────────────────────────────────────


def test_a_wake_inside_the_token_life_refreshes_on_the_first_tick_even_above_the_refresh_line(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    assert lease.tick().renewed and engine.count("refresh") == 0
    clocks.sleep(300)                                  # five minutes asleep: the wall clock jumps, the monotonic one does not
    clocks.advance(HEARTBEAT_S)                        # still above the 50-minute refresh line (3 240 s left): only the wake owes one
    woke = lease.tick()
    assert woke.refreshed and engine.tokens[-1] == ("refresh", "tok-0")
    assert lease.token_expires_at == clocks.wall + 3_600, "the new token has its full life again"


def test_a_normal_tick_above_the_refresh_line_does_not_refresh(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    for _ in range(5):
        clocks.advance(HEARTBEAT_S)
        assert not lease.tick().refreshed
    clocks.sleep(HEARTBEAT_S / 2)                      # a small step (an NTP correction) is not a wake
    clocks.advance(HEARTBEAT_S)
    assert not lease.tick().refreshed and engine.count("refresh") == 0


def test_a_wake_is_seen_without_a_monotonic_clock_that_stops_in_sleep(tmp_path: Path) -> None:
    """A monotonic clock that counts sleep (Windows) shows no jump; a gap of several heartbeats between ticks is the signal."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.advance(200)                                # both clocks moved: no jump, but over 3 minutes without a tick
    assert lease.tick().refreshed


def test_a_wake_past_the_token_life_is_the_lapse_not_a_refresh_with_a_dead_token(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.sleep(4_000)
    clocks.advance(HEARTBEAT_S)
    engine.refuse_bind = REFUSED
    with capture_logs() as logs:
        lease.tick()
    assert engine.tokens[-1] == ("refresh", None), "an expired token is not sent"
    assert len([e for e in logs if LAPSED_LINE in str(e.get("consequence", ""))]) == 1


def test_a_wake_while_the_engine_is_still_unreachable_retries_soon_then_refreshes(tmp_path: Path) -> None:
    """The network is not up for a few seconds after a wake: the refresh is not left to the next 60-second heartbeat."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.sleep(300)
    clocks.advance(HEARTBEAT_S)
    engine.unreachable = True
    woke = lease.tick()
    assert "engine unavailable" in (woke.error or "") and not woke.refreshed
    assert lease._next_heartbeat - clocks.wall == WAKE_RETRY_S
    engine.unreachable = False
    clocks.advance(WAKE_RETRY_S)
    assert lease.due() and lease.tick().refreshed


def test_without_an_injected_monotonic_clock_an_injected_wall_clock_never_reads_as_a_wake(tmp_path: Path) -> None:
    """Tests (and the engine journeys) step a fake wall clock by minutes; only the real pair of clocks can show a wake."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks, with_mono=False)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.advance(300)                                # still above the refresh line: only a wake would refresh
    assert not lease.tick().refreshed


def test_a_delegated_identity_refreshes_with_its_own_token_not_the_dispatchers(tmp_path: Path) -> None:
    """The process's token file names the dispatcher (the parent); the delegated child's refresh sends the child's token."""
    now = [1_000.0]
    engine = _Engine()
    handle = DelegatedIdentity(client=engine, identity="i9", generation=1, path=tmp_path / "i9.token", token="tok-child",
                               lifetime_s=3_600.0, evidence={"pid": 1, "started_at": 2, "proc_start": None, "host": HOST},
                               clock=lambda: now[0])
    assert handle.tick() and engine.tokens == []
    now[0] += 1_801
    assert handle.tick()
    assert engine.tokens == [("refresh", "tok-child")]


def test_the_token_file_of_a_lapsed_lease_is_read_back_unchanged(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)
    before = read_token_file(_path(tmp_path))
    engine.refuse_bind = REFUSED
    lease.tick()
    assert read_token_file(_path(tmp_path)) == before


# ── round 1 review: refresh line, lapse race, wake bookkeeping, clock steps ─────────────────────────────────


def test_a_sleep_of_45_minutes_never_lapses_a_token_whatever_its_phase(tmp_path: Path) -> None:
    """Critic S2: refreshing at half life left 30 to 60 minutes of life at any sleep start, so a sleep of 45 minutes lapsed
    a token that was 31 minutes from expiry. Refreshing under 50 minutes of life leaves at least 50 at any sleep start."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    for _ in range(29):                                # 29 minutes of heartbeats: under half-life refreshing, no refresh yet
        lease.tick()
        clocks.advance(HEARTBEAT_S)
    assert lease.token_expires_at is not None and lease.token_expires_at - clocks.wall <= 3_600
    clocks.sleep(45 * 60)                              # the laptop sleeps 45 minutes
    clocks.advance(HEARTBEAT_S)
    woke = lease.tick()
    assert woke.refreshed and engine.tokens[-1][0] == "refresh" and engine.tokens[-1][1] is not None, (
        "the token was still live at the wake, so the refresh carried it")
    assert not lease._token_lapsed and lease.token_expires_at is not None and lease.token_expires_at > clocks.wall


def test_a_token_adopted_while_the_refresh_was_out_is_not_lapsed_by_the_late_403(tmp_path: Path) -> None:
    """Reviewer I1: a marker adopts a live token on the loop thread while a refresh with the old token is in flight; the 403
    that comes back is about the old token and must not stop refreshing the new one."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)           # a refresh is due
    once = {"left": 1}
    orig = engine.bind

    def bind(identity: str, event: str, generation: int, evidence: Any = None, *, token: str | None = None) -> dict:
        if event == "refresh" and once["left"]:
            once["left"] -= 1
            _file(tmp_path, "tok-rebound", expires_at=clocks.wall + 3_600)   # the hook rebinds meanwhile
            assert lease.adopt_marker(identity="i1", generation=1, token_file=str(_path(tmp_path)))
            raise REFUSED
        return orig(identity, event, generation, evidence, token=token)

    engine.bind = bind                                 # type: ignore[method-assign]
    lease.tick()
    assert not lease._token_lapsed and lease.token == "tok-rebound"
    for _ in range(12):                                # the new token ages past the 50-minute line: it is refreshed
        clocks.advance(HEARTBEAT_S)
        lease.tick()
    assert ("refresh", "tok-rebound") in engine.tokens, "the new token is still being refreshed"


def test_a_newer_but_expired_token_in_the_file_does_not_clear_the_lapse(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)
    engine.refuse_bind = REFUSED
    lease.tick()
    assert lease._token_lapsed
    _file(tmp_path, "tok-dead", expires_at=clocks.wall - 10)           # a different token, but already past its life
    for _ in range(3):
        clocks.advance(HEARTBEAT_S)
        lease.tick()
    assert lease._token_lapsed and engine.count("refresh") == 1, "an expired token is not a rebind"


def test_a_wake_seen_while_lapsed_is_forgotten_so_a_later_rebind_is_not_refreshed_for_it(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 1_000)
    engine.refuse_bind = REFUSED
    lease.tick()
    assert lease._token_lapsed
    clocks.sleep(300)                                  # a wake while there is nothing to refresh
    clocks.advance(HEARTBEAT_S)
    lease.tick()
    assert not lease._wake_pending
    engine.refuse_bind = None
    _file(tmp_path, "tok-rebound", expires_at=clocks.wall + 3_600)
    clocks.advance(HEARTBEAT_S)
    assert not lease.tick().refreshed, "a fresh token needs no refresh for a wake it was not alive for"


def test_a_wake_owed_a_refresh_is_forgotten_when_the_seat_is_not_held(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.sleep(300)
    clocks.advance(HEARTBEAT_S)

    def not_held(identity: str, generation: int, client_version: str | None = None) -> dict:
        raise IdentityApiError(409, "identity_not_held", "nobody holds it", {})

    engine.renew = not_held                            # type: ignore[method-assign]
    lease.tick()
    assert not lease._wake_pending


def test_the_wake_retry_backs_off_from_5_seconds_to_a_minute_and_starts_over_after_a_refresh(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.sleep(300)
    clocks.advance(HEARTBEAT_S)
    engine.unreachable = True
    waits = []
    for _ in range(7):
        lease.tick()
        waits.append(lease._next_heartbeat - clocks.wall)
        clocks.advance(waits[-1])
    assert waits == [5, 10, 20, 40, 60, 60, 60], "doubling, capped at the heartbeat"
    engine.unreachable = False
    assert lease.tick().refreshed
    clocks.sleep(300)
    clocks.advance(HEARTBEAT_S)
    engine.unreachable = True
    lease.tick()
    assert lease._next_heartbeat - clocks.wall == WAKE_RETRY_S, "a new wake starts the schedule over"


def test_a_backwards_wall_clock_step_makes_the_next_heartbeat_and_renewal_due_at_once(tmp_path: Path) -> None:
    """Critic M4: a clock set back by N seconds stalled the schedule N seconds, and a renew gap over 300 s lapses the seat."""
    clocks = _Clocks()
    lease, engine, _ = _lease(tmp_path, clocks)
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    lease.tick()
    clocks.advance(HEARTBEAT_S)
    lease.tick()
    before = engine.count("renew")
    clocks.wall -= 400                                 # the step back
    assert lease.due(), "not left to wait the 400 seconds out"
    lease.tick()
    assert engine.count("renew") == before + 1


def test_a_refused_fork_row_warns_once_per_session_id(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    engine.refuse_bind = IdentityApiError(403, "not_owner", "the token names another identity", {})
    _file(tmp_path, "tok-0", expires_at=clocks.wall + 3_600)
    procs.write_registry(100, 100_000, T0, session_id="branch-1")
    with capture_logs() as logs:
        for _ in range(4):
            clocks.advance(HEARTBEAT_S)
            report = lease.tick()
            assert report.renewed, "the renewal is not the fork row's hostage"
    assert len([e for e in logs if e.get("event") == "identity_lease_fork_failed"]) == 1
    procs.write_registry(100, 100_000, T0, session_id="branch-2")
    with capture_logs() as logs:
        clocks.advance(HEARTBEAT_S)
        lease.tick()
    assert len([e for e in logs if e.get("event") == "identity_lease_fork_failed"]) == 1, "another session id warns again"


def test_no_token_value_reaches_a_lease_log_line(tmp_path: Path) -> None:
    clocks = _Clocks()
    lease, engine, procs = _lease(tmp_path, clocks)
    engine.session = lambda sid: (_ for _ in ()).throw(IdentityApiError(404, "unknown_session", "no", {}))   # type: ignore[method-assign]
    _file(tmp_path, "tok-secret-0", expires_at=clocks.wall + 1_000)
    procs.write_registry(100, 100_000, T0, session_id="branch-1")
    engine.refuse_bind = REFUSED
    with capture_logs() as logs:
        for _ in range(3):
            lease.tick()
            clocks.sleep(300)
            clocks.advance(HEARTBEAT_S)
        engine.unreachable = True
        lease.tick()
    assert any(LAPSED_LINE in str(e.get("consequence", "")) for e in logs), "the scenario reached the lapse line"
    assert "tok-secret" not in repr(logs) and "tok-" not in repr(logs)


# ── the delegated child: a refresh refused for want of a live token stops, the renewal goes on ─────────────


def test_a_delegated_refresh_refused_identity_token_required_logs_once_and_stops_while_renewal_continues(tmp_path: Path) -> None:
    now = [1_000.0]
    engine = _Engine()
    engine.refuse_bind = REFUSED
    handle = DelegatedIdentity(client=engine, identity="i9", generation=1, path=tmp_path / "i9.token", token="tok-child",
                               lifetime_s=3_600.0, evidence={"pid": 1, "started_at": 2, "proc_start": None, "host": HOST},
                               clock=lambda: now[0])
    now[0] += 1_801                                    # past its half life: a refresh is due
    with capture_logs() as logs:
        for _ in range(5):
            assert handle.tick(), "the loop goes on"
            now[0] += 60
    assert engine.count("refresh") == 1, "no refresh after the refusal"
    assert engine.count("renew") == 5, "renewal continues"
    assert len([e for e in logs if e.get("event") == "delegated_identity_token_lapsed"]) == 1
    assert not any(e.get("event") == "delegated_identity_renew_failed" for e in logs)
    assert "tok-child" not in repr(logs)


# ── the hook: a fork refused for want of a live token falls through to a start of the SAME identity ───────


class _ExpiredTokenEngine(FakeEngine):
    """A data credential's engine after the laptop slept past the token's hour: a fork is refused, a start needs no token."""

    def bind(self, identity: str, event: str, expected_generation: int, evidence: Any = None, *, token: Any = None) -> dict:
        if event == "fork":
            self.calls.append(("bind", (identity, event, expected_generation, dict(evidence or {}))))
            raise IdentityApiError(403, "identity_token_required", "a live identity token is required",
                                   {"error": "identity_token_required"})
        return super().bind(identity, event, expected_generation, evidence, token=token)


class _CloudDataEngine(_ExpiredTokenEngine):
    """The production cloud /clear: the data credential may not send ``clear`` (403 forbidden), and its ``fork`` is refused."""

    def bind(self, identity: str, event: str, expected_generation: int, evidence: Any = None, *, token: Any = None) -> dict:
        if event == "clear":
            self.calls.append(("bind", (identity, event, expected_generation, dict(evidence or {}))))
            raise IdentityApiError(403, "forbidden", "a 'data' credential may not call clear", {"error": "forbidden"})
        return super().bind(identity, event, expected_generation, evidence, token=token)


def _own(engine: FakeEngine, **kw: Any) -> str:
    holder = {"host": HOST, "pid": 100, "started_at": 100_000, "proc_start": T0, "session_id": "s1"}
    return engine.stage(state="live", generation=1, holder=holder, **kw)


def _binder_facts(tmp_path: Path, engine: FakeEngine, source: str, *, privileged: bool = False,
                  **kw: Any) -> tuple[SessionBinder, LaunchFacts]:
    procs = FakeProcesses(tmp_path / "sessions")
    procs.start(100, T0, started_at=100_000, session_id="s2")
    binder = SessionBinder(client=engine, procs=procs, identity_dir=tmp_path / "identity", allow_privileged=privileged)
    return binder, LaunchFacts(source=source, session_id="s2", host=procs.host, claude_pid=100, proc_start=T0,
                               started_at=100_000, entrypoint="cli", **kw)


def _events(engine: FakeEngine) -> list[str]:
    return [args[1] for name, args in engine.calls if name == "bind"] + [name for name, _ in engine.calls if name == "create_ephemeral"]


def _assert_same_identity_one_binding(engine: FakeEngine, out: Any, own: str, generation: int = 1) -> None:
    """The session ends bound to the identity it held, at the same generation, with ONE live binding for its pid."""
    assert out.action == "bound" and out.identity == own, f"not the held identity: {out.row} {out.identity} {out.notices}"
    assert out.generation == generation and out.token_file is not None and out.token_file.exists()
    assert "create_ephemeral" not in _events(engine), "no second identity"
    live = engine.bindings_by_host(HOST, 100)
    assert [v["id"] for v in live] == [own], f"exactly one live binding for the pid: {[v['id'] for v in live]}"
    starts = [args for name, args in engine.calls if name == "bind" and args[1] == "start"]
    assert len(starts) == 1 and starts[0][0] == own and starts[0][2] == generation
    assert starts[0][3]["session_id"] == "s2", "the start carries the NEW session id: the fork it replaces indexed it"
    assert starts[0][3].get("pid") == 100 and starts[0][3].get("started_at") == 100_000, "the same process evidence"
    assert "holder_gone" not in starts[0][3]
    assert engine.sessions["s2"][0] == own, "the session index knows the new session id"


@pytest.mark.parametrize("env_seat", [True, False], ids=["NX_SEAT", "no_NX_SEAT"])
@pytest.mark.parametrize("kind", ["ephemeral", "seat"])
@pytest.mark.parametrize("source", ["fork", "resume"])
def test_a_hook_refused_for_an_expired_token_restarts_the_same_identity_and_ends_bound(
        tmp_path: Path, source: str, kind: str, env_seat: bool) -> None:
    """Reviewer I2 and critic S1: a start of the held identity (no token needed, the engine's same-process branch), never a
    new ephemeral and never another named seat. ``resume`` here is an unindexed session id in a process that holds one."""
    engine = _ExpiredTokenEngine()
    own = _own(engine, kind=kind, name="alpha" if kind == "seat" else None)
    binder, facts = _binder_facts(tmp_path, engine, source, seat="alpha" if env_seat and kind == "seat" else None)
    out = binder.bind(facts)
    _assert_same_identity_one_binding(engine, out, own)
    assert [e for e in _events(engine) if e in ("fork", "start")][:2] == ["fork", "start"], "the fork was tried first"


def test_a_resume_naming_another_seat_never_leaves_the_held_identity_when_the_token_is_lapsed(tmp_path: Path) -> None:
    """The unprivileged path with a differently named seat: it keeps what it holds (the method's own docstring)."""
    engine = _ExpiredTokenEngine()
    own = _own(engine, kind="ephemeral")
    engine.stage(kind="seat", name="beta", state="unheld", generation=3)
    binder, facts = _binder_facts(tmp_path, engine, "resume", seat="beta")
    out = binder.bind(facts)
    _assert_same_identity_one_binding(engine, out, own)


@pytest.mark.parametrize("kind", ["ephemeral", "seat"])
def test_the_privileged_cloud_clear_falls_through_clear_403_then_fork_403_to_the_same_identity(tmp_path: Path, kind: str) -> None:
    engine = _CloudDataEngine()
    own = _own(engine, kind=kind, name="alpha" if kind == "seat" else None)
    binder, facts = _binder_facts(tmp_path, engine, "clear", privileged=True)
    out = binder.bind(facts)
    _assert_same_identity_one_binding(engine, out, own)
    assert [e for e in _events(engine) if e in ("clear", "fork", "start")] == ["clear", "fork", "start"]


def test_a_fork_refused_for_another_reason_still_ends_unbound_with_the_engines_words(tmp_path: Path) -> None:
    """Only a missing token falls through: another 403 is not a reason to start anything."""

    class _NotOwner(_ExpiredTokenEngine):
        def bind(self, identity: str, event: str, expected_generation: int, evidence: Any = None, *, token: Any = None) -> dict:
            if event == "fork":
                raise IdentityApiError(403, "not_owner", "the token names another identity", {})
            return FakeEngine.bind(self, identity, event, expected_generation, evidence, token=token)

    engine = _NotOwner()
    _own(engine, kind="ephemeral")
    binder, facts = _binder_facts(tmp_path, engine, "fork")
    out = binder.bind(facts)
    assert out.action == "unbound" and "not_owner" in " ".join(out.notices)
    assert "start" not in _events(engine) and "create_ephemeral" not in _events(engine)


class _NoClearEngine(FakeEngine):
    """The data credential's engine: ``clear`` is refused 403 forbidden, so the hook sends the same change as ``fork``."""

    def bind(self, identity: str, event: str, expected_generation: int, evidence: Any = None, *, token: Any = None) -> dict:
        if event == "clear":
            raise IdentityApiError(403, "forbidden", "a 'data' credential may not call clear", {"error": "forbidden"})
        if event != "fork":
            return super().bind(identity, event, expected_generation, evidence, token=token)
        ev = dict(evidence or {})                      # the shared fake has no fork: same generation, new session id
        self.calls.append(("bind", (identity, event, expected_generation, ev)))
        if token is not None:
            self.bind_tokens.append((event, token))
        rec = self.records[identity]
        rec["holder"] = {**(rec["holder"] or {}), "session_id": ev.get("session_id")}
        self.sessions[ev["session_id"]] = (identity, rec["generation"])
        return {**self._view(rec), "event": event, "noop": False}


def _own_file(tmp_path: Path, identity: str, *, token: str = "tok-own", generation: int = 1,
              expires_at: float = 9e9) -> None:
    write_token_file(token_path(tmp_path / "identity", 100, 100_000), TokenFile(
        identity=identity, generation=generation, token=token, expires_at=expires_at, session_id="s1", pid=100,
        started_at=100_000, proc_start=T0, host=HOST))


@pytest.mark.parametrize("source", ["clear", "fork", "resume"])
def test_the_hooks_fork_sends_its_own_token_files_token_never_the_ambient_one(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    """The hook knows its process (``<claude pid>.<startedAt>``), so its fork carries that file's token. The nearest Claude
    ancestor of the hook's own interpreter may hold another identity (a test under a live session, a nested launch), and
    the engine refuses that token. ``resume`` here is an unindexed session id in a process that holds one (a fork row)."""
    monkeypatch.setattr(token_header, "current", lambda **_: _record("tok-ambient"))
    engine = _NoClearEngine()
    own = _own(engine, kind="seat", name="alpha")
    _own_file(tmp_path, own)
    binder, facts = _binder_facts(tmp_path, engine, source, privileged=True)
    out = binder.bind(facts)
    assert (out.identity, out.event) == (own, "fork"), f"{out.row} {out.notices}"
    assert engine.bind_tokens == [("fork", "tok-own")]


@pytest.mark.parametrize("stale", ["another_identity", "another_generation", "expired"])
def test_the_hooks_fork_sends_no_token_when_its_file_holds_none_for_this_identity(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale: str) -> None:
    """A file naming another identity or generation, or an expired token: none is sent (never the ambient one), and a
    data engine's 403 then restarts the same identity (the existing fall-through)."""
    monkeypatch.setattr(token_header, "current", lambda **_: _record("tok-ambient"))
    engine = _NoClearEngine()
    own = _own(engine, kind="seat", name="alpha")
    _own_file(tmp_path, "i999" if stale == "another_identity" else own, generation=2 if stale == "another_generation" else 1,
              expires_at=1.0 if stale == "expired" else 9e9)
    binder, facts = _binder_facts(tmp_path, engine, "fork")
    out = binder.bind(facts)
    assert out.event == "fork" and engine.bind_tokens == []


def test_no_token_value_reaches_a_binder_log_line(tmp_path: Path) -> None:
    engine = _ExpiredTokenEngine()
    own = _own(engine, kind="seat", name="alpha")
    binder, facts = _binder_facts(tmp_path, engine, "fork")
    with capture_logs() as logs:
        out = binder.bind(facts)
    assert out.identity == own and out.token_file is not None
    token = read_token_file(out.token_file).token                      # type: ignore[union-attr]
    assert token and token not in repr(logs) and token not in " ".join(out.notices)
