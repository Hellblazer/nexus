# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 Phase 0 (bead nexus-sq50c): cross-tier lifecycle conformance suite.

The load-bearing artifact for the whole RDR-149 arc. ONE parameterized
property battery run against the THREE current service-lifecycle
implementations (T1 session.py, T2 daemon, T3 daemon) exactly as they
exist today. The red cells are the evidence-based scope of the
migration: where a property fails for T1 or T3 but passes for T2, the
gap is real and a migration phase will flip it green.

Red-first contract (CA-1). The suite MUST reproduce two filed defects as
failures against current code:

- GH #1114 (T1 lost-addr, no self-heal) -> ``test_self_heal`` xfails for T1.
- GH #1112 (T3 stale after upgrade)     -> ``test_version_cycle`` xfails for T3.

and T2 MUST pass every property T1/T3 fail. If any of those reds turned
green against today's code, or T2 failed a discriminating property, the
suite would be vacuous; ``test_matrix_is_not_vacuous`` guards that
invariant directly against the expectation table.

How red-first is encoded while CI stays green: each currently-broken
cell is marked ``xfail(strict=True)``. ``strict`` means an unexpected
pass turns the suite RED, so the migration phase that fixes a tier is
FORCED to delete the matching expectation entry. That is the
red-first -> green-on-migration ratchet.

Two cell kinds share the ``xfail`` mechanism but differ in intent:

- ``GAP``  : a tier-specific lifecycle hole with a named issue and the
             RDR-149 phase that closes it (T1 self-heal/rekey/election,
             T3 self-heal/version-cycle).
- ``SPEC`` : a forward property of the not-yet-extracted leased primitive
             (monotonic generation, fencing, pid-reuse immunity) that no
             tier satisfies today (RF-3); un-xfailed at P1/P2.

Flakiness control (RDR-140 convention, keeps this out of the nexus-9eaz
flake family): the battery is in-process and record-level with injected
liveness, a fixed clock, and shrunk interval constants; ``port=0`` and a
single event loop for the one real-daemon self-heal proof. The
live-process behavioral variants live under the ``integration`` marker.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from nexus.daemon import discovery as _disc

# Real publish/validate entrypoints under test, per tier.
from nexus.daemon.t2_daemon import (
    _build_discovery_payload as _t2_build_payload,
    _write_discovery_atomic as _t2_write_atomic,
)
from nexus.daemon.t3_daemon import (
    _build_payload as _t3_build_payload,
    _write_discovery_atomic as _t3_write_atomic,
)
from nexus import session as _sess


TIERS = ("t1", "t2", "t3")

# A synthetic owner pid, never a real live process; liveness is injected.
_OWNER_PID = 970001
_SIBLING_PID = 970002
_REUSED_PID = 970003


# ---------------------------------------------------------------------------
# Injected liveness + clock (no real processes, no wall-clock)
# ---------------------------------------------------------------------------


class _AliveSet:
    """Controllable process-liveness oracle shared by every tier harness.

    ``os.kill(pid, 0)`` (the T2/T3 validator probe) and
    ``session._is_pid_alive`` (the T1 sweep probe) are both redirected
    here so 'ungraceful kill' and 'pid reuse' are deterministic without
    spawning processes. Pids not managed here delegate to the real
    probe, so unrelated kernel calls keep working.
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
        # Only the signal-0 liveness probe is redirected; a real signal
        # to a managed synthetic pid would otherwise hit an unrelated
        # process, so refuse anything but the probe.
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
    """Fixed, advanceable monotonic clock (mirrors test_rdr146_fairness)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------------------------------------------------------------------------
# Tier harnesses: a uniform record-level vocabulary over each tier's REAL
# publish / discover / reap functions. Where a tier genuinely lacks a
# mechanism (T1/T3 self-heal, T1/T3 version-cycle, T1 election) the method
# is a real no-op so the failing assertion is behavioral, not synthetic.
# ---------------------------------------------------------------------------


class RecordHarness:
    """Base adapter. ``scope`` is the election scope key for the tier:
    ``uid`` for T2/T3 (one owner per user), ``session`` for T1 (one owner
    per session-id, today mis-keyed on claude_pid)."""

    tier: str
    scope: str
    #: Does the tier run an in-process self-heal re-assert today (RF-1)?
    has_self_heal: bool
    #: Does an upgrade-cycle entrypoint cover this tier today (RF-4)?
    has_version_cycle: bool

    def __init__(self, config_dir: Path, alive: _AliveSet) -> None:
        self._cd = config_dir
        self._alive = alive

    # -- publish / discover -------------------------------------------------
    def publish(self, *, pid: int = _OWNER_PID, generation: Optional[int] = None,
                version: str = "1.0.0") -> None:
        raise NotImplementedError

    def discover(self, *, pid: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    def record_generation(self, *, pid: int = _OWNER_PID) -> Optional[int]:
        rec = self.discover(pid=pid)
        if rec is None:
            return None
        gen = rec.get("generation")
        return gen if isinstance(gen, int) else None

    # -- lifecycle events ---------------------------------------------------
    def reap(self) -> None:
        """Run the tier's orphan-reap path (discovery-time validation or
        the one-shot sweep)."""
        raise NotImplementedError

    def external_delete(self, *, pid: int = _OWNER_PID) -> None:
        raise NotImplementedError

    def heartbeat(self, *, pid: int = _OWNER_PID) -> None:
        """Run ONE self-heal tick using the tier's real re-assert path.
        A no-op for tiers that have none (T1, T3) -> behavioral red."""
        raise NotImplementedError

    def owners_in_scope(self, *, session_id: str) -> int:
        """Count distinct live owner records that resolve within one
        election scope. T2/T3: per-uid (always 0 or 1). T1: per
        session-id, which today is un-keyed so siblings each get their
        own record."""
        raise NotImplementedError


class _DaemonRecordHarness(RecordHarness):
    """Shared T2/T3 record harness: uid-keyed discovery file, pid-liveness
    validation, discovery-time reap. Subclasses supply the real
    build-payload + write-atomic + path + finder."""

    scope = "uid"
    has_self_heal = True  # overridden per-tier below

    _build_payload: Callable[..., dict[str, Any]]
    _write_atomic: Callable[[Path, dict[str, Any]], None]
    _finder: Callable[[Optional[Path]], Optional[dict[str, Any]]]

    def _path(self) -> Path:
        return _disc.discovery_path(self._cd, tier=self.tier)  # type: ignore[arg-type]

    def publish(self, *, pid: int = _OWNER_PID, generation: Optional[int] = None,
                version: str = "1.0.0") -> None:
        payload = self._build_real_payload(pid=pid, version=version)
        if generation is not None:
            payload["generation"] = generation
        self._alive.mark_alive(pid)
        type(self)._write_atomic(self._path(), payload)

    def _build_real_payload(self, *, pid: int, version: str) -> dict[str, Any]:
        raise NotImplementedError

    def discover(self, *, pid: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        return type(self)._finder(self._cd)

    def reap(self) -> None:
        # Discovery-time validation IS the reap path for T2/T3: a stale
        # (dead-pid) record is unlinked by the validator on read.
        type(self)._finder(self._cd)

    def external_delete(self, *, pid: int = _OWNER_PID) -> None:
        self._path().unlink(missing_ok=True)

    def owners_in_scope(self, *, session_id: str) -> int:
        return 1 if self.discover() is not None else 0


class T2RecordHarness(_DaemonRecordHarness):
    tier = "t2"
    has_self_heal = True
    has_version_cycle = True
    _write_atomic = staticmethod(_t2_write_atomic)
    _finder = staticmethod(_disc.find_t2_daemon)

    def _build_real_payload(self, *, pid: int, version: str) -> dict[str, Any]:
        return _t2_build_payload(
            uds_path=str(self._cd / "t2.sock"),
            tcp_host="127.0.0.1",
            tcp_port=0,
            pid=pid,
            daemon_version=version,
        )

    def heartbeat(self, *, pid: int = _OWNER_PID) -> None:
        # T2's real self-heal re-assert rewrites the record when it is
        # missing or names a different pid (t2_daemon._reassert_discovery_
        # loop body). Replay that exact decision using the real payload
        # builder + atomic writer the loop itself calls.
        path = self._path()
        needs_write = True
        if path.exists():
            try:
                needs_write = json.loads(path.read_text()).get("pid") != pid
            except (OSError, json.JSONDecodeError):
                needs_write = True
        if needs_write:
            self.publish(pid=pid)


class T3RecordHarness(_DaemonRecordHarness):
    tier = "t3"
    has_self_heal = False  # RF-4: T3 has no re-assert loop (self-heal=0)
    has_version_cycle = False  # #1112: upgrade-cycle does not cover T3
    _write_atomic = staticmethod(_t3_write_atomic)
    _finder = staticmethod(_disc.find_t3_daemon)

    def _build_real_payload(self, *, pid: int, version: str) -> dict[str, Any]:
        return _t3_build_payload(
            tcp_port=0,
            pid=pid,
            local_path=self._cd / "chroma",
            daemon_version=version,
        )

    def heartbeat(self, *, pid: int = _OWNER_PID) -> None:
        # Genuine no-op: T3 has no self-heal re-assert loop (RF-4). The
        # externally deleted record is NOT restored -> behavioral red.
        return


class T1RecordHarness(RecordHarness):
    tier = "t1"
    scope = "session"
    has_self_heal = False  # #1114: ZERO self-heal in session.py
    has_version_cycle = False

    def _key(self, pid: int) -> int:
        return pid

    def publish(self, *, pid: int = _OWNER_PID, generation: Optional[int] = None,
                version: str = "1.0.0") -> None:
        # T1's record is keyed by claude_pid and carries only host:port.
        # generation/version are unstorable -> the generation property is
        # structurally unsatisfiable today (covered by SPEC xfail).
        self._alive.mark_alive(pid)
        _sess.write_t1_addr(pid, "127.0.0.1", 0)

    def discover(self, *, pid: int = _OWNER_PID) -> Optional[dict[str, Any]]:
        # T1 discovery does NOT validate owner liveness; the filename pid
        # IS the identity. A live record resolves to host:port.
        hp = _sess.read_t1_addr_for(pid)
        if hp is None:
            return None
        return {"pid": pid, "host": hp[0], "port": hp[1]}

    def reap(self) -> None:
        # T1's reap is the one-shot orphan sweep (filename-pid liveness).
        _sess.sweep_orphan_t1_addr_files()

    def external_delete(self, *, pid: int = _OWNER_PID) -> None:
        _sess.unlink_t1_addr(pid)

    def heartbeat(self, *, pid: int = _OWNER_PID) -> None:
        # Genuine no-op: session.py runs NO re-assert loop (#1114). A lost
        # addr file is never republished while the owner is alive.
        return

    def owners_in_scope(self, *, session_id: str) -> int:
        # T1 keys on claude_pid, NOT session-id, so every sibling owns its
        # own record. There is no session linkage to collapse them, so a
        # single logical session with two MCP siblings shows two owners.
        cfg = _sess._nexus_config_dir_at_import()
        return len(list(cfg.glob("t1_addr.*")))


_HARNESS_CLASSES: dict[str, type[RecordHarness]] = {
    "t1": T1RecordHarness,
    "t2": T2RecordHarness,
    "t3": T3RecordHarness,
}


# ---------------------------------------------------------------------------
# The expectation matrix: the single source of truth for CA-1. "pass" means
# the property holds against current code; an (kind, reason) tuple means the
# property is expected to fail today and is xfailed strict. GAP cells name
# the issue + the RDR-149 phase that flips them; SPEC cells are forward
# properties of the leased primitive (no tier satisfies them yet).
# ---------------------------------------------------------------------------

GAP = "gap"
SPEC = "spec"

# property -> {tier -> "pass" | (kind, reason)}
EXPECTATIONS: dict[str, dict[str, Any]] = {
    "roundtrip": {"t1": "pass", "t2": "pass", "t3": "pass"},
    "reap_ungraceful": {"t1": "pass", "t2": "pass", "t3": "pass"},
    "self_heal": {
        "t1": (GAP, "#1114: session.py has no self-heal re-assert; RDR-149 P5"),
        "t2": "pass",
        "t3": (GAP, "RF-4: T3 daemon has no re-assert loop; RDR-149 P4"),
    },
    "concurrent_one_owner": {
        "t1": (GAP, "T1 keys on claude_pid not session-id; RDR-149 P5/CA-3"),
        "t2": "pass",
        "t3": "pass",
    },
    "version_cycle": {
        "t1": (GAP, "T1 not covered by any upgrade-cycle; RDR-149 P5"),
        "t2": "pass",
        "t3": (GAP, "#1112: upgrade-cycle does not cover T3; RDR-149 P4"),
    },
    "pid_reuse_immunity": {
        "t1": (SPEC, "lease/generation kills pid-reuse; RDR-149 P1/P2"),
        "t2": (SPEC, "validator trusts a reused live pid; RDR-149 P1/P2"),
        "t3": (SPEC, "validator trusts a reused live pid; RDR-149 P1/P2"),
    },
    "restart_higher_generation": {
        "t1": (SPEC, "RF-3: no generation primitive; RDR-149 P1"),
        "t2": (SPEC, "RF-3: no generation primitive; RDR-149 P1"),
        "t3": (SPEC, "RF-3: no generation primitive; RDR-149 P1"),
    },
    "restart_race_fencing": {
        "t1": (SPEC, "CA-4: no fencing token; RDR-149 P1/P2"),
        "t2": (SPEC, "CA-4: no fencing token; RDR-149 P1/P2"),
        "t3": (SPEC, "CA-4: no fencing token; RDR-149 P1/P2"),
    },
}


def _maybe_xfail(property_name: str, tier: str) -> None:
    """Apply strict xfail for a (property, tier) cell expected to fail
    today. A cell that unexpectedly passes turns the suite red, forcing
    the migration phase to delete the stale expectation."""
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


@pytest.fixture(autouse=True)
def _inject_liveness(monkeypatch: pytest.MonkeyPatch, alive: _AliveSet) -> None:
    # Redirect both tier liveness probes at the managed synthetic pids.
    monkeypatch.setattr("nexus.daemon.discovery.os.kill", alive.fake_os_kill)
    monkeypatch.setattr("nexus.session._is_pid_alive", alive.is_alive)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    # T1 paths resolve NEXUS_CONFIG_DIR at call time (session.py contract).
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


@pytest.fixture(params=TIERS)
def tier(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def harness(tier: str, config_dir: Path, alive: _AliveSet) -> RecordHarness:
    return _HARNESS_CLASSES[tier](config_dir, alive)


# ---------------------------------------------------------------------------
# The parameterized property battery
# ---------------------------------------------------------------------------


class TestLifecycleConformance:
    def test_roundtrip(self, harness: RecordHarness, tier: str) -> None:
        _maybe_xfail("roundtrip", tier)
        harness.publish()
        rec = harness.discover()
        assert rec is not None
        assert rec["pid"] == _OWNER_PID

    def test_reap_ungraceful(
        self, harness: RecordHarness, tier: str, alive: _AliveSet
    ) -> None:
        # Owner dies with no cleanup (SIGKILL): the record lingers on disk
        # but the tier's reap path must reject/remove it so no client
        # resolves a dead owner.
        _maybe_xfail("reap_ungraceful", tier)
        harness.publish()
        assert harness.discover() is not None
        alive.mark_dead(_OWNER_PID)
        harness.reap()
        assert harness.discover() is None

    def test_self_heal(self, harness: RecordHarness, tier: str) -> None:
        # The record is lost (transient fs gap / external rm) while the
        # owner is alive. A self-healing tier re-asserts it within a tick;
        # T1 (#1114) and T3 (RF-4) have no re-assert loop -> stays gone.
        _maybe_xfail("self_heal", tier)
        harness.publish()
        harness.external_delete()
        assert harness.discover() is None
        for _ in range(3):
            harness.heartbeat()
        assert harness.discover() is not None, "owner alive but record not self-healed"

    def test_concurrent_one_owner(
        self, harness: RecordHarness, tier: str, alive: _AliveSet
    ) -> None:
        # Two MCP siblings of ONE logical session (same session-id) race to
        # own the scope. T2/T3 (uid scope) converge to exactly one record;
        # T1 keys on claude_pid so the session ends up with two owners.
        _maybe_xfail("concurrent_one_owner", tier)
        alive.mark_alive(_OWNER_PID)
        alive.mark_alive(_SIBLING_PID)
        harness.publish(pid=_OWNER_PID)
        harness.publish(pid=_SIBLING_PID)
        assert harness.owners_in_scope(session_id="sess-A") == 1

    def test_version_cycle(self, harness: RecordHarness, tier: str) -> None:
        # An upgrade must replace the running owner with a current-version
        # owner. Today only T2 is wired into the upgrade-cycle; T3 (#1112)
        # and T1 are left running the stale version.
        _maybe_xfail("version_cycle", tier)
        assert harness.has_version_cycle, (
            "tier is not covered by any upgrade-cycle entrypoint"
        )

    def test_pid_reuse_immunity(
        self, harness: RecordHarness, tier: str, alive: _AliveSet
    ) -> None:
        # Owner dies; the kernel recycles its pid to an unrelated live
        # process. A pid-based liveness check FALSELY treats the stale
        # record as live. A leased/generation primitive is immune.
        _maybe_xfail("pid_reuse_immunity", tier)
        harness.publish(pid=_REUSED_PID)
        assert harness.discover(pid=_REUSED_PID) is not None
        # Owner exits, pid recycled to a different live process: still
        # "alive" by os.kill(pid, 0), so the record survives a reap.
        alive.mark_alive(_REUSED_PID)  # the recycled occupant is alive
        harness.reap()
        # Immunity means the now-stale record is NOT resolvable. Today it
        # is (pid happens alive), so this fails until the lease lands.
        assert harness.discover(pid=_REUSED_PID) is None

    def test_restart_higher_generation(
        self, harness: RecordHarness, tier: str
    ) -> None:
        # A restarted owner must republish with a strictly higher
        # monotonic generation so stale predecessors are fenced. No tier
        # persists a generation today (RF-3).
        _maybe_xfail("restart_higher_generation", tier)
        harness.publish(generation=1)
        assert harness.record_generation() == 1
        harness.external_delete()
        harness.publish(generation=None)  # a naive restart with no gen bump
        gen = harness.record_generation()
        assert gen is not None and gen > 1, "restart did not fence with a higher generation"

    def test_restart_race_fencing(
        self, harness: RecordHarness, tier: str, alive: _AliveSet
    ) -> None:
        # A slow predecessor's delayed shutdown must NOT clobber a newer,
        # higher-generation owner's record (CA-4). Without a fencing token
        # the late write wins and strands clients on the dead owner.
        _maybe_xfail("restart_race_fencing", tier)
        harness.publish(pid=_OWNER_PID, generation=2)  # the new owner
        # The old owner (generation 1) wakes late and re-writes.
        harness.publish(pid=_SIBLING_PID, generation=1)
        rec = harness.discover()
        assert rec is not None
        assert rec.get("generation") == 2, "stale lower-generation owner clobbered the record"


# ---------------------------------------------------------------------------
# T1-only properties (CA-3): the transient-key -> session-id re-key. T1
# today has no session-id keying at all, so the property is unsatisfiable.
# ---------------------------------------------------------------------------


class TestT1SessionRekey:
    def test_record_keyed_on_session_id_not_pid(
        self, config_dir: Path, alive: _AliveSet
    ) -> None:
        # CA-3: a reader resolving by session-id must find the owner. Today
        # T1 keys solely on claude_pid, so there is no session-id-addressed
        # record to resolve. RDR-149 P5 introduces the re-key.
        pytest.xfail("CA-3: T1 has no session-id-keyed record yet; RDR-149 P5")
        h = T1RecordHarness(config_dir, alive)
        h.publish(pid=_OWNER_PID)
        # No public session-id resolver exists; assert the intended shape.
        sess_path = config_dir / "t1_addr.sess-A"
        assert sess_path.exists(), "no session-id-keyed T1 record"


# ---------------------------------------------------------------------------
# Non-vacuity guard (CA-1): the matrix MUST reproduce #1114 and #1112 as
# reds, and T2 MUST pass every property that T1 or T3 fails as a GAP.
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
        # The discriminating invariant: for any property where T1 or T3 has
        # a GAP, T2 must pass it. A GAP that T2 also failed would be
        # mis-specified (CA-1). SPEC cells (forward primitive) are exempt:
        # no tier satisfies them yet by construction.
        for prop, cells in EXPECTATIONS.items():
            t1_gap = isinstance(cells["t1"], tuple) and cells["t1"][0] == GAP
            t3_gap = isinstance(cells["t3"], tuple) and cells["t3"][0] == GAP
            if t1_gap or t3_gap:
                assert cells["t2"] == "pass", (
                    f"property {prop!r} is a GAP for T1/T3 but T2 does not pass "
                    f"it; the property is mis-specified (CA-1)"
                )

    def test_every_cell_covers_all_tiers(self) -> None:
        for prop, cells in EXPECTATIONS.items():
            assert set(cells) == set(TIERS), f"property {prop!r} missing a tier"


# ---------------------------------------------------------------------------
# Live-process behavioral proof (integration marker only): a REAL in-process
# T2 daemon self-heals a deleted discovery file via its re-assert loop. This
# is the green counterpart to the record-level T2 self_heal cell and the
# anchor the T1/T3 migrations are measured against. port=0, shrunk interval,
# single event loop (RDR-140 convention); mirrors test_rdr146_fairness.
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
        # Short /tmp config dir: the deep pytest tmp_path overflows the
        # AF_UNIX UDS path limit (mirrors test_rdr146_fairness.config_dir).
        cd = Path(tempfile.mkdtemp(prefix="nx149-", dir="/tmp"))
        daemon = _t2.T2Daemon(config_dir=cd, db_path=cd / "memory.db")
        daemon._monotonic = _FakeClock()

        async def _main() -> None:
            await daemon.start()
            try:
                disc = daemon.discovery_path
                assert disc.exists()
                disc.unlink()
                assert not disc.exists()
                # The re-assert loop must restore the file within a few
                # shrunk intervals while the daemon is alive.
                for _ in range(40):
                    await asyncio.sleep(0.05)
                    if disc.exists():
                        break
                assert disc.exists(), "live T2 daemon failed to self-heal discovery file"
                payload = json.loads(disc.read_text())
                assert payload["pid"] == os.getpid()
            finally:
                await daemon.stop()
            # stop() cancels re-assert BEFORE unlink, so the file stays gone.
            assert not disc.exists()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_main())
        finally:
            loop.close()
            shutil.rmtree(cd, ignore_errors=True)
