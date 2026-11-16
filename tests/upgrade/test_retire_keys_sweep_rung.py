# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.43 (RDR-236 Phase 4 Step 3): the ``retire-keys-sweep`` rung.

Each test here is a branch on the engine's answer to the dry run or the apply job (the engine's own definitions are
pinned by ``ChunkKeySweepIntegrationTest`` and, through the rung, by the one journey of
``tests/integration/test_retire_keys_sweep_rung_engine.py``), so a stand-in T3 client that answers as the engine documents
is right for them. The rung never accepts a residue, never sweeps a managed service unattended, never calls a sweep
converged on a read it could not make, and never turns a condition of the engine (a refused credential, a job another
request started, a lost job) into a failed upgrade.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nexus.db.http_vector_client import (
    ChunkKeysApplyResult,
    ChunkKeysDryRunTimeout,
    ChunkKeysJobLeftRunning,
    VectorServiceError,
)
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
    LadderRegistry,
    default_registry,
)
from nexus.upgrade_ladder.runner import LadderRunner, RungOutcome
from nexus.upgrade_ladder.rungs.retire_keys_sweep import (
    ACCEPT_EFFECTS,
    RetireKeysSweepRung,
    UnreadableReport,
    condition_of,
    read_report,
)
from nexus.upgrade_ladder.unattended import bounded_reads, cheap_detect, unattended_run
from tests.upgrade.conftest import InMemoryCompletionLedger

TENANT = "tenant-a"
#: The residues the engine lists as acceptable (``ChunkKeySweep.ACCEPTABLE``).
ACCEPTABLE = ["legacy_label_rows", "keyless_notes", "keyless_ttl_rows", "homeless_documents", "null_span_rows",
              "generated_nulls"]


def report(*, retired: int = 0, sweepable: int | None = None, blocked: int = 0, wrong_width: int = 0,
           blocking: dict[str, int] | None = None, tenant: str = TENANT, collections: list[dict] | None = None,
           acceptable: list[str] | None = None) -> dict:
    """A dry run's answer, in the shape ``chunk_keys_sweep`` documents."""
    blocking = blocking or {}
    return {
        "tenant": tenant, "dry_run": True,
        "collections": collections or [],
        "totals": {"retired_key_rows": retired, "sweepable_rows": retired if sweepable is None else sweepable,
                   "blocked_rows": blocked, "wrong_width_rows": wrong_width},
        "preconditions": {"legacy_label_rows": 0, "keyless_notes": 0, "null_span_rows": 0, **blocking},
        "acceptable_residues": ACCEPTABLE if acceptable is None else acceptable,
        "blocking": sorted(blocking), "applicable": not blocking,
    }


def applied(*, swept: int, left: int = 0, done: bool = True, **extra) -> ChunkKeysApplyResult:
    return ChunkKeysApplyResult(
        {"tenant": TENANT, "rows_swept": swept, "remaining_rows": left, "done": done, "blocked_collections": [],
         **extra}, "job-1")


def refusal(code: int, reason: str | None = None, **body) -> VectorServiceError:
    return VectorServiceError(f"engine said {code} {reason}", code=code, reason=reason, engine_body=body or None)


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


class Notes:
    """An in-memory measurement note."""

    def __init__(self) -> None:
        self.note: dict | None = None
        self.saves = 0

    def load(self, *, strict: bool = False):
        return self.note

    def save(self, note: dict) -> None:
        self.note = dict(note)
        self.saves += 1

    def clear(self) -> None:
        self.note = None


def rung(t3: T3, *, managed: bool = False, record=None, notes: Notes | None = None, **kw) -> RetireKeysSweepRung:
    return RetireKeysSweepRung(
        client_factory=lambda: t3, managed_fn=lambda: managed, record_fn=lambda: record, state_store=notes, **kw)


class Recorded:
    verified_at = "2026-11-16T00:00:00Z"


# ── the report ───────────────────────────────────────────────────────────────


def test_a_report_without_counts_is_unreadable_not_zero() -> None:
    for bad in (None, {}, {"totals": None}, {"totals": {"retired_key_rows": None, "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": "3", "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": True, "sweepable_rows": 0}},
                {"totals": {"retired_key_rows": 1, "sweepable_rows": 1}, "blocking": "legacy_label_rows"},
                {"totals": {"retired_key_rows": 1, "sweepable_rows": 1}, "acceptable_residues": "keyless_notes"}):
        with pytest.raises(UnreadableReport):
            read_report(bad)
    ok = read_report(report(retired=4, blocking={"keyless_notes": 2}, collections=[
        {"collection": "c", "blocked": "legacy_unmanifested"}, {"collection": "d", "blocked": None}]))
    assert (ok.retired_key_rows, ok.blocking, ok.preconditions["keyless_notes"], ok.blocked_collections) == (
        4, ("keyless_notes",), 2, ("c (legacy_unmanifested)",))
    assert ok.acceptable == tuple(ACCEPTABLE)


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


# ── the engine's conditions ──────────────────────────────────────────────────


@pytest.mark.parametrize("exc, kind", [
    (refusal(403, None), "credential"),
    (refusal(403, "tenant_mismatch"), "credential"),
    (refusal(409, "sweep_job_running", kind="apply", job_id="j9"), "job_running"),
    (refusal(409, "sweep_job_running", kind="dry_run", job_id="j9"), "job_running"),
    (refusal(503, "sweep_jobs_busy"), "busy"),
    (refusal(404, "sweep_job_not_found"), "job_lost"),
    (refusal(409, "sweep_job_stopped"), "job_lost"),
    (refusal(404, None), "no_route"),
    (ChunkKeysDryRunTimeout("j1", 600.0, "apply"), "waiting"),
    (ChunkKeysJobLeftRunning("j1", "apply"), "waiting"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_what_the_engine_says_that_is_not_a_failure_is_a_condition(exc, kind) -> None:
    cond = condition_of(exc)
    assert cond is not None and cond.kind == kind


@pytest.mark.parametrize("exc", [
    refusal(500, "sweep_failed"), refusal(503, None), refusal(502, None), refusal(422, "unknown_tenant"),
    refusal(404, "no_such_thing_the_rung_knows"),
], ids=lambda e: f"{e.code}-{e.reason}")
def test_any_other_engine_answer_is_a_failure_and_not_a_condition(exc) -> None:
    assert condition_of(exc) is None


def test_a_404_with_a_reason_is_never_an_engine_without_the_routes() -> None:
    """The verb reads 404 as 'no routes' only when it carries no reason (retire_keys.py); a lost job is a 404 too."""
    lost = condition_of(refusal(404, "sweep_job_not_found"))
    assert lost is not None and lost.kind == "job_lost" and "does not carry" not in lost.text
    assert condition_of(refusal(404, "some_other_reason")) is None
    absent = condition_of(refusal(404, None))
    assert absent is not None and absent.kind == "no_route" and "does not carry" in absent.text


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


def test_detect_reads_the_conditions_of_the_engine_as_not_applicable_or_pending_never_a_raise() -> None:
    credential = rung(T3(refusal(403, None))).detect()
    assert (credential.applicable, credential.converged) == (False, False)
    assert "tenant or root credential" in credential.pending_detail
    absent = rung(T3(refusal(404, None))).detect()
    assert (absent.applicable, absent.converged) == (False, False) and "does not carry" in absent.pending_detail
    for exc, word in ((refusal(409, "sweep_job_running", kind="apply", job_id="j9"), "j9"),
                      (refusal(503, "sweep_jobs_busy"), "as many sweep jobs"),
                      (ChunkKeysDryRunTimeout("j3", 900.0, "dry run"), "j3"),
                      (refusal(404, "sweep_job_not_found"), "lost")):
        status = rung(T3(exc)).detect()
        assert status.applicable and not status.converged and word in status.pending_detail, (exc, status)
        assert "does not carry" not in status.pending_detail, "a lost job is not an engine without the routes"


def test_detect_raises_on_an_engine_that_is_broken_or_away() -> None:
    for exc in (refusal(500, "sweep_failed"), refusal(503, None), refusal(502, None)):
        with pytest.raises(VectorServiceError):
            rung(T3(exc)).detect()
    with pytest.raises(UnreadableReport):
        rung(T3({"nothing": "here"})).detect()


def test_detect_under_auto_against_a_managed_service_reads_nothing_and_answers_from_the_record_or_says_not_applicable() -> None:
    t3 = T3(report(retired=99))
    with unattended_run():
        none = rung(t3, managed=True).detect()
        recorded = rung(t3, managed=True, record=Recorded()).detect()
    assert (none.applicable, none.converged) == (False, False) and "operator verb" in none.pending_detail
    assert (recorded.applicable, recorded.converged, recorded.from_record) == (True, True, Recorded.verified_at)
    assert t3.dry_run_calls == [], "no dry run, no client call"


def test_the_steady_state_trusts_the_record_so_an_interactive_upgrade_makes_no_dry_run() -> None:
    t3 = T3(report(retired=99))
    for ctx in (cheap_detect(), bounded_reads()):
        with ctx:
            status = rung(t3, record=Recorded()).detect()
        assert (status.applicable, status.converged, status.from_record) == (True, True, Recorded.verified_at)
    assert rung(t3, record=Recorded()).detect().converged, "and a plain interactive detect, such as `nx upgrade --dry-run`"
    assert t3.dry_run_calls == []


def test_nx_doctors_bounded_probe_reads_nothing_and_its_not_applicable_text_is_true_on_a_local_engine() -> None:
    t3 = T3(report(retired=99))
    with bounded_reads():
        status = rung(t3, managed=False).detect()
    assert status.applicable is False and t3.dry_run_calls == []
    assert "`nx doctor` does not run the retired-key dry run" in status.pending_detail
    assert "managed" not in status.pending_detail, "a local engine is not a managed service"
    assert "interactive `nx upgrade`" in status.pending_detail, "the shared remedy that makes doctor report 'not checked'"


def test_an_interactive_upgrade_with_no_record_defers_the_read_to_converge() -> None:
    t3 = T3(report(retired=99))
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


def test_a_blocking_precondition_defers_writes_nothing_and_says_what_accepting_each_residue_destroys() -> None:
    t3 = T3(report(retired=9, blocking={"legacy_label_rows": 49378, "keyless_notes": 17, "generated_nulls": 3}))
    result = rung(t3).converge(Reporter())
    assert result.outcome is ConvergeOutcome.DEFERRED and result.deferred
    assert t3.apply_calls == [], "no apply, so no row can change"
    d = result.detail
    for name, count in (("legacy_label_rows", 49378), ("keyless_notes", 17), ("generated_nulls", 3)):
        assert f"{name}={count}" in d and f"`--accept {name}={count}`" in d, name
    assert "`tags=` and `category=` filters stop finding those documents" in d
    assert "follow the catalog's TTL" in d and "reads as permanent" in d
    assert "the label is lost" in d
    assert "keeps no copy" in d and "never accepts a residue" in d
    assert "nx t3 retire-keys --tenant tenant-a --apply" in d
    assert "--accept legacy_label_rows=49378 --accept" not in d, "no paste-ready accept-everything command"


def test_every_acceptable_residue_the_engine_lists_has_an_effect_stated() -> None:
    assert set(ACCEPTABLE) == set(ACCEPT_EFFECTS)


def test_a_residue_the_engine_does_not_list_as_acceptable_is_fix_required_with_no_accept_offered() -> None:
    answer = report(retired=4, blocking={"keyless_notes": 2, "chunk_text_hash_mismatch": 5})
    result = rung(T3(answer)).converge(Reporter())
    d = result.detail
    assert "`--accept keyless_notes=2`" in d
    assert "chunk_text_hash_mismatch=5: the engine does not accept this residue, so it has to be fixed" in d
    assert "--accept chunk_text_hash_mismatch" not in d


def test_with_no_acceptable_list_the_rung_offers_no_accept_at_all() -> None:
    result = rung(T3(report(retired=4, blocking={"keyless_notes": 2}, acceptable=[]))).converge(Reporter())
    assert "--accept keyless_notes" not in result.detail and "says whether it can be accepted" in result.detail


def test_a_blocked_residue_prints_in_full_once_and_as_one_line_while_the_counts_stay_the_same() -> None:
    notes = Notes()
    answer = report(retired=9, blocking={"keyless_notes": 17})
    first = rung(T3(answer), notes=notes, now_fn=lambda: datetime(2026, 11, 16, tzinfo=UTC)).converge(Reporter())
    assert "accept it with" in first.detail and notes.note["outcome"] == "blocked"
    assert notes.note["detail"] == first.detail, "the doctor row carries the full text"
    again = rung(T3(answer), notes=notes).converge(Reporter())
    assert "accept it with" not in again.detail and "still held by keyless_notes=17" in again.detail
    assert "`nx doctor`" in again.detail and "\n" not in again.detail
    assert notes.note["detail"] == first.detail and notes.note["since"] == "2026-11-16T00:00:00+00:00"
    changed = rung(T3(report(retired=9, blocking={"keyless_notes": 18})), notes=notes).converge(Reporter())
    assert "accept it with" in changed.detail, "new counts are a new decision: the full text again"


def test_a_dry_run_that_sweeps_nothing_defers_and_names_blocked_collections_and_wrong_width_rows() -> None:
    notes = Notes()
    result = rung(T3(report(retired=6, sweepable=0, blocked=4, wrong_width=2, collections=[
        {"collection": "docs__x__m__v1", "blocked": "legacy_unmanifested"}])), notes=notes).converge(Reporter())
    assert result.deferred and "docs__x__m__v1 (legacy_unmanifested)" in result.detail and "2 with a wrong-width" in result.detail
    assert notes.note["outcome"] == "residue"


def test_converge_completes_on_zero_rows_with_no_apply_and_hands_the_reading_to_verify() -> None:
    t3 = T3(report(retired=0))
    notes = Notes()
    r = rung(t3, notes=notes)
    assert r.converge(Reporter()).completed
    assert r.verify() is True
    assert len(t3.dry_run_calls) == 1, "verify took the clean reading converge made"
    assert t3.apply_calls == []
    assert notes.note["outcome"] == "clean"


def test_the_apply_accepts_nothing_and_a_finished_sweep_is_verified_by_a_fresh_dry_run() -> None:
    t3 = T3(report(retired=5), report(retired=0), applies=[
        applied(swept=5, first_batch_ms=12, batches=1, elapsed_ms=40)])
    notes = Notes()
    r = rung(t3, notes=notes)
    reporter = Reporter()
    result = r.converge(reporter)
    assert result.completed and "swept 5 row(s)" in result.detail
    assert [c["accept"] for c in t3.apply_calls] == [{}], "the engine counts every precondition itself; nothing is accepted"
    assert "tenant" not in t3.apply_calls[0], "this install's own tenant"
    assert ("retire_keys_sweep_batch", {"swept": 5, "remaining": 0, "done": True, "first_batch_ms": 12,
                                       "batches": 1, "elapsed_ms": 40}) in reporter.events, "the first batch's time is surfaced"
    assert ("retire_keys_sweep_job", {"job_id": "job-1", "phase": "started"}) in reporter.events
    assert r.verify() is True and len(t3.dry_run_calls) == 2, "the confirming dry run is a fresh read, not converge's"
    assert "5 swept this run" in r.record_detail() and notes.note["outcome"] == "swept"


def test_a_wait_that_ends_leaves_the_job_running_and_resumable_then_the_next_call_joins_it() -> None:
    gone = ChunkKeysDryRunTimeout("job-1", 600.0, "apply")
    running = refusal(409, "sweep_job_running", kind="apply", job_id="job-1")
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
    notes = Notes()
    refused = refusal(409, "sweep_preconditions_not_met", blocking=["keyless_notes"],
                      report={"tenant": TENANT, "preconditions": {"keyless_notes": 4},
                              "acceptable_residues": ACCEPTABLE})
    result = rung(T3(report(retired=5), applies=[refused]), notes=notes).converge(Reporter())
    assert result.deferred and "keyless_notes=4" in result.detail and "`--accept keyless_notes=4`" in result.detail
    assert notes.note["outcome"] == "blocked"


# ── converge: conditions of the engine defer, failures raise ─────────────────


@pytest.mark.parametrize("first, word", [
    (refusal(403, None), "tenant or root credential"),
    (refusal(409, "sweep_job_running", kind="dry_run", job_id="j7"), "j7"),
    (refusal(503, "sweep_jobs_busy"), "as many sweep jobs"),
    (refusal(404, "sweep_job_not_found"), "lost or stopped"),
    (refusal(404, None), "does not carry the retire-keys routes"),
    (ChunkKeysDryRunTimeout("j3", 900.0, "dry run"), "j3"),
], ids=["credential", "other-request-job", "busy", "job-lost", "no-route", "count-still-running"])
def test_a_condition_on_the_dry_run_defers_naming_the_operator_verb_and_writes_nothing(first, word) -> None:
    t3 = T3(first)
    result = rung(t3).converge(Reporter())
    assert result.deferred and word in result.detail and "nx t3 retire-keys" in result.detail
    assert "nothing was written" in result.detail and t3.apply_calls == []


@pytest.mark.parametrize("exc, word", [
    (refusal(403, None), "tenant or root credential"),
    (refusal(409, "sweep_job_running", kind="apply", job_id="j8"), "j8"),
    (refusal(503, "sweep_jobs_busy"), "as many sweep jobs"),
    (refusal(404, "sweep_job_not_found"), "lost or stopped"),
    (refusal(404, None), "does not carry the retire-keys routes"),
], ids=["credential", "other-request-job", "busy", "job-lost", "no-route"])
def test_a_condition_on_the_apply_defers_naming_the_operator_verb(exc, word) -> None:
    result = rung(T3(report(retired=5), applies=[exc])).converge(Reporter())
    assert result.deferred and word in result.detail and "`nx t3 retire-keys --tenant tenant-a`" in result.detail


def test_a_dry_run_of_another_requests_apply_that_the_rung_cannot_join_defers() -> None:
    """The operator's apply (another fingerprint) is running: the rung asks to join it, the engine refuses, it defers."""
    running = refusal(409, "sweep_job_running", kind="apply", job_id="j8")
    t3 = T3(running, applies=[running])
    result = rung(t3).converge(Reporter())
    assert result.deferred and "j8" in result.detail and len(t3.apply_calls) == 1


@pytest.mark.parametrize("exc", [refusal(500, "sweep_failed", rows_swept=2), refusal(503, None), refusal(502, None)])
def test_a_broken_or_away_engine_raises_from_the_apply_and_the_rows_swept_are_kept(exc) -> None:
    with pytest.raises(VectorServiceError):
        rung(T3(report(retired=5), applies=[exc])).converge(Reporter())


@pytest.mark.parametrize("exc", [refusal(500, "sweep_failed"), refusal(503, None), refusal(502, None)])
def test_a_broken_or_away_engine_raises_from_the_dry_run(exc) -> None:
    t3 = T3(exc)
    with pytest.raises(VectorServiceError):
        rung(t3).converge(Reporter())
    assert t3.apply_calls == []


def test_an_answer_of_an_unknown_shape_fails_the_call_it_is_never_read_as_zero() -> None:
    with pytest.raises(UnreadableReport):
        rung(T3({"totals": {}})).converge(Reporter())


def test_converge_under_auto_against_a_managed_service_is_deferred_before_any_client_is_built() -> None:
    def no_client():
        raise AssertionError("an unattended managed run built a client")

    r = RetireKeysSweepRung(client_factory=no_client, managed_fn=lambda: True, record_fn=lambda: None, state_store=None)
    with unattended_run():
        result = r.converge(Reporter())
    assert result.deferred and "operator verb" in result.detail


def test_converge_past_the_auto_time_limit_defers_without_a_client() -> None:
    now = [0.0]

    def no_client():
        raise AssertionError("a call past the time limit built a client")

    r = RetireKeysSweepRung(client_factory=no_client, managed_fn=lambda: False, record_fn=lambda: None, state_store=None)
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


def test_detects_reading_is_the_one_converge_acts_on() -> None:
    t3 = T3(report(retired=5), applies=[applied(swept=5)])
    r = rung(t3)
    assert r.detect().pending
    assert r.converge(Reporter()).completed
    assert len(t3.dry_run_calls) == 1, "converge did not count again"


# ── verify: positive signal only ─────────────────────────────────────────────


@pytest.mark.parametrize("answer", [
    refusal(500, "sweep_failed"),
    {"totals": {}},
    {"nothing": "here"},
    report(retired=3),
], ids=["error", "no-counts", "unknown-shape", "sweepable-rows-remain"])
def test_verify_is_false_with_a_detail_unless_a_dry_run_succeeded_and_counted_zero(answer) -> None:
    r = rung(T3(answer))
    assert r.verify() is False
    assert r.verify_detail() and r.verify_deferred() is False and r.record_detail() == ""


def test_verify_reraises_an_engine_that_is_away_so_the_runner_waits_for_it() -> None:
    for exc in (refusal(502, None), refusal(503, None)):
        with pytest.raises(VectorServiceError):
            rung(T3(exc)).verify()


@pytest.mark.parametrize("exc, word", [
    (ChunkKeysDryRunTimeout("j5", 900.0, "dry run"), "j5"),
    (refusal(403, None), "tenant or root credential"),
    (refusal(409, "sweep_job_running", kind="dry_run", job_id="j6"), "j6"),
    (refusal(503, "sweep_jobs_busy"), "as many sweep jobs"),
    (refusal(404, "sweep_job_not_found"), "lost or stopped"),
], ids=["timeout", "credential", "other-request-job", "busy", "job-lost"])
def test_a_verify_the_engine_could_not_answer_is_deferred_never_failed(exc, word) -> None:
    r = rung(T3(exc))
    assert r.verify() is False and r.verify_deferred() is True
    assert word in r.verify_detail()
    assert "--wait-seconds" not in r.verify_detail(), "the rung has no such flag; the text is not str(exc)"


def test_verify_under_auto_against_a_managed_service_is_a_deferral_not_a_read() -> None:
    t3 = T3(report(retired=0))
    r = rung(t3, managed=True)
    with unattended_run():
        assert r.verify() is False
    assert r.verify_deferred() and t3.dry_run_calls == []


def test_rows_the_sweep_cannot_take_are_a_deferral_after_the_apply_completes_never_a_failed_verify() -> None:
    """The engine counts a wrong-width chash row in retired_key_rows and never sweeps it: the apply completes, the next dry
    run still counts it, and that is a residue to name, not a sweep that failed."""
    t3 = T3(report(retired=5), report(retired=2, sweepable=0, wrong_width=2), applies=[applied(swept=3)])
    notes = Notes()
    r = rung(t3, notes=notes)
    assert r.converge(Reporter()).completed
    assert r.verify() is False and r.verify_deferred() is True
    assert "2 with a wrong-width chash" in r.verify_detail() and "the sweep ran" in r.verify_detail()
    assert r.record_detail() == ""
    assert notes.note["outcome"] == "residue"


def test_through_the_runner_a_wrong_width_residue_is_deferred_and_not_recorded_while_zero_is_recorded() -> None:
    def walk(t3: T3, ledger: InMemoryCompletionLedger):
        walked = RetireKeysSweepRung(client_factory=lambda: t3, managed_fn=lambda: False, record_fn=lambda: None,
                                     state_store=None)
        return LadderRunner(LadderRegistry((walked,)), ledger, package_version_fn=lambda: "9.9.9").run()

    ledger = InMemoryCompletionLedger()
    report_ = walk(T3(report(retired=5), report(retired=2, sweepable=0, wrong_width=2), applies=[applied(swept=3)]), ledger)
    (run,) = report_.runs
    assert run.outcome is RungOutcome.DEFERRED and "wrong-width" in run.detail
    assert ledger.verified_rungs() == frozenset()

    report_ = walk(T3(report(retired=5), report(retired=0), applies=[applied(swept=5)]), ledger)
    assert [r.outcome for r in report_.runs] == [RungOutcome.RECORDED]
    assert ledger.verified_rungs() == frozenset({RUNG_RETIRE_KEYS_SWEEP})

    report_ = walk(T3(report(retired=3, blocking={"keyless_notes": 1})), InMemoryCompletionLedger())
    assert [r.outcome for r in report_.runs] == [RungOutcome.DEFERRED], "a blocked residue defers"


def test_through_the_runner_a_refused_credential_is_a_deferral_and_not_a_failed_upgrade() -> None:
    walked = RetireKeysSweepRung(client_factory=lambda: T3(refusal(403, None)), managed_fn=lambda: False,
                                 record_fn=lambda: None, state_store=None)
    report_ = LadderRunner(LadderRegistry((walked,)), InMemoryCompletionLedger(), package_version_fn=lambda: "9.9.9").run()
    # detect reads the refused credential as not applicable, so the walk skips the rung and nothing fails
    assert [r.outcome for r in report_.runs] == [RungOutcome.SKIPPED_NOT_APPLICABLE]
