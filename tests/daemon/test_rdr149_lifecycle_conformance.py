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
    ttl_for_tier,
)
from nexus import session as _sess


# "t2" removed (nexus-i711w Stage 2 sub-stage B): the T2 daemon is retired, so
# its row proved lease semantics for a tier nothing publishes. This is a
# DELIBERATE reduction of a cross-tier battery, not bookkeeping.
#
# "t3" removed the same way (nexus-pmag3, 2026-08-07). DETERMINATION: PHANTOM
# TIER, not deliberate generic-harness coverage — reading (a), not (b). The
# Chroma-serving T3 daemon that published ``ServiceRegistry(tier="t3", ...)``
# (verified in git history: ``daemon/t3_daemon.py`` before its RDR-155 P4b
# deletion) is gone; ``grep -rn 'tier="t3"' src/`` returns zero production
# hits, and ``TIER_TTLS`` (``service_registry.py``) carries no "t3" override —
# the tier falls back to ``DEFAULT_TTL`` for want of anyone real to tune it
# for. T3RecordHarness was proving lease semantics for a tier nothing
# publishes, identically to t2's case above, not exercising the primitive
# under a name a future tier might reuse — the primitive's generic behavior
# is already proven by storage_service and aspect_worker, both real.
#
# "t1" removed the same way (nexus-8zfwv, 2026-08-07). PHANTOM TIER, same
# determination as t2/t3: T1LeasePublisher (the ``ServiceRegistry(tier="t1",
# ...)`` publisher T1RecordHarness drove) was deleted at ff744321 — nothing
# in production publishes the ``t1_addr.*`` lease format this primitive
# instance served any more. T1's live cross-process "session has a live T1
# scope" signal moved to a different mechanism entirely: the lease file
# ``nexus.db.t1.publish_t1_session_lease`` writes at
# ``t1_session_lease.<session_id>`` (JSON ``{token, expires_at}``, no
# ServiceRegistry involved). That mechanism is conformance-tested in
# ``tests/db/test_t1_cli_dedicated_session.py``, not here — it never rode
# this primitive, so it has no cell to occupy. The generic
# publish/discover/reap/fence properties this suite proves remain covered by
# storage_service and aspect_worker, both real.
TIERS = ("storage_service", "aspect_worker")

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
    scope: str  # "uid" (one per user) | "session" (one per session-id) | "tenant" (one per tenant)
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
        # nexus-lz3f2: each tier rides its REAL per-tier TTL (storage_service=15s,
        # t2/t3=3s default) so the conformance battery's reap/self-heal timing
        # reflects production for every tier, not a hardcoded 3s.
        self._tier_ttl = ttl_for_tier(self._REGISTRY_TIER)
        self._registry = ServiceRegistry(
            dir=config_dir, tier=self._REGISTRY_TIER, clock=clock,
            ttl=self._tier_ttl, heartbeat_interval=1.0,
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
        self._clock.advance(self._tier_ttl + 0.1)  # past this tier's TTL

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


# NO T3RecordHarness: RDR-149 P3 migrated the T3-daemon lease onto this same
# primitive, but the T3 daemon itself retired at RDR-155 P4b (nexus-pmag3,
# 2026-08-07) — see TIERS' comment above for the phantom-tier determination.
#
# NO T1RecordHarness: RDR-149 P4 migrated T1 onto this same primitive via
# T1LeasePublisher; nexus-yfh5x then refactored the harness onto the shared
# _LeaseHarness after that publisher retired as dead production code. Both
# were an intermediate state -- T1 has since retired from this primitive
# ENTIRELY (nexus-8zfwv, 2026-08-07, see TIERS' comment above): its live
# lease mechanism (nexus.db.t1.publish_t1_session_lease /
# t1_session_lease.<session_id>) never touches ServiceRegistry, so there is
# no publish/discover/reap path left for a harness to drive here.

class StorageServiceRecordHarness(_LeaseHarness):
    """RDR-149 P5.1 (nexus-gmiaf.30): storage_service rides the leased registry,
    supervised by StorageServiceSupervisor. Identical lease semantics to the
    retired T3-daemon tier (uid-scoped, uid-scoped external-process supervisor,
    version-cycled by _cycle_storage_service_to_current). The scope key is
    str(os.getuid()); the registry tier prefix is "storage_service"; addr file
    = storage_service_addr.<uid>.
    """

    tier = "storage_service"
    _REGISTRY_TIER = "storage_service"


class AspectWorkerRecordHarness(_LeaseHarness):
    """RDR-173 P1 (nexus-plzhp): the aspect-worker rides the SAME leased registry
    as storage_service (and, historically, the now-retired T2/T3 daemons) —
    one more leased tier, not a bespoke daemon. Real scope is per-tenant
    (per-host would need BYPASSRLS, forbidden by RDR-152); the lifecycle
    mechanics exercised here (reap / fence / self-heal) are key-agnostic, so
    the harness reuses the uid scope key — the per-tenant keying itself is
    pinned by tests/daemon/test_aspect_worker_daemon.py. Unlike
    storage_service there is no in-process version-cycle: an upgrade
    re-spawns the daemon from the (upgraded) enqueue hook and the new
    generation fences the old, so version_cycle is a documented N/A (like
    T1), not a wired cycle_to_current."""

    tier = "aspect_worker"
    scope = "tenant"
    _REGISTRY_TIER = "aspect_worker"
    has_version_cycle = False


_HARNESS_CLASSES: dict[str, type[RecordHarness]] = {
    "storage_service": StorageServiceRecordHarness,
    "aspect_worker": AspectWorkerRecordHarness,
}


# ---------------------------------------------------------------------------
# The expectation matrix: the single source of truth for CA-1.
# ---------------------------------------------------------------------------

GAP = "gap"
SPEC = "spec"

EXPECTATIONS: dict[str, dict[str, Any]] = {
    "roundtrip": {"storage_service": "pass", "aspect_worker": "pass"},
    "reap_ungraceful": {"storage_service": "pass", "aspect_worker": "pass"},
    "self_heal": {
        "storage_service": "pass",  # RDR-149 P5.1: supervisor heartbeat self-heals
        "aspect_worker": "pass",  # RDR-173 P1: rides the same supervisor heartbeat
    },
    "concurrent_one_owner": {
        "storage_service": "pass",  # uid-scoped, same lease fencing as the retired T2/T3 daemons
        "aspect_worker": "pass",  # per-tenant scope converges to one owner (RDR-173 P1)
    },
    "version_cycle": {
        "storage_service": "pass",  # RDR-149 P5.1: _cycle_storage_service_to_current
        # aspect-worker is spawn-if-absent from the enqueue hook; an upgrade
        # re-spawns + generation-fences the predecessor rather than driving an
        # in-process cycle_to_current (RDR-173 P1/P2) — documented N/A, like the
        # retired T1 harness row used to be.
        "aspect_worker": (GAP, "aspect-worker upgrade = re-spawn + fence, not in-process cycle; RDR-173"),
    },
    # RDR-149 P5.1 + RDR-173 P1: both LIVE tiers ride the primitive, so their
    # lease properties pass. (T1/T2/T3 rode it too, historically; all three
    # are retired — nexus-8zfwv / nexus-i711w / nexus-pmag3 — and their
    # columns removed with them, not left as phantom rows.)
    "pid_reuse_immunity": {
        "storage_service": "pass",  # RDR-149 P5.1: lease/generation kills pid-reuse
        "aspect_worker": "pass",  # RDR-173 P1: lease/generation kills pid-reuse
    },
    "restart_higher_generation": {
        "storage_service": "pass",  # RDR-149 P5.1: generation fencing token
        "aspect_worker": "pass",  # RDR-173 P1: generation fencing token
    },
    "restart_race_fencing": {
        "storage_service": "pass",  # RDR-149 P5.1: CA-4 heartbeat-fencing arm
        "aspect_worker": "pass",  # RDR-173 P1: CA-4 heartbeat-fencing arm
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
    # Only ONE seam now. There used to be a second
    # (`nexus.daemon.discovery.os.kill`) because discovery.py ran its own
    # os.kill liveness probes for the T2 tier; that module was deleted with the
    # daemon (nexus-i711w Stage 2 sub-stage B). ServiceRegistry does NOT call
    # os.kill directly — it goes through `nexus.session._is_pid_alive` — so
    # re-pointing the old patch at the primitive would have been decorative
    # (verified by deleting it: the suite stays green either way, which is the
    # signal that it was doing nothing).
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


# NO "T1-only property (CA-3)" section: the locked RF-2 transient-key ->
# session-id re-key (TestT1SessionRekey, T1LeasePublisher, _t1_publisher)
# tested T1LeasePublisher directly. That was ``T1LeasePublisher``-only
# behavior -- no production caller ever exercised the re-key path (zero
# production construction sites) -- and was first retired down to a
# dangling cross-reference at nexus-yfh5x (T1RecordHarness refactored onto
# the shared _LeaseHarness, which never re-keys a scope in-place). T1 has
# since retired from this primitive ENTIRELY (nexus-8zfwv, 2026-08-07,
# T1LeasePublisher deleted at ff744321) -- T1 no longer rides
# ServiceRegistry at all, so there is no re-key protocol left to prove
# here, and no bare-Bash-sibling claude_pid-ancestor-matching test surface
# either (``discover_t1_by_claude_ancestor`` retired with it -- see
# daemon/t1_lease.py's deletion). T1's current lease mechanism
# (nexus.db.t1.publish_t1_session_lease, a flat {token, expires_at} JSON
# file with no transient-key re-key step and no claude-ancestor fallback)
# is conformance-tested in tests/db/test_t1_cli_dedicated_session.py.


# ---------------------------------------------------------------------------
# Non-vacuity guard (CA-1).
# ---------------------------------------------------------------------------


class TestMatrixIsNotVacuous:
    # NO test_1114_t1_self_heal_fixed_structurally: #1114 (T1 chroma runs
    # with a lost addr file, no self-heal) was the red-first GAP cell
    # through P0-P3; RDR-149 P4 fixed it structurally by migrating T1 onto
    # the leased registry. T1 has since moved OFF this primitive entirely
    # (nexus-8zfwv, 2026-08-07 — T1LeasePublisher deleted at ff744321): its
    # live cross-process signal is now nexus.db.t1's t1_session_lease.*
    # file, conformance-tested in tests/db/test_t1_cli_dedicated_session.py
    # instead. There is no "t1" cell left in EXPECTATIONS to guard.

    # NO test_1112_t3_version_cycle_fixed_structurally: #1112 (T3 stale after
    # upgrade) was the red-first GAP cell through P0-P2; RDR-149 P3 fixed it
    # structurally by moving the version-skew cycle onto the shared
    # supervisor (cycle_to_current). The T3 daemon itself retired at
    # RDR-155 P4b (nexus-pmag3, 2026-08-07 — phantom-tier determination,
    # see TIERS' comment above), so there is no "t3" cell left to guard.
    # test_storage_service_version_cycle_is_behaviorally_proven exercises the
    # SAME cycle_to_current mechanic the #1112 fix generalised, on the tier
    # that is actually live.

    def test_reference_tier_passes_every_gap_another_tier_fails(self) -> None:
        # CA-1: a property that is a GAP everywhere would be mis-specified, so
        # at least the REFERENCE tier must pass it. T1/T2/T3 were that
        # reference in turn until all three retired (nexus-8zfwv / nexus-i711w
        # Stage 2 sub-stage B / nexus-pmag3); storage_service takes over,
        # being the one supervised tier that is actually live. Generalized
        # over every non-reference tier (not hardcoded to one retired tier
        # name) so this keeps working as the tier roster changes.
        reference = "storage_service"
        for prop, cells in EXPECTATIONS.items():
            for tier_name, cell in cells.items():
                if tier_name == reference:
                    continue
                is_gap = isinstance(cell, tuple) and cell[0] == GAP
                if is_gap:
                    assert cells[reference] == "pass", (
                        f"property {prop!r} is a GAP for {tier_name!r} but "
                        f"{reference} does not pass it; the property is "
                        f"mis-specified (CA-1)"
                    )

    # NO test_t2_migration_flipped_its_spec_cells / test_t3_migration_flipped_
    # its_cells: the RDR-149 P2/P3 ratchets each asserted every lease property
    # stayed "pass" for T2/T3 once they rode the primitive. Both tiers are
    # retired (nexus-i711w Stage 2 sub-stage B; nexus-pmag3), so there is no
    # cell left to ratchet for either. The storage_service and aspect_worker
    # ratchets below carry the same guarantee for the tiers that are actually
    # live.

    # NO test_t1_migration_flipped_its_cells: the RDR-149 P4 ratchet asserted
    # every T1 lease property stayed "pass" once it rode the primitive. T1
    # has since retired from this primitive altogether (nexus-8zfwv,
    # 2026-08-07 — see TIERS' comment above), so there is no cell left to
    # ratchet.

    def test_storage_service_migration_flipped_its_cells(self) -> None:
        # RDR-149 P5.1 ratchet: storage_service now rides the primitive
        # (StorageServiceSupervisor + _cycle_storage_service_to_current), so
        # all lease properties pass. A regression surfaces here.
        for prop in (
            "roundtrip",
            "reap_ungraceful",
            "self_heal",
            "concurrent_one_owner",
            "version_cycle",
            "pid_reuse_immunity",
            "restart_higher_generation",
            "restart_race_fencing",
        ):
            assert EXPECTATIONS[prop]["storage_service"] == "pass", (
                f"storage_service lease property {prop!r} regressed to non-pass after P5.1"
            )

    def test_storage_service_version_cycle_is_behaviorally_proven(self) -> None:
        # version_cycle[storage_service] == "pass" above is a claim; this proves
        # it. The shared ``test_version_cycle`` only asserts a class attribute,
        # so the battery must exercise the real upgrade entrypoint
        # (_cycle_storage_service_to_current) to show a running service is
        # actually stopped-then-started (round-3 SIG: vacuity guard). Wrong verb
        # order or a no-op would fail here.
        from nexus.commands.upgrade import _cycle_storage_service_to_current

        calls: list[str] = []

        def _run(argv, **_kwargs):
            # argv == [*nx, "daemon", "service", <verb>]
            calls.append(argv[-1])

        # A live lease is present -> the cycle must fire stop then start.
        _cycle_storage_service_to_current(
            _discover_fn=lambda: object(),
            _run_fn=_run,
            _nx_bin_fn=lambda: ["nx"],
        )
        assert calls == ["stop", "start"], (
            "a running storage service must be stopped BEFORE start during an "
            f"upgrade cycle; got {calls}"
        )

        # No live lease -> no subprocess calls (no auto-spawn during upgrade).
        calls.clear()
        _cycle_storage_service_to_current(
            _discover_fn=lambda: None,
            _run_fn=_run,
            _nx_bin_fn=lambda: ["nx"],
        )
        assert calls == [], "upgrade cycle must not spawn a service that was not running"

        # nexus-f0pmd (RDR-183 candidate 0): a lease already AT the installed
        # version must be left alone — the ungated cycle ran on every
        # SessionStart hook firing and churned a current supervisor.
        class _CurrentLease:
            version = "9.9.9"

        calls.clear()
        _cycle_storage_service_to_current(
            _discover_fn=lambda: _CurrentLease(),
            _run_fn=_run,
            _nx_bin_fn=lambda: ["nx"],
            _installed_version_fn=lambda: "9.9.9",
        )
        assert calls == [], (
            "a supervisor already on the installed version must not be cycled "
            f"at upgrade/SessionStart; got {calls}"
        )

    def test_every_cell_covers_all_tiers(self) -> None:
        for prop, cells in EXPECTATIONS.items():
            assert set(cells) == set(TIERS), f"property {prop!r} missing a tier"


# NO TestLiveT2SelfHeal: the integration-marked live-process proof drove a REAL
# in-process T2Daemon and asserted it reasserted a deleted discovery file via
# its supervisor heartbeat. Both the daemon and the discovery module it wrote
# to are deleted (nexus-i711w Stage 2 sub-stage B). The self_heal PROPERTY is
# still covered for every surviving tier by the EXPECTATIONS matrix above; what
# is genuinely lost is the only live-process (rather than harness-injected)
# proof of it, and no surviving tier has an equivalent in-process daemon to
# stand in — storage_service is an external Java process.


class TestDiscoverReapToctou:
    """RDR-149 regression (nexus-2mpns): discover()'s reap of an expired lease
    must not delete a concurrently-published higher-generation live record.

    Conformance-suite home per the CLAUDE.md daemon mandate (lifecycle fixes
    land in the primitive AND this suite). The race: discover() reads a stale
    record (unlocked), judges it stale, then a successor publishes a fresh
    higher-generation record under the election flock; the OLD blind-unlink
    would delete the successor's live record by path.
    """

    def _registry(self, config_dir: Path, clock: _FakeClock) -> ServiceRegistry:
        # Tier string is incidental here — this exercises the PRIMITIVE's
        # reap/publish race, not a tier's behaviour. Moved off the retired "t2"
        # (nexus-i711w Stage 2 sub-stage B) to a live tier so the fixture names
        # something that still exists.
        return ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock, ttl=3.0,
            heartbeat_interval=1.0,
        )

    def test_reap_cannot_delete_concurrently_published_successor(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        reg = self._registry(config_dir, clock)
        endpoint = {"host": "127.0.0.1", "port": 5000}
        stale = reg.publish("scope", endpoint=endpoint, version="1", owner_token="A")
        clock.advance(10.0)  # A is now stale (ttl=3.0)

        # Successor B publishes a fresh, higher-generation record (the race winner).
        fresh = reg.publish("scope", endpoint={"host": "127.0.0.1", "port": 6000},
                            version="1", owner_token="B")
        assert fresh.generation > stale.generation

        # The reap, fired with the captured stale record, must re-read under the
        # flock and leave the successor's live record intact.
        reg._reap_if_still_stale(stale)

        survived = reg.discover("scope")
        assert survived is not None, "successor's higher-generation record was reaped"
        assert survived.owner_token == "B"
        assert survived.generation == fresh.generation


# NO "Fix #3: T1 lifecycle GC" section (TestSweepDeadT1Holders,
# TestSweepDeadT1ElectLocks): sweep_dead_t1_holders and
# sweep_dead_t1_elect_locks (service_registry.py) were the startup-sweep
# half of the nexus-ycwec T1 lifecycle GC, scanning t1_addr.*/t1_elect.*.lock
# files nothing publishes any more (T1LeasePublisher retired, nexus-8zfwv,
# 2026-08-07, deleted at ff744321). Both functions had ZERO production
# callers even before this retirement -- doubly dead, not merely orphaned by
# the publisher's removal -- and are deleted alongside it. T1's current
# lease file (nexus.db.t1's t1_session_lease.*) has its own orphan-reap
# check (nexus.health._check_orphan_t1_lease), tested in
# tests/test_doctor_integrity.py, not here.


# ---------------------------------------------------------------------------
# GAP C: per-session elect-lock cleanup on graceful relinquish.
# ---------------------------------------------------------------------------


class TestRelinquishCleansElectLock:
    """GAP C fix: ServiceRegistry.relinquish must remove the elect lock
    for the scope being released, so graceful shutdown leaves no
    <tier>_elect.*.lock orphan.

    Tier string is incidental here (same reasoning as
    TestDiscoverReapToctou above): this exercises the PRIMITIVE's
    relinquish/elect-lock behaviour, not a specific tier's lifecycle. Moved
    off "t1" (nexus-8zfwv, 2026-08-07) since T1LeasePublisher — the only
    production caller that ever published ``ServiceRegistry(tier="t1",
    ...)`` records — is retired; "storage_service" names a tier that still
    exists.

    Safety: the lock is only unlinked for the scope WE own — the identity
    check (``current.owner_token != record.owner_token``) runs first.  If
    a successor has already taken the scope, we leave the lock alone (same
    as the addr-file non-deletion).
    """

    def _registry(self, config_dir: Path, clock: _FakeClock) -> "ServiceRegistry":
        return ServiceRegistry(
            dir=config_dir, tier="storage_service", clock=clock, ttl=300.0,
            heartbeat_interval=1.0,
        )

    def test_relinquish_removes_elect_lock(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """After a graceful relinquish the elect lock file must be gone."""
        reg = self._registry(config_dir, clock)
        record = reg.publish(
            "my-session",
            endpoint={"host": "127.0.0.1", "port": 9999},
            version="1.0",
            owner_token="tok-A",
        )
        lock = config_dir / "storage_service_elect.my-session.lock"
        assert lock.exists(), "publish must create the elect lock"

        reg.relinquish(record)

        assert not (config_dir / "storage_service_addr.my-session").exists(), "addr file must be unlinked"
        assert not lock.exists(), "elect lock must be removed by relinquish (GAP C)"

    def test_relinquish_does_not_remove_successor_elect_lock(
        self, config_dir: Path, clock: _FakeClock
    ) -> None:
        """If a successor has already taken over, relinquish must NOT remove
        the elect lock (successor owns it now)."""
        reg = self._registry(config_dir, clock)
        stale = reg.publish(
            "shared-scope",
            endpoint={"host": "127.0.0.1", "port": 9998},
            version="1.0",
            owner_token="tok-stale",
        )
        # Successor publishes (higher generation, new token).
        _fresh = reg.publish(
            "shared-scope",
            endpoint={"host": "127.0.0.1", "port": 9997},
            version="1.0",
            owner_token="tok-fresh",
        )
        lock = config_dir / "storage_service_elect.shared-scope.lock"
        assert lock.exists()

        # Stale owner tries to relinquish — must leave successor's lock intact.
        reg.relinquish(stale)

        assert lock.exists(), (
            "relinquish of a STALE record must not remove the successor's elect lock"
        )
        # Successor addr file should also still be present.
        assert (config_dir / "storage_service_addr.shared-scope").exists()
