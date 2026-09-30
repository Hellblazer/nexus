# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-192 (nexus-wbfpw.41): the upgrade-ladder rung that censuses and
backfills legacy-unmanifested chunks on every install, and the gate the
reaper reads.

Unit level: the rung's detect/converge/verify semantics and its walk through
the real ``LadderRunner`` against injected census and backfill seams. The
engine-backed journey (real ``nexus.chunks`` rows seeded with direct SQL,
real ``HttpLadderStore`` completion ledger) lives in
``test_rdr192_manifest_backfill_substrate.py``.

The property under test is the one the reaper depends on: a completion
record exists ONLY when a fresh census read zero legacy-unmanifested chunks,
because ``reapable(c)`` treats a manifest-less chunk as garbage and the
reaper (nexus-2x9xa) refuses to run on a tenant without that record.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import nexus.db as nexus_db
from nexus.db.http_vector_client import VectorServiceError
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    RUNG_NAME,
    _default_census,
    BackfillIncompleteError,
    CensusReading,
    CensusUnavailable,
    CollectionReading,
    Rdr192ManifestBackfillRung,
    rdr192_backfill_complete,
    require_rdr192_backfill_complete,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.conftest import InMemoryCompletionLedger

_REPO = Path(__file__).resolve().parents[2]


def _reading(**legacy_by_collection: int) -> CensusReading:
    return CensusReading(collections=tuple(
        CollectionReading(collection=name, legacy_unmanifested=count, unclassified=0)
        for name, count in legacy_by_collection.items()
    ))


class FakeTenant:
    """A tenant whose census answers from mutable state and whose backfill
    heals a collection by ``heal_per_pass`` chunks (default: all of it)."""

    def __init__(self, legacy: dict[str, int], *, heal_per_pass: int | None = None,
                 unclassified: dict[str, int] | None = None) -> None:
        self.legacy = dict(legacy)
        self.unclassified = dict(unclassified or {})
        self.heal_per_pass = heal_per_pass
        self.censuses = 0
        self.backfilled: list[str] = []
        self.unavailable = False
        self.backfill_errors: dict[str, Exception] = {}

    def census(self) -> CensusReading:
        self.censuses += 1
        if self.unavailable:
            raise CensusUnavailable("engine not reachable")
        names = sorted(set(self.legacy) | set(self.unclassified))
        return CensusReading(collections=tuple(
            CollectionReading(
                collection=n,
                legacy_unmanifested=self.legacy.get(n, 0),
                unclassified=self.unclassified.get(n, 0),
            )
            for n in names
        ))

    def backfill(self, collection: str) -> int:
        self.backfilled.append(collection)
        if collection in self.backfill_errors:
            raise self.backfill_errors[collection]
        before = self.legacy.get(collection, 0)
        healed = before if self.heal_per_pass is None else min(before, self.heal_per_pass)
        self.legacy[collection] = before - healed
        return healed

    def rung(self, **kw) -> Rdr192ManifestBackfillRung:
        kw.setdefault("recorded_fn", lambda: False)  # never reach for a real engine ledger
        return Rdr192ManifestBackfillRung(census_fn=self.census, backfill_fn=self.backfill, **kw)


class _Reporter:
    def emit(self, event: str, **fields: object) -> None:  # noqa: D401
        pass


# ── seam conformance ─────────────────────────────────────────────────────────


def test_rung_conforms_to_the_rung_protocol_and_carries_the_pinned_name() -> None:
    rung = FakeTenant({}).rung()
    assert isinstance(rung, Rung)
    assert rung.name == RUNG_NAME == "rdr192-manifest-backfill"


def test_rung_is_registered_in_the_production_ladder() -> None:
    names = [r.name for r in default_registry()]
    assert RUNG_NAME in names


def test_java_reaper_gate_names_the_same_rung() -> None:
    """The engine reads ``nexus.ladder_completions`` by this literal name. If
    the two constants ever drift, the gate reads a rung that nothing records
    and the reaper is refused forever (or, worse, reads one that something
    else records)."""
    java = (
        _REPO / "service/src/main/java/dev/nexus/service/db/Rdr192BackfillGate.java"
    ).read_text(encoding="utf-8")
    assert f'RUNG_NAME = "{RUNG_NAME}"' in java


# ── detect: read-only, state-derived ─────────────────────────────────────────


def test_detect_converged_when_census_reads_zero() -> None:
    status = FakeTenant({"knowledge__a": 0}).rung().detect()
    assert status.applicable and status.converged and not status.pending


def test_detect_pending_names_count_and_collections() -> None:
    status = FakeTenant({"knowledge__a": 2, "knowledge__b": 0, "code__c": 1}).rung().detect()
    assert status.pending
    assert "3" in status.pending_detail
    assert "knowledge__a" in status.pending_detail and "code__c" in status.pending_detail
    assert "knowledge__b" not in status.pending_detail


def test_detect_never_backfills() -> None:
    tenant = FakeTenant({"knowledge__a": 5})
    tenant.rung().detect()
    assert tenant.backfilled == []


def test_detect_on_an_empty_listing_is_converged_not_vacuously_skipped() -> None:
    """A fresh install has no collections. The census SUCCEEDED and found
    nothing to backfill: converged, and it must be recordable so the reaper
    can ever run there. (The other direction, a census that could not run,
    is pinned below and is never converged.)"""
    status = FakeTenant({}).rung().detect()
    assert status.applicable and status.converged


def test_detect_unreachable_engine_is_pending_with_the_reason_not_converged() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    tenant.unavailable = True
    status = tenant.rung().detect()
    assert status.applicable and not status.converged
    assert "engine not reachable" in status.pending_detail


def test_detect_unclassified_rows_are_pending() -> None:
    status = FakeTenant({}, unclassified={"knowledge__a": 1}).rung().detect()
    assert status.pending
    assert "unclassified" in status.pending_detail


# ── converge ─────────────────────────────────────────────────────────────────


def test_converge_backfills_only_the_collections_with_legacy_chunks() -> None:
    tenant = FakeTenant({"knowledge__a": 2, "knowledge__b": 0, "code__c": 1})
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert sorted(tenant.backfilled) == ["code__c", "knowledge__a"]
    assert tenant.legacy == {"knowledge__a": 0, "knowledge__b": 0, "code__c": 0}


def test_converge_repeats_until_the_census_reads_zero() -> None:
    tenant = FakeTenant({"knowledge__a": 3}, heal_per_pass=1)
    tenant.rung().converge(_Reporter())
    assert tenant.legacy["knowledge__a"] == 0
    assert tenant.backfilled.count("knowledge__a") == 3


def test_converge_stops_when_a_pass_makes_no_progress() -> None:
    """An unhealable residual must not spin: two passes with no drop end the
    loop, and verify() is what refuses the record."""
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    tenant.rung(max_passes=50).converge(_Reporter())
    assert len(tenant.backfilled) <= 2


def test_converge_is_bounded_by_max_passes() -> None:
    tenant = FakeTenant({"knowledge__a": 100}, heal_per_pass=1)
    tenant.rung(max_passes=3).converge(_Reporter())
    assert tenant.backfilled.count("knowledge__a") == 3


def test_converge_defers_when_the_census_cannot_run() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    tenant.unavailable = True
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "engine not reachable" in result.detail
    assert tenant.backfilled == []


def test_a_failing_backfill_does_not_stop_the_other_collections() -> None:
    tenant = FakeTenant({"knowledge__a": 1, "knowledge__b": 1})
    tenant.backfill_errors["knowledge__a"] = RuntimeError("boom")
    rung = tenant.rung()
    rung.converge(_Reporter())
    assert tenant.legacy["knowledge__b"] == 0
    assert tenant.legacy["knowledge__a"] == 1
    assert rung.verify() is False
    assert "boom" in rung.verify_detail()


# ── verify: presence of the positive signal, from a fresh read ───────────────


def test_verify_true_only_on_a_fresh_zero_census() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    rung = tenant.rung()
    assert rung.verify() is False
    rung.converge(_Reporter())
    assert rung.verify() is True


def test_verify_re_reads_the_census_and_does_not_trust_converge() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    rung = tenant.rung()
    rung.converge(_Reporter())
    tenant.legacy["knowledge__a"] = 1  # a legacy row reappears after converge
    assert rung.verify() is False


def test_verify_unknown_is_not_reached() -> None:
    tenant = FakeTenant({})
    tenant.unavailable = True
    rung = tenant.rung()
    assert rung.verify() is False
    assert "engine not reachable" in rung.verify_detail()


def test_verify_refuses_unclassified_rows() -> None:
    tenant = FakeTenant({}, unclassified={"knowledge__a": 2})
    rung = tenant.rung()
    assert rung.verify() is False
    assert "unclassified" in rung.verify_detail()


# ── the walk: RDR-142 verify-before-record, idempotence ──────────────────────


def _walk(tenant: FakeTenant, ledger: InMemoryCompletionLedger):
    rung = tenant.rung(recorded_fn=lambda: RUNG_NAME in ledger.verified_rungs())
    return LadderRunner(LadderRegistry((rung,)), ledger).run()


def test_walk_records_completion_after_a_real_heal() -> None:
    tenant = FakeTenant({"knowledge__a": 2})
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    assert RUNG_NAME in ledger.verified_rungs()


def test_walk_records_on_a_clean_tenant_without_backfilling_anything() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    assert tenant.backfilled == []


def test_walk_with_an_unhealable_residual_does_not_record() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.VERIFY_FAILED]
    assert RUNG_NAME not in ledger.verified_rungs()
    assert "knowledge__a" in report.runs[0].detail
    assert "nx t3 census-manifest-less" in report.runs[0].detail


def test_walk_defers_and_does_not_record_when_the_engine_is_unreachable() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    tenant.unavailable = True
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
    assert RUNG_NAME not in ledger.verified_rungs()
    assert not report.hard_failed


def test_second_walk_is_a_no_op_and_takes_no_census() -> None:
    """nx upgrade --auto walks the ladder at every SessionStart; once the
    completion is on file the rung must not re-census the tenant."""
    tenant = FakeTenant({"knowledge__a": 2})
    ledger = InMemoryCompletionLedger()
    _walk(tenant, ledger)
    calls_after_first = list(tenant.backfilled)
    censuses_after_first = tenant.censuses
    assert censuses_after_first > 0  # control: the first walk did census
    second = _walk(tenant, ledger)
    assert [r.outcome for r in second.runs] == [RungOutcome.ALREADY_RECORDED]
    assert tenant.backfilled == calls_after_first
    assert tenant.censuses == censuses_after_first


def test_detect_trusts_a_record_and_skips_the_census() -> None:
    tenant = FakeTenant({"knowledge__a": 5})
    status = tenant.rung(recorded_fn=lambda: True).detect()
    assert status.applicable and status.converged
    assert tenant.censuses == 0


def test_detect_falls_through_to_the_census_when_the_ledger_probe_raises() -> None:
    def boom() -> bool:
        raise ConnectionError("ledger down")

    tenant = FakeTenant({"knowledge__a": 5})
    status = tenant.rung(recorded_fn=boom).detect()
    assert status.pending
    assert tenant.censuses == 1


def test_walk_after_a_failed_run_resumes_and_records() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    ledger = InMemoryCompletionLedger()
    assert _walk(tenant, ledger).hard_failed
    tenant.heal_per_pass = None  # the operator fixed the cause
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]


# ── the reaper's gate ────────────────────────────────────────────────────────


def test_gate_is_closed_until_the_rung_is_recorded() -> None:
    ledger = InMemoryCompletionLedger()
    assert rdr192_backfill_complete(ledger) is False
    with pytest.raises(BackfillIncompleteError) as excinfo:
        require_rdr192_backfill_complete(ledger)
    assert "nx upgrade" in str(excinfo.value)
    assert RUNG_NAME in str(excinfo.value)

    _walk(FakeTenant({"knowledge__a": 1}), ledger)
    assert rdr192_backfill_complete(ledger) is True
    require_rdr192_backfill_complete(ledger)  # does not raise


def test_gate_ignores_other_rungs() -> None:
    ledger = InMemoryCompletionLedger()
    ledger.record_verified("some-other-rung", package_version="x")
    assert rdr192_backfill_complete(ledger) is False


def test_gate_treats_an_unreadable_ledger_as_closed() -> None:
    class Down:
        def verified_rungs(self):
            raise ConnectionError("engine down")

    assert rdr192_backfill_complete(Down()) is False
    with pytest.raises(BackfillIncompleteError, match="engine down"):
        require_rdr192_backfill_complete(Down())


# ── the production census: what counts as "could not be taken" ───────────────


class _FakeVectorClient:
    def __init__(self, rows, totals_by_collection=None, *, list_error=None, census_error=None):
        self._rows = rows
        self._totals = totals_by_collection or {}
        self._list_error = list_error
        self._census_error = census_error
        self.censused: list[str] = []

    def list_collections(self, *, strict=False):
        assert strict is True, "a failed listing must raise, not read as an empty tenant"
        if self._list_error:
            raise self._list_error
        return self._rows

    def manifest_less_census(self, collection, limit=100, offset=0):
        if self._census_error:
            raise self._census_error
        self.censused.append(collection)
        return {"totals": self._totals.get(collection, {})}


def _patch_client(monkeypatch, client) -> None:

    monkeypatch.setattr(nexus_db, "make_t3", lambda **kw: client)


def test_default_census_reads_legacy_and_unclassified_and_skips_quarantine(monkeypatch) -> None:

    client = _FakeVectorClient(
        [
            {"name": "knowledge__a", "lifecycle_state": "live"},
            {"name": "docs__b"},  # no catalog row joined: unregistered, stays IN
            {"name": "quarantine-x", "lifecycle_state": "quarantine"},
        ],
        {
            "knowledge__a": {"legacy-unmanifested": 2, "no-owner": 9},
            "docs__b": {"unclassified": 1},
            "quarantine-x": {"legacy-unmanifested": 99},
        },
    )
    _patch_client(monkeypatch, client)
    reading = _default_census()
    assert client.censused == ["knowledge__a", "docs__b"]
    assert reading.legacy_total == 2 and reading.unclassified_total == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ConnectionRefusedError("refused"), id="connection-refused"),
        pytest.param(TimeoutError("slow"), id="timeout"),
        pytest.param(ValueError("malformed config.yml while resolving the endpoint"), id="endpoint-resolution"),
    ],
)
def test_default_census_maps_any_failure_to_reach_the_engine_to_unavailable(monkeypatch, error) -> None:

    _patch_client(monkeypatch, _FakeVectorClient([], list_error=error))
    with pytest.raises(CensusUnavailable, match="could not be reached"):
        _default_census()


def test_default_census_names_an_engine_that_predates_the_route(monkeypatch) -> None:

    client = _FakeVectorClient(
        [{"name": "knowledge__a"}], census_error=VectorServiceError("nope", code=404),
    )
    _patch_client(monkeypatch, client)
    with pytest.raises(CensusUnavailable, match="predates the manifest-less-census route"):
        _default_census()


def test_default_census_does_not_swallow_a_bug_in_reading_the_answer(monkeypatch) -> None:

    _patch_client(monkeypatch, _FakeVectorClient([{"count": 3}]))  # a row with no "name"
    with pytest.raises(KeyError):
        _default_census()
