# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The HTTP calls to the engine's ``/v1/identity`` routes (RDR-253 Step 1, ``IdentityHandler``).

One small synchronous client shared by the SessionStart hook (the tenant credential), the MCP server (the data
credential) and the CLI. It attaches an identity token only where it is handed one: the bind that mints one is the
call that cannot have one, every other call here is a read or an unfenced write, and a data credential makes, promotes
and delegates seats through the caller's token (RDR-253 option 2: ``create_seat``, ``promote``, ``create_delegated``
take ``token=``; the callers pass it when :attr:`IdentityClient.scope` is data; so does a data credential's ``bind`` of
``refresh`` or ``fork``, engine 0.1.198). Fenced writes go through the T2
stores, which attach the token (:mod:`nexus.identity.token_header`).

Which routes each credential may call is the engine's decision (``IdentityHandler.admit``), not this module's: a
refusal comes back as :class:`IdentityApiError` with the engine's status and error code. The calls the MCP
server's pending bind and renewal make (alias, session, bindings by host and its history mode, ephemeral create,
bind with start/resume/fork/refresh/end, renew) are all admitted to the data credential; ``clear``, ``release``,
``takeover`` and ``gone`` are the tenant credential's.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from nexus.identity import token_contract
from nexus.identity.token_file import TokenFile

DEFAULT_TIMEOUT_S: float = 10.0
DEFAULT_TENANT: str = "default"

#: The credential's scope as this client sees it: a static service token is the tenant credential; a box that mints
#: short-lived data tokens (cloud mode) holds the data credential.
SCOPE_TENANT: str = "tenant"
SCOPE_DATA: str = "data"

#: The refusal an engine older than 0.1.197 gives a data credential on a seat route (RDR-253 option 2). It is the whole
#: of what the client can tell about the engine, as the ``/version`` flag is not forwarded by the edge yet.
OLD_ENGINE_SEAT_REFUSAL: str = "a 'data' credential may not call"
#: The one line every seat caller says for it.
SEATS_NEED_ENGINE: str = "seats need engine 0.1.197 in cloud mode"

#: The bind events a data credential sends only with the identity's own live token (engine 0.1.198, RDR-253: a refresh
#: or fork mints or re-keys a token, so the fence's check applies). The others stay as they were.
TOKEN_BIND_EVENTS: frozenset[str] = frozenset({"refresh", "fork"})

#: Seats one identity may create through a data credential (RDR-253, Sam 2026-11-19: cap 5). The engine's 409
#: ``seat_limit`` names its own ``cap``, which wins; this is only the number the line says when it does not.
SEAT_LIMIT: int = 5


class IdentityApiError(Exception):
    """The engine refused an identity call. *code* is its ``error`` field (``held``, ``generation_conflict``,
    ``unknown_name``, ...); *body* is the whole answer, which carries the holder on a ``held``."""

    def __init__(self, status: int, code: str | None, message: str, body: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"identity engine {status} {code or ''}: {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.body: Mapping[str, Any] = body or {}


class IdentityUnavailableError(Exception):
    """The engine could not be reached, or its endpoint or credential does not resolve."""


def is_old_engine_seat_refusal(exc: IdentityApiError) -> bool:
    """Whether *exc* is an engine without RDR-253 option 2 refusing a data credential a seat route."""
    return exc.status == 403 and exc.message.startswith(OLD_ENGINE_SEAT_REFUSAL)


#: What a seat call's caller says of its own token when the engine refuses it (:func:`seat_refusal_message`).
OWN_TOKEN = "this process's token"
#: The advice for a seat call that sent no token, for a caller that may be the session or a script naming a session's file.
NO_TOKEN_ADVICE = ("no identity token was sent, and in cloud mode a seat is made, promoted or delegated only through one: "
                   "run it inside the session that holds the identity, or name that session's token file with --token-file")

#: What a caller adds to the 401 line, by who it is: the session itself, or a script naming a session's token file. A
#: script and a dispatch are not run from a session, so they never say to.
OWN_TOKEN_401_ADVICE = "a session's server renews its token while it runs, so run this from a live session (or restart this one)"
FILE_TOKEN_401_ADVICE = "the session that wrote that file renews the token while it runs, so that session must be live"
#: What a caller adds to the ``seat_limit`` line. The cap is per identity for its whole life: no route removes a seat (purge
#: keeps it, rename keeps its creator), so the way out is to reuse a seat, or make a new one with the uncapped tenant
#: credential from a local-mode or operator shell.
SEAT_LIMIT_ADVICE = ("reuse one of them, or make a new one from a local-mode or operator shell "
                     "(the tenant credential has no cap)")
#: One line per engine refusal of a seat call (RDR-253 option 2) that needs nothing from the caller; the codes that name
#: the caller's token, the identity or the token's owner are built in :func:`seat_refusal_message`.
_SEAT_REFUSALS = {
    "identity_not_held": "no process holds this identity now, so its token cannot act for it",
    "not_ephemeral": "that identity is not an ephemeral one: only an ephemeral identity is promoted to a seat",
    "forbidden": "this credential may not make, promote or delegate seats",
}


def seat_refusal_message(exc: IdentityApiError, identity: str | None = None, *, token_source: str = OWN_TOKEN,
                         no_token: str = NO_TOKEN_ADVICE, unauthorized: str = OWN_TOKEN_401_ADVICE,
                         seat_limit: str = SEAT_LIMIT_ADVICE) -> str:
    """One line for an engine refusal of a seat call (a data credential's create, promote or delegate). *token_source*
    says whose token was sent (``this process's token``, or ``the token in <file>``), *no_token* is the advice for a
    call that sent none, *unauthorized* the advice after a 401 and *seat_limit* the way out of ``409 seat_limit``, so
    each caller words them for itself. Never contains a token value.

    Every 401 is the one line: the engine's AuthFilter answers all of them ``{"error": "unauthorized"}`` (it logs the
    reason, an expired or another tenant's identity token, and does not send it), so the client cannot tell the data
    credential from the identity token and does not guess."""
    if is_old_engine_seat_refusal(exc):
        return SEATS_NEED_ENGINE
    code = exc.code
    if exc.status == 401:
        return f"the credential or {token_source} was not accepted (expired, unknown or another tenant's)" + (
            f": {unauthorized}" if unauthorized else "")
    if code == token_contract.TOKEN_REQUIRED_CODE:
        return no_token
    if code == "not_owner":
        return (f"the identity token names {exc.body.get('token_identity') or 'another identity'}, not "
                f"{identity or 'the one asked for'}: a session acts only for its own identity")
    if code == "unknown_identity":
        return f"the engine has no identity {identity or ''}".strip()
    if code == "generation_superseded":
        return f"another process holds this identity now, and {token_source} is for an older generation"
    if code == "session_held":
        return "the engine already indexes that session id under another identity, so a delegated identity cannot be bound to it"
    if code == "seat_limit":
        named = exc.body.get("cap")
        cap = named if isinstance(named, int) and not isinstance(named, bool) else SEAT_LIMIT
        named_id = exc.body.get("identity")
        who = named_id if isinstance(named_id, str) and named_id else identity   # a create names none of its own: the body does
        return f"{who or 'this identity'} has created its {cap} seats, the most a session's credential may: {seat_limit}"
    if code == "bad_created_by":
        return "a tenant credential may give created_by only as sam or restart"
    if code in _SEAT_REFUSALS:
        return _SEAT_REFUSALS[code]
    return f"{code or exc.status}: {exc.message}"


def evidence_body(
    *, session_id: str | None = None, host: str | None = None, pid: int | None = None, started_at: int | None = None,
    proc_start: str | None = None, pane: str | None = None, client_version: str | None = None,
    lease: str | None = None, prior_session_id: str | None = None, holder_gone: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A bind's evidence in the engine's field names, with every unset field left out."""
    fields: dict[str, Any] = {
        "session_id": session_id, "host": host, "pid": pid, "started_at": started_at, "proc_start": proc_start,
        "pane": pane, "client_version": client_version, "lease": lease, "prior_session_id": prior_session_id,
        "holder_gone": dict(holder_gone) if holder_gone else None,
    }
    return {k: v for k, v in fields.items() if v is not None}


def _seg(value: str) -> str:
    """One URL path segment: a ``/``, space or ``?`` in an id or a group name stays inside its segment."""
    return quote(value, safe="")


class IdentityClient:
    """``/v1/identity`` over httpx. *bearer* is the credential, or a callable resolved on every request so a data
    token that refreshes is never baked in."""

    def __init__(self, base_url: str, bearer: str | Callable[[], str], *, tenant: str = DEFAULT_TENANT,
                 timeout: float = DEFAULT_TIMEOUT_S, http: httpx.Client | None = None, scope: str = SCOPE_TENANT) -> None:
        self._scope = scope
        self._base = base_url.rstrip("/")
        self._bearer = bearer if callable(bearer) else (lambda: bearer)
        self._tenant = tenant
        if http is None:
            from nexus.db.t2.shared_ssl import get_shared_ssl_context  # noqa: PLC0415 — deferred: the db package import is heavy for this module

            # The process-wide SSL context: the MCP identity lease builds one of these periodically, and a
            # per-client context re-reads the CA bundle from an install generation that may be gone (2026-11-21).
            http = httpx.Client(timeout=timeout, verify=get_shared_ssl_context())
        self._http = http

    @classmethod
    def from_config(cls, *, timeout: float = DEFAULT_TIMEOUT_S) -> "IdentityClient":
        """The client the hook, the MCP server and the CLI use: the endpoint and the credential ``nx`` already
        resolves (``NX_SERVICE_URL``/``NX_SERVICE_TOKEN``, the config file, the supervisor's lease), and, on a box
        that holds only a mint credential, the data token the stores mint. Raises :class:`IdentityUnavailableError`
        when none resolves."""
        from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred: hook import cost

        try:
            base_url, token = resolve_service_endpoint()
        except Exception as exc:  # noqa: BLE001 — every resolution failure is one fact: no engine to bind against
            raise IdentityUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        if token:
            return cls(base_url, token, timeout=timeout)

        def mint() -> str:
            from nexus.db.data_token import get_data_token_manager  # noqa: PLC0415 — deferred, as above

            return get_data_token_manager().bearer_for(base_url, DEFAULT_TENANT) or ""

        return cls(base_url, mint, timeout=timeout, scope=SCOPE_DATA)

    def close(self) -> None:
        self._http.close()

    # The connection a sibling client over other routes (``nexus.groups``) reuses: same engine, same credential,
    # same httpx client. Read-only views, so the sibling never owns or closes them.

    @property
    def base_url(self) -> str:
        return self._base

    @property
    def scope(self) -> str:
        """:data:`SCOPE_DATA` when the bearer is a minted data token (cloud mode), else :data:`SCOPE_TENANT`. A data
        credential makes, promotes and delegates seats only through the caller's identity token (RDR-253 option 2)."""
        return self._scope

    @property
    def tenant(self) -> str:
        return self._tenant

    @property
    def http(self) -> httpx.Client:
        return self._http

    def credential(self) -> str:
        """The bearer for this call: a fixed token, or a freshly resolved one."""
        return self._bearer()

    # ── transport ───────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None,
                 fenced: bool = False, token: str | TokenFile | None = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._bearer()}", "X-Nexus-Tenant": self._tenant}
        headers.update(token_contract.headers(token.token if isinstance(token, TokenFile) else token))
        if fenced:
            # RDR-253 Step 6: a membership write is fenced by the engine (ownership and generation). The token is the
            # caller's own; with none, the engine's 403 identity_token_required is the honest answer.
            from nexus.identity.token_header import identity_headers  # noqa: PLC0415 — deferred: hook import cost

            headers.update(identity_headers())
        try:
            resp = self._http.request(method, f"{self._base}/v1/identity{path}", json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise IdentityUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = None
        if resp.status_code >= 400:
            body = body if isinstance(body, dict) else {}
            if fenced:
                from nexus.identity.token_header import superseded_from_body  # noqa: PLC0415 — deferred, as above

                if (typed := superseded_from_body(resp.status_code, body)) is not None:
                    raise typed
            message = body.get("message") or body.get("error") or resp.reason_phrase
            raise IdentityApiError(resp.status_code, body.get("error") if isinstance(body.get("error"), str) else None,
                                   str(message), body)
        if not isinstance(body, dict):  # a 2xx that is not the engine's JSON object (a proxy page) is not an answer
            raise IdentityApiError(resp.status_code, "bad_response", "the engine's answer was not a JSON object")
        return body

    # ── reads ───────────────────────────────────────────────────────────────

    def alias(self, name: str) -> dict[str, Any]:
        """Resolve a seat name: ``identity``, ``state`` (live or retired), ``generation``, ``identity_state``."""
        return self._request("GET", f"/alias/{name}")

    def show(self, identity: str) -> dict[str, Any]:
        """The identity as of now: ``state``, ``generation``, ``lease``, ``holder``, ``reserved_until``."""
        return self._request("GET", f"/{identity}")

    def session(self, session_id: str) -> dict[str, Any]:
        """The session index: ``identity``, ``generation``, ``current_generation``, ``state``; 404 when unindexed."""
        return self._request("GET", f"/sessions/{session_id}")

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        """The live and lapsed holders on *host* (optionally one pid), each the identity view with its ``holder``."""
        params: dict[str, Any] = {"host": host}
        if pid is not None:
            params["pid"] = pid
        return list(self._request("GET", "/bindings", params=params).get("bindings") or [])

    def history(self, host: str, pid: int, proc_start: str) -> list[dict[str, Any]]:
        """Every identity that ever held the process (*host*, *pid*, *proc_start*), most recent first: the identity
        view plus ``binding`` (its newest row for that process). ``proc_start`` is required by the engine."""
        params = {"host": host, "pid": pid, "proc_start": proc_start, "history": "1"}
        return list(self._request("GET", "/bindings", params=params).get("bindings") or [])

    def memberships(self, identity: str) -> list[dict[str, Any]]:
        """The groups *identity* belongs to, in join order: ``{identity, group, role, joined_at}`` each (RDR-253 Step 1,
        ``GET /v1/identity/<id>/memberships``). 404 ``unknown_identity`` for an identity the engine has no record of.
        The one reader of this route (``nexus.groups`` delegates here). An answer with no ``memberships`` list is
        :class:`IdentityApiError` ``bad_response``, never "no memberships"."""
        rows = self._request("GET", f"/{quote(identity, safe='')}/memberships").get("memberships")
        if not isinstance(rows, list):
            raise IdentityApiError(200, "bad_response", "the membership answer carries no memberships list")
        return rows

    def log(self, identity: str) -> list[dict[str, Any]]:
        """The identity's binding log, oldest first (``nx identity show`` prints its tail)."""
        return list(self._request("GET", f"/{identity}/bindings").get("bindings") or [])

    # ── writes ──────────────────────────────────────────────────────────────

    def create_seat(self, name: str, *, created_by: str = "sam", display_name: str | None = None,
                    token: str | TokenFile | None = None) -> dict[str, Any]:
        """A seat: unheld, generation 0, with a live alias. The tenant credential makes one outright; the data credential
        only with *token*, a live identity token of the caller, which the engine stamps into ``created_by``. The caller
        of this is ``nx identity create`` (Step 4); the policy tests use it to stage seats."""
        body: dict[str, Any] = {"kind": "seat", "name": name, "created_by": created_by}
        if display_name:
            body["display_name"] = display_name
        return self._request("POST", "", json=body, token=token)

    def create_ephemeral(self, *, display_name: str | None, created_by: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """A new ephemeral identity already bound to the caller (generation 1, event ``start``)."""
        body: dict[str, Any] = {"kind": "ephemeral", "created_by": created_by, "bind": dict(evidence)}
        if display_name:
            body["display_name"] = display_name
        return self._request("POST", "", json=body)

    def create_delegated(self, parent: str, *, display_name: str | None, evidence: Mapping[str, Any],
                         token: str | TokenFile | None = None) -> dict[str, Any]:
        """A delegated identity under *parent* for a ``claude -p`` child (tenant credential, or the data credential with
        *token*, which must name *parent*): generation 1, live,
        one ``start`` row naming the dispatcher's process (*evidence*: host, pid, started_at, proc_start). The answer
        carries ``identity_token``. Step 4: the ``nx`` operator dispatcher is its caller."""
        body: dict[str, Any] = {"parent": parent, "bind": dict(evidence)}
        if display_name:
            body["display_name"] = display_name
        return self._request("POST", "/delegated", json=body, token=token)

    def promote(self, identity: str, name: str, *, token: str | TokenFile | None = None) -> dict[str, Any]:
        """Make an ephemeral identity a seat named *name* (tenant credential, or the data credential with *token*,
        which must name *identity*)."""
        return self._request("POST", f"/{identity}/promote", json={"name": name}, token=token)

    def rename(self, old: str, new: str) -> dict[str, Any]:
        """Retire seat *old*'s alias (a name or an id) and give it *new*, retitling its board entry (tenant credential only)."""
        return self._request("POST", "/rename", json={"old": old, "new": new})

    def purge(self, identity: str) -> dict[str, Any]:
        """The operator purge of an identity's fenced state (tenant credential only); the engine refuses a live,
        unheld or plainly lapsed identity and audit-logs the rest."""
        return self._request("POST", f"/{identity}/purge", json={"operator": True})

    def bind(self, identity: str, event: str, expected_generation: int, evidence: Mapping[str, Any] | None = None, *,
             token: str | TokenFile | None = None) -> dict[str, Any]:
        """The compare-and-set. The answer is the identity view plus ``event``, ``generation_changed``, ``noop``,
        and, from Step 2, ``identity_token``.

        A data credential's ``refresh`` and ``fork`` carry the identity's own live token (engine 0.1.198): exactly
        *token*, which the caller reads for the identity it binds (the lease its own, the hook its process's token
        file, a delegated child its own). Never the nearest Claude ancestor's file: that may be another identity's (a
        delegated child, a nested launch, a test under a live session). With none the engine answers 403
        ``identity_token_required``, the honest refusal. No other event sends one, and the tenant credential never
        does: it is the trust boundary for a request that names no process, and an identity token the engine cannot
        resolve is a 401 on every route it rides."""
        body: dict[str, Any] = {"event": event, "expected_generation": expected_generation}
        body.update(evidence or {})
        if event not in TOKEN_BIND_EVENTS or self._scope != SCOPE_DATA:
            token = None
        return self._request("POST", f"/{identity}/bind", json=body, token=token)

    def gone(self, identity: str, expected_generation: int, holder_gone: Mapping[str, Any]) -> dict[str, Any]:
        """Record that the holder is gone (tenant credential only). ``recorded`` is false for a repeat."""
        return self._request("POST", f"/{identity}/gone",
                             json={"expected_generation": expected_generation, "holder_gone": dict(holder_gone)})

    def renew(self, identity: str, generation: int, client_version: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"generation": generation}
        if client_version:
            body["client_version"] = client_version
        return self._request("POST", f"/{identity}/renew", json=body)

    # ── work and memberships (RDR-253 Step 6) ───────────────────────────────

    def put_work(self, identity: str, kind: str, ref: str, state: str = "running") -> dict[str, Any]:
        """Register work *identity* owns, or change its state in place (a label, not fenced: a lab run on another host
        has no token file). ``409 work_limit`` at 200 rows."""
        return self._request("POST", f"/{_seg(identity)}/work", json={"kind": kind, "ref": ref, "state": state})

    def remove_work(self, identity: str, kind: str, ref: str) -> dict[str, Any]:
        """Forget the work; the answer's ``removed`` is 0 for work that was not there."""
        return self._request("DELETE", f"/{_seg(identity)}/work", params={"kind": kind, "ref": ref})

    def work_of(self, identity: str) -> list[dict[str, Any]]:
        """Every work row of *identity* (``kind``, ``ref``, ``state``, ``updated_at``), by kind and ref."""
        return list(self._request("GET", f"/{_seg(identity)}/work").get("work") or [])

    def work_owners(self, kind: str, ref: str) -> list[dict[str, Any]]:
        """Every identity that registered the resource (*kind*, *ref*), one row each, with its state."""
        return list(self._request("GET", "/work", params={"kind": kind, "ref": ref}).get("owners") or [])

    def join_group(self, identity: str, group: str, role: str = "member") -> dict[str, Any]:
        """Join *group* as ``leader`` or ``member``, or change the role (the join time stays). Fenced: the caller's
        token must be *identity*'s."""
        return self._request("POST", f"/{_seg(identity)}/memberships", json={"group": group, "role": role}, fenced=True)

    def leave_group(self, identity: str, group: str) -> dict[str, Any]:
        """Leave *group*; leaving one never joined removes nothing (``removed`` 0). Fenced."""
        return self._request("DELETE", f"/{_seg(identity)}/memberships/{_seg(group)}", fenced=True)

    def roster(self, group: str) -> list[dict[str, Any]]:
        """The members of *group* in join order."""
        return list(self._request("GET", f"/groups/{_seg(group)}").get("members") or [])
