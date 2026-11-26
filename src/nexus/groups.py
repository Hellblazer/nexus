# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The HTTP client for the engine's group routes (RDR-252 Step 4): ``/v1/group`` and the membership routes of
``/v1/identity`` (RDR-253).

* ``POST /v1/group`` forms a group (or re-forms a disbanded name), ``GET /v1/group/<name>`` reads the record,
  ``POST .../set``, ``.../disband`` and ``.../override`` change it.
* ``POST /v1/identity/<id>/memberships`` joins, ``DELETE .../memberships/<group>`` leaves, and the two ``GET`` routes
  list an identity's groups and a group's roster in join order.

**Which credential, and the identity token.** Every call sends the bearer ``nx`` already resolves (the tenant or the
data credential; the engine decides which routes each may call). A group write (form, set, disband) and a membership
write (join, leave) are fenced by the identity of the LEADER or of the member: they carry this process's identity token
(:func:`nexus.identity.token_header.identity_headers`, the same one a threads write sends), always, because the engine
refuses a group write with no token even to an unleased leader. On a 401 with a token attached the token file is read
once more and the request resent once if it now holds a different token (:func:`retry_headers_after_401`). The operator
override carries no token: it is the tenant credential's, for a leader that cannot write. Reads never carry one.

**The reader for the aura load stage.** :func:`read_group` (and :meth:`GroupsClient.record`) returns the group record as
a :class:`GroupRecord`, or ``None`` when there is no record: a group with no record is not loaded from (RDR-252 "Load by
membership": no record reads as disbanded). :meth:`GroupsClient.memberships` is the one read of an identity's groups.
Any other failure raises :class:`GroupsError`, so a load stage can tell "no group" from "could not ask".
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from nexus.identity import token_header
from nexus.identity.bind_client import DEFAULT_TENANT, DEFAULT_TIMEOUT_S, IdentityClient, IdentityUnavailableError

#: A group name, and a base topic name in ``serves`` (no ``--``): the engine's address-segment pattern.
GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDENTITY_ID = re.compile(r"^i[0-9]+$")


class GroupsError(Exception):
    """The engine refused a group call. *code* is its ``error`` field (``group_exists``, ``bad_leader``,
    ``leader_cannot_leave``, ``unknown_group``, ...); *body* is the whole answer."""

    def __init__(self, status: int, code: str | None, message: str, body: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code or status}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.body: Mapping[str, Any] = body or {}


class GroupsUnavailableError(Exception):
    """The engine could not be reached, or its endpoint or credential does not resolve."""


class NoIdentityError(Exception):
    """This process holds no identity token, so it cannot write a group or a membership."""


@dataclass(frozen=True)
class GroupRecord:
    """A group record as ``GET /v1/group/<name>`` returns it. ``live`` is the engine's verdict on the whole rule:
    false once ``ended_at`` is set or ``ends`` has passed; None when the answer carries none (an absent key is no verdict,
    never "disbanded")."""

    name: str
    leader: str
    purpose: str | None
    requested_by: str | None
    formed_at: str | None
    ends: str | None
    ended_at: str | None
    ack_deadline: int
    ack_deadline_by_topic: dict[str, int]
    serves: tuple[str, ...]
    live: bool | None

    @classmethod
    def from_wire(cls, body: Mapping[str, Any]) -> GroupRecord:
        """The record from the engine's answer. An answer the record cannot be built from (a missing ``name`` or
        ``leader``, a deadline that is not a number, a map that is not a map) is a :class:`GroupsError` with code
        ``bad_response``, never a KeyError, ValueError or TypeError for a verb to trace-back on."""
        try:
            return cls(
                name=str(body["name"]), leader=str(body["leader"]), purpose=body.get("purpose"),
                requested_by=body.get("requested_by"), formed_at=body.get("formed_at"), ends=body.get("ends"),
                ended_at=body.get("ended_at"), ack_deadline=int(body.get("ack_deadline") or 0),
                ack_deadline_by_topic={str(k): int(v) for k, v in (body.get("ack_deadline_by_topic") or {}).items()},
                serves=tuple(body.get("serves") or ()), live=None if body.get("live") is None else bool(body["live"]))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise GroupsError(200, "bad_response", f"the engine's group record is unreadable ({type(exc).__name__}: {exc})") from exc


def _rows_naming(rows: list[Any], key: str, what: str, *, also: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """*rows* when every one is a map whose *key* and every *also* field are text, else ``bad_response``: a verb indexes or
    formats those fields, and a malformed answer is one line and exit status 1, never a KeyError or a TypeError."""
    for field in (key, *also):
        if not all(isinstance(row, dict) and isinstance(row.get(field), str) for row in rows):
            raise GroupsError(200, "bad_response", f"a {what} row from the engine names no {field}")
    return rows


def own_identity_id() -> str:
    """The identity this process holds: the one its token file names (a Bash child reads its Claude ancestor's)."""
    record = token_header.current()
    if record is None:
        raise NoIdentityError(
            "this process holds no identity token (no bound Claude session ancestor, or its token has expired): "
            "a group write needs the identity of its leader")
    return record.identity


def _unavailable_on_failure(bearer: Callable[[], str]) -> Callable[[], str]:
    """*bearer* with every failure to get the credential (a data-token mint refused or unreachable, an endpoint or
    token that does not resolve) raised as :class:`GroupsUnavailableError`: the engine could not be asked, the same
    fact as a refused connection. The identity client built over it passes the error through unchanged."""
    def get() -> str:
        try:
            return bearer()
        except GroupsUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 -- DataTokenMintError and its siblings: one fact, no credential
            raise GroupsUnavailableError(f"no credential for the engine ({type(exc).__name__}: {exc})") from exc

    return get


class GroupsClient:
    """``/v1/group`` and the membership routes over httpx. *bearer* is the credential, or a callable resolved on every
    request. *identity_headers* and *retry_headers* are the token seams (:mod:`nexus.identity.token_header`'s by
    default); a test passes fixed ones."""

    def __init__(
        self, base_url: str, bearer: str | Callable[[], str], *, tenant: str = DEFAULT_TENANT,
        timeout: float = DEFAULT_TIMEOUT_S, http: httpx.Client | None = None,
        identity_headers: Callable[[], dict[str, str]] = token_header.identity_headers,
        retry_headers: Callable[[Mapping[str, str], str], dict[str, str] | None] = token_header.retry_headers_after_401,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._bearer = _unavailable_on_failure(bearer if callable(bearer) else (lambda: bearer))
        self._tenant = tenant
        if http is None:
            from nexus.db.t2.shared_ssl import get_shared_ssl_context  # noqa: PLC0415 — deferred: the db package import is heavy for this module

            # The process-wide SSL context (see IdentityClient.__init__).
            http = httpx.Client(timeout=timeout, verify=get_shared_ssl_context())
        self._http = http
        self._identity_headers = identity_headers
        self._retry_headers = retry_headers
        self._identities = IdentityClient(self._base, self._bearer, tenant=tenant, http=self._http)

    @classmethod
    def from_config(cls, *, timeout: float = DEFAULT_TIMEOUT_S) -> GroupsClient:
        """The client the CLI uses: the endpoint and credential ``nx`` already resolves. Raises
        :class:`GroupsUnavailableError` when none resolves."""
        try:
            ident = IdentityClient.from_config(timeout=timeout)
        except IdentityUnavailableError as exc:
            raise GroupsUnavailableError(str(exc)) from exc
        return cls(ident.base_url, ident.credential, tenant=ident.tenant, http=ident.http)

    def close(self) -> None:
        self._http.close()

    # ── transport ───────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, *, json: Any = None, fenced: bool = False) -> dict[str, Any]:
        url = f"{self._base}{path}"
        auth = {"Authorization": f"Bearer {self._bearer()}", "X-Nexus-Tenant": self._tenant}
        sent = {**auth, **(self._identity_headers() if fenced else {})}
        try:
            resp = self._http.request(method, url, json=json, headers=sent)
            if resp.status_code == 401 and fenced and (fresh := self._retry_headers(sent, path)):
                resp = self._http.request(method, url, json=json, headers=fresh)
        except httpx.HTTPError as exc:
            raise GroupsUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            body = None
        if resp.status_code >= 400:
            body = body if isinstance(body, dict) else {}
            if (stop := token_header.superseded_from_body(resp.status_code, body)) is not None:
                raise stop
            code = body.get("error") if isinstance(body.get("error"), str) else None
            raise GroupsError(resp.status_code, code, str(body.get("message") or body.get("error") or resp.reason_phrase), body)
        if not isinstance(body, dict):  # a 2xx that is not the engine's JSON object (a proxy page) is not an answer
            raise GroupsError(resp.status_code, "bad_response", "the engine's answer was not a JSON object")
        return body

    # ── the group record ────────────────────────────────────────────────────

    def form(
        self, name: str, leader: str, *, serves: list[str] | None = None, purpose: str | None = None,
        requested_by: str | None = None, ends: str | None = None, ack_deadline: int | None = None,
        ack_deadline_by_topic: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        """Form the group, or re-form a disbanded name; *leader* is the writer's own identity (a live, leased seat)."""
        fields = {"serves": serves, "purpose": purpose, "requested_by": requested_by, "ends": ends,
                  "ack_deadline": ack_deadline, "ack_deadline_by_topic": dict(ack_deadline_by_topic) if ack_deadline_by_topic else None}
        body = {"name": name, "leader": leader, **{k: v for k, v in fields.items() if v is not None}}
        return self._request("POST", "/v1/group", json=body, fenced=True)

    def read(self, name: str) -> dict[str, Any]:
        """The record as the engine sends it; :class:`GroupsError` ``unknown_group`` (404) when there is none."""
        return self._request("GET", f"/v1/group/{quote(name, safe='')}")

    def record(self, name: str) -> GroupRecord | None:
        """The record, or None when the name has no record (a 404 ``unknown_group``). Any other failure raises."""
        try:
            return GroupRecord.from_wire(self.read(name))
        except GroupsError as exc:
            if exc.status == 404 and exc.code == "unknown_group":
                return None
            raise

    def set(self, name: str, **fields: Any) -> dict[str, Any]:
        """Change the record in place: ``leader``, ``ends``, ``ack_deadline``, ``ack_deadline_by_topic`` (the whole
        map), ``serves``. Only the fields given are sent."""
        return self._request("POST", f"/v1/group/{quote(name, safe='')}/set", json=fields, fenced=True)

    def disband(self, name: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/group/{quote(name, safe='')}/disband", json={}, fenced=True)

    def override(self, name: str, *, leader: str | None = None, disband: bool = False) -> dict[str, Any]:
        """The operator override: a new leader, or the end of the group, while the stored leader cannot write. The
        tenant credential only; no identity token is sent."""
        body: dict[str, Any] = {"ended_at": True} if disband else {"leader": leader}
        return self._request("POST", f"/v1/group/{quote(name, safe='')}/override", json=body)

    # ── memberships ─────────────────────────────────────────────────────────

    def memberships(self, identity: str) -> list[dict[str, Any]]:
        """The groups of *identity*, in join order: ``{identity, group, role, joined_at}`` each."""
        rows = self._call_identity(lambda: self._identities.memberships(identity))  # the one reader of the route
        return _rows_naming(rows, "group", "membership")

    def roster(self, group: str) -> list[dict[str, Any]]:
        """The members of *group* in join order. An answer with no ``members`` list is an error, not an empty roster."""
        rows = self._request("GET", f"/v1/identity/groups/{quote(group, safe='')}").get("members")
        if not isinstance(rows, list):
            raise GroupsError(200, "bad_response", "the roster answer carries no members list")
        return _rows_naming(rows, "identity", "roster", also=("role",))

    def join(self, identity: str, group: str, role: str = "member") -> dict[str, Any]:
        """Join *group* as *identity* (the caller's own: the engine checks the token against it), or change the role."""
        return self._request("POST", f"/v1/identity/{quote(identity, safe='')}/memberships",
                             json={"group": group, "role": role}, fenced=True)

    def leave(self, identity: str, group: str) -> dict[str, Any]:
        """Leave *group*; ``removed`` is 0 for a group never joined. The engine refuses the current leader."""
        return self._request("DELETE", f"/v1/identity/{quote(identity, safe='')}/memberships/{quote(group, safe='')}", fenced=True)

    # ── identities, read-only ───────────────────────────────────────────────

    def identity(self, identity: str) -> dict[str, Any]:
        """The identity view: ``kind``, ``name`` (its alias), ``state``, ``lease``, ``liveness``, ``generation``."""
        return self._call_identity(lambda: self._identities.show(identity))

    def resolve(self, ref: str) -> str:
        """An identity id from a seat name or an id (an id is returned as given, unchecked)."""
        if _IDENTITY_ID.match(ref):
            return ref
        return str(self._call_identity(lambda: self._identities.alias(ref))["identity"])

    @staticmethod
    def _call_identity(call: Callable[[], Any]) -> Any:
        from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415 — only the failure path needs it

        try:
            return call()
        except IdentityApiError as exc:
            raise GroupsError(exc.status, exc.code, exc.message, exc.body) from exc
        except IdentityUnavailableError as exc:
            raise GroupsUnavailableError(str(exc)) from exc


def read_group(client: GroupsClient, name: str) -> GroupRecord | None:
    """The group record for *name*, or None when there is no record. The aura load stage's reader (RDR-252 Step 4)."""
    return client.record(name)


def state_word(view: Mapping[str, Any]) -> str:
    """An identity's RDR-253 state as ``nx group list`` shows it: ``unheld``, ``live``, ``lapsed``, ``ended``, or
    ``unleased`` for a live binding whose MCP server cannot renew (the engine cannot say whether its holder is alive)."""
    state = str(view.get("state") or "unknown")
    return "unleased" if state == "live" and view.get("lease") == "unleased" else state
