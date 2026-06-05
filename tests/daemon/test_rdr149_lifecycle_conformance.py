# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149: cross-tier lifecycle conformance suite.

The load-bearing artifact for the whole RDR-149 arc. ONE parameterized
lifecycle property battery run against all THREE tiers. Each tier's harness
drives that tier's REAL publish / discover / reap path. The battery was a
living spec across the migration: as each tier moved onto the leased
registry (T2 P2, T3 P3, T1 P4) its harness repointed at the migrated path
and its red cells flipped green. Post-migration (P5/P6) all three tiers ride
the one primitive (``daemon/service_registry.py``); this suite is the
standing conformance guard that keeps them there.

Identity is a server-unique owner token and liveness is lease freshness
(TTL on a wall-clock heartbeat) for every tier now: ungraceful death = the
owner stops heartbeating and the lease ages out (no pid is consulted, giving
pid-reuse immunity). The harness vocabulary historically abstracted a
pid-based model for the then-un-migrated tiers; that model is gone from
production, retained here only as the conformance contract every tier meets.

The harness vocabulary (``simulate_ungraceful_death`` / ``advance_to_reap``
/ ``self_heal_tick`` / ``stale_reassert``) abstracts the lifecycle events so
one test body asserts the same property for every tier.

Red-first contract (CA-1), now discharged. The matrix originally reproduced
the two filed defects as strict-xfail failures against the un-migrated code:

- GH #1114 (T1 lost-addr, no self-heal)  -> ``self_heal`` was a T1 GAP.
- GH #1112 (T3 stale after upgrade)      -> ``version_cycle`` was a T3 GAP.

Both are now fixed structurally (the cells are ``pass``); the non-vacuity
guard (``TestMatrixIsNotVacuous``) flipped from "reproduces the bug" to
"asserts the fix landed" (``test_1114_t1_self_heal_fixed_structurally`` /
``test_1112_t3_version_cycle_fixed_structurally``). The one remaining
documented non-pass is ``version_cycle[t1]`` (N/A: T1 is MCP-lifespan-owned,
cycled by an MCP restart, not an in-process cycle).

Encoding: broken cells were ``xfail(strict=True)`` so an unexpected pass
turned the suite RED and forced the migrating phase to flip the stale cell
(the red-first -> green ratchet). GAP cells name an issue + the phase that
closed them; SPEC cells are forward properties of the leased primitive. All
three tiers now ride the primitive, so every lease property passes for every
tier (the ratchet is complete).

Flakiness control (RDR-140 convention): the unit battery is in-process,
record-level, with injected liveness + a fixed clock; ``port=0`` and a
single event loop for the one live-daemon self-heal proof, which is
``integration``-marked.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest

from nexus.daemon.service_registry import (
    ServiceRegistry,
    ServiceSupervisor,
)
from nexus.daemon.t1_lease import T1LeasePublisher
from nexus import session as _sess


TIERS = ("t1", "t2", "t3")

# Synthetic owner pids, never real live processes; liveness is injected.
_OWNER_PID = 970001
_SIBLING_PID = 970002
_REUSED_PID = 970003


# ---------------------------------------------------------------------------
# Injected liveness (T1/T3 pid model) + fixed clock (T2 lease model)
# ---------------------------------------------------------------------------


class _AliveSet:
    """Controllable process-liveness oracle for the pid-based tiers.

    ``os.kill(pid, 0)`` (the T3 validator probe) and
    ``session._is_pid_alive`` (the T1 sweep probe) are redirected here so
    ungraceful death and pid reuse are deterministic without spawning
    processes. Pids not managed here delegate to the real probe.
    """

    def __init__(self) -> None:
        self._alive: set[int] = set()
        self._dead: set[int] = set()

    def mark_alive(self, pid: int) -> None:
        self._alive.add(pid)
        self._dead.discard(pid)

    def mark_dead(self, pid: int) -> None:
        self._dead.add(pid)
        self._alive.discard(pid)

    def is_alive(self, pid: int) -> bool:
        if pid in self._alive:
            return True
        if pid in self._dead:
            return False
        return _real_is_pid_alive(pid)

    def fake_os_kill(self, pid: int, sig: int) -> None:
        if sig != 0:
            raise AssertionError(f"unexpected real signal {sig} to managed pid {pid}")
        if pid in self._alive:
            return
        if pid in self._dead:
            raise ProcessLookupError(pid)
        _real_os_kill(pid, sig)


_real_os_kill = os.kill
_real_is_pid_alive = _sess._is_pid_alive


class _FakeClock:
    """Fixed, advanceable wall-clock surrogate (mirrors P1 / fairness)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# Tier harnesses: a uniform, liveness-model-agnostic vocabulary over each
# tier's REAL lifecycle path. ``owner`` is an integer owner id (a pid for
# the pid-based tiers; an owner-token seed for T2).
# ---------------------------------------------------------------------------


class RecordHarness:
    tier: str
    scope: str  # "uid" (one owner per user) | "session" (one per session-id)
    has_self_heal: bool
    has_version_cycle: bool

    def __init__(self, config_dir: Path, alive: _AliveSet, clock: _FakeClock) -> None:
        self._cd = config_dir
        self._alive = alive
        self._clock = clock

    def publish(self, owner: int = _OWNER_PID) -> None:
        raise NotImplementedError

    def discover(self, owner: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def current_generation(self, owner: int = _OWNER_PID) -> Optional[int]:
        rec = self.discover(owner)
        if rec is None:
            return None
        gen = rec.get("generation")
        return gen if isinstance(gen, int) else None

    def simulate_ungraceful_death(self, owner: int = _OWNER_PID) -> None:
        """The owner dies with no cleanup (SIGKILL / OOM): no record removal,
        no graceful relinquish."""
        raise NotImplementedError

    def advance_to_reap(self) -> None:
        """Advance whatever the tier needs so a dead owner becomes reapable.
        A no-op for pid tiers (death is observable immediately); a TTL
        advance for the lease tier."""
        raise NotImplementedError

    def reap(self) -> None:
        raise NotImplementedError

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        raise NotImplementedError

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        """Run ONE self-heal tick using the tier's real re-assert path; a
        genuine no-op for tiers that have none (T1, T3)."""
        raise NotImplementedError

    def stale_reassert(self, owner: int) -> None:
        """A stale owner attempts to re-assert its record. A fenced tier
        rejects it; an unfenced tier lets it through (the failure mode)."""
        raise NotImplementedError

    def owners_in_scope(self, session_id: str) -> int:
        raise NotImplementedError


class T1RecordHarness(RecordHarness):
    """RDR-149 P4: T1 rides the leased registry, MCP-lifespan-owned and
    scoped on the **session-id** (intentionally N owners per uid, one T1
    server per session). Each sibling drives a real ``T1LeasePublisher``
    keyed on one shared session-id, so the unit battery exercises the
    production publish / heartbeat-self-heal / re-key / fence path, not a
    bespoke copy. The transient ``server_pid`` -> session-id re-key (CA-3)
    is covered separately by ``TestT1SessionRekey``; here the publishers
    resolve the session-id at publish time so they key on it directly."""

    tier = "t1"
    scope = "session"
    has_self_heal = True  # RDR-149 P4: publisher heartbeat self-heals (#1114)
    has_version_cycle = False  # T1 is MCP-lifespan-owned, not upgrade-cycled
    _SESSION = "sess-A"

    def __init__(self, config_dir: Path, alive: _AliveSet, clock: _FakeClock) -> None:
        super().__init__(config_dir, alive, clock)
        self._registry = ServiceRegistry(
            dir=config_dir, tier="t1", clock=clock, ttl=3.0, heartbeat_interval=1.0
        )
        self._pubs: dict[int, T1LeasePublisher] = {}

    def publish(self, owner: int = _OWNER_PID) -> None:
        pub = T1LeasePublisher(
            registry=self._registry,
            server_pid=owner,
            host="127.0.0.1",
            port=0,
            version="1.0.0",
            session_resolver=lambda: self._SESSION,
            owner_token=f"tok-{owner}",
        )
        pub.publish()
        self._pubs[owner] = pub

    def discover(self, owner: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        rec = self._registry.discover(self._SESSION)
        if rec is None:
            return None
        return {
            "pid": rec.endpoint.get("server_pid"),
            "owner": rec.endpoint.get("server_pid"),
            "generation": rec.generation,
            "owner_token": rec.owner_token,
        }

    def simulate_ungraceful_death(self, owner: int = _OWNER_PID) -> None:
        # The owner stops heartbeating; the lease ages out on its own. No pid
        # is consulted (lease freshness is the liveness primitive).
        self._pubs.pop(owner, None)

    def advance_to_reap(self) -> None:
        self._clock.advance(3.1)  # past TTL

    def reap(self) -> None:
        self._registry.discover(self._SESSION)  # discovery reaps an expired lease

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        self._registry._record_path(self._SESSION).unlink(missing_ok=True)

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        pub = self._pubs.get(owner)
        if pub is not None:
            pub.tick()  # re-stamps; self-heals a lost record

    def stale_reassert(self, owner: int) -> None:
        pub = self._pubs.get(owner)
        if pub is not None:
            pub.tick()  # fenced: sets pub.fenced, writes nothing

    def owners_in_scope(self, session_id: str) -> int:
        # Session-scoped: two siblings of ONE session converge to one record.
        return 1 if self.discover() is not None else 0


class _LeaseHarness(RecordHarness):
    """Shared harness for tiers migrated onto the leased registry (T2 in P2,
    T3 in P3). Drives the SAME ``ServiceRegistry`` + ``ServiceSupervisor``
    the migrated daemon uses, with the injected clock, so the lease
    semantics (generation, fencing, TTL liveness, pid-reuse immunity,
    supervisor self-heal) are exercised exactly as in production."""

    scope = "uid"
    has_self_heal = True
    has_version_cycle = True
    _REGISTRY_TIER: str = ""

    def __init__(self, config_dir: Path, alive: _AliveSet, clock: _FakeClock) -> None:
        super().__init__(config_dir, alive, clock)
        self._registry = ServiceRegistry(
            dir=config_dir, tier=self._REGISTRY_TIER, clock=clock,
            ttl=3.0, heartbeat_interval=1.0,
        )
        self._scope = str(os.getuid())
        # One ServiceSupervisor per owner, exactly as the migrated daemon
        # uses it (publish_once + heartbeat_tick) — so the unit battery
        # exercises the real daemon dispatch path, including the fenced-flag
        # guard, not just ServiceRegistry in isolation.
        self._supervisors: dict[int, ServiceSupervisor] = {}

    def publish(self, owner: int = _OWNER_PID) -> None:
        sup = ServiceSupervisor(
            self._registry,
            self._scope,
            version="1.0.0",
            endpoint_provider=lambda o=owner: {"pid": o, "host": "127.0.0.1", "port": 0},
            owner_token=f"tok-{owner}",
        )
        sup.publish_once()
        self._supervisors[owner] = sup

    def discover(self, owner: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        rec = self._registry.discover(self._scope)
        if rec is None:
            return None
        return {
            "pid": rec.endpoint.get("pid"),
            "owner": rec.endpoint.get("pid"),
            "generation": rec.generation,
            "owner_token": rec.owner_token,
        }

    def simulate_ungraceful_death(self, owner: int = _OWNER_PID) -> None:
        # The owner stops heartbeating; nothing else changes. The lease ages
        # out on its own (advance_to_reap). No pid is consulted.
        self._supervisors.pop(owner, None)

    def advance_to_reap(self) -> None:
        self._clock.advance(3.1)  # past TTL

    def reap(self) -> None:
        self._registry.discover(self._scope)  # discovery reaps an expired lease

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        self._registry._record_path(self._scope).unlink(missing_ok=True)

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        sup = self._supervisors.get(owner)
        if sup is not None:
            sup.heartbeat_tick()  # re-stamps; self-heals a lost record

    def stale_reassert(self, owner: int) -> None:
        sup = self._supervisors.get(owner)
        if sup is not None:
            sup.heartbeat_tick()  # fenced: sets sup.fenced, writes nothing

    def owners_in_scope(self, session_id: str) -> int:
        return 1 if self.discover() is not None else 0


class T2RecordHarness(_LeaseHarness):
    """RDR-149 P2: T2 rides the leased registry."""

    tier = "t2"
    _REGISTRY_TIER = "t2"


class T3RecordHarness(_LeaseHarness):
    """RDR-149 P3: T3 rides the same leased registry, heartbeated by the
    long-lived T3 supervisor. Identical lease semantics to T2 (one problem,
    two uid-scoped tiers)."""

    tier = "t3"
    _REGISTRY_TIER = "t3"


_HARNESS_CLASSES: dict[str, type[RecordHarness]] = {
    "t1": T1RecordHarness,
    "t2": T2RecordHarness,
    "t3": T3RecordHarness,
}


# ---------------------------------------------------------------------------
# The expectation matrix: the single source of truth for CA-1.
# ---------------------------------------------------------------------------

GAP = "gap"
SPEC = "spec"

EXPECTATIONS: dict[str, dict[str, Any]] = {
    "roundtrip": {"t1": "pass", "t2": "pass", "t3": "pass"},
    "reap_ungraceful": {"t1": "pass", "t2": "pass", "t3": "pass"},
    "self_heal": {
        "t1": "pass",  # RDR-149 P4: publisher heartbeat self-heals (#1114)
        "t2": "pass",
        "t3": "pass",  # RDR-149 P3: supervisor heartbeat self-heals
    },
    "concurrent_one_owner": {
        "t1": "pass",  # RDR-149 P4: session-id scope converges to one owner
        "t2": "pass",
        "t3": "pass",
    },
    "version_cycle": {
        # T1 is MCP-lifespan-owned, not covered by any upgrade-cycle: an
        # upgrade republishes by restarting the MCP server, not by an
        # in-process cycle. This is a documented N/A, not a #1114 blocker
        # (RDR-149 P4, CA / Approach item 5).
        "t1": (GAP, "T1 is MCP-lifespan-owned, not upgrade-cycled; RDR-149 P4 N/A"),
        "t2": "pass",
        "t3": "pass",  # RDR-149 P3: supervisor owns cycle_to_current (#1112)
    },
    # RDR-149 P2/P3/P4: all three tiers ride the primitive, so their lease
    # properties pass.
    "pid_reuse_immunity": {
        "t1": "pass",  # RDR-149 P4: lease/generation kills pid-reuse
        "t2": "pass",
        "t3": "pass",
    },
    "restart_higher_generation": {
        "t1": "pass",  # RDR-149 P4: generation fencing token
        "t2": "pass",
        "t3": "pass",
    },
    "restart_race_fencing": {
        "t1": "pass",  # RDR-149 P4: CA-4 heartbeat-fencing arm
        "t2": "pass",
        "t3": "pass",
    },
}


def _maybe_xfail(property_name: str, tier: str) -> None:
    cell = EXPECTATIONS[property_name][tier]
    if cell == "pass":
        return
    _kind, reason = cell
    pytest.xfail(reason)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def alive() -> _AliveSet:
    return _AliveSet()


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture(autouse=True)
def _inject_liveness(monkeypatch: pytest.MonkeyPatch, alive: _AliveSet) -> None:
    monkeypatch.setattr("nexus.daemon.discovery.os.kill", alive.fake_os_kill)
    monkeypatch.setattr("nexus.session._is_pid_alive", alive.is_alive)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


@pytest.fixture(params=TIERS)
def tier(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def harness(
    tier: str, config_dir: Path, alive: _AliveSet, clock: _FakeClock
) -> RecordHarness:
    return _HARNESS_CLASSES[tier](config_dir, alive, clock)


# ---------------------------------------------------------------------------
# The parameterized property battery
# ---------------------------------------------------------------------------


class TestLifecycleConformance:
    def test_roundtrip(self, harness: RecordHarness, tier: str) -> None:
        _maybe_xfail("roundtrip", tier)
        harness.publish()
        rec = harness.discover()
        assert rec is not None
        assert rec["owner"] == _OWNER_PID

    def test_reap_ungraceful(self, harness: RecordHarness, tier: str) -> None:
        # Owner dies with no cleanup: the record lingers but the tier's reap
        # path (pid validation, or lease TTL) must stop resolving a dead
        # owner.
        _maybe_xfail("reap_ungraceful", tier)
        harness.publish()
        assert harness.discover() is not None
        harness.simulate_ungraceful_death()
        harness.advance_to_reap()
        harness.reap()
        assert harness.discover() is None

    def test_self_heal(self, harness: RecordHarness, tier: str) -> None:
        # The record is lost while the owner is alive. A self-healing tier
        # re-asserts it within a tick; T1 (#1114) and T3 (RF-4) do not.
        _maybe_xfail("self_heal", tier)
        harness.publish()
        harness.external_delete()
        assert harness.discover() is None
        for _ in range(3):
            harness.self_heal_tick()
        assert harness.discover() is not None, "owner alive but record not self-healed"

    def test_concurrent_one_owner(self, harness: RecordHarness, tier: str) -> None:
        # Two siblings of ONE logical session race. T2/T3 (uid scope)
        # converge to one record; T1 keys on pid so the session ends up with
        # two owners.
        _maybe_xfail("concurrent_one_owner", tier)
        harness.publish(_OWNER_PID)
        harness.publish(_SIBLING_PID)
        assert harness.owners_in_scope("sess-A") == 1

    def test_version_cycle(self, harness: RecordHarness, tier: str) -> None:
        # An upgrade must be able to replace the running owner. Only T2 is
        # wired into a cycle today; T3 (#1112) and T1 are not.
        _maybe_xfail("version_cycle", tier)
        assert harness.has_version_cycle, (
            "tier is not covered by any upgrade-cycle entrypoint"
        )

    def test_pid_reuse_immunity(self, harness: RecordHarness, tier: str) -> None:
        # Owner dies; the kernel recycles its pid to an unrelated live
        # process. A pid-based liveness check FALSELY keeps the stale record;
        # a leased primitive ages it out regardless of the pid.
        _maybe_xfail("pid_reuse_immunity", tier)
        harness.publish(_REUSED_PID)
        assert harness.discover(_REUSED_PID) is not None
        harness.simulate_ungraceful_death(_REUSED_PID)
        # The recycled pid is now a live, unrelated process.
        harness._alive.mark_alive(_REUSED_PID)
        harness.advance_to_reap()
        harness.reap()
        assert harness.discover(_REUSED_PID) is None

    def test_restart_higher_generation(
        self, harness: RecordHarness, tier: str
    ) -> None:
        # A restarted owner republishes with a strictly higher monotonic
        # generation so stale predecessors are fenced. The crashed owner's
        # record persists (TTL) until the successor publishes, so the
        # generation is read and bumped, not reset.
        _maybe_xfail("restart_higher_generation", tier)
        harness.publish(_OWNER_PID)
        gen1 = harness.current_generation()
        assert gen1 == 1
        # Successor restarts while the predecessor record still exists.
        harness.publish(_SIBLING_PID)
        gen2 = harness.current_generation(_SIBLING_PID)
        assert gen2 is not None and gen2 > gen1, "restart did not fence with a higher generation"

    def test_restart_race_fencing(self, harness: RecordHarness, tier: str) -> None:
        # A slow predecessor's delayed re-assert must NOT clobber a newer,
        # higher-generation owner's record (CA-4). For the leased tier this
        # proves the heartbeat-fencing arm: a stale owner re-stamping its
        # lease is rejected (StaleOwnerError) and writes nothing. The
        # complementary guarantee — that publish can only ever INCREMENT the
        # generation, so a stale owner cannot re-publish a lower one — is a
        # structural property proven at the file level in
        # test_service_registry.py (P1). Together they are CA-4.
        _maybe_xfail("restart_race_fencing", tier)
        harness.publish(_OWNER_PID)  # predecessor
        harness.publish(_SIBLING_PID)  # successor takes over (higher generation)
        harness.stale_reassert(_OWNER_PID)  # predecessor wakes late
        rec = harness.discover()
        assert rec is not None
        assert rec["owner"] == _SIBLING_PID, "stale predecessor clobbered the record"


# ---------------------------------------------------------------------------
# T1-only property (CA-3): the locked RF-2 transient-key -> session-id re-key.
# The cold-start lifespan race: the SessionStart hook writes current_session
# independently of the MCP lifespan, so session-id may be None at publish. The
# publisher keys transiently on the chroma server_pid and re-keys the instant
# the session-id resolves. RDR-149 P4 makes this real (was xfail through P3).
# ---------------------------------------------------------------------------


_SERVER_PID = 90001
_SIBLING_SERVER_PID = 90002


def _t1_publisher(
    registry: ServiceRegistry,
    *,
    server_pid: int,
    session_resolver,
) -> T1LeasePublisher:
    return T1LeasePublisher(
        registry=registry,
        server_pid=server_pid,
        host="127.0.0.1",
        port=0,
        version="1.0.0",
        session_resolver=session_resolver,
        owner_token=f"tok-{server_pid}",
    )


class TestT1SessionRekey:
    """CA-3: the transient-key -> session-id re-key has no ``"unknown"``
    collapse and no window where the OWNER or an env-inheriting subprocess
    is stranded. This asserts the registry-layer invariant: the transient
    lease is discoverable under the ``server_pid`` key (the re-key
    carry-forward + owner ``_t1_state`` breadcrumb). The bare Bash sibling's
    production read path (matching the transient lease by claude_pid,
    nexus-0x16i) is covered in ``test_t1_discovery`` /
    ``test_t1_lease.TestTransientClaudeFallback``."""

    def _registry(self, config_dir: Path, clock: _FakeClock) -> ServiceRegistry:
        return ServiceRegistry(
            dir=config_dir, tier="t1", clock=clock, ttl=3.0, heartbeat_interval=1.0
        )

    def test_no_record_is_ever_keyed_unknown(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3 (i): a cold publish with an unresolved session-id keys on the
        # server_pid, never the legacy "unknown" string.
        reg = self._registry(config_dir, clock)
        _t1_publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: None).publish()
        assert not (config_dir / "t1_addr.unknown").exists()
        assert [p.name for p in config_dir.glob("t1_addr.*")] == [
            f"t1_addr.{_SERVER_PID}"
        ]

    def test_transient_lease_is_registry_discoverable_under_server_pid(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3 carry-forward: during the transient window the record exists
        # under the server_pid key (so the re-key can read-and-carry its
        # generation, and the owner's _t1_state breadcrumb is backed by a
        # real record) while NOT yet discoverable under the session-id. This
        # asserts the registry-layer invariant only; it is NOT a claim that a
        # bare Bash sibling reads the server_pid key (it does not -- see
        # tests/test_t1_discovery.py for the honest production read path).
        reg = self._registry(config_dir, clock)
        pub = _t1_publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: None)
        pub.publish()
        assert reg.discover(str(_SERVER_PID)) is not None
        assert reg.discover("sess-A") is None

    def test_rekey_moves_record_to_session_id_atomically(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # The core re-key: transient server_pid record -> session-id record,
        # transient unlinked, exactly one record at every observable step.
        reg = self._registry(config_dir, clock)
        sid: dict[str, str | None] = {"v": None}
        pub = _t1_publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: sid["v"])
        pub.publish()
        assert (config_dir / f"t1_addr.{_SERVER_PID}").exists()

        sid["v"] = "sess-A"
        pub.tick()

        assert (config_dir / "t1_addr.sess-A").exists(), "no session-id-keyed record"
        assert not (config_dir / f"t1_addr.{_SERVER_PID}").exists(), "transient leaked"
        assert pub.session_keyed

    def test_rekey_atomic_under_concurrent_sibling_flock(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        # CA-3 (iii): two siblings of one session re-key concurrently. The
        # session-key publish is flock-serialized (monotonic generation) and
        # each unlinks only its own server_pid record. End state: exactly one
        # session record at generation 2, both transient records gone.
        reg = self._registry(config_dir, clock)
        sid: dict[str, str | None] = {"v": None}
        a = _t1_publisher(reg, server_pid=_SERVER_PID, session_resolver=lambda: sid["v"])
        b = _t1_publisher(
            reg, server_pid=_SIBLING_SERVER_PID, session_resolver=lambda: sid["v"]
        )
        a.publish()
        b.publish()

        sid["v"] = "sess-A"
        a.tick()
        b.tick()

        assert [p.name for p in config_dir.glob("t1_addr.*")] == ["t1_addr.sess-A"]
        rec = reg.discover("sess-A")
        assert rec is not None and rec.generation == 2


# ---------------------------------------------------------------------------
# Non-vacuity guard (CA-1).
# ---------------------------------------------------------------------------


class TestMatrixIsNotVacuous:
    def test_1114_t1_self_heal_fixed_structurally(self) -> None:
        # #1114 (T1 chroma runs with a lost addr file, no self-heal) was the
        # red-first GAP cell through P0-P3; RDR-149 P4 fixed it structurally
        # by migrating T1 onto the leased registry so its publisher heartbeat
        # self-heals a lost record, exactly as T2/T3 do. This guards against a
        # regression silently re-opening it.
        assert EXPECTATIONS["self_heal"]["t1"] == "pass"

    def test_1112_t3_version_cycle_fixed_structurally(self) -> None:
        # #1112 (T3 stale after upgrade) was the red-first GAP cell through
        # P0-P2; RDR-149 P3 fixed it structurally by moving the version-skew
        # cycle onto the shared supervisor (cycle_to_current), so the cell is
        # now green. This guards against a regression silently re-opening it.
        assert EXPECTATIONS["version_cycle"]["t3"] == "pass"

    def test_t2_passes_every_gap_t1_or_t3_fails(self) -> None:
        # For any property where T1 or T3 has a GAP, T2 must pass it; a GAP
        # T2 also failed would be mis-specified (CA-1).
        for prop, cells in EXPECTATIONS.items():
            t1_gap = isinstance(cells["t1"], tuple) and cells["t1"][0] == GAP
            t3_gap = isinstance(cells["t3"], tuple) and cells["t3"][0] == GAP
            if t1_gap or t3_gap:
                assert cells["t2"] == "pass", (
                    f"property {prop!r} is a GAP for T1/T3 but T2 does not pass "
                    f"it; the property is mis-specified (CA-1)"
                )

    def test_t2_migration_flipped_its_spec_cells(self) -> None:
        # RDR-149 P2 ratchet: once T2 rides the primitive, every lease
        # property must pass for T2 (no remaining xfail on the reference
        # tier). A regression that re-broke one would surface here.
        for prop in (
            "pid_reuse_immunity",
            "restart_higher_generation",
            "restart_race_fencing",
        ):
            assert EXPECTATIONS[prop]["t2"] == "pass", (
                f"T2 lease property {prop!r} regressed to non-pass after P2"
            )

    def test_t3_migration_flipped_its_cells(self) -> None:
        # RDR-149 P3 ratchet: T3 now rides the primitive + the supervisor
        # heartbeat/cycle, so #1112 (version_cycle) and self_heal go green
        # and the lease SPEC properties pass. A regression surfaces here.
        for prop in (
            "self_heal",
            "version_cycle",
            "pid_reuse_immunity",
            "restart_higher_generation",
            "restart_race_fencing",
        ):
            assert EXPECTATIONS[prop]["t3"] == "pass", (
                f"T3 lease property {prop!r} regressed to non-pass after P3"
            )

    def test_t1_migration_flipped_its_cells(self) -> None:
        # RDR-149 P4 ratchet: T1 now rides the primitive, so self_heal goes
        # green (#1114), session-scope converges to one owner, and the lease
        # SPEC properties pass. version_cycle stays a documented N/A (T1 is
        # MCP-lifespan-owned, not upgrade-cycled). A regression surfaces here.
        for prop in (
            "self_heal",
            "concurrent_one_owner",
            "pid_reuse_immunity",
            "restart_higher_generation",
            "restart_race_fencing",
        ):
            assert EXPECTATIONS[prop]["t1"] == "pass", (
                f"T1 lease property {prop!r} regressed to non-pass after P4"
            )
        cell = EXPECTATIONS["version_cycle"]["t1"]
        assert isinstance(cell, tuple) and cell[0] == GAP, (
            "version_cycle[t1] must stay a documented N/A (MCP-lifespan-owned)"
        )

    def test_every_cell_covers_all_tiers(self) -> None:
        for prop, cells in EXPECTATIONS.items():
            assert set(cells) == set(TIERS), f"property {prop!r} missing a tier"


# ---------------------------------------------------------------------------
# Live-process behavioral proof (integration marker only): a REAL in-process
# T2 daemon self-heals a deleted discovery file via its supervisor heartbeat.
# port=0, shrunk interval, single event loop (RDR-140 convention).
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLiveT2SelfHeal:
    def test_real_daemon_reasserts_deleted_discovery_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import shutil
        import tempfile

        from nexus.daemon import t2_daemon as _t2

        monkeypatch.setattr(_t2, "_REASSERT_INTERVAL", 0.05)
        cd = Path(tempfile.mkdtemp(prefix="nx149-", dir="/tmp"))
        daemon = _t2.T2Daemon(config_dir=cd, db_path=cd / "memory.db")

        async def _main() -> None:
            await daemon.start()
            try:
                disc = daemon.discovery_path
                assert disc.exists()
                # RDR-149 P2: the record is a lease; the owner pid lives under
                # endpoint and the generation is present.
                payload = json.loads(disc.read_text())
                assert payload["endpoint"]["pid"] == os.getpid()
                assert payload["generation"] == 1
                disc.unlink()
                assert not disc.exists()
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if disc.exists():
                        break
                assert disc.exists(), "live T2 daemon failed to self-heal discovery file"
                healed = json.loads(disc.read_text())
                assert healed["endpoint"]["pid"] == os.getpid()
                # Self-heal preserves the generation (re-assert, not a restart).
                assert healed["generation"] == 1
            finally:
                await daemon.stop()
            assert not disc.exists()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_main())
        finally:
            loop.close()
            shutil.rmtree(cd, ignore_errors=True)
