# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client for the engine's user-started chash CHECK VALIDATE job and the manifest repair (nexus-f3yqd).

The engine validates the chash width CHECKs on ``nexus.chunks`` and ``nexus.catalog_document_chunks`` only when a user
starts the job (engine 0.1.190, Sam 2026-11-11: never at boot). ``nx t3 validate-chash-checks`` and ``nx doctor --fix``
start it here and poll it to the end. A job that ends ``done`` is not a success by itself: the verdict reads ``refused``
(the data needs the repair), ``deferred`` (rerun) and ``all_validated``.

Routes:

- ``GET /v1/schema/chash-conformance`` (any token, flag ``chash_conformance_constraints_supported``): ``all_validated``.
- ``POST /v1/admin/validate-constraints`` ``{dry_run}`` (root token only, flag ``chash_constraints_validate_job_supported``)
  answers ``{job_id, created}``; ``GET .../{job_id}`` reports progress and the result. A tenant token gets 403: on a
  managed install the operator runs it.
- ``POST /v1/catalog/manifest/repair-chash`` ``{doc_id, dry_run, limit}`` (tenant token, flag
  ``catalog_manifest_repair_chash_supported``) deletes a document's 16-byte manifest rows and marks it for re-index.

There is no fallback: an engine without the job flag is too old, and the caller says so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from nexus.clock import SYSTEM, Clock
from nexus.clock import sleep as clock_sleep
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin

_log = structlog.get_logger(__name__)

JOB_PATH: str = "/v1/admin/validate-constraints"
CONFORMANCE_PATH: str = "/v1/schema/chash-conformance"
REPAIR_PATH: str = "/manifest/repair-chash"  # under the catalog client's /v1/catalog prefix
JOB_FLAG: str = "chash_constraints_validate_job_supported"
CONFORMANCE_FLAG: str = "chash_conformance_constraints_supported"
REPAIR_FLAG: str = "catalog_manifest_repair_chash_supported"
#: Seconds between polls of a running job. VALIDATE gives no row progress, so a poll reports the table it is on.
POLL_EVERY_S: float = 5.0

TOO_OLD = (
    "this engine does not run the chash CHECK validate job (it needs engine-service 0.1.190 or newer); "
    "upgrade the engine with `nx upgrade`, then run this again"
)
ASK_OPERATOR = (
    "the chash CHECK validate job needs the engine's operator credentials, which a managed install does not hold: "
    "ask the operator of your nexus service to run `nx t3 validate-chash-checks`"
)


class ValidateJobError(RuntimeError):
    """The job could not be started or read; the message says what to do."""


@dataclass(frozen=True)
class Verdict:
    """What a finished job means. ``ok`` only when every CHECK validated."""

    ok: bool
    summary: str
    lines: list[str] = field(default_factory=list)


def _flags(client: Any) -> dict[str, Any]:
    flags = client._version_flags()
    return flags if isinstance(flags, dict) else {}


def job_supported(client: Any) -> bool:
    return _flags(client).get(JOB_FLAG) is True


def repair_supported(client: Any) -> bool:
    return _flags(client).get(REPAIR_FLAG) is True


def all_validated(client: Any) -> bool | None:
    """The conformance route's ``all_validated``: True, False, or None when the engine cannot say."""
    if _flags(client).get(CONFORMANCE_FLAG) is not True:
        return None
    try:
        body = RefreshableHttpStoreMixin._get(client, CONFORMANCE_PATH, idempotent=True)
    except Exception as exc:  # noqa: BLE001 — "cannot say" is the answer to any failure
        _log.info("chash_conformance_unreadable", error=str(exc))
        return None
    value = body.get("all_validated") if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


def _status_code(exc: httpx.HTTPStatusError) -> int:
    return exc.response.status_code if exc.response is not None else 0


def _error_text(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body: the status says enough
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def start(client: Any, *, dry_run: bool) -> dict[str, Any]:
    """Start the job (or be handed the one already running with the same ``dry_run``). Raises ValidateJobError."""
    if not job_supported(client):
        raise ValidateJobError(TOO_OLD)
    try:
        body = RefreshableHttpStoreMixin._post(client, JOB_PATH, {"dry_run": dry_run}, idempotent=False)
    except httpx.HTTPStatusError as exc:
        code = _status_code(exc)
        if code == 403:
            raise ValidateJobError(ASK_OPERATOR) from exc
        if code == 409:
            raise ValidateJobError(
                f"a validate job with the other dry-run setting is already running ({_error_text(exc)}); "
                "wait for it to end, or poll it with `nx t3 validate-chash-checks --job ID`"
            ) from exc
        raise ValidateJobError(f"the engine refused to start the validate job (HTTP {code}): {_error_text(exc)}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("job_id"), str):
        raise ValidateJobError(f"the engine answered the start with no job id: {body!r}")
    return body


def status(client: Any, job_id: str) -> dict[str, Any]:
    """One poll. Raises ValidateJobError (a 404 means the engine no longer holds the job: start it again)."""
    try:
        body = RefreshableHttpStoreMixin._get(client, f"{JOB_PATH}/{job_id}", idempotent=True)
    except httpx.HTTPStatusError as exc:
        code = _status_code(exc)
        if code == 404:
            raise ValidateJobError(
                f"the engine does not hold job {job_id} (it expired or the engine restarted); start it again: "
                "a constraint already validated is skipped"
            ) from exc
        if code == 403:
            raise ValidateJobError(ASK_OPERATOR) from exc
        raise ValidateJobError(f"could not read validate job {job_id} (HTTP {code}): {_error_text(exc)}") from exc
    if not isinstance(body, dict) or "state" not in body:
        raise ValidateJobError(f"the engine answered job {job_id} with no state: {body!r}")
    return body


def progress_line(body: dict[str, Any]) -> str:
    done, total = body.get("tables_done"), body.get("tables_total")
    current = body.get("current") if isinstance(body.get("current"), dict) else None
    where = ""
    if current:
        size = current.get("bytes")
        where = f", on {current.get('table')}" + (f" ({int(size) / 1e9:.1f} GB)" if isinstance(size, int) else "")
    return f"Validating the chash CHECKs: {done} of {total} tables{where}."


def wait(
    client: Any,
    job_id: str,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    poll_every_s: float = POLL_EVERY_S,
    clock: Clock = SYSTEM,
) -> dict[str, Any]:
    """Poll until the job leaves ``running``; returns the last body."""
    while True:
        body = status(client, job_id)
        if body.get("state") != "running":
            return body
        if on_progress is not None:
            on_progress(body)
        clock_sleep(clock, poll_every_s)


def _entry(e: Any) -> str:
    if not isinstance(e, dict):
        return str(e)
    bits = [f"{e.get('table')} {e.get('constraint')}"]
    if e.get("reason"):
        bits.append(str(e["reason"]))
    if isinstance(e.get("bad_rows"), int):
        bits.append(f"{e['bad_rows']}{'+' if e.get('bad_rows_capped') else ''} bad row(s)")
    if e.get("precheck") and e.get("precheck") != "bad_rows":
        bits.append(f"pre-check {e['precheck']}")
    return ": ".join(bits[:1]) + (" (" + "; ".join(bits[1:]) + ")" if len(bits) > 1 else "")


def verdict(body: dict[str, Any]) -> Verdict:
    """Read a finished job: refused means repair, deferred means rerun, and done alone means nothing."""
    state = body.get("state")
    if state == "failed":
        return Verdict(False, f"the validate job failed: {body.get('error') or body.get('reason') or 'no reason given'}; "
                       "start it again")
    refused = [e for e in body.get("refused") or []]
    deferred = [e for e in body.get("deferred") or []]
    validated = body.get("validated") or []
    lines: list[str] = []
    if body.get("dry_run"):
        would = body.get("would_validate") or []
        lines += [f"would validate: {_entry(e)}" for e in would]
        lines += [f"would refuse: {_entry(e)}" for e in refused]
        lines += [f"would defer: {_entry(e)}" for e in deferred]
        ok = not refused
        return Verdict(ok, f"dry run: {len(would)} would validate, {len(refused)} would be refused, "
                           f"{len(deferred)} would be deferred; nothing was changed", lines)
    for e in refused:
        lines.append(f"refused: {_entry(e)}")
        nxt = e.get("next_step") if isinstance(e, dict) else None
        if nxt:
            lines.append(f"  next: {nxt}")
    lines += [f"deferred: {_entry(e)}" for e in deferred]
    if refused:
        return Verdict(False, f"{len(refused)} CHECK(s) refused: rows that break them must be repaired first "
                              "(`nx t3 repair-chash` for a manifest row), then run this again", lines)
    if deferred:
        return Verdict(False, f"{len(deferred)} CHECK(s) deferred (the table was busy): run this again, "
                              "already-validated CHECKs are skipped", lines)
    if body.get("all_validated") is True:
        return Verdict(True, f"every chash CHECK is validated ({len(validated)} validated by this run)", lines)
    return Verdict(False, "the job ended but the engine does not report every CHECK validated "
                          f"(all_validated={body.get('all_validated')!r}); run this again", lines)


def repair(client: Any, *, doc_id: str | None, dry_run: bool, limit: int | None = None) -> dict[str, Any]:
    """POST the manifest repair; ``doc_id`` None means every document with a bad row (up to ``limit``)."""
    if not repair_supported(client):
        raise ValidateJobError(
            "this engine has no chash manifest repair (it needs a newer engine-service); upgrade with `nx upgrade`"
        )
    payload: dict[str, Any] = {"doc_id": doc_id, "dry_run": dry_run}
    if limit is not None:
        payload["limit"] = limit
    try:
        body = client._post(REPAIR_PATH, payload, idempotent=False)
    except httpx.HTTPStatusError as exc:
        raise ValidateJobError(f"the engine refused the repair (HTTP {_status_code(exc)}): {_error_text(exc)}") from exc
    if not isinstance(body, dict) or not isinstance(body.get("documents"), list):
        raise ValidateJobError(f"the engine answered the repair with no document list: {body!r}")
    return body


def doctor_fix_lines(client: Any, *, dry_run: bool, echo: Callable[[str], None]) -> bool:
    """The ``nx doctor --fix`` step: start and wait for the job when the conformance route says a CHECK is not
    validated. Echoes what it did; returns False only when a run it started did not validate everything."""
    if all_validated(client) is True:
        echo("chash CHECKs: already validated, nothing to run")
        return True
    if not job_supported(client):
        echo(f"chash CHECKs: not validated, and {TOO_OLD}")
        return True
    try:
        started = start(client, dry_run=dry_run)
        echo(f"chash CHECKs: validate job {started['job_id']} {'started' if started.get('created') else 'already running'}"
             f"{' (dry run)' if dry_run else ''}; safe to stop, it resumes where it left off")
        body = wait(client, started["job_id"], on_progress=lambda b: echo(progress_line(b)))
    except ValidateJobError as exc:
        echo(f"chash CHECKs: {exc}")
        return True if str(exc) == ASK_OPERATOR else False
    v = verdict(body)
    for line in v.lines:
        echo(f"  {line}")
    echo(f"chash CHECKs: {v.summary}")
    return v.ok
