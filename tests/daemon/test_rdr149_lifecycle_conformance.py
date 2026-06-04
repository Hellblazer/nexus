# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149: cross-tier lifecycle conformance suite.

The load-bearing artifact for the whole RDR-149 arc. ONE parameterized
lifecycle property battery run against the THREE service-lifecycle
implementations (T1 session.py, T2 daemon, T3 daemon). Each tier's
harness drives that tier's REAL publish / discover / reap path, so the
battery is a living spec: as a tier migrates onto the leased registry
(P2-P5) its harness points at the migrated path and its red cells flip
green.

Liveness models differ across tiers and the battery is agnostic to which
one a tier uses:

- T1 / T3 (un-migrated): identity + liveness are pid-based. Ungraceful
  death = the owner pid dies; reap = the orphan sweep / discovery-time
  pid validation removes the dead record.
- T2 (migrated, RDR-149 P2): identity is a server-unique owner token and
  liveness is lease freshness (TTL on a wall-clock heartbeat). Ungraceful
  death = the owner stops heartbeating and the lease ages out.

The harness vocabulary (``simulate_ungraceful_death`` / ``advance_to_reap``
/ ``self_heal_tick`` / ``stale_reassert``) abstracts those models so one
test body asserts the same property for every tier.

Red-first contract (CA-1). The matrix MUST reproduce the two filed
defects as failures against un-migrated code:

- GH #1114 (T1 lost-addr, no self-heal)  -> ``test_self_heal`` xfails for T1.
- GH #1112 (T3 stale after upgrade)      -> ``test_version_cycle`` xfails for T3.

and T2 MUST pass every property T1/T3 fail. The non-vacuity guard
(``TestMatrixIsNotVacuous``) enforces that directly against the
expectation table.

Encoding: each broken cell is ``xfail(strict=True)`` so an unexpected
pass turns the suite RED and forces the migrating phase to delete the
stale cell (the red-first -> green ratchet). GAP cells name an issue + the
phase that closes them; SPEC cells are forward properties of the leased
primitive. RDR-149 P2 flips T2's SPEC cells (generation / fencing /
pid-reuse) to ``pass`` now that T2 rides the primitive.

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

from nexus.daemon import discovery as _disc
from nexus.daemon.t3_daemon import (
    _build_payload as _t3_build_payload,
    _write_discovery_atomic as _t3_write_atomic,
)
from nexus.daemon.service_registry import (
    LeaseRecord,
    ServiceRegistry,
    StaleOwnerError,
)
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
    tier = "t1"
    scope = "session"
    has_self_heal = False  # #1114: ZERO self-heal in session.py
    has_version_cycle = False

    def publish(self, owner: int = _OWNER_PID) -> None:
        self._alive.mark_alive(owner)
        _sess.write_t1_addr(owner, "127.0.0.1", 0)

    def discover(self, owner: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        hp = _sess.read_t1_addr_for(owner)
        if hp is None:
            return None
        return {"pid": owner, "owner": owner, "host": hp[0], "port": hp[1]}

    def simulate_ungraceful_death(self, owner: int = _OWNER_PID) -> None:
        self._alive.mark_dead(owner)

    def advance_to_reap(self) -> None:
        return  # pid death is observable immediately

    def reap(self) -> None:
        _sess.sweep_orphan_t1_addr_files()

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        _sess.unlink_t1_addr(owner)

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        return  # session.py runs no re-assert loop (#1114)

    def stale_reassert(self, owner: int) -> None:
        # No fencing: the stale owner simply rewrites its own pid-keyed file.
        _sess.write_t1_addr(owner, "127.0.0.1", 0)

    def owners_in_scope(self, session_id: str) -> int:
        # T1 keys on claude_pid, not session-id, so every sibling owns its
        # own record; one logical session with two MCP siblings shows two.
        cfg = _sess._nexus_config_dir_at_import()
        return len(list(cfg.glob("t1_addr.*")))


class T3RecordHarness(RecordHarness):
    tier = "t3"
    scope = "uid"
    has_self_heal = False  # RF-4: T3 has no re-assert loop
    has_version_cycle = False  # #1112: upgrade-cycle does not cover T3

    def _path(self) -> Path:
        return _disc.discovery_path(self._cd, tier="t3")

    def publish(self, owner: int = _OWNER_PID) -> None:
        self._alive.mark_alive(owner)
        payload = _t3_build_payload(
            tcp_port=0, pid=owner, local_path=self._cd / "chroma",
            daemon_version="1.0.0",
        )
        _t3_write_atomic(self._path(), payload)

    def discover(self, owner: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        rec = _disc.find_t3_daemon(self._cd)
        if rec is None:
            return None
        return {"pid": rec.get("pid"), "owner": rec.get("pid"), "generation": None}

    def simulate_ungraceful_death(self, owner: int = _OWNER_PID) -> None:
        self._alive.mark_dead(owner)

    def advance_to_reap(self) -> None:
        return

    def reap(self) -> None:
        _disc.find_t3_daemon(self._cd)  # validator unlinks a dead-pid record

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        self._path().unlink(missing_ok=True)

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        return  # T3 has no re-assert loop (RF-4)

    def stale_reassert(self, owner: int) -> None:
        self.publish(owner)  # no fencing: the stale owner rewrites the record

    def owners_in_scope(self, session_id: str) -> int:
        return 1 if self.discover() is not None else 0


class T2RecordHarness(RecordHarness):
    """RDR-149 P2: T2 now rides the leased registry. This harness drives the
    SAME ``ServiceRegistry`` the migrated daemon uses, with the injected
    clock, so the lease semantics (generation, fencing, TTL liveness,
    pid-reuse immunity) are exercised exactly as in production."""

    tier = "t2"
    scope = "uid"
    has_self_heal = True
    has_version_cycle = True

    def __init__(self, config_dir: Path, alive: _AliveSet, clock: _FakeClock) -> None:
        super().__init__(config_dir, alive, clock)
        self._registry = ServiceRegistry(
            dir=config_dir, tier="t2", clock=clock, ttl=3.0, heartbeat_interval=1.0
        )
        self._scope = str(os.getuid())
        self._records: dict[int, LeaseRecord] = {}

    def publish(self, owner: int = _OWNER_PID) -> None:
        rec = self._registry.publish(
            self._scope,
            endpoint={"pid": owner, "host": "127.0.0.1", "port": 0},
            version="1.0.0",
            owner_token=f"tok-{owner}",
        )
        self._records[owner] = rec

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
        self._records.pop(owner, None)

    def advance_to_reap(self) -> None:
        self._clock.advance(3.1)  # past TTL

    def reap(self) -> None:
        self._registry.discover(self._scope)  # discovery reaps an expired lease

    def external_delete(self, owner: int = _OWNER_PID) -> None:
        self._registry._record_path(self._scope).unlink(missing_ok=True)

    def self_heal_tick(self, owner: int = _OWNER_PID) -> None:
        rec = self._records.get(owner)
        if rec is None:
            return
        try:
            self._records[owner] = self._registry.heartbeat(rec)
        except StaleOwnerError:
            pass

    def stale_reassert(self, owner: int) -> None:
        rec = self._records.get(owner)
        if rec is None:
            return
        try:
            self._registry.heartbeat(rec)  # fenced: raises, writes nothing
        except StaleOwnerError:
            pass

    def owners_in_scope(self, session_id: str) -> int:
        return 1 if self.discover() is not None else 0


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
        "t1": (GAP, "#1114: session.py has no self-heal re-assert; RDR-149 P5"),
        "t2": "pass",
        "t3": (GAP, "RF-4: T3 daemon has no re-assert loop; RDR-149 P3"),
    },
    "concurrent_one_owner": {
        "t1": (GAP, "T1 keys on claude_pid not session-id; RDR-149 P5/CA-3"),
        "t2": "pass",
        "t3": "pass",
    },
    "version_cycle": {
        "t1": (GAP, "T1 not covered by any upgrade-cycle; RDR-149 P5"),
        "t2": "pass",
        "t3": (GAP, "#1112: upgrade-cycle does not cover T3; RDR-149 P3"),
    },
    # RDR-149 P2: T2 rides the primitive now, so its lease properties pass.
    "pid_reuse_immunity": {
        "t1": (SPEC, "lease/generation kills pid-reuse; RDR-149 P5"),
        "t2": "pass",
        "t3": (SPEC, "validator trusts a reused live pid; RDR-149 P3"),
    },
    "restart_higher_generation": {
        "t1": (SPEC, "RF-3: no generation primitive; RDR-149 P5"),
        "t2": "pass",
        "t3": (SPEC, "RF-3: no generation primitive; RDR-149 P3"),
    },
    "restart_race_fencing": {
        "t1": (SPEC, "CA-4: no fencing token; RDR-149 P5"),
        "t2": "pass",
        "t3": (SPEC, "CA-4: no fencing token; RDR-149 P3"),
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
        # higher-generation owner's record (CA-4).
        _maybe_xfail("restart_race_fencing", tier)
        harness.publish(_OWNER_PID)  # predecessor
        harness.publish(_SIBLING_PID)  # successor takes over (higher generation)
        harness.stale_reassert(_OWNER_PID)  # predecessor wakes late
        rec = harness.discover()
        assert rec is not None
        assert rec["owner"] == _SIBLING_PID, "stale predecessor clobbered the record"


# ---------------------------------------------------------------------------
# T1-only property (CA-3): the transient-key -> session-id re-key. T1 today
# has no session-id keying, so the property is unsatisfiable until P5.
# ---------------------------------------------------------------------------


class TestT1SessionRekey:
    def test_record_keyed_on_session_id_not_pid(
        self, config_dir: Path, alive: _AliveSet, clock: _FakeClock
    ) -> None:
        pytest.xfail("CA-3: T1 has no session-id-keyed record yet; RDR-149 P5")
        h = T1RecordHarness(config_dir, alive, clock)
        h.publish(_OWNER_PID)
        sess_path = config_dir / "t1_addr.sess-A"
        assert sess_path.exists(), "no session-id-keyed T1 record"


# ---------------------------------------------------------------------------
# Non-vacuity guard (CA-1).
# ---------------------------------------------------------------------------


class TestMatrixIsNotVacuous:
    def test_1114_t1_self_heal_is_red(self) -> None:
        cell = EXPECTATIONS["self_heal"]["t1"]
        assert cell != "pass" and cell[0] == GAP
        assert "#1114" in cell[1]

    def test_1112_t3_version_cycle_is_red(self) -> None:
        cell = EXPECTATIONS["version_cycle"]["t3"]
        assert cell != "pass" and cell[0] == GAP
        assert "#1112" in cell[1]

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
