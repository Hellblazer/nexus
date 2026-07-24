# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Engine rekey client — the SURVIVING half of the remap surface (RDR-155
P4b P0e, decision D-D).

P0e rehome (nexus-g37fr plan v3, partition record T2
``nexus/p4b-sqlite-partition-2026-07-23``): the REKEY surface — submit
``POST /v1/remap/rekey`` + poll ``GET /v1/remap/rekey/{job_id}`` (RDR-180
Item6 / nexus-b878d) — SURVIVES the combined 7.0.0 wave: its consumer is
the ``chash_rekey`` ladder rung, the standing convergence path for
pre-7.0 PG boxes with legacy half-digest keys. It moves here (next to
its :class:`~nexus.db.t2._refreshable_client.RefreshableHttpStoreMixin`
substrate and the other surviving HTTP twins), cleanly separated from
the WIRE-REID LEDGER surface (``record_batch`` / ``clear_leg`` /
``membership`` / ``count`` / ``pairs`` / ``entries`` /
``source_collections``), which DIES with RDR-187 .11's 410 set and stays
in :mod:`nexus.migration.remap_client` —
:class:`~nexus.migration.remap_client.HttpRemapStore` now EXTENDS
:class:`HttpRekeyClient` with the dying ledger methods, so remap_client
deletes WHOLE-FILE at P2 with zero surgery here. Pure move — no behavior
change.
"""
from __future__ import annotations

import time

import httpx
import structlog

from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

#: How long :meth:`HttpRekeyClient.rekey` waits for a submitted job. The
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


class HttpRekeyClient(RefreshableHttpStoreMixin):
    """Thin HTTP client for the engine's ``/v1/remap/rekey`` surface.

    Endpoint/token resolution, tenant stamping, and 401 self-healing come
    from :class:`RefreshableHttpStoreMixin` (the f2qvx house pattern) —
    construct with no arguments in production to resolve via
    ``resolve_service_endpoint()``.
    """

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

    def __enter__(self) -> "HttpRekeyClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
