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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nexus.catalog.factory as factory
import nexus.db as nexus_db
from nexus.db.http_vector_client import VectorServiceError
from nexus.upgrade_ladder.completion import CompletionRecord
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung
from nexus.upgrade_ladder.registry import LadderRegistry, default_registry
from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import (
    RUNG_NAME,
    RETRY_ENV,
    BackfillIncompleteError,
    BackfillOutcome,
    CensusReading,
    CensusUnavailable,
    CollectionReading,
    Rdr192ManifestBackfillRung,
    _cross_process_lock,
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
        self.now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        self.skipped: dict[str, dict[str, int]] = {}

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

    def backfill(self, collection: str):
        self.backfilled.append(collection)
        if collection in self.backfill_errors:
            raise self.backfill_errors[collection]
        before = self.legacy.get(collection, 0)
        healed = before if self.heal_per_pass is None else min(before, self.heal_per_pass)
        self.legacy[collection] = before - healed
        if collection in self.skipped:
            return BackfillOutcome(chunks_written=healed, skipped=self.skipped[collection])
        return healed

    def later(self, **delta) -> None:
        self.now += timedelta(**delta)

    def rung(self, **kw) -> Rdr192ManifestBackfillRung:
        kw.setdefault("record_fn", lambda: self.record)
        kw.setdefault("installed_version_fn", lambda: _VERSION)
        kw.setdefault("memo", self.memo)
        kw.setdefault("lock_factory", self.lock)
        kw.setdefault("now_fn", lambda: self.now)
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
    """The heavy work (backfill scans, re-censuses) must not repeat for a
    residual that has not changed, even after the time gate opens."""
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    backfills_after_first = len(tenant.backfilled)
    censuses_after_first = tenant.censuses

    tenant.later(hours=25)
    second = rung.converge(_Reporter())
    assert second.outcome is ConvergeOutcome.DEFERRED
    assert "unchanged since the last attempt" in second.detail
    assert len(tenant.backfilled) == backfills_after_first
    assert tenant.censuses == censuses_after_first + 1  # one fresh census to compare, nothing more


def test_within_the_gate_an_unchanged_residual_is_not_even_censused() -> None:
    """A stuck tenant must not pay a census per collection at every session
    start."""
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    censuses_after_first = tenant.censuses
    tenant.legacy["knowledge__b"] = 1  # a change inside the window is not looked at

    tenant.later(hours=23)
    result = rung.converge(_Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "not re-examined" in result.detail and RETRY_ENV in result.detail
    assert tenant.censuses == censuses_after_first
    assert tenant.backfilled.count("knowledge__b") == 0


def test_after_the_gate_a_still_unchanged_residual_renews_it() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    tenant.later(hours=25)
    rung.converge(_Reporter())  # census, unchanged, renews the timestamp
    censuses = tenant.censuses
    tenant.later(hours=1)
    rung.converge(_Reporter())
    assert tenant.censuses == censuses, "the renewed note gates the next hour too"


def test_the_retry_env_forces_a_full_retry_inside_the_gate(monkeypatch) -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    backfills = len(tenant.backfilled)
    tenant.heal_per_pass = None  # the operator re-put the note
    monkeypatch.setenv(RETRY_ENV, "1")
    assert rung.converge(_Reporter()).outcome is ConvergeOutcome.COMPLETED
    assert len(tenant.backfilled) > backfills


def test_a_changed_residual_is_retried_once_the_gate_opens() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    backfills_after_first = len(tenant.backfilled)
    tenant.legacy["knowledge__b"] = 1  # a new legacy chunk elsewhere
    tenant.later(hours=25)
    rung.converge(_Reporter())
    assert len(tenant.backfilled) > backfills_after_first


def test_a_new_package_version_retries_inside_the_gate() -> None:
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
    tenant.later(hours=25)
    assert rung.converge(_Reporter()).outcome is ConvergeOutcome.COMPLETED
    assert tenant.memo.note is None


def test_a_backfill_error_is_never_remembered_and_is_retried_next_session() -> None:
    """A transient 503 must not be memoized as an unhealable residual (verify
    round N1): the healable chunk would stay hidden until the next package
    version."""
    tenant = FakeTenant({"knowledge__a": 1})
    tenant.backfill_errors["knowledge__a"] = RuntimeError("503 from the engine")
    rung = tenant.rung()
    first = rung.converge(_Reporter())
    assert first.outcome is ConvergeOutcome.DEFERRED
    assert tenant.memo.note is None, "an errored attempt leaves no memo"
    assert "usually transient" in first.detail and "nx store put" not in first.detail

    del tenant.backfill_errors["knowledge__a"]  # the outage is over
    second = rung.converge(_Reporter())  # no clock advance: nothing gates it
    assert second.outcome is ConvergeOutcome.COMPLETED
    assert tenant.legacy["knowledge__a"] == 0
    assert tenant.backfilled == ["knowledge__a", "knowledge__a"]


def test_a_skipped_document_is_named_in_the_deferred_detail() -> None:
    """The BackfillResult skip counters must reach the operator, not only a
    structlog line."""
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    tenant.skipped["knowledge__a"] = {"zero_chunks": 1, "chunk_count_mismatch": 2}
    result = tenant.rung().converge(_Reporter())
    assert "The backfill skipped documents: knowledge__a: chunk_count_mismatch=2, zero_chunks=1." in result.detail
    assert "chunk_count_mismatch=2" in tenant.memo.note["detail"]


def test_a_lock_that_cannot_be_taken_defers_and_does_not_raise() -> None:
    """An unwritable config dir must not fail `nx upgrade` (verify round N3)."""

    @contextlib.contextmanager
    def unwritable():
        raise PermissionError(13, "Permission denied", "/root/.config/nexus")
        yield True  # pragma: no cover

    tenant = FakeTenant({"knowledge__a": 1})
    result = tenant.rung(lock_factory=unwritable).converge(_Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "could not take its lock" in result.detail
    assert tenant.censuses == 0


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
    legacy = _remedy(CensusReading((CollectionReading("knowledge__c", 2, 0),)))
    unclassified = _remedy(CensusReading((CollectionReading("c", 0, 3),)))
    assert "cannot heal them, and no verb does" in legacy
    assert "nx store put - --collection knowledge__c --title" in legacy, "names the collection to put into"
    assert "nx t3 census-manifest-less --collection knowledge__c" in legacy
    assert "owner document's title" in legacy
    assert "hidden from `nx store get`" in legacy and "your own copy" in legacy
    assert "unclassified:" not in legacy
    assert "no verb heals" in unclassified and "maintainers" in unclassified
    assert "nx store put" not in unclassified
    for text in (legacy, unclassified):
        assert RETRY_ENV in text and "no prior completion record" in text


# ── verify: presence of the positive signal, from a fresh read ───────────────


def test_verify_true_only_on_a_fresh_zero_census() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    rung = tenant.rung()
    assert rung.verify() is False
    rung.converge(_Reporter())
    assert rung.verify() is True


def test_verify_re_reads_a_census_that_is_no_longer_fresh() -> None:
    tenant = FakeTenant({"knowledge__a": 1})
    rung = tenant.rung()
    rung.converge(_Reporter())
    tenant.legacy["knowledge__a"] = 1  # a legacy row reappears after converge
    tenant.later(minutes=5)
    assert rung.verify() is False


def test_verify_reuses_the_clean_census_converge_just_took() -> None:
    """A clean tenant pays for one census, not two (verify round N4)."""
    tenant = FakeTenant({"knowledge__a": 0})
    rung = tenant.rung()
    rung.converge(_Reporter())
    assert tenant.censuses == 1
    assert rung.verify() is True
    assert tenant.censuses == 1
    assert rung.verify() is True, "a second verify is a fresh read"
    assert tenant.censuses == 2


def test_verify_never_reuses_a_dirty_census() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    rung.converge(_Reporter())
    censuses = tenant.censuses
    assert rung.verify() is False
    assert tenant.censuses == censuses + 1


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


def test_walk_on_a_clean_tenant_takes_exactly_one_census() -> None:
    tenant = FakeTenant({"knowledge__a": 0})
    ledger = InMemoryCompletionLedger()
    report = _walk(tenant, ledger)
    assert [r.outcome for r in report.runs] == [RungOutcome.RECORDED]
    assert tenant.censuses == 1


def test_a_census_outage_inside_verify_defers_instead_of_failing_the_walk() -> None:
    """converge's census was clean; verify's own (fresh) census hits an outage.
    That is unknown, not a refusal: DEFERRED, nothing recorded, and the walk is
    not hard-failed (so nx upgrade runs its later steps)."""
    tenant = FakeTenant({"knowledge__a": 0})
    calls = {"n": 0}

    def flaky_census():
        calls["n"] += 1
        if calls["n"] > 1:
            raise CensusUnavailable("engine dropped")
        return tenant.census()

    clock = {"t": tenant.now}

    def advancing_now():
        clock["t"] += timedelta(minutes=2)  # every look at the clock ages the census past the reuse window
        return clock["t"]

    ledger = InMemoryCompletionLedger()
    rung = Rdr192ManifestBackfillRung(
        census_fn=flaky_census, backfill_fn=tenant.backfill, record_fn=lambda: None,
        installed_version_fn=lambda: _VERSION, memo=tenant.memo, lock_factory=tenant.lock,
        now_fn=advancing_now,
    )
    report = LadderRunner(LadderRegistry((rung,)), ledger).run()
    assert [r.outcome for r in report.runs] == [RungOutcome.DEFERRED]
    assert not report.hard_failed
    assert RUNG_NAME not in ledger.verified_rungs()
    assert "engine dropped" in report.runs[0].detail


def test_a_real_residual_in_verify_is_still_a_plain_refusal() -> None:
    tenant = FakeTenant({"knowledge__a": 1}, heal_per_pass=0)
    rung = tenant.rung()
    assert rung.verify() is False
    assert rung.verify_deferred() is False


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
    tenant.later(hours=25)
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


# ── the real cross-process lock ──────────────────────────────────────────────


def test_the_real_lock_excludes_a_second_holder_and_frees_on_release() -> None:
    """flock is per open file description, so two acquisitions in one process
    contend exactly as two processes do."""
    with _cross_process_lock() as first:
        assert first is True
        with _cross_process_lock() as second:
            assert second is False, "a concurrent session start must not stack a backfill"
    with _cross_process_lock() as again:
        assert again is True, "the lock is released when the holder leaves"


def test_default_census_maps_an_unresolvable_endpoint_to_a_clean_message(monkeypatch) -> None:
    """The endpoint error's own text explains a retired Chroma path; that is
    noise in `nx upgrade` output (verify round N7)."""
    from nexus.db.service_endpoint import ServiceEndpointUnresolvableError

    def unresolvable(**kw):
        raise ServiceEndpointUnresolvableError(
            "nexus-service endpoint is not resolvable: ... the direct Chroma serving paths are retired ..."
        )

    monkeypatch.setattr(nexus_db, "make_t3", unresolvable)
    with pytest.raises(CensusUnavailable) as excinfo:
        _default_census()
    message = str(excinfo.value)
    assert "nx daemon service start" in message
    assert "chroma" not in message.lower()


def test_the_memo_closes_the_store_it_opens(monkeypatch) -> None:
    import nexus.db.t2.http_memory_store as memory_module
    from nexus.upgrade_ladder.rungs.rdr192_manifest_backfill import T2ResidualMemo

    events: list[str] = []

    class FakeStore:
        def get(self, **kw):
            events.append("get")
            return {"content": '{"detail": "x"}'}

        def put(self, *a, **kw):
            events.append("put")

        def delete(self, **kw):
            events.append("delete")

        def close(self):
            events.append("close")

    monkeypatch.setattr(memory_module, "HttpMemoryStore", FakeStore)
    memo = T2ResidualMemo()
    assert memo.load() == {"detail": "x"}
    memo.save({"a": 1})
    memo.clear()
    assert events == ["get", "close", "put", "close", "delete", "close"]
