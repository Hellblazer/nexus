# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): strip the retired chunk-row metadata keys from this install's tenant.

WHY A RUNG. Phase 4 Step 1 stopped the clients sending the document-level and position keys on a chunk row
(``title``, ``tags``, the section labels, the spans, ...): the catalog and the manifest hold them now. Rows written
before that still carry them, and the allow-list CHECK a later engine cut adds (Step 2c) would refuse any update of such
a row. The two cloud tenants were swept by an operator on 2026-11-08 (``nx t3 retire-keys``). A local install has no
operator: the upgrade ladder (RDR-185) is the standing mechanism for a data transition that must converge, so the sweep
is a rung and ``nx upgrade`` runs it once the engine is at the pinned tag.

THIS IS A DATA MOVE AT ``nx upgrade``, NEVER AT ENGINE BOOT (RDR-246's rule: a release does not rewrite user data
when the engine starts). The work is the engine's own sweep, run as an engine JOB through
:meth:`~nexus.db.http_vector_client.HttpVectorClient.chunk_keys_dry_run` and ``chunk_keys_apply`` (the same routes the
operator verb ``nx t3 retire-keys`` drives): visible, resumable, audited, and refused by the engine itself while a
precondition stands. ``nx upgrade --auto`` on a local engine runs it too, and that is a REAL UNATTENDED WRITE (it runs
at every session start until the install is swept): the deferrals below are what keeps it from writing a residue nobody
judged.

WHICH CREDENTIAL. The route takes a tenant or a root credential and refuses any other with 403 (the scope guard). A
local install presents the supervisor's persistent root bearer (the lease's token, ``storage_service_daemon``): root
scope, the credential's own tenant, and the engine audits each call in ``gc_audit``. A managed install that ``nx init``
set up usually holds a mint or data credential, which the route refuses. That, a 409 ``sweep_job_running`` for a job
another request started (the operator verb, a dry run), and a 503 ``sweep_jobs_busy`` are CONDITIONS, not failures:
converge is DEFERRED naming the operator verb and the running job, detect reads a refused credential as not applicable
and a running job as pending, and verify defers. A 5xx other than ``sweep_jobs_busy``, or an answer whose shape the
rung does not know, still raises: that is an engine that is broken, and ``nx upgrade`` says so.

WHAT IT DOES.
  detect   : converged from the completion record (as ``manifest-span-fill`` does: the sweep is recorded once it
             verifies, and an older client can write a retired key back before the Step 2c engine check, so
             ``nx doctor``'s ``Retired chunk keys`` row names the record as not a live count). With no record: not
             applicable where a read is not allowed (below), CHEAP_PENDING in an interactive ``nx upgrade`` (the dry run
             is made once, in converge), else the dry run, read-only (a count job in the engine; nothing is written or
             locked), converged when it reports no retired-key row.
  converge : the dry run first. When it reports a precondition that stands in the way (``blocking``: legacy-label
             manifest rows, keyless notes, home-less documents, NULL manifest spans, GENERATED NULLs, ...) the rung is
             DEFERRED and writes nothing. THE RUNG NEVER ACCEPTS A RESIDUE ON THE USER'S BEHALF: accepting one is the
             operator's judgment, per residue, with the counts in front of them (the 2026-11-08 sweeps were accepted
             by Sam, per tenant). The deferral text states what accepting each residue destroys (the sweep keeps no
             copy of a removed key) and offers ``--accept NAME=N`` only for a name the engine lists in
             ``acceptable_residues``; any other is fix-required. It prints in full the first time and as one line
             while the counts are unchanged (the full text is kept in a T2 note and is on ``nx doctor``'s row).
             Otherwise the apply job runs through ``chunk_keys_apply`` with nothing accepted, so the engine itself
             refuses a residue that grew since the dry run. One call waits at most :data:`APPLY_WAIT_S` seconds (less
             under the ``--auto`` time limit): while rows remain the rung is RESUMABLE and the job goes on in the
             engine, so the next call joins it.
  verify   : positive signal only. A dry run that SUCCEEDED and counts no retired-key row. An error or an unknown
             answer is False with ``verify_detail`` (nexus-hdumg); an engine that is away re-raises so the runner
             waits for it. Retired-key rows the sweep CANNOT take (a wrong-width chash, a blocked collection) and a
             count that did not finish are not a failed sweep: verify defers, naming them.

ORDER. After the RDR-192 manifest backfill, the manifest section-label rung, the manifest span-fill rung and the
note-tags-array rung (the hard edges in :mod:`nexus.upgrade_ladder.registry`): those fill the manifest rows and the
catalog entries the sweep's preconditions count, and the sweep deletes the chunk-row copy they read from. Once the
sweep is recorded, the RDR-192 backfill rung refuses to run (it attributes a chunk by the ``doc_id`` and
``catalog_doc_id`` the sweep removes).

A MANAGED SERVICE. On the cloud the sweep is an operator verb. ``nx upgrade --auto`` against a managed service
(:func:`~nexus.upgrade_ladder.auto_guard.refuses_unbounded_read`) runs no dry run and no sweep: detect answers from a
completion record or says not applicable, and converge is DEFERRED. An interactive ``nx upgrade`` does the dry run, and
the apply when no precondition stands in the way.

OUT OF SCOPE HERE. The add-constraint and validate steps (the allow-list CHECK, NOT VALID and then VALID) wait for
the engine cut of RDR-236 Step 2c (nexus-6pfgb.66, after the would-refuse read ends 2026-11-28), and a constraint is
never added by a rung: it is table-wide and operator-only (``nx t3 retire-keys --add-check``). The RDR text that has a
local single-tenant install run them from this rung is superseded by that.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from nexus.db.http_vector_client import (
    ChunkKeysDryRunTimeout,
    ChunkKeysJobLeftRunning,
    VectorServiceError,
)
from nexus.upgrade_ladder.auto_guard import (
    CHEAP_PENDING,
    INTERACTIVE_REMEDY,
    completion_recorded_at,
    default_completion_record,
    detect_refuses,
    managed_service,
    refuses_unbounded_read,
    unattended_verify_refusal,
)
from nexus.upgrade_ladder.census_progress import NoteStore, guarded
from nexus.upgrade_ladder.deadline import active_deadline, active_expired
from nexus.upgrade_ladder.engine_outage import engine_unreachable
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_RETIRE_KEYS_SWEEP
from nexus.upgrade_ladder.retire_keys_state import BLOCKED, CLEAN, RESIDUE, SWEPT, default_state_store
from nexus.upgrade_ladder.scan_reuse import ScanReuse
from nexus.upgrade_ladder.unattended import is_bounded, is_cheap_detect

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_RETIRE_KEYS_SWEEP

#: The longest one ``converge`` call waits for the apply job, in seconds. The job keeps sweeping in the engine when
#: the wait ends, and the next call joins it (the engine hands a repeat of the same running request the job that runs).
APPLY_WAIT_S = 600.0

#: The shortest wait a call gives a job when the ``--auto`` time limit is nearly spent, so a call is never a no-op poll.
_MIN_WAIT_S = 30.0

#: What accepting each residue destroys. The sweep keeps NO copy of a key it removes, so each is a fact about what the
#: operator gives up, not about what might be recoverable. A name not here is described by the engine only.
ACCEPT_EFFECTS: dict[str, str] = {
    "legacy_label_rows": (
        "those manifest rows keep no section label, and the chunk row's label is deleted: the label is lost"
    ),
    "keyless_notes": (
        "those notes follow the catalog's TTL from now on, and one with no manifest row reads as permanent: a TTL only "
        "the chunk row carried is lost"
    ),
    "keyless_ttl_rows": "a TTL only the chunk row carried is lost, and the note reads as permanent",
    "homeless_documents": "the document-level keys those chunk rows carried (title, ids) have no catalog copy and are lost",
    "null_span_rows": (
        "the manifest keeps NULL line and character spans, and the span the chunk row recorded is deleted: where the "
        "chunk sat in its file is lost"
    ),
    "generated_nulls": (
        "`tags=` and `category=` filters stop finding those documents, because the catalog holds no value for the key "
        "and the row's copy is deleted"
    ),
}


@dataclass(frozen=True)
class Reading:
    """What one dry run said, reduced to what the rung decides on."""

    tenant: str
    retired_key_rows: int
    sweepable_rows: int
    blocked_rows: int
    wrong_width_rows: int
    blocking: tuple[str, ...]
    preconditions: dict[str, int]
    blocked_collections: tuple[str, ...]
    #: The residues the engine says an operator may accept by name (``acceptable_residues``); empty when it said none.
    acceptable: tuple[str, ...] = ()


class UnreadableReport(ValueError):
    """The engine answered a dry run in a shape the rung does not know: nothing can be concluded from it."""


def _count(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnreadableReport(f"the dry run's {what} is {value!r}, not a count")
    return value


def read_report(report: object) -> Reading:
    """The :class:`Reading` of a dry run's answer, or :class:`UnreadableReport` when it lacks the counts."""
    if not isinstance(report, dict) or not isinstance(report.get("totals"), dict):
        raise UnreadableReport("the dry run answered no totals")
    totals = report["totals"]
    blocking = report.get("blocking") or []
    pre = report.get("preconditions") or {}
    acceptable = report.get("acceptable_residues") or []
    if not isinstance(blocking, list) or not isinstance(pre, dict) or not isinstance(acceptable, list):
        raise UnreadableReport("the dry run's blocking, preconditions or acceptable residues are not the engine's shapes")
    return Reading(
        tenant=str(report.get("tenant") or ""),
        retired_key_rows=_count(totals.get("retired_key_rows"), "total retired_key_rows"),
        sweepable_rows=_count(totals.get("sweepable_rows"), "total sweepable_rows"),
        blocked_rows=_count(totals.get("blocked_rows", 0), "total blocked_rows"),
        wrong_width_rows=_count(totals.get("wrong_width_rows", 0), "total wrong_width_rows"),
        blocking=tuple(str(n) for n in blocking),
        preconditions={str(k): v for k, v in pre.items() if isinstance(v, int) and not isinstance(v, bool)},
        blocked_collections=tuple(
            f"{c.get('collection')} ({c.get('blocked')})"
            for c in report.get("collections") or [] if isinstance(c, dict) and c.get("blocked")
        ),
        acceptable=tuple(str(n) for n in acceptable),
    )


# ── conditions: what the engine says that is not a failure ───────────────────

#: ``reason`` values of a job the engine lost, stopped or left running: a later run starts or joins it again.
_JOB_LOST = frozenset({"sweep_job_not_found", "sweep_job_stopped", "sweep_job_gone_after_cancel"})


@dataclass(frozen=True)
class Condition:
    """An answer of the engine that stops this call without being a failure of it."""

    kind: str   # "credential" | "job_running" | "busy" | "job_lost" | "waiting" | "no_route"
    text: str


def condition_of(exc: VectorServiceError) -> Condition | None:
    """The :class:`Condition` *exc* is, or ``None`` when it is a failure the caller must raise. The order matters: a
    503 ``sweep_jobs_busy`` is a condition although every other 503 is an engine that is away, and a 404 with a reason
    is a lost job, never an engine without the routes (as ``nx t3 retire-keys`` reads it)."""
    body = exc.engine_body or {}
    job = body.get("job_id") or getattr(exc, "job_id", None)
    if isinstance(exc, (ChunkKeysDryRunTimeout, ChunkKeysJobLeftRunning)) or exc.reason in (
            "sweep_job_still_running", "sweep_job_left_running"):
        what = getattr(exc, "what", "dry run")
        return Condition("waiting", f"the {what} job ({job}) is still running in the engine and a later `nx upgrade` joins it")
    if exc.code == 403:
        return Condition(
            "credential",
            "the credential this install presents is not accepted by the sweep route, which needs a tenant or root "
            "credential (a managed install usually holds a mint or data credential)",
        )
    if exc.reason == "sweep_job_running":
        kind = "sweep" if body.get("kind") == "apply" else "dry run"
        return Condition(
            "job_running",
            f"the engine is already running a {kind} of this tenant for a different request (job {job}); this rung does "
            "not join or cancel it",
        )
    if exc.reason == "sweep_jobs_busy":
        return Condition("busy", "the engine is running as many sweep jobs as it allows")
    if exc.reason in _JOB_LOST:
        return Condition("job_lost", "the engine lost or stopped the job (it restarted, or the job expired)")
    if exc.code == 404 and exc.reason is None:
        return Condition("no_route", "this engine does not carry the retire-keys routes")
    return None


def _operator(tenant: str) -> str:
    return f"`nx t3 retire-keys --tenant {tenant or '<tenant>'}`"


def _blocking_detail(reading: Reading) -> str:
    """The full deferral text: each standing count, what accepting it destroys, and which can be accepted at all."""
    counts = {n: reading.preconditions.get(n, 0) for n in reading.blocking}
    clauses = []
    for name, count in counts.items():
        if reading.acceptable and name in reading.acceptable:
            effect = ACCEPT_EFFECTS.get(name, "the engine describes it in the dry run")
            clauses.append(
                f"{name}={count}: fix it, or accept it with `--accept {name}={count}`, which means {effect}"
            )
        elif reading.acceptable:
            clauses.append(f"{name}={count}: the engine does not accept this residue, so it has to be fixed")
        else:
            clauses.append(f"{name}={count}: {_operator(reading.tenant)} says whether it can be accepted")
    return (
        f"{reading.retired_key_rows} chunk row(s) hold retired metadata keys and the sweep is not run, because "
        + "; ".join(clauses)
        + f". The sweep keeps no copy of a key it removes. This rung never accepts a residue: decide each with the "
        f"counts from {_operator(reading.tenant)}, then apply with `nx t3 retire-keys --tenant "
        f"{reading.tenant or '<tenant>'} --apply` and one `--accept NAME=N` for each residue you accept"
    )


def _fingerprint(reading: Reading) -> str:
    key = json.dumps([reading.retired_key_rows, sorted((n, reading.preconditions.get(n, 0)) for n in reading.blocking)])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _residue_detail(reading: Reading) -> str:
    return (
        f"{reading.retired_key_rows} chunk row(s) hold retired keys that the sweep takes none of: "
        f"{reading.blocked_rows} in blocked collection(s) ({', '.join(reading.blocked_collections) or 'none named'}), "
        f"{reading.wrong_width_rows} with a wrong-width chash. {_operator(reading.tenant)} names them"
    )


def _wait_budget(ceiling: float | None) -> dict[str, float]:
    """``max_wait_s`` for a job wait: *ceiling* (the client's own default when ``None``), cut to what is left of the
    ``--auto`` time limit (never below :data:`_MIN_WAIT_S`)."""
    deadline = active_deadline()
    left = None if deadline is None else max(_MIN_WAIT_S, deadline.seconds - deadline.elapsed())
    if ceiling is None and left is None:
        return {}
    return {"max_wait_s": min(x for x in (ceiling, left) if x is not None)}


def _default_client() -> Any:
    from nexus.db import make_t3  # noqa: PLC0415 — deferred: keeps the registry import cheap

    return make_t3()


def _default_record() -> object | None:
    return default_completion_record(RUNG_NAME)


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 — a failed close never fails the sweep
            _log.debug("retire_keys_sweep_close_failed", exc_info=True)


_DEFAULT_STATE = object()


class RetireKeysSweepRung:
    """The rung. Collaborators are injected for tests: *client_factory* (the T3 client: ``chunk_keys_dry_run`` and
    ``chunk_keys_apply``), *record_fn* (this tenant's completion record), *managed_fn* and *state_store* (the T2 note
    of what the last dry run saw; the production default is used only with the production client, so a test that
    injects a client reaches no store unless it passes one)."""

    name = RUNG_NAME

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = _default_client,
        managed_fn: Callable[[], bool] = managed_service,
        record_fn: Callable[[], object | None] = _default_record,
        apply_wait_s: float = APPLY_WAIT_S,
        state_store: NoteStore | None | object = _DEFAULT_STATE,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),  # noqa: E731 — a seam, not a def
    ) -> None:
        self._client_factory = client_factory
        self._managed_fn = managed_fn
        self._record_fn = record_fn
        self._apply_wait_s = apply_wait_s
        self._now_fn = now_fn
        if state_store is _DEFAULT_STATE:
            state_store = default_state_store() if client_factory is _default_client else None
        self._state: NoteStore | None = state_store  # type: ignore[assignment]
        self._swept = 0
        self._detected: ScanReuse[Reading] = ScanReuse()   # detect's reading, for the converge that follows it
        self._clean: ScanReuse[Reading] = ScanReuse()      # converge's reading that found nothing, for verify
        self._verify_detail = ""
        self._verify_deferred = False
        self._record_detail = ""

    # ── reading ──────────────────────────────────────────────────────────────

    def _dry_run(self, client: Any) -> Reading:
        """One dry run (an engine job), read. ``measure_batch=False``: the batch-cost sample is for the operator; the
        sweep's own first-batch time is reported by the apply."""
        report = client.chunk_keys_dry_run(accept={}, measure_batch=False, **_wait_budget(None))
        return read_report(report)

    def _read(self) -> Reading:
        client = self._client_factory()
        try:
            return self._dry_run(client)
        finally:
            _close(client)

    # ── the measurement note ─────────────────────────────────────────────────

    def _load(self) -> dict | None:
        return guarded("retire_keys_state_load", self._state.load, None) if self._state is not None else None

    def _remember(self, outcome: str, reading: Reading, detail: str, *, since: str | None = None) -> None:
        if self._state is None:
            return
        now = self._now_fn().replace(microsecond=0).isoformat()
        note = {
            "outcome": outcome, "measured_at": now, "since": since or now, "tenant": reading.tenant,
            "retired_key_rows": reading.retired_key_rows, "sweepable_rows": reading.sweepable_rows,
            "blocking": {n: reading.preconditions.get(n, 0) for n in reading.blocking},
            "fingerprint": _fingerprint(reading), "detail": detail[:2000],
        }
        guarded("retire_keys_state_save", lambda: self._state.save(note), None)  # type: ignore[union-attr]

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        # The record first, in every mode (one bounded ledger read; an unreadable ledger is no record): like
        # manifest-span-fill, a recorded sweep is converged, so a steady-state `nx upgrade` makes no dry run. The
        # staleness caveat (an older client can write a retired key back before the Step 2c engine check) is on
        # `nx doctor`'s `Retired chunk keys` row, which says a record is not a live count.
        recorded_at = completion_recorded_at(self._record_fn, RUNG_NAME)
        if recorded_at is not None:
            return RungStatus(applicable=True, converged=True, from_record=recorded_at)
        if detect_refuses(self._managed_fn):
            if refuses_unbounded_read(self._managed_fn):
                reason = (
                    "unattended `nx upgrade --auto` does not run the retired-key dry run against a managed service "
                    f"(its sweep is an operator verb, `nx t3 retire-keys`); {INTERACTIVE_REMEDY}"
                )
            else:
                reason = (
                    "`nx doctor` does not run the retired-key dry run (an engine job whose cost grows with the "
                    f"store); {INTERACTIVE_REMEDY}"
                ) if is_bounded() else f"the retired-key dry run is not run here; {INTERACTIVE_REMEDY}"
            _log.info("retire_keys_sweep_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if is_cheap_detect():
            # nexus-mjmjl: an interactive `nx upgrade` makes the one dry run in converge.
            return RungStatus(applicable=True, converged=False, pending_detail=CHEAP_PENDING)
        try:
            reading = self._read()
        except VectorServiceError as exc:
            cond = condition_of(exc)
            if cond is None or (engine_unreachable(exc) and cond.kind != "busy"):
                raise
            if cond.kind in ("credential", "no_route"):
                _log.info("retire_keys_sweep_rung_skipped", reason=cond.text)
                return RungStatus(applicable=False, converged=False, pending_detail=cond.text)
            return RungStatus(
                applicable=True, converged=False,
                pending_detail=f"the retired-key count could not be taken now: {cond.text}",
            )
        if reading.retired_key_rows == 0:
            return RungStatus(applicable=True, converged=True)
        self._detected.keep(reading)
        detail = (
            f"{reading.retired_key_rows} chunk row(s) of tenant {reading.tenant or '?'} hold retired metadata keys "
            f"({reading.sweepable_rows} to sweep)"
        )
        if reading.blocking:
            detail += "; " + ", ".join(f"{n}={reading.preconditions.get(n, 0)}" for n in reading.blocking) + (
                f" stand in the way: {_operator(reading.tenant)} reads them and takes the operator's accept")
        return RungStatus(applicable=True, converged=False, pending_detail=detail)

    # ── converge ─────────────────────────────────────────────────────────────

    def _deferred(self, cond: Condition, tenant: str = "") -> ConvergeResult:
        return ConvergeResult(
            ConvergeOutcome.DEFERRED,
            detail=f"{cond.text}; nothing was written by this call. The operator verb is {_operator(tenant)}",
        )

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        self._clean.clear()
        if refuses_unbounded_read(self._managed_fn):
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    "unattended `nx upgrade --auto` does not sweep a managed service (the sweep is an operator verb, "
                    f"`nx t3 retire-keys`); {INTERACTIVE_REMEDY}"
                ),
            )
        if active_expired():
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"the `nx upgrade --auto` time limit passed after {self._swept} row(s) swept; the sweep resumes on "
                    "the next `nx upgrade`"
                ),
            )
        reading = self._detected.take()
        client = self._client_factory()
        try:
            if reading is None:
                try:
                    reading = self._dry_run(client)
                except VectorServiceError as exc:
                    cond = condition_of(exc)
                    body = exc.engine_body or {}
                    if cond is not None and cond.kind == "job_running" and body.get("kind") == "apply":
                        # An earlier call's apply may still be sweeping: there is no count to take, so join it
                        # (the same request is handed that job; any other is refused and defers in _apply).
                        return self._apply(client, report, None)
                    if cond is None or (engine_unreachable(exc) and cond.kind != "busy"):
                        raise
                    return self._deferred(cond)
            if reading.retired_key_rows == 0:
                self._clean.keep(reading)
                self._remember(CLEAN, reading, "no chunk row holds a retired key")
                return ConvergeResult(ConvergeOutcome.COMPLETED, detail="no chunk row holds a retired key")
            if reading.blocking:
                _log.info("retire_keys_sweep_rung_blocked", blocking=list(reading.blocking))
                return self._blocked(reading)
            if reading.sweepable_rows == 0:
                detail = _residue_detail(reading)
                self._remember(RESIDUE, reading, detail)
                return ConvergeResult(ConvergeOutcome.DEFERRED, detail=detail)
            return self._apply(client, report, reading)
        finally:
            _close(client)

    def _blocked(self, reading: Reading) -> ConvergeResult:
        """DEFERRED, naming what stands in the way. The full text the first time the counts are read, one line while
        they stay the same (the full text is on `nx doctor`)."""
        full = _blocking_detail(reading)
        note = self._load()
        same = bool(note) and note.get("outcome") == BLOCKED and note.get("fingerprint") == _fingerprint(reading)
        since = str(note["since"]) if same and note.get("since") else None
        self._remember(BLOCKED, reading, full, since=since)
        if not same:
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=full)
        names = ", ".join(f"{n}={reading.preconditions.get(n, 0)}" for n in reading.blocking)
        return ConvergeResult(
            ConvergeOutcome.DEFERRED,
            detail=(
                f"the retired-key sweep is still held by {names}, unchanged since {since or 'the last run'}; "
                f"`nx doctor` has the full text and {_operator(reading.tenant)} the counts"
            ),
        )

    def _apply(self, client: Any, report: ProgressReporter, reading: Reading | None) -> ConvergeResult:
        """The apply job, accepting nothing (the engine counts every precondition itself and refuses a residue)."""

        def note(event: dict) -> None:
            if event.get("event") in ("started", "restarted"):
                report.emit("retire_keys_sweep_job", job_id=event.get("job_id"), phase=event["event"])

        tenant = reading.tenant if reading else ""
        try:
            result = client.chunk_keys_apply(accept={}, progress=note, **_wait_budget(self._apply_wait_s))
        except ChunkKeysDryRunTimeout as exc:
            # The job goes on in the engine and keeps sweeping; the next call joins it.
            _log.info("retire_keys_sweep_wait_ended", detail=str(exc))
            return ConvergeResult(
                ConvergeOutcome.RESUMABLE,
                detail=f"{self._swept} row(s) swept so far; the sweep job is still running in the engine",
            )
        except VectorServiceError as exc:
            if exc.reason == "sweep_preconditions_not_met":
                body = exc.engine_body or {}
                rep = body.get("report") or {}
                pre = rep.get("preconditions") or {}
                acceptable = rep.get("acceptable_residues") or (reading.acceptable if reading else ())
                residue = Reading(
                    tenant=str(rep.get("tenant") or tenant),
                    retired_key_rows=reading.retired_key_rows if reading else 0,
                    sweepable_rows=0, blocked_rows=0, wrong_width_rows=0,
                    blocking=tuple(str(n) for n in body.get("blocking") or []),
                    preconditions={str(k): v for k, v in pre.items() if isinstance(v, int)},
                    blocked_collections=(), acceptable=tuple(str(n) for n in acceptable),
                )
                return self._blocked(residue)
            cond = condition_of(exc)
            if cond is None or (engine_unreachable(exc) and cond.kind != "busy"):
                raise
            return self._deferred(cond, tenant)
        body = result.body
        swept = body.get("rows_swept")
        self._swept += swept if isinstance(swept, int) and not isinstance(swept, bool) else 0
        report.emit(
            "retire_keys_sweep_batch", swept=swept, remaining=body.get("remaining_rows"), done=body.get("done"),
            first_batch_ms=body.get("first_batch_ms"), batches=body.get("batches"), elapsed_ms=body.get("elapsed_ms"),
        )
        left = body.get("remaining_rows")
        blocked = body.get("blocked_collections") or []
        if body.get("done"):
            if blocked:
                named = ", ".join(f"{c.get('collection')} ({c.get('blocked')})" for c in blocked if isinstance(c, dict))
                return ConvergeResult(
                    ConvergeOutcome.DEFERRED,
                    detail=f"swept {self._swept} row(s); the sweep leaves these collections alone: {named}",
                )
            return ConvergeResult(ConvergeOutcome.COMPLETED, detail=f"swept {self._swept} row(s)")
        stopped = body.get("stopped")
        if stopped == "cancelled" or body.get("stalled") or not swept:
            return ConvergeResult(
                ConvergeOutcome.DEFERRED,
                detail=(
                    f"swept {self._swept} row(s); {left} left and the job made no further progress "
                    f"(stopped: {stopped or 'stalled'}; rows locked by a writer count here). The next `nx upgrade` "
                    "takes up from the rows that are left"
                ),
            )
        return ConvergeResult(ConvergeOutcome.RESUMABLE, detail=f"swept {self._swept} row(s); {left} more")

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """True only when a dry run SUCCEEDED and counts no retired-key row. A dry run that errors, or answers a shape
        this rung does not know, is not 'zero rows' (nexus-hdumg): it is False, with :meth:`verify_detail`. A count
        that did not finish, a credential or job conflict, and retired-key rows the sweep cannot take (a wrong-width
        chash, a blocked collection) are conditions, not a failed sweep: the verify is deferred (:meth:`verify_deferred`)."""
        self._verify_detail = ""
        self._verify_deferred = False
        if refuses_unbounded_read(self._managed_fn):
            self._verify_deferred = True
            self._verify_detail = unattended_verify_refusal("the retired-key count")
            return False
        try:
            reading = self._clean.take() or self._read()
        except Exception as exc:  # noqa: BLE001 — an unreadable dry run is unknown, and unknown is not reached
            cond = condition_of(exc) if isinstance(exc, VectorServiceError) else None
            if cond is not None and (not engine_unreachable(exc) or cond.kind == "busy"):
                self._verify_deferred = True
                self._verify_detail = f"could not confirm the sweep now: {cond.text}"
                return False
            if engine_unreachable(exc):
                raise  # the runner waits for a restarting engine and confirms again (nexus-f3yqd.5)
            self._verify_detail = f"could not read the retired-key dry run to confirm the sweep: {exc}"
            return False
        if reading.retired_key_rows and reading.sweepable_rows == 0:
            detail = _residue_detail(reading)
            self._remember(RESIDUE, reading, detail)
            self._verify_deferred = True
            self._verify_detail = f"the sweep ran, and {detail}"
            return False
        if reading.retired_key_rows:
            self._verify_detail = (
                f"{reading.retired_key_rows} chunk row(s) still hold retired metadata keys, {reading.sweepable_rows} "
                "of them ones the sweep can take"
            )
            return False
        self._remember(SWEPT if self._swept else CLEAN, reading, "no chunk row holds a retired key")
        self._record_detail = f"dry run counts no retired-key row; {self._swept} swept this run"
        return True

    def verify_deferred(self) -> bool:
        """True when the last refusing :meth:`verify` is a condition (unattended refusal, a count still running, a
        credential or job conflict, rows the sweep cannot take), which the walk reports as DEFERRED."""
        return self._verify_deferred

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail
