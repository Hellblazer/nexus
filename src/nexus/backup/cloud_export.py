# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx backup`` in cloud mode: pull one tenant's export session into one verified file (nexus-csayy.3, RDR-248).

The engine holds one read-only snapshot per export session and serves it in pages, because the cloud edge ends
every exchange at 60 seconds and caps a body at 20 MB. This module is the client half: it opens the session,
pulls every table page by page into a ``.partial`` file made with :mod:`nexus.backup.tenant_file`, closes the
session, and moves the file into place. The wire contract is ``docs/wire-contract-pending.md`` (entry bbb8d7b2b)
and ``TenantExportHandler``'s class javadoc.

What the driver does on each answer (the csayy.2 review gate's client rules):

* ``GET /version`` first: no ``tenant_export_supported`` is refused naming the engine release that has the routes
  (:data:`TENANT_EXPORT_ENGINE`), so an older engine is never sent a request it would answer 404.
* A 401 asks the credentials for a fresh token and repeats the SAME request, from the same key: a token that
  expires mid-export costs nothing, because the session belongs to the tenant and not to the token. A root token
  gets 403 ``export_scope_forbidden`` and is refused with what to use instead.
* A page that fails (transport error, 5xx, a hash or row count that is not the page's) is asked for again from
  the same ``after`` key, up to :data:`PAGE_ATTEMPTS` times, waiting :data:`BACKOFF_LADDER` between tries; a 503
  or 409 ``page_in_flight`` with ``Retry-After`` waits that long instead. When the tries run out the session is
  closed, the partial file removed, and the message says to run again.
* A 404 or 410 on the client's OWN session id means the engine forgot it (it restarted, the session expired, the
  request reached another instance): start over with a new session, at most :data:`MAX_RESTARTS` times, and past
  the session's ``expires_at`` fail naming the engine's limit instead.
* 409 ``export_session_exists`` carries the id of the tenant's open session (from a killed earlier run): it is
  deleted once and the open repeated.
* Any exit, an interrupt included, sends a best-effort DELETE and removes the partial file.

Imported by ``nexus.commands.backup_cmd`` only (``tests/test_nx_backup_pin.py``).
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog

from nexus._log_safe_url import log_endpoint, scrub_transport_error
from nexus.backup.pg_backup import BackupFailed, BackupRefused, _publish, already_there, cannot_write
from nexus.backup.tenant_file import (
    ENGINE_FORMAT_VERSIONS,
    TenantFileError,
    TenantFileWriter,
    count_copy_rows,
    count_true_in_column,
    verify_file,
)
from nexus.db.client_identity import client_identity_headers
from nexus.db.request_gate import before_request

_log = structlog.get_logger(__name__)

#: The engine release that first serves the export routes (engine-service-v0.1.191, RDR-248 Step 2). Used in the
#: refusal message only: the run-time gate is the ``tenant_export_supported`` flag on ``/version``, never this number.
TENANT_EXPORT_ENGINE = "0.1.191"
#: Waits between the tries of one request that failed without a ``Retry-After``; one more try than there are waits.
BACKOFF_LADDER: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)
PAGE_ATTEMPTS = len(BACKOFF_LADDER) + 1
#: How many times a forgotten session is replaced with a new one before the run gives up.
MAX_RESTARTS = 2
#: How long an open waits in total for a free export slot (503 ``export_capacity_full``) before it refuses.
OPEN_WAIT_BUDGET_S = 300.0
_RETRY_AFTER_CAP_S = 120.0
_DELETE_TIMEOUT_S = 3.0
_PAGE_TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=60.0, pool=10.0)
_PROBE_TIMEOUT_S = 5.0
_AGAIN = "The backup was not kept; run nx backup again."

#: What a backup leaves out (RDR-248 "The classes": the ``excluded`` registry entries), printed after every run.
OMITTED_NOTICE = (
    "Not in this backup: tuple-space state (tuples, claims, deliveries), in-flight PDF and re-embed jobs, "
    "session scratch (T1) and operational logs (the request ledger, tier writes, routing events and the like). "
    "None of it is your documents or notes, and all of it is rebuilt or restarts empty."
)


class ExportRefused(BackupRefused):
    """A precondition failed (engine too old, token refused, destination taken); nothing was written."""


class Credentials(Protocol):
    """Where the bearer comes from, asked afresh for every request."""

    def bearer(self) -> str: ...

    def rejected(self, sent: str) -> bool:
        """The engine answered 401 to *sent*. True when a different bearer is now available (re-minted, or
        re-read from config); False when this one is all there is."""


class ManagedCredentials:
    """The cloud client's token: a minted data token when ``mint_token`` is configured, else the static
    ``service_token`` re-read from env and config on every call (so a rotated token is picked up)."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def bearer(self) -> str:
        from nexus.db.data_token import DataTokenMintError, get_data_token_manager  # noqa: PLC0415 - deferred, heavy
        from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 - deferred, heavy

        manager = get_data_token_manager()
        try:
            minted = manager.bearer_for(self._base_url, DEFAULT_TENANT) if manager.is_configured() else None
        except DataTokenMintError as exc:
            raise ExportRefused(f"cannot get a data token to read the export with: {exc}") from None
        return minted or self._static()

    def _static(self) -> str:
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 - deferred, heavy

        try:
            _, token = resolve_service_endpoint()
        except RuntimeError as exc:
            raise ExportRefused(str(exc)) from None
        if not token:
            raise ExportRefused(
                "no service_token is configured, and an export needs a tenant or data token. Set one with "
                "`nx config set service_token <token>` (or export NX_SERVICE_TOKEN) and run nx backup again."
            )
        return token

    def rejected(self, sent: str) -> bool:
        from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 - deferred, heavy
        from nexus.db.t2._refreshable_client import DEFAULT_TENANT  # noqa: PLC0415 - deferred, heavy

        manager = get_data_token_manager()
        if manager.is_configured():
            return bool(manager.invalidate_if_current(self._base_url, DEFAULT_TENANT, sent))
        try:
            return self._static() != sent
        except ExportRefused:
            return False


def connect() -> ExportClient:
    """A client for the configured managed service. Raises :class:`ExportRefused` when none resolves."""
    from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 - deferred, heavy

    try:
        base_url, _ = resolve_service_endpoint()
    except RuntimeError as exc:
        raise ExportRefused(str(exc)) from None
    return ExportClient(base_url, ManagedCredentials(base_url))


# -- the engine's answers ------------------------------------------------------------------------------


@dataclass
class Session:
    id: str
    header: dict[str, Any]
    expires_at: datetime | None
    total_seconds: int


@dataclass(frozen=True)
class Page:
    table: str
    rows: int
    last_key: str | None
    done: bool
    sha256: str
    table_rows: int | None
    body: bytes


class _SessionGone(Exception):
    """404 or 410 on the client's own session id."""


class _SessionExists(Exception):
    def __init__(self, session_id: str | None) -> None:
        super().__init__("export_session_exists")
        self.session_id = session_id


class _Unfit(Exception):
    """A 200 whose page does not verify: retried like a failed request."""


def default_file_name(tenant: str, *, now: datetime | None = None) -> str:
    """``nexus-<tenant>-<UTC yyyymmddThhmmssZ>.nxtenant``, the tenant id reduced to filename-safe characters."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in tenant) or "tenant"
    return f"nexus-{safe}-{stamp}.nxtenant"


def _reason(resp: httpx.Response) -> tuple[str, str]:
    """``(reason code, message)`` from the engine's ``{"error": message, "reason": code}`` body, tolerantly."""
    try:
        body = resp.json()
    except ValueError:
        return "", resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("reason") or ""), str(body.get("error") or "")[:300]
    return "", ""


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return min(max(float(raw), 0.0), _RETRY_AFTER_CAP_S) if raw is not None else None
    except ValueError:
        return None


def _parse_instant(text: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


class ExportClient:
    """The engine's three export routes, with the retry rules above. One instance per run."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        open_wait_budget_s: float = OPEN_WAIT_BUDGET_S,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._shown = log_endpoint(self._base)
        self._creds = credentials
        self._http = http if http is not None else httpx.Client(timeout=_PAGE_TIMEOUT)
        self._sleep = sleep
        self.now = now
        self._open_budget = open_wait_budget_s

    def close_http(self) -> None:
        self._http.close()

    # -- one logical request: retried, re-authenticated, classified -------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        what: str,
        own_session: bool = False,
        parse: Callable[[httpx.Response], Any] = lambda r: r,
        busy_budget_s: float = 0.0,
    ) -> Any:
        failures = 0
        reauths = 0
        waited = 0.0
        while True:
            sent = self._creds.bearer()
            headers = {"Authorization": f"Bearer {sent}", **client_identity_headers()}
            try:
                before_request()
                resp = self._http.request(method, self._base + path, params=params, headers=headers)
            except httpx.TransportError as exc:
                problem, delay = f"{type(exc).__name__}: {scrub_transport_error(exc, self._base)}", None
            else:
                status = resp.status_code
                if status == 200:
                    try:
                        return parse(resp)
                    except _Unfit as exc:
                        problem, delay = str(exc), None
                else:
                    reason, message = _reason(resp)
                    retry_after = _retry_after(resp)
                    if status == 401:
                        if reauths < 2 and self._creds.rejected(sent):
                            reauths += 1
                            continue
                        raise ExportRefused(
                            f"the service at {self._shown} rejected the token (401) and no fresh one is available. "
                            "Set a current tenant or data token with `nx config set service_token <token>`, then run nx backup again."
                        )
                    if status == 403:
                        if reason == "export_scope_forbidden":
                            raise ExportRefused(
                                "an export is read with the tenant's own tenant or data token, and this install's "
                                "token is an operator (root) token, which the export refuses. Use a tenant or data token "
                                "(`nx config set service_token <token>`)."
                            )
                        raise ExportRefused(f"the service refused this token for the export (403 {reason or message})")
                    if status in (404, 410) and own_session:
                        raise _SessionGone(reason or str(status))
                    if status == 409 and reason == "export_session_exists":
                        raise _SessionExists(_session_id(resp))
                    if status == 503 and reason == "export_capacity_full" and busy_budget_s > 0:
                        if waited >= busy_budget_s:
                            raise ExportRefused(
                                f"the engine at {self._shown} is at its limit of concurrent export sessions and none freed in "
                                f"{int(busy_budget_s)} seconds. Try again in a few minutes."
                            )
                        wait = retry_after if retry_after is not None else BACKOFF_LADDER[-1]
                        self._sleep(wait)
                        waited += wait
                        continue
                    if status in (400, 404, 405, 409) and not (status == 409 and reason == "page_in_flight"):
                        raise BackupFailed(f"{what}: the engine answered {status} {reason or message}. {_AGAIN}")
                    problem = f"HTTP {status} {reason or message}".strip()
                    delay = retry_after if status in (503, 409) else None
            failures += 1
            if failures >= PAGE_ATTEMPTS:
                raise BackupFailed(f"{what} failed {failures} times in a row (last: {problem}). {_AGAIN}")
            self._sleep(delay if delay is not None else BACKOFF_LADDER[failures - 1])

    # -- the routes ----------------------------------------------------------------------------------

    def check_engine(self) -> None:
        """Refuse an engine that has no export routes, or one whose export format this client does not read."""
        before_request()
        try:
            resp = self._http.get(f"{self._base}/version", timeout=_PROBE_TIMEOUT_S, headers=client_identity_headers())
        except httpx.TransportError as exc:
            raise ExportRefused(
                f"the nexus service at {self._shown} is unreachable ({type(exc).__name__}: "
                f"{scrub_transport_error(exc, self._base)}). Check connectivity, then run nx backup again."
            ) from None
        try:
            body = resp.json() if resp.status_code == 200 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ExportRefused(f"the service at {self._shown} answered /version with HTTP {resp.status_code} and no usable body; is NX_SERVICE_URL right?")
        release = str(body.get("release_version") or "unknown")
        if body.get("tenant_export_supported") is not True:
            raise ExportRefused(
                f"the engine at {self._shown} (release {release}) has no tenant export routes, which cloud `nx backup` needs. "
                f"They first ship in engine {TENANT_EXPORT_ENGINE}. Ask whoever runs the engine to upgrade it."
            )
        fmt = body.get("tenant_export_format_version")
        if fmt not in ENGINE_FORMAT_VERSIONS:
            raise ExportRefused(
                f"the engine at {self._shown} writes export format {fmt}, and this nx reads format "
                f"{', '.join(str(v) for v in sorted(ENGINE_FORMAT_VERSIONS))}. Upgrade nx (`nx upgrade`)."
            )

    def open_session(self) -> Session:
        """Open the tenant's session. A session left by a killed earlier run is deleted once and the open repeated."""
        deleted_stale = False
        while True:
            try:
                body = self._request(
                    "POST", "/v1/tenant/export", what="opening the export session",
                    parse=lambda r: r.json(), busy_budget_s=self._open_budget,
                )
            except _SessionExists as exc:
                if deleted_stale or not exc.session_id:
                    raise ExportRefused(
                        f"the tenant already has an export session on the engine at {self._shown} that this run cannot "
                        "clear (the engine closes an idle one after 10 minutes). Wait, then run nx backup again."
                    ) from None
                deleted_stale = True
                _log.info("backup_stale_export_session_deleted", session_id=exc.session_id)
                self._delete_quietly(exc.session_id)
                continue
            total = int(body.get("total_timeout_seconds") or 3600)
            return Session(str(body["session_id"]), body, _parse_instant(body.get("expires_at")), total)

    def page(self, session: Session, table: str, after: str | None, columns: int) -> Page:
        params = {"table": table, **({"after": after} if after is not None else {})}

        def parse(resp: httpx.Response) -> Page:
            return _verified_page(resp, table, columns)

        return self._request(
            "GET", f"/v1/tenant/export/{session.id}/rows", params=params, own_session=True, parse=parse,
            what=f"reading table {table}" + (f" after key {after}" if after else ""),
        )

    def close(self, session: Session) -> None:
        self._delete_quietly(session.id)

    def _delete_quietly(self, session_id: str) -> None:
        """DELETE the session, once, on a short timeout; every failure is swallowed (the engine's own bounds
        close an abandoned session). Never raises."""
        with contextlib.suppress(Exception):
            before_request()
            headers = {"Authorization": f"Bearer {self._creds.bearer()}", **client_identity_headers()}
            self._http.delete(f"{self._base}/v1/tenant/export/{session_id}", headers=headers, timeout=_DELETE_TIMEOUT_S)


def _session_id(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    sid = body.get("session_id") if isinstance(body, dict) else None
    return str(sid) if sid else None


def _verified_page(resp: httpx.Response, table: str, columns: int) -> Page:
    import hashlib  # noqa: PLC0415 - one use

    h = resp.headers
    body = resp.content
    try:
        rows = int(h["X-Nexus-Export-Rows"])
        claimed = h["X-Nexus-Export-Sha256"]
        done = h["X-Nexus-Export-Done"].strip().lower() == "true"
        table_rows = int(h["X-Nexus-Export-Table-Rows"]) if "X-Nexus-Export-Table-Rows" in h else None
    except (KeyError, ValueError) as exc:
        raise _Unfit(f"a page answer without its {exc} header") from None
    if h.get("X-Nexus-Export-Table") != table:
        raise _Unfit(f"a page of table {h.get('X-Nexus-Export-Table')!r} for a request for {table!r}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != claimed:
        raise _Unfit(f"the page's SHA-256 is {actual}, the engine said {claimed}")
    try:
        counted = count_copy_rows(body, fields=columns)
    except ValueError as exc:
        raise _Unfit(f"the page is not a clean binary COPY stream: {exc}") from None
    if counted != rows:
        raise _Unfit(f"the engine said {rows} rows and the page holds {counted}")
    return Page(table, rows, h.get("X-Nexus-Export-Last-Key"), done, claimed, table_rows, body)


# -- the run -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    path: Path
    size: int
    seconds: float
    header: dict[str, Any]
    tables: list[tuple[str, str, int, int]]  # name, class, rows, pages
    sensitive_documents: int
    restarts: int = 0
    rows_by_class: dict[str, int] = field(default_factory=dict)


#: ``on_progress(bytes_written, table, table_index, table_count, rows_so_far)``, after each page is kept.
Progress = Callable[[int, str, int, int, int], None]


def run_export(
    client: ExportClient,
    destination: Path | Callable[[str], Path],
    *,
    on_progress: Progress | None = None,
    on_note: Callable[[str], None] | None = None,
    max_restarts: int = MAX_RESTARTS,
) -> ExportResult:
    """Export the caller's tenant to *destination* (a path, or a function of the tenant id that makes one).

    Raises :class:`ExportRefused` (nothing written), :class:`BackupFailed` (the run failed; no partial file),
    or whatever interrupted it (SystemExit, KeyboardInterrupt) after sending a best-effort DELETE and
    removing the partial file."""
    note = on_note or (lambda _msg: None)
    if isinstance(destination, Path) and os.path.lexists(destination):
        raise already_there(destination)
    client.check_engine()
    started = time.monotonic()
    session: Session | None = None
    tmp: Path | None = None
    fh: Any = None
    try:
        for restart in range(max_restarts + 1):
            session = client.open_session()
            header = _checked_header(session)
            if tmp is None:
                dest = destination(str(header["tenant_id"])) if callable(destination) else destination
                if os.path.lexists(dest):
                    raise already_there(dest)
                tmp, fh = _open_partial(dest)
            else:
                fh.seek(0)
                fh.truncate()
            writer = TenantFileWriter(fh)
            try:
                tables, sensitive = _pull(client, session, header, writer, on_progress)
            except _SessionGone:
                gone, session = session, None
                if gone.expires_at is not None and client.now() >= gone.expires_at:
                    raise BackupFailed(
                        f"the engine ended the export session: it allows {gone.total_seconds // 60}-minute sessions and this "
                        f"export did not finish in time. {_AGAIN} A large tenant needs a faster connection or a quieter "
                        "time of day; the limit is the engine's, not this client's."
                    ) from None
                if restart == max_restarts:
                    raise BackupFailed(
                        f"the engine forgot the export session {max_restarts + 1} times (it restarted, or the request "
                        f"reached another instance). {_AGAIN}"
                    ) from None
                note("The engine ended the export session; starting over with a new one.")
                continue
            break
        assert session is not None and tmp is not None and fh is not None  # the loop only exits by break or raise
        trailer = writer.finish()
        fh.flush()
        os.fsync(fh.fileno())
        size = writer.bytes_written
        fh.close()
        fh = None
        client.close(session)
        session = None
        note("Checking the finished file.")
        try:
            verify_file(tmp)  # the bytes on disk, not the bytes written: catches a bad disk and any writer bug
        except TenantFileError as exc:
            raise BackupFailed(f"the finished file does not verify: {exc}. {_AGAIN}") from None
        _publish(tmp, dest)
        by_class: dict[str, int] = {}
        for _name, cls, rows, _pages in tables:
            by_class[cls] = by_class.get(cls, 0) + rows
        _log.info("backup_cloud_export_done", tenant=header["tenant_id"], bytes=size, rows=trailer["total_rows"])
        return ExportResult(dest, size, time.monotonic() - started, header, tables, sensitive, restart, by_class)
    except OSError as exc:
        raise BackupFailed(f"cannot write the backup: {exc.strerror or exc}. Free some space or pick another path with --to. {_AGAIN}") from None
    finally:
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.close()
        if session is not None:
            client.close(session)  # best effort, never raises: a killed or failed run releases the snapshot at once
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()  # gone after a publish; the partial after anything else


def _checked_header(session: Session) -> dict[str, Any]:
    header = dict(session.header)
    for key in ("session_id", "expires_at", "idle_timeout_seconds", "total_timeout_seconds"):
        header.pop(key, None)
    if header.get("schema_changeset_count") is None:
        raise ExportRefused(
            "the engine's export header has no schema_changeset_count (it could not read its changelog), so the file "
            "would carry no schema marker a restore can check. Nothing was written."
        )
    if header.get("format_version") not in ENGINE_FORMAT_VERSIONS:
        raise ExportRefused(f"the engine's export header has format version {header.get('format_version')!r}, which this nx does not read. Nothing was written.")
    if not header.get("tenant_id") or not header.get("tables"):
        raise ExportRefused("the engine's export header names no tenant or no tables. Nothing was written.")
    return header


def _open_partial(dest: Path) -> tuple[Path, Any]:
    from nexus._winsec import open_private  # noqa: PLC0415 - the one place that makes a file owner-only on both platforms

    parent = dest.parent
    try:
        if not parent.exists():
            parent.mkdir(parents=True)
            if os.name != "nt":
                parent.chmod(0o700)  # the file holds the tenant's data (on Windows the folder's ACL decides)
        tmp = parent / f".{dest.name}.partial-{os.getpid()}"
        fd = open_private(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # owner-only before the first byte
    except (OSError, ValueError) as exc:
        raise cannot_write(dest, exc) from None
    return tmp, os.fdopen(fd, "wb")


def _pull(
    client: ExportClient, session: Session, header: dict[str, Any], writer: TenantFileWriter,
    on_progress: Progress | None,
) -> tuple[list[tuple[str, str, int, int]], int]:
    """Every table of the header, page by page, into *writer*. Returns the per-table summary and the count of
    sensitive documents seen. A :class:`_SessionGone` escapes to the caller, which starts over."""
    writer.write_header(header)
    tables: list[tuple[str, str, int, int]] = []
    sensitive = 0
    rows_so_far = 0
    total = len(header["tables"])
    for index, t in enumerate(header["tables"], 1):
        name = t["name"]
        names = [c["name"] for c in t["columns"]]
        sens_col = names.index("sensitive") if name == "catalog_documents" and "sensitive" in names else None
        after: str | None = None
        started = False
        table_rows = pages = 0
        while True:
            page = client.page(session, name, after, len(names))
            try:
                if not started:
                    if page.table_rows is None:
                        raise BackupFailed(f"the engine's first page of table {name} carried no row count. {_AGAIN}")
                    writer.begin_table(name, page.table_rows)
                    started, table_rows = True, page.table_rows
                writer.add_page(name, page.body, rows=page.rows, last_key=page.last_key, done=page.done, sha256=page.sha256)
            except TenantFileError as exc:
                raise BackupFailed(f"{exc}. {_AGAIN}") from None
            if sens_col is not None:
                sensitive += count_true_in_column(page.body, sens_col)
            pages += 1
            rows_so_far += page.rows
            if on_progress is not None:
                on_progress(writer.bytes_written, name, index, total, rows_so_far)
            if page.done:
                break
            if page.last_key is None or page.last_key == after or page.rows == 0:
                raise BackupFailed(f"the engine's page of table {name} does not advance (no new last key). {_AGAIN}")
            after = page.last_key
        try:
            writer.end_table()
        except TenantFileError as exc:
            raise BackupFailed(f"{exc}: the table changed under the snapshot or a page was lost. {_AGAIN}") from None
        tables.append((name, t["class"], table_rows, pages))
    return tables, sensitive
