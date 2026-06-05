# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 Phase 1 (bead nexus-qf0lk): the leased service-registry primitive.

Unit suite for ``nexus.daemon.service_registry`` — the single pure,
deterministic, fixed-clock-testable substrate that T1/T2/T3 migrate onto
(P2-P5). No tier-specific code is exercised here; the registry is
parameterized only by a scope key + a tier file prefix.

Core semantics under test (RDR-149 Decision):
- Lease, not PID: liveness is TTL freshness on a wall-clock heartbeat
  stamp; identity is a server-unique ``owner_token`` (uuid4), never a pid.
- Fencing token: a per-scope monotonic ``generation`` bumped under the
  election flock at publish (read-increment-write, RF-3).
- Atomic publish: write-temp + os.replace, always.
- Scope-keyed election: a per-scope flock serializes the generation RMW.
- CA-4: a stale lower-generation owner can never clobber a newer
  higher-generation owner's record (the restart-race harness).
- pid-reuse immunity: no pid in the identity or liveness path.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from nexus.daemon.service_registry import (
    LeaseRecord,
    ServiceRegistry,
    ServiceSupervisor,
    StaleOwnerError,
)


class _FakeClock:
    """Fixed, advanceable wall-clock surrogate (mirrors P0 / fairness)."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def registry(tmp_path: Path, clock: _FakeClock) -> ServiceRegistry:
    return ServiceRegistry(
        dir=tmp_path, tier="t2", clock=clock, ttl=3.0, heartbeat_interval=1.0
    )


def _endpoint(port: int = 5000) -> dict:
    return {"host": "127.0.0.1", "port": port}


# ---------------------------------------------------------------------------
# LeaseRecord
# ---------------------------------------------------------------------------


class TestLeaseRecord:
    def test_json_roundtrip(self) -> None:
        rec = LeaseRecord(
            scope_key="42",
            generation=7,
            owner_token="tok-abc",
            heartbeat_epoch=1234.5,
            ttl=3.0,
            endpoint=_endpoint(),
            version="1.2.3",
            payload={"k": "v"},
        )
        back = LeaseRecord.from_json(rec.to_json())
        assert back == rec

    def test_is_fresh_within_ttl(self) -> None:
        rec = LeaseRecord(
            scope_key="42", generation=1, owner_token="t", heartbeat_epoch=1000.0,
            ttl=3.0, endpoint=_endpoint(), version="1",
        )
        assert rec.is_fresh(1002.99) is True
        assert rec.is_fresh(1003.01) is False

    def test_shutdown_marker_is_never_fresh(self) -> None:
        rec = LeaseRecord(
            scope_key="42", generation=1, owner_token="t", heartbeat_epoch=1000.0,
            ttl=3.0, endpoint=_endpoint(), version="1", status="shutting_down",
        )
        assert rec.is_fresh(1000.0) is False


# ---------------------------------------------------------------------------
# publish: election + generation fencing
# ---------------------------------------------------------------------------


class TestPublish:
    def test_first_publish_is_generation_one(self, registry: ServiceRegistry) -> None:
        rec = registry.publish(
            "42", endpoint=_endpoint(), version="1", owner_token="A"
        )
        assert rec.generation == 1
        assert rec.owner_token == "A"

    def test_republish_increments_generation(self, registry: ServiceRegistry) -> None:
        registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        rec2 = registry.publish("42", endpoint=_endpoint(), version="2", owner_token="B")
        assert rec2.generation == 2

    def test_publish_stamps_current_clock(
        self, registry: ServiceRegistry, clock: _FakeClock
    ) -> None:
        clock.advance(50.0)
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        assert rec.heartbeat_epoch == 1050.0

    def test_publish_is_atomic_no_tmp_left(
        self, registry: ServiceRegistry, tmp_path: Path
    ) -> None:
        registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_distinct_scopes_are_independent(self, registry: ServiceRegistry) -> None:
        a = registry.publish("uidA", endpoint=_endpoint(), version="1", owner_token="A")
        b = registry.publish("uidB", endpoint=_endpoint(), version="1", owner_token="B")
        assert a.generation == 1 and b.generation == 1
        assert registry.discover("uidA").owner_token == "A"
        assert registry.discover("uidB").owner_token == "B"


# ---------------------------------------------------------------------------
# heartbeat: refresh, self-heal, fencing
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_heartbeat_refreshes_epoch_same_generation(
        self, registry: ServiceRegistry, clock: _FakeClock
    ) -> None:
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        clock.advance(1.0)
        refreshed = registry.heartbeat(rec)
        assert refreshed.generation == 1
        assert refreshed.owner_token == "A"
        assert refreshed.heartbeat_epoch == 1001.0

    def test_heartbeat_self_heals_lost_record(
        self, registry: ServiceRegistry
    ) -> None:
        # RF-1: the record is externally deleted while the owner is alive;
        # the next heartbeat re-publishes it at the SAME generation.
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        registry._record_path("42").unlink()
        assert registry.discover("42") is None
        healed = registry.heartbeat(rec)
        assert healed.generation == 1
        assert registry.discover("42") is not None

    def test_heartbeat_fenced_by_newer_generation_raises(
        self, registry: ServiceRegistry
    ) -> None:
        # CA-4: a newer owner (gen 2) exists; the old owner's heartbeat must
        # NOT clobber it and must learn it has been fenced.
        old = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        registry.publish("42", endpoint=_endpoint(), version="2", owner_token="B")
        with pytest.raises(StaleOwnerError):
            registry.heartbeat(old)
        # B's record is untouched.
        cur = registry.discover("42")
        assert cur.generation == 2 and cur.owner_token == "B"

    def test_heartbeat_foreign_same_generation_raises(
        self, registry: ServiceRegistry, tmp_path: Path
    ) -> None:
        # A different owner_token at the same generation means our identity
        # was displaced; refuse to overwrite.
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        # Forge a same-generation record owned by someone else.
        foreign = LeaseRecord(
            scope_key="42", generation=1, owner_token="X",
            heartbeat_epoch=rec.heartbeat_epoch, ttl=3.0, endpoint=_endpoint(),
            version="1",
        )
        registry._record_path("42").write_text(foreign.to_json())
        with pytest.raises(StaleOwnerError):
            registry.heartbeat(rec)


# ---------------------------------------------------------------------------
# discover + reap (TTL liveness, pid-free)
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_discover_returns_fresh_owner(self, registry: ServiceRegistry) -> None:
        registry.publish("42", endpoint=_endpoint(7), version="1", owner_token="A")
        rec = registry.discover("42")
        assert rec is not None and rec.endpoint["port"] == 7

    def test_discover_none_when_missing(self, registry: ServiceRegistry) -> None:
        assert registry.discover("nope") is None

    def test_discover_none_and_reaps_when_stale(
        self, registry: ServiceRegistry, clock: _FakeClock
    ) -> None:
        registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        clock.advance(3.1)  # past TTL
        assert registry.discover("42") is None
        # Stale record was reaped.
        assert registry._record_path("42").exists() is False

    def test_discover_none_on_shutdown_marker(
        self, registry: ServiceRegistry
    ) -> None:
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        registry.mark_shutting_down(rec)
        assert registry.discover("42") is None

    def test_pid_reuse_immunity_dead_lease_ages_out(
        self, registry: ServiceRegistry, clock: _FakeClock
    ) -> None:
        # No pid is consulted anywhere; a dead owner's lease simply ages
        # past TTL and is gone, regardless of any pid recycling.
        registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        clock.advance(10.0)
        assert registry.discover("42") is None


# ---------------------------------------------------------------------------
# relinquish: own-record-only deletion (CA-4 shutdown ordering)
# ---------------------------------------------------------------------------


class TestRelinquish:
    def test_relinquish_deletes_own_record(self, registry: ServiceRegistry) -> None:
        rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        registry.relinquish(rec)
        assert registry.discover("42") is None

    def test_relinquish_does_not_delete_successor_record(
        self, registry: ServiceRegistry
    ) -> None:
        # CA-4: an old owner's delayed shutdown must not unlink the newer
        # owner's record.
        old = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="A")
        registry.publish("42", endpoint=_endpoint(), version="2", owner_token="B")
        registry.relinquish(old)  # A shutting down late
        cur = registry.discover("42")
        assert cur is not None and cur.owner_token == "B"


# ---------------------------------------------------------------------------
# Election: concurrent publish serializes to distinct generations
# ---------------------------------------------------------------------------


class TestElection:
    def test_concurrent_publish_distinct_generations_one_survivor(
        self, registry: ServiceRegistry
    ) -> None:
        # The per-scope flock serializes the read-increment-write so two
        # racing publishers get distinct generations and exactly one final
        # record survives (last writer), never a torn or duplicated record.
        results: list[LeaseRecord] = []
        barrier = threading.Barrier(2)

        def _pub(tok: str) -> None:
            barrier.wait()
            results.append(
                registry.publish("42", endpoint=_endpoint(), version="1", owner_token=tok)
            )

        ths = [threading.Thread(target=_pub, args=(t,)) for t in ("A", "B")]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=10)

        gens = sorted(r.generation for r in results)
        assert gens == [1, 2], f"election did not serialize generations: {gens}"
        assert registry.discover("42") is not None


# ---------------------------------------------------------------------------
# CA-4 restart-race harness (the load-bearing fencing proof)
# ---------------------------------------------------------------------------


class TestRestartRaceFencing:
    def test_delayed_old_owner_cannot_clobber_newer(
        self, registry: ServiceRegistry
    ) -> None:
        # A: gen 1. Restart elects B: gen 2. A's shutdown + heartbeat arrive
        # LATE. Neither may touch B's record.
        a = registry.publish("42", endpoint=_endpoint(1), version="1", owner_token="A")
        b = registry.publish("42", endpoint=_endpoint(2), version="2", owner_token="B")
        assert b.generation == 2

        # Late heartbeat from A: fenced.
        with pytest.raises(StaleOwnerError):
            registry.heartbeat(a)
        # Late relinquish from A: no-op on B's record.
        registry.relinquish(a)

        cur = registry.discover("42")
        assert cur is not None
        assert cur.generation == 2
        assert cur.owner_token == "B"
        assert cur.endpoint["port"] == 2


# ---------------------------------------------------------------------------
# Supervisor: heartbeat cadence + version-cycle (covers all tiers)
# ---------------------------------------------------------------------------


class TestSupervisor:
    def test_publish_once_then_tick_refreshes(
        self, registry: ServiceRegistry, clock: _FakeClock
    ) -> None:
        sup = ServiceSupervisor(
            registry, "42", version="1", endpoint_provider=lambda: _endpoint()
        )
        rec = sup.publish_once()
        assert rec.generation == 1
        clock.advance(1.0)
        sup.heartbeat_tick()
        assert registry.discover("42").heartbeat_epoch == 1001.0

    def test_tick_after_fence_marks_supervisor_fenced(
        self, registry: ServiceRegistry
    ) -> None:
        sup = ServiceSupervisor(
            registry, "42", version="1", endpoint_provider=lambda: _endpoint()
        )
        sup.publish_once()
        # A newer owner takes over.
        registry.publish("42", endpoint=_endpoint(), version="2", owner_token="newer")
        sup.heartbeat_tick()
        assert sup.fenced is True

    def test_cycle_to_current_no_op_when_versions_match(
        self, registry: ServiceRegistry
    ) -> None:
        sup = ServiceSupervisor(
            registry, "42", version="1", endpoint_provider=lambda: _endpoint()
        )
        sup.publish_once()
        calls: list[str] = []
        sup.cycle_to_current(
            "1", stop_owner=lambda: calls.append("stop"),
            start_owner=lambda: calls.append("start"),
        )
        assert calls == []

    def test_cycle_to_current_stops_and_starts_on_skew(
        self, registry: ServiceRegistry
    ) -> None:
        # #1112 root cause is T3 having no cycle; the primitive supplies a
        # generic one driven by version-skew on the lease.
        sup = ServiceSupervisor(
            registry, "42", version="0.9", endpoint_provider=lambda: _endpoint()
        )
        sup.publish_once()
        calls: list[str] = []
        sup.cycle_to_current(
            "1.0", stop_owner=lambda: calls.append("stop"),
            start_owner=lambda: calls.append("start"),
        )
        assert calls == ["stop", "start"]
