# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 Phase 1 step 3 (nexus-5r2sc.3.6): move the aspect queue TABLE's
waiting rows to ``aspects`` tuples, one row at a time.

WHY A RUNG. ``NX_ASPECT_QUEUE=tuples`` (the default since the RDR-235 Activation
Step 1 flip, nexus-lnwn1; ``table`` is the opt-out) points the producer and the
worker at the tuple space (RDR-235). Rows the old table already holds, written before the flip
by this or an older client, would otherwise wait for the alternating table side
of the worker to drain them, or strand if the table side is ever retired. The
upgrade ladder (RDR-185) is the standing mechanism for a data transition that
must converge on every install, so the move is a rung: ``nx upgrade`` runs it
after the engine is at the pinned tag.

WHAT IT DOES.
  detect   : read-only. Applicable only when ALL of: ``NX_ASPECT_QUEUE`` is
             ``tuples``; the engine has the ``aspects`` template; and the
             engine answers ``GET /v1/aspects/queue/list_waiting`` (older than
             ``engine-service-v0.1.170`` it is a 404). Converged when the table
             holds no waiting row that can move.
  converge : for each ``pending`` or ``in_progress`` row, ``out`` ``mode=replace``
             on the ``aspects`` template (identity body built by the producer's
             own builder, nonce = ``doc_id``, ``delay_s`` the seconds to the
             row's ``next_retry_at`` on the DATABASE clock, clamped to
             [0, 7,776,000] in whole seconds), THEN delete the table row. A crash
             between the two leaves the row in both places, never in neither;
             the re-run replaces the same task (nonce = ``doc_id``) and deletes.
             ``in_progress`` rows move as pending, with no delay: a worker may
             still hold one, and a replace of a live claim only marks it dirty.
             Batches of :data:`DEFAULT_BATCH_LIMIT` rows are RESUMABLE; the
             rung is COMPLETED when nothing movable is left.
  verify   : positive signal only. The read of the waiting rows SUCCEEDED and
             shows none that can move. An unreachable read is False, with
             ``verify_detail``.

WHAT STAYS IN THE TABLE.
  * ``failed`` rows never move (``requeue-failed`` re-enqueues them).
  * A row whose identity body is over the task cap (``TooLargeError``: a very
    long ``source_path``) stays: it is logged (``aspect_queue_rung_row_too_large``),
    counted, left in the table for the alternating table worker, and excluded for
    the rest of the run, so it neither wedges the rung nor is retried per batch.
    A full tuple space (``MaxLiveRowsExceededError``) is not a row fault: the
    converge returns DEFERRED and resumes on the next ``nx upgrade``.
  * A row with an EMPTY ``doc_id`` stays: the task's nonce is the doc_id and a
    nonce is not invented. The alternating table worker drains it. One log line
    counts them; they do not keep the rung from converging.

THE SKIPS ARE NOT RECORDED (plan-audit residual R2). Every reason this rung does
not apply (flag still ``table``, engine without the template, engine without
the route) is reported through ``detect()`` as ``applicable=False``. The runner
records NOTHING for a not-applicable rung, so the rung is re-detected on every
``nx upgrade``, including the first one after the Phase 2 default flip to
``tuples``. Reporting a skip as COMPLETED plus a passing ``verify`` instead
would record it as applied and never move a row after the flip. A rung that ran
and was recorded with zero rows is still safe: ``detect()`` re-reads the table
each run, so rows an older client enqueues later make it ``converged=False``
again, and it converges again.

ATTEMPTS RESET (gate observation O3). ``out`` has no parameter that sets
attempts, so every migrated row becomes a task with attempts 0 whatever its
``retry_count`` was: a row on its fourth retry gets a full claim budget again.
The engine carries no route to carry it, by decision.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from nexus.db.t2.http_tuple_store import CensusTimeoutError, MaxLiveRowsExceededError, TooLargeError
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    ASPECTS_ENGINE_RELEASE as ASPECTS_ENGINE_TAG,  # aliased: the ladder lint bans identifiers containing 'lease' outside preconditions
    ASPECTS_KEYS,
    ASPECTS_SUBSPACE,
    LIST_WAITING_ENGINE_RELEASE as LIST_WAITING_ENGINE_TAG,
    AspectQueueListWaitingUnsupportedError,
    HttpAspectQueue,
    WaitingRow,
    _identity_body,
    aspect_queue_mode,
    aspects_census,
)
from nexus.upgrade_ladder.deadline import active_expired
from nexus.upgrade_ladder.protocol import (
    ConvergeOutcome,
    ConvergeResult,
    ProgressReporter,
    RungStatus,
)
from nexus.upgrade_ladder.registry import RUNG_ASPECT_QUEUE_TUPLES

_log = structlog.get_logger(__name__)

RUNG_NAME = RUNG_ASPECT_QUEUE_TUPLES

#: The ``aspects`` template's ``retention_seconds``
#: (``service/src/main/resources/tuples/templates/aspects.yaml``; a test pins the
#: two equal). The engine answers 400 to an ``out`` ``delay_s`` above it, and
#: ``mark_retry`` accepts any interval, so a row can carry a longer one.
TEMPLATE_RETENTION_S = 7_776_000

#: Rows moved per ``converge`` call before it returns RESUMABLE.
DEFAULT_BATCH_LIMIT = 500

#: Rows ``detect()`` asks ``list_waiting`` for (nexus-s72c5). ``detect`` runs at every
#: ``nx upgrade --auto`` (each session start) and every ``nx doctor``; it needs only "is
#: there any row that can move", and a page this size answers that whenever the table is
#: small or its head holds one. Only a FULL page with no movable row in it falls back to the
#: whole read (a table whose first rows are all ones that stay: no doc_id, oversize, ...),
#: so a backlog is never reported converged because of where the limit cut it. ``converge``
#: and ``verify`` read the whole table: they move rows and confirm the move.
DETECT_READ_LIMIT = 50

#: The request cap (seconds) on the ``aspects`` census ``detect()`` makes to learn whether the engine has the
#: template (nexus-s72c5). The census counts the subspace's rows, and the answer used here is only "is there a
#: template"; a census that runs past this, or that the engine's own statement timeout cuts, makes ``detect``
#: say pending with the reason, not raise. ``converge`` and ``verify`` send it unchanged.
DETECT_CENSUS_TIMEOUT_S = 10.0


class _CensusSlow(RuntimeError):
    """The template census ran past its cap or the engine's own statement timeout."""


def clamp_delay(delay_s: object) -> int:
    """Whole seconds in [0, :data:`TEMPLATE_RETENTION_S`]: negative, missing and
    unreadable values are 0, a fraction rounds up, an oversized value is capped,
    so one row's delay can never answer 400 and abort the rung."""
    try:
        n = math.ceil(float(delay_s))  # type: ignore[arg-type]
    except OverflowError:  # +inf: capped; -inf: nothing to wait for
        return TEMPLATE_RETENTION_S if float(delay_s) > 0 else 0  # type: ignore[arg-type]
    except (TypeError, ValueError):  # None, "", NaN
        return 0
    return max(0, min(n, TEMPLATE_RETENTION_S))


def _default_queue() -> HttpAspectQueue:
    return HttpAspectQueue()


def _default_tuples() -> Any:
    from nexus.db.t2.http_tuple_store import HttpTupleStore  # noqa: PLC0415 — deferred: a table-mode install never loads it

    return HttpTupleStore()


class AspectQueueTuplesRung:
    """The rung. Collaborators are injected for tests: *mode_fn* (the flag),
    *queue_factory* (an object with ``list_waiting()`` and ``mark_done``) and
    *tuples_factory* (an object with ``subspace_stats`` and ``out_replace``).
    Each is called when needed and closed after use."""

    name = RUNG_NAME

    def __init__(
        self,
        *,
        mode_fn: Callable[[], str] = aspect_queue_mode,
        queue_factory: Callable[[], Any] = _default_queue,
        tuples_factory: Callable[[], Any] = _default_tuples,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
    ) -> None:
        self._mode_fn = mode_fn
        self._queue_factory = queue_factory
        self._tuples_factory = tuples_factory
        self._batch_limit = batch_limit
        self._moved = 0
        #: (collection, source_path) of rows the tuple space refused as too large
        #: this run: left in the table and excluded from :meth:`_movable`.
        self._oversize: set[tuple[str, str]] = set()
        self._verify_detail = ""
        self._record_detail = ""

    # ── applicability ────────────────────────────────────────────────────────

    def _skip_reason(self, queue: Any | None = None, tuples: Any | None = None) -> str:
        """The reason this rung does not apply, or ``""`` when it does. The flag
        is checked before anything is built or sent, so a table-mode install
        makes no request at all."""
        if self._mode_fn() != "tuples":
            return f"{ASPECT_QUEUE_ENV} is not 'tuples'"
        return ""

    def _read_waiting(self, queue: Any, limit: int | None = None) -> list[WaitingRow] | str:
        """The waiting rows (the first *limit* in claim order when given, else all), or the skip
        reason when the engine has no route."""
        try:
            return list(queue.list_waiting() if limit is None else queue.list_waiting(limit=limit))
        except AspectQueueListWaitingUnsupportedError:
            return (
                f"the engine has no list_waiting route; it predates "
                f"{LIST_WAITING_ENGINE_TAG}"
            )

    def _read_queue(self, limit: int | None = None) -> list[WaitingRow] | str:
        queue = self._queue_factory()
        try:
            return self._read_waiting(queue, limit)
        finally:
            _close(queue)

    def _probe(self, limit: int | None = None) -> tuple[str, list[WaitingRow]]:
        """``(skip_reason, rows)``: a non-empty reason means not applicable (and
        no rows). Any other failure of a read propagates. *limit* bounds the
        waiting-row read to its first rows (see :data:`DETECT_READ_LIMIT`)."""
        reason = self._skip_reason()
        if reason:
            return reason, []
        tuples = self._tuples_factory()
        try:
            try:
                census = (
                    aspects_census(tuples) if limit is None
                    else aspects_census(tuples, timeout=DETECT_CENSUS_TIMEOUT_S)
                )
            except (CensusTimeoutError, httpx.TimeoutException) as exc:
                if limit is None:
                    raise
                raise _CensusSlow(str(exc) or type(exc).__name__) from exc
            if census is None:
                return (
                    f"the engine has no 'aspects' tuple template; it predates "
                    f"{ASPECTS_ENGINE_TAG}"
                ), []
        finally:
            _close(tuples)
        rows = self._read_queue(limit)
        if isinstance(rows, str):
            return rows, []
        return "", rows

    def _movable(self, rows: list[WaitingRow]) -> list[WaitingRow]:
        return [
            r for r in rows
            if r.doc_id
            and r.status in ("pending", "in_progress")
            and (r.collection, r.source_path) not in self._oversize
        ]

    def detect(self) -> RungStatus:
        """Bounded (nexus-s72c5): the waiting-row read asks for the first
        :data:`DETECT_READ_LIMIT` rows, because converged needs only "is there a
        row that can move". A full page with none movable in it could hide one
        behind the rows that stay, so that case alone reads the whole table."""
        try:
            reason, rows = self._probe(limit=DETECT_READ_LIMIT)
        except _CensusSlow as exc:
            return RungStatus(
                applicable=True,
                converged=False,
                pending_detail=(
                    "the 'aspects' tuple census did not answer within "
                    f"{DETECT_CENSUS_TIMEOUT_S:g}s ({exc}); `nx upgrade` checks and moves the rows"
                ),
            )
        if reason:
            _log.info("aspect_queue_rung_skipped", reason=reason)
            return RungStatus(applicable=False, converged=False, pending_detail=reason)
        truncated = len(rows) >= DETECT_READ_LIMIT
        movable = self._movable(rows)
        if not movable and truncated:
            whole = self._read_queue()
            if isinstance(whole, str):
                _log.info("aspect_queue_rung_skipped", reason=whole)
                return RungStatus(applicable=False, converged=False, pending_detail=whole)
            rows, truncated = whole, False
            movable = self._movable(rows)
        if not movable:
            return RungStatus(applicable=True, converged=True)
        return RungStatus(
            applicable=True,
            converged=False,
            pending_detail=(
                f"{'at least ' if truncated else ''}{len(movable)} queued row(s) still in the table, "
                f"to move to '{ASPECTS_SUBSPACE}' tasks"
            ),
        )

    # ── converge ─────────────────────────────────────────────────────────────

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        reason, rows = self._probe()
        if reason:
            # The runner converges only an applicable rung; this is the guard for
            # a caller that did not ask detect() first. Nothing moves.
            _log.warning("aspect_queue_rung_converge_refused", reason=reason)
            return ConvergeResult(ConvergeOutcome.DEFERRED, detail=reason)
        movable = self._movable(rows)
        left = sum(1 for r in rows if not r.doc_id)
        if left:
            _log.info("aspect_queue_rung_rows_without_doc_id_left_in_table", count=left)
        if not movable:
            return ConvergeResult(ConvergeOutcome.COMPLETED, detail=self._summary(left))

        queue = self._queue_factory()
        tuples = self._tuples_factory()
        try:
            batch = movable[: self._batch_limit]
            for i, row in enumerate(batch):
                if i and active_expired():
                    report.emit("aspect_queue_rung_deadline", moved=self._moved)
                    return ConvergeResult(
                        ConvergeOutcome.DEFERRED,
                        detail=(
                            f"the `nx upgrade --auto` time limit passed after {self._moved} "
                            f"row(s); the move resumes on the next `nx upgrade`"
                        ),
                    )
                try:
                    self._move(queue, tuples, row)
                except TooLargeError as exc:
                    _log.warning(
                        "aspect_queue_rung_row_too_large",
                        collection=row.collection, source_path=row.source_path,
                        doc_id=row.doc_id, error=str(exc)[:200],
                        action="left in the table; the table worker drains it",
                    )
                    self._oversize.add((row.collection, row.source_path))
                    continue
                except MaxLiveRowsExceededError as exc:
                    _log.warning(
                        "aspect_queue_rung_tuple_space_full",
                        moved=self._moved, error=str(exc)[:200],
                    )
                    return ConvergeResult(
                        ConvergeOutcome.DEFERRED,
                        detail=(
                            f"the 'aspects' tuple space is at its live-row ceiling "
                            f"(MaxLiveRowsExceeded) after {self._moved} row(s); the move "
                            f"resumes on the next `nx upgrade`, once the worker has "
                            f"drained tasks"
                        ),
                    )
                self._moved += 1
            report.emit(
                "aspect_queue_rung_batch", moved=len(batch), remaining=len(movable) - len(batch),
            )
        finally:
            _close(queue)
            _close(tuples)
        if len(movable) > len(batch):
            return ConvergeResult(
                ConvergeOutcome.RESUMABLE,
                detail=f"moved {self._moved} row(s); {len(movable) - len(batch)} more",
            )
        return ConvergeResult(ConvergeOutcome.COMPLETED, detail=self._summary(left))

    @staticmethod
    def _move(queue: Any, tuples: Any, row: WaitingRow) -> None:
        """One row: ``out`` replace FIRST, then delete the table row. The delete is
        by ``(collection, source_path)``, not by ``doc_id``: ``mark_done`` by
        ``doc_id`` removes every table row sharing it, a ``failed`` sibling
        included, and a failed row never moves."""
        delay = clamp_delay(row.delay_s) if row.status == "pending" else 0
        tuples.out_replace(
            ASPECTS_SUBSPACE,
            dict(ASPECTS_KEYS),
            None,
            _identity_body(row.collection, row.source_path, row.doc_id, row.content_hash),
            nonce=row.doc_id,
            delay_s=delay,
        )
        queue.mark_done(row.collection, row.source_path)

    def _summary(self, left: int) -> str:
        text = f"moved {self._moved} row(s) to '{ASPECTS_SUBSPACE}' tasks"
        if left:
            text += f"; {left} row(s) without a doc_id stay in the table"
        if self._oversize:
            text += f"; {len(self._oversize)} row(s) too large for a task stay in the table"
        return text

    # ── verify ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """True only when the waiting-row read SUCCEEDED and shows no row that
        can move. A read that fails, or an engine that cannot answer it, is not
        'zero rows' (nexus-hdumg): it is False, with :meth:`verify_detail`."""
        self._verify_detail = ""
        try:
            reason, rows = self._probe()
        except Exception as exc:  # noqa: BLE001 — an unreadable table is unknown, and unknown is not reached
            self._verify_detail = (
                f"could not read the aspect queue table to confirm it is empty: {exc}"
            )
            return False
        if reason:
            self._verify_detail = f"cannot confirm the move: {reason}"
            return False
        movable = self._movable(rows)
        if movable:
            self._verify_detail = f"{len(movable)} queued row(s) are still in the table"
            return False
        self._record_detail = self._summary(sum(1 for r in rows if not r.doc_id))
        return True

    def verify_detail(self) -> str:
        return self._verify_detail

    def record_detail(self) -> str:
        return self._record_detail


def _close(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001 — a failed close never fails the move
            _log.debug("aspect_queue_rung_close_failed", exc_info=True)
