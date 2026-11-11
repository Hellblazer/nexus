# SPDX-License-Identifier: AGPL-3.0-or-later
"""Client for the engine's user-started chash CHECK VALIDATE job and the manifest repair (nexus-f3yqd).

The engine validates the chash width CHECKs on ``nexus.chunks`` and ``nexus.catalog_document_chunks`` only when a user
starts the job (engine 0.1.190, Sam 2026-11-11: never at boot). ``nx t3 validate-chash-checks`` and ``nx doctor --fix``
start it here and poll it to the end. A job that ends ``done`` is not a success by itself: the verdict reads each
``refused`` and ``deferred`` entry by its reason, and ``all_validated``.

What validating buys: a NOT VALID CHECK already refuses a new bad row; VALID proves no old row breaks it, which lets
``nx doctor`` skip its conformance scan.

Routes:

- ``GET /v1/schema/chash-conformance`` (any token): read through ``HttpCatalogClient.chash_constraints_validated``.
- ``POST /v1/admin/validate-constraints`` ``{dry_run}`` (root token only, flag ``chash_constraints_validate_job_supported``)
  answers ``{job_id, created}``; ``GET .../{job_id}`` reports progress and the result. A tenant token gets 403.
- ``POST /v1/catalog/manifest/repair-chash`` ``{doc_id, dry_run, limit}`` (flag ``catalog_manifest_repair_chash_supported``):
  the repair a manifest ``bad_rows`` refusal needs. No released engine has it yet; the client says so.

There is no fallback to direct SQL: an engine without the job flag is named too old.
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
REPAIR_PATH: str = "/manifest/repair-chash"  # under the catalog client's /v1/catalog prefix
JOB_FLAG: str = "chash_constraints_validate_job_supported"
REPAIR_FLAG: str = "catalog_manifest_repair_chash_supported"
MANIFEST_TABLE: str = "nexus.catalog_document_chunks"
#: Seconds between polls; VALIDATE gives no row progress, so a poll shows the table it is on.
POLL_EVERY_S: float = 5.0
#: A progress line is printed when the table changes, and at least this often otherwise.
HEARTBEAT_S: float = 60.0
#: Consecutive failed polls tolerated (an engine restart, a gateway blip) before giving up, with backoff to 60 s.
MAX_POLL_FAILURES: int = 8


class ValidateJobError(RuntimeError):
    """The job could not be started or read. ``exit_code`` 3: this engine or these credentials cannot run it;
    1: run it again (the job was lost, or the engine could not be reached)."""

    exit_code: int = 1


class CannotRun(ValidateJobError):
    exit_code = 3


class AskOperator(CannotRun):
    """The credentials are not the engine's root token."""


def _ask_operator_text() -> str:
    from nexus.config import is_local_mode  # noqa: PLC0415 — config only

    if is_local_mode():
        return ("the chash CHECK validate job needs the engine's root token, and this install's credentials are not "
                "it: set it with `nx config set service_token <root token>` (or NX_SERVICE_TOKEN), then run this again")
    return ("the chash CHECK validate job needs the engine operator's credentials, which a managed install does not "
            "hold: ask the operator of your nexus service to run `nx t3 validate-chash-checks`")


def _flags(client: Any) -> dict[str, Any]:
    flags = client._version_flags()
    return flags if isinstance(flags, dict) else {}


def _cannot_tell_or_too_old(client: Any) -> CannotRun:
    """The job flag is absent: say why we think so (no answer at all is not 'too old')."""
    from nexus.config import is_local_mode  # noqa: PLC0415 — config only

    if not _flags(client):
        return CannotRun("could not read the engine's capabilities (it may be down or restarting): check it with "
                         "`nx doctor`, then run this again")
    if not is_local_mode():
        return AskOperator(_ask_operator_text())
    return CannotRun("this engine does not run the chash CHECK validate job (it needs engine-service 0.1.190 or "
                     "newer): upgrade with `nx upgrade`, then run this again")


def job_supported(client: Any) -> bool:
    return _flags(client).get(JOB_FLAG) is True


def repair_supported(client: Any) -> bool:
    return _flags(client).get(REPAIR_FLAG) is True


def all_validated(client: Any) -> bool | None:
    """The strict answer (both poison tables listed, every CHECK present, validated and the expected definition):
    True, False, or None when the engine cannot say."""
    probe = getattr(client, "chash_constraints_validated", None)
    try:
        answer = probe() if callable(probe) else None
    except Exception as exc:  # noqa: BLE001 — "cannot say" is the answer to any failure
        _log.info("chash_conformance_unreadable", error=str(exc))
        return None
    return answer if isinstance(answer, bool) else None


def _status_code(exc: httpx.HTTPStatusError) -> int:
    return exc.response.status_code if exc.response is not None else 0


def _error_text(exc: httpx.HTTPStatusError) -> str:
    try:
        body = exc.response.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body: the status says enough
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def poll_hint(job_id: str) -> str:
    return f"the job may still be running on the engine; follow it with `nx t3 validate-chash-checks --job {job_id}`"


def start(client: Any, *, dry_run: bool) -> dict[str, Any]:
    """Start the job (or be handed the one already running with the same ``dry_run``)."""
    if not job_supported(client):
        raise _cannot_tell_or_too_old(client)
    try:
        body = RefreshableHttpStoreMixin._post(client, JOB_PATH, {"dry_run": dry_run}, idempotent=False)
    except httpx.HTTPStatusError as exc:
        code = _status_code(exc)
        if code == 403:
            raise AskOperator(_ask_operator_text()) from exc
        if code == 409:
            raise ValidateJobError(
                f"a validate job with the other dry-run setting is already running ({_error_text(exc)}); wait for it "
                "to end, or follow it with `nx t3 validate-chash-checks --job ID`"
            ) from exc
        if code == 503:
            raise ValidateJobError(
                f"the engine cannot run the job right now ({_error_text(exc) or 'busy or starting'}); run this again "
                "in a minute"
            ) from exc
        raise ValidateJobError(f"the engine refused to start the validate job (HTTP {code}): {_error_text(exc)}") from exc
    except Exception as exc:  # noqa: BLE001 — transport, or the client's re-resolve giving up (a RuntimeError)
        raise ValidateJobError(f"could not reach the engine to start the job ({exc}); check it with `nx doctor`") from exc
    if not isinstance(body, dict) or not isinstance(body.get("job_id"), str):
        raise ValidateJobError(f"the engine answered the start with no job id: {body!r}")
    return body


class _Transient(Exception):
    """A poll that may succeed if repeated."""


def status(client: Any, job_id: str) -> dict[str, Any]:
    """One poll. A 404 means the engine no longer holds the job; 5xx and transport errors are transient."""
    try:
        body = RefreshableHttpStoreMixin._get(client, f"{JOB_PATH}/{job_id}", idempotent=True)
    except httpx.HTTPStatusError as exc:
        code = _status_code(exc)
        if code == 404:
            raise ValidateJobError(
                f"the engine no longer holds job {job_id} (it expired or the engine restarted). Start it again with "
                "`nx t3 validate-chash-checks`: it resumes, skipping every CHECK already validated"
            ) from exc
        if code == 403:
            raise AskOperator(_ask_operator_text()) from exc
        if code >= 500:
            raise _Transient(f"HTTP {code}") from exc
        raise ValidateJobError(f"could not read validate job {job_id} (HTTP {code}): {_error_text(exc)}") from exc
    except Exception as exc:  # noqa: BLE001 — transport, or the client's re-resolve giving up (a RuntimeError)
        raise _Transient(str(exc) or type(exc).__name__) from exc
    if not isinstance(body, dict) or "state" not in body:
        raise ValidateJobError(f"the engine answered job {job_id} with no state: {body!r}")
    return body


def progress_line(body: dict[str, Any], *, elapsed_s: float | None = None) -> str:
    done, total = body.get("tables_done"), body.get("tables_total")
    current = body.get("current") if isinstance(body.get("current"), dict) else None
    where = ""
    if current:
        size = current.get("bytes")
        where = f", on {current.get('table')}" + (f" ({int(size) / 1e9:.1f} GB)" if isinstance(size, int) else "")
    since = f" [{int(elapsed_s // 60)} min]" if elapsed_s is not None and elapsed_s >= 60 else ""
    return f"Validating the chash CHECKs: {done} of {total} tables{where}.{since}"


def wait(
    client: Any,
    job_id: str,
    *,
    on_progress: Callable[[str], None] | None = None,
    poll_every_s: float | None = None,
    clock: Clock = SYSTEM,
) -> dict[str, Any]:
    """Poll until the job leaves ``running``; returns the last body. Prints a progress line when the table changes
    and every HEARTBEAT_S otherwise. Up to MAX_POLL_FAILURES consecutive transient failures are ridden out with
    backoff; past that the error names the job id."""
    every = POLL_EVERY_S if poll_every_s is None else poll_every_s
    started = clock()
    last_shown: tuple[Any, Any] | None = None
    last_at = started
    failures = 0
    while True:
        try:
            body = status(client, job_id)
        except _Transient as exc:
            failures += 1
            if failures > MAX_POLL_FAILURES:
                raise ValidateJobError(f"lost touch with the engine ({exc}); {poll_hint(job_id)}") from exc
            clock_sleep(clock, min(60.0, max(every, 1.0) * 2 ** (failures - 1)))
            continue
        failures = 0
        if body.get("state") != "running":
            return body
        if on_progress is not None:
            key = (body.get("tables_done"), (body.get("current") or {}).get("table"))
            now = clock()
            if key != last_shown or now - last_at >= HEARTBEAT_S:
                on_progress(progress_line(body, elapsed_s=now - started))
                last_shown, last_at = key, now
        clock_sleep(clock, every)


@dataclass(frozen=True)
class Verdict:
    """What a finished job means. ``ok`` only when every CHECK validated (or, for a dry run, nothing would be
    refused or deferred)."""

    ok: bool
    summary: str
    lines: list[str] = field(default_factory=list)


#: A deferral reason's meaning and next step.
_DEFERRED: dict[str, str] = {
    "lock_timeout": "the table was busy; run this again",
    "deadlock_detected": "the table was busy; run this again",
    "relation_vanished": "the table changed under the job; run this again",
    "state_unreadable": "the engine could not read the CHECK's state afterwards; run this again",
    "not_validated_after": "the VALIDATE ran but the CHECK did not read valid afterwards; run this again",
    "interrupted": "the engine stopped the step (shutdown or restart); run this again",
    "stopped": "the engine stopped the step (shutdown or restart); run this again",
    "descendant_not_validated": "a partition under it is not validated yet; it follows once those are",
    "statement_timeout": ("the VALIDATE took longer than the engine allows; a rerun hits the same limit, so the "
                          "operator raises nexus.validate.statement_timeout_ms (30 minutes by default) first"),
    "constraint_absent": ("the CHECK is missing on this table, which no rerun fixes: the engine's schema needs the "
                          "operator (run `nx upgrade`, or report it)"),
    "precheck_unavailable": "the job cannot count this table's bad rows (row-level security); a real run decides it",
    "admin_cannot_see_all_tenants": "the job cannot count this table's bad rows across tenants; a real run decides it",
    "precheck_timeout": "counting this table's bad rows took too long; a real run decides it",
    "precheck_failed": "counting this table's bad rows failed; a real run decides it",
}


def _where(e: dict[str, Any]) -> str:
    return f"{e.get('table')} {e.get('constraint')}"


def _refused_lines(e: dict[str, Any], *, repair_ready: bool) -> list[str]:
    reason = e.get("reason")
    if reason == "definition_mismatch":
        return [f"refused: {_where(e)}: a CHECK of that name exists but is not the width check; this is a schema "
                "problem no row repair fixes, so report it to the operator"]
    count = e.get("bad_rows")
    rows = f"{count}{'+' if e.get('bad_rows_capped') else ''} bad row(s)" if isinstance(count, int) else "bad rows"
    out = [f"refused: {_where(e)}: {rows} break it"]
    tenants = [t for t in e.get("tenants") or [] if isinstance(t, dict)]
    if tenants:
        out.append("  tenants: " + ", ".join(f"{t.get('tenant')} ({t.get('bad_rows')})" for t in tenants))
    examples = e.get("example_chashes") or []
    if examples:
        out.append("  examples: " + ", ".join(str(x) for x in examples[:5]))
    if e.get("table") == MANIFEST_TABLE:
        out.append("  next: `nx t3 repair-chash --all` (a dry run) then `--all --apply`, re-index, run this again"
                   if repair_ready else
                   "  next: these manifest rows need the repair route (`nx t3 repair-chash`), which no released "
                   "engine has yet; nothing else to do until it lands")
    else:
        out.append("  next: no client repair exists for chunk rows yet; report it to the operator with these examples")
    return out


def _deferred_line(e: dict[str, Any]) -> str:
    reason = str(e.get("reason") or "")
    meaning = _DEFERRED.get(reason) or f"{reason or 'no reason given'}; run this again"
    return f"deferred: {_where(e)}: {meaning}"


def _count(value: Any, listed: int) -> int:
    """The engine's full count when it is an int, never less than what the capped list holds."""
    return max(value, listed) if isinstance(value, int) and not isinstance(value, bool) else listed


def verdict(body: dict[str, Any], *, repair_ready: bool = False) -> Verdict:
    """Read a finished job by its entries' reasons; ``done`` alone means nothing."""
    if body.get("state") == "failed":
        return Verdict(False, f"the validate job failed: {body.get('error') or body.get('reason') or 'no reason given'}"
                              "; run this again")
    if body.get("state") != "done":
        return Verdict(False, f"the job ended in state {body.get('state')!r}; run this again")
    refused = [e for e in body.get("refused") or [] if isinstance(e, dict)]
    deferred = [e for e in body.get("deferred") or [] if isinstance(e, dict)]
    n_refused = _count(body.get("refused_count"), len(refused))
    n_deferred = _count(body.get("deferred_count"), len(deferred))
    lines: list[str] = []
    for e in refused:
        lines += _refused_lines(e, repair_ready=repair_ready)
    lines += [_deferred_line(e) for e in deferred]
    if len(refused) < n_refused or len(deferred) < n_deferred:
        lines.append("(lists cut short by the engine; the counts are complete)")
    if body.get("dry_run"):
        would = body.get("would_validate_count", len(body.get("would_validate") or []))
        return Verdict(not n_refused and not n_deferred,
                       f"dry run: {would} would validate, {n_refused} would be refused, {n_deferred} would be deferred; "
                       "nothing was changed", lines)
    if n_refused:
        return Verdict(False, f"{n_refused} CHECK(s) refused; see each one's next step", lines)
    if n_deferred:
        return Verdict(False, f"{n_deferred} CHECK(s) deferred; see each one's next step", lines)
    if body.get("all_validated") is True:
        done = body.get("validated_count", len(body.get("validated") or []))
        return Verdict(True, f"every chash CHECK is validated ({done} by this run, "
                             f"{body.get('already_valid_count', 0)} already valid)", lines)
    return Verdict(False, "the job ended but the engine could not confirm every CHECK validated "
                          f"(all_validated={body.get('all_validated')!r}); run this again", lines)


def repair(client: Any, *, doc_id: str | None, dry_run: bool, limit: int | None = None) -> dict[str, Any]:
    """POST the manifest repair; ``doc_id`` None means every document with a bad row (up to ``limit``)."""
    if not repair_supported(client):
        raise CannotRun("this engine has no chash manifest repair route; no released engine has it yet "
                        "(nexus-f3yqd), so there is nothing to run until it lands")
    payload: dict[str, Any] = {"doc_id": doc_id, "dry_run": dry_run}
    if limit is not None:
        payload["limit"] = limit
    try:
        body = client._post(REPAIR_PATH, payload, idempotent=False, mutates=not dry_run)
    except httpx.HTTPStatusError as exc:
        raise ValidateJobError(f"the engine refused the repair (HTTP {_status_code(exc)}): {_error_text(exc)}") from exc
    except Exception as exc:  # noqa: BLE001 — transport, or the client's re-resolve giving up
        raise ValidateJobError(f"could not reach the engine ({exc}); check it with `nx doctor`") from exc
    if not isinstance(body, dict) or not isinstance(body.get("documents"), list):
        raise ValidateJobError(f"the engine answered the repair with no document list: {body!r}")
    return body


def doctor_fix_lines(client: Any, *, dry_run: bool, echo: Callable[[str], None], clock: Clock = SYSTEM) -> bool:
    """The ``nx doctor --fix`` step: start and wait for the job when the engine says a CHECK is not validated.
    Echoes what it did. Returns False only for a run it started that did not validate everything; never raises."""
    answer = all_validated(client)
    if answer is True:
        echo("chash CHECKs: already validated, nothing to run")
        return True
    if not job_supported(client):
        reason = _cannot_tell_or_too_old(client)
        echo(f"chash CHECKs: {'could not tell whether they are validated' if answer is None else 'not validated'}; "
             f"{reason}")
        return True
    job_id = None
    try:
        started = start(client, dry_run=dry_run)
        job_id = started["job_id"]
        echo(f"chash CHECKs: validate job {job_id} {'started' if started.get('created') else 'already running'}"
             f"{' (dry run)' if dry_run else ''}; if you stop this, {poll_hint(job_id)}")
        body = wait(client, job_id, on_progress=echo, clock=clock)
    except CannotRun as exc:
        echo(f"chash CHECKs: {exc}")
        return True
    except ValidateJobError as exc:
        echo(f"chash CHECKs: {exc}")
        return False
    except KeyboardInterrupt:
        echo(f"chash CHECKs: stopped following; {poll_hint(job_id)}" if job_id else "chash CHECKs: stopped")
        raise
    v = verdict(body, repair_ready=repair_supported(client))
    for line in v.lines:
        echo(f"  {line}")
    echo(f"chash CHECKs: {v.summary}")
    return v.ok
