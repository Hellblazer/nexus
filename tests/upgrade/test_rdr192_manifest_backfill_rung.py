# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-192 (nexus-wbfpw.41): the upgrade-ladder rung that censuses and
backfills legacy-unmanifested chunks on every install, and the gate the
reaper reads.

Unit level: the rung's detect/converge/verify semantics and its walk through
the real ``LadderRunner`` against injected census, backfill, ledger, memo and
lock seams. The engine-backed journey (real ``nexus.chunks`` rows seeded with
direct SQL, real ``HttpLadderStore`` completion ledger, real T2 memo) lives in
``test_rdr192_manifest_backfill_substrate.py``.

The property under test is the one the reaper depends on: a completion
record exists ONLY when a fresh census read zero legacy-unmanifested chunks,
because ``reapable(c)`` treats a manifest-less chunk as garbage and the
reaper (nexus-2x9xa) refuses to run on a tenant without that record.
"""
from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

import nexus.db as nexus_db
from nexus.db.http_vector_client import VectorServiceError
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    RUNG_NAME,
    BackfillIncompleteError,
    CensusReading,
    CensusUnavailable,
    CollectionReading,
    Rdr192ManifestBackfillRung,
    _default_census,
    _remedy,
    rdr192_backfill_complete,
    require_rdr192_backfill_complete,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from tests.upgrade.conftest import InMemoryCompletionLedger

_REPO = Path(__file__).resolve().parents[2]
_VERSION = "9.9.9"


class InMemoryMemo:
    def __init__(self) -> None:
        self.note: dict | None = None
        self.loads = 0

    def load(self):
        self.loads += 1
        return self.note

    def save(self, note: dict) -> None:
        self.note = dict(note)

    def clear(self) -> None:
        self.note = None


class FakeLock:
    """The cross-process lock seam: acquired unless ``held_elsewhere``."""

    def __init__(self) -> None:
        self.held_elsewhere = False
        self.entered = 0
        self.released = 0

    @contextlib.contextmanager
    def __call__(self):
        if self.held_elsewhere:
            yield False
            return
        self.entered += 1
        try:
            yield True
        finally:
            self.released += 1


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
        self.record: CompletionRecord | None = None
        self.memo = InMemoryMemo()
        self.lock = FakeLock()

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
                superseded=3, dead_owner=1, no_owner=2, scope_chunks=40,
            )
            for n in names
        ), quarantine_skipped=1)

    def backfill(self, collection: str) -> int:
        self.backfilled.append(collection)
        if collection in self.backfill_errors:
            raise self.backfill_errors[collection]
        before = self.legacy.get(collection, 0)
        healed = before if self.heal_per_pass is None else min(before, self.heal_per_pass)
        self.legacy[collection] = before - healed
        return healed

    def rung(self, **kw) -> Rdr192ManifestBackfillRung:
        kw.setdefault("record_fn", lambda: self.record)
        kw.setdefault("installed_version_fn", lambda: _VERSION)
        kw.setdefault("memo", self.memo)
        kw.setdefault("lock_factory", self.lock)
        return Rdr192ManifestBackfillRung(census_fn=self.census, backfill_fn=self.backfill, **kw)


def _record(version: str = _VERSION) -> CompletionRecord:
    return CompletionRecord(rung_name=RUNG_NAME, verified_at="t0", package_version=version)


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


# ── detect: cheap, never a census ────────────────────────────────────────────


def test_detect_converged_only_for_a_record_at_the_installed_version() -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    tenant.record = _record()
    status = tenant.rung().detect()
    assert status.applicable and status.converged and not status.pending
    assert tenant.censuses == 0


def test_detect_never_takes_a_census_in_any_state() -> None:
    for record in (None, _record(), _record("1.0.0")):
        tenant = FakeTenant({"knowledge__a": 7})
        tenant.record = record
        tenant.rung().detect()
        assert tenant.censuses == 0, f"detect censused with record={record}"


def test_detect_pending_without_a_record() -> None:
    tenant = FakeTenant({"knowledge__a": 7})
    status = tenant.rung().detect()
    assert status.pending and "no completion recorded" in status.pending_detail
    assert tenant.backfilled == []


def test_detect_pending_when_the_record_is_from_another_package_version() -> None:
    """The record is an attestation at a version; a new version re-derives it
    (review S1's cheaper bound)."""
    tenant = FakeTenant({})
    tenant.record = _record("1.0.0")
    status = tenant.rung().detect()
    assert status.pending
    assert "1.0.0" in status.pending_detail and _VERSION in status.pending_detail


def test_detect_reports_the_last_residual_from_the_memo() -> None:
    tenant = FakeTenant({})
    tenant.memo.note = {"fingerprint": "x", "at": "2026-09-30T00:00:00+00:00", "detail": "2 legacy in knowledge__a"}
    status = tenant.rung().detect()
    assert "2 legacy in knowledge__a" in status.pending_detail


def test_detect_unreadable_ledger_is_pending_with_the_reason() -> None:
    def boom():
        raise ConnectionError("ledger down")

    tenant = FakeTenant({})
    status = tenant.rung(record_fn=boom).detect()
    assert status.pending and "ledger down" in status.pending_detail


# ── converge ─────────────────────────────────────────────────────────────────


def test_converge_backfills_only_the_collections_with_legacy_chunks() -> None:
    tenant = FakeTenant({"knowledge__a": 2, "knowledge__b": 0, "code__c": 1})
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert sorted(tenant.backfilled) == ["code__c", "knowledge__a"]
    assert tenant.legacy == {"knowledge__a": 0, "knowledge__b": 0, "code__c": 0}


def test_converge_on_a_clean_census_backfills_nothing_and_completes() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert tenant.backfilled == []


def test_converge_repeats_until_the_census_reads_zero() -> None:
    tenant = FakeTenant({"knowledge__a": 3}, heal_per_pass=1)
    tenant.rung().converge(_Reporter())
    assert tenant.legacy["knowledge__a"] == 0
    assert tenant.backfilled.count("knowledge__a") == 3


def test_converge_stops_when_a_pass_makes_no_progress() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    tenant.rung(max_passes=50).converge(_Reporter())
    assert len(tenant.backfilled) == 1


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
    result = tenant.rung().converge(_Reporter())
    assert tenant.legacy["knowledge__b"] == 0
    assert tenant.legacy["knowledge__a"] == 1
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "boom" in result.detail


# ── the unhealable residual: deferred, loud, remembered, not repeated ────────


def test_an_unhealable_residual_defers_with_the_collections_and_a_real_remedy() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "knowledge__a" in result.detail and "1 legacy-unmanifested" in result.detail
    assert "nx store put" in result.detail
    assert "cannot heal" in result.detail


def test_a_deferred_residual_leaves_a_memo_for_the_next_run() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    tenant.rung().converge(_Reporter())
    assert tenant.memo.note is not None
    assert "knowledge__a" in tenant.memo.note["detail"]


def test_an_unchanged_residual_is_not_retried() -> None:
    """The heavy work (backfill scans, re-censuses) must not repeat at every
    session start for a residual that has not changed."""
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    backfills_after_first = len(tenant.backfilled)
    censuses_after_first = tenant.censuses

    second = rung.converge(_Reporter())
    assert second.outcome is ConvergeOutcome.DEFERRED
    assert "unchanged since the last attempt" in second.detail
    assert len(tenant.backfilled) == backfills_after_first
    assert tenant.censuses == censuses_after_first + 1  # one fresh census to compare, nothing more


def test_a_changed_residual_is_retried() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    backfills_after_first = len(tenant.backfilled)
    tenant.legacy["knowledge__b"] = 1  # a new legacy chunk elsewhere
    rung.converge(_Reporter())
    assert len(tenant.backfilled) > backfills_after_first


def test_a_new_package_version_retries_an_unchanged_residual() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    tenant.rung(installed_version_fn=lambda: "1.0.0").converge(_Reporter())
    backfills_after_first = len(tenant.backfilled)
    tenant.rung(installed_version_fn=lambda: "2.0.0").converge(_Reporter())
    assert len(tenant.backfilled) > backfills_after_first


def test_a_healed_residual_clears_the_memo() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    assert tenant.memo.note is not None
    tenant.heal_per_pass = None  # the cause was fixed
    tenant.legacy["knowledge__a"] = 0
    assert rung.converge(_Reporter()).outcome is ConvergeOutcome.COMPLETED
    assert tenant.memo.note is None


def test_a_held_lock_defers_without_touching_the_engine() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    tenant.lock.held_elsewhere = True
    result = tenant.rung().converge(_Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "another nx process" in result.detail
    assert tenant.censuses == 0 and tenant.backfilled == []


def test_the_lock_is_released_after_converge_even_when_the_census_raises() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    tenant.unavailable = True
    tenant.rung().converge(_Reporter())
    assert tenant.lock.entered == 1 and tenant.lock.released == 1


def test_remedy_names_a_path_for_each_class_and_admits_when_no_verb_heals() -> None:
    legacy = _remedy(CensusReading((CollectionReading("c", 2, 0),)))
    unclassified = _remedy(CensusReading((CollectionReading("c", 0, 3),)))
    assert "nx store put" in legacy and "cannot heal" in legacy and "No verb does" in legacy
    assert "nx t3 census-manifest-less" in legacy
    assert "unclassified" not in legacy
    assert "no verb heals" in unclassified and "maintainers" in unclassified
    assert "nx store put" not in unclassified


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


def test_record_detail_carries_the_census_summary() -> None:
    tenant = FakeTenant({"knowledge__a": 0, "knowledge__b": 0})
    rung = tenant.rung()
    assert rung.verify() is True
    detail = rung.record_detail()
    assert "collections=2" in detail and "quarantine_skipped=1" in detail
    assert "legacy-unmanifested=0" in detail and "unclassified=0" in detail
    assert "superseded=6" in detail and "no-owner=4" in detail and "chunks=80" in detail


# ── the walk: RDR-142 verify-before-record, idempotence ──────────────────────


def _walk(tenant: FakeTenant, ledger: InMemoryCompletionLedger, **kw):
    def record_from_ledger():
        return ledger.completions().get(RUNG_NAME)

    rung = tenant.rung(record_fn=record_from_ledger, **kw)
    return LadderRunner(
        LadderRegistry((rung,)), ledger, package_version_fn=lambda: _VERSION,
    ).run()


def test_walk_records_completion_after_a_real_heal_with_provenance() -> None:
    tenant = FakeTenant({"knowledge__a": 2})
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    record = ledger.completions()[RUNG_NAME]
    assert record.package_version == _VERSION
    assert "legacy-unmanifested=0" in record.detail and "collections=1" in record.detail


def test_walk_records_on_a_clean_tenant_without_backfilling_anything() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    assert tenant.backfilled == []


def test_walk_with_an_unhealable_residual_defers_and_does_not_record() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
    assert not report.hard_failed, "a residual must not fail the rest of `nx upgrade`"
    assert RUNG_NAME not in ledger.verified_rungs()
    assert "knowledge__a" in report.runs[0].detail


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
    completion is on file at this version the rung must not census."""
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


def test_walk_after_a_residual_resumes_and_records_when_it_is_fixed() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    ledger = InMemoryCompletionLedger()
    assert [r.outcome for r in _walk(tenant, ledger).runs] == [RungOutcome.DEFERRED]
    tenant.legacy["knowledge__a"] = 0  # the operator re-put the note
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]


def test_walk_re_censuses_after_a_package_version_change() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    ledger = InMemoryCompletionLedger()
    _walk(tenant, ledger)
    censuses = tenant.censuses
    rung = tenant.rung(record_fn=lambda: ledger.completions().get(RUNG_NAME), installed_version_fn=lambda: "10.0.0")
    report = LadderRunner(LadderRegistry((rung,)), ledger, package_version_fn=lambda: "10.0.0").run()
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    assert tenant.censuses > censuses
    assert ledger.completions()[RUNG_NAME].package_version == "10.0.0"


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


def test_gate_stays_closed_over_a_deferred_residual() -> None:
    ledger = InMemoryCompletionLedger()
    _walk(FakeTenant({"knowledge__a": 1}, heal_per_pass=0), ledger)
    assert rdr192_backfill_complete(ledger) is False


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

_ALL_BUCKETS = {
    "superseded": 0, "legacy-unmanifested": 0, "dead-owner": 0, "no-owner": 0, "unclassified": 0,
}


class _FakeVectorClient:
    def __init__(self, rows, pages=None, *, list_error=None, census_error=None):
        self._rows = rows
        self._pages = pages or {}
        self._list_error = list_error
        self._census_error = census_error
        self.censused: list[str] = []

    def list_collections(self, *, strict=False):
        assert strict is True, "a failed listing must raise, not read as an empty tenant"
        if self._list_error:
            raise self._list_error
        return self._rows

    def manifest_less_census(self, collection, limit=100, offset=0):
        if collection in (self._census_error or {}):
            raise self._census_error[collection]
        self.censused.append(collection)
        return self._pages.get(collection, {})


def _page(**totals: int) -> dict:
    return {"totals": {**_ALL_BUCKETS, **{k.replace("_", "-"): v for k, v in totals.items()}}, "scope_chunk_total": 10}


def _patch_client(monkeypatch, client) -> None:
    monkeypatch.setattr(nexus_db, "make_t3", lambda **kw: client)


def _patch_catalog_chunk_count(monkeypatch, count: int | Exception) -> None:
    class _Catalog:
        def stats(self):
            if isinstance(count, Exception):
                raise count
            return {"chunk_count": count}

    import nexus.catalog.factory as factory

    monkeypatch.setattr(factory, "make_catalog_reader", lambda: _Catalog())


def test_default_census_reads_legacy_and_unclassified_and_skips_quarantine(monkeypatch) -> None:
    client = _FakeVectorClient(
        [
            {"name": "knowledge__a", "lifecycle_state": "live"},
            {"name": "docs__b"},  # no catalog row joined: unregistered, stays IN
            {"name": "quarantine-x", "lifecycle_state": "quarantine"},
        ],
        {
            "knowledge__a": _page(legacy_unmanifested=2, no_owner=9),
            "docs__b": _page(unclassified=1),
            "quarantine-x": _page(legacy_unmanifested=99),
        },
    )
    _patch_client(monkeypatch, client)
    reading = _default_census()
    assert client.censused == ["knowledge__a", "docs__b"]
    assert reading.legacy_total == 2 and reading.unclassified_total == 1
    assert reading.quarantine_skipped == 1
    assert sum(c.no_owner for c in reading.collections) == 9


def test_default_census_counts_an_engine_refused_quarantine_instead_of_deferring(monkeypatch) -> None:
    """A quarantine sibling with no catalog row carries no lifecycle_state, so
    the census route's own 400 is the signal. It must not defer forever."""
    refusal = VectorServiceError(
        "POST /v1/vectors/manifest-less-census -> HTTP 400: collection quarantine-x is a quarantine collection",
        code=400,
    )
    client = _FakeVectorClient(
        [{"name": "knowledge__a"}, {"name": "quarantine-x"}],
        {"knowledge__a": _page()},
        census_error={"quarantine-x": refusal},
    )
    _patch_client(monkeypatch, client)
    reading = _default_census()
    assert [c.collection for c in reading.collections] == ["knowledge__a"]
    assert reading.quarantine_skipped == 1


def test_default_census_a_400_that_is_not_quarantine_defers(monkeypatch) -> None:
    client = _FakeVectorClient(
        [{"name": "knowledge__a"}],
        census_error={"knowledge__a": VectorServiceError("bad request", code=400)},
    )
    _patch_client(monkeypatch, client)
    with pytest.raises(CensusUnavailable, match="census request failed"):
        _default_census()


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
        [{"name": "knowledge__a"}],
        census_error={"knowledge__a": VectorServiceError("nope", code=404)},
    )
    _patch_client(monkeypatch, client)
    with pytest.raises(CensusUnavailable, match="predates the manifest-less-census route"):
        _default_census()


def test_default_census_does_not_swallow_a_bug_in_reading_the_listing(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeVectorClient([{"count": 3}]))  # a row with no "name"
    with pytest.raises(KeyError):
        _default_census()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({}, id="empty-answer"),
        pytest.param({"totals": {}, "scope_chunk_total": 10}, id="empty-totals"),
        pytest.param({"totals": _ALL_BUCKETS}, id="no-scope-chunk-total"),
        pytest.param(
            {"totals": {k: v for k, v in _ALL_BUCKETS.items() if k != "legacy-unmanifested"}, "scope_chunk_total": 10},
            id="missing-legacy-key",
        ),
        pytest.param(
            {"totals": {k: v for k, v in _ALL_BUCKETS.items() if k != "unclassified"}, "scope_chunk_total": 10},
            id="missing-unclassified-key",
        ),
        pytest.param(None, id="not-a-dict"),
    ],
)
def test_default_census_fails_closed_on_an_answer_without_the_positive_signal(monkeypatch, answer) -> None:
    """A census that returns nothing is not a clean census. Before this was
    pinned, an empty totals dict read as zero legacy chunks and verify()
    recorded a completion over an answer that examined nothing."""
    _patch_client(monkeypatch, _FakeVectorClient([{"name": "knowledge__a"}], {"knowledge__a": answer}))
    with pytest.raises(CensusUnavailable, match="not treating it as clean"):
        _default_census()


def test_an_unusable_answer_leaves_no_record(monkeypatch) -> None:
    """End to end through the real runner: fail-closed parse => no completion."""
    _patch_client(monkeypatch, _FakeVectorClient([{"name": "knowledge__a"}], {"knowledge__a": {}}))
    tenant = FakeTenant({})
    ledger = InMemoryCompletionLedger()
    rung = Rdr192ManifestBackfillRung(
        backfill_fn=tenant.backfill, record_fn=lambda: None,
        installed_version_fn=lambda: _VERSION, memo=tenant.memo, lock_factory=tenant.lock,
    )
    report = LadderRunner(LadderRegistry((rung,)), ledger).run()
    assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
    assert RUNG_NAME not in ledger.verified_rungs()


def test_default_census_refuses_an_empty_scope_over_a_listed_collection(monkeypatch) -> None:
    """Listing says the collection holds chunks; the census scope says zero:
    it read a different tenant or collection."""
    client = _FakeVectorClient(
        [{"name": "knowledge__a", "stored_count": 5}],
        {"knowledge__a": {"totals": dict(_ALL_BUCKETS), "scope_chunk_total": 0}},
    )
    _patch_client(monkeypatch, client)
    with pytest.raises(CensusUnavailable, match="scope"):
        _default_census()


def test_default_census_empty_listing_over_an_empty_catalog_is_a_clean_tenant(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeVectorClient([]))
    _patch_catalog_chunk_count(monkeypatch, 0)
    reading = _default_census()
    assert reading.collections == () and reading.clean


def test_default_census_empty_listing_over_a_populated_catalog_defers(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeVectorClient([]))
    _patch_catalog_chunk_count(monkeypatch, 12)
    with pytest.raises(CensusUnavailable, match="12 manifest row"):
        _default_census()


def test_default_census_empty_listing_with_an_unreadable_catalog_defers(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeVectorClient([]))
    _patch_catalog_chunk_count(monkeypatch, ConnectionError("catalog down"))
    with pytest.raises(CensusUnavailable, match="could not confirm"):
        _default_census()
