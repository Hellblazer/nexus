# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Engine-backed chash_remap facts — the RDR-186 .6 client (nexus-146xx.6).

:class:`HttpRemapStore` is the write-through + read surface for the PG
``nexus.chash_remap`` table (the ``/v1/remap`` endpoints, engine ≥ v0.1.45),
replacing the local ``chash_remap.db`` WRITE path. The local
:class:`~nexus.migration.wire_reid.ChashRemapStore` is demoted to a read-only
MIGRATION SOURCE: :func:`seed_local_map` uploads its pre-existing facts once
(idempotent upsert), and :class:`CompositeReadMapStore` keeps them visible to
the read-side probes until the seed has landed — a pre-seed install must never
probe falsely clean.

Design inputs honored (pinned on nexus-146xx.6 by the .4/.5 stacked review):

* **Torn-read reconcile** — :meth:`HttpRemapStore.all_pairs` pages the
  ``/pairs`` endpoint across multiple independent transactions (no snapshot,
  unlike the SQLite ``fetchall()`` it replaces), so it brackets the paged read
  with total-count reads and raises :class:`RemapReadTornError` on any
  mismatch. Could-not-tell is LOUD; a silently short global view would let
  ``unreflected_stores`` under-report (the no-silent-fallback class).
* **Zero-count short-circuit** — ``total == 0`` answers ``[]`` in one round
  trip with no ``/pairs`` calls at all. This is a live read, not a cached
  verdict: it re-asks the engine every call and tracks the world both
  directions (the Gap-4 property, one level up). Any richer probe-before-fetch
  marker is deliberately NOT built here — see the Gap-4 caution on the bead;
  that design belongs with the .7 detect()/verify() calling convention.
* **Batch bound** — :meth:`record_batch` groups per source collection (the
  endpoint fixes ``source_collection`` per POST) and pages at
  ``QUOTAS.MAX_RECORDS_PER_WRITE`` (300), the cap the endpoint enforces.

RF-186-1: every surface here is raw facts or live counts — no verdict is read,
written, cached, or derivable server-side.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
import structlog

from nexus.db.limits import QUOTAS
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

if TYPE_CHECKING:
    from pathlib import Path

    from nexus.migration.wire_reid import ChashRemapStore, RemapEntry

_log = structlog.get_logger(__name__)

#: Page size for the paged /pairs read (the endpoint's MAX_PAGE).
_PAIRS_PAGE: int = 1000

#: How long :meth:`HttpRemapStore.rekey` waits for a submitted job. The
#: production cutover's largest tenant took ~90s; an hour is slack for a
#: store an order of magnitude larger, and the ceiling exists so a wedged
#: job surfaces as could-not-tell rather than hanging a rung forever.
_REKEY_TIMEOUT_S: float = 3600.0

#: Gap between polls of a running rekey job. Each poll is an in-memory
#: registry read server-side, so this is about not generating thousands of
#: requests across a long rekey rather than about engine load.
_REKEY_POLL_INTERVAL_S: float = 2.0

#: The engine's answer to a poll whose job id predates the running instance.
HTTP_GONE: int = 410

#: The engine's answer to a legacy-id collision (one old id, two digests).
HTTP_CONFLICT: int = 409

#: Count keys every RekeyOps envelope carries. Used to tell a pre-b878d
#: engine's synchronous 200 envelope from some other job_id-less 2xx body:
#: absence of ``job_id`` alone is too weak a signal to hand a caller a
#: response as if it were a completed rekey.
_ENVELOPE_MARKERS: frozenset[str] = frozenset(
    {"rehashed", "collapsed_duplicates", "residual_mismatched"}
)


def _error_detail(exc: httpx.HTTPStatusError) -> str:
    """The engine's ``error`` field, falling back to the response text."""
    try:
        return str(exc.response.json().get("error", exc.response.text))
    except Exception:  # noqa: BLE001 — boundary catch: a non-JSON error body is still reportable
        return exc.response.text


class RemapReadTornError(RuntimeError):
    """The paged global read could not be reconciled against the total count.

    A row shifted past an OFFSET boundary by a concurrent write would be
    SILENTLY missing from the result; callers must treat this as
    could-not-tell (the probes' existing degraded branches), never as a
    clean short list.
    """


class RekeyJobFailedError(RuntimeError):
    """A submitted rekey job reached a terminal FAILED state (nexus-b878d).

    Known-failed: the engine reports the cause and the transaction rolled
    back. Covers both shapes the engine uses to say so — a 200 poll carrying
    ``status: "failed"``, and the 409 it returns for a legacy-id collision
    (one old id, two distinct digests). Callers get one type for one outcome
    rather than a type that depends on which status code the server picked.

    Also raised if a submit returns a body that is neither a job handle nor a
    recognisable envelope, since guessing what such a response meant is how a
    caller ends up believing a rekey happened when none did.
    """


class RekeyJobTimeoutError(RuntimeError):
    """A submitted rekey job was still running when the client gave up.

    COULD-NOT-TELL, not known-failed — the distinction the 504 collapsed and
    b878d exists to restore. The transaction may still be in flight and may
    yet commit; the server-side ``event=rekey_complete`` log is the
    authoritative record of what it did.
    """


class RekeyJobLostError(RuntimeError):
    """The engine restarted while a submitted rekey was in flight (HTTP 410).

    Also COULD-NOT-TELL, and for a subtler reason than the timeout: the
    engine commits inside its transaction scope but records the job's success
    a few steps later, so a death between the two leaves a store that HAS
    changed and a job that never reached SUCCEEDED. The engine deliberately
    reports ``store_changed: "unknown"`` rather than guessing.

    This has its own type because it is the case with the cleanest recovery:
    the rekey is idempotent, so re-running is safe and self-answering — over
    an already-rekeyed store it reports all-zero counts. Without a type, this
    would arrive as a bare ``httpx.HTTPStatusError`` indistinguishable from a
    transport fault, which is what the engine's structured payload exists to
    prevent.
    """


class HttpRemapStore(RefreshableHttpStoreMixin):
    """Thin HTTP client for the engine's ``/v1/remap`` endpoints.

    Endpoint/token resolution, tenant stamping, and 401 self-healing come
    from :class:`RefreshableHttpStoreMixin` (the f2qvx house pattern) —
    construct with no arguments in production to resolve via
    ``resolve_service_endpoint()``.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        # Test seam for exercising multi-page reads with small fixtures.
        self._page: int = _PAIRS_PAGE

    # ── write path (the .6 write-through) ───────────────────────────────────

    def record_batch(self, entries: list["RemapEntry"]) -> int:
        """Persist *entries* via the engine, grouped per source collection
        and paged at the endpoint's 300-entry cap.

        Each POST is one PG transaction server-side; paging splits a large
        batch into several. That is SAFE for the r2 ordering: the transform
        calls this strictly BEFORE the target write, so the map can only
        ever be AHEAD of the data it describes (over-recorded facts are
        harmless idempotent upserts on resume; under-recording is the bug
        the ordering forbids).

        Raises on any HTTP failure — a map fact that did not durably land
        must abort the batch before the target write (fail loud).
        """
        if not entries:
            return 0
        recorded = 0
        by_source: dict[str, list[RemapEntry]] = {}
        for entry in entries:
            by_source.setdefault(entry.source_collection, []).append(entry)
        cap = QUOTAS.MAX_RECORDS_PER_WRITE
        for source_collection, group in by_source.items():
            for start in range(0, len(group), cap):
                page = group[start:start + cap]
                result = self._post("/v1/remap/record_batch", {
                    "source_collection": source_collection,
                    "entries": [
                        {
                            "old_id": e.old_id,
                            "new_chash": e.new_chash,
                            "target_collection": e.target_collection,
                            "provenance": e.provenance,
                        }
                        for e in page
                    ],
                })
                acked = result.get("recorded") if isinstance(result, dict) else None
                if acked is None:
                    # nexus-znwc2: the old default fabricated len(page) as the
                    # durable count. This method's own contract is fail-loud —
                    # a fact whose ack cannot be read did not provably land,
                    # and the r2 ordering forbids proceeding to the target
                    # write on an unproven map.
                    raise RuntimeError(
                        "remap record_batch response carried no `recorded` "
                        f"ack for a {len(page)}-entry page "
                        f"({source_collection!r}) — cannot attest the map "
                        "facts landed; aborting before the target write"
                    )
                recorded += int(acked)
        return recorded

    # ── leg operations ──────────────────────────────────────────────────────

    def rekey(
        self,
        orphan_policy: str = "drop",
        *,
        timeout_s: float = _REKEY_TIMEOUT_S,
        poll_interval_s: float = _REKEY_POLL_INTERVAL_S,
    ) -> dict:
        """RDR-180 Item6 (nexus-jxizy.6): drive the per-tenant full-digest
        rekey. Idempotent engine-side (digest-mismatch predicate); returns
        the disposition + per-table counts envelope. A legacy-id collision
        (one old id, two digests) surfaces as the transport's HTTP-409
        error — never resolved silently.

        The envelope contract is unchanged, but the transport underneath is
        not (nexus-b878d): the endpoint now answers ``202 {job_id}`` and the
        envelope is collected by polling ``/v1/remap/rekey/{job_id}``. The
        wait lives here so this method's ``(orphan_policy) -> envelope``
        signature — what the ``chash-rekey`` rung is written against — holds
        across the change.

        Why the transport moved: synchronously the rekey ran ~90s+ at
        production scale against the tls sidecar's ~120s
        ``proxy_read_timeout``, so gate-xr789 took a 504 at 120.3s while the
        transaction COMMITTED 88s later. The operator saw a failure over a
        store that had changed. Polling keeps every individual request short,
        so no proxy is ever in a position to guess.

        Three typed outcomes, because collapsing them is what the 504 did:

        * :class:`RekeyJobFailedError` — KNOWN-FAILED. The engine reported a
          terminal failure; the transaction rolled back.
        * :class:`RekeyJobTimeoutError` — COULD-NOT-TELL. *timeout_s* elapsed
          with the job still running; it may yet commit.
        * :class:`RekeyJobLostError` — COULD-NOT-TELL. The engine restarted
          under the job (HTTP 410), so the store may or may not have changed.
          Re-running is safe: the rekey is idempotent.

        Any other transport error propagates untyped, as before.
        """
        submitted = dict(self._post("/v1/remap/rekey", {"orphan_policy": orphan_policy}))

        # Version skew converges rather than refusing (the one-engine-per-release
        # rule): an engine older than b878d answers the submit with the envelope
        # itself, having already done the work synchronously. That IS the result
        # — return it rather than failing on a missing job_id.
        #
        # But absence of job_id is too weak a signal on its own: it would also
        # accept some future 2xx that is missing job_id for an entirely
        # different reason and hand it back as a completed rekey. Require the
        # body to actually look like an envelope, and fail loud otherwise.
        job_id = submitted.get("job_id")
        if job_id is None:
            if _ENVELOPE_MARKERS & submitted.keys():
                return submitted
            raise RekeyJobFailedError(
                "rekey submit returned neither a job_id nor a recognisable "
                f"counts envelope; refusing to guess what it meant: {submitted}"
            )

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                state = dict(self._get(f"/v1/remap/rekey/{job_id}"))
            except httpx.HTTPStatusError as exc:
                # The mixin's _raise_for_status turns EVERY non-2xx into a bare
                # HTTPStatusError, which lands before the status dispatch below
                # and would flatten the two outcomes the engine encodes as
                # status codes into "some transport error". Give them back
                # their types; anything else is a genuine transport fault and
                # propagates untouched.
                code = exc.response.status_code
                if code == HTTP_GONE:
                    raise RekeyJobLostError(
                        f"rekey job {job_id} was lost to an engine restart: "
                        f"{_error_detail(exc)}"
                    ) from exc
                if code == HTTP_CONFLICT:
                    # A legacy-id collision (one old id, two digests). The
                    # engine answers it 409 rather than 200-with-status-failed,
                    # so without this it would reach the caller as a different
                    # exception type than every other known-failure — same
                    # outcome, different type, purely because of which code the
                    # server chose.
                    raise RekeyJobFailedError(
                        f"rekey job {job_id} failed: {_error_detail(exc)}"
                    ) from exc
                raise

            status = state.get("status")
            if status == "succeeded":
                return dict(state.get("envelope") or {})
            if status == "failed":
                raise RekeyJobFailedError(
                    f"rekey job {job_id} failed: {state.get('error', 'no detail reported')}"
                )
            if status != "running":
                raise RekeyJobFailedError(
                    f"rekey job {job_id} reported an unrecognized status {status!r}: {state}"
                )
            if time.monotonic() >= deadline:
                raise RekeyJobTimeoutError(
                    f"rekey job {job_id} still running after {timeout_s:.0f}s. This is "
                    f"could-not-tell, not failure: the transaction may still be in "
                    f"flight, and its outcome is recorded server-side "
                    f"(event=rekey_complete). Poll GET /v1/remap/rekey/{job_id}."
                )
            time.sleep(poll_interval_s)

    def clear_leg(self, source_collection: str, target_collection: str) -> int:
        """Clear ONE leg's rows — the (source, target) PAIR is required by
        the endpoint (a wide clear would delete a co-resident sibling leg's
        claims). The CALLER owns the D2 whole-leg ordering discipline."""
        result = self._post("/v1/remap/clear_leg", {
            "source_collection": source_collection,
            "target_collection": target_collection,
        })
        return int(result["deleted"])

    def membership(
        self, source_collection: str, target_collection: str
    ) -> tuple[int, int]:
        """LIVE ``(mapped_total, present_count)`` for one leg — computed
        fresh by ``nexus.remap_membership()`` on every call. Converged iff
        equal (including 0 == 0: nothing owed); that interpretation is the
        caller's (bead .7), never stored anywhere."""
        result = self._get("/v1/remap/membership", {
            "source_collection": source_collection,
            "target_collection": target_collection,
        })
        return int(result["mapped_total"]), int(result["present_count"])

    # ── read path (the demotion's replacement reads) ────────────────────────

    def total_count(self, source_collection: str | None = None) -> int:
        """Total fact rows (optionally one source's) — one cheap round trip;
        the reconcile bracket and the zero-fact short-circuit input."""
        params = (
            {"source_collection": source_collection} if source_collection else None
        )
        return int(self._get("/v1/remap/count", params)["total"])

    def all_pairs(self) -> list[tuple[str, str]]:
        """Every ``(old_id, new_chash)`` fact — the cascade / unreflected
        global view, count-reconciled (see module docstring).

        Raises :class:`RemapReadTornError` when the bracket counts disagree
        or the collected rows do not match the total.

        ACCEPTED RESIDUAL (reviewer-146xx-6): the reconcile detects only
        SIZE-changing races. A same-cardinality content swap between the
        bracket reads (a concurrent ``clear_leg`` of N rows plus a
        concurrent ``record_batch`` of N rows) reconciles clean while the
        snapshot is torn. Migration writes and cascade reads are
        run-sequential within one converge, so the exposure is a concurrent
        OPERATOR action mid-read — narrow, and bounded by the facts being
        idempotent upserts; revisit if a genuine concurrent-operator
        workflow ever appears.
        """
        before = self.total_count()
        if before == 0:
            return []
        pairs: list[tuple[str, str]] = []
        offset = 0
        while True:
            page = self._get(
                "/v1/remap/pairs", {"limit": self._page, "offset": offset}
            )["pairs"]
            pairs.extend((row[0], row[1]) for row in page)
            if len(page) < self._page:
                break  # partial (or empty) page = exhaustion; saves a round trip
            offset += self._page
        after = self.total_count()
        if before != after or len(pairs) != after:
            raise RemapReadTornError(
                f"paged chash_remap read could not be reconciled: "
                f"count before={before}, after={after}, rows collected={len(pairs)} "
                "— a concurrent write moved rows across page boundaries; "
                "treat as could-not-tell and retry"
            )
        return pairs

    def entries_with_targets(
        self, source_collection: str
    ) -> dict[str, tuple[str, str]]:
        """``old_id → (new_chash, target_collection)`` for one source — the
        rollback / cross-model read shape (mirrors the local store)."""
        rows = self._get(
            "/v1/remap/entries", {"source_collection": source_collection}
        )["entries"]
        return {
            row["old_id"]: (row["new_chash"], row["target_collection"])
            for row in rows
        }

    def entries_for_collection(self, source_collection: str) -> dict[str, str]:
        """``old_id → new_chash`` for one source (mirrors the local store)."""
        return {
            old: new
            for old, (new, _target) in self.entries_with_targets(source_collection).items()
        }

    def source_collections(self) -> frozenset[str]:
        """Distinct source collections — the source-gone probe input."""
        return frozenset(
            self._get("/v1/remap/source_collections")["source_collections"]
        )

    def __enter__(self) -> "HttpRemapStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


#: Suffix stamped onto a local map file whose facts have verifiably reached
#: PG. The renamed file is the read-only migration/rollback SOURCE — content
#: untouched — but no code path ever opens it as a live fact source again.
SEEDED_SUFFIX: str = ".seeded"


def seed_local_map(local: "ChashRemapStore", engine: HttpRemapStore) -> int:
    """Upload *local*'s facts into PG (idempotent upserts on the natural key).

    The primitive under :func:`seed_and_quarantine` — production code calls
    the wrapper, never this directly: an un-quarantined seed re-run after a
    rollback's ``clear_leg`` would RESURRECT the cleared claims from the
    local file (the reseed-resurrection hazard, Hal directive 2026-07-18).
    Returns the number of facts uploaded.
    """
    entries = local.all_entries()
    if not entries:
        return 0
    recorded = engine.record_batch(entries)
    _log.info("remap_local_map_seeded", facts=len(entries), recorded=recorded)
    return len(entries)


def seed_and_quarantine(map_path: "Path", engine: HttpRemapStore) -> int:
    """ONE-TIME backfill of the pre-.6 local map into PG, then quarantine.

    After every page of the seed has committed (``record_batch`` raises on
    any failure, so returning at all means every POST was a 200/committed
    PG transaction), the file is renamed ``chash_remap.db`` →
    ``chash_remap.db.seeded``. One-time is BY CONSTRUCTION, not by memory:
    the renamed file no longer matches the live name, so neither a later
    converge (re-seed) nor the composite read probes consult it — a
    rollback's engine-side ``clear_leg`` (D2 absence-encoding) can never be
    silently undone by a re-upload of stale local facts. The file itself
    survives byte-intact as the read-only migration/rollback source.

    A crash between the last committed page and the rename leaves the file
    live: the NEXT converge re-seeds (harmless idempotent upserts — the
    engine already holds the facts) and then quarantines. The re-seed
    window is that single crash gap, never steady-state. (Resurrecting a
    CLEARED leg through this gap requires the compound precondition of a
    process crash inside the seed→rename window AND an operator rollback's
    ``clear_leg`` landing before the retry — accepted as documented,
    critic-146xx-6 Q2 residual.)

    No-ops (returns 0) when the live-named file does not exist — fresh
    installs and already-quarantined installs alike.
    """
    from nexus.migration.wire_reid import ChashRemapStore  # noqa: PLC0415 — deferred to avoid import cycle

    if not map_path.exists():
        return 0
    with ChashRemapStore(map_path) as local:
        seeded = seed_local_map(local, engine)
    quarantined = map_path.with_name(map_path.name + SEEDED_SUFFIX)
    map_path.rename(quarantined)
    _log.info(
        "remap_local_map_quarantined",
        facts=seeded,
        source=str(map_path),
        quarantined=str(quarantined),
    )
    return seeded


class CompositeReadMapStore:
    """Read-only union of engine facts and (optional) local legacy facts.

    The pre-seed window's correctness guard: an install whose facts still
    live only in ``chash_remap.db`` must not probe falsely clean against an
    empty engine table. Exposes exactly the two read shapes the probes and
    the cascade consume (``all_pairs``, ``source_collections``); identical
    facts collapse, conflicting ones surface downstream via
    ``_global_view``'s existing :class:`AmbiguousRemapError` — never
    silently resolved here.
    """

    def __init__(
        self, engine: HttpRemapStore, local: "ChashRemapStore | None"
    ) -> None:
        self._engine = engine
        self._local = local

    def all_pairs(self) -> list[tuple[str, str]]:
        seen: dict[tuple[str, str], None] = {}
        for pair in self._engine.all_pairs():
            seen[pair] = None
        if self._local is not None:
            for pair in self._local.all_pairs():
                seen[(pair[0], pair[1])] = None
        return list(seen)

    def source_collections(self) -> frozenset[str]:
        sources = self._engine.source_collections()
        if self._local is not None:
            sources = sources | self._local.source_collections()
        return sources
