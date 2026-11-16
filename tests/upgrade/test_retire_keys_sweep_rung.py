# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.43 (RDR-236 Phase 4 Step 3): the ``retire-keys-sweep`` rung.

Each test here is a branch on the engine's answer to the dry run or the apply job (the engine's own definitions are
pinned by ``ChunkKeySweepIntegrationTest`` and, through the rung, by the one journey of
``tests/integration/test_retire_keys_sweep_rung_engine.py``), so a stand-in T3 client that answers as the engine documents is right
for them. The rung never accepts a residue, never sweeps a managed service unattended, and never calls a sweep
converged on a read it could not make.
"""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import ChunkKeysApplyResult, ChunkKeysDryRunTimeout, VectorServiceError
from nexus.upgrade_ladder import deadline as dl
from nexus.upgrade_ladder.protocol import ConvergeOutcome
from nexus.upgrade_ladder.registry import (
    HARD_EDGES,
    RUNG_MANIFEST_SECTION_LABELS,
    RUNG_MANIFEST_SPAN_FILL,
    RUNG_NOTE_TAGS_ARRAY,
    RUNG_ORDER,
    RUNG_RDR192_MANIFEST_BACKFILL,
    RUNG_RETIRE_KEYS_SWEEP,
    default_registry,
)
from nexus.upgrade_ladder.rungs.retire_keys_sweep import (
    RetireKeysSweepRung,
    UnreadableReport,
    read_report,
)
from nexus.upgrade_ladder.unattended import bounded_reads, cheap_detect, unattended_run

TENANT = "tenant-a"


def report(*, retired: int = 0, sweepable: int | None = None, blocked: int = 0, wrong_width: int = 0,
           blocking: dict[str, int] | None = None, tenant: str = TENANT, collections: list[dict] | None = None) -> dict:
    """A dry run's answer, in the shape ``chunk_keys_sweep`` documents."""
    blocking = blocking or {}
    return {
        "tenant": tenant, "dry_run": True,
        "collections": collections or [],
        "totals": {"retired_key_rows": retired, "sweepable_rows": retired if sweepable is None else sweepable,
                   "blocked_rows": blocked, "wrong_width_rows": wrong_width},
        "preconditions": {"legacy_label_rows": 0, "keyless_notes": 0, "null_span_rows": 0, **blocking},
        "blocking": sorted(blocking), "applicable": not blocking,
    }


def applied(*, swept: int, left: int = 0, done: bool = True, **extra) -> ChunkKeysApplyResult:
    return ChunkKeysApplyResult(
        {"tenant": TENANT, "rows_swept": swept, "remaining_rows": left, "done": done, "blocked_collections": [],
         **extra}, "job-1")


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class T3:
    """The two calls the rung makes. Answers are scripted; every call is recorded."""

    def __init__(self, *dry_runs, applies=()) -> None:
        self._dry = list(dry_runs)
        self._apply = list(applies)
        self.dry_run_calls: list[dict] = []
        self.apply_calls: list[dict] = []
        self.closed = 0

    def chunk_keys_dry_run(self, **kw):
        self.dry_run_calls.append(kw)
        answer = self._dry.pop(0) if len(self._dry) > 1 else self._dry[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def chunk_keys_apply(self, **kw):
        self.apply_calls.append(kw)
        kw["progress"]({"event": "started", "job_id": "job-1"})
        answer = self._apply.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.closed += 1


def rung(t3: T3, *, managed: bool = False, record=None, **kw) -> RetireKeysSweepRung:
    return RetireKeysSweepRung(client_factory=lambda: t3, managed_fn=lambda: managed, record_fn=lambda: record, **kw)


class Recorded:
    verified_at = "2026-11-16T00:00:00Z"


# ── the report ───────────────────────────────────────────────────────────────


def test_a_report_without_counts_is_unreadable_not_zero() -> None:
    for bad in (None, {}, {"totals": None}, {"totals": {"retired_key_rows": None, "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": "3", "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": True, "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": 1, "sweepable_rows": 1}, "blocking": "legacy_label_rows"}):
        with pytest.raises(UnreadableReport):
            read_report(bad)
    ok = read_report(report(retired=4, blocking={"keyless_notes": 2}, collections=[
        {"collection": "c", "blocked": "legacy_unmanifested"}, {"collection": "d", "blocked": None}]))
    assert (ok.retired_key_rows, ok.blocking, ok.preconditions["keyless_notes"], ok.blocked_collections) == (
        4, ("keyless_notes",), 2, ("c (legacy_unmanifested)",))


# ── registry ─────────────────────────────────────────────────────────────────


def test_the_rung_is_registered_last_behind_the_four_rungs_that_clear_its_preconditions() -> None:
    assert RUNG_ORDER[-1] == RUNG_RETIRE_KEYS_SWEEP == "retire-keys-sweep"
    registry = default_registry()
    assert [r.name for r in registry][-1] == RUNG_RETIRE_KEYS_SWEEP
    assert registry.prerequisites(RUNG_RETIRE_KEYS_SWEEP) == frozenset({
        RUNG_RDR192_MANIFEST_BACKFILL, RUNG_NOTE_TAGS_ARRAY, RUNG_MANIFEST_SECTION_LABELS, RUNG_MANIFEST_SPAN_FILL})
    assert {(b, a) for b, a in HARD_EDGES if a == RUNG_RETIRE_KEYS_SWEEP} == {
        (RUNG_RDR192_MANIFEST_BACKFILL, RUNG_RETIRE_KEYS_SWEEP), (RUNG_NOTE_TAGS_ARRAY, RUNG_RETIRE_KEYS_SWEEP),
        (RUNG_MANIFEST_SECTION_LABELS, RUNG_RETIRE_KEYS_SWEEP), (RUNG_MANIFEST_SPAN_FILL, RUNG_RETIRE_KEYS_SWEEP)}


# ── detect ───────────────────────────────────────────────────────────────────


def test_detect_is_converged_on_a_dry_run_of_zero_and_pending_with_the_rows_and_what_blocks_them() -> None:
    t3 = T3(report(retired=0))
    status = rung(t3).detect()
    assert (status.applicable, status.converged) == (True, True)
    assert [c for c in t3.dry_run_calls] == [{"accept": {}, "measure_batch": False}], "read-only: nothing accepted, no batch timing"
    assert t3.apply_calls == [] and t3.closed == 1

    pending = rung(T3(report(retired=7, sweepable=7))).detect()
    assert pending.pending and "7 chunk row(s) of tenant tenant-a hold retired metadata keys (7 to sweep)" in pending.pending_detail

    blocked = rung(T3(report(retired=7, blocking={"keyless_notes": 3, "null_span_rows": 5}))).detect()
    assert blocked.pending and "keyless_notes=3, null_span_rows=5 stand in the way" in blocked.pending_detail


def test_detect_on_an_engine_without_the_routes_is_not_applicable_and_any_other_failure_propagates() -> None:
    status = rung(T3(VectorServiceError("no route", code=404))).detect()
    assert (status.applicable, status.converged) == (False, False) and "does not carry" in status.pending_detail
    with pytest.raises(VectorServiceError):
        rung(T3(VectorServiceError("refused", code=403, reason="scope"))).detect()
    with pytest.raises(VectorServiceError):
        rung(T3(VectorServiceError("away", code=503))).detect()


def test_detect_under_auto_against_a_managed_service_reads_nothing_and_answers_from_the_record_or_says_not_applicable() -> None:
    t3 = T3(report(retired=99))
    with unattended_run():
        none = rung(t3, managed=True).detect()
        recorded = rung(t3, managed=True, record=Recorded()).detect()
    assert (none.applicable, none.converged) == (False, False) and "operator verb" in none.pending_detail
    assert (recorded.applicable, recorded.converged, recorded.from_record) == (True, True, Recorded.verified_at)
    assert t3.dry_run_calls == [], "no dry run, no client call"


def test_nx_doctors_bounded_probe_reads_nothing_on_any_engine_and_an_interactive_upgrade_with_no_record_defers_the_read() -> None:
    t3 = T3(report(retired=99))
    with bounded_reads():
        assert rung(t3).detect().applicable is False
    with cheap_detect():
        status = rung(t3).detect()
    assert status.pending and "not checked before this run" in status.pending_detail
    assert t3.dry_run_calls == []


def test_an_unattended_run_against_a_local_engine_does_the_dry_run() -> None:
    t3 = T3(report(retired=0))
    with unattended_run():
        assert rung(t3, managed=False).detect().converged
    assert len(t3.dry_run_calls) == 1


# ── converge ─────────────────────────────────────────────────────────────────


def test_a_blocking_precondition_defers_writes_nothing_and_names_each_count_with_the_operator_command() -> None:
    t3 = T3(report(retired=9, blocking={"legacy_label_rows": 49378, "keyless_notes": 17}))
    result = rung(t3).converge(Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED and result.deferred
    assert t3.apply_calls == [], "no apply, so no row can change"
    d = result.detail
    assert "legacy_label_rows=49378" in d and "keyless_notes=17" in d
    assert "nx t3 retire-keys --tenant tenant-a --apply --accept keyless_notes=17 --accept legacy_label_rows=49378" in d
    assert "never accepts a residue for you" in d


def test_a_dry_run_that_sweeps_nothing_defers_and_names_blocked_collections_and_wrong_width_rows() -> None:
    result = rung(T3(report(retired=6, sweepable=0, blocked=4, wrong_width=2, collections=[
        {"collection": "docs__x__m__v1", "blocked": "legacy_unmanifested"}]))).converge(Reporter())
    assert result.deferred and "docs__x__m__v1 (legacy_unmanifested)" in result.detail and "2 with a wrong-width" in result.detail


def test_converge_completes_on_zero_rows_with_no_apply_and_hands_the_reading_to_verify() -> None:
    t3 = T3(report(retired=0))
    r = rung(t3)
    assert r.converge(Reporter()).completed
    assert r.verify() is True
    assert len(t3.dry_run_calls) == 1, "verify took the clean reading converge made"
    assert t3.apply_calls == []


def test_the_apply_accepts_nothing_and_a_finished_sweep_is_verified_by_a_fresh_dry_run() -> None:
    t3 = T3(report(retired=5), report(retired=0), applies=[applied(swept=5)])
    r = rung(t3)
    reporter = Reporter()
    result = r.converge(reporter)
    assert result.completed and "swept 5 row(s)" in result.detail
    assert [c["accept"] for c in t3.apply_calls] == [{}], "the engine counts every precondition itself; nothing is accepted"
    assert "tenant" not in t3.apply_calls[0], "this install's own tenant"
    assert ("retire_keys_sweep_batch", {"swept": 5, "remaining": 0, "done": True}) in reporter.events
    assert ("retire_keys_sweep_job", {"job_id": "job-1", "phase": "started"}) in reporter.events
    assert r.verify() is True and len(t3.dry_run_calls) == 2, "the confirming dry run is a fresh read, not converge's"
    assert "5 swept this run" in r.record_detail()


def test_a_wait_that_ends_leaves_the_job_running_and_resumable_then_the_next_call_joins_it() -> None:
    gone = ChunkKeysDryRunTimeout("job-1", 600.0, "apply")
    running = VectorServiceError("already running", code=409, reason="sweep_job_running",
                                 engine_body={"kind": "apply", "job_id": "job-1"})
    t3 = T3(report(retired=8), running, report(retired=0),
            applies=[gone, applied(swept=8, left=0)])
    r = rung(t3, apply_wait_s=42.0)
    first = r.converge(Reporter())
    assert first.outcome is ConvergeOutcome.RESUMABLE and "still running" in first.detail
    assert t3.apply_calls[0]["max_wait_s"] == 42.0
    second = r.converge(Reporter())
    assert second.completed, "the dry run found the apply running and the rung joined it with the same request"
    assert [c["accept"] for c in t3.apply_calls] == [{}, {}]
    assert r.verify() is True


def test_an_apply_that_is_not_done_is_resumable_and_one_that_stops_making_progress_defers() -> None:
    more = rung(T3(report(retired=8), applies=[applied(swept=3, left=5, done=False)]))
    assert more.converge(Reporter()).outcome is ConvergeOutcome.RESUMABLE
    stuck = rung(T3(report(retired=8), applies=[applied(swept=0, left=8, done=False, stalled=True)]))
    result = stuck.converge(Reporter())
    assert result.deferred and "8 left" in result.detail
    cancelled = rung(T3(report(retired=8), applies=[applied(swept=2, left=6, done=False, stopped="cancelled")]))
    assert cancelled.converge(Reporter()).deferred


def test_a_sweep_that_leaves_a_blocked_collection_alone_defers_rather_than_failing_verify() -> None:
    body = applied(swept=3)
    body.body["blocked_collections"] = [{"collection": "docs__y__m__v1", "blocked": "legacy_unmanifested"}]
    result = rung(T3(report(retired=5), applies=[body])).converge(Reporter())
    assert result.deferred and "docs__y__m__v1 (legacy_unmanifested)" in result.detail


def test_a_residue_that_grew_since_the_dry_run_is_refused_by_the_engine_and_defers() -> None:
    refused = VectorServiceError(
        "preconditions", code=409, reason="sweep_preconditions_not_met",
        engine_body={"blocking": ["keyless_notes"], "report": {"tenant": TENANT, "preconditions": {"keyless_notes": 4}}})
    result = rung(T3(report(retired=5), applies=[refused])).converge(Reporter())
    assert result.deferred and "keyless_notes=4" in result.detail and "--accept keyless_notes=4" in result.detail


def test_any_other_engine_refusal_during_the_sweep_fails_the_call_and_the_rows_swept_are_kept() -> None:
    boom = VectorServiceError("sweep failed", code=500, reason="sweep_failed", engine_body={"rows_swept": 2})
    with pytest.raises(VectorServiceError):
        rung(T3(report(retired=5), applies=[boom])).converge(Reporter())


def test_converge_under_auto_against_a_managed_service_is_deferred_before_any_client_is_built() -> None:
    def no_client():
        raise AssertionError("an unattended managed run built a client")

    r = RetireKeysSweepRung(client_factory=no_client, managed_fn=lambda: True, record_fn=lambda: None)
    with unattended_run():
        result = r.converge(Reporter())
    assert result.deferred and "operator verb" in result.detail


def test_converge_past_the_auto_time_limit_defers_without_a_client() -> None:
    now = [0.0]

    def no_client():
        raise AssertionError("a call past the time limit built a client")

    r = RetireKeysSweepRung(client_factory=no_client, managed_fn=lambda: False, record_fn=lambda: None)
    with dl.bound(dl.Deadline(60.0, clock=lambda: now[0])):
        now[0] = 61.0
        result = r.converge(Reporter())
    assert result.deferred and "time limit" in result.detail


def test_a_wait_is_cut_to_what_is_left_of_the_auto_time_limit() -> None:
    now = [0.0]
    gone = ChunkKeysDryRunTimeout("job-1", 90.0, "apply")
    t3 = T3(report(retired=8), applies=[gone])
    with dl.bound(dl.Deadline(120.0, clock=lambda: now[0])):
        now[0] = 30.0
        rung(t3, apply_wait_s=600.0).converge(Reporter())
    assert t3.apply_calls[0]["max_wait_s"] == 90.0 and t3.dry_run_calls[0]["max_wait_s"] == 90.0


def test_a_count_that_outlasts_its_wait_defers_and_the_next_run_joins_it() -> None:
    t3 = T3(ChunkKeysDryRunTimeout("job-0", 900.0, "dry run"))
    result = rung(t3).converge(Reporter())
    assert result.deferred and t3.apply_calls == []


def test_detects_reading_is_the_one_converge_acts_on() -> None:
    t3 = T3(report(retired=5), applies=[applied(swept=5)])
    r = rung(t3)
    assert r.detect().pending
    assert r.converge(Reporter()).completed
    assert len(t3.dry_run_calls) == 1, "converge did not count again"


# ── verify: positive signal only ─────────────────────────────────────────────


@pytest.mark.parametrize("answer", [
    VectorServiceError("refused", code=403, reason="scope"),
    {"totals": {}},
    {"nothing": "here"},
    report(retired=3),
], ids=["error", "no-counts", "unknown-shape", "rows-remain"])
def test_verify_is_false_with_a_detail_unless_a_dry_run_succeeded_and_counted_zero(answer) -> None:
    r = rung(T3(answer))
    assert r.verify() is False
    assert r.verify_detail() and r.verify_deferred() is False and r.record_detail() == ""


def test_verify_reraises_an_engine_that_is_away_so_the_runner_waits_for_it() -> None:
    with pytest.raises(VectorServiceError):
        rung(T3(VectorServiceError("bad gateway", code=502))).verify()


def test_verify_under_auto_against_a_managed_service_is_a_deferral_not_a_read() -> None:
    t3 = T3(report(retired=0))
    r = rung(t3, managed=True)
    with unattended_run():
        assert r.verify() is False
    assert r.verify_deferred() and t3.dry_run_calls == []
