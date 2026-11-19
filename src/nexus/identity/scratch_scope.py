# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The identity T1 scope (RDR-253 "Identity scratch and per-identity T2"; Step 6, nexus-hczdo.1.9).

Scratch that must cross ``/clear`` and restart but is not worth a permanent record lives in the T1 scope whose
``session_id`` is ``identity:<id>``. It needs no table: the engine's ``ScratchHandler`` accepts that scope, fences its
writes (the owner's token, the current generation), leaves its reads open, and never closes it with a session. This module
is the client half: the scope a caller reads and writes, and a store bound to it.

* The scope is the caller's own identity. A delegated child (``claude -p``) reads its **parent's** scope: its writes
  are refused by the engine (the child's token is not the parent's), which is the point, and its reads are not checked.
* The store is a plain :class:`~nexus.db.http_scratch_store.HttpScratchStore` carrying the session's own minted
  session token and the identity scope as its ``session_id``; ``HttpScratchStore`` attaches the identity token itself
  for an ``identity:`` scope, so a write needs nothing else here.
* A process with no identity token holds no scope: asking for it is an error naming that, never a quiet fall back to the
  session scope (an entry written to the wrong scope is lost to the reader who looks in the right one).
"""
from __future__ import annotations

from typing import Any

IDENTITY_SCOPE_PREFIX = "identity:"


class IdentityScopeError(RuntimeError):
    """The caller holds no identity, so it has no identity scratch scope."""


def scope_owner(client: Any, *, identity: str | None = None) -> str:
    """The identity whose scratch the caller works in: its own, or the parent's for a delegated child.

    *client* answers ``show(identity)`` (an :class:`~nexus.identity.bind_client.IdentityClient`). *identity* overrides
    the caller's own (tests). Raises :class:`IdentityScopeError` when no identity is bound to this process."""
    if identity is None:
        from nexus.identity import token_header  # noqa: PLC0415 — deferred: leaf import cost

        record = token_header.current()
        if record is None:
            raise IdentityScopeError(
                "no identity is bound to this process, so it has no identity scratch (it needs a Claude session "
                "with a bound identity, or a delegated child of one)")
        identity = record.identity
    view = client.show(identity)
    parent = view.get("parent") if view.get("kind") == "delegated" else None
    return str(parent or identity)


def scope_id(owner: str) -> str:
    """The T1 ``session_id`` of *owner*'s identity scope."""
    return f"{IDENTITY_SCOPE_PREFIX}{owner}"


def identity_scratch(base: Any = None, *, client: Any = None, identity: str | None = None) -> Any:
    """A T1-shaped store bound to the caller's identity scope.

    *base* is the session's own T1 store (``get_t1_database()`` by default): the identity store reuses its minted session
    token and endpoint, so the engine's token gate admits the call. *client* is the identity client that finds the
    delegated parent (built from the config when None, and closed afterwards)."""
    from nexus.db.http_scratch_store import HttpScratchStore  # noqa: PLC0415 — deferred: http store must not load on every CLI start

    owned = client is None
    if owned:
        from nexus.identity.bind_client import IdentityClient  # noqa: PLC0415

        client = IdentityClient.from_config()
    try:
        owner = scope_owner(client, identity=identity)
    finally:
        if owned:
            client.close()
    if base is None:
        from nexus.db.t1 import get_t1_database  # noqa: PLC0415

        base = get_t1_database()
    inner = getattr(base, "_store", base)               # the CLI-dedicated wrapper holds the real store
    token = getattr(inner, "_session_token", None)
    return HttpScratchStore(session_id=scope_id(owner), _session_token=token or None)
