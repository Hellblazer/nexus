# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Reds of a ``board/ci/<topic>--owner`` subspace, read the way ``nx group ack-times`` and ``nx ci ack`` both need
(RDR-252, bead nexus-igea9): the split of its rows into reds, and the resolver that names who posted an ack.

**The split** (:func:`split`). A red begins at an owner row that is neither a refresh nor a hand-on, and lasts to the
next clear row or the next red's first row. Its later owner rows continue it: a refresh keeps the red's ``episode_at``,
but a hand-off or an escalation is stamped by the engine with a NEW ``episode_at``, so ``reason`` (``handoff`` or
``escalated``) is what ties such a row to the open red. Only owner and clear rows the engine wrote count (``from=engine``,
no sender stamp, a readable body for an owner row), as in the engine's own ``isState``.

**Who posted an ack** (:class:`AckResolver`). The engine counts an ack when the row's sender-session stamp, mapped through
the session index (a delegated identity to its parent), names the current owner; it never reads ``from``. So the
resolver asks the session route first, and reads ``from`` (an identity id as written, a name through the red's own
stamped pairs and then the identity service) only for an ack that carries no sender session. Only the service's own
"unknown session" (404 ``unknown_session``), "unknown name" (404 ``unknown_name``) or a retired name (410) is a definite
"nobody". Anything else (a failed call, a 404 from an engine with no such route, a stamp that is no session id, a
delegated identity whose parent cannot be read) counts the ack in :attr:`AckResolver.unresolved` and is never
remembered, so one transient error cannot make an ack read as no ack for the rest of the run.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nexus.tuple_directory import (
    SAFE_NAME_RE,
    looks_like_agent_id,
    looks_like_identity_id,
    looks_like_session_id,
)

#: The owner-row reasons the engine posts for a hand-on; each carries a new ``episode_at`` and continues the open red.
HAND_ON_REASONS = ("handoff", "escalated")

Lookup = Callable[[str], Mapping[str, Any]]


def instant(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def json_body(row: Any) -> dict[str, Any]:
    try:
        body = json.loads(row.body or "{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


@dataclass
class Episode:
    """One red: its key (the first row's ``episode_at``), its owner rows as ``(time, row, body)`` oldest first, and the
    time of the clear row that ended it (None while open)."""

    key: str
    rows: list[tuple[datetime, Any, dict[str, Any]]]
    cleared: datetime | None = None


def _is_state(row: Any, kind: str) -> bool:
    """Whether *row* is state the engine itself wrote, as its own ``isState`` decides: ``from=engine``, a null sender
    stamp (a session wrote nothing here), and for an owner row a body that parses as a JSON object. A session can post
    ``kind=owner`` or ``kind=clear`` (the board guard refuses only ``from=engine`` from a client), and the engine ignores
    both, so neither may open, continue or end a red here."""
    if (row.dims or {}).get("from") != "engine" or getattr(row, "sender_session", None):
        return False
    if kind != "owner":
        return True
    try:
        return isinstance(json.loads(row.body or ""), dict)
    except ValueError:
        return False


def split(rows: list[Any]) -> tuple[list[Episode], list[tuple[datetime, Any]]]:
    """The reds of *rows* oldest first, and the ack rows with their times. A row with no readable time is skipped, and
    so is an owner or clear row the engine did not write (:func:`_is_state`)."""
    ordered = sorted(rows, key=lambda r: (r.created_at or "", r.id))
    reds: list[Episode] = []
    open_red: Episode | None = None
    acks: list[tuple[datetime, Any]] = []
    for row in ordered:
        at = instant(row.created_at)
        if at is None:
            continue
        kind = (row.dims or {}).get("kind")
        if kind in ("owner", "clear") and not _is_state(row, kind):
            continue
        if kind == "owner":
            body = json_body(row)
            key = body.get("episode_at") or row.created_at
            if open_red is not None and (body.get("reason") in HAND_ON_REASONS or key == open_red.key):
                open_red.rows.append((at, row, body))
            else:
                open_red = Episode(key, [(at, row, body)])
                reds.append(open_red)
        elif kind == "clear":
            if open_red is not None:
                open_red.cleared = at
                open_red = None
        elif kind == "ack":
            acks.append((at, row))
    return reds, acks


class _Unresolvable(Exception):
    """A lookup that answered, but not with what the engine would count (a delegated identity whose parent is unreadable)."""


@dataclass
class AckResolver:
    """Names the identity an ack row counts for. *session_lookup* is the identity service's session route
    (``IdentityClient.session``), *identity_lookup* its record route (``IdentityClient.show``, read for the parent of a
    delegated identity) and *alias_lookup* its name route (``IdentityClient.alias``); each takes a key and answers a
    mapping. Any may be None (a test, or an engine with no identity routes).

    :attr:`unresolved` counts the ACKS whose poster could not be named for a reason other than "the service does not
    know it": a failed or refused lookup, a stamp that is no session id, a delegated identity whose parent could not be
    read. None of those is remembered, so one transient error cannot make an ack read as no ack for the rest of the run."""

    alias_lookup: Lookup | None = None
    session_lookup: Lookup | None = None
    identity_lookup: Lookup | None = None
    #: Acks whose poster could not be resolved (see the class docstring).
    unresolved: int = 0
    _failures: int = 0
    _names: dict[str, str | None] = field(default_factory=dict)
    _sessions: dict[str, str | None] = field(default_factory=dict)

    def _ask(self, cache: dict[str, str | None], key: str, fetch: Callable[[str], str],
             final: Callable[[Any], bool]) -> str | None:
        if key in cache:
            return cache[key]
        try:
            found = fetch(key)
        except Exception as exc:  # noqa: BLE001 -- sorted below: a final answer of the service, or a failure to ask
            if not final(exc):
                self._failures += 1
                return None
            found = ""
        cache[key] = found if looks_like_identity_id(found) else None
        return cache[key]

    @staticmethod
    def _final_session(exc: Any) -> bool:
        """Only the session route's own 404 for an unknown session says nobody held it: an engine with no such route
        answers 404 to any id."""
        return getattr(exc, "status", None) == 404 and getattr(exc, "code", None) == "unknown_session"

    @staticmethod
    def _final_name(exc: Any) -> bool:
        status, code = getattr(exc, "status", None), getattr(exc, "code", None)
        return (status == 404 and code == "unknown_name") or status == 410

    def _fetch_session(self, session_id: str) -> str:
        assert self.session_lookup is not None
        answer = self.session_lookup(session_id)
        identity = str(answer.get("identity") or "")
        if answer.get("kind") != "delegated":
            return identity
        # BoardReads.identities maps a delegated identity (a `claude -p` child) to its parent; the session route names
        # the child. The parent comes with the answer, or from the child's record.
        parent = answer.get("parent")
        if not parent and self.identity_lookup is not None:
            try:
                parent = self.identity_lookup(identity).get("parent")
            except Exception as exc:  # noqa: BLE001 -- a refused or failed read is unresolved, never nobody
                raise _Unresolvable(str(exc)) from exc
        if not looks_like_identity_id(str(parent or "")):
            raise _Unresolvable(f"the parent of the delegated identity {identity} could not be read")
        return str(parent)

    def session(self, session_id: str) -> str | None:
        """The identity the session index maps *session_id* to, a delegated identity mapped to its parent. None for an
        unknown session; and None, counted as a failure, for a stamp that is no session id or an answer that could not
        be had."""
        if self.session_lookup is None:
            return None
        if not looks_like_session_id(session_id):
            self._failures += 1
            return None
        return self._ask(self._sessions, session_id, self._fetch_session, self._final_session)

    def name(self, name: str) -> str | None:
        """The identity holding *name* now. A session id, an agent id or a string that is no safe name is never put in a
        URL (``mailbox_send``'s own guard)."""
        lookup = self.alias_lookup
        if (lookup is None or not SAFE_NAME_RE.fullmatch(name or "") or looks_like_session_id(name)
                or looks_like_agent_id(name)):
            return None
        return self._ask(self._names, name, lambda n: str(lookup(n).get("identity") or ""), self._final_name)

    def poster(self, ack: Any, stamped: Mapping[str, str]) -> str | None:
        """The identity *ack* counts for. The sender-session stamp decides when the row has one and sessions can be
        asked (the engine's rule); otherwise ``from``: an identity id as written, a name through *stamped* (the red's own
        ``owner_alias``/``next_alias`` pairs), then the identity service. Counts the ack in :attr:`unresolved` when a
        lookup it needed failed."""
        failures = self._failures
        who = self._poster(ack, stamped)
        if self._failures > failures:
            self.unresolved += 1
        return who

    def _poster(self, ack: Any, stamped: Mapping[str, str]) -> str | None:
        sender = getattr(ack, "sender_session", None)
        if sender and self.session_lookup is not None:
            return self.session(str(sender))
        who = (ack.dims or {}).get("from")
        if not who:
            return None
        if looks_like_identity_id(who):
            return who
        return stamped.get(who) or self.name(who)
