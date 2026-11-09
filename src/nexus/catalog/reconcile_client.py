# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): the HTTP half of a catalog reconcile run, and ``resolve_or_register``.

:class:`~nexus.catalog.http_catalog_client.HttpCatalogClient` keeps the public methods (``reconcile_stage``,
``reconcile_commit``, ``resolve_or_register``) and hands each to :class:`ReconcileClient`, which holds the transport's
``_post`` and ``_get`` and nothing else (composition: a client this size takes no new mixin). The job contract is the
engine's (``CatalogHandler.handleReconcile*``), counted at the HTTP client by
``tests/catalog/test_http_catalog_client_reconcile.py``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
import structlog

_log = structlog.get_logger(__name__)


class CatalogReconcileError(RuntimeError):
    """A reconcile stage or commit that did not end in an outcome (RDR-193 Step 6, nexus-u5m5g.7).

    ``reason`` is the engine's own (``run_not_found``, ``incomplete_stage``, ``transient_db_error``,
    ``commit_timeout``, ``job_stopped``, ``commit_in_progress`` ...) or one the client names: ``commit_conflict``
    (another run of the owner was committing, so this run's rows were not committed, whatever became of that other
    run), ``registry_busy`` (the engine's job registry stayed full through every resubmit), ``engine_job_lost`` (the
    engine lost the job twice), ``poll_deadline`` and ``no_outcome``. ``http_status`` is the status the commit ended
    with when the engine gave one, ``job_id`` the job concerned. A raise here is never to be read as a commit: nothing
    the run staged is to be trusted.
    """

    def __init__(self, reason: str, error: str, *, http_status: int | None = None, job_id: str = "") -> None:
        super().__init__(f"catalog reconcile commit failed ({reason}): {error}")
        self.reason = reason
        self.error = error
        self.http_status = http_status
        self.job_id = job_id


@dataclass(frozen=True)
class ResolveOutcome:
    """What ``POST /v1/catalog/doc/resolve_or_register`` answered: the document, whether this call minted it, and
    whether it belongs to another owner (then it was neither adopted nor written, and ``created`` is False)."""

    tumbler: str
    created: bool
    foreign_owner: bool


#: Seconds waited before each resubmit of a reconcile commit the engine could not take (a full job registry, or a
#: transient database error): the schedule's length is the resubmit cap (three), the shared job contract's.
_RECONCILE_RESUBMIT_BACKOFF_S: tuple[float, ...] = (2.0, 5.0, 10.0)
#: Seconds between two polls of a running commit job, growing to the cap below.
_RECONCILE_POLL_INTERVAL_S: float = 0.25
_RECONCILE_POLL_INTERVAL_CAP_S: float = 2.0
#: A commit that is still running after this long is not waited for further. The engine bounds a commit's statement
#: well inside it, so a job running this long is a lost one.
_RECONCILE_POLL_DEADLINE_S: float = 1800.0


class _ReconcileJobLost(Exception):
    """A poll answered 404: the engine holds no such job (an engine restart, an expired entry)."""

    def __init__(self, job_id: str) -> None:
        super().__init__(job_id)
        self.job_id = job_id


def _error_body(resp: "httpx.Response") -> dict:
    """The JSON object of an error response, ``{}`` when it has none."""
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - an unreadable error body is an empty one
        return {}
    return body if isinstance(body, dict) else {}


class ReconcileClient:
    """Stage, commit and poll a reconcile run over the catalog client's transport (*post* and *get*)."""

    def __init__(self, post: Callable[..., Any], get: Callable[..., Any]) -> None:
        self._post = post
        self._get = get

    # ── stage ────────────────────────────────────────────────────────────────────────────────────────────────────

    def stage(self, owner: str, run_id: str, walk: str, expected_rows: int, rows: list[dict]) -> int:
        """Stage one page (at most 1000) of a run's landing rows; returns the rows the engine staged.

        Sent once: a page the engine staged whose answer was lost is a duplicate on a resend, so the transport never
        resends it, and a failure raises to the caller. A ``409`` is the engine saying the run is already being
        committed (``commit_in_progress``): it raises :class:`CatalogReconcileError` and the page is not staged.
        """
        try:
            result = self._post(
                "/reconcile/stage",
                {"owner": str(owner), "run_id": run_id, "walk": walk, "expected_rows": expected_rows, "rows": rows},
                idempotent=False,
            ) or {}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            body = _error_body(exc.response)
            raise CatalogReconcileError(
                str(body.get("reason") or "commit_in_progress"),
                str(body.get("error") or f"run {run_id} is already being committed"), http_status=409,
            ) from exc
        return int(result.get("staged", 0))

    # ── commit ───────────────────────────────────────────────────────────────────────────────────────────────────

    def commit(self, owner: str, run_id: str) -> dict:
        """Commit a staged run as an engine job, poll it to its end and return the outcome (RDR-193 Part A1).

        The shared job contract, each branch counted by ``test_http_catalog_client_reconcile``:

        - ``409`` on the submit: another run of the owner is committing. Its job is polled to its end and this run is
          never submitted again; its outcome is another run's, so the call then raises ``commit_conflict`` (also when
          that job was lost or failed: this run was not committed either way, and the other job's own reason is not
          this run's).
        - ``404`` on a poll (an engine restart lost the job): ``engine_job_lost`` is logged and the commit is
          resubmitted ONCE (a second loss raises). The resubmit is safe: a run the engine already committed answers
          its stored outcome and writes nothing.
        - ``503`` on the submit (the registry is full), or a ``failed`` job whose reason is ``transient_db_error``
          (nothing was written, the run is still staged): resubmitted at most three times with backoff, then raised.
        - any other ``failed``: raised with the engine's reason and message. Nothing is resubmitted and the caller
          must not go on as if the run were committed.

        The submit goes through the transport's own retry (it is idempotent: for one ``{owner, run_id}`` the engine
        answers the job already running, or the stored outcome of a run it already committed), which rides out a
        gateway 502/503/504 and one connection reset. The resubmit ladder above is separate and counts only what the
        transport gave up on, so the two multiply but stay bounded: at most four submits (the first and three
        resubmits), each sent at most four times by the transport, with the ladder's own 2+5+10 s between submits.
        """
        body = {"owner": str(owner), "run_id": run_id}
        resubmits = 0
        lost_once = False
        while True:
            try:
                job = self._post("/reconcile/commit", body)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 409:
                    self._conflict(owner, run_id, _error_body(exc.response), exc)
                if status == 503:
                    if resubmits >= len(_RECONCILE_RESUBMIT_BACKOFF_S):
                        raise CatalogReconcileError(
                            "registry_busy", str(_error_body(exc.response).get("error") or "the job registry is full"),
                            http_status=503,
                        ) from exc
                    _log.warning("catalog_reconcile_commit_busy_resubmit", owner=str(owner), run_id=run_id,
                                 resubmit=resubmits + 1)
                    time.sleep(_RECONCILE_RESUBMIT_BACKOFF_S[resubmits])
                    resubmits += 1
                    continue
                raise
            job_id = str((job or {}).get("job_id") or "")
            if not job_id:
                raise CatalogReconcileError("no_job", f"the engine accepted the commit without a job id: {job!r}")
            try:
                return self._poll(job_id)
            except _ReconcileJobLost:
                if lost_once:
                    raise CatalogReconcileError(
                        "engine_job_lost", f"the engine lost the commit job twice (job {job_id}), run {run_id}",
                        job_id=job_id,
                    ) from None
                lost_once = True
                _log.warning("engine_job_lost", job_id=job_id, owner=str(owner), run_id=run_id)
            except CatalogReconcileError as err:
                if err.reason != "transient_db_error" or resubmits >= len(_RECONCILE_RESUBMIT_BACKOFF_S):
                    raise
                _log.warning("catalog_reconcile_commit_transient_resubmit", owner=str(owner), run_id=run_id,
                             resubmit=resubmits + 1, job_id=job_id)
                time.sleep(_RECONCILE_RESUBMIT_BACKOFF_S[resubmits])
                resubmits += 1

    def _conflict(self, owner: str, run_id: str, body: dict, cause: Exception) -> None:
        """The 409 branch: wait for the named job, then raise ``commit_conflict``. Never returns."""
        other = str(body.get("job_id") or "")
        outcome_of_other = "ended"
        if other:
            try:
                self._poll(other)  # waited for, outcome discarded: it is another run's
            except _ReconcileJobLost:
                outcome_of_other = "was lost by the engine"
            except CatalogReconcileError as err:
                outcome_of_other = f"failed ({err.reason})"
        raise CatalogReconcileError(
            "commit_conflict",
            f"another run of owner {owner} was committing (job {other or 'unnamed'}, which {outcome_of_other}), so run "
            f"{run_id} was not committed; its rows stay staged until they are reaped, run again once that commit is "
            f"over",
            http_status=409, job_id=other,
        ) from cause

    def _poll(self, job_id: str) -> dict:
        """Poll one reconcile job to its end: the outcome of a ``succeeded`` job, a raise for a ``failed`` one,
        :class:`_ReconcileJobLost` for a 404 (the engine holds no such job)."""
        deadline = time.monotonic() + _RECONCILE_POLL_DEADLINE_S
        interval = _RECONCILE_POLL_INTERVAL_S
        while True:
            try:
                status = self._get(f"/reconcile/{job_id}")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise _ReconcileJobLost(job_id) from exc
                raise
            state = (status or {}).get("status")
            if state == "succeeded":
                outcome = status.get("outcome")
                if not isinstance(outcome, dict):
                    raise CatalogReconcileError("no_outcome", f"job {job_id} succeeded without an outcome",
                                                http_status=status.get("http_status"), job_id=job_id)
                return outcome
            if state == "failed":
                raise CatalogReconcileError(
                    str(status.get("reason") or "failed"), str(status.get("error") or "the commit failed"),
                    http_status=status.get("http_status"), job_id=job_id,
                )
            if time.monotonic() >= deadline:
                raise CatalogReconcileError(
                    "poll_deadline", f"job {job_id} was still {state or 'unanswered'} after "
                    f"{_RECONCILE_POLL_DEADLINE_S:.0f} s", job_id=job_id,
                )
            time.sleep(interval)
            interval = min(interval * 2 if interval else 0.0, _RECONCILE_POLL_INTERVAL_CAP_S)

    # ── resolve_or_register ──────────────────────────────────────────────────────────────────────────────────────

    def resolve_or_register(
        self,
        owner_name: str,
        *,
        source_uri: str = "",
        file_path: str = "",
        title: str = "",
        year: int | None = None,
        title_fallback: bool = False,
        source_mtime: float | None = None,
        content_type: str | None = None,
        physical_collection: str | None = None,
        sensitive: bool = False,
    ) -> ResolveOutcome:
        """Resolve a document of a curator owner by ``source_uri`` or register it, in one request (RDR-193 Part A1).

        A live document holding the source URI under another owner is answered as ``foreign_owner`` and left alone;
        one under this owner is returned with its empty title and zero year filled from this call; with
        ``title_fallback`` a ghost of the owner with exactly this title becomes the document; otherwise the document is
        registered. ``sensitive`` applies only when this call registers a new row (it is then born flagged); a row that
        is found keeps its sensitivity. Give ``source_uri``, or an absolute ``file_path`` the engine derives one from.
        """
        if not source_uri and not file_path:
            raise ValueError("resolve_or_register needs a source_uri, or an absolute file_path to derive one from")
        body: dict[str, Any] = {"owner_name": owner_name}
        if source_uri:
            body["source_uri"] = source_uri
        if file_path:
            body["file_path"] = file_path
        body["title"] = title
        if year is not None:
            body["year"] = year
        if title_fallback:
            body["title_fallback"] = True
        if source_mtime is not None:
            body["source_mtime"] = source_mtime
        if content_type is not None:
            body["content_type"] = content_type
        if physical_collection is not None:
            body["physical_collection"] = physical_collection
        if sensitive:
            body["sensitive"] = True
        result = self._post("/doc/resolve_or_register", body) or {}
        foreign = bool(result.get("foreign_owner"))
        return ResolveOutcome(str(result["tumbler"]), False if foreign else bool(result.get("created", False)), foreign)
