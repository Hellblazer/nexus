# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Reds of a ``board/ci/<topic>--owner`` subspace, read the way ``nx group ack-times`` and ``nx ci ack`` both need
(RDR-252, bead nexus-igea9): the split of its rows into reds, and the resolver that names who posted an ack.

**The split** (:func:`split`). A red begins at an owner row that is neither a refresh nor a hand-on, and lasts to the
next clear row or the next red's first row. Its later owner rows continue it: a refresh keeps the red's ``episode_at``,
but a hand-off or an escalation is stamped by the engine with a NEW ``episode_at``, so ``reason`` (``handoff`` or
``escalated``) is what ties such a row to the open red.

**Who posted an ack** (:class:`AckResolver`). The engine counts an ack when the row's sender-session stamp, mapped through
the session index, names the current owner; it never reads ``from``. So the resolver asks the session route first, and
reads ``from`` (an identity id as written, a name through the red's own stamped pairs and then the identity service)
only for an ack that carries no sender session. A lookup the service answers with 404 or 410 is a definite "nobody";
any other failure is counted in :attr:`AckResolver.unresolved`, never remembered, so one transient error cannot make an
ack read as no ack for the rest of the run.
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


def split(rows: list[Any]) -> tuple[list[Episode], list[tuple[datetime, Any]]]:
    """The reds of *rows* oldest first, and the ack rows with their times. A row with no readable time is skipped."""
    ordered = sorted(rows, key=lambda r: (r.created_at or "", r.id))
    reds: list[Episode] = []
    open_red: Episode | None = None
    acks: list[tuple[datetime, Any]] = []
    for row in ordered:
        at = instant(row.created_at)
        if at is None:
            continue
        kind = (row.dims or {}).get("kind")
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


@dataclass
class AckResolver:
    """Names the identity an ack row counts for. *alias_lookup* is the identity service's name route
    (``IdentityClient.alias``) and *session_lookup* its session route (``IdentityClient.session``); each takes a key and
    answers a mapping with ``identity``. Either may be None (a test, or an engine with no identity routes)."""

    alias_lookup: Lookup | None = None
    session_lookup: Lookup | None = None
    #: Lookups that failed for a reason other than "the service does not know it". Never cached.
    unresolved: int = 0
    _names: dict[str, str | None] = field(default_factory=dict)
    _sessions: dict[str, str | None] = field(default_factory=dict)

    def _ask(self, cache: dict[str, str | None], lookup: Lookup, key: str) -> str | None:
        if key in cache:
            return cache[key]
        from nexus.identity.bind_client import IdentityApiError  # noqa: PLC0415 -- deferred: CLI startup cost

        try:
            found = str(lookup(key).get("identity") or "")
        except IdentityApiError as exc:
            if exc.status not in (404, 410):
                self.unresolved += 1
                return None
            found = ""
        except Exception:  # noqa: BLE001 -- a transport failure or an engine with no identity routes: counted, never cached
            self.unresolved += 1
            return None
        cache[key] = found if looks_like_identity_id(found) else None
        return cache[key]

    def session(self, session_id: str) -> str | None:
        """The identity the session index maps *session_id* to (None: not indexed, or not a session id)."""
        if self.session_lookup is None or not looks_like_session_id(session_id):
            return None
        return self._ask(self._sessions, self.session_lookup, session_id)

    def name(self, name: str) -> str | None:
        """The identity holding *name* now. A session id, an agent id or a string that is no safe name is never put in a
        URL (``mailbox_send``'s own guard)."""
        if (self.alias_lookup is None or not SAFE_NAME_RE.fullmatch(name or "") or looks_like_session_id(name)
                or looks_like_agent_id(name)):
            return None
        return self._ask(self._names, self.alias_lookup, name)

    def poster(self, ack: Any, stamped: Mapping[str, str]) -> str | None:
        """The identity *ack* counts for. The sender-session stamp decides when the row has one and sessions can be
        asked (the engine's rule); otherwise ``from``: an identity id as written, a name through *stamped* (the red's own
        ``owner_alias``/``next_alias`` pairs), then the identity service."""
        sender = getattr(ack, "sender_session", None)
        if sender and self.session_lookup is not None:
            return self.session(str(sender))
        who = (ack.dims or {}).get("from")
        if not who:
            return None
        if looks_like_identity_id(who):
            return who
        return stamped.get(who) or self.name(who)
