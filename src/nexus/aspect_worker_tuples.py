# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The aspect worker's side of the ``aspects`` tuple queue (RDR-235, client half).

``AspectExtractionWorker`` keeps the extraction half (``_extract_aspects`` and
its batch form) and the table drain; this module owns what is new:

* a worker id ``W`` unique per live process, and one claimant string
  ``W:<slot>`` per claim in a batch (a repeat ``in``/``inp`` by one claimant
  returns the claim it already holds, so five claims under one string would be
  one task five times);
* the claim itself: slots 1..N-1 are non-blocking ``inp``; slot 0 is one parked
  ``in`` (``park_timeout_s``) when the caller allows a park and nothing else was
  found, else an ``inp`` too. Every slot is visited on every call, so a claim
  left live by a failed ``ack`` comes back to its own slot;
* a bounded renewer that renews every held claim every ``renew_every_s``
  only while extraction is inside its deadline;
* the verdict map: success -> ``complete_aspect`` with the claim pair (a fenced
  upsert, then ``ack``); a missing catalog document, an unsupported collection
  or a permanent read failure -> ``ack`` as dropped; a retryable failure
  (including ``ExtractFail(infra_unavailable)``) -> ``nack`` with the RDR-163
  ladder; a permanent failure -> ``nack`` dead; a retired collection name
  (409) -> ``release`` once, ``nack`` dead on a repeat; a lapsed claim (404 or
  403) -> drop the result, write nothing, do not ack; a self fault -> release
  every held claim and stand down.

The ladder index is the claimed tuple's own ``attempts`` (``TupleRow.attempts``
is the number of earlier failed attempts: 0 on a first claim, +1 for each nack
and each lapsed lease), which is what the table's ``retry_count`` was.
"""
from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.records import QueueRow

_log = structlog.get_logger(__name__)

#: The tuple-space address of the queue (RDR-235 E3). Mirrors
#: ``http_aspect_queue.ASPECTS_SUBSPACE`` / ``ASPECTS_KEYS`` (pinned equal by
#: tests/test_aspect_worker_tuples.py); not imported so a table-mode worker
#: never loads the producer's module for them.
SUBSPACE: str = "aspects"
KEYS: dict[str, str] = {"queue": "aspects"}

#: Lease asked of every claim and every renewal (RDR-235 Worker side step 1). The
#: template's ``default_lease_seconds`` is the same 600, and ``max_lease_seconds``
#: is 900 (``LeaseTooLongError`` above it).
LEASE_S: int = 600
#: Slot 0's parked ``in`` (the engine's own cap is 25 s a call).
PARK_TIMEOUT_S: int = 25
#: How often the renewer renews, against the 600 s lease.
RENEW_INTERVAL_S: float = 120.0
#: Added to the extractor's worst case before the renewer stops (T3 reads, the
#: manifest lookup and the persist are outside the extractor's own timeouts).
RENEW_DEADLINE_MARGIN_S: float = 120.0
#: ``tuple`` claims re-probe an engine that lacked the template after this long
#: (an engine upgrade does not restart the aspect-worker daemon).
UNSUPPORTED_REPROBE_S: float = 3600.0
#: Task ids the 409 bound remembers (oldest dropped past this).
SUPERSEDED_MEMORY: int = 1024

_ENGINE_UNSUPPORTED_EVENT = "aspect_worker_tuples_unsupported_by_engine"


@dataclass
class HeldClaim:
    """One claim this process holds: the task, its claim pair and its slot."""

    slot: int
    claimant: str
    claim_id: str
    task_id: str
    attempts: int
    #: The queue row built from the task body; ``None`` when the body is malformed.
    row: QueueRow | None


@dataclass(frozen=True)
class ExtractRaised:
    """An extraction call raised; carried per row so the verdict map can route it."""

    exc: BaseException


def worker_id(tenant: str, *, pid: int, nonce: str) -> str:
    """``W``: unique per live process (a tenant tag, the pid and a start nonce).

    The tenant is hashed to 8 hex characters so ``f"{W}:{slot}"`` is far under
    the 128-byte claimant ceiling whatever the tenant string is."""
    import hashlib  # noqa: PLC0415 — stdlib, deferred with its only use

    tag = hashlib.sha256(tenant.encode("utf-8")).hexdigest()[:8]
    return f"aw-{tag}-{pid}-{nonce}"


def _lapse_errors() -> tuple[type[BaseException], ...]:
    from nexus.db.t2.http_tuple_store import (  # noqa: PLC0415 — deferred: a table-mode worker never loads the tuple client
        ClaimNotFoundError,
        ClaimOwnershipError,
        ClaimTaskMismatchError,
    )

    return (ClaimNotFoundError, ClaimOwnershipError, ClaimTaskMismatchError)


def _row_from_task(row: Any) -> QueueRow | None:
    """The queue row for a claimed task, or ``None`` when the body is not the
    identity body (RDR-235 E2): JSON with a non-blank ``collection``,
    ``source_path`` and ``doc_id``. ``content`` is never in a task."""
    try:
        body = json.loads(row.body) if isinstance(row.body, str) else None
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    collection, source_path, doc_id = (
        body.get("collection"), body.get("source_path"), body.get("doc_id"),
    )
    if not all(isinstance(v, str) and v for v in (collection, source_path, doc_id)):
        return None
    return QueueRow(
        collection=collection,
        source_path=source_path,
        content_hash=str(body.get("content_hash") or ""),
        content="",
        retry_count=int(getattr(row, "attempts", 0) or 0),
        doc_id=doc_id,
    )


class ClaimRenewer:
    """Renews a set of held claims every ``interval_s``, only while ``deadline_s``
    has not passed since :meth:`start`; then it stops renewing and the leases
    lapse (a wedged extraction releases its tasks instead of holding them
    until retention). ``renew(claim)`` raises a lapse error for a claim that is
    no longer ours; that claim is dropped from the set and logged."""

    def __init__(
        self,
        claims: Sequence[HeldClaim],
        renew: Callable[[HeldClaim], Any],
        *,
        interval_s: float,
        deadline_s: float,
        lapse_errors: tuple[type[BaseException], ...],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._claims = list(claims)
        self._renew = renew
        self._interval_s = interval_s
        self._deadline_s = deadline_s
        self._lapse_errors = lapse_errors
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rounds = 0
        self.stopped_at_deadline = False

    def start(self) -> None:
        self._t0 = self._clock()
        self._thread = threading.Thread(
            target=self._run, name="aspect-claim-renewer", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            if self._clock() - self._t0 >= self._deadline_s:
                self.stopped_at_deadline = True
                _log.warning(
                    "aspect_worker_renewer_deadline_passed",
                    deadline_seconds=self._deadline_s, claims=len(self._claims),
                    action="renewal stopped; the leases lapse and the tasks are retried",
                )
                return
            self.rounds += 1
            for claim in list(self._claims):
                try:
                    self._renew(claim)
                except self._lapse_errors:
                    self._claims.remove(claim)
                    _log.warning(
                        "aspect_worker_renew_claim_lost",
                        task_id=claim.task_id, claimant=claim.claimant,
                    )
                except Exception:  # noqa: BLE001 — a transport blip must not kill the renewer; the next round retries
                    _log.warning(
                        "aspect_worker_renew_failed",
                        task_id=claim.task_id, claimant=claim.claimant, exc_info=True,
                    )


class TupleQueueSide:
    """Claims, renews and finishes tasks of the ``aspects`` queue for one worker.

    *tuples_call* runs ``fn(tuples_store)`` against the process's service
    handle (``t2_index_write``); *complete* persists one result through
    ``complete_aspect`` with the claim pair; *extract* turns the ready claims'
    rows into one result per row (an ``AspectRecord``, ``None``, an
    ``ExtractFail`` or an :class:`ExtractRaised`); *on_self_fault* is the
    worker's stand-down. Everything else is plain data."""

    def __init__(
        self,
        *,
        worker_id: str,
        batch_size: int,
        tuples_call: Callable[[Callable[[Any], Any]], Any],
        complete: Callable[[dict[str, Any], str, str], Any],
        extract: Callable[[list[HeldClaim]], list[Any]],
        on_self_fault: Callable[[str, HeldClaim, BaseException], None],
        lease_s: int = LEASE_S,
        park_timeout_s: int = PARK_TIMEOUT_S,
        renew_every_s: float = RENEW_INTERVAL_S,
        deadline_s: Callable[[int], float] | None = None,
        document_gone: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from nexus.aspect_extractor import worst_case_extract_seconds  # noqa: PLC0415 — deferred: heavy module

        self.worker_id = worker_id
        self._batch_size = batch_size
        self._call = tuples_call
        self._complete = complete
        self._extract = extract
        self._on_self_fault = on_self_fault
        self._lease_s = lease_s
        self._park_timeout_s = park_timeout_s
        self._renew_every_s = renew_every_s
        self._deadline_s = deadline_s or (
            lambda n: worst_case_extract_seconds(n) + RENEW_DEADLINE_MARGIN_S
        )
        self._document_gone = document_gone or (lambda _doc_id: False)
        self._clock = clock
        self._held: dict[str, HeldClaim] = {}
        self._held_lock = threading.Lock()
        self._superseded: OrderedDict[str, int] = OrderedDict()
        self._unsupported_until: float | None = None
        self._abandon = threading.Event()
        self.parked = False

    # ── claiming ──────────────────────────────────────────────────────────

    def claimant(self, slot: int) -> str:
        return f"{self.worker_id}:{slot}"

    def supported(self) -> bool:
        """False while the engine is known to lack the ``aspects`` template."""
        until = self._unsupported_until
        if until is None:
            return True
        if self._clock() >= until:
            self._unsupported_until = None
            return True
        return False

    def claim_batch(self, *, park: bool) -> tuple[list[HeldClaim], bool]:
        """Visit every slot; return ``(claims, failed)``.

        Slots 1..N-1 are ``inp``. Slot 0 is the parked ``in`` when *park* is
        true and the other slots found nothing (a worker holding claims must not
        sit in a park), else an ``inp``. A slot that raises ends the visit: the
        claims already taken are returned with ``failed=True``. An engine with
        no ``aspects`` template (``UnknownSubspaceError``) is logged once per
        process and not asked again for an hour."""
        from nexus.db.t2.http_tuple_store import (  # noqa: PLC0415 — deferred: a table-mode worker never loads the tuple client
            ParkCapExceededError,
            UnknownSubspaceError,
        )

        claims: list[HeldClaim] = []
        order = list(range(1, self._batch_size)) + [0]
        try:
            for slot in order:
                parked = park and slot == 0 and not claims
                claimant = self.claimant(slot)
                if parked:
                    self.parked = True
                    try:
                        got = self._call(lambda t, c=claimant: t.in_(
                            SUBSPACE, dict(KEYS), claimant=c, lease_s=self._lease_s,
                            timeout_s=self._park_timeout_s,
                        ))
                    finally:
                        self.parked = False
                else:
                    got = self._call(lambda t, c=claimant: t.inp(
                        SUBSPACE, dict(KEYS), claimant=c, lease_s=self._lease_s,
                    ))
                if got is None:
                    continue
                row, claim_id = got
                claim = HeldClaim(
                    slot=slot, claimant=claimant, claim_id=claim_id, task_id=row.id,
                    attempts=int(getattr(row, "attempts", 0) or 0), row=_row_from_task(row),
                )
                with self._held_lock:
                    self._held[claim_id] = claim
                claims.append(claim)
        except UnknownSubspaceError:
            if self._unsupported_until is None:
                _log.warning(
                    _ENGINE_UNSUPPORTED_EVENT,
                    action="tuple claiming skipped; the table is still drained",
                )
            self._unsupported_until = self._clock() + UNSUPPORTED_REPROBE_S
            return claims, False
        except ParkCapExceededError:
            _log.warning("aspect_worker_park_refused", claimant=self.claimant(0))
            return claims, True
        except Exception:  # noqa: BLE001 — a claim failure must not kill the worker thread; logged, retried on the next tick
            _log.warning("aspect_worker_tuple_claim_failed", exc_info=True)
            return claims, True
        return claims, False

    # ── finishing ─────────────────────────────────────────────────────────

    def process(self, claims: list[HeldClaim]) -> None:
        """Extract the claimed tasks and give each its verdict."""
        self._abandon.clear()
        ready: list[HeldClaim] = []
        for claim in claims:
            if claim.row is None:
                _log.warning(
                    "aspect_worker_task_malformed", task_id=claim.task_id,
                    action="nack dead: a body without collection, source_path and doc_id can never be extracted",
                )
                self._nack(claim, dead=True)
                continue
            try:
                gone = self._document_gone(claim.row.doc_id)
            except Exception:  # noqa: BLE001 — an unanswerable lookup is not a missing document; extraction decides
                gone = False
            if gone:
                self._gone_verdict(claim)
                continue
            ready.append(claim)
        if not ready:
            return

        renewer = ClaimRenewer(
            ready, self._renew_one,
            interval_s=self._renew_every_s,
            deadline_s=self._deadline_s(len(ready)),
            lapse_errors=_lapse_errors(),
            clock=self._clock,
        )
        renewer.start()
        try:
            results = self._extract(ready)
        except Exception as exc:  # noqa: BLE001 — the extract callable routes its own errors; this is the backstop
            results = [ExtractRaised(exc)] * len(ready)
        finally:
            renewer.stop()

        if len(results) < len(ready):
            # An extractor that returned fewer results than claims: the unmatched
            # claims have no verdict to give, so hand them back (no attempt).
            _log.warning(
                "aspect_worker_extract_short_result",
                claims=len(ready), results=len(results),
            )
            for claim in ready[len(results):]:
                self._release(claim)
        for claim, result in zip(ready, results):
            if self._abandon.is_set():
                break
            try:
                self._verdict(claim, result)
            except Exception as exc:  # noqa: BLE001 — one task's verdict must not strand its neighbours
                _log.warning(
                    "aspect_worker_verdict_failed", task_id=claim.task_id, exc_info=True,
                )
                try:
                    self._route_failure(claim, exc)
                except Exception:  # noqa: BLE001 — the claim stays held; its lease lapses and the task is retried
                    _log.warning("aspect_worker_route_failed", task_id=claim.task_id, exc_info=True)

    def _verdict(self, claim: HeldClaim, result: Any) -> None:
        from nexus import aspect_worker as w  # noqa: PLC0415 — deferred: aspect_worker imports this module lazily
        from nexus.aspect_extractor import ExtractFail  # noqa: PLC0415 — deferred: heavy module
        from nexus.db.t2.http_document_aspects_store import (  # noqa: PLC0415 — deferred: table-mode never loads it
            AspectUpsertBadRequestError,
            AspectUpsertConflictError,
        )

        row = claim.row
        assert row is not None
        if isinstance(result, ExtractRaised):
            _log.warning(
                "aspect_worker_extract_raised", collection=row.collection,
                source_path=row.source_path, exc_info=result.exc,
            )
            self._route_failure(claim, result.exc)
            return
        if result is None:
            _log.info(
                "aspect_worker_extract_dropped_unsupported",
                collection=row.collection, source_path=row.source_path, task_id=claim.task_id,
            )
            self._ack(claim)
            return
        if isinstance(result, ExtractFail):
            if result.reason in ("infra_unavailable", "transport"):
                # Gate S3: the T3 client could not be obtained, or the read hit a
                # transport failure (T3, the manifest, an HTTP fetch). A fact about
                # this process's reach, not about the document: retry, never drop.
                _log.warning(
                    "aspect_worker_extract_infra_unavailable",
                    uri=result.uri, detail=result.detail, task_id=claim.task_id,
                    attempts=claim.attempts,
                )
                self._retry_or_dead(claim)
                return
            _log.info(
                "aspect_worker_extract_skip",
                uri=result.uri, reason=result.reason, detail=result.detail,
            )
            self._ack(claim)
            return

        attributed = w._attributed(result, row)
        if w._is_unattributable(attributed):
            _log.warning(
                "aspect_worker_unattributable_identity",
                collection=row.collection, source_path=row.source_path,
            )
            self._nack(claim, dead=True)
            return
        lapse = _lapse_errors()
        try:
            self._complete(dataclasses.asdict(attributed), claim.claim_id, claim.claimant)
        except lapse:
            # 404 or 403: the claim is not ours any more. Write nothing, do not ack.
            _log.warning(
                "aspect_worker_result_dropped_claim_lapsed",
                task_id=claim.task_id, collection=row.collection, source_path=row.source_path,
            )
            self._forget(claim)
            return
        except SupersededCollectionWriteError as exc:
            self._superseded_verdict(claim, exc)
            return
        except AspectUpsertBadRequestError as exc:
            _log.warning(
                "aspect_worker_upsert_bad_request", task_id=claim.task_id, error=str(exc)[:300],
                action="nack dead: the engine refused the record as malformed",
            )
            self._nack(claim, dead=True)
            return
        except AspectUpsertConflictError as exc:
            _log.warning(
                "aspect_worker_upsert_conflict", task_id=claim.task_id, error=str(exc)[:300],
            )
            if self._gone_now(row.doc_id):
                self._gone_verdict(claim)
            else:
                self._retry_or_dead(claim)
            return
        except httpx.HTTPStatusError as exc:
            # A bare engine 5xx on the persist call: the extraction is good and the
            # engine is unwell. The generic retry predicate accepts only
            # 429/502/503/504, so a 500 was dead-lettered (nexus-5r2sc.3.9). Any
            # other status keeps the old route (raise -> _route_failure -> dead).
            if exc.response.status_code < 500:
                raise
            _log.warning(
                "aspect_worker_persist_server_error",
                task_id=claim.task_id, status=exc.response.status_code, attempts=claim.attempts,
            )
            self._retry_or_dead(claim)
            return
        self._superseded.pop(claim.task_id, None)
        self._forget(claim)

    # ── routing ───────────────────────────────────────────────────────────

    def _route_failure(self, claim: HeldClaim, exc: BaseException) -> None:
        """A raised failure: a self fault stands the worker down (release, no
        attempt); a non-retryable one is nacked dead; a retryable one climbs the
        RDR-163 ladder."""
        from nexus import aspect_worker as w  # noqa: PLC0415 — deferred: aspect_worker imports this module lazily

        fault = w._self_fault_reason(exc)
        if fault is not None:
            self.release_held_claims()
            self._abandon.set()
            self._on_self_fault(fault, claim, exc)
            return
        if not w._is_retryable(exc):
            self._nack(claim, dead=True)
            return
        self._retry_or_dead(claim)

    def _retry_or_dead(self, claim: HeldClaim) -> None:
        from nexus import aspect_worker as w  # noqa: PLC0415 — deferred: aspect_worker imports this module lazily

        if claim.attempts >= w._RETRY_MAX_ATTEMPTS:
            _log.warning(
                "aspect_worker_retry_budget_exhausted",
                task_id=claim.task_id, attempts=claim.attempts,
            )
            self._nack(claim, dead=True)
            return
        interval = w._backoff_interval_seconds(claim.attempts)
        if self._nack(claim, retry_after_s=interval):
            _log.info(
                "aspect_worker_retry_scheduled",
                task_id=claim.task_id, attempts=claim.attempts + 1, interval_seconds=interval,
            )

    def _superseded_verdict(self, claim: HeldClaim, exc: SupersededCollectionWriteError) -> None:
        """409: the collection name was retired. The rename rewrote the task body
        to the successor, so the claim is released (no attempt counted) and the
        next claim extracts under the new name. Bounded: a task that answers 409
        a second time in this process (enqueued under a retired name after the
        rewrite) is nacked dead and logged."""
        seen = self._superseded.get(claim.task_id, 0)
        row = claim.row
        assert row is not None
        if seen >= 1:
            _log.error(
                "aspect_worker_superseded_repeat_dead",
                task_id=claim.task_id, collection=row.collection,
                successor=getattr(exc, "successor", None),
                action="second 409 for this task: nack dead",
            )
            self._superseded.pop(claim.task_id, None)
            self._nack(claim, dead=True)
            return
        self._superseded[claim.task_id] = seen + 1
        while len(self._superseded) > SUPERSEDED_MEMORY:
            self._superseded.popitem(last=False)
        _log.warning(
            "aspect_worker_superseded_released",
            task_id=claim.task_id, collection=row.collection,
            successor=getattr(exc, "successor", None),
        )
        self._release(claim)

    def _gone_verdict(self, claim: HeldClaim) -> None:
        """The catalog answered and has no such document (nexus-1o3ft). The table's
        doc_id FK refused such a row at enqueue; the tuple queue checks nothing, and
        the enqueue hook can fire (and wake a worker, ~11 ms) before the catalog row
        commits. So the FIRST claim (``attempts == 0``) is a short retryable nack on
        the ladder's first interval, never a drop; a document still gone on a later
        claim is acked as dropped. A lookup that could not answer never reaches here
        (it is not a gone verdict)."""
        from nexus import aspect_worker as w  # noqa: PLC0415 — deferred: aspect_worker imports this module lazily

        row = claim.row
        assert row is not None
        if claim.attempts == 0:
            interval = w._backoff_interval_seconds(0)
            _log.info(
                "aspect_task_doc_not_visible_yet",
                doc_id=row.doc_id, collection=row.collection, source_path=row.source_path,
                task_id=claim.task_id, retry_after_s=interval,
            )
            self._nack(claim, retry_after_s=interval)
            return
        _log.info(
            "aspect_task_dropped_missing_doc",
            doc_id=row.doc_id, collection=row.collection,
            source_path=row.source_path, task_id=claim.task_id,
        )
        self._ack(claim)

    def _gone_now(self, doc_id: str) -> bool:
        try:
            return bool(self._document_gone(doc_id))
        except Exception:  # noqa: BLE001 — unanswerable is not gone
            return False

    # ── the four claim operations ────────────────────────────────────────

    def _forget(self, claim: HeldClaim) -> None:
        with self._held_lock:
            self._held.pop(claim.claim_id, None)

    def _finish(self, claim: HeldClaim, op: str, fn: Callable[[Any], Any]) -> bool:
        """Run one claim-ending call. A lapsed claim is logged and forgotten; any
        other failure leaves the claim held, so its slot's next call re-serves it
        and the worker finishes it as its own."""
        try:
            self._call(fn)
        except _lapse_errors():
            _log.info(
                "aspect_worker_claim_lapsed_at_" + op,
                task_id=claim.task_id, claimant=claim.claimant,
            )
            self._forget(claim)
            return False
        except Exception:  # noqa: BLE001 — leave the claim held; the slot's next call re-serves it
            _log.warning(
                "aspect_worker_" + op + "_failed", task_id=claim.task_id,
                claimant=claim.claimant, exc_info=True,
            )
            return False
        self._forget(claim)
        return True

    def _ack(self, claim: HeldClaim) -> bool:
        done = self._finish(claim, "ack", lambda t: t.ack(claim.claim_id, claim.claimant))
        if done:
            self._superseded.pop(claim.task_id, None)  # terminal: no later 409 is a repeat
        return done

    def _nack(
        self, claim: HeldClaim, *, retry_after_s: int | None = None, dead: bool = False,
    ) -> bool:
        done = self._finish(
            claim, "nack",
            lambda t: t.nack(claim.claim_id, claim.claimant, retry_after_s=retry_after_s, dead=dead),
        )
        if done and dead:
            self._superseded.pop(claim.task_id, None)
        return done

    def _release(self, claim: HeldClaim) -> bool:
        return self._finish(claim, "release", lambda t: t.release(claim.claim_id, claim.claimant))

    def _renew_one(self, claim: HeldClaim) -> Any:
        return self._call(lambda t: t.renew(claim.claim_id, claim.claimant, self._lease_s))

    # ── stand-down and stop ──────────────────────────────────────────────

    def held(self) -> list[HeldClaim]:
        with self._held_lock:
            return list(self._held.values())

    def release_held_claims(self) -> int:
        """Release every claim this process still holds (no attempt counted): a
        self-fault stand-down and the daemon's stop sweep. A claim the engine
        no longer knows (it lapsed, was acked) is not an error. Returns the
        number released."""
        released = 0
        for claim in self.held():
            if self._release(claim):
                released += 1
        if released:
            _log.info("aspect_worker_released_held_claims", count=released)
        return released
