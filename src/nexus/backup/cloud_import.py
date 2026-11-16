# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup restore FILE``: load one ``.nxtenant`` file into the caller's own, empty tenant (nexus-csayy.6, RDR-248).

The engine holds one import session per tenant: a held transaction that stages the file's tables, then loads them
in one transaction on commit. The cloud edge ends every exchange at 60 seconds and caps a body at 20 MB, so the
file travels in parts of at most 8 MiB. This module is the client half. The wire contract is the csayy.6 entry of
``docs/wire-contract-pending.md`` (bodies, status fields, reason codes) and ``TenantImportHandler``'s class javadoc.

What the driver does, in order:

* ``GET /version``: no ``tenant_import_supported`` is refused naming the engine release that has the routes
  (:data:`TENANT_IMPORT_ENGINE`), so an older engine is never sent a request it would answer 404.
* The whole file is verified before any byte leaves (:func:`~nexus.backup.tenant_file.verify_file`: every page's
  SHA-256, every table's count, the trailer). A file that fails is refused and the engine is never opened against.
* Open with the file's header, ``header_sha256`` (the SHA-256 of the header frame's payload, which the engine's
  restore marker names) and ``row_bytes`` (the bytes the engine will stage: page bodies re-cut into parts with their COPY headers and trailers). A 409 ``session_exists`` names the open
  session's kind: it is deleted once through the matching route and the open repeated, unless it is an import
  that is already loading (deleting that would cancel the load).
* Each exported page is re-cut at row boundaries into parts of at most the engine's part size, each a complete
  binary COPY stream with its own SHA-256 header, numbered from 0 within the table. A part is sent again on a
  transport failure, a 5xx or a hash refusal (``part_sha256_mismatch``); 409 ``part_in_flight`` waits (own
  budget) and asks again; 409 ``part_already_staged`` is success (the part is in). Each table is closed with the
  trailer's row count.
* Commit (202). 503 ``restore_busy`` is retried every ``Retry-After`` (60 s) until the engine closes the session at
  its restore-wait bound, which it reports as ``failed: ...`` on the next commit or status read.
* Poll the status until ``committed`` or ``failed``, reading the body of EVERY 200 (a 200 from status or from a
  repeated commit can carry ``failed: <reason>``). After a lost connection the next move is the status route
  again; after "no such session" it is ``GET /v1/tenant/import-marker``, and the restore is reported committed
  only when that marker names this file's header SHA-256.
* A failure before the first commit attempt, and one after a ``restore_busy``, sends a best-effort DELETE for the
  session. After a commit that may have started the load, nothing is deleted: a DELETE would cancel it.

Imported by ``nexus.commands.backup_cmd`` only (``tests/test_nx_backup_pin.py``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import httpx
import structlog

from nexus._log_safe_url import log_endpoint, scrub_transport_error
from nexus.backup.cloud_export import (
    BACKOFF_LADDER,
    BUSY_WAIT_BUDGET_S,
    OPEN_WAIT_BUDGET_S,
    PAGE_ATTEMPTS,
    Credentials,
    ManagedCredentials,
    _check_bearer,
    _PAGE_TIMEOUT,
    _reason,
    _retry_after,
)
from nexus.backup.local_token import LocalRestoreCredentials
from nexus.backup.pg_backup import BackupFailed, BackupRefused
from nexus.backup.tenant_file import (
    FileIndex,
    TenantFileError,
    index_file,
    read_page_body,
    split_copy_body,
    verify_file,
)
from nexus.db.client_identity import client_identity_headers
from nexus.db.request_gate import before_request

_log = structlog.get_logger(__name__)

#: The engine release that first serves the import routes (provisional until the engine release is cut). Used in the
#: refusal message only: the run-time gate is the ``tenant_import_supported`` flag on ``/version``.
TENANT_IMPORT_ENGINE = "0.1.192"
#: The part size the client never exceeds, whatever the engine offers (the edge's body cap leaves room for 8 MiB).
MAX_PART_BYTES = 8 * 1024 * 1024
#: How often the status route is read while the load runs, in seconds.
POLL_INTERVAL_S = 2.0
#: How long a commit waits for the restore-wait bound the engine enforces before the client gives up on its own:
#: the client ends the session this long after the bound, so a silent engine cannot hold it forever.
RESTORE_WAIT_SLACK_S = 180.0
#: How long the status poll keeps going, past the engine's load bound, before the outcome is called unknown.
POLL_SLACK_S = 300.0
_RESTORE_BUSY_DEFAULT_WAIT_S = 60.0
_DELETE_TIMEOUT_S = 3.0
_PROBE_TIMEOUT_S = 5.0
_AGAIN = "Nothing was loaded into your tenant; run nx backup restore again."

#: Answers that carry a decision the caller makes, so a 5xx among them is not retried as a failed request.
_PASS_THROUGH_REASONS = frozenset({"restore_busy", "import_capacity_full", "singleton_lock_not_held", "shutting_down"})

#: The states of a stale import session a DELETE cannot hurt: its upload is staged or it has ended.
_DELETABLE_STATES = frozenset({"staging", "committed", "failed", "gone"})
#: A "Loading:" note is repeated at most this often while the status text stays the same, in seconds.
NOTE_EVERY_S = 30.0

IMPORT = "/v1/tenant/import"
EXPORT = "/v1/tenant/export"


class RestoreRefused(BackupRefused):
    """A precondition failed (engine too old, file damaged, tenant not empty); nothing was staged or loaded."""


class RestoreFailed(BackupFailed):
    """The restore did not commit. The load is one transaction, so the tenant is as it was."""


class RestoreOutcomeUnknown(BackupFailed):
    """The client lost the engine while a load may have been running and could not learn how it ended."""


class _Gone(Exception):
    """The engine answered 404 ``no_such_session`` for the client's own session."""


class _RequestFailed(RestoreFailed):
    """One logical request failed after its tries. ``transport`` is true when the engine never answered."""

    def __init__(self, message: str, *, transport: bool) -> None:
        super().__init__(message)
        self.transport = transport


def connect() -> ImportClient:
    """A client for the configured service, cloud or local. Raises :class:`RestoreRefused` when none resolves."""
    from nexus import config as _config  # noqa: PLC0415 - read through the module, so a test's patch is seen
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 - deferred, heavy

    try:
        base_url, _ = resolve_service_endpoint()
        remote = bool(_config.get_service_url())
    except RuntimeError as exc:
        raise RestoreRefused(str(exc)) from None
    creds: Credentials = ManagedCredentials(base_url, restore=True)
    if not remote:
        # A local install holds the supervisor's root token, which the import refuses: the client mints a short-lived
        # default-tenant token from it (rdr248-local-root). A token that is not the supervisor's passes through.
        creds = LocalRestoreCredentials(creds, base_url)
    return ImportClient(base_url, creds, remote=remote)


@dataclass
class Session:
    id: str
    part_bytes: int
    tables: list[str]
    skipped_tables: dict[str, str]
    notes: list[str]
    load_max_seconds: int
    max_bytes: int


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class ImportClient:
    """The engine's import routes, with the retry rules above. One instance per run."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        open_wait_budget_s: float = OPEN_WAIT_BUDGET_S,
        busy_wait_budget_s: float = BUSY_WAIT_BUDGET_S,
        remote: bool = True,
    ) -> None:
        #: A service_url is set (the managed service). Without one the engine is the local one, whose own token is root.
        self._remote = remote
        self._base = base_url.rstrip("/")
        self._shown = log_endpoint(self._base)
        self._creds = credentials
        self._http = http if http is not None else httpx.Client(timeout=_PAGE_TIMEOUT)
        self._sleep = sleep
        self._monotonic = monotonic
        self._open_budget = open_wait_budget_s
        self._busy_budget = busy_wait_budget_s
        #: Called with a one-line status when a wait would otherwise be silent.
        self.note: Callable[[str], None] = lambda _msg: None
        #: Sessions this client opened and may DELETE on the way out; one that may be loading is taken out.
        self._live: dict[str, None] = {}
        self._loading: set[str] = set()

    def close_http(self) -> None:
        self._http.close()

    def release_credentials(self) -> None:
        """Tell the credentials the run is over (a local restore revokes the token it minted). Never raises."""
        release = getattr(self._creds, "release", None)
        if release is not None:
            with contextlib.suppress(Exception):
                release()

    # -- one logical request: retried, re-authenticated, classified ------------------------------------

    def _exchange(
        self,
        method: str,
        path: str,
        *,
        what: str,
        params: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        retry_reasons: frozenset[str] = frozenset(),
        busy_reasons: frozenset[str] = frozenset({"part_in_flight"}),
    ) -> httpx.Response:
        """Send one request until it gets an answer a caller decides on: any 2xx, and any 4xx or pass-through
        503 that is not a retry or busy reason. Transport errors, other 5xx answers and *retry_reasons* take the
        attempts and the backoff ladder; *busy_reasons* wait ``Retry-After`` on their own budget."""
        failures = reauths = 0
        waited = 0.0
        while True:
            sent = self._bearer()
            all_headers = {"Authorization": f"Bearer {sent}", **client_identity_headers(), **(headers or {})}
            transport = False
            try:
                before_request()
                resp = self._http.request(method, self._base + path, params=params, content=content, headers=all_headers)
            except httpx.InvalidURL:
                self._refuse(f"the service URL ({self._shown}) is not a valid URL; check NX_SERVICE_URL.")
            except httpx.HTTPError as exc:  # transport errors, but also a body that will not decode, too many redirects
                problem, delay, transport = f"{type(exc).__name__}: {scrub_transport_error(exc, self._base)}", None, True
            else:
                status = resp.status_code
                if status < 400:
                    return resp
                reason, message = _reason(resp)
                if status == 401:
                    if reauths < 2 and self._creds.rejected(sent):
                        reauths += 1
                        continue
                    self._refuse(self._token_text(f"the service at {self._shown} rejected the token (401) and no fresh one is available."))
                if status == 403 and reason == "import_scope_forbidden":
                    self._refuse(self._token_text(
                        "a restore is made with the tenant's own tenant or data token, and the token this install sends is an "
                        "operator (root) token, which the import refuses."
                    ))
                if status == 409 and reason in busy_reasons:
                    if waited >= self._busy_budget:
                        raise _RequestFailed(f"{what}: the engine kept answering {reason} for {int(self._busy_budget)} seconds", transport=False)
                    wait = max(_retry_after(resp) or 1.0, 1.0)
                    self._sleep(wait)
                    waited += wait
                    continue
                if reason in retry_reasons or (status >= 500 and reason not in _PASS_THROUGH_REASONS):
                    problem = f"HTTP {status} {reason or message}".strip()
                    delay = _retry_after(resp) if status == 503 else None
                else:
                    return resp
            failures += 1
            if failures >= PAGE_ATTEMPTS:
                raise _RequestFailed(f"{what} failed {failures} times in a row (last: {problem})", transport=transport)
            self._sleep(delay if delay is not None else BACKOFF_LADDER[failures - 1])

    def _bearer(self) -> str:
        """The token to send, checked. A refusal after the first commit attempt says the outcome is unknown."""
        try:
            token = self._creds.bearer()
            _check_bearer(token)
        except BackupRefused as exc:
            self._refuse(str(exc))
        return token

    def _refuse(self, text: str) -> NoReturn:
        """Refuse with *text*: before the first commit attempt nothing was loaded; after it a load may be running or
        may have committed, which the message must say."""
        if self._loading:
            raise RestoreOutcomeUnknown(
                f"{text} The load had already been committed to the engine when this happened, so it may be running or "
                f"may have committed. {self._maybe_loaded()}"
            )
        raise RestoreRefused(text)

    def _token_text(self, why: str) -> str:
        if self._remote:
            return f"{why} Set a current tenant or data token with `nx config set service_token <token>`, then run the restore again."
        return (
            f"{why} This install talks to a local engine, and the token it sends is an operator token, which a restore "
            "refuses. Issue a token for the tenant you are restoring into with `nx service token issue --tenant <tenant>` (`default` for the install's own) and "
            "export it as NX_SERVICE_TOKEN (a local install reads the token from that variable; `nx config set service_token` "
            "applies only when a service_url is set), then run the restore again."
        )

    # -- the engine gate ---------------------------------------------------------------------------------

    def check_token(self) -> None:
        self._bearer()

    def check_engine(self) -> None:
        """Refuse an engine that has no import routes, naming the release that has them."""
        resp = None
        for attempt in range(3):
            before_request()
            try:
                resp = self._http.get(f"{self._base}/version", timeout=_PROBE_TIMEOUT_S, headers=client_identity_headers())
            except httpx.InvalidURL:
                self._refuse(f"the service URL ({self._shown}) is not a valid URL; check NX_SERVICE_URL.")
            except httpx.HTTPError as exc:
                raise RestoreRefused(
                    f"the nexus service at {self._shown} is unreachable ({type(exc).__name__}: "
                    f"{scrub_transport_error(exc, self._base)}). Check connectivity, then run nx backup restore again."
                ) from None
            if resp.status_code in (502, 503, 504) and attempt < 2:
                self._sleep(BACKOFF_LADDER[attempt])
                continue
            break
        assert resp is not None
        body = _json_body(resp) if resp.status_code == 200 else {}
        if not body:
            raise RestoreRefused(f"the service at {self._shown} answered /version with HTTP {resp.status_code} and no usable body; is NX_SERVICE_URL right?")
        release = str(body.get("release_version") or "unknown")
        if body.get("tenant_import_supported") is not True:
            raise RestoreRefused(
                f"the engine at {self._shown} (release {release}) has no tenant import routes, which `nx backup restore` needs. "
                f"They first ship in engine {TENANT_IMPORT_ENGINE}. Ask whoever runs the engine to upgrade it. If it is already "
                "that version or newer, the public edge in front of it may not relay `tenant_import_supported` from /version yet "
                "(it passes an allowlist of fields); tell whoever runs the edge."
            )

    # -- open ----------------------------------------------------------------------------------------------

    def open_session(self, index: FileIndex) -> Session:
        """Open the tenant's import session with the file's header. A 409 for a session left by a killed earlier run
        is deleted once (through the route of its kind) and the open repeated."""
        body = {**index.header, "header_sha256": index.header_sha256, "row_bytes": index.row_bytes}
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        deleted_stale = False
        capacity_waited = 0.0
        while True:
            resp = self._exchange(
                "POST", IMPORT, what="opening the import session", content=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 200:
                return self._parse_open(resp)
            reason, message = _reason(resp)
            data = _json_body(resp)
            status = resp.status_code
            if status == 409 and reason == "session_exists":
                stale, kind = str(data.get("session_id") or ""), str(data.get("session_kind") or "import")
                if stale and not deleted_stale:
                    deleted_stale = True
                    self._clear_stale(stale, kind)
                    continue
                raise RestoreRefused(
                    f"the tenant already has an {kind} session on the engine at {self._shown} that this run cannot clear "
                    f"(the engine closes an idle one after 10 minutes). Wait, then run nx backup restore again."
                )
            if status == 503 and reason == "import_capacity_full":
                if capacity_waited >= self._open_budget:
                    raise RestoreRefused(
                        f"the engine at {self._shown} is at its limit of concurrent import and export sessions and none freed in "
                        f"{int(self._open_budget)} seconds. Try again in a few minutes."
                    )
                wait = max(_retry_after(resp) or BACKOFF_LADDER[-1], 1.0)
                self.note(f"The engine is at its limit of concurrent sessions; waiting {wait:g} s for a slot.")
                self._sleep(wait)
                capacity_waited += wait
                continue
            if status == 409 and reason == "tenant_not_empty":
                raise RestoreRefused(self._not_empty_text(message, index))
            problems = data.get("problems")
            extra = (" " + "; ".join(str(p) for p in problems)) if isinstance(problems, list) and len(problems) > 1 else ""
            if status in (400, 409, 413, 422, 503):
                raise RestoreRefused(f"the engine refused to open the import ({status} {reason or 'error'}): {message}{extra}. Nothing was written.")
            raise RestoreFailed(f"opening the import session: the engine answered {status} {reason or message}. {_AGAIN}")

    def _parse_open(self, resp: httpx.Response) -> Session:
        data = _json_body(resp)
        if not data.get("session_id"):
            raise RestoreFailed(
                "opening the import session: the answer is not an import session (no JSON with a session_id). Something between you "
                f"and the engine may be answering for it, such as a proxy or an edge. {_AGAIN}"
            )
        sid = str(data["session_id"])
        self._live[sid] = None
        try:
            part_bytes = max(1024, min(int(data.get("part_bytes") or MAX_PART_BYTES), MAX_PART_BYTES))
        except (TypeError, ValueError):
            part_bytes = MAX_PART_BYTES
        try:
            load_max = int(data.get("load_max_seconds") or 3960)
        except (TypeError, ValueError):
            load_max = 3960
        try:
            max_bytes = int(data.get("max_bytes") or 0)
        except (TypeError, ValueError):
            max_bytes = 0
        skipped = data.get("skipped_tables") if isinstance(data.get("skipped_tables"), dict) else {}
        notes = [str(n) for n in data.get("notes") or []]
        return Session(sid, part_bytes, [str(t) for t in data.get("tables") or []],
                       {str(k): str(v) for k, v in skipped.items()}, notes, load_max, max_bytes)

    def _clear_stale(self, session_id: str, kind: str) -> None:
        """Delete the tenant's leftover session. An import is deleted only when its state was READ and is one a DELETE
        cannot hurt (staging, or already ended), or the engine says it is not there: a read that failed, or one that
        says loading, verifying or nothing at all, leaves it alone, because a DELETE during the load cancels it."""
        if kind == "import" and self._stale_import_state(session_id) not in _DELETABLE_STATES:
            raise RestoreRefused(
                "an import session of this tenant is on the engine and this run could not confirm that it is not loading "
                "(an earlier run may have started the load, and deleting a loading session cancels it), so it leaves it. "
                "Wait for it to end (the engine closes an idle one after 10 minutes), then check the tenant: if the load "
                "committed, this file's marker is on it, and running the restore again says so."
            )
        _log.info("restore_stale_session_deleted", session_id=session_id, kind=kind)
        self._delete_quietly(session_id, EXPORT if kind == "export" else IMPORT)

    def _stale_import_state(self, session_id: str) -> str | None:
        """The stale import's state when it was read as one of :data:`_DELETABLE_STATES` (or ``gone`` for an answered
        ``no_such_session``); None for anything else: a transport failure, a 5xx, a 401, a 200 with no state."""
        try:
            resp = self._exchange("GET", f"{IMPORT}/{session_id}", what="reading a stale import session")
        except BackupFailed:
            return None
        except BackupRefused:
            return None
        if resp.status_code == 200:
            state = str(_json_body(resp).get("state") or "")
            return state if state in _DELETABLE_STATES else None
        if resp.status_code == 404 and _reason(resp)[0] == "no_such_session":
            return "gone"
        return None

    def _not_empty_text(self, engine_message: str, index: FileIndex) -> str:
        text = f"the tenant is not empty, so nothing was staged ({engine_message}). A restore loads into a fresh, empty tenant only."
        with contextlib.suppress(Exception):
            marker = self.read_marker()
            if marker is not None and marker.get("header_sha256") == index.header_sha256:
                return (
                    "this file was already restored into this tenant"
                    f" (session {marker.get('session_id', '?')}, committed {marker.get('committed_at', '?')}); nothing more to do."
                )
        return text

    # -- upload ----------------------------------------------------------------------------------------------

    def put_part(self, session: Session, table: str, part: int, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        what = f"uploading table {table} part {part}"
        resp = self._exchange(
            "PUT", f"{IMPORT}/{session.id}/rows", what=what, params={"table": table, "part": str(part)}, content=data,
            headers={"X-Nexus-Part-Sha256": digest, "Content-Type": "application/octet-stream"},
            retry_reasons=frozenset({"part_sha256_mismatch"}),
        )
        if resp.status_code == 200:
            return
        reason, message = _reason(resp)
        if resp.status_code == 409 and reason == "part_already_staged":
            return  # the engine has it (a retry after a lost answer)
        self._unexpected(session, what, resp, reason, message)

    def close_table(self, session: Session, table: str, rows: int) -> None:
        what = f"closing table {table}"
        resp = self._exchange("POST", f"{IMPORT}/{session.id}/close", what=what, params={"table": table, "rows": str(rows)})
        if resp.status_code == 200:
            return
        reason, message = _reason(resp)
        if resp.status_code == 409 and reason == "row_count_mismatch":
            data = _json_body(resp)
            raise RestoreFailed(
                f"table {table}: the engine staged {data.get('staged', '?')} rows and the file's trailer says {data.get('expected', rows)}. {_AGAIN}"
            )
        self._unexpected(session, what, resp, reason, message)

    def _unexpected(self, session: Session, what: str, resp: httpx.Response, reason: str, message: str) -> None:
        if resp.status_code == 404:
            self._live.pop(session.id, None)
            raise RestoreFailed(f"{what}: the engine no longer has the import session{self._why_gone(session)}. {_AGAIN}")
        if resp.status_code == 422 and reason == "part_failed":
            self._live.pop(session.id, None)
            raise RestoreFailed(f"{what}: the engine could not stage it and closed the session ({message}). {_AGAIN}")
        raise RestoreFailed(f"{what}: the engine answered {resp.status_code} {reason or message}. {_AGAIN}")

    def _why_gone(self, session: Session) -> str:
        """Ended sessions keep their outcome for 24 hours on the status route: name the bound when it says one."""
        with contextlib.suppress(Exception):
            resp = self._exchange("GET", f"{IMPORT}/{session.id}", what="reading why the session ended")
            if resp.status_code == 200:
                status = str(_json_body(resp).get("status") or "")
                if status.startswith("failed"):
                    return f" ({status})"
        return " (it restarted, or a time bound closed it)"

    # -- commit and the wait -----------------------------------------------------------------------------------

    def read_marker(self) -> dict[str, Any] | None:
        """The tenant's restore marker, or None when it names no committed restore (absent, or unreadable)."""
        resp = self._exchange("GET", f"{IMPORT}-marker", what="reading the restore marker")
        if resp.status_code != 200:
            raise _RequestFailed(f"reading the restore marker: HTTP {resp.status_code}", transport=False)
        data = _json_body(resp)
        marker = data.get("marker")
        if data.get("present") is True and data.get("committed") is not False and isinstance(marker, dict):
            return marker
        return None

    def commit_and_wait(self, session: Session, index: FileIndex, *, poll_interval_s: float | None = None) -> Outcome:
        """Commit, retry ``restore_busy``, and read the status until the load ends. Returns the outcome only when
        the engine says ``committed`` (or the marker names this file); a ``failed`` answer is settled by the marker."""
        interval = POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s
        first_attempt = self._monotonic()
        poll_deadline: float | None = None
        need_commit = True
        last_text, last_note_at = "", -NOTE_EVERY_S
        last_seen = ""
        while True:
            if need_commit:
                try:
                    body = self._commit_once(session, first_attempt)
                except _Gone:
                    return self._after_gone(session, index)
                need_commit = False
                if poll_deadline is None:
                    poll_deadline = self._monotonic() + session.load_max_seconds + POLL_SLACK_S
                if body is None:  # the answer was lost: the next move is the status route
                    continue
            else:
                try:
                    resp = self._exchange("GET", f"{IMPORT}/{session.id}", what="reading the restore status")
                except _RequestFailed as exc:
                    if poll_deadline is not None and self._monotonic() >= poll_deadline:
                        raise RestoreOutcomeUnknown(self._unknown_text(exc, last_seen)) from None
                    self._sleep(interval)
                    continue
                if resp.status_code == 404:
                    if _reason(resp)[0] == "no_such_session":
                        return self._after_gone(session, index)
                    raise RestoreOutcomeUnknown(
                        f"reading the restore status: the engine answered 404 {_reason(resp)[0] or 'not found'}, which is not "
                        f"the engine saying it forgot the session. {self._maybe_loaded()}"
                    )
                if resp.status_code != 200:
                    raise RestoreOutcomeUnknown(
                        f"reading the restore status: the engine answered {resp.status_code} {_reason(resp)[0]}. {self._maybe_loaded()}"
                    )
                body = _json_body(resp)
            verdict = self._judge(session, index, body)
            if verdict is not None:
                return verdict
            state = str(body.get("state") or "")
            if state == "staging":
                need_commit = True  # the commit never landed (a lost request): send it again, which is idempotent
                continue
            last_seen = str(body.get("status") or state)
            if poll_deadline is not None and self._monotonic() >= poll_deadline:
                raise RestoreOutcomeUnknown(self._unknown_text(None, last_seen))
            now = self._monotonic()
            if last_seen != last_text or now - last_note_at >= NOTE_EVERY_S:
                self.note(f"Loading: {last_seen}")
                last_text, last_note_at = last_seen, now
            self._sleep(interval)

    def _commit_once(self, session: Session, first_attempt: float) -> dict[str, Any] | None:
        """One commit, with ``restore_busy`` retried. Returns the status body (None when the answer was lost);
        raises :class:`_Gone` when the engine says it has no such session."""
        while True:
            self._loading.add(session.id)  # from the moment it is sent, the load may have started
            try:
                resp = self._exchange("POST", f"{IMPORT}/{session.id}/commit", what="committing the restore")
            except _RequestFailed:
                return None
            status = resp.status_code
            reason, message = _reason(resp)
            if status in (200, 202):
                return _json_body(resp)
            if status == 503 and reason == "restore_busy":
                self._loading.discard(session.id)  # the load did not start: the upload is staged and may be deleted
                waited = self._monotonic() - first_attempt
                if waited >= session.load_max_seconds + RESTORE_WAIT_SLACK_S:
                    raise RestoreFailed(
                        f"another restore kept the engine's load slot for {int(waited)} seconds (the engine's bound is "
                        f"{session.load_max_seconds} s). {_AGAIN}"
                    )
                wait = _retry_after(resp) or _RESTORE_BUSY_DEFAULT_WAIT_S
                self.note(f"Another restore is loading; the upload stays staged. Trying again in {wait:g} s.")
                self._sleep(wait)
                continue
            if status == 404 and reason == "no_such_session":
                raise _Gone
            if status in (400, 409):  # a refusal the engine made before starting anything
                self._loading.discard(session.id)
                raise RestoreFailed(f"committing: the engine answered {status} {reason or message}. {_AGAIN}")
            raise RestoreOutcomeUnknown(
                f"committing: the engine answered {status} {reason or message}, which does not say whether the load started. {self._maybe_loaded()}"
            )

    def _judge(self, session: Session, index: FileIndex, body: dict[str, Any]) -> Outcome | None:
        """The outcome a status body states, or None while the load is still going. A body is committed only when its
        state says so and its status text does not say it failed. A ``failed`` one is read as a failure even on a 200,
        and is settled by the marker: committed only when it names this file (the engine's own words are a hint)."""
        state = str(body.get("state") or "")
        text = str(body.get("status") or "")
        notes = [str(n) for n in body.get("notes") or []]
        if state == "committed" and not text.startswith("failed"):
            self._live.pop(session.id, None)
            report = body.get("report") if isinstance(body.get("report"), dict) else {}
            return Outcome("status", report, notes, None)
        if state == "failed" or text.startswith("failed"):
            self._live.pop(session.id, None)
            marker = self._marker_or_unknown(f"the engine reports the load failed ({text or 'failed'})")
            if marker is not None and marker.get("header_sha256") == index.header_sha256:
                return Outcome("marker", {}, notes, marker)
            raise RestoreFailed(f"the engine rolled the restore back ({text or 'failed'}). The tenant is as it was. Run the restore again.")
        return None

    def _after_gone(self, session: Session, index: FileIndex) -> Outcome:
        """"No such session": the engine restarted or forgot it. Only the marker says whether the load committed."""
        self._live.pop(session.id, None)
        marker = self._marker_or_unknown("the engine no longer has the session")
        if marker is not None and marker.get("header_sha256") == index.header_sha256:
            return Outcome("marker", {}, [], marker)
        raise RestoreFailed(
            "the engine no longer has the import session and the tenant's restore marker does not name this file, so the load "
            "did not commit (it ends in one transaction, rolled back with the session). The tenant is as it was. Run the restore again."
        )

    def _marker_or_unknown(self, why: str) -> dict[str, Any] | None:
        try:
            return self.read_marker()
        except _RequestFailed:
            raise RestoreOutcomeUnknown(
                f"{why}, and the restore marker could not be read either, so whether the load committed is not known. "
                f"{self._maybe_loaded()}"
            ) from None

    @staticmethod
    def _maybe_loaded() -> str:
        return (
            "The load may still be running or may have committed: check the tenant before restoring again "
            "(running nx backup restore on the same file says so if it did commit)."
        )

    def _unknown_text(self, exc: _RequestFailed | None, last_seen: str) -> str:
        if exc is None:
            return (
                f"the engine still reports the load as running ({last_seen or 'loading'}) past its own load bound and this client's "
                f"wait for it. {self._maybe_loaded()}"
            )
        seen = f" The last status read said: {last_seen}." if last_seen else ""
        return f"the restore status could not be read to the end of the engine's load bound (last: {exc}).{seen} {self._maybe_loaded()}"

    # -- cleanup ---------------------------------------------------------------------------------------------------

    def close_all(self) -> None:
        """A best-effort DELETE for every session this client opened that cannot be loading."""
        for sid in list(self._live):
            self._live.pop(sid, None)
            if sid not in self._loading:
                self._delete_quietly(sid, IMPORT)

    def may_be_loading(self) -> bool:
        return bool(self._loading)

    def _delete_quietly(self, session_id: str, route: str) -> None:
        """DELETE the session, once, on a short timeout; every failure is swallowed (the engine's own bounds close an
        abandoned session). Never raises."""
        with contextlib.suppress(Exception):
            before_request()
            headers = {"Authorization": f"Bearer {self._bearer()}", **client_identity_headers()}
            self._http.delete(f"{self._base}{route}/{session_id}", headers=headers, timeout=_DELETE_TIMEOUT_S)


@dataclass(frozen=True)
class Outcome:
    """How a committed restore was learned: from the status body (``report`` is the engine's) or from the marker."""

    source: str  # "status" or "marker"
    report: dict[str, Any]
    notes: list[str]
    marker: dict[str, Any] | None


# -- the run ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    seconds: float
    header: dict[str, Any]
    header_sha256: str
    outcome: Outcome
    #: table name -> class, from the file's header
    classes: dict[str, str]
    open_notes: list[str] = field(default_factory=list)
    skipped_at_open: dict[str, str] = field(default_factory=dict)
    bytes_sent: int = 0


#: ``on_progress(bytes_sent, total_bytes, table, table_index, table_count)``, after each part is kept.
Progress = Callable[[int, int, str, int, int], None]


def run_restore(
    client: ImportClient,
    path: Path,
    *,
    on_progress: Progress | None = None,
    on_note: Callable[[str], None] | None = None,
    poll_interval_s: float | None = None,
) -> RestoreResult:
    """Restore *path* into the caller's own tenant.

    Raises :class:`RestoreRefused` (nothing staged or loaded), :class:`RestoreFailed` (the load did not commit),
    :class:`RestoreOutcomeUnknown` (the engine was lost mid-load and the marker could not be read), or whatever
    interrupted it. A session that cannot be loading is deleted on the way out; one that may be is left to finish.
    A token the client minted for a local restore is revoked when the run ends, however it ends."""
    try:
        return _run_restore(client, path, on_progress=on_progress, on_note=on_note, poll_interval_s=poll_interval_s)
    finally:
        client.release_credentials()


def _run_restore(
    client: ImportClient,
    path: Path,
    *,
    on_progress: Progress | None,
    on_note: Callable[[str], None] | None,
    poll_interval_s: float | None,
) -> RestoreResult:
    note = on_note or (lambda _msg: None)
    client.note = note
    started = time.monotonic()
    client.check_token()
    client.check_engine()
    note("Verifying the file.")
    try:
        verify_file(path)
        index = index_file(path, part_bytes=MAX_PART_BYTES)
    except TenantFileError as exc:
        raise RestoreRefused(f"{path} does not verify: {exc}. Nothing was sent to the engine; fix or re-take the backup.") from None
    try:
        session = client.open_session(index)
        for n in session.notes:
            note(n)
        sent = _upload(client, session, index, path, on_progress)
        note("Uploaded; starting the load.")
        outcome = client.commit_and_wait(session, index, poll_interval_s=poll_interval_s)
    except _RequestFailed as exc:
        if client.may_be_loading():
            raise
        raise RestoreFailed(f"{exc}. {_AGAIN}") from None
    except KeyboardInterrupt:
        if client.may_be_loading():
            note("Interrupted. The engine may be loading the restore; it finishes or rolls back by itself and is not cancelled.")
        raise
    finally:
        client.close_all()
    classes = {t["name"]: str(t["class"]) for t in index.header["tables"]}
    _log.info("backup_restore_done", source=outcome.source, tables=len(index.tables))
    return RestoreResult(time.monotonic() - started, index.header, index.header_sha256, outcome, classes,
                         list(session.notes), dict(session.skipped_tables), sent)


def _upload(client: ImportClient, session: Session, index: FileIndex, path: Path, on_progress: Progress | None) -> int:
    """Every table the engine will load, in its load order: pages re-cut into parts, then the table closed."""
    unknown = [t for t in session.tables if t not in index.tables]
    if unknown:
        raise RestoreFailed(f"the engine asked for table {unknown[0]}, which the file does not hold. {_AGAIN}")
    sent = 0
    total = index.row_bytes
    try:
        fh = path.open("rb")
    except OSError as exc:
        raise RestoreRefused(f"cannot read {path}: {exc.strerror or exc}") from None
    with fh:
        for n, name in enumerate(session.tables, 1):
            table = index.tables[name]
            part = 0
            for ref in table.pages:
                try:
                    body = read_page_body(fh, ref)
                except TenantFileError as exc:
                    raise RestoreFailed(f"table {name}: {exc}. {_AGAIN}") from None
                if hashlib.sha256(body).hexdigest() != ref.sha256:
                    raise RestoreFailed(f"table {name}: a page of the file no longer matches its hash (the file changed after it was verified). {_AGAIN}")
                try:
                    parts = split_copy_body(body, session.part_bytes)
                except ValueError as exc:
                    raise RestoreFailed(f"table {name}: cannot cut a page into parts: {exc}. {_AGAIN}") from None
                for data, _rows in parts:
                    if session.max_bytes and sent + len(data) > session.max_bytes:
                        raise RestoreFailed(
                            f"table {name}: the next part would take the staged bytes past the engine's cap of {session.max_bytes} "
                            f"(NX_IMPORT_MAX_BYTES). {_AGAIN}"
                        )
                    client.put_part(session, name, part, data)
                    part += 1
                    sent += len(data)
                    if on_progress is not None:
                        on_progress(min(sent, total), total, name, n, len(session.tables))
            client.close_table(session, name, table.rows)
    return sent
