# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-236 Phase 4 Step 3 (nexus-6pfgb.43): strip the retired chunk-row metadata keys from this install's tenant.

WHY A RUNG. Phase 4 Step 1 stopped the clients sending the document-level and position keys on a chunk row
(``title``, ``tags``, the section labels, the spans, ...): the catalog and the manifest hold them now. Rows written
before that still carry them, and the allow-list CHECK a later engine cut adds (Step 2c) would refuse any update of such
a row. The two cloud tenants were swept by an operator on 2026-11-08 (``nx t3 retire-keys``). Every other install, local
mode chiefly, has no operator: the upgrade ladder (RDR-185) is the standing mechanism for a data transition that must
converge on every install, so the sweep is a rung and ``nx upgrade`` runs it once the engine is at the pinned tag.

THIS IS A DATA MOVE AT ``nx upgrade``, NEVER AT ENGINE BOOT (RDR-246's rule: a release does not rewrite user data
when the engine starts). The work is the engine's own sweep, run as an engine JOB through
:meth:`~nexus.db.http_vector_client.HttpVectorClient.chunk_keys_dry_run` and ``chunk_keys_apply`` (the same routes the
operator verb ``nx t3 retire-keys`` drives): visible, resumable, audited, and refused by the engine itself while a
precondition stands.

WHAT IT DOES.
  detect   : read-only, and the dry run (a count job in the engine; nothing is written or locked). Converged when it
             reports no chunk row holding a retired key. Pending names the rows, and the preconditions that stand in the
             way. Where a read of every row is not allowed it reads nothing (below).
  converge : the dry run first. When it reports a precondition that stands in the way (``blocking``: legacy-label
             manifest rows, keyless notes, home-less documents, NULL manifest spans, GENERATED NULLs, ...) the rung is
             DEFERRED and writes nothing: it names each count with the operator command
             (``nx t3 retire-keys --tenant T --apply --accept NAME=N``). THE RUNG NEVER ACCEPTS A RESIDUE ON THE
             USER'S BEHALF: whether a residue is acceptable is the operator's judgment (the 2026-11-08 sweeps were
             accepted by Sam, per tenant, with the counts in front of him). Otherwise the apply job runs through
             ``chunk_keys_apply`` with nothing accepted, so the engine itself refuses a residue that grew since the
             dry run. One call waits for at most :data:`APPLY_WAIT_S` seconds (and for the ``--auto`` time limit):
             while rows remain the rung is RESUMABLE, and the job goes on in the engine, so the next call joins it.
  verify   : positive signal only. A dry run that SUCCEEDED and counts no retired-key row. A dry run that errors or
             answers a shape the rung does not know is False, with ``verify_detail`` (nexus-hdumg); an engine that is
             away re-raises so the runner waits for it.

ORDER. After the RDR-192 manifest backfill, the manifest section-label rung, the manifest span-fill rung and the
note-tags-array rung (the hard edges in :mod:`nexus.upgrade_ladder.registry`): those fill the manifest rows and the
catalog entries the sweep's preconditions count, and the sweep deletes the chunk-row copy they read from.

A MANAGED SERVICE. On the cloud the sweep is an operator verb. ``nx upgrade --auto`` against a managed service
(:func:`~nexus.upgrade_ladder.auto_guard.refuses_unbounded_read`) runs no dry run and no sweep: detect answers from a
completion record or says not applicable, and converge is DEFERRED. An interactive ``nx upgrade`` does the dry run, and
the apply when no precondition stands in the way.

OUT OF SCOPE HERE. The add-constraint and validate steps (the allow-list CHECK, NOT VALID and then VALID) wait for
the engine cut of RDR-236 Step 2c (nexus-6pfgb.66, after the would-refuse read ends 2026-11-28), and a constraint is
never added by a rung: it is table-wide and operator-only (``nx t3 retire-keys --add-check``).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.db.http_vector_client import ChunkKeysDryRunTimeout, VectorServiceError
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
from nexus.upgrade_ladder.deadline import active_deadline, active_expired
from nexus.upgrade_ladder.engine_outage import engine_unreachable
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_RETIRE_KEYS_SWEEP
from nexus.upgrade_ladder.scan_reuse import ScanReuse
from nexus.upgrade_ladder.unattended import is_cheap_detect

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_RETIRE_KEYS_SWEEP

#: The longest one ``converge`` call waits for the apply job, in seconds. The job keeps sweeping in the engine when
#: the wait ends, and the next call joins it (the engine hands a repeat of the same running request the job that runs).
APPLY_WAIT_S = 600.0

#: The shortest wait a call gives a job when the ``--auto`` time limit is nearly spent, so a call is never a no-op poll.
_MIN_WAIT_S = 30.0


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
    if not isinstance(blocking, list) or not isinstance(pre, dict):
        raise UnreadableReport("the dry run's blocking or preconditions are not the shapes the engine documents")
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
    )


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


def _wait_budget(ceiling: float | None) -> dict[str, float]:
    """``max_wait_s`` for a job wait: *ceiling* (the client's own default when ``None``), cut to what is left of the
    ``--auto`` time limit (never below :data:`_MIN_WAIT_S`)."""
    deadline = active_deadline()
    left = None if deadline is None else max(_MIN_WAIT_S, deadline.seconds - deadline.elapsed())
    if ceiling is None and left is None:
        return {}
    return {"max_wait_s": min(x for x in (ceiling, left) if x is not None)}


def _operator_command(tenant: str, names: dict[str, int]) -> str:
    accept = " ".join(f"--accept {n}={c}" for n, c in names.items())
    return f"nx t3 retire-keys --tenant {tenant or '<tenant>'} --apply {accept}".strip()


def _blocking_detail(reading: Reading) -> str:
    counts = {n: reading.preconditions.get(n, 0) for n in reading.blocking}
    named = ", ".join(f"{n}={c}" for n, c in counts.items())
    return (
        f"{reading.retired_key_rows} chunk row(s) hold retired metadata keys and the sweep is not run: "
        f"{named} stand in the way. This rung never accepts a residue for you. Read the dry run with "
        f"`nx t3 retire-keys --tenant {reading.tenant or '<tenant>'}`, fix what you can, and for each residue you "
        f"judge acceptable apply with `{_operator_command(reading.tenant, counts)}`"
    )


class RetireKeysSweepRung:
    """The rung. Collaborators are injected for tests: *client_factory* (the T3 client: ``chunk_keys_dry_run`` and
    ``chunk_keys_apply``), *record_fn* (this tenant's completion record) and *managed_fn*. The client is built when
    needed and closed after use."""

    name = RUNG_NAME

    def __init__(
        self,
        *,
        client_factory: Callable[[], Any] = _default_client,
        managed_fn: Callable[[], bool] = managed_service,
        record_fn: Callable[[], object | None] = _default_record,
        apply_wait_s: float = APPLY_WAIT_S,
    ) -> None:
        self._client_factory = client_factory
        self._managed_fn = managed_fn
        self._record_fn = record_fn
        self._apply_wait_s = apply_wait_s
        self._swept = 0
        self._detected: ScanReuse[Reading] = ScanReuse()   # detect's reading, for the converge that follows it
        self._clean: ScanReuse[Reading] = ScanReuse()      # converge's reading that found nothing, for verify
        self._verify_detail = ""
        self._verify_deferred = False
        self._record_detail = ""

    # ── reading ──────────────────────────────────────────────────────────────

    def _dry_run(self, client: Any) -> Reading:
        """One dry run (an engine job), read. ``measure_batch=False``: the batch-cost sample is for the operator."""
        report = client.chunk_keys_dry_run(accept={}, measure_batch=False, **_wait_budget(None))
        return read_report(report)

    def _read(self) -> Reading:
        client = self._client_factory()
        try:
            return self._dry_run(client)
        finally:
            _close(client)

    # ── detect ───────────────────────────────────────────────────────────────

    def detect(self) -> RungStatus:
        refuses = detect_refuses(self._managed_fn)
        recorded_at = completion_recorded_at(self._record_fn, RUNG_NAME) if refuses or is_cheap_detect() else None
        if refuses:
            # A managed service's sweep is an operator verb, and unattended it reads nothing. The one cheaper answer
            # is the completion record the interactive run that swept and verified wrote (nexus-mq980).
            if recorded_at is not None:
                return RungStatus(applicable=True, converged=True, from_record=recorded_at)
            reason = (
                "unattended `nx upgrade --auto` does not run the retired-key dry run against a managed service "
                f"(its sweep is an operator verb, `nx t3 retire-keys`); {INTERACTIVE_REMEDY}"
            )
            _log.info("retire_keys_sweep_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if is_cheap_detect() and recorded_at is None:
            # nexus-mjmjl: an interactive `nx upgrade` makes the one dry run in converge.
            return RungStatus(applicable=True, converged=False, pending_detail=CHEAP_PENDING)
        try:
            reading = self._read()
        except VectorServiceError as exc:
            if engine_unreachable(exc) or exc.code != 404:
                raise
            reason = "this engine does not carry the retire-keys routes"
            _log.info("retire_keys_sweep_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        if reading.retired_key_rows == 0:
            return RungStatus(applicable=True, converged=True)
        self._detected.keep(reading)
        detail = (
            f"{reading.retired_key_rows} chunk row(s) of tenant {reading.tenant or '?'} hold retired metadata keys "
            f"({reading.sweepable_rows} to sweep)"
        )
        if reading.blocking:
            detail += "; " + ", ".join(f"{n}={reading.preconditions.get(n, 0)}" for n in reading.blocking) + (
                " stand in the way: `nx t3 retire-keys` reads them and takes the operator's accept")
        return RungStatus(applicable=True, converged=False, pending_detail=detail)

    # ── converge ─────────────────────────────────────────────────────────────

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
                except ChunkKeysDryRunTimeout as exc:
                    return ConvergeResult(
                        ConvergeOutcome.DEFERRED,
                        detail=f"{exc} The count goes on in the engine; the next `nx upgrade` joins it.",
                    )
                except VectorServiceError as exc:
                    body = exc.engine_body or {}
                    if exc.reason == "sweep_job_running" and body.get("kind") == "apply":
                        # An earlier call's apply is still sweeping: no count to take, join it below.
                        return self._apply(client, report, None)
                    if exc.code == 404 and not engine_unreachable(exc):
                        return ConvergeResult(
                            ConvergeOutcome.DEFERRED, detail="this engine does not carry the retire-keys routes",
                        )
                    raise
            if reading.retired_key_rows == 0:
                self._clean.keep(reading)
                return ConvergeResult(ConvergeOutcome.COMPLETED, detail="no chunk row holds a retired key")
            if reading.blocking:
                _log.info("retire_keys_sweep_rung_blocked", blocking=list(reading.blocking))
                return ConvergeResult(ConvergeOutcome.DEFERRED, detail=_blocking_detail(reading))
            if reading.sweepable_rows == 0:
                return ConvergeResult(
                    ConvergeOutcome.DEFERRED,
                    detail=(
                        f"{reading.retired_key_rows} chunk row(s) hold retired keys and the sweep takes none of them: "
                        f"{reading.blocked_rows} in blocked collection(s) "
                        f"({', '.join(reading.blocked_collections) or 'none named'}), {reading.wrong_width_rows} with a "
                        "wrong-width chash. `nx t3 retire-keys` names them"
                    ),
                )
            return self._apply(client, report, reading)
        finally:
            _close(client)

    def _apply(self, client: Any, report: ProgressReporter, reading: Reading | None) -> ConvergeResult:
        """The apply job, accepting nothing (the engine counts every precondition itself and refuses a residue)."""

        def note(event: dict) -> None:
            if event.get("event") in ("started", "restarted"):
                report.emit("retire_keys_sweep_job", job_id=event.get("job_id"), phase=event["event"])

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
                residue = Reading(
                    tenant=str(rep.get("tenant") or (reading.tenant if reading else "")),
                    retired_key_rows=reading.retired_key_rows if reading else 0,
                    sweepable_rows=0, blocked_rows=0, wrong_width_rows=0,
                    blocking=tuple(str(n) for n in body.get("blocking") or []),
                    preconditions={str(k): v for k, v in pre.items() if isinstance(v, int)},
                    blocked_collections=(),
                )
                return ConvergeResult(ConvergeOutcome.DEFERRED, detail=_blocking_detail(residue))
            if exc.code == 404 and not engine_unreachable(exc):
                return ConvergeResult(
                    ConvergeOutcome.DEFERRED, detail="this engine does not carry the retire-keys routes",
                )
            raise
        body = result.body
        swept = body.get("rows_swept")
        self._swept += swept if isinstance(swept, int) and not isinstance(swept, bool) else 0
        report.emit("retire_keys_sweep_batch", swept=swept, remaining=body.get("remaining_rows"), done=body.get("done"))
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
        this rung does not know, is not 'zero rows' (nexus-hdumg): it is False, with :meth:`verify_detail`."""
        self._verify_detail = ""
        self._verify_deferred = False
        if refuses_unbounded_read(self._managed_fn):
            self._verify_deferred = True
            self._verify_detail = unattended_verify_refusal("the retired-key count")
            return False
        try:
            reading = self._clean.take() or self._read()
        except Exception as exc:  # noqa: BLE001 — an unreadable dry run is unknown, and unknown is not reached
            if engine_unreachable(exc):
                raise  # the runner waits for a restarting engine and confirms again (nexus-f3yqd.5)
            self._verify_detail = f"could not read the retired-key dry run to confirm the sweep: {exc}"
            return False
        if reading.retired_key_rows:
            self._verify_detail = f"{reading.retired_key_rows} chunk row(s) still hold retired metadata keys"
            return False
        self._record_detail = f"dry run counts no retired-key row; {self._swept} swept this run"
        return True

    def verify_deferred(self) -> bool:
        """True when the last refusing :meth:`verify` is the unattended refusal, which the walk reports as DEFERRED."""
        return self._verify_deferred

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail
